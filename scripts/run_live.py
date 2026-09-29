#!/usr/bin/env python3
"""Continuous live loop for a real always-on host (a VPS, not CI): runs a bot
cycle, regenerates the dashboard's web/data.json, commits+pushes it to git,
and pings an optional external heartbeat monitor (HEALTHCHECK_PING_URL),
forever, on config.yaml's loop.poll_interval_seconds.

`python -m src.bot` (no --once) / scripts/run_paper.py already loop bot
cycles forever, but neither touches web/data.json or git -- that's fine when
GitHub Actions' refresh-dashboard.yml handles the export+push step in its
own separate, short-lived runs. It is NOT fine to run both at once: that
would be two independent processes trading against the same paper portfolio
concurrently (state divergence, racing git pushes to web/data.json). If
you're running this script continuously on a VPS, disable
refresh-dashboard.yml's `schedule:` trigger first (keep workflow_dispatch
for occasional manual use) so only one process is ever actually running
cycles. See deploy/README.md.

On a real always-on host, portfolio state (cash, open positions) and
logs/trade_plans_current.json etc. just live on local disk and persist
naturally across restarts -- none of the actions/cache dance
refresh-dashboard.yml needs on CI's ephemeral runners applies here.

Refuses to run if config.yaml has mode: live, same guard as run_paper.py.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bot import build_components, run_cycle
from src.config import load_config
from src.heartbeat import send_heartbeat_ping

PROJECT_ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger("run_live")


def _export_and_push() -> None:
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "export_dashboard_data.py")],
        check=True,
        cwd=PROJECT_ROOT,
    )
    subprocess.run(["git", "add", "web/data.json"], check=True, cwd=PROJECT_ROOT)
    unchanged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=PROJECT_ROOT).returncode == 0
    if unchanged:
        return
    subprocess.run(["git", "commit", "-m", "chore: refresh dashboard data"], check=True, cwd=PROJECT_ROOT)
    subprocess.run(["git", "push"], check=True, cwd=PROJECT_ROOT)


def main() -> None:
    cfg = load_config()
    if cfg.mode != "paper":
        raise SystemExit(
            f"config.yaml mode is {cfg.mode!r}, but scripts/run_live.py only runs paper mode. "
            "Set mode: paper in config/config.yaml for this always-on loop."
        )
    logger.info("starting live loop: mode=%s exchange=%s symbols=%s", cfg.mode, cfg.exchange_id, cfg.symbols)

    exchange, risk_engine, portfolio, broker, store, audit = build_components(cfg)
    interval = cfg.loop.get("poll_interval_seconds", 300)

    while True:
        try:
            run_cycle(cfg, exchange, risk_engine, portfolio, broker, store, audit)
        except Exception:
            logger.exception("bot cycle failed -- will retry next interval")
        try:
            _export_and_push()
        except Exception:
            logger.exception("dashboard export/push failed -- will retry next interval")
        send_heartbeat_ping(cfg.healthcheck_ping_url)
        time.sleep(interval)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()
