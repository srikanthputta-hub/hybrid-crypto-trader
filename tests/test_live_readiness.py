"""Issue #99 live-readiness fixes (docs/live_readiness_design.md). Every test uses
tests/_live_fakes.FaultyExchange: no network, no credentials, no real order.

Sections:
  1. Write-ahead in-flight marker + deterministic clientOrderId (item 1).
"""
from __future__ import annotations

import json
import re

import ccxt
import pytest

import src.broker_live as broker_live
from src.broker_live import (
    HALT_MARKER_NAME,
    ORDER_STATE_NAME,
    LiveBroker,
    LiveBrokerHalted,
    LiveOrderStateError,
    LiveOrderUnreconciled,
    client_order_id,
)
from src.monitor import MonitorAction
from src.portfolio import Portfolio, Position
from tests._live_fakes import PLAN, FaultyExchange, SimulatedCrash, logs_dir, make_store, start


def _state_file(tmp_path):
    return logs_dir(tmp_path) / ORDER_STATE_NAME


def _state(tmp_path) -> dict:
    return json.loads(_state_file(tmp_path).read_text())


def _halt_file(tmp_path):
    return logs_dir(tmp_path) / HALT_MARKER_NAME


def _risk(tmp_path):
    from src.risk_engine import RiskEngine

    return RiskEngine(
        starting_cash_usd=10_000, max_position_pct=0.1, max_total_exposure_pct=0.4, max_loss_per_trade_pct=0.01,
        max_daily_loss_pct=0.05, max_volatility_pct=0.08, min_risk_reward=1.5, cooldown_minutes=60,
        kill_switch_path=tmp_path / "KILL_SWITCH", symbol_whitelist=["BTC/USDT", "ETH/USDT"],
    )


def _plan_ref(store, symbol):
    return f"{symbol}|{store.get(symbol).created_at}"


def _fill(symbol="BTC/USDT", price=50_000.0):
    return MonitorAction(symbol=symbol, action="FILL", price=price, reason="in zone")


def _stop(symbol="BTC/USDT", price=45_000.0):
    return MonitorAction(symbol=symbol, action="STOP_LOSS", price=price, reason="stop")


def _open_btc(tmp_path, exchange=None):
    """A live broker with an OPEN, settled BTC/USDT plan bought through the bot's own path."""
    from src.bot import _execute_monitor_action

    exchange = exchange or FaultyExchange()
    broker, portfolio, store = start(tmp_path, exchange)
    store.set_pending("BTC/USDT", dict(PLAN))
    _execute_monitor_action(_fill(), store.get("BTC/USDT"), broker, store, _risk(tmp_path), {})
    assert store.get("BTC/USDT").status == "OPEN"
    return broker, portfolio, store, exchange


# --- 1. write-ahead marker + clientOrderId ----------------------------------------------


def test_client_order_id_is_deterministic_and_exchange_safe():
    a = client_order_id("BTC/USDT|2026-10-10T00:00:00.000001+00:00", "buy", 0)
    assert a == client_order_id("BTC/USDT|2026-10-10T00:00:00.000001+00:00", "buy", 0)
    assert re.fullmatch(r"hct[0-9a-f]{15}", a)  # Kraken cl_ord_id free text <= 18; Binance <= 36
    others = {
        client_order_id("BTC/USDT|2026-10-10T00:00:00.000001+00:00", "sell", 0),
        client_order_id("BTC/USDT|2026-10-10T00:00:00.000001+00:00", "buy", 1),
        client_order_id("BTC/USDT|2026-10-10T00:00:00.000002+00:00", "buy", 0),
        client_order_id("ETH/USDT|2026-10-10T00:00:00.000001+00:00", "buy", 0),
    }
    assert a not in others and len(others) == 4


