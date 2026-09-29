from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.htf_filter import _cache, daily_trend_confirmed, daily_trend_confirmed_from_df


def _daily_df(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


def test_daily_trend_confirmed_from_df_true_on_uptrend():
    # A steadily rising close series -> fast EMA pulls above slow EMA.
    closes = [100 + i for i in range(40)]
    assert daily_trend_confirmed_from_df(_daily_df(closes), fast_ema=5, slow_ema=20) is True


def test_daily_trend_confirmed_from_df_false_on_downtrend():
    closes = [140 - i for i in range(40)]
    assert daily_trend_confirmed_from_df(_daily_df(closes), fast_ema=5, slow_ema=20) is False


def test_daily_trend_confirmed_from_df_fails_closed_on_too_little_history():
    closes = [100, 101, 102]
    assert daily_trend_confirmed_from_df(_daily_df(closes), fast_ema=5, slow_ema=20) is False


@pytest.fixture(autouse=True)
def _clear_cache():
    _cache.clear()
    yield
    _cache.clear()


def test_daily_trend_confirmed_fetches_and_caches(monkeypatch):
    closes = [100 + i for i in range(40)]
    exchange = MagicMock()
    exchange.fetch_ohlcv_df.return_value = _daily_df(closes)
    today = date(2026, 9, 29)

    result = daily_trend_confirmed(exchange, "BTC/USDT", fast_ema=5, slow_ema=20, today=today)

    assert result is True
    exchange.fetch_ohlcv_df.assert_called_once_with("BTC/USDT", "1d", 30)

    # Second call same day -- served from cache, no second fetch.
    result2 = daily_trend_confirmed(exchange, "BTC/USDT", fast_ema=5, slow_ema=20, today=today)
    assert result2 is True
    exchange.fetch_ohlcv_df.assert_called_once()


def test_daily_trend_confirmed_refetches_on_new_day_and_prunes_old_entry():
    closes = [100 + i for i in range(40)]
    exchange = MagicMock()
    exchange.fetch_ohlcv_df.return_value = _daily_df(closes)

    daily_trend_confirmed(exchange, "ETH/USDT", fast_ema=5, slow_ema=20, today=date(2026, 9, 29))
    assert ("ETH/USDT", date(2026, 9, 29)) in _cache

    daily_trend_confirmed(exchange, "ETH/USDT", fast_ema=5, slow_ema=20, today=date(2026, 9, 30))

    assert exchange.fetch_ohlcv_df.call_count == 2
    assert ("ETH/USDT", date(2026, 9, 29)) not in _cache  # pruned
    assert ("ETH/USDT", date(2026, 9, 30)) in _cache


def test_daily_trend_confirmed_fails_closed_on_fetch_error():
    exchange = MagicMock()
    exchange.fetch_ohlcv_df.side_effect = RuntimeError("network error")

    result = daily_trend_confirmed(exchange, "SOL/USDT", fast_ema=5, slow_ema=20, today=date(2026, 9, 29))

    assert result is False
