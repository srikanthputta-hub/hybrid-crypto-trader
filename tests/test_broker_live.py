"""Order/ledger hardening of src/broker_live.py (see its module docstring).

Every test uses an in-process fake exchange -- no network, no credentials, no real order.

Groups:
  1. The two-switch gate (unchanged; extended coverage, never relaxed).
  2. Pre-order input guards: bad input -> ValueError, NO order, state untouched.
  3. Post-order responses: unusable -> conservative booking + halt; missing -> old fallback.
  4. create_market_order raising: definite rejection vs. unknown outcome.
  5. Partial sells.
  6. Parity with origin/main (tests/_legacy_broker_live.py) on every VALID fill.
  7. Wiring through src.bot._execute_monitor_action with a real TradePlanStore (no
     duplicate order on retry), and a multi-tick run_cycle replay on the real config.
"""
from __future__ import annotations

import copy
import json
import math
from dataclasses import asdict
from decimal import Decimal

import ccxt
import numpy as np
import pytest

import src.broker_live as broker_live
import tests._legacy_broker_live as legacy_broker_live
from src.broker_live import (
    HALT_MARKER_NAME,
    LiveBroker,
    LiveBrokerHalted,
    LiveOrderUnreconciled,
    LivePartialFill,
    LiveTradingDisabled,
)
from src.portfolio import Portfolio, Position

NAN, INF = float("nan"), float("inf")
FIXED_TS = "2026-10-10T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    # Trade-log rows carry now_iso(); pin it in both brokers so logs compare byte-for-byte.
    monkeypatch.setattr(broker_live, "now_iso", lambda: FIXED_TS)
    monkeypatch.setattr(legacy_broker_live, "now_iso", lambda: FIXED_TS)


class FakeExchange:
    """Stands in for ExchangeClient.create_market_order. Records every call BEFORE
    answering, so `error` simulates the exchange raising after the request went out."""

    def __init__(self, response=None, error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.orders: list[tuple[str, str, float]] = []

    def create_market_order(self, symbol, side, amount):
        self.orders.append((symbol, side, amount))
        if self.error is not None:
            raise self.error
        r = self.response
        return r(symbol, side, amount) if callable(r) else copy.deepcopy(r)


def _full_fill(symbol, side, amount, price=50_000.0):
    return {"id": f"o-{side}", "status": "closed", "average": price, "price": None,
            "filled": amount, "remaining": 0.0, "amount": amount}


def _make(tmp_path, response=_full_fill, error=None, cash=10_000.0, positions=None):
    exchange = FakeExchange(response, error)
    portfolio = Portfolio(cash=cash, positions=positions or {}, trade_log_path=tmp_path / "logs" / "trades.csv")
    return LiveBroker(exchange, portfolio, "live", True), exchange, portfolio


def _state(portfolio: Portfolio):
    """NaN-safe, exact snapshot of everything a broker call may mutate."""
    log = portfolio.trade_log_path.read_bytes() if portfolio.trade_log_path.exists() else None
    return (
        repr(portfolio.cash),
        {s: (repr(p.amount), repr(p.entry_price)) for s, p in portfolio.positions.items()},
        log,
    )


def _marker(tmp_path):
    return tmp_path / "logs" / HALT_MARKER_NAME


def _btc(amount=0.1, entry=50_000.0):
    return {"BTC/USDT": Position("BTC/USDT", amount, entry)}


BAD_NUMBERS = [
    NAN, INF, -INF, 0, 0.0, -1.0, -1e-12, True, False, np.bool_(True), None, "100", Decimal("100"),
    np.array([100.0]), [100.0],
]


# --- 1. two-switch gate --------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "confirmed"),
    [("live", False), ("paper", True), ("paper", False), ("LIVE", True), ("live ", True), ("", True),
     (None, True), ("backtest", True)],
)
def test_gate_refuses_unless_both_switches(tmp_path, mode, confirmed):
    exchange = FakeExchange()
    portfolio = Portfolio(cash=1.0, trade_log_path=tmp_path / "logs" / "trades.csv")
    with pytest.raises(LiveTradingDisabled):
        LiveBroker(exchange, portfolio, mode, confirmed)
    assert exchange.orders == []
    assert not (tmp_path / "logs").exists()


def test_gate_constructs_only_with_both_switches(tmp_path):
    broker, exchange, _ = _make(tmp_path)
    assert isinstance(broker, LiveBroker) and not broker.halted and exchange.orders == []


def test_gate_is_independent_of_an_existing_halt_marker(tmp_path):
    # A halt marker never makes construction easier (or harder): the gate is the gate.
    _marker(tmp_path).parent.mkdir(parents=True)
    _marker(tmp_path).write_text("{}")
    with pytest.raises(LiveTradingDisabled):
        LiveBroker(FakeExchange(), Portfolio(cash=1.0, trade_log_path=tmp_path / "logs" / "t.csv"), "live", False)
    broker = LiveBroker(FakeExchange(), Portfolio(cash=1.0, trade_log_path=tmp_path / "logs" / "t.csv"), "live", True)
    assert broker.halted


# --- 2. pre-order guards ---------------------------------------------------------


def _assert_refused_before_order(broker, exchange, portfolio, before, tmp_path):
    assert exchange.orders == []
    assert _state(portfolio) == before
    assert not broker.halted and not _marker(tmp_path).exists()


@pytest.mark.parametrize("bad", BAD_NUMBERS)
def test_buy_refuses_bad_quote_amount_before_any_order(tmp_path, bad):
    broker, exchange, portfolio = _make(tmp_path)
    before = _state(portfolio)
    with pytest.raises(ValueError, match="quote_amount"):
        broker.buy("BTC/USDT", bad, 50_000.0)
    _assert_refused_before_order(broker, exchange, portfolio, before, tmp_path)


