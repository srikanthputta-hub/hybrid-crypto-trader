"""web/display.js, the redesigned page's pure layer, run under node: escaping, number formatting, freshness thresholds,
the bar-decision status, open P/L and drawdown, the kill rule at its 20/30/40/50-trade checkpoints, the KPI row, the
status strip / health rows / alert priority, the instance manifest, and the inline-SVG charts. Every number the page
shows comes from here, so this is where the "never invent a number, say no data" rule is pinned.

(The older helpers - banner text, "prices as of", P/L default tab - are tested in tests/test_dashboard_display.py;
the page itself, in a real browser, in tests/test_dashboard_page.py.) Needs `node`; skipped without it.
"""
from __future__ import annotations

import json

import pytest

from tests.test_dashboard_display import REPO, needs_node, node_json


def display_js(body: str):
    """Run `body` (JS, with `d` = web/display.js's exports and `NOW` = 2026-11-21T21:10Z in ms) under node, return its JSON."""
    return node_json(f"""
      const d = require({json.dumps(str(REPO / 'web' / 'display.js'))});
      const NOW = Date.parse("2026-11-21T21:10:00Z");
      const out = (() => {{ {body} }})();
      console.log(JSON.stringify(out));
    """)


@needs_node
def test_html_escapes_everything_unless_marked_raw():
    got = display_js(r'''
      const evil = '<img src=x onerror=alert(1)>"\'&';
      return {
        plain: d.html`<b>${evil}</b>`.__raw,
        attr: d.html`<td data-label="${evil}">x</td>`.__raw,
        raw: d.html`<i>${d.raw("<u>ok</u>")}</i>`.__raw,
        nested: d.html`${[d.html`<li>${evil}</li>`, "<b>", d.raw("<hr>")]}`.__raw,
        nul: d.html`[${null}][${undefined}][${0}][${false}]`.__raw,
        esc: d.escapeHtml(5) + d.escapeHtml(null),
      };
    ''')
    assert got["plain"] == "<b>&lt;img src=x onerror=alert(1)&gt;&quot;&#39;&amp;</b>"
    assert "<img" not in got["attr"] and "&quot;" in got["attr"] and got["attr"].count('"') == 2
    assert got["raw"] == "<i><u>ok</u></i>"
    assert got["nested"] == "<li>&lt;img src=x onerror=alert(1)&gt;&quot;&#39;&amp;</li>&lt;b&gt;<hr>"
    assert got["nul"] == "[][][0][false]" and got["esc"] == "5"


@needs_node
def test_number_formatting():
    got = display_js(r'''
      return {
        money: [d.fmtMoney(1234.5), d.fmtMoney(-1234.5), d.fmtMoney(0), d.fmtMoney(-0), d.fmtMoney(12.3, true), d.fmtMoney(-12.3, true), d.fmtMoney(null), d.fmtMoney(NaN), d.fmtMoney("5")],
        pct: [d.fmtPct(0.0123), d.fmtPct(-0.5, 0), d.fmtPct(0.1, 1, true), d.fmtPct(0, 2, true), d.fmtPct(undefined), d.fmtPct(Infinity)],
        r: [d.fmtR(0.1234), d.fmtR(-0.574), d.fmtR(-0.004), d.fmtR(0), d.fmtR(null), d.fmtR(2, 1)],
        price: [d.fmtPrice(1234.5678901), d.fmtPrice(0.0000245), d.fmtPrice(0), d.fmtPrice(null), d.fmtPrice(98000), d.fmtPrice(0.5)],
        sign: [d.signClass(1), d.signClass(-1), d.signClass(0), d.signClass(null)],
        short: [d.fmtUtcShort("2026-10-17T00:05:09Z"), d.fmtUtcShort("2026-10-17T02:00:00+02:00"), d.fmtUtcShort(null), d.fmtUtcShort("x")],
      };
    ''')
    assert got["money"] == ["$1,234.50", "-$1,234.50", "$0.00", "$0.00", "+$12.30", "-$12.30", "—", "—", "—"]
    assert got["pct"] == ["1.23%", "-50%", "+10.0%", "0.00%", "—", "—"]
    assert got["r"] == ["+0.12R", "-0.57R", "0.00R", "0.00R", "—", "+2.0R"]
    assert got["price"] == ["1,234.56789", "0.00002450", "0", "—", "98,000", "0.5000"]
    assert got["sign"] == ["pos", "neg", "", ""]
    assert got["short"] == ["Oct 17 00:05", "Oct 17 00:00", "—", "—"]


