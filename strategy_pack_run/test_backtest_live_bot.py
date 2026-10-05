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

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from engine import Costs  # noqa: E402

from backtest_live_bot import compute_symbol_signals, simulate_portfolio  # noqa: E402
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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("PASS", t.__name__)
    print(f"{len(tests)} tests passed")
