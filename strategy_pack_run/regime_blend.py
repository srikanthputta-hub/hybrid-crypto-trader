"""regime_blend.py - NEW strategy candidate: regime-switching trend / mean-reversion blend, written to engine.py's
generic enter/hold/rank/atr contract (strategies.trend_ensemble imported, unmodified).

SOURCE: the "regime-switching trend/mean-reversion blend" concept on this routine's shortlist (originally from the
awesome-systematic-trading crypto catalog; its claimed Sharpe of 1.11 is NOT trusted or used). This is this project's
OWN mechanical implementation of the general idea, not a reproduction of any paper's system or results.

Per symbol, per bar, a regime filter (Wilder ADX(14), computed with TA-Lib like the rest of strategies.py) decides which
leg may open a position:
  * TRENDING (ADX >= adx_trend): the trend_ensemble entry (Donchian ensemble + BTC SMA200 regime) may enter.
  * CHOPPY   (ADX <  adx_chop) : a long-only mean-reversion entry may enter -- the 3-bar return z-score (vs rolling
                                  std of 1-bar returns) <= -z_entry (same construction as overreaction_fade.py).
  * in between: no entry.
Exits follow whichever leg generated the most recent entry signal: trend legs use trend_ensemble's hold; mean-reversion
legs exit on a rebound to SMA(exit_len) or a max_hold-bar time stop. (The strategy cannot see the engine's actual fill,
so "most recent entry signal" is the leg tag -- an approximation, same as overreaction_fade's time stop.)
The mean-reversion leg is NOT gated by the BTC regime or a trend-SMA (in a choppy market there is no trend to require).

All values are a-priori (ADX 25/20 are the textbook thresholds; the rest reuse overreaction_fade's), one gated run, no
tuning. Every column on row i uses only rows <= i.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import talib

from strategies import TrendParams, trend_ensemble


@dataclass(frozen=True)
class BlendParams:
    adx_len: int = 14
    adx_trend: float = 25.0   # >= : trending regime
    adx_chop: float = 20.0    # <  : choppy regime
    n: int = 3                # mean-reversion move horizon (bars)
    z_entry: float = 1.5
    vol_len: int = 20
    exit_len: int = 5
    max_hold: int = 8


def regime_blend(df: pd.DataFrame, regime: pd.Series, p: BlendParams = BlendParams(), tp: TrendParams = TrendParams()) -> pd.DataFrame:
    out = trend_ensemble(df, regime, tp)
    adx = pd.Series(talib.ADX(df["high"].to_numpy(float), df["low"].to_numpy(float), df["close"].to_numpy(float), p.adx_len), index=df.index)
    trending = adx >= p.adx_trend
    choppy = adx < p.adx_chop
    close = df["close"]
    sigma = close.pct_change().rolling(p.vol_len).std()
    z = (close / close.shift(p.n) - 1.0) / (sigma * np.sqrt(p.n))
    trend_enter = out["enter"].astype(bool) & trending.fillna(False)
    mr_enter = (z <= -p.z_entry).fillna(False) & choppy.fillna(False) & ~trend_enter

    leg = pd.Series(np.where(trend_enter, 1.0, np.where(mr_enter, 2.0, np.nan)), index=df.index).ffill()
    pos = pd.Series(np.arange(len(df)), index=df.index, dtype=float)
    last_sig = pos.where(trend_enter | mr_enter).ffill()
    since = (pos - last_sig).fillna(np.inf)
    mr_hold = ~((close >= close.rolling(p.exit_len).mean()) | (since > p.max_hold))
    trend_hold = out["hold"].astype(bool)

    out["adx"] = adx
    out["leg"] = leg
    out["enter"] = (trend_enter | mr_enter).to_numpy()
    out["hold"] = np.where(leg == 2.0, mr_hold.to_numpy(), trend_hold.to_numpy())
    out["rank"] = np.where(mr_enter, -z.fillna(0.0).to_numpy(), out["rank"].astype(float).to_numpy())
    out["warm"] = out["warm"] & adx.notna() & z.notna()
    return out
