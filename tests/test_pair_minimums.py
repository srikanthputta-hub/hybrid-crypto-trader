"""Issue #81 (remaining part): Kraken's PER-PAIR exchange minimums on the LIVE path.

The flat risk.min_order_quote_usd floor (src/risk_engine.py) already REJECTS small buys; this
covers the exchange's own minimums (ccxt market `limits.amount.min`, base units, and
`limits.cost.min`, quote units), checked by src/broker_live.py right before a BUY:

* below amount.min AFTER amount_to_precision, or below cost.min -> LiveOrderBelowMinimum;
* limits / precision that can't be determined -> LiveMarketLimitsUnavailable (fail closed);
* either way: NO order, NO write-ahead record, NO resize, NOT halted, one alert;
* sells keep the review-F8c dust check through the same decision function, and unknown limits
  still let a protective sell go out (the exchange is the arbiter of a stop-loss).

Fakes only: no network, no keys, no real order.
"""
from __future__ import annotations

import json
import math

import ccxt
import pytest

import src.broker_live as broker_live
from src.bot import _execute_monitor_action
from src.broker_live import (
    ORDER_STATE_NAME,
    LiveExitBlocked,
    LiveMarketLimitsUnavailable,
    LiveOrderBelowMinimum,
)
from src.exchange import (
    ExchangeClient,
    MarketLimits,
    MarketLimitsUnavailable,
    order_minimum_violation,
    parse_market_limits,
)
from src.monitor import MonitorAction
from tests._live_fakes import PLAN, FaultyExchange, logs_dir, start

BTC = "BTC/USDT"
FILL = MonitorAction(symbol=BTC, action="FILL", price=50_000.0, reason="in zone")
STOP = MonitorAction(symbol=BTC, action="STOP_LOSS", price=45_000.0, reason="stop")
# PLAN buys $100 of BTC at 50_000 -> 0.002 BTC requested.
REQUESTED = 100.0 / 50_000.0


@pytest.fixture(autouse=True)
def _no_grace(monkeypatch):
    monkeypatch.setattr(broker_live, "RECONCILE_NOT_FOUND_GRACE_SECONDS", 0)


class _Risk:
    def __init__(self):
        self.recorded = []

    def record_trade(self, *a, **k):
        self.recorded.append(a)


def _act(action, broker, store, risk=None):
    _execute_monitor_action(action, store.get(action.symbol), broker, store, risk or _Risk(), {})


def _state(tmp_path) -> dict:
    p = logs_dir(tmp_path) / ORDER_STATE_NAME
    return json.loads(p.read_text()) if p.exists() else {"inflight": {}, "attempts": {}, "unverified": {}}


def _assert_refused_cleanly(tmp_path, ex, broker, portfolio, store, kind):
    """Nothing placed, nothing recorded, nothing booked, not halted, plan still PENDING, one alert."""
    assert ex.orders == []
    state = _state(tmp_path)
    assert state["inflight"] == {} and state["attempts"] == {} and state["unverified"] == {}
    assert not broker.halted
    assert portfolio.cash == 10_000.0 and BTC not in portfolio.positions
    assert store.get(BTC).status == "PENDING"
    assert not (logs_dir(tmp_path) / "trades.csv").exists()
    alerts = broker.pop_alerts()
    assert [a["kind"] for a in alerts] == ["buy_refused"]
    assert alerts[0]["symbol"] == BTC and alerts[0]["side"] == "buy" and alerts[0]["client_order_id"] is None
    assert "NOT halted" in alerts[0]["bot_action"]
    assert ("limits_unavailable" if kind == "unavailable" else "below_minimum") in alerts[0]["_key"]
    return alerts[0]


def _pending(tmp_path, ex):
    broker, portfolio, store = start(tmp_path, ex)
    store.set_pending(BTC, dict(PLAN))
    return broker, portfolio, store


# --- pure helpers (src/exchange.py) ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("market", "expected"),
    [
        ({"limits": {"amount": {"min": 770000}, "cost": {"min": 0.5}}}, (770000.0, 0.5)),
        ({"limits": {"amount": {"min": "0.0001"}, "cost": {"min": None}}}, (0.0001, None)),
        ({"limits": {"amount": {}, "cost": {"min": 0}}}, (None, 0.0)),
        ({"limits": {}}, (None, None)),
    ],
)
def test_parse_market_limits_reads_ccxt_limits(market, expected):
    limits = parse_market_limits("SHIB/USD", market)
    assert (limits.amount_min, limits.cost_min) == expected