@needs_node
def test_freshness_thresholds_amber_after_15_red_after_60():
    got = display_js(r'''
      const lv = (m) => d.freshness(m).level;
      const iso = (minAgo) => new Date(NOW - minAgo * 60000).toISOString();
      return {
        levels: [0, 14.99, 15, 15.01, 59.99, 60, 60.01, 600, null, NaN, undefined].map(lv),
        age: [d.ageMinutes(iso(3), NOW), d.ageMinutes(iso(-5), NOW), d.ageMinutes(null, NOW), d.ageMinutes("junk", NOW), d.ageMinutes(iso(3), null)],
        text: [0, 0.9, 1, 59.9, 60, 65, 119, 1440, 1500, 3000, 2880, null].map(d.ageText),
        skew: [d.clockSkewMs("Sat, 21 Nov 2026 21:10:00 GMT", NOW - 3 * 3600e3), d.clockSkewMs("garbage", NOW), d.clockSkewMs(null, NOW)],
        consts: [d.FRESH_WARN_MIN, d.FRESH_STALE_MIN],
      };
    ''')
    assert got["levels"] == ["ok", "ok", "ok", "warn", "warn", "warn", "stale", "stale", "unknown", "unknown", "unknown"]
    assert got["age"] == [3, 0, None, None, None]  # a timestamp in the future is clamped to 0, never negative
    assert got["text"] == ["just now", "just now", "1 min ago", "59 min ago", "1 h ago", "1 h 5 min ago", "1 h 59 min ago", "1 d ago", "1 d 1 h ago", "2 d 2 h ago", "2 d ago", "unknown"]
    assert got["skew"] == [3 * 3600 * 1000, 0, 0] and got["consts"] == [15, 60]


@needs_node
def test_freshness_thresholds_follow_the_push_interval():
    got = display_js(r'''
      const withInterval = (v) => ({ops: {health: {push_interval_minutes: v}}});
      const th = (data) => { const t = d.freshThresholds(data); return [t.warn, t.stale, t.interval]; };
      const lv = (min, data) => d.freshness(min, d.freshThresholds(data)).level;
      const iso = (minAgo) => new Date(NOW - minAgo * 60000).toISOString();
      const base = (v, minAgo) => ({generated_at: iso(minAgo), ops: {health: {push_interval_minutes: v, last_event_at: iso(1)},
        kill_switch: {present: false}, instance: {timeframe: "1d"}}});
      return {
        thresholds: [th(withInterval(60)), th(withInterval(5)), th(withInterval(20)), th(withInterval(240)), th({}), th(null), th(withInterval(0)),
                     th(withInterval("60")), th(withInterval(-5)), th(withInterval(NaN)), th({ops: {}})],
        hourly: [0, 89, 90, 91, 179, 180, 181].map((m) => lv(m, withInterval(60))),
        legacy: [14, 16, 59, 61].map((m) => lv(m, {})),
        shortInterval: [14, 16, 59, 61].map((m) => lv(m, withInterval(5))),
        chip: [d.statusModel(base(60, 40), null, NOW).fresh.level, d.statusModel(base(60, 100), null, NOW).fresh.level, d.statusModel(base(60, 200), null, NOW).fresh.level],
        chipLegacy: d.statusModel(base(undefined, 40), null, NOW).fresh.level,
        alerts: [d.alertModel(base(60, 40), NOW, null), d.alertModel(base(60, 100), NOW, null).text, d.alertModel(base(60, 200), NOW, null).text.slice(0, 11)],
        row: d.systemHealth(base(60, 40), NOW)[0],
        rowLegacy: d.systemHealth(base(undefined, 40), NOW)[0].note,
        interval: [d.fmtInterval(60), d.fmtInterval(90), d.fmtInterval(120), d.fmtInterval(180)],
      };
    ''')
    assert got["thresholds"] == [[90, 180, 60], [15, 60, 5], [30, 60, 20], [360, 720, 240], [15, 60, None], [15, 60, None], [15, 60, None],
                                 [15, 60, None], [15, 60, None], [15, 60, None], [15, 60, None]]
    assert got["hourly"] == ["ok", "ok", "ok", "warn", "warn", "warn", "stale"]  # amber above 1.5x, red above 3x
    assert got["legacy"] == ["ok", "warn", "warn", "stale"] and got["shortInterval"] == ["ok", "warn", "warn", "stale"]
    assert got["chip"] == ["ok", "warn", "stale"] and got["chipLegacy"] == "warn"
    assert got["alerts"][0] is None and got["alerts"][1] == "The data is 100 min old; it normally refreshes at least every 60 min." and got["alerts"][2] == "STALE DATA:"
    assert got["row"]["level"] == "ok" and "at least every 60 min" in got["row"]["note"] and "every ~5 min" in got["rowLegacy"]
    assert got["interval"] == ["60 min", "90 min", "2 h", "3 h"]


