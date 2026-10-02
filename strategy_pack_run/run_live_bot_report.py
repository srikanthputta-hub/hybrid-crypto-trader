"""run_live_bot_report.py - backtests the LIVE bot's OWN pipeline (src/scanner.py ->
src/signal_engine.py -> src/htf_filter.py -> src/trade_planner.py -> src/risk_engine.py ->
src/monitor.py, imported read-only, unmodified) on real daily candles and writes an honest
report in the same format/rigor as strategy_pack_run/report_kraken_720.md /
report_long.md -- same 5-gate verdict table, same fees+slippage/OOS-split/time-shift-placebo/
bootstrap-CI methodology -- except this backtests the bot's actual configured strategy, not a
candidate replacement. See backtest_live_bot.py's module docstring for the full methodology
and its documented simplifications/limitations (read that before trusting these numbers).

Usage (run from strategy_pack_run/, matching run_report.py's own convention):
    python run_live_bot_report.py --data-dir data      --out report_live_strategy_kraken_720.md
    python run_live_bot_report.py --data-dir data_long --out report_live_strategy_long.md
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from engine import COST_PRESETS, buy_hold, summarize  # noqa: E402
from run_report import fmt, split_stats, table  # noqa: E402

from backtest_live_bot import build_enriched_and_signals, load_dir, placebo_run, simulate_portfolio  # noqa: E402
from src.config import load_config  # noqa: E402

PPY_1D = 365.0

LIMITATIONS = """\
## Important limits on what this report can tell you

- **Daily bars, not the live 1h feed.** `config/config.yaml` runs `market.timeframe: "1h"`; only
  daily history this deep was available to backtest against, so this evaluates the live
  pipeline's *decision logic* (same scanner/signal-engine/HTF-filter/trade-planner/risk-engine/
  monitor code, same configured thresholds) at daily resolution, not an exact replay of live 1h
  behavior. Patterns that only resolve within a day, and the bot's actual hour-by-hour monitoring
  cadence, are invisible here.
- **Two risk-engine gates are structurally non-binding at this resolution.** `risk.cooldown_minutes`
  (60) is far shorter than the ~1440 minutes between daily evaluations, so the cooldown gate can
  never fire in this replay (it still can, briefly, live at 1h spacing). `risk.max_daily_loss_pct`'s
  baseline resets every time a new calendar date is seen, which -- at one evaluation per day --
  means it always resets immediately before that day's own check, so it also never fires here.
  Neither is a bug in this harness; both are genuine artifacts of testing an hourly strategy on
  daily bars. See `backtest_live_bot.py`'s module docstring for the full list of simplifications.
- **The risk engine's own R:R gate (`risk.min_risk_reward=1.5`) is never the binding constraint**,
  here or live: `trade_planner.py` solves `take_profit` to hit exactly its OWN configured target
  (`trade_planner.min_risk_reward=2.0`), so every plan's risk/reward comes out at essentially 2.0
  by construction -- comfortably above the risk engine's 1.5 floor.
- **Survivorship warning** (same as the strategy-pack reports): the universe is today's 16 coins
  only. Coins that were delisted or collapsed are missing, which flatters results.
