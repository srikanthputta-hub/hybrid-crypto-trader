"""scripts/run_live.py's export+push step and its throttle (scripts/dashboard_push.py).

Two layers: mocked-subprocess tests for the command sequence, and a real-git harness (a temporary clone with a bare
origin) proving the throttle end to end: first run pushes; unchanged content inside the heartbeat does not commit;
a content change commits at once; the heartbeat commits after the interval; a restart with persisted state does not
re-push; a corrupt state file pushes; a failed push is retried on the next cycle. Nothing here touches the real
repo's logs/ or web/data.json: PROJECT_ROOT is redirected into tmp_path.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from scripts import dashboard_push, run_live
from scripts.run_live import _export_and_push, main


def test_main_refuses_live_mode(monkeypatch):
    monkeypatch.setattr("scripts.run_live.load_config", lambda: type("Cfg", (), {"mode": "live"})())
    with pytest.raises(SystemExit):
        main()


# --------------------------------------------------------------------------- #
# data.json builder + clock shared by both layers
# --------------------------------------------------------------------------- #
def make_data(generated_at: str, equity: float = 10000.0, bar_close: str = "2026-10-22T00:00:00+00:00") -> dict:
    return {
        "generated_at": generated_at,
        "portfolio": {"cash": 10000.0, "equity": equity, "pnl_pct": round((equity - 10000) / 10000, 4)},
        "positions": [],
        "pending": [],
        "ops": {
            "trade_stats": {"n": 0},
            "closed_trades": [],
            "decisions": [{"symbol": "BTC/USD", "state": "blocked", "reason": "r", "bar_close": bar_close, "timestamp": generated_at}],
            "kill_switch": {"present": False, "file": "KILL_SWITCH"},
            "health": {"latest_bar": {"bar_close": bar_close, "symbols_decided": 1, "stale_skips": 0, "errors": 0}, "last_event_at": generated_at},
            "parity": None,
            "equity_curve": {"points": [["2026-10-19T00:00:00+00:00", 10000.0, 10000.0], [generated_at, equity, 10000.0]]},
        },
    }


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def advance(self, minutes: float) -> None:
        self.t += minutes * 60

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(dashboard_push, "now_epoch", c)
    monkeypatch.setattr(dashboard_push, "PUSH_MIN_INTERVAL_MINUTES", 60.0)
    return c


# --------------------------------------------------------------------------- #
# mocked-subprocess layer
# --------------------------------------------------------------------------- #
@pytest.fixture
def sandbox(tmp_path, monkeypatch, clock):
    """A fake PROJECT_ROOT holding web/data.json; the export step just (re)writes that file from `box['data']`."""
    (tmp_path / "web").mkdir()
    box = {"data": make_data("2026-10-22T10:00:00+00:00")}

    def fake_export():
        (tmp_path / "web" / "data.json").write_text(json.dumps(box["data"]))

    monkeypatch.setattr(run_live, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(run_live, "_run_export", fake_export)
    box["root"] = tmp_path
    return box


def fake_runner(calls, *, staged=True, push_results=None):
    pushes = iter(push_results or [])

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        result = MagicMock()
        if cmd[:2] == ["git", "diff"]:
            result.returncode = 1 if staged else 0  # "git diff --cached --quiet": 1 == something staged
        elif cmd == ["git", "push"]:
            result.returncode = next(pushes, 0)
        else:
            result.returncode = 0
        return result

    return fake_run


def test_first_run_commits_and_pushes_and_records_state(sandbox):
    calls = []
    with patch("scripts.run_live.subprocess.run", side_effect=fake_runner(calls)):
        _export_and_push()
    assert ["git", "add", "web/data.json"] in calls
    assert any(cmd[:2] == ["git", "commit"] for cmd in calls) and ["git", "push"] in calls
    assert not any(cmd[:2] == ["git", "pull"] for cmd in calls)  # the first push succeeded, no need to pull
    state = dashboard_push.load_state(dashboard_push.state_path(sandbox["root"]))
    assert state["fingerprint"] == dashboard_push.fingerprint_file(sandbox["root"] / "web" / "data.json")


def test_no_commit_and_no_empty_commit_when_nothing_is_staged(sandbox):
    calls = []
    with patch("scripts.run_live.subprocess.run", side_effect=fake_runner(calls, staged=False)):
        _export_and_push()  # due (first run) but git sees no diff
    assert not any(cmd[:2] == ["git", "commit"] for cmd in calls)


def test_skipped_cycle_runs_no_git_command_at_all(sandbox, clock):
    with patch("scripts.run_live.subprocess.run", side_effect=fake_runner([])):
        _export_and_push()
    calls = []
    clock.advance(5)
    sandbox["data"] = make_data("2026-10-22T10:05:00+00:00")  # timestamp-only change
    with patch("scripts.run_live.subprocess.run", side_effect=fake_runner(calls)):
        _export_and_push()
    assert calls == []


def test_export_and_push_pulls_and_retries_when_push_rejected(sandbox):
    # Simulates the recurring "remote moved ahead" case: another commit landed on origin/main (e.g. a merged PR) since
    # this loop's own last push, so a plain `git push` is rejected as a non-fast-forward. The loop should pull and
    # retry once rather than surfacing that as a failure every cycle.
    calls = []
    with patch("scripts.run_live.subprocess.run", side_effect=fake_runner(calls, push_results=[1, 0])):
        _export_and_push()
    push_indexes = [i for i, cmd in enumerate(calls) if cmd == ["git", "push"]]
    pull_index = next(i for i, cmd in enumerate(calls) if cmd[:2] == ["git", "pull"])
    assert len(push_indexes) == 2 and push_indexes[0] < pull_index < push_indexes[1]
    assert dashboard_push.load_state(dashboard_push.state_path(sandbox["root"]))["fingerprint"] is not None


def test_failed_push_does_not_advance_state(sandbox):
    calls = []
    runner = fake_runner(calls, push_results=[1])

    def failing(cmd, **kwargs):
        if cmd[:2] == ["git", "pull"]:
            raise subprocess.CalledProcessError(1, cmd)
        return runner(cmd, **kwargs)

    with patch("scripts.run_live.subprocess.run", side_effect=failing):
        with pytest.raises(subprocess.CalledProcessError):
            _export_and_push()
    assert not dashboard_push.state_path(sandbox["root"]).exists()


def test_run_export_passes_the_heartbeat_to_the_exporter(monkeypatch, clock):
    seen = {}
    monkeypatch.setattr(dashboard_push, "PUSH_MIN_INTERVAL_MINUTES", 45.0)
    monkeypatch.setattr("scripts.run_live.subprocess.run", lambda cmd, **kw: seen.update(cmd=cmd, env=kw.get("env")))
    run_live._run_export()
    assert seen["env"][dashboard_push.PUSH_MINUTES_ENV] == "45" and seen["cmd"][1].endswith("export_dashboard_data.py")


# --------------------------------------------------------------------------- #
# real git: temp clone + bare origin
# --------------------------------------------------------------------------- #
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is needed")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Harness:
    def __init__(self, tmp_path, monkeypatch, clock):
        self.clock = clock
        self.origin = tmp_path / "origin.git"
        self.work = tmp_path / "work"
        git(tmp_path, "init", "--bare", "-b", "main", str(self.origin))
        git(tmp_path, "clone", str(self.origin), str(self.work))
        for cwd in (self.work,):
            git(cwd, "config", "user.name", "t")
            git(cwd, "config", "user.email", "t@example.com")
            git(cwd, "checkout", "-b", "main")
        (self.work / "web").mkdir()
        (self.work / "logs").mkdir()
        (self.work / ".gitignore").write_text("logs/*.json\nlogs/*.tmp\n")  # same rule as the real repo's .gitignore
        (self.work / "web" / "data.json").write_text("{}")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-m", "init")
        git(self.work, "push", "-u", "origin", "main")
        self.data = make_data("2026-10-22T10:00:00+00:00")
        self.tmp_path = tmp_path
        monkeypatch.setattr(run_live, "PROJECT_ROOT", self.work)
        monkeypatch.setattr(run_live, "_run_export", self._export)

    def _export(self):
        (self.work / "web" / "data.json").write_text(json.dumps(self.data))

    def cycle(self):
        _export_and_push()

    def origin_commits(self) -> int:
        return int(git(self.origin, "rev-list", "--count", "main"))

    @property
    def state_file(self):
        return dashboard_push.state_path(self.work)

    def set_data(self, minutes_after_first: int, **kw):
        self.data = make_data(f"2026-10-22T{10 + minutes_after_first // 60:02d}:{minutes_after_first % 60:02d}:00+00:00", **kw)


@pytest.fixture
def h(tmp_path, monkeypatch, clock):
    return Harness(tmp_path, monkeypatch, clock)


@needs_git
def test_real_git_first_run_pushes(h):
    before = h.origin_commits()
    h.cycle()
    assert h.origin_commits() == before + 1
    assert json.loads(git(h.origin, "show", "main:web/data.json"))["generated_at"] == "2026-10-22T10:00:00+00:00"
    assert git(h.work, "log", "-1", "--format=%s") == "chore: refresh dashboard data"
    assert h.state_file.exists()
    # nothing under logs/ is tracked, and the state file is not an untracked leftover either
    assert git(h.work, "ls-files", "logs") == "" and "logs/" not in git(h.work, "status", "--porcelain")


@needs_git
def test_real_git_unchanged_content_within_the_interval_does_not_commit(h):
    h.cycle()
    pushed = h.origin_commits()
    local_head = git(h.work, "rev-parse", "HEAD")
    for minute in (5, 10, 55):  # only timestamps move
        h.clock.advance(5 if minute < 55 else 45)
        h.set_data(minute)
        h.cycle()
    assert h.origin_commits() == pushed and git(h.work, "rev-parse", "HEAD") == local_head
    # the skipped exports are left modified in the working tree (the next export regenerates it)
    assert git(h.work, "status", "--porcelain") == "M web/data.json"


@needs_git
def test_real_git_content_change_commits_immediately(h):
    h.cycle()
    pushed = h.origin_commits()
    h.clock.advance(5)
    h.set_data(5, equity=10123.45)
    h.cycle()
    assert h.origin_commits() == pushed + 1
    assert json.loads(git(h.origin, "show", "main:web/data.json"))["portfolio"]["equity"] == 10123.45
    assert git(h.work, "status", "--porcelain") == ""


@needs_git
def test_real_git_a_new_bar_close_is_a_content_change(h):
    h.cycle()
    pushed = h.origin_commits()
    h.clock.advance(5)
    h.set_data(5, bar_close="2026-10-23T00:00:00+00:00")
    h.cycle()
    assert h.origin_commits() == pushed + 1


@needs_git
def test_real_git_heartbeat_commits_after_the_interval(h):
    h.cycle()
    pushed = h.origin_commits()
    h.clock.advance(59)
    h.set_data(59)
    h.cycle()
    assert h.origin_commits() == pushed  # not yet
    h.clock.advance(1)
    h.set_data(60)
    h.cycle()
    assert h.origin_commits() == pushed + 1
    assert json.loads(git(h.origin, "show", "main:web/data.json"))["generated_at"] == "2026-10-22T11:00:00+00:00"
    h.clock.advance(5)  # the heartbeat restarts the clock
    h.set_data(65)
    h.cycle()
    assert h.origin_commits() == pushed + 1


@needs_git
def test_real_git_restart_with_persisted_state_does_not_repush(h, monkeypatch):
    h.cycle()
    pushed = h.origin_commits()
    # "restart": a fresh interpreter has no in-memory state; only logs/dashboard_push_state.json carries over
    assert json.loads(h.state_file.read_text())["fingerprint"]
    h.clock.advance(7)
    h.set_data(7)
    h.cycle()
    assert h.origin_commits() == pushed


@needs_git
def test_real_git_corrupt_state_file_pushes(h):
    h.cycle()
    pushed = h.origin_commits()
    h.state_file.write_text("{garbage")
    h.clock.advance(5)
    h.set_data(5)  # timestamp-only change: only the corrupt state can be the reason to push
    h.cycle()
    assert h.origin_commits() == pushed + 1
    assert dashboard_push.load_state(h.state_file)["fingerprint"]  # rewritten as valid state


@needs_git
def test_real_git_missing_state_file_pushes(h):
    h.cycle()
    pushed = h.origin_commits()
    h.state_file.unlink()
    h.clock.advance(5)
    h.set_data(5)
    h.cycle()
    assert h.origin_commits() == pushed + 1


@needs_git
def test_real_git_failed_push_is_retried_next_cycle_without_an_empty_commit(h):
    h.cycle()
    good_url = git(h.work, "remote", "get-url", "origin")
    pushed = h.origin_commits()
    state_before = h.state_file.read_text()

    git(h.work, "remote", "set-url", "origin", str(h.tmp_path / "does-not-exist.git"))
    h.clock.advance(5)
    h.set_data(5, equity=9000.0)
    with pytest.raises(subprocess.CalledProcessError):
        h.cycle()
    assert h.state_file.read_text() == state_before  # not advanced
    assert h.origin_commits() == pushed

    git(h.work, "remote", "set-url", "origin", good_url)
    h.clock.advance(5)
    h.set_data(5, equity=9000.0)  # identical file to the one already committed locally: nothing new to stage
    local_commits = int(git(h.work, "rev-list", "--count", "HEAD"))
    h.cycle()
    assert h.origin_commits() == pushed + 1
    assert int(git(h.work, "rev-list", "--count", "HEAD")) == local_commits  # no empty commit was created
    assert json.loads(git(h.origin, "show", "main:web/data.json"))["portfolio"]["equity"] == 9000.0
    assert dashboard_push.load_state(h.state_file)["fingerprint"] != json.loads(state_before)["fingerprint"]


@needs_git
def test_real_git_pull_retry_works_after_skipped_cycles_left_data_json_dirty(h):
    h.cycle()
    h.clock.advance(5)
    h.set_data(5)
    h.cycle()  # skipped: web/data.json is now modified in the working tree
    assert git(h.work, "status", "--porcelain") == "M web/data.json"

    # someone else (a merged PR, another clone) advances origin/main with an unrelated file
    other = h.tmp_path / "other"
    git(h.tmp_path, "clone", str(h.origin), str(other))
    git(other, "config", "user.name", "o")
    git(other, "config", "user.email", "o@example.com")
    (other / "README.md").write_text("hello")
    git(other, "add", "README.md")
    git(other, "commit", "-m", "unrelated")
    git(other, "push", "origin", "main")

    h.clock.advance(5)
    h.set_data(10, equity=10500.0)  # a real change: commit, push is rejected, pull --no-rebase, push again
    h.cycle()
    assert json.loads(git(h.origin, "show", "main:web/data.json"))["portfolio"]["equity"] == 10500.0
    assert git(h.origin, "show", "main:README.md") == "hello"
    assert git(h.work, "status", "--porcelain") == ""


@needs_git
def test_real_git_a_dirty_data_json_does_not_block_a_pull(h):
    """The case the skip path creates: data.json modified, then `git pull --no-rebase` for an unrelated upstream file."""
    h.cycle()
    h.set_data(5)
    h._export()  # what a skipped cycle leaves behind
    other = h.tmp_path / "other"
    git(h.tmp_path, "clone", str(h.origin), str(other))
    git(other, "config", "user.name", "o")
    git(other, "config", "user.email", "o@example.com")
    (other / "NOTES.md").write_text("x")
    git(other, "add", "NOTES.md")
    git(other, "commit", "-m", "unrelated")
    git(other, "push", "origin", "main")
    git(h.work, "pull", "--no-rebase", "--no-edit")  # raises (check=True) if the dirty file blocked it
    assert (h.work / "NOTES.md").read_text() == "x"
