"""scripts/dashboard_deploy.py: direct `wrangler deploy` of ./web from the VM, and its wiring into scripts/run_live.py.

No network and no real wrangler: a fake `wrangler` executable (a tiny Python script written into tmp_path and pointed at
by HCT_WRANGLER_BIN) records its argv / cwd / environment and can succeed, fail, hang (with a grandchild that holds
the output pipe) or print the credentials. The clock is injected (dashboard_deploy.now_epoch).
"""
from __future__ import annotations

import json
import logging
import os
import stat
import sys
import threading
import time

import pytest

from scripts import dashboard_deploy as dd
from scripts import run_live

TOKEN = "cfut_SUPER-SECRET-TOKEN-0123456789abcdef"
ACCOUNT = "0123456789abcdef0123456789abcdef"
OTHER_SECRET = "exchange-secret-do-not-leak-987654"

FAKE_WRANGLER = r'''#!{python}
import json, os, subprocess, sys, time, urllib.parse
from pathlib import Path

here = Path(__file__).parent
mode = (here / "mode").read_text().strip()
with open(here / "record.jsonl", "a") as fh:
    fh.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ)}}) + "\n")
token, account = os.environ.get("CLOUDFLARE_API_TOKEN", ""), os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
if mode == "ok":
    print("Uploaded 1 asset\nDeployed hybrid-crypto-trader")
elif mode == "fail":
    print("ERROR: Cloudflare API says no")
    sys.exit(1)
elif mode == "leak_fail":
    print("Authorization: Bearer " + token)
    print("CLOUDFLARE_API_TOKEN=" + token + " account " + account + " enc " + urllib.parse.quote(token, safe=""))
    print("PUT /accounts/" + account + "/workers/scripts/x failed")
    sys.exit(1)
elif mode == "leak_ok":
    print("token " + token + " account " + account)
elif mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])  # inherits our stdout pipe
    (here / "child.pid").write_text(str(child.pid))
    time.sleep(300)
'''


def set_mode(box, mode: str) -> None:
    (box["bin"].parent / "mode").write_text(mode)