@pytest.mark.parametrize(
    "market",
    [
        None, [], {"limits": None}, {}, {"limits": []}, {"limits": {"amount": 5}},
        {"limits": {"amount": {"min": float("nan")}}}, {"limits": {"amount": {"min": float("inf")}}},
        {"limits": {"amount": {"min": -1}}}, {"limits": {"cost": {"min": "abc"}}},
        {"limits": {"cost": {"min": True}}}, {"limits": {"cost": {"min": [1]}}},
    ],
)
def test_parse_market_limits_fails_closed_on_anything_malformed(market):
    with pytest.raises(MarketLimitsUnavailable):
        parse_market_limits("BTC/USD", market)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ((0.002, 50_000.0, 0.0001, 0.5, 0.002), None),
        ((0.0001, 50_000.0, 0.0001, 0.5, 0.0001), None),                      # exactly at the minimum
        ((0.00009, 50_000.0, 0.0001, 0.5, 0.00009), "below the exchange minimum amount 0.0001"),
        ((0.002, 100.0, None, 0.5, 0.002), "below the exchange minimum cost 0.5"),
        ((0.002, 50_000.0, None, None, 0.0), "truncates to 0 at the market's precision"),
        ((0.002, 50_000.0, 0.0, 0.0, None), None),                           # 0 = no constraint
        ((0.002, 50_000.0, float("nan"), None, None), None),                 # unusable = not applied (sells)
    ],
)
def test_order_minimum_violation(args, expected):
    why = order_minimum_violation(*args)
    assert (why is None) if expected is None else (expected in why)


class _MarketsClient:
    def __init__(self, markets=None, error=None, precision=None):
        self.markets, self.error, self.precision = markets, error, precision
        self.created = []

    def load_markets(self):
        if self.error is not None:
            raise self.error
        return self.markets

    def amount_to_precision(self, symbol, amount):
        if self.precision is None:
            return str(amount)
        return self.precision(amount)

    def create_order(self, **kw):  # pragma: no cover - must never be reached in these tests
        self.created.append(kw)
        raise AssertionError("no order may be placed")


def _client(markets=None, error=None, precision=None) -> ExchangeClient:
    ex = ExchangeClient.__new__(ExchangeClient)  # no ccxt construction, no network, no keys
    ex.client, ex.exchange_id = _MarketsClient(markets, error, precision), "kraken"
    return ex


def test_exchange_client_market_limits_fails_closed_but_sell_view_stays_unknown():
    shib = {"SHIB/USD": {"limits": {"amount": {"min": 770000.0}, "cost": {"min": 0.5}}}}
    assert _client(shib).market_limits("SHIB/USD") == MarketLimits("SHIB/USD", 770000.0, 0.5)
    with pytest.raises(MarketLimitsUnavailable):
        _client(shib).market_limits("PEPE/USD")          # not listed: a buy can't be checked
    with pytest.raises(MarketLimitsUnavailable):
        _client(None).market_limits("SHIB/USD")
    with pytest.raises(ccxt.NetworkError):
        _client(error=ccxt.NetworkError("down")).market_limits("SHIB/USD")
    # The sell-side view (min_order_amount / min_order_cost) is the same parser, but an unlisted
    # symbol is "not published" (None), exactly as before issue #81.
    assert _client(shib).min_order_amount("SHIB/USD") == 770000.0
    assert _client(shib).min_order_cost("SHIB/USD") == 0.5
    assert _client(shib).min_order_amount("PEPE/USD") is None


# --- the live BUY guard ----------------------------------------------------------------------------


def test_buy_below_amount_minimum_is_refused_never_resized(tmp_path):
    ex = FaultyExchange()
    ex.min_amount[BTC] = 0.01                          # 0.01 BTC = $500 > the $100 plan
    broker, portfolio, store = _pending(tmp_path, ex)
    with pytest.raises(LiveOrderBelowMinimum, match="below the exchange minimum amount 0.01"):
        _act(FILL, broker, store)
    alert = _assert_refused_cleanly(tmp_path, ex, broker, portfolio, store, "below")
    assert "nothing was resized" in alert["bot_action"]


