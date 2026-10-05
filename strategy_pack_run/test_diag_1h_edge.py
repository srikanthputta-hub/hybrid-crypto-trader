"""test_diag_1h_edge.py - proves the two pieces of NEW analysis logic in diag_1h_edge.py behave as
documented (everything else there is a direct call into the real, already-tested pipeline):

  1. isolated_outcomes == a plain loop over the REAL src.monitor.check_open with next-bar-open
     execution (the harness's own semantics), and it is truncation-invariant (no look-ahead past
     the exit fill).
  2. htf_confirmed_at reproduces compute_symbol_signals' own htf decision exactly, on both its
     branches (real daily closes for sub-daily replays; the scan window itself at daily).
  3. recording() is a pure pass-through: simulate_portfolio's output is byte-identical with and
     without it, and backtest_live_bot's module names are restored afterwards.

Run via pytest: cd strategy_pack_run && PYTHONPATH=..:. python3 -m pytest -q test_diag_1h_edge.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import backtest_live_bot as blb  # noqa: E402
from diag_1h_edge import htf_confirmed_at, isolated_outcomes, recording, replay_attributed  # noqa: E402
from engine import Costs  # noqa: E402
from test_backtest_live_bot import PLANNER_CFG, SCANNER_CFG, SIGNAL_CFG  # noqa: E402

from src.indicators import add_indicators  # noqa: E402
from src.monitor import check_open  # noqa: E402
from src.trade_plan_store import PlanRecord  # noqa: E402

COSTS = Costs(0.0038, 0.0005, "t")


def _random_walk(n: int, seed: int, freq: str = "1h", drift: float = 0.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    r = rng.normal(drift, 0.01, n)
    close = 100 * np.exp(np.cumsum(r))
    open_ = np.r_[100.0, close[:-1]] * np.exp(rng.normal(0, 0.002, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.004, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.004, n)))
    vol = rng.lognormal(3, 0.6, n)
    idx = pd.date_range("2025-01-01 01:00", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame(dict(open=open_, high=high, low=low, close=close, volume=vol), index=idx)


def _loop_reference(df, k, sl, tp, eh, costs):
    """Literal reference: backtest_live_bot's FILL-at-next-open, then src.monitor.check_open on
    every subsequent CLOSE, exit at the following bar's open."""
    o, c = df["open"].to_numpy(), df["close"].to_numpy()
    n = len(c)
    f = k + 1
    plan = dict(stop_loss=sl, take_profit=tp)
    for j in range(f, n):
        act = check_open(PlanRecord(symbol="X", status="OPEN", plan=plan), float(c[j]))
        if act is not None:
            if j + 1 < n:
                ex, reason = o[j + 1], act.action.lower()
            else:
                ex, reason = c[j], "end"
            break
    else:
        ex, reason = c[n - 1], "end"
    en = o[f]
    en_px, ex_px = en * (1 + costs.slippage), ex * (1 - costs.slippage)
    qty = (1 - costs.fee) / en_px
    net = qty * ex_px * (1 - costs.fee) - 1.0
    return net / (qty * (eh - sl)), (ex - en) / (eh - sl), reason


def test_isolated_outcomes_matches_real_check_open_loop():
    df = _random_walk(1500, seed=1)
    rng = np.random.default_rng(7)
    ks = rng.integers(20, 1450, 300)
    c = df["close"].to_numpy()
    width = rng.uniform(0.005, 0.06, len(ks))
    eh = c[ks] * (1 + 0.001)
    sl = eh * (1 - width)
    tp = eh + 2.0 * (eh - sl)
    iso = isolated_outcomes(df, ks, sl, tp, eh, COSTS, chunk=37)  # odd chunk exercises the chunk boundary
    for i, k in enumerate(ks):
        r, rg, reason = _loop_reference(df, int(k), sl[i], tp[i], eh[i], COSTS)
        assert iso.reason[i] == reason
        assert np.isclose(iso.R[i], r, rtol=1e-12, atol=1e-12)
        assert np.isclose(iso.R_gross[i], rg, rtol=1e-12, atol=1e-12)
    assert set(iso.reason) >= {"stop_loss", "take_profit"}


def test_isolated_outcomes_is_truncation_invariant_after_exit():
    """Nothing after the exit FILL bar may influence a trade's outcome (no look-ahead)."""
    df = _random_walk(800, seed=3)
    c = df["close"].to_numpy()
    k = 100
    eh = c[k] * 1.001
    sl, tp = eh * 0.97, eh + 2 * eh * 0.03
    full = isolated_outcomes(df, np.array([k]), np.array([sl]), np.array([tp]), np.array([eh]), COSTS)
    assert full.reason[0] in ("stop_loss", "take_profit")
    exit_fill_bar = k + 1 + int(full.bars_held[0])
    cut = df.iloc[: exit_fill_bar + 1]
    trunc = isolated_outcomes(cut, np.array([k]), np.array([sl]), np.array([tp]), np.array([eh]), COSTS)
    assert trunc.reason[0] == full.reason[0] and np.isclose(trunc.R[0], full.R[0])
    # And scrambling everything after the exit fill bar changes nothing either.
    scrambled = df.copy()
    scrambled.iloc[exit_fill_bar + 1 :, :4] = 1e-6
    s = isolated_outcomes(scrambled, np.array([k]), np.array([sl]), np.array([tp]), np.array([eh]), COSTS)
    assert np.isclose(s.R[0], full.R[0])


