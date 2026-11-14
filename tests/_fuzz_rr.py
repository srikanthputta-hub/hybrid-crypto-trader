"""Risk Officer fault-injection fuzz for claude/live-readiness-99 (from the review round, adapted to the
fixed code; run in a bounded-seed form by tests/test_live_fuzz.py). Fakes only, no network, no keys.

A fake ccxt client with a real order book + balances sits behind the REAL src.exchange.ExchangeClient
(so find_order / fetch_free_balance / amount_to_precision are the branch's code). The bot-level flow
(src.bot._execute_monitor_action / _reconcile_live_orders) drives src.broker_live.LiveBroker. Faults:
create_order (timeouts before/after booking, crashes before/after booking, garbage responses,
partial fills, fees in base/quote/BNB), lookups/balances (errors, omitted assets), persistence
(broker state writes, trades.csv, plan store: OSError or SIGKILL-stand-in before/after the write).

Usage: PYTHONPATH=<worktree> python fuzz_rr99.py [episodes] [cycles] [seed0]
"""
from __future__ import annotations

import collections
import json
import math
import random
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

import ccxt

import src.broker_live as bl
import src.portfolio as pf
import src.trade_plan_store as tps
from src.bot import _execute_monitor_action, _reconcile_live_orders
from src.exchange import ExchangeClient
from src.monitor import MonitorAction
from src.portfolio import Portfolio, Position, reconstruct_cash
from src.risk_engine import RiskEngine
from src.trade_plan_store import TradePlanStore

SYMS = ["BTC/USDT", "ETH/USDT"]
import os
DEBUG = os.environ.get("RR_DEBUG") == "1"
TRACE = os.environ.get("RR_TRACE") == "1"
DEBUG_EXC = set(os.environ.get("RR_DEBUG_EXC", "KeyError").split(","))
START_USDT = 10_000.0
LOT_STEP = 1e-8  # the fake exchange's amount precision (amount_to_precision truncates to it)


class Crash(BaseException):
    pass


