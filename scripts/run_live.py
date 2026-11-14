#!/usr/bin/env python3
"""Continuous live loop for a real always-on host (a VPS, not CI): runs a bot
cycle, regenerates the dashboard's web/data.json, commits+pushes it to git
(throttled: only on a content change or a heartbeat, scripts/dashboard_push.py),
optionally deploys ./web straight to Cloudflare with wrangler (scripts/dashboard_deploy.py, only when
CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID are set -- no Workers Build involved),
prunes old log rows once a day (config.yaml logging.retention_days),
pings an optional external heartbeat monitor (HEALTHCHECK_PING_URL), and once per UTC day
checks the exchange's per-pair minimum orders against risk.min_order_quote_usd
(scripts/check_pair_minimums.py, issue #81; a Telegram warning when it fails),
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
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bot import build_components, build_market_data, run_cycle
from src.config import load_config
from src.heartbeat import send_heartbeat_ping
from src.log_retention import run_log_retention
from src import notifier
from scripts import dashboard_deploy, dashboard_push

PROJECT_ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger("run_live")

# True once this cycle's export has finished successfully (set by _export_and_push, read by _deploy_dashboard):
# a failed export must not redeploy a stale web/data.json.
_export_ok = False


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


def _run_export() -> None:
    """Regenerate web/data.json (local, cheap). The active heartbeat is passed down so the exporter can write it into
    data.json (ops.health.push_interval_minutes) and the page can scale its freshness thresholds to it."""
    env = {**os.environ, dashboard_push.PUSH_MINUTES_ENV: f"{dashboard_push.PUSH_MIN_INTERVAL_MINUTES:g}"}
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "export_dashboard_data.py")],
        check=True,
        cwd=PROJECT_ROOT,
        env=env,
    )


def _export_and_push() -> None:
    """Export every cycle, but commit+push only when the dashboard-relevant content changed or the heartbeat
    (PUSH_MIN_INTERVAL_MINUTES) is due -- every push is one Cloudflare build (see scripts/dashboard_push.py).

    The pushed-state (fingerprint + time, logs/dashboard_push_state.json) is advanced only AFTER a successful push, so
    a failed commit/push (this raises, the loop logs it) is retried on the next cycle. A skipped cycle leaves
    web/data.json modified in the working tree; that is harmless to the later `git pull --no-rebase`, which only
    ever runs right after we committed that file (so it is clean then) -- the next export regenerates it anyway.
    """
    global _export_ok
    _export_ok = False
    _run_export()
    _export_ok = True
    data_path = PROJECT_ROOT / "web" / "data.json"
    fingerprint = dashboard_push.fingerprint_file(data_path)
    state_file = dashboard_push.state_path(PROJECT_ROOT)
    due, reason = dashboard_push.push_decision(
        fingerprint,
        dashboard_push.load_state(state_file),
        dashboard_push.now_epoch(),
        dashboard_push.PUSH_MIN_INTERVAL_MINUTES,
    )
    if not due:
        logger.debug("dashboard push skipped: %s", reason)
        return
    logger.info("dashboard push: %s", reason)
    subprocess.run(["git", "add", "web/data.json"], check=True, cwd=PROJECT_ROOT)
    nothing_staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=PROJECT_ROOT).returncode == 0
    if not nothing_staged:  # never an empty commit
        subprocess.run(["git", "commit", "-m", "chore: refresh dashboard data"], check=True, cwd=PROJECT_ROOT)
    # Pushed even when nothing was staged: a commit left unpushed by an earlier failed attempt must still go out.
    _push_with_pull_retry()
    dashboard_push.save_state(state_file, fingerprint, dashboard_push.now_epoch())


def _deploy_dashboard() -> None:
    """Publish ./web straight to Cloudflare (scripts/dashboard_deploy.py), which bypasses Workers Builds entirely.
    A silent no-op unless CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID are set. maybe_deploy() never raises and waits
    at most its own hard timeout; the caller still wraps this in try/except so nothing can reach the trading loop."""
    if not _export_ok:
        return
    dashboard_deploy.maybe_deploy(PROJECT_ROOT, PROJECT_ROOT / "web" / "data.json")


# --------------------------------------------------------------------------- #
# per-pair exchange minimums (issue #81): once per UTC day, never in the way of trading
# --------------------------------------------------------------------------- #
PAIR_MINIMUMS_SCRIPT = PROJECT_ROOT / "scripts" / "check_pair_minimums.py"
PAIR_MINIMUMS_TIMEOUT_SECONDS = 180.0      # the whole check; each request is also capped (--timeout)
PAIR_MINIMUMS_REQUEST_TIMEOUT_SECONDS = 15.0
PAIR_MINIMUMS_TELEGRAM_TIMEOUT_SECONDS = 5.0
PAIR_MINIMUMS_OUTPUT_CHARS = 2500
# Environment variables never handed to the check (it needs none of them: public data only).
_SECRET_ENV_MARKERS = ("TOKEN", "SECRET", "KEY", "PASSWORD", "PASSPHRASE", "CHAT_ID", "CONFIRM", "PING_URL",
                       "ACCOUNT_ID")


def _check_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not any(m in k.upper() for m in _SECRET_ENV_MARKERS)}


class PairMinimumsMonitor:
    """Runs scripts/check_pair_minimums.py at most once per UTC day as a SEPARATE subprocess (its own
    session, no secrets in its environment, output to a temp file) and polls it on later ticks, so a
    slow exchange never blocks the loop. A run longer than PAIR_MINIMUMS_TIMEOUT_SECONDS is killed.
    A non-zero exit, a timeout or a failure to start is logged and sent to Telegram (escaped,
    redacted, capped). It never raises and never touches a trading decision: it only reads public
    market data in another process and sends a warning."""

    def __init__(self, popen=subprocess.Popen, clock=time.monotonic,
                 today=lambda: datetime.now(timezone.utc).date(), send=None) -> None:
        self._popen, self._clock, self._today = popen, clock, today
        self._send = send if send is not None else notifier.send_telegram_message
        self.last_date = None
        self.proc = None
        self._out = None
        self._started = 0.0
        self.results: list[str] = []          # outcome of each finished run (tests / debugging)

    def tick(self, cfg) -> None:
        try:
            self._tick(cfg)
        except Exception as exc:  # noqa: BLE001 -- monitoring must never disturb the trading loop
            logger.warning("pair-minimums check: internal error (%s) -- ignored", type(exc).__name__)

    def _tick(self, cfg) -> None:
        if self.proc is not None:
            code = self.proc.poll()
            if code is None:
                if self._clock() - self._started <= PAIR_MINIMUMS_TIMEOUT_SECONDS:
                    return
                self._kill()
                self._finish(cfg, f"did not finish within {PAIR_MINIMUMS_TIMEOUT_SECONDS:g} s (killed)")
                return
            self._finish(cfg, None if code == 0 else f"exit code {code}")
            return
        today = self._today()
        if today == self.last_date:
            return
        self.last_date = today                 # at most once per UTC day, even if it can't start
        try:
            self._out = tempfile.TemporaryFile()
            self.proc = self._popen(
                [sys.executable, str(PAIR_MINIMUMS_SCRIPT), "--timeout", f"{PAIR_MINIMUMS_REQUEST_TIMEOUT_SECONDS:g}"],
                cwd=str(PROJECT_ROOT), env=_check_env(), stdin=subprocess.DEVNULL, stdout=self._out,
                stderr=subprocess.STDOUT, start_new_session=True,
            )
            self._started = self._clock()
        except Exception as exc:  # noqa: BLE001
            self.proc = None
            self._finish(cfg, f"could not start ({type(exc).__name__})")

    def _kill(self) -> None:
        try:
            os.killpg(self.proc.pid, 9)
        except Exception:  # noqa: BLE001
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def _output(self) -> str:
        if self._out is None:
            return ""
        try:
            self._out.seek(0)
            return self._out.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return ""
        finally:
            try:
                self._out.close()
            except Exception:  # noqa: BLE001
                pass
            self._out = None

    def _finish(self, cfg, failure: str | None) -> None:
        output = self._output().strip()
        self.proc = None
        self.results.append("ok" if failure is None else failure)
        if failure is None:
            logger.info("pair-minimums check: OK")
            return
        token = getattr(cfg, "telegram_bot_token", None)
        output = notifier.redact_secrets(output, token)[-PAIR_MINIMUMS_OUTPUT_CHARS:]
        logger.warning("pair-minimums check FAILED (%s):\n%s", failure, output)
        try:
            prefix = notifier.label_prefix(getattr(cfg, "notifier_label", None))
        except Exception:  # noqa: BLE001
            prefix = ""
        text = (
            f"{prefix}⚠️ *Pair minimums check*: {notifier.escape_md(failure)}\n"
            + notifier.escape_md(
                "An exchange minimum order is at/near/above risk.min_order_quote_usd, or could not be checked. "
                "Live buys below a pair's minimum would be REFUSED; paper trading is unaffected.")
            + "\n"
            f"{notifier.escape_md(output)}"
        )
        self._send(token, getattr(cfg, "telegram_chat_id", None), text,
                   timeout=PAIR_MINIMUMS_TELEGRAM_TIMEOUT_SECONDS)


def main() -> None:
    cfg = load_config()
    if cfg.mode != "paper":
        raise SystemExit(
            f"config.yaml mode is {cfg.mode!r}, but scripts/run_live.py only runs paper mode. "
            "Set mode: paper in config/config.yaml for this always-on loop."
        )
    logger.info("starting live loop: mode=%s exchange=%s symbols=%s", cfg.mode, cfg.exchange_id, cfg.symbols)
    # load_config() has just loaded .env, so HCT_DASHBOARD_PUSH_MINUTES set there is visible only now.
    logger.info("dashboard push heartbeat: %g min", dashboard_push.refresh_interval_from_env())
    try:
        if dashboard_deploy.is_enabled():  # never log the credential values, only that they are present
            logger.info("dashboard direct deploy (wrangler): enabled, heartbeat %g min", dashboard_deploy.deploy_interval_minutes())
        else:
            logger.info("dashboard direct deploy (wrangler): disabled (CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID not both set)")
    except Exception:  # noqa: BLE001
        pass

    exchange, risk_engine, portfolio, broker, store, audit = build_components(cfg)
    market_data = build_market_data(cfg, exchange)
    interval = cfg.loop.get("poll_interval_seconds", 300)
    last_retention_run_date = None
    pair_minimums = PairMinimumsMonitor()

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
        try:
            _deploy_dashboard()
        except Exception:  # noqa: BLE001 -- the direct deploy must never be able to disturb the trading loop
            logger.warning("dashboard direct deploy failed unexpectedly -- will retry next interval")
        send_heartbeat_ping(cfg.healthcheck_ping_url)
        try:
            pair_minimums.tick(cfg)  # starts / polls a separate process; never blocks or raises
        except Exception:  # noqa: BLE001 -- monitoring must never be able to disturb the trading loop
            logger.warning("pair-minimums check failed unexpectedly -- will retry tomorrow")

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
