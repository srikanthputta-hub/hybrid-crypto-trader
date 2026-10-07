"""run_engine_score_diagnosis.py - where does `engine_score` saturate? (docs/engine_score_saturation.md §1)

Read-only diagnosis. Imports, never modifies: src.scanner / src.signal_engine / src.htf_filter,
backtest_live_bot (simulate_portfolio, compute_symbol_signals), universe_gate_inputs (P1e builder).

For every bar of every symbol it runs the REAL scanner on the same trailing window
`compute_symbol_signals` uses, but with `src.scanner._clip01` temporarily replaced by the identity,
so each candidate's RAW (pre-clip) strength is visible. The real clipped strength is then re-derived
as clip01(raw) -- exactly what the unpatched detector returns, because `_clip01` is the last
operation on every detector's strength and never feeds a detection condition. The real
`signal_engine.evaluate` + HTF gate are then applied to the clipped candidates, and the resulting
Signal is asserted equal to `compute_symbol_signals`' own output (sanity check, below).

It also records, from a canonical `use_rank_order=True` harness run with the LEGACY rank key, every
`rank_signals` call with >= 2 competing symbols (the calls where processing order can matter).

Run from strategy_pack_run/:
    PYTHONPATH=..:. python3 run_engine_score_diagnosis.py --out <scratch>/diag.pkl
"""
from __future__ import annotations

import argparse
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

from backtest_live_bot import (  # noqa: E402
    SCAN_TAIL_BARS,
    compute_symbol_signals,
    load_dir,
    simulate_portfolio,
)
from engine import COST_PRESETS  # noqa: E402
from src import htf_filter, scanner, signal_engine  # noqa: E402
from src.config import load_config  # noqa: E402
from src.indicators import add_indicators  # noqa: E402

STATE: dict[str, Any] = {}


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def raw_rows_for_symbol(sym: str, enriched: pd.DataFrame, cfg) -> list[dict]:
    """Mirror of backtest_live_bot.compute_symbol_signals (daily path), recording raw strengths."""
    sc, se = cfg.scanner, cfg.signal_engine
    htf_enabled = bool(cfg.htf_filter.get("enabled", True))
    fast_ema, slow_ema = cfg.indicators["fast_ema"], cfg.indicators["slow_ema"]
    rows: list[dict] = []
    with patch.object(scanner, "_clip01", lambda x: x):
        for i in range(3, len(enriched)):
            window = enriched.iloc[max(0, i - SCAN_TAIL_BARS + 1): i + 1]
            raw_cands = scanner.scan(window, sc)
            if not raw_cands:
                continue
            clipped = [scanner.Candidate(c.pattern, c.direction, _clip01(c.strength), c.details) for c in raw_cands]
            sig = signal_engine.evaluate(clipped, **se)
            if sig is None or sig.direction != "bullish":
                continue
            if htf_enabled and not htf_filter.daily_trend_confirmed_from_df(window, fast_ema, slow_ema):
                continue
            group = [(c, r) for c, r in zip(clipped, raw_cands) if c.strength >= se.get("min_strength", 0.5) and c.direction == "bullish"]
            prim = next(c for c, _ in group if c.pattern == sig.primary_pattern)
            prim_raw = next(r for c, r in group if c.pattern == sig.primary_pattern)
            rows.append(dict(
                symbol=sym, ts=enriched.index[i], engine_score=sig.engine_score, n=len(group),
                primary=sig.primary_pattern, primary_strength=prim.strength, primary_raw=prim_raw.strength,
                max_raw=max(r.strength for _, r in group),
                patterns=tuple(c.pattern for c, _ in group),
                strengths=tuple(c.strength for c, _ in group),
                raws=tuple(r.strength for _, r in group),
                adx=float(window["adx"].iloc[-1]) if "adx" in window and not pd.isna(window["adx"].iloc[-1]) else np.nan,
                details=dict(prim.details),
                signal=sig,
            ))
    return rows


def _worker(sym: str):
    return sym, raw_rows_for_symbol(sym, STATE["native"][sym], STATE["cfg"])


def rows_for(native: dict[str, pd.DataFrame], symbols: list[str], cfg, workers: int) -> pd.DataFrame:
    STATE["native"], STATE["cfg"] = native, cfg
    out: list[dict] = []
    with mp.get_context("fork").Pool(workers) as pool:
        for _sym, rows in pool.imap_unordered(_worker, symbols, chunksize=2):
            out.extend(rows)
    return pd.DataFrame(out)