class FakeCcxt:
    """Kraken-like: fetchOpenOrders + fetchClosedOrders (canceled orders listed as closed)."""

    def __init__(self, rng: random.Random, p: dict):
        self.rng, self.p = rng, p
        self.has = {"fetchOpenOrders": True, "fetchClosedOrders": True}
        self.orders: list[dict] = []
        self.bal = collections.defaultdict(float, {"USDT": START_USDT})
        self.prices = {"BTC/USDT": 50_000.0, "ETH/USDT": 2_000.0}
        self.n = 0
        self.log: list[tuple] = []
        self.fault_on = True

    # -- helpers
    def _f(self, name):
        hit = self.fault_on and self.rng.random() < self.p.get(name, 0.0)
        if hit and TRACE:
            print("   [exchange fault]", name)
        return hit

    def load_markets(self):
        return {}

    def amount_to_precision(self, symbol, amount):
        return f"{math.floor(float(amount) * 1e8) / 1e8:.8f}"  # lot step LOT_STEP = 1e-8

    # -- trading
    def create_order(self, symbol, type, side, amount, params=None):
        coid = (params or {}).get("clientOrderId")
        amount = float(self.amount_to_precision(symbol, amount))
        self.log.append(("create", symbol, side, amount, coid))
        if self._f("crash_before"):
            raise Crash("killed before the request left")
        if self._f("timeout_before"):
            raise ccxt.RequestTimeout("timeout (request never reached the matching engine)")
        if amount < 1e-6:
            raise ccxt.InvalidOrder("EOrder:Order minimum not met (dust)")
        if self._f("reject"):
            raise ccxt.InvalidOrder("EOrder:Order minimum not met")
        base, quote = symbol.split("/")
        price = self.prices[symbol] * (1 + self.rng.uniform(-0.002, 0.002))
        ratio = 1.0
        status = "closed"
        if self._f("partial"):
            ratio, status = self.rng.choice([0.0, self.rng.uniform(0.2, 0.95)]), "canceled"
        filled = amount * ratio
        fee_kind = self.rng.choice(["none", "quote", "quote", "base", "BNB"]) if self.fault_on else "quote"
        if side == "sell" and fee_kind == "base":
            fee_kind = "quote"
        fee = None
        if filled and fee_kind != "none":
            if fee_kind == "base":
                fee = {"currency": base, "cost": filled * 0.0026}
            elif fee_kind == "quote":
                fee = {"currency": quote, "cost": filled * price * 0.0026}
            else:
                fee = {"currency": "BNB", "cost": 0.0001}
        # balance enforcement (dedicated account)
        if side == "buy":
            need = filled * price + (fee["cost"] if fee and fee["currency"] == quote else 0.0)
            if need > self.bal[quote] + 1e-9:
                raise ccxt.InsufficientFunds("EOrder:Insufficient funds")
        else:
            if amount > self.bal[base] + 1e-12:
                raise ccxt.InsufficientFunds("EOrder:Insufficient funds")
        self.n += 1
        order = {"id": f"O{self.n}", "clientOrderId": coid, "symbol": symbol, "side": side, "type": "market",
                 "status": status if filled or status != "closed" else "canceled", "amount": amount,
                 "filled": filled, "remaining": amount - filled, "average": price if filled else None,
                 "price": None, "cost": filled * price, "timestamp": int(time.time() * 1000),
                 "fee": fee, "fees": [fee] if fee else []}
        if filled == 0:
            order["status"] = "canceled"
        # settle balances
        if side == "buy":
            self.bal[quote] -= filled * price
            self.bal[base] += filled
        else:
            self.bal[base] -= filled
            self.bal[quote] += filled * price
        if fee:
            self.bal[fee["currency"]] -= fee["cost"]
        self.orders.append(order)
        if TRACE:
            print(f"   [exchange] {side} {symbol} amt={amount} filled={filled:.8f} st={order['status']} fee={fee} coid={coid} bal={dict(self.bal)}")
        if self._f("timeout_after"):
            raise ccxt.RequestTimeout("timeout after the order was accepted")
        if self._f("bare_error_after"):
            raise ccxt.ExchangeError("EGeneral:Internal error (unmapped)")
        if self._f("crash_after"):
            raise Crash("killed after the order reached the exchange")
        resp = json.loads(json.dumps(order))
        if self._f("ack_only"):
            # RO round 2 (N1): Kraken's create_order answers with the txid only -- no fill, no
            # status. The broker must look the order up (and book it unverified if it can't).
            return {"id": order["id"], "info": {"txid": [order["id"]]}}
        if self._f("garbage"):
            resp = self._corrupt(resp)
        return resp

    def _corrupt(self, r):
        kind = self.rng.choice(["noid", "nan", "str", "neg", "over", "inf", "list", "nofilled", "zero",
                                "amount_bad", "remaining_bad", "none"])
        if kind == "noid":
            r.pop("id", None)
        elif kind == "nan":
            r["filled"] = float("nan")
        elif kind == "str":
            r["filled"] = str(r["filled"])
        elif kind == "neg":
            r["filled"] = -1.0
        elif kind == "over":
            r["filled"] = r["amount"] * 10
        elif kind == "inf":
            r["average"] = float("inf")
        elif kind == "list":
            return [r]
        elif kind == "nofilled":
            r.pop("filled", None); r.pop("remaining", None); r.pop("amount", None)
        elif kind == "zero":
            r["filled"] = 0
        elif kind == "amount_bad":
            r["amount"] = "x"
        elif kind == "remaining_bad":
            r["remaining"] = float("nan")
        elif kind == "none":
            return None
        return r

    # -- reads
    def fetch_order(self, id, symbol=None):
        if self._f("lookup_err"):
            raise ccxt.NetworkError("lookup failed")
        for o in self.orders:
            if o["id"] == id:
                return json.loads(json.dumps(o))
        raise ccxt.OrderNotFound(id)

    def _list(self, symbol, since, statuses):
        if self._f("lookup_err"):
            raise ccxt.NetworkError("listing failed")
        out = [json.loads(json.dumps(o)) for o in self.orders
               if o["symbol"] == symbol and o["status"] in statuses and (since is None or o["timestamp"] >= since)]
        return out

    def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        return self._list(symbol, since, ("open",))

    def fetch_closed_orders(self, symbol=None, since=None, limit=None, params=None):
        return self._list(symbol, since, ("closed", "canceled", "expired", "rejected"))

    def fetch_balance(self):
        if self._f("balance_err"):
            raise ccxt.NetworkError("balance failed")
        free = {k: v for k, v in self.bal.items() if abs(v) > 1e-15}  # Kraken omits zero balances
        if self._f("balance_omit"):
            free = {}
        return {"free": free}


