"""The redesigned dashboard page (web/index.html + app.js + display.js + style.css) in real Chromium.

tests/dashboard_page_check.cjs serves a copy of web/ with web/_headers applied (so the Content-Security-Policy is
really enforced), loads fixture JSON from tests/dashboard_fixture.py, drives the page, and prints what it did as one
JSON object; this file asserts on it. One browser run is shared by every test here. Skipped when node, Playwright or
the pre-installed Chromium (/opt/pw-browsers/chromium) is missing.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests import dashboard_fixture as fx

REPO = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
CHROMIUM = Path("/opt/pw-browsers/chromium")


def _playwright_available() -> bool:
    if not NODE or not CHROMIUM.exists():
        return False
    probe = subprocess.run(
        [NODE, "-e", "require(require.resolve('playwright',{paths:[process.env.NODE_PATH||'/opt/node22/lib/node_modules','.']}))"],
        capture_output=True, text=True, timeout=60)
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(not _playwright_available(), reason="node + playwright + /opt/pw-browsers/chromium are needed")


@pytest.fixture(scope="module")
def page_run(tmp_path_factory):
    work = tmp_path_factory.mktemp("dashboard_fixtures")
    fx.main(str(work))
    # what the VM's data.json looked like before the `ops` block existed (2026-10-20, day 0 of the trial: nothing traded yet)
    old = fx.build("busy")
    old.pop("ops")
    old.update(generated_at="2026-10-20T20:30:28.388610+00:00", positions=[], pending=[], signal_history=[],
               portfolio={"cash": 10000, "equity": 10000, "pnl_pct": 0.0},
               pnl={"summary": {"total_trades": 0, "total_realized_pnl": 0, "win_rate": None, "avg_win": None, "avg_loss": None, "best_trade": 0.0, "worst_trade": 0.0}, "hourly": [], "daily": [], "weekly": []},
               risk={"sample_size": 0, "min_trades_for_stats": 5, "var_cvar": {}, "drawdown": {"max_drawdown_pct": None, "current_drawdown_pct": None, "peak_equity": None, "trough_equity": None}, "significance": {}},
               display={"timeframe": "1d", "label": "PAPER TRIAL", "price_as_of": "2026-10-20T00:00:00+00:00", "pnl_default_period": "daily"})
    (work / "real.json").write_text(json.dumps(old))
    out = subprocess.run([NODE, str(REPO / "tests" / "dashboard_page_check.cjs"), str(REPO / "web"), str(work)], capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_busy_daily_trial_on_a_phone(page_run):
    b = page_run["busy"]
    assert b["name"] == "Daily trial" and b["label"] == "PAPER TRIAL · 1d bars" and b["mode"] == "mode=paper"
    assert b["fresh"] == {"text": "updated 3 min ago", "cls": "chip ok"}
    assert b["bar"] == {"text": "last bar 2026-11-21 00:00 UTC", "hidden": False}
    assert b["kill"]["text"] == "kill switch off" and b["rule"]["text"] == "kill rule: within limits"
    assert {k: v["value"] for k, v in b["kpis"].items()} == {
        "equity": "$8,782.93", "cash": "$6,100.00", "open_pnl": "+$96.14", "closed": "24", "win_rate": "33%", "avg_r": "-0.33R", "max_dd": "13.10%"}
    assert "re-costed, Kraken T3 taker" in b["kpis"]["avg_r"]["caption"]
    assert b["killPill"] == "within limits" and b["killCps"][0] == "20: -0.30R vs -0.57R · ok" and b["killCps"][1].startswith("30: pending (24/30)")
    assert b["killSvg"] and b["equitySvgPath"]
    assert len(b["decisions"]) == 16 and b["filters"][0] == "all 16" and b["posRows"] == 3 and b["pendRows"] == 2 and b["closedRows"] == 8
    assert {h["key"]: h["cls"] for h in b["health"]}["stale_skips"] == "bad"  # the fixture has one stale-bar skip
    assert b["alert"]["hidden"] is True and b["problems"] == [] and b["foreign"] == []


def test_no_horizontal_scroll_and_no_inline_script_style_or_handler(page_run):
    for key in ("busy", "desktop", "stale", "amber", "real", "evil", "multi.first"):
        snap = page_run["multi"]["first"] if key == "multi.first" else page_run[key]
        assert snap["scrollWidth"] <= snap["clientWidth"], key
        assert snap["inlineHandlers"] == 0 and snap["inlineStyles"] == 0 and snap["injected"]["scripts"] == 0, key


def test_filter_theme_toggle_and_show_all(page_run):
    b = page_run["busy"]
    assert b["afterFilter"] == ["BTC/USD:blocked", "XTZ/USD:blocked"]
    assert b["afterToggle"] == {"theme": "dark", "stored": "dark", "bodyBg": "rgb(13, 17, 23)"} and b["afterToggleBack"] == "light"
    assert b["closedRowsAll"] == 24 and b["symChart"] is True  # the vendored chart renders under the strict CSP


def test_theme_follows_the_system_and_a_stored_choice_wins(page_run):
    d = page_run["darkSystem"]
    assert d["theme"] is None and d["bodyBg"] == "rgb(13, 17, 23)"
    assert d["storedLight"] == {"bodyBg": "rgb(244, 246, 249)", "theme": "light"}


def test_desktop_layout_uses_real_tables(page_run):
    assert page_run["desktop"]["headerCells"] == 8 and page_run["desktop"]["problems"] == []


def test_freshness_chip_amber_after_15_minutes_and_red_after_60(page_run):
    amber, stale = page_run["amber"], page_run["stale"]
    assert amber["fresh"] == {"text": "updated 40 min ago", "cls": "chip warn"} and amber["alert"]["cls"] == "alert warn"
    assert stale["fresh"] == {"text": "STALE · updated 1 h 35 min ago", "cls": "chip bad"}
    assert stale["alert"]["hidden"] is False and stale["alert"]["cls"] == "alert bad" and "STALE DATA" in stale["alert"]["text"] and stale["mainStale"] is True


def test_a_wrong_viewer_clock_does_not_make_fresh_data_look_stale(page_run):
    assert page_run["skew"]["fresh"] == {"text": "updated 3 min ago", "cls": "chip ok"} and page_run["skew"]["alert"]["hidden"] is True


def test_an_older_export_without_ops_says_no_data_and_still_shows_not_enough_trades(page_run):
    r = page_run["real"]
    assert r["fresh"]["text"] == "updated 4 min ago" and r["label"] == "PAPER TRIAL · 1d bars"
    assert r["killPill"] == "0/20 trades" and "not enough trades yet (0/20)" in r["killLede"] and r["rule"]["text"] == "kill rule: 0/20 trades"
    assert r["killCps"][0] == "20: pending (0/20) · stop below -0.57R"
    assert r["kpis"]["avg_r"] == {"value": "—", "caption": "no data"} and r["kpis"]["max_dd"] == {"value": "—", "caption": "no data"}
    assert r["kpis"]["win_rate"]["caption"] == "no closed trades yet" and r["kpis"]["equity"]["value"] == "$10,000.00"
    assert r["kill"]["hidden"] is True and r["bar"]["hidden"] is True  # no kill-switch / last-bar data in this file: nothing invented
    assert r["decisions"] == [] and [h["key"] for h in r["health"]] == ["export", "ops"] and r["problems"] == []


def test_hostile_strings_are_shown_as_text_never_as_markup(page_run):
    e = page_run["evil"]
    assert e["injected"] == {"pwned": None, "imgs": 0, "scripts": 0} and e["inlineHandlers"] == 0
    assert e["html"] is False and e["text"] is True  # the literal text is on the page
    assert "<img src=x" in e["label"] and "<img src=x" in e["mode"]
    assert e["decisions"][1]["state"] == "no data"  # an unknown state degrades instead of becoming a class name
    assert e["problems"] == []


def test_two_instances_get_tabs_a_hash_and_their_own_kill_rule(page_run):
    m = page_run["multi"]
    assert m["first"]["tabs"] == [{"id": "daily", "name": "Daily trial", "selected": "true"}, {"id": "intraday", "name": "Intraday", "selected": "false"}]
    assert m["first"]["tabsHidden"] is False and m["first"]["name"] == "Daily trial"
    assert m["second"]["name"] == "Intraday" and m["second"]["tabs"][1]["selected"] == "true" and m["hash"] == "#intraday" and m["stored"] == "intraday"
    assert m["second"]["killPill"] == "no rule" and m["secondKillGridHidden"] is True  # no kill rule configured for the second instance
    assert m["second"]["label"] == "PAPER INTRADAY · 1h bars" and m["second"]["foot"].startswith("data_intraday.json")
    assert m["reload"] == "Intraday"  # remembered across a reload


def test_a_hostile_manifest_cannot_make_the_page_request_anything_else(page_run):
    b = page_run["badManifest"]
    assert b["name"] == "Dashboard" and b["tabsHidden"] is True
    assert b["requests"][-2:] == ["/instances.json", "/data.json"] and not any("evil" in r or ".." in r or "passwd" in r for r in b["requests"])


def test_a_missing_data_file_and_a_broken_manifest_degrade_to_a_clear_message(page_run):
    m = page_run["missing"]
    assert m["alert"]["hidden"] is False and "HTTP 404" in m["alert"]["text"] and m["fresh"]["text"] == "no data" and m["kpis"] == {}