def contested_calls(eg, sigs, cfg, *, masks=None, costs_by_symbol=None, costs=None) -> list[dict]:
    """Canonical legacy rank run; returns every rank_signals call with >=2 symbols."""
    calls: list[dict] = []
    original = signal_engine.rank_signals

    def spy(signals, liquidity, *a, **k):
        ranked = original(signals, liquidity, *a, **k)
        if len(signals) >= 2:
            calls.append(dict(signals=dict(signals), liquidity={s: liquidity.get(s) for s in signals}, ranked=ranked))
        return ranked

    with patch.object(signal_engine, "rank_signals", spy):
        simulate_portfolio(eg, sigs, costs or COST_PRESETS["kraken_T3_taker"], cfg.risk, cfg.trade_planner, 10_000.0, 365.0,
                           _REPO_ROOT, use_rank_order=True, eligible_mask_by_symbol=masks, costs_by_symbol=costs_by_symbol)
    return calls


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    cfg = load_config()
    result: dict[str, Any] = {}
    t0 = time.time()

    # static-16 legacy datasets
    for name, rel in (("data", "data"), ("data_long", "data_long")):
        raw = load_dir(_THIS_DIR / rel)
        native = {s: add_indicators(df, cfg.indicators["fast_ema"], cfg.indicators["slow_ema"], cfg.indicators["rsi_period"]) for s, df in raw.items()}
        df = rows_for(native, sorted(native), cfg, a.workers)
        # sanity: same signal set + identical Signal objects as the harness's own Stage A
        ind = cfg.indicators
        for s in sorted(native):
            real = compute_symbol_signals(native[s], cfg.scanner, cfg.signal_engine, bool(cfg.htf_filter.get("enabled", True)),
                                          ind["fast_ema"], ind["slow_ema"])
            mine = {r.ts: r.signal for r in df[df.symbol == s].itertuples()}
            assert set(real) == set(mine), (name, s)
            assert all(real[k] == mine[k] for k in real), (name, s)
        idx = pd.DatetimeIndex(sorted(set().union(*[f.index for f in native.values()])))
        eg = {s: native[s].reindex(idx) for s in native}
        sigs = {s: {r.ts: r.signal for r in df[df.symbol == s].itertuples()} for s in native}
        result[name] = dict(rows=df.drop(columns=["signal"]), calls=contested_calls(eg, sigs, cfg))
        print(f"[{name}] {len(df)} signals, {len(result[name]['calls'])} contested calls ({time.time() - t0:.0f}s)", flush=True)

    # kraken_720_universe, $1M candidate (P1e's exact eligibility/costs)
    from universe_gate_inputs import build_costs, build_eligibility, enrich_native, load_universe_raw, shared_axis
    frames, _ = load_universe_raw(_THIS_DIR / "data_universe/raw_kraken")
    native = enrich_native(frames, cfg.indicators)
    idx = shared_axis(frames)
    enriched = {s: native[s].reindex(idx) for s in native}
    u = cfg.universe_cfg
    masks, _v, _f = build_eligibility("floor", enriched, native, idx, floor=1_000_000.0,
                                      exit_volume_ratio=float(u["exit_volume_ratio"]),
                                      min_listing_age_days=int(u["min_listing_age_days"]), max_symbols=int(u["max_symbols"]))
    ever = sorted(s for s, mk in masks.items() if mk.any())
    df = rows_for(native, ever, cfg, a.workers)
    pos = {ts: i for i, ts in enumerate(idx)}
    df["eligible"] = [bool(masks[r.symbol][pos[r.ts]]) for r in df.itertuples()]
    eg = {s: enriched[s] for s in ever}
    sigs = {s: {r.ts: r.signal for r in df[df.symbol == s].itertuples()} for s in ever}
    calls = contested_calls(eg, sigs, cfg, masks={s: masks[s] for s in ever}, costs_by_symbol=build_costs(eg))
    result["kraken_720_universe_1m"] = dict(rows=df.drop(columns=["signal"]), calls=calls)
    print(f"[kraken_720_universe_1m] {len(df)} signals ({int(df.eligible.sum())} eligible), {len(calls)} contested calls ({time.time() - t0:.0f}s)", flush=True)

    Path(a.out).write_bytes(pickle.dumps(result))
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