def test_amount_minimum_is_checked_after_precision_truncation(tmp_path):
    # Raw 0.002 >= min 0.0015, but at 2-decimal precision ccxt would send 0.00 -> refused; at
    # 3 decimals it would send 0.002 -> placed (the raw amount: the broker never resizes).
    ex = FaultyExchange()
    ex.min_amount[BTC] = 0.0015
    ex.precision = lambda symbol, amount: math.floor(amount * 100) / 100
    broker, portfolio, store = _pending(tmp_path, ex)
    with pytest.raises(LiveOrderBelowMinimum, match="truncates to 0"):
        _act(FILL, broker, store)
    _assert_refused_cleanly(tmp_path, ex, broker, portfolio, store, "below")

    clock = {"t": broker._clock() + broker_live.ALERT_REMINDER_SECONDS}  # past the alert throttle
    broker._clock = lambda: clock["t"]
    ex.min_amount[BTC] = 0.00205
    ex.precision = lambda symbol, amount: math.floor(amount * 1e5) / 1e5   # 0.002 -> 0.002
    with pytest.raises(LiveOrderBelowMinimum, match="0.002 is below the exchange minimum amount 0.00205"):
        _act(FILL, broker, store)
    _assert_refused_cleanly(tmp_path, ex, broker, portfolio, store, "below")


def test_precision_that_truncates_below_the_minimum_refuses(tmp_path):
    # Raw request (0.002) is above the minimum, the precision-truncated one is below it.
    ex = FaultyExchange()
    ex.min_amount[BTC] = 0.0019
    ex.precision = lambda symbol, amount: math.floor(amount * 1e3 - 1e-9) / 1e3 if amount < 0.0021 else amount
    broker, portfolio, store = _pending(tmp_path, ex)
    assert ex.precision(BTC, REQUESTED) == 0.001
    with pytest.raises(LiveOrderBelowMinimum, match="0.001 is below the exchange minimum amount 0.0019"):
        _act(FILL, broker, store)
    _assert_refused_cleanly(tmp_path, ex, broker, portfolio, store, "below")


def test_ccxt_invalid_order_from_precision_is_below_minimum(tmp_path):
    ex = FaultyExchange()
    ex.precision_error = ccxt.InvalidOrder("amount of BTC/USDT must be greater than minimum amount precision")
    broker, portfolio, store = _pending(tmp_path, ex)
    with pytest.raises(LiveOrderBelowMinimum, match=f"{REQUESTED!r} truncates to 0"):
        _act(FILL, broker, store)
    _assert_refused_cleanly(tmp_path, ex, broker, portfolio, store, "below")


def test_buy_below_cost_minimum_is_refused(tmp_path):
    ex = FaultyExchange()
    ex.min_cost[BTC] = 100.01                          # 0.002 x 50_000 = 100.00
    broker, portfolio, store = _pending(tmp_path, ex)
    with pytest.raises(LiveOrderBelowMinimum, match="below the exchange minimum cost 100.01"):
        _act(FILL, broker, store)
    _assert_refused_cleanly(tmp_path, ex, broker, portfolio, store, "below")


@pytest.mark.parametrize(
    "fault",
    [
        ("limits_error", ccxt.NetworkError("load_markets down")),
        ("limits_error", MarketLimitsUnavailable("kraken lists no market 'BTC/USDT'")),
        ("precision_error", ccxt.BadSymbol("no amount precision")),
        ("precision_error", ccxt.NotSupported("no precision")),
        ("precision", lambda symbol, amount: float("nan")),
        ("precision", lambda symbol, amount: amount * 2),   # "truncation" that grows the order
    ],
)
def test_unknown_limits_or_precision_refuse_the_buy_fail_closed(tmp_path, fault):
    ex = FaultyExchange()
    setattr(ex, fault[0], fault[1])
    broker, portfolio, store = _pending(tmp_path, ex)
    with pytest.raises(LiveMarketLimitsUnavailable, match="could not be determined"):
        _act(FILL, broker, store)
    _assert_refused_cleanly(tmp_path, ex, broker, portfolio, store, "unavailable")


