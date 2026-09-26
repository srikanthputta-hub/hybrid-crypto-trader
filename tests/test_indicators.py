import numpy as np
import pandas as pd

from src.indicators import ema, rsi, sma


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
