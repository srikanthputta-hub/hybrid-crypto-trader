"""The additive `ops` block of web/data.json (scripts/export_dashboard_data.py) that the redesigned dashboard reads:
the per-symbol decision for the latest bar, health counters, per-trade R (as booked and re-costed at the trial's
cost tier), the equity-sample curve, the kill-switch file, the newest parity report, and the CLI for a second bot
instance.

Contract with the rest of the export: every pre-existing key is unchanged (tests/test_export_audit_tail.py and
tests/test_dashboard_display.py compare the whole file with `ops` set aside), a failure building `ops` only drops that
one key, and the audit log is still read newest-first through the bounded `_scan_audit` stream.
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import scripts.export_dashboard_data as new
from tests.test_export_audit_tail import (
    NOW, SYMBOLS, _fixed_datetime, _iso, _peak, _snapshot, main_raw, run_main, synthetic_events, without_ops, write_events,
)

BAR = "2026-10-16T00:00:00+00:00"
PREV_BAR = "2026-10-15T00:00:00+00:00"


def ev(symbol: str, minutes_ago: float = 5, bar: str | None = BAR, **fields) -> dict:
    out = {"symbol": symbol, "mode": "paper", "timestamp": _iso(NOW - timedelta(minutes=minutes_ago))}
    if bar is not None:
        out["bar_close"] = bar
    out.update(fields)
    return out


# --------------------------------------------------------------------------- #
# _classify_decision: the audit's own wording -> state + reason
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("event,state,fragment", [
    ({"candidates": [], "action": "none"}, "scan", "no setup"),
    ({"candidates": [{"pattern": "BREAKOUT", "strength": 0.4}, {"pattern": "PULLBACK", "strength": 0.2}], "action": "none"}, "scan", "2 candidate(s) (BREAKOUT 0.4, PULLBACK 0.2), no signal"),
    ({"action": "none", "note": "stale bar: entries skipped", "entry_delay_minutes": 61.5}, "skip", "61.5 min after the close"),
    ({"action": "none", "note": "bearish signal logged only; exits are governed by the open plan's stop/target", "signal": {"direction": "bearish"}}, "skip", "bearish"),
    ({"action": "none", "note": "S9/USD is monitor-only (not in the tradeable universe); no new plan is opened on it"}, "skip", "monitor-only"),
    ({"action": "none", "note": "blocked: daily trend not confirmed bullish (higher-timeframe filter)", "candidates": [{}]}, "blocked", "daily trend not confirmed"),
    ({"action": "none", "note": "blocked: BTC/USD already has an active plan on base asset BTC (one active plan per base asset)"}, "blocked", "one active plan per base asset"),
    ({"action": "none", "signal": {"pattern": "X"}, "plan": {}, "risk_decision": {"allowed": False, "reason": "exposure cap"}}, "blocked", "risk check said no: exposure cap"),
    ({"action": "none", "signal": {"pattern": "X"}, "advisor_verdict": {"verdict": "VETO", "reasoning": "weak"}}, "blocked", "advisor veto: weak"),
    ({"action": "none", "signal": {"pattern": "X"}}, "skip", "no plan could be built"),
    ({"action": "none", "note": "OPEN plan active for X, no trigger this cycle"}, "holding", "open position"),
    ({"action": "none", "note": "PENDING plan active for X, no trigger this cycle"}, "holding", "pending plan"),
    ({"action": "planned", "plan": {}, "execution": {"price": 101.0}}, "entered", "bought at $101.00"),
    ({"action": "planned", "plan": {}}, "holding", "waiting"),
    ({"action": "none", "execution": {"price": 5.5}, "monitor_action": {"action": "FILL"}}, "entered", "filled at $5.50"),
    ({"action": "none", "execution": {"price": 90.0}, "monitor_action": {"action": "STOP_LOSS"}}, "exited", "stop-loss hit, sold at $90.00"),
    ({"action": "none", "execution": {"price": 120.0}, "monitor_action": {"action": "TAKE_PROFIT"}}, "exited", "target reached"),
    ({"action": "none", "error": "market data unavailable this cycle"}, "nodata", "unavailable"),
    ({"action": "none", "error": "boom"}, "error", "error: boom"),
    ({"action": "none"}, "skip", "no action recorded"),
])
def test_classify_decision(event, state, fragment):
    got_state, reason = new._classify_decision(event)
    assert got_state == state and fragment in reason, (got_state, reason)


def test_classify_survives_odd_field_types():
    for event in ({"note": 5}, {"execution": "x", "monitor_action": "y"}, {"risk_decision": "no"}, {"candidates": "abc"},
                  {"advisor_verdict": 3}, {"signal": "s"}, {"candidates": [1, None]}):
        state, reason = new._classify_decision(event)
        assert isinstance(state, str) and isinstance(reason, str)
    assert new._pattern_of({"candidates": [None, {"pattern": "A", "strength": "x"}, {"pattern": "B", "strength": 0.3}]}) == "B"
    assert new._pattern_of({"signal": {"pattern": "S"}, "candidates": [{"pattern": "C"}]}) == "S"
    assert new._pattern_of({}) is None


def test_long_reasons_are_clipped():
    state, reason = new._classify_decision({"error": "x" * 5000})
    assert state == "error" and len(reason) == new.DECISION_REASON_MAX and reason.endswith("…")


# --------------------------------------------------------------------------- #
# _DecisionCollector through the real newest-first scan
# --------------------------------------------------------------------------- #
def scan(events_oldest_first: list[dict], symbols: list[str], window: timedelta = new.OPS_HEALTH_WINDOW):
    collector = new._DecisionCollector(NOW, window)
    view = new._scan_audit(iter(reversed(events_oldest_first)), NOW, collectors=(collector,))
    return view, collector, collector.rows(symbols)


def echo(symbol: str, minutes_ago: float, bar: str = BAR) -> dict:
    return ev(symbol, minutes_ago, bar, action="none", note=f"bar closing {bar} already decided for {symbol}; next entry decision on the next closed bar")


def todays_log() -> list[dict]:
    log = [ev(s, 60 * 24 + 5, PREV_BAR, candidates=[], action="none") for s in SYMBOLS[:6]]
    log += [
        ev("S0/USD", 55, candidates=[], action="none"),
        ev("S1/USD", 55, candidates=[{"pattern": "MOMENTUM", "strength": 0.31}], action="none"),
        ev("S2/USD", 55, candidates=[{}], signal={"pattern": "BREAKOUT"}, daily_trend_confirmed=False, action="none",
           note="blocked: daily trend not confirmed bullish (higher-timeframe filter)"),
        ev("S3/USD", 55, candidates=[{}], signal={"pattern": "BREAKOUT"}, plan={"pattern": "BREAKOUT"},
           risk_decision={"allowed": False, "reason": "max total exposure reached"}, action="none"),
        ev("S4/USD", 55, action="none", note="stale bar: entries skipped", entry_delay_minutes=75.0),
        ev("S5/USD", 55, action="none", note="OPEN plan active for S5/USD, no trigger this cycle"),
    ]
    for k in range(4, 0, -1):  # later cycles of the same bar: echoes for the flat symbols, a monitor line for the held one
        log += [echo(s, k * 10) for s in SYMBOLS[:5]] + [ev("S5/USD", k * 10, action="none", note="OPEN plan active for S5/USD, no trigger this cycle")]
    return log


def test_rows_are_the_deciding_event_not_the_echo_and_in_config_order():
    _, _, rows = scan(todays_log(), SYMBOLS[:6])
    assert [r["symbol"] for r in rows] == SYMBOLS[:6]
    got = {r["symbol"]: (r["state"], r["bar_close"]) for r in rows}
    assert got == {"S0/USD": ("scan", BAR), "S1/USD": ("scan", BAR), "S2/USD": ("blocked", BAR), "S3/USD": ("blocked", BAR),
                   "S4/USD": ("skip", BAR), "S5/USD": ("holding", BAR)}
    by = {r["symbol"]: r for r in rows}
    assert by["S1/USD"]["pattern"] == "MOMENTUM" and "MOMENTUM 0.31" in by["S1/USD"]["reason"]
    assert "max total exposure reached" in by["S3/USD"]["reason"] and "75.0 min" in by["S4/USD"]["reason"]
    assert all(set(r) >= {"symbol", "state", "reason", "bar_close", "timestamp", "pattern"} for r in rows)


def test_health_counts_the_latest_bar_and_the_window():
    log = todays_log() + [ev("S0/USD", 1, bar=None, action="none", error="market data unavailable this cycle"),
                          ev("S1/USD", 2, action="none", error="ValueError: boom")]
    view, collector, rows = scan(log, SYMBOLS[:6])
    health = collector.health(6)
    assert health["latest_bar"] == {"bar_close": BAR, "symbols_decided": 6, "stale_skips": 1, "errors": 1}  # S1 was decided earlier in the bar; its later exception line is counted
    assert health["window"]["stale_skips"] == 1 and health["window"]["unavailable"] == 1 and health["window"]["errors"] == 1
    assert health["window"]["events"] == len(log) and health["expected_symbols"] == 6
    by = {r["symbol"]: r for r in rows}
    # a one-cycle data outage flags the symbol but does not hide its real decision from the bar; an exception line is the newest decision
    assert by["S0/USD"]["state"] == "scan" and by["S0/USD"]["data_unavailable"] is True
    assert by["S1/USD"]["state"] == "error"
    assert view.newest["symbol"] == "S1/USD"


def test_unseen_and_unavailable_only_symbols_and_extra_held_symbols():
    log = [ev("S0/USD", 3, bar=None, action="none", error="market data unavailable this cycle"),
           ev("OLD/USD", 3, action="none", note="OPEN plan active for OLD/USD, no trigger this cycle")]
    _, _, rows = scan(log, ["S0/USD", "S1/USD"])
    assert [(r["symbol"], r["state"]) for r in rows] == [("S0/USD", "nodata"), ("S1/USD", "nodata"), ("OLD/USD", "holding")]
    assert "no audit line" in rows[1]["reason"] and rows[0]["data_unavailable"] is True and "data_unavailable" not in rows[1]


def test_the_collector_stops_at_the_window_edge_and_ignores_older_events():
    log = [ev("S0/USD", 60 * 24 * 9, "2026-10-07T00:00:00+00:00", candidates=[], action="none", note="stale bar: entries skipped"),
           ev("S1/USD", 60 * 24 * 8, "2026-10-08T00:00:00+00:00", action="none", note="stale bar: entries skipped")] + todays_log()
    _, collector, rows = scan(log, SYMBOLS[:2])
    assert collector.done and collector.stale_skips == 1 and collector.health(2)["window"]["events"] == len(todays_log())
    assert {r["symbol"]: r["state"] for r in rows if r["symbol"] in ("S0/USD", "S1/USD")} == {"S0/USD": "scan", "S1/USD": "scan"}
    # a symbol whose only deciding line is outside the window is "nodata", not a stale answer
    old_only = [ev("S0/USD", 60 * 24 * 9, "2026-10-07T00:00:00+00:00", candidates=[], action="none")] + [echo("S1/USD", 5)]
    _, _, rows = scan(old_only, ["S0/USD", "S1/USD"])
    assert [r["state"] for r in rows] == ["nodata", "nodata"]


def test_events_without_a_symbol_or_with_odd_field_types_do_not_break_the_collector():
    collector = new._DecisionCollector(NOW)
    for event in ({"timestamp": _iso(NOW)}, {"symbol": 5, "timestamp": 7}, ev("S0/USD", 1, candidates=[], action="none"),
                  {"symbol": "S1/USD", "note": ["x"], "error": ["y"], "bar_close": 3}, {"symbol": "", "timestamp": None}):
        collector.feed(event)
    rows = collector.rows(["S0/USD", "S1/USD"])
    assert rows[0]["state"] == "scan" and rows[1]["state"] == "error" and collector.events == 2


def test_collectors_never_change_the_existing_reducers_or_the_early_stop(tmp_path):
    path = tmp_path / "audit.jsonl"
    write_events(path, synthetic_events(days=60, per_day=3, advisor=True))
    plain = new._scan_audit(new._iter_audit_events_reversed(path), NOW)
    consumed = 0

    def counting(it):
        nonlocal consumed
        for e in it:
            consumed += 1
            yield e

    collector = new._DecisionCollector(NOW)
    with_collector = new._scan_audit(counting(new._iter_audit_events_reversed(path)), NOW, collectors=(collector,))
    assert with_collector.history == plain.history and with_collector.last_by_role == plain.last_by_role
    assert with_collector.newest == plain.newest
    assert consumed < sum(1 for _ in open(path))  # still stops early (history cut-off passed, every role found, window passed)
    assert collector.done and len(collector.rows(SYMBOLS)) == 16


def test_collector_memory_is_independent_of_log_size(tmp_path):
    def build(path: Path, n: int) -> None:
        base = NOW - timedelta(days=3)
        with open(path, "w") as f:
            for i in range(n):
                f.write(json.dumps(ev(SYMBOLS[i % 16], 0, BAR, action="none", note=f"bar closing {BAR} already decided for X").copy()
                                   | {"timestamp": _iso(base + timedelta(seconds=i))}) + "\n")

    small, big = tmp_path / "small.jsonl", tmp_path / "big.jsonl"
    build(small, 20_000)
    build(big, 80_000)

    def run(p: Path):
        return lambda: new._scan_audit(new._iter_audit_events_reversed(p), NOW, collectors=(new._DecisionCollector(NOW),))

    small_peak, big_peak = _peak(run(small)), _peak(run(big))
    assert big_peak < 4 * 1024 * 1024 and big_peak < small_peak * 1.5 + 1024 * 1024


# --------------------------------------------------------------------------- #
# R: as booked and re-costed
# --------------------------------------------------------------------------- #
def closed_form_r(entry, exit_, risk, fee=0.0038, slip=0.0005):
    """The harness's definition, written out independently: spend 1 unit of quote."""
    qty = (1 - fee) / (entry * (1 + slip))
    proceeds = qty * exit_ * (1 - slip) * (1 - fee)
    return (proceeds - 1.0) / (qty * risk)


