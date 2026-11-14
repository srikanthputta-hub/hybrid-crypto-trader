"""Issue #99 item 2: startup/cycle reconcile of in-flight live orders + halt alerts.

One test per row of the decision table in docs/live_readiness_design.md §2 (R0-R12), each a
restart simulation from the persisted order record: the broker "crashes" (SimulatedCrash, a
BaseException, so nothing in the broker can swallow it), then a fresh store + portfolio + LiveBroker
are rebuilt from disk exactly like src.bot.build_components does, and bot._reconcile_live_orders
runs, as at the top of every run_cycle. FaultyExchange only: no network, no keys, no real order.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import ccxt
import pytest

import src.broker_live as broker_live
from src.bot import _apply_reconcile_result, _execute_monitor_action, _format_broker_alert, _reconcile_live_orders
from src.bot import _send_broker_alerts
from src.broker_live import HALT_MARKER_NAME, ORDER_STATE_NAME, LiveBrokerHalted, client_order_id
from src.exchange import OrderLookupAmbiguous
from src.monitor import MonitorAction
from tests._live_fakes import PLAN, FaultyExchange, SimulatedCrash, logs_dir, start

BTC = "BTC/USDT"


@pytest.fixture(autouse=True)
def _no_grace(monkeypatch):
    # The not-found grace period is exercised explicitly in the R10 test.
    monkeypatch.setattr(broker_live, "RECONCILE_NOT_FOUND_GRACE_SECONDS", 0)


def _risk(tmp_path):
    from src.risk_engine import RiskEngine

    return RiskEngine(
        starting_cash_usd=10_000, max_position_pct=0.1, max_total_exposure_pct=0.4, max_loss_per_trade_pct=0.01,
        max_daily_loss_pct=0.05, max_volatility_pct=0.08, min_risk_reward=1.5, cooldown_minutes=60,
        kill_switch_path=tmp_path / "KILL_SWITCH", symbol_whitelist=[BTC, "ETH/USDT"],
    )


def _fill(price=50_000.0):
    return MonitorAction(symbol=BTC, action="FILL", price=price, reason="in zone")


def _stop(price=45_000.0):
    return MonitorAction(symbol=BTC, action="STOP_LOSS", price=price, reason="stop")


def _act(action, broker, store, tmp_path):
    _execute_monitor_action(action, store.get(action.symbol), broker, store, _risk(tmp_path), {})


def _inflight(tmp_path) -> dict:
    path = logs_dir(tmp_path) / ORDER_STATE_NAME
    return json.loads(path.read_text())["inflight"] if path.exists() else {}


def _attempts(tmp_path) -> dict:
    return json.loads((logs_dir(tmp_path) / ORDER_STATE_NAME).read_text())["attempts"]


def _trade_rows(tmp_path) -> list[str]:
    path = logs_dir(tmp_path) / "trades.csv"
    return path.read_text().splitlines()[1:] if path.exists() else []


def _history(tmp_path) -> list[dict]:
    lines = (logs_dir(tmp_path) / "trade_plans_history.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def _pending(tmp_path, exchange=None):
    exchange = exchange or FaultyExchange()
    broker, portfolio, store = start(tmp_path, exchange)
    store.set_pending(BTC, dict(PLAN))
    return broker, portfolio, store, exchange


def _open(tmp_path, exchange=None):
    broker, portfolio, store, exchange = _pending(tmp_path, exchange)
    _act(_fill(), broker, store, tmp_path)
    assert store.get(BTC).status == "OPEN" and _inflight(tmp_path) == {}
    return broker, portfolio, store, exchange


def _crash_buy(tmp_path, **faults):
    """PENDING plan; the FILL's order reaches the exchange (with `faults`), then the process dies."""
    broker, portfolio, store, exchange = _pending(tmp_path)
    for key, value in faults.items():
        setattr(exchange, key, value)
    exchange.crash_after_create = True
    with pytest.raises(SimulatedCrash):
        _act(_fill(), broker, store, tmp_path)
    exchange.crash_after_create = False
    exchange.fill_ratio, exchange.status = 1.0, "closed"
    return exchange


def _crash_sell(tmp_path, **faults):
    broker, portfolio, store, exchange = _open(tmp_path)
    for key, value in faults.items():
        setattr(exchange, key, value)
    exchange.crash_after_create = True
    with pytest.raises(SimulatedCrash):
        _act(_stop(), broker, store, tmp_path)
    exchange.crash_after_create = False
    exchange.fill_ratio, exchange.status = 1.0, "closed"
    return exchange


