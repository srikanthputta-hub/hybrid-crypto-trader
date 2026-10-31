"""Issue #99, review round 2 -- Risk Officer re-verification of 624c7954 (N1, N2, N3, minor a/b).

Fakes only (tests._live_fakes): no network, no keys, no real order. The RO's repros
(test_n1/test_n2/test_n3, which reproduced the defects) are kept here INVERTED -- they assert the
fixed behaviour -- plus her G3/G4 checks that kill mutants R06/R19.
"""
from __future__ import annotations

import csv
import json
from types import SimpleNamespace

import ccxt
import pytest

import src.bot as bot
import src.broker_live as broker_live
from src.bot import _execute_monitor_action, _reconcile_live_orders
from src.broker_live import (
    HALT_MARKER_NAME, ORDER_STATE_NAME, LiveBrokerHalted, LiveOrderNotExecuted, LiveOrderUnreconciled,
)
from src.monitor import MonitorAction
from tests._live_fakes import PLAN, FaultyExchange, logs_dir, start

BTC = "BTC/USDT"
ETH = "ETH/USDT"


@pytest.fixture(autouse=True)
def _no_grace(monkeypatch):
    monkeypatch.setattr(broker_live, "RECONCILE_NOT_FOUND_GRACE_SECONDS", 0)


def _risk(tmp_path):
    from src.risk_engine import RiskEngine
    return RiskEngine(starting_cash_usd=10_000, max_position_pct=0.1, max_total_exposure_pct=0.4,
                      max_loss_per_trade_pct=0.01, max_daily_loss_pct=0.05, max_volatility_pct=0.08,
                      min_risk_reward=1.5, cooldown_minutes=60, kill_switch_path=tmp_path / "KILL_SWITCH",
                      symbol_whitelist=[BTC, ETH])


def _act(action, broker, store, tmp_path):
    _execute_monitor_action(action, store.get(action.symbol), broker, store, _risk(tmp_path), {})


FILL = MonitorAction(symbol=BTC, action="FILL", price=50_000.0, reason="z")
STOP = MonitorAction(symbol=BTC, action="STOP_LOSS", price=45_000.0, reason="s")


def _state(tmp_path):
    p = logs_dir(tmp_path) / ORDER_STATE_NAME
    return json.loads(p.read_text()) if p.exists() else {"inflight": {}, "unverified": {}}


def _rows(tmp_path):
    p = logs_dir(tmp_path) / "trades.csv"
    if not p.exists():
        return []
    with open(p, newline="") as f:
        return list(csv.DictReader(f))


def _ack_only_unverified_buy(tmp_path, price=49_123.45):
    """Kraken-like: create answers with the txid only, the verification lookup fails -> the buy is
    booked UNVERIFIED at the requested (unrounded) amount. Returns (broker, portfolio, store,
    exchange, coid, booked, ack) with the plan OPEN and the inflight record settled."""
    exchange = FaultyExchange()
    broker, portfolio, store = start(tmp_path, exchange)
    broker._sleep = lambda seconds: None
    store.set_pending(BTC, dict(PLAN))
    ack = {}

    def respond(order):
        ack.update(order)
        return {"id": order["id"], "clientOrderId": order["clientOrderId"], "status": None,
                "filled": None, "remaining": None, "amount": None, "average": None, "price": None}

    exchange.respond = respond
    exchange.find_error = ccxt.NetworkError("lookup failed once")
    _act(MonitorAction(symbol=BTC, action="FILL", price=price, reason="z"), broker, store, tmp_path)
    coid = exchange.client_ids[-1]
    state = _state(tmp_path)
    assert coid in state["unverified"] and state["inflight"] == {}
    assert store.get(BTC).status == "OPEN" and broker.halted
    exchange.find_error = None
    exchange.respond = None
    return broker, portfolio, store, exchange, coid, state["unverified"][coid]["booked_base_amount"], ack


# ----------------------------------------------------------------- N1: precision-aware re-check
def test_n1_precision_truncated_unverified_fill_is_a_match_and_clears(tmp_path):
    """RO repro test_n1, inverted: the exchange filled the 8-dp truncation of the booked amount.
    That is a match -> the entry clears (no permanent false mismatch); deleting the marker resumes."""
    broker, portfolio, store, exchange, coid, booked, ack = _ack_only_unverified_buy(tmp_path)
    truncated = int(booked * 1e8) / 1e8
    assert truncated != booked
    exchange.find_override[coid] = {**ack, "amount": truncated, "filled": truncated, "remaining": 0.0}
    titles = []
    for _ in range(3):
        _reconcile_live_orders(broker, store)
        titles += [a["title"] for a in broker.pop_alerts() if a["kind"] == "unverified"]
    assert coid not in _state(tmp_path)["unverified"]
    assert titles and all("verify_mismatch" not in t for t in titles) and "verified" in titles[0]
    assert len(_rows(tmp_path)) == 1  # a match writes nothing
    (logs_dir(tmp_path) / HALT_MARKER_NAME).unlink()
    b2, p2, s2 = start(tmp_path, exchange)
    _reconcile_live_orders(b2, s2)
    assert not b2.halted
    b2.buy(ETH, 50.0, 2_000.0)  # resumed