def test_recosted_r_matches_the_closed_form_and_the_harness_cost_preset():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from strategy_pack_run.engine import COST_PRESETS

    preset = COST_PRESETS["kraken_T3_taker"]
    assert (new.RECOST_FEE, new.RECOST_SLIPPAGE) == (preset.fee, preset.slippage)  # the trial's judging tier
    assert new._r_recosted(101.0, 90.0, 6.0, 0.0038, 0.0005) == pytest.approx(closed_form_r(101.0, 90.0, 6.0))
    assert new._r_recosted(101.0, 90.0, 6.0, 0.0038, 0.0005) == pytest.approx(-1.9704642725356358)  # the harness's own number for this trade
    assert new._r_recosted(100.0, 112.0, 4.0, 0.0, 0.0) == pytest.approx(3.0)  # no costs: (112-100)/4
    for bad in ((0, 90, 6), (101, 0, 6), (101, 90, 0), (101, 90, -1), (101, 90, None)):
        assert new._r_recosted(*bad, 0.0038, 0.0005) is None


def test_recosted_r_agrees_with_the_harness_function_on_a_toy_path(monkeypatch):
    pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")
    import numpy as np

    root = Path(__file__).resolve().parent.parent
    monkeypatch.syspath_prepend(str(root / "strategy_pack_run"))
    monkeypatch.syspath_prepend(str(root))
    try:
        import diag_1h_edge as harness
        from strategy_pack_run.engine import COST_PRESETS
    except Exception as exc:  # noqa: BLE001 — the harness module is optional research tooling
        pytest.skip(f"harness not importable: {exc}")
    o = np.array([100, 101, 102, 90, 89.0])
    c = np.array([100.5, 101.5, 95, 89.5, 88.0])  # plan at bar 0, filled at open[1]=101, close 95 <= stop 96 -> exit at open[3]=90
    out = harness.isolated_outcomes(pd.DataFrame({"open": o, "close": c}), np.array([0]), np.array([96.0]), np.array([200.0]),
                                    np.array([102.0]), COST_PRESETS["kraken_T3_taker"])
    assert new._r_recosted(101.0, 90.0, 102.0 - 96.0, 0.0038, 0.0005) == pytest.approx(float(out["R"].iloc[0]))


