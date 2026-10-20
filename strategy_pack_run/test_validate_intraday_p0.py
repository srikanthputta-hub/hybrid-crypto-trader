"""test_validate_intraday_p0.py - validate_intraday_p0.py on hand-built OHLCV frames (no network).

Run: python -m pytest -q strategy_pack_run/test_validate_intraday_p0.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import validate_intraday_p0 as v  # noqa: E402

Q = 900_000
H = 3_600_000
T0 = 1_600_000_000_000 // H * H


def _frame(ts, price0=100.0):
    rows = []
    for i, t in enumerate(ts):
        o = price0 + i
        rows.append([t, o, o + 2, o - 1, o + 1, 5.0])
    return pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])


# ---- gaps
def test_find_gap_runs_reports_start_and_missing_count():
    ts = np.array([T0, T0 + Q, T0 + 5 * Q, T0 + 6 * Q, T0 + 8 * Q])
    assert v.find_gap_runs(ts, Q) == [(T0 + 2 * Q, 3), (T0 + 7 * Q, 1)]


def test_find_gap_runs_none_for_contiguous_series():
    assert v.find_gap_runs(np.array([T0 + i * Q for i in range(10)]), Q) == []


def test_gap_stats_counts_longest_and_percentage():
    # 12 expected bars in span (T0..T0+11Q), 3 missing in one run + 1 missing elsewhere... build explicitly
    present = [0, 1, 2, 6, 7, 9, 10, 11]  # missing 3,4,5 (3 bars) and 8 (1 bar)
    ts = np.array([T0 + i * Q for i in present])
    s = v.gap_stats(ts, Q)
    assert s["expected_bars_in_span"] == 12 and s["bars"] == 8 and s["missing_bars"] == 4
    assert s["gap_runs"] == 2 and s["longest_gap_bars"] == 3 and s["longest_gap_hours"] == 0.75
    assert s["missing_pct"] == round(100 * 4 / 12, 3)
    assert s["gap_runs_ge_1h"] == 0


def test_gap_stats_24h_bucket():
    ts = np.array([T0, T0 + 97 * Q])  # 96 missing quarter-bars = 24h exactly
    s = v.gap_stats(ts, Q)
    assert s["gap_runs_ge_24h"] == 1 and s["gap_runs_ge_6h"] == 1 and s["gap_runs_ge_1h"] == 1


def test_missing_timestamps_and_market_wide_gaps_need_enough_coins():
    lo, hi = T0, T0 + 9 * Q
    a = v.missing_timestamps(np.array([T0 + i * Q for i in (0, 1, 2, 7, 8, 9)]), Q, lo, hi)   # missing 3..6
    b = v.missing_timestamps(np.array([T0 + i * Q for i in (0, 1, 2, 3, 6, 7, 8, 9)]), Q, lo, hi)  # missing 4,5
    c = v.missing_timestamps(np.array([T0 + i * Q for i in range(10)]), Q, lo, hi)  # nothing missing
    assert a == {T0 + i * Q for i in (3, 4, 5, 6)}
    runs = v.market_wide_gaps({"A": a, "B": b, "C": c}, Q, min_coins=2, min_run_bars=2)
    assert len(runs) == 1 and runs[0]["bars"] == 2 and runs[0]["min_coins_missing"] == 2
    assert runs[0]["start"] == v._iso(T0 + 4 * Q)
    assert v.market_wide_gaps({"A": a, "B": b, "C": c}, Q, min_coins=3, min_run_bars=1) == []


def test_yearly_coverage_does_not_penalise_pre_listing_time():
    start = int(pd.Timestamp("2021-06-01", tz="UTC").timestamp() * 1000)
    ts = np.array([start + i * H for i in range(100)])
    cov = v.yearly_coverage(ts, H)
    assert cov["2021"]["bars"] == 100 and cov["2021"]["expected"] == 100 and cov["2021"]["coverage_pct"] == 100.0


# ---- sanity
def test_sanity_clean_frame_has_no_violations():
    s = v.sanity_report(_frame([T0 + i * Q for i in range(20)]))
    assert s["duplicate_timestamps"] == 0 and s["zero_volume_bars"] == 0
    assert s["high_below_open_close_low"] == 0 and s["low_above_open_close_high"] == 0
    assert s["nonpositive_price_bars"] == 0 and s["nan_values"] == 0 and not s["unsorted"]


def test_sanity_flags_each_violation_type():
    df = _frame([T0 + i * Q for i in range(6)])
    df.loc[1, "high"] = df.loc[1, "close"] - 5      # high below close
    df.loc[2, "low"] = df.loc[2, "open"] + 5        # low above open
    df.loc[3, "volume"] = 0.0
    df.loc[4, "close"] = -1.0
    df.loc[5, "timestamp"] = df.loc[0, "timestamp"]  # duplicate and unsorted
    s = v.sanity_report(df)
    assert s["high_below_open_close_low"] >= 1 and s["low_above_open_close_high"] >= 1
    assert s["zero_volume_bars"] == 1 and s["nonpositive_price_bars"] == 1
    assert s["duplicate_timestamps"] == 1 and s["unsorted"] is True
    assert len(s["ohlc_violation_examples"]) >= 1


# ---- 15m -> 1h consistency
def _quarters(n_hours, drop=()):
    rows, k = [], 0
    for hr in range(n_hours):
        for q in range(4):
            t = T0 + hr * H + q * Q
            o = 100.0 + k
            if (hr, q) not in drop:
                rows.append([t, o, o + 3 + q, o - 2 - q, o + 0.5, 1.0 + q])
            k += 1
    return pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])


def _aggregate(q):
    q = q.copy()
    q["hour"] = q["timestamp"] // H * H
    g = q.groupby("hour").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                              close=("close", "last"), volume=("volume", "sum"))
    return g.reset_index().rename(columns={"hour": "timestamp"})


def test_resample_consistency_perfect_when_1h_is_exact_aggregate():
    q = _quarters(10)
    r = v.resample_consistency(q, _aggregate(q))
    assert r["hours_compared_complete"] == 10
    assert r["complete_hours_with_any_ohlc_mismatch"] == 0 and r["complete_mismatch_volume"] == 0
    assert r["hours_only_in_15m"] == 0 and r["hours_only_in_1h"] == 0


def test_resample_consistency_detects_corrupted_complete_hour():
    q = _quarters(10)
    h = _aggregate(q)
    h.loc[3, "high"] += 1.0   # a real inconsistency in a complete hour
    h.loc[5, "volume"] *= 1.5
    r = v.resample_consistency(q, h)
    assert r["complete_mismatch_high"] == 1 and r["complete_hours_with_any_ohlc_mismatch"] == 1
    assert r["complete_mismatch_volume"] == 1
    assert r["complete_mismatch_examples"] == [v._iso(T0 + 3 * H)]


def test_resample_consistency_partial_hour_uses_containment_not_equality():
    q_full = _quarters(6)
    h = _aggregate(q_full)
    q_partial = _quarters(6, drop={(2, 3)})   # last quarter of hour 2 had no trades in 15m feed
    r = v.resample_consistency(q_partial, h)
    assert r["hours_compared_complete"] == 5 and r["hours_compared_partial"] == 1
    assert r["partial_high_containment_violations"] == 0 and r["partial_low_containment_violations"] == 0
    assert r["complete_hours_with_any_ohlc_mismatch"] == 0
    assert r["partial_hours_open_or_close_differs"] == 1  # close came from the missing quarter


def test_resample_consistency_flags_1h_bar_narrower_than_its_quarters():
    q = _quarters(4, drop={(1, 0)})
    h = _aggregate(_quarters(4))
    h.loc[1, "high"] = h.loc[1, "high"] - 10   # 1h high below a 15m high that exists
    r = v.resample_consistency(q, h)
    assert r["partial_high_containment_violations"] == 1


def test_resample_consistency_counts_hours_present_in_only_one_feed():
    q = _quarters(5)
    h = _aggregate(q).drop(index=[2]).reset_index(drop=True)
    r = v.resample_consistency(q, h)
    assert r["hours_only_in_15m"] == 1
    r2 = v.resample_consistency(q.iloc[4:].reset_index(drop=True), _aggregate(q))
    assert r2["hours_only_in_1h"] == 1


def test_resample_consistency_reports_mismatch_magnitude_in_bp_and_by_year():
    q = _quarters(10)
    h = _aggregate(q)
    h.loc[3, "close"] = h.loc[3, "close"] * (1 + 20e-4)   # 1h close is 20 bp above the 15m-derived close
    r = v.resample_consistency(q, h)
    p50, p99, mx = r["complete_close_abs_bp_p50_p99_max"]
    assert 19.0 < p50 < 20.1 and mx == p50  # one mismatching hour
    assert r["complete_open_abs_bp_p50_p99_max"] == [0.0, 0.0, 0.0]  # stats cover hours with ANY mismatch; open agrees there
    by_year = r["complete_ohlc_mismatch_by_year"]
    assert sum(y["hours"] for y in by_year.values()) == 10
    assert sum(round(y["ohlc_mismatch_pct"] * y["hours"] / 100) for y in by_year.values()) == 1


def _daily_from(q, day_ms):
    d = q.copy()
    d["day"] = d["timestamp"] // day_ms * day_ms
    g = d.groupby("day").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                             close=("close", "last"), volume=("volume", "sum"))
    return g.reset_index().rename(columns={"day": "timestamp"})


def test_daily_consistency_zero_mismatch_for_exact_aggregate_and_skips_incomplete_days():
    day = 86_400_000
    start = T0 // day * day
    ts = [start + i * Q for i in range(96 * 3 - 5)]          # day 3 is incomplete (5 bars missing)
    q = _frame(ts)
    r = v.daily_consistency(q, "15m", _daily_from(q, day))
    assert sum(y["days"] for y in r.values()) == 2           # incomplete day excluded
    assert all(y["close_mismatch_pct"] == 0.0 and y["volume_mismatch_pct"] == 0.0 for y in r.values())


def test_daily_consistency_detects_which_feed_disagrees_with_daily():
    day = 86_400_000
    start = T0 // day * day
    q = _frame([start + i * Q for i in range(96 * 4)])
    d = _daily_from(q, day)
    d.loc[1, "close"] *= 1.01
    d.loc[2, "volume"] *= 2
    r = v.daily_consistency(q, "15m", d)
    tot = lambda k: sum(y["days"] * y[k] / 100 for y in r.values())  # noqa: E731
    assert round(tot("close_mismatch_pct")) == 1 and round(tot("volume_mismatch_pct")) == 1
    assert round(tot("high_mismatch_pct")) == 0


# ---- cross venue
def _cb_kr(bp_offsets):
    ts = [T0 + i * H for i in range(len(bp_offsets))]
    kr = pd.DataFrame({"timestamp": ts, "close": 100.0, "volume": 10.0})
    cb = pd.DataFrame({"timestamp": ts, "close": [100.0 * (1 + b / 1e4) for b in bp_offsets], "volume": 20.0})
    return cb, kr


def test_bp_diff_is_signed_bp_of_second_argument():
    assert np.allclose(v.bp_diff(np.array([101.0, 99.0]), np.array([100.0, 100.0])), [100.0, -100.0])


def test_cross_check_flags_nothing_for_tight_agreement():
    cb, kr = _cb_kr([1.0, -2.0, 3.0, 0.5] * 25)
    s = v.cross_check_stats(cb, kr)
    assert s["flag"] is None and s["overlap_bars"] == 100 and s["median_abs_bp"] < 10
    assert s["median_volume_ratio_cb_over_kr"] == 2.0


def test_cross_check_flags_median_over_10bp():
    cb, kr = _cb_kr([25.0] * 100)
    assert "median" in v.cross_check_stats(cb, kr)["flag"]


def test_cross_check_flags_p99_over_100bp_even_when_median_is_fine():
    cb, kr = _cb_kr([1.0] * 95 + [300.0] * 5)
    s = v.cross_check_stats(cb, kr)
    assert s["median_abs_bp"] < 10 and "p99" in s["flag"] and "median" not in s["flag"]


def test_cross_check_boundary_exactly_10bp_median_is_not_flagged():
    cb, kr = _cb_kr([10.0] * 100)
    assert v.cross_check_stats(cb, kr)["flag"] is None  # the brief says "> 10bp"


def test_cross_check_no_overlap():
    cb, kr = _cb_kr([1.0] * 5)
    kr["timestamp"] += 10 * H
    s = v.cross_check_stats(cb, kr)
    assert s["overlap_bars"] == 0 and s["flag"] is None


# ---- checksums
def test_write_sha256sums_matches_hashlib_and_uses_relative_posix_paths(tmp_path):
    import hashlib
    (tmp_path / "1h").mkdir()
    p = tmp_path / "1h" / "BTC_USD_1h.csv"
    p.write_bytes(b"timestamp,open\n1,2\n")
    out = tmp_path / "SHA256SUMS.txt"
    v.write_sha256sums([p], tmp_path, out)
    assert out.read_text() == f"{hashlib.sha256(p.read_bytes()).hexdigest()}  1h/BTC_USD_1h.csv\n"
