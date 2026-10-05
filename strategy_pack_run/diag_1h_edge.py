"""diag_1h_edge.py - follow-up diagnosis to docs/p2_0_1h_rebaseline.md: WHY the live strategy's
edge shrinks at 1h. Analysis tooling only -- see docs/p2_0_1h_edge_diagnosis.md for the findings.

READ-ONLY WITH RESPECT TO THE LIVE PIPELINE. Nothing under src/, config/config.yaml,
engine.py, strategies.py or hyperopt_param.py is modified. Every pipeline stage is the real one:

  * signals: backtest_live_bot.compute_symbol_signals (-> src.scanner.scan ->
    src.signal_engine.evaluate -> src.htf_filter.daily_trend_confirmed_from_df), unchanged.
  * portfolio replay: backtest_live_bot.simulate_portfolio (-> src.trade_planner.build_plan ->
    src.risk_engine.RiskEngine.check_new_trade -> src.monitor.check_pending/check_open), unchanged.
    To attribute each realized trade to the pattern/plan that produced it, `recording()` swaps two
    module-level names INSIDE backtest_live_bot's namespace for the duration of one call
    (`trade_planner` -> a pass-through wrapper, `RiskEngine` -> a pass-through subclass). Both
    forward every argument to the real implementation and return its result untouched; they only
    append a copy of what they saw to a list. The fidelity check for this is that the recorded run
    reproduces P2.0's committed numbers exactly (see docs/p2_0_1h_edge_diagnosis.md §0).
  * parameter variants: a COPY of cfg.trade_planner with one key overridden (same
    `_set_nested`-style pattern as hyperopt_param.py), never config/config.yaml itself.

TWO THINGS HERE ARE NEW ANALYSIS LOGIC (not pipeline replays), both tested in
test_diag_1h_edge.py:

  1. `htf_confirmed_at` -- reproduces compute_symbol_signals' htf decision for ONE bar so that
     htf-REJECTED bullish signals (which compute_symbol_signals discards) can be studied too.
     Verified equal to compute_symbol_signals(htf_enabled=True)'s output on the full datasets by
     `classify_and_verify`, not just on synthetic data.
  2. `isolated_outcomes` -- the outcome of each plan traded ALONE (no portfolio, no risk engine):
     fill at the NEXT bar's open, then exit at the open of the bar after the first CLOSE at/below
     stop_loss or at/above take_profit -- exactly src/monitor.check_open's close-only trigger plus
     backtest_live_bot's next-open execution. Vectorized for speed; test_diag_1h_edge.py proves it
     agrees trade-for-trade with a loop that calls the real src.monitor.check_open. Decisions use
     only data up to and including the trigger bar's close (no look-ahead); the forward bars are
     the trade's OUTCOME, which is the thing being measured.
"""
from __future__ import annotations

import argparse
import contextlib
import math
import pickle
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import backtest_live_bot as blb  # noqa: E402
from engine import COST_PRESETS, Costs, buy_hold, summarize  # noqa: E402
from run_live_bot_report import PPY_BY_TF  # noqa: E402
from run_report import split_stats  # noqa: E402

from src import htf_filter, trade_planner  # noqa: E402
from src.config import load_config  # noqa: E402
from src.indicators import add_indicators  # noqa: E402
from src.risk_engine import RiskEngine  # noqa: E402
from src.signal_engine import Signal  # noqa: E402

GATE_COST = "kraken_T3_taker"


