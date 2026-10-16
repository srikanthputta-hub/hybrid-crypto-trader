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

Portfolio itself (src/portfolio.py) is in-memory only and doesn't survive a
process restart, so "cash" here is reconstructed the same way src/bot.py
reconstructs it at startup: replaying logs/trades.csv from the configured
starting balance (see src.portfolio.reconstruct_cash).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.pnl import build_pnl_report
from src.risk_analytics import build_risk_report
from src.portfolio import reconstruct_cash
from src.trade_plan_store import TradePlanStore

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
) -> "AuditView":
    """ONE pass over the audit events, newest first, keeping only what the dashboard shows: the newest event
    (the Boss card's last_active), the newest event matching each role's marker keys, and the signal-history
    items. This is exactly what `_role_statuses` (a reversed scan per role, to the first match) and
    `_signal_history` (a reversed scan to its age/size cut-off) computed from the full list before -- same events
    inspected in the same order -- but nothing else is retained, so memory no longer grows with the log.

    The scan ends as soon as the history has hit its cut-off AND every role has been found; a role that never
    appears (e.g. the advisor when it is disabled) keeps it going to the end of the stream, which the reader
    bounds at AUDIT_TAIL_MAX_LINES."""
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
        if history.done and not pending:
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


def main() -> None:
    cfg = load_config()
    log_cfg = cfg.logging_cfg
    now = datetime.now(timezone.utc)

    store = TradePlanStore(
        current_path=PROJECT_ROOT / log_cfg.get("trade_plan_current_path", "logs/trade_plans_current.json"),
        history_path=PROJECT_ROOT / log_cfg.get("trade_plan_history_path", "logs/trade_plans_history.jsonl"),
    )
    # Streamed newest-first and reduced as it goes (see _scan_audit): the whole audit log is never in memory.
    audit_view = _scan_audit(
        _iter_audit_events_reversed(PROJECT_ROOT / log_cfg.get("audit_log_path", "logs/audit_log.jsonl")), now
    )
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

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
