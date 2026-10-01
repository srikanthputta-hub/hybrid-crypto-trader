"""Statistical risk validation on the bot's own realized trade history.

Three independent tools, all computed from logs/trades.csv (via src.pnl's
ClosedTrade loader) -- no synthetic backtest, no historical replay, just
honest statistics on trades this process actually made:

  - Historical VaR / CVaR: how bad is the tail of the trade-return
    distribution, based on what's actually happened so far?
  - Drawdown: peak-to-trough decline of the realized-P/L equity curve.
  - Win-rate significance: is the observed win rate surprising given the
    strategy's own configured risk:reward floor, or is it still within the
    range small samples naturally produce by chance?

This deliberately does NOT replay historical candles through the pipeline --
that would be a real backtest engine, a much bigger undertaking (see the
Vibe-Trading research notes). What this does instead is ask a narrower,
more honest question of the live paper-trading record we already have:
"given only the trades this bot actually took, what can we say with any
confidence?" All three tools degrade to an explicit "not enough data" state
below MIN_TRADES_FOR_STATS rather than reporting a noisy number -- 2-3
trades of history is not a population worth drawing conclusions from.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.pnl import ClosedTrade, load_closed_trades

MIN_TRADES_FOR_STATS = 5
VAR_CONFIDENCE_LEVELS = (0.95, 0.99)
MONTE_CARLO_SIMULATIONS = 10_000
MONTE_CARLO_SEED = 42


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (same convention as numpy's default),
    with no numpy dependency -- this module's inputs are at most a few
    hundred floats, nowhere near where that would matter."""
    if not sorted_values:
        raise ValueError("cannot take a percentile of an empty sequence")
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = pct * (len(sorted_values) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return sorted_values[lo]
    frac = rank - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def historical_var_cvar(
    trades: list[ClosedTrade], confidence_levels: tuple[float, ...] = VAR_CONFIDENCE_LEVELS
) -> dict[str, Any]:
    """Historical (non-parametric) VaR/CVaR on the pnl_pct distribution of
    closed trades. VaR at 95% is the loss threshold such that 95% of trades
    did at least that well (i.e. the 5th percentile of returns); CVaR at 95%
    is the average return of the worst 5% of trades -- how bad the tail
    actually gets, not just where it starts. Both are returns, not dollar
    amounts, so they generalize across position sizes.

    Returns {} (not an error) with fewer than MIN_TRADES_FOR_STATS trades --
    a single bad trade is not a tail, it's the whole sample.
    """
    if len(trades) < MIN_TRADES_FOR_STATS:
        return {}

    returns = sorted(t.pnl_pct for t in trades)
    levels: dict[str, Any] = {}
    for level in confidence_levels:
        tail_pct = 1 - level
        var = _percentile(returns, tail_pct)
        tail_cutoff_rank = max(1, round(tail_pct * len(returns)))
        tail = returns[:tail_cutoff_rank]
        cvar = sum(tail) / len(tail)
        levels[f"{int(level * 100)}"] = {
            "var_pct": round(var, 4),
            "cvar_pct": round(cvar, 4),
        }
    return levels


@dataclass
class DrawdownReport:
    max_drawdown_pct: float | None
    current_drawdown_pct: float | None
    peak_equity: float | None
    trough_equity: float | None


def drawdown_from_trades(trades: list[ClosedTrade], starting_cash: float) -> DrawdownReport:
    """Peak-to-trough decline of the realized-P/L equity curve, built by
    replaying closed trades in sell_time order against starting_cash.

    This is realized P/L only -- it does not include unrealized P/L on any
    currently-open position (that already has its own unrealized_pnl_pct in
    the dashboard's positions table). A position that's deep underwater
    right now won't show up here until it closes; this reports how the
    *settled* equity curve has actually moved, not a live mark-to-market.
    """
    if not trades or not starting_cash:
        return DrawdownReport(None, None, None, None)

    ordered = sorted(trades, key=lambda t: t.sell_time)
    equity = starting_cash
    peak = starting_cash
    max_drawdown = 0.0
    trough_at_max_drawdown = starting_cash
    for t in ordered:
        equity += t.pnl
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak if peak else 0.0
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            trough_at_max_drawdown = equity

    current_drawdown = (peak - equity) / peak if peak else 0.0
    return DrawdownReport(
        max_drawdown_pct=round(max_drawdown, 4),
        current_drawdown_pct=round(current_drawdown, 4),
        peak_equity=round(peak, 2),
        trough_equity=round(trough_at_max_drawdown, 2),
    )


def win_rate_significance(
    trades: list[ClosedTrade],
    expected_win_rate: float,
    n_simulations: int = MONTE_CARLO_SIMULATIONS,
    seed: int = MONTE_CARLO_SEED,
) -> dict[str, Any]:
    """Monte Carlo test: under the null hypothesis that each trade
    independently wins with probability `expected_win_rate`, how often does a
    run of len(trades) random trades produce a win count at or below what was
    actually observed?

    `expected_win_rate` is meant to be the strategy's own configured
    break-even win rate (1 / (1 + min_risk_reward), see
    scripts/export_dashboard_data.py) -- the bar the strategy needs to clear
    to be worth running at all, not an arbitrary external benchmark. A low
    p_value here means "losing streaks this bad are rare even assuming the
    strategy merely breaks even" -- worth investigating. A high p_value means
    "this is a perfectly ordinary losing streak for a small sample," even if
    it doesn't feel that way watching it happen one trade at a time.

    Returns {} with fewer than MIN_TRADES_FOR_STATS trades.
    """
    if len(trades) < MIN_TRADES_FOR_STATS:
        return {}

    n = len(trades)
    observed_wins = sum(1 for t in trades if t.pnl > 0)
    observed_win_rate = observed_wins / n

    rng = random.Random(seed)
    at_or_below = 0
    for _ in range(n_simulations):
        simulated_wins = sum(1 for _ in range(n) if rng.random() < expected_win_rate)
        if simulated_wins <= observed_wins:
            at_or_below += 1
    p_value = at_or_below / n_simulations

    return {
        "sample_size": n,
        "observed_wins": observed_wins,
        "observed_win_rate": round(observed_win_rate, 4),
        "expected_win_rate": round(expected_win_rate, 4),
        "p_value": round(p_value, 4),
        # A plain-language verdict, since a bare p-value means nothing to
        # someone checking a dashboard, not reading a stats textbook.
        "verdict": (
            "insufficient_data"
            if n < MIN_TRADES_FOR_STATS
            else "surprising" if p_value < 0.05 else "plausible" if p_value < 0.20 else "unremarkable"
        ),
    }


def build_risk_report(
    trade_log_path: Path, starting_cash: float, min_risk_reward: float, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    trades = load_closed_trades(trade_log_path)
    breakeven_win_rate = 1 / (1 + min_risk_reward) if min_risk_reward else 0.5
    drawdown = drawdown_from_trades(trades, starting_cash)

    return {
        "sample_size": len(trades),
        "min_trades_for_stats": MIN_TRADES_FOR_STATS,
        "var_cvar": historical_var_cvar(trades),
        "drawdown": {
            "max_drawdown_pct": drawdown.max_drawdown_pct,
            "current_drawdown_pct": drawdown.current_drawdown_pct,
            "peak_equity": drawdown.peak_equity,
            "trough_equity": drawdown.trough_equity,
        },
        "significance": win_rate_significance(trades, breakeven_win_rate),
    }
