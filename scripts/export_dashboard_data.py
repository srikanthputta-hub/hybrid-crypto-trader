#!/usr/bin/env python3
"""Export a snapshot of the bot's live state to web/data.json for the dashboard.

Run this after (or alongside) the bot to refresh the dashboard:

    python scripts/export_dashboard_data.py

The dashboard (web/index.html) is a static site — it doesn't talk to the bot
directly. This script is the bridge: it reads the same files the bot writes
(logs/trade_plans_current.json, logs/audit_log.jsonl, logs/trades.csv) plus a
live price/candle fetch, and writes one self-contained JSON file the page
reads. Re-run it (e.g. on a cron, or right before a git push if you're using
the GitHub -> Cloudflare Pages auto-deploy flow from the README) to refresh
what the dashboard shows.

Known simplification: this bot doesn't persist portfolio cash across process
restarts (see README), so "cash" here is reconstructed by replaying
logs/trades.csv from the configured starting balance — accurate for a
single continuous run, approximate if the bot has been restarted since.
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.exchange import ExchangeClient
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


def _read_audit_tail(path: Path, max_lines: int = 500) -> list[dict[str, Any]]:
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


def _reconstruct_cash(trade_log_path: Path, starting_cash: float) -> float:
    if not trade_log_path.exists():
        return starting_cash
    cash = starting_cash
    with open(trade_log_path) as f:
        for row in csv.DictReader(f):
            amount = float(row["quote_amount"])
            cash += amount if row["side"] == "sell" else -amount
    return cash


def _fmt_num(n: Any) -> str:
    if n is None:
        return "?"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    return f"{n:,.2f}" if n >= 1 else f"{n:,.6f}"


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
            return f"{symbol}: {signal.get('pattern')} signal ({signal.get('direction')}, score {signal.get('engine_score')})"
        candidates = event.get("candidates") or []
        if candidates:
            best = max(candidates, key=lambda c: c.get("strength", 0))
            return f"{symbol}: scanning — {best.get('pattern')} candidate (strength {best.get('strength')})"
        return f"{symbol}: scanned, no pattern found"

    if role_id == "trade_strategist":
        plan = event.get("plan")
        if plan:
            zone = plan.get("entry_zone")
            zone_str = f"{_fmt_num(zone[0])}–{_fmt_num(zone[1])}" if zone else "?"
            return f"{symbol}: built {plan.get('pattern')} plan — buy {zone_str}, stop {_fmt_num(plan.get('stop_loss'))}"
        return None

    if role_id == "risk_officer":
        decision = event.get("risk_decision")
        if decision:
            verdict = "approved" if decision.get("allowed") else "rejected"
            return f"{symbol}: {verdict} — {decision.get('reason')}"
        return None

    if role_id == "compliance_reviewer":
        verdict = event.get("advisor_verdict")
        if verdict:
            return f"{symbol}: {verdict.get('verdict')} (confidence {verdict.get('confidence_stars')})"
        return None

    if role_id == "execution_trader":
        execution = event.get("execution")
        monitor_action = (event.get("monitor_action") or {}).get("action")
        if execution and monitor_action in ("STOP_LOSS", "TAKE_PROFIT"):
            return f"{symbol}: closed ({monitor_action.replace('_', ' ').title()}) @ {_fmt_num(execution.get('price'))}"
        if execution:
            return f"{symbol}: bought @ {_fmt_num(execution.get('price'))}"
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


def _signal_history(events: list[dict[str, Any]], max_items: int = 20) -> list[dict[str, Any]]:
    """A curated, human-readable timeline of the notable things that have
    happened recently -- signals planned, fills, closes, risk rejections,
    advisor vetoes -- as opposed to every single scanner tick. Powers the
    dashboard's "Signal History" view."""
    history: list[dict[str, Any]] = []
    for event in reversed(events):
        symbol = event.get("symbol")
        ts = event.get("timestamp")
        if not symbol or not ts:
            continue

        if event.get("action") == "planned" and "plan" in event:
            plan = event["plan"]
            zone = plan.get("entry_zone")
            zone_str = f"{_fmt_num(zone[0])}–{_fmt_num(zone[1])}" if zone else "?"
            history.append(
                {
                    "timestamp": ts,
                    "symbol": symbol,
                    "kind": "signal",
                    "summary": f"{plan.get('pattern')} signal — buy {zone_str}, stop {_fmt_num(plan.get('stop_loss'))}",
                }
            )

        execution = event.get("execution")
        monitor_action = (event.get("monitor_action") or {}).get("action")
        if execution and monitor_action in ("STOP_LOSS", "TAKE_PROFIT"):
            history.append(
                {
                    "timestamp": ts,
                    "symbol": symbol,
                    "kind": "closed",
                    "summary": f"Closed ({monitor_action.replace('_', ' ').title()}) @ {_fmt_num(execution.get('price'))}",
                }
            )
        elif execution:
            history.append(
                {"timestamp": ts, "symbol": symbol, "kind": "filled", "summary": f"Bought @ {_fmt_num(execution.get('price'))}"}
            )

        risk_decision = event.get("risk_decision")
        if risk_decision and not risk_decision.get("allowed"):
            history.append(
                {
                    "timestamp": ts,
                    "symbol": symbol,
                    "kind": "rejected",
                    "summary": f"Risk check rejected — {risk_decision.get('reason')}",
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
                    "summary": f"Advisor vetoed — {advisor_verdict.get('reasoning')}",
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
    cash = _reconstruct_cash(PROJECT_ROOT / log_cfg.get("trade_log_path", "logs/trades.csv"), cfg.risk["starting_cash_usd"])

    current_prices: dict[str, float] = {}
    # One chart entry per symbol with an active signal (OPEN or PENDING) -- the
    # dashboard's "Latest Trade Plan" chart switches between these as the user
    # clicks a row in the Signals panel, instead of being locked to whichever
    # symbol happened to be picked first.
    charts: dict[str, dict[str, Any]] = {}
    try:
        exchange = ExchangeClient(cfg.exchange_id, testnet=False)
        for symbol in cfg.symbols:
            try:
                current_prices[symbol] = exchange.fetch_ticker_price(symbol)
            except Exception:
                pass

        records = store.all_current()
        for chart_symbol, record in records.items():
            try:
                ohlcv = exchange.fetch_ohlcv_df(chart_symbol, cfg.timeframe, 150)
            except Exception as exc:  # noqa: BLE001 — one bad symbol shouldn't drop the rest of the charts
                print(f"warning: candle fetch failed for {chart_symbol}, skipping its chart: {exc}", file=sys.stderr)
                continue
            charts[chart_symbol] = {
                "symbol": chart_symbol,
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
                "plan": _record_view(chart_symbol, record, current_prices.get(chart_symbol)),
            }
    except Exception as exc:  # noqa: BLE001 — a network hiccup shouldn't stop the rest of the export
        print(f"warning: live price/candle fetch failed, dashboard will show stale/partial data: {exc}", file=sys.stderr)

    records = store.all_current()
    positions = [_record_view(sym, rec, current_prices.get(sym)) for sym, rec in records.items() if rec.status == "OPEN"]
    pending = [_record_view(sym, rec, current_prices.get(sym)) for sym, rec in records.items() if rec.status == "PENDING"]

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
        "signal_history": _signal_history(audit_events),
        "charts": charts,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