def make_exchange_client(fake) -> ExchangeClient:
    ec = object.__new__(ExchangeClient)
    ec.client, ec.exchange_id = fake, "fakekraken"
    return ec


class Faults:
    """Persistence fault injection, installed by monkeypatching module attributes."""

    def __init__(self, rng, p):
        self.rng, self.p, self.on = rng, p, True
        self.real_atomic = bl._atomic_write_json
        self.real_log = pf.Portfolio.log_trade
        self.real_persist = tps.TradePlanStore._persist_current
        self.real_hist = tps.TradePlanStore._append_history

    def _maybe(self, where, fn):
        if self.on:
            r = self.rng.random()
            if r < self.p.get(where + "_oserr", 0):
                TRACE and print("   [disk fault] OSError", where)
                raise OSError(f"{where}: EIO")
            if r < self.p.get(where + "_oserr", 0) + self.p.get(where + "_crash_before", 0):
                TRACE and print("   [disk fault] crash before", where)
                raise Crash(f"killed before {where}")
        out = fn()
        if self.on and self.rng.random() < self.p.get(where + "_crash_after", 0):
            TRACE and print("   [disk fault] crash after", where)
            raise Crash(f"killed after {where}")
        return out

    def install(self):
        f = self
        bl._atomic_write_json = lambda path, payload: f._maybe(
            "halt" if path.name == bl.HALT_MARKER_NAME else "state", lambda: f.real_atomic(path, payload))
        pf.Portfolio.log_trade = lambda self_, row: f._maybe("trades", lambda: f.real_log(self_, row))
        tps.TradePlanStore._persist_current = lambda self_, *a: f._maybe("store", lambda: f.real_persist(self_, *a))
        tps.TradePlanStore._append_history = lambda self_, rec: f._maybe("hist", lambda: f.real_hist(self_, rec))

    def uninstall(self):
        bl._atomic_write_json = self.real_atomic
        pf.Portfolio.log_trade = self.real_log
        tps.TradePlanStore._persist_current = self.real_persist
        tps.TradePlanStore._append_history = self.real_hist


def build(root: Path, ec):
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    store = TradePlanStore(logs / "trade_plans_current.json", logs / "trade_plans_history.jsonl")
    trade_log = logs / "trades.csv"
    cash = reconstruct_cash(trade_log, START_USDT)
    positions = {s: Position(s, r.fill_amount, r.fill_price) for s, r in store.all_current().items()
                 if r.status == "OPEN" and r.fill_amount and r.fill_price}
    portfolio = Portfolio(cash=cash, positions=positions, trade_log_path=trade_log)
    broker = bl.LiveBroker(ec, portfolio, "live", True)
    broker._sleep = lambda seconds: None  # the bounded verify re-poll never really sleeps here
    return broker, portfolio, store


def risk(root):
    return RiskEngine(starting_cash_usd=START_USDT, max_position_pct=0.1, max_total_exposure_pct=0.4,
                      max_loss_per_trade_pct=0.01, max_daily_loss_pct=0.05, max_volatility_pct=0.08,
                      min_risk_reward=1.5, cooldown_minutes=0, kill_switch_path=root / "KILL_SWITCH",
                      symbol_whitelist=SYMS)


def trade_rows(root):
    p = root / "logs" / "trades.csv"
    if not p.exists():
        return []
    import csv
    with open(p, newline="") as f:
        return list(csv.DictReader(f))


