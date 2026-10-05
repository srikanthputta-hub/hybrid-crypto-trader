"""resample_ohlcv.py - derive 1h/4h/1d OHLCV bars from a 15m source, by resampling rather than
by separately fetching each timeframe.

Why resample instead of fetch each timeframe separately (docs/system-redesign-2026-10.md §5.4
point 1): this guarantees all four timeframes are consistent BY CONSTRUCTION -- a 4h bar is
always exactly four of the same 1h bars that exist in the 1h file, which are always exactly four
of the same 15m bars that exist in the 15m file. Fetching 1h/4h/1d independently from Coinbase
would risk subtle boundary/rounding mismatches between timeframes that this harness's own
multi-timeframe work (Phase 2a, later) would then be unable to distinguish from a real strategy
effect.

Bar-boundary alignment: Coinbase Exchange has no native 4h candle. This module resamples with
pandas' default subday-frequency anchor (`origin="start_day"`), which bins 4h at
00:00/04:00/08:00/12:00/16:00/20:00 UTC -- the same boundaries Kraken's own native 4h candles use
(spot-checked 2026-10-05 against a live `ex.fetch_ohlcv("BTC/USD", "4h")` call: Kraken's own 4h
bars open exactly on those six UTC boundaries). 1h and 1d bins land on the obvious boundaries
(every hour; midnight UTC) by the same anchor rule, so all three derived timeframes line up with
how a Kraken-native fetch of the same timeframe would have binned the same period.

Completeness, not interpolation: a resampled bin is only emitted if EVERY one of its expected 15m
rows is present (a full 1h bin needs 4, a full 4h bin needs 16, a full 1d bin needs 96). A bin
missing even one expected row -- the live trailing partial bar, or an internal gap in the 15m
source -- is dropped rather than computed from incomplete inputs. This is also what makes
resampling truncation-invariant (see strategy_pack_run/test_pack.py::test_truncation_no_lookahead
for the established pattern this module's own test follows): a bin that's complete in a truncated
prefix of the 15m frame is always identical to that same bin computed from the full frame, because
a bin's value, once all its rows exist, never gets revised by rows arriving later at a DIFFERENT
bin. A bin that isn't complete yet is simply absent from the truncated result -- it can only
*appear* later, never change after it already appeared.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Same convention as strategies.py's TF_SECONDS (import-only file; this is a parallel literal
# matching it, not an import, since strategies.py's own dict isn't exported for reuse and this
# module must not import anything from a file this repo's CLAUDE.md marks import-only... actually
# strategies.py has no restriction on being imported from; kept as a literal here anyway so this
# module has zero dependency on the unrelated trend_ensemble/tsmom engine it must not modify).
SOURCE_TF = "15m"
SOURCE_MINUTES = 15
PANDAS_RULE = {"1h": "1h", "4h": "4h", "1d": "1D"}
TARGET_MINUTES = {"1h": 60, "4h": 240, "1d": 1440}

_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def resample_ohlcv(df_15m: pd.DataFrame, target_tf: str) -> pd.DataFrame:
    """df_15m: DataFrame indexed by bar OPEN time (UTC, tz-aware DatetimeIndex), columns
    open/high/low/close/volume, one row per CLOSED 15m bar (the same contract
    fetch_coinbase_long.py's CSV output uses before backtest_live_bot.load_csv's +tf_seconds
    close-time shift) -- sorted ascending, no duplicate index values.

    Returns a DataFrame in the SAME convention (indexed by the derived bar's OWN open time),
    containing only bins where every expected 15m row was present (see module docstring).
    """
    if target_tf not in PANDAS_RULE:
        raise ValueError(f"unsupported target_tf {target_tf!r}; expected one of {sorted(PANDAS_RULE)}")
    if df_15m.empty:
        return df_15m.copy()
    expected_rows = TARGET_MINUTES[target_tf] // SOURCE_MINUTES
    grouped = df_15m.resample(PANDAS_RULE[target_tf], label="left", closed="left")
    counts = grouped["close"].count()
    agg = grouped.agg(_AGG)
    complete_idx = counts.index[counts == expected_rows]
    return agg.loc[complete_idx].sort_index()


def load_15m_csv(path: str | Path) -> pd.DataFrame:
    """Reads a fetch_coinbase_long.py-style 15m file (timestamp = bar OPEN time, epoch ms or ISO;
    open, high, low, close, volume) into the open-time-indexed DataFrame resample_ohlcv expects.
    Despite the name (kept for the function that existed before parquet support was added),
    reads CSV or parquet based on the path's own suffix -- either is the same contract otherwise."""
    path = Path(path)
    raw = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)
    ts = raw["timestamp"]
    open_time = (
        pd.to_datetime(ts, unit="ms", utc=True)
        if pd.api.types.is_numeric_dtype(ts)
        else pd.to_datetime(ts, utc=True)
    )
    df = raw[["open", "high", "low", "close", "volume"]].astype(float).copy()
    df.index = open_time
    df.index.name = "open_time"
    return df[~df.index.duplicated(keep="last")].sort_index()