# --------------------------------------------------------------------------- #
# 1. Signals: every bullish signal (htf gate OFF), then the htf decision per signal
# --------------------------------------------------------------------------- #
def htf_confirmed_at(
    enriched: pd.DataFrame,
    i: int,
    fast_ema: int,
    slow_ema: int,
    daily_closes: pd.Series | None,
    tail: int = blb.SCAN_TAIL_BARS,
) -> bool:
    """The htf gate decision compute_symbol_signals makes at bar i, reproduced for ONE bar (it only
    ever returns the bars that PASS, so the rejected ones are otherwise unobservable). Same two
    branches, same inputs: real daily closes up to this bar's close time when given (sub-daily
    replay), otherwise the scan-timeframe window itself (daily replay)."""
    if daily_closes is not None:
        as_of = enriched.index[i]
        eligible = pd.DataFrame({"close": daily_closes.loc[:as_of].tail(blb._htf_fetch_bars(slow_ema))})
        return htf_filter.daily_trend_confirmed_from_df(eligible, fast_ema, slow_ema)
    window = enriched.iloc[max(0, i - tail + 1) : i + 1]
    return htf_filter.daily_trend_confirmed_from_df(window, fast_ema, slow_ema)


def _signals_one_symbol(args):
    sym, raw, daily_close, cfg_ind, cfg_scan, cfg_sig = args
    fast, slow, rsi = cfg_ind["fast_ema"], cfg_ind["slow_ema"], cfg_ind["rsi_period"]
    enr = add_indicators(raw, fast, slow, rsi)
    all_bull = blb.compute_symbol_signals(enr, cfg_scan, cfg_sig, False, fast, slow, daily_closes=None)
    confirmed_ref = blb.compute_symbol_signals(enr, cfg_scan, cfg_sig, True, fast, slow, daily_closes=daily_close)
    pos = {ts: k for k, ts in enumerate(enr.index)}
    htf = {ts: htf_confirmed_at(enr, pos[ts], fast, slow, daily_close) for ts in all_bull}
    return sym, enr, all_bull, htf, confirmed_ref


def classify_and_verify(data_dir: str, timeframe: str, cfg, workers: int = 4) -> dict[str, Any]:
    """Every bullish signal per symbol + its htf decision. Asserts the htf-confirmed subset is
    EXACTLY what the harness's own compute_symbol_signals(htf_enabled=True) returns (same
    timestamps, same Signal objects) -- the guarantee that the 'rejected' set studied below is
    precisely what the live gate rejects, not an approximation."""
    raw = blb.load_dir(data_dir, timeframe)
    daily = None
    if timeframe != "1d":
        daily = {s: df["close"] for s, df in blb.load_dir(data_dir, "1d").items()}
    jobs = [
        (s, raw[s], (daily[s] if daily is not None else None), cfg.indicators, cfg.scanner, cfg.signal_engine)
        for s in sorted(raw)
    ]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(_signals_one_symbol, jobs))
    enriched_native, all_bull, htf, confirmed = {}, {}, {}, {}
    for sym, enr, ab, h, ref in results:
        mine = {ts: sig for ts, sig in ab.items() if h[ts]}
        assert list(mine) == list(ref), f"{sym}: htf reclassification disagrees with compute_symbol_signals"
        assert all(asdict(mine[ts]) == asdict(ref[ts]) for ts in ref), f"{sym}: signal payload mismatch"
        enriched_native[sym], all_bull[sym], htf[sym], confirmed[sym] = enr, ab, h, ref
    global_idx = pd.DatetimeIndex(sorted(set().union(*[f.index for f in enriched_native.values()])))
    enriched_global = {s: e.reindex(global_idx) for s, e in enriched_native.items()}
    return dict(
        data_dir=data_dir, timeframe=timeframe, enriched_native=enriched_native,
        enriched_global=enriched_global, idx=global_idx, all_bull=all_bull, htf=htf, confirmed=confirmed,
    )


# --------------------------------------------------------------------------- #
# 2. Portfolio replay with pass-through recording (attribution only)
# --------------------------------------------------------------------------- #
class _Recorder:
    def __init__(self):
        self.last_built: dict[str, tuple[Signal, Any, float, float]] = {}
        self.decisions: list[dict[str, Any]] = []


