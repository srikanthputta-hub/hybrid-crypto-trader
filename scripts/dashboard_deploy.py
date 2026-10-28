"""Direct dashboard deploy for the VM (scripts/run_live.py): `wrangler deploy` of the static ./web site.

Why this exists: every push to `main` triggers a Cloudflare Workers Build, and build minutes ran out. The throttle in
scripts/dashboard_push.py cut pushes to ~30/day; this module removes the dependency altogether by uploading the
static assets (wrangler.jsonc: assets directory ./web, which includes the freshly exported web/data.json) straight
from the VM. Git pushes of web/data.json stay as optional history (the orchestrator reads it from git).

ENABLED only when BOTH CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID are in the environment (they come from the
gitignored .env that load_config() loads). Otherwise maybe_deploy() is a silent no-op.

Cadence: deploy when the dashboard content fingerprint (scripts/dashboard_push.content_fingerprint) changed since the
last SUCCESSFUL deploy, or the heartbeat (HCT_DASHBOARD_DEPLOY_MINUTES, default 15, floor 5) is due. A failed deploy
leaves the persisted state untouched and is retried only after a backoff (5, 10, 20, 40, then 60 minutes between
attempts while failures continue), so a Cloudflare outage cannot make every cycle spend time or spam the log.

Safety (this runs inside the trading loop's process, so a bug here must never stop, slow or crash trading):
  * maybe_deploy() never raises (every Exception is caught and logged, scrubbed).
  * wrangler runs in a worker thread (at most one at a time -- overlapping deploys are impossible) as a subprocess in
    its own session with a hard timeout; on timeout the whole process group is killed. The caller waits for the
    worker at most DEPLOY_TIMEOUT_SECONDS + a small grace, so a hung wrangler cannot block the next bot cycle.
  * The credentials reach wrangler ONLY through the subprocess environment (never argv), and that environment is a
    whitelist (PATH, HOME, proxy/CA settings, ...) plus the two Cloudflare values -- the exchange / Telegram / Claude
    secrets in the VM's environment are not handed to the Node toolchain.
  * Nothing from the environment is ever logged: captured wrangler output is scrubbed of the two values (raw and
    URL-encoded), Bearer tokens and Telegram-token patterns before the short tail is logged.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from scripts.dashboard_push import UNKNOWN_FINGERPRINT, fingerprint_file
from src.notifier import redact_secrets

logger = logging.getLogger("dashboard_deploy")

TOKEN_ENV = "CLOUDFLARE_API_TOKEN"
ACCOUNT_ENV = "CLOUDFLARE_ACCOUNT_ID"
WRANGLER_BIN_ENV = "HCT_WRANGLER_BIN"
DEPLOY_MINUTES_ENV = "HCT_DASHBOARD_DEPLOY_MINUTES"

DEFAULT_DEPLOY_MINUTES = 15.0
MIN_DEPLOY_MINUTES = 5.0
DEPLOY_TIMEOUT_SECONDS = 120.0
JOIN_GRACE_SECONDS = 20.0  # extra wait for the worker after the subprocess timeout (kill + cleanup)
BACKOFF_BASE_MINUTES = 5.0
BACKOFF_MAX_MINUTES = 60.0
TAIL_CHARS = 300

STATE_RELATIVE_PATH = Path("logs") / "dashboard_deploy_state.json"  # logs/*.json and logs/*.tmp are gitignored

# What wrangler (a Node program) gets from our environment. Deliberately NOT the whole environment.
_PASSTHROUGH_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "TERM", "SHELL",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "NODE_OPTIONS",
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_ASSIGN_RE = re.compile(r"(?i)(CLOUDFLARE_API_TOKEN|CLOUDFLARE_ACCOUNT_ID|CF_API_TOKEN)\s*[=:]\s*\S+")
_MIN_SECRET_LITERAL = 4


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def credentials_from_env() -> tuple[str, str] | None:
    """(token, account_id) when BOTH are present and non-blank, else None. Values are only ever held in memory."""
    token = (os.environ.get(TOKEN_ENV) or "").strip()
    account = (os.environ.get(ACCOUNT_ENV) or "").strip()
    return (token, account) if token and account else None


def is_enabled() -> bool:
    return credentials_from_env() is not None


def parse_deploy_minutes(raw: Any) -> float:
    """Heartbeat minutes from an env-var string: garbage (None, empty, non-numeric, NaN, inf, bool, <=0) -> 15;
    a valid number below 5 is clamped up to 5. Never raises."""
    try:
        if raw is None or isinstance(raw, bool):
            return DEFAULT_DEPLOY_MINUTES
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_DEPLOY_MINUTES
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_DEPLOY_MINUTES
    return max(MIN_DEPLOY_MINUTES, value)


def deploy_interval_minutes() -> float:
    return parse_deploy_minutes(os.environ.get(DEPLOY_MINUTES_ENV))


def exported_interval_minutes() -> float | None:
    """What the exporter writes to ops.health.deploy_interval_minutes: the heartbeat when direct deploy is enabled,
    else None (the key is then omitted). Never raises."""
    try:
        return deploy_interval_minutes() if is_enabled() else None
    except Exception:  # noqa: BLE001 -- the exporter must not fail because of this
        return None


def state_path(project_root: Path) -> Path:
    return Path(project_root) / STATE_RELATIVE_PATH


def now_epoch() -> float:
    return time.time()


# --------------------------------------------------------------------------- #
# persisted state (last SUCCESSFUL deploy)
# --------------------------------------------------------------------------- #
def load_state(path: Path) -> dict[str, Any]:
    """{"fingerprint": str|None, "deployed_at": float|None}. Missing / corrupt / wrongly typed -> the empty state, which
    deploy_decision() reads as 'deploy now'. Never raises."""
    empty: dict[str, Any] = {"fingerprint": None, "deployed_at": None}
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return empty
    if not isinstance(raw, dict):
        return empty
    fp, at = raw.get("fingerprint"), raw.get("deployed_at")
    if not isinstance(fp, str) or isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
        return empty
    return {"fingerprint": fp, "deployed_at": float(at)}


def save_state(path: Path, fingerprint: str | None, deployed_at: float) -> bool:
    """Atomically (temp + os.replace) record a successful deploy. False (and a log line) on an OS error."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"fingerprint": fingerprint or UNKNOWN_FINGERPRINT, "deployed_at": deployed_at}))
        os.replace(tmp, path)
        return True
    except OSError as exc:
        logger.warning("could not persist dashboard deploy state to %s: %s", path, type(exc).__name__)
        return False


