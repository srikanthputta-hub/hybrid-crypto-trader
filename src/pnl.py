"""Realized P/L reporting: reads logs/trades.csv (the durable financial
record -- see src.log_retention's docstring for why it's never pruned) and
rolls closed round-trips up into hourly/daily/weekly buckets, the same shape
a real broker app's P/L tab shows. Powers the dashboard's P/L panel.

This bot is long-only spot with at most one open position per symbol at a
time (src.portfolio.Portfolio.positions is keyed by symbol), so a "closed
round-trip" is simply: the most recent still-open BUY row for a symbol,
paired with the next SELL row for that same symbol. There's no FIFO/LIFO
lot-matching ambiguity to resolve.

Realized P/L per round-trip = sell.quote_amount (net proceeds, fee already
deducted -- see src.broker_paper.PaperBroker.sell) minus buy.quote_amount
(gross cost, fee already included -- see src.broker_paper.PaperBroker.buy).
Both fees are already baked into these two numbers, so no separate fee
adjustment is needed here.

An open position (an unmatched BUY with no SELL yet) is not realized P/L --
that already shows up as unrealized_pnl_pct in the dashboard's positions
table (see scripts/export_dashboard_data.py's _record_view). This module
only ever reports on trades that have actually closed.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


@dataclass
class ClosedTrade:
    symbol: str
    buy_time: datetime
    sell_time: datetime
    cost: float      # buy.quote_amount -- gross spend, fee included
    proceeds: float   # sell.quote_amount -- net proceeds, fee deducted
    pnl: float        # proceeds - cost
    pnl_pct: float     # pnl / cost


def _parse_timestamp(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_closed_trades(trade_log_path: Path) -> list[ClosedTrade]:
    """Pairs each SELL row with the most recent still-open BUY row for the
    same symbol. trades.csv rows are in chronological order (it's an
    append-only log -- see src.portfolio.Portfolio.log_trade), so a single
    left-to-right pass with one pending-buy slot per symbol is enough.
    """
    if not trade_log_path.exists():
        return []

    open_buys: dict[str, dict[str, Any]] = {}
    closed: list[ClosedTrade] = []
    with open(trade_log_path) as f:
        for row in csv.DictReader(f):
            symbol = row["symbol"]
            side = row["side"]
            if side == "buy":
                open_buys[symbol] = row
                continue
            if side != "sell":
                continue
            buy_row = open_buys.pop(symbol, None)
            if buy_row is None:
                # A sell with no matching buy in this file -- e.g. the buy
                # predates this trade log (shouldn't happen in practice
                # since trades.csv is never pruned, but fail safe rather
                # than crash the dashboard export over one bad row).
                continue
            cost = float(buy_row["quote_amount"])
            proceeds = float(row["quote_amount"])
            closed.append(
                ClosedTrade(
                    symbol=symbol,
                    buy_time=_parse_timestamp(buy_row["timestamp"]),
                    sell_time=_parse_timestamp(row["timestamp"]),
                    cost=cost,
                    proceeds=proceeds,
                    pnl=proceeds - cost,
                    pnl_pct=(proceeds - cost) / cost if cost else 0.0,
                )
            )
    return closed


def _bucket(
    trades: list[ClosedTrade], now: datetime, count: int, step: timedelta, label: Callable[[datetime], str]
) -> list[dict[str, Any]]:
    """`count` contiguous, half-open buckets of width `step`, oldest first,
    the most recent one ending exactly at `now`. A trade lands in a bucket by
    its sell_time (when the P/L was actually realized), matching how a real
    broker's P/L-by-period view works.
    """
    buckets = []
    for i in range(count - 1, -1, -1):
        start = now - step * (i + 1)
        end = now - step * i
        in_bucket = [t for t in trades if start <= t.sell_time < end]
        buckets.append(
            {
                "label": label(end),
                "start": start.isoformat(),
                "pnl": round(sum(t.pnl for t in in_bucket), 2),
                "trades": len(in_bucket),
            }
        )
    return buckets


def build_pnl_report(trade_log_path: Path, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    trades = load_closed_trades(trade_log_path)

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    summary = {
        "total_trades": len(trades),
        "total_realized_pnl": round(sum(t.pnl for t in trades), 2),
        "win_rate": round(len(wins) / len(trades), 4) if trades else None,
        "avg_win": round(sum(t.pnl for t in wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(t.pnl for t in losses) / len(losses), 2) if losses else None,
        "best_trade": round(max((t.pnl for t in trades), default=0.0), 2),
        "worst_trade": round(min((t.pnl for t in trades), default=0.0), 2),
    }

    return {
        "summary": summary,
        "hourly": _bucket(trades, now, 24, timedelta(hours=1), lambda dt: dt.strftime("%H:00")),
        "daily": _bucket(trades, now, 30, timedelta(days=1), lambda dt: dt.strftime("%b %d")),
        "weekly": _bucket(trades, now, 12, timedelta(weeks=1), lambda dt: dt.strftime("%b %d")),
    }