@needs_node
def test_freshness_thresholds_prefer_the_deploy_interval_over_the_push_interval():
    got = display_js(r'''
      const h = (o) => ({ops: {health: o}});
      const th = (data) => { const t = d.freshThresholds(data); return [t.warn, t.stale, t.interval]; };
      return {
        both: th(h({deploy_interval_minutes: 15, push_interval_minutes: 60})),
        deployOnly: th(h({deploy_interval_minutes: 40})),
        pushOnly: th(h({push_interval_minutes: 60})),
        floors: th(h({deploy_interval_minutes: 5, push_interval_minutes: 60})),
        long: th(h({deploy_interval_minutes: 120})),
        invalidDeployFallsBack: [th(h({deploy_interval_minutes: 0, push_interval_minutes: 60})), th(h({deploy_interval_minutes: "15", push_interval_minutes: 60})),
                                 th(h({deploy_interval_minutes: null, push_interval_minutes: 60})), th(h({deploy_interval_minutes: NaN, push_interval_minutes: 30}))],
        neither: [th(h({deploy_interval_minutes: -1})), th(h({})), th({}), th(null)],
        level: [30, 100, 200].map((m) => d.freshness(m, d.freshThresholds(h({deploy_interval_minutes: 40, push_interval_minutes: 60}))).level),
      };
    ''')
    assert got["both"] == [22.5, 60, 15]  # 1.5x amber (floor 15), 3x red (floor 60): the deploy cadence, not 60 -> 90/180
    assert got["deployOnly"] == [60, 120, 40] and got["pushOnly"] == [90, 180, 60]
    assert got["floors"] == [15, 60, 5] and got["long"] == [180, 360, 120]
    assert got["invalidDeployFallsBack"] == [[90, 180, 60], [90, 180, 60], [90, 180, 60], [45, 90, 30]]
    assert got["neither"] == [[15, 60, None]] * 4
    assert got["level"] == ["ok", "warn", "stale"]  # amber above 60, red above 120


@needs_node
def test_bar_status_waiting_late_missed():
    got = display_js(r'''
      const at = (iso) => Date.parse(iso);
      const s = (last, tf, now) => d.barStatus(last, tf, at(now)).state;
      return {
        daily: [
          s("2026-11-21T00:00:00Z", "1d", "2026-11-21T20:00:00Z"),   // decided today's close
          s("2026-11-20T00:00:00Z", "1d", "2026-11-21T00:10:00Z"),   // within the 15 min grace
          s("2026-11-20T00:00:00Z", "1d", "2026-11-21T00:15:00Z"),
          s("2026-11-20T00:00:00Z", "1d", "2026-11-21T00:30:00Z"),   // late
          s("2026-11-20T00:00:00Z", "1d", "2026-11-21T01:00:00Z"),
          s("2026-11-20T00:00:00Z", "1d", "2026-11-21T01:01:00Z"),   // past the 60 min entry guard: missed
          s(null, "1d", "2026-11-21T00:01:00Z"),
        ],
        hourly: [s("2026-11-21T20:00:00Z", "1h", "2026-11-21T20:20:00Z"), s("2026-11-21T19:00:00Z", "1h", "2026-11-21T20:05:00Z"), s("2026-11-21T19:00:00Z", "1h", "2026-11-21T20:30:00Z"), s("2026-11-21T19:00:00Z", "1h", "2026-11-21T21:05:00Z"), s("2026-11-21T20:00:00Z", "1h", "2026-11-21T21:05:00Z")],
        quarter: [s("2026-11-21T20:45:00Z", "15m", "2026-11-21T20:47:00Z"), s("2026-11-21T20:30:00Z", "15m", "2026-11-21T20:47:00Z"), s("2026-11-21T20:30:00Z", "15m", "2026-11-21T21:00:00Z"), s("2026-11-21T20:45:00Z", "15m", "2026-11-21T21:09:00Z")],
        unknown: [s("2026-11-21T00:00:00Z", "1w", "2026-11-21T20:00:00Z"), s("2026-11-21T00:00:00Z", null, "2026-11-21T20:00:00Z"), s("2026-11-21T00:00:00Z", "1M", "2026-11-21T20:00:00Z")],
        expected: d.barStatus("2026-11-20T00:00:00Z", "1d", at("2026-11-21T20:00:00Z")).expectedClose,
        minutes: [d.timeframeMinutes("1d"), d.timeframeMinutes("15m"), d.timeframeMinutes("4h"), d.timeframeMinutes("0h"), d.timeframeMinutes("1w")],
      };
    ''')
    assert got["daily"] == ["current", "waiting", "waiting", "late", "late", "missed", "waiting"]
    assert got["hourly"] == ["current", "waiting", "late", "missed", "waiting"]  # a skipped whole bar is missed even though the next close is only 5 min old
    assert got["quarter"] == ["current", "waiting", "missed", "late"]  # grace is a quarter of a bar (3.75 min) at 15m
    assert got["unknown"] == ["unknown"] * 3 and got["expected"] == "2026-11-21T00:00:00.000Z"
    assert got["minutes"] == [1440, 15, 240, None, None]


