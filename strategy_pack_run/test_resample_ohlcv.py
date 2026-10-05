"""test_resample_ohlcv.py - proves resample_ohlcv.py's correctness on synthetic 15m data:
boundary alignment (00:00 UTC anchor, matching Kraken's own native 4h candles -- spot-checked
2026-10-05, see the module docstring), completeness (an incomplete bin is dropped, never computed
from partial inputs), and -- the look-ahead-hazard check the design doc calls out explicitly
(docs/system-redesign-2026-10.md §5.4) -- truncation invariance, modeled on
strategy_pack_run/test_pack.py::test_truncation_no_lookahead.

Run directly:  python strategy_pack_run/test_resample_ohlcv.py
Run via pytest: pytest strategy_pack_run/test_resample_ohlcv.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
for _p in (_THIS_DIR,):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from resample_ohlcv import derive_all_timeframes, load_15m_csv, resample_ohlcv, write_derived_csv  # noqa: E402


def _synthetic_15m(start: str, n_bars: int, seed: int = 0) -> pd.DataFrame:
    """n_bars of close-enough-to-realistic 15m OHLCV, open-time indexed, starting at `start`
    (any UTC time -- deliberately NOT always midnight, so boundary tests prove the anchor is
    independent of where the data happens to start)."""
    idx = pd.date_range(start, periods=n_bars, freq="15min", tz="UTC")
    rng = np.random.default_rng(seed)
    close = 100.0 + np.cumsum(rng.normal(0, 0.05, n_bars))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + rng.random(n_bars) * 0.1
    low = np.minimum(open_, close) - rng.random(n_bars) * 0.1
    volume = 1.0 + rng.random(n_bars)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)


# --------------------------------------------------------------------------- #
# Boundary alignment
# --------------------------------------------------------------------------- #
def test_resample_4h_aligns_to_kraken_00_00_utc_boundaries():
    """Kraken's own native 4h candles open at 00:00/04:00/08:00/12:00/16:00/20:00 UTC (verified
    live against ex.fetch_ohlcv("BTC/USD", "4h") on 2026-10-05 -- see the module docstring).
    Data starting mid-day must still resample onto those same six boundaries, not onto
    offsets relative to the data's own start."""
    df = _synthetic_15m("2026-03-11 07:15", 4 * 24 * 5)  # 5 days, starts mid-day on purpose
    r4h = resample_ohlcv(df, "4h")
    assert set(r4h.index.hour.unique()) <= {0, 4, 8, 12, 16, 20}
    assert (r4h.index.minute == 0).all() and (r4h.index.second == 0).all()


def test_resample_1h_and_1d_also_land_on_utc_boundaries():
    df = _synthetic_15m("2026-03-11 03:37", 4 * 24 * 4)
    r1h = resample_ohlcv(df, "1h")
    r1d = resample_ohlcv(df, "1d")
    assert (r1h.index.minute == 0).all()
    assert (r1d.index.hour == 0).all() and (r1d.index.minute == 0).all()


# --------------------------------------------------------------------------- #
# Completeness -- never compute a bin from partial inputs
# --------------------------------------------------------------------------- #
def test_resample_drops_incomplete_trailing_bin():
    """3 hours of 15m data plus 2 extra bars (a partial 4th hour) must yield exactly 3 complete
    1h bins, never a 4th bin averaged over only 2 of its 4 expected rows."""
    df = _synthetic_15m("2026-01-01 00:00", 4 * 3 + 2)
    r1h = resample_ohlcv(df, "1h")
    assert len(r1h) == 3, f"expected 3 complete hourly bins, got {len(r1h)}"
    assert r1h.index[-1] == pd.Timestamp("2026-01-01 02:00", tz="UTC")


def test_resample_drops_bin_with_an_internal_gap():
    """Deleting one 15m row from the middle of an otherwise-complete hour must drop that whole
    hour's bin, not silently aggregate the remaining 3 rows as if they were a full hour."""
    df = _synthetic_15m("2026-01-01 00:00", 4 * 3)  # 3 complete hours
    gappy = df.drop(df.index[5])  # remove one row inside hour index 1 (01:00-01:45)
    r1h = resample_ohlcv(gappy, "1h")
    assert len(r1h) == 2, f"expected only hours 0 and 2 to survive, got index:\n{r1h.index}"
    assert pd.Timestamp("2026-01-01 01:00", tz="UTC") not in r1h.index


def test_resample_empty_input_returns_empty_output():
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    empty.index = pd.DatetimeIndex([], tz="UTC")
    out = resample_ohlcv(empty, "1h")
    assert out.empty


def test_resample_rejects_unsupported_timeframe():
    df = _synthetic_15m("2026-01-01", 4)
    try:
        resample_ohlcv(df, "2h")
        assert False, "expected ValueError for an unsupported target_tf"
    except ValueError:
        pass


