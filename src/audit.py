"""Append-only JSONL audit trail. Every cycle, for every symbol, one line is written
here regardless of whether a trade happened — so "why didn't it trade at 14:32" is
always answerable from this file alone.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: dict[str, Any]) -> None:
        with open(self.path, "a") as f:
            f.write(json.dumps(event, default=str) + "\n")
