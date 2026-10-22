"""Throttle for the VM's dashboard pushes (scripts/run_live.py).

Why this exists: Cloudflare Workers Build runs one build per push to `main`, and the VM used to push
web/data.json every 5-minute cycle (~288 builds/day), which exhausted the monthly build-minute allowance and froze
the live dashboard. The export still runs every cycle (cheap, local); this module decides whether the result is
worth a commit+push:

  * the dashboard-relevant CONTENT changed (a fingerprint, below), or
  * at least PUSH_MIN_INTERVAL_MINUTES have passed since the last successful push (a heartbeat, so the page's
    "updated N min ago" stays alive and a dead VM is still detectable).

Everything here is pure or best-effort I/O: nothing raises into the trading loop's caller.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("dashboard_push")

PUSH_MINUTES_ENV = "HCT_DASHBOARD_PUSH_MINUTES"
DEFAULT_PUSH_MINUTES = 60.0
MIN_PUSH_MINUTES = 5.0

# Persisted across restarts. logs/*.json is gitignored (see .gitignore), and so is the atomic-write temp file
# (logs/*.tmp): nothing under logs/ is ever committed.
STATE_RELATIVE_PATH = Path("logs") / "dashboard_push_state.json"
UNKNOWN_FINGERPRINT = "unknown"


def parse_push_minutes(raw: Any) -> float:
    """The heartbeat interval in minutes from an env-var string. Garbage (None, empty, non-numeric, NaN, inf, bool,
    zero or negative) falls back to the 60-minute default; a valid number below 5 is clamped up to 5. Never raises."""
    try:
        if raw is None or isinstance(raw, bool):
            return DEFAULT_PUSH_MINUTES
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_PUSH_MINUTES
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_PUSH_MINUTES
    return max(MIN_PUSH_MINUTES, value)


# The CLI-free constant: HCT_DASHBOARD_PUSH_MINUTES read once at import (the service is restarted to apply a change).
PUSH_MIN_INTERVAL_MINUTES = parse_push_minutes(os.environ.get(PUSH_MINUTES_ENV))


def refresh_interval_from_env() -> float:
    """Re-read the env var into PUSH_MIN_INTERVAL_MINUTES. run_live.main() calls this after load_config(), which is
    what loads `.env` into os.environ -- after this module was imported."""
    global PUSH_MIN_INTERVAL_MINUTES
    PUSH_MIN_INTERVAL_MINUTES = parse_push_minutes(os.environ.get(PUSH_MINUTES_ENV))
    return PUSH_MIN_INTERVAL_MINUTES


# --------------------------------------------------------------------------- #
# Content fingerprint
# --------------------------------------------------------------------------- #
# THE list of what counts as "the dashboard content changed". Each entry is (name, path into data.json, reducer).
# Deliberately NOT here (volatile, change every cycle without meaning anything new): generated_at, the hourly pnl
# labels/start times, ops.health.last_event_at, the equity curve's end timestamp, every *_age field, decision
# timestamps, ops.health.window.since, parity file_modified... Add a field here only if a viewer would see a
# different number or state; tests/test_dashboard_push.py pins both directions.
def _decision_rows(rows: Any) -> Any:
    if not isinstance(rows, list):
        return rows
    return [
        [r.get("symbol"), r.get("state"), r.get("reason"), r.get("bar_close")] if isinstance(r, dict) else r
        for r in rows
    ]


def _last_equity_values(curve: Any) -> Any:
    """The newest equity point's VALUES ([equity, cash]), never its timestamp."""
    points = curve.get("points") if isinstance(curve, dict) else None
    if not isinstance(points, list) or not points:
        return None
    last = points[-1]
    return list(last[1:]) if isinstance(last, (list, tuple)) else last


def _identity(value: Any) -> Any:
    return value


FINGERPRINT_FIELDS: tuple[tuple[str, tuple[str, ...], Any], ...] = (
    ("portfolio", ("portfolio",), _identity),
    ("positions", ("positions",), _identity),
    ("pending", ("pending",), _identity),
    ("ops.trade_stats", ("ops", "trade_stats"), _identity),
    ("ops.closed_trades", ("ops", "closed_trades"), _identity),
    ("ops.decisions", ("ops", "decisions"), _decision_rows),
    ("ops.kill_switch", ("ops", "kill_switch"), _identity),
    ("ops.health.latest_bar", ("ops", "health", "latest_bar"), _identity),
    ("ops.parity", ("ops", "parity"), _identity),
    ("ops.equity_curve.last_point_values", ("ops", "equity_curve"), _last_equity_values),
)


def _dig(data: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def content_fingerprint(data: Any) -> str | None:
    """A stable hash (sha256 over canonical JSON) of FINGERPRINT_FIELDS in a parsed data.json, or None if `data`
    is not a dict. A missing field contributes null, so an older export without `ops` still fingerprints."""
    if not isinstance(data, dict):
        return None
    reduced = {name: reducer(_dig(data, path)) for name, path, reducer in FINGERPRINT_FIELDS}
    canonical = json.dumps(reduced, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def fingerprint_file(path: Path) -> str | None:
    """content_fingerprint of the JSON file at `path`; None when it is missing or unreadable."""
    try:
        return content_fingerprint(json.loads(Path(path).read_text()))
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Persisted push state
# --------------------------------------------------------------------------- #
def load_state(path: Path) -> dict[str, Any]:
    """{"fingerprint": str|None, "pushed_at": float|None} from `path`. Missing, corrupt or wrongly-typed content
    yields the empty state, which push_decision() reads as 'push now'. Never raises."""
    empty: dict[str, Any] = {"fingerprint": None, "pushed_at": None}
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return empty
    if not isinstance(raw, dict):
        return empty
    fp = raw.get("fingerprint")
    at = raw.get("pushed_at")
    if not isinstance(fp, str) or isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
        return empty
    return {"fingerprint": fp, "pushed_at": float(at)}


def save_state(path: Path, fingerprint: str | None, pushed_at: float) -> bool:
    """Atomically (temp file + os.replace) record a SUCCESSFUL push. Returns False (and logs) on an OS error: the
    worst outcome is one redundant push after the next restart. A None fingerprint (unreadable export) is stored as
    UNKNOWN_FINGERPRINT so the record stays valid and the heartbeat clock still runs."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"fingerprint": fingerprint or UNKNOWN_FINGERPRINT, "pushed_at": pushed_at}))
        os.replace(tmp, path)
        return True
    except OSError as exc:
        logger.warning("could not persist dashboard push state to %s: %s", path, exc)
        return False


def push_decision(
    current_fingerprint: str | None, state: dict[str, Any], now: float, interval_minutes: float
) -> tuple[bool, str]:
    """(push?, reason). Push when there is no valid state, the content changed, or the heartbeat is due. A clock
    that moved backwards (pushed_at in the future) counts as due. An unfingerprintable export (None) never counts as
    'changed': only the heartbeat can push it, so a persistent problem cannot bring back one build per cycle."""
    last_fp, last_at = state.get("fingerprint"), state.get("pushed_at")
    if last_fp is None or last_at is None:
        return True, "no previous push recorded"
    if current_fingerprint is not None and current_fingerprint != last_fp:
        return True, "content changed"
    elapsed = now - last_at
    if elapsed < 0 or elapsed >= interval_minutes * 60:
        return True, f"heartbeat ({interval_minutes:g} min)"
    return False, "unchanged within the heartbeat interval"


def state_path(project_root: Path) -> Path:
    return Path(project_root) / STATE_RELATIVE_PATH


def now_epoch() -> float:
    return time.time()