def records(box) -> list[dict]:
    path = box["bin"].parent / "record.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def make_data(equity: float = 10000.0, generated_at: str = "2026-10-22T10:00:00+00:00") -> dict:
    return {
        "generated_at": generated_at,
        "portfolio": {"cash": 10000.0, "equity": equity},
        "positions": [], "pending": [],
        "ops": {"trade_stats": {"n": 0}, "closed_trades": [], "decisions": [], "kill_switch": {"present": False},
                "health": {"latest_bar": {"bar_close": "2026-10-22T00:00:00+00:00"}, "last_event_at": generated_at},
                "parity": None, "equity_curve": {"points": [["2026-10-19T00:00:00+00:00", 10000.0, 10000.0], [generated_at, equity, 10000.0]]}},
    }


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def advance(self, minutes: float) -> None:
        self.t += minutes * 60

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A fake project root (web/data.json), an enabled environment, a fake wrangler, an injected clock."""
    root = tmp_path / "project"
    (root / "web").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrangler = bin_dir / "wrangler"
    wrangler.write_text(FAKE_WRANGLER.format(python=sys.executable))
    wrangler.chmod(wrangler.stat().st_mode | stat.S_IXUSR)
    clock = Clock()
    for name in (dd.TOKEN_ENV, dd.ACCOUNT_ENV, dd.DEPLOY_MINUTES_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(dd.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(dd.ACCOUNT_ENV, ACCOUNT)
    monkeypatch.setenv(dd.WRANGLER_BIN_ENV, str(wrangler))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", OTHER_SECRET)  # must never reach wrangler
    monkeypatch.setattr(dd, "now_epoch", clock)
    dd._reset_for_tests()
    out = {"root": root, "data": root / "web" / "data.json", "bin": wrangler, "clock": clock}
    set_mode(out, "ok")
    write(out)
    yield out
    dd._reset_for_tests()


def write(box, **kw) -> None:
    box["data"].write_text(json.dumps(make_data(**kw)))


def deploy(box) -> str:
    return dd.maybe_deploy(box["root"], box["data"])


def state(box) -> dict:
    return dd.load_state(dd.state_path(box["root"]))


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, 15.0), ("", 15.0), ("abc", 15.0), ("nan", 15.0), ("inf", 15.0), ("0", 15.0), ("-3", 15.0), (True, 15.0), ([], 15.0),
        ("15", 15.0), ("30", 30.0), (" 45 ", 45.0), ("7.5", 7.5), ("5", 5.0), ("4", 5.0), ("0.1", 5.0),
    ],
)
def test_parse_deploy_minutes(raw, expected):
    assert dd.parse_deploy_minutes(raw) == expected


@pytest.mark.parametrize("missing", ["token", "account", "both", "blank"])
def test_disabled_unless_both_credentials_are_present(box, monkeypatch, missing):
    if missing in ("token", "both"):
        monkeypatch.delenv(dd.TOKEN_ENV)
    if missing in ("account", "both"):
        monkeypatch.delenv(dd.ACCOUNT_ENV)
    if missing == "blank":
        monkeypatch.setenv(dd.TOKEN_ENV, "   ")
    assert dd.is_enabled() is False and dd.exported_interval_minutes() is None
    assert deploy(box) == "disabled"
    assert records(box) == [] and not dd.state_path(box["root"]).exists()


def test_exported_interval_follows_the_env(box, monkeypatch):
    assert dd.exported_interval_minutes() == 15.0
    monkeypatch.setenv(dd.DEPLOY_MINUTES_ENV, "garbage")
    assert dd.exported_interval_minutes() == 15.0
    monkeypatch.setenv(dd.DEPLOY_MINUTES_ENV, "2")
    assert dd.exported_interval_minutes() == 5.0
    monkeypatch.setenv(dd.DEPLOY_MINUTES_ENV, "30")
    assert dd.exported_interval_minutes() == 30.0


def test_backoff_schedule():
    assert [dd.backoff_minutes(n) for n in (0, 1, 2, 3, 4, 5, 6, 50, 5000)] == [0, 5, 10, 20, 40, 60, 60, 60, 60]


# --------------------------------------------------------------------------- #
# cadence
# --------------------------------------------------------------------------- #
def test_first_run_deploys_from_the_repo_root_and_records_state(box):
    assert deploy(box) == "deployed"
    (rec,) = records(box)
    assert rec["argv"] == ["deploy"] and os.path.realpath(rec["cwd"]) == os.path.realpath(box["root"])
    st = state(box)
    assert st["fingerprint"] == dd.fingerprint_file(box["data"]) and st["deployed_at"] == box["clock"].t


def test_unchanged_content_within_the_heartbeat_does_not_deploy(box):
    deploy(box)
    for minute in (2, 3, 4):  # only timestamps move; 9 minutes < the 15 minute heartbeat
        box["clock"].advance(minute)
        write(box, generated_at=f"2026-10-22T10:{minute:02d}:30+00:00")
        assert deploy(box) == "skipped"
    assert len(records(box)) == 1


def test_content_change_deploys_immediately(box):
    deploy(box)
    box["clock"].advance(1)
    write(box, equity=10234.5)
    assert deploy(box) == "deployed" and len(records(box)) == 2
    assert state(box)["fingerprint"] == dd.fingerprint_file(box["data"])


def test_heartbeat_deploys_after_the_interval(box, monkeypatch):
    deploy(box)
    box["clock"].advance(14.9)
    assert deploy(box) == "skipped"
    box["clock"].advance(0.2)
    assert deploy(box) == "deployed" and len(records(box)) == 2
    monkeypatch.setenv(dd.DEPLOY_MINUTES_ENV, "30")  # a longer heartbeat is honoured
    box["clock"].advance(20)
    assert deploy(box) == "skipped"
    box["clock"].advance(10.1)
    assert deploy(box) == "deployed"


def test_restart_with_persisted_state_does_not_redeploy(box):
    deploy(box)
    dd._reset_for_tests()  # a new process: only logs/dashboard_deploy_state.json carries over
    box["clock"].advance(3)
    assert deploy(box) == "skipped" and len(records(box)) == 1


@pytest.mark.parametrize("content", ["{garbage", "[]", '{"fingerprint": 5, "deployed_at": "x"}', '{"fingerprint": "a", "deployed_at": true}', ""])
def test_corrupt_state_deploys(box, content):
    deploy(box)
    dd.state_path(box["root"]).write_text(content)
    box["clock"].advance(1)
    assert deploy(box) == "deployed" and len(records(box)) == 2
    assert state(box)["fingerprint"]  # rewritten as valid state


def test_missing_state_deploys(box):
    deploy(box)
    dd.state_path(box["root"]).unlink()
    assert deploy(box) == "deployed" and len(records(box)) == 2


def test_state_file_is_written_atomically_and_is_gitignored_by_pattern(box):
    deploy(box)
    path = dd.state_path(box["root"])
    assert path.parent.name == "logs" and path.name == "dashboard_deploy_state.json"  # logs/*.json is gitignored
    assert not list(path.parent.glob("*.tmp"))  # temp file replaced, not left behind


def test_unreadable_export_is_only_deployed_by_the_heartbeat(box):
    deploy(box)
    box["data"].write_text("{not json")
    box["clock"].advance(5)
    assert deploy(box) == "skipped"
    box["clock"].advance(11)
    assert deploy(box) == "deployed" and state(box)["fingerprint"] == dd.UNKNOWN_FINGERPRINT


# --------------------------------------------------------------------------- #
# failure, backoff, timeout
# --------------------------------------------------------------------------- #
def test_failure_leaves_state_alone_and_backs_off(box):
    deploy(box)
    before = dd.state_path(box["root"]).read_text()
    set_mode(box, "fail")
    box["clock"].advance(16)  # heartbeat due
    assert deploy(box) == "failed" and len(records(box)) == 2
    assert dd.state_path(box["root"]).read_text() == before  # not advanced
    # inside the 5 minute backoff: no attempt, even though the content changed
    box["clock"].advance(4.9)
    write(box, equity=1.0)
    assert deploy(box) == "backoff" and len(records(box)) == 2
    box["clock"].advance(0.2)
    assert deploy(box) == "failed" and len(records(box)) == 3  # retried after >= 5 min; second failure -> 10 min
    box["clock"].advance(9.9)
    assert deploy(box) == "backoff"
    box["clock"].advance(0.2)
    assert deploy(box) == "failed" and len(records(box)) == 4  # third failure -> 20 min
    box["clock"].advance(19.9)
    assert deploy(box) == "backoff"
    set_mode(box, "ok")
    box["clock"].advance(0.2)
    assert deploy(box) == "deployed" and len(records(box)) == 5
    assert state(box)["fingerprint"] == dd.fingerprint_file(box["data"])
    # success resets the backoff: the next content change deploys at once
    box["clock"].advance(1)
    write(box, equity=2.0)
    assert deploy(box) == "deployed"


def test_backoff_is_capped_at_60_minutes(box):
    set_mode(box, "fail")
    for _ in range(8):
        deploy(box)
        box["clock"].advance(61)
    assert dd._next_attempt_at - (box["clock"].t - 61) <= 60 * 60 + 1


def test_a_missing_wrangler_is_a_logged_failure_with_backoff_not_an_exception(box, monkeypatch, caplog):
    monkeypatch.setenv(dd.WRANGLER_BIN_ENV, str(box["root"] / "nope" / "wrangler"))
    with caplog.at_level(logging.INFO, logger="dashboard_deploy"):
        assert deploy(box) == "failed"
    assert not dd.state_path(box["root"]).exists()
    assert deploy(box) == "backoff"
    box["clock"].advance(6)
    assert deploy(box) == "failed"


def test_no_wrangler_anywhere_backs_off(box, monkeypatch):
    monkeypatch.delenv(dd.WRANGLER_BIN_ENV)
    monkeypatch.setattr(dd.shutil, "which", lambda name: None)
    assert deploy(box) == "no-wrangler" and deploy(box) == "backoff"


def test_wrangler_resolution_order(box, monkeypatch):
    root = box["root"]
    assert dd.resolve_wrangler(root) == [str(box["bin"])]  # explicit override
    monkeypatch.delenv(dd.WRANGLER_BIN_ENV)
    local = root / "node_modules" / ".bin" / "wrangler"
    local.parent.mkdir(parents=True)
    local.write_text("#!/bin/sh\n")
    local.chmod(0o755)
    assert dd.resolve_wrangler(root) == [str(local)]
    local.unlink()
    monkeypatch.setattr(dd.shutil, "which", lambda name: {"wrangler": "/usr/bin/wrangler", "npx": "/usr/bin/npx"}.get(name))
    assert dd.resolve_wrangler(root) == ["/usr/bin/wrangler"]
    monkeypatch.setattr(dd.shutil, "which", lambda name: {"npx": "/usr/bin/npx"}.get(name))
    assert dd.resolve_wrangler(root) == ["/usr/bin/npx", "--no-install", "wrangler"]
    monkeypatch.setattr(dd.shutil, "which", lambda name: None)
    assert dd.resolve_wrangler(root) is None


def _alive(pid: int) -> bool:
    """True while `pid` is a running process (a zombie awaiting its reaper counts as dead)."""
    try:
        stat_text = open(f"/proc/{pid}/stat").read()
    except OSError:
        return False
    return stat_text.rsplit(")", 1)[1].split()[0] != "Z"


def test_timeout_kills_the_whole_process_group_and_returns(box, monkeypatch, caplog):
    monkeypatch.setattr(dd, "DEPLOY_TIMEOUT_SECONDS", 1.5)
    set_mode(box, "hang")
    started = time.monotonic()
    with caplog.at_level(logging.INFO, logger="dashboard_deploy"):
        assert deploy(box) == "failed"
    assert time.monotonic() - started < 10  # returned promptly after the 1.5 s hard timeout, not after 300 s
    assert "timeout" in caplog.text and not dd.state_path(box["root"]).exists()
    child_pid = int((box["bin"].parent / "child.pid").read_text())
    for _ in range(50):  # the grandchild (which held the output pipe) was killed with the group
        if not _alive(child_pid):
            break
        time.sleep(0.1)
    else:
        pytest.fail("grandchild process survived the timeout")
    assert deploy(box) == "backoff"


# --------------------------------------------------------------------------- #
# secrets
# --------------------------------------------------------------------------- #
def test_credentials_are_in_the_subprocess_env_not_argv_and_nothing_else_leaks_in(box):
    deploy(box)
    (rec,) = records(box)
    assert rec["env"]["CLOUDFLARE_API_TOKEN"] == TOKEN and rec["env"]["CLOUDFLARE_ACCOUNT_ID"] == ACCOUNT
    blob = " ".join(rec["argv"])
    assert TOKEN not in blob and ACCOUNT not in blob and rec["argv"] == ["deploy"]
    assert "TELEGRAM_BOT_TOKEN" not in rec["env"] and OTHER_SECRET not in json.dumps(rec["env"])  # whitelisted env
    assert rec["env"]["CI"] == "1" and rec["env"]["WRANGLER_SEND_METRICS"] == "false"


def test_the_command_line_never_contains_the_credentials(box, monkeypatch):
    seen = {}
    real_popen = dd.subprocess.Popen

    def spy(cmd, *a, **kw):
        seen["cmd"] = list(cmd)
        return real_popen(cmd, *a, **kw)

    monkeypatch.setattr(dd.subprocess, "Popen", spy)
    deploy(box)
    assert seen["cmd"] == [str(box["bin"]), "deploy"]


@pytest.mark.parametrize("mode", ["leak_fail", "leak_ok"])
def test_credentials_never_reach_the_log_even_if_wrangler_prints_them(box, caplog, mode):
    set_mode(box, mode)
    with caplog.at_level(logging.DEBUG):  # everything, every logger
        deploy(box)
        box["clock"].advance(6)
        deploy(box)
    text = caplog.text
    assert TOKEN not in text and ACCOUNT not in text and "SUPER-SECRET" not in text
    if mode == "leak_fail":
        assert "dashboard deploy FAILED" in text and "<redacted>" in text  # the tail was logged, scrubbed
        assert "Bearer <redacted>" in text


def test_scrub_removes_env_values_encodings_and_telegram_tokens(box):
    raw = f"a {TOKEN} b {ACCOUNT} d Bearer abc.DEF-123 e bot123456:AAH_abcdefghijklmnopqrstuvwxyz0123456 f"
    out = dd.scrub(raw)
    assert TOKEN not in out and ACCOUNT not in out and "abc.DEF-123" not in out and "AAH_abcdef" not in out
    assert "a%2Fb%2Bc" not in dd.scrub("x a%2Fb%2Bc%3Ddd y", ("a/b+c=dd",))  # URL-encoded form of an explicit secret
    assert dd.scrub(None) == "None" and dd.scrub(object()).startswith("<")


def test_scrub_of_an_explicit_secret_even_after_the_env_is_cleared(box, monkeypatch):
    monkeypatch.delenv(dd.TOKEN_ENV)
    assert "hunter2hunter2" not in dd.scrub("x hunter2hunter2 y", ("hunter2hunter2",))


def test_one_info_line_per_successful_deploy(box, caplog):
    with caplog.at_level(logging.INFO, logger="dashboard_deploy"):
        deploy(box)
    lines = [r for r in caplog.records if r.name == "dashboard_deploy"]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO and "ok in" in lines[0].getMessage()


# --------------------------------------------------------------------------- #
# overlap, never-raise
# --------------------------------------------------------------------------- #
def test_overlapping_deploys_are_prevented(box, monkeypatch):
    gate, entered = threading.Event(), threading.Event()
    calls = []

    def slow_wrangler(*args, **kwargs):
        calls.append(1)
        entered.set()
        gate.wait(10)
        return "ok", 0.1, ""

    monkeypatch.setattr(dd, "run_wrangler", slow_wrangler)
    monkeypatch.setattr(dd, "DEPLOY_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(dd, "JOIN_GRACE_SECONDS", 0.0)
    started = time.monotonic()
    assert deploy(box) == "running"  # the caller gave up waiting; the worker is still going
    assert time.monotonic() - started < 3 and entered.is_set()
    box["clock"].advance(30)
    write(box, equity=5.0)
    assert deploy(box) == "busy" and calls == [1]  # no second wrangler while the first is alive
    gate.set()
    for _ in range(100):
        if not dd._worker.is_alive():
            break
        time.sleep(0.05)
    assert state(box)["fingerprint"] is not None  # the finished background deploy still recorded its success
    assert deploy(box) == "deployed" and calls == [1, 1]  # and the next one may start


def test_concurrent_callers_start_exactly_one_deploy(box, monkeypatch):
    gate = threading.Event()
    calls = []

    def slow_wrangler(*args, **kwargs):
        calls.append(1)
        gate.wait(10)
        return "ok", 0.1, ""

    monkeypatch.setattr(dd, "run_wrangler", slow_wrangler)
    monkeypatch.setattr(dd, "DEPLOY_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(dd, "JOIN_GRACE_SECONDS", 0.0)
    results = []
    threads = [threading.Thread(target=lambda: results.append(deploy(box))) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    gate.set()
    assert calls == [1] and sorted(results).count("running") == 1 and results.count("busy") == 5


def test_maybe_deploy_never_raises(box, monkeypatch):
    monkeypatch.setattr(dd, "fingerprint_file", lambda p: (_ for _ in ()).throw(RuntimeError("boom " + TOKEN)))
    assert deploy(box) == "error"


def test_a_crash_inside_the_worker_is_a_failure_with_backoff(box, monkeypatch, caplog):
    monkeypatch.setattr(dd, "run_wrangler", lambda *a, **k: (_ for _ in ()).throw(OSError(TOKEN)))
    with caplog.at_level(logging.DEBUG):
        assert deploy(box) == "failed"
    assert TOKEN not in caplog.text and not dd.state_path(box["root"]).exists()
    assert deploy(box) == "backoff"


def test_unwritable_state_directory_does_not_raise(box, monkeypatch):
    monkeypatch.setattr(dd.os, "replace", lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied")))
    assert deploy(box) == "deployed"  # the deploy itself worked; only the bookkeeping failed (logged)


# --------------------------------------------------------------------------- #
# run_live wiring
# --------------------------------------------------------------------------- #
def test_run_live_deploy_wrapper_calls_maybe_deploy_only_after_a_good_export(box, monkeypatch):
    monkeypatch.setattr(run_live, "PROJECT_ROOT", box["root"])
    run_live._export_ok = False
    run_live._deploy_dashboard()
    assert records(box) == []  # the export did not succeed this cycle: nothing stale is redeployed
    run_live._export_ok = True
    run_live._deploy_dashboard()
    assert len(records(box)) == 1


def test_export_and_push_marks_the_export_ok_even_when_the_git_push_fails(box, monkeypatch):
    monkeypatch.setattr(run_live, "PROJECT_ROOT", box["root"])
    monkeypatch.setattr(run_live, "_run_export", lambda: None)

    def boom(cmd, **kw):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(run_live.subprocess, "run", boom)
    run_live._export_ok = False
    with pytest.raises(RuntimeError):
        run_live._export_and_push()
    assert run_live._export_ok is True  # a git problem must not block publishing the fresh export
    monkeypatch.setattr(run_live, "_run_export", lambda: (_ for _ in ()).throw(RuntimeError("export failed")))
    with pytest.raises(RuntimeError):
        run_live._export_and_push()
    assert run_live._export_ok is False


class _StopLoop(Exception):
    pass


def _run_one_loop_iteration(monkeypatch, order: list[str]) -> None:
    cfg = type("Cfg", (), {"mode": "paper", "exchange_id": "x", "symbols": ["BTC/USD"], "loop": {"poll_interval_seconds": 300},
                           "healthcheck_ping_url": None})()
    monkeypatch.setattr(run_live, "load_config", lambda: cfg)
    monkeypatch.setattr(run_live, "build_components", lambda c: (None,) * 6)
    monkeypatch.setattr(run_live, "build_market_data", lambda c, e: None)
    monkeypatch.setattr(run_live, "run_cycle", lambda *a, **k: order.append("cycle"))
    monkeypatch.setattr(run_live, "send_heartbeat_ping", lambda url: order.append("ping"))
    monkeypatch.setattr(run_live, "run_log_retention", lambda c: order.append("retention"))

    def stop(_seconds):
        order.append("sleep")
        raise _StopLoop

    monkeypatch.setattr(run_live.time, "sleep", stop)
    with pytest.raises(_StopLoop):
        run_live.main()


def test_the_trading_loop_survives_a_deploy_that_blows_up(box, monkeypatch, caplog):
    order: list[str] = []
    monkeypatch.setattr(run_live, "_export_and_push", lambda: (order.append("export"), setattr(run_live, "_export_ok", True)))
    monkeypatch.setattr(run_live.dashboard_deploy, "maybe_deploy", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("deploy bug " + TOKEN)))
    with caplog.at_level(logging.DEBUG):
        _run_one_loop_iteration(monkeypatch, order)
    assert order == ["cycle", "export", "ping", "retention", "sleep"]  # the loop went on to the heartbeat ping and sleep
    assert "failed unexpectedly" in caplog.text and TOKEN not in caplog.text


def test_the_trading_loop_is_not_blocked_by_a_hung_wrangler(box, monkeypatch):
    monkeypatch.setattr(dd, "DEPLOY_TIMEOUT_SECONDS", 1.5)
    monkeypatch.setattr(run_live, "PROJECT_ROOT", box["root"])
    set_mode(box, "hang")
    order: list[str] = []
    monkeypatch.setattr(run_live, "_export_and_push", lambda: (order.append("export"), setattr(run_live, "_export_ok", True)))
    started = time.monotonic()
    _run_one_loop_iteration(monkeypatch, order)
    assert time.monotonic() - started < 15  # bounded by the hard timeout, nowhere near wrangler's 300 s hang
    assert order == ["cycle", "export", "ping", "retention", "sleep"]


def test_the_loop_does_nothing_extra_when_direct_deploy_is_disabled(box, monkeypatch):
    monkeypatch.delenv(dd.TOKEN_ENV)
    monkeypatch.setattr(run_live, "PROJECT_ROOT", box["root"])
    order: list[str] = []
    monkeypatch.setattr(run_live, "_export_and_push", lambda: (order.append("export"), setattr(run_live, "_export_ok", True)))
    _run_one_loop_iteration(monkeypatch, order)
    assert records(box) == [] and order == ["cycle", "export", "ping", "retention", "sleep"]
