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


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index (Wilder's smoothing) — trend-strength, 0-100.

    Requires 'high', 'low', 'close' columns. Wilder interpretation: ADX > 25
    means a real, sustainable trend; ADX < 20 means choppy/directionless
    conditions where breakout/momentum signals are more likely to fail. Used
    by the scanner as an additive strength-confirmation signal, never as a
    hard gate (see src/scanner.py).
    """
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    prev_high = high.shift(1)
    prev_low = low.shift(1)

    up_move = high - prev_high
    down_move = prev_low - low

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index
    )

    true_range = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    smoothed_tr = true_range.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    smoothed_plus_dm = plus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    smoothed_minus_dm = minus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    tr_safe = smoothed_tr.replace(0, np.nan)
    plus_di = 100 * (smoothed_plus_dm / tr_safe)
    minus_di = 100 * (smoothed_minus_dm / tr_safe)

    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    # Both +DI and -DI zero (no directional movement / no true range at all)
    # means no directional strength either way, not "undefined" — 0 is correct.
    dx = dx.fillna(0.0)

    out = dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    return out.fillna(0.0)  # still-warming-up bars read as 0 (no confirmed trend yet)


def add_indicators(df: pd.DataFrame, fast_ema: int, slow_ema: int, rsi_period: int) -> pd.DataFrame:
    """Return a copy of an OHLCV dataframe (must have a 'close' column) with indicator columns added."""
    out = df.copy()
    out["ema_fast"] = ema(out["close"], fast_ema)
    out["ema_slow"] = ema(out["close"], slow_ema)
    out["rsi"] = rsi(out["close"], rsi_period)
    out["atr"] = atr(out, period=14)
    out["adx"] = adx(out, period=14)
    return out
