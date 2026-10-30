"""Architect crash-injection fuzz for #99 (from the review round, adapted to the fixed code; run in a
bounded-seed form by tests/test_live_fuzz.py). Fakes only, no network, no keys.

A process "crash" = SimulatedCrash (BaseException) raised immediately BEFORE or AFTER one persistence
step: order-state/halt-marker atomic write, trades.csv append, plan-store current.json write,
plan-store history append, or the exchange accepting the order. After a crash the broker, store and
portfolio are rebuilt from disk exactly like src.bot.build_components, and cycles continue
(reconcile first, then monitor actions, then new plans only when not halted). No human ever acts.

Invariants (violations are collected, not asserted, so every seed runs to the end):
  DUP_BUY      more than one filled exchange BUY for one plan
  DUP_COID     two exchange orders carrying the same clientOrderId
  LEDGER       not halted and no in-flight record, but ledger cash/positions != exchange balances
  DISK_CASH    not halted, no in-flight record, but reconstruct_cash(trades.csv) != exchange cash
  HALT_LOST    a halt that REACHED DISK in an earlier process (marker, pending_halt stash, or an order
               record's halt_reason), but a later process is not halted. (Adapted in the fix round: a
               halt that existed only in memory because EVERY write after it failed or crashed cannot
               survive a crash; those are counted as `halt_lost_memory_only` in the returned stats,
               not as a violation -- the order records themselves still block every affected symbol.)
  PLAN_HALTED  a PENDING plan created while the broker was halted (driver mirrors bot's gate)
  DOUBLE_ROW   one exchange order id appears in more than one trades.csv row
"""
from __future__ import annotations

import collections
import copy
import csv
import json
import math
import random
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import ccxt

import src.broker_live as bl
from src.bot import _execute_monitor_action, _reconcile_live_orders
from src.monitor import MonitorAction
from src.portfolio import Portfolio, reconstruct_cash
from src.trade_plan_store import TradePlanStore
from tests._live_fakes import SimulatedCrash, start

SYMS = ["BTC/USDT", "ETH/USDT"]
START_CASH = 10_000.0


class Book:
    """A spot exchange: balances are enforced, every accepted order is kept."""

    def __init__(self, rng: random.Random, kraken_zero_omit: bool):
        self.rng = rng
        self.bal = collections.defaultdict(float, {"USDT": START_CASH})
        self.orders: list[dict] = []
        self.prices = {"BTC/USDT": 50_000.0, "ETH/USDT": 2_000.0}
        self.kraken = kraken_zero_omit
        self.fault = None          # set per create by the driver
        self.lookup_fail_p = 0.0
        self.state_path: Path | None = None
        self.hook = None           # crash hook around "exchange accepted"

    def _plan_ref_of(self, coid):
        try:
            return json.loads(self.state_path.read_text())["inflight"][coid]["plan_ref"]
        except Exception:
            return None

    def create_market_order(self, symbol, side, amount, client_order_id=None):
        f = self.fault or {}
        base = symbol.split("/")[0]
        price = self.prices[symbol]
        if f.get("definite"):
            raise ccxt.InvalidOrder("rejected")
        if f.get("timeout_before"):
            raise ccxt.RequestTimeout("t/o before reaching the exchange")
        if side == "sell" and amount > self.bal[base] * (1 + 1e-12):
            raise ccxt.InsufficientFunds(f"have {self.bal[base]}, sell {amount}")
        ratio = f.get("ratio", 1.0)
        filled = amount * ratio
        fee = None
        if f.get("fee") == "quote":
            fee = {"currency": "USDT", "cost": filled * price * 0.001}
        elif f.get("fee") == "base" and side == "buy":
            fee = {"currency": base, "cost": filled * 0.001}
        cost = filled * price
        if side == "buy" and cost + (fee["cost"] if fee and fee["currency"] == "USDT" else 0) > self.bal["USDT"]:
            raise ccxt.InsufficientFunds("cash")
        oid = f"o{len(self.orders) + 1}"
        order = {"id": oid, "clientOrderId": client_order_id, "symbol": symbol, "side": side, "status": "closed",
                 "amount": amount, "filled": filled, "remaining": amount - filled, "average": price, "price": None,
                 "cost": cost, "plan_ref": self._plan_ref_of(client_order_id)}
        if fee:
            order["fee"] = dict(fee)
            order["fees"] = [dict(fee)]
        if self.hook:
            self.hook("exchange", "before")
        # the exchange executes it
        self.orders.append(order)
        if side == "buy":
            self.bal["USDT"] -= cost
            self.bal[base] += filled
        else:
            self.bal["USDT"] += cost
            self.bal[base] -= filled
        if fee:
            self.bal[fee["currency"]] -= fee["cost"]
        if self.hook:
            self.hook("exchange", "after")
        if f.get("timeout_after"):
            raise ccxt.RequestTimeout("t/o after the exchange executed it")
        resp = {k: v for k, v in order.items() if k != "plan_ref"}
        if f.get("unusable"):
            resp["filled"] = float("nan")
        if f.get("idless"):
            resp["id"] = None
        return copy.deepcopy(resp)

    def fetch_free_balance(self, asset):
        v = self.bal.get(asset, 0.0)
        if self.kraken and v <= 1e-15:
            return None
        return v

    def amount_to_precision(self, symbol, amount):
        raise ccxt.NotSupported("no precision")

    def find_order(self, symbol, client_order_id, exchange_order_id=None, since_ms=None, side=None):
        if self.rng.random() < self.lookup_fail_p:
            raise ccxt.NetworkError("lookup down")
        clean = lambda o: {k: v for k, v in o.items() if k != "plan_ref"}  # noqa: E731
        if exchange_order_id is not None:
            for o in self.orders:
                if o["id"] == exchange_order_id:
                    return copy.deepcopy(clean(o))
            raise ccxt.OrderNotFound(exchange_order_id)
        m = [o for o in self.orders if o["symbol"] == symbol and o["clientOrderId"] == client_order_id]
        if len(m) > 1:
            raise RuntimeError("ambiguous")
        return copy.deepcopy(clean(m[0])) if m else None