@contextlib.contextmanager
def recording():
    """Temporarily swap backtest_live_bot's `trade_planner`/`RiskEngine` names for pass-through
    recorders. Restores the originals on exit even on error. See module docstring."""
    rec = _Recorder()
    real_build = trade_planner.build_plan

    class _PlannerShim:
        @staticmethod
        def build_plan(sig, symbol, price, atr, **kw):
            plan = real_build(sig, symbol, price, atr, **kw)
            rec.last_built[symbol] = (sig, plan, price, atr)
            return plan

    class _RecordingRiskEngine(RiskEngine):
        def check_new_trade(self, symbol, side, **kw):
            d = super().check_new_trade(symbol, side, **kw)
            sig, plan, price, atr = rec.last_built[symbol]
            rec.decisions.append(dict(
                symbol=symbol, plan_time=kw.get("now"), allowed=d.allowed, reason=d.reason,
                quote=d.max_quote_amount, pattern=sig.primary_pattern, engine_score=sig.engine_score,
                n_supporting=len(sig.supporting_patterns), price=price, atr=atr,
                entry_low=plan.entry_zone[0], entry_high=plan.entry_zone[1], stop=plan.stop_loss,
                tp=plan.take_profit, invalidation=plan.invalidation,
                stop_mult_used=plan.rationale.get("stop_atr_mult_used"),
                adx_confirmed=plan.rationale.get("adx_trend_confirmed"),
            ))
            return d

    orig_tp, orig_re = blb.trade_planner, blb.RiskEngine
    blb.trade_planner, blb.RiskEngine = _PlannerShim, _RecordingRiskEngine
    try:
        yield rec
    finally:
        blb.trade_planner, blb.RiskEngine = orig_tp, orig_re


def replay_attributed(ds: dict, cfg, costs: Costs, planner_cfg: dict | None = None):
    """Real simulate_portfolio run + per-trade attribution. Returns (Result, trades_df, decisions_df).
    Each trade is linked to the last risk-APPROVED plan on its symbol strictly before its fill bar
    -- unambiguous because backtest_live_bot (like src/bot.py) holds at most one PENDING/OPEN plan
    per symbol. The link is then CHECKED: the trade's R recomputed from the linked plan's
    entry_high - stop_loss must equal the harness's own R."""
    planner_cfg = planner_cfg if planner_cfg is not None else cfg.trade_planner
    with recording() as rec:
        res = blb.simulate_portfolio(
            ds["enriched_global"], ds["confirmed"], costs, cfg.risk, planner_cfg, 10_000.0,
            PPY_BY_TF[ds["timeframe"]], _REPO_ROOT, timeframe=ds["timeframe"],
        )
    dec = pd.DataFrame(rec.decisions)
    tr = res.trades.copy()
    appr = dec[dec.allowed].sort_values("plan_time")
    linked = []
    for _, t in tr.iterrows():
        cand = appr[(appr.symbol == t.symbol) & (appr.plan_time < t.entry_time)]
        linked.append(cand.iloc[-1])
    L = pd.DataFrame(linked).reset_index(drop=True)
    for c in ["plan_time", "pattern", "engine_score", "n_supporting", "price", "atr", "entry_low", "entry_high",
              "stop", "tp", "invalidation", "stop_mult_used", "adx_confirmed"]:
        tr[c] = L[c].values
    tr["plan_time"] = pd.to_datetime(L["plan_time"], utc=True).values  # row-wise rebuild drops the tz
    tr["plan_time"] = pd.to_datetime(tr["plan_time"], utc=True)
    assert (tr.plan_time < tr.entry_time).all()
    risk = tr.entry_high - tr.stop
    r_check = tr.net_pnl / (tr.qty * risk)
    assert np.allclose(r_check, tr.R, rtol=1e-9, atol=1e-9), "trade<->plan attribution failed the R cross-check"
    entry_raw = tr.entry_px / (1 + costs.slippage)
    exit_raw = tr.exit_px / (1 - costs.slippage)
    tr["R_gross"] = (exit_raw - entry_raw) / risk
    tr["cost_drag_R"] = tr.R - tr.R_gross
    tr["stop_pct"] = (tr.entry_high - tr.stop) / tr.entry_high
    tr["risk_atr"] = risk / tr.atr
    tr["fill_gap_R"] = (entry_raw - tr.entry_high) / risk  # +ve = filled ABOVE the planned entry_high
    # how far below the planning close the pattern's level sat (0 for level-less MOMENTUM), in ATRs
    tr["level_dist_atr"] = (tr.price - tr.entry_low) / tr.atr - planner_cfg["entry_buffer_atr_mult"]
    return res, tr, dec