def test_isolated_outcomes_fills_at_next_open_not_planning_close():
    df = _random_walk(200, seed=5)
    k = 50
    c = df["close"].to_numpy()
    eh = c[k] * 1.001
    sl, tp = eh * 0.5, eh * 10  # never triggered -> 'end'
    bumped = df.copy()
    bumped.iloc[k + 1, bumped.columns.get_loc("open")] *= 1.05
    a = isolated_outcomes(df, np.array([k]), np.array([sl]), np.array([tp]), np.array([eh]), COSTS)
    b = isolated_outcomes(bumped, np.array([k]), np.array([sl]), np.array([tp]), np.array([eh]), COSTS)
    assert a.reason[0] == "end" and not np.isclose(a.R[0], b.R[0])
    assert np.isclose(b.fill_gap_R[0], (df.open.iat[k + 1] * 1.05 - eh) / (eh - sl))


def _hourly_with_daily(seed: int):
    h = _random_walk(24 * 60, seed=seed, drift=0.0002)
    # Daily closes derived the same way resample_ohlcv does (close-time index = open + 1d): the
    # close of the 24th hourly bar of each UTC day.
    d = h["close"].resample("1D", label="right", closed="right").last().dropna()
    return h, d


def test_htf_confirmed_at_matches_compute_symbol_signals_with_daily_closes():
    for seed in (11, 12, 13):
        raw, daily = _hourly_with_daily(seed)
        enr = add_indicators(raw, 12, 26, 14)
        everything = blb.compute_symbol_signals(enr, SCANNER_CFG, SIGNAL_CFG, False, 12, 26)
        ref = blb.compute_symbol_signals(enr, SCANNER_CFG, SIGNAL_CFG, True, 12, 26, daily_closes=daily)
        pos = {ts: i for i, ts in enumerate(enr.index)}
        mine = [ts for ts in everything if htf_confirmed_at(enr, pos[ts], 12, 26, daily)]
        assert mine == list(ref)
        assert 0 < len(ref) < len(everything), "test data must exercise both htf outcomes"


def test_htf_confirmed_at_matches_compute_symbol_signals_daily_window_branch():
    raw = _random_walk(700, seed=21, freq="1D", drift=0.0005)
    enr = add_indicators(raw, 12, 26, 14)
    everything = blb.compute_symbol_signals(enr, SCANNER_CFG, SIGNAL_CFG, False, 12, 26)
    ref = blb.compute_symbol_signals(enr, SCANNER_CFG, SIGNAL_CFG, True, 12, 26)
    pos = {ts: i for i, ts in enumerate(enr.index)}
    mine = [ts for ts in everything if htf_confirmed_at(enr, pos[ts], 12, 26, None)]
    assert mine == list(ref)
    assert 0 < len(ref) < len(everything)


def _two_symbol_ds():
    raws = {"X": _random_walk(1200, seed=31, freq="1D", drift=0.001),
            "Y": _random_walk(1200, seed=32, freq="1D", drift=0.001)}
    enriched_global, confirmed, idx = blb.build_enriched_and_signals(
        raws, dict(fast_ema=12, slow_ema=26, rsi_period=14), SCANNER_CFG, SIGNAL_CFG, True
    )
    return dict(enriched_global=enriched_global, confirmed=confirmed, idx=idx, timeframe="1d")


RISK_CFG = dict(
    starting_cash_usd=10_000.0, max_position_pct=0.10, max_total_exposure_pct=0.40,
    max_loss_per_trade_pct=0.01, max_daily_loss_pct=0.05, max_volatility_pct=0.08,
    min_risk_reward=1.5, cooldown_minutes=60, kill_switch_file="__no_such_kill_switch__",
)


def test_recording_is_pure_pass_through_and_restores_module_names():
    ds = _two_symbol_ds()
    orig_tp, orig_re = blb.trade_planner, blb.RiskEngine
    args = (ds["enriched_global"], ds["confirmed"], COSTS, RISK_CFG, PLANNER_CFG, 10_000.0, 365.0, _REPO_ROOT)
    plain = blb.simulate_portfolio(*args)
    with recording() as rec:
        recorded = blb.simulate_portfolio(*args)
    assert blb.trade_planner is orig_tp and blb.RiskEngine is orig_re
    assert len(plain.trades) > 5
    pd.testing.assert_frame_equal(plain.trades, recorded.trades)
    pd.testing.assert_series_equal(plain.equity, recorded.equity)
    assert len(rec.decisions) >= len(plain.trades)


def test_replay_attributed_links_every_trade_and_passes_its_own_r_cross_check():
    ds = _two_symbol_ds()
    cfg = SimpleNamespace(risk=RISK_CFG, trade_planner=PLANNER_CFG)
    res, tr, dec = replay_attributed(ds, cfg, COSTS)
    assert len(tr) == len(res.trades) > 5
    assert (tr.plan_time < tr.entry_time).all()
    assert tr.pattern.notna().all()
    # gross + cost drag reassembles the harness's own net R
    assert np.allclose(tr.R_gross + tr.cost_drag_R, tr.R)
