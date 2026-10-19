"""run_phase2_weekly_filter_study.py -- evidence driver for docs/phase2_weekly_filter_study.md.

Runs the pre-registered baseline + W1/W2/W3 (+ the information-only no-seed rows on data/) on the
UNMODIFIED harness (backtest_live_bot.simulate_portfolio / placebo_run, count_same_bar_approvals=True,
kraken_T3_taker, config/config.yaml as committed: min_order_quote_usd 10), datasets data/ and
data_long/, start offsets 0/7/14/21, 200-sim placebo seed 0. Writes results.json, a markdown
summary and the offset-0 trade lists under --out-dir. Nothing in src/ or config/ is touched.

Usage (from strategy_pack_run/):
    python run_phase2_weekly_filter_study.py --out-dir phase2_weekly_filter_study
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import phase2_weekly_filter as p2  # noqa: E402
from backtest_live_bot import build_enriched_and_signals, load_dir, placebo_run, simulate_portfolio  # noqa: E402
from engine import COST_PRESETS, buy_hold, summarize  # noqa: E402
from src.config import load_config  # noqa: E402

COST = "kraken_T3_taker"
EQUITY = 10_000.0
PPY = 365.0
SIMS = 200
OFFSETS = (0, 7, 14, 21)
DATASETS = ("data", "data_long")
SEED_DIR = {"data": "data_long", "data_long": None}  # doc §0.4: gate-input warm-up seed
GATE_NAMES = ("trades>=100", "CI low>0", "later30>0", "placebo>=90", "ret>0&Sharpe>BH")


def _gate_inputs(dataset: str, seeded: bool) -> tuple[dict[str, pd.Series], pd.Series, pd.DatetimeIndex]:
    raw = load_dir(_THIS_DIR / dataset)
    seed_raw = load_dir(_THIS_DIR / SEED_DIR[dataset]) if (seeded and SEED_DIR[dataset]) else {}
    closes = {s: p2.stitch_closes(df["close"], seed_raw[s]["close"] if s in seed_raw else None) for s, df in raw.items()}
    axis = pd.DatetimeIndex(sorted(set().union(*[f.index for f in raw.values()])))
    return closes, closes["BTC"], axis


def build_all_gates(dataset: str, fast: int, slow: int) -> dict[str, dict[str, pd.Series]]:
    out = {}
    closes, btc, axis = _gate_inputs(dataset, seeded=True)
    out.update(p2.build_gates(closes, btc, axis, fast, slow))
    if SEED_DIR[dataset]:
        closes_ns, btc_ns, _ = _gate_inputs(dataset, seeded=False)
        for c, g in p2.build_gates(closes_ns, btc_ns, axis, fast, slow).items():
            out[f"{c}_noseed"] = g
    return out


def _evaluate(eg, sigs, idx, cfg, bh_sharpe: float) -> tuple[dict, pd.DataFrame]:
    costs = COST_PRESETS[COST]
    res = simulate_portfolio(eg, sigs, costs, cfg.risk, cfg.trade_planner, EQUITY, PPY, _REPO_ROOT,
                             count_same_bar_approvals=True)
    m = summarize(res)
    tr = res.trades
    cut, later = p2.later_split(tr, idx)
    pl = placebo_run(eg, sigs, costs, cfg.risk, cfg.trade_planner, EQUITY, PPY, _REPO_ROOT, n_sims=SIMS, seed=0,
                     count_same_bar_approvals=True)
    n = m["trades"]
    pr_ret = float((pl["total_return"] < m["total_return"]).mean())
    pr_R = float((pl["avg_R"].dropna() < m.get("avg_R", -9)).mean()) if n else math.nan
    lo, hi = p2.day_block_ci(tr) if n else (math.nan, math.nan)
    later_R = float(later["R"].mean()) if len(later) else math.nan
    ci_low = m.get("avg_R_ci_low", math.nan)
    gates = [
        n >= 100,
        bool(ci_low > 0) if not math.isnan(ci_low) else False,
        bool(later_R > 0) if not math.isnan(later_R) else False,
        pr_ret >= 0.90 and pr_R >= 0.90,
        bool(m["total_return"] > 0 and m["sharpe"] > bh_sharpe),
    ]
    out = dict(
        signals=int(sum(len(v) for v in sigs.values())), trades=n, win_rate=m.get("win_rate"),
        avg_R=m.get("avg_R"), ci_iid=[m.get("avg_R_ci_low"), m.get("avg_R_ci_high")], ci_day=[lo, hi],
        total_return=m["total_return"], sharpe=m["sharpe"], max_dd=m["max_drawdown"], exposure=m["avg_exposure"],
        later_cut=str(cut.date()), later_n=int(len(later)), later_R=later_R,
        placebo_pct_ret=pr_ret, placebo_pct_R=pr_R, bh_sharpe=bh_sharpe, gates=gates, gates_passed=int(sum(gates)),
        per_year={str(k): v for k, v in p2.per_year(tr).items()},
    )
    return out, tr


def run_job(args) -> dict:
    dataset, k, gates, runs = args
    cfg = load_config()
    raw = {s: df.iloc[k:] for s, df in load_dir(_THIS_DIR / dataset).items()}
    raw = {s: df for s, df in raw.items() if len(df)}
    htf_enabled = bool(cfg.htf_filter.get("enabled", True))
    eg, sigs, idx = build_enriched_and_signals(raw, cfg.indicators, cfg.scanner, cfg.signal_engine, htf_enabled)
    bh = buy_hold(eg, list(eg), EQUITY, PPY)["sharpe"]
    results, trades = {}, {}
    base_sig_n = sum(len(v) for v in sigs.values())
    for name in runs:
        s = sigs if name == "B" else p2.apply_gate(sigs, gates[name])
        r, tr = _evaluate(eg, s, idx, cfg, bh)
        r["veto_rate"] = 1.0 - r["signals"] / base_sig_n if base_sig_n else math.nan
        results[name], trades[name] = r, tr
    comps = {}
    for name in runs:
        if name == "B":
            continue
        b, c = trades["B"], trades[name]
        cut = pd.Timestamp(results["B"]["later_cut"], tz="UTC")
        rb, rc = results["B"], results[name]
        comps[name] = dict(
            delta=rc["avg_R"] - rb["avg_R"],
            paired=p2.paired_delta_ci(b, c),
            later_delta=rc["later_R"] - rb["later_R"],
            loyo=p2.leave_one_year_out(b, c),
            n_ratio=rc["trades"] / rb["trades"] if rb["trades"] else math.nan,
            gate_regressions=[GATE_NAMES[i] for i in range(5) if rb["gates"][i] and not rc["gates"][i]],
            diff=p2.trade_diff(b, c),
        )
        assert results["B"]["later_cut"] == results[name]["later_cut"] and cut is not None
    return dict(dataset=dataset, offset=k, window=[str(idx[0].date()), str(idx[-1].date()), len(idx)],
                results=results, comparisons=comps,
                trades={n: t.assign(entry_time=t["entry_time"].astype(str), exit_time=t["exit_time"].astype(str)).to_dict("list")
                        for n, t in trades.items()} if k == 0 else {})


def gate_coverage(gates: dict[str, dict[str, pd.Series]], dataset: str) -> dict:
    raw = load_dir(_THIS_DIR / dataset)
    out = {}
    for name, by_sym in gates.items():
        on = [float(by_sym[s].reindex(df.index).fillna(False).astype(bool).mean()) for s, df in raw.items()]
        out[name] = float(np.mean(on))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="phase2_weekly_filter_study")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    out_dir = _THIS_DIR / a.out_dir
    out_dir.mkdir(exist_ok=True)
    cfg = load_config()
    fast, slow = cfg.indicators["fast_ema"], cfg.indicators["slow_ema"]
    assert cfg.risk.get("min_order_quote_usd") == 10, "pre-registered at the committed floor 10"

    gates = {d: build_all_gates(d, fast, slow) for d in DATASETS}
    jobs = []
    for d in DATASETS:
        for k in OFFSETS:
            runs = ["B", *p2.CANDIDATES]
            if k == 0 and SEED_DIR[d]:
                runs += [f"{c}_noseed" for c in p2.CANDIDATES]
            jobs.append((d, k, gates[d], runs))
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        out = list(ex.map(run_job, jobs))

    payload = dict(
        params=dict(cost=COST, equity=EQUITY, sims=SIMS, seed=0, offsets=OFFSETS, harness_version="p6-same-bar-exposure-v1",
                    min_order_quote_usd=cfg.risk.get("min_order_quote_usd"), fast_ema=fast, slow_ema=slow,
                    weekly_rule=p2.WEEK_RULE, regime_sma=p2.REGIME_SMA),
        gate_on_share={d: gate_coverage(gates[d], d) for d in DATASETS},
        jobs=[{k: v for k, v in j.items() if k != "trades"} for j in out],
    )
    (out_dir / "results.json").write_text(json.dumps(payload, indent=1, default=float), encoding="utf-8")
    for j in out:
        for name, tr in j["trades"].items():
            pd.DataFrame(tr).to_csv(out_dir / f"trades_{j['dataset']}_{name}.csv", index=False)
    print(json.dumps({f"{j['dataset']} k={j['offset']}": {n: (r["trades"], round(r["avg_R"], 4), r["gates_passed"])
                                                         for n, r in j["results"].items()} for j in out}, indent=1))


if __name__ == "__main__":
    main()
