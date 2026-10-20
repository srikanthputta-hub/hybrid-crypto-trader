"""
fetch_intraday_p0.py - deep 15m / 1h OHLCV history for the 16 watchlist coins from the
Coinbase Exchange public candles endpoint (docs/intraday_research_plan.md, phase P0).

  python strategy_pack_run/fetch_intraday_p0.py --out strategy_pack_run/data_intraday \
      --timeframes 15m 1h --workers 4 --rate 5

Differences from fetch_coinbase_long.py (which this extends, it does not replace):
  * Talks to https://api.exchange.coinbase.com/products/<BASE>-USD/candles directly with
    explicit, NON-overlapping [start, end] windows of exactly 300 bars (no ccxt cursor drift).
  * Starts each coin at its first DAILY bar (data_long/<BASE>_USD_1d.csv) so a coin listed late
    isn't probed from 2014 -- the intraday history cannot start before the daily one.
  * Resumable: rows are appended to the CSV as each window completes and a small JSON state file
    (<out>/_state/) records the next window cursor, so a rerun continues where it stopped.
  * Global request-rate limiter shared across worker threads, exponential back-off on 429/5xx/
    network errors (never crashes the whole run on one transient failure; a window that exhausts
    its retries aborts only that (coin, timeframe) job, which a rerun resumes).
  * Closed bars only (bar open + timeframe <= now), UTC epoch-ms bar OPEN time, de-duplicated,
    sorted -- same CSV schema as strategy_pack_run/data_long: timestamp,open,high,low,close,volume.

IMPORTANT data semantics (documented in docs/intraday_data.md): Coinbase only emits a candle
for an interval in which at least one trade happened. A "gap" in the output is therefore a
no-trade interval (or a rare API/ingestion hole); the two cannot be told apart from the candles
alone. Nothing is forward-filled or interpolated here.

No API keys are used or needed (public endpoint).
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

WATCHLIST = ["BTC", "ETH", "XRP", "SOL", "ADA", "DOGE", "AVAX", "LINK", "BNB", "LTC", "DOT", "ATOM", "ALGO", "BCH", "XTZ", "SHIB"]
BASE_URL = "https://api.exchange.coinbase.com"
TF_SECONDS = {"15m": 900, "1h": 3600}
BARS_PER_REQUEST = 300
COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]
MAX_RETRIES = 7
RETRY_BASE_SLEEP_S = 1.5


class RateLimiter:
    """Thread-safe minimum-spacing limiter (`rate` requests/second across all threads)."""

    def __init__(self, rate: float):
        self.interval = 1.0 / rate
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            t = max(now, self._next)
            self._next = t + self.interval
        if t > now:
            time.sleep(t - now)


def window_end_ms(start_ms: int, tf_ms: int, bars: int = BARS_PER_REQUEST) -> int:
    """Inclusive OPEN time of the last bar of a window that begins at start_ms: exactly `bars`
    bars (start .. start+(bars-1)*tf). Coinbase treats `end` as inclusive, so asking for
    start+bars*tf would return bars+1 rows and overlap the next window."""
    return start_ms + (bars - 1) * tf_ms


def parse_candles(raw: list[list], start_ms: int, end_ms: int) -> list[list]:
    """Coinbase rows are [time_s, low, high, open, close, volume] (newest first). Returns rows in
    this repo's [timestamp_ms, open, high, low, close, volume] order, oldest first, restricted to
    [start_ms, end_ms] (defensive: the endpoint has been seen to return bars just outside the
    requested range) and de-duplicated on timestamp."""
    out: dict[int, list] = {}
    for r in raw:
        ts = int(r[0]) * 1000
        if ts < start_ms or ts > end_ms:
            continue
        out[ts] = [ts, float(r[3]), float(r[2]), float(r[1]), float(r[4]), float(r[5])]
    return [out[k] for k in sorted(out)]


def drop_unclosed(rows: list[list], tf_ms: int, now_ms: int) -> list[list]:
    """Closed bars only: a bar is closed once its open time + timeframe <= now."""
    return [r for r in rows if r[0] + tf_ms <= now_ms]


def fetch_window(session: requests.Session, limiter: RateLimiter, product: str, tf: str,
                 start_ms: int, end_ms: int, sleep=time.sleep) -> list[list]:
    """One window request with retry/back-off. Returns parsed rows (possibly empty = no trades)."""
    params = {
        "start": datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "granularity": TF_SECONDS[tf],
    }
    url = f"{BASE_URL}/products/{product}/candles"
    last_err = "unknown"
    for attempt in range(MAX_RETRIES):
        limiter.wait()
        try:
            resp = session.get(url, params=params, timeout=30)
        except requests.RequestException as exc:
            last_err = f"{type(exc).__name__}"
        else:
            if resp.status_code == 200:
                return parse_candles(resp.json(), start_ms, end_ms)
            if resp.status_code == 404:
                # Product not found / not yet listed for that range: treat as no data.
                return []
            last_err = f"HTTP {resp.status_code}"
            if resp.status_code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f"{product} {tf} {params['start']}: non-retryable {last_err}: {resp.text[:120]}")
        sleep(RETRY_BASE_SLEEP_S * (2 ** attempt))
    raise RuntimeError(f"{product} {tf} {params['start']}: gave up after {MAX_RETRIES} attempts ({last_err})")


def first_daily_open_ms(daily_dir: Path, base: str, quote: str) -> int | None:
    p = daily_dir / f"{base}_{quote}_1d.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p, usecols=["timestamp"], nrows=1)
    return int(df["timestamp"].iloc[0]) if len(df) else None


def last_csv_timestamp(path: Path) -> int | None:
    """Timestamp of the last data row of an existing CSV (None if header-only / missing)."""
    if not path.exists():
        return None
    last = None
    with open(path, "rb") as f:
        for line in f:
            last = line
    if last is None or last.startswith(b"timestamp"):
        return None
    return int(last.split(b",", 1)[0])


def load_state(path: Path) -> dict | None:
    if path.exists():
        return json.loads(path.read_text())
    return None


def fetch_job(base: str, tf: str, out_dir: Path, daily_dir: Path, quote: str, limiter: RateLimiter,
              until_ms: int | None, log=print) -> dict:
    """Fetch one (coin, timeframe) to <out_dir>/<tf>/<BASE>_<QUOTE>_<tf>.csv, resumably."""
    tf_ms = TF_SECONDS[tf] * 1000
    tf_dir = out_dir / tf
    state_dir = out_dir / "_state"
    tf_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    csv_path = tf_dir / f"{base}_{quote}_{tf}.csv"
    state_path = state_dir / f"{base}_{quote}_{tf}.json"
    product = f"{base}-{quote}"

    now_ms = int(time.time() * 1000)
    stop_ms = now_ms if until_ms is None else min(now_ms, until_ms)
    state = load_state(state_path)
    if state and csv_path.exists():
        cursor = state["cursor_ms"]
    else:
        first = first_daily_open_ms(daily_dir, base, quote)
        cursor = (first if first is not None else int(datetime(2015, 1, 1, tzinfo=timezone.utc).timestamp() * 1000))
        cursor = cursor // tf_ms * tf_ms
        csv_path.write_text(",".join(COLUMNS) + "\n")
    if cursor >= stop_ms:
        return {"base": base, "tf": tf, "status": "already-complete"}

    last_ts = last_csv_timestamp(csv_path)  # crash-safety: a rerun must never append a row twice
    session = requests.Session()
    session.trust_env = True
    n_req = n_rows = 0
    t0 = time.time()
    while cursor < stop_ms:
        end_ms = window_end_ms(cursor, tf_ms)
        rows = drop_unclosed(fetch_window(session, limiter, product, tf, cursor, end_ms), tf_ms, stop_ms)
        rows = [r for r in rows if r[0] < stop_ms and (last_ts is None or r[0] > last_ts)]
        n_req += 1
        if rows:
            with open(csv_path, "a", encoding="utf-8") as f:
                for r in rows:
                    f.write(f"{r[0]},{r[1]!r},{r[2]!r},{r[3]!r},{r[4]!r},{r[5]!r}\n")
            n_rows += len(rows)
            last_ts = rows[-1][0]
        cursor = end_ms + tf_ms
        if cursor >= stop_ms:
            # Park the cursor on the first not-yet-closed boundary so a later top-up run resumes
            # exactly there (never skips bars that were still forming when this run ended).
            cursor = stop_ms // tf_ms * tf_ms
            state_path.write_text(json.dumps({"cursor_ms": cursor}))
            break
        state_path.write_text(json.dumps({"cursor_ms": cursor}))
        if n_req % 100 == 0:
            d = datetime.fromtimestamp(cursor / 1000, tz=timezone.utc).date()
            log(f"  {base} {tf}: {n_req} req, {n_rows} rows, cursor {d}, {time.time() - t0:.0f}s", flush=True)
    return {"base": base, "tf": tf, "status": "fetched", "requests": n_req, "rows": n_rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="strategy_pack_run/data_intraday")
    ap.add_argument("--daily-dir", default="strategy_pack_run/data_long",
                    help="Where the daily CSVs live; each coin's intraday fetch starts at its first daily bar.")
    ap.add_argument("--quote", default="USD")
    ap.add_argument("--timeframes", nargs="+", default=["1h", "15m"], choices=sorted(TF_SECONDS))
    ap.add_argument("--coins", nargs="+", default=WATCHLIST)
    ap.add_argument("--until", default=None, help="ISO8601 exclusive bound on bar open time (default: now)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--rate", type=float, default=5.0, help="max requests/second across all workers")
    a = ap.parse_args()

    until_ms = int(datetime.fromisoformat(a.until.replace("Z", "+00:00")).timestamp() * 1000) if a.until else None
    limiter = RateLimiter(a.rate)
    out_dir, daily_dir = Path(a.out), Path(a.daily_dir)
    jobs = [(b, tf) for tf in a.timeframes for b in a.coins]
    failures = []
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futs = {pool.submit(fetch_job, b, tf, out_dir, daily_dir, a.quote, limiter, until_ms): (b, tf) for b, tf in jobs}
        for fut in as_completed(futs):
            b, tf = futs[fut]
            try:
                res = fut.result()
                print(f"done {b} {tf}: {res}", flush=True)
            except Exception as exc:  # one failed job must not kill the others; rerun resumes it
                failures.append((b, tf, str(exc)))
                print(f"FAILED {b} {tf}: {exc}", flush=True)
    if failures:
        print("\nINCOMPLETE (rerun the same command to resume):")
        for f in failures:
            print("  ", f)
        raise SystemExit(1)
    print("all jobs complete")


if __name__ == "__main__":
    main()