@pytest.mark.parametrize("bad", BAD_NUMBERS)
def test_buy_refuses_bad_price_hint_before_any_order(tmp_path, bad):
    broker, exchange, portfolio = _make(tmp_path)
    before = _state(portfolio)
    with pytest.raises(ValueError, match="price_hint"):
        broker.buy("BTC/USDT", 100.0, bad)
    _assert_refused_before_order(broker, exchange, portfolio, before, tmp_path)


@pytest.mark.parametrize(("quote", "price"), [(1e308, 1e-308), (5e-324, 1e10)])  # estimate -> inf / -> 0
def test_buy_refuses_non_finite_or_zero_base_estimate(tmp_path, quote, price):
    broker, exchange, portfolio = _make(tmp_path)
    before = _state(portfolio)
    with pytest.raises(ValueError, match="base amount"):
        broker.buy("BTC/USDT", quote, price)
    _assert_refused_before_order(broker, exchange, portfolio, before, tmp_path)


@pytest.mark.parametrize(
    ("cash", "positions"),
    [(NAN, None), (INF, None), (-INF, None),
     (10_000.0, _btc(amount=NAN)), (10_000.0, _btc(entry=NAN)), (10_000.0, _btc(entry=INF)),
     (10_000.0, _btc(amount=-1.0))],
)
def test_buy_refuses_when_ledger_already_corrupt(tmp_path, cash, positions):
    # The requested fill could not be booked afterwards -> never place the order.
    broker, exchange, portfolio = _make(tmp_path, cash=cash, positions=copy.deepcopy(positions))
    before = _state(portfolio)
    with pytest.raises(ValueError, match="non-finite"):
        broker.buy("BTC/USDT", 100.0, 50_000.0)
    _assert_refused_before_order(broker, exchange, portfolio, before, tmp_path)


def test_buy_refuses_nan_quote_amount_read_back_from_persisted_plan(tmp_path):
    # The deferred FILL path reads approved_quote_amount back from the plan JSON;
    # Python's json happily round-trips NaN.
    plan = json.loads(json.dumps({"approved_quote_amount": NAN}))
    broker, exchange, portfolio = _make(tmp_path)
    with pytest.raises(ValueError, match="quote_amount"):
        broker.buy("BTC/USDT", plan["approved_quote_amount"], 50_000.0)
    assert exchange.orders == []


def test_buy_accepts_numpy_and_int_inputs(tmp_path):
    broker, exchange, portfolio = _make(tmp_path)
    broker.buy("BTC/USDT", np.float64(100.0), 50_000)
    assert len(exchange.orders) == 1 and not broker.halted


@pytest.mark.parametrize("bad_amount", [NAN, INF, 0.0, -0.1, True, None, "0.1"])
def test_sell_refuses_bad_held_amount_before_any_order(tmp_path, bad_amount):
    broker, exchange, portfolio = _make(tmp_path, positions=_btc(amount=bad_amount))
    before = _state(portfolio)
    with pytest.raises(ValueError, match="held amount"):
        broker.sell("BTC/USDT", 50_000.0)
    _assert_refused_before_order(broker, exchange, portfolio, before, tmp_path)


@pytest.mark.parametrize("bad", BAD_NUMBERS)
def test_sell_refuses_bad_price_hint_before_any_order(tmp_path, bad):
    broker, exchange, portfolio = _make(tmp_path, positions=_btc())
    before = _state(portfolio)
    with pytest.raises(ValueError, match="price_hint"):
        broker.sell("BTC/USDT", bad)
    _assert_refused_before_order(broker, exchange, portfolio, before, tmp_path)


def test_sell_without_position_raises_keyerror_like_before(tmp_path):
    broker, exchange, _ = _make(tmp_path)
    with pytest.raises(KeyError):
        broker.sell("BTC/USDT", 50_000.0)
    assert exchange.orders == []


# --- 3. post-order responses ---------------------------------------------------------

# Buy of 100 USDT at hint 50_000 -> requested 0.002 BTC.
UNUSABLE_BUY_RESPONSES = [
    {"average": NAN}, {"average": INF}, {"average": -INF}, {"average": -5.0}, {"average": True},
    {"average": "50000"}, {"average": [1.0]},
    {"average": None, "price": NAN}, {"average": None, "price": -1.0}, {"average": 0, "price": INF},
    {"filled": NAN}, {"filled": INF}, {"filled": 0}, {"filled": 0.0}, {"filled": -0.002},
    {"filled": True}, {"filled": "0.002"}, {"filled": np.bool_(True)},
    {"filled": 0.0021},  # more than requested -> units/parse error
    {"remaining": NAN}, {"remaining": -1.0}, {"remaining": "0"},
]


def _response(**overrides):
    base = {"id": "o-1", "status": "closed", "average": 50_100.0, "price": None,
            "filled": 0.002, "remaining": 0.0, "amount": 0.002}
    base.update(overrides)
    return base


def _assert_halted_and_buys_refused(broker, exchange, tmp_path):
    assert broker.halted
    marker = _marker(tmp_path)
    assert marker.exists() and "reason" in json.loads(marker.read_text())
    n = len(exchange.orders)
    with pytest.raises(LiveBrokerHalted):
        broker.buy("ETH/USDT", 50.0, 2_000.0)
    assert len(exchange.orders) == n  # refused before touching the exchange


