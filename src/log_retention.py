"""Prunes old rows from the bot's append-only JSONL logs so they don't grow
unbounded on a 24/7-running host's disk (see deploy/README.md's Oracle VM
setup -- an Always Free instance's boot volume is finite).

Only ever trims logs/audit_log.jsonl (one line per symbol per cycle, the
highest-volume file by far) and logs/trade_plans_history.jsonl (one line per
plan state transition). logs/trades.csv -- the actual financial trade
record, used for cash reconstruction (src.portfolio.reconstruct_cash) and
the P/L dashboard -- is never touched here; that one is meant to be a
durable record, not a rolling window.

Fails safe on anything ambiguous: a line whose timestamp can't be parsed is
kept, never dropped, on the theory that losing an audit line to an
overly-aggressive prune is worse than one extra old line staying around an
extra day.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _parse_timestamp(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _row_timestamp(row: dict, timestamp_keys: list[str]) -> datetime | None:
    for key in timestamp_keys:
        value = row.get(key)
        if value:
            parsed = _parse_timestamp(value)
            if parsed is not None:
                return parsed
    return None


def prune_jsonl_by_age(path: Path, cutoff: datetime, timestamp_keys: list[str]) -> int:
    """Rewrites `path` keeping only rows at/after `cutoff` (by the first present
    key in `timestamp_keys`, checked in order). Rows with no parseable
    timestamp are always kept. Returns how many rows were removed.

    Writes to a temp file first and atomically replaces the original
    (os.replace), so a crash mid-write never leaves a truncated/corrupt log.
    """
    if not path.exists():
        return 0

    kept: list[str] = []
    removed = 0
    with open(path) as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                kept.append(line)  # fail safe: keep anything we can't even parse
                continue
            ts = _row_timestamp(row, timestamp_keys)
            if ts is not None and ts < cutoff:
                removed += 1
                continue
            kept.append(line)

    if removed == 0:
        return 0

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w") as f:
        f.writelines(kept)
    os.replace(tmp_path, path)
    return removed


def run_log_retention(cfg, now: datetime | None = None) -> None:
    """Prunes audit_log.jsonl and trade_plans_history.jsonl to
    logging.retention_days (default 30), per config/config.yaml.

    A non-positive retention_days disables pruning entirely (kept as an
    explicit opt-out, e.g. for local debugging where you want full history).
    """
    now = now or datetime.now(timezone.utc)
    log_cfg = cfg.logging_cfg
    retention_days = log_cfg.get("retention_days", 30)
    if retention_days is None or retention_days <= 0:
        return
    cutoff = now - timedelta(days=retention_days)

    from src.config import PROJECT_ROOT  # local import: avoids a cycle at module load time

    targets = [
        (PROJECT_ROOT / log_cfg.get("audit_log_path", "logs/audit_log.jsonl"), ["timestamp"]),
        (
            PROJECT_ROOT / log_cfg.get("trade_plan_history_path", "logs/trade_plans_history.jsonl"),
            ["closed_at", "filled_at", "created_at"],
        ),
    ]
    for path, timestamp_keys in targets:
        try:
            removed = prune_jsonl_by_age(path, cutoff, timestamp_keys)
            if removed:
                logger.info("log retention: pruned %d row(s) older than %d days from %s", removed, retention_days, path)
        except Exception:
            logger.exception("log retention failed for %s -- leaving it as-is", path)
