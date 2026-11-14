"""Risk Officer round-3 review of #99 (unverified-fill correction N1, zero-fill confirmation N3):
the code was correct, but four of her mutations survived the suite (D02, D03, D04, D07 in
scripts/mutation_check_live_readiness.py). Each test below pins the behaviour one of them breaks.
Fakes only: no network, no keys, no real order."""
from __future__ import annotations

import json

import ccxt
import pytest

import src.broker_live as broker_live
from src.bot import _execute_monitor_action, _reconcile_live_orders
from src.broker_live import HALT_MARKER_NAME, ORDER_STATE_NAME
from src.monitor import MonitorAction
from tests._live_fakes import PLAN, FaultyExchange, logs_dir, start

BTC = "BTC/USDT"
FILL = MonitorAction(symbol=BTC, action="FILL", price=49_123.45, reason="in zone")
STOP = MonitorAction(symbol=BTC, action="STOP_LOSS", price=45_000.0, reason="stop")


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(broker_live, "RECONCILE_NOT_FOUND_GRACE_SECONDS", 0)
    monkeypatch.setattr(broker_live, "VERIFY_REPOLL_SECONDS", 0.0)


class _Risk:
    def record_trade(self, *a, **k):
        pass


def _act(action, broker, store):
    _execute_monitor_action(action, store.get(action.symbol), broker, store, _Risk(), {})


def _state(tmp_path) -> dict:
    p = logs_dir(tmp_path) / ORDER_STATE_NAME
    return json.loads(p.read_text()) if p.exists() else {"inflight": {}, "unverified": {}}


def _adjust_rows(tmp_path) -> list[str]:
    p = logs_dir(tmp_path) / "trades.csv"
    return [r for r in p.read_text().splitlines()[1:] if ",adjust," in r] if p.exists() else []


def _ack_only(snapshot: dict):
    def respond(order):
        snapshot.update(order)
        return {"id": order["id"], "clientOrderId": order["clientOrderId"], "status": None, "filled": None,
                "remaining": None, "amount": None, "average": None, "price": None}
    return respond


def _exchange_shows(ex, coid, snapshot, booked, actual):
    """From now on the exchange's own record of `coid` is a terminal partial fill of `actual`."""
    ex.find_error = None
    ex.respond = None
    ex.find_override[coid] = {**snapshot, "status": "canceled", "amount": booked, "filled": actual,
                              "remaining": booked - actual, "average": snapshot.get("average") or 49_123.45}


def _unverified_buy_via_bot(tmp_path, ex):
    """Ack-only buy response + failed verification lookup: booked UNVERIFIED (fallback amount),
    plan OPEN, record settled (bot path)."""
    broker, portfolio, store = start(tmp_path, ex)
    store.set_pending(BTC, dict(PLAN))
    snapshot: dict = {}
    ex.respond = _ack_only(snapshot)
    ex.find_error = ccxt.NetworkError("down")
    _act(FILL, broker, store)
    coid = ex.client_ids[-1]
    state = _state(tmp_path)
    assert coid in state["unverified"] and state["inflight"] == {}
    return broker, portfolio, store, coid, snapshot, state["unverified"][coid]["booked_base_amount"]


def _alerts(broker, outcome):
    return [a for a in broker.pop_alerts() if a.get("kind") == "unverified" and f"fill {outcome}" in a["title"]]


# --- D02: the correction checks that the tracked position still equals booked/corrected ------------


def test_d02_unverified_buy_is_not_corrected_once_the_tracked_position_changed(tmp_path):
    ex = FaultyExchange()
    broker, portfolio, store, coid, snapshot, booked = _unverified_buy_via_bot(tmp_path, ex)
    actual = int(booked * 0.5 * 1e8) / 1e8
    _exchange_shows(ex, coid, snapshot, booked, actual)
    # Something other than this order moved the tracked position (e.g. a human fixed the ledger by
    # hand): it equals neither the booked nor the exchange's amount, so the bot must not guess.
    changed = booked * 0.8
    portfolio.positions[BTC].amount = changed
    cash = portfolio.cash
    broker.pop_alerts()
    for _ in range(2):
        _reconcile_live_orders(broker, store)
    assert portfolio.positions[BTC].amount == changed and portfolio.cash == cash
    assert _adjust_rows(tmp_path) == []
    assert coid in _state(tmp_path)["unverified"] and broker.halted
    mismatch = _alerts(broker, "verify_mismatch")
    assert mismatch and "no longer equals the booked amount" in mismatch[0]["title"]
    assert "not corrected automatically" in mismatch[0]["title"]


def test_d02_control_an_unchanged_position_is_corrected(tmp_path):
    ex = FaultyExchange()
    broker, portfolio, store, coid, snapshot, booked = _unverified_buy_via_bot(tmp_path, ex)
    actual = int(booked * 0.5 * 1e8) / 1e8
    _exchange_shows(ex, coid, snapshot, booked, actual)
    _reconcile_live_orders(broker, store)
    assert portfolio.positions[BTC].amount == pytest.approx(actual)
    assert len(_adjust_rows(tmp_path)) == 1 and coid not in _state(tmp_path)["unverified"]


