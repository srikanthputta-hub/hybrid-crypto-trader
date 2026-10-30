"""Issue #99 review round: every reviewer reproduction (Risk Officer `rr99/test_rr99_repros.py`
f1-f7, Architect `arch/test_arch_repro.py` A-H) converted into a regression test that asserts the
CORRECT behaviour. Each of these failed on 2168b7ae. Fakes only: no network, no keys, no order.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import ccxt
import pytest

import src.broker_live as broker_live
from src.bot import _execute_monitor_action, _reconcile_live_orders, _send_broker_alerts
from src.broker_live import HALT_MARKER_NAME, ORDER_STATE_NAME, LiveBrokerHalted, LiveOrderUnreconciled
from src.monitor import MonitorAction
from src.portfolio import Portfolio, reconstruct_cash
from src.trade_plan_store import TradePlanStore
from tests._live_fakes import PLAN, FaultyExchange, SimulatedCrash, logs_dir, start

BTC = "BTC/USDT"
REPO = Path(__file__).resolve().parent.parent
FILL = MonitorAction(symbol=BTC, action="FILL", price=50_000.0, reason="in zone")
STOP = MonitorAction(symbol=BTC, action="STOP_LOSS", price=45_000.0, reason="stop")


@pytest.fixture(autouse=True)
def _no_grace(monkeypatch):
    monkeypatch.setattr(broker_live, "RECONCILE_NOT_FOUND_GRACE_SECONDS", 0)


class _Risk:
    def record_trade(self, *a, **k):
        pass


def _act(action, broker, store):
    _execute_monitor_action(action, store.get(action.symbol), broker, store, _Risk(), {})


def _inflight(tmp_path) -> dict:
    p = logs_dir(tmp_path) / ORDER_STATE_NAME
    return json.loads(p.read_text())["inflight"] if p.exists() else {}


def _rows(tmp_path) -> list[str]:
    p = logs_dir(tmp_path) / "trades.csv"
    return p.read_text().splitlines()[1:] if p.exists() else []


def _opened(tmp_path, exchange):
    broker, portfolio, store = start(tmp_path, exchange)
    store.set_pending(BTC, dict(PLAN))
    _act(FILL, broker, store)
    assert store.get(BTC).status == "OPEN" and _inflight(tmp_path) == {}
    return broker, portfolio, store


# --- F1: trades.csv failure must never lead to a double booking ------------------------------


def test_f1_rr_trades_csv_failure_then_reconciles_books_the_order_exactly_once(tmp_path):
    exchange = FaultyExchange()
    broker, portfolio, store = start(tmp_path, exchange)
    store.set_pending(BTC, dict(PLAN))
    real_log = portfolio.log_trade
    failing = {"on": True}

    def log_trade(row):
        if failing["on"]:
            raise OSError("ENOSPC: disk full (trades.csv only)")
        return real_log(row)

    portfolio.log_trade = log_trade
    with pytest.raises(OSError):
        _act(FILL, broker, store)
    assert BTC not in portfolio.positions and portfolio.cash == 10_000.0  # memory untouched
    for _ in range(2):  # disk still failing for 2 more cycles
        _reconcile_live_orders(broker, store)
    failing["on"] = False
    _reconcile_live_orders(broker, store)
    assert len(exchange.orders) == 1
    assert store.get(BTC).status == "OPEN" and store.get(BTC).fill_amount == pytest.approx(0.002)
    assert portfolio.positions[BTC].amount == pytest.approx(0.002)       # not 0.008
    assert portfolio.cash == pytest.approx(9_900.0)                       # not 9_600
    assert len(_rows(tmp_path)) == 1
    # and the stop-loss sells the REAL amount
    broker._halt_reason = None
    (logs_dir(tmp_path) / HALT_MARKER_NAME).unlink()
    _act(STOP, broker, store)
    assert exchange.orders[-1] == (BTC, "sell", pytest.approx(0.002)) and BTC not in portfolio.positions


def test_f1_arch_b_trades_csv_failure_same_process_reconcile_books_once(tmp_path):
    ex = FaultyExchange()
    broker, portfolio, store = start(tmp_path, ex)
    store.set_pending(BTC, dict(PLAN))
    real = Portfolio.log_trade
    calls = {"n": 0}

    def flaky(self, row):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("EIO on trades.csv")
        return real(self, row)

    with patch.object(Portfolio, "log_trade", flaky):
        with pytest.raises(OSError):
            _act(FILL, broker, store)
        _reconcile_live_orders(broker, store)
    assert len(ex.orders) == 1
    assert portfolio.positions[BTC].amount == pytest.approx(0.002) and portfolio.cash == pytest.approx(9_900.0)
    assert store.get(BTC).fill_amount == pytest.approx(0.002)


# --- F2: an unusable sell's id is never reused, its record never overwritten/deleted -----------


def test_f2a_unusable_sell_record_survives_and_its_id_is_never_resent(tmp_path):
    exchange = FaultyExchange()
    broker, portfolio, store = _opened(tmp_path, exchange)
    exchange.free["BTC"] = 0.002
    exchange.respond = lambda order: {**order, "filled": float("nan")}
    exchange.track_balances = True
    with pytest.raises(LiveOrderUnreconciled):
        _act(STOP, broker, store)
    first = exchange.client_ids[-1]
    record = _inflight(tmp_path)[first]
    assert record["state"] == "unusable"
    assert json.loads((logs_dir(tmp_path) / ORDER_STATE_NAME).read_text())["attempts"][
        f"{record['plan_ref']}|sell"] == record["attempt"] + 1  # consumed in the same write
    exchange.respond = None
    exchange.find_error = ccxt.NetworkError("lookup down")
    exchange.balance_error = ccxt.NetworkError("balance down")
    _reconcile_live_orders(broker, store)
    n = len(exchange.orders)
    with pytest.raises(LiveBrokerHalted):
        _act(STOP, broker, store)                       # the re-sell is refused...
    assert len(exchange.orders) == n                    # ...before reaching the exchange
    assert _inflight(tmp_path)[first]["state"] == "unusable"
    assert first not in exchange.client_ids[:-1]        # never sent twice
    # once the lookup works again, the reconcile books the one real sale and closes the plan
    exchange.find_error = None
    _reconcile_live_orders(broker, store)
    assert store.get(BTC) is None and BTC not in portfolio.positions and _inflight(tmp_path) == {}
    assert [s for _sym, s, _a in exchange.orders].count("sell") == 1


def test_f2b_shared_account_unusable_sell_is_never_sold_twice(tmp_path):
    exchange = FaultyExchange()
    broker, portfolio, store = _opened(tmp_path, exchange)
    exchange.free["BTC"] = 1.0  # the human's own BTC lives in the same account
    exchange.track_balances = True
    exchange.respond = lambda order: {**order, "filled": "0.002"}
    with pytest.raises(LiveOrderUnreconciled):
        _act(STOP, broker, store)
    exchange.respond = None
    exchange.find_error = ccxt.NetworkError("lookup down")
    _reconcile_live_orders(broker, store)
    with pytest.raises(LiveBrokerHalted):
        _act(STOP, broker, store)
    sells = [a for _s, side, a in exchange.orders if side == "sell"]
    assert sells == [pytest.approx(0.002)]  # once
    assert exchange.free["BTC"] == pytest.approx(1.0 - 0.002)  # the human's coins were not sold


def test_f2_arch_a_resell_after_unusable_never_reuses_the_id_or_deletes_the_record(tmp_path):
    ex = FaultyExchange()
    broker, portfolio, store = _opened(tmp_path, ex)
    ex.respond = lambda o: dict(o, filled=float("nan"))
    with pytest.raises(LiveOrderUnreconciled):
        _act(STOP, broker, store)
    c1 = next(iter(_inflight(tmp_path)))
    ex.respond = None
    ex.find_error = ccxt.NetworkError("read api down")
    _reconcile_live_orders(broker, store)
    ex.create_error = ccxt.InsufficientFunds("nothing to sell")
    with pytest.raises(LiveBrokerHalted):
        _act(STOP, broker, store)
    assert ex.client_ids.count(c1) == 1
    assert c1 in _inflight(tmp_path)


def test_f2_record_intent_refuses_an_id_that_already_has_a_record(tmp_path):
    exchange = FaultyExchange()
    broker, portfolio, store = start(tmp_path, exchange)
    coid = broker_live.client_order_id(f"{BTC}|p", "buy", 0)
    broker._state["inflight"][coid] = {"client_order_id": coid, "plan_ref": f"{BTC}|p", "symbol": "ETH/USDT",
                                       "side": "buy", "attempt": 0, "state": "booked"}
    broker._session_booked.add(coid)
    with pytest.raises(LiveBrokerHalted, match="already in use"):
        broker.buy(BTC, 100.0, 50_000.0, order_ref=f"{BTC}|p")
    assert exchange.orders == [] and broker.halted


def test_f2_drop_entry_never_deletes_a_record_of_a_known_order(tmp_path):
    exchange = FaultyExchange()
    broker, portfolio, store = start(tmp_path, exchange)
    coid = "hctknown000000000"
    broker._state["inflight"][coid] = {"client_order_id": coid, "plan_ref": "x", "symbol": BTC, "side": "sell",
                                       "attempt": 0, "state": "unusable"}
    broker._drop_entry(coid)
    assert coid in broker._state["inflight"]


# --- F3: plan store / trades.csv durability -----------------------------------------------------


def test_f3_arch_h_store_write_error_never_leads_to_a_second_buy(tmp_path):
    ex = FaultyExchange()
    broker, portfolio, store = start(tmp_path, ex)
    store.set_pending(BTC, dict(PLAN))
    real = TradePlanStore._persist_current
    n = {"k": 0}

    def flaky(self, *args):
        n["k"] += 1
        if n["k"] == 1:
            raise OSError("ENOSPC on trade_plans_current.json")
        return real(self, *args)

    with patch.object(TradePlanStore, "_persist_current", flaky):
        with pytest.raises(OSError):
            _act(FILL, broker, store)
    assert store.get(BTC).status == "PENDING"  # memory never claimed what the disk doesn't have
    assert broker.halted and any("not persisted" in a["title"] for a in broker.pop_alerts())
    broker2, portfolio2, store2 = start(tmp_path, ex)  # an ordinary restart
    _reconcile_live_orders(broker2, store2)
    assert store2.get(BTC).status == "OPEN"
    with pytest.raises(LiveBrokerHalted):
        _act(FILL, broker2, store2)
    assert len(ex.orders) == 1


def test_f3_arch_f_store_write_is_durable_before_the_order_record_is_cleared(tmp_path, monkeypatch):
    ex = FaultyExchange()
    broker, portfolio, store = start(tmp_path, ex)
    store.set_pending(BTC, dict(PLAN))
    events: list[str] = []
    real_fsync = os.fsync
    real_atomic = broker_live._atomic_write_json

    def fsync(fd):
        try:
            name = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            name = "?"
        events.append(f"fsync {Path(name).name}")
        return real_fsync(fd)

    def atomic(path, payload):
        if path.name == ORDER_STATE_NAME and not payload["inflight"]:
            events.append("settle-write")
        return real_atomic(path, payload)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(broker_live, "_atomic_write_json", atomic)
    _act(FILL, broker, store)
    settle = events.index("settle-write")
    assert "fsync trade_plans_current.json.tmp" in events[:settle], events
    assert "fsync trades.csv" in events[:settle], events


def test_f3_store_transitions_are_copy_on_write(tmp_path):
    store = TradePlanStore(tmp_path / "c.json", tmp_path / "h.jsonl")
    store.set_pending(BTC, dict(PLAN))
    with patch.object(TradePlanStore, "_persist_current", side_effect=OSError("EIO")):
        for call in (lambda: store.mark_open(BTC, 1.0, 1.0), lambda: store.discard_pending(BTC, "x", 1.0)):
            with pytest.raises(OSError):
                call()
            assert store.get(BTC).status == "PENDING"
    assert json.loads((tmp_path / "c.json").read_text())[BTC]["status"] == "PENDING"


def test_f3_arch_f2_torn_current_json_live_quarantines_and_halts_paper_unchanged(tmp_path, monkeypatch):
    from src.bot import build_components
    from src.config import Config
    from tests.test_bot import _minimal_raw_config

    monkeypatch.setattr("src.bot.PROJECT_ROOT", tmp_path)
    (tmp_path / "logs").mkdir()
    torn = '{"BTC/USDT": {"symbol": "BT'
    (tmp_path / "logs" / "trade_plans_current.json").write_text(torn)
    with pytest.raises(json.JSONDecodeError):  # paper: unchanged behaviour
        build_components(Config(raw=_minimal_raw_config()))
    raw = _minimal_raw_config()
    raw["mode"] = "live"
    monkeypatch.setenv("LIVE_TRADING_CONFIRM", "YES-I-UNDERSTAND-THE-RISK")
    _exchange, _risk, _portfolio, broker, store, _audit = build_components(Config(raw=raw))
    assert broker.halted and store.all_current() == {}
    assert [p.name for p in (tmp_path / "logs").glob("trade_plans_current.json.corrupt-*")]
    assert any("plan store was unreadable" in a["title"] for a in broker.pop_alerts())
    quarantined = next((tmp_path / "logs").glob("trade_plans_current.json.corrupt-*"))
    assert quarantined.read_text() == torn  # evidence kept


def test_f3_portfolio_log_trade_is_fsynced(tmp_path, monkeypatch):
    calls = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real(fd))[1])
    Portfolio(cash=1.0, trade_log_path=tmp_path / "t.csv").log_trade({"a": 1})
    assert calls


# --- F4: a partial-sell halt survives a crash --------------------------------------------------


def test_f4_arch_c_partial_sell_halt_survives_a_crash_during_the_marker_write(tmp_path):
    ex = FaultyExchange()
    broker, portfolio, store = _opened(tmp_path, ex)
    ex.fill_ratio = 0.5
    real = broker_live._atomic_write_json

    def crash_on_marker(path, payload):
        if path.name == HALT_MARKER_NAME:
            raise SimulatedCrash("killed while writing the halt marker")
        return real(path, payload)

    with patch.object(broker_live, "_atomic_write_json", crash_on_marker):
        with pytest.raises(SimulatedCrash):
            _act(STOP, broker, store)
    ex.fill_ratio = 1.0
    broker2, portfolio2, store2 = start(tmp_path, ex)
    _reconcile_live_orders(broker2, store2)
    assert broker2.halted
    assert portfolio2.positions[BTC].amount == pytest.approx(0.001)  # the remainder, booked once
    assert len(_rows(tmp_path)) == 2


def test_f4_partial_sell_halt_reason_is_kept_when_the_marker_write_fails(tmp_path, monkeypatch):
    ex = FaultyExchange()
    broker, portfolio, store = _opened(tmp_path, ex)
    ex.fill_ratio = 0.5
    real = broker_live._atomic_write_json

    def no_marker(path, payload):
        if path.name == HALT_MARKER_NAME:
            raise OSError("read-only")
        return real(path, payload)

    monkeypatch.setattr(broker_live, "_atomic_write_json", no_marker)
    with pytest.raises(broker_live.LivePartialFill):
        _act(STOP, broker, store)
    record = next(iter(_inflight(tmp_path).values()))  # settle refused: the halt is not on disk
    assert record["halt_reason"] and record["halt_durable"] is False
    monkeypatch.setattr(broker_live, "_atomic_write_json", real)
    broker2, _p, _s = start(tmp_path, ex)
    assert broker2.halted


def test_f4_reconcile_alert_text_tells_the_truth_about_the_halt(tmp_path):
    ex = FaultyExchange()
    broker, portfolio, store = start(tmp_path, ex)
    store.set_pending(BTC, dict(PLAN))
    store.mark_open = lambda *a, **k: (_ for _ in ()).throw(SimulatedCrash("killed"))
    with pytest.raises(SimulatedCrash):
        _act(FILL, broker, store)
    broker2, _p, store2 = start(tmp_path, ex)  # a known outcome: not halted
    _reconcile_live_orders(broker2, store2)
    texts = [a["bot_action"] for a in broker2.pop_alerts() if a["kind"] == "reconcile"]
    assert texts and all("NOT halted" in t for t in texts) and not broker2.halted


def test_f4_fuzz_a_halt_stashed_before_its_marker_survives_a_crash_in_the_marker_write(tmp_path):
    # Found by the bounded Architect fuzz in the fix round (seed 90/183 class): a halt that is not
    # tied to an order record (here: a free-balance mismatch) and whose marker write is interrupted.
    ex = FaultyExchange()
    broker, portfolio, store = _opened(tmp_path, ex)
    ex.free["BTC"] = 0.0005
    real = broker_live._atomic_write_json

    def crash_on_marker(path, payload):
        if path.name == HALT_MARKER_NAME:
            raise SimulatedCrash("killed while writing the halt marker")
        return real(path, payload)

    with patch.object(broker_live, "_atomic_write_json", crash_on_marker):
        with pytest.raises(SimulatedCrash):
            _act(STOP, broker, store)
    assert not (logs_dir(tmp_path) / HALT_MARKER_NAME).exists()
    broker2, _p, _s = start(tmp_path, ex)
    assert broker2.halted and (logs_dir(tmp_path) / HALT_MARKER_NAME).exists()
    assert "pending_halt" not in json.loads((logs_dir(tmp_path) / ORDER_STATE_NAME).read_text())
    # ...and once the marker is on disk it is the human's control: deleting it resumes.
    (logs_dir(tmp_path) / HALT_MARKER_NAME).unlink()
    broker3, _p, _s = start(tmp_path, ex)
    assert not broker3.halted


# --- F5: settle never outruns the halt; terminal zero fills are not executed ----------------------


def test_f5_settle_refused_while_the_halt_is_only_in_memory(tmp_path, monkeypatch):
    ex = FaultyExchange()
    broker, portfolio, store = start(tmp_path, ex)
    store.set_pending(BTC, dict(PLAN))
    real = broker_live._atomic_write_json

    def no_marker(path, payload):
        if path.name == HALT_MARKER_NAME:
            raise OSError("read-only")
        return real(path, payload)

    monkeypatch.setattr(broker_live, "_atomic_write_json", no_marker)
    ex.respond = lambda order: dict(order, average=float("nan"))  # halting buy
    _act(FILL, broker, store)
    assert len(_inflight(tmp_path)) == 1  # kept: it is what keeps the next start halted
    assert any(a["kind"] == "halt_marker_failed" for a in broker.pop_alerts())
    monkeypatch.setattr(broker_live, "_atomic_write_json", real)
    broker2, _p, _s = start(tmp_path, ex)
    assert broker2.halted


@pytest.mark.parametrize("status", ["canceled", "expired", "rejected"])
def test_f5_terminal_zero_fill_buy_is_not_executed(tmp_path, status):
    ex = FaultyExchange()
    broker, portfolio, store = start(tmp_path, ex)
    store.set_pending(BTC, dict(PLAN))
    ex.fill_ratio, ex.status = 0.0, status
    with pytest.raises(broker_live.LiveOrderNotExecuted):
        _act(FILL, broker, store)
    assert BTC not in portfolio.positions and portfolio.cash == 10_000.0 and _rows(tmp_path) == []
    assert store.get(BTC).status == "PENDING" and _inflight(tmp_path) == {} and not broker.halted
    ex.fill_ratio, ex.status = 1.0, "closed"
    _act(FILL, broker, store)  # retried with a NEW id (the first exists on the exchange)
    assert len(set(ex.client_ids)) == 2 and store.get(BTC).status == "OPEN"


def test_f5_ack_only_zero_fill_is_resolved_by_the_lookup(tmp_path):
    ex = FaultyExchange()
    broker, portfolio, store = start(tmp_path, ex)
    store.set_pending(BTC, dict(PLAN))
    ex.fill_ratio, ex.status = 0.0, "canceled"
    ex.respond = lambda order: {"id": order["id"]}  # Kraken-style ack
    with pytest.raises(broker_live.LiveOrderNotExecuted):
        _act(FILL, broker, store)
    assert BTC not in portfolio.positions and _rows(tmp_path) == []


# --- F6: no DISCARD while the plan's buy is unresolved ---------------------------------------------


def test_f6_rr_f3_discard_is_deferred_while_the_buy_is_unresolved(tmp_path):
    exchange = FaultyExchange()
    broker, portfolio, store = start(tmp_path, exchange)
    store.set_pending(BTC, dict(PLAN))
    exchange.error_after_book = ccxt.RequestTimeout("timeout")
    with pytest.raises(LiveOrderUnreconciled):
        _act(FILL, broker, store)
    exchange.error_after_book = None
    exchange.find_error = ccxt.NetworkError("lookup down")
    _reconcile_live_orders(broker, store)
    event: dict = {}
    _execute_monitor_action(MonitorAction(symbol=BTC, action="DISCARD", price=39_000.0, reason="x"),
                            store.get(BTC), broker, store, _Risk(), event)
    assert store.get(BTC).status == "PENDING" and "deferred" in event["note"]
    exchange.find_error = None
    _reconcile_live_orders(broker, store)
    assert store.get(BTC).status == "OPEN" and portfolio.positions[BTC].amount == pytest.approx(0.002)


# --- F7: id-less responses, paper headers, reset_portfolio ------------------------------------------


def _crash_after_csv_row():
    real = Portfolio.log_trade

    def write_then_crash(self, row):
        real(self, row)
        raise SimulatedCrash("killed after the trades.csv row")

    return patch.object(Portfolio, "log_trade", write_then_crash)


def test_f7_rr_f7_idless_buy_crash_is_debited_once(tmp_path, monkeypatch):
    exchange = FaultyExchange()
    broker, portfolio, store = start(tmp_path, exchange)
    store.set_pending(BTC, dict(PLAN))
    exchange.respond = lambda order: {k: v for k, v in order.items() if k != "id"}
    real = broker_live._atomic_write_json

    def crash_on_booked(path, payload):
        if path.name == ORDER_STATE_NAME and any(e["state"] == "booked" for e in payload["inflight"].values()):
            raise SimulatedCrash("killed")
        return real(path, payload)

    monkeypatch.setattr(broker_live, "_atomic_write_json", crash_on_booked)
    with pytest.raises(SimulatedCrash):
        _act(FILL, broker, store)
    monkeypatch.setattr(broker_live, "_atomic_write_json", real)
    exchange.respond = None
    broker2, portfolio2, store2 = start(tmp_path, exchange)
    _reconcile_live_orders(broker2, store2)
    assert len(_rows(tmp_path)) == 1 and portfolio2.cash == pytest.approx(9_900.0)
    assert ",None" not in "".join(_rows(tmp_path))  # never the literal "None" as an order id


def test_f7_arch_e_idless_sell_crash_is_credited_once(tmp_path):
    ex = FaultyExchange()
    broker, portfolio, store = _opened(tmp_path, ex)
    cash_before = reconstruct_cash(logs_dir(tmp_path) / "trades.csv", 10_000.0)
    ex.respond = lambda o: dict(o, id=None)
    with _crash_after_csv_row():
        with pytest.raises(SimulatedCrash):
            _act(STOP, broker, store)
    ex.respond = None
    broker2, portfolio2, store2 = start(tmp_path, ex)
    _reconcile_live_orders(broker2, store2)
    cash_after = reconstruct_cash(logs_dir(tmp_path) / "trades.csv", 10_000.0)
    assert cash_after == pytest.approx(cash_before + 0.002 * 50_000.0)  # once, not twice
    assert portfolio2.cash == pytest.approx(cash_after) and store2.get(BTC) is None


def test_f7_arch_d_paper_header_keeps_the_dedup_working(tmp_path):
    log = logs_dir(tmp_path)
    log.mkdir(parents=True)
    (log / "trades.csv").write_text(
        "timestamp,mode,symbol,side,price,base_amount,quote_amount\n"
        "2026-01-01T00:00:00+00:00,paper,ETH/USDT,buy,2000,0.05,100\n"
        "2026-01-02T00:00:00+00:00,paper,ETH/USDT,sell,2000,0.05,100\n"
    )
    ex = FaultyExchange()
    broker, portfolio, store = start(tmp_path, ex)
    store.set_pending(BTC, dict(PLAN))
    with _crash_after_csv_row():
        with pytest.raises(SimulatedCrash):
            _act(FILL, broker, store)
    broker2, portfolio2, store2 = start(tmp_path, ex)
    _reconcile_live_orders(broker2, store2)
    lines = (log / "trades.csv").read_text().splitlines()
    assert lines[0].endswith(",order_id")  # header extended atomically
    assert len([r for r in lines if ",live," in r]) == 1 and len(ex.orders) == 1
    assert reconstruct_cash(log / "trades.csv", 10_000.0) == pytest.approx(9_900.0)


@pytest.mark.parametrize("leftover", ["halt", "record"])
def test_f7_reset_portfolio_refuses_while_live_state_is_unresolved(tmp_path, leftover):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "trades.csv").write_text("x\n")
    if leftover == "halt":
        (logs / HALT_MARKER_NAME).write_text("{}")
    else:
        (logs / ORDER_STATE_NAME).write_text(json.dumps({"inflight": {"c": {}}, "attempts": {}}))
    script = REPO / "scripts" / "reset_portfolio.py"
    out = subprocess.run([sys.executable, str(script), "--logs-dir", str(logs)],
                         capture_output=True, text=True, cwd=REPO)
    assert out.returncode != 0 and "refus" in (out.stdout + out.stderr).lower()
    assert (logs / "trades.csv").exists()
    out = subprocess.run([sys.executable, str(script), "--logs-dir", str(logs), "--include-live-state"],
                         capture_output=True, text=True, cwd=REPO)
    assert out.returncode == 0, out.stderr
    assert "WARNING" in out.stdout + out.stderr
    assert not (logs / HALT_MARKER_NAME).exists() and not (logs / ORDER_STATE_NAME).exists()
    archived = list((logs / "archive").glob("*/*"))  # archived, not deleted
    assert {p.name for p in archived} >= {"trades.csv"} and len(archived) == 2


# --- F8: Kraken specifics --------------------------------------------------------------------------


def test_f8b_ack_only_response_is_verified_before_booking(tmp_path):
    ex = FaultyExchange()
    ex.fee = {"currency": "quote", "rate": 0.0026}
    broker, portfolio, store = start(tmp_path, ex)
    ex.respond = lambda order: {"id": order["id"]}  # Kraken create_order: txid only
    ex.fill_ratio, ex.status = 0.5, "canceled"
    fill = broker.buy(BTC, 100.0, 50_000.0)
    assert fill.base_amount == pytest.approx(0.001)                      # the REAL filled amount
    assert fill.quote_amount == pytest.approx(50.0 + 50.0 * 0.0026)      # with the real fee
    assert not broker.halted


def test_f8b_ack_only_with_failed_lookup_is_booked_unverified_halted_and_rechecked(tmp_path):
    ex = FaultyExchange()
    broker, portfolio, store = start(tmp_path, ex)
    ex.respond = lambda order: {"id": order["id"]}
    ex.find_error = ccxt.NetworkError("down")
    broker.buy(BTC, 100.0, 50_000.0)
    assert broker.halted
    state = json.loads((logs_dir(tmp_path) / ORDER_STATE_NAME).read_text())
    assert list(state["unverified"]) == [ex.client_ids[0]]
    broker.pop_alerts()
    ex.find_error = None
    broker.reconcile()
    state = json.loads((logs_dir(tmp_path) / ORDER_STATE_NAME).read_text())
    assert state["unverified"] == {}  # the lookup confirmed the booked amount
    assert any("verified" in a["title"] for a in broker.pop_alerts())


def test_f8c_dust_sell_is_not_sent_and_is_alerted(tmp_path):
    ex = FaultyExchange()
    ex.min_order_amount = lambda symbol: 0.01
    broker, portfolio, store = _opened(tmp_path, ex)
    n = len(ex.orders)
    with pytest.raises(broker_live.LiveExitBlocked):
        _act(STOP, broker, store)
    assert len(ex.orders) == n
    assert any("cannot exit" in a["title"] for a in broker.pop_alerts())


def test_f8d_precision_truncated_full_fill_is_not_labelled_partial(tmp_path):
    ex = FaultyExchange()
    ex.precision = lambda symbol, amount: float(f"{amount:.3f}")
    broker, portfolio, store = start(tmp_path, ex)
    store.set_pending(BTC, dict(PLAN, approved_quote_amount=123.0))  # requests 0.00246 BTC
    ex.crash_after_create = True
    with pytest.raises(SimulatedCrash):
        _act(FILL, broker, store)
    ex.crash_after_create = False
    coid = ex.client_ids[0]
    ex.book[coid]["filled"] = float(f"{ex.book[coid]['amount']:.3f}")
    broker2, _p, _s = start(tmp_path, ex)
    results = broker2.reconcile()
    assert [r.outcome for r in results] == ["filled"]


# --- F9: one live process per logs/ --------------------------------------------------------------


def test_f9_arch_g_a_second_live_process_refuses_to_start(tmp_path):
    logs = tmp_path / "logs"
    holder = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(REPO)!r})
        from src.broker_live import LiveBroker
        from src.portfolio import Portfolio
        b = LiveBroker(object(), Portfolio(cash=1.0, trade_log_path=__import__('pathlib').Path({str(logs)!r}) / 't.csv'),
                       'live', True)
        print('locked', flush=True)
        sys.stdin.read()
    """)
    proc = subprocess.Popen([sys.executable, "-c", holder], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "locked"
        second = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(REPO)!r})
            from src.broker_live import LiveBroker, LiveBrokerLocked
            from src.portfolio import Portfolio
            try:
                LiveBroker(object(), Portfolio(cash=1.0, trade_log_path=__import__('pathlib').Path({str(logs)!r}) / 't.csv'),
                           'live', True)
            except LiveBrokerLocked as exc:
                print('refused:', exc); sys.exit(3)
            print('started'); sys.exit(0)
        """)
        out = subprocess.run([sys.executable, "-c", second], capture_output=True, text=True)
        assert out.returncode == 3 and "another live bot process" in out.stdout
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
    out = subprocess.run([sys.executable, "-c", second], capture_output=True, text=True)
    assert out.returncode == 0  # the lock is released with its process


def test_f9_the_gate_is_checked_before_the_lock(tmp_path):
    with pytest.raises(broker_live.LiveTradingDisabled):
        broker_live.LiveBroker(object(), Portfolio(cash=1.0, trade_log_path=tmp_path / "logs" / "t.csv"), "live", False)
    assert not (tmp_path / "logs").exists()  # no lock file, nothing touched


# --- F10: alerts ---------------------------------------------------------------------------------


@pytest.mark.parametrize("exc", [ccxt.InvalidOrder("EOrder:Order minimum not met"),
                                 ccxt.PermissionDenied("EGeneral:Permission denied"),
                                 ccxt.AccountSuspended("EAccount:suspended")], ids=lambda e: type(e).__name__)
def test_f10_rr_f4_every_failed_exit_is_alerted_with_reminders(tmp_path, exc):
    exchange = FaultyExchange()
    broker, portfolio, store = _opened(tmp_path, exchange)
    now = [1_000_000.0]
    broker._clock = lambda: now[0]
    broker.pop_alerts()
    exchange.create_error = exc
    seen = []
    for _ in range(5):
        with pytest.raises(type(exc)):
            _act(STOP, broker, store)
        seen += [a for a in broker.pop_alerts() if a["kind"] == "exit_failed"]
        now[0] += 60
    assert len(seen) == 1 and type(exc).__name__ in seen[0]["title"]
    assert str(exc) not in str(seen[0])  # class name only, never the exception text
    now[0] += broker_live.ALERT_REMINDER_SECONDS
    with pytest.raises(type(exc)):
        _act(STOP, broker, store)
    reminders = [a for a in broker.pop_alerts() if a["kind"] == "exit_failed"]
    assert len(reminders) == 1 and reminders[0]["reminder"] is True


def test_f10_rr_f5_a_failed_telegram_send_is_retried_until_delivered(tmp_path):
    exchange = FaultyExchange()
    broker, portfolio, store = start(tmp_path, exchange)
    store.set_pending(BTC, dict(PLAN))
    exchange.error_after_book = ccxt.RequestTimeout("timeout")
    exchange.find_error = ccxt.NetworkError("down")
    with pytest.raises(LiveOrderUnreconciled):
        _act(FILL, broker, store)
    cfg = SimpleNamespace(telegram_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ", telegram_chat_id="1",
                          notifier_label=None)
    attempts = []
    results = iter([False, False, True] + [True] * 50)

    def send(token, chat, text):
        attempts.append(text)
        return next(results)

    with patch("src.bot.notifier.send_telegram_message", side_effect=send):
        for _ in range(4):
            _send_broker_alerts(cfg, broker)
    halt = [t for t in attempts if "create\\_market\\_order raised RequestTimeout" in t]
    assert len(halt) == 3  # failed, failed, delivered -- then not again


def test_f10_halt_reminder_while_still_halted(tmp_path):
    exchange = FaultyExchange()
    broker, portfolio, store = start(tmp_path, exchange)
    now = [0.0]
    broker._clock = lambda: now[0]
    exchange.create_error = ccxt.RequestTimeout("t/o")
    with pytest.raises(LiveOrderUnreconciled):
        broker.buy(BTC, 100.0, 50_000.0)
    broker.pop_alerts()
    now[0] += broker_live.ALERT_REMINDER_SECONDS + 1
    assert any(a["kind"] == "halt_reminder" for a in broker.pop_alerts())


def test_f10_alert_length_is_capped_and_state_file_alert_has_no_exception_text(tmp_path):
    from src.bot import ALERT_MAX_CHARS, _format_broker_alert

    text = _format_broker_alert(SimpleNamespace(), {"title": "x" * 10_000, "bot_action": "y", "operator_check": "z"})
    assert len(text) <= ALERT_MAX_CHARS
    logs = logs_dir(tmp_path)
    logs.mkdir(parents=True)
    (logs / ORDER_STATE_NAME).write_text("{definitely not json SECRET-DETAIL")
    broker, _p, _s = start(tmp_path, FaultyExchange())
    alerts = broker.pop_alerts()
    assert alerts and all("SECRET-DETAIL" not in str(a) and "Expecting" not in str(a) for a in alerts)


# --- mutations that survived the review (Architect my_mut.txt X03/X05/X06/X08/X09/X11/X15) -----------


def _never_sent_intent(tmp_path):
    """A buy intent whose request never left (crash before sending): the record is all there is."""
    ex = FaultyExchange()
    broker, portfolio, store = start(tmp_path, ex)
    store.set_pending(BTC, dict(PLAN))
    ex.on_create = lambda *a: (_ for _ in ()).throw(SimulatedCrash("killed before sending"))
    with pytest.raises(SimulatedCrash):
        _act(FILL, broker, store)
    ex.on_create = None
    broker2, portfolio2, store2 = start(tmp_path, ex)
    entry = next(iter(_inflight(tmp_path).values()))
    return ex, broker2, store2, entry


def test_x03_not_found_is_trusted_only_after_the_120s_grace_period(tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    monkeypatch.setattr(broker_live, "RECONCILE_NOT_FOUND_GRACE_SECONDS", 120)
    ex, broker, store, entry = _never_sent_intent(tmp_path)
    created = datetime.fromisoformat(entry["created_at"])
    assert [r.outcome for r in broker.reconcile(now=created + timedelta(seconds=60))] == ["unknown"]
    assert [r.outcome for r in broker.reconcile(now=created + timedelta(seconds=119))] == ["unknown"]
    assert [r.outcome for r in broker.reconcile(now=created + timedelta(seconds=121))] == ["never_placed"]


def test_x03_the_shipped_grace_period_is_120_seconds_and_is_the_one_used():
    # (the source, not the module attribute: the autouse fixture patches the attribute to 0)
    source = Path(broker_live.__file__).read_text()
    assert "\nRECONCILE_NOT_FOUND_GRACE_SECONDS = 120\n" in source
    assert "(now - created).total_seconds() < RECONCILE_NOT_FOUND_GRACE_SECONDS:" in source


def test_x15_order_listing_looks_back_ten_minutes_before_the_record(tmp_path):
    from datetime import datetime

    ex, broker, store, entry = _never_sent_intent(tmp_path)
    broker.reconcile()
    created_ms = int(datetime.fromisoformat(entry["created_at"]).timestamp() * 1000)
    assert ex.lookups[-1][3] == created_ms - broker_live.RECONCILE_LOOKBACK_SECONDS * 1000
    assert broker_live.RECONCILE_LOOKBACK_SECONDS == 600


def test_x05_order_state_writes_are_fsynced_before_the_replace(tmp_path, monkeypatch):
    synced = []
    real = os.fsync

    def fsync(fd):
        try:
            synced.append(Path(os.readlink(f"/proc/self/fd/{fd}")).name)
        except OSError:
            synced.append("?")
        return real(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    broker_live._atomic_write_json(tmp_path / ORDER_STATE_NAME, {"x": 1})
    assert synced[0] == ORDER_STATE_NAME + ".tmp"


def test_x06_every_cycle_reconciles_not_only_the_first(tmp_path):
    calls = []
    broker = SimpleNamespace(reconcile=lambda: calls.append(1) or [])
    for _ in range(3):
        _reconcile_live_orders(broker, SimpleNamespace())
    assert len(calls) == 3


def test_x08_an_unusable_record_alone_halts_buys_everywhere(tmp_path):
    ex = FaultyExchange()
    broker, portfolio, store = _opened(tmp_path, ex)
    ex.respond = lambda o: dict(o, filled=float("nan"))
    with pytest.raises(LiveOrderUnreconciled):
        _act(STOP, broker, store)
    ex.respond = None
    broker._halt_reason = None
    (logs_dir(tmp_path) / HALT_MARKER_NAME).unlink()  # only the record is left
    assert broker.halted
    with pytest.raises(LiveBrokerHalted):
        broker.buy("ETH/USDT", 50.0, 2_000.0)
    broker2, _p, _s = start(tmp_path, ex)  # and after a restart
    assert broker2.halted


def test_x09_a_reconciled_buy_is_never_attached_to_a_different_plan(tmp_path):
    ex = FaultyExchange()
    broker, portfolio, store = start(tmp_path, ex)
    store.set_pending(BTC, dict(PLAN))
    store.mark_open = lambda *a, **k: (_ for _ in ()).throw(SimulatedCrash("killed"))
    with pytest.raises(SimulatedCrash):
        _act(FILL, broker, store)
    broker2, portfolio2, store2 = start(tmp_path, ex)
    store2.discard_pending(BTC, reason="manual", price=1.0)
    store2.set_pending(BTC, dict(PLAN))  # a NEW plan on the same symbol
    _reconcile_live_orders(broker2, store2)
    assert store2.get(BTC).status == "PENDING"  # not opened with the old plan's fill
    assert len(_inflight(tmp_path)) == 1 and broker2.halted


def test_x11_order_state_memory_moves_only_after_the_disk_write(tmp_path, monkeypatch):
    ex = FaultyExchange()
    broker, portfolio, store = start(tmp_path, ex)
    monkeypatch.setattr(broker_live, "_atomic_write_json", lambda path, payload: (_ for _ in ()).throw(OSError("EIO")))
    with pytest.raises(OSError):
        broker._mutate_state(lambda s: s["attempts"].__setitem__("k", 1))
    assert "k" not in broker._state["attempts"]


# --- S1: the broker contract ------------------------------------------------------------------------


def test_s1_contract_declared_and_paper_broker_has_none_of_it():
    from src.broker_contract import LIVE_BROKER_ATTRIBUTES, LiveBrokerContract
    from src.broker_paper import PaperBroker

    assert set(LIVE_BROKER_ATTRIBUTES) == {"TRACKS_ORDER_REFS", "halted", "reconcile", "settle", "settle_order",
                                            "report_unmatched", "pop_alerts", "requeue_alerts",
                                            "report_store_failure", "order_in_flight"}
    paper = PaperBroker(Portfolio(cash=1.0))
    assert not any(hasattr(paper, name) for name in LIVE_BROKER_ATTRIBUTES)
    for name in LIVE_BROKER_ATTRIBUTES:
        assert hasattr(broker_live.LiveBroker, name), name
    assert LiveBrokerContract is not None


def test_s1_a_mock_broker_never_blocks_planning_or_gets_order_refs():
    from unittest.mock import MagicMock

    from src.bot import _flag, _order_kwargs

    mock = MagicMock()
    assert _flag(mock, "halted") is False and _order_kwargs(mock, SimpleNamespace(symbol=BTC, created_at="t")) == {}
