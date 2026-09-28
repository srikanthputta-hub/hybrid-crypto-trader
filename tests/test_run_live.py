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