def _restart_and_reconcile(tmp_path, exchange):
    broker, portfolio, store = start(tmp_path, exchange)
    _reconcile_live_orders(broker, store)
    return broker, portfolio, store


# --- R0 -------------------------------------------------------------------------------


def test_r0_no_record_means_no_exchange_call(tmp_path):
    broker, _portfolio, store, exchange = _open(tmp_path)
    assert broker.reconcile() == []
    _reconcile_live_orders(broker, store)
    assert exchange.lookups == [] and not broker.halted


# --- R5 / R5s: found filled -----------------------------------------------------------------


def test_r5_buy_lost_in_a_crash_is_found_booked_and_protected_without_a_second_order(tmp_path):
    exchange = _crash_buy(tmp_path)
    broker, portfolio, store = _restart_and_reconcile(tmp_path, exchange)

    record = store.get(BTC)
    assert record.status == "OPEN" and (record.fill_price, record.fill_amount) == (50_000.0, 0.002)
    assert portfolio.positions[BTC].amount == 0.002 and portfolio.cash == pytest.approx(10_000.0 - 100.0)
    assert len(_trade_rows(tmp_path)) == 1 and _inflight(tmp_path) == {}
    assert _attempts(tmp_path)[f"{BTC}|{record.created_at}|buy"] == 1
    assert len(exchange.orders) == 1
    # No auto-resume: the halt marker stays for the human; buys refused...
    assert broker.halted and (logs_dir(tmp_path) / HALT_MARKER_NAME).exists()
    with pytest.raises(LiveBrokerHalted):
        broker.buy("ETH/USDT", 50.0, 2_000.0)
    # ...but the reconciled position's stop-loss now works.
    _act(_stop(), broker, store, tmp_path)
    assert store.get(BTC) is None and BTC not in portfolio.positions
    # A third start sees a clean ledger.
    _b, portfolio3, store3 = start(tmp_path, exchange)
    assert portfolio3.positions == {} and store3.all_current() == {}
    assert portfolio3.cash == pytest.approx(10_000.0 - 100.0 + 0.002 * 50_000.0)


def test_r5s_sell_lost_in_a_crash_is_found_and_closes_the_plan(tmp_path):
    exchange = _crash_sell(tmp_path)
    broker, portfolio, store = _restart_and_reconcile(tmp_path, exchange)
    assert store.get(BTC) is None and BTC not in portfolio.positions
    assert _history(tmp_path)[-1]["close_reason"] == "stop_loss"
    assert portfolio.cash == pytest.approx(10_000.0)  # bought and sold at the fake's 50_000
    assert [side for _s, side, _a in exchange.orders] == ["buy", "sell"] and _inflight(tmp_path) == {}


# --- R4: crash after the trades.csv row, before the record says booked ------------------------


def test_r4_fill_already_in_trades_csv_is_never_booked_twice(tmp_path, monkeypatch):
    broker, portfolio, store, exchange = _pending(tmp_path)
    real = broker_live._atomic_write_json

    def crash_on_booked(path, payload):
        if path.name == ORDER_STATE_NAME and any(e["state"] == "booked" for e in payload["inflight"].values()):
            raise SimulatedCrash("killed between trades.csv and the booked record")
        return real(path, payload)

    monkeypatch.setattr(broker_live, "_atomic_write_json", crash_on_booked)
    with pytest.raises(SimulatedCrash):
        _act(_fill(), broker, store, tmp_path)
    monkeypatch.setattr(broker_live, "_atomic_write_json", real)
    assert next(iter(_inflight(tmp_path).values()))["state"] == "placed" and len(_trade_rows(tmp_path)) == 1

    broker2, portfolio2, store2 = _restart_and_reconcile(tmp_path, exchange)
    assert len(_trade_rows(tmp_path)) == 1  # not booked a second time
    assert portfolio2.cash == pytest.approx(10_000.0 - 100.0)
    assert store2.get(BTC).status == "OPEN" and portfolio2.positions[BTC].amount == 0.002
    assert _inflight(tmp_path) == {} and len(exchange.orders) == 1


# --- R1 / R1b / R1c: booked, plan store lagging ----------------------------------------------