def test_intent_is_on_disk_before_the_order_is_sent(tmp_path):
    exchange = FaultyExchange()
    broker, _portfolio, _store = start(tmp_path, exchange)
    seen = []

    def check(symbol, side, amount, coid):
        entry = _state(tmp_path)["inflight"][coid]
        seen.append((entry["state"], entry["symbol"], entry["side"], entry["requested_amount"]))

    exchange.on_create = check
    broker.buy("BTC/USDT", 100.0, 50_000.0, order_ref="BTC/USDT|p1")
    assert seen == [("intent", "BTC/USDT", "buy", 100.0 / 50_000.0)]
    assert exchange.client_ids == [client_order_id("BTC/USDT|p1", "buy", 0)]


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_marker_write_failure_places_no_order_and_halts(tmp_path, monkeypatch, side):
    exchange = FaultyExchange()
    positions = {"BTC/USDT": Position("BTC/USDT", 0.1, 50_000.0)}
    portfolio = Portfolio(cash=10_000.0, positions=positions, trade_log_path=logs_dir(tmp_path) / "trades.csv")
    broker = LiveBroker(exchange, portfolio, "live", True)
    real = broker_live._atomic_write_json

    def failing(path, payload):
        if path.name == ORDER_STATE_NAME:
            raise OSError("disk full")
        return real(path, payload)

    monkeypatch.setattr(broker_live, "_atomic_write_json", failing)
    with pytest.raises(LiveOrderStateError):
        broker.buy("BTC/USDT", 100.0, 50_000.0) if side == "buy" else broker.sell("BTC/USDT", 55_000.0)
    assert exchange.orders == []  # fail closed: no record, no order
    assert broker.halted
    assert portfolio.positions["BTC/USDT"].amount == 0.1 and portfolio.cash == 10_000.0


def test_definite_rejection_clears_intent_and_retry_reuses_the_same_id(tmp_path):
    exchange = FaultyExchange()
    broker, _portfolio, _store = start(tmp_path, exchange)
    exchange.create_error = ccxt.InsufficientFunds("no USDT")
    with pytest.raises(ccxt.InsufficientFunds):
        broker.buy("BTC/USDT", 100.0, 50_000.0, order_ref="BTC/USDT|p1")
    assert _state(tmp_path)["inflight"] == {} and not broker.halted
    exchange.create_error = None
    broker.buy("BTC/USDT", 100.0, 50_000.0, order_ref="BTC/USDT|p1")
    assert exchange.client_ids[0] == exchange.client_ids[1] == client_order_id("BTC/USDT|p1", "buy", 0)


def test_booked_order_consumes_the_attempt_and_settle_clears_it(tmp_path):
    exchange = FaultyExchange()
    broker, _portfolio, _store = start(tmp_path, exchange)
    broker.buy("BTC/USDT", 100.0, 50_000.0, order_ref="BTC/USDT|p1")
    coid = client_order_id("BTC/USDT|p1", "buy", 0)
    state = _state(tmp_path)
    assert state["inflight"][coid]["state"] == "booked"
    assert state["inflight"][coid]["fill"]["order_id"] == "x1"
    assert state["attempts"]["BTC/USDT|p1|buy"] == 1
    broker.settle("BTC/USDT")
    assert _state(tmp_path)["inflight"] == {}
    broker.sell("BTC/USDT", 55_000.0, order_ref="BTC/USDT|p1", exit_reason="take_profit")
    broker.settle("BTC/USDT")
    broker.buy("BTC/USDT", 100.0, 50_000.0, order_ref="BTC/USDT|p1")
    assert exchange.client_ids == [coid, client_order_id("BTC/USDT|p1", "sell", 0),
                                   client_order_id("BTC/USDT|p1", "buy", 1)]
    assert len(set(exchange.client_ids)) == 3


