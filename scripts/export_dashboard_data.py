#!/usr/bin/env python3
"""Export a snapshot of the bot's live state to web/data.json for the dashboard.

Run this after (or alongside) the bot to refresh the dashboard:

    python scripts/export_dashboard_data.py

The dashboard (web/index.html) is a static site — it doesn't talk to the bot
directly. This script is the bridge: it reads the same files the bot writes
(logs/trade_plans_current.json, logs/audit_log.jsonl, logs/trades.csv) plus a
live ticker-price fetch (for equity/unrealized-P&L math) and, for every
symbol with an active signal, an OHLCV candle fetch (TradingView's free
widget has no JS API for drawing custom price lines, so an active signal
gets its own annotated chart built from these candles instead), and writes
one self-contained JSON file the page reads. Re-run it (e.g. on a cron, or
right before a git push if you're using the GitHub -> Cloudflare Pages
auto-deploy flow from the README) to refresh what the dashboard shows.

The additive `ops` key (see "`ops`" below) feeds the redesigned page; options `--config/--out/--instance-id/
--instance-name` export a second bot instance (web/instances.json). With no options this is exactly as before.

Portfolio itself (src/portfolio.py) is in-memory only and doesn't survive a
process restart, so "cash" here is reconstructed the same way src/bot.py
reconstructs it at startup: replaying logs/trades.csv from the configured
starting balance (see src.portfolio.reconstruct_cash).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.pnl import build_pnl_report
from src.risk_analytics import build_risk_report
from src.portfolio import reconstruct_cash
from src.trade_plan_store import TradePlanStore
from scripts.dashboard_push import PUSH_MINUTES_ENV, parse_push_minutes

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = PROJECT_ROOT / "web" / "data.json"
BACKTEST_SUMMARY_PATH = PROJECT_ROOT / "scripts" / "backtest_summary.json"

# How old logs/market_snapshot.json's generated_at can be before this export
# treats it as stale rather than live -- 3x the bot's default 300s poll
# interval (src/market_data.py's MarketDataCache is refreshed once per
# run_cycle, i.e. normally every poll_interval_seconds). Missing this just
# means "no --once/continuous bot cycle has run yet" or the loop has stalled;
# either way the export must say so, not quietly show old data as current.
MARKET_SNAPSHOT_STALE_SECONDS = 900


def _load_backtest_summary() -> dict[str, Any] | None:
    """The "Testing" dashboard panel shows strategy-backtest results (see
    the strategy_pack_run work, branch claude/strategy-pack-backtest) --
    these only change when a new backtest is actually run, not every cycle
    like the rest of this export, so they live in a small checked-in JSON
    file rather than being recomputed here. Re-run the backtest pack and
    overwrite scripts/backtest_summary.json by hand to update this panel;
    every regeneration of web/data.json re-attaches whatever's currently in
    that file, so it survives the normal automated dashboard refresh."""
    if not BACKTEST_SUMMARY_PATH.exists():
        return None
    try:
        return json.loads(BACKTEST_SUMMARY_PATH.read_text())
    except Exception as exc:  # noqa: BLE001 — a bad summary file shouldn't break the live export
        print(f"warning: couldn't read {BACKTEST_SUMMARY_PATH}, Testing panel will show empty: {exc}", file=sys.stderr)
        return None


def _load_market_snapshot(
    path: Path, now: datetime, stale_after_seconds: float = MARKET_SNAPSHOT_STALE_SECONDS
) -> tuple[dict[str, float], dict[str, dict[str, Any]]]:
    """Reads current prices + chart candles from logs/market_snapshot.json
    (written by src/market_data.py's MarketDataCache every run_cycle) instead
    of this script making its own exchange calls -- see
    docs/system-redesign-2026-10.md §4.1/§4.3. Returns ({}, {}) with a clear
    stderr warning if the file is missing or unreadable, same "degrade, don't
    crash" contract the old direct-fetch fallback had. A present-but-stale
    snapshot is still used (better than a blank dashboard) but gets its own
    loud warning -- this must never present old data as if it were fresh."""
    if not path.exists():
        print(
            f"warning: no market snapshot at {path} yet -- dashboard prices/charts will be empty "
            "until the bot runs a cycle (python -m src.bot --once, or the live loop)",
            file=sys.stderr,
        )
        return {}, {}
    try:
        snapshot = json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001 — a bad/partially-written snapshot must not crash the export
        print(f"warning: couldn't read market snapshot at {path}, dashboard prices/charts will be empty: {exc}", file=sys.stderr)
        return {}, {}

    generated_at = _parse_iso(snapshot.get("generated_at"))
    if generated_at is None:
        print(f"warning: market snapshot at {path} has no valid generated_at timestamp -- treating as stale", file=sys.stderr)
    else:
        age_seconds = (now - generated_at).total_seconds()
        if age_seconds > stale_after_seconds:
            print(
                f"warning: market snapshot at {path} is {age_seconds / 60:.1f} min old "
                f"(stale threshold {stale_after_seconds / 60:.0f} min) -- dashboard will show STALE prices/charts, not live ones",
                file=sys.stderr,
            )

    current_prices: dict[str, float] = {}
    charts: dict[str, dict[str, Any]] = {}
    for symbol, entry in (snapshot.get("symbols") or {}).items():
        if entry.get("price") is not None:
            current_prices[symbol] = entry["price"]
        if entry.get("candles"):
            charts[symbol] = {"candles": entry["candles"]}
    return current_prices, charts

# Which audit-event key marks each role as "working" this cycle, and a short
# label for the dashboard's office view. Order here is left-to-right/desk order.
ROLE_MARKERS = [
    ("market_analyst", "Market Analyst", "sonnet", ["candidates", "signal"]),
    ("trade_strategist", "Trade Strategist", "sonnet", ["plan"]),
    ("risk_officer", "Risk Officer", "opus", ["risk_decision"]),
    ("compliance_reviewer", "Compliance Reviewer", "sonnet", ["advisor_verdict"]),
    ("execution_trader", "Execution Trader", "opus", ["execution", "monitor_action"]),
]
RECENCY_WINDOW = timedelta(minutes=15)

# The Backtester (see the daily Routine set up alongside the Testing
# dashboard's candidate leaderboard) doesn't run inside the live per-cycle
# pipeline the roles above are read from -- it's a once-a-day offline job
# that never writes to audit_log.jsonl. "Working" for a single daily run has
# no equivalent of RECENCY_WINDOW's 15 minutes, so it gets its own, wider
# window instead.
BACKTESTER_RECENCY_WINDOW = timedelta(hours=6)

# How long a FAILED candidate stays visible on the dashboard's "Failed" tab
# before it's dropped from the export -- see _visible_candidates. This only
# affects what web/data.json exposes; scripts/backtest_summary.json itself is
# never touched, since the daily Backtester Routine still needs every past
# candidate (pass or fail) on disk to avoid re-testing an already-tried
# parameter (see candidates_schema / "append-only" in that file).
FAILED_CANDIDATE_RETENTION_WINDOW = timedelta(hours=24)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


_TIMEFRAME_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400, "M": 30 * 86400}
DAILY_SECONDS = 86400


def _timeframe_seconds(timeframe: str | None) -> int | None:
    """ccxt timeframe string ("15m", "1h", "1d", "1w", "1M") -> seconds, or None if it isn't one."""
    if not isinstance(timeframe, str) or len(timeframe) < 2 or timeframe[-1] not in _TIMEFRAME_UNIT_SECONDS:
        return None
    digits = timeframe[:-1]
    if not digits.isdigit() or int(digits) <= 0:
        return None
    return int(digits) * _TIMEFRAME_UNIT_SECONDS[timeframe[-1]]


def _price_as_of(charts: dict[str, dict[str, Any]], timeframe_seconds: int | None) -> str | None:
    """The close time of the newest closed bar behind the exported prices/candles: the last candle's open time
    (market_data's snapshot writes only CLOSED bars; `time` is the bar's open, epoch seconds) plus one bar."""
    if not timeframe_seconds:
        return None
    try:
        opens = [c["candles"][-1]["time"] for c in charts.values() if c.get("candles")]
        if not opens:
            return None
        return datetime.fromtimestamp(max(opens) + timeframe_seconds, tz=timezone.utc).isoformat()
    except (KeyError, IndexError, TypeError, ValueError, OverflowError, OSError, AttributeError):
        # A malformed snapshot candle (missing / None / string / absurd `time`) must not take the whole export down
        # (same degrade-don't-crash contract as _load_market_snapshot): no "as of" hint, everything else exported.
        return None


def _display_block(timeframe: str | None, label: str | None, charts: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Display hints for web/app.js. None (the key is omitted from data.json altogether) at the hourly default
    with no `notifier.label`, so a 1h export is byte-for-byte what it was before this block existed.

    At a daily-or-longer bar every price and candle is as of the last bar close, up to a bar old: the dashboard
    says so ("as of <bar close>"), starts the P/L modal on its Daily tab (the Hourly tab is empty at one
    decision a day), and shows a banner carrying `notifier.label` (e.g. "PAPER TRIAL") when one is set."""
    seconds = _timeframe_seconds(timeframe)
    daily_or_longer = seconds is not None and seconds >= DAILY_SECONDS
    if not daily_or_longer and not label:
        return None
    return {
        "timeframe": timeframe,
        "label": label or None,
        "price_as_of": _price_as_of(charts, seconds) if daily_or_longer else None,
        "pnl_default_period": "daily" if daily_or_longer else "hourly",
    }


def _visible_candidates(summary: dict[str, Any] | None, now: datetime) -> list[dict[str, Any]]:
    """Dashboard-facing candidate list for the Testing modal's Passed/Failed
    tabs: a FAILED candidate (gates.overall == "FAIL") older than
    FAILED_CANDIDATE_RETENTION_WINDOW is dropped here, at export time only --
    scripts/backtest_summary.json on disk is never modified, so the Routine's
    own "don't retest an already-tried parameter" logic keeps seeing the full
    history. A PASS candidate, or one with no gates verdict at all, is always
    kept."""
    visible = []
    for c in (summary or {}).get("candidates") or []:
        if (c.get("gates") or {}).get("overall") == "FAIL":
            tested_at = _parse_iso(c.get("tested_at"))
            if tested_at is not None and now - tested_at > FAILED_CANDIDATE_RETENTION_WINDOW:
                continue
        visible.append(c)
    return visible


AUDIT_TAIL_MAX_LINES = 200_000
AUDIT_TAIL_CHUNK_BYTES = 1 << 20  # 1 MiB read chunks: the reader's whole working set, plus the longest single line
# Byte ceiling on how far back from the end of the file the reader will ever go. It exists only to bound run
# time on a pathological log (200,000 enormous lines); at the real ~250 bytes/line the 200,000-line cap above
# binds long before it (about 50 MB), so for every realistic log the output is identical to a whole-file read.
AUDIT_TAIL_MAX_BYTES = 128 * 1024 * 1024


def _first_content_offset(path: Path) -> int | None:
    """Byte offset where the physical line holding the file's first non-whitespace text starts (None if the
    file is all whitespace). Reads only the leading blank prefix, normally nothing past the first line."""
    offset = 0
    with open(path, "rb") as f:
        for segment in f:  # splits on 0x0A only, like the reverse reader
            text = segment[:-1] if segment.endswith(b"\n") else segment
            if text.decode("utf-8", errors="replace").strip():
                return offset
            offset += len(segment)
    return None


def _iter_raw_lines_reversed(path: Path, chunk_bytes: int, max_bytes: int | None):
    """Every line of the file, newest first, one chunk in memory at a time (see _iter_audit_lines_reversed)."""

    def lines_of(segment: bytes, start: int) -> list[str]:
        # The 0x0A that ended the segment still counts as a line break: "x\u2028" + "\n" is two lines, "x" and "",
        # exactly as the whole text splits. (After the file's last segment it only adds a trailing blank line,
        # which is dropped.)
        lines = (segment.decode("utf-8", errors="replace") + "\n").splitlines()
        if start == head_start:  # `strip()` also removed the file's leading whitespace: the first content line loses its own
            for i, line in enumerate(lines):
                if line.strip():
                    lines[i] = line.lstrip()
                    break
        return lines

    head_start = _first_content_offset(path)
    with open(path, "rb") as f:
        f.seek(0, 2)
        pos = f.tell()
        read_total = 0
        pieces: list[bytes] = []  # the unfinished (oldest) line, as pieces in REVERSE file order
        while pos > 0:
            if max_bytes is not None and read_total >= max_bytes:
                return  # cap reached: the partial oldest line is discarded, never yielded
            n = min(chunk_bytes, pos)
            end = pos
            pos -= n
            f.seek(pos)
            data = f.read(n)
            read_total += n
            if b"\n" not in data:
                pieces.append(data)
                continue
            parts = data.split(b"\n")
            start = end - len(parts[-1])  # absolute offset of the last segment (its tail may continue into `pieces`)
            parts[-1] = parts[-1] + b"".join(reversed(pieces))
            pieces = [parts[0]]
            for i in range(len(parts) - 1, 0, -1):
                yield from reversed(lines_of(parts[i], start))
                start -= 1 + len(parts[i - 1])
        # pos == 0: what is left is the first physical line of the file
        yield from reversed(lines_of(b"".join(reversed(pieces)), 0))


def _iter_audit_lines_reversed(
    path: Path,
    max_lines: int | None = AUDIT_TAIL_MAX_LINES,
    chunk_bytes: int = AUDIT_TAIL_CHUNK_BYTES,
    max_bytes: int | None = AUDIT_TAIL_MAX_BYTES,
):
    """Yields the audit log's lines NEWEST FIRST without ever holding more than one chunk (plus one line).

    Line semantics are those of the former whole-file read,
    `path.read_text().strip().splitlines()[-max_lines:]`: text split with `str.splitlines()` (so "\\r\\n", "\\r"
    and the unicode separators split like before), trailing whitespace-only lines and the last line's trailing
    whitespace dropped, the first non-blank line's leading whitespace dropped, and the cap counted in lines
    (blank lines included). Splitting happens on the 0x0A byte BEFORE decoding, and 0x0A never occurs inside a
    multi-byte UTF-8 sequence, so a character straddling a chunk edge is always decoded whole.

    Deliberate differences, both only on corrupt input: invalid UTF-8 is replaced (U+FFFD) instead of aborting
    the whole export with UnicodeDecodeError, and `max_bytes` bounds how far back the read goes."""
    if max_lines is not None and max_lines < 0:
        raise ValueError("max_lines must be >= 0 or None")
    if max_lines == 0:
        return
    emitted = 0
    started = False  # the newest non-blank line has been seen (trailing blank lines are dropped, as strip() did)
    for line in _iter_raw_lines_reversed(path, chunk_bytes, max_bytes):
        if not started:
            if not line.strip():
                continue
            line = line.rstrip()
            started = True
        yield line
        emitted += 1
        if max_lines is not None and emitted >= max_lines:
            return


def _iter_audit_events_reversed(
    path: Path,
    max_lines: int | None = AUDIT_TAIL_MAX_LINES,
    chunk_bytes: int = AUDIT_TAIL_CHUNK_BYTES,
    max_bytes: int | None = AUDIT_TAIL_MAX_BYTES,
):
    """The parsed audit events, newest first, one at a time (unparseable lines skipped, as before). Nothing is
    accumulated here: callers keep only what they need."""
    if not path.exists():
        return
    for line in _iter_audit_lines_reversed(path, max_lines, chunk_bytes, max_bytes):
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _read_audit_tail(path: Path, max_lines: int = AUDIT_TAIL_MAX_LINES) -> list[dict[str, Any]]:
    """The last `max_lines` parsed audit events, oldest first, as a list. Kept for callers and tests that want
    the list; `main()` streams instead (see `_scan_audit`) so memory no longer grows with the log.

    `max_lines` is a safety ceiling against an unexpectedly huge audit log,
    not a normal limit -- it needs to comfortably cover HISTORY_MAX_AGE's
    worth of lines (audit_log.jsonl gets one line per symbol per cycle, so at
    a 5-minute poll interval and 16 symbols that's ~4,600 lines/day; 200,000
    covers 2 weeks with plenty of headroom before src.log_retention's own
    30-day prune ever gets a chance to trim it)."""
    return list(reversed(list(_iter_audit_events_reversed(path, max_lines))))


def _fmt_price(n: Any) -> str:
    """USD-formats a price the same way src/bot.py's Telegram messages do --
    $X,XXX.XX for $1+, more decimals below that (a sub-cent coin like SHIB
    would just read "$0.00" at a fixed 2 decimals)."""
    if n is None:
        return "?"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 1:
        return f"${n:,.2f}"
    if n >= 0.01:
        return f"${n:,.4f}"
    return f"${n:,.6f}"


def _role_detail(role_id: str, event: dict[str, Any] | None) -> str | None:
    """One human-readable line describing what a role's last matching audit
    event actually was -- shown live on the dashboard's center screen and in
    each desk's drawer, instead of just an idle/working dot."""
    if event is None:
        return None
    symbol = event.get("symbol", "?")

    if role_id == "market_analyst":
        signal = event.get("signal")
        if signal:
            return f"{symbol}: found a {signal.get('pattern')} setup ({signal.get('direction')})"
        candidates = event.get("candidates") or []
        if candidates:
            best = max(candidates, key=lambda c: c.get("strength", 0))
            return f"{symbol}: watching a possible {best.get('pattern')} setup"
        return f"{symbol}: checked, nothing worth trading"

    if role_id == "trade_strategist":
        plan = event.get("plan")
        if plan:
            zone = plan.get("entry_zone")
            zone_str = f"{_fmt_price(zone[0])} and {_fmt_price(zone[1])}" if zone else "?"
            return f"{symbol}: buy between {zone_str}, stop-loss {_fmt_price(plan.get('stop_loss'))}"
        return None

    if role_id == "risk_officer":
        decision = event.get("risk_decision")
        if decision:
            verdict = "OK to trade" if decision.get("allowed") else "blocked"
            return f"{symbol}: {verdict} — {decision.get('reason')}"
        return None

    if role_id == "compliance_reviewer":
        verdict = event.get("advisor_verdict")
        if verdict:
            plain = "approved" if verdict.get("verdict") == "APPROVE" else "vetoed"
            return f"{symbol}: {plain} (confidence {verdict.get('confidence_stars')}/5)"
        return None

    if role_id == "execution_trader":
        execution = event.get("execution")
        monitor_action = (event.get("monitor_action") or {}).get("action")
        if execution and monitor_action in ("STOP_LOSS", "TAKE_PROFIT"):
            reason = "stop-loss hit" if monitor_action == "STOP_LOSS" else "target reached"
            return f"{symbol}: sold — {reason} at {_fmt_price(execution.get('price'))}"
        if execution:
            return f"{symbol}: bought at {_fmt_price(execution.get('price'))}"
        return None

    return None


def _backtester_role(summary: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    """Builds the Backtester's card from scripts/backtest_summary.json's
    `candidates` array (the daily Routine's own output) instead of the audit
    log the other roles read from -- see the module docstring on
    BACKTESTER_RECENCY_WINDOW for why its "working" window is different."""
    candidates = (summary or {}).get("candidates") or []
    if not candidates:
        return {
            "id": "backtester",
            "label": "Backtester",
            "model_tier": "sonnet",
            "status": "idle",
            "last_active": None,
            "detail": "No candidates tested yet — daily sweep runs ~04:59 UTC",
        }

    # The most recent EVENT is either a test (tested_at) or a promotion
    # (promoted_at) -- a promotion is the more important thing to surface on
    # the card when it's the newer of the two, since it means live config
    # actually changed, not just that a backtest ran.
    promoted_candidates = [c for c in candidates if c.get("promoted_at")]
    latest_test = max(candidates, key=lambda c: c.get("tested_at") or "")
    latest_promotion = max(promoted_candidates, key=lambda c: c["promoted_at"]) if promoted_candidates else None

    test_ts = _parse_iso(latest_test.get("tested_at"))
    promo_ts = _parse_iso(latest_promotion["promoted_at"]) if latest_promotion else None

    if promo_ts is not None and (test_ts is None or promo_ts >= test_ts):
        last_active = latest_promotion["promoted_at"]
        event_ts = promo_ts
        config_key = latest_promotion.get("config_key", "?")
        config_value = latest_promotion.get("config_value", "?")
        detail = f"Promoted '{latest_promotion.get('name', '?')}' to live config — {config_key}={config_value}"
    else:
        last_active = latest_test.get("tested_at")
        event_ts = test_ts
        gates = latest_test.get("gates") or {}
        gate_keys = [k for k in gates if k != "overall"]
        if gates.get("overall") == "PASS":
            verdict = "passed all 5 gates ✓"
        elif gate_keys:
            passed = sum(1 for k in gate_keys if gates.get(k) == "PASS")
            verdict = f"{passed}/{len(gate_keys)} gates passed"
        else:
            verdict = "tested"
        name = latest_test.get("name", "?")
        dataset = latest_test.get("dataset", "?")
        detail = f"{name}: {verdict} ({dataset})"

    status = "idle"
    if event_ts is not None and now - event_ts <= BACKTESTER_RECENCY_WINDOW:
        status = "working"

    return {
        "id": "backtester",
        "label": "Backtester",
        "model_tier": "sonnet",
        "status": status,
        "last_active": last_active,
        "detail": detail,
    }


_MISSING = object()


def _scan_audit(
    events_newest_first,
    now: datetime,
    max_age: "timedelta | None" = None,
    max_items: "int | None" = None,
    collectors: "tuple[Any, ...]" = (),
) -> "AuditView":
    """ONE pass over the audit events, newest first, keeping only what the dashboard shows: the newest event
    (the Boss card's last_active), the newest event matching each role's marker keys, and the signal-history
    items. This is exactly what `_role_statuses` (a reversed scan per role, to the first match) and
    `_signal_history` (a reversed scan to its age/size cut-off) computed from the full list before -- same events
    inspected in the same order -- but nothing else is retained, so memory no longer grows with the log.

    The scan ends as soon as the history has hit its cut-off AND every role has been found; a role that never
    appears (e.g. the advisor when it is disabled) keeps it going to the end of the stream, which the reader
    bounds at AUDIT_TAIL_MAX_LINES.

    `collectors` (optional, empty by default = exactly the behaviour above) are extra one-event-at-a-time reducers
    with a `feed(event)` method and a `done` flag (see `_DecisionCollector`): they see the same newest-first
    stream, are not fed once `done`, and the scan also waits for them before stopping early."""
    history = _HistoryCollector(
        now,
        HISTORY_MAX_AGE if max_age is None else max_age,
        HISTORY_MAX_ITEMS if max_items is None else max_items,
    )
    pending = {role_id: marker_keys for role_id, _label, _tier, marker_keys in ROLE_MARKERS}
    last_by_role: dict[str, dict[str, Any]] = {}
    newest: Any = _MISSING
    for event in events_newest_first:
        if newest is _MISSING:
            newest = event
        for role_id in [r for r, keys in pending.items() if any(key in event for key in keys)]:
            last_by_role[role_id] = event
            del pending[role_id]
        if not history.done:
            history.feed(event)
        for collector in collectors:
            if not collector.done:
                collector.feed(event)
        if history.done and not pending and all(collector.done for collector in collectors):
            break
    return AuditView(newest=None if newest is _MISSING else newest, last_by_role=last_by_role, history=history.items)


class AuditView:
    """What `_scan_audit` keeps: see there."""

    def __init__(self, newest: Any, last_by_role: dict[str, Any], history: list[dict[str, Any]]) -> None:
        self.newest = newest
        self.last_by_role = last_by_role
        self.history = history


def _role_recency_window(timeframe: str | None) -> timedelta:
    """How recently a role's last audit event must be for the card to read "working". 15 minutes at 1h (and for
    any timeframe up to 6h: unchanged). At a daily bar the whole decision pass for a closed bar happens in the
    first hour after the close (the bot's own max_entry_delay guard is 60 min), so 15 minutes made the cards
    flicker working for a few minutes a day; it scales as timeframe/24 (1d -> 1 h). Display only."""
    seconds = _timeframe_seconds(timeframe)
    if seconds is None:
        return RECENCY_WINDOW
    return max(RECENCY_WINDOW, timedelta(seconds=seconds / 24))


def _role_statuses(
    events: list[dict[str, Any]],
    now: datetime,
    advisor_enabled: bool,
    backtest_summary: dict[str, Any] | None,
    recency_window: timedelta = RECENCY_WINDOW,
) -> list[dict[str, Any]]:
    """List form (oldest-first `events`), kept for callers and tests; `main()` uses `_role_statuses_from_view`."""
    view = _scan_audit(reversed(events), now)
    return _role_statuses_from_view(view, now, advisor_enabled, backtest_summary, recency_window)


def _role_statuses_from_view(
    view: "AuditView",
    now: datetime,
    advisor_enabled: bool,
    backtest_summary: dict[str, Any] | None,
    recency_window: timedelta = RECENCY_WINDOW,
) -> list[dict[str, Any]]:
    roles = []
    for role_id, label, model_tier, marker_keys in ROLE_MARKERS:
        last_event = view.last_by_role.get(role_id)
        status = "idle"
        last_active = None
        if last_event is not None and "timestamp" in last_event:
            last_active = last_event["timestamp"]
            try:
                ts = datetime.fromisoformat(last_active)
                if now - ts <= recency_window:
                    status = "working"
            except ValueError:
                pass
        detail = _role_detail(role_id, last_event)
        if role_id == "compliance_reviewer" and not advisor_enabled:
            # advisor.enabled: false in config.yaml -- this role never fires,
            # distinct from "idle" (which implies it's just between cycles).
            status = "disabled"
            detail = "Disabled — no ANTHROPIC_API_KEY configured in CI"
        roles.append(
            {
                "id": role_id,
                "label": label,
                "model_tier": model_tier,
                "status": status,
                "last_active": last_active,
                "detail": detail,
            }
        )
    working_specialists = [r for r in roles if r["status"] == "working"]
    newest = view.newest
    roles.insert(
        0,
        {
            "id": "boss",
            "label": "CEO / Orchestrator",
            "model_tier": "opus",
            "status": "working" if working_specialists else "idle",
            "last_active": newest["timestamp"] if newest is not None and "timestamp" in newest else None,
            "detail": (
                f"Coordinating {len(working_specialists)} active role(s) this cycle"
                if working_specialists
                else "Idle — waiting for the next scheduled cycle"
            ),
        },
    )
    roles.append(_backtester_role(backtest_summary, now))
    return roles


HISTORY_MAX_AGE = timedelta(days=14)
HISTORY_MAX_ITEMS = 3000  # safety ceiling, not a normal limit -- see HISTORY_MAX_AGE


class _HistoryCollector:
    """The loop body of the former `_signal_history`, fed one event at a time NEWEST FIRST. `done` flips when
    the old loop would have `break`-ed: an event older than the cutoff, or `max_items` items collected."""

    def __init__(self, now: datetime, max_age: timedelta, max_items: int) -> None:
        self.cutoff = now - max_age
        self.max_items = max_items
        self.items: list[dict[str, Any]] = []
        self.done = False

    def feed(self, event: dict[str, Any]) -> None:
        history = self.items
        symbol = event.get("symbol")
        ts = event.get("timestamp")
        if not symbol or not ts:
            return
        try:
            event_dt = datetime.fromisoformat(ts)
        except ValueError:
            return
        if event_dt.tzinfo is None:
            event_dt = event_dt.replace(tzinfo=timezone.utc)
        if event_dt < self.cutoff:
            # events arrive newest first, so everything from here on is even
            # older -- safe to stop scanning entirely, not just skip this one.
            self.done = True
            return

        if event.get("action") == "planned" and "plan" in event:
            plan = event["plan"]
            zone = plan.get("entry_zone")
            zone_str = f"{_fmt_price(zone[0])} and {_fmt_price(zone[1])}" if zone else "?"
            history.append(
                {
                    "timestamp": ts,
                    "symbol": symbol,
                    "kind": "signal",
                    "summary": f"Buy between {zone_str} — stop-loss {_fmt_price(plan.get('stop_loss'))} (sell here to cap the loss)",
                }
            )

        execution = event.get("execution")
        monitor_action = (event.get("monitor_action") or {}).get("action")
        if execution and monitor_action == "STOP_LOSS":
            history.append(
                {
                    "timestamp": ts,
                    "symbol": symbol,
                    "kind": "stop_loss",
                    "summary": f"Sold — stop-loss hit at {_fmt_price(execution.get('price'))}",
                }
            )
        elif execution and monitor_action == "TAKE_PROFIT":
            history.append(
                {
                    "timestamp": ts,
                    "symbol": symbol,
                    "kind": "take_profit",
                    "summary": f"Sold — target reached at {_fmt_price(execution.get('price'))}",
                }
            )
        elif execution:
            history.append(
                {
                    "timestamp": ts,
                    "symbol": symbol,
                    "kind": "filled",
                    "summary": f"Bought at {_fmt_price(execution.get('price'))}",
                }
            )

        risk_decision = event.get("risk_decision")
        if risk_decision and not risk_decision.get("allowed"):
            history.append(
                {
                    "timestamp": ts,
                    "symbol": symbol,
                    "kind": "rejected",
                    "summary": f"Skipped — risk check said no ({risk_decision.get('reason')})",
                }
            )

        advisor_verdict = event.get("advisor_verdict")
        if advisor_verdict and advisor_verdict.get("verdict") == "VETO" and not advisor_verdict.get("raw_error"):
            # Only a genuine content-based veto -- not the old "no API key" fallback
            # noise (raw_error set), which old audit logs may still contain.
            history.append(
                {
                    "timestamp": ts,
                    "symbol": symbol,
                    "kind": "vetoed",
                    "summary": f"Skipped — advisor said no ({advisor_verdict.get('reasoning')})",
                }
            )

        if event.get("daily_trend_confirmed") is False:
            history.append(
                {
                    "timestamp": ts,
                    "symbol": symbol,
                    "kind": "rejected",
                    "summary": "Skipped — daily trend not confirmed bullish (higher-timeframe filter)",
                }
            )

        if len(history) >= self.max_items:
            self.done = True


def _signal_history(
    events: list[dict[str, Any]], now: datetime, max_age: timedelta = HISTORY_MAX_AGE, max_items: int = HISTORY_MAX_ITEMS
) -> list[dict[str, Any]]:
    """A curated, human-readable timeline of the notable things that have
    happened recently -- signals planned, fills, closes, risk rejections,
    advisor vetoes -- as opposed to every single scanner tick. Powers the
    dashboard's "Signal History" view, which groups this by hour/day/week
    client-side (see web/app.js's renderHistoryFeed) -- up to `max_age` old,
    not a fixed item count, since a fixed count would only ever cover a few
    hours at this bot's cycle frequency. List form (oldest-first `events`);
    `main()` collects the same items while streaming (see `_scan_audit`)."""
    collector = _HistoryCollector(now, max_age, max_items)
    for event in reversed(events):
        collector.feed(event)
        if collector.done:
            break
    return collector.items


def _record_view(symbol: str, record, current_price: float | None) -> dict[str, Any]:
    plan = record.plan
    view = {
        "symbol": symbol,
        "pattern": plan.get("pattern"),
        "entry_zone": plan.get("entry_zone"),
        "stop_loss": plan.get("stop_loss"),
        "take_profit": plan.get("take_profit"),
        "invalidation": plan.get("invalidation"),
        "risk_reward": plan.get("risk_reward"),
        "confidence_rule_based": plan.get("rule_based_confidence_stars"),
        "confidence_claude": (record.claude_verdict or {}).get("confidence_stars"),
        "fill_price": record.fill_price,
        "fill_amount": record.fill_amount,
        "current_price": current_price,
        # what "When" means for this row: an OPEN position's fill time, or
        # a PENDING plan's plan time -- used for both display and sorting
        # newest-first in the dashboard's Active signals table.
        "timestamp": record.filled_at or record.created_at,
    }
    if record.status == "OPEN" and record.fill_price and current_price:
        view["unrealized_pnl_pct"] = (current_price - record.fill_price) / record.fill_price
    return view


# --------------------------------------------------------------------------- #
# `ops`: the additive block the redesigned dashboard (web/app.js) reads
# --------------------------------------------------------------------------- #
# Everything the page needs that the original export did not carry: instance identity, the per-symbol decision for
# the latest bar, per-trade R (as booked and re-costed), an equity curve, health counters, the kill-switch file,
# the newest parity report. It is ADDED under one new key; every pre-existing key is unchanged, and a failure while
# building it only drops this key (main() warns on stderr and still writes everything else). Nothing here touches
# trading: this script only reads the bot's files (and appends to its own small equity-sample log).
#
# Memory: the audit log is only ever read through `_scan_audit`'s newest-first stream (the tail-read of #107), with
# one more reducer (`_DecisionCollector`) that keeps two events per symbol and a few counters. trades.csv and the
# plan history are small (a few lines per trade) and are streamed line by line.
OPS_SCHEMA = 1
OPS_HEALTH_WINDOW = timedelta(days=7)
# The cost tier the paper daily trial is JUDGED at: Kraken T3 taker, 0.38% fee + 0.05% slippage per side
# (docs/STATUS.md "fee tier the trial is judged at stays Kraken T3 taker"; deploy/PAPER_TRIAL_RUNBOOK.md section 7;
# the same numbers as strategy_pack_run/engine.py's COST_PRESETS["kraken_T3_taker"]). The kill-rule thresholds are
# defined on this re-costed R, so the exported R series is re-costed with exactly the harness's formula.
RECOST_FEE = 0.0038
RECOST_SLIPPAGE = 0.0005
RECOST_LABEL = "Kraken T3 taker: 0.38% fee + 0.05% slippage per side"
CLOSED_TRADES_EXPORTED = 50  # rows for the "recent closed trades" table
R_SERIES_MAX = 1000  # every trade's R in close order (the kill rule needs the first 20/30/40/50)
PLAN_MATCH_TOLERANCE = timedelta(minutes=10)  # trades.csv row vs plan-history record, same symbol, closest in time
EQUITY_SAMPLE_MIN_GAP = timedelta(hours=6)
EQUITY_POINTS_MAX = 1500
DECISION_REASON_MAX = 240


def _clip(text: Any, limit: int = DECISION_REASON_MAX) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _note_of(event: dict[str, Any]) -> str:
    note = event.get("note")
    return note if isinstance(note, str) else ""


def _is_unavailable(event: dict[str, Any]) -> bool:
    return "market data unavailable" in str(event.get("error") or "")


def _classify_decision(event: dict[str, Any]) -> tuple[str, str]:
    """(state, reason) for one audit event, from what src/bot.py wrote on it. States: `scan` (scanned, nothing to
    trade), `skip` (deliberately not acted on), `holding` (open position / pending plan), `blocked` (a gate said no),
    `entered` / `exited` (a fill this bar), `error`, `nodata`. The reason is the audit's own wording, never invented."""
    err = event.get("error")
    if err:
        if _is_unavailable(event):
            return "nodata", "market data unavailable this cycle"
        return "error", _clip(f"error: {err}")
    note = _note_of(event)
    execution = event.get("execution") if isinstance(event.get("execution"), dict) else None
    monitor = event.get("monitor_action")
    monitor_action = monitor.get("action") if isinstance(monitor, dict) else None
    if execution and monitor_action == "STOP_LOSS":
        return "exited", f"stop-loss hit, sold at {_fmt_price(execution.get('price'))}"
    if execution and monitor_action == "TAKE_PROFIT":
        return "exited", f"target reached, sold at {_fmt_price(execution.get('price'))}"
    if execution and monitor_action == "FILL":
        return "entered", f"pending plan filled at {_fmt_price(execution.get('price'))}"
    if event.get("action") == "planned":
        if execution:
            return "entered", f"bought at {_fmt_price(execution.get('price'))}"
        return "holding", "plan queued, waiting for price to reach the entry zone"
    if note.startswith("OPEN plan active"):
        return "holding", "open position, no stop/target trigger this cycle"
    if note.startswith("PENDING plan active"):
        return "holding", "pending plan, no entry trigger this cycle"
    if note.startswith("stale bar"):
        delay = event.get("entry_delay_minutes")
        late = f" ({delay} min after the close)" if delay is not None else ""
        return "skip", f"stale bar: entries skipped{late}"
    if note.startswith("bearish signal"):
        return "skip", "bearish signal logged only (long-only bot)"
    if "monitor-only" in note:
        return "skip", "monitor-only symbol (not in the tradeable universe)"
    if note.startswith("blocked:"):
        return "blocked", _clip(note[len("blocked:"):].strip())
    decision = event.get("risk_decision")
    if isinstance(decision, dict) and not decision.get("allowed"):
        return "blocked", _clip(f"risk check said no: {decision.get('reason')}")
    verdict = event.get("advisor_verdict")
    if isinstance(verdict, dict) and verdict.get("verdict") == "VETO":
        return "blocked", _clip(f"advisor veto: {verdict.get('reasoning')}")
    if event.get("daily_trend_confirmed") is False:
        return "blocked", "daily trend not confirmed bullish (higher-timeframe filter)"
    if isinstance(event.get("signal"), dict):
        return "skip", _clip(note) if note else "signal found but no plan could be built"
    candidates = event.get("candidates")
    if isinstance(candidates, list) and candidates:
        parts = [f"{c.get('pattern')} {c.get('strength')}" for c in candidates[:3] if isinstance(c, dict)]
        return "scan", _clip(f"{len(candidates)} candidate(s) ({', '.join(parts)}), no signal")
    if "candidates" in event:
        return "scan", "scanned: no setup"
    return "skip", _clip(note or "no action recorded")


def _pattern_of(event: dict[str, Any]) -> str | None:
    signal = event.get("signal")
    if isinstance(signal, dict) and signal.get("pattern"):
        return str(signal["pattern"])
    plan = event.get("plan")
    if isinstance(plan, dict) and plan.get("pattern"):
        return str(plan["pattern"])
    candidates = [c for c in (event.get("candidates") or []) if isinstance(c, dict)] if isinstance(event.get("candidates"), list) else []
    best = max(candidates, key=lambda c: _num(c.get("strength")) or 0, default=None)
    return str(best["pattern"]) if best and best.get("pattern") else None


class _DecisionCollector:
    """Fed the audit events newest first (see `_scan_audit`). Keeps, per symbol, only the newest event and the newest
    event that is not an "already decided" echo (the deciding line, or a held symbol's monitor line), plus counters
    for the latest bar and for the last OPS_HEALTH_WINDOW. A "market data unavailable" line is never the deciding
    event (a one-cycle outage must not hide the bar's real decision); it only flags the symbol.

    `done` flips once an event is older than the window. Timestamps are compared as ISO strings: the bot writes
    them as UTC `datetime.isoformat()`, which sorts chronologically (the window is a display bound, so an odd
    timestamp format can only move its edge, never change a decision)."""

    def __init__(self, now: datetime, window: timedelta = OPS_HEALTH_WINDOW) -> None:
        self.cutoff_iso = (now - window).isoformat()
        self.window = window
        self.done = False
        self.newest: dict[str, dict[str, Any]] = {}
        self.deciding: dict[str, dict[str, Any]] = {}
        self.latest_bar: str | None = None
        self.latest_decided: set[str] = set()
        self.latest_stale = 0
        self.latest_errors = 0
        self.events = 0
        self.stale_skips = 0
        self.errors = 0
        self.unavailable = 0
        self.oldest: str | None = None

    def feed(self, event: dict[str, Any]) -> None:
        ts = event.get("timestamp")
        if isinstance(ts, str) and ts < self.cutoff_iso:
            self.done = True
            return
        symbol = event.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            return
        note = _note_of(event)
        err = event.get("error")
        unavailable = bool(err) and _is_unavailable(event)
        stale = note.startswith("stale bar")
        self.events += 1
        if isinstance(ts, str):
            self.oldest = ts
        self.stale_skips += stale
        self.unavailable += unavailable
        self.errors += bool(err) and not unavailable
        bar = event.get("bar_close")
        if isinstance(bar, str) and self.latest_bar is None:
            self.latest_bar = bar
        if isinstance(bar, str) and bar == self.latest_bar:
            if not err:
                self.latest_decided.add(symbol)
            self.latest_stale += stale
            self.latest_errors += bool(err) and not unavailable
        if symbol not in self.newest:
            self.newest[symbol] = event
        if "already decided" not in note and not unavailable and symbol not in self.deciding:
            self.deciding[symbol] = event

    def rows(self, symbols: list[str]) -> list[dict[str, Any]]:
        ordered = list(symbols) + sorted(s for s in set(self.newest) | set(self.deciding) if s not in symbols)
        rows = []
        for symbol in ordered:
            event = self.deciding.get(symbol)
            newest = self.newest.get(symbol)
            if event is None:
                reason = "market data unavailable this cycle" if newest is not None else "no audit line for this symbol in the last 7 days"
                state, event = "nodata", (newest or {})
            else:
                state, reason = _classify_decision(event)
            row = {
                "symbol": symbol,
                "state": state,
                "reason": reason,
                "bar_close": event.get("bar_close"),
                "timestamp": event.get("timestamp"),
                "pattern": _pattern_of(event),
            }
            if newest is not None and _is_unavailable(newest):
                row["data_unavailable"] = True
            rows.append(row)
        return rows

    def health(self, expected_symbols: int) -> dict[str, Any]:
        return {
            "expected_symbols": expected_symbols,
            "latest_bar": {
                "bar_close": self.latest_bar,
                "symbols_decided": len(self.latest_decided),
                "stale_skips": self.latest_stale,
                "errors": self.latest_errors,
            },
            "window": {
                "days": self.window.days,
                "since": self.oldest,
                "events": self.events,
                "stale_skips": self.stale_skips,
                "errors": self.errors,
                "unavailable": self.unavailable,
            },
        }


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _load_round_trips(trade_log_path: Path) -> list[dict[str, Any]]:
    """Closed round-trips from trades.csv, oldest first: the same pairing as src.pnl.load_closed_trades (the most
    recent open BUY per symbol with the next SELL), but keeping the prices and the base amount the R maths needs.
    A malformed row is skipped (never aborts the export)."""
    if not trade_log_path.exists():
        return []
    open_buys: dict[str, dict[str, Any]] = {}
    trips: list[dict[str, Any]] = []
    with open(trade_log_path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                symbol, side = row["symbol"], row["side"]
                when = _parse_iso(row["timestamp"]) if isinstance(row["timestamp"], str) else None
                price, base, quote = float(row["price"]), float(row["base_amount"]), float(row["quote_amount"])
            except (KeyError, ValueError, TypeError):
                continue
            if when is None:
                continue
            if side == "buy":
                open_buys[symbol] = {"time": when, "price": price, "base": base, "quote": quote}
            elif side == "sell" and symbol in open_buys:
                buy = open_buys.pop(symbol)
                cost = buy["quote"]
                trips.append(
                    {
                        "symbol": symbol,
                        "opened_at": buy["time"],
                        "closed_at": when,
                        "entry": buy["price"],
                        "exit": price,
                        "base": buy["base"],
                        "pnl": quote - cost,
                        "pnl_pct": (quote - cost) / cost if cost else 0.0,
                    }
                )
    return trips


def _closed_plan_records(history_path: Path, keep: int = 5000) -> list[dict[str, Any]]:
    """The CLOSED, actually-filled records of the plan history (trade_plans_history.jsonl), as compact dicts. The
    file is streamed line by line; only the few fields the R maths needs are kept, newest `keep` records at most."""
    if not history_path.exists():
        return []
    kept: deque[dict[str, Any]] = deque(maxlen=keep)
    with open(history_path, errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or rec.get("status") != "CLOSED" or rec.get("fill_price") is None:
                continue
            plan = rec.get("plan") if isinstance(rec.get("plan"), dict) else {}
            zone = plan.get("entry_zone")
            entry_high = _num(zone[1]) if isinstance(zone, (list, tuple)) and len(zone) == 2 else None
            stop = _num(plan.get("stop_loss"))
            kept.append(
                {
                    "symbol": rec.get("symbol"),
                    "closed_at": _parse_iso(rec["closed_at"]) if isinstance(rec.get("closed_at"), str) else None,
                    "reason": rec.get("close_reason"),
                    "risk": (entry_high - stop) if entry_high is not None and stop is not None else None,
                }
            )
    return list(kept)


def _r_recosted(entry: float, exit_: float, risk: float | None, fee: float, slippage: float) -> float | None:
    """R of one round-trip at a given cost tier, with strategy_pack_run's definition (diag_1h_edge.py /
    backtest_live_bot.record_trade): per unit of quote spent, qty = (1 - fee) / (entry * (1 + slippage)), the exit
    fills at exit * (1 - slippage) less the fee, and R = net / (qty * planned risk), the planned risk per unit being
    entry_zone high - stop_loss. Scale-free, so it needs only the two raw prices and the plan's risk."""
    if risk is None or risk <= 0 or entry <= 0 or exit_ <= 0:
        return None
    qty = (1 - fee) / (entry * (1 + slippage))
    net = qty * (exit_ * (1 - slippage)) * (1 - fee) - 1.0
    return net / (qty * risk)


def _trade_stats(trips: list[dict[str, Any]], plans: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-trade R for every closed round-trip (booked = the paper broker's actual P/L over the planned risk;
    re-costed = at RECOST_*), the recent rows for the table, and the roll-ups. A trip with no matching plan record
    (or a plan with no usable risk) has no R: counted in `r_missing`, never guessed."""
    unused = list(range(len(plans)))
    rows: list[dict[str, Any]] = []
    for trip in sorted(trips, key=lambda t: t["closed_at"]):
        best_i, best_gap = None, None
        for i in unused:
            plan = plans[i]
            if plan["symbol"] != trip["symbol"] or plan["closed_at"] is None:
                continue
            gap = abs(plan["closed_at"] - trip["closed_at"])
            if gap <= PLAN_MATCH_TOLERANCE and (best_gap is None or gap < best_gap):
                best_i, best_gap = i, gap
        plan = plans[best_i] if best_i is not None else None
        if best_i is not None:
            unused.remove(best_i)
        risk = plan["risk"] if plan else None
        r_booked = trip["pnl"] / (trip["base"] * risk) if risk and risk > 0 and trip["base"] > 0 else None
        r_recost = _r_recosted(trip["entry"], trip["exit"], risk, RECOST_FEE, RECOST_SLIPPAGE)
        rows.append(
            {
                "symbol": trip["symbol"],
                "opened_at": trip["opened_at"].isoformat(),
                "closed_at": trip["closed_at"].isoformat(),
                "reason": plan["reason"] if plan else None,
                "entry": trip["entry"],
                "exit": trip["exit"],
                "pnl": round(trip["pnl"], 2),
                "pnl_pct": round(trip["pnl_pct"], 4),
                "r_booked": None if r_booked is None else round(r_booked, 4),
                "r_recosted": None if r_recost is None else round(r_recost, 4),
            }
        )
    n = len(rows)
    booked = [r["r_booked"] for r in rows if r["r_booked"] is not None]
    recost = [r["r_recosted"] for r in rows if r["r_recosted"] is not None]
    wins = sum(1 for r in rows if r["pnl"] > 0)
    return {
        "closed_trades": rows[-CLOSED_TRADES_EXPORTED:][::-1],  # newest first
        "r_recosted_series": [r["r_recosted"] for r in rows[-R_SERIES_MAX:]],  # close order, None where unknown
        "stats": {
            "n": n,
            "wins": wins,
            "win_rate": round(wins / n, 4) if n else None,
            "avg_r_booked": round(sum(booked) / len(booked), 4) if booked else None,
            "avg_r_recosted": round(sum(recost) / len(recost), 4) if recost else None,
            "r_known": len(recost),
            "r_missing": n - len(recost),
        },
    }


def _read_equity_samples(path: Path) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    try:
        if not path.is_file():
            return []
        for line in _iter_audit_lines_reversed(path, EQUITY_POINTS_MAX):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and isinstance(item.get("t"), str) and _num(item.get("equity")) is not None:
                samples.append(item)
    except OSError as exc:  # unreadable sample log: the curve restarts from the seed instead of failing the export
        print(f"warning: couldn't read {path}: {exc}", file=sys.stderr)
        return []
    samples.reverse()
    return samples


def _first_event_time(audit_path: Path) -> datetime | None:
    """Timestamp of the audit log's first parseable line (reads at most the first few lines): the trial's start."""
    if not audit_path.exists():
        return None
    with open(audit_path, errors="replace") as f:
        for _ in range(50):
            line = f.readline()
            if not line:
                return None
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _parse_iso(event["timestamp"]) if isinstance(event, dict) and isinstance(event.get("timestamp"), str) else None
            if ts is not None:
                return ts
    return None


def _equity_samples_to_store(
    samples: list[dict[str, Any]], now: datetime, equity: float, cash: float, seed_at: datetime | None, starting_cash: float | None
) -> list[dict[str, Any]]:
    """Which samples to append to the sample log this run. The portfolio is only held in memory by the bot, so the
    only mark-to-market history there can ever be is the one this exporter keeps: a sample whenever (equity, cash)
    changed since the last one or EQUITY_SAMPLE_MIN_GAP passed (a daily bar moves prices once a day, so this is a
    handful of lines per day). The very first run also writes a seed at the audit log's first line, at the starting
    cash: before the first event nothing had been traded, by construction."""
    new: list[dict[str, Any]] = []
    if not samples and seed_at is not None and starting_cash is not None and seed_at < now:
        new.append({"t": seed_at.isoformat(), "equity": round(starting_cash, 2), "cash": round(starting_cash, 2), "seed": True})
    last = (new or samples or [None])[-1]
    sample = {"t": now.isoformat(), "equity": round(equity, 2), "cash": round(cash, 2)}
    last_t = _parse_iso(last["t"]) if last else None
    changed = last is None or (last.get("equity"), last.get("cash")) != (sample["equity"], sample["cash"])
    if changed or last_t is None or now - last_t >= EQUITY_SAMPLE_MIN_GAP:
        new.append(sample)
    return new


def _equity_curve(
    path: Path, now: datetime, equity: float, cash: float, seed_at: datetime | None, starting_cash: float | None
) -> dict[str, Any]:
    samples = _read_equity_samples(path)
    if seed_at is not None:
        # Samples older than the audit log's first line belong to an earlier run of the portfolio (the log was reset
        # or archived, scripts/reset_portfolio.py): they would draw a curve for a portfolio that no longer exists.
        # (With log retention on, the audit log's start slides and so does this window; the paper trial keeps it off.)
        samples = [x for x in samples if (_parse_iso(x["t"]) or seed_at) >= seed_at]
    new = _equity_samples_to_store(samples, now, equity, cash, seed_at, starting_cash)
    if new:
        try:
            with open(path, "a") as f:
                for item in new:
                    f.write(json.dumps(item) + "\n")
        except OSError as exc:  # the curve is a nicety: a read-only/full disk only means this run's sample is not kept
            print(f"warning: couldn't append to {path}: {exc}", file=sys.stderr)
    stored = samples + new
    points = [[s["t"], s["equity"], s.get("cash")] for s in stored]
    if not stored or stored[-1]["t"] != now.isoformat():  # always end the curve at "now" with the live equity
        points.append([now.isoformat(), round(equity, 2), round(cash, 2)])
    return {
        "since": points[0][0] if points else None,
        "source": "samples kept by the dashboard export (mark-to-market at each export), seeded at the audit log's first line with the starting cash",
        "points": points,
    }


def _load_parity_summary(directory: Path) -> dict[str, Any] | None:
    """The newest `parity_*.json` (the weekly scripts/paper_parity_check.py --json-out report, runbook section 6),
    reduced to what the page shows. None when there is none or it cannot be read: the page then says so."""
    try:
        files = sorted(directory.glob("parity_*.json"))
    except OSError:
        return None
    for path in reversed(files):
        try:
            report = json.loads(path.read_text())
            if not isinstance(report, dict) or not report.get("status"):
                continue
            window = report.get("window") if isinstance(report.get("window"), dict) else None
            return {
                "status": str(report["status"]),
                "message": _clip(report.get("message") or ""),
                "window": {k: window.get(k) for k in ("since", "until", "decided_bars")} if window else None,
                "trial_start": report.get("trial_start"),
                "budget": report.get("budget"),
                "file": path.name,
                "file_modified": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
            }
        except (OSError, ValueError):
            continue
    return None


def _timeframe_word(timeframe: str | None) -> str:
    return {"1d": "daily", "1w": "weekly", "1M": "monthly"}.get(timeframe or "", timeframe or "bot")


def _build_ops(
    cfg: Any,
    now: datetime,
    audit_view: "AuditView",
    decisions: "_DecisionCollector",
    *,
    audit_path: Path,
    trade_log_path: Path,
    plan_history_path: Path,
    equity: float,
    cash: float,
    instance_id: str | None,
    instance_name: str | None,
) -> dict[str, Any]:
    starting_cash = _num(cfg.risk.get("starting_cash_usd"))
    newest = audit_view.newest if isinstance(audit_view.newest, dict) else {}
    stats = _trade_stats(_load_round_trips(trade_log_path), _closed_plan_records(plan_history_path))
    kill_file = cfg.risk.get("kill_switch_file", "KILL_SWITCH")
    health = decisions.health(len(cfg.symbols))
    health.update(
        last_event_at=newest.get("timestamp"),
        last_bar_close=decisions.latest_bar,
        poll_interval_seconds=cfg.loop.get("poll_interval_seconds"),
        max_entry_delay_minutes=cfg.loop.get("max_entry_delay_minutes"),
    )
    if os.environ.get(PUSH_MINUTES_ENV) is not None:
        # Additive: how often the VM pushes this file (scripts/run_live.py sets the variable for its export). The page
        # scales its "updated N min ago" amber/red thresholds from it; absent -> the page's 15/60 minute defaults.
        health["push_interval_minutes"] = parse_push_minutes(os.environ.get(PUSH_MINUTES_ENV))
    return {
        "schema": OPS_SCHEMA,
        "instance": {
            "id": instance_id or _timeframe_word(cfg.timeframe),
            "name": instance_name or f"{_timeframe_word(cfg.timeframe).capitalize()} bot",
            "label": cfg.notifier_label,
            "mode": cfg.mode,
            "timeframe": cfg.timeframe,
            "exchange_id": cfg.exchange_id,
        },
        "starting_cash": starting_cash,
        "kill_switch": {"present": (PROJECT_ROOT / kill_file).exists(), "file": Path(kill_file).name},
        "recost": {"label": RECOST_LABEL, "fee": RECOST_FEE, "slippage": RECOST_SLIPPAGE},
        "trade_stats": stats["stats"],
        "r_recosted_series": stats["r_recosted_series"],
        "closed_trades": stats["closed_trades"],
        "decisions": decisions.rows(cfg.symbols),
        "health": health,
        "equity_curve": _equity_curve(
            audit_path.parent / "dashboard_equity.jsonl", now, equity, cash, _first_event_time(audit_path), starting_cash
        ),
        "parity": _load_parity_summary(audit_path.parent),
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export the bot's live state to a dashboard JSON file (default web/data.json). All options are "
        "for running a SECOND bot instance (see web/instances.json); with none, the export is exactly as before."
    )
    parser.add_argument("--config", help="config.yaml of the bot instance to export (default: config/config.yaml)")
    parser.add_argument("--out", help="output file (default: web/data.json)")
    parser.add_argument("--instance-id", help="short id for ops.instance.id (default: derived from market.timeframe)")
    parser.add_argument("--instance-name", help="display name for ops.instance.name (default: '<Timeframe> bot')")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    # argv is None for the programmatic/test call and the VM's `python scripts/export_dashboard_data.py`
    # (the __main__ block below passes sys.argv[1:]); nothing here parses sys.argv on its own.
    args = _parse_args(argv) if argv is not None else argparse.Namespace(config=None, out=None, instance_id=None, instance_name=None)
    cfg = load_config(args.config) if args.config else load_config()
    log_cfg = cfg.logging_cfg
    now = datetime.now(timezone.utc)

    store = TradePlanStore(
        current_path=PROJECT_ROOT / log_cfg.get("trade_plan_current_path", "logs/trade_plans_current.json"),
        history_path=PROJECT_ROOT / log_cfg.get("trade_plan_history_path", "logs/trade_plans_history.jsonl"),
    )
    # Streamed newest-first and reduced as it goes (see _scan_audit): the whole audit log is never in memory.
    audit_path = PROJECT_ROOT / log_cfg.get("audit_log_path", "logs/audit_log.jsonl")
    decisions = _DecisionCollector(now)  # feeds the additive `ops` block below
    audit_view = _scan_audit(_iter_audit_events_reversed(audit_path), now, collectors=(decisions,))
    trade_log_path = PROJECT_ROOT / log_cfg.get("trade_log_path", "logs/trades.csv")
    cash = reconstruct_cash(trade_log_path, cfg.risk["starting_cash_usd"])
    pnl_report = build_pnl_report(trade_log_path, now)
    risk_report = build_risk_report(trade_log_path, cfg.risk["starting_cash_usd"], cfg.risk["min_risk_reward"], now)

    records = store.all_current()

    # Current prices (last closed bar's close) + OHLCV candles, read from the
    # snapshot src/market_data.py's MarketDataCache already wrote this cycle
    # -- this script makes zero exchange calls of its own now (previously an
    # independent fetch_ticker/fetch_ohlcv path with its own, uncoordinated
    # ccxt rate limiter; see docs/system-redesign-2026-10.md §4.1/§4.3). The
    # dashboard's chart is a live TradingView widget by default, but
    # TradingView's free widget has no JS API for drawing custom price lines
    # -- so for a symbol with an active plan, the dashboard swaps in its own
    # annotated chart (built from these candles) that can actually show the
    # entry/stop/target/invalidation levels.
    snapshot_path = PROJECT_ROOT / log_cfg.get("market_snapshot_path", "logs/market_snapshot.json")
    current_prices, charts = _load_market_snapshot(snapshot_path, now)

    positions = [_record_view(sym, rec, current_prices.get(sym)) for sym, rec in records.items() if rec.status == "OPEN"]
    pending = [_record_view(sym, rec, current_prices.get(sym)) for sym, rec in records.items() if rec.status == "PENDING"]
    # Newest signal first -- ISO8601 UTC timestamps sort correctly as plain strings.
    positions.sort(key=lambda p: p["timestamp"] or "", reverse=True)
    pending.sort(key=lambda p: p["timestamp"] or "", reverse=True)

    equity = cash + sum((p["current_price"] or p["fill_price"] or 0) * (p["fill_amount"] or 0) for p in positions)
    backtest_summary = _load_backtest_summary()
    # Role status (_role_statuses below) reads the raw summary -- the
    # Backtester's working/idle state should reflect the latest test or
    # promotion regardless of whether that candidate is still visible on the
    # dashboard's Failed tab. Only the exported "backtest" payload itself gets
    # the stale-FAILED filter applied, for the Testing modal's candidate list.
    visible_backtest = (
        {**backtest_summary, "candidates": _visible_candidates(backtest_summary, now)}
        if backtest_summary
        else None
    )

    data = {
        "generated_at": now.isoformat(),
        "mode": cfg.mode,
        "exchange_id": cfg.exchange_id,
        "symbols": cfg.symbols,
        "portfolio": {
            "cash": round(cash, 2),
            "equity": round(equity, 2),
            "pnl_pct": round((equity - cfg.risk["starting_cash_usd"]) / cfg.risk["starting_cash_usd"], 4),
        },
        "roles": _role_statuses_from_view(
            audit_view, now, cfg.advisor.get("enabled", False), backtest_summary, _role_recency_window(cfg.timeframe)
        ),
        "positions": positions,
        "pending": pending,
        "signal_history": audit_view.history,
        "charts": charts,
        "pnl": pnl_report,
        "risk": risk_report,
        "backtest": visible_backtest,
    }
    display = _display_block(cfg.timeframe, cfg.notifier_label, charts)
    if display is not None:
        data["display"] = display

    # Additive block for the redesigned page. Built last and on its own: if anything in it raises, the key is simply
    # missing (the page says "no data" for what it needs) and every field above is still written.
    try:
        data["ops"] = _build_ops(
            cfg,
            now,
            audit_view,
            decisions,
            audit_path=audit_path,
            trade_log_path=trade_log_path,
            plan_history_path=PROJECT_ROOT / log_cfg.get("trade_plan_history_path", "logs/trade_plans_history.jsonl"),
            equity=equity,
            cash=cash,
            instance_id=args.instance_id,
            instance_name=args.instance_name,
        )
    except Exception as exc:  # noqa: BLE001 — the new block must never cost the dashboard its existing data
        print(f"warning: couldn't build the ops block, exporting without it: {type(exc).__name__}: {exc}", file=sys.stderr)

    out_path = Path(args.out) if args.out else OUT_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main(sys.argv[1:])