class Injector:
    def __init__(self, rng, p, perr=0.0):
        self.rng, self.p, self.perr, self.n, self.armed = rng, p, perr, 0, True
        self.log = []

    def __call__(self, kind, when):
        self.n += 1
        if not self.armed:
            return
        r = self.rng.random()
        if r < self.p:
            self.log.append((self.n, kind, when))
            raise SimulatedCrash(f"{kind} {when}")
        if when == "before" and kind != "exchange" and r < self.p + self.perr:
            self.log.append((self.n, kind, "OSError"))
            raise OSError(f"EIO {kind}")


class _Risk:
    def record_trade(self, *a, **k):
        pass


PLAN = {"approved_quote_amount": 100.0, "entry_zone": (0, 1e12), "invalidation": 0, "stop_loss": 1, "take_profit": 1e12}


def run_seed(seed: int, cycles: int = 40, crash_p: float = 0.04, err_p: float = 0.0, clean_w: int = 3, verbose=False):
    rng = random.Random(seed)
    tmp = Path(tempfile.mkdtemp(prefix=f"fz{seed}_"))
    logs = tmp / "logs"
    book = Book(rng, kraken_zero_omit=rng.random() < 0.5)
    book.state_path = logs / bl.ORDER_STATE_NAME
    book.lookup_fail_p = rng.choice([0.0, 0.0, 0.2])
    inj = Injector(rng, crash_p, err_p)
    book.hook = inj
    violations = collections.Counter()
    details = []
    ever_halted_durably_expected = False
    ever_halted_in_memory = False
    memory_only_lost = [0]

    def halt_on_disk() -> bool:
        if (logs / bl.HALT_MARKER_NAME).exists():
            return True
        try:
            st = json.loads(book.state_path.read_text())
        except Exception:  # noqa: BLE001
            return False
        return bool(st.get("pending_halt")) or any(e.get("halt_reason") for e in st.get("inflight", {}).values())
    restarts = 0

    real_atomic = bl._atomic_write_json
    real_log = Portfolio.log_trade
    real_persist = TradePlanStore._persist_current
    real_hist = TradePlanStore._append_history

    def w_atomic(path, payload):
        inj("state" if path.name == bl.ORDER_STATE_NAME else "halt", "before")
        real_atomic(path, payload)
        inj("state" if path.name == bl.ORDER_STATE_NAME else "halt", "after")

    def w_log(self, row):
        inj("csv", "before"); real_log(self, row); inj("csv", "after")  # noqa: E702

    def w_persist(self, *args):  # TradePlanStore._persist_current(new_state) since the review (copy-on-write)
        inj("store", "before"); real_persist(self, *args); inj("store", "after")  # noqa: E702

    def w_hist(self, r):
        inj("hist", "before"); real_hist(self, r); inj("hist", "after")  # noqa: E702

    def violate(kind, msg):
        violations[kind] += 1
        if len(details) < 6:
            details.append((kind, msg, list(inj.log)))

    def check_after_reconcile(broker, portfolio, store):
        st = json.loads(book.state_path.read_text()) if book.state_path.exists() else {"inflight": {}}
        # DUP_BUY / DUP_COID
        buys = collections.Counter(o["plan_ref"] for o in book.orders if o["side"] == "buy" and o["filled"] > 0)
        for ref, n in buys.items():
            if n > 1:
                violate("DUP_BUY", f"{ref} x{n}")
        coids = collections.Counter(o["clientOrderId"] for o in book.orders)
        for c, n in coids.items():
            if n > 1:
                violate("DUP_COID", f"{c} x{n}")
        tl = logs / "trades.csv"
        if tl.exists():
            with open(tl, newline="") as f:
                ids = collections.Counter(r.get("order_id") for r in csv.DictReader(f))
            for i, n in ids.items():
                if i not in (None, "None") and n > 1:
                    violate("DOUBLE_ROW", f"{i} x{n}")
        if broker.halted or st.get("inflight"):
            return
        if not math.isclose(portfolio.cash, book.bal["USDT"], rel_tol=1e-9, abs_tol=1e-6):
            violate("LEDGER", f"cash {portfolio.cash} vs exchange {book.bal['USDT']}")
        disk = reconstruct_cash(tl, START_CASH)
        if not math.isclose(disk, book.bal["USDT"], rel_tol=1e-9, abs_tol=1e-6):
            violate("DISK_CASH", f"trades.csv cash {disk} vs exchange {book.bal['USDT']}")
        for s in SYMS:
            b = s.split("/")[0]
            mine = portfolio.positions[s].amount if s in portfolio.positions else 0.0
            if not math.isclose(mine, book.bal[b], rel_tol=1e-9, abs_tol=1e-12):
                violate("LEDGER", f"{s} ledger {mine} vs exchange {book.bal[b]}")

    with patch.object(bl, "_atomic_write_json", w_atomic), patch.object(Portfolio, "log_trade", w_log), \
            patch.object(TradePlanStore, "_persist_current", w_persist), \
            patch.object(TradePlanStore, "_append_history", w_hist), \
            patch.object(bl, "RECONCILE_NOT_FOUND_GRACE_SECONDS", 0):
        c = 0
        while c < cycles:
            try:
                inj.armed = False  # crash injection only inside cycles, not during the rebuild itself
                broker, portfolio, store = start(tmp, book, START_CASH)
                if ever_halted_durably_expected and not broker.halted:
                    violate("HALT_LOST", f"restart #{restarts} after a halt is not halted")
                elif ever_halted_in_memory and not broker.halted:
                    memory_only_lost[0] += 1
                inj.armed = True
                while c < cycles:
                    c += 1
                    for s in SYMS:  # price random walk
                        book.prices[s] *= math.exp(rng.gauss(0, 0.01))
                    _reconcile_live_orders(broker, store)
                    inj.armed = False
                    check_after_reconcile(broker, portfolio, store)
                    inj.armed = True
                    for s in SYMS:
                        rec = store.get(s)
                        book.fault = rng.choice([{}] * clean_w + [
                            {"fee": "quote"}, {"fee": "base"}, {"ratio": rng.uniform(0.3, 0.9)},
                            {"definite": True}, {"timeout_before": True}, {"timeout_after": True},
                            {"unusable": True}, {"idless": True},
                        ])
                        try:
                            if rec is None:
                                if broker.halted:
                                    continue
                                store.set_pending(s, dict(PLAN))
                                if broker.halted:
                                    violate("PLAN_HALTED", s)
                            elif rec.status == "PENDING" and rng.random() < 0.7:
                                _execute_monitor_action(MonitorAction(s, "FILL", book.prices[s], "z"), rec, broker,
                                                        store, _Risk(), {})
                            elif rec.status == "OPEN" and rng.random() < 0.5:
                                _execute_monitor_action(MonitorAction(s, "STOP_LOSS", book.prices[s], "z"), rec,
                                                        broker, store, _Risk(), {})
                        except SimulatedCrash:
                            raise
                        except Exception:  # noqa: BLE001 -- bot's per-symbol except
                            pass
                        if broker._halt_reason is not None:
                            ever_halted_in_memory = True
                            if halt_on_disk():
                                ever_halted_durably_expected = True
            except SimulatedCrash:
                restarts += 1
                if broker._halt_reason is not None:
                    ever_halted_in_memory = True
                    if halt_on_disk():
                        ever_halted_durably_expected = True
                continue
        inj.armed = False
        broker, portfolio, store = start(tmp, book, START_CASH)
        _reconcile_live_orders(broker, store)
        check_after_reconcile(broker, portfolio, store)
    run_seed.last_memory_only_lost = memory_only_lost[0]  # a statistic, not a violation (see docstring)
    return violations, details, restarts, len(book.orders)


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    cp, ep, cw = float(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4])
    total = collections.Counter()
    examples = collections.defaultdict(list)
    orders = restarts = 0
    for seed in range(n):
        v, d, r, o = run_seed(seed, crash_p=cp, err_p=ep, clean_w=cw)
        restarts += r
        orders += o
        for k in v:
            total[k] += 1  # seeds affected
        for kind, msg, log in d:
            if len(examples[kind]) < 3:
                examples[kind].append((seed, msg, log[-3:]))
    print(f"seeds={n} restarts={restarts} exchange_orders={orders}")
    print("seeds with violations:", dict(total))
    for k, ex in examples.items():
        for e in ex:
            print(" ", k, e)
