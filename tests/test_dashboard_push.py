"""scripts/dashboard_push.py: the push throttle's pure parts (env parse, content fingerprint, persisted state, the
push/skip decision). The git side (real repo, bare origin) is in tests/test_run_live.py."""
from __future__ import annotations

import copy
import json

import pytest

from scripts import dashboard_push as dp


# --------------------------------------------------------------------------- #
# HCT_DASHBOARD_PUSH_MINUTES
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, 60.0), ("", 60.0), ("   ", 60.0), ("abc", 60.0), ("nan", 60.0), ("inf", 60.0), ("-inf", 60.0),
        ("0", 60.0), ("-30", 60.0), (True, 60.0), ([], 60.0), ({}, 60.0),  # garbage -> the default, never an exception
        ("60", 60.0), ("45", 45.0), (" 90 ", 90.0), ("12.5", 12.5), (30, 30.0), ("1e2", 100.0),
        ("5", 5.0), ("4", 5.0), ("0.5", 5.0), ("0.0001", 5.0),  # a valid number below 5 clamps UP to 5
    ],
)
def test_parse_push_minutes(raw, expected):
    assert dp.parse_push_minutes(raw) == expected


def test_the_constant_is_a_validated_number_of_at_least_five():
    assert isinstance(dp.PUSH_MIN_INTERVAL_MINUTES, float) and dp.PUSH_MIN_INTERVAL_MINUTES >= dp.MIN_PUSH_MINUTES
    assert dp.DEFAULT_PUSH_MINUTES == 60.0 and dp.PUSH_MINUTES_ENV == "HCT_DASHBOARD_PUSH_MINUTES"


# --------------------------------------------------------------------------- #
# fingerprint
# --------------------------------------------------------------------------- #
def sample() -> dict:
    return {
        "generated_at": "2026-10-22T10:00:00+00:00",
        "mode": "paper",
        "portfolio": {"cash": 6100.0, "equity": 10000.0, "pnl_pct": 0.0},
        "positions": [{"symbol": "BTC/USD", "fill_price": 100.0, "current_price": 101.0, "timestamp": "2026-10-20T00:01:00+00:00"}],
        "pending": [{"symbol": "ETH/USD", "entry_zone": [1, 2], "timestamp": "2026-10-21T00:01:00+00:00"}],
        "pnl": {"hourly": [{"label": "2026-10-22 10:00", "pnl": 0.0}]},
        "ops": {
            "trade_stats": {"n": 3, "wins": 1, "avg_r_recosted": -0.2},
            "closed_trades": [{"symbol": "SOL/USD", "r": -1.0, "closed_at": "2026-10-19T00:00:00+00:00"}],
            "decisions": [
                {"symbol": "BTC/USD", "state": "blocked", "reason": "risk cap", "bar_close": "2026-10-22T00:00:00+00:00",
                 "timestamp": "2026-10-22T00:05:00+00:00", "pattern": "Breakout"},
            ],
            "kill_switch": {"present": False, "file": "KILL_SWITCH"},
            "health": {
                "expected_symbols": 16,
                "latest_bar": {"bar_close": "2026-10-22T00:00:00+00:00", "symbols_decided": 16, "stale_skips": 0, "errors": 0},
                "window": {"days": 7, "since": "2026-10-15T10:00:00+00:00", "events": 100, "stale_skips": 0, "errors": 0, "unavailable": 0},
                "last_event_at": "2026-10-22T09:59:00+00:00",
                "last_bar_close": "2026-10-22T00:00:00+00:00",
                "push_interval_minutes": 60.0,
            },
            "parity": {"status": "PASS", "message": "ok", "file": "parity_1.json", "file_modified": "2026-10-20T00:00:00+00:00"},
            "equity_curve": {"since": "2026-10-19T00:00:00+00:00", "points": [["2026-10-19T00:00:00+00:00", 10000.0, 10000.0], ["2026-10-22T10:00:00+00:00", 10000.0, 6100.0]]},
        },
    }


def test_the_field_list_is_defined_once_and_names_what_the_spec_lists():
    assert [name for name, _p, _r in dp.FINGERPRINT_FIELDS] == [
        "portfolio", "positions", "pending", "ops.trade_stats", "ops.closed_trades", "ops.decisions",
        "ops.kill_switch", "ops.health.latest_bar", "ops.parity", "ops.equity_curve.last_point_values",
    ]


