"""Tests for phase2_weekly_filter.py (docs/phase2_weekly_filter_study.md §0.9).

Weekly-bin alignment against Kraken's native 1w closes, no look-ahead (a weekly bar is only usable
once CLOSED), truncation invariance for W1/W2/W3 (also on the seeded series), fail-closed warm-up,
seed stitching, signal filtering, and the bootstrap/statistics helpers.

Run: pytest strategy_pack_run/test_phase2_weekly_filter.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import phase2_weekly_filter as p2  # noqa: E402
from backtest_live_bot import load_csv, simulate_portfolio  # noqa: E402
from engine import Costs  # noqa: E402
from src.signal_engine import Signal  # noqa: E402

FAST, SLOW = 12, 26
UTC = "UTC"


def _daily(values, start="2023-01-05") -> pd.Series:
    """Daily closes indexed by CLOSE time; first bar OPENS on `start` (2023-01-05 is a Thursday)."""
    opens = pd.date_range(start, periods=len(values), freq="1D", tz=UTC)
    return pd.Series(np.asarray(values, float), index=opens + pd.Timedelta(days=1), name="close")


def _random_walk(n: int, seed: int, start="2022-03-12") -> pd.Series:
    rng = np.random.default_rng(seed)
    return _daily(100 * np.exp(np.cumsum(rng.normal(0, 0.04, n))), start=start)


# --------------------------------------------------------------------------- #
# 1. Weekly-bin alignment
# --------------------------------------------------------------------------- #
def test_weekly_bins_are_thursday_anchored_and_close_seven_days_later():
    d = _random_walk(200, 1, start="2024-10-12")  # starts on a Saturday, like data/
    w = p2.weekly_closes(d)
    assert set(w.index.day_name()) == {"Thursday"}
    assert (w.index.hour == 0).all()
    # each weekly close equals the close of the last daily bar opened before the weekly close time
    for t, v in w.items():
        in_week = d[(d.index - pd.Timedelta(days=1) >= t - pd.Timedelta(days=7)) & (d.index <= t)]
        assert v == in_week.iloc[-1]


def test_168h_epoch_equals_w_thu_left_left():
    d = _random_walk(400, 2, start="2021-02-03")
    by_open = pd.Series(d.to_numpy(), index=d.index - pd.Timedelta(days=1))
    a = by_open.resample("168h", origin="epoch").last()
    b = by_open.resample("W-THU", closed="left", label="left").last()
    assert a.index.equals(b.index) and np.allclose(a.to_numpy(), b.to_numpy(), equal_nan=True)


# Kraken native 1w closes (public OHLC endpoint, interval=10080), fetched 2026-10-19 for the
# pre-registration check (doc §0.2). Keyed by weekly CLOSE time (= Kraken bin open + 7 days).
KRAKEN_BTC_1W_CLOSES = {
    "2024-10-17": 67589.9,  # bin 2024-10-10; partial in data/ (data/ starts Sat 2024-10-12) but same last close
    "2024-10-24": 66633.4,
    "2024-10-31": 72331.1,
    "2024-11-07": 75642.0,
    "2024-11-14": 90490.9,
    "2024-11-21": 94364.5,
    "2026-09-17": 76146.1,
    "2026-09-24": 84384.6,
    "2026-10-01": 83564.7,
}


def test_resampled_kraken_daily_matches_native_kraken_weekly_closes():
    path = _THIS_DIR / "data" / "BTC_USD_1d.csv"
    if not path.exists():
        pytest.skip("committed data/ not present")
    w = p2.weekly_closes(load_csv(path)["close"])
    for day, close in KRAKEN_BTC_1W_CLOSES.items():
        assert w.loc[pd.Timestamp(day, tz=UTC)] == pytest.approx(close, rel=0, abs=1e-9)
    # data/ ends with the Wednesday 2026-09-30 bar (closes Thu 2026-10-01): the last week is complete,
    # and no forming week exists beyond it.
    assert w.index[-1] == pd.Timestamp("2026-10-01", tz=UTC)


# --------------------------------------------------------------------------- #
# 2. Availability: a weekly bar is usable only once closed
# --------------------------------------------------------------------------- #
def _flip_on_last_week():
    """40 weeks of decline, then the final week's LAST daily bar (Wednesday) jumps 100x. The weekly
    verdict must be False until the decision at that week's close time (Thursday 00:00)."""
    n_days = 7 * 41
    vals = 1000 * np.exp(-0.01 * np.arange(n_days))
    vals[-1] = vals[-2] * 100.0
    d = _daily(vals, start="2023-01-05")  # Thursday start: exactly 41 full weeks
    return d


