"""phase2_weekly_filter.py -- study-only context gates for docs/phase2_weekly_filter_study.md.

RESEARCH HARNESS ONLY. Nothing here is imported by src/ or by the live bot, and nothing in src/ is
changed. The three pre-registered candidates (doc §0.3) are ENTRY VETOES applied to the unchanged
daily pipeline's final signal set (scanner -> signal_engine -> htf_filter, as produced by
backtest_live_bot.build_enriched_and_signals). A vetoed (symbol, bar) signal simply never becomes a
plan; every later stage (trade_planner, risk_engine, monitor) is the unmodified harness.

  W1  weekly trend gate: src.htf_filter.daily_trend_confirmed_from_df on the last
      max(slow+5, 30) CLOSED weekly closes (Kraken 1w anchor: Thursday 00:00 UTC), same EMA
      lengths as indicators.fast_ema / slow_ema. Fails closed below `slow` weekly bars.
  W2  BTC regime veto: research.v2_signals.regime_on (latest completed BTC daily close > SMA200).
  W3  W1 AND W2.

Index conventions (same as backtest_live_bot.load_csv): daily series are indexed by bar CLOSE
time. Weekly series returned here are indexed by WEEKLY CLOSE time (bin open + 7 days). A weekly
bar is usable at decision time t iff its close time <= t (doc §0.4) -- the forming week is never
visible, which is what `weekly.loc[:as_of]` enforces.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from backtest_live_bot import _htf_fetch_bars  # noqa: E402  (the live htf wrapper's fetch-length rule)
from research.v2_signals import regime_on  # noqa: E402
from src import htf_filter  # noqa: E402

DAY = pd.Timedelta(days=1)
WEEK = pd.Timedelta(days=7)
# "168h" with origin="epoch" bins on Thursday 00:00 UTC (1970-01-01 was a Thursday). NOT "7D":
# on pandas 3.0.6 "7D" silently ignores `origin` and starts bins on the data's first day.
WEEK_RULE = "168h"
REGIME_SMA = 200
CANDIDATES = ("W1", "W2", "W3")


# --------------------------------------------------------------------------- #
# Series construction
# --------------------------------------------------------------------------- #
def weekly_closes(daily_close: pd.Series) -> pd.Series:
    """Weekly closes from a daily-close series indexed by daily CLOSE time.

    Daily bars are binned by their OPEN time (close - 1 day) into Thursday-00:00-UTC weeks; the
    weekly close is the last daily close in the bin; the result is indexed by the weekly CLOSE
    time (bin start + 7 days). Empty bins (data gaps) are dropped, never filled. A week whose last
    daily bar is missing still closes at bin start + 7 days, with the last available close."""
    s = daily_close.dropna()
    if s.empty:
        return pd.Series(dtype=float, index=pd.DatetimeIndex([], tz="UTC", name="close_time"))
    by_open = pd.Series(s.to_numpy(dtype=float), index=s.index - DAY)
    w = by_open.resample(WEEK_RULE, origin="epoch").last().dropna()
    w.index = w.index + WEEK
    w.index.name = "close_time"
    return w


def stitch_closes(primary: pd.Series, seed: pd.Series | None) -> pd.Series:
    """Gate-input warm-up seed (doc §0.4): `seed` rows STRICTLY BEFORE the primary's first close
    time, then the primary unchanged. Seed rows at/after the primary start are never used."""
    primary = primary.dropna()
    if seed is None or primary.empty:
        return primary.copy()
    pre = seed.dropna()
    pre = pre.loc[pre.index < primary.index[0]]
    out = pd.concat([pre, primary])
    out.index.name = primary.index.name
    return out


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #
def weekly_trend_at(weekly: pd.Series, as_of: pd.Timestamp, fast_ema: int, slow_ema: int) -> bool:
    """W1 reference (point-in-time) implementation: only weekly bars CLOSED at or before `as_of`,
    tailed to the live htf wrapper's fetch length, through the unmodified htf_filter function."""
    eligible = weekly.loc[:as_of].tail(_htf_fetch_bars(slow_ema))
    return htf_filter.daily_trend_confirmed_from_df(pd.DataFrame({"close": eligible}), fast_ema, slow_ema)