@pytest.mark.parametrize("overrides", UNUSABLE_BUY_RESPONSES, ids=repr)
def test_buy_unusable_response_books_requested_fill_and_halts(tmp_path, overrides):
    broker, exchange, portfolio = _make(tmp_path, response=_response(**overrides))
    fill = broker.buy("BTC/USDT", 100.0, 50_000.0)

    assert exchange.orders == [("BTC/USDT", "buy", 100.0 / 50_000.0)]
    requested = 100.0 / 50_000.0
    # Booked exactly the request (finite, > 0), never the garbage.
    assert (fill.price, fill.base_amount) == (50_000.0, requested)
    assert portfolio.cash == 10_000.0 - requested * 50_000.0
    pos = portfolio.positions["BTC/USDT"]
    assert (pos.amount, pos.entry_price) == (requested, 50_000.0)
    assert all(math.isfinite(v) for v in (portfolio.cash, pos.amount, pos.entry_price, fill.quote_amount))
    assert portfolio.trade_log_path.exists()
    # The halt record names the offending field, for the human reconciling it.
    reason = json.loads(_marker(tmp_path).read_text())["reason"]
    assert any(f"{key}=" in reason for key in overrides), reason
    _assert_halted_and_buys_refused(broker, exchange, tmp_path)


@pytest.mark.parametrize("response", [None, [], "ok", 42], ids=repr)
def test_buy_non_dict_response_books_requested_fill_and_halts(tmp_path, response):
    broker, exchange, portfolio = _make(tmp_path, response=response)
    fill = broker.buy("BTC/USDT", 100.0, 50_000.0)
    assert fill.order_id is None and fill.base_amount == 100.0 / 50_000.0
    _assert_halted_and_buys_refused(broker, exchange, tmp_path)


def test_buy_valid_fields_that_overflow_the_ledger_book_requested_fill_and_halt(tmp_path):
    # Every field individually finite/positive, but filled*average overflows to inf.
    broker, exchange, portfolio = _make(
        tmp_path, cash=1e301, response=lambda s, side, a: {"id": "x", "average": 10.0, "filled": a},
    )
    fill = broker.buy("BTC/USDT", 1e300, 1e-8)  # requested 1e308 base units
    assert fill.price == 1e-8 and math.isfinite(portfolio.cash)
    assert broker.halted


@pytest.mark.parametrize(
    ("cash", "positions", "quote", "hint", "average", "filled"),
    [
        # cost underflows to 0 (free coins) -> refused
        (10_000.0, None, 100.0, 50_000.0, 5e-324, 0.002),
        # cost finite, but (already diverged, finite) cash - cost -> -inf
        (-1.7e308, None, 1e300, 1e-8, 1.5, 1e308),
        # cost finite, but the merged entry price's numerator overflows
        (10_000.0, _btc(1e300, 1e8), 1.0, 1e-8, 1e300, 1e8),
        # merged amount would go <= 0 (existing position already corrupt-negative)
        (10_000.0, _btc(-0.001, 50_000.0), 100.0, 50_000.0, 50_000.0, 0.0005),
    ],
)
def test_buy_each_ledger_check_falls_back_to_requested_fill_and_halts(tmp_path, cash, positions, quote, hint,
                                                                      average, filled):
    broker, exchange, portfolio = _make(
        tmp_path, cash=cash, positions=copy.deepcopy(positions),
        response={"id": "x", "average": average, "filled": filled},
    )
    fill = broker.buy("BTC/USDT", quote, hint)
    assert (fill.price, fill.base_amount) == (float(hint), quote / hint)
    pos = portfolio.positions["BTC/USDT"]
    assert all(math.isfinite(v) for v in (portfolio.cash, pos.amount, pos.entry_price))
    assert pos.amount > 0 and pos.entry_price > 0
    assert broker.halted


def test_buy_merges_into_existing_position_on_unusable_response(tmp_path):
    broker, exchange, portfolio = _make(tmp_path, positions=_btc(0.1, 40_000.0), response=_response(filled=NAN))
    broker.buy("BTC/USDT", 100.0, 50_000.0)
    pos = portfolio.positions["BTC/USDT"]
    assert pos.amount == 0.1 + 0.002
    assert pos.entry_price == (40_000.0 * 0.1 + 50_000.0 * 0.002) / (0.1 + 0.002)
    assert broker.halted


@pytest.mark.parametrize(
    "overrides",
    [{"average": None, "price": None, "filled": None, "remaining": None},  # Kraken-style ack only
     {"average": 0, "price": 0}, {"average": 0.0, "price": 50_200.0}, {"average": None, "price": 50_300.0},
     {"filled": 0.0015, "remaining": 0.0005},  # partial BUY: booked as filled (unchanged behaviour)
     {"filled": 0.002 * (1 + 1e-12)}],  # float noise above the request is tolerated
    ids=repr,
)
def test_buy_missing_or_zero_price_fields_fall_back_like_before_without_halt(tmp_path, overrides):
    broker, exchange, portfolio = _make(tmp_path, response=_response(**overrides))
    broker.buy("BTC/USDT", 100.0, 50_000.0)
    assert not broker.halted and not _marker(tmp_path).exists()
    assert math.isfinite(portfolio.cash)


def test_buy_marker_survives_restart(tmp_path):
    broker, exchange, portfolio = _make(tmp_path, response=_response(average=NAN))
    broker.buy("BTC/USDT", 100.0, 50_000.0)
    reborn = LiveBroker(exchange, Portfolio(cash=1e4, trade_log_path=portfolio.trade_log_path), "live", True)
    n = len(exchange.orders)
    with pytest.raises(LiveBrokerHalted):
        reborn.buy("ETH/USDT", 50.0, 2_000.0)
    assert len(exchange.orders) == n
    # A human deleting the marker (after reconciling) re-enables buys on the next start.
    _marker(tmp_path).unlink()
    fresh = LiveBroker(FakeExchange(_full_fill), Portfolio(cash=1e4, trade_log_path=portfolio.trade_log_path), "live", True)
    fresh.buy("ETH/USDT", 50.0, 2_000.0)


