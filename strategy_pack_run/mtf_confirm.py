"""mtf_confirm.py - NEW strategy candidate: multi-timeframe trend confirmation, written to engine.py's generic
enter/hold/rank/atr contract on top of strategies.trend_ensemble (imported, unmodified).

SOURCE: the "multi-timeframe trend confirmation" concept on this routine's shortlist (originally from the
awesome-systematic-trading crypto catalog; its claimed Sharpe of 3.39 is NOT trusted). This is this project's OWN
implementation of the general idea, not a reproduction of any paper's system or results.

Idea: take a daily trend_ensemble entry only when the WEEKLY trend agrees -- weekly close above its
SMA(sma_weeks) AND that SMA rising. Exits (hold) are unchanged. Daily and weekly are both derived from the daily
bars (no intraday data needed). Look-ahead safe: a weekly value is only used from the daily bar at/after the week
close (right-closed, right-labelled weekly bins, forward-filled onto daily close times).

Note: the live bot's htf_filter already confirms 1h signals against the DAILY trend; this tests the analogous
daily-vs-weekly confirmation on the daily datasets. A-priori parameter (20 weeks ~ 140 days), one gated run.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from strategies import TrendParams, trend_ensemble


@dataclass(frozen=True)
class MtfParams:
    sma_weeks: int = 20   # weekly SMA length used for the higher-timeframe trend


def weekly_uptrend(df: pd.DataFrame, sma_weeks: int) -> pd.Series:
    """True on daily bars where the latest COMPLETED weekly close > weekly SMA(sma_weeks) and that SMA is rising."""
    wclose = df["close"].resample("1W", closed="right", label="right").last().dropna()
    sma = wclose.rolling(sma_weeks).mean()
    up = (wclose > sma) & (sma > sma.shift(1))
    up = up.where(sma.notna() & sma.shift(1).notna())
    return up.reindex(df.index, method="ffill")


def mtf_trend(df: pd.DataFrame, regime: pd.Series, p: MtfParams = MtfParams(), tp: TrendParams = TrendParams()) -> pd.DataFrame:
    out = trend_ensemble(df, regime, tp)
    wk = weekly_uptrend(df, p.sma_weeks)
    out["weekly_up"] = wk.fillna(False).astype(bool)
    out["enter"] = out["enter"].astype(bool) & out["weekly_up"].to_numpy()
    out["warm"] = out["warm"] & wk.notna()
    return out
