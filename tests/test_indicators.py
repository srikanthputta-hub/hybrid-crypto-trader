import numpy as np
import pandas as pd

from src.indicators import adx, ema, rsi, sma


def test_sma_basic():
    s = pd.Series([1, 2, 3, 4, 5])
    out = sma(s, 2)
    assert np.isnan(out.iloc[0])
    assert out.iloc[1] == 1.5
    assert out.iloc[-1] == 4.5


def test_ema_reacts_faster_than_longer_span():
    s = pd.Series([1.0] * 20 + [10.0] * 10)
    fast = ema(s, 3)
    slow = ema(s, 10)
    # After the jump, the short-span EMA should have moved further toward the new value.
    assert fast.iloc[-1] > slow.iloc[-1]


def test_rsi_all_gains_is_100():
    s = pd.Series(range(1, 30))  # strictly increasing -> no losses at all
    out = rsi(s, period=14)
    assert out.iloc[-1] > 90


def test_rsi_all_losses_is_near_zero():
    s = pd.Series(range(30, 1, -1))  # strictly decreasing -> no gains at all
    out = rsi(s, period=14)
    assert out.iloc[-1] < 10


def test_rsi_flat_series_is_neutral():
    s = pd.Series([100.0] * 30)
    out = rsi(s, period=14)
    assert 45 <= out.iloc[-1] <= 55


def _ohlc(closes):
    return pd.DataFrame(
        {
            "high": [c * 1.002 for c in closes],
            "low": [c * 0.998 for c in closes],
            "close": closes,
        }
    )


def test_adx_high_on_a_strong_sustained_trend():
    closes = [100 + 0.6 * i for i in range(40)]  # clean, persistent uptrend
    out = adx(_ohlc(closes), period=14)
    assert out.iloc[-1] >= 25  # Wilder convention: >=25 confirms a real trend


def test_adx_low_on_a_choppy_directionless_market():
    closes = [100 + (i % 3 - 1) * 0.5 for i in range(40)]  # oscillates, no net direction
    out = adx(_ohlc(closes), period=14)
    assert out.iloc[-1] < 20  # Wilder convention: <20 is choppy/directionless
