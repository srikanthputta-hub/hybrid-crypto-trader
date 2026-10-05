"""investor_inspired.py - two NEW strategy candidates written to engine.py's generic enter/hold/rank/atr
contract, built on top of strategies.trend_ensemble (imported, unmodified).

IMPORTANT FRAMING: both are this project's OWN MECHANICAL TRANSLATION of a named investor's general
philosophy into something testable -- NOT a reproduction of anything they actually did, and the investor's
name is motivation for trying the mechanic, not evidence it works in crypto.

1. dalio_smooth_trend  (Dalio-inspired: prefer persistent, low-noise regimes over volatile ones)
   Same entry as trend_ensemble, but additionally require the trend to be a SMOOTH grind: R^2 of a linear fit of
   log-price over the last `r2_len` bars >= `r2_min` AND positive slope. Distinct from the BTC SMA(200) gate,
   which only checks direction, not smoothness.
2. druck_concentrated  (Druckenmiller-inspired: concentrate in the single highest-conviction idea)
   Of all symbols with a qualifying trend_ensemble entry on a bar, only the single highest-ranked one may enter
   (top `n`), instead of every qualifying symbol; paired with more aggressive sizing in the runner.

All parameter values are a-priori, NOT tuned (one gated run each). Every column on row i uses only rows <= i.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from strategies import TrendParams, trend_ensemble


@dataclass(frozen=True)
class SmoothParams:
    r2_len: int = 60       # bars over which trend smoothness is measured
    r2_min: float = 0.80   # minimum R^2 of the linear fit of log-price (a smooth grind up)


def trend_smoothness(close: pd.Series, n: int) -> tuple[pd.Series, pd.Series]:
    """(corr, R^2) of log-price against time over a trailing n-bar window (rows <= i only)."""
    t = pd.Series(np.arange(len(close), dtype=float), index=close.index)
    corr = np.log(close).rolling(n).corr(t)
    return corr, corr ** 2


def dalio_smooth_trend(df: pd.DataFrame, regime: pd.Series, p: SmoothParams = SmoothParams(),
                       tp: TrendParams = TrendParams()) -> pd.DataFrame:
    out = trend_ensemble(df, regime, tp)
    corr, r2 = trend_smoothness(df["close"], p.r2_len)
    smooth_up = (corr > 0) & (r2 >= p.r2_min)
    out["r2"] = r2
    out["enter"] = out["enter"].astype(bool) & smooth_up.fillna(False).to_numpy()
    out["rank"] = out["rank"].astype(float) + r2.fillna(0.0)   # votes first, smoother trend breaks ties
    out["warm"] = out["warm"] & r2.notna()
    return out


def conviction_rank(df: pd.DataFrame, regime: pd.Series, tp: TrendParams = TrendParams(), mom_len: int = 60) -> pd.DataFrame:
    """trend_ensemble with rank = votes + a (0,1) tie-break from trailing momentum, so ties across symbols resolve."""
    out = trend_ensemble(df, regime, tp)
    mom = df["close"] / df["close"].shift(mom_len) - 1.0
    out["rank"] = out["rank"].astype(float) + (mom / (1.0 + mom.abs()) + 1.0) / 2.0
    out["rank"] = out["rank"].where(mom.notna(), out["rank"].astype(float))
    return out


def concentrate_top_n(frames: dict[str, pd.DataFrame], n: int = 1) -> dict[str, pd.DataFrame]:
    """Allow an entry only for the top-n ranked symbols among those with enter=True on each bar."""
    idx = pd.DatetimeIndex(sorted(set().union(*[f.index for f in frames.values()])))
    syms = list(frames)
    enter = pd.DataFrame({s: frames[s]["enter"].reindex(idx).fillna(False).astype(bool) for s in syms})
    rank = pd.DataFrame({s: frames[s]["rank"].reindex(idx).astype(float) for s in syms}).where(enter)
    pos = rank.rank(axis=1, ascending=False, method="first")   # 1 = best among entering symbols on that bar
    allowed = enter & (pos <= n)
    out = {}
    for s, f in frames.items():
        g = f.copy()
        g["enter"] = allowed[s].reindex(f.index).fillna(False).to_numpy()
        out[s] = g
    return out
