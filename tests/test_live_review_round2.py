"""Issue #99, Architect re-verification of 624c7954 (round 2). Each Architect reproduction
(`arch/test_arch_repro2.py` I, J, K, L) and each new item is a regression test asserting the CORRECT
behaviour. Fakes only: no network, no keys, no real order.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import ccxt
import pytest

import src.bot as bot
import src.broker_live as broker_live
from src.bot import _execute_monitor_action, _reconcile_live_orders, _send_broker_alerts, run_cycle
from src.broker_live import HALT_MARKER_NAME, ORDER_STATE_NAME, LiveBrokerHalted
from src.monitor import MonitorAction
from src.trade_plan_store import TradePlanStore
from tests import test_bot_universe_wiring as wiring
from tests import test_live_smoke as smoke
from tests._live_fakes import PLAN, FaultyExchange, SimulatedCrash, logs_dir, start

BTC = "BTC/USDT"
CFG = SimpleNamespace(telegram_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ", telegram_chat_id="1",
                      notifier_label=None)


@pytest.fixture(autouse=True)
def _no_grace(monkeypatch):
    monkeypatch.setattr(broker_live, "RECONCILE_NOT_FOUND_GRACE_SECONDS", 0)


class _Risk:
    def record_trade(self, *a, **k):
        pass


def _act(kind, broker, store, price=None):
    price = price or {"FILL": 50_000.0, "STOP_LOSS": 45_000.0}[kind]
    _execute_monitor_action(MonitorAction(BTC, kind, price, "x"), store.get(BTC), broker, store, _Risk(), {})


def _state(tmp_path) -> dict:
    p = logs_dir(tmp_path) / ORDER_STATE_NAME
    return json.loads(p.read_text()) if p.exists() else {"inflight": {}, "attempts": {}, "unverified": {}}


def _queue(broker, n, prefix="condition"):
    for i in range(n):
        broker._queue_alert((prefix, i), kind="test", title=f"{prefix} {i}", symbol=None, side=None, amount=None,
                            client_order_id=None, bot_action="x", operator_check="y")


# --- item 1: alert retry storm --------------------------------------------------------------------


def test_item1_arch_L_undelivered_alerts_are_attempted_at_most_once_per_run_cycle(tmp_path, monkeypatch):
    """Telegram unreachable, 3 undelivered alerts, real run_cycle over many symbols and ticks: at
    most ONE send attempt per cycle (was 3 per audit event: 36 in a 10-symbol cycle), the queue
    stays bounded, and the exits still run."""
    monkeypatch.setattr(wiring.htf_filter, "_cache", {})
    monkeypatch.setattr("src.risk_engine.datetime", wiring._FakeDatetime)
    wiring._Clock.now = wiring.datetime.fromtimestamp(wiring.T0_MS / 1000, tz=wiring.timezone.utc)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    real_raw = wiring._pre_trial_hourly_raw(smoke.load_config().raw)
    symbols = list(real_raw["market"]["symbols"])
    hourly = {s: wiring._random_walk_hourly(seed=i, start_price=10.0 * (i + 1)) for i, s in enumerate(symbols)}
    daily = {s: wiring._daily(10.0 * (i + 1), uptrend=(i % 4 != 3)) for i, s in enumerate(symbols)}
    fake = smoke.LiveFakeCcxt(hourly, daily)
    cfg, parts, _events = smoke._boot(wiring._isolated_raw(real_raw, tmp_path), fake)
    broker = parts[3]
    _queue(broker, 3)
    attempts: list[float] = []

    def unreachable(token, chat, text, timeout=10):
        if "LIVE BROKER ALERT" in text:  # (signal/fill messages from _maybe_notify are not alerts)
            attempts.append(timeout)
        return False

    cycles = 0
    with patch("src.bot.notifier.send_telegram_message", unreachable):
        for bar in range(wiring.FIRST_TICK_BAR, wiring.LAST_TICK_BAR + 1):
            wiring._tick_to(fake, bar)
            run_cycle(cfg, *parts)
            cycles += 1
            assert len(broker._alerts) <= broker_live.ALERT_QUEUE_MAX
    assert len(attempts) <= cycles  # at most one (failing) attempt per cycle
    assert all(t <= bot.ALERT_SEND_BUDGET_SECONDS for t in attempts)
    assert [c[1] for c in fake.creates].count("sell") >= 2  # exits still ran


def test_item1_a_hanging_telegram_costs_one_bounded_attempt_per_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "ALERT_SEND_BUDGET_SECONDS", 0.5)
    ex = FaultyExchange()
    broker, _p, _s = start(tmp_path, ex)
    _queue(broker, 5)
    calls = []

    def hangs(token, chat, text, timeout=10):
        calls.append(timeout)
        time.sleep(min(timeout, 0.2))
        return False

    with patch("src.bot.notifier.send_telegram_message", hangs):
        started = time.monotonic()
        for _ in range(3):
            _send_broker_alerts(CFG, broker)
        elapsed = time.monotonic() - started
    assert len(calls) == 3 and elapsed < 3 * 0.5 + 0.5
    assert all(timeout <= 0.5 for timeout in calls)  # each request's timeout is the budget left
    assert len(broker._alerts) == 5  # nothing lost, nothing duplicated


def test_item1_the_time_budget_stops_slow_successful_sends(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "ALERT_SEND_BUDGET_SECONDS", 0.5)
    ex = FaultyExchange()
    broker, _p, _s = start(tmp_path, ex)
    _queue(broker, 10)
    sent = []

    def slow(token, chat, text, timeout=10):
        time.sleep(0.2)
        sent.append(text)
        return True

    with patch("src.bot.notifier.send_telegram_message", slow):
        _send_broker_alerts(CFG, broker)
    assert 1 <= len(sent) <= 3
    assert len(broker._alerts) == 10 - len(sent)  # the rest waits for the next cycle


def test_item1_queue_is_deduplicated_by_key_and_capped(tmp_path, caplog):
    ex = FaultyExchange()
    broker, _p, _s = start(tmp_path, ex)
    _queue(broker, 2)
    undelivered = broker.pop_alerts()
    broker.requeue_alerts(undelivered)
    broker.requeue_alerts(undelivered)  # the same keys again
    assert len(broker._alerts) == 2
    _queue(broker, broker_live.ALERT_QUEUE_MAX + 7, prefix="flood")
    assert len(broker._alerts) == broker_live.ALERT_QUEUE_MAX
    assert broker.alerts_dropped == 9
    assert broker._alerts[-1]["title"] == f"flood {broker_live.ALERT_QUEUE_MAX + 6}"  # newest kept
    assert any("alert queue full" in r.getMessage() for r in caplog.records)


def test_item1_first_failure_stops_the_rest_of_the_cycle_but_nothing_is_lost(tmp_path):
    ex = FaultyExchange()
    broker, _p, _s = start(tmp_path, ex)
    _queue(broker, 4)
    results = iter([True, False, True, True, True, True, True])
    calls = []

    def send(token, chat, text, timeout=10):
        calls.append(text)
        return next(results)

    with patch("src.bot.notifier.send_telegram_message", send):
        _send_broker_alerts(CFG, broker)
        assert len(calls) == 2 and len(broker._alerts) == 3
        _send_broker_alerts(CFG, broker)
    assert len(calls) == 5 and broker._alerts == []


# --- item 2: reconcile alert text states the halt state exactly once ----------------------------------


def test_item2_arch_I_reconcile_alert_states_the_real_halt_state_once(tmp_path):
    ex = FaultyExchange()
    broker, portfolio, store = start(tmp_path, ex)
    store.set_pending(BTC, dict(PLAN))
    with patch.object(TradePlanStore, "mark_open", lambda *a, **k: (_ for _ in ()).throw(SimulatedCrash())):
        with pytest.raises(SimulatedCrash):
            _act("FILL", broker, store)
    broker2, _p, s2 = start(tmp_path, ex)
    _reconcile_live_orders(broker2, s2)
    texts = [a["bot_action"] for a in broker2.pop_alerts() if a.get("kind") == "reconcile"]
    assert texts and not broker2.halted
    for text in texts:
        assert "NOT halted" in text and "halted" not in text.replace("NOT halted", "").lower()


def test_item2_halted_reconcile_alert_says_halted_once(tmp_path):
    ex = FaultyExchange()
    broker, portfolio, store = start(tmp_path, ex)
    store.set_pending(BTC, dict(PLAN))
    ex.error_after_book = ccxt.RequestTimeout("t/o")
    with pytest.raises(broker_live.LiveOrderUnreconciled):
        _act("FILL", broker, store)
    ex.error_after_book = None
    _reconcile_live_orders(broker, store)
    texts = [a["bot_action"] for a in broker.pop_alerts() if a.get("kind") == "reconcile"]
    assert texts and broker.halted
    for text in texts:
        assert text.lower().count("halted") == 1 and "NOT halted" not in text


# --- item 3: the full resume procedure works (arch J) ------------------------------------------------


def test_item3_arch_J_resume_needs_the_unverified_entry_resolved_first(tmp_path):
    ex = FaultyExchange()
    broker, portfolio, store = start(tmp_path, ex)
    broker._sleep = lambda s: None
    store.set_pending(BTC, dict(PLAN))
    ex.respond = lambda o: {"id": o["id"], "clientOrderId": o["clientOrderId"], "status": None}
    ex.find_error = ccxt.NetworkError("down")
    _act("FILL", broker, store)
    assert _state(tmp_path)["unverified"]
    # Deleting only the marker is NOT enough (documented): the restart re-halts.
    (logs_dir(tmp_path) / HALT_MARKER_NAME).unlink()
    assert start(tmp_path, ex)[0].halted
    # The documented procedure: resolve the live_order_state.json entries FIRST (here: the human
    # checked the exchange and removes the `unverified` entry), THEN delete the marker.
    state_path = logs_dir(tmp_path) / ORDER_STATE_NAME
    state = json.loads(state_path.read_text())
    state["unverified"] = {}
    state_path.write_text(json.dumps(state))
    (logs_dir(tmp_path) / HALT_MARKER_NAME).unlink()
    assert not start(tmp_path, ex)[0].halted


# --- item 4: Kraken ack + not-yet-terminal lookup (arch K) -------------------------------------------


def _ack_then_lagging(tmp_path, open_polls):
    ex = FaultyExchange()
    broker, portfolio, store = start(tmp_path, ex)
    sleeps = []
    broker._sleep = sleeps.append
    store.set_pending(BTC, dict(PLAN))
    ex.respond = lambda o: {"id": o["id"], "clientOrderId": o["clientOrderId"], "status": None}
    orig = ex.find_order
    seen = {"n": 0}

    def lagging(symbol, coid, exchange_order_id=None, since_ms=None, side=None):
        seen["n"] += 1
        o = orig(symbol, coid, exchange_order_id, since_ms, side)
        if o is not None and seen["n"] <= open_polls:
            return dict(o, status="open", filled=0.0, remaining=o["amount"])
        return o

    ex.find_order = lagging
    _act("FILL", broker, store)
    return broker, store, sleeps, seen


def test_item4_arch_K_a_briefly_open_kraken_order_is_verified_without_halting(tmp_path):
    broker, store, sleeps, seen = _ack_then_lagging(tmp_path, open_polls=2)
    assert not broker.halted and _state(tmp_path)["unverified"] == {}
    assert store.get(BTC).status == "OPEN" and store.get(BTC).fill_amount == pytest.approx(0.002)
    assert sleeps == [broker_live.VERIFY_REPOLL_SECONDS] * 2 and seen["n"] == 3


def test_item4_a_still_open_order_after_the_bounded_repoll_is_unverified_and_halts(tmp_path):
    broker, store, sleeps, seen = _ack_then_lagging(tmp_path, open_polls=99)
    assert broker.halted and _state(tmp_path)["unverified"]
    assert len(sleeps) == broker_live.VERIFY_REPOLLS and seen["n"] == broker_live.VERIFY_REPOLLS + 1
    assert broker_live.VERIFY_REPOLLS * broker_live.VERIFY_REPOLL_SECONDS <= 5  # bounded


# --- item 5: one marker deletion is enough --------------------------------------------------------------


def test_item5_store_failure_halt_needs_only_one_marker_deletion(tmp_path):
    ex = FaultyExchange()
    broker, portfolio, store = start(tmp_path, ex)
    store.set_pending(BTC, dict(PLAN))
    with patch.object(TradePlanStore, "_persist_current", lambda self, *a: (_ for _ in ()).throw(OSError("EIO"))):
        with pytest.raises(OSError):
            _act("FILL", broker, store)
    record = next(iter(_state(tmp_path)["inflight"].values()))
    assert record["halt_reason"] and record["halt_durable"] is True  # the marker was written right after
    (logs_dir(tmp_path) / HALT_MARKER_NAME).unlink()  # the human resumes: ONE deletion
    broker2, _p, s2 = start(tmp_path, ex)
    assert not broker2.halted
    _reconcile_live_orders(broker2, s2)  # the store catches up; one buy only
    assert s2.get(BTC).status == "OPEN" and len(ex.orders) == 1


def test_item5_settle_refusal_record_turns_durable_once_the_marker_is_written(tmp_path, monkeypatch):
    ex = FaultyExchange()
    broker, portfolio, store = start(tmp_path, ex)
    store.set_pending(BTC, dict(PLAN))
    real = broker_live._atomic_write_json
    failing = {"on": True}

    def flaky_marker(path, payload):
        if path.name == HALT_MARKER_NAME and failing["on"]:
            raise OSError("read-only")
        return real(path, payload)

    monkeypatch.setattr(broker_live, "_atomic_write_json", flaky_marker)
    ex.respond = lambda order: dict(order, average=float("nan"))  # a halting buy
    _act("FILL", broker, store)
    assert next(iter(_state(tmp_path)["inflight"].values()))["halt_durable"] is False
    failing["on"] = False
    broker.require_human("disk fixed: marker written now")
    assert next(iter(_state(tmp_path)["inflight"].values()))["halt_durable"] is True
    assert "pending_halt" not in _state(tmp_path)


# --- item 6: X02 (partial-sell settle without the on-disk read-back) ----------------------------------


def test_item6_x02_partial_sell_settle_reads_the_store_back_from_disk(tmp_path):
    ex = FaultyExchange()
    broker, portfolio, store = start(tmp_path, ex)
    store.set_pending(BTC, dict(PLAN))
    _act("FILL", broker, store)
    ex.fill_ratio = 0.25
    with patch.object(TradePlanStore, "_persist_current", lambda self, *a: None):  # the write is lost
        with pytest.raises(broker_live.LivePartialFill):
            _act("STOP_LOSS", broker, store)
    assert len(_state(tmp_path)["inflight"]) == 1 and broker.halted  # record kept, not settled
    broker2, p2, s2 = start(tmp_path, ex)  # disk still says the ORIGINAL amount
    _reconcile_live_orders(broker2, s2)
    assert s2.get(BTC).fill_amount == pytest.approx(0.0015) and p2.positions[BTC].amount == pytest.approx(0.0015)
    with pytest.raises(LiveBrokerHalted):
        broker2.buy("ETH/USDT", 50.0, 2_000.0)
