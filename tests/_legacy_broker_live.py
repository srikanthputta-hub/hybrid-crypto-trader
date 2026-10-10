"""FROZEN, verbatim copy of src/broker_live.py as of origin/main c4f50f0 (before the
order/ledger hardening). Test-only parity reference: tests/test_broker_live.py checks the
hardened LiveBroker books byte-identical ledgers / trade logs to this on every VALID fill.
Never import this from src/. Never "fix" it -- its value is that it does not change.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.exchange import ExchangeClient
from src.portfolio import Portfolio, now_iso


class LiveTradingDisabled(RuntimeError):
    pass


@dataclass
class FillResult:
    symbol: str
    side: str
    price: float
    base_amount: float
    quote_amount: float
    timestamp: str
    order_id: str | None


class LiveBroker:
    def __init__(
        self,
        exchange: ExchangeClient,
        portfolio: Portfolio,
        mode: str,
        live_trading_confirmed: bool,
    ) -> None:
        if mode == "live" and not live_trading_confirmed:
            raise LiveTradingDisabled(
                "config.yaml has mode: live but LIVE_TRADING_CONFIRM env var is not set to "
                "'YES-I-UNDERSTAND-THE-RISK'. Refusing to construct a live broker. "
                "This is intentional — see .env.example."
            )
        if mode != "live":
            raise LiveTradingDisabled(
                f"LiveBroker instantiated but config.yaml mode is {mode!r}, not 'live'."
            )
        self.exchange = exchange
        self.portfolio = portfolio

    def buy(self, symbol: str, quote_amount: float, price_hint: float) -> FillResult:
        base_amount_estimate = quote_amount / price_hint
        order = self.exchange.create_market_order(symbol, "buy", base_amount_estimate)
        filled_price = float(order.get("average") or order.get("price") or price_hint)
        filled_amount = float(order.get("filled") or base_amount_estimate)
        # Reconcile our local ledger with what the exchange actually reports.
        self.portfolio.cash -= filled_amount * filled_price
        existing = self.portfolio.positions.get(symbol)
        if existing is None:
            from src.portfolio import Position

            self.portfolio.positions[symbol] = Position(symbol, filled_amount, filled_price)
        else:
            total = existing.amount + filled_amount
            existing.entry_price = (
                existing.entry_price * existing.amount + filled_price * filled_amount
            ) / total
            existing.amount = total
        result = FillResult(
            symbol, "buy", filled_price, filled_amount, filled_amount * filled_price,
            now_iso(), str(order.get("id")),
        )
        self.portfolio.log_trade(
            {
                "timestamp": result.timestamp,
                "mode": "live",
                "symbol": symbol,
                "side": "buy",
                "price": filled_price,
                "base_amount": filled_amount,
                "quote_amount": result.quote_amount,
                "order_id": result.order_id,
            }
        )
        return result

    def sell(self, symbol: str, price_hint: float) -> FillResult:
        pos = self.portfolio.positions[symbol]
        order = self.exchange.create_market_order(symbol, "sell", pos.amount)
        filled_price = float(order.get("average") or order.get("price") or price_hint)
        filled_amount = float(order.get("filled") or pos.amount)
        proceeds = filled_amount * filled_price
        self.portfolio.cash += proceeds
        del self.portfolio.positions[symbol]
        result = FillResult(
            symbol, "sell", filled_price, filled_amount, proceeds, now_iso(), str(order.get("id"))
        )
        self.portfolio.log_trade(
            {
                "timestamp": result.timestamp,
                "mode": "live",
                "symbol": symbol,
                "side": "sell",
                "price": filled_price,
                "base_amount": filled_amount,
                "quote_amount": proceeds,
                "order_id": result.order_id,
            }
        )
        return result