@needs_node
def test_open_pnl_and_drawdown_series():
    got = display_js(r'''
      const pos = [
        {fill_price: 100, fill_amount: 2, current_price: 110},
        {fill_price: 50, fill_amount: 1, current_price: 45},
        {fill_price: 10, fill_amount: 1, current_price: null},
      ];
      const pts = [["2026-11-01T00:05:00Z", 10000, 5000], ["2026-11-02T00:05:00Z", 10500, 5000], ["2026-11-03T00:05:00Z", 9450, 5000], ["2026-11-04T00:05:00Z", 9975, 5000], ["bad", 1, 1], ["2026-11-05T00:05:00Z", "x", 1]];
      const dd = d.drawdownSeries(pts, 10000);
      const noStart = d.drawdownSeries([["2026-11-01T00:05:00Z", 8000, 1], ["2026-11-02T00:05:00Z", 9000, 1], ["2026-11-03T00:05:00Z", 4500, 1]], null);
      return {
        pnl: d.openPnl(pos), none: d.openPnl([]), bad: d.openPnl(null),
        dd: {max: dd.maxDd, cur: dd.currentDd, peak: dd.peak, n: dd.points.length, maxAt: new Date(dd.maxAt).toISOString(), first: dd.points[0].dd},
        noStart: {max: noStart.maxDd, peak: noStart.peak},
        empty: [d.drawdownSeries([], 10000), d.drawdownSeries(null, 10000), d.drawdownSeries([["junk", 1]], 1)],
        belowStart: d.drawdownSeries([["2026-11-01T00:05:00Z", 9000, 1]], 10000).currentDd,
      };
    ''')
    assert got["pnl"] == {"value": 15.0, "pct": pytest.approx(15 / 250), "cost": 250, "count": 3, "known": 2}
    assert got["none"]["count"] == 0 and got["none"]["pct"] is None and got["bad"]["count"] == 0
    assert got["dd"]["max"] == pytest.approx(0.1) and got["dd"]["cur"] == pytest.approx(0.05) and got["dd"]["peak"] == 10500
    assert got["dd"]["n"] == 4 and got["dd"]["maxAt"] == "2026-11-03T00:05:00.000Z" and got["dd"]["first"] == 0
    assert got["noStart"] == {"max": 0.5, "peak": 9000}  # peak starts at the first equity when no starting cash is known
    assert got["empty"] == [None, None, None]
    assert got["belowStart"] == pytest.approx(0.1)  # the starting cash is the first peak: a first sample under it is already a drawdown


RULE_JS = 'const RULE = {avg_r: [{n: 20, max: -0.57}, {n: 30, max: -0.43}, {n: 40, max: -0.34}, {n: 50, max: -0.30}], max_drawdown: 0.30};'


@needs_node
def test_kill_rule_not_enough_trades_is_honest_and_never_breaches():
    got = display_js(RULE_JS + r'''
      const st = (series, dd) => d.killRuleStatus(RULE, series, dd);
      const worst = Array(19).fill(-3);
      return {
        zero: st([], null), nineteen: st(worst, null), none: st(null, null),
        ddOnly: st([], d.drawdownSeries([["2026-11-01T00:05:00Z", 9000, 1]], 10000)),
      };
    ''')
    z = got["zero"]
    assert z["avgR"] == {"state": "insufficient", "need": 20, "have": 0} and z["state"] == "ok" and z["trades"] == 0
    assert [c["status"] for c in z["checkpoints"]] == ["pending"] * 4 and z["next"]["n"] == 20 and z["latest"] is None
    # 19 trades averaging -3R is terrible, but the rule is first evaluated AT 20 trades: nothing fires, and we say so
    assert got["nineteen"]["avgR"]["state"] == "insufficient" and got["nineteen"]["avgR"]["have"] == 19 and got["nineteen"]["state"] == "ok"
    assert got["none"]["avgR"] == {"state": "nodata"} and got["none"]["state"] == "nodata" and got["none"]["trades"] is None
    assert got["ddOnly"]["drawdown"]["state"] == "ok" and got["ddOnly"]["state"] == "ok"


@needs_node
def test_kill_rule_checkpoints_use_only_the_first_n_trades():
    got = display_js(RULE_JS + r'''
      const st = (series, dd) => d.killRuleStatus(RULE, series, dd);
      const mk = (n, v) => Array(n).fill(v);
      return {
        atThreshold: st(mk(20, -0.57)).checkpoints[0],               // exactly the threshold: not below it
        justBelow: st(mk(20, -0.5701)),
        ok20: st(mk(20, -0.2)),
        // 20 good trades then 10 terrible ones: the 20-trade checkpoint stays ok, the 30-trade one breaches
        later: st(mk(20, 0.1).concat(mk(10, -2.0))),
        runningMeanOnly: st(mk(25, -0.7)),
        partial: st(mk(15, -0.1).concat([null, null, null, null, null])),
        allNull: st(mk(20, null)),
        fifty: st(mk(50, -0.29)),
        fiftyBad: st(mk(50, -0.31)),
        curve: st([1, null, -1, 3]).curve,
      };
    ''')
    assert got["atThreshold"]["status"] == "ok" and got["atThreshold"]["mean"] == pytest.approx(-0.57)
    assert got["justBelow"]["avgR"]["state"] == "breach" and got["justBelow"]["state"] == "breach"
    assert got["ok20"]["avgR"]["state"] == "ok" and got["ok20"]["latest"]["n"] == 20 and got["ok20"]["next"]["n"] == 30
    later = got["later"]
    assert [c["status"] for c in later["checkpoints"]] == ["ok", "breach", "pending", "pending"] and later["state"] == "breach"
    assert later["checkpoints"][1]["mean"] == pytest.approx((20 * 0.1 - 20) / 30)
    assert got["runningMeanOnly"]["current"]["mean"] == pytest.approx(-0.7) and got["runningMeanOnly"]["state"] == "breach"
    p = got["partial"]["checkpoints"][0]
    assert p["status"] == "partial" and p["known"] == 15 and got["partial"]["avgR"]["state"] == "partial" and got["partial"]["state"] == "ok"
    assert got["allNull"]["checkpoints"][0]["status"] == "nodata" and got["allNull"]["avgR"]["state"] == "partial"
    assert got["fifty"]["state"] == "ok" and got["fifty"]["next"] is None and got["fifty"]["latest"]["n"] == 50
    assert got["fiftyBad"]["checkpoints"][3]["status"] == "breach" and got["fiftyBad"]["state"] == "breach"
    assert [c["mean"] for c in got["curve"]] == [1, 1, pytest.approx(0.0), pytest.approx(1.0)] and [c["n"] for c in got["curve"]] == [1, 2, 3, 4]


