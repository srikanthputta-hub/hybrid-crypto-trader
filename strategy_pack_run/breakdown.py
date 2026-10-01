"""
breakdown.py - where do the strategy's results come from? By calendar year and by coin.

  python breakdown.py --data-dir data --timeframe 1d --cost kraken_T3_taker
  python breakdown.py --synthetic

Why: the published evidence says trend/momentum works mainly in bull markets. If all the profit comes from one year or
one coin, that is a warning sign, not a strategy.
"""
from __future__ import annotations

import argparse

import pandas as pd

from engine import COST_PRESETS, EngineConfig, run_backtest
from run_report import BPD, PPY, build_frames, load_dir
from strategies import TrendParams


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--timeframe", default="1d", choices=list(PPY))
    ap.add_argument("--sma", type=int, default=200)
    ap.add_argument("--cost", default="kraken_T3_taker", choices=list(COST_PRESETS))
    ap.add_argument("--synthetic", action="store_true")
    a = ap.parse_args()
    if a.synthetic:
        from test_pack import make_market
        market, tf, sma = make_market(n=1500, seed=11, trendy=True), "1d", 100
    else:
        market, tf, sma = load_dir(a.data_dir, a.timeframe), a.timeframe, a.sma
    bpd = BPD[tf]
    cfg = EngineConfig(periods_per_year=PPY[tf], atr_mult=TrendParams(bars_per_day=bpd).stop_mult)
    for strat in ("trend_ensemble", "tsmom"):
        res = run_backtest(build_frames(market, strat, sma, bpd), COST_PRESETS[a.cost], cfg)
        tr = res.trades.copy()
        print(f"\n=== {strat} (costs {a.cost}) ===")
        if tr.empty:
            print("no trades")
            continue
        tr["year"] = tr["exit_time"].dt.year
        for key in ("year", "symbol"):
            g = tr.groupby(key).agg(trades=("R", "size"), win=("net_pnl", lambda x: (x > 0).mean()),
                                    avg_R=("R", "mean"), net_pnl=("net_pnl", "sum"))
            g["share_of_total_pnl"] = g["net_pnl"] / tr["net_pnl"].sum() if tr["net_pnl"].sum() != 0 else float("nan")
            print(f"\nBy {key}:")
            print(g.round(3).to_string())


if __name__ == "__main__":
    main()