def test_weekly_bar_not_visible_before_its_close():
    d = _flip_on_last_week()
    w = p2.weekly_closes(d)
    week_close = w.index[-1]
    assert week_close.day_name() == "Thursday" and week_close == d.index[-1]
    assert not p2.weekly_trend_at(w, week_close - pd.Timedelta(days=1), FAST, SLOW)  # Wed 00:00
    assert not p2.weekly_trend_at(w, week_close - pd.Timedelta(seconds=1), FAST, SLOW)
    assert not p2.weekly_trend_at(w, week_close - pd.Timedelta(days=7), FAST, SLOW)  # previous Thursday
    assert p2.weekly_trend_at(w, week_close, FAST, SLOW)  # flips exactly at the Thursday 00:00 decision
    s = p2.weekly_gate_series(w, d.index, FAST, SLOW)
    assert not s.iloc[:-1].any() and s.iloc[-1]


def test_monday_decision_uses_previous_thursday_week():
    d = _random_walk(500, 3)
    w = p2.weekly_closes(d)
    mondays = [t for t in d.index if t.day_name() == "Monday"][10:20]
    for t in mondays:
        last_week_close = w.loc[:t].index[-1]
        assert last_week_close.day_name() == "Thursday"
        assert t - last_week_close == pd.Timedelta(days=4)


# --------------------------------------------------------------------------- #
# 3. Truncation invariance (no look-ahead), W1/W2/W3, raw and seeded
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("seed", [4, 5])
def test_truncation_invariance_w1_w2_w3(seed):
    d = _random_walk(700, seed)
    btc = _random_walk(700, seed + 100)
    full_w1 = p2.weekly_gate_series(p2.weekly_closes(d), d.index, FAST, SLOW)
    full_w2 = p2.btc_regime_series(btc, d.index)
    gates = p2.build_gates({"X": d}, btc, d.index, FAST, SLOW)
    assert gates["W1"]["X"].equals(full_w1) and gates["W2"]["X"].equals(full_w2)
    assert gates["W3"]["X"].equals(full_w1 & full_w2)
    assert full_w1.any() and (~full_w1).any() and full_w2.any() and (~full_w2).any()  # non-trivial
    rng = np.random.default_rng(seed)
    for i in sorted(rng.choice(np.arange(150, 700), 60, replace=False)):
        t = d.index[i]
        w1_t = p2.weekly_trend_at(p2.weekly_closes(d.loc[:t]), t, FAST, SLOW)
        w2_t = bool(p2.btc_regime_series(btc.loc[:t], pd.DatetimeIndex([t])).iloc[0])
        assert w1_t == bool(full_w1.loc[t]), t
        assert w2_t == bool(full_w2.loc[t]), t
        g_t = p2.build_gates({"X": d.loc[:t]}, btc.loc[:t], pd.DatetimeIndex([t]), FAST, SLOW)
        assert bool(g_t["W3"]["X"].iloc[0]) == bool(gates["W3"]["X"].loc[t])


def test_truncation_invariance_on_seeded_series():
    seed_src = _random_walk(900, 7, start="2020-01-01")
    primary = _random_walk(400, 8, start="2022-06-01")
    stitched = p2.stitch_closes(primary, seed_src)
    full = p2.weekly_gate_series(p2.weekly_closes(stitched), primary.index, FAST, SLOW)
    for t in primary.index[::9]:
        trunc = p2.stitch_closes(primary.loc[:t], seed_src)  # seed fully known, primary only to t
        assert p2.weekly_trend_at(p2.weekly_closes(trunc), t, FAST, SLOW) == bool(full.loc[t])


