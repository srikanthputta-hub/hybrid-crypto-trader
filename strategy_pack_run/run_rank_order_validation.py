"""run_rank_order_validation.py - P1c harness validation (one-off investigation script, not part
of the Daily Backtester Routine's regular tooling): does processing each bar's signals in
src.signal_engine.rank_signals order (instead of backtest_live_bot.simulate_portfolio's existing
sorted(symbols) order) change anything on the EXISTING data/data_long datasets, separate from
any universe expansion (docs/system-redesign-2026-10.md SS4.1/SS4.4 rule 2, SS8 P1c)?

Reuses engine.py's summarize()/bootstrap_ci()/buy_hold()/COST_PRESETS and run_report.py's
fmt()/split_stats()/table() UNCHANGED (import only) -- same 5-gate methodology as
run_live_bot_report.py, which this script also imports (build_live_signals, LIMITATIONS) rather
than reimplementing. Produces, for BOTH kraken_720 (data/) and long (data_long/):
  1. A baseline sanity check: the sorted(symbols) leg's gate table must reproduce the numbers
     already on record in report_live_strategy_kraken_720.md / report_live_strategy_long.md.
  2. The same table for the rank_signals leg (use_rank_order=True), same data/config/costs.
  3. Whether risk_engine.max_new_entries_per_window ever actually bound (instrumented via a
     lightweight, read-only patch of RiskEngine.check_new_trade -- logs every call's (now,
     symbol, allowed, reason); does not alter src/risk_engine.py or its behavior at all), and if
     so, concrete bars where the two orderings picked different symbols for the scarce slot(s).

Run from strategy_pack_run/:  PYTHONPATH=..:. python3 run_rank_order_validation.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from engine import COST_PRESETS, buy_hold, summarize  # noqa: E402
from run_report import fmt, split_stats, table  # noqa: E402

from backtest_live_bot import placebo_run, simulate_portfolio  # noqa: E402
from run_live_bot_report import build_live_signals  # noqa: E402
from src.config import load_config  # noqa: E402
from src.risk_engine import RiskEngine  # noqa: E402

PPY_1D = 365.0
COST_NAME = "kraken_T3_taker"
N_PLACEBO_SIMS = 200


def _instrumented_run(enriched_global, signals_by_symbol, cfg, use_rank_order: bool, log: list):
    """Runs simulate_portfolio with RiskEngine.check_new_trade wrapped to append
    (now, symbol, allowed, reason) to `log` for every call -- read-only instrumentation, the
    real method still runs unchanged and its return value is passed through untouched."""
    original = RiskEngine.check_new_trade

    def wrapped(self, symbol, side, *args, **kwargs):
        decision = original(self, symbol, side, *args, **kwargs)
        if side == "buy":
            log.append((kwargs.get("now"), symbol, decision.allowed, decision.reason))
        return decision

    with patch.object(RiskEngine, "check_new_trade", wrapped):
        return simulate_portfolio(
            enriched_global, signals_by_symbol, COST_PRESETS[COST_NAME], cfg.risk, cfg.trade_planner,
            10_000.0, PPY_1D, _REPO_ROOT, use_rank_order=use_rank_order,
        )


def gate_row_for(res, m, bh_all, idx) -> list:
    t0, t1 = idx[0], idx[-1]
    cut = t0 + (t1 - t0) * 0.7
    (_n_is, _w_is, _r_is), (_n_oos, _w_oos, r_oos) = (
        split_stats(res.trades, cut) if len(res.trades) else ((0, "n/a", "n/a"),) * 2
    )
    ci_low = m.get("avg_R_ci_low", float("nan"))
    return [
        m["trades"] >= 100,
        (ci_low > 0) if not np.isnan(ci_low) else False,
        (r_oos != "n/a" and float(r_oos) > 0),
        None,  # placebo gate filled in by caller (needs its own placebo_run)
        (m["total_return"] > 0 and m["sharpe"] > bh_all["sharpe"]),
    ]


def run_one(data_dir: str, label: str) -> None:
    cfg = load_config()
    enriched_global, signals_by_symbol, idx = build_live_signals(data_dir, cfg)
    t0, t1 = idx[0], idx[-1]
    bh_all = buy_hold(enriched_global, list(enriched_global), 10_000.0, PPY_1D)

    print(f"\n{'=' * 90}\n{label} ({data_dir}): {len(enriched_global)} symbols, {len(idx)} bars, "
          f"{t0.date()} to {t1.date()}\n{'=' * 90}")

    rows = []
    gate_rows = []
    logs: dict[bool, list] = {}
    results: dict[bool, tuple] = {}
    for use_rank in (False, True):
        log: list = []
        res = _instrumented_run(enriched_global, signals_by_symbol, cfg, use_rank, log)
        m = summarize(res)
        gate = gate_row_for(res, m, bh_all, idx)

        placebo = placebo_run(
            enriched_global, signals_by_symbol, COST_PRESETS[COST_NAME], cfg.risk, cfg.trade_planner,
            10_000.0, PPY_1D, _REPO_ROOT, n_sims=N_PLACEBO_SIMS, use_rank_order=use_rank,
        )
        pr_ret = float((placebo["total_return"] < m["total_return"]).mean())
        pr_R = float((placebo["avg_R"].dropna() < m.get("avg_R", -9)).mean()) if m["trades"] else float("nan")
        gate[3] = (pr_ret >= 0.90 and pr_R >= 0.90)

        order_name = "rank_signals" if use_rank else "sorted(symbols)"
        ci = f"[{fmt(m.get('avg_R_ci_low'))}, {fmt(m.get('avg_R_ci_high'))}]" if m["trades"] else "n/a"
        rows.append([
            order_name, m["trades"], fmt(m.get("win_rate"), True), fmt(m.get("avg_R")), ci,
            fmt(m.get("profit_factor")), fmt(m["total_return"], True), fmt(m["max_drawdown"], True),
            fmt(m["sharpe"]), fmt(m["avg_exposure"], True),
        ])
        gate_rows.append([
            order_name, *["PASS" if g else "FAIL" for g in gate],
            f"{pr_ret*100:.0f}th / {pr_R*100:.0f}th",
        ])

        logs[use_rank] = log
        results[use_rank] = (res, m)

    print("\n" + table(
        rows,
        ["order", "trades", "win%", "avg R", "avg R 95% CI", "PF", "return", "maxDD", "Sharpe", "exposure"],
    ))
    print("\n" + table(
        gate_rows,
        ["order", ">=100 trades", "avg R CI low > 0", "later 30% avg R > 0", "beats placebo >=90th pct",
         "net return > 0 and Sharpe > bh", "placebo pctile (return/avgR)"],
    ))

    # --- Does max_new_entries_per_window ever actually bind? ---
    rate_cap_rejections = {
        use_rank: [e for e in logs[use_rank] if not e[2] and "new-entry rate cap" in (e[3] or "")]
        for use_rank in (False, True)
    }
    print(f"\nmax_new_entries_per_window rejections -- sorted(symbols): {len(rate_cap_rejections[False])}, "
          f"rank_signals: {len(rate_cap_rejections[True])}")

    # --- Does max_total_exposure_pct ever actually bind (any "no room left" rejection where
    #     the exposure term specifically was the limiting one)? Reported as a cross-check that
    #     the ordering can/can't distinguish anything via this cap either. ---
    exposure_rejections = {
        use_rank: [e for e in logs[use_rank] if not e[2] and "no room left" in (e[3] or "")]
        for use_rank in (False, True)
    }
    print(f"'no room left to size this trade' rejections -- sorted(symbols): {len(exposure_rejections[False])}, "
          f"rank_signals: {len(exposure_rejections[True])}")

    # --- Concrete bar-level winner comparison on bars with >=2 competing candidates ---
    by_bar: dict[bool, dict] = {False: {}, True: {}}
    for use_rank in (False, True):
        for now, symbol, allowed, reason in logs[use_rank]:
            by_bar[use_rank].setdefault(now, []).append((symbol, allowed, reason))

    contested_bars = sorted(
        now for now, entries in by_bar[False].items()
        if len(entries) >= 2 and any(not allowed and "rate cap" in (reason or "") for _, allowed, reason in entries)
    )
    print(f"\nBars where >=2 symbols competed AND the rate cap rejected at least one "
          f"(sorted(symbols) run): {len(contested_bars)}")
    shown = 0
    for now in contested_bars:
        a = by_bar[False].get(now, [])
        b = by_bar[True].get(now, [])
        won_a = sorted(s for s, allowed, _ in a if allowed)
        won_b = sorted(s for s, allowed, _ in b if allowed)
        if won_a != won_b:
            cand_a = [s for s, _, _ in a]
            cand_b = [s for s, _, _ in b]
            print(f"  {now.date() if hasattr(now, 'date') else now}: "
                  f"sorted order candidates={cand_a} -> won={won_a} | "
                  f"rank order candidates={cand_b} -> won={won_b}")
            shown += 1
            if shown >= 15:
                print("  ... (truncated)")
                break
    if shown == 0 and contested_bars:
        print("  (rate cap bound on these bars, but the WINNING symbol set was identical under "
              "both orderings on every one of them)")

    # --- Trade-level diff: does the final trades_df actually differ at all? ---
    tr_false, tr_true = results[False][0].trades, results[True][0].trades
    same_shape = tr_false.shape == tr_true.shape
    same_trades = same_shape and tr_false[["symbol", "entry_time", "exit_time", "reason"]].equals(
        tr_true[["symbol", "entry_time", "exit_time", "reason"]]
    )
    print(f"\nTrades identical between orderings (symbol/entry_time/exit_time/reason, "
          f"{len(tr_false)} vs {len(tr_true)} trades): {same_trades}")
    if not same_trades:
        key_cols = ["symbol", "entry_time", "exit_time", "reason"]
        set_false = set(tuple(r) for r in tr_false[key_cols].itertuples(index=False, name=None))
        set_true = set(tuple(r) for r in tr_true[key_cols].itertuples(index=False, name=None))
        only_false = sorted(set_false - set_true)
        only_true = sorted(set_true - set_false)
        print(f"  trades only in sorted(symbols) run ({len(only_false)}): {only_false[:10]}")
        print(f"  trades only in rank_signals run ({len(only_true)}): {only_true[:10]}")


if __name__ == "__main__":
    run_one("data", "kraken_720")
    run_one("data_long", "long")