def test_missing_market_limits_method_refuses(tmp_path):
    ex = FaultyExchange()
    ex.market_limits = None                            # not callable -> unknown -> refuse
    broker, portfolio, store = _pending(tmp_path, ex)
    with pytest.raises(LiveMarketLimitsUnavailable):
        _act(FILL, broker, store)
    _assert_refused_cleanly(tmp_path, ex, broker, portfolio, store, "unavailable")


def test_refused_plan_fills_once_when_the_limits_recover(tmp_path):
    ex = FaultyExchange()
    ex.limits_error = ccxt.NetworkError("down")
    broker, portfolio, store = _pending(tmp_path, ex)
    for _ in range(3):
        with pytest.raises(LiveMarketLimitsUnavailable):
            _act(FILL, broker, store)
    assert ex.orders == [] and store.get(BTC).status == "PENDING"
    ex.limits_error = None
    _act(FILL, broker, store)
    assert [(s, side) for s, side, _a in ex.orders] == [(BTC, "buy")]
    assert ex.orders[0][2] == REQUESTED                # the requested amount, never resized
    assert store.get(BTC).status == "OPEN" and portfolio.positions[BTC].amount == pytest.approx(REQUESTED)
    assert _state(tmp_path)["inflight"] == {}


def test_order_at_or_above_the_minimums_is_identical_to_no_minimums(tmp_path):
    results = []
    for label, amount_min, cost_min in (("none", None, None), ("at", REQUESTED, 100.0), ("below", 0.0001, 0.5)):
        ex = FaultyExchange()
        if amount_min is not None:
            ex.min_amount[BTC], ex.min_cost[BTC] = amount_min, cost_min
        broker, portfolio, store = _pending(tmp_path / label, ex)
        _act(FILL, broker, store)
        rows = (logs_dir(tmp_path / label) / "trades.csv").read_text().splitlines()
        results.append((ex.orders, portfolio.cash, portfolio.positions[BTC].amount,
                        [r.split(",", 1)[1] for r in rows], broker.halted, broker.pop_alerts()))
    assert results[0] == results[1] == results[2]


def test_refusal_happens_before_the_write_ahead_record_and_after_the_halt_check(tmp_path):
    ex = FaultyExchange()
    ex.min_amount[BTC] = 1.0
    broker, portfolio, store = _pending(tmp_path, ex)
    broker.require_human("operator test halt")
    calls = []
    ex.market_limits = lambda symbol: calls.append(symbol) or MarketLimits(symbol, 1.0, None)
    with pytest.raises(broker_live.LiveBrokerHalted):
        _act(FILL, broker, store)
    assert calls == [] and ex.orders == []             # halted: refused before any limits lookup


def test_alert_for_a_persisting_refusal_is_throttled_and_says_why(tmp_path):
    ex = FaultyExchange()
    ex.min_amount[BTC] = 0.01
    broker, portfolio, store = _pending(tmp_path, ex)
    clock = {"t": 1_000.0}
    broker._clock = lambda: clock["t"]
    for _ in range(3):
        with pytest.raises(LiveOrderBelowMinimum):
            _act(FILL, broker, store)
    alerts = broker.pop_alerts()
    assert len(alerts) == 1 and "REFUSED" in alerts[0]["title"] and "0.01" in alerts[0]["title"]
    assert "scripts/check_pair_minimums.py" in alerts[0]["operator_check"]
    clock["t"] += broker_live.ALERT_REMINDER_SECONDS
    with pytest.raises(LiveOrderBelowMinimum):
        _act(FILL, broker, store)
    assert [a["reminder"] for a in broker.pop_alerts()] == [True]


def test_real_exchange_client_shib_like_minimum(tmp_path):
    """Through the REAL ExchangeClient: SHIB/USD amount.min 770,000 (Kraken, 2026-10-28)."""
    shib = {"SHIB/USD": {"limits": {"amount": {"min": 770000.0}, "cost": {"min": 0.5}}}}
    ex_client = _client(shib, precision=lambda amount: f"{math.floor(amount)}")
    from src.portfolio import Portfolio
    portfolio = Portfolio(cash=10_000.0, trade_log_path=tmp_path / "logs" / "trades.csv")
    broker = broker_live.LiveBroker(ex_client, portfolio, "live", True)
    # $10 at 0.0000135 = 740,740.7 SHIB -> below 770,000: refused, nothing sent.
    with pytest.raises(LiveOrderBelowMinimum, match="740740.0 is below the exchange minimum amount 770000.0"):
        broker.buy("SHIB/USD", 10.0, 0.0000135)
    # An unlisted pair: unknown limits -> refused.
    with pytest.raises(LiveMarketLimitsUnavailable):
        broker.buy("PEPE/USD", 10.0, 0.00001)
    # Markets can't be loaded at all -> refused.
    ex_client.client.error = ccxt.ExchangeNotAvailable("maintenance")
    with pytest.raises(LiveMarketLimitsUnavailable):
        broker.buy("SHIB/USD", 100.0, 0.0000135)
    assert ex_client.client.created == [] and not broker.halted
    assert [a["kind"] for a in broker.pop_alerts()] == ["buy_refused"] * 3


