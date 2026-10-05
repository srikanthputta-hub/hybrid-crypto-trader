#!/usr/bin/env python3
"""Continuous live loop for a real always-on host (a VPS, not CI): runs a bot
cycle, regenerates the dashboard's web/data.json, commits+pushes it to git,
prunes old log rows once a day (config.yaml logging.retention_days), and
pings an optional external heartbeat monitor (HEALTHCHECK_PING_URL),
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
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bot import build_components, build_market_data, run_cycle
from src.config import load_config
from src.heartbeat import send_heartbeat_ping
from src.log_retention import run_log_retention

PROJECT_ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger("run_live")


def _push_with_pull_retry() -> None:
    """Push the just-made commit, self-healing if the remote has moved ahead
    (e.g. a PR merged on GitHub since this loop's own last push). A plain
    push in that situation always fails outright -- this process never pulls
    on its own otherwise -- which used to mean every cycle logged the same
    push error until someone SSHed in to manually `git pull`+`push`. One
    retry after a pull covers that case; a real conflict (extremely unlikely,
    since nothing else ever touches web/data.json) still surfaces as an
    exception for the caller's existing "will retry next interval" handling.
    """
    push = subprocess.run(["git", "push"], cwd=PROJECT_ROOT)
    if push.returncode == 0:
        return
    logger.warning("git push rejected (remote likely moved ahead) -- pulling and retrying once")
    subprocess.run(["git", "pull", "--no-rebase", "--no-edit"], check=True, cwd=PROJECT_ROOT)
    subprocess.run(["git", "push"], check=True, cwd=PROJECT_ROOT)


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
    _push_with_pull_retry()


def main() -> None:
    cfg = load_config()
    if cfg.mode != "paper":
        raise SystemExit(
            f"config.yaml mode is {cfg.mode!r}, but scripts/run_live.py only runs paper mode. "
            "Set mode: paper in config/config.yaml for this always-on loop."
        )
    logger.info("starting live loop: mode=%s exchange=%s symbols=%s", cfg.mode, cfg.exchange_id, cfg.symbols)

    exchange, risk_engine, portfolio, broker, store, audit = build_components(cfg)
    market_data = build_market_data(cfg, exchange)
    interval = cfg.loop.get("poll_interval_seconds", 300)
    last_retention_run_date = None

    while True:
        cycle_started = time.monotonic()
        try:
            run_cycle(cfg, exchange, risk_engine, portfolio, broker, store, audit, market_data)
        except Exception:
            logger.exception("bot cycle failed -- will retry next interval")
        try:
            _export_and_push()
        except Exception:
            logger.exception("dashboard export/push failed -- will retry next interval")
        send_heartbeat_ping(cfg.healthcheck_ping_url)

        today = datetime.now(timezone.utc).date()
        if today != last_retention_run_date:
            run_log_retention(cfg)
            last_retention_run_date = today

        # Sleep interval - elapsed rather than a flat interval, so a pass that
        # took longer than usual (e.g. a bar-close scan, a slow push) doesn't
        # push the next tick's timing out by a full extra interval on top of
        # however long this one already took. Never sleeps negative.
        elapsed = time.monotonic() - cycle_started
        time.sleep(max(0.0, interval - elapsed))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()