def test_n1_precision_from_market_info_is_used(tmp_path):
    """With market precision available, the comparison is against `_sent_reference` (the amount
    ccxt really sent): a 2-dp lot truncation is a match even though it is > the 1e-4 fallback."""
    broker, portfolio, store, exchange, coid, booked, ack = _ack_only_unverified_buy(tmp_path, price=48_000.0)
    exchange.precision = lambda symbol, amount: int(amount * 1e4) / 1e4  # 0.00208333 -> 0.0020
    sent = int(booked * 1e4) / 1e4
    assert abs(booked - sent) > booked * broker_live.UNVERIFIED_ROUNDING_TOLERANCE
    exchange.find_override[coid] = {**ack, "amount": sent, "filled": sent, "remaining": 0.0}
    _reconcile_live_orders(broker, store)
    assert coid not in _state(tmp_path)["unverified"]
    assert len(_rows(tmp_path)) == 1


def test_n1_real_difference_on_a_buy_is_corrected_csv_first_then_store_then_cleared(tmp_path):
    broker, portfolio, store, exchange, coid, booked, ack = _ack_only_unverified_buy(tmp_path, price=50_000.0)
    actual = round(booked * 0.6, 8)
    exchange.find_override[coid] = {**ack, "status": "canceled", "amount": booked, "filled": actual,
                                    "remaining": booked - actual, "average": 50_000.0}
    _reconcile_live_orders(broker, store)
    rows = _rows(tmp_path)
    corr = [r for r in rows if r["order_id"] == f"corr:{coid}"]
    assert len(corr) == 1 and corr[0]["side"] == "adjust"
    assert float(corr[0]["base_amount"]) == pytest.approx(actual - booked)
    assert portfolio.positions[BTC].amount == pytest.approx(actual)
    assert portfolio.cash == pytest.approx(10_000.0 - actual * 50_000.0)
    assert store.get(BTC).fill_amount == pytest.approx(actual) and store.get(BTC).status == "OPEN"
    assert coid not in _state(tmp_path)["unverified"]
    alerts = [a for a in broker.pop_alerts() if a["kind"] == "unverified"]
    assert any("corrected" in a["title"] for a in alerts)
    # idempotent: more cycles + a restart change nothing
    for _ in range(2):
        _reconcile_live_orders(broker, store)
    b2, p2, s2 = start(tmp_path, exchange)
    _reconcile_live_orders(b2, s2)
    assert [r["order_id"] for r in _rows(tmp_path)].count(f"corr:{coid}") == 1
    assert p2.cash == pytest.approx(10_000.0 - actual * 50_000.0)
    assert p2.positions[BTC].amount == pytest.approx(actual)
    assert b2.halted  # the marker still waits for the human


def test_n1_correction_interrupted_before_the_store_is_finished_next_cycle_exactly_once(tmp_path):
    broker, portfolio, store, exchange, coid, booked, ack = _ack_only_unverified_buy(tmp_path, price=50_000.0)
    actual = round(booked * 0.5, 8)
    exchange.find_override[coid] = {**ack, "status": "canceled", "amount": booked, "filled": actual,
                                    "remaining": booked - actual, "average": 50_000.0}
    real = store.update_open_amount

    def boom(*a, **k):
        raise OSError("disk full")

    store.update_open_amount = boom
    _reconcile_live_orders(broker, store)
    assert coid in _state(tmp_path)["unverified"]          # not cleared: the store did not move
    assert _state(tmp_path)["unverified"][coid]["corrected_to"] == pytest.approx(actual)
    store.update_open_amount = real
    # restart: cash from trades.csv (with the correction), position from the stale store
    b2, p2, s2 = start(tmp_path, exchange)
    assert p2.positions[BTC].amount == pytest.approx(booked)
    _reconcile_live_orders(b2, s2)
    assert coid not in _state(tmp_path)["unverified"]
    assert [r["order_id"] for r in _rows(tmp_path)].count(f"corr:{coid}") == 1
    assert p2.cash == pytest.approx(10_000.0 - actual * 50_000.0)   # not debited twice
    assert p2.positions[BTC].amount == pytest.approx(actual) and s2.get(BTC).fill_amount == pytest.approx(actual)