# --------------------------------------------------------------------------- #
# OHLCV aggregation correctness
# --------------------------------------------------------------------------- #
def test_resample_ohlc_aggregation_is_correct_on_a_hand_built_hour():
    idx = pd.date_range("2026-01-01 00:00", periods=4, freq="15min", tz="UTC")
    df = pd.DataFrame(
        {
            "open":   [10.0, 10.5, 10.2, 10.8],
            "high":   [10.6, 10.7, 10.9, 11.0],
            "low":    [9.9, 10.1, 10.0, 10.5],
            "close":  [10.5, 10.2, 10.8, 10.9],
            "volume": [1.0, 2.0, 3.0, 4.0],
        },
        index=idx,
    )
    r1h = resample_ohlcv(df, "1h")
    assert len(r1h) == 1
    row = r1h.iloc[0]
    assert row["open"] == 10.0          # first bar's open
    assert row["high"] == 11.0          # max high
    assert row["low"] == 9.9            # min low
    assert row["close"] == 10.9         # last bar's close
    assert row["volume"] == 10.0        # sum of volumes


# --------------------------------------------------------------------------- #
# Truncation invariance -- the look-ahead-hazard check, modeled on
# test_pack.py::test_truncation_no_lookahead
# --------------------------------------------------------------------------- #
def test_resample_truncation_invariant():
    """A bin present in a TRUNCATED prefix of the 15m frame must be byte-identical to that same
    bin computed from the FULL frame -- a resampled bar, once it exists, is never later revised
    by data that arrives after it. This is what the design doc's §5.4 look-ahead hazard note is
    actually about: at a 1h close t, the harness must never use a 4h/1d bar whose own close time
    is after t, and this test proves resample_ohlcv.py can't accidentally violate that by
    recomputing an already-complete bin differently once more rows exist."""
    df = _synthetic_15m("2026-02-01 00:00", 4 * 24 * 10, seed=7)  # 10 days of 15m bars
    for target_tf in ("1h", "4h", "1d"):
        full = resample_ohlcv(df, target_tf)
        for cut_bars in (4 * 24 * 2 + 3, 4 * 24 * 5 + 17, 4 * 24 * 8):
            part = resample_ohlcv(df.iloc[: cut_bars], target_tf)
            common = full.index.intersection(part.index)
            assert len(common) > 0, f"{target_tf} cut at {cut_bars}: no common complete bins to compare"
            for col in ("open", "high", "low", "close", "volume"):
                a = full.loc[common, col].to_numpy()
                b = part.loc[common, col].to_numpy()
                assert np.allclose(a, b), (
                    f"look-ahead in resample_ohlcv({target_tf}).{col} at cut={cut_bars}: "
                    f"full vs truncated differ for a bin both consider complete"
                )
            # And every bin the truncated frame DOES consider complete must also appear, with
            # the same values, in the full-frame result -- a truncated run can never invent a
            # bin the full run disagrees with.
            assert part.index.isin(full.index).all()


# --------------------------------------------------------------------------- #
# Round trip through the CSV shape backtest_live_bot.load_csv expects
# --------------------------------------------------------------------------- #
def test_write_derived_csv_round_trips_through_load_15m_csv(tmp_path):
    df = _synthetic_15m("2026-01-01 00:00", 4 * 24 * 2)
    r1h = resample_ohlcv(df, "1h")
    out_path = tmp_path / "X_USD_1h.csv"
    write_derived_csv(r1h, out_path)

    back = load_15m_csv(out_path)
    assert (back.index == r1h.index).all()
    for col in ("open", "high", "low", "close", "volume"):
        assert np.allclose(back[col].to_numpy(), r1h[col].to_numpy())


def test_write_derived_parquet_round_trips_through_load_15m_csv(tmp_path):
    """Phase 2.0's actual on-disk format (docs/system-redesign-2026-10.md §5.4: "stored
    gitignored as parquet, not as CSV") -- same round trip, `.parquet` suffix instead of `.csv`."""
    df = _synthetic_15m("2026-01-01 00:00", 4 * 24 * 2)
    r1h = resample_ohlcv(df, "1h")
    out_path = tmp_path / "X_USD_1h.parquet"
    write_derived_csv(r1h, out_path)
    assert out_path.exists()

    back = load_15m_csv(out_path)
    assert (back.index == r1h.index).all()
    for col in ("open", "high", "low", "close", "volume"):
        assert np.allclose(back[col].to_numpy(), r1h[col].to_numpy())


def test_derive_all_timeframes_writes_parquet_by_default(tmp_path):
    """derive_all_timeframes' default out_format="parquet" (Phase 2.0's actual on-disk format)
    writes <BASE>_<quote>_{15m,1h,4h,1d}.parquet from a raw 15m CSV, and the row counts it
    returns match each derived file's own length."""
    src_dir = tmp_path / "raw"
    out_dir = tmp_path / "derived"
    df = _synthetic_15m("2026-01-01 00:00", 4 * 24 * 10)
    raw_path = src_dir / "BTC_USD_15m.csv"
    write_derived_csv(df, raw_path)  # raw fetch output is always CSV

    counts = derive_all_timeframes(src_dir, out_dir, quote="USD")
    assert counts["BTC"]["15m"] == len(df)
    for tf in ("15m", "1h", "4h", "1d"):
        p = out_dir / f"BTC_USD_{tf}.parquet"
        assert p.exists(), f"expected {p} to exist"
        back = load_15m_csv(p)
        assert len(back) == counts["BTC"][tf]


if __name__ == "__main__":
    import tempfile

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        if "tmp_path" in t.__code__.co_varnames[: t.__code__.co_argcount]:
            with tempfile.TemporaryDirectory() as d:
                t(Path(d))
        else:
            t()
        print("PASS", t.__name__)
    print(f"{len(tests)} tests passed")
