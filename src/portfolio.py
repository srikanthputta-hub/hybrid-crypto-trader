"""In-memory (optionally CSV-persisted) portfolio state shared by paper and live brokers."""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Position:
    symbol: str
    amount: float          # base-currency amount held, e.g. BTC
    entry_price: float     # average entry price in quote currency, e.g. USDT


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    trade_log_path: Path | None = None

    def equity(self, current_prices: dict[str, float]) -> float:
        total = self.cash
        for symbol, pos in self.positions.items():
            price = current_prices.get(symbol, pos.entry_price)
            total += pos.amount * price
        return total

    def unrealized_pnl_pct(self, symbol: str, current_price: float) -> float | None:
        pos = self.positions.get(symbol)
        if pos is None or pos.entry_price == 0:
            return None
        return (current_price - pos.entry_price) / pos.entry_price

    def apply_buy(self, symbol: str, quote_amount: float, price: float, fee_pct: float = 0.001) -> float:
        """Spend `quote_amount` of cash buying `symbol` at `price`. Returns base amount bought."""
        if quote_amount > self.cash:
            raise ValueError(f"insufficient cash: have {self.cash}, need {quote_amount}")
        fee = quote_amount * fee_pct
        net_quote = quote_amount - fee
        base_amount = net_quote / price
        self.cash -= quote_amount
        existing = self.positions.get(symbol)
        if existing is None:
            self.positions[symbol] = Position(symbol=symbol, amount=base_amount, entry_price=price)
        else:
            total_amount = existing.amount + base_amount
            existing.entry_price = (
                existing.entry_price * existing.amount + price * base_amount
            ) / total_amount
            existing.amount = total_amount
        return base_amount

    def apply_sell(self, symbol: str, price: float, fee_pct: float = 0.001) -> float:
        """Sell the *entire* position in `symbol` at `price`. Returns net quote proceeds."""
        pos = self.positions.get(symbol)
        if pos is None or pos.amount <= 0:
            raise ValueError(f"no open position in {symbol} to sell")
        gross = pos.amount * price
        fee = gross * fee_pct
        net = gross - fee
        self.cash += net
        del self.positions[symbol]
        return net

    def log_trade(self, row: dict) -> None:
        if self.trade_log_path is None:
            return
        self.trade_log_path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not self.trade_log_path.exists()
        with open(self.trade_log_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(row.keys()))
            if write_header:
                writer.writeheader()
            writer.writerow(row)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