def test_fingerprint_is_stable_and_a_hex_digest():
    fp = dp.content_fingerprint(sample())
    assert fp == dp.content_fingerprint(copy.deepcopy(sample())) and len(fp) == 64 and int(fp, 16) >= 0


def test_a_timestamp_only_change_leaves_the_fingerprint_unchanged():
    base = dp.content_fingerprint(sample())
    d = sample()
    d["generated_at"] = "2026-10-22T10:05:00+00:00"
    d["pnl"]["hourly"] = [{"label": "2026-10-22 11:00", "pnl": 0.0}]          # hourly labels / start times
    d["ops"]["health"]["last_event_at"] = "2026-10-22T10:04:00+00:00"
    d["ops"]["health"]["push_interval_minutes"] = 30.0
    d["ops"]["health"]["window"]["since"] = "2026-10-15T10:05:00+00:00"
    d["ops"]["health"]["window"]["events"] = 101
    d["ops"]["decisions"][0]["timestamp"] = "2026-10-22T00:10:00+00:00"        # decision timestamp / pattern are not in the reduction
    d["ops"]["decisions"][0]["pattern"] = "Pullback"
    d["ops"]["equity_curve"]["points"][-1][0] = "2026-10-22T10:05:00+00:00"    # curve END timestamp, same values
    d["ops"]["equity_curve"]["points"].insert(0, ["2026-10-18T00:00:00+00:00", 9000.0, 9000.0])  # earlier points are not fingerprinted
    d["ops"]["equity_curve"]["since"] = "2026-10-18T00:00:00+00:00"
    assert dp.content_fingerprint(d) == base


def _mutations():
    def set_(path, value):
        def apply(d):
            cur = d
            for key in path[:-1]:
                cur = cur[key]
            cur[path[-1]] = value
        return apply

    return {
        "portfolio.equity": set_(["portfolio", "equity"], 10001.0),
        "portfolio.cash": set_(["portfolio", "cash"], 1.0),
        "positions": lambda d: d["positions"].clear(),
        "positions.price": set_(["positions", 0, "current_price"], 105.0),
        "pending": lambda d: d["pending"].append({"symbol": "X"}),
        "trade_stats": set_(["ops", "trade_stats", "n"], 4),
        "closed_trades": lambda d: d["ops"]["closed_trades"].append({"symbol": "Y"}),
        "decision.state": set_(["ops", "decisions", 0, "state"], "entered"),
        "decision.reason": set_(["ops", "decisions", 0, "reason"], "other"),
        "decision.bar_close": set_(["ops", "decisions", 0, "bar_close"], "2026-10-23T00:00:00+00:00"),
        "decision.symbol": set_(["ops", "decisions", 0, "symbol"], "ETH/USD"),
        "decisions.rows": lambda d: d["ops"]["decisions"].append({"symbol": "Z", "state": "nodata", "reason": "r", "bar_close": None}),
        "kill_switch": set_(["ops", "kill_switch", "present"], True),
        "latest_bar.bar_close": set_(["ops", "health", "latest_bar", "bar_close"], "2026-10-23T00:00:00+00:00"),
        "latest_bar.decided": set_(["ops", "health", "latest_bar", "symbols_decided"], 15),
        "latest_bar.stale": set_(["ops", "health", "latest_bar", "stale_skips"], 1),
        "parity.status": set_(["ops", "parity", "status"], "FAIL"),
        "parity.gone": set_(["ops", "parity"], None),
        "equity.last_equity": set_(["ops", "equity_curve", "points", -1, 1], 9999.0),
        "equity.last_cash": set_(["ops", "equity_curve", "points", -1, 2], 5000.0),
        "ops.gone": lambda d: d.pop("ops"),
    }


@pytest.mark.parametrize("name", sorted(_mutations()))
def test_each_relevant_change_alters_the_fingerprint(name):
    d = sample()
    _mutations()[name](d)
    assert dp.content_fingerprint(d) != dp.content_fingerprint(sample()), name


def test_fingerprint_of_non_dicts_and_partial_exports():
    assert dp.content_fingerprint(None) is None and dp.content_fingerprint([1]) is None
    assert dp.content_fingerprint({}) is not None  # an older export without ops/portfolio still fingerprints
    bad = sample()
    bad["ops"]["decisions"] = "garbage"
    bad["ops"]["equity_curve"] = {"points": "x"}
    assert dp.content_fingerprint(bad) is not None