def write_trades(path: Path, rows: list[tuple]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "mode", "symbol", "side", "price", "base_amount", "quote_amount"])
        for ts, symbol, side, price, base, quote in rows:
            w.writerow([ts, "paper", symbol, side, price, base, quote])


def plan_line(symbol, closed_at, reason, entry_high=101.0, stop=95.0, fill_price=101.0, status="CLOSED") -> str:
    return json.dumps({"symbol": symbol, "status": status, "plan": {"pattern": "BREAKOUT", "entry_zone": [entry_high - 1, entry_high], "stop_loss": stop},
                       "created_at": "2026-10-10T00:05:00+00:00", "filled_at": "2026-10-10T00:05:01+00:00", "fill_price": fill_price,
                       "fill_amount": 10.0, "closed_at": closed_at, "close_reason": reason, "close_price": 0})


def ts(day: int, hour: int = 0, minute: int = 5, second: int = 0) -> str:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=timezone.utc).isoformat()


def test_trade_stats_from_trades_csv_and_plan_history(tmp_path):
    trades, plans = tmp_path / "trades.csv", tmp_path / "plans.jsonl"
    write_trades(trades, [
        (ts(10), "S0/USD", "buy", 101.0, 9.99, 1010.0),
        (ts(11), "S1/USD", "buy", 50.0, 10.0, 500.5),
        (ts(12), "S0/USD", "sell", 111.0, 9.99, 1107.7),          # +97.7
        (ts(13), "S1/USD", "sell", 47.0, 10.0, 469.5),            # -31.0
        (ts(13, 1), "S2/USD", "buy", 10.0, 100.0, 1001.0),
        (ts(14), "S2/USD", "sell", 9.0, 100.0, 899.1),            # -101.9, no plan record
        (ts(14, 1), "S3/USD", "buy", 20.0, 5.0, 100.1),            # still open: not a round trip
    ])
    plans.write_text("\n".join([
        plan_line("S0/USD", ts(12, 0, 5, 0), "take_profit", 101.0, 95.0, 101.0),
        plan_line("S1/USD", ts(13, 0, 5, 1), "stop_loss", 51.0, 46.0, 50.0),
        plan_line("S3/USD", ts(14, 0, 0), "invalidated", status="CLOSED", fill_price=None),  # a discarded pending plan: never filled
        '{"symbol": "S9/USD", "status": "OPEN"}', "", "not json",
    ]) + "\n")
    out = new._trade_stats(new._load_round_trips(trades), new._closed_plan_records(plans))
    stats = out["stats"]
    assert (stats["n"], stats["wins"], stats["win_rate"], stats["r_known"], stats["r_missing"]) == (3, 1, 0.3333, 2, 1)
    # newest first; the unmatched trip keeps its P/L but has no R (never guessed)
    assert [r["symbol"] for r in out["closed_trades"]] == ["S2/USD", "S1/USD", "S0/USD"]
    s2, s1, s0 = out["closed_trades"]
    assert s2["r_booked"] is None and s2["r_recosted"] is None and s2["reason"] is None and s2["pnl"] == -101.9
    assert s0["reason"] == "take_profit" and s1["reason"] == "stop_loss"
    assert s0["r_booked"] == pytest.approx(97.7 / (9.99 * 6.0), abs=1e-4)           # P/L over qty * (entry-zone high - stop)
    assert s1["r_booked"] == pytest.approx(-31.0 / (10.0 * 5.0), abs=1e-4)
    assert s0["r_recosted"] == pytest.approx(closed_form_r(101.0, 111.0, 6.0), abs=1e-4)
    assert s1["r_recosted"] == pytest.approx(closed_form_r(50.0, 47.0, 5.0), abs=1e-4)
    assert out["r_recosted_series"] == [s0["r_recosted"], s1["r_recosted"], None]  # close order, None where unknown
    assert stats["avg_r_recosted"] == pytest.approx((s0["r_recosted"] + s1["r_recosted"]) / 2, abs=1e-4)
    assert stats["avg_r_booked"] == pytest.approx((s0["r_booked"] + s1["r_booked"]) / 2, abs=1e-4)
    assert s0["pnl_pct"] == pytest.approx(97.7 / 1010.0, abs=1e-4)


