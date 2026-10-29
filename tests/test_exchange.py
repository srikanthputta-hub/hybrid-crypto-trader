"""Tests for src/exchange.py's ExchangeClient.fetch_ohlcv_df.

These never hit a real exchange: ExchangeClient is constructed normally (so its
own __init__ wiring is exercised), then its real ccxt instance (`.client`) is
swapped out for a small fake object exposing just the three ccxt methods
fetch_ohlcv_df relies on: fetch_ohlcv, parse_timeframe, milliseconds. This
mirrors the fake-exchange pattern in research/v2_signals.py's
test_forming_candle_is_dropped, but is a standalone fake (not imported from
research/, which is intentionally not wired into src/).
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.exchange import ExchangeClient


class FakeCcxtClient:
    """Fake ccxt exchange: candles are 1h apart, starting at t=0."""

    def __init__(self, num_bars: int, now_ms: int, tf_seconds: int = 3600):
        self.num_bars = num_bars
        self.now_ms = now_ms
        self.tf_seconds = tf_seconds
        self.last_fetch_limit: int | None = None

    def fetch_ohlcv(self, symbol, timeframe=None, limit=None):
        self.last_fetch_limit = limit
        tf_ms = self.tf_seconds * 1000
        n = min(limit, self.num_bars) if limit is not None else self.num_bars
        return [
            [i * tf_ms, 100 + i, 101 + i, 99 + i, 100.5 + i, 10 + i]
            for i in range(n)
        ]

    def parse_timeframe(self, timeframe):
        return self.tf_seconds

    def milliseconds(self):
        return self.now_ms


def _make_client(fake: FakeCcxtClient) -> ExchangeClient:
    # testnet=False avoids ccxt's set_sandbox_mode entirely -- irrelevant here
    # since .client is swapped for the fake immediately after construction.
    client = ExchangeClient(exchange_id="kraken", testnet=False)
    client.client = fake
    return client


def test_forming_last_candle_is_excluded():
    # 5 bars of 1h fetched; "now" is 30min into the 5th (index-4) bar, so that
    # bar's close time (4h + 1h = 5h) is still in the future -> must be dropped.
    tf_ms = 3600_000
    now_ms = 4 * tf_ms + 30 * 60_000
    fake = FakeCcxtClient(num_bars=5, now_ms=now_ms)
    client = _make_client(fake)

    df = client.fetch_ohlcv_df("BTC/USDT", "1h", limit=4)

    # requested limit=4 -> should over-fetch 5, but only 4 bars (index 0..3)
    # have actually closed by `now_ms`.
    assert len(df) == 4
    assert fake.last_fetch_limit == 5
    # the forming 5th bar (open index 4) must not appear
    assert df["open"].tolist() == [100, 101, 102, 103]


def test_no_forming_candle_returns_full_limit():
    # "now" is well past the close of all 5 fetched bars -> nothing dropped,
    # normal `limit` count returned.
    tf_ms = 3600_000
    now_ms = 10 * tf_ms
    fake = FakeCcxtClient(num_bars=5, now_ms=now_ms)
    client = _make_client(fake)

    df = client.fetch_ohlcv_df("BTC/USDT", "1h", limit=4)

    assert len(df) == 4
    # most recent 4 of the 5 closed bars (index 1..4), chronological order
    assert df["open"].tolist() == [101, 102, 103, 104]


def test_timestamp_column_is_open_time_utc():
    tf_ms = 3600_000
    now_ms = 10 * tf_ms
    fake = FakeCcxtClient(num_bars=3, now_ms=now_ms)
    client = _make_client(fake)

    df = client.fetch_ohlcv_df("BTC/USDT", "1h", limit=3)

    assert isinstance(df["timestamp"].iloc[0], pd.Timestamp)
    assert str(df["timestamp"].iloc[0].tz) == "UTC"
    # row 0's open time is bar index 0's raw open ms (0), not its close time.
    assert df["timestamp"].iloc[0] == pd.Timestamp(0, unit="ms", tz="UTC")
    assert df["timestamp"].iloc[1] == pd.Timestamp(tf_ms, unit="ms", tz="UTC")


def test_fewer_closed_bars_than_limit_returns_what_is_available():
    # Only 2 bars exist at all, and "now" is mid-way through the 2nd (forming)
    # -> only 1 bar has actually closed, even though limit=10 was requested.
    tf_ms = 3600_000
    now_ms = tf_ms + 100  # just barely into bar index 1
    fake = FakeCcxtClient(num_bars=2, now_ms=now_ms)
    client = _make_client(fake)

    df = client.fetch_ohlcv_df("BTC/USDT", "1h", limit=10)

    assert len(df) == 1
    assert df["open"].tolist() == [100]
    # no error, no padding


def test_returned_dataframe_has_expected_columns_and_reset_index():
    tf_ms = 3600_000
    now_ms = 10 * tf_ms
    fake = FakeCcxtClient(num_bars=3, now_ms=now_ms)
    client = _make_client(fake)

    df = client.fetch_ohlcv_df("BTC/USDT", "1h", limit=3)

    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert df.index.tolist() == list(range(len(df)))


# --- issue #99: order placement / reconcile helpers (fake ccxt client, no network) ---


class _OrderClient:
    """Records create_order calls exactly as ccxt receives them."""

    def __init__(self):
        self.calls = []

    def create_order(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"id": "1"}


def test_create_market_order_without_client_id_is_the_pre_99_call():
    client = _make_client(FakeCcxtClient(num_bars=1, now_ms=0))
    client.client = _OrderClient()
    client.create_market_order("BTC/USD", "buy", 0.5)
    assert client.client.calls == [((), {"symbol": "BTC/USD", "type": "market", "side": "buy", "amount": 0.5})]


def test_create_market_order_sends_client_order_id_as_unified_param():
    client = _make_client(FakeCcxtClient(num_bars=1, now_ms=0))
    client.client = _OrderClient()
    client.create_market_order("BTC/USD", "sell", 0.5, client_order_id="hct0123456789abcde")
    assert client.client.calls == [((), {"symbol": "BTC/USD", "type": "market", "side": "sell", "amount": 0.5,
                                          "params": {"clientOrderId": "hct0123456789abcde"}})]


def test_create_market_order_still_rejects_a_bad_side_before_any_call():
    client = _make_client(FakeCcxtClient(num_bars=1, now_ms=0))
    client.client = _OrderClient()
    with pytest.raises(ValueError):
        client.create_market_order("BTC/USD", "short", 0.5, client_order_id="hct0123456789abcde")
    assert client.client.calls == []
