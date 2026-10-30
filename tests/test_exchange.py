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

import ccxt
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


class _ListingClient:
    """Order-listing fake: `has` like ccxt, every call recorded."""

    def __init__(self, has, orders=None, open_orders=None, closed=None, canceled=None, by_id=None):
        self.has = has
        self.orders, self.open_orders, self.closed, self.canceled = orders, open_orders, closed, canceled
        self.by_id = by_id or {}
        self.calls = []

    def fetch_orders(self, symbol, since=None):
        self.calls.append(("orders", symbol, since))
        return self.orders

    def fetch_open_orders(self, symbol, since=None):
        self.calls.append(("open", symbol, since))
        return self.open_orders

    def fetch_closed_orders(self, symbol, since=None):
        self.calls.append(("closed", symbol, since))
        return self.closed

    def fetch_canceled_orders(self, symbol, since=None):
        self.calls.append(("canceled", symbol, since))
        return self.canceled

    def fetch_order(self, order_id, symbol):
        self.calls.append(("order", order_id, symbol))
        if order_id not in self.by_id:
            raise ccxt.OrderNotFound(order_id)
        return self.by_id[order_id]


def _lister(client):
    exchange = _make_client(FakeCcxtClient(num_bars=1, now_ms=0))
    exchange.client = client
    return exchange


MINE = {"id": "7", "clientOrderId": "hctmine", "side": "buy", "status": "closed", "filled": 1.0}
OTHER = {"id": "8", "clientOrderId": "hctother", "side": "buy", "status": "closed", "filled": 1.0}
NO_ID_BUY = {"id": "9", "clientOrderId": None, "side": "buy", "status": "closed", "filled": 1.0}
NO_ID_SELL = {"id": "10", "clientOrderId": None, "side": "sell", "status": "closed", "filled": 1.0}


def test_find_order_by_known_exchange_id_is_authoritative():
    client = _ListingClient({"fetchOrders": True}, by_id={"7": MINE})
    assert _lister(client).find_order("BTC/USD", "hctmine", exchange_order_id="7") == MINE
    with pytest.raises(ccxt.OrderNotFound):  # we know it was placed: not found is NOT "never placed"
        _lister(client).find_order("BTC/USD", "hctmine", exchange_order_id="404")


@pytest.mark.parametrize(
    ("orders", "expected"),
    [([OTHER, MINE], MINE), ([OTHER], None), ([], None), ([NO_ID_SELL, OTHER], None)],
)
def test_find_order_by_client_id_via_fetch_orders(orders, expected):
    client = _ListingClient({"fetchOrders": True}, orders=orders)
    assert _lister(client).find_order("BTC/USD", "hctmine", since_ms=123, side="buy") == expected
    assert client.calls == [("orders", "BTC/USD", 123)]


@pytest.mark.parametrize(
    "orders",
    [[MINE, dict(MINE, id="77")],  # two orders carry our id
     [NO_ID_BUY],                  # same side, no client id: could be ours
     "garbage", [None]],
    ids=["duplicate", "unattributable", "not-a-list", "not-a-dict"],
)
def test_find_order_refuses_to_conclude_when_ambiguous(orders):
    from src.exchange import OrderLookupAmbiguous

    client = _ListingClient({"fetchOrders": True}, orders=orders)
    with pytest.raises(OrderLookupAmbiguous):
        _lister(client).find_order("BTC/USD", "hctmine", side="buy")


def test_find_order_kraken_style_lists_open_closed_and_canceled():
    has = {"fetchOpenOrders": True, "fetchClosedOrders": True, "fetchCanceledOrders": True}
    client = _ListingClient(has, open_orders=[], closed=[OTHER], canceled=[MINE])
    assert _lister(client).find_order("BTC/USD", "hctmine", since_ms=5) == MINE
    assert [c[0] for c in client.calls] == ["open", "closed", "canceled"]
    client = _ListingClient({"fetchOpenOrders": True, "fetchClosedOrders": True}, open_orders=[], closed=[OTHER])
    assert _lister(client).find_order("BTC/USD", "hctmine") is None


def test_find_order_without_listing_support_is_ambiguous():
    from src.exchange import OrderLookupAmbiguous

    with pytest.raises(OrderLookupAmbiguous):
        _lister(_ListingClient({"fetchOpenOrders": True})).find_order("BTC/USD", "hctmine")


def test_find_order_listing_errors_propagate():
    class Failing(_ListingClient):
        def fetch_orders(self, symbol, since=None):
            raise ccxt.RequestTimeout("t/o")

    with pytest.raises(ccxt.RequestTimeout):
        _lister(Failing({"fetchOrders": True})).find_order("BTC/USD", "hctmine")


class _BalanceClient:
    def __init__(self, balance):
        self.balance = balance
        self.markets_loaded = 0

    def fetch_balance(self):
        return self.balance

    def load_markets(self):
        self.markets_loaded += 1

    def amount_to_precision(self, symbol, amount):
        return f"{int(amount * 1000) / 1000:.3f}"


@pytest.mark.parametrize(
    ("balance", "expected"),
    [({"free": {"BTC": 0.5}}, 0.5), ({"free": {"BTC": "0.25"}}, 0.25), ({"free": {"ETH": 1.0}}, None),
     ({"free": None}, None), ({}, None)],
)
def test_fetch_free_balance(balance, expected):
    exchange = _lister(_BalanceClient(balance))
    assert exchange.fetch_free_balance("BTC") == expected


def test_amount_to_precision_loads_markets_and_returns_a_float():
    client = _BalanceClient({})
    exchange = _lister(client)
    assert exchange.amount_to_precision("BTC/USD", 0.12345) == 0.123 and client.markets_loaded == 1


def test_create_market_order_still_rejects_a_bad_side_before_any_call():
    client = _make_client(FakeCcxtClient(num_bars=1, now_ms=0))
    client.client = _OrderClient()
    with pytest.raises(ValueError):
        client.create_market_order("BTC/USD", "short", 0.5, client_order_id="hct0123456789abcde")
    assert client.client.calls == []