def test_trade_stats_with_nothing_closed_has_no_averages(tmp_path):
    out = new._trade_stats(new._load_round_trips(tmp_path / "missing.csv"), new._closed_plan_records(tmp_path / "missing.jsonl"))
    assert out["stats"] == {"n": 0, "wins": 0, "win_rate": None, "avg_r_booked": None, "avg_r_recosted": None, "r_known": 0, "r_missing": 0}
    assert out["closed_trades"] == [] and out["r_recosted_series"] == []


def test_a_plan_record_is_used_once_and_must_be_close_in_time(tmp_path):
    trades, plans = tmp_path / "trades.csv", tmp_path / "plans.jsonl"
    write_trades(trades, [
        (ts(10), "S0/USD", "buy", 101.0, 10.0, 1010.0), (ts(11, 0, 5, 0), "S0/USD", "sell", 111.0, 10.0, 1100.0),
        (ts(11, 1), "S0/USD", "buy", 101.0, 10.0, 1010.0), (ts(12, 0, 5, 0), "S0/USD", "sell", 80.0, 10.0, 800.0),
    ])
    plans.write_text(plan_line("S0/USD", ts(11, 0, 5, 1), "take_profit") + "\n" + plan_line("S0/USD", ts(12, 3, 0, 0), "stop_loss") + "\n")
    rows = new._trade_stats(new._load_round_trips(trades), new._closed_plan_records(plans))["closed_trades"]
    assert rows[1]["reason"] == "take_profit" and rows[0]["reason"] is None  # the second record is 3 h away: no match