@pytest.mark.parametrize("error", [ccxt.RequestTimeout("t/o"), ccxt.ExchangeError("internal")], ids=repr)
def test_unknown_outcome_keeps_intent_halts_buys_and_same_symbol_sells(tmp_path, error):
    exchange = FaultyExchange()
    positions = {"ETH/USDT": Position("ETH/USDT", 1.0, 2_000.0), "BTC/USDT": Position("BTC/USDT", 0.1, 50_000.0)}
    portfolio = Portfolio(cash=10_000.0, positions=positions, trade_log_path=logs_dir(tmp_path) / "trades.csv")
    broker = LiveBroker(exchange, portfolio, "live", True)
    exchange.create_error = error
    with pytest.raises(LiveOrderUnreconciled):
        broker.sell("BTC/USDT", 55_000.0, order_ref="BTC/USDT|p1")
    entry = next(iter(_state(tmp_path)["inflight"].values()))
    assert entry["state"] == "intent" and entry["symbol"] == "BTC/USDT"
    exchange.create_error = None
    n = len(exchange.orders)
    with pytest.raises(LiveBrokerHalted):
        broker.sell("BTC/USDT", 55_000.0, order_ref="BTC/USDT|p1")  # could double-sell
    with pytest.raises(LiveBrokerHalted):
        broker.buy("SOL/USDT", 50.0, 100.0)
    assert len(exchange.orders) == n
    broker.sell("ETH/USDT", 2_100.0)  # another symbol's protective exit still runs
    assert "ETH/USDT" not in portfolio.positions


def test_crash_after_create_restart_is_halted_and_never_reorders(tmp_path):
    from src.bot import _execute_monitor_action

    exchange = FaultyExchange()
    broker, _portfolio, store = start(tmp_path, exchange)
    store.set_pending("BTC/USDT", dict(PLAN))
    exchange.crash_after_create = True
    with pytest.raises(SimulatedCrash):
        _execute_monitor_action(_fill(), store.get("BTC/USDT"), broker, store, _risk(tmp_path), {})
    exchange.crash_after_create = False
    assert next(iter(_state(tmp_path)["inflight"].values()))["state"] == "intent"
    assert not _halt_file(tmp_path).exists()  # the crash left no halt marker behind

    broker2, _portfolio2, store2 = start(tmp_path, exchange)  # restart from disk
    assert broker2.halted and _halt_file(tmp_path).exists()
    assert store2.get("BTC/USDT").status == "PENDING"
    for _cycle in range(3):  # the monitor keeps re-triggering FILL on the PENDING plan
        with pytest.raises(LiveBrokerHalted):
            _execute_monitor_action(_fill(), store2.get("BTC/USDT"), broker2, store2, _risk(tmp_path), {})
    assert len(exchange.orders) == 1  # never a duplicate


def test_failed_halt_marker_write_then_restart_still_halts(tmp_path, monkeypatch):
    exchange = FaultyExchange()
    broker, _portfolio, _store = start(tmp_path, exchange)
    real = broker_live._atomic_write_json

    def no_halt_marker(path, payload):
        if path.name == HALT_MARKER_NAME:
            raise OSError("read-only")
        return real(path, payload)

    monkeypatch.setattr(broker_live, "_atomic_write_json", no_halt_marker)
    exchange.create_error = ccxt.RequestTimeout("t/o")
    with pytest.raises(LiveOrderUnreconciled):
        broker.buy("BTC/USDT", 100.0, 50_000.0, order_ref="BTC/USDT|p1")
    assert not _halt_file(tmp_path).exists()
    exchange.create_error = None
    broker2, _p, _s = start(tmp_path, exchange)  # restart: the halt marker never made it to disk
    with pytest.raises(LiveBrokerHalted):
        broker2.buy("BTC/USDT", 100.0, 50_000.0, order_ref="BTC/USDT|p1")
    assert len(exchange.orders) == 1


def test_leftover_booked_entry_blocks_its_symbol_after_restart(tmp_path):
    from src.bot import _execute_monitor_action

    exchange = FaultyExchange()
    broker, _portfolio, store = start(tmp_path, exchange)
    store.set_pending("BTC/USDT", dict(PLAN))
    real_mark_open = store.mark_open

    def crash_mark_open(*args, **kwargs):
        raise SimulatedCrash("killed between booking and the store update")

    store.mark_open = crash_mark_open
    with pytest.raises(SimulatedCrash):
        _execute_monitor_action(_fill(), store.get("BTC/USDT"), broker, store, _risk(tmp_path), {})
    store.mark_open = real_mark_open
    assert next(iter(_state(tmp_path)["inflight"].values()))["state"] == "booked"

    broker2, _p, store2 = start(tmp_path, exchange)
    assert store2.get("BTC/USDT").status == "PENDING"  # the store never saw the fill
    with pytest.raises(LiveBrokerHalted, match="awaiting reconcile"):
        broker2.buy("BTC/USDT", 100.0, 50_000.0, order_ref=_plan_ref(store2, "BTC/USDT"))
    broker2.buy("ETH/USDT", 50.0, 2_000.0)  # other symbols unaffected: the outcome is known
    assert [o[0] for o in exchange.orders] == ["BTC/USDT", "ETH/USDT"]