def test_gate_series_equals_pointwise_reference():
    d = _random_walk(600, 9)
    w = p2.weekly_closes(d)
    s = p2.weekly_gate_series(w, d.index, FAST, SLOW)
    ref = pd.Series([p2.weekly_trend_at(w, t, FAST, SLOW) for t in d.index], index=d.index)
    assert s.equals(ref)


# --------------------------------------------------------------------------- #
# 4. Fail closed during warm-up
# --------------------------------------------------------------------------- #
def test_w1_fails_closed_below_slow_weekly_bars():
    d = _daily(np.linspace(100, 300, 7 * 30), start="2023-01-05")  # steady uptrend, 30 full weeks
    w = p2.weekly_closes(d)
    assert p2.weekly_trend_at(w, w.index[SLOW - 2], FAST, SLOW) is False  # 25 closed weeks
    assert p2.weekly_trend_at(w, w.index[SLOW - 1], FAST, SLOW) is True  # 26 closed weeks
    s = p2.weekly_gate_series(w, d.index, FAST, SLOW)
    assert not s.loc[: w.index[SLOW - 1] - pd.Timedelta(days=1)].any()


def test_w2_fails_closed_below_200_btc_bars():
    btc = _daily(np.linspace(100, 300, 260))
    r = p2.btc_regime_series(btc, btc.index)
    assert not r.iloc[:199].any()
    assert r.iloc[199:].all()
    # a decision before the first BTC close is False, not an error
    early = p2.btc_regime_series(btc, pd.DatetimeIndex([btc.index[0] - pd.Timedelta(days=3)]))
    assert not early.iloc[0]


# --------------------------------------------------------------------------- #
# 5. Seed stitching
# --------------------------------------------------------------------------- #
def test_stitch_uses_seed_strictly_before_primary_and_never_overwrites():
    seed = _daily(np.full(100, 1.0), start="2023-01-01")
    primary = _daily(np.full(50, 2.0), start="2023-03-01")
    s = p2.stitch_closes(primary, seed)
    assert s.index.is_monotonic_increasing and not s.index.duplicated().any()
    assert (s.loc[primary.index] == 2.0).all()
    assert (s.loc[s.index < primary.index[0]] == 1.0).all()
    assert len(s) == len(primary) + int((seed.index < primary.index[0]).sum())
    assert p2.stitch_closes(primary, None).equals(primary)


# --------------------------------------------------------------------------- #
# 6. Signal filtering and simulate_portfolio identity
# --------------------------------------------------------------------------- #
def _sig() -> Signal:
    return Signal(direction="bullish", primary_pattern="BREAKOUT", engine_score=0.8,
                  supporting_patterns=["BREAKOUT"], primary_details={}, rationale={})


def test_apply_gate_removes_only_vetoed_and_keeps_identity():
    idx = pd.date_range("2024-01-02", periods=5, freq="1D", tz=UTC)
    s1, s2, s3 = _sig(), _sig(), _sig()
    sigs = {"A": {idx[1]: s1, idx[3]: s2}, "B": {idx[2]: s3}}
    gate = {"A": pd.Series([True, True, True, False, True], index=idx), "B": pd.Series(False, index=idx)}
    out = p2.apply_gate(sigs, gate)
    assert out == {"A": {idx[1]: s1}, "B": {}}
    assert out["A"][idx[1]] is s1
    assert sigs == {"A": {idx[1]: s1, idx[3]: s2}, "B": {idx[2]: s3}}  # input not mutated
    with pytest.raises(KeyError):
        p2.apply_gate(sigs, {"A": gate["A"]})


PLANNER = dict(entry_buffer_atr_mult=0.1, stop_atr_mult=1.0, invalidation_atr_mult=0.5,
               min_risk_reward=2.0, adx_trend_threshold=25, trend_stop_atr_bonus=0.3)
RISK = dict(starting_cash_usd=10_000.0, max_position_pct=0.10, max_total_exposure_pct=0.40,
            max_loss_per_trade_pct=0.01, max_daily_loss_pct=0.9, max_volatility_pct=1.0,
            min_risk_reward=1.0, cooldown_minutes=0, max_new_entries_per_window=50,
            new_entry_window_minutes=1, min_order_quote_usd=10)