def test_halt_holds_in_memory_when_marker_cannot_be_written(tmp_path):
    broker, exchange, portfolio = _make(tmp_path, response=_response(average=NAN))
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    broker._halt_marker_path = blocker / HALT_MARKER_NAME  # parent is a file -> mkdir/write fails
    broker.buy("BTC/USDT", 100.0, 50_000.0)
    assert broker.halted
    with pytest.raises(LiveBrokerHalted):
        broker.buy("ETH/USDT", 50.0, 2_000.0)
    assert len(exchange.orders) == 1


def test_halt_without_trade_log_path_is_in_memory(tmp_path):
    exchange = FakeExchange(_response(average=NAN))
    broker = LiveBroker(exchange, Portfolio(cash=1e4), "live", True)
    broker.buy("BTC/USDT", 100.0, 50_000.0)
    with pytest.raises(LiveBrokerHalted):
        broker.buy("ETH/USDT", 50.0, 2_000.0)
    assert len(exchange.orders) == 1


# Sell of a 0.1 BTC position.
UNUSABLE_SELL_RESPONSES = [
    {"average": NAN}, {"average": INF}, {"average": -5.0}, {"average": True}, {"average": "50000"},
    {"average": None, "price": NAN},
    {"filled": NAN}, {"filled": INF}, {"filled": 0}, {"filled": -0.1}, {"filled": True}, {"filled": "0.1"},
    {"filled": 0.11},  # sold more than we hold
    {"remaining": NAN}, {"remaining": -1.0},
    {"filled": None, "remaining": 0.05},  # partial with no filled amount
    {"filled": 0.1, "remaining": 0.05},  # contradictory: all filled yet some remaining
]  # overflowing proceeds/cash: test_sell_refuses_to_book_overflowing_proceeds_or_cash


def _sell_response(**overrides):
    base = {"id": "s-1", "status": "closed", "average": 55_000.0, "price": None,
            "filled": 0.1, "remaining": 0.0, "amount": 0.1}
    base.update(overrides)
    return base


def _assert_sell_kept_position_and_halted(broker, exchange, portfolio, before, tmp_path):
    assert _state(portfolio) == before  # nothing booked: position kept, no cash, no log row
    _assert_halted_and_buys_refused(broker, exchange, tmp_path)
    # Selling (closing risk) stays allowed while halted.
    exchange.response = _full_fill
    exchange.error = None
    broker.sell("BTC/USDT", 55_000.0)
    assert "BTC/USDT" not in portfolio.positions


@pytest.mark.parametrize("overrides", UNUSABLE_SELL_RESPONSES, ids=repr)
def test_sell_unusable_response_books_nothing_keeps_position_and_halts(tmp_path, overrides):
    broker, exchange, portfolio = _make(tmp_path, positions=_btc(), response=_sell_response(**overrides))
    before = _state(portfolio)
    with pytest.raises(LiveOrderUnreconciled):
        broker.sell("BTC/USDT", 55_000.0)
    assert exchange.orders == [("BTC/USDT", "sell", 0.1)]
    _assert_sell_kept_position_and_halted(broker, exchange, portfolio, before, tmp_path)


@pytest.mark.parametrize("bad_filled", [True, np.bool_(True), 1.0 + 0j, Decimal("1")], ids=repr)
def test_sell_bool_or_non_real_filled_rejected_even_when_in_range(tmp_path, bad_filled):
    # Holding 2.0, a `filled` of True (== 1) would pass a plain range check.
    broker, exchange, portfolio = _make(tmp_path, positions=_btc(amount=2.0),
                                        response=_sell_response(filled=bad_filled, amount=2.0, remaining=None))
    before = _state(portfolio)
    with pytest.raises(LiveOrderUnreconciled, match="filled="):
        broker.sell("BTC/USDT", 55_000.0)
    _assert_sell_kept_position_and_halted(broker, exchange, portfolio, before, tmp_path)


@pytest.mark.parametrize("response", [None, [], "ok"], ids=repr)
def test_sell_non_dict_response_books_nothing_and_halts(tmp_path, response):
    broker, exchange, portfolio = _make(tmp_path, positions=_btc(), response=response)
    before = _state(portfolio)
    with pytest.raises(LiveOrderUnreconciled):
        broker.sell("BTC/USDT", 55_000.0)
    _assert_sell_kept_position_and_halted(broker, exchange, portfolio, before, tmp_path)


@pytest.mark.parametrize(
    ("cash", "held", "average"),
    [(0.0, 10.0, 1.7e308),       # proceeds overflow to inf
     (1.7e308, 1.0, 1.7e308),    # proceeds finite, but finite cash -> inf
     (0.0, 0.1, 5e-324)],        # proceeds underflow to 0
)
def test_sell_refuses_to_book_overflowing_proceeds_or_cash(tmp_path, cash, held, average):
    broker, exchange, portfolio = _make(
        tmp_path, cash=cash, positions=_btc(amount=held),
        response=lambda s, side, a: {"id": "s", "average": average, "filled": a},
    )
    before = _state(portfolio)
    with pytest.raises(LiveOrderUnreconciled):
        broker.sell("BTC/USDT", 55_000.0)
    assert _state(portfolio) == before and broker.halted


def test_sell_still_closes_when_cash_already_non_finite(tmp_path):
    # Mirrors Portfolio.apply_sell: corrupt cash must not block closing risk.
    broker, exchange, portfolio = _make(tmp_path, cash=NAN, positions=_btc(), response=_sell_response())
    broker.sell("BTC/USDT", 55_000.0)
    assert "BTC/USDT" not in portfolio.positions and not broker.halted


def test_sell_full_fill_with_rounding_dust_closes_like_before(tmp_path):
    # Exchange truncated the amount to its precision; no `remaining` reported -> full close.
    broker, exchange, portfolio = _make(tmp_path, positions=_btc(0.123456789),
                                        response=_sell_response(filled=0.123456, remaining=None))
    broker.sell("BTC/USDT", 55_000.0)
    assert "BTC/USDT" not in portfolio.positions and not broker.halted