def deploy_decision(
    fingerprint: str | None, state: dict[str, Any], now: float, interval_minutes: float
) -> tuple[bool, str]:
    """(deploy?, reason): no valid state, content changed, or heartbeat due (a clock that moved backwards counts as
    due). An unfingerprintable export (None) is never 'changed' -- only the heartbeat can deploy it."""
    last_fp, last_at = state.get("fingerprint"), state.get("deployed_at")
    if last_fp is None or last_at is None:
        return True, "no previous deploy recorded"
    if fingerprint is not None and fingerprint != last_fp:
        return True, "content changed"
    elapsed = now - last_at
    if elapsed < 0 or elapsed >= interval_minutes * 60:
        return True, f"heartbeat ({interval_minutes:g} min)"
    return False, "unchanged within the heartbeat interval"


def backoff_minutes(consecutive_failures: int) -> float:
    """Minutes to wait after the Nth consecutive failure: 5, 10, 20, 40, 60, 60, ..."""
    if consecutive_failures <= 0:
        return 0.0
    exponent = min(consecutive_failures - 1, 10)
    return min(BACKOFF_MAX_MINUTES, BACKOFF_BASE_MINUTES * (2 ** exponent))


# --------------------------------------------------------------------------- #
# scrubbing / environment
# --------------------------------------------------------------------------- #
def scrub(text: Any, secrets: tuple[str, ...] = ()) -> str:
    """`text` with every given secret (raw and URL-encoded), the two Cloudflare env values, Bearer tokens, KEY=value
    assignments of the Cloudflare variables and Telegram-token patterns replaced by <redacted>. Never raises."""
    try:
        out = _ANSI_RE.sub("", str(text))
        literals = set(secrets)
        creds = credentials_from_env()
        if creds:
            literals.update(creds)
        for secret in sorted(literals, key=len, reverse=True):
            if len(secret) < _MIN_SECRET_LITERAL:
                continue
            for form in {secret, quote(secret, safe=""), quote(secret)}:
                out = out.replace(form, "<redacted>")
        out = _ASSIGN_RE.sub(lambda m: f"{m.group(1)}=<redacted>", out)
        out = _BEARER_RE.sub("Bearer <redacted>", out)
        return redact_secrets(out)
    except Exception:  # noqa: BLE001
        return "<unprintable>"