# --------------------------------------------------------------------------- #
# 3. Isolated per-plan outcomes (no portfolio) -- vectorized monitor.check_open semantics
# --------------------------------------------------------------------------- #
def isolated_outcomes(
    enr: pd.DataFrame, plan_bars: np.ndarray, stops: np.ndarray, tps: np.ndarray, entry_high: np.ndarray,
    costs: Costs, chunk: int = 512,
) -> pd.DataFrame:
    """For each plan created at the CLOSE of native bar plan_bars[k]: fill at open[k+1] (the plan's
    entry zone always contains the planning close, so backtest_live_bot always queues an immediate
    FILL -- see its docstring note 3), then the first bar j >= k+1 whose CLOSE <= stop (checked
    first, like monitor.check_open) or >= take_profit triggers an exit at open[j+1]. No trigger by
    the last bar -> 'end' exit at the last close. R is per unit of planned risk (entry_high -
    stop_loss), same definition as backtest_live_bot.record_trade, net of `costs`; R_gross is the
    same price move with zero fees/slippage."""
    o, c = enr["open"].to_numpy(float), enr["close"].to_numpy(float)
    n = len(c)
    out = []
    for k, sl, tp, eh in zip(plan_bars, stops, tps, entry_high):
        if k + 1 >= n:
            out.append((np.nan,) * 5 + ("nofill",))
            continue
        f = k + 1
        j_hit, reason = None, "end"
        start = f
        while start < n:
            seg = c[start : start + chunk]
            hit = (seg <= sl) | (seg >= tp)
            if hit.any():
                j = start + int(np.argmax(hit))
                j_hit, reason = j, ("stop_loss" if c[j] <= sl else "take_profit")
                break
            start += chunk
        if j_hit is not None and j_hit + 1 < n:
            ex_raw, ex_bar = o[j_hit + 1], j_hit + 1
        elif j_hit is not None:
            ex_raw, ex_bar, reason = c[j_hit], j_hit, "end"
        else:
            ex_raw, ex_bar = c[n - 1], n - 1
        en_raw = o[f]
        risk = eh - sl
        en_px = en_raw * (1 + costs.slippage)
        ex_px = ex_raw * (1 - costs.slippage)
        # per 1 unit of quote spent (scale-free): qty = (1 - fee)/en_px
        qty = (1 - costs.fee) / en_px
        net = qty * ex_px * (1 - costs.fee) - 1.0
        out.append((net / (qty * risk), (ex_raw - en_raw) / risk, ex_bar - f, (en_raw - eh) / risk, ex_raw, reason))
    return pd.DataFrame(out, columns=["R", "R_gross", "bars_held", "fill_gap_R", "exit_raw", "reason"])