def test_n1_uncorrectable_mismatch_alert_names_the_exact_entry(tmp_path):
    """A sell / a changed position / nothing filled is never corrected automatically: the alert
    (with reminders) says exactly which entry of which file to resolve."""
    broker, portfolio, store, exchange, coid, booked, ack = _ack_only_unverified_buy(tmp_path, price=50_000.0)
    exchange.find_override[coid] = {**ack, "status": "canceled", "amount": booked, "filled": 0.0,
                                    "remaining": booked, "average": None}
    broker.pop_alerts()
    _reconcile_live_orders(broker, store)
    alerts = [a for a in broker.pop_alerts() if a["kind"] == "unverified"]
    assert alerts and "verify_mismatch" in alerts[0]["title"]
    check = alerts[0]["operator_check"]
    assert f'"unverified" -> "{coid}"' in check and ORDER_STATE_NAME in check and HALT_MARKER_NAME in check
    assert coid in _state(tmp_path)["unverified"] and len(_rows(tmp_path)) == 1
    assert portfolio.positions[BTC].amount == pytest.approx(booked)  # untouched


# ----------------------------------------------------------------- N3: zero-fill responses are confirmed
def _lying_zero_fill(exchange):
    exchange.respond = lambda o: {**o, "status": "canceled", "filled": 0.0, "remaining": o["amount"]}


def test_n3_false_zero_fill_response_is_contradicted_by_the_lookup_booked_once_and_halted(tmp_path):
    """RO repro test_n3, inverted: the response says canceled/0 but the order really filled."""
    exchange = FaultyExchange()
    broker, portfolio, store = start(tmp_path, exchange)
    store.set_pending(BTC, dict(PLAN))
    _lying_zero_fill(exchange)
    _act(FILL, broker, store, tmp_path)
    assert broker.halted and store.get(BTC).status == "OPEN"
    assert portfolio.positions[BTC].amount == pytest.approx(0.002)
    exchange.respond = None
    _reconcile_live_orders(broker, store)
    with pytest.raises(LiveBrokerHalted):
        broker.buy(BTC, 100.0, 50_000.0)
    assert [side for _s, side, _a in exchange.orders].count("buy") == 1


def test_n3_unconfirmable_zero_fill_keeps_the_record_halts_and_never_buys_again(tmp_path):
    exchange = FaultyExchange()
    broker, portfolio, store = start(tmp_path, exchange)
    broker._sleep = lambda seconds: None
    store.set_pending(BTC, dict(PLAN))
    _lying_zero_fill(exchange)
    exchange.find_error = ccxt.NetworkError("down")
    with pytest.raises(LiveOrderUnreconciled):
        _act(FILL, broker, store, tmp_path)
    coid = exchange.client_ids[-1]
    assert broker.halted and coid in _state(tmp_path)["inflight"]
    assert BTC not in portfolio.positions and store.get(BTC).status == "PENDING"
    exchange.respond = None
    exchange.find_error = None
    _reconcile_live_orders(broker, store)  # the real fill is found and booked
    assert store.get(BTC).status == "OPEN" and portfolio.positions[BTC].amount == pytest.approx(0.002)
    assert _state(tmp_path)["inflight"] == {}
    assert [side for _s, side, _a in exchange.orders].count("buy") == 1


def test_n3_true_zero_fill_is_confirmed_and_retired_without_a_halt(tmp_path):
    exchange = FaultyExchange()
    broker, portfolio, store = start(tmp_path, exchange)
    store.set_pending(BTC, dict(PLAN))
    exchange.fill_ratio, exchange.status = 0.0, "canceled"
    with pytest.raises(LiveOrderNotExecuted):
        broker.buy(BTC, 100.0, 50_000.0, order_ref=f"{BTC}|x")
    assert not broker.halted and _state(tmp_path)["inflight"] == {}
    assert exchange.lookups  # confirmed with the exchange, not taken on the response's word


def test_n3_unconfirmable_zero_fill_sell_keeps_position_and_record(tmp_path):
    exchange = FaultyExchange()
    broker, portfolio, store = start(tmp_path, exchange)
    store.set_pending(BTC, dict(PLAN))
    _act(FILL, broker, store, tmp_path)
    _lying_zero_fill(exchange)
    exchange.find_error = ccxt.NetworkError("down")
    broker._sleep = lambda seconds: None
    with pytest.raises(LiveOrderUnreconciled):
        _act(STOP, broker, store, tmp_path)
    assert broker.halted and store.get(BTC).status == "OPEN" and portfolio.positions[BTC].amount == pytest.approx(0.002)
    with pytest.raises(LiveBrokerHalted):
        _act(STOP, broker, store, tmp_path)  # no second sell while the first is unresolved
    assert [side for _s, side, _a in exchange.orders].count("sell") == 1


