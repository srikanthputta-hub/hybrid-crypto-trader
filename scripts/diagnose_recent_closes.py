#!/usr/bin/env python3
"""Diagnose the live bot's recent closed trades: pattern, timing, and outcome,
read straight from logs/trade_plans_history.jsonl (one line per plan state
transition -- PENDING/OPEN/CLOSED -- written by src.trade_plan_store; the
exact filename comes from config.yaml's logging.trade_plan_history_path).

Read-only. Doesn't touch the live bot, place orders, or modify any file.

Usage (run on the VM, from the repo root):

    python3 scripts/diagnose_recent_closes.py
    python3 scripts/diagnose_recent_closes.py --history logs/trade_plans_history.jsonl

Why trade_plans_history.jsonl and not logs/audit_log.jsonl: audit_log.jsonl
has one entry per symbol per cycle (mostly "nothing happened" or "rejected"),
so finding the handful of actual closes means wading through thousands of
lines. trade_plans_history.jsonl has exactly one CLOSED record per completed
trade, already carrying the plan's pattern, confidence, and risk/reward
alongside the fill/close prices -- the single cleanest source for "what
actually happened on each of our closed trades."
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def load_closed_trades(history_path: Path) -> list[dict]:
    """Each plan gets multiple lines (PENDING, then OPEN/filled, then CLOSED)
    as it progresses -- keep only the final CLOSED line per (symbol, created_at)
    plan, since that's the one line carrying the complete picture (fill price,
    close price, close reason) for a trade that actually ran its course.

    src.trade_plan_store.discard_pending() ALSO writes status="CLOSED" for a
    PENDING plan whose invalidation level got hit before it ever filled -- no
    money was ever at risk, so that's not a real trade (and isn't one of the
    ones counted in logs/trades.csv / the dashboard's "20 trades"). Only keep
    records with a real fill_price, which discard_pending's records never have
    (mark_open is what sets fill_price, and discard_pending skips straight
    from PENDING to CLOSED without ever calling it)."""
    closed_by_key: dict[tuple[str, str], dict] = {}
    if not history_path.exists():
        return []
    with open(history_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") != "CLOSED" or rec.get("fill_price") is None:
                continue
            key = (rec.get("symbol"), rec.get("created_at"))
            closed_by_key[key] = rec  # last CLOSED line for this plan wins
    return list(closed_by_key.values())


def compute_r(rec: dict) -> float | None:
    plan = rec.get("plan") or {}
    fill_price = rec.get("fill_price")
    close_price = rec.get("close_price")
    stop_loss = plan.get("stop_loss")
    if fill_price is None or close_price is None or stop_loss is None:
        return None
    risk_per_unit = fill_price - stop_loss
    if risk_per_unit <= 0:
        return None
    return (close_price - fill_price) / risk_per_unit


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", default="logs/trade_plans_history.jsonl")
    a = ap.parse_args()

    trades = load_closed_trades(Path(a.history))
    if not trades:
        print(f"No CLOSED records found in {a.history}.")
        return

    trades.sort(key=lambda r: r.get("closed_at") or "")

    print(f"{'closed_at':<26} {'symbol':<10} {'pattern':<20} {'reason':<12} {'stars':<6} {'R':<7} {'rr':<6}")
    print("-" * 95)
    for rec in trades:
        plan = rec.get("plan") or {}
        r = compute_r(rec)
        print(
            f"{(rec.get('closed_at') or '')[:19]:<26} "
            f"{rec.get('symbol', '?'):<10} "
            f"{plan.get('pattern', '?'):<20} "
            f"{(rec.get('close_reason') or '?'):<12} "
            f"{plan.get('rule_based_confidence_stars', '?'):<6} "
            f"{(f'{r:+.2f}' if r is not None else '?'):<7} "
            f"{plan.get('risk_reward', '?'):<6}"
        )

    print()
    print("=== by pattern ===")
    by_pattern: dict[str, list[dict]] = defaultdict(list)
    for rec in trades:
        by_pattern[(rec.get("plan") or {}).get("pattern", "unknown")].append(rec)
    for pattern, recs in sorted(by_pattern.items(), key=lambda kv: -len(kv[1])):
        wins = sum(1 for r in recs if r.get("close_reason") == "take_profit")
        print(f"  {pattern:<22} {len(recs):>3} trades, {wins}/{len(recs)} wins")

    print()
    print("=== by symbol ===")
    by_symbol = Counter(rec.get("symbol", "?") for rec in trades)
    for symbol, n in by_symbol.most_common():
        print(f"  {symbol:<12} {n} trade(s)")

    print()
    print("=== close-time clustering (closes within 10 min of each other) ===")
    # Sort by TIME first, symbol only as a tiebreaker -- sorting a (symbol, time) tuple
    # the other way around sorts alphabetically by symbol first, which groups e.g. ADA
    # next to ALGO regardless of when they actually closed. Caught this via a real run
    # producing a "cluster" list in exact alphabetical order -- a tell that the grouping
    # was keying off symbol name, not real time proximity.
    timestamps = sorted(
        ((rec.get("symbol"), _parse_ts(rec.get("closed_at"))) for rec in trades),
        key=lambda pair: (pair[1] is None, pair[1], pair[0]),
    )
    timestamps = [(s, t) for s, t in timestamps if t is not None]
    cluster: list[tuple[str, datetime]] = []
    clusters_found = 0
    for i, (sym, ts) in enumerate(timestamps):
        if cluster and (ts - cluster[-1][1]).total_seconds() > 600:
            if len(cluster) > 1:
                clusters_found += 1
                print(f"  cluster: {[s for s, _ in cluster]} around {cluster[0][1].isoformat()}")
            cluster = []
        cluster.append((sym, ts))
    if len(cluster) > 1:
        clusters_found += 1
        print(f"  cluster: {[s for s, _ in cluster]} around {cluster[0][1].isoformat()}")
    if clusters_found == 0:
        print("  none found -- closes are spread out, not simultaneous")

    print()
    print(f"Total closed trades: {len(trades)}")


if __name__ == "__main__":
    main()