def test_malformed_trade_rows_and_plan_fields_are_skipped(tmp_path):
    trades = tmp_path / "trades.csv"
    trades.write_text("timestamp,mode,symbol,side,price,base_amount,quote_amount\n"
                      "garbage,paper,S0/USD,buy,1,1,1\n"
                      f"{ts(10)},paper,S0/USD,buy,x,1,1\n"
                      f"{ts(10)},paper,S0/USD,buy,100,1,100\n"
                      f"{ts(11)},paper,S0/USD,sell,110,1,109\n")
    assert [t["symbol"] for t in new._load_round_trips(trades)] == ["S0/USD"]
    plans = tmp_path / "plans.jsonl"
    plans.write_text(json.dumps({"symbol": "S0/USD", "status": "CLOSED", "fill_price": 1, "closed_at": 5, "plan": {"entry_zone": "x", "stop_loss": "y"}}) + "\n")
    assert new._closed_plan_records(plans)[0]["closed_at"] is None and new._closed_plan_records(plans)[0]["risk"] is None


# --------------------------------------------------------------------------- #
# the equity sample log
# --------------------------------------------------------------------------- #
T0 = NOW - timedelta(days=3)


def test_first_run_seeds_the_curve_with_the_starting_cash_at_the_first_audit_line(tmp_path):
    path = tmp_path / "eq.jsonl"
    curve = new._equity_curve(path, NOW, 10_000.0, 10_000.0, T0, 10_000.0)
    assert [p[1] for p in curve["points"]] == [10_000.0, 10_000.0] and curve["since"] == T0.isoformat()
    assert curve["points"][-1][0] == NOW.isoformat()
    stored = [json.loads(l) for l in path.read_text().splitlines()]
    assert stored[0] == {"t": T0.isoformat(), "equity": 10_000.0, "cash": 10_000.0, "seed": True} and len(stored) == 2