def test_r1b_booked_buy_after_restart_re_adds_position_without_touching_cash(tmp_path):
    broker, portfolio, store, exchange = _pending(tmp_path)

    def crash(*a, **k):
        raise SimulatedCrash("killed before the store update")

    store.mark_open = crash
    with pytest.raises(SimulatedCrash):
        _act(_fill(), broker, store, tmp_path)
    broker2, portfolio2, store2 = _restart_and_reconcile(tmp_path, exchange)
    assert exchange.lookups == []  # outcome already known: no exchange call
    assert store2.get(BTC).status == "OPEN" and portfolio2.positions[BTC].amount == 0.002
    assert portfolio2.cash == pytest.approx(10_000.0 - 100.0) and len(_trade_rows(tmp_path)) == 1
    assert _inflight(tmp_path) == {} and len(exchange.orders) == 1
    assert not broker2.halted  # the outcome was never unknown


def test_r1_store_failure_in_process_is_repaired_by_the_next_cycle(tmp_path):
    broker, portfolio, store, exchange = _pending(tmp_path)
    real = store.mark_open
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk hiccup")
        return real(*a, **k)

    store.mark_open = flaky
    with pytest.raises(OSError):
        _act(_fill(), broker, store, tmp_path)
    assert store.get(BTC).status == "PENDING"
    _reconcile_live_orders(broker, store)  # next cycle
    assert store.get(BTC).status == "OPEN" and portfolio.positions[BTC].amount == 0.002
    assert _inflight(tmp_path) == {} and len(exchange.orders) == 1


def test_r1_unsettled_booked_record_blocks_its_symbol_until_the_store_catches_up(tmp_path):
    broker, portfolio, store, exchange = _pending(tmp_path)
    store.mark_open = lambda *a, **k: (_ for _ in ()).throw(OSError("disk"))
    with pytest.raises(OSError):
        _act(_fill(), broker, store, tmp_path)
    _reconcile_live_orders(broker, store)  # the store still fails -> record kept
    assert store.get(BTC).status == "PENDING"
    assert broker.halted  # review F3: a store failure for a booked order halts and alerts
    with pytest.raises(LiveBrokerHalted):
        _act(_fill(), broker, store, tmp_path)  # the monitor would FILL the PENDING plan again
    broker._halt_reason = None  # even without the halt latch, the record alone blocks the symbol
    (logs_dir(tmp_path) / HALT_MARKER_NAME).unlink()
    with pytest.raises(LiveBrokerHalted, match="awaiting reconcile"):
        _act(_fill(), broker, store, tmp_path)
    assert len(exchange.orders) == 1


def test_r1c_booked_buy_without_a_matching_plan_is_kept_halted_and_alerted(tmp_path):
    broker, portfolio, store, exchange = _pending(tmp_path)
    store.mark_open = lambda *a, **k: (_ for _ in ()).throw(SimulatedCrash("killed"))
    with pytest.raises(SimulatedCrash):
        _act(_fill(), broker, store, tmp_path)
    broker2, portfolio2, store2 = start(tmp_path, exchange)
    store2.discard_pending(BTC, reason="manual", price=1.0)  # e.g. a human edited the store
    _reconcile_live_orders(broker2, store2)
    assert len(_inflight(tmp_path)) == 1 and broker2.halted
    assert any("no PENDING/OPEN plan" in a["title"] for a in broker2.pop_alerts())
    with pytest.raises(LiveBrokerHalted):
        broker2.sell(BTC, 50_000.0)


# --- R2 / R2b: booked sell, plan store lagging -----------------------------------------------


def test_r2_booked_sell_after_restart_drops_position_without_touching_cash(tmp_path):
    broker, portfolio, store, exchange = _open(tmp_path)
    store.mark_closed = lambda *a, **k: (_ for _ in ()).throw(SimulatedCrash("killed"))
    with pytest.raises(SimulatedCrash):
        _act(_stop(), broker, store, tmp_path)
    broker2, portfolio2, store2 = start(tmp_path, exchange)
    assert BTC in portfolio2.positions  # rebuilt from the still-OPEN record
    _reconcile_live_orders(broker2, store2)
    assert BTC not in portfolio2.positions and store2.get(BTC) is None
    assert _history(tmp_path)[-1]["close_reason"] == "stop_loss"
    assert portfolio2.cash == pytest.approx(10_000.0) and len(_trade_rows(tmp_path)) == 2
    assert _inflight(tmp_path) == {} and exchange.lookups == []