@needs_node
def test_kill_rule_drawdown_line_and_missing_rule():
    got = display_js(RULE_JS + r'''
      const dd = (peakToTrough) => d.drawdownSeries([["2026-11-01T00:05:00Z", 10000, 1], ["2026-11-02T00:05:00Z", 10000 * (1 - peakToTrough), 1], ["2026-11-03T00:05:00Z", 10000, 1]], 10000);
      const st = (x) => d.killRuleStatus(RULE, [], dd(x));
      return {
        at30: st(0.30).drawdown, over: st(0.31), under: st(0.29).drawdown,
        noRule: d.killRuleStatus(null, [1, 2], null), emptyRule: d.killRuleStatus({avg_r: []}, [], null),
        noLimit: d.killRuleStatus({avg_r: RULE.avg_r}, [], dd(0.5)).drawdown,
        unsorted: d.killRuleStatus({avg_r: [{n: 40, max: -1}, {n: 20, max: -0.5}], max_drawdown: 0.3}, Array(20).fill(-0.6), null).checkpoints.map((c) => c.n),
      };
    ''')
    assert got["at30"]["state"] == "ok"  # "exceeds 30%" means strictly more
    over = got["over"]
    assert over["state"] == "breach" and over["drawdown"]["max"] == pytest.approx(0.31)
    # it recovered to the peak afterwards, but it breached once: the trial should have stopped then, so it stays a breach
    assert over["drawdown"]["current"] == 0 and over["drawdown"]["currentBreach"] is False
    assert got["under"]["state"] == "ok"
    assert got["noRule"]["hasRule"] is False and got["noRule"]["state"] == "nodata"
    assert got["emptyRule"]["hasRule"] is False
    assert got["noLimit"]["state"] == "nodata"
    assert got["unsorted"] == [20, 40]


@needs_node
def test_decision_counts_and_manifest_and_instance_choice():
    got = display_js(r'''
      const m = d.parseManifest({default: "b", instances: [
        {id: "a", name: "Alpha", data: "data.json", kill_rule: {avg_r: [{n: 20, max: -0.5}, {n: "x"}, null], max_drawdown: 0.3, label: "L", source: 5}},
        {id: "b", name: "", data: "data_b.json"},
        {id: "a", name: "Dup", data: "other.json"},
        {id: "c", name: "Evil1", data: "../data.json"}, {id: "d", name: "Evil2", data: "http://evil.example/x.json"},
        {id: "e", name: "Evil3", data: "/etc/x.json"}, {id: "f/g", name: "BadId", data: "ok.json"}, {id: "h", name: "NotJson", data: "x.txt"},
        {id: "i", name: "Dots", data: "..json"}, null, 5,
      ]});
      const fallback = (j) => d.parseManifest(j);
      return {
        counts: d.decisionCounts([{state: "scan"}, {state: "scan"}, {state: "blocked"}, {state: "weird"}, null, {}]),
        manifest: m,
        fallbacks: [fallback(null), fallback({}), fallback({instances: []}), fallback({instances: [{id: "../x", data: "d.json"}]})].map((f) => [f.default, f.instances.length, f.instances[0].data]),
        defaultOnlyIfPresent: d.parseManifest({default: "zzz", instances: [{id: "q", data: "q.json"}]}).default,
        pick: [d.pickInstance(m, "#b", "a"), d.pickInstance(m, "", "b"), d.pickInstance(m, "#nope", "nope"), d.pickInstance(m, "#nope", "a"), d.pickInstance(m, "", null)],
        safe: ["data.json", "data_intraday.json", "a-b.c.json", "../x.json", "a/b.json", "x.json?y=1", "https://x/y.json", ".hidden.json", "", "a".repeat(80) + ".json"].map((s) => d.SAFE_DATA.test(s)),
      };
    ''')
    assert got["counts"] == {"scan": 2, "blocked": 1, "nodata": 3}
    ids = [i["id"] for i in got["manifest"]["instances"]]
    assert ids == ["a", "b"] and got["manifest"]["default"] == "b"  # duplicates, paths, schemes, odd ids, non-json and "..json" are all dropped
    a, b = got["manifest"]["instances"]
    assert a["kill_rule"] == {"label": "L", "source": None, "avg_r": [{"n": 20, "max": -0.5}], "max_drawdown": 0.3}
    assert b["name"] == "b" and b["kill_rule"] is None and b["data"] == "data_b.json"
    assert got["fallbacks"] == [["default", 1, "data.json"]] * 4 and got["defaultOnlyIfPresent"] == "q"
    assert got["pick"] == ["b", "b", "b", "a", "b"]
    assert got["safe"] == [True, True, True, False, False, False, False, False, False, False]