def isolated_for_signals(ds: dict, cfg, costs: Costs, planner_cfg: dict | None = None) -> pd.DataFrame:
    """Isolated outcome of EVERY bullish signal (htf-confirmed and htf-rejected), planned with the
    real build_plan exactly as simulate_portfolio would (same ATR fallback)."""
    planner_cfg = planner_cfg if planner_cfg is not None else cfg.trade_planner
    rows = []
    for sym, sigs in ds["all_bull"].items():
        enr = ds["enriched_native"][sym]
        pos = {ts: k for k, ts in enumerate(enr.index)}
        recs = []
        for ts, sig in sigs.items():
            k = pos[ts]
            price = float(enr["close"].iat[k])
            a = enr["atr"].iat[k]
            atr_val = float(a) if not math.isnan(a) else price * 0.01
            plan = trade_planner.build_plan(sig, sym, price, atr_val, **planner_cfg)
            recs.append(dict(
                symbol=sym, time=ts, bar=k, pattern=sig.primary_pattern, engine_score=sig.engine_score,
                n_supporting=len(sig.supporting_patterns), htf=ds["htf"][sym][ts], price=price, atr=atr_val,
                entry_low=plan.entry_zone[0], entry_high=plan.entry_zone[1], stop=plan.stop_loss, tp=plan.take_profit,
                adx_confirmed=plan.rationale["adx_trend_confirmed"],
            ))
        if not recs:
            continue
        df = pd.DataFrame(recs)
        iso = isolated_outcomes(enr, df.bar.to_numpy(), df.stop.to_numpy(), df.tp.to_numpy(), df.entry_high.to_numpy(), costs)
        rows.append(pd.concat([df, iso], axis=1))
    out = pd.concat(rows, ignore_index=True)
    out["stop_pct"] = (out.entry_high - out.stop) / out.entry_high
    out["risk_atr"] = (out.entry_high - out.stop) / out.atr
    return out


def isolated_baseline(ds: dict, cfg, costs: Costs, every: int, planner_cfg: dict | None = None) -> pd.DataFrame:
    """Random-timing control: a plan at every `every`-th native bar of every symbol, built with
    exactly the placebo recipe backtest_live_bot.placebo_run uses (_placebo_signal_at: no pattern
    level -> zone centered on the bar's own close, real ADX carried so the trend-stop bonus still
    applies). Tagged with the real htf decision at that bar, so 'baseline | htf up' isolates what
    the daily-trend gate alone buys, with no scanner pattern involved."""
    planner_cfg = planner_cfg if planner_cfg is not None else cfg.trade_planner
    fast, slow = cfg.indicators["fast_ema"], cfg.indicators["slow_ema"]
    daily = None
    if ds["timeframe"] != "1d":
        daily = {s: df["close"] for s, df in blb.load_dir(ds["data_dir"], "1d").items()}
    dummy = Signal("bullish", "BASELINE", 0.5, ["BASELINE"], {}, {})
    rows = []
    for sym, enr in ds["enriched_native"].items():
        recs = []
        for k in range(60, len(enr) - 1, every):  # 60: past indicator warmup, matches nothing else's tuning
            sig = blb._placebo_signal_at(dummy, enr, k)
            if sig is None:
                continue
            price = float(enr["close"].iat[k])
            a = enr["atr"].iat[k]
            atr_val = float(a) if not math.isnan(a) else price * 0.01
            plan = trade_planner.build_plan(sig, sym, price, atr_val, **planner_cfg)
            htf_ok = htf_confirmed_at(enr, k, fast, slow, daily[sym] if daily is not None else None)
            recs.append(dict(symbol=sym, time=enr.index[k], bar=k, htf=htf_ok, price=price, atr=atr_val,
                             entry_high=plan.entry_zone[1], stop=plan.stop_loss, tp=plan.take_profit))
        df = pd.DataFrame(recs)
        iso = isolated_outcomes(enr, df.bar.to_numpy(), df.stop.to_numpy(), df.tp.to_numpy(), df.entry_high.to_numpy(), costs)
        rows.append(pd.concat([df, iso], axis=1))
    out = pd.concat(rows, ignore_index=True)
    out["stop_pct"] = (out.entry_high - out.stop) / out.entry_high
    return out