def test_bot_passes_the_plan_ref_and_settles_after_the_store_update(tmp_path):
    broker, portfolio, store, exchange = _open_btc(tmp_path)
    record = store.get("BTC/USDT")
    assert exchange.client_ids == [client_order_id(f"BTC/USDT|{record.created_at}", "buy", 0)]
    assert _state(tmp_path)["inflight"] == {}  # settled once the plan was OPEN
    from src.bot import _execute_monitor_action

    _execute_monitor_action(_stop(), record, broker, store, _risk(tmp_path), {})
    assert exchange.client_ids[-1] == client_order_id(f"BTC/USDT|{record.created_at}", "sell", 0)
    assert _state(tmp_path)["inflight"] == {} and store.get("BTC/USDT") is None


@pytest.mark.parametrize("content", ["{not json", "[]", '{"version": 1, "inflight": {"x": 3}, "attempts": {}}'])
def test_corrupt_state_file_refuses_every_order_and_is_left_untouched(tmp_path, content):
    _state_file(tmp_path).parent.mkdir(parents=True)
    _state_file(tmp_path).write_text(content)
    exchange = FaultyExchange()
    positions = {"BTC/USDT": Position("BTC/USDT", 0.1, 50_000.0)}
    portfolio = Portfolio(cash=10_000.0, positions=positions, trade_log_path=logs_dir(tmp_path) / "trades.csv")
    broker = LiveBroker(exchange, portfolio, "live", True)
    assert broker.halted
    with pytest.raises(LiveBrokerHalted):
        broker.buy("ETH/USDT", 50.0, 2_000.0)
    with pytest.raises(LiveBrokerHalted, match="order state"):
        broker.sell("BTC/USDT", 55_000.0)
    assert exchange.orders == [] and _state_file(tmp_path).read_text() == content


def test_gate_still_checked_before_any_state_file_is_read_or_written(tmp_path):
    _state_file(tmp_path).parent.mkdir(parents=True)
    _state_file(tmp_path).write_text("{not json")
    for mode, confirmed in (("live", False), ("paper", True)):
        with pytest.raises(broker_live.LiveTradingDisabled):
            LiveBroker(FaultyExchange(), Portfolio(cash=1.0, trade_log_path=logs_dir(tmp_path) / "t.csv"), mode, confirmed)
    assert not _halt_file(tmp_path).exists()


# --- 3. sells capped at the real free balance (item 3) -------------------------------------


def _held_btc(tmp_path, amount=0.002, free=None, exchange=None):
    exchange = exchange or FaultyExchange()
    if free is not None:
        exchange.free["BTC"] = free
    positions = {"BTC/USDT": Position("BTC/USDT", amount, 50_000.0)}
    portfolio = Portfolio(cash=10_000.0, positions=positions, trade_log_path=logs_dir(tmp_path) / "trades.csv")
    return LiveBroker(exchange, portfolio, "live", True), portfolio, exchange


@pytest.mark.parametrize("free", [0.002, 0.5, 10.0])
def test_sell_never_sends_more_than_tracked(tmp_path, free):
    broker, portfolio, exchange = _held_btc(tmp_path, free=free)
    broker.sell("BTC/USDT", 55_000.0)
    assert exchange.orders == [("BTC/USDT", "sell", 0.002)]  # other coins in the account are never sold
    assert "BTC/USDT" not in portfolio.positions and not broker.halted