def run_episode(seed: int, cycles: int, p: dict) -> dict:
    rng = random.Random(seed)
    root = Path(tempfile.mkdtemp(prefix="rr99_"))
    fake = FakeCcxt(rng, p)
    ec = make_exchange_client(fake)
    faults = Faults(rng, p)
    faults.install()
    violations: list[str] = []
    stats = collections.Counter()
    plan_refs: set[str] = set()
    marker = root / "logs" / bl.HALT_MARKER_NAME
    marker_seen = False
    try:
        broker = portfolio = store = None
        while broker is None:
            try:
                broker, portfolio, store = build(root, ec)
            except Crash:
                stats["crash_at_start"] += 1
        re = risk(root)
        for cyc in range(cycles):
            for s in SYMS:
                fake.prices[s] *= 1 + rng.uniform(-0.03, 0.03)
            try:
                try:
                    _reconcile_live_orders(broker, store)
                    al = broker.pop_alerts()
                    if TRACE:
                        for a in al:
                            print(f"   [alert] {a.get('kind')}: {str(a.get('title'))[:160]}")
                    for s in SYMS:
                        rec = store.get(s)
                        price = fake.prices[s]
                        try:
                            if rec is None:
                                if not broker.halted:
                                    r = store.set_pending(s, {"approved_quote_amount": rng.choice([50.0, 100.0, 150.0]),
                                                              "entry_zone": (0, 1e12), "invalidation": 0.0,
                                                              "stop_loss": 0.0, "take_profit": 1e12})
                                    plan_refs.add(f"{s}|{r.created_at}")
                                    stats["plans"] += 1
                            elif rec.status == "PENDING":
                                x = rng.random()
                                act = "FILL" if x < 0.75 else ("DISCARD" if x < 0.85 else None)
                                if act:
                                    TRACE and print(f"cyc {cyc} {s} {act} cash={portfolio.cash:.2f} pos={ {k: v.amount for k, v in portfolio.positions.items()} }")
                                    _execute_monitor_action(MonitorAction(s, act, price, "fuzz"), rec, broker, store, re, {})
                            elif rec.status == "OPEN":
                                if rng.random() < 0.45:
                                    TRACE and print(f"cyc {cyc} {s} EXIT cash={portfolio.cash:.2f} pos={ {k: v.amount for k, v in portfolio.positions.items()} }")
                                    _execute_monitor_action(MonitorAction(s, rng.choice(["STOP_LOSS", "TAKE_PROFIT"]), price, "fuzz"),
                                                            rec, broker, store, re, {})
                        except Crash:
                            raise
                        except Exception as exc:  # noqa: BLE001 -- bot's per-symbol error path
                            stats["err:" + type(exc).__name__] += 1
                            TRACE and print(f"   -> {type(exc).__name__}: {str(exc)[:200]}")
                            if DEBUG and type(exc).__name__ in DEBUG_EXC:
                                print(f"[seed {seed} cyc {cyc} {s} {rec.status if rec else None}] {type(exc).__name__}: {exc}")
                                traceback.print_exc(limit=-3)
                except Crash:
                    TRACE and print(f"cyc {cyc} *** CRASH -> restart")
                    stats["crash"] += 1
                    broker = None
                    while broker is None:
                        try:
                            broker, portfolio, store = build(root, ec)
                        except Crash:
                            stats["crash_at_start"] += 1
                        except Exception as exc:  # store json torn etc.
                            violations.append(f"cycle {cyc}: restart failed: {type(exc).__name__}: {exc}")
                            raise StopIteration
            except StopIteration:
                break
            # ---- invariants after each cycle
            if marker.exists():
                marker_seen = True
            elif marker_seen:
                violations.append(f"cycle {cyc}: HALT MARKER DISAPPEARED")
            check_orders(fake, plan_refs, violations, cyc)
        # ---- final: faults off, restart, reconcile twice, compare ledger with the exchange
        faults.on = False
        fake.fault_on = False
        mem = (portfolio.cash, {s: p_.amount for s, p_ in portfolio.positions.items()}) if portfolio else None
        broker, portfolio, store = build(root, ec)
        disk = (portfolio.cash, {s: p_.amount for s, p_ in portfolio.positions.items()})
        if mem is not None and not _inflight(root):
            if abs(mem[0] - disk[0]) > 1e-6 or any(abs(mem[1].get(s, 0) - disk[1].get(s, 0)) > 1e-12 for s in SYMS):
                violations.append(f"in-memory ledger != restart ledger with NO inflight record: mem={mem} disk={disk}")
        for _ in range(2):
            _reconcile_live_orders(broker, store)
        rows = trade_rows(root)
        per_id = collections.Counter(r["order_id"] for r in rows)
        fills = {o["id"]: o for o in fake.orders}
        for oid, n in per_id.items():
            if oid in ("None", "", None):
                stats["rows_without_order_id"] += n
                continue
            if n > 1:
                violations.append(f"DOUBLE BOOKING in trades.csv: order {oid} booked {n} times")
            if oid.startswith("corr:"):  # N1 correction row: folded into its order below
                stats["unverified_corrected"] += 1
                if not any(x["clientOrderId"] == oid[5:] for x in fake.orders):
                    violations.append(f"trades.csv corrects order {oid} that the exchange never had")
                continue
            o = fills.get(oid) or next((x for x in fake.orders if f"coid:{x['clientOrderId']}" == oid), None)
            if o is None:
                violations.append(f"trades.csv books order {oid} that the exchange never had")
            else:
                corr = f"corr:{o['clientOrderId']}"
                booked = sum(float(r["base_amount"]) for r in rows if r["order_id"] in (oid, corr))
                if booked > o["filled"] * (1 + 1e-9) + 1e-12:
                    if booked - o["filled"] < LOT_STEP:
                        # N1: an unverified fallback booked the unrounded request; the exchange
                        # truncated it to its lot step. Less than one lot = a match (entry cleared).
                        stats["overbooked_within_one_lot"] += 1
                    elif o["clientOrderId"] in _unverified(root) and halted_now(root):
                        stats["overbooked_flagged_unverified"] += 1
                    else:
                        violations.append(f"OVER-BOOKED order {oid}: booked {booked} > filled {o['filled']}")
        for o in fake.orders:
            if o["filled"] > 0 and per_id.get(o["id"], 0) == 0 and per_id.get(f"coid:{o['clientOrderId']}", 0) == 0:
                stats["exchange_fill_not_in_trades_csv"] += 1
        halted = broker.halted
        stats["halted_end"] += int(halted)
        inflight = _inflight(root)
        stats["inflight_end"] += len(inflight)
        stats["unverified_end"] += len(_unverified(root))
        diffs = []
        for s in SYMS:
            base = s.split("/")[0]
            ex = fake.bal[base]
            led = portfolio.positions[s].amount if s in portfolio.positions else 0.0
            if abs(ex - led) > max(1e-6, 0.006 * max(ex, led)):
                diffs.append(f"{base}: exchange {ex:.8f} ledger {led:.8f} plan={getattr(store.get(s), 'status', None)}")
            if ex > 1e-5 and (store.get(s) is None or store.get(s).status != "OPEN"):
                diffs.append(f"{base}: exchange holds {ex:.8f} with NO OPEN plan (no stop-loss)")
        cash_diff = portfolio.cash - fake.bal["USDT"]
        if abs(cash_diff) > 2.0 + 0.01 * stats.get("plans", 0) * 1.0:
            diffs.append(f"USDT: ledger {portfolio.cash:.2f} exchange {fake.bal['USDT']:.2f} (ledger-exchange {cash_diff:+.2f})")
        if diffs:
            if halted or inflight:
                stats["divergence_flagged"] += 1
            else:
                violations.append("SILENT divergence (not halted, no record): " + "; ".join(diffs))
        return {"seed": seed, "violations": violations, "stats": dict(stats), "diffs": diffs,
                "halted": halted, "root": str(root)}
    finally:
        faults.uninstall()
        shutil.rmtree(root, ignore_errors=True)


