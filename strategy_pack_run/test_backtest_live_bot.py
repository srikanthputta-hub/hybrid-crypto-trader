"""test_backtest_live_bot.py - proves backtest_live_bot.py's HARNESS MECHANICS (next-bar-open
fills and exits, correct P&L/fee accounting, the risk engine's cooldown actually blocking a
would-be re-entry) on small, crafted price series. Same spirit and style as
strategy_pack_run/test_pack.py's mechanics tests -- this does not prove the live strategy makes
money on real crypto (see report_live_strategy_*.md for that, produced by run_live_bot_report.py).

Run directly:  python strategy_pack_run/test_backtest_live_bot.py
Run via pytest: pytest strategy_pack_run/test_backtest_live_bot.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from engine import Costs  # noqa: E402

import backtest_live_bot as blb  # noqa: E402
from backtest_live_bot import (  # noqa: E402
    TF_SECONDS,
    compute_symbol_signals,
    liquidity_window_bars,
    load_csv,
    load_dir,
    placebo_run,
    simulate_portfolio,
    trailing_quote_volume,
)
from src.indicators import add_indicators  # noqa: E402
from src import scanner, signal_engine, trade_planner  # noqa: E402
from src.signal_engine import Signal  # noqa: E402

# A generous, fixed risk config used across tests so the RiskEngine itself never becomes the
# bottleneck except where a test specifically wants it to (the cooldown test).
PERMISSIVE_RISK_CFG = dict(
    starting_cash_usd=10_000.0,
    max_position_pct=0.5,
    max_total_exposure_pct=0.9,
    max_loss_per_trade_pct=0.5,
    max_daily_loss_pct=0.5,
    max_volatility_pct=1.0,
    min_risk_reward=1.0,
    cooldown_minutes=0,
    symbol_whitelist=["X"],
)
PLANNER_CFG = dict(
    entry_buffer_atr_mult=0.1,
    stop_atr_mult=1.0,
    invalidation_atr_mult=0.5,
    min_risk_reward=2.0,
    adx_trend_threshold=25,
    trend_stop_atr_bonus=0.3,
)
SCANNER_CFG = dict(
    breakout_lookback=5, breakout_volume_multiplier=1.5, pullback_bounce_lookback=3,
    pullback_tolerance_pct=0.004, momentum_lookback=3, momentum_return_threshold=0.03,
    trend_confirm_bars=3, consolidation_bars=3, consolidation_max_range_atr=2.5,
    reversal_swing_lookback=3, reversal_rsi_oversold=30, reversal_rsi_overbought=70,
    adx_trend_threshold=25,
)
SIGNAL_CFG = dict(min_strength=0.5, confluence_bonus=0.08,
                   pattern_priority=["BREAKOUT", "TREND_CONTINUATION", "MOMENTUM", "PULLBACK", "REVERSAL"])


def _idx(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2024-01-02", periods=n, freq="1D", tz="UTC")


# --------------------------------------------------------------------------- #
# A known, deterministic BREAKOUT, built from the REAL scanner/signal_engine/trade_planner --
# 20 flat warm-up bars, then one bar that breaks cleanly above resistance on a volume spike.
# --------------------------------------------------------------------------- #
def _breakout_frame(tail_rows: list[tuple[float, float, float, float, float]]) -> pd.DataFrame:
    """20 flat warm-up bars + the breakout bar + whatever `tail_rows` (open, high, low, close,
    volume) come after it."""
    n_warm = 20
    rows = [(100.0, 101.0, 99.0, 100.0, 10.0)] * n_warm
    rows.append((100.0, 116.0, 100.0, 115.0, 50.0))  # the breakout bar itself
    rows.extend(tail_rows)
    idx = _idx(len(rows))
    o, h, l, c, v = zip(*rows)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": v}, index=idx), idx


def _known_breakout_signal(df: pd.DataFrame) -> tuple[Signal, pd.Timestamp, float, float]:
    """Confirms via the REAL pipeline (no htf_filter -- that's covered by tests/test_htf_filter.py
    already) that a BREAKOUT bullish signal fires on the warm-up frame's last bar, and returns it
    plus that bar's own price/atr. This is "knowing in advance a BREAKOUT should fire": we ask the
    real detector, rather than asserting a hand-guessed outcome."""
    enriched = add_indicators(df, fast_ema=12, slow_ema=26, rsi_period=14)
    breakout_row = enriched.iloc[20]
    candidates = scanner.scan(enriched.iloc[: 21], SCANNER_CFG)
    sig = signal_engine.evaluate(candidates, **SIGNAL_CFG)
    assert sig is not None and sig.direction == "bullish" and sig.primary_pattern == "BREAKOUT", (
        f"test setup must produce a known bullish BREAKOUT, got {sig!r}"
    )
    return sig, enriched.index[20], float(breakout_row["close"]), float(breakout_row["atr"])


def _plan_levels(sig: Signal, price: float, atr: float):
    plan = trade_planner.build_plan(sig, "X", price, atr, **PLANNER_CFG)
    assert plan is not None
    return plan


def test_known_breakout_fills_next_open_and_hits_take_profit_next_open():
    sig0, t0, price0, atr0 = _known_breakout_signal(_breakout_frame([])[0])
    plan = _plan_levels(sig0, price0, atr0)
    # entry_zone=(100.7, 115.3) stop_loss=97.7 take_profit=150.5 for this exact crafted series
    # (asserted below, not just assumed) -- the rest of the series is built around these REAL
    # numbers, not hand-guessed ones.
    assert plan.entry_zone == (100.7, 115.3)
    assert plan.stop_loss == 97.7 and plan.take_profit == 150.5

    # bar21 (fill bar, open=105): opens inside no particular constraint (FILL, once triggered,
    # always fills at the next bar's open regardless of where that open lands -- see
    # src/monitor.py / the module docstring). close=160 clears take_profit (150.5).
    # bar22 (exit bar, open=162): the actual TAKE_PROFIT exit price.
    tail = [
        (105.0, 110.0, 104.0, 160.0, 10.0),   # bar21: fills here (open), triggers TP on its own close
        (162.0, 163.0, 161.0, 162.0, 10.0),   # bar22: TP exit fills here (open)
    ]
    df, idx = _breakout_frame(tail)
    enriched = add_indicators(df, fast_ema=12, slow_ema=26, rsi_period=14)
    signals = {t0: sig0}

    res = simulate_portfolio(
        {"X": enriched}, {"X": signals}, Costs(0.0, 0.0), PERMISSIVE_RISK_CFG, PLANNER_CFG,
        start_equity=10_000.0, periods_per_year=365.0, project_root=_REPO_ROOT,
    )
    tr = res.trades
    assert len(tr) == 1, f"expected exactly one trade, got:\n{tr}"
    row = tr.iloc[0]
    assert row["entry_time"] == idx[21] and row["exit_time"] == idx[22]
    assert row["entry_px"] == 105.0, "zero-cost entry must fill at bar21's own open, unmodified"
    assert row["exit_px"] == 162.0, "zero-cost exit must fill at bar22's own open, unmodified"
    assert row["reason"] == "take_profit"
    assert row["bars_held"] == 1
    expected_pnl = row["qty"] * (162.0 - 105.0)
    assert np.isclose(row["net_pnl"], expected_pnl), "zero-cost net P&L must equal qty * (exit - entry)"
    expected_r = expected_pnl / (row["qty"] * (plan.entry_zone[1] - plan.stop_loss))
    assert np.isclose(row["R"], expected_r)
    assert row["fees"] == 0.0


def test_known_breakout_fills_next_open_and_hits_stop_loss_next_open():
    sig0, t0, price0, atr0 = _known_breakout_signal(_breakout_frame([])[0])
    plan = _plan_levels(sig0, price0, atr0)
    assert plan.stop_loss == 97.7

    tail = [
        (105.0, 106.0, 90.0, 95.0, 10.0),   # bar21: fills here (open); close 95 <= stop_loss 97.7
        (90.0, 91.0, 88.0, 90.0, 10.0),     # bar22: stop-loss exit fills here (open)
    ]
    df, idx = _breakout_frame(tail)
    enriched = add_indicators(df, fast_ema=12, slow_ema=26, rsi_period=14)
    signals = {t0: sig0}

    res = simulate_portfolio(
        {"X": enriched}, {"X": signals}, Costs(0.0, 0.0), PERMISSIVE_RISK_CFG, PLANNER_CFG,
        start_equity=10_000.0, periods_per_year=365.0, project_root=_REPO_ROOT,
    )
    tr = res.trades
    assert len(tr) == 1
    row = tr.iloc[0]
    assert row["entry_time"] == idx[21] and row["exit_time"] == idx[22]
    assert row["entry_px"] == 105.0 and row["exit_px"] == 90.0
    assert row["reason"] == "stop_loss"
    assert row["net_pnl"] < 0, "a stop-loss exit below entry must be a net loser at zero cost"
    assert np.isclose(row["net_pnl"], row["qty"] * (90.0 - 105.0))


def test_costs_reduce_net_pnl_and_produce_nonzero_fees():
    """Same take-profit scenario as above, zero-cost vs Kraken-taker costs: costs must strictly
    reduce the booked P&L and show up as nonzero fees -- mirrors test_pack.py's
    test_accounting_identity_and_costs."""
    tail = [
        (105.0, 110.0, 104.0, 160.0, 10.0),
        (162.0, 163.0, 161.0, 162.0, 10.0),
    ]
    df, idx = _breakout_frame(tail)
    sig0, t0, _, _ = _known_breakout_signal(_breakout_frame([])[0])
    enriched = add_indicators(df, fast_ema=12, slow_ema=26, rsi_period=14)
    signals = {t0: sig0}

    r0 = simulate_portfolio(
        {"X": enriched}, {"X": signals}, Costs(0.0, 0.0), PERMISSIVE_RISK_CFG, PLANNER_CFG,
        10_000.0, 365.0, _REPO_ROOT,
    )
    r1 = simulate_portfolio(
        {"X": enriched}, {"X": signals}, Costs(0.0038, 0.0005, "kraken_T3_taker"), PERMISSIVE_RISK_CFG,
        PLANNER_CFG, 10_000.0, 365.0, _REPO_ROOT,
    )
    assert len(r0.trades) == 1 and len(r1.trades) == 1
    assert r0.trades.iloc[0]["fees"] == 0.0
    assert r1.trades.iloc[0]["fees"] > 0.0
    assert r1.trades.iloc[0]["net_pnl"] < r0.trades.iloc[0]["net_pnl"], "realistic costs must reduce booked P&L"
    assert r1.equity.iloc[-1] < r0.equity.iloc[-1]


# --------------------------------------------------------------------------- #
# RiskEngine cooldown rejection correctly blocks a would-be re-entry in the loop
# --------------------------------------------------------------------------- #
def _cooldown_frame_and_signals():
    """bar1: a bullish signal fires -> immediate FILL queued for bar2's open. bar2 close (95)
    breaches stop_loss (97.8) -> STOP_LOSS queued for bar3's open. bar4: a second bullish signal
    fires for the SAME symbol -> with cooldown active this must be rejected (no new PENDING);
    with cooldown_minutes=0 it must succeed, fill at bar5's open, and stay open to the end of the
    data (force-closed as "end")."""
    rows = [
        (100.0, 101.0, 99.0, 100.0, 10.0),   # bar0: filler
        (100.0, 101.0, 99.0, 100.0, 10.0),   # bar1: signal_a fires here (close=100, atr=2.0)
        (100.0, 101.0, 94.0, 95.0, 10.0),    # bar2: fills signal_a's plan at open=100; close 95 <= stop_loss 97.8
        (94.0, 95.0, 93.0, 94.0, 10.0),      # bar3: stop-loss exit fills at open=94
        (94.0, 95.0, 93.0, 94.0, 10.0),      # bar4: signal_b fires here (close=94, atr=2.0)
        (94.0, 95.0, 93.0, 94.0, 10.0),      # bar5: fills signal_b's plan (if allowed) at open=94
    ]
    idx = _idx(len(rows))
    o, h, l, c, v = zip(*rows)
    raw = pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": v}, index=idx)
    enriched = raw.copy()
    enriched["atr"] = 2.0  # bypass add_indicators entirely -- these signals are hand-built, not scanner-detected
    sig = Signal(direction="bullish", primary_pattern="BREAKOUT", engine_score=0.8,
                 supporting_patterns=["BREAKOUT"], primary_details={}, rationale={})
    signals = {idx[1]: sig, idx[4]: sig}
    return enriched, idx, signals


def test_cooldown_rejection_blocks_a_would_be_reentry():
    enriched, idx, signals = _cooldown_frame_and_signals()

    blocked_cfg = {**PERMISSIVE_RISK_CFG, "cooldown_minutes": 10_000_000}
    res_blocked = simulate_portfolio(
        {"X": enriched}, {"X": signals}, Costs(0.0, 0.0), blocked_cfg, PLANNER_CFG, 10_000.0, 365.0, _REPO_ROOT,
    )
    assert len(res_blocked.trades) == 1, (
        "with a huge cooldown, the second signal must be rejected -- only the first (stop-loss) "
        f"trade should exist, got:\n{res_blocked.trades}"
    )
    assert res_blocked.trades.iloc[0]["reason"] == "stop_loss"

    allowed_cfg = {**PERMISSIVE_RISK_CFG, "cooldown_minutes": 0}
    res_allowed = simulate_portfolio(
        {"X": enriched}, {"X": signals}, Costs(0.0, 0.0), allowed_cfg, PLANNER_CFG, 10_000.0, 365.0, _REPO_ROOT,
    )
    assert len(res_allowed.trades) == 2, (
        "with no cooldown, the second signal must go through, fill, and (since the data ends "
        f"right after) get force-closed at the end -- got:\n{res_allowed.trades}"
    )
    assert list(res_allowed.trades["reason"]) == ["stop_loss", "end"]
    assert res_allowed.trades.iloc[1]["entry_time"] == idx[5]


# --------------------------------------------------------------------------- #
# Optional RiskEngine keys the harness forwards (min_order_quote_usd, PR #86 / issue #81)
# --------------------------------------------------------------------------- #
# _cooldown_frame_and_signals() with cooldown_minutes=0 books two ~$5000 / ~$4850 trades
# (max_position_pct=0.5 of $10k equity, then of the post-stop-loss equity) -- values printed
# from a throwaway run before being transcribed here, not guessed.
def _run_with_risk_overrides(**overrides):
    enriched, _idx_, signals = _cooldown_frame_and_signals()
    cfg = {**PERMISSIVE_RISK_CFG, "cooldown_minutes": 0, **overrides}
    return simulate_portfolio(
        {"X": enriched}, {"X": signals}, Costs(0.0, 0.0), cfg, PLANNER_CFG, 10_000.0, 365.0, _REPO_ROOT,
    )


def test_min_order_quote_usd_above_every_sized_order_rejects_all_trades():
    baseline = _run_with_risk_overrides()
    assert len(baseline.trades) == 2  # sanity: the floor-free run does trade
    res = _run_with_risk_overrides(min_order_quote_usd=6_000.0)
    assert len(res.trades) == 0, f"every sized order (<= $5000) is below the floor, got:\n{res.trades}"


def test_min_order_quote_usd_rejects_only_the_below_floor_trade():
    # floor 4900 sits between the first order (~$5000) and the second (~$4850)
    res = _run_with_risk_overrides(min_order_quote_usd=4_900.0)
    assert len(res.trades) == 1
    assert res.trades.iloc[0]["reason"] == "stop_loss"
    assert np.isclose(res.trades.iloc[0]["qty"] * res.trades.iloc[0]["entry_px"], 5_000.0)


def test_min_order_quote_usd_absent_zero_and_tiny_floor_are_identical_to_before():
    absent = _run_with_risk_overrides()
    for floor in (0, 0.0, 1.0):
        res = _run_with_risk_overrides(min_order_quote_usd=floor)
        pd.testing.assert_frame_equal(res.trades, absent.trades)
        pd.testing.assert_series_equal(res.equity, absent.equity)


def test_harness_forwards_min_order_quote_usd_to_the_risk_engine_it_builds():
    """Direct check on the constructed engine (spy on the class the harness imported)."""
    built = []
    real = blb.RiskEngine

    def spy(*a, **kw):
        eng = real(*a, **kw)
        built.append(eng)
        return eng

    with patch.object(blb, "RiskEngine", spy):
        _run_with_risk_overrides(min_order_quote_usd=25.0)
        _run_with_risk_overrides()
    assert [e.min_order_quote_usd for e in built] == [25.0, 0.0]


# --------------------------------------------------------------------------- #
# Stage A sanity: compute_symbol_signals finds the known breakout and nothing spurious around it
# --------------------------------------------------------------------------- #
def test_compute_symbol_signals_finds_the_known_breakout():
    df, idx = _breakout_frame([(105.0, 110.0, 104.0, 108.0, 10.0)] * 3)
    enriched = add_indicators(df, fast_ema=12, slow_ema=26, rsi_period=14)
    signals = compute_symbol_signals(enriched, SCANNER_CFG, SIGNAL_CFG, htf_enabled=False, fast_ema=12, slow_ema=26)
    assert idx[20] in signals, "the known breakout bar must be detected"
    assert signals[idx[20]].primary_pattern == "BREAKOUT"
    assert signals[idx[20]].direction == "bullish"


# --------------------------------------------------------------------------- #
# P1c: use_rank_order (docs/p1c_rank_signals_validation.md) -- per-bar scarce-slot competition
# --------------------------------------------------------------------------- #
RANK_ORDER_RISK_CFG = dict(
    starting_cash_usd=10_000.0,
    max_position_pct=0.5,
    max_total_exposure_pct=0.9,
    max_loss_per_trade_pct=0.5,
    max_daily_loss_pct=0.5,
    max_volatility_pct=1.0,
    min_risk_reward=1.0,
    cooldown_minutes=0,
    symbol_whitelist=["AAA", "ZZZ"],
    # Tight on purpose: exactly one of the two symbols below can win the only slot.
    max_new_entries_per_window=1,
    new_entry_window_minutes=120,
)


def _flat_frame(n: int, volume: float = 10.0) -> tuple[pd.DataFrame, pd.DatetimeIndex]:
    """A constant-price, constant-ATR frame -- bypasses add_indicators/scanner entirely (same
    style as _cooldown_frame_and_signals above) since these tests hand-build the Signal that
    fires, and only care about which of two competing symbols gets the new-entry slot, not about
    pattern detection."""
    idx = _idx(n)
    df = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": volume}, index=idx
    )
    df["atr"] = 2.0
    return df, idx


def _hand_signal(engine_score: float) -> Signal:
    return Signal(direction="bullish", primary_pattern="BREAKOUT", engine_score=engine_score,
                  supporting_patterns=["BREAKOUT"], primary_details={}, rationale={})


def test_simulate_portfolio_use_rank_order_defaults_false_and_is_unchanged():
    """Calling simulate_portfolio without use_rank_order must behave exactly like passing
    use_rank_order=False explicitly -- guards against the default ever silently flipping."""
    enriched, idx, signals = _cooldown_frame_and_signals()
    res_implicit = simulate_portfolio(
        {"X": enriched}, {"X": signals}, Costs(0.0, 0.0), PERMISSIVE_RISK_CFG, PLANNER_CFG,
        10_000.0, 365.0, _REPO_ROOT,
    )
    res_explicit = simulate_portfolio(
        {"X": enriched}, {"X": signals}, Costs(0.0, 0.0), PERMISSIVE_RISK_CFG, PLANNER_CFG,
        10_000.0, 365.0, _REPO_ROOT, use_rank_order=False,
    )
    pd.testing.assert_frame_equal(res_implicit.trades, res_explicit.trades)
    pd.testing.assert_series_equal(res_implicit.equity, res_explicit.equity)


def test_rank_order_picks_higher_engine_score_over_alphabetical_order():
    """Two symbols signal on the SAME bar; the new-entry cap only allows one of them through.
    sorted(symbols) order hands the slot to 'AAA' (alphabetically first) regardless of strength.
    rank_signals order must instead hand it to 'ZZZ', which has the higher engine_score."""
    enr_a, idx = _flat_frame(3)
    enr_z, _ = _flat_frame(3)
    enriched = {"AAA": enr_a, "ZZZ": enr_z}
    signals = {"AAA": {idx[0]: _hand_signal(0.3)}, "ZZZ": {idx[0]: _hand_signal(0.9)}}

    res_sorted = simulate_portfolio(
        enriched, signals, Costs(0.0, 0.0), RANK_ORDER_RISK_CFG, PLANNER_CFG,
        10_000.0, 365.0, _REPO_ROOT, use_rank_order=False,
    )
    res_rank = simulate_portfolio(
        enriched, signals, Costs(0.0, 0.0), RANK_ORDER_RISK_CFG, PLANNER_CFG,
        10_000.0, 365.0, _REPO_ROOT, use_rank_order=True,
    )
    assert list(res_sorted.trades["symbol"]) == ["AAA"], (
        f"sorted(symbols) order must pick the alphabetically-first symbol, got:\n{res_sorted.trades}"
    )
    assert list(res_rank.trades["symbol"]) == ["ZZZ"], (
        f"rank_signals order must pick the higher-engine_score symbol, got:\n{res_rank.trades}"
    )


def test_rank_order_tie_break_uses_liquidity_when_engine_score_ties():
    """Equal engine_score on both symbols this time -- rank_signals must tie-break on
    trailing_quote_volume (ZZZ's volume is 1,000,000x AAA's), still overriding alphabetical
    sorted(symbols) order."""
    enr_a, idx = _flat_frame(3, volume=1.0)
    enr_z, _ = _flat_frame(3, volume=1_000_000.0)
    enriched = {"AAA": enr_a, "ZZZ": enr_z}
    signals = {"AAA": {idx[0]: _hand_signal(0.8)}, "ZZZ": {idx[0]: _hand_signal(0.8)}}

    res_sorted = simulate_portfolio(
        enriched, signals, Costs(0.0, 0.0), RANK_ORDER_RISK_CFG, PLANNER_CFG,
        10_000.0, 365.0, _REPO_ROOT, use_rank_order=False,
    )
    res_rank = simulate_portfolio(
        enriched, signals, Costs(0.0, 0.0), RANK_ORDER_RISK_CFG, PLANNER_CFG,
        10_000.0, 365.0, _REPO_ROOT, use_rank_order=True,
    )
    assert list(res_sorted.trades["symbol"]) == ["AAA"]
    assert list(res_rank.trades["symbol"]) == ["ZZZ"], (
        f"rank_signals order must tie-break on higher trailing_quote_volume, got:\n{res_rank.trades}"
    )


# --------------------------------------------------------------------------- #
# P1d (docs/system-redesign-2026-10.md §4.4/§8): simulate_portfolio's new
# `eligible_mask_by_symbol` (point-in-time universe gate, universe_eligibility.py) and
# `costs_by_symbol` (tiered slippage, tiered_slippage.py) parameters. Both default to None,
# preserving every existing test/caller above this point byte-for-byte.
# --------------------------------------------------------------------------- #
def test_eligible_mask_by_symbol_default_none_is_unchanged():
    """Passing neither new parameter, or passing both as None explicitly, must produce identical
    results -- guards against either default ever silently changing behavior."""
    enriched, idx, signals = _cooldown_frame_and_signals()
    res_implicit = simulate_portfolio(
        {"X": enriched}, {"X": signals}, Costs(0.0, 0.0), PERMISSIVE_RISK_CFG, PLANNER_CFG,
        10_000.0, 365.0, _REPO_ROOT,
    )
    res_explicit_none = simulate_portfolio(
        {"X": enriched}, {"X": signals}, Costs(0.0, 0.0), PERMISSIVE_RISK_CFG, PLANNER_CFG,
        10_000.0, 365.0, _REPO_ROOT, eligible_mask_by_symbol=None, costs_by_symbol=None,
    )
    pd.testing.assert_frame_equal(res_implicit.trades, res_explicit_none.trades)
    pd.testing.assert_series_equal(res_implicit.equity, res_explicit_none.equity)


def test_eligible_mask_by_symbol_blocks_new_entry_when_ineligible_everywhere():
    sig0, t0, price0, atr0 = _known_breakout_signal(_breakout_frame([])[0])
    tail = [
        (105.0, 110.0, 104.0, 160.0, 10.0),
        (162.0, 163.0, 161.0, 162.0, 10.0),
    ]
    df, idx = _breakout_frame(tail)
    enriched = add_indicators(df, fast_ema=12, slow_ema=26, rsi_period=14)
    signals = {t0: sig0}
    n = len(idx)

    res_blocked = simulate_portfolio(
        {"X": enriched}, {"X": signals}, Costs(0.0, 0.0), PERMISSIVE_RISK_CFG, PLANNER_CFG,
        10_000.0, 365.0, _REPO_ROOT, eligible_mask_by_symbol={"X": np.zeros(n, dtype=bool)},
    )
    assert len(res_blocked.trades) == 0, (
        "a symbol ineligible at the signal bar must never open a new position, even though the "
        "exact same signal/plan would otherwise fill and hit take-profit (see the un-gated "
        "version of this same scenario above)"
    )

    res_allowed = simulate_portfolio(
        {"X": enriched}, {"X": signals}, Costs(0.0, 0.0), PERMISSIVE_RISK_CFG, PLANNER_CFG,
        10_000.0, 365.0, _REPO_ROOT, eligible_mask_by_symbol={"X": np.ones(n, dtype=bool)},
    )
    assert len(res_allowed.trades) == 1, "eligible at every bar must behave exactly like no mask at all"


def test_eligible_mask_by_symbol_does_not_block_monitoring_an_already_open_position():
    """Eligible ONLY on the bar the signal fires and the plan is opened; ineligible on every bar
    after that. The position must still fill and be monitored all the way to its take-profit exit
    -- eligible_mask_by_symbol only gates opening a brand NEW position, never an existing
    OPEN/PENDING one (matches the design doc's "monitor always runs for every held symbol, in or
    out of the universe")."""
    sig0, t0, price0, atr0 = _known_breakout_signal(_breakout_frame([])[0])
    tail = [
        (105.0, 110.0, 104.0, 160.0, 10.0),
        (162.0, 163.0, 161.0, 162.0, 10.0),
    ]
    df, idx = _breakout_frame(tail)
    enriched = add_indicators(df, fast_ema=12, slow_ema=26, rsi_period=14)
    signals = {t0: sig0}
    n = len(idx)
    t0_pos = list(idx).index(t0)

    mask = np.zeros(n, dtype=bool)
    mask[t0_pos] = True  # eligible only on the signal/planning bar itself
    res = simulate_portfolio(
        {"X": enriched}, {"X": signals}, Costs(0.0, 0.0), PERMISSIVE_RISK_CFG, PLANNER_CFG,
        10_000.0, 365.0, _REPO_ROOT, eligible_mask_by_symbol={"X": mask},
    )
    tr = res.trades
    assert len(tr) == 1, f"expected the position to still open, fill, and reach take-profit:\n{tr}"
    assert tr.iloc[0]["reason"] == "take_profit"


def test_eligible_mask_by_symbol_missing_symbol_defaults_to_eligible():
    """A symbol simply absent from eligible_mask_by_symbol (rather than explicitly present with an
    all-True mask) must behave as fully eligible -- lets a caller only supply masks for the
    symbols it actually wants gated."""
    sig0, t0, price0, atr0 = _known_breakout_signal(_breakout_frame([])[0])
    tail = [
        (105.0, 110.0, 104.0, 160.0, 10.0),
        (162.0, 163.0, 161.0, 162.0, 10.0),
    ]
    df, idx = _breakout_frame(tail)
    enriched = add_indicators(df, fast_ema=12, slow_ema=26, rsi_period=14)
    signals = {t0: sig0}

    res = simulate_portfolio(
        {"X": enriched}, {"X": signals}, Costs(0.0, 0.0), PERMISSIVE_RISK_CFG, PLANNER_CFG,
        10_000.0, 365.0, _REPO_ROOT, eligible_mask_by_symbol={"SOME_OTHER_SYMBOL": np.zeros(len(idx), dtype=bool)},
    )
    assert len(res.trades) == 1, "X isn't in the mask dict at all -- must default to eligible, not blocked"


COSTS_BY_SYMBOL_RISK_CFG = dict(
    starting_cash_usd=1_000_000.0,
    max_position_pct=0.1,
    max_total_exposure_pct=0.9,
    max_loss_per_trade_pct=0.5,
    max_daily_loss_pct=0.5,
    max_volatility_pct=1.0,
    min_risk_reward=1.0,
    cooldown_minutes=0,
)


def test_costs_by_symbol_overrides_global_costs_per_symbol():
    """Two symbols with the IDENTICAL signal/price path: AAA has a steep costs_by_symbol override
    (a per-bar array, one Costs per bar on the shared global axis -- see simulate_portfolio's
    docstring on why this is a per-bar array and not a single static Costs), BBB has no entry in
    the dict at all. AAA's booked fees/slippage must reflect the override; BBB must fall back to
    the global (zero-cost) Costs unchanged -- proves the override is applied per-symbol, not
    globally, and that a missing entry doesn't silently inherit the override."""
    sig0, t0, price0, atr0 = _known_breakout_signal(_breakout_frame([])[0])
    tail = [
        (105.0, 110.0, 104.0, 160.0, 10.0),
        (162.0, 163.0, 161.0, 162.0, 10.0),
    ]
    df, idx = _breakout_frame(tail)
    enriched = add_indicators(df, fast_ema=12, slow_ema=26, rsi_period=14)
    signals = {t0: sig0}

    steep = Costs(0.01, 0.01, "steep")
    steep_series = np.full(len(idx), steep, dtype=object)
    res = simulate_portfolio(
        {"AAA": enriched, "BBB": enriched}, {"AAA": signals, "BBB": signals}, Costs(0.0, 0.0),
        COSTS_BY_SYMBOL_RISK_CFG, PLANNER_CFG, 10_000.0, 365.0, _REPO_ROOT,
        costs_by_symbol={"AAA": steep_series},
    )
    tr = res.trades.set_index("symbol")
    assert len(tr) == 2, f"expected one trade per symbol:\n{res.trades}"
    assert tr.loc["AAA", "fees"] > 0.0, "AAA must use the override Costs, not the zero-cost global default"
    assert tr.loc["BBB", "fees"] == 0.0, "BBB has no override entry -- must fall back to the global Costs unchanged"
    assert tr.loc["AAA", "net_pnl"] < tr.loc["BBB", "net_pnl"], "AAA's steeper costs must book a worse net P&L"


def test_costs_by_symbol_is_read_per_bar_not_static_for_the_whole_run():
    """Architect review fix: costs_by_symbol must be read FRESH at each bar's own index, not one
    static Costs applied to every fill/exit in the run. Zero costs at the FILL bar and steep costs
    at the exit (take-profit) bar must book a trade whose entry carries no cost but whose exit
    does -- proving the array is indexed by bar, not collapsed to one value for the whole trade."""
    sig0, t0, price0, atr0 = _known_breakout_signal(_breakout_frame([])[0])
    tail = [
        (105.0, 110.0, 104.0, 160.0, 10.0),
        (162.0, 163.0, 161.0, 162.0, 10.0),
    ]
    df, idx = _breakout_frame(tail)
    enriched = add_indicators(df, fast_ema=12, slow_ema=26, rsi_period=14)
    signals = {t0: sig0}
    n = len(idx)
    t0_pos = list(idx).index(t0)

    zero = Costs(0.0, 0.0, "zero")
    steep = Costs(0.02, 0.02, "steep")
    # Zero cost at and before the fill bar, steep cost from the following bar onward (the
    # take-profit exit lands well after that) -- the entry fee must be exactly 0.0 and the exit fee
    # must be > 0.0, which can only happen if costs_for actually re-reads the array per bar.
    per_bar = np.array([zero] * (t0_pos + 2) + [steep] * (n - t0_pos - 2), dtype=object)
    res = simulate_portfolio(
        {"X": enriched}, {"X": signals}, Costs(0.0, 0.0), PERMISSIVE_RISK_CFG, PLANNER_CFG,
        10_000.0, 365.0, _REPO_ROOT, costs_by_symbol={"X": per_bar},
    )
    tr = res.trades
    assert len(tr) == 1, f"expected exactly one trade:\n{tr}"
    row = tr.iloc[0]
    assert row["reason"] == "take_profit"
    entry_fee = row["fees"] - (row["qty"] * row["exit_px"] * steep.fee)
    assert abs(entry_fee) < 1e-9, (
        f"entry must have been booked at the zero-cost bar's Costs (fee≈0), not the steep exit "
        f"bar's -- got total fees={row['fees']}"
    )


def test_costs_by_symbol_mismatched_length_raises():
    sig0, t0, price0, atr0 = _known_breakout_signal(_breakout_frame([])[0])
    df, idx = _breakout_frame([])
    enriched = add_indicators(df, fast_ema=12, slow_ema=26, rsi_period=14)
    signals = {t0: sig0}
    too_short = np.full(len(idx) - 1, Costs(0.0, 0.0), dtype=object)
    try:
        simulate_portfolio(
            {"X": enriched}, {"X": signals}, Costs(0.0, 0.0), PERMISSIVE_RISK_CFG, PLANNER_CFG,
            10_000.0, 365.0, _REPO_ROOT, costs_by_symbol={"X": too_short},
        )
    except ValueError:
        pass
    else:
        raise AssertionError("a costs_by_symbol array shorter than the global axis must raise, not silently misalign")


def test_eligible_mask_by_symbol_mismatched_length_raises():
    sig0, t0, price0, atr0 = _known_breakout_signal(_breakout_frame([])[0])
    df, idx = _breakout_frame([])
    enriched = add_indicators(df, fast_ema=12, slow_ema=26, rsi_period=14)
    signals = {t0: sig0}
    too_long = np.ones(len(idx) + 3, dtype=bool)
    try:
        simulate_portfolio(
            {"X": enriched}, {"X": signals}, Costs(0.0, 0.0), PERMISSIVE_RISK_CFG, PLANNER_CFG,
            10_000.0, 365.0, _REPO_ROOT, eligible_mask_by_symbol={"X": too_long},
        )
    except ValueError:
        pass
    else:
        raise AssertionError("an eligible_mask_by_symbol array longer than the global axis must raise")


# --------------------------------------------------------------------------- #
# Architect review fix: placebo_run must forward eligible_mask_by_symbol/costs_by_symbol to its
# own internal simulate_portfolio call, UNCHANGED (not time-shifted along with the placebo's own
# signal-timing shift) -- otherwise a P1e gate run comparing a universe-gated, tiered-cost real run
# against un-gated, flat-cost placebos would not be a like-for-like comparison.
# --------------------------------------------------------------------------- #
def test_placebo_run_forwards_eligible_mask_and_costs_by_symbol_as_the_same_object_every_draw():
    """Captures every call placebo_run makes to simulate_portfolio (across n_sims draws, each with
    a DIFFERENT random time-shift) and confirms eligible_mask_by_symbol/costs_by_symbol are passed
    through as the exact same object every single draw -- i.e. never reconstructed, reindexed, or
    shifted along with the placebo's own per-draw signal timing."""
    sig0, t0, price0, atr0 = _known_breakout_signal(_breakout_frame([])[0])
    df, idx = _breakout_frame([])
    enriched = add_indicators(df, fast_ema=12, slow_ema=26, rsi_period=14)
    signals = {t0: sig0}
    n = len(idx)

    mask = {"X": np.zeros(n, dtype=bool)}
    costs_arr = {"X": np.full(n, Costs(0.01, 0.01, "steep"), dtype=object)}

    real_simulate = blb.simulate_portfolio
    captured: list[dict] = []

    def spy(*args, **kwargs):
        captured.append(kwargs)
        return real_simulate(*args, **kwargs)

    with patch.object(blb, "simulate_portfolio", side_effect=spy):
        placebo_run(
            {"X": enriched}, {"X": signals}, Costs(0.0, 0.0), PERMISSIVE_RISK_CFG, PLANNER_CFG,
            10_000.0, 365.0, _REPO_ROOT, n_sims=5, seed=1,
            eligible_mask_by_symbol=mask, costs_by_symbol=costs_arr,
        )

    assert len(captured) == 5, "expected one simulate_portfolio call per placebo draw"
    for kwargs in captured:
        assert kwargs["eligible_mask_by_symbol"] is mask, (
            "placebo_run must forward the SAME mask object unchanged on every draw, not a "
            "time-shifted copy"
        )
        assert kwargs["costs_by_symbol"] is costs_arr, (
            "placebo_run must forward the SAME per-bar costs object unchanged on every draw, not "
            "a time-shifted copy"
        )


def test_placebo_run_default_none_for_new_params_is_unchanged():
    """Every pre-P1d caller (run_live_bot_report.py, run_rank_order_validation.py) never passes
    these two params -- confirms the bugfix (forwarding them) didn't change placebo_run's output
    for callers that don't supply them at all."""
    sig0, t0, price0, atr0 = _known_breakout_signal(_breakout_frame([])[0])
    df, idx = _breakout_frame([])
    enriched = add_indicators(df, fast_ema=12, slow_ema=26, rsi_period=14)
    signals = {t0: sig0}

    out_implicit = placebo_run(
        {"X": enriched}, {"X": signals}, Costs(0.0, 0.0), PERMISSIVE_RISK_CFG, PLANNER_CFG,
        10_000.0, 365.0, _REPO_ROOT, n_sims=5, seed=7,
    )
    out_explicit_none = placebo_run(
        {"X": enriched}, {"X": signals}, Costs(0.0, 0.0), PERMISSIVE_RISK_CFG, PLANNER_CFG,
        10_000.0, 365.0, _REPO_ROOT, n_sims=5, seed=7,
        eligible_mask_by_symbol=None, costs_by_symbol=None,
    )
    pd.testing.assert_frame_equal(out_implicit, out_explicit_none)


def test_placebo_run_eligible_mask_by_symbol_actually_blocks_every_draws_trades():
    """Behavioral proof, not just structural forwarding: an all-False eligibility mask forwarded
    through placebo_run must prevent EVERY one of the n_sims placebo draws from opening a single
    new position, regardless of where that draw's randomly time-shifted signal lands -- confirming
    the forwarded mask is actually applied inside each draw's simulate_portfolio call, not merely
    accepted and ignored."""
    sig0, t0, price0, atr0 = _known_breakout_signal(_breakout_frame([])[0])
    tail = [
        (105.0, 110.0, 104.0, 160.0, 10.0),
        (162.0, 163.0, 161.0, 162.0, 10.0),
    ]
    df, idx = _breakout_frame(tail)
    enriched = add_indicators(df, fast_ema=12, slow_ema=26, rsi_period=14)
    signals = {t0: sig0}
    n = len(idx)

    out = placebo_run(
        {"X": enriched}, {"X": signals}, Costs(0.0, 0.0), PERMISSIVE_RISK_CFG, PLANNER_CFG,
        10_000.0, 365.0, _REPO_ROOT, n_sims=8, seed=3,
        eligible_mask_by_symbol={"X": np.zeros(n, dtype=bool)},
    )
    assert (out["trades"] == 0).all(), f"every placebo draw should be blocked from trading:\n{out}"


# --------------------------------------------------------------------------- #
# Phase 2.0 harness generalization (docs/system-redesign-2026-10.md §5.4): load_csv/load_dir/
# liquidity_window_bars/simulate_portfolio's new `timeframe` parameter. Every test above this
# point never passes `timeframe`, so they also double as the "default unchanged" regression
# guard -- these tests cover the NEW behavior specifically.
# --------------------------------------------------------------------------- #
def _write_csv(path: Path, timeframe: str, n: int, start: str = "2026-01-01 00:00") -> None:
    tf_s = TF_SECONDS[timeframe]
    open_times = pd.date_range(start, periods=n, freq=pd.Timedelta(seconds=tf_s), tz="UTC")
    ts_ms = open_times.values.astype("datetime64[ms]").astype("int64")
    df = pd.DataFrame({
        "timestamp": ts_ms,
        "open": 100.0 + np.arange(n, dtype=float),
        "high": 101.0 + np.arange(n, dtype=float),
        "low": 99.0 + np.arange(n, dtype=float),
        "close": 100.5 + np.arange(n, dtype=float),
        "volume": 1.0,
    })
    df.to_csv(path, index=False)


def test_load_csv_timeframe_default_is_1d_unchanged():
    """No `timeframe` arg -> same close-time shift (TF_SECONDS["1d"]) as before this parameter
    existed."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "X_USD_1d.csv"
        _write_csv(path, "1d", 3)
        df = load_csv(path)  # no timeframe kwarg
        first_open = pd.Timestamp("2026-01-01", tz="UTC")
        assert df.index[0] == first_open + pd.Timedelta(seconds=TF_SECONDS["1d"])


def test_load_csv_timeframe_aware_close_time_shift():
    """An explicit timeframe="1h" shifts the bar-OPEN timestamp by 3600s (not 86400s) to get the
    bar-CLOSE index this harness walks on."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "X_USD_1h.csv"
        _write_csv(path, "1h", 3)
        df = load_csv(path, timeframe="1h")
        first_open = pd.Timestamp("2026-01-01 00:00", tz="UTC")
        assert df.index[0] == first_open + pd.Timedelta(hours=1)
        assert df.index[1] == first_open + pd.Timedelta(hours=2)


def test_load_dir_globs_only_the_requested_timeframe_suffix(tmp_path):
    """A directory holding BOTH 1h and 1d files for the same symbol (exactly resample_ohlcv.py's
    own output layout) must have load_dir(..., timeframe="1h") return ONLY the 1h data, never
    accidentally pick up the 1d file for the same base."""
    _write_csv(tmp_path / "BTC_USD_1h.csv", "1h", 5)
    _write_csv(tmp_path / "BTC_USD_1d.csv", "1d", 2)
    _write_csv(tmp_path / "ETH_USD_1h.csv", "1h", 5)

    hourly = load_dir(tmp_path, timeframe="1h")
    assert set(hourly) == {"BTC", "ETH"}
    assert len(hourly["BTC"]) == 5

    daily = load_dir(tmp_path, timeframe="1d")
    assert set(daily) == {"BTC"}
    assert len(daily["BTC"]) == 2


def test_load_dir_default_timeframe_is_1d_unchanged(tmp_path):
    _write_csv(tmp_path / "BTC_USD_1d.csv", "1d", 4)
    out = load_dir(tmp_path)  # no timeframe kwarg
    assert set(out) == {"BTC"}
    assert len(out["BTC"]) == 4


def test_load_dir_also_discovers_parquet_files(tmp_path):
    """Phase 2.0's actual on-disk format (docs/system-redesign-2026-10.md §5.4: intraday data
    "stored gitignored as parquet, not as CSV") -- load_dir must find `.parquet` files for the
    requested timeframe exactly like it finds `.csv` ones."""
    tf_s = TF_SECONDS["1h"]
    open_times = pd.date_range("2026-01-01 00:00", periods=6, freq=pd.Timedelta(seconds=tf_s), tz="UTC")
    ts_ms = open_times.values.astype("datetime64[ms]").astype("int64")
    df = pd.DataFrame({
        "timestamp": ts_ms, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1.0,
    })
    df.to_parquet(tmp_path / "ETH_USD_1h.parquet", index=False)

    out = load_dir(tmp_path, timeframe="1h")
    assert set(out) == {"ETH"}
    assert len(out["ETH"]) == 6


def test_load_csv_reads_parquet_by_suffix(tmp_path):
    tf_s = TF_SECONDS["1h"]
    open_times = pd.date_range("2026-01-01 00:00", periods=3, freq=pd.Timedelta(seconds=tf_s), tz="UTC")
    ts_ms = open_times.values.astype("datetime64[ms]").astype("int64")
    df = pd.DataFrame({
        "timestamp": ts_ms, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 9.0,
    })
    path = tmp_path / "X_USD_1h.parquet"
    df.to_parquet(path, index=False)

    back = load_csv(path, timeframe="1h")
    assert len(back) == 3
    assert back.index[0] == open_times[0] + pd.Timedelta(hours=1)


def test_liquidity_window_bars_scales_with_timeframe():
    """30 calendar days is 30 bars at 1d (unchanged), but 720 bars at 1h, 180 at 4h, 2880 at
    15m -- a bare bar-count constant would silently mean '30 hours' instead of '30 days' at 1h,
    which is exactly the generalization this function exists to fix."""
    assert liquidity_window_bars("1d") == 30
    assert liquidity_window_bars("1h") == 720
    assert liquidity_window_bars("4h") == 180
    assert liquidity_window_bars("15m") == 2880


def test_simulate_portfolio_timeframe_default_does_not_change_rank_order_result():
    """simulate_portfolio's new `timeframe` kwarg must default to "1d" -- confirms the rank-order
    tie-break test above (which exercises trailing_quote_volume through use_rank_order=True)
    gives an IDENTICAL result whether `timeframe` is omitted or passed explicitly as "1d"."""
    enr_a, idx = _flat_frame(3, volume=1.0)
    enr_z, _ = _flat_frame(3, volume=1_000_000.0)
    enriched = {"AAA": enr_a, "ZZZ": enr_z}
    signals = {"AAA": {idx[0]: _hand_signal(0.8)}, "ZZZ": {idx[0]: _hand_signal(0.8)}}

    res_implicit = simulate_portfolio(
        enriched, signals, Costs(0.0, 0.0), RANK_ORDER_RISK_CFG, PLANNER_CFG,
        10_000.0, 365.0, _REPO_ROOT, use_rank_order=True,
    )
    res_explicit = simulate_portfolio(
        enriched, signals, Costs(0.0, 0.0), RANK_ORDER_RISK_CFG, PLANNER_CFG,
        10_000.0, 365.0, _REPO_ROOT, use_rank_order=True, timeframe="1d",
    )
    pd.testing.assert_frame_equal(res_implicit.trades, res_explicit.trades)


def _breakout_frame_hourly(n_warm: int = 35) -> tuple[pd.DataFrame, int]:
    """Same shape as _breakout_frame (flat warm-up bars then one clean breakout bar), but with
    HOURLY timestamps and enough warm-up bars (>= slow_ema=26) for htf_filter's own EMA to be
    meaningful on the reused-window path -- needed so the "old behavior" sanity check below is a
    real confirmation, not a fails-closed-on-too-little-history no-op. Returns (df, breakout_row_index)."""
    rows = [(100.0, 101.0, 99.0, 100.0, 10.0)] * n_warm
    rows.append((100.0, 116.0, 100.0, 115.0, 50.0))  # the breakout bar itself
    idx = pd.date_range("2024-03-01 00:00", periods=len(rows), freq="1h", tz="UTC")
    o, h, l, c, v = zip(*rows)
    df = pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": v}, index=idx)
    return df, n_warm


def test_compute_symbol_signals_daily_closes_overrides_window_trend():
    """Phase 2.0 fix (docs/system-redesign-2026-10.md §5.4): src/htf_filter.py's LIVE wrapper
    always fetches real "1d" candles regardless of market.timeframe -- it is NOT the same frame
    the scanner runs on. Reusing the scan-timeframe `window` for the htf gate (the old,
    daily-only-harness behavior) was only ever harmless because the daily harness's `window` WAS
    the real daily frame. This test proves that at a sub-daily replay, a real `daily_closes`
    series in a clear DOWNTREND blocks a signal the (upward-shaped) hourly window alone would
    have confirmed -- i.e. the gate reads the real daily series, not the sub-daily window's own
    EMA cross."""
    df, _ = _breakout_frame_hourly()
    enriched = add_indicators(df, fast_ema=12, slow_ema=26, rsi_period=14)

    # OLD behavior (daily_closes=None): reuses `window` itself, which IS a clean uptrend shape
    # (flat then a breakout) -- sanity check this still confirms, so the downtrend test below is
    # a real override, not a vacuous "nothing ever confirms" result.
    signals_old = compute_symbol_signals(
        enriched, SCANNER_CFG, SIGNAL_CFG, htf_enabled=True, fast_ema=12, slow_ema=26,
    )
    assert len(signals_old) >= 1, "sanity check failed: the reuse-window path should confirm here"

    # A real daily series, straight downtrend, with 70 days of history ending well AFTER the
    # hourly frame's own last timestamp -- plenty of point-in-time history available at every
    # hourly bar in `enriched`.
    daily_idx = pd.date_range("2024-01-01", periods=70, freq="1D", tz="UTC")
    daily_closes = pd.Series(np.linspace(200.0, 100.0, len(daily_idx)), index=daily_idx)

    signals_new = compute_symbol_signals(
        enriched, SCANNER_CFG, SIGNAL_CFG, htf_enabled=True, fast_ema=12, slow_ema=26,
        daily_closes=daily_closes,
    )
    assert signals_new == {}, f"a real daily downtrend must block every signal, got:\n{signals_new}"


def _flat_then_one_decisive_bar(flat_periods: int, decisive_date: pd.Timestamp) -> pd.Series:
    """Flat 100.0 history, then exactly ONE decisive 10,000.0 bar dated `decisive_date`. Used
    by both halves of the test below: a single, unmistakable value (not a gradual ramp starting
    back at 100.0) so that leaking even that one extra day, were it placed in the future, could
    never be mistaken for "no visible change" -- the Architect review of this branch found the
    original version used a ramp whose first future value equaled the flat past (both 100.0), so
    a one-day look-ahead leak was silently invisible to the old assertion; it passed a mutation
    that genuinely broke the point-in-time cut. This version is built so that mutation fails."""
    past_idx = pd.date_range("2024-01-01", periods=flat_periods, freq="1D", tz="UTC")
    return pd.concat([
        pd.Series(100.0, index=past_idx),
        pd.Series([10_000.0], index=[decisive_date]),
    ])


def test_compute_symbol_signals_daily_closes_excludes_forming_daily_bar():
    """Point-in-time check (the design doc's §5.4 look-ahead hazard, applied to the existing
    htf_filter gate rather than the not-yet-built MTF confluence): a daily bar whose own close
    time is AFTER the hourly bar being evaluated must never be visible to the gate -- proven here
    by putting the one decisive (unmistakably trend-confirming) daily bar in the FUTURE relative
    to the signal bar, with flat history available up to that point, and confirming the signal is
    blocked. Paired with the positive control below: the identical decisive bar, moved to be
    visible at-or-before the signal bar, must flip the result -- proving this test's mechanism
    actually depends on the cut, rather than always blocking regardless of what's fed in."""
    df, n_warm = _breakout_frame_hourly()
    enriched = add_indicators(df, fast_ema=12, slow_ema=26, rsi_period=14)
    breakout_bar_time = enriched.index[n_warm]

    future_date = breakout_bar_time.normalize() + pd.Timedelta(days=1)
    daily_closes_future = _flat_then_one_decisive_bar(60, future_date)
    assert daily_closes_future.index[-2] <= breakout_bar_time  # sanity: flat history is at/before
    assert future_date > breakout_bar_time  # sanity: the decisive bar really is strictly after it

    signals = compute_symbol_signals(
        enriched, SCANNER_CFG, SIGNAL_CFG, htf_enabled=True, fast_ema=12, slow_ema=26,
        daily_closes=daily_closes_future,
    )
    assert signals == {}, (
        f"the future decisive bar must not be visible at the breakout bar's own close time, got:\n{signals}"
    )


def test_compute_symbol_signals_daily_closes_positive_control_decisive_bar_at_or_before_confirms():
    """Positive control for the look-ahead test above: move the SAME decisive bar from strictly
    AFTER the signal bar to AT it (same date as the breakout bar itself, which `.loc[:as_of]`
    includes), with everything else identical. This must flip blocked -> confirmed, proving the
    gate genuinely reads whatever daily data is visible at the cut, rather than the prior test
    passing merely because nothing in the daily series was ever capable of confirming at all."""
    df, n_warm = _breakout_frame_hourly()
    enriched = add_indicators(df, fast_ema=12, slow_ema=26, rsi_period=14)
    breakout_bar_time = enriched.index[n_warm]

    visible_date = breakout_bar_time.normalize()
    daily_closes_visible = _flat_then_one_decisive_bar(60, visible_date)
    assert visible_date <= breakout_bar_time  # sanity: now at/before, not after

    signals = compute_symbol_signals(
        enriched, SCANNER_CFG, SIGNAL_CFG, htf_enabled=True, fast_ema=12, slow_ema=26,
        daily_closes=daily_closes_visible,
    )
    assert len(signals) >= 1, (
        "the SAME decisive bar, now visible at-or-before the signal bar, must confirm -- "
        "if this still blocks, the gate isn't actually reading the cut daily series"
    )


def test_trailing_quote_volume_window_bars_changes_tie_break_sensitivity():
    """Sanity check that liquidity_window_bars actually flows into trailing_quote_volume as a bar
    count, not a calendar-day count: a block of elevated volume sitting inside a 30-bar
    (1d-equivalent) trailing window -- but entirely outside a short 5-bar window -- must shift
    the wide window's MEDIAN while leaving the narrow window's median untouched (a single
    isolated spike wouldn't move a median at all, which is why this uses a contiguous elevated
    block large enough to be a majority of the wide window instead)."""
    idx = pd.date_range("2026-01-01", periods=40, freq="1h", tz="UTC")
    volume = np.full(40, 1.0)
    volume[10:26] = 100.0  # 16 elevated bars: majority of a trailing 30-bar window at i=39 (10-39)
    close = np.full(40, 100.0)
    enriched = pd.DataFrame({"close": close, "volume": volume}, index=idx)

    wide = trailing_quote_volume(enriched, window=liquidity_window_bars("1d"))  # 30 bars: 10..39
    narrow = trailing_quote_volume(enriched, window=5)  # 35..39, entirely past the elevated block
    assert narrow.iloc[-1] == 100.0
    assert wide.iloc[-1] > narrow.iloc[-1]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        if "tmp_path" in t.__code__.co_varnames[: t.__code__.co_argcount]:
            import tempfile

            with tempfile.TemporaryDirectory() as d:
                t(Path(d))
        else:
            t()
        print("PASS", t.__name__)
    print(f"{len(tests)} tests passed")
