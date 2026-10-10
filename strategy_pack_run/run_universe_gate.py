"""run_universe_gate.py - P1e gate run: `universe.mode: discover` at the $1M and $100k floors, on
kraken_720_universe and long_universe, against a same-harness static-16 baseline.

Implements the rules pre-registered in docs/p1e_universe_gate_run.md §0 (read that first). One-off
evidence script, same family as run_rank_order_validation.py; not part of the Daily Routine.
Imports, never modifies: backtest_live_bot.simulate_portfolio / placebo_run, engine.summarize /
bootstrap_ci / buy_hold / COST_PRESETS, run_report.split_stats, universe_gate_inputs (P1e builder).

Run from strategy_pack_run/ (raw datasets must be present and pass SHA256SUMS.txt):
    PYTHONPATH=..:. python3 run_universe_gate.py --cache-dir <scratch dir> --workers 4

Writes report_p1e_universe_gate.md and p1e_universe_gate_results.json next to this file.

--count-same-bar-approvals (P6, docs/p6_harness_exposure_fix.md; default OFF = the P1e run exactly):
forwards `count_same_bar_approvals=True` to EVERY simulate_portfolio/placebo_run call this runner
makes (canonical, placebo, order seeds, superset check, legacy rows). Results are harness_version
`p1e-universe-v1 + p6-same-bar-exposure-v1`. Pass --out-md/--out-json so the committed P1e outputs
are not overwritten.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import pickle
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from backtest_live_bot import load_dir, placebo_run, simulate_portfolio  # noqa: E402
from engine import COST_PRESETS, bootstrap_ci, buy_hold, summarize  # noqa: E402
from run_live_bot_report import build_live_signals  # noqa: E402
from src import signal_engine  # noqa: E402
from src.config import load_config  # noqa: E402
from src.risk_engine import RiskEngine  # noqa: E402
from universe_gate_inputs import (  # noqa: E402
    ALL_EXCLUDE_BASES,
    build_costs,
    build_eligibility,
    enrich_native,
    load_universe_raw,
    original_bases,
    shared_axis,
    signals_for,
)

PPY = 365.0
EQUITY = 10_000.0
COST_NAME = "kraken_T3_taker"
N_PLACEBO = 200
PLACEBO_SEED = 0
ORDER_SEEDS = list(range(1, 11))
FLOORS = {"1m": 1_000_000.0, "100k": 100_000.0}
DATASETS = {
    "kraken_720_universe": ("data_universe/raw_kraken", "floor"),
    "long_universe": ("data_universe/raw_long", "rank_topn"),
}
LEGACY = {"kraken_720": "data", "long": "data_long"}
UNIVERSE_DIR = _THIS_DIR / "data_universe"

# Populated in the parent before the worker pool forks (fork start method: workers inherit it).
STATE: dict[str, Any] = {}


# --------------------------------------------------------------------------- #
def verify_sha256sums() -> tuple[int, int]:
    ok = total = 0
    for line in (UNIVERSE_DIR / "SHA256SUMS.txt").read_text().splitlines():
        if not line.strip():
            continue
        digest, name = line.split(None, 1)
        name = name.strip().lstrip("*")
        total += 1
        p = UNIVERSE_DIR / name
        if p.exists() and hashlib.sha256(p.read_bytes()).hexdigest() == digest:
            ok += 1
    return ok, total


def cfg_hash(cfg) -> str:
    blob = json.dumps(
        [cfg.indicators, cfg.scanner, cfg.signal_engine, cfg.htf_filter], sort_keys=True, default=str
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _signals_worker(args):
    ds, sym = args
    return sym, signals_for(STATE["native"][ds], [sym], STATE["cfg"])[sym]


def cached_signals(ds: str, symbols: list[str], cfg, cache_dir: Path, workers: int) -> dict:
    sums = hashlib.sha256((UNIVERSE_DIR / "SHA256SUMS.txt").read_bytes()).hexdigest()[:16]
    path = cache_dir / f"signals_{ds}_{sums}_{cfg_hash(cfg)}.pkl"
    cache: dict = pickle.loads(path.read_bytes()) if path.exists() else {}
    missing = [s for s in symbols if s not in cache]
    if missing:
        t = time.time()
        with mp.get_context("fork").Pool(workers) as pool:
            for sym, sig in pool.imap_unordered(_signals_worker, [(ds, s) for s in missing], chunksize=4):
                cache[sym] = sig
        path.write_bytes(pickle.dumps(cache))
        print(f"  [{ds}] computed signals for {len(missing)} symbols in {time.time() - t:.0f}s", flush=True)
    return {s: cache[s] for s in symbols}


# --------------------------------------------------------------------------- #
def make_random_tiebreak(seed: int):
    """Alternate processing order (§0.10): engine_score stays the primary key, the liquidity
    tie-break is replaced by a seeded random draw per call. Deterministic given `seed`."""
    rng = np.random.default_rng(seed)

    def ranked(signals, liquidity):  # same signature as src.signal_engine.rank_signals
        keys = {s: rng.random() for s in sorted(signals)}
        return sorted(signals, key=lambda s: (-signals[s].engine_score, keys[s], s))

    return ranked


def case_inputs(case: dict) -> tuple[dict, dict, dict | None, dict | None]:
    ds, syms = case["dataset"], case["symbols"]
    eg = {s: STATE["enriched"][ds][s] for s in syms}
    sig = {s: STATE["signals"][ds][s] for s in syms}
    masks = None if case["mask"] is None else {s: STATE["masks"][ds][case["mask"]][s] for s in syms}
    costs = None if case["costs"] == "flat" else {s: STATE["costs"][ds][s] for s in syms}
    return eg, sig, masks, costs


def run_sim(case: dict, order_seed: int | None = None):
    eg, sig, masks, costs = case_inputs(case)
    kwargs = dict(
        use_rank_order=case["rank"], eligible_mask_by_symbol=masks, costs_by_symbol=costs,
        **STATE.get("harness_kw", {}),
    )
    args = (eg, sig, COST_PRESETS[COST_NAME], STATE["cfg"].risk, STATE["cfg"].trade_planner, EQUITY, PPY, _REPO_ROOT)
    if order_seed is None:
        return simulate_portfolio(*args, **kwargs)
    with patch.object(signal_engine, "rank_signals", make_random_tiebreak(order_seed)):
        return simulate_portfolio(*args, **kwargs)


def instrumented_sim(case: dict) -> tuple[Any, dict]:
    """Canonical run with RiskEngine.check_new_trade wrapped read-only (same pattern as
    run_rank_order_validation.py): counts rejections by reason, and detects the same-bar cash-race
    clip (backtest_live_bot docstring note 5) by comparing each fill's spent quote amount with the
    amount the risk engine approved for that plan."""
    log: list = []
    original = RiskEngine.check_new_trade

    def wrapped(self, symbol, side, *args, **kwargs):
        d = original(self, symbol, side, *args, **kwargs)
        if side == "buy":
            log.append((kwargs.get("now"), symbol, d.allowed, d.max_quote_amount, d.reason))
        return d

    with patch.object(RiskEngine, "check_new_trade", wrapped):
        res = run_sim(case)
    rej: dict[str, int] = {}
    for _, _, ok, _, reason in log:
        if not ok:
            key = "new-entry rate cap" if "rate cap" in (reason or "") else ("no room (exposure/cash)" if "no room" in (reason or "") else (reason or "?").split("(")[0].strip()[:40])
            rej[key] = rej.get(key, 0) + 1
    approvals = sorted((now, sym, amt) for now, sym, ok, amt, _ in log if ok)
    fee = COST_PRESETS[COST_NAME].fee
    clipped = 0
    for tr in res.trades.itertuples():
        prior = [amt for now, sym, amt in approvals if sym == tr.symbol and now < tr.entry_time]
        if prior and (tr.qty * tr.entry_px / (1 - fee)) < prior[-1] * 0.999:
            clipped += 1
    diag = dict(checks=len(log), approved=sum(1 for e in log if e[2]), rejections=rej, fills_clipped_by_cash=clipped)
    return res, diag


def _placebo_worker(case: dict):
    t = time.time()
    eg, sig, masks, costs = case_inputs(case)
    df = placebo_run(
        eg, sig, COST_PRESETS[COST_NAME], STATE["cfg"].risk, STATE["cfg"].trade_planner, EQUITY, PPY, _REPO_ROOT,
        n_sims=STATE["n_placebo"], seed=PLACEBO_SEED, use_rank_order=case["rank"],
        eligible_mask_by_symbol=masks, costs_by_symbol=costs, **STATE.get("harness_kw", {}),
    )
    print(f"  placebo done: {case['id']} ({time.time() - t:.0f}s)", flush=True)
    return case["id"], df


def _order_worker(args):
    case, seed = args
    res = run_sim(case, seed)
    return case["id"], seed, res


# --------------------------------------------------------------------------- #
def decomposition(trades: pd.DataFrame, orig: set[str]) -> dict:
    out = {}
    for name, sub in (("original_16", trades[trades.symbol.isin(orig)]), ("added", trades[~trades.symbol.isin(orig)])):
        r = sub["R"].to_numpy(dtype=float)
        lo, hi = bootstrap_ci(r) if len(r) else (float("nan"), float("nan"))
        out[name] = dict(
            trades=int(len(sub)), avg_R=float(np.nanmean(r)) if len(r) else float("nan"), ci=[lo, hi],
            net_pnl=float(sub["net_pnl"].sum()) if len(sub) else 0.0,
            symbols_traded=int(sub.symbol.nunique()),
        )
    return out


def metrics_for(res, idx: pd.DatetimeIndex, orig: set[str]) -> dict:
    m = summarize(res)
    tr = res.trades
    t0, t1 = idx[0], idx[-1]
    cut = t0 + (t1 - t0) * 0.7
    late = tr[tr.entry_time >= cut] if len(tr) else tr
    early = tr[tr.entry_time < cut] if len(tr) else tr
    return dict(
        trades=int(m["trades"]),
        win_rate=m.get("win_rate", float("nan")),
        avg_R=m.get("avg_R", float("nan")),
        ci=[m.get("avg_R_ci_low", float("nan")), m.get("avg_R_ci_high", float("nan"))],
        profit_factor=m.get("profit_factor", float("nan")),
        total_return=m["total_return"],
        max_drawdown=m["max_drawdown"],
        sharpe=m["sharpe"],
        avg_exposure=m["avg_exposure"],
        cut=str(cut.date()),
        is_trades=int(len(early)), is_avg_R=float(early.R.mean()) if len(early) else float("nan"),
        oos_trades=int(len(late)), oos_avg_R=float(late.R.mean()) if len(late) else float("nan"),
        decomposition=decomposition(tr, orig) if len(tr) else None,
        symbols_traded=int(tr.symbol.nunique()) if len(tr) else 0,
    )


def gates(m: dict, bench_sharpe: float, pr_ret: float | None, pr_R: float | None) -> dict:
    ci_low = m["ci"][0]
    g = {
        "min_100_trades": m["trades"] >= 100,
        "avg_r_ci_low_gt_0": bool(ci_low > 0) if not np.isnan(ci_low) else False,
        "later_30pct_avg_r_gt_0": bool(m["oos_avg_R"] > 0) if not np.isnan(m["oos_avg_R"]) else False,
        "beats_placebo_90th_pct": None if pr_ret is None else bool(pr_ret >= 0.90 and pr_R >= 0.90),
        "net_return_and_sharpe_gt_buy_hold": bool(m["total_return"] > 0 and m["sharpe"] > bench_sharpe),
    }
    return g


def criterion_c(m: dict) -> str:
    d = (m.get("decomposition") or {}).get("added")
    if not d or d["trades"] == 0:
        return "INCONCLUSIVE"
    if not d["avg_R"] > 0:
        return "FAIL"
    return "PASS" if d["trades"] >= 30 else "INCONCLUSIVE"


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", required=True, help="where to cache Stage-A signals (outside the repo)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--placebo-sims", type=int, default=N_PLACEBO, help="200 for the real run; lower only for smoke tests")
    ap.add_argument("--order-seeds", type=int, default=len(ORDER_SEEDS))
    ap.add_argument("--out-md", default=str(_THIS_DIR / "report_p1e_universe_gate.md"))
    ap.add_argument("--out-json", default=str(_THIS_DIR / "p1e_universe_gate_results.json"))
    ap.add_argument("--count-same-bar-approvals", action="store_true",
                    help="P6: count same-bar approvals against the exposure cap and cash (bot.py parity)")
    a = ap.parse_args()
    STATE["n_placebo"] = a.placebo_sims
    STATE["harness_kw"] = {"count_same_bar_approvals": True} if a.count_same_bar_approvals else {}
    hkw = STATE["harness_kw"]
    seeds = ORDER_SEEDS[: a.order_seeds]
    cache_dir = Path(a.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    t_start = time.time()

    ok, total = verify_sha256sums()
    print(f"SHA256SUMS: {ok}/{total} OK", flush=True)
    if ok != total:
        raise SystemExit("dataset snapshot does not match SHA256SUMS.txt -- refusing to run (P1d storage caveat)")

    cfg = load_config()
    STATE["cfg"] = cfg
    ucfg = cfg.universe_cfg  # read, never written: these are the live config's own values
    min_age = int(ucfg["min_listing_age_days"])
    exit_ratio = float(ucfg["exit_volume_ratio"])
    max_symbols = int(ucfg["max_symbols"])
    orig_list = original_bases(cfg.symbols)
    orig = set(orig_list)
    out: dict[str, Any] = dict(
        generated_at=pd.Timestamp.now(tz="UTC").isoformat(), sha256sums=f"{ok}/{total} OK",
        params=dict(min_listing_age_days=min_age, exit_volume_ratio=exit_ratio, max_symbols=max_symbols,
                    floors=FLOORS, cost=COST_NAME, placebo_sims=STATE["n_placebo"], placebo_seed=PLACEBO_SEED,
                    order_seeds=seeds, original_16=orig_list, excluded_bases=sorted(ALL_EXCLUDE_BASES),
                    **({"harness_version": "p1e-universe-v1 + p6-same-bar-exposure-v1"} if hkw else {})),
        datasets={}, cases={}, legacy={},
    )

    # ---- 1) load, enrich, eligibility, costs per dataset ----
    STATE.update(native={}, enriched={}, masks={}, costs={}, signals={}, idx={})
    top_n: dict[str, int] = {}
    for ds, (rel, rule) in DATASETS.items():  # kraken first: long's N comes from it
        frames, dropped = load_universe_raw(_THIS_DIR / rel)
        native = enrich_native(frames, cfg.indicators)
        idx = shared_axis(frames)
        enriched = {s: native[s].reindex(idx) for s in native}
        STATE["native"][ds], STATE["enriched"][ds], STATE["idx"][ds] = native, enriched, idx
        missing16 = sorted(orig - set(frames))
        info = dict(symbols_loaded=len(frames), dropped=dropped, bars=len(idx), window=[str(idx[0].date()), str(idx[-1].date())],
                    original_16_missing=missing16, eligibility={})
        STATE["masks"][ds] = {}
        for fname, floor in FLOORS.items():
            masks, _vol, _fvb = build_eligibility(
                rule, enriched, native, idx, floor=floor, exit_volume_ratio=exit_ratio,
                min_listing_age_days=min_age, max_symbols=max_symbols, top_n=top_n.get(fname),
            )
            counts = np.sum(np.vstack(list(masks.values())), axis=0)
            ever = sorted(s for s, mk in masks.items() if mk.any())
            if ds == "kraken_720_universe":
                top_n[fname] = int(min(counts[-1], max_symbols))
            STATE["masks"][ds][fname] = masks
            info["eligibility"][fname] = dict(
                rule=rule, floor=floor, top_n=top_n.get(fname) if rule == "rank_topn" else None,
                count_last_bar=int(counts[-1]), count_median=float(np.median(counts)),
                count_median_nonzero=float(np.median(counts[counts > 0])) if (counts > 0).any() else 0.0,
                count_max=int(counts.max()), first_bar_with_any=str(idx[int(np.argmax(counts > 0))].date()) if (counts > 0).any() else None,
                ever_eligible=len(ever), ever_eligible_original_16=len(set(ever) & orig),
            )
        out["datasets"][ds] = info
        print(f"[{ds}] {len(frames)} symbols, {len(idx)} bars {info['window']}, eligibility "
              f"{json.dumps({k: (v['count_last_bar'], v['count_median'], v['ever_eligible'], v['top_n']) for k, v in info['eligibility'].items()})}",
              flush=True)

        needed = sorted(set().union(*[{s for s, mk in m.items() if mk.any()} for m in STATE["masks"][ds].values()]) | (orig & set(frames)))
        STATE["signals"][ds] = cached_signals(ds, needed, cfg, cache_dir, a.workers)
        STATE["costs"][ds] = build_costs({s: enriched[s] for s in needed})
    out["top_n_long_universe"] = top_n

    # ---- 2) cases ----
    cases: list[dict] = []
    for ds in DATASETS:
        o16 = sorted(orig & set(STATE["native"][ds]))
        cases.append(dict(id=f"static16|{ds}", dataset=ds, symbols=o16, mask=None, costs="tiered", rank=True, kind="baseline"))
        for fname in FLOORS:
            ever = sorted(s for s, mk in STATE["masks"][ds][fname].items() if mk.any())
            cases.append(dict(id=f"discover-{fname}|{ds}", dataset=ds, symbols=ever, mask=fname, costs="tiered", rank=True, kind="candidate"))
            cases.append(dict(id=f"discover-{fname}-flat5bp|{ds}", dataset=ds, symbols=ever, mask=fname, costs="flat", rank=True, kind="info_flat"))
    by_id = {c["id"]: c for c in cases}

    # ---- 3) placebo (the expensive part) in parallel; canonical runs in parent meanwhile ----
    placebo_cases = [c for c in cases if c["kind"] in ("baseline", "candidate")]
    pool = mp.get_context("fork").Pool(a.workers)
    placebo_async = pool.map_async(_placebo_worker, placebo_cases, chunksize=1)
    order_jobs = [(c, s) for c in cases if c["kind"] == "candidate" for s in seeds]
    order_async = pool.map_async(_order_worker, order_jobs, chunksize=1)

    canon: dict[str, Any] = {}
    diags: dict[str, dict] = {}
    for c in cases:
        res, diags[c["id"]] = instrumented_sim(c)
        idx = STATE["idx"][c["dataset"]]
        canon[c["id"]] = (res, metrics_for(res, idx, orig))
        print(f"  canonical {c['id']}: {canon[c['id']][1]['trades']} trades, avg R {canon[c['id']][1]['avg_R']:.3f}", flush=True)

    # benchmarks (pre-registered §0.7 gate 5)
    bench: dict[str, dict] = {}
    for ds in DATASETS:
        eg = STATE["enriched"][ds]
        o16 = sorted(orig & set(eg))
        bench[ds] = {"ew_original_16": buy_hold({s: eg[s] for s in o16}, o16, EQUITY, PPY),
                     "btc": buy_hold({"BTC": eg["BTC"]}, ["BTC"], EQUITY, PPY)}
        for fname in FLOORS:
            ever = by_id[f"discover-{fname}|{ds}"]["symbols"]
            bench[ds][f"ew_ever_eligible_{fname}"] = buy_hold({s: eg[s] for s in ever}, ever, EQUITY, PPY)
    out["benchmarks"] = bench

    placebo = dict(placebo_async.get())
    order_results = order_async.get()
    pool.close()
    pool.join()

    for c in cases:
        res, m = canon[c["id"]]
        ds = c["dataset"]
        fname = c["mask"]
        bench_sh = bench[ds]["ew_original_16"]["sharpe"]
        if fname is not None:
            bench_sh = max(bench_sh, bench[ds][f"ew_ever_eligible_{fname}"]["sharpe"])
        pr_ret = pr_R = None
        if c["id"] in placebo:
            p = placebo[c["id"]]
            pr_ret = float((p["total_return"] < m["total_return"]).mean())
            pr_R = float((p["avg_R"].dropna() < m["avg_R"]).mean()) if m["trades"] else float("nan")
            m["placebo"] = dict(pct_return=pr_ret, pct_avg_R=pr_R, median_return=float(p["total_return"].median()),
                                median_avg_R=float(p["avg_R"].median()), median_trades=float(p["trades"].median()))
        m["benchmark_sharpe"] = bench_sh
        m["gates"] = gates(m, bench_sh, pr_ret, pr_R)
        m["criterion_c"] = criterion_c(m) if c["kind"] != "baseline" else None
        m["case"] = {k: v for k, v in c.items() if k != "symbols"} | {"n_symbols": len(c["symbols"])}
        m["risk_diagnostics"] = diags[c["id"]]
        out["cases"][c["id"]] = m

    # order robustness
    rob: dict[str, list] = {}
    for cid, seed, res in order_results:
        c = by_id[cid]
        m = metrics_for(res, STATE["idx"][c["dataset"]], orig)
        bench_sh = out["cases"][cid]["benchmark_sharpe"]
        g = gates(m, bench_sh, None, None)
        rob.setdefault(cid, []).append(dict(
            seed=seed, trades=m["trades"], avg_R=m["avg_R"], ci=m["ci"], total_return=m["total_return"],
            sharpe=m["sharpe"], oos_avg_R=m["oos_avg_R"],
            added_avg_R=(m["decomposition"] or {}).get("added", {}).get("avg_R"),
            added_trades=(m["decomposition"] or {}).get("added", {}).get("trades"),
            gates=g, criterion_c=criterion_c(m),
        ))
    for cid in rob:
        rob[cid].sort(key=lambda r: r["seed"])
        out["cases"][cid]["order_robustness"] = rob[cid]

    # ---- 3b) sanity: simulating only ever-eligible symbols == simulating a superset ----
    # Never-eligible symbols can never open a position, so dropping them must change nothing.
    # Checked by re-running the $1M candidate over the $100k candidate's (larger) symbol set.
    out["superset_equivalence"] = {}
    for ds in DATASETS:
        c1 = by_id[f"discover-1m|{ds}"]
        sup = dict(c1, symbols=by_id[f"discover-100k|{ds}"]["symbols"])
        assert set(c1["symbols"]) <= set(sup["symbols"])
        r_sup = run_sim(sup)
        a_, b_ = canon[c1["id"]][0].trades, r_sup.trades
        key = ["symbol", "entry_time", "exit_time", "R"]
        same = len(a_) == len(b_) and a_[key].reset_index(drop=True).equals(b_[key].reset_index(drop=True))
        out["superset_equivalence"][ds] = dict(symbols=len(c1["symbols"]), superset=len(sup["symbols"]), identical=bool(same))
        print(f"  superset equivalence {ds}: {same}", flush=True)

    # engine_score saturation (P1c finding A), on the symbols each dataset actually simulates
    out["engine_score_saturation"] = {}
    for ds in DATASETS:
        scores = [sig.engine_score for m_ in STATE["signals"][ds].values() for sig in m_.values()]
        out["engine_score_saturation"][ds] = dict(signals=len(scores), share_at_1=float(np.mean(np.isclose(scores, 1.0))) if scores else None)

    # ---- 4) legacy checks on data/ and data_long/ ----
    for name, rel in LEGACY.items():
        eg, sigs, idx = build_live_signals(str(_THIS_DIR / rel), cfg)
        r_flat = simulate_portfolio(eg, sigs, COST_PRESETS[COST_NAME], cfg.risk, cfg.trade_planner, EQUITY, PPY, _REPO_ROOT, **hkw)
        m_flat = metrics_for(r_flat, idx, orig)
        costs = build_costs(eg)
        r_same = simulate_portfolio(eg, sigs, COST_PRESETS[COST_NAME], cfg.risk, cfg.trade_planner, EQUITY, PPY, _REPO_ROOT,
                                    use_rank_order=True, costs_by_symbol=costs, **hkw)
        m_same = metrics_for(r_same, idx, orig)
        bh = buy_hold(eg, list(eg), EQUITY, PPY)
        p = placebo_run(eg, sigs, COST_PRESETS[COST_NAME], cfg.risk, cfg.trade_planner, EQUITY, PPY, _REPO_ROOT,
                        n_sims=STATE["n_placebo"], seed=PLACEBO_SEED, use_rank_order=True, costs_by_symbol=costs, **hkw)
        pr_ret = float((p["total_return"] < m_same["total_return"]).mean())
        pr_R = float((p["avg_R"].dropna() < m_same["avg_R"]).mean())
        m_same["placebo"] = dict(pct_return=pr_ret, pct_avg_R=pr_R)
        m_same["gates"] = gates(m_same, bh["sharpe"], pr_ret, pr_R)
        m_same["benchmark_sharpe"] = bh["sharpe"]
        out["legacy"][name] = dict(window=[str(idx[0].date()), str(idx[-1].date())], bars=len(idx),
                                   flat_sorted_reproduction=m_flat, static16_same_harness=m_same)
        print(f"  legacy {name}: flat/sorted {m_flat['trades']} trades avg R {m_flat['avg_R']:.3f}; "
              f"same-harness {m_same['trades']} trades avg R {m_same['avg_R']:.3f}", flush=True)

    out["runtime_s"] = round(time.time() - t_start)
    Path(a.out_json).write_text(json.dumps(out, indent=1, default=lambda x: None if isinstance(x, float) and np.isnan(x) else str(x)))
    Path(a.out_md).write_text(render_markdown(out))
    print(f"done in {out['runtime_s']}s -> {a.out_md}, {a.out_json}")


# --------------------------------------------------------------------------- #
def _f(x, pct=False, nd=2):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "n/a"
    return f"{x * 100:.1f}%" if pct else f"{x:.{nd}f}"


def _pf(b):
    return "n/a" if b is None else ("PASS" if b else "FAIL")


def render_markdown(out: dict) -> str:
    L = ["# P1e universe gate run: raw results (generated by run_universe_gate.py)\n",
         f"Generated {out['generated_at']}. Dataset snapshot: SHA256SUMS {out['sha256sums']}. "
         f"Rules: docs/p1e_universe_gate_run.md §0. Costs: {out['params']['cost']} fee + frozen tiered slippage "
         f"(5/20/50bp) unless marked flat. Placebo: {out['params']['placebo_sims']} sims, seed {out['params']['placebo_seed']}.\n"]
    if out["params"].get("harness_version"):
        L.append(f"Harness version: {out['params']['harness_version']} (docs/p6_harness_exposure_fix.md).\n")
    L.append("## Datasets and eligibility\n")
    L.append("| dataset | symbols loaded | bars | window | floor | rule | top-N | eligible last bar | median eligible | max | ever eligible (of which orig-16) | first bar with any |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for ds, info in out["datasets"].items():
        for fname, e in info["eligibility"].items():
            L.append(f"| {ds} | {info['symbols_loaded']} | {info['bars']} | {info['window'][0]} to {info['window'][1]} | {fname} | {e['rule']} | "
                     f"{e['top_n'] if e['top_n'] is not None else '-'} | {e['count_last_bar']} | {e['count_median']:.0f} | {e['count_max']} | "
                     f"{e['ever_eligible']} ({e['ever_eligible_original_16']}) | {e['first_bar_with_any']} |")
    L.append("\n## Results\n")
    L.append("| case | symbols | trades | win% | avg R | avg R 95% CI | PF | return | maxDD | Sharpe | bench Sharpe | later-30% n / avg R | placebo pct (ret/avgR) |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for cid, m in out["cases"].items():
        p = m.get("placebo")
        L.append(f"| {cid} | {m['case']['n_symbols']} | {m['trades']} | {_f(m['win_rate'], True)} | {_f(m['avg_R'])} | "
                 f"[{_f(m['ci'][0])}, {_f(m['ci'][1])}] | {_f(m['profit_factor'])} | {_f(m['total_return'], True)} | "
                 f"{_f(m['max_drawdown'], True)} | {_f(m['sharpe'])} | {_f(m['benchmark_sharpe'])} | {m['oos_trades']} / {_f(m['oos_avg_R'])} | "
                 f"{(str(round(p['pct_return'] * 100)) + 'th / ' + str(round(p['pct_avg_R'] * 100)) + 'th') if p else 'not run'} |")
    L.append("\n## Gates\n")
    L.append("| case | >=100 trades | CI low > 0 | later-30% avg R > 0 | placebo >=90th | return>0 & Sharpe>bench | all 5 | criterion (c) |")
    L.append("|---|---|---|---|---|---|---|---|")
    for cid, m in out["cases"].items():
        g = m["gates"]
        allp = all(v for v in g.values()) if None not in g.values() else None
        L.append(f"| {cid} | " + " | ".join(_pf(v) for v in g.values()) + f" | {_pf(allp)} | {m['criterion_c'] or '-'} |")
    L.append("\n## Tier decomposition (original 16 vs added)\n")
    L.append("| case | orig-16 trades | orig-16 avg R | orig-16 CI | added trades | added symbols traded | added avg R | added CI |")
    L.append("|---|---|---|---|---|---|---|---|")
    for cid, m in out["cases"].items():
        d = m.get("decomposition")
        if not d:
            continue
        o, a_ = d["original_16"], d["added"]
        L.append(f"| {cid} | {o['trades']} | {_f(o['avg_R'])} | [{_f(o['ci'][0])}, {_f(o['ci'][1])}] | {a_['trades']} | {a_['symbols_traded']} | "
                 f"{_f(a_['avg_R'])} | [{_f(a_['ci'][0])}, {_f(a_['ci'][1])}] |")
    L.append("\n## Processing-order robustness (random liquidity tie-break, seeds 1-10; no placebo)\n")
    L.append("| case | trades min-max | avg R min / median / max | return min / max | Sharpe min / max | added avg R min / max | seeds failing gates 1,2,3,5 or (c) |")
    L.append("|---|---|---|---|---|---|---|")
    for cid, m in out["cases"].items():
        rob = m.get("order_robustness")
        if not rob:
            continue
        ar = [r["avg_R"] for r in rob]
        fails = [r["seed"] for r in rob if not all(v for k, v in r["gates"].items() if k != "beats_placebo_90th_pct") or r["criterion_c"] != "PASS"]
        add = [r["added_avg_R"] for r in rob if r["added_avg_R"] is not None]
        L.append(f"| {cid} | {min(r['trades'] for r in rob)}-{max(r['trades'] for r in rob)} | {_f(min(ar))} / {_f(float(np.median(ar)))} / {_f(max(ar))} | "
                 f"{_f(min(r['total_return'] for r in rob), True)} / {_f(max(r['total_return'] for r in rob), True)} | "
                 f"{_f(min(r['sharpe'] for r in rob))} / {_f(max(r['sharpe'] for r in rob))} | "
                 f"{_f(min(add)) if add else 'n/a'} / {_f(max(add)) if add else 'n/a'} | {fails if fails else 'none'} |")
    L.append("\n## Risk-engine diagnostics (canonical runs)\n")
    L.append("| case | new-trade checks | approved | rejections by reason | fills clipped by same-bar cash race |")
    L.append("|---|---|---|---|---|")
    for cid, m in out["cases"].items():
        d = m.get("risk_diagnostics") or {}
        L.append(f"| {cid} | {d.get('checks')} | {d.get('approved')} | {d.get('rejections')} | {d.get('fills_clipped_by_cash')} |")
    L.append("\n## Sanity checks\n")
    for ds, e in (out.get("superset_equivalence") or {}).items():
        L.append(f"- superset equivalence ({ds}): $1M candidate over {e['symbols']} vs {e['superset']} symbols -> identical trades: {e['identical']}")
    for ds, e in (out.get("engine_score_saturation") or {}).items():
        L.append(f"- engine_score == 1.0 share ({ds}): {_f(e['share_at_1'], True)} of {e['signals']} signals")
    L.append("\n## Benchmarks (buy&hold, no costs)\n")
    L.append("| dataset | benchmark | return | maxDD | Sharpe |")
    L.append("|---|---|---|---|---|")
    for ds, bs in out["benchmarks"].items():
        for name, b in bs.items():
            L.append(f"| {ds} | {name} | {_f(b['total_return'], True)} | {_f(b['max_drawdown'], True)} | {_f(b['sharpe'])} |")
    L.append("\n## Legacy datasets (data/, data_long/)\n")
    L.append("| dataset | run | trades | avg R | CI | return | Sharpe | gates |")
    L.append("|---|---|---|---|---|---|---|---|")
    for name, lg in out["legacy"].items():
        f, s = lg["flat_sorted_reproduction"], lg["static16_same_harness"]
        L.append(f"| {name} | legacy flat 5bp, sorted order (reproduction) | {f['trades']} | {_f(f['avg_R'])} | [{_f(f['ci'][0])}, {_f(f['ci'][1])}] | "
                 f"{_f(f['total_return'], True)} | {_f(f['sharpe'])} | - |")
        L.append(f"| {name} | static-16 same harness (rank + tiered) | {s['trades']} | {_f(s['avg_R'])} | [{_f(s['ci'][0])}, {_f(s['ci'][1])}] | "
                 f"{_f(s['total_return'], True)} | {_f(s['sharpe'])} | {' '.join(_pf(v) for v in s['gates'].values())} |")
    L.append(f"\nRuntime: {out['runtime_s']}s.\n")
    return "\n".join(L)


if __name__ == "__main__":
    main()