def ops_data() -> str:
    return json.dumps({
        "generated_at": "2026-11-21T21:07:00+00:00", "mode": "paper",
        "portfolio": {"cash": 6000, "equity": 9000, "pnl_pct": -0.1},
        "positions": [{"fill_price": 100, "fill_amount": 2, "current_price": 110, "unrealized_pnl_pct": 0.1}],
        "pnl": {"summary": {"total_trades": 10, "total_realized_pnl": -50, "win_rate": 0.4}},
        "risk": {"drawdown": {"max_drawdown_pct": 0.02}},
        "ops": {
            "schema": 1, "starting_cash": 10000, "instance": {"name": "Daily bot", "label": "PAPER TRIAL", "timeframe": "1d", "mode": "paper"},
            "recost": {"label": "Kraken T3 taker"}, "kill_switch": {"present": False},
            "trade_stats": {"n": 10, "wins": 4, "win_rate": 0.4, "avg_r_booked": -0.1, "avg_r_recosted": -0.25, "r_known": 10, "r_missing": 0},
            "equity_curve": {"points": [["2026-11-01T00:05:00Z", 10000, 5000], ["2026-11-10T00:05:00Z", 9000, 5000]]},
            "health": {"last_event_at": "2026-11-21T21:06:00+00:00", "last_bar_close": "2026-11-21T00:00:00+00:00", "expected_symbols": 16,
                       "latest_bar": {"symbols_decided": 16, "stale_skips": 0, "errors": 0}, "window": {"days": 7, "stale_skips": 0, "errors": 0, "unavailable": 0},
                       "max_entry_delay_minutes": 60},
            "parity": {"status": "PASS", "message": "ok", "file_modified": "2026-11-20T10:00:00+00:00"},
        },
    })


@needs_node
def test_kpis_from_a_full_export_and_from_an_old_one():
    got = display_js(f'''
      const full = {ops_data()};
      const old = JSON.parse(JSON.stringify(full)); delete old.ops;
      const empty = {{}};
      const fresh = {{portfolio: {{cash: 10000, equity: 10000, pnl_pct: 0}}, positions: [], pnl: {{summary: {{total_trades: 0, win_rate: null}}}}, risk: {{drawdown: {{max_drawdown_pct: null}}}}}};
      const partial = JSON.parse(JSON.stringify(full)); partial.ops.trade_stats.avg_r_recosted = null; partial.positions.push({{fill_price: 1, fill_amount: 1}});
      const missingR = JSON.parse(JSON.stringify(full)); missingR.ops.trade_stats.r_missing = 2; delete missingR.risk;
      const k = (x) => Object.fromEntries(d.buildKpis(x).map((i) => [i.id, [i.value, i.caption, i.cls]]));
      return {{ids: d.buildKpis(full).map((i) => i.id), full: k(full), old: k(old), empty: k(empty), fresh: k(fresh), partial: k(partial), missingR: k(missingR)}};
    ''')
    assert got["ids"] == ["equity", "cash", "open_pnl", "closed", "win_rate", "avg_r", "max_dd"]
    f = got["full"]
    assert f["equity"][0] == "$9,000.00" and "-10.00% since start" in f["equity"][1]
    assert f["cash"][0] == "$6,000.00" and "66.7% of equity" in f["cash"][1]
    assert f["open_pnl"] == ["+$20.00", "+10.00% unrealised across 1 position", "pos"]
    assert f["closed"][0] == "10" and "realised -$50.00" in f["closed"][1]
    assert f["win_rate"][0] == "40%" and f["win_rate"][1] == "4 of 10 closed trades made money"
    assert f["avg_r"][0] == "-0.25R" and "re-costed, Kraken T3 taker" in f["avg_r"][1] and "as booked -0.10R" in f["avg_r"][1] and f["avg_r"][2] == "neg"
    assert f["max_dd"][0] == "10.00%" and "equity samples" in f["max_dd"][1]
    # an older export (no ops): no R, drawdown falls back to the realised-only figure, and says so
    o = got["old"]
    assert o["avg_r"] == ["—", "no data", ""] and o["max_dd"][0] == "2.00%" and "realised trades only" in o["max_dd"][1]
    assert o["closed"][0] == "10" and o["win_rate"][0] == "40%"
    e = got["empty"]
    assert all(v[0] == "—" for k, v in e.items() if k != "open_pnl") and e["open_pnl"][0] == "$0.00" and e["equity"][1].startswith("cash + open")
    fr = got["fresh"]
    assert fr["win_rate"] == ["—", "no closed trades yet", ""] and fr["closed"][0] == "0" and fr["avg_r"][0] == "—" and fr["max_dd"][0] == "—"
    assert fr["open_pnl"] == ["$0.00", "no open positions", ""]
    assert got["partial"]["avg_r"][0] == "—" and got["partial"]["open_pnl"][1].startswith("unrealised, 1 of 2 positions priced")
    assert "2 trades without R" in got["missingR"]["avg_r"][1]