# --------------------------------------------------------------------------- #
# 4. Full 5-gate evaluation for a planner_cfg variant (same gates as run_live_bot_report.main)
# --------------------------------------------------------------------------- #
def _placebo_one(args):
    k, ds_light, costs, risk_cfg, planner_cfg, ppy, timeframe = args
    enriched_global, dense, idx = ds_light
    shifted = {}
    for s, arr in dense.items():
        rolled = np.roll(arr, k)
        enr = enriched_global[s]
        m = {}
        for i, sig in enumerate(rolled):
            if sig is None:
                continue
            ps = blb._placebo_signal_at(sig, enr, i)
            if ps is not None:
                m[idx[i]] = ps
        shifted[s] = m
    res = blb.simulate_portfolio(enriched_global, shifted, costs, risk_cfg, planner_cfg, 10_000.0, ppy, _REPO_ROOT,
                                 timeframe=timeframe)
    mm = summarize(res)
    return dict(total_return=mm["total_return"], avg_R=mm.get("avg_R", np.nan), trades=mm["trades"])


def placebo_parallel(ds, cfg, costs, planner_cfg, n_sims=200, seed=0, workers=4) -> pd.DataFrame:
    """Same placebo as backtest_live_bot.placebo_run -- same rng, same seed, same offset draws in the
    same order, same _placebo_signal_at rebuild, same simulate_portfolio -- just with the 200
    independent simulations fanned out over processes. Its equality with placebo_run's own output
    for the live config is checked in `main` (the baseline's percentile ranks must reproduce P2.0's
    92nd/100th exactly)."""
    idx = ds["idx"]
    T = len(idx)
    dense = {s: blb._dense_signal_array(m, idx) for s, m in ds["confirmed"].items()}
    rng = np.random.default_rng(seed)
    ks = [int(rng.integers(int(T * 0.10), int(T * 0.90))) for _ in range(n_sims)]
    light = (ds["enriched_global"], dense, idx)
    ppy = PPY_BY_TF[ds["timeframe"]]
    jobs = [(k, light, costs, cfg.risk, planner_cfg, ppy, ds["timeframe"]) for k in ks]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        return pd.DataFrame(list(ex.map(_placebo_one, jobs, chunksize=4)))


def gate_eval(ds, cfg, planner_cfg, n_sims=200, workers=4) -> dict[str, Any]:
    """The exact 5 gates run_live_bot_report.main computes, for one planner_cfg."""
    ppy = PPY_BY_TF[ds["timeframe"]]
    by_cost = {}
    for name, costs in COST_PRESETS.items():
        res = blb.simulate_portfolio(ds["enriched_global"], ds["confirmed"], costs, cfg.risk, planner_cfg, 10_000.0,
                                     ppy, _REPO_ROOT, timeframe=ds["timeframe"])
        by_cost[name] = (res, summarize(res))
    res, m = by_cost[GATE_COST]
    idx = ds["idx"]
    t0, t1 = idx[0], idx[-1]
    cut = t0 + (t1 - t0) * 0.7
    (n_is, w_is, r_is), (n_oos, w_oos, r_oos) = split_stats(res.trades, cut)
    bh_all = buy_hold(ds["enriched_global"], list(ds["enriched_global"]), 10_000.0, ppy)
    base = placebo_parallel(ds, cfg, COST_PRESETS[GATE_COST], planner_cfg, n_sims=n_sims, workers=workers)
    pr_ret = float((base["total_return"] < m["total_return"]).mean())
    pr_R = float((base["avg_R"].dropna() < m.get("avg_R", -9)).mean())
    ci_low = m.get("avg_R_ci_low", float("nan"))
    gates = {
        "min_100_trades": m["trades"] >= 100,
        "avg_r_ci_low_gt_0": (ci_low > 0) if not np.isnan(ci_low) else False,
        "later_30pct_avg_r_gt_0": (r_oos != "n/a" and float(r_oos) > 0),
        "beats_placebo_90th_pct": (pr_ret >= 0.90 and pr_R >= 0.90),
        "net_return_and_sharpe_gt_buy_hold": (m["total_return"] > 0 and m["sharpe"] > bh_all["sharpe"]),
    }
    return dict(
        by_cost={k: v[1] for k, v in by_cost.items()}, trades_gate=by_cost[GATE_COST][0].trades,
        split=((n_is, w_is, r_is), (n_oos, w_oos, r_oos)), cut=cut, placebo_pct=(pr_ret, pr_R),
        placebo_mean_avg_R=float(base["avg_R"].mean()), bh_sharpe=bh_all["sharpe"], gates=gates,
    )


