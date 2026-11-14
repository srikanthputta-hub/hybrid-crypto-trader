#!/usr/bin/env python3
"""Reset the paper portfolio back to a fresh starting balance.

src.portfolio.Portfolio is in-memory only (see its own module docstring) --
cash is reconstructed on every process start by replaying logs/trades.csv
from config.yaml's risk.starting_cash_usd (src.portfolio.reconstruct_cash).
So "reset the portfolio" means: move the files that currently encode its
state out of the way, so the next process start finds none of them and
reconstructs fresh. This ARCHIVES those files (moves them into a timestamped
subdirectory) rather than deleting them -- the trade history stays on disk
for later reference, it's just no longer what the running bot reads from.

Archives, if present:
  - logs/trades.csv                  (the durable financial record)
  - logs/trade_plans_current.json    (any PENDING/OPEN plans)
  - logs/trade_plans_history.jsonl   (full plan lifecycle history)
  - logs/audit_log.jsonl             (per-cycle audit trail)

Does NOT touch config/config.yaml, .env, KILL_SWITCH, or anything outside
logs/. Does NOT place any order, modify risk/trading logic, or touch the
live-trading gate.

This only resets the FILES. The running systemd service still holds its old
in-memory Portfolio/TradePlanStore until restarted -- run this, then:

    sudo systemctl restart hybrid-crypto-trader

Usage (run on the VM, from the repo root):

    python3 scripts/reset_portfolio.py
    python3 scripts/reset_portfolio.py --logs-dir logs   # (default)

Live-broker state (issue #99): logs/live_order_state.json (write-ahead order records) and
logs/live_broker_halt.json (the halt marker) describe orders that may be REAL on the exchange.
While either holds anything unresolved (a halt, an in-flight or unverified order record, or an
unreadable file) this script REFUSES to reset, by default: archiving the ledger underneath them
would lose the only record of those orders. --include-live-state archives them too, with a loud
warning -- only after the exchange account has been reconciled by hand.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ARCHIVE_FILES = [
    "trades.csv",
    "trade_plans_current.json",
    "trade_plans_history.jsonl",
    "audit_log.jsonl",
]
# Names mirror src/broker_live.py (ORDER_STATE_NAME, HALT_MARKER_NAME); not imported, so this
# script never loads the live broker module.
LIVE_STATE_FILES = ["live_order_state.json", "live_broker_halt.json"]


def _unresolved_live_state(logs_dir: Path) -> list[str]:
    """Why the live state can't be dropped silently ([] = nothing unresolved)."""
    reasons = []
    if (logs_dir / "live_broker_halt.json").exists():
        reasons.append("the live broker is HALTED (live_broker_halt.json exists)")
    state = logs_dir / "live_order_state.json"
    if state.exists():
        try:
            raw = json.loads(state.read_text())
            if raw.get("inflight"):
                reasons.append(f"{len(raw['inflight'])} in-flight live order record(s)")
            if raw.get("unverified"):
                reasons.append(f"{len(raw['unverified'])} unverified live fill(s)")
        except Exception as exc:  # noqa: BLE001 -- unreadable = unknown = refuse
            reasons.append(f"live_order_state.json is unreadable ({type(exc).__name__})")
    return reasons


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs-dir", default="logs")
    ap.add_argument("--include-live-state", action="store_true",
                    help="also archive live_order_state.json and live_broker_halt.json (DANGEROUS: only after "
                         "reconciling the exchange account by hand)")
    a = ap.parse_args()

    logs_dir = Path(a.logs_dir)
    if not logs_dir.exists():
        print(f"{logs_dir} doesn't exist -- nothing to reset (a fresh start already).")
        return

    unresolved = _unresolved_live_state(logs_dir)
    names = list(ARCHIVE_FILES)
    if unresolved and not a.include_live_state:
        print("REFUSING to reset: " + "; ".join(unresolved) + ". These may be REAL orders on the exchange. "
              "Reconcile the exchange account by hand first, then re-run with --include-live-state.",
              file=sys.stderr)
        sys.exit(2)
    if a.include_live_state:
        names += LIVE_STATE_FILES
        print("WARNING: archiving the LIVE broker's order records and halt marker"
              + (" (" + "; ".join(unresolved) + ")" if unresolved else "")
              + ". If any order is still unresolved on the exchange, the bot will no longer know about it.")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_dir = logs_dir / "archive" / stamp

    moved = []
    for name in names:
        src = logs_dir / name
        if not src.exists():
            continue
        archive_dir.mkdir(parents=True, exist_ok=True)
        dest = archive_dir / name
        shutil.move(str(src), str(dest))
        moved.append(name)

    if not moved:
        print("No portfolio state files found -- already a fresh start.")
        return

    print(f"Archived to {archive_dir}:")
    for name in moved:
        print(f"  - {name}")
    print()
    print("Portfolio will reconstruct fresh (starting_cash_usd from config.yaml) on next process start.")
    print("Run: sudo systemctl restart hybrid-crypto-trader")


if __name__ == "__main__":
    main()
