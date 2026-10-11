"""Post-hoc diagnostic (NOT pre-registered; docs/min_order_floor_study.md section 3.3): size of the
live-only "fee-slack dust" order the live paper bot could approve on each bar.
On every bar where a harness risk check is rejected for "no room"/"below the minimum" after >=1
approval earlier on the same bar, live (paper fee f, position worth quote*(1-f)) sees
f * (the LAST, room-clipped approval) of extra room: the clip absorbs the slack of the earlier
fills on the bar, so only the clipped fill's fee reappears. Bounded by f * max_position_pct * equity.
usage (from anywhere): python slack_diag.py <data|data_long> <floor> <cost preset>"""
import sys
from pathlib import Path

W = str(Path(__file__).resolve().parents[1])  # strategy_pack_run/
sys.path.insert(0, W)
sys.path.insert(0, str(Path(W).parent))
import os
os.chdir(W)
import copy
import numpy as np
import pandas as pd
import backtest_live_bot as harness
import run_live_bot_report as report
from engine import COST_PRESETS
from src.config import Config, load_config

ds, floor, cost = sys.argv[1], float(sys.argv[2]), sys.argv[3]
raw = copy.deepcopy(load_config().raw)
raw["risk"]["min_order_quote_usd"] = floor
cfg = Config(raw=raw)
enr, sigs, idx = report.build_live_signals(ds, cfg, "1d")
checks = []
orig = harness.RiskEngine


class Rec(orig):
    def check_new_trade(self, symbol, side, portfolio_equity, cash_available, **kw):
        d = super().check_new_trade(symbol, side, portfolio_equity, cash_available, **kw)
        checks.append((kw["now"], d.allowed, d.max_quote_amount, d.reason, portfolio_equity))
        return d


harness.RiskEngine = Rec
res = harness.simulate_portfolio(enr, sigs, COST_PRESETS[cost], cfg.risk, cfg.trade_planner, 10_000.0, 365.0,
                                 Path(W).parent, count_same_bar_approvals=True)
harness.RiskEngine = orig
by_bar = {}
for c in checks:
    by_bar.setdefault(c[0], []).append(c)
slack = []
for t, seq in by_bar.items():
    acc = 0.0
    for _t, ok, q, reason, eq in seq:
        if ok:
            acc = q  # live-only room after a room-clipped fill = fee * that fill (earlier slack is absorbed by the clip)
        elif ("no room" in reason or "below the minimum" in reason) and acc > 0:
            slack.append((t, 0.001 * acc, 0.001 * acc / eq))
            break
s = np.array([x[1] for x in slack])
p = np.array([x[2] for x in slack])
print(ds, floor, cost, "trades", len(res.trades), "bars with same-bar fill then no-room:", len(s))
if len(s):
    print("  potential slack USD (at 0.10%% fee): median %.2f p90 %.2f max %.2f; >5: %d >10: %d >25: %d; max %% of equity %.4f%%"
          % (np.median(s), np.percentile(s, 90), s.max(), (s > 5).sum(), (s > 10).sum(), (s > 25).sum(), 100 * p.max()))
    top = sorted(slack, key=lambda x: -x[1])[:5]
    print("  top:", [(str(pd.Timestamp(a).date()), round(b, 2)) for a, b, _ in top])