def test_samples_are_stored_when_equity_changes_or_after_the_gap_not_every_run(tmp_path):
    path = tmp_path / "eq.jsonl"
    new._equity_curve(path, NOW, 10_000.0, 10_000.0, T0, 10_000.0)
    lines = lambda: len(path.read_text().splitlines())  # noqa: E731
    assert lines() == 2
    same_again = new._equity_curve(path, NOW + timedelta(minutes=5), 10_000.0, 10_000.0, T0, 10_000.0)
    assert lines() == 2  # nothing changed, 5 minutes later: not stored...
    assert same_again["points"][-1] == [(NOW + timedelta(minutes=5)).isoformat(), 10_000.0, 10_000.0]  # ...but the curve still ends at "now"
    assert len(same_again["points"]) == 3
    new._equity_curve(path, NOW + timedelta(minutes=10), 10_050.25, 9_000.0, T0, 10_000.0)
    assert lines() == 3  # equity moved
    new._equity_curve(path, NOW + timedelta(minutes=15), 10_050.25, 9_000.0, T0, 10_000.0)
    assert lines() == 3
    new._equity_curve(path, NOW + timedelta(hours=7), 10_050.25, 9_000.0, T0, 10_000.0)
    assert lines() == 4  # nothing moved for more than EQUITY_SAMPLE_MIN_GAP: a heartbeat sample
    same_run = new._equity_curve(path, NOW + timedelta(hours=7), 10_050.25, 9_000.0, T0, 10_000.0)
    assert lines() == 4 and same_run["points"][-1][0] == (NOW + timedelta(hours=7)).isoformat()
    assert len(same_run["points"]) == 4  # the same instant is not added twice


def test_samples_from_before_the_audit_log_began_belong_to_an_earlier_portfolio(tmp_path):
    path = tmp_path / "eq.jsonl"
    path.write_text(json.dumps({"t": (T0 - timedelta(days=10)).isoformat(), "equity": 7000.0, "cash": 7000.0}) + "\n")
    curve = new._equity_curve(path, NOW, 10_000.0, 10_000.0, T0, 10_000.0)
    assert [p[1] for p in curve["points"]] == [10_000.0, 10_000.0] and curve["points"][0][0] == T0.isoformat()


def test_corrupt_and_foreign_lines_in_the_sample_log_are_skipped(tmp_path):
    path = tmp_path / "eq.jsonl"
    path.write_text('{"t": "' + T0.isoformat() + '", "equity": 10000, "cash": 10000}\nnot json\n[1,2]\n{"t": 5, "equity": 1}\n{"t": "x", "equity": "NaN"}\n')
    assert [s["equity"] for s in new._read_equity_samples(path)] == [10000]


def test_an_unwritable_sample_log_only_loses_this_runs_sample(tmp_path, capsys):
    blocked = tmp_path / "eq.jsonl"
    blocked.mkdir()  # appending to a directory raises OSError
    curve = new._equity_curve(blocked, NOW, 9_000.0, 9_000.0, T0, 10_000.0)
    assert "couldn't append" in capsys.readouterr().err
    assert curve["points"][-1][1] == 9_000.0


def test_no_audit_log_means_no_seed_and_no_starting_cash_means_none(tmp_path):
    curve = new._equity_curve(tmp_path / "eq.jsonl", NOW, 10_000.0, 10_000.0, None, 10_000.0)
    assert len(curve["points"]) == 1 and curve["since"] == NOW.isoformat()
    assert new._first_event_time(tmp_path / "missing.jsonl") is None
    audit = tmp_path / "a.jsonl"
    audit.write_text('\nnot json\n{"timestamp": "2026-10-13T00:05:00+00:00"}\n{"timestamp": "2026-10-14T00:05:00+00:00"}\n')
    assert new._first_event_time(audit) == datetime(2026, 10, 13, 0, 5, tzinfo=timezone.utc)
    audit.write_text('{"x": 1}\n' * 3)
    assert new._first_event_time(audit) is None


