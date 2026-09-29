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
from src.exchange import ExchangeClient
from src.pnl import build_pnl_report
from src.portfolio import reconstruct_cash
from src.trade_plan_store import TradePlanStore

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = PROJECT_ROOT / "web" / "data.json"

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


def _read_audit_tail(path: Path, max_lines: int = 200_000) -> list[dict[str, Any]]:
    """`max_lines` is a safety ceiling against an unexpectedly huge audit log,
    not a normal limit -- it needs to comfortably cover HISTORY_MAX_AGE's
    worth of lines (audit_log.jsonl gets one line per symbol per cycle, so at
    a 5-minute poll interval and 16 symbols that's ~4,600 lines/day; 200,000
    covers 2 weeks with plenty of headroom before src.log_retention's own
    30-day prune ever gets a chance to trim it)."""
    if not path.exists():
        return []
    lines = path.read_text().strip().splitlines()
    events = []
    for line in lines[-max_lines:]:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


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


def _role_statuses(events: list[dict[str, Any]], now: datetime, advisor_enabled: bool) -> list[dict[str, Any]]:
    roles = []
    for role_id, label, model_tier, marker_keys in ROLE_MARKERS:
        last_event = None
        for event in reversed(events):
            if any(key in event for key in marker_keys):
                last_event = event
                break
        status = "idle"
        last_active = None
        if last_event is not None and "timestamp" in last_event:
            last_active = last_event["timestamp"]
            try:
                ts = datetime.fromisoformat(last_active)
                if now - ts <= RECENCY_WINDOW:
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
    roles.insert(
        0,
        {
            "id": "boss",
            "label": "CEO / Orchestrator",
            "model_tier": "opus",
            "status": "working" if working_specialists else "idle",
            "last_active": events[-1]["timestamp"] if events and "timestamp" in events[-1] else None,
            "detail": (
                f"Coordinating {len(working_specialists)} active role(s) this cycle"
                if working_specialists
                else "Idle — waiting for the next scheduled cycle"
            ),
        },
    )
    return roles


HISTORY_MAX_AGE = timedelta(days=14)
HISTORY_MAX_ITEMS = 3000  # safety ceiling, not a normal limit -- see HISTORY_MAX_AGE


def _signal_history(
    events: list[dict[str, Any]], now: datetime, max_age: timedelta = HISTORY_MAX_AGE, max_items: int = HISTORY_MAX_ITEMS
) -> list[dict[str, Any]]:
    """A curated, human-readable timeline of the notable things that have
    happened recently -- signals planned, fills, closes, risk rejections,
    advisor vetoes -- as opposed to every single scanner tick. Powers the
    dashboard's "Signal History" view, which groups this by hour/day/week
    client-side (see web/app.js's renderHistoryFeed) -- up to `max_age` old,
    not a fixed item count, since a fixed count would only ever cover a few
    hours at this bot's cycle frequency."""
    history: list[dict[str, Any]] = []
    cutoff = now - max_age
    for event in reversed(events):
        symbol = event.get("symbol")
        ts = event.get("timestamp")
        if not symbol or not ts:
            continue
        try:
            event_dt = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if event_dt.tzinfo is None:
            event_dt = event_dt.replace(tzinfo=timezone.utc)
        if event_dt < cutoff:
            # events is chronological, so everything from here on is even
            # older -- safe to stop scanning entirely, not just skip this one.
            break

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

        if len(history) >= max_items:
            break
    return history


def _record_view(symbol: str, record, current_price: float | None) -> dict[str, Any]:
    plan = record.plan
    view = {
        "symbol": symbol,
        "status": record.status,
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
        "created_at": record.created_at,
        "filled_at": record.filled_at,
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
    audit_events = _read_audit_tail(PROJECT_ROOT / log_cfg.get("audit_log_path", "logs/audit_log.jsonl"))
    trade_log_path = PROJECT_ROOT / log_cfg.get("trade_log_path", "logs/trades.csv")
    cash = reconstruct_cash(trade_log_path, cfg.risk["starting_cash_usd"])
    pnl_report = build_pnl_report(trade_log_path, now)

    records = store.all_current()

    # Ticker prices for equity/unrealized-P&L, plus OHLCV candles for every
    # symbol with an active signal (OPEN or PENDING). The dashboard's chart
    # is a live TradingView widget by default, but TradingView's free widget
    # has no JS API for drawing custom price lines -- so for a symbol with an
    # active plan, the dashboard swaps in its own annotated chart (built from
    # these candles) that can actually show the entry/stop/target/
    # invalidation levels. A candle-fetch failure for one symbol falls back
    # to that symbol just not having an annotated chart (still has its
    # TradingView view) rather than dropping the whole export.
    current_prices: dict[str, float] = {}
    charts: dict[str, dict[str, Any]] = {}
    try:
        exchange = ExchangeClient(cfg.exchange_id, testnet=False)
        for symbol in cfg.symbols:
            try:
                current_prices[symbol] = exchange.fetch_ticker_price(symbol)
            except Exception:
                pass

        for symbol in records:
            try:
                ohlcv = exchange.fetch_ohlcv_df(symbol, cfg.timeframe, 150)
            except Exception as exc:  # noqa: BLE001 — one bad symbol shouldn't drop the rest of the charts
                print(f"warning: candle fetch failed for {symbol}, it'll show the TradingView view instead: {exc}", file=sys.stderr)
                continue
            charts[symbol] = {
                "candles": [
                    {
                        "time": int(row.timestamp.timestamp()),
                        "open": round(row.open, 8),
                        "high": round(row.high, 8),
                        "low": round(row.low, 8),
                        "close": round(row.close, 8),
                    }
                    for row in ohlcv.itertuples()
                ],
            }
    except Exception as exc:  # noqa: BLE001 — a network hiccup shouldn't stop the rest of the export
        print(f"warning: live price/candle fetch failed, dashboard will show stale/partial data: {exc}", file=sys.stderr)

    positions = [_record_view(sym, rec, current_prices.get(sym)) for sym, rec in records.items() if rec.status == "OPEN"]
    pending = [_record_view(sym, rec, current_prices.get(sym)) for sym, rec in records.items() if rec.status == "PENDING"]
    # Newest signal first -- ISO8601 UTC timestamps sort correctly as plain strings.
    positions.sort(key=lambda p: p["timestamp"] or "", reverse=True)
    pending.sort(key=lambda p: p["timestamp"] or "", reverse=True)

    equity = cash + sum((p["current_price"] or p["fill_price"] or 0) * (p["fill_amount"] or 0) for p in positions)

    data = {
        "generated_at": now.isoformat(),
        "mode": cfg.mode,
        "exchange_id": cfg.exchange_id,
        "symbols": cfg.symbols,
        "portfolio": {
            "starting_cash": cfg.risk["starting_cash_usd"],
            "cash": round(cash, 2),
            "equity": round(equity, 2),
            "pnl_pct": round((equity - cfg.risk["starting_cash_usd"]) / cfg.risk["starting_cash_usd"], 4),
        },
        "roles": _role_statuses(audit_events, now, cfg.advisor.get("enabled", False)),
        "positions": positions,
        "pending": pending,
        "signal_history": _signal_history(audit_events, now),
        "charts": charts,
        "pnl": pnl_report,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