def test_r2b_booked_sell_with_store_already_closed_is_just_cleared(tmp_path):
    broker, portfolio, store, exchange = _open(tmp_path)
    broker.settle = lambda symbol: (_ for _ in ()).throw(SimulatedCrash("killed before settle"))
    with pytest.raises(SimulatedCrash):
        _act(_stop(), broker, store, tmp_path)
    broker2, portfolio2, store2 = _restart_and_reconcile(tmp_path, exchange)
    assert store2.get(BTC) is None and portfolio2.positions == {} and _inflight(tmp_path) == {}
    assert portfolio2.cash == pytest.approx(10_000.0)


# --- R6 / R6s: found partially filled ------------------------------------------------------


def test_r6_partially_filled_buy_is_booked_as_filled_part(tmp_path):
    exchange = _crash_buy(tmp_path, fill_ratio=0.5, status="canceled")
    broker, portfolio, store = _restart_and_reconcile(tmp_path, exchange)
    assert store.get(BTC).status == "OPEN" and store.get(BTC).fill_amount == pytest.approx(0.001)
    assert portfolio.cash == pytest.approx(10_000.0 - 50.0) and _inflight(tmp_path) == {}


def test_r6s_partially_filled_sell_keeps_and_persists_the_remainder(tmp_path):
    exchange = _crash_sell(tmp_path, fill_ratio=0.25, status="canceled")
    broker, portfolio, store = _restart_and_reconcile(tmp_path, exchange)
    assert store.get(BTC).status == "OPEN" and store.get(BTC).fill_amount == pytest.approx(0.0015)
    assert portfolio.positions[BTC].amount == pytest.approx(0.0015)
    _b, portfolio3, store3 = start(tmp_path, exchange)  # and a restart sees the remainder
    assert portfolio3.positions[BTC].amount == pytest.approx(0.0015)


# --- R5x: found filled but not tracked --------------------------------------------------------


def test_r5x_sell_found_filled_but_position_not_tracked_is_an_anomaly(tmp_path):
    exchange = _crash_sell(tmp_path)
    broker, portfolio, store = start(tmp_path, exchange)
    del portfolio.positions[BTC]  # e.g. a human closed the plan by hand meanwhile
    results = broker.reconcile()
    assert [r.outcome for r in results] == ["anomaly"]
    assert len(_inflight(tmp_path)) == 1 and broker.halted and len(_trade_rows(tmp_path)) == 1


# --- R7 / R8 / R9 / R10 / R11 ---------------------------------------------------------------


def test_r7_order_still_open_is_not_treated_as_filled(tmp_path):
    exchange = _crash_buy(tmp_path, status="open", fill_ratio=0.0)
    broker, portfolio, store = _restart_and_reconcile(tmp_path, exchange)
    assert store.get(BTC).status == "PENDING" and BTC not in portfolio.positions
    assert len(_inflight(tmp_path)) == 1 and broker.halted and _trade_rows(tmp_path) == []
    with pytest.raises(LiveBrokerHalted):
        _act(_fill(), broker, store, tmp_path)
    assert len(exchange.orders) == 1


def test_r8_canceled_unfilled_order_is_cleared_and_its_id_consumed(tmp_path):
    exchange = _crash_buy(tmp_path, status="canceled", fill_ratio=0.0)
    first_id = exchange.client_ids[0]
    broker, portfolio, store = _restart_and_reconcile(tmp_path, exchange)
    assert _inflight(tmp_path) == {} and store.get(BTC).status == "PENDING"
    assert broker.halted  # no auto-resume
    (logs_dir(tmp_path) / HALT_MARKER_NAME).unlink()  # the human reconciled
    broker3, _p, store3 = start(tmp_path, exchange)
    _act(_fill(), broker3, store3, tmp_path)
    assert exchange.client_ids[1] == client_order_id(f"{BTC}|{store3.get(BTC).created_at}", "buy", 1) != first_id


def test_r9_never_placed_is_cleared_and_the_retry_reuses_the_same_id(tmp_path):
    broker, portfolio, store, exchange = _pending(tmp_path)

    def die_before_sending(*a):
        raise SimulatedCrash("killed before the request left")

    exchange.on_create = die_before_sending
    with pytest.raises(SimulatedCrash):
        _act(_fill(), broker, store, tmp_path)
    exchange.on_create = None
    assert exchange.orders == [] and len(_inflight(tmp_path)) == 1
    broker2, portfolio2, store2 = _restart_and_reconcile(tmp_path, exchange)
    assert _inflight(tmp_path) == {} and broker2.halted
    (logs_dir(tmp_path) / HALT_MARKER_NAME).unlink()
    broker3, _p, store3 = start(tmp_path, exchange)
    _act(_fill(), broker3, store3, tmp_path)
    assert exchange.client_ids == [client_order_id(f"{BTC}|{store3.get(BTC).created_at}", "buy", 0)]


