"""Paper broker: simulates fills using real live prices, never touches a real exchange account."""
from __future__ import annotations

from dataclasses import dataclass

from src.portfolio import Portfolio, now_iso


@dataclass
class FillResult:
    symbol: str
    side: str
    price: float
    base_amount: float
    quote_amount: float
    timestamp: str


class PaperBroker:
    """Requires no exchange API credentials — read-only ExchangeClient supplies prices."""

    def __init__(self, portfolio: Portfolio, fee_pct: float = 0.001) -> None:
        self.portfolio = portfolio
        self.fee_pct = fee_pct

    def buy(self, symbol: str, quote_amount: float, price: float) -> FillResult:
        base_amount = self.portfolio.apply_buy(symbol, quote_amount, price, self.fee_pct)
        result = FillResult(symbol, "buy", price, base_amount, quote_amount, now_iso())
        self.portfolio.log_trade(
            {
                "timestamp": result.timestamp,
                "mode": "paper",
                "symbol": symbol,
                "side": "buy",
                "price": price,
                "base_amount": base_amount,
                "quote_amount": quote_amount,
            }
        )
        return result

    def sell(self, symbol: str, price: float) -> FillResult:
        pos = self.portfolio.positions[symbol]
        base_amount = pos.amount
        net_quote = self.portfolio.apply_sell(symbol, price, self.fee_pct)
        result = FillResult(symbol, "sell", price, base_amount, net_quote, now_iso())
        self.portfolio.log_trade(
            {
                "timestamp": result.timestamp,
                "mode": "paper",
                "symbol": symbol,
                "side": "sell",
                "price": price,
                "base_amount": base_amount,
                "quote_amount": net_quote,
            }
        )
        return result