# --------------------------------------------------------------------------- #
# the parity summary
# --------------------------------------------------------------------------- #
def test_parity_summary_is_the_newest_readable_report(tmp_path):
    assert new._load_parity_summary(tmp_path) is None
    (tmp_path / "parity_2026-10-14.json").write_text(json.dumps({"status": "PASS", "message": "old", "window": {"since": "a", "until": "b", "decided_bars": 3, "x": 1}}))
    (tmp_path / "parity_2026-10-16.json").write_text(json.dumps({
        "status": "WARN", "message": "m" * 1000, "budget": 0.1, "trial_start": "2026-10-14T00:00:00+00:00",
        "window": {"since": "2026-10-10", "until": "2026-10-16", "decided_bars": 7, "secret": "x"}, "inputs": {"audit_log": "/home/x/logs/audit_log.jsonl"}}))
    (tmp_path / "parity_2026-10-17.json").write_text("{corrupt")
    got = new._load_parity_summary(tmp_path)
    assert got["status"] == "WARN" and got["file"] == "parity_2026-10-16.json" and len(got["message"]) == new.DECISION_REASON_MAX
    assert got["window"] == {"since": "2026-10-10", "until": "2026-10-16", "decided_bars": 7} and got["budget"] == 0.1
    assert "inputs" not in got and "/home" not in json.dumps(got)  # paths from the report are never exported
    (tmp_path / "parity_2026-10-16.json").write_text(json.dumps({"no": "status"}))
    assert new._load_parity_summary(tmp_path)["file"] == "parity_2026-10-14.json"


# --------------------------------------------------------------------------- #
# main(): the whole block end to end, the CLI, and failure isolation
# --------------------------------------------------------------------------- #
def build_instance(tmp_path: Path, kill: bool = False) -> dict:
    audit = tmp_path / "audit.jsonl"
    write_events(audit, todays_log())
    _snapshot(tmp_path / "snapshot.json", "1d")
    raw = main_raw(tmp_path, audit, timeframe="1d", label="PAPER TRIAL")
    raw["market"]["symbols"] = SYMBOLS[:6]
    raw["risk"]["kill_switch_file"] = str(tmp_path / "KILL_SWITCH")
    if kill:
        (tmp_path / "KILL_SWITCH").write_text("")
    write_trades(tmp_path / "trades.csv", [
        (ts(11), "S0/USD", "buy", 101.0, 9.99, 1010.0), (ts(12), "S0/USD", "sell", 111.0, 9.99, 1107.7),
        (ts(14), "S5/USD", "buy", 20.0, 5.0, 100.1),
    ])
    (tmp_path / "plans.jsonl").write_text(plan_line("S0/USD", ts(12, 0, 5, 0), "take_profit") + "\n")
    (tmp_path / "parity_2026-10-16.json").write_text(json.dumps({"status": "PASS", "message": "ok", "window": {"since": "a", "until": "b", "decided_bars": 3}}))
    return raw


def test_main_writes_the_ops_block_and_leaves_every_other_key_alone(tmp_path, monkeypatch):
    import tests._legacy_export_dashboard_data as old

    raw = build_instance(tmp_path, kill=True)
    after = run_main(new, tmp_path, raw, monkeypatch)
    # at 1d the role cards use the wider "working" window and `display` exists (tests/test_dashboard_display.py);
    # every other key is what the legacy script wrote, byte for byte once serialised the same way
    legacy, current = json.loads(run_main(old, tmp_path, raw, monkeypatch)), json.loads(after)
    current.pop("ops")
    for key in ("roles", "display"):
        legacy.pop(key, None), current.pop(key)
    assert current == legacy
    ops = json.loads(after)["ops"]
    assert list(json.loads(after))[-1] == "ops"  # appended last
    assert ops["schema"] == new.OPS_SCHEMA
    assert ops["instance"] == {"id": "daily", "name": "Daily bot", "label": "PAPER TRIAL", "mode": "paper", "timeframe": "1d", "exchange_id": raw["exchange"]["id"]}
    assert ops["starting_cash"] == 10_000 and ops["kill_switch"] == {"present": True, "file": "KILL_SWITCH"}
    assert ops["recost"] == {"label": new.RECOST_LABEL, "fee": 0.0038, "slippage": 0.0005}
    assert ops["trade_stats"]["n"] == 1 and ops["trade_stats"]["win_rate"] == 1.0 and ops["trade_stats"]["r_missing"] == 0
    assert len(ops["r_recosted_series"]) == 1 and len(ops["closed_trades"]) == 1
    assert [d["symbol"] for d in ops["decisions"]] == SYMBOLS[:6] and ops["decisions"][5]["state"] == "holding"
    assert ops["health"]["latest_bar"]["bar_close"] == BAR and ops["health"]["last_bar_close"] == BAR
    assert ops["health"]["last_event_at"] == _iso(NOW - timedelta(minutes=10)) and ops["health"]["poll_interval_seconds"] == 300
    assert ops["health"]["max_entry_delay_minutes"] == 60
    assert ops["parity"]["status"] == "PASS"
    points = ops["equity_curve"]["points"]
    assert points[0][1] == 10_000 and points[-1][0] == NOW.isoformat() and (tmp_path / "dashboard_equity.jsonl").exists()


