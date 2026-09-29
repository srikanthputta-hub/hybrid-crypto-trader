import json
from datetime import datetime, timedelta, timezone

from src.log_retention import prune_jsonl_by_age, run_log_retention


def _line(ts: str | None, extra: dict | None = None) -> str:
    row = dict(extra or {})
    if ts is not None:
        row["timestamp"] = ts
    return json.dumps(row) + "\n"


def test_prune_jsonl_by_age_removes_old_rows_keeps_recent(tmp_path):
    path = tmp_path / "audit_log.jsonl"
    now = datetime(2026, 9, 29, tzinfo=timezone.utc)
    cutoff = now - timedelta(days=30)
    old = (cutoff - timedelta(days=1)).isoformat()
    recent = (cutoff + timedelta(days=1)).isoformat()
    path.write_text(_line(old, {"symbol": "OLD"}) + _line(recent, {"symbol": "RECENT"}))

    removed = prune_jsonl_by_age(path, cutoff, ["timestamp"])

    assert removed == 1
    remaining = [json.loads(line) for line in path.read_text().splitlines()]
    assert remaining == [{"timestamp": recent, "symbol": "RECENT"}]


def test_prune_jsonl_by_age_keeps_unparseable_and_missing_timestamps(tmp_path):
    path = tmp_path / "audit_log.jsonl"
    cutoff = datetime(2026, 9, 29, tzinfo=timezone.utc)
    path.write_text(
        "not json at all\n"
        + _line("not-a-real-timestamp", {"symbol": "BAD_TS"})
        + _line(None, {"symbol": "NO_TS"})
    )

    removed = prune_jsonl_by_age(path, cutoff, ["timestamp"])

    assert removed == 0
    assert path.read_text().count("\n") == 3


def test_prune_jsonl_by_age_missing_file_is_noop(tmp_path):
    path = tmp_path / "does_not_exist.jsonl"
    assert prune_jsonl_by_age(path, datetime.now(timezone.utc), ["timestamp"]) == 0


def test_prune_jsonl_by_age_falls_back_through_timestamp_keys(tmp_path):
    path = tmp_path / "history.jsonl"
    cutoff = datetime(2026, 9, 29, tzinfo=timezone.utc)
    old = (cutoff - timedelta(days=5)).isoformat()
    recent = (cutoff + timedelta(days=5)).isoformat()
    # closed_at missing -> falls back to filled_at; filled_at missing -> falls back to created_at
    path.write_text(
        json.dumps({"created_at": old, "filled_at": None, "closed_at": None}) + "\n"
        + json.dumps({"created_at": old, "filled_at": recent, "closed_at": None}) + "\n"
    )

    removed = prune_jsonl_by_age(path, cutoff, ["closed_at", "filled_at", "created_at"])

    assert removed == 1
    remaining = [json.loads(line) for line in path.read_text().splitlines()]
    assert remaining == [{"created_at": old, "filled_at": recent, "closed_at": None}]


class _FakeConfig:
    def __init__(self, retention_days, tmp_path):
        self._retention_days = retention_days
        self._tmp_path = tmp_path

    @property
    def logging_cfg(self):
        return {
            "audit_log_path": str(self._tmp_path / "audit_log.jsonl"),
            "trade_plan_history_path": str(self._tmp_path / "history.jsonl"),
            "retention_days": self._retention_days,
        }


def test_run_log_retention_disabled_when_non_positive(tmp_path, monkeypatch):
    monkeypatch.setattr("src.config.PROJECT_ROOT", tmp_path)
    audit_path = tmp_path / "audit_log.jsonl"
    old = (datetime(2000, 1, 1, tzinfo=timezone.utc)).isoformat()
    audit_path.write_text(_line(old))

    run_log_retention(_FakeConfig(0, tmp_path), now=datetime(2026, 9, 29, tzinfo=timezone.utc))

    assert audit_path.read_text().count("\n") == 1  # untouched


def test_run_log_retention_prunes_configured_files(tmp_path, monkeypatch):
    monkeypatch.setattr("src.config.PROJECT_ROOT", tmp_path)
    now = datetime(2026, 9, 29, tzinfo=timezone.utc)
    audit_path = tmp_path / "audit_log.jsonl"
    old = (now - timedelta(days=60)).isoformat()
    recent = (now - timedelta(days=1)).isoformat()
    audit_path.write_text(_line(old) + _line(recent))

    run_log_retention(_FakeConfig(30, tmp_path), now=now)

    remaining = [json.loads(line) for line in audit_path.read_text().splitlines()]
    assert remaining == [{"timestamp": recent}]
