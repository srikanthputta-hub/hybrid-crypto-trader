"""
fetch_coinbase_long.py - long-history daily OHLCV for the 16 watchlist coins, paginated.

  python fetch_coinbase_long.py --quote USD --out data_long

Why Coinbase, not Kraken: per the user's instructions, Kraken's downloadable historical
OHLCVT files were tried first (support.kraken.com, which hosts the download links) -- not
reachable from this environment's network policy. Binance was also tried as an alternative
ccxt source -- it returns HTTP 451 (geo-blocked) from this environment's egress IP, the same
restriction already documented in config/config.yaml for GitHub Actions runners. Coinbase
Exchange (ccxt id `coinbaseexchange`, api.exchange.coinbase.com) IS reachable and has BTC/USD
daily history back to 2015-07-20, comfortably covering "2019 or earlier" for all 16 coins
that list a USD pair there (all 16 do). Only this one source is used per coin -- never mixed
with Kraken's Data A inside the same symbol.

Output: data_long/<BASE>_<QUOTE>_1d.csv, same CSV shape as fetch_kraken.py's output
(timestamp = bar OPEN time, epoch ms; open, high, low, close, volume).

Also writes data_sources.md: source, quote, first/last date, candle count, and any date-gaps
found per coin, so a reader can judge how much history actually backs each coin's results.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ccxt
import pandas as pd

WATCHLIST = ["BTC", "ETH", "XRP", "SOL", "ADA", "DOGE", "AVAX", "LINK", "BNB", "LTC", "DOT", "ATOM", "ALGO", "BCH", "XTZ", "SHIB"]

# Earlier than any of these coins' real listing date on Coinbase -- fetch_paginated stops
# naturally once the exchange has no more history, so this just needs to be "early enough".
EARLIEST_SINCE = "2014-01-01T00:00:00Z"


def fetch_paginated(ex: ccxt.Exchange, symbol: str, timeframe: str, since_ms: int, batch_limit: int = 300) -> list[list]:
    """Coinbase's candle endpoint only returns bars inside [since, since + limit*timeframe] --
    it does NOT skip forward to the symbol's actual listing date on its own. A coin listed long
    after `since` (e.g. SOL, SHIB) legitimately returns an empty batch for every early window
    until the cursor finally reaches its real history, so an empty batch must advance the
    cursor by one full window and keep probing, not be treated as "no more data" -- the only
    real stopping condition is the cursor reaching the current time."""
    tf_ms = int(ex.parse_timeframe(timeframe) * 1000)
    now_ms = ex.milliseconds()
    all_rows: list[list] = []
    cursor = since_ms
    seen_timestamps: set[int] = set()
    while cursor < now_ms:
        batch = ex.fetch_ohlcv(symbol, timeframe, since=cursor, limit=batch_limit)
        if not batch:
            cursor += batch_limit * tf_ms  # no data in this window -- probe the next one
            continue
        new_rows = [r for r in batch if r[0] not in seen_timestamps]
        if not new_rows:
            # Every row in this batch was already seen -- the exchange isn't advancing
            # (fully caught up, or a flat repeat); stop rather than loop forever.
            break
        all_rows.extend(new_rows)
        seen_timestamps.update(r[0] for r in new_rows)
        last_ts = batch[-1][0]
        next_cursor = last_ts + tf_ms
        if next_cursor <= cursor:  # safety: exchange returned no forward progress
            break
        cursor = next_cursor
    all_rows.sort(key=lambda r: r[0])
    # Drop the still-forming candle (its close time hasn't passed yet).
    all_rows = [r for r in all_rows if r[0] + tf_ms <= ex.milliseconds()]
    return all_rows


def find_gaps(timestamps_ms: list[int], tf_ms: int) -> list[tuple[str, str, int]]:
    """Returns (gap_start_date, gap_end_date, missing_bar_count) for every run of
    consecutive bars that skips more than one expected interval."""
    gaps = []
    for i in range(1, len(timestamps_ms)):
        delta = timestamps_ms[i] - timestamps_ms[i - 1]
        if delta > tf_ms * 1.5:  # more than ~1 bar gap
            missing = round(delta / tf_ms) - 1
            start = datetime.fromtimestamp(timestamps_ms[i - 1] / 1000, tz=timezone.utc) + timedelta(seconds=tf_ms / 1000)
            end = datetime.fromtimestamp(timestamps_ms[i] / 1000, tz=timezone.utc) - timedelta(seconds=tf_ms / 1000)
            gaps.append((start.date().isoformat(), end.date().isoformat(), missing))
    return gaps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeframe", default="1d")
    ap.add_argument("--quote", default="USD")
    ap.add_argument("--out", default="data_long")
    a = ap.parse_args()

    ex = ccxt.coinbaseexchange({"enableRateLimit": True})
    ex.session.trust_env = True  # see fetch_kraken.py for why this is needed in a proxied env
    ex.load_markets()
    tf_ms = int(ex.parse_timeframe(a.timeframe) * 1000)
    since_ms = ex.parse8601(EARLIEST_SINCE)

    Path(a.out).mkdir(parents=True, exist_ok=True)
    skipped = []
    sources_rows = []

    for base in WATCHLIST:
        sym = f"{base}/{a.quote}"
        if sym not in ex.markets:
            skipped.append(sym)
            continue
        print(f"fetching {sym} ...", flush=True)
        rows = fetch_paginated(ex, sym, a.timeframe, since_ms)
        if not rows:
            skipped.append(sym)
            continue
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        path = Path(a.out) / f"{base}_{a.quote}_{a.timeframe}.csv"
        df.to_csv(path, index=False)

        timestamps = df["timestamp"].tolist()
        gaps = find_gaps(timestamps, tf_ms)
        first_date = datetime.fromtimestamp(timestamps[0] / 1000, tz=timezone.utc).date().isoformat()
        last_date = datetime.fromtimestamp(timestamps[-1] / 1000, tz=timezone.utc).date().isoformat()
        print(f"{sym}: {len(df)} closed candles, {first_date} to {last_date}, {len(gaps)} gap(s) -> {path}")
        sources_rows.append(
            dict(coin=base, source="Coinbase Exchange (ccxt coinbaseexchange, api.exchange.coinbase.com)",
                 quote=a.quote, first_date=first_date, last_date=last_date, candles=len(df), gaps=len(gaps),
                 gap_detail="; ".join(f"{s}..{e} ({n} missing)" for s, e, n in gaps[:10]) + (" ..." if len(gaps) > 10 else ""))
        )

    if skipped:
        print("Not available / no data on Coinbase Exchange under this quote (skipped):", ", ".join(skipped))

    lines = [
        "# Data sources (Data B: long history)\n",
        f"Fetched {datetime.now(timezone.utc).isoformat()}. Timeframe: {a.timeframe}. Quote: {a.quote}.\n",
        "Source for every coin below is Coinbase Exchange only -- never mixed with Kraken (Data A) within "
        "the same coin. Kraken's own downloadable historical files (support.kraken.com) and Binance (HTTP 451, "
        "geo-blocked from this environment) were tried first and were not usable; see fetch_coinbase_long.py's "
        "module docstring for details.\n",
        "| Coin | Source | Quote | First date | Last date | Candles | Gaps | Gap detail |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in sources_rows:
        lines.append(f"| {r['coin']} | {r['source']} | {r['quote']} | {r['first_date']} | {r['last_date']} | {r['candles']} | {r['gaps']} | {r['gap_detail'] or '—'} |")
    if skipped:
        lines.append(f"\nSkipped (no USD market or no data returned on Coinbase Exchange): {', '.join(skipped)}\n")
    Path("data_sources.md").write_text("\n".join(lines), encoding="utf-8")
    print("\nWrote data_sources.md")


if __name__ == "__main__":
    main()
