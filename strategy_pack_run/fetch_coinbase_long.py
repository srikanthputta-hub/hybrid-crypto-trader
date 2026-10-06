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

Also writes data_sources.md (or wherever --sources-out points): source, quote, first/last date,
candle count, and any date-gaps found per coin, so a reader can judge how much history actually
backs each coin's results.

--since/--until (added for Phase 2.0's 15m intraday fetch, docs/system-redesign-2026-10.md §5.4):
optional ISO8601 bounds on bar OPEN time. Both default to the original "fetch everything available"
behavior (since=EARLIEST_SINCE, until=None i.e. page to the real current time) so every existing
call site/usage of this script is unaffected. Example, 15m OHLCV over the same ~2-year window as
strategy_pack_run/data's kraken_720 dataset:

  python fetch_coinbase_long.py --timeframe 15m --quote USD --out data_15m \\
      --since 2024-10-12T00:00:00Z --until 2026-09-30T00:00:00Z \\
      --sources-out data_sources_15m.md

--universe discover (added for P1d, docs/system-redesign-2026-10.md §4.4/§8 "P1d universe
datasets"): instead of the hardcoded 16-coin WATCHLIST, fetch every base listed in a Kraken
discover-mode manifest (see fetch_kraken.py --universe discover's manifest.csv) that Coinbase
Exchange ALSO lists under --quote -- "the Coinbase bases that are also in the Kraken universe",
per the design doc's `long_universe` definition, not "every base Coinbase lists" (Coinbase's own
full listing isn't the relevant universe here; Kraken is the live/scan venue). Bases Kraken
excluded (stablecoins/fiat/wrapped) are therefore already excluded here too, since they're absent
from the manifest's included rows. Reuses fetch_paginated/_fetch_ohlcv_with_retry unchanged -- the
only difference from the watchlist path is where the base list comes from and that --resume/
--since/--until all apply per-base exactly as before.

  python fetch_coinbase_long.py --universe discover \\
      --kraken-manifest data_universe/raw_kraken/manifest.csv \\
      --quote USD --out data_universe/raw_long --resume \\
      --sources-out data_universe/raw_long/data_sources.md
"""
from __future__ import annotations

import argparse
import csv
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ccxt
import pandas as pd

WATCHLIST = ["BTC", "ETH", "XRP", "SOL", "ADA", "DOGE", "AVAX", "LINK", "BNB", "LTC", "DOT", "ATOM", "ALGO", "BCH", "XTZ", "SHIB"]


def bases_from_kraken_manifest(path: str | Path) -> list[str]:
    """Reads fetch_kraken.py --universe discover's manifest.csv and returns the INCLUDED bases
    (status=anything, included=True -- stablecoin/fiat/wrapped exclusions already applied there),
    sorted, deduplicated. Raises if the file doesn't look like that manifest shape, rather than
    silently fetching an empty/wrong universe."""
    path = Path(path)
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows or "base" not in rows[0] or "included" not in rows[0]:
        raise ValueError(
            f"{path}: doesn't look like a fetch_kraken.py --universe discover manifest.csv "
            f"(expected columns including 'base' and 'included')"
        )
    bases = sorted({r["base"] for r in rows if r["included"] in ("True", "true", "1")})
    if not bases:
        raise ValueError(f"{path}: manifest has zero included bases -- check the file")
    return bases

# Earlier than any of these coins' real listing date on Coinbase -- fetch_paginated stops
# naturally once the exchange has no more history, so this just needs to be "early enough".
EARLIEST_SINCE = "2014-01-01T00:00:00Z"

# Added for the Phase 2.0 15m fetch (docs/system-redesign-2026-10.md §5.4): a 15m/2-year pull is
# ~230 paginated requests PER COIN, and Coinbase Exchange's public candles endpoint enforces a
# tighter rate limit in practice than ccxt's `enableRateLimit` throttle alone avoids (observed
# 2026-10-05: a bare HTTP 429 "Public rate limit exceeded" partway through this exact fetch,
# which an uncaught ccxt.RateLimitExceeded then crashed the whole run on -- the original 1d-only
# fetch never ran long enough to hit this). Retry with exponential backoff rather than crashing;
# this is the only behavior change on the success path (none -- a request that succeeds first try
# is identical to before).
MAX_FETCH_RETRIES = 6
RETRY_BASE_SLEEP_S = 2.0


def _fetch_ohlcv_with_retry(ex: ccxt.Exchange, symbol: str, timeframe: str, since: int, limit: int) -> list[list]:
    for attempt in range(MAX_FETCH_RETRIES):
        try:
            return ex.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        except (ccxt.RateLimitExceeded, ccxt.NetworkError, ccxt.ExchangeNotAvailable) as exc:
            if attempt == MAX_FETCH_RETRIES - 1:
                raise
            sleep_s = RETRY_BASE_SLEEP_S * (2 ** attempt)
            print(f"  ... {type(exc).__name__} on {symbol} (attempt {attempt + 1}/{MAX_FETCH_RETRIES}), "
                  f"retrying in {sleep_s:.0f}s", flush=True)
            time.sleep(sleep_s)
    raise AssertionError("unreachable")  # loop either returns or raises above


def fetch_paginated(
    ex: ccxt.Exchange,
    symbol: str,
    timeframe: str,
    since_ms: int,
    batch_limit: int = 300,
    until_ms: int | None = None,
) -> list[list]:
    """Coinbase's candle endpoint only returns bars inside [since, since + limit*timeframe] --
    it does NOT skip forward to the symbol's actual listing date on its own. A coin listed long
    after `since` (e.g. SOL, SHIB) legitimately returns an empty batch for every early window
    until the cursor finally reaches its real history, so an empty batch must advance the
    cursor by one full window and keep probing, not be treated as "no more data" -- the only
    real stopping condition is the cursor reaching the current time (or `until_ms`, if given).

    `until_ms` (added for the Phase 2.0 intraday fetch, docs/system-redesign-2026-10.md §5.4):
    an optional upper bound, exclusive, on bar OPEN time -- lets a caller match a specific prior
    dataset's window (e.g. `data/`'s `kraken_720` date range) instead of always paging all the way
    to "now". `None` (default) preserves every existing caller's behavior byte-for-byte (pages to
    the real current time, same as before this parameter existed)."""
    tf_ms = int(ex.parse_timeframe(timeframe) * 1000)
    now_ms = ex.milliseconds()
    stop_ms = now_ms if until_ms is None else min(now_ms, until_ms)
    all_rows: list[list] = []
    cursor = since_ms
    seen_timestamps: set[int] = set()
    while cursor < stop_ms:
        batch = _fetch_ohlcv_with_retry(ex, symbol, timeframe, since=cursor, limit=batch_limit)
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
    # Drop the still-forming candle (its close time hasn't passed yet) and anything at/after
    # `until_ms` (a batch can legitimately overshoot the requested window, since Coinbase returns
    # up to `batch_limit` bars from `since` regardless of where the caller wanted to stop).
    all_rows = [r for r in all_rows if r[0] + tf_ms <= now_ms and (until_ms is None or r[0] < until_ms)]
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
    ap.add_argument(
        "--since", default=EARLIEST_SINCE,
        help="ISO8601 lower bound on bar OPEN time (default: earlier than any watchlist coin's "
             "real Coinbase listing date, i.e. fetch everything available).",
    )
    ap.add_argument(
        "--until", default=None,
        help="ISO8601 upper bound (exclusive) on bar OPEN time. Default: page all the way to the "
             "real current time, same as before this flag existed. Set this to match a specific "
             "prior dataset's window (e.g. Phase 2.0's 15m fetch matching data/'s kraken_720 "
             "range -- see docs/system-redesign-2026-10.md §5.4) instead of always fetching "
             "up to 'now'.",
    )
    ap.add_argument(
        "--sources-out", default="data_sources.md",
        help="Where to write the provenance/gap-report markdown. Default matches the original "
             "behavior (data_sources.md in the cwd) -- override this for any --out other than "
             "data_long so a second fetch run doesn't overwrite data_long's own provenance doc.",
    )
    ap.add_argument(
        "--resume", action="store_true",
        help="Skip any base whose output CSV already exists under --out, instead of refetching "
             "and overwriting it. Added for the Phase 2.0 15m fetch (docs/system-redesign-2026-10.md "
             "§5.4): a 16-coin x ~2-year x 15m pull is long enough to hit a transient rate-limit or "
             "network failure partway through (see MAX_FETCH_RETRIES's docstring), so a rerun with "
             "--resume only fetches what's still missing. Off by default -- every existing call site "
             "(the original data_long fetch) is unaffected.",
    )
    ap.add_argument(
        "--universe", choices=["watchlist", "discover"], default="watchlist",
        help='"watchlist" (default): the original hardcoded 16-coin WATCHLIST, byte-identical to '
             'every existing caller. "discover": every base in --kraken-manifest (a fetch_kraken.py '
             "--universe discover manifest.csv) that Coinbase Exchange also lists under --quote -- "
             'this is the P1d "long_universe" (Coinbase bases that are also in the Kraken universe), '
             "not Coinbase's full listing.",
    )
    ap.add_argument(
        "--kraken-manifest", default=None,
        help="Path to fetch_kraken.py --universe discover's manifest.csv. Required when "
             "--universe discover is set; ignored otherwise.",
    )
    a = ap.parse_args()
    if a.universe == "discover" and not a.kraken_manifest:
        ap.error("--universe discover requires --kraken-manifest <path to fetch_kraken.py manifest.csv>")

    ex = ccxt.coinbaseexchange({"enableRateLimit": True})
    ex.session.trust_env = True  # see fetch_kraken.py for why this is needed in a proxied env
    ex.load_markets()
    tf_ms = int(ex.parse_timeframe(a.timeframe) * 1000)
    since_ms = ex.parse8601(a.since)
    until_ms = ex.parse8601(a.until) if a.until else None

    Path(a.out).mkdir(parents=True, exist_ok=True)
    skipped = []
    sources_rows = []
    not_on_coinbase = []

    if a.universe == "discover":
        candidate_bases = bases_from_kraken_manifest(a.kraken_manifest)
        bases = [b for b in candidate_bases if f"{b}/{a.quote}" in ex.markets]
        not_on_coinbase = [b for b in candidate_bases if b not in bases]
        print(f"discover mode: {len(candidate_bases)} Kraken-universe base(s), "
              f"{len(bases)} also listed on Coinbase Exchange under {a.quote} "
              f"({len(not_on_coinbase)} not listed there)", flush=True)
    else:
        bases = WATCHLIST

    for base in bases:
        sym = f"{base}/{a.quote}"
        if sym not in ex.markets:
            skipped.append(sym)
            continue
        path = Path(a.out) / f"{base}_{a.quote}_{a.timeframe}.csv"
        if a.resume and path.exists():
            print(f"{sym}: --resume, already fetched -> {path} (skipping)", flush=True)
            existing = pd.read_csv(path)
            timestamps = existing["timestamp"].tolist()
            gaps = find_gaps(timestamps, tf_ms)
            sources_rows.append(
                dict(coin=base, source="Coinbase Exchange (ccxt coinbaseexchange, api.exchange.coinbase.com)",
                     quote=a.quote,
                     first_date=datetime.fromtimestamp(timestamps[0] / 1000, tz=timezone.utc).date().isoformat(),
                     last_date=datetime.fromtimestamp(timestamps[-1] / 1000, tz=timezone.utc).date().isoformat(),
                     candles=len(existing), gaps=len(gaps),
                     gap_detail="; ".join(f"{s}..{e} ({n} missing)" for s, e, n in gaps[:10]) + (" ..." if len(gaps) > 10 else ""))
            )
            continue
        print(f"fetching {sym} ...", flush=True)
        rows = fetch_paginated(ex, sym, a.timeframe, since_ms, until_ms=until_ms)
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
        "# Data sources (Data B: long history)\n" if a.timeframe == "1d" else f"# Data sources ({a.timeframe} intraday fetch -> {a.out})\n",
        f"Fetched {datetime.now(timezone.utc).isoformat()}. Timeframe: {a.timeframe}. Quote: {a.quote}. "
        f"Requested window: since={a.since}, until={a.until or 'now'}.\n",
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
    if not_on_coinbase:
        lines.append(
            f"\nKraken-universe base(s) not listed on Coinbase Exchange under {a.quote} at all "
            f"(excluded from long_universe before any fetch attempt): {', '.join(not_on_coinbase)}\n"
        )
    Path(a.sources_out).write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {a.sources_out}")


if __name__ == "__main__":
    main()
