"""Information only (NOT pre-registered): what a fixed floor does to smaller accounts.
simulate_portfolio only (no placebo), P6 ON, kraken_T3_taker."""
import sys, os, copy
from pathlib import Path
W = str(Path(__file__).resolve().parents[1])  # strategy_pack_run/
sys.path[:0] = [W, str(Path(W).parent)]
os.chdir(W)
import numpy as np
import backtest_live_bot as harness
import run_live_bot_report as report
from engine import COST_PRESETS, summarize
from src.config import Config, load_config
ds = sys.argv[1]
base = load_config()
enr, sigs, idx = report.build_live_signals(ds, base, "1d")
for eq in (1000.0, 2500.0, 10000.0):
    for F in (0.0, 5.0, 10.0, 25.0):
        raw = copy.deepcopy(base.raw); raw["risk"]["min_order_quote_usd"] = F; raw["risk"]["starting_cash_usd"] = eq
        cfg = Config(raw=raw)
        res = harness.simulate_portfolio(enr, sigs, COST_PRESETS["kraken_T3_taker"], cfg.risk, cfg.trade_planner, eq, 365.0, Path(W).parent, count_same_bar_approvals=True)
        m = summarize(res)
        print(f"{ds} equity {eq:>7.0f} floor {F:>4.0f}: trades {m['trades']:>4} avgR {m['avg_R']:.2f} [{m['avg_R_ci_low']:.2f},{m['avg_R_ci_high']:.2f}] ret {m['total_return']*100:6.1f}% sharpe {m['sharpe']:.2f}", flush=True)