def test_r9_record_is_kept_while_the_halt_is_not_on_disk(tmp_path, monkeypatch):
    broker, portfolio, store, exchange = _pending(tmp_path)
    exchange.on_create = lambda *a: (_ for _ in ()).throw(SimulatedCrash("killed"))
    with pytest.raises(SimulatedCrash):
        _act(_fill(), broker, store, tmp_path)
    exchange.on_create = None
    real = broker_live._atomic_write_json

    def no_halt_marker(path, payload):
        if path.name == HALT_MARKER_NAME:
            raise OSError("read-only")
        return real(path, payload)

    monkeypatch.setattr(broker_live, "_atomic_write_json", no_halt_marker)
    broker2, _p, store2 = _restart_and_reconcile(tmp_path, exchange)
    assert len(_inflight(tmp_path)) == 1  # clearing it would let the NEXT restart buy without a human
    assert broker2.halted


def test_r10_not_found_inside_the_grace_period_stays_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(broker_live, "RECONCILE_NOT_FOUND_GRACE_SECONDS", 3600)
    broker, portfolio, store, exchange = _pending(tmp_path)
    exchange.on_create = lambda *a: (_ for _ in ()).throw(SimulatedCrash("killed"))
    with pytest.raises(SimulatedCrash):
        _act(_fill(), broker, store, tmp_path)
    exchange.on_create = None
    broker2, _p, store2 = start(tmp_path, exchange)
    assert [r.outcome for r in broker2.reconcile()] == ["unknown"]
    assert len(_inflight(tmp_path)) == 1 and broker2.halted


@pytest.mark.parametrize(
    "fault",
    [
        {"find_error": ccxt.RequestTimeout("t/o")},
        {"find_error": OrderLookupAmbiguous("an order without a clientOrderId")},
        {"find_error": ccxt.OrderNotFound("x")},
        {"override": {"status": "closed", "filled": float("nan"), "id": "x1"}},
        {"override": {"status": "closed", "filled": 1.0, "id": "x1"}},  # more than requested
        {"override": {"status": "weird", "filled": 0.002, "id": "x1"}},
        {"override": {"status": "closed", "filled": 0.0, "id": "x1"}},
        {"override": ["not", "an", "order"]},
    ],
    ids=repr,
)
def test_r11_undeterminable_outcome_is_never_guessed(tmp_path, fault):
    exchange = _crash_buy(tmp_path)
    coid = exchange.client_ids[0]
    exchange.find_error = fault.get("find_error")
    if "override" in fault:
        exchange.find_override[coid] = fault["override"]
    broker, portfolio, store = _restart_and_reconcile(tmp_path, exchange)
    assert _inflight(tmp_path)[coid]["state"] == "intent"
    assert store.get(BTC).status == "PENDING" and portfolio.positions == {} and _trade_rows(tmp_path) == []
    assert broker.halted
    with pytest.raises(LiveBrokerHalted):
        _act(_fill(), broker, store, tmp_path)
    assert len(exchange.orders) == 1


def test_r11_order_that_answered_once_but_is_not_found_stays_unknown(tmp_path, monkeypatch):
    # The response came back (record `placed`, exchange id known), then the process died before booking.
    broker, portfolio, store, exchange = _pending(tmp_path)
    real = broker_live.LiveBroker._compute_buy

    def die(*a, **k):
        raise SimulatedCrash("killed while booking")

    monkeypatch.setattr(broker_live.LiveBroker, "_compute_buy", die)
    with pytest.raises(SimulatedCrash):
        _act(_fill(), broker, store, tmp_path)
    monkeypatch.setattr(broker_live.LiveBroker, "_compute_buy", real)
    coid = exchange.client_ids[0]
    assert _inflight(tmp_path)[coid]["state"] == "placed"
    exchange.find_override[coid] = None  # the exchange "forgot" it
    broker2, _p, store2 = _restart_and_reconcile(tmp_path, exchange)
    assert _inflight(tmp_path)[coid]["state"] == "placed" and broker2.halted


