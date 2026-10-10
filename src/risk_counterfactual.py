"""Counterfactual replay of trade plans the risk engine REJECTED.

Question this answers: for each risk rule (cooldown, volatility cap, R:R
floor, exposure/sizing cap, daily-loss halt, ...), when it said "no", would
the rejected plan have made or lost money if it had been taken instead? A rule
whose rejected plans would mostly have lost money is earning its keep; a rule
whose rejected plans would mostly have won *may* be costing opportunity.

Inputs: rejection events from logs/audit_log.jsonl (any event whose
`risk_decision.allowed is False`; src/bot.py writes `event["plan"]` before
the risk check, so every rejection carries the full plan) plus real,
subsequent, CLOSED OHLCV candles from the exchange (read-only).

This is diagnostic/reporting only. It never feeds back into
src/risk_engine.py, never places orders, never touches portfolio/position
state, config, or any file under logs/ other than reading the audit log. It
does not import src.risk_engine or src.trade_planner at all. It works on the
plain dicts the audit log already holds.

WHAT THE R-MULTIPLES HERE ARE, AND ARE NOT
------------------------------------------
Every R-multiple in this module is GROSS: no fees, no slippage, no funding.
That is unlike src/pnl.py's realized P/L, which is what was actually booked.
Read these numbers as "directional only". A small positive net R here can
easily turn negative once costs are included. A negative net R is the more
robust signal, because costs only make it worse. This is not a backtest and
must not be quoted as one.

Other known limitations, all deliberate:
  - Each rejected plan is replayed independently. The real bot holds at most
    one plan per symbol and is capped by cash and total exposure, so the sum of
    counterfactual R is "if every rejected plan had been taken in isolation",
    not a portfolio simulation. Windows for the same symbol can overlap, so
    rejections are not statistically independent samples.
  - No statistical-significance claim is made. The per-reason verdict is a
    sign check on net gross R, gated on MIN_REJECTIONS_FOR_STATS.
  - A rule looking "costly" here is evidence to review, NOT an instruction to
    loosen it. Kill switch and daily-loss halt exist to cap tail risk, not to
    maximize expected R. Any change to src/risk_engine.py goes through the
    normal process (written reason + out-of-sample test).

REPLAY MODEL (no look-ahead)
----------------------------
Let T be the rejection event's timestamp. src/bot.py builds the plan from
bars that had already closed before T, so the replay window starts at the
first candle whose OPEN time is >= T. The candle that was still forming at T
is excluded, because part of its range happened before the plan existed. Only
closed candles are ever used (ExchangeClient.fetch_ohlcv_df drops the forming
bar). The window is at most `lookahead_bars` candles long.

  1. Entry (mirrors src/monitor.check_pending, which acts on a closed bar's
     close). For each window bar in order, look at its CLOSE:
       - close <= invalidation  -> the bot would have DISCARDED the plan;
                                   not entered (not_entered_reason="invalidated").
       - entry_low <= close <= entry_high -> entry triggered by that closed bar;
                                   filled at the NEXT bar's OPEN (next-bar-open
                                   discipline, per CLAUDE.md). A bar's close is
                                   never used as a fill price inside that same bar.
     If the window runs out first, or the trigger is the window's last bar
     (no next bar to fill on), the result is not entered
     (not_entered_reason="window_ended").
  2. Exit, starting with the fill bar itself (its whole range comes after the
     fill at its open). stop_loss and take_profit are levels known in advance,
     so an intrabar touch counts:
       - low  <= stop_loss   -> "stop_loss"; exit at stop_loss, or at the bar's
                                open if it gapped below the stop (worse fill).
                                If the fill itself was already at or below the
                                stop, exit at that bar's LOW, so the loss isn't
                                hidden as R=0.
       - high >= take_profit -> "take_profit"; exit at take_profit, or at the
                                bar's open if it gapped above the target.
       - If BOTH are inside the same bar's range, STOP LOSS IS ASSUMED TO HAVE
         HIT FIRST. OHLC cannot tell us the intrabar order, so we take the
         conservative side.
       - Neither by the end of the window -> "neither_by_window_end", marked to
         the last window bar's close (an unrealized, provisional R).
     Note: the live monitor checks stop/target against closes, not intrabar
     highs/lows. This replay's touch-based exits are therefore a little harsher
     on stops (a wick through the stop counts) and a little kinder on targets
     (a wick through the target counts) than the live bot would actually be.

R-multiple:  R = (exit_price - fill_price) / (entry_zone[1] - stop_loss)
The denominator is the plan's own risk per unit, which is exactly the stop
distance src/bot.py passes to the risk engine for sizing (entry_price =
entry_zone[1]). So R means "multiples of the risk budget the position would
have been sized to". Using the actual fill price in the denominator instead
would explode toward +/-infinity whenever a gap fills right next to the stop.
This bot is long-only spot (src/trade_planner.py builds "buy" plans only), so
the formula is the long one.

DEDUPLICATION
-------------
The bot re-evaluates every symbol on every poll (default 5 min) against 1h
candles, so a single setup blocked by, say, a 60-minute cooldown is logged as
~12 rejections carrying an identical plan. Counting each would inflate every
total ~12x. Rejections with an identical (symbol, entry_zone, stop_loss,
take_profit) are collapsed into one, keeping the earliest event. Its reason is
the one credited. The number collapsed is reported.

Everything degrades to an explicit "insufficient_data" result below
MIN_REJECTIONS_FOR_STATS rather than printing a noisy number. A missing or
empty audit log is a normal, reportable state, not a crash.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from src.risk_analytics import _percentile

# Same role as src.risk_analytics.MIN_TRADES_FOR_STATS. It counts replayed
# rejections that would have ENTERED (i.e. that have an R-multiple), since
# those are the population the R statistics are computed over. Rejections
# that never entered tell us nothing about P/L either way.
MIN_REJECTIONS_FOR_STATS = 5
DEFAULT_LOOKAHEAD_BARS = 200
# Kraken's OHLC endpoint returns at most 720 candles per call. ccxt caps a
# larger `limit` to whatever the exchange returns anyway. Rejections older than
# the fetched history are reported as skipped, never replayed from a later
# starting point (that would silently drop the bars right after the event).
DEFAULT_MAX_FETCH_BARS = 720

# Ordered (substring, category) pairs matched against RiskDecision.reason.
# These mirror the literal reason strings in src/risk_engine.py's
# check_new_trade. Anything unrecognized gets its own "other: <reason>" group
# rather than being silently merged into a known one.
_REASON_CATEGORIES: tuple[tuple[str, str], ...] = (
    # Listed first: these reasons embed the offending value's repr, so they must never
    # be matched by a later needle by accident (issue #87).
    ("invalid risk input", "invalid_input"),
    ("kill switch", "kill_switch"),
    ("symbol whitelist", "symbol_whitelist"),
    ("daily loss limit", "daily_loss_limit"),
    ("cooldown active", "cooldown"),
    ("new-entry rate cap", "new_entry_rate_cap"),
    ("market volatility too high", "volatility"),
    ("risk/reward", "min_risk_reward"),
    ("no room left to size", "no_sizing_room"),
    ("below the minimum order size", "min_order_size"),
)


def classify_reason(reason: Any) -> str:
    """Map a RiskDecision.reason string to a stable rule category.

    The raw strings embed per-event values (remaining cooldown time, the ATR
    percentage, ...), so grouping by the raw string would split one rule into
    hundreds of groups."""
    if not isinstance(reason, str) or not reason.strip():
        return "unknown"
    lowered = reason.lower()
    for needle, category in _REASON_CATEGORIES:
        if needle in lowered:
            return category
    return f"other: {reason.strip()}"


@dataclass(frozen=True)
class Bar:
    open_time: datetime  # tz-aware UTC, the candle's OPEN time
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Rejection:
    symbol: str
    timestamp: datetime  # tz-aware UTC
    reason: str
    reason_category: str
    entry_low: float
    entry_high: float
    stop_loss: float
    take_profit: float
    invalidation: float
    pattern: str | None = None


@dataclass
class ReplayResult:
    symbol: str
    timestamp: str
    reason: str
    reason_category: str
    pattern: str | None
    status: str  # "replayed" | "skipped"
    skip_reason: str | None = None
    would_have_entered: bool | None = None
    not_entered_reason: str | None = None  # "invalidated" | "window_ended"
    outcome: str | None = None  # "take_profit" | "stop_loss" | "neither_by_window_end"
    fill_price: float | None = None
    exit_price: float | None = None
    r_multiple: float | None = None  # GROSS: no fees/slippage
    fill_bar_time: str | None = None
    exit_bar_time: str | None = None
    bars_replayed: int = 0
    window_complete: bool = False  # True if the full lookahead window was available

    @property
    def provisional(self) -> bool:
        """A result whose conclusion could still change given more bars: the
        window was cut short (event too recent) and nothing decisive
        (stop/target/invalidation) happened inside it."""
        if self.status != "replayed" or self.window_complete:
            return False
        return self.outcome == "neither_by_window_end" or self.not_entered_reason == "window_ended"


@dataclass
class ParsedAuditLog:
    rejections: list[Rejection] = field(default_factory=list)
    raw_rejection_events: int = 0
    duplicates_collapsed: int = 0
    malformed_rejections: int = 0  # a rejection event whose plan/timestamp couldn't be trusted
    unreadable_lines: int = 0


# --- parsing ----------------------------------------------------------------


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # same convention as scripts/export_dashboard_data.py
    return dt.astimezone(timezone.utc)


def is_rejection(event: Any) -> bool:
    """True only for an event whose risk_decision.allowed is literally False.
    Missing or odd shapes are not counted as rejections."""
    if not isinstance(event, dict):
        return False
    decision = event.get("risk_decision")
    return isinstance(decision, dict) and decision.get("allowed") is False


def parse_rejection(event: dict[str, Any]) -> Rejection | None:
    """Build a Rejection from an audit event, or None if any field needed to
    replay it faithfully is missing, non-finite, or geometrically impossible
    for a long plan (invalidation <= stop < entry_high, stop <= entry_low <=
    entry_high < take_profit). A plan we can't trust is excluded and counted,
    not guessed at."""
    if not is_rejection(event):
        return None
    symbol = event.get("symbol")
    ts = _parse_timestamp(event.get("timestamp"))
    plan = event.get("plan")
    if not isinstance(symbol, str) or not symbol or ts is None or not isinstance(plan, dict):
        return None
    zone = plan.get("entry_zone")
    if not isinstance(zone, (list, tuple)) or len(zone) != 2:
        return None
    entry_low, entry_high = _finite_float(zone[0]), _finite_float(zone[1])
    stop = _finite_float(plan.get("stop_loss"))
    target = _finite_float(plan.get("take_profit"))
    invalidation = _finite_float(plan.get("invalidation"))
    if None in (entry_low, entry_high, stop, target, invalidation):
        return None
    if not (0 < invalidation <= stop <= entry_low <= entry_high < target and stop < entry_high):
        return None
    reason = event["risk_decision"].get("reason")
    reason_str = reason if isinstance(reason, str) else ""
    pattern = plan.get("pattern")
    return Rejection(
        symbol=symbol,
        timestamp=ts,
        reason=reason_str,
        reason_category=classify_reason(reason),
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop,
        take_profit=target,
        invalidation=invalidation,
        pattern=pattern if isinstance(pattern, str) else None,
    )


def parse_audit_events(events: Iterable[Any]) -> ParsedAuditLog:
    """Extract and deduplicate rejections from already-decoded audit events."""
    parsed = ParsedAuditLog()
    seen: dict[tuple, int] = {}
    for event in events:
        if not is_rejection(event):
            continue
        parsed.raw_rejection_events += 1
        rej = parse_rejection(event)
        if rej is None:
            parsed.malformed_rejections += 1
            continue
        key = (rej.symbol, rej.entry_low, rej.entry_high, rej.stop_loss, rej.take_profit)
        if key in seen:
            parsed.duplicates_collapsed += 1
            idx = seen[key]
            if rej.timestamp < parsed.rejections[idx].timestamp:
                parsed.rejections[idx] = rej  # keep the earliest (log is normally already chronological)
            continue
        seen[key] = len(parsed.rejections)
        parsed.rejections.append(rej)
    parsed.rejections.sort(key=lambda r: r.timestamp)
    return parsed


def load_rejections(audit_log_path: Path) -> ParsedAuditLog:
    """Stream the audit log line by line. A missing or empty file gives an
    empty result rather than an error. Undecodable lines are counted and skipped."""
    if not audit_log_path.exists():
        return ParsedAuditLog()

    unreadable = 0

    def _events() -> Iterable[Any]:
        nonlocal unreadable
        with open(audit_log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    unreadable += 1

    parsed = parse_audit_events(_events())
    parsed.unreadable_lines = unreadable
    return parsed


# --- candles ----------------------------------------------------------------


def bars_from_ohlcv_df(df: Any) -> list[Bar]:
    """Convert ExchangeClient.fetch_ohlcv_df's DataFrame (timestamp = bar OPEN
    time, tz-aware UTC) into Bars, sorted by open time. Values are carried over
    as-is. Validation happens per replay window, so one bad candle only voids
    the rejections whose window actually contains it."""
    bars = []
    for row in df.itertuples(index=False):
        ts = row.timestamp
        open_time = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if open_time.tzinfo is None:
            open_time = open_time.replace(tzinfo=timezone.utc)
        bars.append(Bar(open_time, float(row.open), float(row.high), float(row.low), float(row.close)))
    bars.sort(key=lambda b: b.open_time)
    return bars


def _bar_is_sane(b: Bar) -> bool:
    values = (b.open, b.high, b.low, b.close)
    if not all(math.isfinite(v) and v > 0 for v in values):
        return False
    return b.low <= min(b.open, b.close) and b.high >= max(b.open, b.close)


# --- replay -----------------------------------------------------------------


def _base_result(rej: Rejection) -> ReplayResult:
    return ReplayResult(
        symbol=rej.symbol,
        timestamp=rej.timestamp.isoformat(),
        reason=rej.reason,
        reason_category=rej.reason_category,
        pattern=rej.pattern,
        status="skipped",
    )


def skipped(rej: Rejection, skip_reason: str) -> ReplayResult:
    result = _base_result(rej)
    result.skip_reason = skip_reason
    return result


def replay_rejection(rej: Rejection, bars: list[Bar], lookahead_bars: int = DEFAULT_LOOKAHEAD_BARS) -> ReplayResult:
    """Replay one rejected plan against closed candles. See the module
    docstring for the exact entry/exit model. `bars` should be the full fetched
    history for the symbol (sorted by open time). It is used both to confirm
    coverage back to the event and to build the window after it."""
    if lookahead_bars < 2:
        raise ValueError("lookahead_bars must be >= 2 (one bar to trigger entry, the next to fill on)")
    if not bars:
        return skipped(rej, "no_candle_data")
    if bars[0].open_time > rej.timestamp:
        # The fetched history starts after the event. The bars immediately
        # following the rejection are missing, so replaying from whatever bar we
        # do have would be a different (later) trade.
        return skipped(rej, "event_predates_fetched_history")

    window = [b for b in bars if b.open_time >= rej.timestamp][:lookahead_bars]
    if not window:
        return skipped(rej, "too_recent_no_closed_bars_after_event")
    if not all(_bar_is_sane(b) for b in window):
        return skipped(rej, "invalid_candle_data")

    result = _base_result(rej)
    result.status = "replayed"
    result.window_complete = len(window) >= lookahead_bars

    # --- entry phase: decide on a CLOSED bar's close, fill at the next bar's open.
    fill_idx: int | None = None
    for i, b in enumerate(window):
        if b.close <= rej.invalidation:
            result.would_have_entered = False
            result.not_entered_reason = "invalidated"
            result.bars_replayed = i + 1
            return result
        if rej.entry_low <= b.close <= rej.entry_high:
            if i + 1 < len(window):
                fill_idx = i + 1
            break
    if fill_idx is None:
        result.would_have_entered = False
        result.not_entered_reason = "window_ended"
        result.bars_replayed = len(window)
        return result

    fill_bar = window[fill_idx]
    fill_price = fill_bar.open
    result.would_have_entered = True
    result.fill_price = fill_price
    result.fill_bar_time = fill_bar.open_time.isoformat()

    # --- exit phase: starts on the fill bar itself (its whole range follows the fill at its open).
    exit_price: float | None = None
    for k in range(fill_idx, len(window)):
        b = window[k]
        if b.low <= rej.stop_loss:  # checked first: if both levels are in range, stop wins
            if k == fill_idx and fill_price <= rej.stop_loss:
                # Filled at an open that had already gapped through the stop.
                # "Exit at the open" would equal the fill and show R=0, hiding
                # a real loss. Take the bar's low (worst price seen while
                # holding) instead.
                exit_price = b.low
            else:
                exit_price = b.open if b.open <= rej.stop_loss else rej.stop_loss
            result.outcome = "stop_loss"
        elif b.high >= rej.take_profit:
            exit_price = b.open if b.open >= rej.take_profit else rej.take_profit
            result.outcome = "take_profit"
        if exit_price is not None:
            result.exit_bar_time = b.open_time.isoformat()
            result.bars_replayed = k + 1
            break
    else:
        last = window[-1]
        exit_price = last.close
        result.outcome = "neither_by_window_end"
        result.exit_bar_time = last.open_time.isoformat()
        result.bars_replayed = len(window)

    planned_risk_per_unit = rej.entry_high - rej.stop_loss  # > 0, guaranteed by parse_rejection
    result.exit_price = exit_price
    result.r_multiple = round((exit_price - fill_price) / planned_risk_per_unit, 4)
    return result


def replay_all(
    rejections: list[Rejection],
    exchange: Any,
    timeframe: str,
    lookahead_bars: int = DEFAULT_LOOKAHEAD_BARS,
    max_fetch_bars: int = DEFAULT_MAX_FETCH_BARS,
) -> list[ReplayResult]:
    """Fetch candles once per symbol via exchange.fetch_ohlcv_df (read-only)
    and replay every rejection. A failed fetch skips that symbol's rejections
    with a recorded reason. It never aborts the whole report and never
    substitutes a guess."""
    by_symbol: dict[str, list[Rejection]] = {}
    for rej in rejections:
        by_symbol.setdefault(rej.symbol, []).append(rej)

    results: list[ReplayResult] = []
    for symbol, rejs in by_symbol.items():
        try:
            bars = bars_from_ohlcv_df(exchange.fetch_ohlcv_df(symbol, timeframe, max_fetch_bars))
        except Exception as exc:  # noqa: BLE001 -- one symbol failing must not kill the report
            reason = f"exchange_fetch_failed: {type(exc).__name__}"
            results.extend(skipped(r, reason) for r in rejs)
            continue
        results.extend(replay_rejection(r, bars, lookahead_bars) for r in rejs)
    results.sort(key=lambda r: r.timestamp)
    return results


# --- aggregation ------------------------------------------------------------


def _verdict(net_r: float) -> str:
    if net_r < 0:
        # Gross R already negative; fees/slippage would only push it lower.
        return "rejections_avoided_net_losers"
    if net_r > 0:
        # Gross only: costs could erase this. Evidence to review, not to act on blindly.
        return "rejections_may_have_cost_opportunity"
    return "neutral"


def summarize(results: list[ReplayResult]) -> dict[str, Any]:
    """Counts are always reported (they're facts). R statistics and a verdict
    only appear once at least MIN_REJECTIONS_FOR_STATS rejections would have
    entered. Below that the stats block is {} and the verdict is
    "insufficient_data"."""
    replayed = [r for r in results if r.status == "replayed"]
    entered = [r for r in replayed if r.would_have_entered]
    skip_reasons: dict[str, int] = {}
    for r in results:
        if r.status == "skipped":
            skip_reasons[r.skip_reason or "unknown"] = skip_reasons.get(r.skip_reason or "unknown", 0) + 1

    summary: dict[str, Any] = {
        "rejections": len(results),
        "replayed": len(replayed),
        "skipped": len(results) - len(replayed),
        "skip_reasons": skip_reasons,
        "not_entered_invalidated": sum(1 for r in replayed if r.not_entered_reason == "invalidated"),
        "not_entered_window_ended": sum(1 for r in replayed if r.not_entered_reason == "window_ended"),
        "would_have_entered": len(entered),
        "take_profit": sum(1 for r in entered if r.outcome == "take_profit"),
        "stop_loss": sum(1 for r in entered if r.outcome == "stop_loss"),
        "neither_by_window_end": sum(1 for r in entered if r.outcome == "neither_by_window_end"),
        "provisional": sum(1 for r in replayed if r.provisional),
        "stats": {},
        "verdict": "insufficient_data",
    }

    if len(entered) < MIN_REJECTIONS_FOR_STATS:
        return summary

    rs = sorted(r.r_multiple for r in entered if r.r_multiple is not None)
    resolved = [r for r in entered if r.outcome in ("take_profit", "stop_loss")]
    net_r = sum(rs)
    summary["stats"] = {
        "gross_net_r": round(net_r, 4),
        "gross_net_r_resolved_only": round(sum(r.r_multiple for r in resolved), 4),
        "gross_mean_r": round(net_r / len(rs), 4),
        "gross_median_r": round(_percentile(rs, 0.5), 4),
        "gross_p25_r": round(_percentile(rs, 0.25), 4),
        "gross_p75_r": round(_percentile(rs, 0.75), 4),
        "win_rate_resolved": (
            round(sum(1 for r in resolved if r.outcome == "take_profit") / len(resolved), 4) if resolved else None
        ),
    }
    summary["verdict"] = _verdict(net_r)
    return summary


def build_counterfactual_report(
    audit_log_path: Path,
    exchange: Any,
    timeframe: str,
    lookahead_bars: int = DEFAULT_LOOKAHEAD_BARS,
    max_fetch_bars: int = DEFAULT_MAX_FETCH_BARS,
) -> dict[str, Any]:
    """Full report: overall summary plus a by-reason-category breakdown.

    `exchange` only needs a read-only `fetch_ohlcv_df(symbol, timeframe,
    limit)`. If there are no rejections it is never called at all, so an
    empty or missing audit log needs no network access."""
    parsed = load_rejections(audit_log_path)
    results = (
        replay_all(parsed.rejections, exchange, timeframe, lookahead_bars, max_fetch_bars)
        if parsed.rejections
        else []
    )

    by_reason: dict[str, list[ReplayResult]] = {}
    for r in results:
        by_reason.setdefault(r.reason_category, []).append(r)

    return {
        "audit_log_path": str(audit_log_path),
        "audit_log_found": audit_log_path.exists(),
        "timeframe": timeframe,
        "lookahead_bars": lookahead_bars,
        "min_rejections_for_stats": MIN_REJECTIONS_FOR_STATS,
        "r_multiples_are_gross": True,  # no fees, no slippage -- directional only
        "raw_rejection_events": parsed.raw_rejection_events,
        "duplicates_collapsed": parsed.duplicates_collapsed,
        "malformed_rejections": parsed.malformed_rejections,
        "unreadable_lines": parsed.unreadable_lines,
        "overall": summarize(results),
        "by_reason": {cat: summarize(rs) for cat, rs in sorted(by_reason.items())},
        "results": [{**asdict(r), "provisional": r.provisional} for r in results],
    }


# --- human-readable output --------------------------------------------------

_VERDICT_TEXT = {
    "insufficient_data": f"not enough data (< {MIN_REJECTIONS_FOR_STATS} would-have-entered rejections)",
    "rejections_avoided_net_losers": "rejected plans would have LOST in aggregate (rule looks protective)",
    "rejections_may_have_cost_opportunity": "rejected plans would have WON in aggregate, gross of costs (review)",
    "neutral": "rejected plans net to zero R",
}


def _summary_lines(s: dict[str, Any], indent: str = "  ") -> list[str]:
    lines = [
        f"{indent}rejections: {s['rejections']}  replayed: {s['replayed']}  skipped: {s['skipped']}"
        + (f"  {s['skip_reasons']}" if s["skip_reasons"] else ""),
        f"{indent}never entered: {s['not_entered_invalidated']} invalidated, "
        f"{s['not_entered_window_ended']} window ended",
        f"{indent}would have entered: {s['would_have_entered']}  -> take-profit {s['take_profit']}, "
        f"stop-loss {s['stop_loss']}, neither by window end {s['neither_by_window_end']}",
    ]
    if s["provisional"]:
        lines.append(f"{indent}provisional (window cut short, may still change): {s['provisional']}")
    st = s["stats"]
    if st:
        win = f"{st['win_rate_resolved']:.0%}" if st["win_rate_resolved"] is not None else "n/a"
        lines.append(
            f"{indent}GROSS net R: {st['gross_net_r']:+.2f}  (resolved only {st['gross_net_r_resolved_only']:+.2f})  "
            f"mean {st['gross_mean_r']:+.2f}  median {st['gross_median_r']:+.2f}  "
            f"p25/p75 {st['gross_p25_r']:+.2f}/{st['gross_p75_r']:+.2f}  win rate (resolved) {win}"
        )
    lines.append(f"{indent}verdict: {_VERDICT_TEXT.get(s['verdict'], s['verdict'])}")
    return lines


def format_report(report: dict[str, Any]) -> str:
    out = [
        "Risk-rejection counterfactual replay",
        "=" * 36,
        "DIAGNOSTIC ONLY. R-multiples are GROSS (no fees, no slippage, no funding) and",
        "directional only. This is not a backtest, and not what would actually have been booked",
        "(see src/pnl.py for real P/L). Each rejected plan is replayed in isolation.",
        "",
        f"audit log: {report['audit_log_path']}" + ("" if report["audit_log_found"] else "  (NOT FOUND)"),
        f"timeframe: {report['timeframe']}  lookahead: {report['lookahead_bars']} bars  "
        f"stats need >= {report['min_rejections_for_stats']} would-have-entered rejections",
        f"rejection events: {report['raw_rejection_events']}  duplicates collapsed: {report['duplicates_collapsed']}  "
        f"malformed/excluded: {report['malformed_rejections']}  unreadable lines: {report['unreadable_lines']}",
        "",
        "OVERALL",
        *_summary_lines(report["overall"]),
    ]
    if report["by_reason"]:
        out += ["", "BY REJECTION REASON"]
        for category, s in report["by_reason"].items():
            out += [f"- {category}", *_summary_lines(s, indent="    ")]
    else:
        out += ["", "No risk-engine rejections found to replay."]
    return "\n".join(out)
