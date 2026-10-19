"""The dashboard at a daily bar (docs/ops_memory_followup.md §3): scripts/export_dashboard_data.py's optional
`display` block, the timeframe-scaled "working" window for the role cards, and web/app.js + web/display.js showing
"prices as of <bar close>", a banner for notifier.label, and the P/L modal opening on Daily.

Golden contract: at the hourly default NOTHING changes.
  - export: web/data.json is byte-identical to the frozen pre-change script's (no `display` key at 1h with no label);
  - page: web/app.js + index.html, run in node against a DOM stub built from index.html's own ids, leave every element
    that existed before in exactly the state the frozen pre-change page leaves it, for the same 1h data.json.
At 1d the differences are exactly the new ones, asserted value by value.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

import scripts.export_dashboard_data as new
import tests._legacy_export_dashboard_data as old
from tests.test_export_audit_tail import NOW, SYMBOLS, _snapshot, main_raw, run_main, write_events

REPO = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
STUB = REPO / "tests" / "dashboard_stub.js"


# --------------------------------------------------------------------------- #
# export: helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("tf,expected", [("1m", 60), ("15m", 900), ("1h", 3600), ("4h", 14400), ("1d", 86400), ("1w", 604800),
                                         ("1M", 2592000), ("3d", 259200), ("d", None), ("0h", None), ("1x", None), ("", None), (None, None), (5, None)])
def test_timeframe_seconds(tf, expected):
    assert new._timeframe_seconds(tf) == expected


@pytest.mark.parametrize("tf,minutes", [("1h", 15), ("15m", 15), ("4h", 15), ("6h", 15), ("12h", 30), ("1d", 60), ("1w", 420), (None, 15), ("weird", 15)])
def test_role_window_scales_with_the_bar_and_is_unchanged_up_to_6h(tf, minutes):
    assert new._role_recency_window(tf) == timedelta(minutes=minutes)
    assert new._role_recency_window("1h") == new.RECENCY_WINDOW  # the hourly window is the old constant


def test_price_as_of_is_the_newest_closed_bars_close():
    day = 86400
    charts = {"A": {"candles": [{"time": 10 * day}, {"time": 20 * day}]}, "B": {"candles": [{"time": 19 * day}]}, "C": {"candles": []}}
    assert new._price_as_of(charts, day) == "1970-01-22T00:00:00+00:00"  # day 20's open + one bar
    assert new._price_as_of({}, day) is None and new._price_as_of(charts, None) is None


@pytest.mark.parametrize("bad", [{}, {"time": None}, {"time": "soon"}, {"time": 1e30}, {"time": [1]}, None, 5])
def test_price_as_of_survives_a_malformed_snapshot_candle(bad):
    day = 86400
    assert new._price_as_of({"A": {"candles": [{"time": day}, bad]}}, day) is None
    assert new._price_as_of({"A": {"candles": [{"time": day}]}, "B": {"candles": [bad]}}, day) is None
    block = new._display_block("1d", "PAPER TRIAL", {"A": {"candles": [bad]}})
    assert block == {"timeframe": "1d", "label": "PAPER TRIAL", "price_as_of": None, "pnl_default_period": "daily"}


def test_a_malformed_candle_does_not_stop_the_whole_export(tmp_path, monkeypatch):
    audit = tmp_path / "audit.jsonl"
    write_events(audit, recent_activity(40))
    _snapshot(tmp_path / "snapshot.json", "1d")
    snap = json.loads((tmp_path / "snapshot.json").read_text())
    snap["symbols"]["S0/USD"]["candles"][-1]["time"] = None
    (tmp_path / "snapshot.json").write_text(json.dumps(snap))
    raw = main_raw(tmp_path, audit, timeframe="1d", label="PAPER TRIAL")
    data = json.loads(run_main(new, tmp_path, raw, monkeypatch))
    assert data["display"]["price_as_of"] is None and data["display"]["label"] == "PAPER TRIAL"
    assert data["roles"] and data["portfolio"]


@pytest.mark.parametrize("tf,label,expected", [
    ("1h", None, None), ("1h", "", None), ("15m", None, None), (None, None, None),
    ("1h", "PAPER TRIAL", {"timeframe": "1h", "label": "PAPER TRIAL", "price_as_of": None, "pnl_default_period": "hourly"}),
    ("1d", None, {"timeframe": "1d", "label": None, "price_as_of": "1970-01-03T00:00:00+00:00", "pnl_default_period": "daily"}),
    ("1d", "PAPER TRIAL", {"timeframe": "1d", "label": "PAPER TRIAL", "price_as_of": "1970-01-03T00:00:00+00:00", "pnl_default_period": "daily"}),
    ("1w", None, {"timeframe": "1w", "label": None, "price_as_of": "1970-01-09T00:00:00+00:00", "pnl_default_period": "daily"}),
])
def test_display_block(tf, label, expected):
    charts = {"A": {"candles": [{"time": 86400}]}}  # one closed bar opened on 1970-01-02
    assert new._display_block(tf, label, charts) == expected


# --------------------------------------------------------------------------- #
# export: main(), old vs new
# --------------------------------------------------------------------------- #
def recent_activity(minutes_ago: int) -> list[dict]:
    ts = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    bar = NOW.replace(hour=0, minute=0).isoformat()
    return [
        {"symbol": "S0/USD", "mode": "paper", "timestamp": (NOW - timedelta(days=1, minutes=minutes_ago)).isoformat(), "bar_close": bar, "action": "none"},
        {"symbol": "S0/USD", "mode": "paper", "timestamp": ts, "bar_close": bar, "candidates": [{"pattern": "BREAKOUT", "strength": 0.7}],
         "signal": {"pattern": "BREAKOUT", "direction": "bullish"}, "plan": {"entry_zone": [100.0, 101.5], "stop_loss": 95.0},
         "risk_decision": {"allowed": True, "reason": "ok"}, "action": "planned", "execution": {"price": 101.0, "side": "buy"}},
    ]


def export_pair(tmp_path, monkeypatch, timeframe: str, label, minutes_ago: int):
    audit = tmp_path / "audit.jsonl"
    write_events(audit, recent_activity(minutes_ago))
    _snapshot(tmp_path / "snapshot.json", "1d" if timeframe == "1d" else "1h")
    raw = main_raw(tmp_path, audit, timeframe=timeframe, label=label)
    before = json.loads(run_main(old, tmp_path, raw, monkeypatch))
    raw_bytes_after = run_main(new, tmp_path, raw, monkeypatch)
    return before, json.loads(raw_bytes_after), raw_bytes_after, raw


def statuses(data):
    return {r["id"]: r["status"] for r in data["roles"]}


def test_hourly_export_is_byte_identical_and_has_no_display_key(tmp_path, monkeypatch):
    audit = tmp_path / "audit.jsonl"
    write_events(audit, recent_activity(10))
    _snapshot(tmp_path / "snapshot.json", "1h")
    raw = main_raw(tmp_path, audit, timeframe="1h", label=None)
    before = run_main(old, tmp_path, raw, monkeypatch)
    after = run_main(new, tmp_path, raw, monkeypatch)
    assert after == before
    assert "display" not in json.loads(after) and statuses(json.loads(after))["market_analyst"] == "working"


def test_hourly_export_with_a_label_differs_only_by_the_display_block(tmp_path, monkeypatch):
    before, after, _, _ = export_pair(tmp_path, monkeypatch, "1h", "PAPER TRIAL", minutes_ago=10)
    display = after.pop("display")
    assert after == before
    assert display == {"timeframe": "1h", "label": "PAPER TRIAL", "price_as_of": None, "pnl_default_period": "hourly"}


def test_hourly_roles_are_idle_40_minutes_after_activity_exactly_as_before(tmp_path, monkeypatch):
    before, after, raw_bytes, _ = export_pair(tmp_path, monkeypatch, "1h", None, minutes_ago=40)
    assert after == before and statuses(after)["market_analyst"] == "idle"


def test_daily_export_differs_from_the_legacy_in_exactly_two_places(tmp_path, monkeypatch):
    """Same audit log, 40 minutes after the decisions. The 15-minute window called every role idle; at a daily bar
    the decision pass spans the first hour after the close, so the cards read working. Nothing else moves."""
    before, after, _, _ = export_pair(tmp_path, monkeypatch, "1d", "PAPER TRIAL", minutes_ago=40)
    assert statuses(before)["market_analyst"] == "idle" and statuses(before)["boss"] == "idle"
    display = after.pop("display")
    assert display == {"timeframe": "1d", "label": "PAPER TRIAL", "pnl_default_period": "daily",
                       "price_as_of": NOW.replace(hour=0, minute=0).isoformat()}
    working = {"market_analyst", "trade_strategist", "risk_officer", "execution_trader"}
    assert {k for k, v in statuses(after).items() if v == "working"} == working | {"boss"}
    roles_before, roles_after = before.pop("roles"), after.pop("roles")
    assert after == before
    for rb, ra in zip(roles_before, roles_after):
        assert {k: v for k, v in rb.items() if k not in ("status", "detail")} == {k: v for k, v in ra.items() if k not in ("status", "detail")}
        if rb["status"] == ra["status"]:
            assert rb == ra
        else:
            assert (rb["status"], ra["status"]) == ("idle", "working")


def test_daily_export_after_the_hour_is_idle_again(tmp_path, monkeypatch):
    before, after, _, _ = export_pair(tmp_path, monkeypatch, "1d", None, minutes_ago=90)
    assert statuses(after) == statuses(before) and statuses(after)["market_analyst"] == "idle"
    assert after["display"]["label"] is None and after["display"]["pnl_default_period"] == "daily"


# --------------------------------------------------------------------------- #
# web/display.js under node
# --------------------------------------------------------------------------- #
def node_json(code: str):
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_display_js_helpers():
    got = node_json(f"""
      const d = require({json.dumps(str(REPO / 'web' / 'display.js'))});
      const daily = {{display: {{timeframe: "1d", label: "PAPER TRIAL", price_as_of: "2026-10-16T00:00:00+00:00", pnl_default_period: "daily"}}}};
      const hourlyLabel = {{display: {{timeframe: "1h", label: "PAPER TRIAL", price_as_of: null, pnl_default_period: "hourly"}}}};
      console.log(JSON.stringify({{
        none: [d.trialBannerText({{}}), d.asOfText({{}}), d.defaultPnlPeriod({{}}), d.trialBannerText(null), d.defaultPnlPeriod(undefined)],
        daily: [d.trialBannerText(daily), d.asOfText(daily), d.defaultPnlPeriod(daily)],
        hourlyLabel: [d.trialBannerText(hourlyLabel), d.asOfText(hourlyLabel), d.defaultPnlPeriod(hourlyLabel)],
        weekly: [d.asOfText({{display: {{timeframe: "1w", price_as_of: "2026-10-12T00:00:00+00:00"}}}})],
        bad: [d.defaultPnlPeriod({{display: {{pnl_default_period: "yearly"}}}}), d.fmtUtc("nope"), d.fmtUtc(null)],
        utc: d.fmtUtc("2026-10-16T00:00:00+00:00"),
        tz: d.fmtUtc("2026-10-16T02:00:00+02:00"),
      }}));
    """)
    assert got["none"] == [None, None, "hourly", None, "hourly"]
    assert got["daily"] == ["PAPER TRIAL · 1d bars", "prices as of 2026-10-16 00:00 UTC (last daily close)", "daily"]
    assert got["hourlyLabel"] == ["PAPER TRIAL · 1h bars", None, "hourly"]
    assert got["weekly"] == ["prices as of 2026-10-12 00:00 UTC (last weekly close)"]
    assert got["bad"] == ["hourly", "nope", "—"]
    assert got["utc"] == "2026-10-16 00:00 UTC" and got["tz"] == "2026-10-16 00:00 UTC"


# --------------------------------------------------------------------------- #
# the page, old vs new, in node
# --------------------------------------------------------------------------- #
NEW_IDS = {"trial-banner", "price-as-of", "overview-as-of"}


def run_page(index: Path, app: Path, display: Path | None, data_path: Path) -> dict:
    out = subprocess.run([NODE, str(STUB), str(index), str(app), str(display) if display else "-", str(data_path)],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def page_new(data_path: Path) -> dict:
    return run_page(REPO / "web" / "index.html", REPO / "web" / "app.js", REPO / "web" / "display.js", data_path)


def page_old(data_path: Path) -> dict:
    return run_page(REPO / "tests" / "_legacy_web" / "index.html", REPO / "tests" / "_legacy_web" / "app.js", None, data_path)


def data_file(tmp_path, monkeypatch, timeframe, label, pending: bool = True, minutes_ago: int = 40) -> Path:
    _, _, raw_bytes, _ = export_pair(tmp_path, monkeypatch, timeframe, label, minutes_ago=minutes_ago)
    data = json.loads(raw_bytes)
    data["symbols"] = SYMBOLS[:4]  # the symbols the snapshot has candles for
    if pending:
        data["pending"] = [{"symbol": "S0/USD", "pattern": "BREAKOUT", "entry_zone": [1.0, 2.0], "stop_loss": 0.5, "take_profit": 3.0,
                            "risk_reward": 2.0, "confidence_rule_based": 3, "timestamp": NOW.isoformat()}]
    path = tmp_path / "page_data.json"
    path.write_text(json.dumps(data))
    return path


def common_ids(a: dict, b: dict) -> list[str]:
    return sorted(i for i in (set(a) & set(b)) - NEW_IDS if not i.startswith("__"))


@needs_node
def test_hourly_page_is_identical_to_the_pre_change_page(tmp_path, monkeypatch):
    path = data_file(tmp_path, monkeypatch, "1h", None)
    before, after = page_old(path), page_new(path)
    assert len(common_ids(before, after)) > 40  # every element and the P/L tab buttons are compared
    for element_id in common_ids(before, after):
        assert after[element_id] == before[element_id], element_id
    assert after["__pnlButtons"] == before["__pnlButtons"]
    assert "annotated chart</span>" in after["chart-title"]["innerHTML"]  # the plan's chart title, no "as of" suffix
    # the new elements exist but stay hidden and empty
    for element_id in NEW_IDS:
        assert after[element_id]["hidden"] is True and after[element_id]["textContent"] == ""
    assert [b["classes"] for b in after["__pnlButtons"]] == [["active", "toggle-btn"], ["toggle-btn"], ["toggle-btn"]]


@needs_node
def test_hourly_page_with_a_label_only_adds_the_banner(tmp_path, monkeypatch):
    path = data_file(tmp_path, monkeypatch, "1h", "PAPER TRIAL")
    before, after = page_old(path), page_new(path)
    for element_id in common_ids(before, after):
        assert after[element_id] == before[element_id], element_id
    assert after["trial-banner"]["hidden"] is False and after["trial-banner"]["textContent"] == "PAPER TRIAL · 1h bars"
    assert after["price-as-of"]["hidden"] is True and after["overview-as-of"]["hidden"] is True
    assert after["__pnlButtons"] == before["__pnlButtons"]


@needs_node
def test_daily_page_shows_the_as_of_label_the_banner_and_opens_pnl_on_daily(tmp_path, monkeypatch):
    path = data_file(tmp_path, monkeypatch, "1d", "PAPER TRIAL")
    before, after = page_old(path), page_new(path)
    as_of = "prices as of 2026-10-16 00:00 UTC (last daily close)"
    assert after["trial-banner"] == dict(after["trial-banner"], hidden=False, textContent="PAPER TRIAL · 1d bars")
    for element_id in ("price-as-of", "overview-as-of"):
        assert after[element_id]["hidden"] is False and after[element_id]["textContent"] == as_of
    assert [(b["period"], "active" in b["classes"]) for b in after["__pnlButtons"]] == [("hourly", False), ("daily", True), ("weekly", False)]
    assert f"annotated chart · {as_of}</span>" in after["chart-title"]["innerHTML"]
    # everything else on the page is what the old page rendered from the same data
    differing = {i for i in common_ids(before, after) if after[i] != before[i]}
    assert differing == {"chart-title"}, differing
    assert [b["classes"] for b in before["__pnlButtons"]] == [["active", "toggle-btn"], ["toggle-btn"], ["toggle-btn"]]


@needs_node
def test_daily_page_without_a_label_has_no_banner_but_keeps_the_as_of(tmp_path, monkeypatch):
    path = data_file(tmp_path, monkeypatch, "1d", None, pending=False)
    after = page_new(path)
    assert after["trial-banner"]["hidden"] is True and after["trial-banner"]["textContent"] == ""
    assert after["price-as-of"]["hidden"] is False
    assert "annotated chart" not in after["chart-title"]["innerHTML"]  # no plan selected: the live-market title as before


@needs_node
def test_every_id_the_page_script_asks_for_exists_in_the_html():
    """The stub returns null for an unknown id, so a mismatch would have thrown above; this pins the new ids."""
    html = (REPO / "web" / "index.html").read_text()
    for element_id in NEW_IDS:
        assert f'id="{element_id}"' in html
    assert html.index('src="display.js"') < html.index('src="app.js"')
    assert "/display.js" in (REPO / "web" / "_headers").read_text()
