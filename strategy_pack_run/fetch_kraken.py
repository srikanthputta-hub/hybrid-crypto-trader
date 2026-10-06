"""
fetch_kraken.py - download closed OHLCV candles from Kraken via ccxt.

  python fetch_kraken.py --timeframe 1d --quote USD --out data

Output: data/<BASE>_<QUOTE>_<timeframe>.csv with columns timestamp (bar OPEN, epoch ms), open, high, low, close, volume.

IMPORTANT LIMIT: Kraken's public OHLC endpoint returns at most the most recent 720 candles per request
(about 2 years of 1d, 120 days of 4h, 30 days of 1h). That is enough for a smoke test but too short to judge a
strategy. For multi-year history, use Kraken's downloadable historical OHLCVT files (see their support site)
or another exchange's bulk history, and save the files in the same CSV format.

If a symbol is not listed on Kraken (or under your quote currency) it is skipped and reported.

--universe (added for P1d, docs/system-redesign-2026-10.md §4.4/§8 "P1d universe datasets"):
  "watchlist" (default) -- exactly the original behavior, the hardcoded 16-coin WATCHLIST below,
     byte-identical to every existing caller (fetch_kraken.py with no --universe flag at all).
  "discover" -- every USD-quoted base Kraken actually lists right now (confirmed live via
     ex.load_markets(), not hardcoded), across ALL listing statuses (online, cancel_only,
     post_only). cancel_only pairs are deliberately INCLUDED: they're being wound down, which is
     exactly the population a point-in-time backtest needs to avoid survivorship bias (a coin that
     got delisted mid-history should still appear with real data up to its last tradeable day, not
     vanish from the dataset as though it had never existed). Excludes bases in --exclude-bases
     (see config/config.yaml's universe.exclude_bases for the intended authoritative list -- empty
     there as of this writing, see fetch_kraken.py's own module docstring note below; this script's
     DEFAULT_EXCLUDE_BASES is the actual list used, taken from docs/system-redesign-2026-10.md §1's
     measured stablecoin/fiat/wrapped table, until config/config.yaml's own list is populated).
  Reuses the exact same fetch_ohlcv/pagination-free single-call-per-symbol logic as "watchlist" --
  the only thing that changes between modes is which base list the loop iterates over, plus (for
  "discover") a manifest.csv capturing status/inclusion per base for provenance.

NOTE on config/config.yaml's universe.exclude_bases: as of this script's writing it is still the
empty list ("NOT YET USED" per its own comment) -- the stablecoin/fiat/wrapped exclusion list has
not actually been populated into config.yaml yet, despite being fully specified in the design doc.
This script does NOT silently read an empty list and exclude nothing; it uses DEFAULT_EXCLUDE_BASES
(below) so a discover-mode fetch is useful today, and documents this gap rather than pretending
config.yaml already has the list. When config.yaml's list is populated, prefer passing
--exclude-bases explicitly from it over relying on this hardcoded default.
"""
from __future__ import annotations

import argparse
import csv
import time
from pathlib import Path

import ccxt
import pandas as pd

WATCHLIST = ["BTC", "ETH", "XRP", "SOL", "ADA", "DOGE", "AVAX", "LINK", "BNB", "LTC", "DOT", "ATOM", "ALGO", "BCH", "XTZ", "SHIB"]

# Stablecoin/fiat/wrapped bases to always drop in discover mode -- copied from
# docs/system-redesign-2026-10.md §1's measured table (this is the same list the P1d task
# description itself gives verbatim; config/config.yaml's universe.exclude_bases is the INTENDED
# authoritative source but is still empty as of this writing -- see module docstring above).
DEFAULT_EXCLUDE_BASES = [
    "USDT", "USDC", "DAI", "PYUSD", "RLUSD", "USDG", "USDE", "USDS", "USD1", "USDQ",
    "EURC", "EURQ", "EUR", "GBP", "AUD", "PAXG", "XAUT", "TBTC",
]

MAX_FETCH_RETRIES = 5
RETRY_BASE_SLEEP_S = 2.0


def _fetch_ohlcv_with_retry(ex: ccxt.Exchange, symbol: str, timeframe: str, limit: int) -> list[list]:
    """Same retry-with-backoff pattern as fetch_coinbase_long.py's _fetch_ohlcv_with_retry (added
    there for the Phase 2.0 15m fetch) -- a several-hundred-symbol discover-mode fetch is long
    enough to hit a transient rate-limit/network error partway through, which the original
    16-symbol watchlist fetch never ran long enough to realistically hit."""
    for attempt in range(MAX_FETCH_RETRIES):
        try:
            return ex.fetch_ohlcv(symbol, timeframe, limit=limit)
        except (ccxt.RateLimitExceeded, ccxt.NetworkError, ccxt.ExchangeNotAvailable) as exc:
            if attempt == MAX_FETCH_RETRIES - 1:
                raise
            sleep_s = RETRY_BASE_SLEEP_S * (2 ** attempt)
            print(f"  ... {type(exc).__name__} on {symbol} (attempt {attempt + 1}/{MAX_FETCH_RETRIES}), "
                  f"retrying in {sleep_s:.0f}s", flush=True)
            time.sleep(sleep_s)
    raise AssertionError("unreachable")


