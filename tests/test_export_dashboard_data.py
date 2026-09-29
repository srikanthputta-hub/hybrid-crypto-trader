from datetime import datetime, timedelta, timezone

from scripts.export_dashboard_data import _signal_history

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
