"""
fetch_kraken.py - download closed OHLCV candles for the 16 watchlist coins from Kraken via ccxt.

  python fetch_kraken.py --timeframe 1d --quote USD --out data

Output: data/<BASE>_<QUOTE>_<timeframe>.csv with columns timestamp (bar OPEN, epoch ms), open, high, low, close, volume.

IMPORTANT LIMIT: Kraken's public OHLC endpoint returns at most the most recent 720 candles per request
(about 2 years of 1d, 120 days of 4h, 30 days of 1h). That is enough for a smoke test but too short to judge a
strategy. For multi-year history, use Kraken's downloadable historical OHLCVT files (see their support site)
or another exchange's bulk history, and save the files in the same CSV format.

If a symbol is not listed on Kraken (or under your quote currency) it is skipped and reported.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import ccxt
import pandas as pd

WATCHLIST = ["BTC", "ETH", "XRP", "SOL", "ADA", "DOGE", "AVAX", "LINK", "BNB", "LTC", "DOT", "ATOM", "ALGO", "BCH", "XTZ", "SHIB"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeframe", default="1d")
    ap.add_argument("--quote", default="USD")
    ap.add_argument("--out", default="data")
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
    Path(a.out).mkdir(parents=True, exist_ok=True)
    skipped = []
    for base in WATCHLIST:
        sym = f"{base}/{a.quote}"
        if sym not in ex.markets:
            skipped.append(sym)
            continue
        raw = ex.fetch_ohlcv(sym, a.timeframe, limit=720)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = df[df["timestamp"] + tf_ms <= ex.milliseconds()]  # drop the still-forming candle
        path = Path(a.out) / f"{base}_{a.quote}_{a.timeframe}.csv"
        df.to_csv(path, index=False)
        print(f"{sym}: {len(df)} closed candles -> {path}")
    if skipped:
        print("Not available on Kraken under this quote (skipped):", ", ".join(skipped))


if __name__ == "__main__":
    main()