# --- D03: an unverified SELL is never corrected automatically ---------------------------------------


def test_d03_unverified_sell_is_never_corrected(tmp_path):
    ex = FaultyExchange()
    broker, portfolio, store = start(tmp_path, ex)
    store.set_pending(BTC, dict(PLAN))
    _act(FILL, broker, store)                            # a normal, verified buy
    assert store.get(BTC).status == "OPEN"
    held = portfolio.positions[BTC].amount
    snapshot: dict = {}
    ex.respond = _ack_only(snapshot)
    ex.find_error = ccxt.NetworkError("down")
    _act(STOP, broker, store)                            # ack-only sell, lookup down: booked UNVERIFIED
    coid = ex.client_ids[-1]
    state = _state(tmp_path)
    assert coid in state["unverified"] and state["unverified"][coid]["side"] == "sell"
    assert BTC not in portfolio.positions and store.get(BTC) is None
    cash = portfolio.cash
    actual = int(held * 0.5 * 1e8) / 1e8
    _exchange_shows(ex, coid, snapshot, held, actual)
    broker.pop_alerts()
    for _ in range(2):
        _reconcile_live_orders(broker, store)
    assert portfolio.cash == cash and BTC not in portfolio.positions and store.get(BTC) is None
    assert _adjust_rows(tmp_path) == [] and coid in _state(tmp_path)["unverified"] and broker.halted
    mismatch = _alerts(broker, "verify_mismatch")
    assert mismatch and "a sell is not corrected automatically" in mismatch[0]["title"]


# --- D04: no correction while the order's own record is still in flight -----------------------------


def test_d04_correction_waits_until_the_orders_own_record_is_settled(tmp_path):
    ex = FaultyExchange()
    broker, portfolio, store = start(tmp_path, ex)
    snapshot: dict = {}
    ex.respond = _ack_only(snapshot)
    ex.find_error = ccxt.NetworkError("down")
    broker.buy(BTC, 100.0, 49_123.45, order_ref="plan-d04")   # booked UNVERIFIED; bot has NOT settled it
    coid = ex.client_ids[-1]
    state = _state(tmp_path)
    assert coid in state["unverified"] and state["inflight"][coid]["state"] == "booked"
    booked = state["unverified"][coid]["booked_base_amount"]
    actual = int(booked * 0.5 * 1e8) / 1e8
    _exchange_shows(ex, coid, snapshot, booked, actual)
    cash = portfolio.cash
    for _ in range(2):
        results = broker.reconcile()
        # The booked record is re-synced; the unverified entry is NOT corrected while it is in flight.
        assert [r.outcome for r in results] == ["booked"]
        assert portfolio.positions[BTC].amount == booked and portfolio.cash == cash
        assert _adjust_rows(tmp_path) == []
        assert "corrected_to" not in _state(tmp_path)["unverified"][coid]
    assert broker.settle_order(coid)                     # the plan store caught up
    results = broker.reconcile()
    assert [r.outcome for r in results] == ["corrected"]
    assert portfolio.positions[BTC].amount == pytest.approx(actual) and len(_adjust_rows(tmp_path)) == 1


# --- D07: a buy whose response said "nothing filled" but the exchange shows a fill HALTS -------------


def test_d07_contradicted_zero_fill_buy_is_booked_and_halts(tmp_path):
    ex = FaultyExchange({"BTC/USDT": 50_000.0, "ETH/USDT": 2_000.0})
    broker, portfolio, store = start(tmp_path, ex)
    store.set_pending(BTC, dict(PLAN))
    # The response claims canceled + nothing filled; the exchange's own record shows the full fill.
    ex.respond = lambda o: {**o, "status": "canceled", "filled": 0.0, "remaining": o["amount"]}
    _act(MonitorAction(symbol=BTC, action="FILL", price=50_000.0, reason="z"), broker, store)
    assert portfolio.positions[BTC].amount == pytest.approx(0.002) and store.get(BTC).status == "OPEN"
    assert broker.halted
    marker = logs_dir(tmp_path) / HALT_MARKER_NAME
    assert marker.exists() and "the response said nothing was filled" in marker.read_text()
    halts = [a for a in broker.pop_alerts() if a.get("kind") == "halt"]
    assert any("the response said nothing was filled" in a["title"] for a in halts)
    with pytest.raises(broker_live.LiveBrokerHalted):
        broker.buy("ETH/USDT", 100.0, 2_000.0)
    ex.respond = None
    _reconcile_live_orders(broker, store)
    assert [s for _x, s, _a in ex.orders].count("buy") == 1
    # The halt survives a restart (the human must look at it).
    broker2, _p, _s = start(tmp_path, ex)
    assert broker2.halted