def test_r12_unreadable_state_file_reconciles_nothing_and_alerts(tmp_path):
    (logs_dir(tmp_path)).mkdir(parents=True)
    (logs_dir(tmp_path) / ORDER_STATE_NAME).write_text("{torn")
    exchange = FaultyExchange()
    broker, portfolio, store = start(tmp_path, exchange)
    assert broker.reconcile() == [] and exchange.lookups == []
    alerts = broker.pop_alerts()
    assert any("unreadable" in a["title"] for a in alerts)


# --- alerts ---------------------------------------------------------------------------------

TOKEN = "123456789:AAEhBOweik6ad9r_QXMENQjcrGbqCr4K-ra"
CFG = SimpleNamespace(
    telegram_bot_token=TOKEN, telegram_chat_id="42", notifier_label=None,
    exchange_api_key="KEYKEYKEY-live-0001", exchange_api_secret="SECRETSECRETSECRET", exchange_api_password=None,
)


def test_unknown_outcome_sends_one_telegram_alert_with_what_to_check(tmp_path):
    broker, portfolio, store, exchange = _pending(tmp_path)
    exchange.create_error = ccxt.RequestTimeout("timeout")
    with pytest.raises(broker_live.LiveOrderUnreconciled):
        _act(_fill(), broker, store, tmp_path)
    with patch("src.bot.notifier.send_telegram_message", return_value=True) as send:
        _send_broker_alerts(CFG, broker)
        _reconcile_live_orders(broker, store)  # next cycle: still unknown (the fake never saw it)
        _send_broker_alerts(CFG, broker)
        _reconcile_live_orders(broker, store)
        _send_broker_alerts(CFG, broker)
    texts = [c.args[2] for c in send.call_args_list]
    assert len(texts) == 2  # the halt, then the reconcile result once (not every cycle)
    halt = texts[0]
    coid = exchange.client_ids[0]
    for needle in ("LIVE BROKER ALERT", "BTC/USDT", "buy", "0.002", coid, "What the bot did", "Check on the exchange"):
        assert needle in halt, needle
    assert "RequestTimeout" in halt and "timeout" not in halt.replace("RequestTimeout", "")


def test_alert_text_is_markdown_escaped_and_secret_free():
    alert = {
        "kind": "halt", "title": f"TREND_CONTINUATION *x* [y] KEYKEYKEY-live-0001 bot{TOKEN}",
        "symbol": "SHIB_X/USD", "side": "sell", "amount": 1.5, "client_order_id": "hct0123456789abcde",
        "bot_action": "did SECRETSECRETSECRET", "operator_check": f"check {TOKEN}",
    }
    text = _format_broker_alert(CFG, alert)
    assert "TREND\\_CONTINUATION" in text and "\\*x\\*" in text and "\\[y]" in text and "SHIB\\_X/USD" in text
    for secret in ("KEYKEYKEY-live-0001", "SECRETSECRETSECRET", TOKEN, TOKEN.split(":")[1]):
        assert secret not in text


def test_alerts_without_telegram_are_logged_not_sent(tmp_path, caplog):
    broker, portfolio, store, exchange = _pending(tmp_path)
    exchange.create_error = ccxt.RequestTimeout("timeout")
    with pytest.raises(broker_live.LiveOrderUnreconciled):
        _act(_fill(), broker, store, tmp_path)
    cfg = SimpleNamespace(telegram_bot_token=None, telegram_chat_id=None)
    with patch("src.bot.notifier.send_telegram_message") as send:
        _send_broker_alerts(cfg, broker)
    send.assert_not_called()
    assert any("LIVE BROKER ALERT" in r.getMessage() for r in caplog.records)


def test_paper_broker_has_no_reconcile_or_alerts(tmp_path):
    from src.broker_paper import PaperBroker
    from src.portfolio import Portfolio

    broker = PaperBroker(Portfolio(cash=1_000.0))
    store = SimpleNamespace(get=lambda s: pytest.fail("store touched"))
    _reconcile_live_orders(broker, store)  # no-op
    with patch("src.bot.notifier.send_telegram_message") as send:
        _send_broker_alerts(CFG, broker)
    send.assert_not_called()


def test_apply_result_ignores_results_without_a_fill(tmp_path):
    result = broker_live.ReconcileResult("c", BTC, "buy", f"{BTC}|x", "unknown", "d")
    store = SimpleNamespace(get=lambda s: pytest.fail("store touched"))
    _apply_reconcile_result(SimpleNamespace(), store, result)
