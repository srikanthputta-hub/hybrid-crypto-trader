"""24/7 monitor: "monitors open trades, market conditions, and acts in real time."

Per how this bot is wired, "sends alerts" means the bot itself executes the
appropriate action — there's no human-in-the-loop notification step. This
module is pure decision logic (it looks at current prices vs. each plan's
levels and says what should happen); src.bot is what actually calls the broker
and updates the trade plan store with the result. Keeping it pure makes it
trivial to unit test without a real exchange or portfolio.

Two things get watched every tick, for every symbol with an active plan:

  PENDING plan  (signal fired, not filled yet — entry is a zone, not a market
                 order): fill it once price re-enters the entry zone, or
                 discard it if price breaches the invalidation level first —
                 the setup is no longer valid, don't chase it.

  OPEN position (already filled): close it if price hits the take-profit
                 target or the stop loss.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.trade_plan_store import PlanRecord


@dataclass
class MonitorAction:
    symbol: str
    action: str    # "FILL" | "DISCARD" | "TAKE_PROFIT" | "STOP_LOSS"
    price: float
    reason: str


def check_pending(record: PlanRecord, current_price: float) -> MonitorAction | None:
    if record.status != "PENDING":
        return None
    plan = record.plan
    entry_low, entry_high = plan["entry_zone"]
    invalidation = plan["invalidation"]

    if current_price <= invalidation:
        return MonitorAction(
            record.symbol, "DISCARD", current_price,
            f"price {current_price} breached invalidation level {invalidation} before the entry ever filled",
        )
    if entry_low <= current_price <= entry_high:
        return MonitorAction(
            record.symbol, "FILL", current_price,
            f"price {current_price} entered the entry zone [{entry_low}, {entry_high}]",
        )
    return None


def check_open(record: PlanRecord, current_price: float) -> MonitorAction | None:
    if record.status != "OPEN":
        return None
    plan = record.plan
    stop_loss = plan["stop_loss"]
    take_profit = plan["take_profit"]

    if current_price <= stop_loss:
        return MonitorAction(record.symbol, "STOP_LOSS", current_price, f"price {current_price} hit stop loss {stop_loss}")
    if current_price >= take_profit:
        return MonitorAction(
            record.symbol, "TAKE_PROFIT", current_price, f"price {current_price} hit take-profit target {take_profit}"
        )
    return None


def sweep(records: dict[str, PlanRecord], current_prices: dict[str, float]) -> list[MonitorAction]:
    """Check every active plan/position against the latest prices, in one pass."""
    actions: list[MonitorAction] = []
    for symbol, record in records.items():
        price = current_prices.get(symbol)
        if price is None:
            continue
        action = check_pending(record, price) or check_open(record, price)
        if action is not None:
            actions.append(action)
    return actions