def _unverified(root):
    p = root / "logs" / bl.ORDER_STATE_NAME
    try:
        return json.loads(p.read_text()).get("unverified", {}) if p.exists() else {}
    except Exception:  # noqa: BLE001
        return {}


def halted_now(root):
    return (root / "logs" / bl.HALT_MARKER_NAME).exists() or bool(_unverified(root))


def _inflight(root):
    p = root / "logs" / bl.ORDER_STATE_NAME
    try:
        return json.loads(p.read_text())["inflight"] if p.exists() else {}
    except Exception:  # noqa: BLE001
        return {"<unreadable>": True}


def check_orders(fake, plan_refs, violations, cyc):
    # map clientOrderId -> (plan_ref, side)
    idmap = {}
    for ref in plan_refs:
        for side in ("buy", "sell"):
            for a in range(12):
                idmap[bl.client_order_id(ref, side, a)] = (ref, side, a)
    filled_buys = collections.Counter()
    coid_uses = collections.Counter()
    for o in fake.orders:
        key = idmap.get(o["clientOrderId"])
        coid_uses[o["clientOrderId"]] += 1
        if key and key[1] == "buy" and o["filled"] > 0:
            filled_buys[key[0]] += 1
    for ref, n in filled_buys.items():
        if n > 1:
            msg = f"DUPLICATE BUY: plan {ref} has {n} filled buy orders"
            if msg not in violations:
                violations.append(f"cycle {cyc}: " + msg)
    for coid, n in coid_uses.items():
        if n > 1:
            msg = f"clientOrderId {coid} {idmap.get(coid)} sent and ACCEPTED {n} times"
            if not any(msg in v for v in violations):
                violations.append(f"cycle {cyc}: " + msg)