# --------------------------------------------------------------------------- #
# CLI: cache signal sets (the expensive step) once, then every analysis reads the cache
# --------------------------------------------------------------------------- #
DATASETS = {
    "1h": ("data_15m", "1h"),          # P2.0's 1h re-baseline dataset (Coinbase 15m -> 1h)
    "1d_cb": ("data_15m", "1d"),       # same Coinbase source, daily -- the same-source daily control
    "1d_kraken": ("data", "1d"),       # the on-record current-config daily baseline (154 trades)
}


def load_or_build(name: str, cache_dir: Path, cfg, workers: int) -> dict:
    p = cache_dir / f"signals_{name}.pkl"
    if p.exists():
        return pickle.loads(p.read_bytes())
    t = time.time()
    ds = classify_and_verify(*DATASETS[name], cfg, workers=workers)
    p.write_bytes(pickle.dumps(ds))
    print(f"[{name}] signals built + htf reclassification verified in {time.time()-t:.0f}s", flush=True)
    return ds


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", required=True, help="where to cache signal sets / outputs (keep out of git)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--step", choices=["signals", "replay", "isolated", "variants"], required=True)
    ap.add_argument("--stop-mults", default="", help="comma-separated stop_atr_mult values for --step variants")
    ap.add_argument("--datasets", default="1h,1d_cb,1d_kraken")
    ap.add_argument("--sims", type=int, default=200)
    a = ap.parse_args()
    cache = Path(a.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    names = a.datasets.split(",")

    if a.step == "signals":
        for n in names:
            load_or_build(n, cache, cfg, a.workers)
    elif a.step == "replay":
        for n in names:
            ds = load_or_build(n, cache, cfg, a.workers)
            for cname in ("paper_today_0.10%", GATE_COST):
                res, tr, dec = replay_attributed(ds, cfg, COST_PRESETS[cname])
                m = summarize(res)
                print(f"[{n}] {cname}: trades={m['trades']} avg_R={m['avg_R']:.4f} win={m['win_rate']:.4f}", flush=True)
                tr.to_pickle(cache / f"trades_{n}_{cname}.pkl")
                dec.to_pickle(cache / f"decisions_{n}_{cname}.pkl")
    elif a.step == "isolated":
        for n in names:
            ds = load_or_build(n, cache, cfg, a.workers)
            iso = isolated_for_signals(ds, cfg, COST_PRESETS[GATE_COST])
            iso.to_pickle(cache / f"iso_signals_{n}.pkl")
            every = 4 if ds["timeframe"] == "1h" else 1
            base = isolated_baseline(ds, cfg, COST_PRESETS[GATE_COST], every=every)
            base.to_pickle(cache / f"iso_baseline_{n}.pkl")
            print(f"[{n}] isolated: {len(iso)} signals, {len(base)} baseline bars", flush=True)
    elif a.step == "variants":
        mults = [float(x) for x in a.stop_mults.split(",") if x]
        for n in names:
            ds = load_or_build(n, cache, cfg, a.workers)
            for mult in mults:
                pc = dict(cfg.trade_planner)  # COPY -- config/config.yaml is never touched
                pc["stop_atr_mult"] = mult
                t = time.time()
                ev = gate_eval(ds, cfg, pc, n_sims=a.sims, workers=a.workers)
                (cache / f"gates_{n}_stop{mult}.pkl").write_bytes(pickle.dumps(ev))
                g = ev["gates"]
                print(f"[{n}] stop_atr_mult={mult}: {sum(g.values())}/5 {g} "
                      f"T3taker avg_R={ev['by_cost'][GATE_COST]['avg_R']:.3f} placebo={ev['placebo_pct']} "
                      f"({time.time()-t:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
