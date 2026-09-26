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


def _role_statuses(events: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
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
        roles.append(
            {
                "id": role_id,
                "label": label,
                "model_tier": model_tier,
                "status": status,
                "last_active": last_active,
            }
        )
    # CEO/Boss desk: "working" whenever any specialist role is.
    roles.insert(
        0,
        {
            "id": "boss",
            "label": "CEO / Orchestrator",
            "model_tier": "opus",
            "status": "working" if any(r["status"] == "working" for r in roles) else "idle",
            "last_active": events[-1]["timestamp"] if events and "timestamp" in events[-1] else None,
        },
    )
    return roles


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
    chart_payload = None
    try:
        exchange = ExchangeClient(cfg.exchange_id, testnet=False)
        for symbol in cfg.symbols:
            try:
                current_prices[symbol] = exchange.fetch_ticker_price(symbol)
            except Exception:
                pass

        records = store.all_current()
        chart_symbol = next(iter(records), None) or (cfg.symbols[0] if cfg.symbols else None)
        if chart_symbol:
            ohlcv = exchange.fetch_ohlcv_df(chart_symbol, cfg.timeframe, 150)
            chart_payload = {
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
            }
            record = records.get(chart_symbol)
            if record is not None:
                chart_payload["plan"] = _record_view(chart_symbol, record, current_prices.get(chart_symbol))
    except Exception as exc:  # noqa: BLE001 — a network hiccup shouldn't stop the rest of the export
        print(f"warning: live price/candle fetch failed, dashboard will show stale/partial data: {exc}", file=sys.stderr)

    records = store.all_current()
    positions = [_record_view(sym, rec, current_prices.get(sym)) for sym, rec in records.items() if rec.status == "OPEN"]
    pending = [_record_view(sym, rec, current_prices.get(sym)) for sym, rec in records.items() if rec.status == "PENDING"]

    equity = cash + sum((p["current_price"] or p["fill_price"] or 0) * (p["fill_amount"] or 0) for p in positions)

    recent_candidates = []
    for event in reversed(audit_events):
        if event.get("candidates"):
            recent_candidates.append({"symbol": event["symbol"], "timestamp": event.get("timestamp"), "candidates": event["candidates"]})
        if len(recent_candidates) >= 10:
            break

    recent_risk_decisions = []
    for event in reversed(audit_events):
        if event.get("risk_decision"):
            recent_risk_decisions.append(
                {"symbol": event["symbol"], "timestamp": event.get("timestamp"), **event["risk_decision"]}
            )
        if len(recent_risk_decisions) >= 10:
            break

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
        "roles": _role_statuses(audit_events, now),
        "positions": positions,
        "pending": pending,
        "recent_candidates": recent_candidates,
        "recent_risk_decisions": recent_risk_decisions,
        "chart": chart_payload,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
