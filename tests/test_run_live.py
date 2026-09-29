from unittest.mock import MagicMock, patch

import pytest

from scripts.run_live import _export_and_push, main


def test_main_refuses_live_mode(monkeypatch):
    monkeypatch.setattr("scripts.run_live.load_config", lambda: type("Cfg", (), {"mode": "live"})())
    with pytest.raises(SystemExit):
        main()


def test_export_and_push_skips_commit_when_no_diff():
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        result = MagicMock()
        result.returncode = 0  # "git diff --cached --quiet" returncode 0 == nothing staged
        return result

    with patch("scripts.run_live.subprocess.run", side_effect=fake_run):
        _export_and_push()

    assert not any(cmd[:2] == ["git", "commit"] for cmd in calls)
    assert not any(cmd == ["git", "push"] for cmd in calls)


def test_export_and_push_commits_and_pushes_when_changed():
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        result = MagicMock()
        result.returncode = 1 if cmd[:2] == ["git", "diff"] else 0
        return result

    with patch("scripts.run_live.subprocess.run", side_effect=fake_run):
        _export_and_push()

    assert any(cmd[:2] == ["git", "commit"] for cmd in calls)
    assert any(cmd == ["git", "push"] for cmd in calls)
    assert not any(cmd[:2] == ["git", "pull"] for cmd in calls)  # first push succeeded, no need to pull


def test_export_and_push_pulls_and_retries_when_push_rejected():
    # Simulates the recurring "remote moved ahead" case: another commit landed
    # on origin/main (e.g. a merged PR) since this loop's own last push, so a
    # plain `git push` is rejected as a non-fast-forward. The loop should pull
    # and retry once rather than surfacing that as a failure every cycle.
    calls = []
    push_attempts = 0

    def fake_run(cmd, **kwargs):
        nonlocal push_attempts
        calls.append(cmd)
        result = MagicMock()
        if cmd[:2] == ["git", "diff"]:
            result.returncode = 1  # something staged
        elif cmd == ["git", "push"]:
            push_attempts += 1
            result.returncode = 1 if push_attempts == 1 else 0  # first push rejected, retry succeeds
        else:
            result.returncode = 0
        return result

    with patch("scripts.run_live.subprocess.run", side_effect=fake_run):
        _export_and_push()

    assert push_attempts == 2
    push_indexes = [i for i, cmd in enumerate(calls) if cmd == ["git", "push"]]
    pull_index = next(i for i, cmd in enumerate(calls) if cmd[:2] == ["git", "pull"])
    assert push_indexes[0] < pull_index < push_indexes[1]
