"""run_min_order_floor_study.py -- evidence driver for docs/min_order_floor_study.md.

Runs the UNMODIFIED run_live_bot_report.main() with one in-memory override,
risk.min_order_quote_usd = --floor (config/config.yaml is never edited), then replays the same
kraken_T3_taker / count_same_bar_approvals=True simulation once more with a RiskEngine subclass
that only RECORDS every check_new_trade call and its decision, to count dust approvals and the
signals a dust plan blocked (pre-registration §0.3). Nothing in src/ or the harness is changed.

Usage (from strategy_pack_run/):
    python run_min_order_floor_study.py --data-dir data --floor 10 \
        --out <report.md> --json <metrics.json>
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import backtest_live_bot as harness  # noqa: E402
import run_live_bot_report as report  # noqa: E402
from engine import COST_PRESETS  # noqa: E402
from src.config import Config, load_config  # noqa: E402
from src.risk_counterfactual import classify_reason  # noqa: E402

THRESHOLDS = (0.01, 5.0, 10.0, 25.0, 50.0)
DUST_FLOORS = (5.0, 10.0, 25.0)


def config_with_floor(floor: float) -> Config:
    raw = copy.deepcopy(load_config().raw)
    raw["risk"]["min_order_quote_usd"] = floor
    return Config(raw=raw)


def instrumented_run(data_dir: str, cfg: Config, equity: float) -> dict:
    enriched, signals, idx = report.build_live_signals(data_dir, cfg, "1d")
    checks: list[dict] = []
    original = harness.RiskEngine

    class RecordingRiskEngine(original):
        def check_new_trade(self, symbol, side, portfolio_equity, cash_available, **kw):
            d = super().check_new_trade(symbol, side, portfolio_equity, cash_available, **kw)
            checks.append(dict(
                t=pd.Timestamp(kw["now"]), symbol=symbol, allowed=bool(d.allowed),
                quote=None if d.max_quote_amount is None else float(d.max_quote_amount),
                category="approved" if d.allowed else classify_reason(d.reason), reason=d.reason,
                equity=float(portfolio_equity),
            ))
            return d

    harness.RiskEngine = RecordingRiskEngine
    try:
        res = harness.simulate_portfolio(
            enriched, signals, COST_PRESETS["kraken_T3_taker"], cfg.risk, cfg.trade_planner,
            equity, report.PPY_1D, _REPO_ROOT, count_same_bar_approvals=True,
        )
    finally:
        harness.RiskEngine = original

    pos = {ts: i for i, ts in enumerate(idx)}
    trades = res.trades
    approvals = [c for c in checks if c["allowed"]]
    quotes = np.array([c["quote"] for c in approvals]) if approvals else np.array([])
    out: dict = {
        "checks": len(checks),
        "approvals": len(approvals),
        "trades": int(len(trades)),
        "median_spend": float(np.median(quotes)) if len(quotes) else None,
        "p10_spend": float(np.percentile(quotes, 10)) if len(quotes) else None,
        "min_spend": float(quotes.min()) if len(quotes) else None,
        "approvals_below": {f"{x:g}": int((quotes < x).sum()) for x in THRESHOLDS},
        "rejections_by_category": pd.Series([c["category"] for c in checks if not c["allowed"]]).value_counts().to_dict(),
        "peak_equity": float(res.equity.max()),
        "final_equity": float(res.equity.iloc[-1]),
    }

    # Plans blocked by dust (pre-registration §0.3), per candidate floor F, counted on THIS run.
    sig_bars = {s: sorted(pos[ts] for ts in m if ts in pos) for s, m in signals.items()}
    entry_lookup = {(r.symbol, pd.Timestamp(r.entry_time)): pd.Timestamp(r.exit_time) for r in trades.itertuples()}
    checks_by_sym: dict[str, list[int]] = {}
    for c in checks:
        checks_by_sym.setdefault(c["symbol"], []).append(pos[c["t"]])
    blocked: dict[str, dict] = {}
    for F in DUST_FLOORS:
        slot_blocked, rate_blocked, dust_trades_R = 0, 0, []
        for c in approvals:
            if c["quote"] >= F:
                continue
            t0 = pos[c["t"]]
            exit_ts = entry_lookup.get((c["symbol"], idx[t0 + 1])) if t0 + 1 < len(idx) else None
            if exit_ts is not None:
                t_end = pos[exit_ts]
            else:  # never filled: plan held until the symbol's next risk check (approximation)
                later = [t for t in checks_by_sym[c["symbol"]] if t > t0]
                t_end = later[0] if later else len(idx) - 1
            slot_blocked += sum(1 for t in sig_bars.get(c["symbol"], []) if t0 < t < t_end)
        bar_seq: dict[pd.Timestamp, list[dict]] = {}
        for c in checks:
            bar_seq.setdefault(c["t"], []).append(c)
        for seq in bar_seq.values():
            dust_seen = False
            for c in seq:
                if c["allowed"] and c["quote"] < F:
                    dust_seen = True
                elif dust_seen and c["category"] == "new_entry_rate_cap":
                    rate_blocked += 1
        sub = trades[[(r.symbol, pd.Timestamp(r.entry_time)) in {
            (c["symbol"], idx[pos[c["t"]] + 1]) for c in approvals if c["quote"] < F and pos[c["t"]] + 1 < len(idx)
        } for r in trades.itertuples()]] if len(trades) else trades
        blocked[f"{F:g}"] = {
            "sub_floor_approvals": int(sum(1 for c in approvals if c["quote"] < F)),
            "sub_floor_trades": int(len(sub)),
            "sub_floor_trades_avg_R": float(sub["R"].mean()) if len(sub) else None,
            "sub_floor_trades_net_pnl": float(sub["net_pnl"].sum()) if len(sub) else 0.0,
            "signals_blocked_by_dust_plan_slot": int(slot_blocked),
            "rate_cap_rejections_after_same_bar_dust": int(rate_blocked),
        }
    out["dust_blocking"] = blocked
    out["trade_keys"] = sorted(f"{r.symbol}|{pd.Timestamp(r.entry_time).date()}" for r in trades.itertuples())
    out["trade_R"] = {f"{r.symbol}|{pd.Timestamp(r.entry_time).date()}": float(r.R) for r in trades.itertuples()}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--floor", type=float, required=True)
    ap.add_argument("--sims", type=int, default=200)
    ap.add_argument("--equity", type=float, default=10_000.0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--json", required=True)
    a = ap.parse_args()

    cfg = config_with_floor(a.floor)
    assert cfg.risk["min_order_quote_usd"] == a.floor
    report.load_config = lambda: config_with_floor(a.floor)  # the only override
    sys.argv = [
        "run_live_bot_report.py", "--data-dir", a.data_dir, "--count-same-bar-approvals",
        "--cost", "kraken_T3_taker", "--sims", str(a.sims), "--equity", str(a.equity), "--out", a.out,
    ]
    report.main()
    metrics = instrumented_run(a.data_dir, cfg, a.equity)
    metrics.update(floor=a.floor, data_dir=a.data_dir)
    Path(a.json).write_text(json.dumps(metrics, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
