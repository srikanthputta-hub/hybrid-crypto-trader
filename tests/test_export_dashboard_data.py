from datetime import datetime, timedelta, timezone

from scripts.export_dashboard_data import _backtester_role, _signal_history

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)


def _planned_event(symbol: str, minutes_ago: int) -> dict:
    ts = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    return {
        "symbol": symbol,
        "timestamp": ts,
        "action": "planned",
        "plan": {"entry_zone": (100.0, 101.0), "stop_loss": 95.0},
    }


def test_signal_history_includes_events_within_max_age():
    events = [_planned_event("BTC/USDT", minutes_ago=60)]
    history = _signal_history(events, NOW, max_age=timedelta(days=14))
    assert len(history) == 1
    assert history[0]["symbol"] == "BTC/USDT"
    assert history[0]["kind"] == "signal"


def test_signal_history_excludes_events_older_than_max_age():
    # 15 days old -- outside the default 14-day window.
    events = [_planned_event("BTC/USDT", minutes_ago=15 * 24 * 60)]
    history = _signal_history(events, NOW, max_age=timedelta(days=14))
    assert history == []


def test_signal_history_stops_scanning_once_past_cutoff():
    # events is chronological, so reversed() walks newest-first; once an event
    # older than the cutoff is hit, everything after it should be skipped too
    # -- not just that one event -- even though it's itself within range.
    events = [
        _planned_event("OLD/USDT", minutes_ago=20 * 24 * 60),  # 20 days old, oldest
        _planned_event("TOO_OLD/USDT", minutes_ago=15 * 24 * 60),  # 15 days old, past cutoff
        _planned_event("RECENT/USDT", minutes_ago=60),  # 1 hour old, within cutoff
    ]
    history = _signal_history(events, NOW, max_age=timedelta(days=14))
    symbols = [h["symbol"] for h in history]
    assert symbols == ["RECENT/USDT"]


def test_signal_history_handles_naive_timestamps():
    # datetime.fromisoformat on a timestamp with no timezone info -- should be
    # treated as UTC, not raise or silently drop the event.
    events = [
        {
            "symbol": "ETH/USDT",
            "timestamp": (NOW - timedelta(hours=1)).replace(tzinfo=None).isoformat(),
            "action": "planned",
            "plan": {"entry_zone": (100.0, 101.0), "stop_loss": 95.0},
        }
    ]
    history = _signal_history(events, NOW, max_age=timedelta(days=14))
    assert len(history) == 1


def test_signal_history_respects_max_items_safety_cap():
    events = [_planned_event("BTC/USDT", minutes_ago=i) for i in range(10)]
    history = _signal_history(events, NOW, max_age=timedelta(days=14), max_items=3)
    assert len(history) == 3


def _candidate(name: str, tested_at: str, gates: dict, dataset: str = "kraken_720") -> dict:
    return {"name": name, "tested_at": tested_at, "dataset": dataset, "gates": gates}


def test_backtester_role_idle_with_no_candidates():
    role = _backtester_role(None, NOW)
    assert role["id"] == "backtester"
    assert role["status"] == "idle"
    assert role["last_active"] is None
    assert "No candidates tested yet" in role["detail"]

    role_empty = _backtester_role({"candidates": []}, NOW)
    assert role_empty["status"] == "idle"


def test_backtester_role_working_when_latest_candidate_is_recent():
    tested_at = (NOW - timedelta(hours=1)).isoformat()
    summary = {
        "candidates": [
            _candidate("Donchian 25", tested_at, {"overall": "FAIL", "trades": "PASS", "ci": "FAIL", "oos": "PASS", "placebo": "FAIL", "edge": "PASS"})
        ]
    }
    role = _backtester_role(summary, NOW)
    assert role["status"] == "working"
    assert role["last_active"] == tested_at
    assert "gates passed" in role["detail"]
    assert "Donchian 25" in role["detail"]


def test_backtester_role_idle_when_latest_candidate_is_stale():
    tested_at = (NOW - timedelta(hours=12)).isoformat()
    summary = {"candidates": [_candidate("Donchian 25", tested_at, {"overall": "FAIL"})]}
    role = _backtester_role(summary, NOW)
    assert role["status"] == "idle"
    assert role["last_active"] == tested_at


def test_backtester_role_reports_all_gates_passed():
    tested_at = (NOW - timedelta(minutes=30)).isoformat()
    summary = {"candidates": [_candidate("Donchian 25", tested_at, {"overall": "PASS"})]}
    role = _backtester_role(summary, NOW)
    assert "passed all 5 gates" in role["detail"]


def test_backtester_role_picks_most_recently_tested_candidate():
    older = _candidate("Older one", (NOW - timedelta(hours=5)).isoformat(), {"overall": "FAIL"})
    newer_ts = (NOW - timedelta(minutes=10)).isoformat()
    newer = _candidate("Newer one", newer_ts, {"overall": "PASS"})
    role = _backtester_role({"candidates": [older, newer]}, NOW)
    assert "Newer one" in role["detail"]
    assert role["last_active"] == newer_ts