def weekly_gate_series(
    weekly: pd.Series, target_index: pd.DatetimeIndex, fast_ema: int, slow_ema: int
) -> pd.Series:
    """W1 on every decision time in `target_index`. The verdict can only change when a weekly bar
    closes, so it is computed once per weekly close time (with weekly_trend_at) and carried
    forward to later decision times; decision times before the first weekly close are False.
    Equal to calling weekly_trend_at at every t (tested)."""
    target_index = pd.DatetimeIndex(target_index)
    if len(weekly) == 0:
        return pd.Series(False, index=target_index)
    per_week = pd.Series(
        [weekly_trend_at(weekly, t, fast_ema, slow_ema) for t in weekly.index], index=weekly.index, dtype=object
    )
    out = per_week.reindex(target_index, method="ffill")
    return out.where(out.notna(), False).astype(bool)


def btc_regime_series(btc_daily_close: pd.Series, target_index: pd.DatetimeIndex, sma_len: int = REGIME_SMA) -> pd.Series:
    """W2: research.v2_signals.regime_on, reused unchanged (latest completed BTC daily close at or
    before t is above its SMA(sma_len); False while the SMA is undefined)."""
    return regime_on(btc_daily_close.dropna().to_frame("close"), pd.DatetimeIndex(target_index), sma_len)


def build_gates(
    closes_by_symbol: Mapping[str, pd.Series],
    btc_close: pd.Series,
    target_index: pd.DatetimeIndex,
    fast_ema: int,
    slow_ema: int,
) -> dict[str, dict[str, pd.Series]]:
    """{candidate: {symbol: bool Series over target_index}} for W1, W2, W3. `closes_by_symbol` /
    `btc_close` are the gate INPUT daily closes (already seeded if applicable)."""
    w2 = btc_regime_series(btc_close, target_index)
    gates: dict[str, dict[str, pd.Series]] = {c: {} for c in CANDIDATES}
    for sym, close in closes_by_symbol.items():
        w1 = weekly_gate_series(weekly_closes(close), target_index, fast_ema, slow_ema)
        gates["W1"][sym] = w1
        gates["W2"][sym] = w2.copy()
        gates["W3"][sym] = w1 & w2
    return gates


def apply_gate(signals_by_symbol: Mapping[str, Mapping[pd.Timestamp, Any]], gate_by_symbol: Mapping[str, pd.Series]) -> dict:
    """Removes vetoed (symbol, bar) signals. Kept signals are the SAME objects; the input is not
    mutated. A symbol with signals but no gate series raises (no silent pass-through)."""
    out: dict[str, dict[pd.Timestamp, Any]] = {}
    for sym, sig_map in signals_by_symbol.items():
        if sym not in gate_by_symbol:
            raise KeyError(f"apply_gate: no gate series for {sym!r}")
        g = gate_by_symbol[sym]
        out[sym] = {ts: sig for ts, sig in sig_map.items() if bool(g.get(ts, False))}
    return out


# --------------------------------------------------------------------------- #
# Statistics (doc §0.6 / §0.7)
# --------------------------------------------------------------------------- #
def _entry_days(trades: pd.DataFrame) -> pd.Series:
    t = pd.DatetimeIndex(trades["entry_time"])
    t = t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")
    return pd.Series(t.normalize(), index=trades.index)