def discover_bases(ex: ccxt.Exchange, quote: str, exclude_bases: list[str]) -> list[dict]:
    """Every `quote`-quoted spot base Kraken lists right now, any listing status, confirmed live
    via ex.markets (never hardcoded) -- this is the "every USD-quoted base on Kraken" the P1d task
    describes. Returns one row per market (base, symbol, status, included, exclude_reason) so the
    manifest can show exactly what was fetched and what was dropped, and why."""
    rows = []
    for m in ex.markets.values():
        if not m.get("spot") or m.get("quote") != quote:
            continue
        base = m["base"]
        status = (m.get("info") or {}).get("status", "unknown")
        excluded = base in exclude_bases
        rows.append(
            dict(
                base=base, symbol=m["symbol"], status=status,
                included=not excluded,
                exclude_reason="stablecoin/fiat/wrapped (exclude_bases)" if excluded else "",
            )
        )
    rows.sort(key=lambda r: r["base"])
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeframe", default="1d")
    ap.add_argument("--quote", default="USD")
    ap.add_argument("--out", default="data")
    ap.add_argument(
        "--universe", choices=["watchlist", "discover"], default="watchlist",
        help='"watchlist" (default): the original hardcoded 16-coin WATCHLIST, byte-identical to '
             'every existing caller. "discover": every --quote-quoted base Kraken actually lists '
             "right now (all listing statuses, including cancel_only), minus --exclude-bases.",
    )
    ap.add_argument(
        "--exclude-bases", default=",".join(DEFAULT_EXCLUDE_BASES),
        help="Comma-separated bases to drop in --universe discover mode (ignored in watchlist "
             "mode). Default is DEFAULT_EXCLUDE_BASES (see module docstring).",
    )
    ap.add_argument(
        "--manifest-out", default=None,
        help="Where to write the discover-mode manifest CSV (base, symbol, status, included, "
             "exclude_reason). Default: <out>/manifest.csv. Ignored in watchlist mode.",
    )
    a = ap.parse_args()

    ex = ccxt.kraken({"enableRateLimit": True})
    # ccxt's requests Session defaults to trust_env=False, so it ignores
    # HTTP(S)_PROXY entirely unless told otherwise -- harmless on a plain
    # machine with no proxy env vars set, but required in a sandboxed/proxied
    # environment (the ccxt 'trust_env' constructor option doesn't actually
    # propagate here due to an init-ordering quirk, so set it directly).
    ex.session.trust_env = True
    ex.load_markets()
    tf_ms = int(ex.parse_timeframe(a.timeframe) * 1000)
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if a.universe == "watchlist":
        bases = WATCHLIST
        manifest_rows = None
    else:
        exclude_bases = [b.strip().upper() for b in a.exclude_bases.split(",") if b.strip()]
        manifest_rows = discover_bases(ex, a.quote, exclude_bases)
        bases = [r["base"] for r in manifest_rows if r["included"]]
        print(f"discover mode: {len(manifest_rows)} {a.quote} spot markets on Kraken, "
              f"{len(bases)} included after excluding {len(manifest_rows) - len(bases)} "
              f"stablecoin/fiat/wrapped base(s)", flush=True)

    skipped = []
    for n, base in enumerate(bases, start=1):
        sym = f"{base}/{a.quote}"
        if sym not in ex.markets:
            skipped.append(sym)
            continue
        raw = _fetch_ohlcv_with_retry(ex, sym, a.timeframe, limit=720)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = df[df["timestamp"] + tf_ms <= ex.milliseconds()]  # drop the still-forming candle
        path = out_dir / f"{base}_{a.quote}_{a.timeframe}.csv"
        df.to_csv(path, index=False)
        print(f"[{n}/{len(bases)}] {sym}: {len(df)} closed candles -> {path}", flush=True)
    if skipped:
        print("Not available on Kraken under this quote (skipped):", ", ".join(skipped))

    if manifest_rows is not None:
        manifest_path = Path(a.manifest_out) if a.manifest_out else out_dir / "manifest.csv"
        with open(manifest_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["base", "symbol", "status", "included", "exclude_reason"])
            w.writeheader()
            for r in manifest_rows:
                w.writerow(r)
        print(f"Wrote manifest -> {manifest_path}")


if __name__ == "__main__":
    main()
