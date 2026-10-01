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
"""
from __future__ import annotations

import argparse
import shutil
from datetime import datetime, timezone
from pathlib import Path

ARCHIVE_FILES = [
    "trades.csv",
    "trade_plans_current.json",
    "trade_plans_history.jsonl",
    "audit_log.jsonl",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs-dir", default="logs")
    a = ap.parse_args()

    logs_dir = Path(a.logs_dir)
    if not logs_dir.exists():
        print(f"{logs_dir} doesn't exist -- nothing to reset (a fresh start already).")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_dir = logs_dir / "archive" / stamp

    moved = []
    for name in ARCHIVE_FILES:
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
