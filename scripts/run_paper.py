#!/usr/bin/env python3
"""Convenience entry point: `python scripts/run_paper.py` runs the bot loop in paper mode.

Refuses to run if config.yaml has mode: live — use `python -m src.bot` directly
for live trading, so paper and live are never one typo away from each other.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src import bot


def main() -> None:
    cfg = load_config()
    if cfg.mode != "paper":
        raise SystemExit(
            f"config.yaml mode is {cfg.mode!r}, but scripts/run_paper.py only runs paper mode. "
            "Set mode: paper in config/config.yaml, or run `python -m src.bot` for live trading."
        )
    sys.argv = ["bot.py"]  # run continuously, not --once
    bot.main()


if __name__ == "__main__":
    main()