def test_kill_switch_absent_is_reported_absent(tmp_path, monkeypatch):
    raw = build_instance(tmp_path, kill=False)
    assert json.loads(run_main(new, tmp_path, raw, monkeypatch))["ops"]["kill_switch"]["present"] is False


def test_a_failure_building_ops_drops_only_that_key(tmp_path, monkeypatch, capsys):
    raw = build_instance(tmp_path)
    monkeypatch.setattr(new, "_build_ops", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    data = json.loads(run_main(new, tmp_path, raw, monkeypatch))
    assert "ops" not in data and data["roles"] and data["portfolio"] and data["display"]["label"] == "PAPER TRIAL"
    assert "couldn't build the ops block" in capsys.readouterr().err


def test_ops_survives_missing_logs(tmp_path, monkeypatch):
    raw = main_raw(tmp_path, tmp_path / "nope.jsonl", timeframe="1d", label=None)
    _snapshot(tmp_path / "snapshot.json", "1d")
    ops = json.loads(run_main(new, tmp_path, raw, monkeypatch))["ops"]
    assert ops["trade_stats"]["n"] == 0 and ops["closed_trades"] == [] and ops["health"]["last_event_at"] is None
    assert ops["health"]["latest_bar"]["symbols_decided"] == 0 and all(d["state"] == "nodata" for d in ops["decisions"])
    assert ops["parity"] is None and len(ops["equity_curve"]["points"]) == 1


def test_cli_for_a_second_instance(tmp_path, monkeypatch):
    import yaml

    raw = build_instance(tmp_path)
    cfg_path = tmp_path / "intraday.yaml"
    cfg_path.write_text(yaml.safe_dump(raw))
    out = tmp_path / "out" / "data_intraday.json"
    default_out = tmp_path / "default.json"
    monkeypatch.setattr(new, "OUT_PATH", default_out)
    monkeypatch.setattr(new, "datetime", _fixed_datetime())
    new.main(["--config", str(cfg_path), "--out", str(out), "--instance-id", "intraday", "--instance-name", "Intraday"])
    ops = json.loads(out.read_text())["ops"]
    assert ops["instance"]["id"] == "intraday" and ops["instance"]["name"] == "Intraday"
    assert not default_out.exists()  # the default dashboard file was not touched
    with pytest.raises(SystemExit):
        new.main(["--no-such-option"])


def test_main_without_arguments_never_reads_sys_argv(tmp_path, monkeypatch):
    raw = build_instance(tmp_path)
    monkeypatch.setattr(sys, "argv", ["pytest", "-q", "--definitely-not-an-option"])
    assert "ops" in json.loads(run_main(new, tmp_path, raw, monkeypatch))


def test_the_realistic_bot_loop_log_exports_cleanly(tmp_path, monkeypatch):
    """The real run_cycle loop on the committed daily data (the parity-check trial generator), exported."""
    import yaml

    from tests.test_paper_parity_check import _generate_trial

    trial = _generate_trial(tmp_path / "trial", bars=12, ticks=2)
    _snapshot(tmp_path / "snapshot.json", "1d")
    raw = main_raw(tmp_path, trial.dir / "audit.jsonl", timeframe="1d", label="PAPER TRIAL")
    raw["logging"].update(trade_log_path=str(trial.dir / "trades.csv"), trade_plan_history_path=str(trial.dir / "plans.jsonl"),
                          trade_plan_current_path=str(trial.dir / "plans.json"))
    raw["market"]["symbols"] = yaml.safe_load((trial.dir / "config.yaml").read_text())["market"]["symbols"]
    raw["loop"] = {"poll_interval_seconds": 300, "bar_parity": True, "max_entry_delay_minutes": 60}
    data = json.loads(run_main(new, tmp_path, raw, monkeypatch))
    ops = data["ops"]
    assert ops["health"]["latest_bar"]["symbols_decided"] == 16 and ops["health"]["latest_bar"]["stale_skips"] == 0
    assert {d["state"] for d in ops["decisions"]} <= {"scan", "skip", "holding", "blocked", "entered", "exited"}
    stats = ops["trade_stats"]
    assert stats["n"] == data["pnl"]["summary"]["total_trades"] and stats["r_missing"] == 0
    assert len(ops["r_recosted_series"]) == stats["n"]