# ----------------------------------------------------------------- N2: alert delivery stays bounded
def test_n2_telegram_outage_alert_attempts_and_backlog_stay_bounded_over_a_day(tmp_path, monkeypatch):
    """RO repro test_n2, inverted, with the round-2 wiring (one _send_broker_alerts per cycle)."""
    attempts = {"n": 0}

    def down(token, chat, text, timeout=10):
        attempts["n"] += 1
        return False

    monkeypatch.setattr(bot.notifier, "send_telegram_message", down)
    exchange = FaultyExchange()
    broker, portfolio, store = start(tmp_path, exchange)
    clock = {"t": 1_000_000.0}
    broker._clock = lambda: clock["t"]
    store.set_pending(BTC, dict(PLAN))
    exchange.error_after_book = ccxt.RequestTimeout("timeout")
    exchange.find_error = ccxt.NetworkError("down")
    with pytest.raises(LiveOrderUnreconciled):
        _act(FILL, broker, store, tmp_path)
    cfg = SimpleNamespace(telegram_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ", telegram_chat_id="1",
                          notifier_label=None)
    per_cycle, queue = [], []
    for _cycle in range(288):  # 24 h of 5-minute cycles
        clock["t"] += 300
        before = attempts["n"]
        _reconcile_live_orders(broker, store)
        bot._send_broker_alerts(cfg, broker)  # run_cycle's `finally`: once per cycle
        per_cycle.append(attempts["n"] - before)
        queue.append(len(broker._alerts))
    assert max(per_cycle) == 1                      # stops at the first failure
    assert max(queue) <= broker_live.ALERT_QUEUE_MAX
    assert queue[-1] <= queue[10]                   # deduped by key: no growth from reminders
    assert attempts["n"] == 288


# ----------------------------------------------------------------- minor (a): fail closed
class _BrokenHalted:
    TRACKS_ORDER_REFS = True

    @property
    def halted(self):
        raise RuntimeError("state unreadable")


def test_minor_a_flag_halted_fails_closed_when_the_property_raises():
    assert bot._flag(_BrokenHalted(), "halted") is True


def test_minor_a_flag_other_flags_still_fail_open_and_mocks_never_block():
    from unittest.mock import MagicMock

    class Broken:
        @property
        def TRACKS_ORDER_REFS(self):
            raise RuntimeError("x")

    assert bot._flag(Broken(), "TRACKS_ORDER_REFS") is False
    assert bot._flag(MagicMock(), "halted") is False
    assert bot._flag(object(), "halted") is False  # the paper broker has no `halted`


# ----------------------------------------------------------------- minor (b): G3 / G4 (mutants R06 / R19)
def test_g3_row_written_then_fsync_fails_cash_applied_once_in_memory(tmp_path):
    """Kills mutant R06: trades.csv row lands, then the write raises (fsync EIO). The reconcile adopts
    the row and must debit cash exactly once in memory."""
    exchange = FaultyExchange()
    broker, portfolio, store = start(tmp_path, exchange)
    store.set_pending(BTC, dict(PLAN))
    real = portfolio.log_trade
    state = {"first": True}

    def log(row):
        real(row)
        if state["first"]:
            state["first"] = False
            raise OSError("fsync EIO after the row was written")

    portfolio.log_trade = log
    with pytest.raises(OSError):
        _act(FILL, broker, store, tmp_path)
    assert BTC not in portfolio.positions and portfolio.cash == 10_000.0 and len(_rows(tmp_path)) == 1
    _reconcile_live_orders(broker, store)
    assert portfolio.positions[BTC].amount == pytest.approx(0.002)
    assert portfolio.cash == pytest.approx(9_900.0)
    assert len(_rows(tmp_path)) == 1 and store.get(BTC).status == "OPEN"


def test_g4_unverified_alone_keeps_buys_halted_after_marker_deleted(tmp_path):
    """Kills mutant R19: an unverified entry halts by itself, even with no marker and no reason."""
    exchange = FaultyExchange()
    broker, portfolio, store = start(tmp_path, exchange)
    broker._sleep = lambda seconds: None
    store.set_pending(BTC, dict(PLAN))
    exchange.respond = lambda o: {"id": o["id"], "status": None, "filled": None, "remaining": None}
    exchange.find_error = ccxt.NetworkError("down")
    _act(FILL, broker, store, tmp_path)
    assert _state(tmp_path)["unverified"]
    (logs_dir(tmp_path) / HALT_MARKER_NAME).unlink()
    b2, p2, s2 = start(tmp_path, exchange)
    assert b2.halted
    b2._halt_reason = None  # even with no in-memory reason at all, the entry alone halts
    assert b2.halted
    with pytest.raises(LiveBrokerHalted):
        b2.buy(ETH, 50.0, 2_000.0)