@needs_node
def test_status_model_health_rows_and_alert_priority():
    got = display_js(f'''
      const full = {ops_data()};
      const clone = (f) => {{ const c = JSON.parse(JSON.stringify(full)); f(c); return c; }};
      const entry = {{name: "Daily trial"}};
      const m = d.statusModel(full, entry, NOW);
      const rows = (x) => Object.fromEntries(d.systemHealth(x, NOW).map((r) => [r.key, [r.value, r.level]]));
      const alert = (x, err) => {{ const a = d.alertModel(x, NOW, err); return a && a.level; }};
      const t = (min) => new Date(NOW - min * 60000).toISOString();
      return {{
        status: m,
        rows: rows(full),
        noOps: rows(clone((c) => delete c.ops)),
        stalePlain: [alert(clone((c) => c.generated_at = t(61))), alert(clone((c) => c.generated_at = t(60))), alert(clone((c) => c.generated_at = t(16))), alert(clone((c) => c.generated_at = t(15))), alert(full)],
        botDown: d.alertModel(clone((c) => c.ops.health.last_event_at = t(90)), NOW, null).text,
        kill: d.alertModel(clone((c) => c.ops.kill_switch.present = true), NOW, null),
        fetchErr: [alert(full, "HTTP 500"), alert(null, "HTTP 404"), alert(null, null)],
        priority: alert(clone((c) => {{ c.generated_at = t(90); c.ops.kill_switch.present = true; }}), "x"),
        parityFail: rows(clone((c) => c.ops.parity.status = "FAIL")).parity,
        noParity: rows(clone((c) => c.ops.parity = null)).parity,
        staleSkip: rows(clone((c) => {{ c.ops.health.latest_bar.stale_skips = 2; c.ops.health.window.stale_skips = 3; }})).stale_skips,
        errorsRow: rows(clone((c) => {{ c.ops.health.window.errors = 1; }})).errors,
        undecided: rows(clone((c) => c.ops.health.latest_bar.symbols_decided = 14)).decided,
        lateBar: rows(clone((c) => c.ops.health.last_bar_close = "2026-11-20T00:00:00+00:00")).bar_close,
        killRow: rows(clone((c) => c.ops.kill_switch.present = true)).kill_switch,
      }};
    ''')
    s = got["status"]
    assert s["name"] == "Daily trial" and s["label"] == "PAPER TRIAL" and s["mode"] == "paper" and s["timeframe"] == "1d"
    assert s["fresh"]["level"] == "ok" and s["fresh"]["text"] == "updated 3 min ago" and s["kill"] is False
    assert s["lastBar"] == "2026-11-21T00:00:00+00:00" and s["bar"]["state"] == "current"
    r = got["rows"]
    assert r["export"] == ["3 min ago", "ok"] and r["heartbeat"] == ["4 min ago", "ok"] and r["bar_close"][1] == "ok"
    assert r["decided"] == ["16 / 16", "ok"] and r["stale_skips"] == ["0 on the last bar", "ok"] and r["errors"][1] == "ok"
    assert r["parity"] == ["PASS", "ok"] and r["kill_switch"] == ["absent", "ok"]
    assert got["noOps"] == {"export": ["3 min ago", "ok"], "ops": ["no data", "none"]}
    assert got["stalePlain"] == ["bad", "warn", "warn", None, None]
    assert "1 h 30 min" in got["botDown"]
    assert got["kill"]["level"] == "warn"
    assert got["fetchErr"] == ["warn", "bad", None] and got["priority"] == "bad"
    assert got["parityFail"] == ["FAIL", "bad"] and got["noParity"][1] == "none" and "no report" in got["noParity"][0]
    assert got["staleSkip"][1] == "bad" and got["errorsRow"][1] == "bad" and got["undecided"][1] == "warn"
    assert got["lateBar"][1] in ("warn", "bad") and got["killRow"] == ["PRESENT", "bad"]


