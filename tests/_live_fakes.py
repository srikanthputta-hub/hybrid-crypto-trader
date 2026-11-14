"""Fault-injecting stand-ins for issue #99 tests (docs/live_readiness_design.md).

`FaultyExchange` replaces src.exchange.ExchangeClient for src.broker_live.LiveBroker: no network,
no credentials, no real order. It keeps its own "exchange book" (every order it accepted, keyed by
clientOrderId) and per-asset free balances, so a test can crash the broker mid-order, rebuild it from
disk exactly like src.bot.build_components does, and let the reconcile find what the exchange did.
"""
from __future__ import annotations

import copy
from pathlib import Path

import ccxt

from src.portfolio import Portfolio, Position, reconstruct_cash
from src.trade_plan_store import TradePlanStore


class SimulatedCrash(BaseException):
    """SIGKILL/OOM stand-in: a BaseException, so no `except Exception` in the broker can swallow it."""


def base_of(symbol: str) -> str:
    return symbol.split("/")[0]


def quote_of(symbol: str) -> str:
    return symbol.split("/")[1].split(":")[0]


class FaultyExchange:
    def __init__(self, prices: dict[str, float] | None = None) -> None:
        self.prices = dict(prices or {"BTC/USDT": 50_000.0, "ETH/USDT": 2_000.0})
        self.orders: list[tuple[str, str, float]] = []      # every create call, BEFORE any fault
        self.client_ids: list[str | None] = []
        self.book: dict[str, dict] = {}                     # clientOrderId -> order as the exchange reports it
        self.free: dict[str, float] = {}                    # asset -> free balance (None/missing = not reported)
        self.track_balances = False                         # move `free` on every fill
        # --- create_market_order faults ---
        self.fill_ratio = 1.0                               # filled = amount * fill_ratio
        self.status = "closed"
        self.fee: dict | None = None                        # {"currency": "base"|"quote"|code, "rate": x}
        self.create_error: BaseException | None = None      # raised BEFORE the order is on the book
        self.error_after_book: BaseException | None = None  # order IS on the book, then this is raised
        self.crash_after_create = False                     # order IS on the book, then SimulatedCrash
        self.respond = None                                 # callable(order) -> response returned instead
        self.reject_duplicate_ids = False
        # --- other faults ---
        self.balance_error: BaseException | None = None
        self.precision = None                               # callable(symbol, amount) -> float; None = unsupported
        self.find_error: BaseException | None = None
        self.find_override: dict[str, object] = {}          # clientOrderId -> dict | None | BaseException
        self.lookups: list[tuple] = []
        self.balance_calls = 0
        self.on_create = None                               # callable(symbol, side, amount, client_order_id)
        self._n = 0

    # ------------------------------------------------------------- order placement
    def create_market_order(self, symbol, side, amount, client_order_id=None):
        if self.on_create is not None:
            self.on_create(symbol, side, amount, client_order_id)
        self.orders.append((symbol, side, amount))
        self.client_ids.append(client_order_id)
        if self.reject_duplicate_ids and client_order_id in self.book:
            raise ccxt.DuplicateOrderId(f"duplicate clientOrderId {client_order_id}")
        if self.create_error is not None:
            raise self.create_error
        self._n += 1
        price = self.prices[symbol]
        filled = amount * self.fill_ratio
        order = {
            "id": f"x{self._n}", "clientOrderId": client_order_id, "symbol": symbol, "side": side,
            "type": "market", "status": self.status, "amount": amount, "filled": filled,
            "remaining": amount - filled, "average": price if filled else None, "price": None,
            "cost": filled * price,
        }
        if self.fee is not None and filled:
            currency = {"base": base_of(symbol), "quote": quote_of(symbol)}.get(self.fee["currency"], self.fee["currency"])
            cost = filled * self.fee["rate"] if currency == base_of(symbol) else filled * price * self.fee["rate"]
            order["fee"] = {"currency": currency, "cost": cost}
            order["fees"] = [dict(order["fee"])]
        if client_order_id is not None:
            self.book[client_order_id] = order
        if self.track_balances:
            base = base_of(symbol)
            fee_base = order.get("fee", {}).get("cost", 0.0) if order.get("fee", {}).get("currency") == base else 0.0
            delta = filled - fee_base if side == "buy" else -filled
            self.free[base] = self.free.get(base, 0.0) + delta
        if self.error_after_book is not None:
            raise self.error_after_book
        if self.crash_after_create:
            raise SimulatedCrash("process killed after the order reached the exchange")
        if self.respond is not None:
            return self.respond(copy.deepcopy(order))
        return copy.deepcopy(order)

    # ------------------------------------------------------------- read side
    def fetch_free_balance(self, asset):
        self.balance_calls += 1
        if self.balance_error is not None:
            raise self.balance_error
        return self.free.get(asset)

    def amount_to_precision(self, symbol, amount):
        if self.precision is None:
            raise ccxt.NotSupported("no market precision in this fake")
        return self.precision(symbol, amount)

    def find_order(self, symbol, client_order_id, exchange_order_id=None, since_ms=None, side=None):
        self.lookups.append((symbol, client_order_id, exchange_order_id, since_ms, side))
        if self.find_error is not None:
            raise self.find_error
        if client_order_id in self.find_override:
            value = self.find_override[client_order_id]
            if isinstance(value, BaseException):
                raise value
            return copy.deepcopy(value)
        return copy.deepcopy(self.book.get(client_order_id))


def logs_dir(tmp_path: Path) -> Path:
    return tmp_path / "logs"


def make_store(tmp_path: Path) -> TradePlanStore:
    return TradePlanStore(logs_dir(tmp_path) / "trade_plans_current.json",
                          logs_dir(tmp_path) / "trade_plans_history.jsonl")


def rebuild_portfolio(tmp_path: Path, store: TradePlanStore, starting_cash: float = 10_000.0) -> Portfolio:
    """Exactly src.bot.build_components' restart reconstruction."""
    trade_log = logs_dir(tmp_path) / "trades.csv"
    cash = reconstruct_cash(trade_log, starting_cash)
    positions = {
        symbol: Position(symbol=symbol, amount=record.fill_amount, entry_price=record.fill_price)
        for symbol, record in store.all_current().items()
        if record.status == "OPEN" and record.fill_amount and record.fill_price
    }
    return Portfolio(cash=cash, positions=positions, trade_log_path=trade_log)


def start(tmp_path: Path, exchange: FaultyExchange, starting_cash: float = 10_000.0):
    """(Re)start: fresh store + portfolio from disk, fresh LiveBroker (both switches on)."""
    from src.broker_live import LiveBroker

    store = make_store(tmp_path)
    portfolio = rebuild_portfolio(tmp_path, store, starting_cash)
    broker = LiveBroker(exchange, portfolio, "live", True)
    return broker, portfolio, store


PLAN = {"approved_quote_amount": 100.0, "entry_zone": (49_000.0, 50_500.0), "invalidation": 40_000.0,
        "stop_loss": 45_000.0, "take_profit": 60_000.0}
