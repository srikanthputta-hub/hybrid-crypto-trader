"""Config loading: merges config/config.yaml with environment variables (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


@dataclass
class Config:
    raw: dict[str, Any]

    @property
    def mode(self) -> str:
        m = self.raw.get("mode", "paper")
        if m not in ("paper", "live"):
            raise ValueError(f"config.mode must be 'paper' or 'live', got {m!r}")
        return m

    @property
    def exchange_id(self) -> str:
        return self.raw["exchange"]["id"]

    @property
    def testnet(self) -> bool:
        return bool(self.raw["exchange"].get("testnet", True))

    @property
    def symbols(self) -> list[str]:
        return list(self.raw["market"]["symbols"])

    @property
    def timeframe(self) -> str:
        return self.raw["market"]["timeframe"]

    @property
    def history_bars(self) -> int:
        return int(self.raw["market"].get("history_bars", 200))

    @property
    def indicators(self) -> dict[str, Any]:
        return self.raw["indicators"]

    @property
    def scanner(self) -> dict[str, Any]:
        return self.raw.get("scanner", {})

    @property
    def signal_engine(self) -> dict[str, Any]:
        return self.raw.get("signal_engine", {})

    @property
    def trade_planner(self) -> dict[str, Any]:
        return self.raw.get("trade_planner", {})

    @property
    def risk(self) -> dict[str, Any]:
        return self.raw["risk"]

    @property
    def advisor(self) -> dict[str, Any]:
        return self.raw.get("advisor", {"enabled": False})

    @property
    def loop(self) -> dict[str, Any]:
        return self.raw.get("loop", {"poll_interval_seconds": 300})

    @property
    def logging_cfg(self) -> dict[str, Any]:
        return self.raw.get("logging", {})

    # --- env-derived secrets, never come from the yaml file ---
    @property
    def exchange_api_key(self) -> str | None:
        return os.environ.get("EXCHANGE_API_KEY") or None

    @property
    def exchange_api_secret(self) -> str | None:
        return os.environ.get("EXCHANGE_API_SECRET") or None

    @property
    def exchange_api_password(self) -> str | None:
        return os.environ.get("EXCHANGE_API_PASSWORD") or None

    @property
    def anthropic_api_key(self) -> str | None:
        return os.environ.get("ANTHROPIC_API_KEY") or None

    @property
    def telegram_bot_token(self) -> str | None:
        return os.environ.get("TELEGRAM_BOT_TOKEN") or None

    @property
    def telegram_chat_id(self) -> str | None:
        return os.environ.get("TELEGRAM_CHAT_ID") or None

    @property
    def live_trading_confirmed(self) -> bool:
        return os.environ.get("LIVE_TRADING_CONFIRM") == "YES-I-UNDERSTAND-THE-RISK"

    def kill_switch_active(self) -> bool:
        path = PROJECT_ROOT / self.risk.get("kill_switch_file", "KILL_SWITCH")
        return path.exists()


def load_config(config_path: str | Path | None = None) -> Config:
    load_dotenv(PROJECT_ROOT / ".env")
    path = Path(config_path) if config_path else PROJECT_ROOT / "config" / "config.yaml"
    return Config(raw=_load_yaml(path))
