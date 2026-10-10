"""Config loading: merges config/config.yaml with environment variables (.env)."""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_BAR_DECISIONS_PATH = "logs/bar_decisions.json"


def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


@dataclass(frozen=True)
class BarParitySettings:
    """Validated settings for the live-loop bar-parity rules P1-P5
    (docs/multiday_hold_design.md §3.1/§6, docs/bot_bar_parity.md).

    `enabled` (`loop.bar_parity`, default False): P1 one decision per symbol per
    closed bar (persisted to `decisions_path`), P2 monitor pass first then the
    entry pass in sorted(symbol) order, P3 entry room/cash computed as if this
    bar's exits had not executed yet, P5 htf gate on the scan frame at 1d.
    False reproduces the pre-parity run_cycle exactly.

    `max_entry_delay_minutes` (`loop.max_entry_delay_minutes`, default None =
    off): P4 staleness guard -- no NEW plan from a bar that closed more than this
    many minutes ago. Can only remove entries, never exits. Untuned.

    `closed_bars_dir` (`logging.closed_bars_dir`, default None = off): append
    every closed bar the bot sees to `<dir>/<BASE>_<QUOTE>_<tf>.csv` in the
    strategy_pack_run data-CSV format, for the weekly parity check."""

    enabled: bool = False
    max_entry_delay_minutes: float | None = None
    decisions_path: str = DEFAULT_BAR_DECISIONS_PATH
    closed_bars_dir: str | None = None


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    section = raw.get(name, {})
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ValueError(f"config section {name!r} must be a mapping, got {type(section).__name__}")
    return section


def parse_bar_parity_settings(raw: dict[str, Any]) -> BarParitySettings:
    """Strict startup validation of the bar-parity keys. Raises ValueError on
    anything that isn't exactly what the docs say, the same "refuse to start
    rather than guess" rule as src/risk_engine.py's _require_config_number:

    - `loop.bar_parity`: absent -> False; otherwise must be a real bool (not
      1/0, not "true", not null).
    - `loop.max_entry_delay_minutes`: absent or null -> off; otherwise an int or
      float (bool rejected), finite, > 0, representable as a timedelta. 0 is
      rejected (it would block every entry, which is the kill switch's job).
    - `logging.bar_decisions_path`: absent -> logs/bar_decisions.json; otherwise
      a non-empty string.
    - `logging.closed_bars_dir`: absent or null -> off; otherwise a non-empty
      string.
    - max_entry_delay_minutes and closed_bars_dir require bar_parity: true (a
      guard that silently does nothing is worse than a startup error).
    Absent keys reproduce today's behaviour exactly."""
    loop = _section(raw, "loop")
    log_cfg = _section(raw, "logging")

    enabled = loop.get("bar_parity", False)
    if not isinstance(enabled, bool):
        raise ValueError(f"loop.bar_parity must be true or false, got {enabled!r}")

    delay = loop.get("max_entry_delay_minutes")
    if delay is not None:
        ok = False
        if not isinstance(delay, bool) and isinstance(delay, (int, float)):
            try:
                ok = math.isfinite(delay) and delay > 0 and timedelta(minutes=delay) > timedelta(0)
            except OverflowError:
                ok = False
        if not ok:
            raise ValueError(
                f"loop.max_entry_delay_minutes must be null (off) or a finite number of minutes > 0, got {delay!r}"
            )
        delay = float(delay)

    decisions_path = log_cfg.get("bar_decisions_path", DEFAULT_BAR_DECISIONS_PATH)
    if not isinstance(decisions_path, str) or not decisions_path.strip():
        raise ValueError(f"logging.bar_decisions_path must be a non-empty string, got {decisions_path!r}")

    bars_dir = log_cfg.get("closed_bars_dir")
    if bars_dir is not None and (not isinstance(bars_dir, str) or not bars_dir.strip()):
        raise ValueError(f"logging.closed_bars_dir must be null (off) or a non-empty string, got {bars_dir!r}")

    if not enabled:
        if delay is not None:
            raise ValueError("loop.max_entry_delay_minutes requires loop.bar_parity: true")
        if bars_dir is not None:
            raise ValueError("logging.closed_bars_dir requires loop.bar_parity: true")

    return BarParitySettings(
        enabled=enabled,
        max_entry_delay_minutes=delay,
        decisions_path=decisions_path,
        closed_bars_dir=bars_dir,
    )


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
    def htf_filter(self) -> dict[str, Any]:
        return self.raw.get("htf_filter", {"enabled": True})

    @property
    def loop(self) -> dict[str, Any]:
        return self.raw.get("loop", {"poll_interval_seconds": 300})

    @property
    def bar_parity(self) -> BarParitySettings:
        """Validated bar-parity settings; raises ValueError on a bad value (see
        parse_bar_parity_settings). src/bot.py's build_components calls this at
        startup so a bad value stops the process before the first cycle."""
        return parse_bar_parity_settings(self.raw)

    @property
    def logging_cfg(self) -> dict[str, Any]:
        return self.raw.get("logging", {})

    @property
    def universe_cfg(self) -> dict[str, Any]:
        """The `universe:` block (see config/config.yaml and
        docs/system-redesign-2026-10.md §4.2). Only `mode` is actually read
        by src/universe.py's `refresh()` today (in `static` mode it's the
        only thing `refresh()` looks at); the rest of the keys are passed
        through unread for `mode: "discover"`, not implemented yet. Defaults
        to `static` mode with no exchange calls if the block is omitted
        entirely, matching today's `market.symbols`-is-the-whole-universe
        behavior."""
        return self.raw.get("universe", {"mode": "static"})

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

    @property
    def healthcheck_ping_url(self) -> str | None:
        return os.environ.get("HEALTHCHECK_PING_URL") or None

    def kill_switch_active(self) -> bool:
        path = PROJECT_ROOT / self.risk.get("kill_switch_file", "KILL_SWITCH")
        return path.exists()


def load_config(config_path: str | Path | None = None) -> Config:
    load_dotenv(PROJECT_ROOT / ".env")
    path = Path(config_path) if config_path else PROJECT_ROOT / "config" / "config.yaml"
    return Config(raw=_load_yaml(path))
