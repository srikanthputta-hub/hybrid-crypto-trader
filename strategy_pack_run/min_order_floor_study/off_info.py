"""Information only (NOT pre-registered): the ORIGINAL harness (count_same_bar_approvals OFF, what the
Daily Backtester Routine runs) at floor 0 vs 10, kraken_T3_taker, no placebo."""
import sys, os, copy
from pathlib import Path
W = str(Path(__file__).resolve().parents[1])  # strategy_pack_run/
sys.path[:0] = [W, str(Path(W).parent)]; os.chdir(W)
import backtest_live_bot as harness, run_live_bot_report as report
from engine import COST_PRESETS, summarize
from src.config import Config, load_config
ds = sys.argv[1]; base = load_config()
enr, sigs, idx = report.build_live_signals(ds, base, "1d")
for F in (0.0, 10.0):
    raw = copy.deepcopy(base.raw); raw["risk"]["min_order_quote_usd"] = F; cfg = Config(raw=raw)
    res = harness.simulate_portfolio(enr, sigs, COST_PRESETS["kraken_T3_taker"], cfg.risk, cfg.trade_planner, 10000.0, 365.0, Path(W).parent)
    m = summarize(res)
    print(f"OFF {ds} floor {F:.0f}: trades {m['trades']} avgR {m['avg_R']:.2f} [{m['avg_R_ci_low']:.2f},{m['avg_R_ci_high']:.2f}] ret {m['total_return']*100:.1f}% sharpe {m['sharpe']:.2f}", flush=True)
