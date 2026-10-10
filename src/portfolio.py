"""In-memory (optionally CSV-persisted) portfolio state shared by paper and live brokers."""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


def _is_finite_number(value: object) -> bool:
    """True only for a real, finite number (bool rejected). Same rule as src.risk_engine's
    helper, duplicated so the portfolio doesn't import the risk engine."""
    if isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return False


def reconstruct_cash(trade_log_path: Path, starting_cash: float) -> float:
    """Replays trade_log_path (src.portfolio.Portfolio.log_trade's CSV) from
    starting_cash to recover real cash on hand.

    Portfolio itself is in-memory only and doesn't survive a process restart —
    each scheduled bot.py run starts a brand new one. src.bot.build_components
    calls this at startup so cash reflects every buy/sell actually executed so
    far, not just this process's own. (scripts/export_dashboard_data.py used to
    duplicate this same logic purely for display; it now calls this too.)
    """
    if not trade_log_path.exists():
        return starting_cash
    cash = starting_cash
    with open(trade_log_path) as f:
        for row in csv.DictReader(f):
            amount = float(row["quote_amount"])
            cash += amount if row["side"] == "sell" else -amount
    return cash


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
        """Spend `quote_amount` of cash buying `symbol` at `price`. Returns base amount bought.

        Raises ValueError -- leaving cash and positions exactly as they were -- unless
        quote_amount and price are finite numbers > 0, fee_pct is finite in [0, 1),
        cash is finite, and quote_amount <= cash (issue #87: `nan > cash` is False, so a
        NaN amount used to slip past the cash check and make cash NaN permanently). All
        new values are computed and checked BEFORE anything is mutated.
        """
        if not (_is_finite_number(quote_amount) and quote_amount > 0):
            raise ValueError(f"invalid buy quote_amount {quote_amount!r}: must be a finite number > 0")
        if not (_is_finite_number(price) and price > 0):
            raise ValueError(f"invalid buy price {price!r}: must be a finite number > 0")
        if not (_is_finite_number(fee_pct) and 0 <= fee_pct < 1):
            raise ValueError(f"invalid fee_pct {fee_pct!r}: must be a finite number in [0, 1)")
        if not _is_finite_number(self.cash):
            raise ValueError(f"portfolio cash {self.cash!r} is not a finite number; refusing to buy")
        if quote_amount > self.cash:
            raise ValueError(f"insufficient cash: have {self.cash}, need {quote_amount}")
        fee = quote_amount * fee_pct
        net_quote = quote_amount - fee
        base_amount = net_quote / price
        new_cash = self.cash - quote_amount
        existing = self.positions.get(symbol)
        if existing is None:
            new_amount, new_entry_price = base_amount, price
        else:
            total_amount = existing.amount + base_amount
            new_entry_price = (
                existing.entry_price * existing.amount + price * base_amount
            ) / total_amount
            new_amount = total_amount
        if not (
            _is_finite_number(base_amount) and base_amount > 0
            and _is_finite_number(new_cash)
            and _is_finite_number(new_amount) and new_amount > 0
            and _is_finite_number(new_entry_price) and new_entry_price > 0
        ):
            # Only reachable with extreme magnitudes (overflow/underflow) or an existing
            # position that was already corrupt -- refuse rather than write it into state.
            raise ValueError(
                f"buy of {quote_amount!r} {symbol} at {price!r} would produce a non-finite or "
                f"non-positive position/cash (base={base_amount!r}, cash={new_cash!r}, "
                f"amount={new_amount!r}, entry_price={new_entry_price!r}); state left unchanged"
            )
        self.cash = new_cash
        if existing is None:
            self.positions[symbol] = Position(symbol=symbol, amount=new_amount, entry_price=new_entry_price)
        else:
            existing.entry_price = new_entry_price
            existing.amount = new_amount
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