# --- 4. create_market_order raising ---------------------------------------------------

DEFINITE_REJECTIONS = [ccxt.InsufficientFunds("no funds"), ccxt.InvalidOrder("min notional"),
                       ccxt.BadSymbol("??"), ccxt.BadRequest("400"), ccxt.AuthenticationError("401"),
                       ccxt.PermissionDenied("403"), ccxt.MarketClosed("closed"), ccxt.OperationRejected("no"),
                       ccxt.NotSupported("n/a"), ccxt.AccountSuspended("susp")]
UNKNOWN_OUTCOMES = [ccxt.RequestTimeout("t/o"), ccxt.NetworkError("reset"), ccxt.ExchangeNotAvailable("503"),
                    ccxt.RateLimitExceeded("429"), ccxt.BadResponse("garbled"), ccxt.NullResponse("empty"),
                    # ccxt's catch-all for unmapped/internal exchange errors -- NOT proof of rejection
                    ccxt.ExchangeError('coinbase {"error":"internal_server_error"}'),
                    ccxt.ExchangeClosedByUser("closed"), ccxt.DuplicateOrderId("dup"),
                    RuntimeError("boom"), ConnectionResetError("rst")]


def test_rejection_classification_over_installed_ccxt_hierarchy():
    # Verified against the INSTALLED ccxt, not a hand-written list: only a specific,
    # mapped ExchangeError subclass is a definite rejection; everything else is ambiguous.
    import inspect

    from ccxt.base import errors

    ambiguous_subtrees = (ccxt.ExchangeClosedByUser, ccxt.DuplicateOrderId)
    classes = [c for _, c in inspect.getmembers(errors, inspect.isclass) if issubclass(c, ccxt.BaseError)]
    assert ccxt.InsufficientFunds in classes and ccxt.RequestTimeout in classes
    for cls in classes:
        expected = (issubclass(cls, ccxt.ExchangeError) and cls is not ccxt.ExchangeError
                    and not issubclass(cls, ambiguous_subtrees))
        assert broker_live._is_definite_rejection(cls("x")) is expected, cls.__name__


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("error", DEFINITE_REJECTIONS, ids=lambda e: type(e).__name__)
def test_definite_exchange_rejection_reraised_unchanged_nothing_booked_no_halt(tmp_path, side, error):
    broker, exchange, portfolio = _make(tmp_path, error=error, positions=_btc())
    before = _state(portfolio)
    with pytest.raises(type(error)) as info:
        broker.buy("BTC/USDT", 100.0, 50_000.0) if side == "buy" else broker.sell("BTC/USDT", 55_000.0)
    assert info.value is error
    assert len(exchange.orders) == 1 and _state(portfolio) == before
    assert not broker.halted and not _marker(tmp_path).exists()


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("error", UNKNOWN_OUTCOMES, ids=lambda e: type(e).__name__)
def test_unknown_order_outcome_halts_books_nothing_and_raises(tmp_path, side, error):
    broker, exchange, portfolio = _make(tmp_path, error=error, positions=_btc())
    before = _state(portfolio)
    with pytest.raises(LiveOrderUnreconciled) as info:
        broker.buy("BTC/USDT", 100.0, 50_000.0) if side == "buy" else broker.sell("BTC/USDT", 55_000.0)
    assert info.value.__cause__ is error
    assert _state(portfolio) == before  # sell: position still tracked
    exchange.error = None
    _assert_halted_and_buys_refused(broker, exchange, tmp_path)


def test_trade_log_failure_after_fill_halts_and_reraises(tmp_path, monkeypatch):
    broker, exchange, portfolio = _make(tmp_path)

    def broken_log(row):
        raise OSError("disk full")

    monkeypatch.setattr(portfolio, "log_trade", broken_log)
    with pytest.raises(OSError):
        broker.buy("BTC/USDT", 100.0, 50_000.0)
    assert "BTC/USDT" in portfolio.positions  # the real fill is still reflected in memory
    assert broker.halted
    with pytest.raises(LiveBrokerHalted):
        broker.buy("ETH/USDT", 50.0, 2_000.0)
    assert len(exchange.orders) == 1


# --- 5. partial sells -----------------------------------------------------------------


def test_partial_sell_books_filled_part_keeps_remainder_and_halts(tmp_path):
    broker, exchange, portfolio = _make(
        tmp_path, cash=1_000.0, positions=_btc(1.0, 50_000.0),
        response=_sell_response(average=55_000.0, filled=0.4, remaining=0.6, amount=1.0, status="canceled"),
    )
    with pytest.raises(LivePartialFill) as info:
        broker.sell("BTC/USDT", 55_000.0)
    fill = info.value.fill
    assert (fill.side, fill.price, fill.base_amount, fill.quote_amount) == ("sell", 55_000.0, 0.4, 0.4 * 55_000.0)
    assert portfolio.cash == 1_000.0 + 0.4 * 55_000.0
    pos = portfolio.positions["BTC/USDT"]
    assert (pos.amount, pos.entry_price) == (1.0 - 0.4, 50_000.0)
    rows = portfolio.trade_log_path.read_text().splitlines()
    assert len(rows) == 2 and ",sell,55000.0,0.4," in rows[1]
    assert broker.halted

    # Next exit attempt sells exactly the remainder and closes the position.
    exchange.response = _full_fill
    broker.sell("BTC/USDT", 54_000.0)
    assert exchange.orders[-1] == ("BTC/USDT", "sell", 1.0 - 0.4)
    assert "BTC/USDT" not in portfolio.positions


