"""Tests for hyperopt_param.py. Skipped entirely if scikit-optimize isn't installed (it's an
optional dependency -- see requirements-backtest.txt -- not part of the main test suite CI runs)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("skopt")

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from engine import COST_PRESETS  # noqa: E402
from hyperopt_param import optimize_live_bot_param  # noqa: E402

from backtest_live_bot import build_enriched_and_signals, load_dir  # noqa: E402
from src.config import load_config  # noqa: E402

PPY_1D = 365.0


@pytest.fixture(scope="module")
def live_signals():
    cfg = load_config()
    raw = load_dir(_THIS_DIR / "data")
    htf_enabled = bool(cfg.htf_filter.get("enabled", True))
    enriched_global, signals_by_symbol, _idx = build_enriched_and_signals(
        raw, cfg.indicators, cfg.scanner, cfg.signal_engine, htf_enabled
    )
    return cfg, enriched_global, signals_by_symbol


def test_optimize_trade_planner_param_returns_value_within_bounds(live_signals):
    cfg, enriched_global, signals_by_symbol = live_signals
    bounds = (1.0, 2.5)
    result = optimize_live_bot_param(
        enriched_global, signals_by_symbol, COST_PRESETS["kraken_T3_taker"],
        cfg.risk, cfg.trade_planner, 10_000.0, PPY_1D,
        cfg_section="trade_planner", param_name="stop_atr_mult",
        bounds=bounds, n_calls=8, random_state=1,
    )
    assert bounds[0] <= result["best_value"] <= bounds[1]
    assert isinstance(result["train_avg_R"], float)
    assert len(result["all_evaluations"]) == 8


def test_optimize_does_not_mutate_caller_config_dicts(live_signals):
    cfg, enriched_global, signals_by_symbol = live_signals
    planner_before = dict(cfg.trade_planner)
    risk_before = dict(cfg.risk)
    optimize_live_bot_param(
        enriched_global, signals_by_symbol, COST_PRESETS["kraken_T3_taker"],
        cfg.risk, cfg.trade_planner, 10_000.0, PPY_1D,
        cfg_section="trade_planner", param_name="stop_atr_mult",
        bounds=(1.0, 2.5), n_calls=5, random_state=2,
    )
    assert cfg.trade_planner == planner_before
    assert cfg.risk == risk_before


def test_optimize_risk_param(live_signals):
    cfg, enriched_global, signals_by_symbol = live_signals
    result = optimize_live_bot_param(
        enriched_global, signals_by_symbol, COST_PRESETS["kraken_T3_taker"],
        cfg.risk, cfg.trade_planner, 10_000.0, PPY_1D,
        cfg_section="risk", param_name="min_risk_reward",
        bounds=(1.0, 2.0), n_calls=5, random_state=3,
    )
    assert 1.0 <= result["best_value"] <= 2.0


def test_unsupported_cfg_section_raises(live_signals):
    cfg, enriched_global, signals_by_symbol = live_signals
    with pytest.raises(ValueError, match="unsupported cfg_section"):
        optimize_live_bot_param(
            enriched_global, signals_by_symbol, COST_PRESETS["kraken_T3_taker"],
            cfg.risk, cfg.trade_planner, 10_000.0, PPY_1D,
            cfg_section="scanner", param_name="breakout_lookback",
            bounds=(10, 30), n_calls=5,
        )
