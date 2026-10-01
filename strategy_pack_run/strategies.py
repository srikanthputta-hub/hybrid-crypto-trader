"""
strategies.py - two evidence-backed, long-only strategies for the 16-coin bot.

  1. trend_ensemble : Donchian breakout ensemble + BTC regime filter + ATR chandelier stop
  2. tsmom          : time-series momentum (long if N-bar return > 0), rebalanced every K bars

Both return a DataFrame with the columns the engine / your bot needs:
  open, high, low, close, atr, regime, enter, hold, rank
    enter : True on a CLOSED bar where a flat position may be opened (execute at NEXT bar's open)
    hold  : False on a closed bar means "exit at the next open" (the trailing stop also exits)
    rank  : higher = better; used to allocate scarce capital instead of watchlist order

Look-ahead safety: every column on row i uses only rows <= i (see test_pack.py truncation tests).
Bars are indexed by CLOSE time (open time + timeframe).

All parameter values are a-priori starting points from the playbook, NOT tuned values.
Requires: pip install TA-Lib pandas numpy
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import talib

TF_SECONDS = {"15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}


# --------------------------------------------------------------------------- #
# Data helpers
# --------------------------------------------------------------------------- #
def load_csv(path: str | Path, timeframe: str) -> pd.DataFrame:
    """CSV columns: timestamp (bar OPEN time: epoch ms or ISO), open, high, low, close, volume.
    Returns a float DataFrame indexed by bar CLOSE time (UTC)."""
    raw = pd.read_csv(path)
    ts = raw["timestamp"]
    if pd.api.types.is_numeric_dtype(ts):
        open_time = pd.to_datetime(ts, unit="ms", utc=True)
    else:
        open_time = pd.to_datetime(ts, utc=True)
    df = raw[["open", "high", "low", "close", "volume"]].astype(float).copy()
    df.index = open_time + pd.Timedelta(seconds=TF_SECONDS[timeframe])
    df.index.name = "close_time"
    return df[~df.index.duplicated(keep="last")].sort_index()


def daily_regime(btc: pd.DataFrame, sma_len: int = 200) -> pd.Series:
    """1.0 when the latest COMPLETED daily BTC close is above its daily SMA(sma_len), else 0.0 (NaN in warm-up).
    Indexed by daily close time. Works from any intraday timeframe (resampled with right-closed bins)."""
    daily = btc["close"].resample("1D", closed="right", label="right").last().dropna()
    sma = pd.Series(talib.SMA(daily.to_numpy(float), sma_len), index=daily.index)
    # 1.0 = ON, 0.0 = OFF, NaN = unknown during SMA warm-up
    return (daily > sma).astype(float).where(sma.notna())


def align_regime(reg: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    """Latest completed daily regime value at each bar close time. NaN until SMA warm-up is done."""
    return reg.reindex(index, method="ffill")


def _atr(df: pd.DataFrame, n: int) -> np.ndarray:
    return talib.ATR(df["high"].to_numpy(float), df["low"].to_numpy(float), df["close"].to_numpy(float), n)


def _donchian_state(close, entry_level, exit_level) -> np.ndarray:
    state = np.zeros(len(close), dtype=np.int8)
    on = 0
    for i in range(len(close)):
        if on == 0:
            if close[i] > entry_level[i]:  # NaN compares False -> safe warm-up
                on = 1
        elif close[i] < exit_level[i]:
            on = 0
        state[i] = on
    return state


# --------------------------------------------------------------------------- #
# Strategy 1: Donchian trend ensemble
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TrendParams:
    lookbacks: tuple = (10, 20, 40, 60, 100)  # in DAYS; multiplied by bars_per_day internally
    bars_per_day: int = 1          # 1 for 1d bars, 6 for 4h, 24 for 1h. Daily is the supported/validated setting
    min_votes: int = 2             # how many lookbacks must be "long" to open a position
    exit_divisor: int = 2          # exit channel = lookback // exit_divisor
    atr_len: int = 14
    atr_mult: float = 3.0          # chandelier stop distance (also used by the engine for sizing)
    exit_on_regime_off: bool = True

    @property
    def stop_mult(self) -> float:
        """Chandelier multiple in units of the running timeframe's ATR. Scaled by sqrt(bars_per_day) so the stop sits
        at roughly the same price distance as the daily version (random-walk volatility scaling)."""
        return self.atr_mult * float(np.sqrt(self.bars_per_day))


def trend_ensemble(df: pd.DataFrame, regime: pd.Series, p: TrendParams = TrendParams()) -> pd.DataFrame:
    out = df[["open", "high", "low", "close"]].copy()
    out["atr"] = _atr(df, p.atr_len)
    close = df["close"].to_numpy(float)
    votes = np.zeros(len(df), dtype=np.int16)
    for n in (d * p.bars_per_day for d in p.lookbacks):
        entry = df["high"].rolling(n).max().shift(1).to_numpy()
        exit_ = df["low"].rolling(max(2, n // p.exit_divisor)).min().shift(1).to_numpy()
        votes += _donchian_state(close, entry, exit_)
    reg = align_regime(regime, df.index)
    reg_on = reg.fillna(0.0) > 0.5
    out["regime"] = reg_on
    out["votes"] = votes
    out["enter"] = (votes >= p.min_votes) & reg_on.to_numpy()
    out["hold"] = (votes >= 1) & (reg_on.to_numpy() | (not p.exit_on_regime_off))
    out["rank"] = votes.astype(float)
    out["warm"] = reg.notna() & out["atr"].notna()  # True when every input is valid
    return out


# --------------------------------------------------------------------------- #
# Strategy 2: time-series momentum
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MomParams:
    lookback: int = 28             # DAYS; the AUT paper's best crypto setup was 28 days / 5-day hold
    rebalance_every: int = 5       # DAYS between re-evaluations
    bars_per_day: int = 1          # 1 for 1d bars, 6 for 4h, 24 for 1h. Daily is the supported/validated setting
    atr_len: int = 14
    atr_mult: float = 3.0
    exit_on_regime_off: bool = True

    @property
    def stop_mult(self) -> float:
        return self.atr_mult * float(np.sqrt(self.bars_per_day))


def tsmom(df: pd.DataFrame, regime: pd.Series, p: MomParams = MomParams()) -> pd.DataFrame:
    out = df[["open", "high", "low", "close"]].copy()
    out["atr"] = _atr(df, p.atr_len)
    ret = df["close"] / df["close"].shift(p.lookback * p.bars_per_day) - 1.0
    update = pd.Series(np.arange(len(df)) % (p.rebalance_every * p.bars_per_day) == 0, index=df.index)
    raw = (ret > 0).where(update)               # evaluate only on rebalance bars
    state = raw.ffill().fillna(False).astype(bool)
    reg = align_regime(regime, df.index)
    reg_on = reg.fillna(0.0) > 0.5
    out["regime"] = reg_on
    out["enter"] = state & reg_on
    out["hold"] = state & (reg_on | (not p.exit_on_regime_off))
    out["rank"] = ret.fillna(-1.0)
    out["warm"] = reg.notna() & out["atr"].notna() & ret.notna()
    return out


STRATEGIES = {
    "trend_ensemble": (trend_ensemble, TrendParams),
    "tsmom": (tsmom, MomParams),
}
