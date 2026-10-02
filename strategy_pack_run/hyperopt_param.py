"""hyperopt_param.py - Bayesian search (scikit-optimize) for a single trade_planner.* or
risk.* scalar parameter, as a smarter alternative to hand-picking 2-3 nearby values for the
daily Backtester Routine's parameter-variant slot (see scripts/backtest_summary.json /
CLAUDE.md's "daily automated sweep" and the Routine's own stored prompt).

OPTIONAL DEPENDENCY: requires scikit-optimize, which is NOT in the repo's main requirements.txt
(the live bot never imports this module, and this repo's convention -- see research/v2_signals.py's
docstring -- is to avoid extra dependencies on the live-bot deploy path for something only backtest
research needs). Install with:

    pip install -r strategy_pack_run/requirements-backtest.txt

WHY ONLY trade_planner.*/risk.* PARAMETERS (not scanner.*/signal_engine.*)
---------------------------------------------------------------------------
backtest_live_bot.build_enriched_and_signals() is the expensive step (it runs the real scanner
over a trailing window for every bar, for every symbol). A trade_planner.*/risk.* parameter
never affects what counts as a signal -- only what happens to a signal once found (entry zone,
stop distance, position sizing, risk gates) -- so enriched_global/signals_by_symbol can be
computed ONCE and reused unchanged across every search iteration, making each iteration a cheap
simulate_portfolio() call rather than a full rescan. A scanner.*/signal_engine.* parameter WOULD
change the signal set on every iteration (expensive, and a different search-correctness shape);
this module deliberately doesn't support that yet -- use a manual sweep for those, same as
before this tool existed.

NO LOOK-AHEAD INTO THE GATE CHECK -- READ THIS BEFORE USING THE RESULT
------------------------------------------------------------------------
The whole point of this repo's 5-gate verdict (see run_live_bot_report.py) is that the
later-30%-OOS split and the time-shift placebo are evidence the strategy wasn't just curve-fit
to the sample. If a search picks the parameter value that maximizes a metric computed on the
FULL dataset (train+OOS together), then gates it on that same data, the OOS/placebo checks stop
meaning anything -- the search already saw the answer.

So optimize_live_bot_param()'s objective is computed ONLY on the first `train_frac` (default
70%) of trades by entry_time, exactly matching run_live_bot_report.py's own train/OOS cut. The
value this function returns must still be run through the REAL, full run_live_bot_report.py-style
gate check (all 5 gates, full data, fresh placebo sims) before it's recorded as a candidate --
this function's job is only to pick a smarter value to formally test, not to replace the test.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from engine import Costs  # noqa: E402

from backtest_live_bot import simulate_portfolio  # noqa: E402

MIN_TRAIN_TRADES = 20  # below this, a value's train-avg-R is too noisy to trust as a search signal


def _set_nested(cfg: dict[str, Any], dotted_param: str, value: float) -> dict[str, Any]:
    """Returns a COPY of cfg with one scalar overridden -- e.g. _set_nested(planner_cfg,
    "stop_atr_mult", 1.8). Never mutates the caller's dict (the harness's own simulate_portfolio
    reads these every call, so a shared mutable dict would corrupt other iterations/threads)."""
    out = dict(cfg)
    out[dotted_param] = value
    return out


def train_avg_r_objective(
    enriched_global: dict[str, pd.DataFrame],
    signals_by_symbol: dict[str, dict],
    costs: Costs,
    risk_cfg: dict[str, Any],
    planner_cfg: dict[str, Any],
    start_equity: float,
    periods_per_year: float,
    cut: pd.Timestamp,
    cfg_section: str,
    param_name: str,
) -> Callable[[float], float]:
    """Builds the per-value objective function optimize_live_bot_param() searches over: run
    simulate_portfolio with ONE parameter overridden, return the mean R of trades entered before
    `cut` (the train split) -- or a large negative penalty if there aren't enough train trades to
    trust the number, so the search is steered away from degenerate corners (e.g. a parameter
    value so loose almost nothing ever triggers)."""
    if cfg_section == "trade_planner":
        base_planner, base_risk = planner_cfg, risk_cfg
    elif cfg_section == "risk":
        base_planner, base_risk = planner_cfg, risk_cfg
    else:
        raise ValueError(f"unsupported cfg_section {cfg_section!r} -- only 'trade_planner' or 'risk' (see module docstring)")

    def objective(value: float) -> float:
        this_planner = _set_nested(base_planner, param_name, value) if cfg_section == "trade_planner" else base_planner
        this_risk = _set_nested(base_risk, param_name, value) if cfg_section == "risk" else base_risk
        res = simulate_portfolio(
            enriched_global, signals_by_symbol, costs, this_risk, this_planner,
            start_equity, periods_per_year, _REPO_ROOT,
        )
        train_trades = res.trades[res.trades.entry_time < cut] if len(res.trades) else res.trades
        if len(train_trades) < MIN_TRAIN_TRADES:
            return -1e6 + len(train_trades)  # still ranks "a few more trades" above "almost none"
        return float(train_trades.R.mean())

    return objective


def optimize_live_bot_param(
    enriched_global: dict[str, pd.DataFrame],
    signals_by_symbol: dict[str, dict],
    costs: Costs,
    risk_cfg: dict[str, Any],
    planner_cfg: dict[str, Any],
    start_equity: float,
    periods_per_year: float,
    cfg_section: str,
    param_name: str,
    bounds: tuple[float, float],
    n_calls: int = 15,
    random_state: int = 0,
) -> dict[str, Any]:
    """Bayesian-searches ONE trade_planner.*/risk.* scalar parameter over `bounds` for the value
    that maximizes train-split avg R (see module docstring for why train-only). `enriched_global`/
    `signals_by_symbol` come from backtest_live_bot.build_enriched_and_signals(), computed once by
    the caller and passed in unchanged.

    n_calls is deliberately small (default 15, i.e. 15 simulate_portfolio() calls) -- each call is
    a full portfolio walk, and this is meant to run inside a bounded daily Routine, not a long
    offline job. Returns {"best_value", "train_avg_R", "all_evaluations": [(value, train_avg_R), ...]}.
    The caller MUST still run the real gate check (run_live_bot_report.py-style, full data, fresh
    placebo) on best_value before recording it as a candidate -- this function only picks what to
    formally test next.
    """
    try:
        from skopt import gp_minimize
        from skopt.space import Real
    except ImportError as exc:
        raise ImportError(
            "scikit-optimize is required for optimize_live_bot_param(). "
            "Install with: pip install -r strategy_pack_run/requirements-backtest.txt"
        ) from exc

    idx = next(iter(enriched_global.values())).index
    cut = idx[0] + (idx[-1] - idx[0]) * 0.7

    objective = train_avg_r_objective(
        enriched_global, signals_by_symbol, costs, risk_cfg, planner_cfg,
        start_equity, periods_per_year, cut, cfg_section, param_name,
    )

    evaluations: list[tuple[float, float]] = []

    def neg_objective(x: list[float]) -> float:
        value = float(x[0])
        score = objective(value)
        evaluations.append((value, score))
        return -score  # gp_minimize minimizes; we want to maximize train avg R

    # skopt's gp_minimize requires n_calls >= n_initial_points (default 10) -- scale initial
    # points down for a small n_calls (e.g. a quick test run) rather than hard-requiring 10+.
    n_initial_points = min(10, max(2, n_calls // 2))
    result = gp_minimize(
        neg_objective,
        [Real(bounds[0], bounds[1], name=param_name)],
        n_calls=n_calls,
        n_initial_points=n_initial_points,
        random_state=random_state,
    )

    best_value = float(result.x[0])
    best_score = -float(result.fun)
    return {
        "best_value": best_value,
        "train_avg_R": best_score,
        "all_evaluations": evaluations,
    }
