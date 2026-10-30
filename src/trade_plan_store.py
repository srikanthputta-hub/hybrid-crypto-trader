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
import math
import os
from dataclasses import asdict, dataclass, field, replace
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
    """Every transition is copy-on-write (issue #99 review F3): the new state is written to
    current.json (temp file + fsync + os.replace + directory fsync) FIRST and only then
    adopted in memory, then the transition is appended (fsync'd) to history.jsonl. A failed
    write leaves memory exactly as it was, so memory never claims a transition the disk
    doesn't have (which once let a live order record be settled on a never-persisted OPEN)."""

    def __init__(self, current_path: Path, history_path: Path, quarantine_corrupt: bool = False) -> None:
        self.current_path = current_path
        self.history_path = history_path
        self.current_path.parent.mkdir(parents=True, exist_ok=True)
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        # Set when quarantine_corrupt moved an unreadable current.json aside (live mode only:
        # the caller then halts the live broker and alerts). Paper mode keeps raising, as before.
        self.load_error: str | None = None
        try:
            self._current: dict[str, PlanRecord] = self._load_current()
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            if not quarantine_corrupt:
                raise
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            dest = self.current_path.with_name(f"{self.current_path.name}.corrupt-{stamp}")
            os.replace(self.current_path, dest)
            self.load_error = f"{type(exc).__name__} reading {self.current_path.name}; quarantined as {dest.name}"
            self._current = {}

    def _load_current(self) -> dict[str, PlanRecord]:
        if not self.current_path.exists():
            return {}
        with open(self.current_path) as f:
            raw = json.load(f)
        return {symbol: PlanRecord.from_dict(data) for symbol, data in raw.items()}

    def _persist_current(self, current: dict[str, PlanRecord] | None = None) -> None:
        current = self._current if current is None else current
        data = json.dumps({symbol: rec.to_dict() for symbol, rec in current.items()}, indent=2, default=str)
        tmp = self.current_path.with_name(self.current_path.name + ".tmp")
        with open(tmp, "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.current_path)
        _fsync_dir(self.current_path.parent)

    def _append_history(self, record: PlanRecord) -> None:
        with open(self.history_path, "a") as f:
            f.write(json.dumps(record.to_dict(), default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _commit(self, new_current: dict[str, PlanRecord], history_record: PlanRecord) -> None:
        self._persist_current(new_current)  # raises -> memory untouched
        self._current = new_current
        self._append_history(history_record)

    def get(self, symbol: str) -> PlanRecord | None:
        return self._current.get(symbol)

    def all_current(self) -> dict[str, PlanRecord]:
        return dict(self._current)

    def read_persisted(self, symbol: str) -> dict[str, Any] | None:
        """`symbol`'s record as it is ON DISK right now (None if absent): lets a caller verify a
        transition really persisted before acting on it (e.g. clearing a live order record)."""
        if not self.current_path.exists():
            return None
        with open(self.current_path) as f:
            raw = json.load(f)
        return raw.get(symbol) if isinstance(raw, dict) else None

    def set_pending(self, symbol: str, plan: dict[str, Any], claude_verdict: dict[str, Any] | None = None) -> PlanRecord:
        record = PlanRecord(symbol=symbol, status="PENDING", plan=plan, claude_verdict=claude_verdict)
        self._commit({**self._current, symbol: record}, record)
        return record

    def mark_open(self, symbol: str, fill_price: float, fill_amount: float | None = None, now: str | None = None) -> PlanRecord:
        record = replace(self._current[symbol], status="OPEN", filled_at=now or now_iso(),
                         fill_price=fill_price, fill_amount=fill_amount)
        self._commit({**self._current, symbol: record}, record)
        return record

    def update_open_amount(self, symbol: str, fill_amount: float) -> PlanRecord:
        """An OPEN position was partly sold (live broker partial fill, issue #99): record the
        base amount still held, so a restart rebuilds the remainder, not the original fill.
        Like every transition it updates current.json AND appends to history.jsonl.

        Raises (store unchanged) unless the record is OPEN and fill_amount is a finite number > 0."""
        record = self._current.get(symbol)
        if record is None or record.status != "OPEN":
            raise ValueError(f"no OPEN plan for {symbol} to update (status {getattr(record, 'status', None)!r})")
        if isinstance(fill_amount, bool) or not isinstance(fill_amount, (int, float)) or not math.isfinite(fill_amount) \
                or fill_amount <= 0:
            raise ValueError(f"invalid remaining amount {fill_amount!r} for {symbol}: must be a finite number > 0")
        record = replace(record, fill_amount=float(fill_amount))
        self._commit({**self._current, symbol: record}, record)
        return record

    def mark_closed(self, symbol: str, reason: str, price: float, now: str | None = None) -> PlanRecord:
        record = replace(self._current[symbol], status="CLOSED", closed_at=now or now_iso(),
                         close_reason=reason, close_price=price)
        self._commit({s: r for s, r in self._current.items() if s != symbol}, record)
        return record

    def discard_pending(self, symbol: str, reason: str, price: float, now: str | None = None) -> None:
        """A PENDING plan whose invalidation level got hit before it ever filled."""
        record = self._current.get(symbol)
        if record is None or record.status != "PENDING":
            return
        record = replace(record, status="CLOSED", closed_at=now or now_iso(), close_reason=reason, close_price=price)
        self._commit({s: r for s, r in self._current.items() if s != symbol}, record)


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover - platforms without directory fds
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)