def write_derived_csv(df: pd.DataFrame, path: str | Path) -> None:
    """Writes a resampled frame back out in the same row/column shape fetch_coinbase_long.py/
    fetch_kraken.py produce (timestamp column = bar OPEN time, epoch ms), so
    backtest_live_bot.load_csv/load_dir can read it exactly like any other dataset file. Despite
    the name (kept for the function that existed before parquet support was added), writes CSV or
    parquet based on the path's own suffix -- `.parquet` is what Phase 2.0's actual dataset uses
    (docs/system-redesign-2026-10.md §5.4: "stored gitignored as parquet, not as CSV")."""
    out = df.copy()
    # .view("int64") on a tz-aware DatetimeIndex isn't reliably nanoseconds across pandas
    # versions/units (pandas >=2 supports non-ns units) -- cast explicitly via numpy's
    # datetime64[ms] instead of assuming a unit.
    out.insert(0, "timestamp", out.index.values.astype("datetime64[ms]").astype("int64"))
    out = out[["timestamp", "open", "high", "low", "close", "volume"]]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".parquet":
        out.to_parquet(path, index=False)
    else:
        out.to_csv(path, index=False)


def derive_all_timeframes(
    src_dir: str | Path, out_dir: str | Path, quote: str = "USD", out_format: str = "parquet"
) -> dict[str, dict[str, int]]:
    """For every `<BASE>_<quote>_15m.csv` in src_dir (the raw Coinbase fetch, always CSV --
    fetch_coinbase_long.py's own output format), writes `<BASE>_<quote>_{15m,1h,4h,1d}.<out_format>`
    into out_dir -- including a copy of the 15m frame itself, so out_dir ends up holding the
    complete, self-contained 4-timeframe dataset backtest_live_bot.load_dir reads from (same dir
    is fine as both src_dir and out_dir -- the raw 15m CSV and the derived files never collide,
    since `out_format="parquet"` by default gives them different suffixes).

    `out_format` ("parquet" by default, per docs/system-redesign-2026-10.md §5.4): "csv" is still
    supported (used by this module's own tests, and available for debugging) by writing
    `.csv` instead of `.parquet` -- see write_derived_csv's suffix-sniffing.

    Returns {base: {tf: row_count}} for the caller's own reporting (see docs/p2_0_1h_rebaseline.md).
    """
    src_dir, out_dir = Path(src_dir), Path(out_dir)
    ext = ".parquet" if out_format == "parquet" else ".csv"
    counts: dict[str, dict[str, int]] = {}
    for p in sorted(src_dir.glob(f"*_{quote}_15m.csv")):
        base = p.stem.split("_")[0]
        df15 = load_15m_csv(p)
        counts[base] = {}
        write_derived_csv(df15, out_dir / f"{base}_{quote}_15m{ext}")
        counts[base]["15m"] = len(df15)
        for tf in ("1h", "4h", "1d"):
            derived = resample_ohlcv(df15, tf)
            write_derived_csv(derived, out_dir / f"{base}_{quote}_{tf}{ext}")
            counts[base][tf] = len(derived)
    return counts


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src-dir", default="data_15m_raw", help="directory with <BASE>_<quote>_15m.csv files (the raw Coinbase fetch)")
    ap.add_argument("--out-dir", default="data_15m", help="where to write the derived 15m/1h/4h/1d dataset")
    ap.add_argument("--quote", default="USD")
    ap.add_argument("--out-format", default="parquet", choices=["parquet", "csv"])
    a = ap.parse_args()

    result = derive_all_timeframes(a.src_dir, a.out_dir, a.quote, a.out_format)
    for base, by_tf in result.items():
        print(base, by_tf)