def _subprocess_env(token: str, account: str) -> dict[str, str]:
    env = {k: os.environ[k] for k in _PASSTHROUGH_ENV if k in os.environ}
    env[TOKEN_ENV] = token
    env[ACCOUNT_ENV] = account
    env.update(CI="1", NO_COLOR="1", WRANGLER_SEND_METRICS="false", WRANGLER_LOG="log")
    return env


def resolve_wrangler(project_root: Path) -> list[str] | None:
    """The argv prefix that runs wrangler: HCT_WRANGLER_BIN, else a project-local node_modules/.bin/wrangler, else a
    `wrangler` on PATH, else `npx --no-install wrangler`. None when none is available."""
    override = (os.environ.get(WRANGLER_BIN_ENV) or "").strip()
    if override:
        return [override]
    local = Path(project_root) / "node_modules" / ".bin" / "wrangler"
    if local.is_file() and os.access(local, os.X_OK):
        return [str(local)]
    on_path = shutil.which("wrangler")
    if on_path:
        return [on_path]
    npx = shutil.which("npx")
    if npx:
        return [npx, "--no-install", "wrangler"]
    return None


# --------------------------------------------------------------------------- #
# running wrangler
# --------------------------------------------------------------------------- #
def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # the child is its own session/group leader (start_new_session=True)
    except (AttributeError, ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def run_wrangler(
    project_root: Path, argv_prefix: list[str], token: str, account: str, timeout: float | None = None
) -> tuple[str, float, str]:
    """Run `wrangler deploy` from `project_root`. Returns (outcome, seconds, scrubbed tail) where outcome is "ok",
    "timeout", "exit <code>" or "error <ExceptionName>". Credentials go through the environment only."""
    timeout = DEPLOY_TIMEOUT_SECONDS if timeout is None else timeout
    started = time.monotonic()
    secrets = (token, account)
    try:
        proc = subprocess.Popen(
            [*argv_prefix, "deploy"],
            cwd=str(project_root),
            env=_subprocess_env(token, account),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,
        )
    except Exception as exc:  # noqa: BLE001 -- missing binary, permissions, ...
        return f"error {type(exc).__name__}", time.monotonic() - started, ""
    try:
        output, _ = proc.communicate(timeout=timeout)
        outcome = "ok" if proc.returncode == 0 else f"exit {proc.returncode}"
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            output, _ = proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001 -- a grandchild still holding the pipe must not hang us
            output = ""
        outcome = "timeout"
    except Exception as exc:  # noqa: BLE001
        _kill_group(proc)
        output, outcome = "", f"error {type(exc).__name__}"
    cleaned = scrub(output or "", secrets).strip()
    tail = cleaned[-TAIL_CHARS:].replace("\n", " | ")
    return outcome, time.monotonic() - started, tail


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_worker: threading.Thread | None = None
_failures = 0
_next_attempt_at = 0.0


def _reset_for_tests() -> None:
    global _worker, _failures, _next_attempt_at
    _worker, _failures, _next_attempt_at = None, 0, 0.0


def _worker_main(project_root: Path, argv_prefix: list[str], token: str, account: str, fingerprint: str | None, reason: str) -> None:
    global _failures, _next_attempt_at
    outcome, seconds, tail = "error", 0.0, ""
    try:
        outcome, seconds, tail = run_wrangler(project_root, argv_prefix, token, account)
    except Exception as exc:  # noqa: BLE001
        outcome = f"error {type(exc).__name__}"
    try:
        if outcome == "ok":
            _failures, _next_attempt_at = 0, 0.0
            saved = save_state(state_path(project_root), fingerprint, now_epoch())
            logger.info("dashboard deploy ok in %.1fs (%s)%s", seconds, reason, "" if saved else "; state not saved")
        else:
            _failures += 1
            wait = backoff_minutes(_failures)
            _next_attempt_at = now_epoch() + wait * 60
            logger.warning(
                "dashboard deploy FAILED (%s) after %.1fs (%s); next attempt in >= %g min%s",
                scrub(outcome, (token, account)), seconds, reason, wait, f"; output tail: {tail}" if tail else "",
            )
    except Exception as exc:  # noqa: BLE001 -- logging/bookkeeping must never escape the worker either
        try:
            logger.warning("dashboard deploy bookkeeping failed: %s", type(exc).__name__)
        except Exception:  # noqa: BLE001
            pass


def maybe_deploy(project_root: Path, data_path: Path) -> str:
    """Deploy ./web to Cloudflare when enabled and due. Returns a short status ("disabled", "busy", "backoff",
    "skipped", "no-wrangler", "started", "deployed", "failed", "running") for tests/diagnostics. NEVER raises, and waits
    at most DEPLOY_TIMEOUT_SECONDS + JOIN_GRACE_SECONDS."""
    global _worker
    try:
        creds = credentials_from_env()
        if creds is None:
            return "disabled"
        token, account = creds
        project_root = Path(project_root)
        with _lock:
            if _worker is not None and _worker.is_alive():
                logger.debug("dashboard deploy skipped: previous deploy still running")
                return "busy"
            now = now_epoch()
            if now < _next_attempt_at:
                logger.debug("dashboard deploy skipped: backing off after %d failure(s)", _failures)
                return "backoff"
            fingerprint = fingerprint_file(Path(data_path))
            due, reason = deploy_decision(fingerprint, load_state(state_path(project_root)), now, deploy_interval_minutes())
            if not due:
                logger.debug("dashboard deploy skipped: %s", reason)
                return "skipped"
            argv_prefix = resolve_wrangler(project_root)
            if argv_prefix is None:
                _record_failure_locked(now)
                logger.warning("dashboard deploy: no wrangler found (install it, or set %s); next attempt in >= %g min",
                               WRANGLER_BIN_ENV, backoff_minutes(_failures))
                return "no-wrangler"
            failures_before = _failures
            worker = threading.Thread(
                target=_worker_main,
                args=(project_root, argv_prefix, token, account, fingerprint, reason),
                name="dashboard-deploy",
                daemon=True,
            )
            _worker = worker
            worker.start()
        worker.join(timeout=DEPLOY_TIMEOUT_SECONDS + JOIN_GRACE_SECONDS)
        if worker.is_alive():
            logger.warning("dashboard deploy still running after %gs; not waiting longer (no second deploy will start)",
                           DEPLOY_TIMEOUT_SECONDS + JOIN_GRACE_SECONDS)
            return "running"
        return "failed" if _failures > failures_before else "deployed"
    except Exception as exc:  # noqa: BLE001 -- never into the trading loop
        try:
            logger.warning("dashboard deploy error: %s", scrub(type(exc).__name__))
        except Exception:  # noqa: BLE001
            pass
        return "error"


def _record_failure_locked(now: float) -> None:
    global _failures, _next_attempt_at
    _failures += 1
    _next_attempt_at = now + backoff_minutes(_failures) * 60