def test_bare_exchange_error_on_sell_exit_keeps_plan_open_and_halts(tmp_path):
    from src.bot import _execute_monitor_action

    store, risk = _store(tmp_path), _risk(tmp_path)
    broker, exchange, portfolio = _make(tmp_path, response=_response())
    store.set_pending("BTC/USDT", {"approved_quote_amount": 100.0, "stop_loss": 45_000.0, "take_profit": 60_000.0})
    _execute_monitor_action(_fill_action("BTC/USDT", 50_000.0), store.get("BTC/USDT"), broker, store, risk, {})
    exchange.error = ccxt.ExchangeError("internal_server_error")
    with pytest.raises(LiveOrderUnreconciled):
        _execute_monitor_action(_exit_action("BTC/USDT", 45_000.0), store.get("BTC/USDT"), broker, store, risk, {})
    assert store.get("BTC/USDT").status == "OPEN" and "BTC/USDT" in portfolio.positions
    assert broker.halted and [side for _s, side, _a in exchange.orders] == ["buy", "sell"]


@pytest.mark.parametrize(
    ("held", "ordered", "filled", "remaining"),
    [
        (0.1, 0.1, 0.04, None),          # filled < echoed amount, no remaining
        (0.1, 0.1, 0.04, 0.0),           # ... even if the exchange says remaining 0
        (0.1, 0.1, 0.1 * (1 - 1e-6), None),  # small but real shortfall (beyond float noise)
        (12_345_678.9, 12_345_678.0, 12_000_000.0, None),  # SHIB-like: real partial on top of dust
    ],
)
def test_sell_filled_below_echoed_amount_is_partial(tmp_path, held, ordered, filled, remaining):
    broker, exchange, portfolio = _make(
        tmp_path, cash=0.0, positions=_btc(amount=held),
        response=_sell_response(amount=ordered, filled=filled, remaining=remaining),
    )
    with pytest.raises(LivePartialFill):
        broker.sell("BTC/USDT", 55_000.0)
    assert portfolio.positions["BTC/USDT"].amount == held - filled
    assert portfolio.cash == 0.0 + filled * 55_000.0
    assert broker.halted


@pytest.mark.parametrize(
    ("held", "ordered", "filled"),
    [
        (12_345_678.9, 12_345_678.0, 12_345_678.0),   # SHIB-like integer precision: 0.9 dust
        (3.7, 3.0, 3.0),                              # coarse precision: 19% dust, still a full close
        (0.123456789, 0.123456, 0.123456),            # 6-dp precision dust
        (0.1, 0.1, 0.1 * (1 - 1e-12)),                # float noise between filled and amount
        (0.1, 0.1 * (1 + 1e-12), 0.1),                # echoed amount float-noise above held
    ],
)
def test_sell_rounding_dust_close_is_full_without_halt(tmp_path, held, ordered, filled):
    broker, exchange, portfolio = _make(tmp_path, positions=_btc(amount=held),
                                        response=_sell_response(amount=ordered, filled=filled, remaining=0.0))
    broker.sell("BTC/USDT", 55_000.0)
    assert "BTC/USDT" not in portfolio.positions and not broker.halted


@pytest.mark.parametrize("ordered", [NAN, INF, 0.0, -0.1, True, "0.1", 0.2], ids=repr)
def test_sell_garbage_echoed_amount_is_unusable(tmp_path, ordered):
    broker, exchange, portfolio = _make(tmp_path, positions=_btc(), response=_sell_response(amount=ordered))
    before = _state(portfolio)
    with pytest.raises(LiveOrderUnreconciled, match="amount="):
        broker.sell("BTC/USDT", 55_000.0)
    _assert_sell_kept_position_and_halted(broker, exchange, portfolio, before, tmp_path)


def test_sell_with_zero_remaining_is_a_full_close(tmp_path):
    broker, exchange, portfolio = _make(tmp_path, positions=_btc(), response=_sell_response(remaining=0))
    broker.sell("BTC/USDT", 55_000.0)
    assert "BTC/USDT" not in portfolio.positions and not broker.halted


# --- 6. parity with origin/main on valid fills ---------------------------------------

VALID_SCENARIOS = [
    # (name, initial positions, list of (side, args, response))
    ("full-info buy then sell", None, [
        ("buy", ("BTC/USDT", 100.0, 50_000.0), _response()),
        ("sell", ("BTC/USDT", 55_000.0), lambda s, side, a: _sell_response(filled=a, amount=a)),
    ]),
    ("ack-only responses", None, [
        ("buy", ("BTC/USDT", 250.0, 61_234.5), {"id": 7}),
        ("sell", ("BTC/USDT", 60_000.0), {"id": 8}),
    ]),
    ("price-only, zero average, int fields", None, [
        ("buy", ("ETH/USDT", 300, 2_000), {"id": "a", "average": 0, "price": 2_010, "filled": None}),
        ("buy", ("ETH/USDT", 120.0, 1_990.0), {"id": "b", "average": None, "price": None, "filled": 0.05}),
        ("sell", ("ETH/USDT", 2_100.0), {"id": "c", "average": 2_101.25}),
    ]),
    ("merge into existing + partial buy + dusty close", _btc(0.03, 45_000.0), [
        ("buy", ("BTC/USDT", 500.0, 50_000.0), _response(filled=0.0075, average=50_050.0)),
        ("buy", ("BTC/USDT", 100.0, 49_000.0), {"id": "z", "average": 49_100.0, "filled": 0.002}),
        ("sell", ("BTC/USDT", 52_000.0), lambda s, side, a: {"id": "q", "average": 51_999.0,
                                                             "filled": round(a, 6), "remaining": None}),
    ]),
    ("numpy-typed fields", None, [
        ("buy", ("SOL/USDT", 80.0, 150.0), {"id": "n", "average": np.float64(150.5), "filled": np.float64(0.5)}),
        ("sell", ("SOL/USDT", 160.0), {"id": "m", "average": np.float64(160.25), "filled": None}),
    ]),
]


