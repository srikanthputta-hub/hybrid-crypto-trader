"""Persistent record of every trade plan's lifecycle: PENDING -> OPEN -> CLOSED.

A trade plan's entry is a *zone*, not an instant market order — so between the
signal firing and the position actually being filled, the plan sits PENDING
and src.monitor watches it. This store is the shared state both src.bot (which
creates/fills/closes plans) and src.monitor (which watches them every tick)
read and write, and it's also what the dashboard export script reads.

`current.json` holds one live record per symbol (overwritten in place).
`history.jsonl` is an append-only log of every state transition, for the
dashboard's history/trend views and for post-hoc debugging.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PlanRecord:
    symbol: str
    status: str  # "PENDING" | "OPEN" | "CLOSED"
    plan: dict[str, Any]                 # serialized TradePlan fields
    claude_verdict: dict[str, Any] | None = None
    created_at: str = field(default_factory=now_iso)
    filled_at: str | None = None
    fill_price: float | None = None
    fill_amount: float | None = None     # base-currency units bought — needed to value the open position later
    closed_at: str | None = None
    close_reason: str | None = None      # "take_profit" | "stop_loss" | "invalidated" | "manual"
    close_price: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlanRecord":
        return cls(**data)


class TradePlanStore:
    def __init__(self, current_path: Path, history_path: Path) -> None:
        self.current_path = current_path
        self.history_path = history_path
        self.current_path.parent.mkdir(parents=True, exist_ok=True)
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self._current: dict[str, PlanRecord] = self._load_current()

    def _load_current(self) -> dict[str, PlanRecord]:
        if not self.current_path.exists():
            return {}
        with open(self.current_path) as f:
            raw = json.load(f)
        return {symbol: PlanRecord.from_dict(data) for symbol, data in raw.items()}

    def _persist_current(self) -> None:
        with open(self.current_path, "w") as f:
            json.dump({symbol: rec.to_dict() for symbol, rec in self._current.items()}, f, indent=2, default=str)

    def _append_history(self, record: PlanRecord) -> None:
        with open(self.history_path, "a") as f:
            f.write(json.dumps(record.to_dict(), default=str) + "\n")

    def get(self, symbol: str) -> PlanRecord | None:
        return self._current.get(symbol)

    def all_current(self) -> dict[str, PlanRecord]:
        return dict(self._current)

    def set_pending(self, symbol: str, plan: dict[str, Any], claude_verdict: dict[str, Any] | None = None) -> PlanRecord:
        record = PlanRecord(symbol=symbol, status="PENDING", plan=plan, claude_verdict=claude_verdict)
        self._current[symbol] = record
        self._persist_current()
        self._append_history(record)
        return record

    def mark_open(self, symbol: str, fill_price: float, fill_amount: float | None = None, now: str | None = None) -> PlanRecord:
        record = self._current[symbol]
        record.status = "OPEN"
        record.filled_at = now or now_iso()
        record.fill_price = fill_price
        record.fill_amount = fill_amount
        self._persist_current()
        self._append_history(record)
        return record

    def mark_closed(self, symbol: str, reason: str, price: float, now: str | None = None) -> PlanRecord:
        record = self._current[symbol]
        record.status = "CLOSED"
        record.closed_at = now or now_iso()
        record.close_reason = reason
        record.close_price = price
        self._persist_current()
        self._append_history(record)
        del self._current[symbol]
        self._persist_current()
        return record

    def discard_pending(self, symbol: str, reason: str, price: float, now: str | None = None) -> None:
        """A PENDING plan whose invalidation level got hit before it ever filled."""
        record = self._current.get(symbol)
        if record is None or record.status != "PENDING":
            return
        record.status = "CLOSED"
        record.closed_at = now or now_iso()
        record.close_reason = reason
        record.close_price = price
        self._append_history(record)
        del self._current[symbol]
        self._persist_current()