def _trend_frame(n=40, start=100.0, step=1.0):
    idx = pd.date_range("2024-01-02", periods=n, freq="1D", tz=UTC)
    c = start + step * np.arange(n)
    return pd.DataFrame({"open": c, "high": c + 0.5, "low": c - 0.5, "close": c, "volume": 1.0, "atr": 2.0}, index=idx)


def test_all_true_gate_reproduces_baseline_and_a_veto_removes_the_trade():
    eg = {"A": _trend_frame(), "B": _trend_frame(start=50.0, step=-0.5)}
    idx = eg["A"].index
    sigs = {"A": {idx[2]: _sig()}, "B": {idx[2]: _sig()}}
    run = lambda s: simulate_portfolio(eg, s, Costs(0.0038, 0.0005), RISK, PLANNER, 10_000.0, 365.0, _REPO_ROOT,
                                       count_same_bar_approvals=True)
    base = run(sigs)
    assert len(base.trades) == 2
    same = run(p2.apply_gate(sigs, {s: pd.Series(True, index=idx) for s in eg}))
    pd.testing.assert_frame_equal(base.trades, same.trades)
    assert base.equity.equals(same.equity)
    veto_b = run(p2.apply_gate(sigs, {"A": pd.Series(True, index=idx), "B": pd.Series(False, index=idx)}))
    assert list(veto_b.trades["symbol"]) == ["A"]


# --------------------------------------------------------------------------- #
# 7. Statistics helpers
# --------------------------------------------------------------------------- #
def _trades(rows):
    return pd.DataFrame(rows, columns=["symbol", "entry_time", "R"]).assign(
        entry_time=lambda f: pd.to_datetime(f["entry_time"], utc=True))


def test_day_block_ci_is_degenerate_when_each_day_has_the_same_mean():
    tr = _trades([("A", f"2024-01-0{d}", r) for d in range(1, 8) for r in (-1.0, 3.0)])
    lo, hi = p2.day_block_ci(tr)
    assert lo == pytest.approx(1.0) and hi == pytest.approx(1.0)
    # day blocks keep same-day trades together: with all wins on one day and losses on others,
    # resampling the days gives a wider CI than iid resampling would need -- at least it is not degenerate
    tr2 = _trades([("A", "2024-01-01", 3.0)] * 5 + [("B", f"2024-01-0{d}", -1.0) for d in range(2, 8)])
    lo2, hi2 = p2.day_block_ci(tr2)
    assert lo2 < tr2["R"].mean() < hi2


def test_paired_delta_is_zero_width_when_runs_identical_and_detects_a_shift():
    tr = _trades([("A", f"2024-01-{d:02d}", r) for d, r in zip(range(1, 21), np.tile([-1.0, 2.5], 10))])
    same = p2.paired_delta_ci(tr, tr.copy())
    assert same["delta"] == 0 and same["lo"] == 0 and same["hi"] == 0 and same["skipped"] == 0
    better = tr.assign(R=tr["R"] + 0.5)
    d = p2.paired_delta_ci(tr, better)
    assert d["delta"] == pytest.approx(0.5) and d["lo"] == pytest.approx(0.5) and d["hi"] == pytest.approx(0.5)


def test_leave_one_year_out_and_per_year():
    b = _trades([("A", "2022-05-01", -1.0), ("A", "2023-05-01", 2.0), ("A", "2024-05-01", 0.0)])
    c = _trades([("A", "2023-05-01", 2.0), ("A", "2024-05-01", 0.0)])  # C vetoed the 2022 loser
    assert p2.per_year(b) == {2022: (1, -1.0), 2023: (1, 2.0), 2024: (1, 0.0)}
    loyo = p2.leave_one_year_out(b, c)
    assert loyo[2022] == pytest.approx(0.0)  # without 2022 both runs are identical
    assert loyo[2023] == pytest.approx(0.0 - (-0.5))
    assert loyo[2024] == pytest.approx(2.0 - 0.5)
    diff = p2.trade_diff(b, c)
    assert diff["removed_n"] == 1 and diff["removed_avg_R"] == -1.0 and diff["added_n"] == 0 and diff["common"] == 2