# --- sells: the dust check keeps its behaviour, through the same decision function -----------------


def _opened(tmp_path, ex):
    broker, portfolio, store = _pending(tmp_path, ex)
    _act(FILL, broker, store)
    assert store.get(BTC).status == "OPEN"
    broker.pop_alerts()
    return broker, portfolio, store


@pytest.mark.parametrize(
    ("attr", "value", "message"),
    [
        ("min_amount", 0.01, "0.002 is below the exchange minimum amount 0.01"),
        ("min_cost", 100.0, "0.002 x 45000.0 is below the exchange minimum cost 100.0"),
    ],
)
def test_dust_sell_below_the_same_minimums_is_blocked(tmp_path, attr, value, message):
    ex = FaultyExchange()
    broker, portfolio, store = _opened(tmp_path, ex)
    getattr(ex, attr)[BTC] = value
    n = len(ex.orders)
    with pytest.raises(LiveExitBlocked, match=message):
        _act(STOP, broker, store)
    assert len(ex.orders) == n and store.get(BTC).status == "OPEN"
    assert any("cannot exit" in a["title"] for a in broker.pop_alerts())


def test_unknown_limits_never_block_a_protective_sell(tmp_path):
    ex = FaultyExchange()
    broker, portfolio, store = _opened(tmp_path, ex)
    ex.limits_error = ccxt.NetworkError("down")        # the BUY guard's source: not used by sells
    ex.min_order_amount = lambda symbol: (_ for _ in ()).throw(ccxt.NetworkError("down"))
    ex.min_order_cost = lambda symbol: (_ for _ in ()).throw(ccxt.NetworkError("down"))
    ex.precision_error = ccxt.NetworkError("down")
    _act(STOP, broker, store)
    assert [side for _s, side, _a in ex.orders] == ["buy", "sell"]
    assert store.get(BTC) is None and BTC not in portfolio.positions


def test_sell_dust_messages_are_unchanged_from_before_issue_81():
    """The pre-#81 inline checks, verbatim, against the shared function on a grid of inputs."""
    def legacy(sent, price_hint, minimum, min_cost, truncated):
        from src.portfolio import _is_finite_number
        if _is_finite_number(truncated) and truncated == 0:
            return f"{sent!r} truncates to 0 at the market's precision"
        if _is_finite_number(minimum) and minimum > 0 and sent < minimum:
            return f"{sent!r} is below the exchange minimum amount {minimum!r}"
        if _is_finite_number(min_cost) and min_cost > 0 and sent * price_hint < min_cost:
            return f"{sent!r} x {price_hint!r} is below the exchange minimum cost {min_cost!r}"
        return None

    def shared(sent, price_hint, minimum, min_cost, truncated):
        from src.portfolio import _is_finite_number
        return order_minimum_violation(
            sent, price_hint, minimum if _is_finite_number(minimum) else None,
            min_cost if _is_finite_number(min_cost) else None, truncated if _is_finite_number(truncated) else None)

    values = [None, 0, 0.0, 1e-9, 0.001, 0.002, 0.5, 1, 10.0, 770000.0, -1.0, float("nan"), float("inf"), True]
    checked = 0
    for sent in (0.002, 1.0, 800000.0):
        for minimum in values:
            for min_cost in values:
                for truncated in (None, 0.0, sent, float("nan")):
                    assert shared(sent, 45_000.0, minimum, min_cost, truncated) == \
                        legacy(sent, 45_000.0, minimum, min_cost, truncated), (sent, minimum, min_cost, truncated)
                    checked += 1
    assert checked > 2000
