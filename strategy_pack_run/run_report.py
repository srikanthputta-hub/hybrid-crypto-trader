"""
run_report.py - backtest both strategies on real (or synthetic) candles and print an honest report.

Real data:   python fetch_kraken.py --timeframe 1d --quote USD --out data
             python run_report.py --data-dir data --timeframe 1d
Smoke test:  python run_report.py --synthetic

Results are written to report.md as well. Read the GATES table at the bottom first.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from engine import COST_PRESETS, EngineConfig, buy_hold, placebo_baseline, run_backtest, summarize
from dataclasses import replace

from strategies import MomParams, TrendParams, daily_regime, load_csv, trend_ensemble, tsmom

PPY = {"1d": 365.0, "4h": 365.0 * 6, "1h": 365.0 * 24}
BPD = {"1d": 1, "4h": 6, "1h": 24}


def load_dir(data_dir: str, timeframe: str) -> dict[str, pd.DataFrame]:
    out = {}
    for p in sorted(Path(data_dir).glob(f"*_{timeframe}.csv")):
        out[p.stem.split("_")[0].upper()] = load_csv(p, timeframe)
    if "BTC" not in out:
        raise SystemExit("BTC candles are required for the regime filter (expected BTC_<QUOTE>_%s.csv)" % timeframe)
    return out


def build_frames(market, strat, sma_days, bpd=1, tparams=None, mparams=None):
    reg = daily_regime(market["BTC"], sma_days)
    tparams = replace(tparams or TrendParams(), bars_per_day=bpd)
    mparams = replace(mparams or MomParams(), bars_per_day=bpd)
    fn, p = (trend_ensemble, tparams) if strat == "trend_ensemble" else (tsmom, mparams)
    frames = {s: fn(df, reg, p) for s, df in market.items()}
    firsts = [f.index[f["warm"].to_numpy()][0] for f in frames.values() if f["warm"].any()]
    start = min(firsts)  # late-listed coins join when they have data; engine skips symbols without bars
    return {s: f.loc[start:] for s, f in frames.items() if f["warm"].any()}


def fmt(x, pct=False, nd=2):
    if x is None or (isinstance(x, float) and (np.isnan(x))):
        return "n/a"
    if isinstance(x, float) and np.isinf(x):
        return "inf"
    return f"{x*100:.1f}%" if pct else f"{x:.{nd}f}"


def table(rows, header):
    w = [max(len(str(h)), *(len(str(r[i])) for r in rows)) for i, h in enumerate(header)]
    line = lambda r: "| " + " | ".join(str(c).ljust(w[i]) for i, c in enumerate(r)) + " |"
    return "\n".join([line(header), "|" + "|".join("-" * (x + 2) for x in w) + "|", *map(line, rows)])


def split_stats(tr: pd.DataFrame, cut: pd.Timestamp):
    def st(d):
        return (len(d), fmt(float((d.net_pnl > 0).mean()), True) if len(d) else "n/a", fmt(float(d.R.mean())) if len(d) else "n/a")
    return st(tr[tr.entry_time < cut]), st(tr[tr.entry_time >= cut])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--timeframe", default="1d", choices=list(PPY))
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--trendy", action="store_true", help="synthetic only: use a trending market (default is a random walk)")
    ap.add_argument("--sma", type=int, default=200, help="BTC daily SMA length for the regime filter (days)")
    ap.add_argument("--equity", type=float, default=10_000.0)
    ap.add_argument("--sims", type=int, default=100, help="random-entry simulations")
    ap.add_argument("--cost", default="kraken_T3_taker", choices=list(COST_PRESETS))
    ap.add_argument("--out", default="report.md")
    a = ap.parse_args()

    if a.synthetic:
        from test_pack import make_market
        market = make_market(n=1100, seed=7, trendy=a.trendy)
        tf, sma = "1d", min(a.sma, 100)
        src = f"SYNTHETIC ({'trending' if a.trendy else 'random-walk'}) data - mechanics smoke test only"
    else:
        market, tf, sma = load_dir(a.data_dir, a.timeframe), a.timeframe, a.sma
        src = f"{a.data_dir} ({len(market)} symbols, {tf} bars)"

    bpd = BPD[tf]
    cfg = EngineConfig(start_equity=a.equity, periods_per_year=PPY[tf], atr_mult=TrendParams(bars_per_day=bpd).stop_mult)
    lines = [f"# Backtest report\n\nData: {src}. Regime: BTC daily SMA({sma}). Long-only spot, next-open fills.\n\nSurvivorship warning: the universe is today's coins only. Coins that were delisted or collapsed are missing, which flatters results. Late-listed coins join the test when their data starts.\n"]
    gates = []

    for strat in ("trend_ensemble", "tsmom"):
        frames = build_frames(market, strat, sma, bpd)
        T = len(pd.DatetimeIndex(sorted(set().union(*[f.index for f in frames.values()]))))
        t0, t1 = min(f.index[0] for f in frames.values()), max(f.index[-1] for f in frames.values())
        lines.append(f"\n## {strat}\n\nWindow after warm-up: {t0.date()} to {t1.date()} ({T} bars, {len(frames)} symbols).\n")
        rows, results = [], {}
        for name, costs in COST_PRESETS.items():
            res = run_backtest(frames, costs, cfg)
            m = summarize(res)
            results[name] = (res, m)
            ci = f"[{fmt(m.get('avg_R_ci_low'))}, {fmt(m.get('avg_R_ci_high'))}]" if m["trades"] else "n/a"
            rows.append([name, m["trades"], fmt(m.get("win_rate"), True), fmt(m.get("avg_R")), ci, fmt(m.get("profit_factor")),
                         fmt(m["total_return"], True), fmt(m["max_drawdown"], True), fmt(m["sharpe"]), fmt(m["avg_exposure"], True)])
        lines.append(table(rows, ["costs", "trades", "win%", "avg R", "avg R 95% CI", "PF", "return", "maxDD", "Sharpe", "exposure"]))

        bh_all = buy_hold(frames, list(frames), a.equity, PPY[tf])
        bh_btc = buy_hold(frames, ["BTC"], a.equity, PPY[tf])
        lines.append("\n\nBenchmarks over the same window (no costs):\n")
        lines.append(table([["BTC buy&hold", fmt(bh_btc["total_return"], True), fmt(bh_btc["max_drawdown"], True), fmt(bh_btc["sharpe"])],
                            ["equal-weight buy&hold", fmt(bh_all["total_return"], True), fmt(bh_all["max_drawdown"], True), fmt(bh_all["sharpe"])]],
                           ["benchmark", "return", "maxDD", "Sharpe"]))

        res, m = results[a.cost]
        cut = t0 + (t1 - t0) * 0.7
        (n_is, w_is, r_is), (n_oos, w_oos, r_oos) = split_stats(res.trades, cut) if len(res.trades) else ((0, "n/a", "n/a"),) * 2
        lines.append(f"\n\nFixed-rule split at {cut.date()} (costs: {a.cost}; nothing was tuned, so out-of-sample = later trades):\n")
        lines.append(table([["first 70%", n_is, w_is, r_is], ["last 30%", n_oos, w_oos, r_oos]], ["period", "trades", "win%", "avg R"]))

        base = placebo_baseline(frames, COST_PRESETS[a.cost], cfg, n_sims=a.sims)
        pr_ret = float((base["total_return"] < m["total_return"]).mean())
        pr_R = float((base["avg_R"].dropna() < m.get("avg_R", -9)).mean()) if m["trades"] else float("nan")
        lines.append(f"\n\nTime-shift placebo ({a.sims} sims: same signals shifted to random dates, same prices and costs {a.cost}): "
                     f"the real signals' return ranks at the {pr_ret*100:.0f}th percentile and avg R at the {pr_R*100:.0f}th percentile of the placebos.\n")

        if strat == "trend_ensemble":
            grid = []
            for am in (2.0, 3.0, 4.0):
                for mv in (1, 2, 3):
                    tp = TrendParams(atr_mult=am, min_votes=mv, bars_per_day=bpd)
                    fr = build_frames(market, strat, sma, bpd, tparams=tp)
                    r2 = run_backtest(fr, COST_PRESETS[a.cost], EngineConfig(start_equity=a.equity, periods_per_year=PPY[tf], atr_mult=tp.stop_mult))
                    m2 = summarize(r2)
                    grid.append([f"{am:.0f}", mv, m2["trades"], fmt(m2.get("avg_R")), fmt(m2["total_return"], True), fmt(m2["max_drawdown"], True)])
            lines.append("\nParameter robustness (atr_mult x min_votes). Look for a plateau, not one lucky cell:\n")
            lines.append(table(grid, ["atr_mult", "min_votes", "trades", "avg R", "return", "maxDD"]))

        ci_low = m.get("avg_R_ci_low", float("nan"))
        gates.append([strat, m["trades"] >= 100, ci_low > 0 if not np.isnan(ci_low) else False,
                      (r_oos != "n/a" and float(r_oos) > 0), pr_ret >= 0.90 and pr_R >= 0.90,
                      m["total_return"] > 0 and m["sharpe"] > bh_all["sharpe"]])

    lines.append("\n\n## GATES (costs: %s)\n" % a.cost)
    lines.append(table([[g[0], *["PASS" if x else "FAIL" for x in g[1:]]] for g in gates],
                       ["strategy", ">=100 trades", "avg R CI low > 0", "later 30% avg R > 0", "beats placebo >=90th pct", "net return > 0 and Sharpe > buy&hold"]))
    lines.append("\n\nA strategy is only a candidate for paper trading if every gate passes. With fewer than 100 trades, treat any result as a smoke test, not evidence.\n")
    text = "\n".join(lines)
    Path(a.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
