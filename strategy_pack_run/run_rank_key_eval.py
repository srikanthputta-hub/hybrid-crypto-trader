"""run_rank_key_eval.py - evaluates the three pre-registered rank keys (docs/engine_score_saturation.md §2).

One-off evidence script (same family as run_universe_gate.py / run_rank_order_validation.py), not part
of the Daily Routine. Imports, never modifies: backtest_live_bot (simulate_portfolio, placebo_run,
_placebo_signal_at), engine, run_universe_gate (gates, metrics_for, make_random_tiebreak's RNG
pattern), universe_gate_inputs, run_engine_score_diagnosis (raw-strength rescan).

How a candidate key is injected (pre-registered §2.2): runner-local `unittest.mock.patch` of
`src.signal_engine.rank_signals` (P1e §0.10's mechanism). Each real Signal gets its candidate-term
values attached under `rationale["_rk"]` (rationale never affects a simulated number; it is only
copied into a plan's audit rationale), and `_placebo_signal_at` is wrapped so a placebo signal carries
the original's `_rk` exactly as it already carries the original's `engine_score`. The legacy rows use
NO patch at all, so they run origin/main's code path untouched.

Every job's result is cached under --cache-dir, so an interrupted run resumes where it stopped.

Run from strategy_pack_run/:
    PYTHONPATH=..:. python3 -W ignore run_rank_key_eval.py --cache-dir <scratch> --workers 4
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import multiprocessing as mp
import pickle
import sys
import time
from contextlib import ExitStack
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

import backtest_live_bot  # noqa: E402
from backtest_live_bot import load_dir, placebo_run, simulate_portfolio  # noqa: E402
from engine import COST_PRESETS, buy_hold  # noqa: E402
from run_engine_score_diagnosis import rows_for  # noqa: E402
from run_universe_gate import gates, metrics_for, verify_sha256sums  # noqa: E402
from src import signal_engine  # noqa: E402
from src.config import load_config  # noqa: E402
from src.indicators import add_indicators  # noqa: E402
from universe_gate_inputs import (  # noqa: E402
    build_costs,
    build_eligibility,
    enrich_native,
    load_universe_raw,
    original_bases,
    shared_axis,
)

EQUITY, PPY, COST_NAME = 10_000.0, 365.0, "kraken_T3_taker"
N_PLACEBO, PLACEBO_SEED, ORDER_SEEDS = 200, 0, list(range(1, 11))
CANDS = ["legacy", "A", "B", "C"]
GATED_CASES = ["S1", "S2", "U1"]
STATE: dict[str, Any] = {}


# --------------------------------------------------------------------------- #
# candidate keys (pre-registered §2.1)
# --------------------------------------------------------------------------- #
def _missing_last(v) -> tuple[int, float]:
    return (0, -float(v)) if v is not None and math.isfinite(float(v)) else (1, 0.0)


def signal_terms(cand: str, sig) -> tuple:
    """Every sort term BEFORE the liquidity tie-break (criterion (i) ties are measured on this)."""
    if cand == "legacy":
        return (-sig.engine_score,)
    rk = sig.rationale.get("_rk", {})
    if cand == "A":
        return _missing_last(rk.get("A"))
    if cand == "B":
        return (-sig.engine_score, _missing_last(rk.get("B")))
    if cand == "C":
        return (-sig.engine_score, _missing_last(rk.get("C")))
    raise ValueError(cand)


def make_rank(cand: str, seed: int | None, tie_log: list | None):
    rng = np.random.default_rng(seed) if seed is not None else None

    def ranked(signals, liquidity):  # same signature as src.signal_engine.rank_signals
        terms = {s: signal_terms(cand, signals[s]) for s in signals}
        if tie_log is not None and len(signals) >= 2:
            top = min(terms.values())
            k = sum(1 for v in terms.values() if v == top)
            tie_log.append((len(signals), k if k >= 2 else 0))
        if rng is not None:  # P1e make_random_tiebreak's exact RNG consumption pattern
            keys = {s: rng.random() for s in sorted(signals)}
            return sorted(signals, key=lambda s: (terms[s], keys[s], s))

        def liq(s):
            v = liquidity.get(s)
            return -v if v is not None else float("inf")

        return sorted(signals, key=lambda s: (terms[s], liq(s), s))

    return ranked


_orig_placebo_signal_at = backtest_live_bot._placebo_signal_at


def _placebo_carry_rk(original, enriched_g, i):
    sig = _orig_placebo_signal_at(original, enriched_g, i)
    if sig is not None and "_rk" in original.rationale:
        sig.rationale["_rk"] = dict(original.rationale["_rk"])
    return sig


def _patches(cand: str, seed: int | None, tie_log: list | None) -> ExitStack:
    st = ExitStack()
    if cand != "legacy" or seed is not None:
        st.enter_context(patch.object(signal_engine, "rank_signals", make_rank(cand, seed, tie_log)))
    if cand != "legacy":
        st.enter_context(patch.object(backtest_live_bot, "_placebo_signal_at", _placebo_carry_rk))
    return st


# --------------------------------------------------------------------------- #
# case construction
# --------------------------------------------------------------------------- #
def attach_terms(rows: pd.DataFrame, native: dict[str, pd.DataFrame], cfg) -> dict[str, dict]:
    b = float(cfg.signal_engine.get("confluence_bonus", 0.08))
    L = int(cfg.scanner.get("momentum_lookback", 5))
    sigs: dict[str, dict] = {}
    for r in rows.itertuples():
        nat = native[r.symbol]
        i = nat.index.get_loc(r.ts)
        c_t, atr_t = float(nat["close"].iloc[i]), float(nat["atr"].iloc[i])
        mv = None
        if i - L >= 0 and atr_t > 0 and c_t > 0:
            c0 = float(nat["close"].iloc[i - L])
            if c0 > 0:
                mv = (c_t / c0 - 1.0) / (atr_t / c_t)
        adx = r.signal.primary_details.get("adx")
        rk = {"A": float(r.max_raw) + b * (int(r.n) - 1), "B": None if adx is None else float(adx), "C": mv}
        sig = dataclasses.replace(r.signal, rationale={**r.signal.rationale, "_rk": rk})
        sigs.setdefault(r.symbol, {})[r.ts] = sig
    for s in native:
        sigs.setdefault(s, {})
    return sigs


def build_cases(cfg, cache_dir: Path, workers: int) -> dict[str, dict]:
    path = cache_dir / "cases.pkl"
    if path.exists():
        return pickle.loads(path.read_bytes())
    orig = set(original_bases(cfg.symbols))
    cases: dict[str, dict] = {}
    ind = cfg.indicators
    for cid, rel in (("S1", "data"), ("S2", "data_long")):
        raw = load_dir(_THIS_DIR / rel)
        native = {s: add_indicators(df, ind["fast_ema"], ind["slow_ema"], ind["rsi_period"]) for s, df in raw.items()}
        idx = pd.DatetimeIndex(sorted(set().union(*[f.index for f in native.values()])))
        eg = {s: native[s].reindex(idx) for s in native}
        rows = rows_for(native, sorted(native), cfg, workers)
        cases[cid] = dict(eg=eg, sigs=attach_terms(rows, native, cfg), masks=None, costs=None, idx=idx,
                          bench_sharpe=buy_hold(eg, list(eg), EQUITY, PPY)["sharpe"], n_signals=len(rows))
        print(f"[{cid}] {len(rows)} signals", flush=True)
    u = cfg.universe_cfg
    top_n = None
    for cid, rel, rule in (("U1", "data_universe/raw_kraken", "floor"), ("INFO_LU", "data_universe/raw_long", "rank_topn")):
        frames, _ = load_universe_raw(_THIS_DIR / rel)
        native = enrich_native(frames, ind)
        idx = shared_axis(frames)
        enriched = {s: native[s].reindex(idx) for s in native}
        masks, _v, _f = build_eligibility(rule, enriched, native, idx, floor=1_000_000.0,
                                          exit_volume_ratio=float(u["exit_volume_ratio"]),
                                          min_listing_age_days=int(u["min_listing_age_days"]),
                                          max_symbols=int(u["max_symbols"]), top_n=top_n)
        if rule == "floor":
            counts = np.sum(np.vstack(list(masks.values())), axis=0)
            top_n = int(min(counts[-1], int(u["max_symbols"])))
        ever = sorted(s for s, mk in masks.items() if mk.any())
        rows = rows_for(native, ever, cfg, workers)
        eg = {s: enriched[s] for s in ever}
        o16 = sorted(orig & set(enriched))
        bench = max(buy_hold({s: enriched[s] for s in o16}, o16, EQUITY, PPY)["sharpe"], buy_hold(eg, ever, EQUITY, PPY)["sharpe"])
        cases[cid] = dict(eg=eg, sigs=attach_terms(rows, {s: native[s] for s in ever}, cfg), masks={s: masks[s] for s in ever},
                          costs=build_costs(eg), idx=idx, bench_sharpe=bench, n_signals=len(rows), top_n=top_n)
        print(f"[{cid}] {len(ever)} symbols, {len(rows)} signals, top_n={top_n}", flush=True)
    path.write_bytes(pickle.dumps(cases))
    return cases


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #
def _sim(case: dict, rank: bool = True):
    return simulate_portfolio(case["eg"], case["sigs"], COST_PRESETS[COST_NAME], STATE["cfg"].risk, STATE["cfg"].trade_planner,
                              EQUITY, PPY, _REPO_ROOT, use_rank_order=rank, eligible_mask_by_symbol=case["masks"],
                              costs_by_symbol=case["costs"])


def run_job(job: tuple) -> Any:
    kind, cid, cand, seed = job
    case = STATE["cases"][cid]
    if kind == "sorted":
        return metrics_for(_sim(case, rank=False), case["idx"], STATE["orig"])
    if kind == "sim":
        tie_log: list = []
        with _patches(cand, seed, tie_log if seed is None else None):
            res = _sim(case)
        m = metrics_for(res, case["idx"], STATE["orig"])
        if seed is None:
            if cand == "legacy":  # measure legacy ties with the same counter, separate no-op-equivalent pass
                with patch.object(signal_engine, "rank_signals", make_rank("legacy", None, tie_log)):
                    res2 = _sim(case)
                assert res2.trades[["symbol", "entry_time", "R"]].equals(res.trades[["symbol", "entry_time", "R"]])
            m["tie_calls"] = len(tie_log)
            m["tie_competitors"] = int(sum(n for n, _ in tie_log))
            m["tie_tied_at_top"] = int(sum(k for _, k in tie_log))
            m["trades_df"] = res.trades
        return m
    if kind == "placebo":
        with _patches(cand, None, None):
            return placebo_run(case["eg"], case["sigs"], COST_PRESETS[COST_NAME], STATE["cfg"].risk, STATE["cfg"].trade_planner,
                               EQUITY, PPY, _REPO_ROOT, n_sims=N_PLACEBO, seed=PLACEBO_SEED, use_rank_order=True,
                               eligible_mask_by_symbol=case["masks"], costs_by_symbol=case["costs"])
    raise ValueError(kind)


def _worker(job):
    path = STATE["cache_dir"] / ("job_" + "_".join(str(x) for x in job) + ".pkl")
    if path.exists():
        return job, pickle.loads(path.read_bytes())
    t = time.time()
    out = run_job(job)
    path.write_bytes(pickle.dumps(out))
    print(f"  done {job} ({time.time() - t:.0f}s)", flush=True)
    return job, out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out-json", default=str(_THIS_DIR / "engine_score_rank_key_results.json"))
    ap.add_argument("--integrity-only", action="store_true")
    a = ap.parse_args()
    cache_dir = Path(a.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    ok, total = verify_sha256sums()
    if ok != total:
        raise SystemExit(f"SHA256SUMS {ok}/{total}: refusing to run")
    cfg = load_config()
    STATE.update(cfg=cfg, orig=set(original_bases(cfg.symbols)), cache_dir=cache_dir)
    STATE["cases"] = build_cases(cfg, cache_dir, a.workers)

    # ---- integrity check first (pre-registered §2.2) ----
    integ = {}
    for cid in ("S1", "S2"):
        integ[f"{cid}_sorted"] = _worker(("sorted", cid, "legacy", None))[1]
    for cid in GATED_CASES:
        integ[f"{cid}_legacy"] = _worker(("sim", cid, "legacy", None))[1]
    for k, m in integ.items():
        print(f"INTEGRITY {k}: {m['trades']} trades, avg R {m['avg_R']:.3f}", flush=True)
    expect = {"S1_sorted": (154, 0.18), "S2_sorted": (562, 0.52), "S1_legacy": (137, 0.11), "S2_legacy": (572, 0.53), "U1_legacy": (172, 0.41)}
    for k, (n, r) in expect.items():
        if integ[k]["trades"] != n or round(integ[k]["avg_R"], 2) != r:
            raise SystemExit(f"integrity check failed: {k} = {integ[k]['trades']} / {integ[k]['avg_R']:.3f}, expected {n} / {r}")
    print("INTEGRITY OK", flush=True)
    if a.integrity_only:
        return

    jobs = [("placebo", cid, c, None) for cid in GATED_CASES for c in CANDS]
    jobs += [("sim", cid, c, None) for cid in GATED_CASES + ["INFO_LU"] for c in CANDS]
    jobs += [("sim", cid, c, s) for cid in GATED_CASES + ["INFO_LU"] for c in CANDS for s in ORDER_SEEDS]
    with mp.get_context("fork").Pool(a.workers) as pool:
        results = dict(pool.imap_unordered(_worker, jobs, chunksize=1))

    out: dict[str, Any] = {"generated_at": pd.Timestamp.now(tz="UTC").isoformat(), "integrity": {k: {"trades": m["trades"], "avg_R": m["avg_R"]} for k, m in integ.items()}, "cases": {}}
    for cid in GATED_CASES + ["INFO_LU"]:
        case = STATE["cases"][cid]
        for c in CANDS:
            m = dict(results[("sim", cid, c, None)])
            m.pop("trades_df", None)
            pr = results.get(("placebo", cid, c, None))
            pr_ret = pr_R = None
            if pr is not None:
                pr_ret = float((pr["total_return"] < m["total_return"]).mean())
                pr_R = float((pr["avg_R"].dropna() < m["avg_R"]).mean())
                m["placebo"] = dict(pct_return=pr_ret, pct_avg_R=pr_R)
            m["benchmark_sharpe"] = case["bench_sharpe"]
            m["gates"] = gates(m, case["bench_sharpe"], pr_ret, pr_R)
            seeds = []
            for s in ORDER_SEEDS:
                ms = results[("sim", cid, c, s)]
                g = gates(ms, case["bench_sharpe"], None, None)
                seeds.append(dict(seed=s, trades=ms["trades"], avg_R=ms["avg_R"], ci_low=ms["ci"][0], oos_avg_R=ms["oos_avg_R"],
                                  sharpe=ms["sharpe"], total_return=ms["total_return"],
                                  fails=[k for k, v in g.items() if k != "beats_placebo_90th_pct" and not v]))
            m["order_seeds"] = seeds
            out["cases"][f"{cid}|{c}"] = m
    def _strict(x):  # strict JSON: NaN/inf -> null (json.dumps would otherwise emit bare NaN tokens)
        if isinstance(x, dict):
            return {k: _strict(v) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return [_strict(v) for v in x]
        if isinstance(x, (float, np.floating)):
            return float(x) if math.isfinite(float(x)) else None
        if isinstance(x, (np.integer, np.bool_)):
            return x.item()
        return x

    Path(a.out_json).write_text(json.dumps(_strict(out), indent=1, allow_nan=False, default=str))
    print(f"wrote {a.out_json}")


if __name__ == "__main__":
    main()
