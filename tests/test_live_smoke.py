"""End-to-end smoke for issue #99 (the repo's manual broker smoke pattern, as a test): a fake ccxt
client feeds the P1b synthetic OHLCV series through the REAL run_cycle across many ticks with a
LiveBroker (built directly on the paper-mode components -- config mode stays 'paper', no env var,
no keys, no network). The process "dies" (SimulatedCrash) right after an order reached the fake
exchange, is rebuilt from disk like src.bot.build_components does, and keeps ticking.
"""
from __future__ import annotations

import copy
import csv

import pytest

from src import broker_live
from src.bot import run_cycle
from src.config import load_config
from src.portfolio import reconstruct_cash
from tests import test_bot_universe_wiring as wiring
from tests._live_fakes import SimulatedCrash


class LiveFakeCcxt(wiring.FakeCcxt):
    """wiring.FakeCcxt + the order/balance side of ccxt: fills every market order at the last
    closed hourly close, keeps an order book (with clientOrderId) and free balances."""

    has = {"fetchOrders": True}

    def __init__(self, hourly, daily, crash_at: int | None = None) -> None:
        super().__init__(hourly, daily)
        self.creates: list[tuple[str, str, float, str | None]] = []
        self.book: list[dict] = []
        self.free: dict[str, float] = {}
        self.crash_at = crash_at

    def _last_close(self, symbol):
        return [r for r in self.hourly[symbol] if r[0] + wiring.H_MS <= self.now_ms][-1][4]

    def create_order(self, symbol, type, side, amount, price=None, params=None):  # noqa: A002 -- ccxt's signature
        coid = (params or {}).get("clientOrderId")
        self.creates.append((symbol, side, amount, coid))
        order = {"id": f"o{len(self.creates)}", "clientOrderId": coid, "symbol": symbol, "side": side,
                 "status": "closed", "amount": amount, "filled": amount, "remaining": 0.0,
                 "average": self._last_close(symbol), "price": None, "timestamp": self.now_ms}
        self.book.append(order)
        base = symbol.split("/")[0]
        self.free[base] = self.free.get(base, 0.0) + (amount if side == "buy" else -amount)
        if self.crash_at is not None and len(self.creates) == self.crash_at:
            raise SimulatedCrash("process killed right after the order reached the exchange")
        return copy.deepcopy(order)

    def fetch_orders(self, symbol, since=None):
        return [copy.deepcopy(o) for o in self.book if o["symbol"] == symbol]

    def fetch_balance(self):
        return {"free": dict(self.free)}


@pytest.fixture(autouse=True)
def _replay_isolation(monkeypatch):
    monkeypatch.setattr(wiring.htf_filter, "_cache", {})
    monkeypatch.setattr("src.risk_engine.datetime", wiring._FakeDatetime)
    monkeypatch.setattr(broker_live, "RECONCILE_NOT_FOUND_GRACE_SECONDS", 0)
    wiring._Clock.now = wiring.datetime.fromtimestamp(wiring.T0_MS / 1000, tz=wiring.timezone.utc)


def _boot(raw, fake):
    cfg, parts, events = wiring._components(raw, fake)
    exchange, risk_engine, portfolio, _paper, store, audit, market_data = parts
    broker = broker_live.LiveBroker(exchange, portfolio, "live", True)
    return cfg, (exchange, risk_engine, portfolio, broker, store, audit, market_data), events


def _replay(tmp_path, crash_at=None):
    real_raw = wiring._pre_trial_hourly_raw(load_config().raw)
    symbols = list(real_raw["market"]["symbols"])
    hourly = {s: wiring._random_walk_hourly(seed=i, start_price=10.0 * (i + 1)) for i, s in enumerate(symbols)}
    daily = {s: wiring._daily(10.0 * (i + 1), uptrend=(i % 4 != 3)) for i, s in enumerate(symbols)}
    fake = LiveFakeCcxt(hourly, daily, crash_at)
    raw = wiring._isolated_raw(real_raw, tmp_path)
    cfg, parts, events = _boot(raw, fake)
    runs = [events]
    crashed_at_bar = None
    bar = wiring.FIRST_TICK_BAR
    while bar <= wiring.LAST_TICK_BAR:
        wiring._tick_to(fake, bar)
        try:
            run_cycle(cfg, *parts)
        except SimulatedCrash:
            crashed_at_bar = bar
            fake.crash_at = None
            cfg, parts, events = _boot(raw, fake)  # restart from disk, same exchange
            runs.append(events)
            continue  # the same bar again, like a supervisor restart
        bar += 1
    return fake, parts, runs, crashed_at_bar


def _assert_ledger_matches_exchange(tmp_path, fake, parts):
    portfolio, store = parts[2], parts[4]
    held = {s.split("/")[0]: p.amount for s, p in portfolio.positions.items()}
    for base, free in fake.free.items():
        assert held.get(base, 0.0) == pytest.approx(free, abs=1e-9), base
    assert reconstruct_cash(tmp_path / "trades.csv", 10_000.0) == pytest.approx(portfolio.cash)
    open_records = {s: r.fill_amount for s, r in store.all_current().items() if r.status == "OPEN"}
    assert {s: p.amount for s, p in portfolio.positions.items()} == pytest.approx(open_records)


def test_smoke_control_run_trades_and_ledger_matches_exchange(tmp_path):
    fake, parts, runs, crashed = _replay(tmp_path)
    assert crashed is None and not parts[3].halted
    sides = [c[1] for c in fake.creates]
    assert sides.count("buy") >= 3 and sides.count("sell") >= 2  # not vacuous
    coids = [c[3] for c in fake.creates]
    assert None not in coids and len(set(coids)) == len(coids)
    _assert_ledger_matches_exchange(tmp_path, fake, parts)


def test_smoke_crash_after_order_restart_reconciles_without_duplicates(tmp_path):
    fake, parts, runs, crashed = _replay(tmp_path, crash_at=1)
    assert crashed is not None and len(runs) == 2, "fixture assumption: the first order crashed the process"
    crashed_symbol, crashed_side, _amount, crashed_coid = fake.creates[0]
    assert crashed_side == "buy"
    # Never a duplicate: every clientOrderId was sent exactly once, and no second buy of the
    # crashed symbol happened during the bar it crashed on.
    coids = [c[3] for c in fake.creates]
    assert len(set(coids)) == len(coids)
    # The crashed order was found by the reconcile and booked exactly once.
    with open(tmp_path / "trades.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["order_id"] for r in rows].count("o1") == 1
    # After the restart the broker stays halted: no new plans, no new buys...
    after = runs[1]
    assert parts[3].halted
    assert not any(e.get("action") == "planned" for e in after)
    assert any("live broker halted" in e.get("note", "") for e in after)
    assert all(side == "sell" for _s, side, _a, _c in fake.creates[1:])
    # ...but the reconciled position got its plan OPENED (so its stop-loss/target protect it)
    # with the exchange's fill, and it is monitored after the restart.
    import json

    history = [json.loads(x) for x in (tmp_path / "trade_plans_history.jsonl").read_text().splitlines()]
    opened = [h for h in history if h["symbol"] == crashed_symbol and h["status"] == "OPEN"]
    assert len(opened) == 1 and opened[0]["fill_amount"] == pytest.approx(_amount)
    assert any(e["symbol"] == crashed_symbol and "PENDING" not in e.get("note", "") for e in after)
    _assert_ledger_matches_exchange(tmp_path, fake, parts)