def test_fingerprint_file_tolerates_missing_and_corrupt_files(tmp_path):
    assert dp.fingerprint_file(tmp_path / "nope.json") is None
    (tmp_path / "bad.json").write_text("{not json")
    assert dp.fingerprint_file(tmp_path / "bad.json") is None
    (tmp_path / "ok.json").write_text(json.dumps(sample()))
    assert dp.fingerprint_file(tmp_path / "ok.json") == dp.content_fingerprint(sample())


# --------------------------------------------------------------------------- #
# persisted state
# --------------------------------------------------------------------------- #
def test_state_round_trip_is_atomic_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "logs" / "dashboard_push_state.json"
    assert dp.save_state(path, "abc", 1234.5) is True
    assert dp.load_state(path) == {"fingerprint": "abc", "pushed_at": 1234.5}
    assert [p.name for p in path.parent.iterdir()] == ["dashboard_push_state.json"]


def test_a_none_fingerprint_is_stored_as_a_valid_unknown_record(tmp_path):
    path = tmp_path / "s.json"
    dp.save_state(path, None, 10.0)
    assert dp.load_state(path) == {"fingerprint": dp.UNKNOWN_FINGERPRINT, "pushed_at": 10.0}


@pytest.mark.parametrize(
    "content",
    ["", "{not json", "[]", "null", "42", '{"fingerprint": 5, "pushed_at": 1}', '{"fingerprint": "a"}', '{"fingerprint": "a", "pushed_at": "x"}',
     '{"fingerprint": "a", "pushed_at": true}', '{"fingerprint": "a", "pushed_at": NaN}', "\x00\x01"],
)
def test_corrupt_state_loads_as_empty(tmp_path, content):
    path = tmp_path / "s.json"
    path.write_text(content)
    assert dp.load_state(path) == {"fingerprint": None, "pushed_at": None}


def test_missing_state_loads_as_empty_and_save_failure_does_not_raise(tmp_path):
    assert dp.load_state(tmp_path / "absent.json") == {"fingerprint": None, "pushed_at": None}
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert dp.save_state(blocker / "sub" / "s.json", "a", 1.0) is False  # parent is a file: OSError swallowed


def test_state_path_lives_under_logs():
    assert str(dp.state_path("/repo")) == "/repo/logs/dashboard_push_state.json"


# --------------------------------------------------------------------------- #
# the decision
# --------------------------------------------------------------------------- #
def test_push_decision():
    st = {"fingerprint": "A", "pushed_at": 1000.0}
    minute = 60
    assert dp.push_decision("A", {"fingerprint": None, "pushed_at": None}, 1000.0, 60)[0] is True   # no state: push now
    assert dp.push_decision("A", st, 1000.0 + 5 * minute, 60) == (False, "unchanged within the heartbeat interval")
    assert dp.push_decision("B", st, 1000.0 + 5 * minute, 60) == (True, "content changed")
    assert dp.push_decision("A", st, 1000.0 + 59 * minute, 60)[0] is False
    assert dp.push_decision("A", st, 1000.0 + 60 * minute, 60)[0] is True                              # heartbeat due exactly at the interval
    assert dp.push_decision("A", st, 1000.0 + 61 * minute, 60)[0] is True
    assert dp.push_decision("A", st, 500.0, 60)[0] is True                                             # clock went backwards: don't stall
    assert dp.push_decision(None, st, 1000.0 + 5 * minute, 60)[0] is False                            # unreadable export is not "changed"
    assert dp.push_decision(None, st, 1000.0 + 61 * minute, 60)[0] is True                             # ...but the heartbeat still pushes it


def test_refresh_interval_from_env_picks_up_a_value_loaded_after_import(monkeypatch):
    monkeypatch.setattr(dp, "PUSH_MIN_INTERVAL_MINUTES", 60.0)  # restored afterwards
    monkeypatch.setenv(dp.PUSH_MINUTES_ENV, "30")  # e.g. from .env, which load_config() loads after the import
    assert dp.refresh_interval_from_env() == 30.0 and dp.PUSH_MIN_INTERVAL_MINUTES == 30.0
    monkeypatch.setenv(dp.PUSH_MINUTES_ENV, "")  # a blank line in .env.example's style
    assert dp.refresh_interval_from_env() == 60.0