@needs_node
def test_status_model_for_a_minimal_or_missing_export():
    got = display_js(r'''
      return [d.statusModel(null, null, NOW), d.statusModel({}, {name: "X"}, NOW), d.statusModel({generated_at: "garbage", mode: "paper"}, null, NOW)]
        .map((m) => [m.name, m.fresh.level, m.fresh.text, m.mode, m.kill, m.bar.state]);
    ''')
    assert got == [["Bot", "unknown", "update time unknown", None, None, "unknown"], ["X", "unknown", "update time unknown", None, None, "unknown"],
                   ["Bot", "unknown", "update time unknown", "paper", None, "unknown"]]


@needs_node
def test_charts_never_emit_nan_and_escape_labels():
    got = display_js(RULE_JS + r'''
      const bad = (svg) => /NaN|undefined|Infinity/.test(svg);
      const day = (n) => ["2026-11-0" + n + "T00:05:00Z", 10000 - n * 100, 1];
      const dd1 = d.drawdownSeries([day(1)], 10000), dd3 = d.drawdownSeries([day(1), day(2), day(3)], 10000), flat = d.drawdownSeries([["2026-11-01T00:05:00Z", 10000, 1], ["2026-11-02T00:05:00Z", 10000, 1]], 10000);
      const st = (series) => d.killRuleStatus(RULE, series, null);
      const svgs = {
        empty: d.lineChartSvg({width: 300, height: 150, points: [], label: '<b>"x"</b>'}),
        one: d.lineChartSvg({width: 300, height: 150, points: [{x: 5, y: 5}], xKind: "number", yFmt: (v) => "<" + v}),
        eq1: d.equityChartSvg(dd1, 10000, 320, 190), eq3: d.equityChartSvg(dd3, 10000, 320, 190), eqNone: d.equityChartSvg(null, null, 320, 190), eqFlat: d.equityChartSvg(flat, 10000, 320, 190),
        dd3: d.drawdownChartSvg(dd3, 0.3, 320, 150), ddFlat: d.drawdownChartSvg(flat, 0.3, 320, 150), ddNoLimit: d.drawdownChartSvg(dd3, null, 320, 150), ddNone: d.drawdownChartSvg(null, 0.3, 320, 150),
        k0: d.killRuleChartSvg(st([]), 320, 200), k24: d.killRuleChartSvg(st(Array.from({length: 24}, (_, i) => (i % 3 ? -1.4 : 1.8))), 320, 200), k60: d.killRuleChartSvg(st(Array(60).fill(-2)), 320, 200),
        kNullR: d.killRuleChartSvg(st([null, null, 1]), 320, 200), kTiny: d.killRuleChartSvg(st([1]), 10, 10),
        m: d.drawdownMeterSvg(300, 0.1, 0.13, 0.3), mOver: d.drawdownMeterSvg(300, 0.2, 0.45, 0.3), mNone: d.drawdownMeterSvg(300, null, null, null), mNoLimit: d.drawdownMeterSvg(300, 0.1, 0.1, null),
      };
      return {
        bad: Object.entries(svgs).filter(([, v]) => bad(v)).map(([k]) => k),
        empty: svgs.empty, one: svgs.one,
        ddLimit: svgs.dd3.includes("kill line -30%"), killThresholds: ["-0.57", "-0.43", "-0.34", "-0.30"].every((t) => svgs.k0.includes(t)),
        k0NoCurve: !svgs.k0.includes('class="ch-line'), k24Curve: (svgs.k24.match(/class="ch-line/g) || []).length,
        meterBreach: svgs.mOver.includes("dd-max breach"), meterOk: !svgs.m.includes("breach"),
        widths: [svgs.k0.match(/viewBox="0 0 (\d+) (\d+)"/).slice(1), svgs.kTiny.match(/viewBox="0 0 (\d+) (\d+)"/).slice(1)],
        ticks: [d.niceTicks(0, 10, 4), d.niceTicks(5, 5, 3).length, d.niceTicks(NaN, 1, 3), d.niceTicks(0, Infinity, 3)],
      };
    ''')
    assert got["bad"] == []
    assert "&lt;b&gt;" in got["empty"] and "<b>" not in got["empty"] and "no data" in got["empty"]
    assert "&lt;5" in got["one"] and "<5" not in got["one"]
    assert got["ddLimit"] and got["killThresholds"] and got["k0NoCurve"] and got["k24Curve"] >= 2
    assert got["meterBreach"] and got["meterOk"] and got["widths"] == [["320", "200"], ["240", "140"]]
    assert got["ticks"][0] in ([0, 2.5, 5, 7.5, 10], [0, 2, 4, 6, 8, 10]) and got["ticks"][2] == [] and got["ticks"][3] == []


@needs_node
def test_display_js_has_no_dom_clock_or_network():
    """It must stay pure: tests pass `now` in, and nothing here may touch the page, storage or the network."""
    src = (REPO / "web" / "display.js").read_text()
    for forbidden in ("document.", "fetch(", "XMLHttpRequest", "localStorage", "Date.now()", "new Date()", "eval(", "innerHTML"):
        assert forbidden not in src, forbidden