@pytest.mark.parametrize(("name", "positions", "steps"), VALID_SCENARIOS, ids=[s[0] for s in VALID_SCENARIOS])
def test_valid_fills_are_byte_identical_to_main(tmp_path, name, positions, steps):
    results = {}
    for label, module in (("main", legacy_broker_live), ("hardened", broker_live)):
        exchange = FakeExchange()
        portfolio = Portfolio(cash=10_000.0, positions=copy.deepcopy(positions) or {},
                              trade_log_path=tmp_path / label / "trades.csv")
        broker = module.LiveBroker(exchange, portfolio, "live", True)
        fills = []
        for side, args, response in steps:
            exchange.response = response
            fill = getattr(broker, side)(*args)
            fills.append(asdict(fill))
        results[label] = (_state(portfolio), repr(fills), exchange.orders)
        if module is broker_live:
            assert not broker.halted and not (tmp_path / label / HALT_MARKER_NAME).exists()
    assert results["hardened"] == results["main"]


# --- 7. wiring through bot.py (unchanged) -----------------------------------------------


def _store(tmp_path):
    from src.trade_plan_store import TradePlanStore

    return TradePlanStore(tmp_path / "logs" / "current.json", tmp_path / "logs" / "history.jsonl")


def _risk(tmp_path):
    from src.risk_engine import RiskEngine

    return RiskEngine(
        starting_cash_usd=10_000, max_position_pct=0.1, max_total_exposure_pct=0.4, max_loss_per_trade_pct=0.01, max_daily_loss_pct=0.05,
        max_volatility_pct=0.08, min_risk_reward=1.5, cooldown_minutes=60, kill_switch_path=tmp_path / "KILL_SWITCH",
        symbol_whitelist=["BTC/USDT", "ETH/USDT"],
    )


def _fill_action(symbol, price):
    from src.monitor import MonitorAction

    return MonitorAction(symbol=symbol, action="FILL", price=price, reason="in zone")


def _exit_action(symbol, price):
    from src.monitor import MonitorAction

    return MonitorAction(symbol=symbol, action="STOP_LOSS", price=price, reason="stop")


@pytest.mark.parametrize(
    "error", [ccxt.RequestTimeout("t/o"), ccxt.ExchangeError("internal_server_error")], ids=lambda e: type(e).__name__
)
def test_unknown_outcome_on_deferred_fill_never_places_a_duplicate_order(tmp_path, error):
    # Pre-hardening: the exception left the plan PENDING and the next cycle's FILL
    # placed a SECOND real order. Now the retry is refused before the exchange.
    from src.bot import _execute_monitor_action

    store, risk = _store(tmp_path), _risk(tmp_path)
    broker, exchange, _ = _make(tmp_path, error=error)
    store.set_pending("BTC/USDT", {"approved_quote_amount": 100.0, "stop_loss": 45_000.0, "take_profit": 60_000.0})
    with pytest.raises(LiveOrderUnreconciled):
        _execute_monitor_action(_fill_action("BTC/USDT", 50_000.0), store.get("BTC/USDT"), broker, store, risk, {})
    exchange.error = None
    for _cycle in range(3):
        with pytest.raises(LiveBrokerHalted):
            _execute_monitor_action(_fill_action("BTC/USDT", 50_000.0), store.get("BTC/USDT"), broker, store, risk, {})
    assert len(exchange.orders) == 1
    assert store.get("BTC/USDT").status == "PENDING"


def test_unusable_buy_response_still_marks_plan_open_so_stop_loss_protects_it(tmp_path):
    from src.bot import _execute_monitor_action

    store, risk = _store(tmp_path), _risk(tmp_path)
    broker, exchange, portfolio = _make(tmp_path, response=_response(average=NAN, filled=NAN))
    store.set_pending("BTC/USDT", {"approved_quote_amount": 100.0, "stop_loss": 45_000.0, "take_profit": 60_000.0})
    event: dict = {}
    _execute_monitor_action(_fill_action("BTC/USDT", 50_000.0), store.get("BTC/USDT"), broker, store, risk, event)
    record = store.get("BTC/USDT")
    assert record.status == "OPEN" and (record.fill_price, record.fill_amount) == (50_000.0, 0.002)
    assert all(math.isfinite(v) for v in (event["execution"]["price"], event["execution"]["base_amount"]))
    # ...and the stop-loss can still close it while halted.
    exchange.response = _full_fill
    _execute_monitor_action(_exit_action("BTC/USDT", 45_000.0), record, broker, store, risk, {})
    assert store.get("BTC/USDT") is None and "BTC/USDT" not in portfolio.positions


def test_unusable_sell_response_keeps_plan_open_and_position_tracked(tmp_path):
    from src.bot import _execute_monitor_action

    store, risk = _store(tmp_path), _risk(tmp_path)
    broker, exchange, portfolio = _make(tmp_path, response=_response())
    store.set_pending("BTC/USDT", {"approved_quote_amount": 100.0, "stop_loss": 45_000.0, "take_profit": 60_000.0})
    _execute_monitor_action(_fill_action("BTC/USDT", 50_000.0), store.get("BTC/USDT"), broker, store, risk, {})
    exchange.response = _sell_response(average=NAN)
    with pytest.raises(LiveOrderUnreconciled):
        _execute_monitor_action(_exit_action("BTC/USDT", 45_000.0), store.get("BTC/USDT"), broker, store, risk, {})
    assert store.get("BTC/USDT").status == "OPEN" and "BTC/USDT" in portfolio.positions