"""


def build_live_signals(data_dir: str, cfg) -> tuple[dict, dict, pd.DatetimeIndex]:
    raw = load_dir(data_dir)
    if "BTC" not in raw:
        raise SystemExit(f"BTC candles are required (expected BTC_USD_1d.csv under {data_dir})")
    htf_enabled = bool(cfg.htf_filter.get("enabled", True))
    return build_enriched_and_signals(raw, cfg.indicators, cfg.scanner, cfg.signal_engine, htf_enabled)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--equity", type=float, default=10_000.0)
    ap.add_argument("--sims", type=int, default=200, help="time-shift placebo simulations")
    ap.add_argument("--cost", default="kraken_T3_taker", choices=list(COST_PRESETS))
    ap.add_argument("--out", default="report_live_strategy.md")
    a = ap.parse_args()

    cfg = load_config()
    enriched_global, signals_by_symbol, idx = build_live_signals(a.data_dir, cfg)
    t0, t1 = idx[0], idx[-1]
    n_signals = sum(len(v) for v in signals_by_symbol.values())

    lines = [
        "# Live-strategy backtest report\n",
        f"Data: {a.data_dir} ({len(enriched_global)} symbols, 1d bars). "
        f"Bot pipeline: src/scanner.py -> src/signal_engine.py -> src/htf_filter.py "
        f"(enabled={bool(cfg.htf_filter.get('enabled', True))}) -> src/trade_planner.py -> "
        f"src/risk_engine.py -> src/monitor.py, imported read-only and unmodified. "
        f"Long-only spot, next-open fills for every trigger (FILL/DISCARD/STOP_LOSS/TAKE_PROFIT).\n",
        f"\nWindow: {t0.date()} to {t1.date()} ({len(idx)} bars). "
        f"Raw bullish+HTF-confirmed signals detected across all symbols before any risk-engine "
        f"gating: {n_signals}.\n",
        LIMITATIONS,
    ]

    rows, results = [], {}
    for name, costs in COST_PRESETS.items():
        res = simulate_portfolio(
            enriched_global, signals_by_symbol, costs, cfg.risk, cfg.trade_planner,
            a.equity, PPY_1D, _REPO_ROOT,
        )
        m = summarize(res)
        results[name] = (res, m)
        ci = f"[{fmt(m.get('avg_R_ci_low'))}, {fmt(m.get('avg_R_ci_high'))}]" if m["trades"] else "n/a"
        rows.append([
            name, m["trades"], fmt(m.get("win_rate"), True), fmt(m.get("avg_R")), ci,
            fmt(m.get("profit_factor")), fmt(m["total_return"], True), fmt(m["max_drawdown"], True),
            fmt(m["sharpe"]), fmt(m["avg_exposure"], True),
        ])
    lines.append("\n## Results, by cost assumption\n")
    lines.append(table(rows, ["costs", "trades", "win%", "avg R", "avg R 95% CI", "PF", "return", "maxDD", "Sharpe", "exposure"]))

    bh_all = buy_hold(enriched_global, list(enriched_global), a.equity, PPY_1D)
    bh_btc = buy_hold(enriched_global, ["BTC"], a.equity, PPY_1D)
    lines.append("\n\nBenchmarks over the same window (no costs):\n")
    lines.append(table(
        [["BTC buy&hold", fmt(bh_btc["total_return"], True), fmt(bh_btc["max_drawdown"], True), fmt(bh_btc["sharpe"])],
         ["equal-weight buy&hold", fmt(bh_all["total_return"], True), fmt(bh_all["max_drawdown"], True), fmt(bh_all["sharpe"])]],
        ["benchmark", "return", "maxDD", "Sharpe"],
    ))

    res, m = results[a.cost]
    cut = t0 + (t1 - t0) * 0.7
    (n_is, w_is, r_is), (n_oos, w_oos, r_oos) = (
        split_stats(res.trades, cut) if len(res.trades) else ((0, "n/a", "n/a"),) * 2
    )
    lines.append(f"\n\nFixed-rule split at {cut.date()} (costs: {a.cost}; nothing was tuned, so out-of-sample = later trades):\n")
    lines.append(table([["first 70%", n_is, w_is, r_is], ["last 30%", n_oos, w_oos, r_oos]], ["period", "trades", "win%", "avg R"]))

    base = placebo_run(
        enriched_global, signals_by_symbol, COST_PRESETS[a.cost], cfg.risk, cfg.trade_planner,
        a.equity, PPY_1D, _REPO_ROOT, n_sims=a.sims,
    )
    pr_ret = float((base["total_return"] < m["total_return"]).mean())
    pr_R = float((base["avg_R"].dropna() < m.get("avg_R", -9)).mean()) if m["trades"] else float("nan")
    lines.append(
        f"\n\nTime-shift placebo ({a.sims} sims -- adapted from engine.placebo_baseline's pattern, see "
        f"backtest_live_bot.placebo_run's docstring for exactly how: same per-symbol signal timing, "
        f"circularly shifted by one random offset shared by all symbols, plans rebuilt against the REAL "
        f"price/ATR at each shifted landing bar, costs {a.cost}): the real strategy's return ranks at the "
        f"{pr_ret*100:.0f}th percentile and avg R at the {pr_R*100:.0f}th percentile of the placebos.\n"
    )

    ci_low = m.get("avg_R_ci_low", float("nan"))
    gate_row = [
        "live_strategy",
        m["trades"] >= 100,
        (ci_low > 0) if not np.isnan(ci_low) else False,
        (r_oos != "n/a" and float(r_oos) > 0),
        (pr_ret >= 0.90 and pr_R >= 0.90),
        (m["total_return"] > 0 and m["sharpe"] > bh_all["sharpe"]),
    ]
    lines.append("\n\n## GATES (costs: %s)\n" % a.cost)
    lines.append(table(
        [[gate_row[0], *["PASS" if x else "FAIL" for x in gate_row[1:]]]],
        ["strategy", ">=100 trades", "avg R CI low > 0", "later 30% avg R > 0", "beats placebo >=90th pct", "net return > 0 and Sharpe > buy&hold"],
    ))
    lines.append(
        "\n\nThis is a diagnostic backtest of the bot's OWN live strategy (not a candidate replacement). "
        "All 5 gates passing is evidence the strategy's logic has held up reasonably well on this data; "
        "any FAIL is a specific, named thing to investigate before trusting the live paper results -- not "
        "an instruction to change scanner/signal-engine/trade-planner/risk-engine code without its own "
        "written reason and out-of-sample test, per this repo's CLAUDE.md.\n"
    )

    text = "\n".join(lines)
    Path(a.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
