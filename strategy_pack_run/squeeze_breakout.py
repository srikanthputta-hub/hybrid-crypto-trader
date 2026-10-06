"""squeeze_breakout.py - NEW strategy candidate: Bollinger-BandWidth volatility-SQUEEZE breakout, written to engine.py's
generic enter/hold/rank/atr contract (strategies._atr / align_regime helpers imported, unmodified).

SOURCE: the well-known Bollinger BandWidth "squeeze then surge" breakout pattern (John Bollinger's BandWidth indicator;
found via web search on 2026-10-06, e.g. LuxAlgo's "Bollinger Bands Strategy: Squeeze then Surge", the PyQuantLab
"Volatility Squeeze Breakout Strategy in Backtrader" write-up, and the Coinquant crypto breakout backtest). This is this
project's OWN daily-bar, long-only implementation of the general idea -- NOT a reproduction of any source's system, and none of
their claimed results were trusted or used. Mechanically different from the Donchian breakouts already tested: the trigger
requires a prior volatility COMPRESSION, not just a new high.

Rule (all a-priori, one gated run): BandWidth = (upper - lower) / middle of Bollinger(20, 2sigma). A "squeeze" is
BandWidth in the lowest `squeeze_pct` of its trailing `rank_len` values. ENTER when close > the upper band, a squeeze
occurred within the last `recent` bars, and the BTC SMA200 regime is ON. EXIT (hold False) when close falls below the
middle band; the engine's 3xATR chandelier is the loss limit. rank = tighter squeeze first.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from strategies import _atr, align_regime


@dataclass(frozen=True)
class SqueezeParams:
    bb_len: int = 20
    bb_k: float = 2.0
    rank_len: int = 120        # trailing window for the BandWidth percentile
    squeeze_pct: float = 0.20  # squeeze = BandWidth in the lowest 20% of the trailing window
    recent: int = 5            # the squeeze must have occurred within this many bars
    atr_len: int = 14


def squeeze_breakout(df: pd.DataFrame, regime: pd.Series, p: SqueezeParams = SqueezeParams()) -> pd.DataFrame:
    close = df["close"]
    mid = close.rolling(p.bb_len).mean()
    sd = close.rolling(p.bb_len).std(ddof=0)
    upper = mid + p.bb_k * sd
    bbw = (2.0 * p.bb_k * sd) / mid
    # percentile of today's BandWidth within its trailing window (rows <= i only)
    pct = bbw.rolling(p.rank_len).apply(lambda w: (w[:-1] < w[-1]).mean() if len(w) > 1 else np.nan, raw=True)
    squeeze = pct <= p.squeeze_pct
    recent_sq = squeeze.rolling(p.recent, min_periods=1).max().astype(bool)
    reg = align_regime(regime, df.index)
    reg_on = reg.fillna(0.0) > 0.5

    out = df[["open", "high", "low", "close"]].copy()
    out["atr"] = _atr(df, p.atr_len)
    out["regime"] = reg_on
    out["bbw_pct"] = pct
    out["enter"] = ((close > upper) & recent_sq & reg_on).fillna(False)
    out["hold"] = (close >= mid).fillna(False)
    out["rank"] = (1.0 - pct).fillna(0.0)
    out["warm"] = reg.notna() & out["atr"].notna() & pct.notna()
    return out