def test_partial_sell_keeps_plan_open_then_next_exit_closes_remainder(tmp_path):
    from src.bot import _execute_monitor_action

    store, risk = _store(tmp_path), _risk(tmp_path)
    broker, exchange, portfolio = _make(tmp_path, response=_response())
    store.set_pending("BTC/USDT", {"approved_quote_amount": 100.0, "stop_loss": 45_000.0, "take_profit": 60_000.0})
    _execute_monitor_action(_fill_action("BTC/USDT", 50_000.0), store.get("BTC/USDT"), broker, store, risk, {})
    held = portfolio.positions["BTC/USDT"].amount
    exchange.response = lambda s, side, a: {"id": "p", "average": 45_000.0, "filled": a / 4, "remaining": a * 3 / 4}
    with pytest.raises(LivePartialFill):
        _execute_monitor_action(_exit_action("BTC/USDT", 45_000.0), store.get("BTC/USDT"), broker, store, risk, {})
    assert store.get("BTC/USDT").status == "OPEN"
    assert portfolio.positions["BTC/USDT"].amount == held - held / 4
    exchange.response = _full_fill
    _execute_monitor_action(_exit_action("BTC/USDT", 44_000.0), store.get("BTC/USDT"), broker, store, risk, {})
    assert store.get("BTC/USDT") is None and "BTC/USDT" not in portfolio.positions


# --- 7b. multi-tick run_cycle replay on the real config, live broker on a fake exchange ---


def _live_replay(tmp_path, label, module, poison_order: int | None = None):
    """Replays the P1b synthetic market through the real run_cycle with a LiveBroker
    (built directly -- mode stays 'paper' in config, so no env var is involved) whose
    exchange is a fake ccxt client filling every market order at the last closed bar.
    Returns (events, trades.csv bytes, orders, broker)."""
    from src.bot import run_cycle
    from tests import test_bot_universe_wiring as wiring
    from src.config import load_config

    real_raw = load_config().raw
    symbols = list(real_raw["market"]["symbols"])
    hourly = {s: wiring._random_walk_hourly(seed=i, start_price=10.0 * (i + 1)) for i, s in enumerate(symbols)}
    daily = {s: wiring._daily(10.0 * (i + 1), uptrend=(i % 4 != 3)) for i, s in enumerate(symbols)}

    orders: list[tuple] = []

    class OrderingFake(wiring.FakeCcxt):
        def create_order(self, symbol, type, side, amount):  # noqa: A002 -- ccxt's own kwarg name
            orders.append((symbol, side, amount))
            last = [r for r in self.hourly[symbol] if r[0] + 3_600_000 <= self.now_ms][-1][4]
            n = len(orders)
            if poison_order is not None and n == poison_order:
                return {"id": n, "average": float("nan"), "filled": amount}
            if n % 3 == 0:
                return {"id": n}  # ack-only exchange response -> fallback path
            return {"id": n, "average": last, "filled": amount, "remaining": 0.0, "status": "closed"}

    fake = OrderingFake(hourly, daily)
    cfg, parts, events = wiring._components(wiring._isolated_raw(real_raw, tmp_path / label), fake)
    exchange, risk_engine, portfolio, _paper, store, audit, market_data = parts
    broker = module.LiveBroker(exchange, portfolio, "live", True)
    parts = (exchange, risk_engine, portfolio, broker, store, audit, market_data)
    wiring.htf_filter._cache.clear()
    for bar in range(wiring.FIRST_TICK_BAR, wiring.LAST_TICK_BAR + 1):
        wiring._tick_to(fake, bar)
        run_cycle(cfg, *parts)
    return events, portfolio.trade_log_path.read_bytes(), orders, broker, portfolio, store


@pytest.fixture
def _replay_isolation(monkeypatch):
    from tests import test_bot_universe_wiring as wiring

    monkeypatch.setattr(wiring.htf_filter, "_cache", {})
    monkeypatch.setattr("src.risk_engine.datetime", wiring._FakeDatetime)
    wiring._Clock.now = wiring.datetime.fromtimestamp(wiring.T0_MS / 1000, tz=wiring.timezone.utc)


def test_run_cycle_replay_with_live_broker_matches_main(tmp_path, _replay_isolation):
    from tests import test_bot_universe_wiring as wiring

    main = _live_replay(tmp_path, "main", legacy_broker_live)
    wiring._Clock.now = wiring.datetime.fromtimestamp(wiring.T0_MS / 1000, tz=wiring.timezone.utc)
    hard = _live_replay(tmp_path, "hardened", broker_live)

    main_events = [wiring._strip_wall_clock(e) for e in main[0]]
    hard_events = [wiring._strip_wall_clock(e) for e in hard[0]]
    assert hard_events == main_events
    assert hard[1] == main[1]  # trades.csv byte-identical
    assert hard[2] == main[2]  # identical order stream
    assert not hard[3].halted
    # Not vacuous: real buys and sells, including ack-only responses.
    sides = [side for _s, side, _a in hard[2]]
    assert sides.count("buy") >= 3 and sides.count("sell") >= 2


def test_run_cycle_replay_poisoned_fill_halts_new_buys_but_keeps_exits(tmp_path, _replay_isolation):
    events, _log, orders, broker, portfolio, store = _live_replay(tmp_path, "poisoned", broker_live, poison_order=2)
    assert orders[1][1] == "buy", "fixture assumption: the 2nd order of the replay is a buy"
    assert broker.halted and (tmp_path / "poisoned" / HALT_MARKER_NAME).exists()
    # No buy order ever reached the exchange after the poisoned one...
    assert all(side == "sell" for _s, side, _a in orders[2:])
    # ...the refusals were surfaced through run_cycle's normal per-symbol error path...
    assert any("live broker is halted" in e.get("error", "") for e in events)
    # ...and the ledger never went non-finite.
    assert math.isfinite(portfolio.cash)
    assert all(math.isfinite(p.amount) and math.isfinite(p.entry_price) for p in portfolio.positions.values())
    # The poisoned buy itself was booked and its plan opened (so its stop-loss is live).
    poisoned_symbol = orders[1][0]
    assert any(e["symbol"] == poisoned_symbol and "execution" in e for e in events)
