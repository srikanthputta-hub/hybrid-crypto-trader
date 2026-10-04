"""overreaction_fade.py - long-only SHORT-TERM OVERREACTION FADE, written to engine.py's generic
enter/hold/rank/atr contract (same as strategies.py's trend_ensemble/tsmom, which this does NOT modify).

Idea (a different hypothesis from the trend pack): after a sharp, volatility-normalised drop over a few
days inside an established up-trend, price tends to snap partly back; buy the drop, sell the rebound.
Trend-following buys strength; this buys weakness, so the two are structurally different.

  enter : z <= -z_entry  AND  close > SMA(trend_len)  AND  BTC daily regime ON
            z = n-bar return / (ATR/close * sqrt(n))   -- an ATR-normalised n-day move
  hold  : False once close >= SMA(exit_len) (rebound achieved) OR more than max_hold bars since the last
          entry signal (time stop). The engine's ATR chandelier stop still applies as the loss limit.
  rank  : -z (the more oversold, the higher priority when capital is scarce)

All values are a-priori starting points, NOT tuned (one run, no search). Every column on row i uses only
rows <= i; signals are acted on at the NEXT bar's open by engine.run_backtest. Reuses strategies.py helpers
(import only).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from strategies import _atr, align_regime


@dataclass(frozen=True)
class FadeParams:
    n: int = 3               # bars over which the drop is measured
    z_entry: float = 2.0     # ATR-normalised drop required to trigger
    trend_len: int = 100     # only fade dips while close is above this SMA (established up-trend)
    exit_len: int = 5        # exit once close recovers to this SMA
    max_hold: int = 8        # time stop, in bars since the last entry signal
    atr_len: int = 14
    atr_mult: float = 3.0    # chandelier distance (must match EngineConfig.atr_mult)


def overreaction_fade(df: pd.DataFrame, regime: pd.Series, p: FadeParams = FadeParams()) -> pd.DataFrame:
    out = df[["open", "high", "low", "close"]].copy()
    close = df["close"]
    out["atr"] = _atr(df, p.atr_len)
    ret = close / close.shift(p.n) - 1.0
    atr_pct = pd.Series(out["atr"].to_numpy(), index=df.index) / close
    z = ret / (atr_pct * np.sqrt(p.n))
    trend_ok = close > close.rolling(p.trend_len).mean()
    reg = align_regime(regime, df.index)
    reg_on = reg.fillna(0.0) > 0.5
    signal = (z <= -p.z_entry) & trend_ok & reg_on
    out["regime"] = reg_on
    out["z"] = z
    out["enter"] = signal.fillna(False)

    # bars since the most recent entry signal (<= current bar only -> no look-ahead)
    pos = pd.Series(np.arange(len(df)), index=df.index, dtype=float)
    last_sig = pos.where(out["enter"]).ffill()
    since = (pos - last_sig).fillna(np.inf)
    rebound = close >= close.rolling(p.exit_len).mean()
    out["hold"] = ~(rebound | (since > p.max_hold))
    out["rank"] = (-z).fillna(-9.0)
    out["warm"] = reg.notna() & out["atr"].notna() & z.notna() & trend_ok.notna()
    return out
