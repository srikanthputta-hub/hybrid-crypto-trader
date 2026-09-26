"""Pure pandas technical indicators — no extra TA dependency needed."""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(window=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder's smoothing)."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    # avg_loss == 0 makes `rs` undefined (division by nan above), but it has a
    # real meaning: no losses at all in the lookback window.
    all_gains_no_losses = (avg_loss == 0) & (avg_gain > 0)
    no_movement_at_all = (avg_loss == 0) & (avg_gain == 0)
    out = out.where(~all_gains_no_losses, 100.0)
    out = out.where(~no_movement_at_all, 50.0)
    return out.fillna(50.0)  # neutral RSI wherever still undefined (e.g. warmup period)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder's smoothing) — a volatility measure in price units.

    Requires 'high', 'low', 'close' columns. Used both by the scanner (to size
    pattern-strength scores relative to current volatility) and the risk engine
    (to filter out abnormally volatile conditions).
    """
    prev_close = df["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def add_indicators(df: pd.DataFrame, fast_ema: int, slow_ema: int, rsi_period: int) -> pd.DataFrame:
    """Return a copy of an OHLCV dataframe (must have a 'close' column) with indicator columns added."""
    out = df.copy()
    out["ema_fast"] = ema(out["close"], fast_ema)
    out["ema_slow"] = ema(out["close"], slow_ema)
    out["rsi"] = rsi(out["close"], rsi_period)
    out["atr"] = atr(out, period=14)
    return out