def test_sell_capped_at_free_balance_within_fee_tolerance_closes_fully(tmp_path):
    # Binance without BNB: the buy fee came out of the base asset, so 0.1% less is free.
    broker, portfolio, exchange = _held_btc(tmp_path, free=0.002 * 0.999)
    broker.sell("BTC/USDT", 55_000.0)
    assert exchange.orders == [("BTC/USDT", "sell", 0.002 * 0.999)]
    assert "BTC/USDT" not in portfolio.positions and not broker.halted
    assert portfolio.cash == pytest.approx(10_000.0 + 0.002 * 0.999 * 50_000.0)


def test_sell_capped_well_below_tracked_halts_first_then_sells_what_is_there(tmp_path):
    from src.broker_live import LivePartialFill

    # e.g. an unusable buy response booked the requested 0.002 but only 0.0005 really filled,
    # or a stale full amount after a restart following a partial sell.
    broker, portfolio, exchange = _held_btc(tmp_path, free=0.0005)
    with pytest.raises(LivePartialFill) as info:
        broker.sell("BTC/USDT", 55_000.0)
    assert exchange.orders == [("BTC/USDT", "sell", 0.0005)]  # protective exit still happened
    assert info.value.remaining_amount == pytest.approx(0.0015)
    assert portfolio.positions["BTC/USDT"].amount == pytest.approx(0.0015)
    assert broker.halted
    assert any("only 0.0005 is free" in a["title"] for a in broker.pop_alerts())


def test_sell_with_nothing_free_places_no_order_and_halts(tmp_path):
    broker, portfolio, exchange = _held_btc(tmp_path, free=0.0)
    with pytest.raises(LiveOrderUnreconciled, match="no order placed"):
        broker.sell("BTC/USDT", 55_000.0)
    assert exchange.orders == [] and portfolio.positions["BTC/USDT"].amount == 0.002 and broker.halted
    assert not _state_file(tmp_path).exists()  # refused before any write-ahead record


@pytest.mark.parametrize("fault", ["error", "missing", "nan", "negative"])
def test_unknown_free_balance_falls_back_to_the_tracked_amount(tmp_path, fault):
    broker, portfolio, exchange = _held_btc(tmp_path)
    if fault == "error":
        exchange.balance_error = ccxt.NetworkError("down")
    elif fault == "nan":
        exchange.free["BTC"] = float("nan")
    elif fault == "negative":
        exchange.free["BTC"] = -1.0
    broker.sell("BTC/USDT", 55_000.0)
    assert exchange.orders == [("BTC/USDT", "sell", 0.002)] and not broker.halted


def test_insufficient_funds_on_an_exit_is_alerted_not_silent(tmp_path):
    broker, portfolio, exchange = _held_btc(tmp_path)
    exchange.create_error = ccxt.InsufficientFunds("balance too low")
    for _cycle in range(2):
        with pytest.raises(ccxt.InsufficientFunds):
            broker.sell("BTC/USDT", 55_000.0)
    alerts = broker.pop_alerts()
    assert [a["kind"] for a in alerts] == ["exit_jammed"]  # once, not every cycle
    assert alerts[0]["symbol"] == "BTC/USDT" and alerts[0]["client_order_id"] == exchange.client_ids[0]
    assert portfolio.positions["BTC/USDT"].amount == 0.002 and _state(tmp_path)["inflight"] == {}


def test_base_fee_buy_then_exit_no_longer_jams(tmp_path):
    from src.bot import _execute_monitor_action

    exchange = FaultyExchange()
    exchange.track_balances = True
    exchange.fee = {"currency": "base", "rate": 0.001}
    broker, portfolio, store = start(tmp_path, exchange)
    store.set_pending("BTC/USDT", dict(PLAN))
    _execute_monitor_action(_fill(), store.get("BTC/USDT"), broker, store, _risk(tmp_path), {})
    _execute_monitor_action(_stop(), store.get("BTC/USDT"), broker, store, _risk(tmp_path), {})
    assert store.get("BTC/USDT") is None and "BTC/USDT" not in portfolio.positions
    assert exchange.free["BTC"] == pytest.approx(0.0, abs=1e-15)  # sold exactly what was there
    assert not broker.halted