PROFILES = {
    "exchange_faults": dict(crash_before=0.03, timeout_before=0.04, reject=0.04, partial=0.1, timeout_after=0.05,
                            bare_error_after=0.02, crash_after=0.04, garbage=0.08, lookup_err=0.15, ack_only=0.15,
                            balance_err=0.1, balance_omit=0.05),
    "disk_faults": dict(state_oserr=0.03, state_crash_before=0.02, state_crash_after=0.02, halt_oserr=0.1,
                        trades_oserr=0.04, trades_crash_before=0.01, trades_crash_after=0.02,
                        store_crash_before=0.01, store_crash_after=0.02, hist_crash_after=0.01, partial=0.05),
    # RO round 2 (N1): Kraken -- every create_order answer is txid-only and lookups often fail.
    "kraken_ack_only": dict(ack_only=1.0, lookup_err=0.3, partial=0.1, timeout_after=0.03, crash_after=0.02),
    "everything": dict(crash_before=0.02, timeout_before=0.03, reject=0.03, partial=0.08, timeout_after=0.04,
                       bare_error_after=0.02, crash_after=0.03, garbage=0.06, lookup_err=0.1, ack_only=0.1, balance_err=0.08,
                       balance_omit=0.04, state_oserr=0.02, state_crash_before=0.01, state_crash_after=0.02,
                       halt_oserr=0.05, trades_oserr=0.03, trades_crash_after=0.02, store_crash_after=0.02),
}


def main():
    import logging
    logging.disable(logging.CRITICAL)
    bl.RECONCILE_NOT_FOUND_GRACE_SECONDS = 0
    episodes = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    cycles = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    seed0 = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    for name, prof in PROFILES.items():
        agg = collections.Counter()
        kinds = collections.Counter()
        examples = {}
        for i in range(episodes):
            seed = seed0 + i
            try:
                res = run_episode(seed, cycles, prof)
            except Exception:  # noqa: BLE001
                kinds["HARNESS ERROR"] += 1
                examples.setdefault("HARNESS ERROR", (seed, traceback.format_exc()[-800:]))
                continue
            for k, v in res["stats"].items():
                agg[k] += v
            for v in res["violations"]:
                k = v.split(": ", 1)[1][:40] if ": " in v else v[:40]
                for tag in ("DOUBLE BOOKING", "OVER-BOOKED", "never had", "DUPLICATE BUY", "sent and ACCEPTED", "SILENT divergence", "HALT MARKER", "in-memory ledger",
                            "restart failed"):
                    if tag in v:
                        k = tag
                kinds[k] += 1
                examples.setdefault(k, (seed, v))
        print(f"\n=== profile {name}: {episodes} episodes x {cycles} cycles")
        print("stats:", dict(sorted(agg.items())))
        print("violations by kind:", dict(kinds))
        for k, (seed, v) in examples.items():
            print(f"  e.g. seed {seed}: {v[:600]}")


if __name__ == "__main__":
    main()