def day_block_ci(trades: pd.DataFrame, n: int = 5000, seed: int = 0) -> tuple[float, float]:
    """95% percentile CI of trade-weighted mean R, resampling whole UTC entry days with replacement."""
    if len(trades) < 5:
        return (math.nan, math.nan)
    days = _entry_days(trades)
    g = pd.DataFrame({"d": days.to_numpy(), "R": trades["R"].to_numpy(float)}).groupby("d")["R"].agg(["sum", "count"])
    s, c = g["sum"].to_numpy(), g["count"].to_numpy()
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, len(g), size=(n, len(g)))
    means = s[draws].sum(axis=1) / c[draws].sum(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def paired_delta_ci(tr_b: pd.DataFrame, tr_c: pd.DataFrame, n: int = 5000, seed: int = 0) -> dict:
    """Paired day-block bootstrap of Δ = mean R(C) - mean R(B). Blocks = union of both runs' UTC
    entry days; each resample draws days with replacement and evaluates both runs on the SAME drawn
    days. Resamples where either side has no trades are skipped and counted."""
    db, dc = _entry_days(tr_b), _entry_days(tr_c)
    days = pd.DatetimeIndex(sorted(set(db) | set(dc)))
    pos = {d: i for i, d in enumerate(days)}
    nd = len(days)
    sb, cb, sc, cc = (np.zeros(nd) for _ in range(4))
    for d, r in zip(db, tr_b["R"].to_numpy(float)):
        sb[pos[d]] += r
        cb[pos[d]] += 1
    for d, r in zip(dc, tr_c["R"].to_numpy(float)):
        sc[pos[d]] += r
        cc[pos[d]] += 1
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, nd, size=(n, nd))
    nb, nc = cb[draws].sum(axis=1), cc[draws].sum(axis=1)
    ok = (nb > 0) & (nc > 0)
    delta = sc[draws].sum(axis=1)[ok] / nc[ok] - sb[draws].sum(axis=1)[ok] / nb[ok]
    point = float(tr_c["R"].mean() - tr_b["R"].mean())
    return dict(
        delta=point,
        lo=float(np.percentile(delta, 2.5)) if len(delta) else math.nan,
        hi=float(np.percentile(delta, 97.5)) if len(delta) else math.nan,
        skipped=int((~ok).sum()),
        days=nd,
    )


def per_year(trades: pd.DataFrame) -> dict[int, tuple[int, float]]:
    if len(trades) == 0:
        return {}
    y = pd.DatetimeIndex(trades["entry_time"]).year
    return {int(k): (int(len(v)), float(v.mean())) for k, v in trades["R"].groupby(y)}


def leave_one_year_out(tr_b: pd.DataFrame, tr_c: pd.DataFrame) -> dict[int, float]:
    """Δ avg R with each entry year removed in turn (years present in either run)."""
    yb, yc = pd.DatetimeIndex(tr_b["entry_time"]).year, pd.DatetimeIndex(tr_c["entry_time"]).year
    out = {}
    for y in sorted(set(yb) | set(yc)):
        rb, rc = tr_b["R"][yb != y], tr_c["R"][yc != y]
        out[int(y)] = float(rc.mean() - rb.mean()) if len(rb) and len(rc) else math.nan
    return out


def later_split(trades: pd.DataFrame, idx: pd.DatetimeIndex, frac: float = 0.7) -> tuple[pd.Timestamp, pd.DataFrame]:
    """Same cut as run_live_bot_report.py: t0 + frac*(t1-t0); later trades = entry_time >= cut."""
    cut = idx[0] + (idx[-1] - idx[0]) * frac
    return cut, trades[trades["entry_time"] >= cut]


def trade_diff(tr_b: pd.DataFrame, tr_c: pd.DataFrame) -> dict:
    """Trades removed (in B, not C) and added (in C, not B), matched on (symbol, entry_time)."""
    kb = set(zip(tr_b["symbol"], tr_b["entry_time"]))
    kc = set(zip(tr_c["symbol"], tr_c["entry_time"]))
    rem = tr_b[[k not in kc for k in zip(tr_b["symbol"], tr_b["entry_time"])]]
    add = tr_c[[k not in kb for k in zip(tr_c["symbol"], tr_c["entry_time"])]]
    return dict(
        common=len(kb & kc),
        removed_n=int(len(rem)), removed_avg_R=float(rem["R"].mean()) if len(rem) else math.nan,
        added_n=int(len(add)), added_avg_R=float(add["R"].mean()) if len(add) else math.nan,
    )
