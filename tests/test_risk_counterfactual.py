"""Tests for src/risk_counterfactual.py (and its CLI wrapper).

No network. Following tests/test_exchange.py's fake-exchange pattern, a real
ExchangeClient is constructed and its ccxt instance (`.client`) is swapped for
a fake exposing only fetch_ohlcv / parse_timeframe / milliseconds. That way
ExchangeClient.fetch_ohlcv_df's real closed-candle filtering is exercised too.
The audit log is a synthetic JSONL file under tmp_path, shaped exactly like
src/bot.py writes it (plan dict first, then risk_decision dict).
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.exchange import ExchangeClient
from src.risk_counterfactual import (
    MIN_REJECTIONS_FOR_STATS,
    Bar,
    build_counterfactual_report,
    classify_reason,
    format_report,
    load_rejections,
    parse_rejection,
    replay_rejection,
)
from src.risk_engine import RiskEngine

T0 = datetime(2026, 9, 30, 0, 0, tzinfo=timezone.utc)
HOUR_MS = 3600_000

# One plan shape reused everywhere: risk per unit = entry_high - stop = 101 - 95 = 6.
PLAN = {
    "symbol": "X",
    "side": "buy",
    "pattern": "BREAKOUT",
    "entry_zone": [99.0, 101.0],
    "stop_loss": 95.0,
    "take_profit": 113.0,
    "invalidation": 93.0,
    "risk_reward": 2.0,
}
RISK = 6.0
COOLDOWN_REASON = "cooldown active for X: 0:40:00 remaining"
VOL_REASON = "market volatility too high (9.10% > max 8.00%); skipping new entries until it calms down"

# OHLC rows (open, high, low, close), one per hour starting at T0. Row 0 is the
# bar that was still FORMING when the event was logged at T0+10min. It's
# deliberately a "trap": its close is in the entry zone and its low is below
# the stop, so a replay that wrongly used it would fill/stop early.
TRAP_BAR = (100.0, 102.0, 90.0, 100.0)
ABOVE = (104.0, 105.0, 103.0, 104.0)  # entirely above the zone: no trigger
TRIGGER = (104.0, 104.0, 100.0, 100.0)  # closes in the zone -> fill on NEXT bar's open
FILL = (100.5, 106.0, 100.0, 105.0)  # fill at 100.5; range touches neither stop nor target
TP_BAR = (105.0, 114.0, 104.0, 113.0)
SL_BAR = (100.0, 100.0, 94.0, 96.0)
BOTH_BAR = (105.0, 114.0, 94.0, 100.0)  # stop AND target inside one bar's range
QUIET = (104.0, 106.0, 102.0, 104.0)

SCRIPTS = {
    "TP/USDT": [TRAP_BAR, ABOVE, TRIGGER, FILL, TP_BAR, QUIET],
    "SL/USDT": [TRAP_BAR, ABOVE, TRIGGER, FILL, SL_BAR, QUIET],
    "BOTH/USDT": [TRAP_BAR, ABOVE, TRIGGER, FILL, BOTH_BAR, QUIET],
    "NEVER/USDT": [TRAP_BAR, ABOVE, ABOVE, QUIET, ABOVE, QUIET],
}


class FakeCcxtClient:
    """Fake ccxt exchange serving scripted 1h candles per symbol (same three
    methods as tests/test_exchange.py's fake). Every scripted bar has closed
    by `now_ms`, apart from any the test deliberately leaves forming."""

    def __init__(self, scripts: dict[str, list[tuple]], now_ms: int, fail_symbols: set[str] | None = None):
        self.scripts = scripts
        self.now_ms = now_ms
        self.fail_symbols = fail_symbols or set()
        self.fetch_calls: list[str] = []

    def fetch_ohlcv(self, symbol, timeframe=None, limit=None):
        self.fetch_calls.append(symbol)
        if symbol in self.fail_symbols:
            raise RuntimeError("simulated exchange outage")
        t0_ms = int(T0.timestamp() * 1000)
        rows = [[t0_ms + i * HOUR_MS, o, h, l, c, 1.0] for i, (o, h, l, c) in enumerate(self.scripts[symbol])]
        return rows[-limit:] if limit else rows

    def parse_timeframe(self, timeframe):
        return 3600

    def milliseconds(self):
        return self.now_ms


def _exchange(scripts=SCRIPTS, now: datetime | None = None, fail_symbols=None) -> ExchangeClient:
    now = now or T0 + timedelta(days=1)
    client = ExchangeClient(exchange_id="kraken", testnet=False)
    client.client = FakeCcxtClient(scripts, int(now.timestamp() * 1000), fail_symbols)
    return client


def _event(symbol: str, reason: str, minutes_after_t0: int = 10, allowed: bool = False, plan: dict | None = None):
    plan = dict(PLAN if plan is None else plan)
    plan["symbol"] = symbol
    return {
        "symbol": symbol,
        "mode": "paper",
        "timestamp": (T0 + timedelta(minutes=minutes_after_t0)).isoformat(),
        "plan": plan,
        "risk_decision": {"allowed": allowed, "reason": reason, "max_quote_amount": None},
        "action": "none",
    }


def _write_log(path: Path, events: list) -> Path:
    with open(path, "w") as f:
        for e in events:
            f.write((e if isinstance(e, str) else json.dumps(e)) + "\n")
    return path


def _results_by_symbol(report: dict) -> dict[str, dict]:
    return {r["symbol"]: r for r in report["results"]}


# --- individual replay outcomes ---------------------------------------------


def test_take_profit_case_fills_next_bar_open_and_ignores_forming_bar(tmp_path):
    log = _write_log(tmp_path / "audit.jsonl", [_event("TP/USDT", COOLDOWN_REASON)])
    report = build_counterfactual_report(log, _exchange(), "1h", lookahead_bars=50)
    r = _results_by_symbol(report)["TP/USDT"]
    assert r["status"] == "replayed"
    assert r["would_have_entered"] is True
    assert r["outcome"] == "take_profit"
    # Filled at the open of the bar AFTER the triggering close, not at the
    # trigger bar's close (100) and not on the forming trap bar.
    assert r["fill_price"] == 100.5
    assert r["fill_bar_time"] == (T0 + timedelta(hours=3)).isoformat()
    assert r["exit_price"] == 113.0
    assert r["r_multiple"] == pytest.approx((113.0 - 100.5) / RISK, abs=1e-4)


def test_stop_loss_case(tmp_path):
    log = _write_log(tmp_path / "audit.jsonl", [_event("SL/USDT", COOLDOWN_REASON)])
    r = _results_by_symbol(build_counterfactual_report(log, _exchange(), "1h", lookahead_bars=50))["SL/USDT"]
    assert r["outcome"] == "stop_loss"
    assert r["exit_price"] == 95.0
    assert r["r_multiple"] == pytest.approx((95.0 - 100.5) / RISK, abs=1e-4)


def test_stop_and_target_in_same_bar_assumes_stop_first(tmp_path):
    log = _write_log(tmp_path / "audit.jsonl", [_event("BOTH/USDT", COOLDOWN_REASON)])
    r = _results_by_symbol(build_counterfactual_report(log, _exchange(), "1h", lookahead_bars=50))["BOTH/USDT"]
    assert r["outcome"] == "stop_loss"
    assert r["r_multiple"] < 0


def test_never_triggered_case(tmp_path):
    log = _write_log(tmp_path / "audit.jsonl", [_event("NEVER/USDT", VOL_REASON)])
    r = _results_by_symbol(build_counterfactual_report(log, _exchange(), "1h", lookahead_bars=50))["NEVER/USDT"]
    assert r["status"] == "replayed"
    assert r["would_have_entered"] is False
    assert r["not_entered_reason"] == "window_ended"
    assert r["r_multiple"] is None
    assert r["outcome"] is None


# --- pure replay_rejection edge cases (hand-built bars) ---------------------


def _rej(ts: datetime = T0 + timedelta(minutes=10)):
    return replace(parse_rejection(_event("X", COOLDOWN_REASON)), timestamp=ts)


def _bars(rows: list[tuple]) -> list[Bar]:
    return [Bar(T0 + timedelta(hours=i), *row) for i, row in enumerate(rows)]


def test_close_below_invalidation_discards_before_entry_like_monitor():
    # Wicks into the entry zone (low 99.5) but closes at 92 <= invalidation 93:
    # monitor.check_pending would DISCARD, never fill.
    bars = _bars([TRAP_BAR, (104.0, 104.0, 91.0, 92.0), FILL, TP_BAR])
    r = replay_rejection(_rej(), bars, lookahead_bars=50)
    assert r.would_have_entered is False
    assert r.not_entered_reason == "invalidated"


def test_intrabar_wick_into_zone_without_close_in_zone_does_not_enter():
    # Entry is decided on a CLOSED bar's close only, never on an intrabar touch.
    bars = _bars([TRAP_BAR, (104.0, 104.0, 99.5, 103.0), ABOVE, QUIET])
    r = replay_rejection(_rej(), bars, lookahead_bars=50)
    assert r.would_have_entered is False


def test_trigger_on_last_window_bar_cannot_fill():
    bars = _bars([TRAP_BAR, ABOVE, TRIGGER, FILL, TP_BAR])
    r = replay_rejection(_rej(), bars, lookahead_bars=2)  # window = [ABOVE, TRIGGER]
    assert r.would_have_entered is False
    assert r.not_entered_reason == "window_ended"
    assert r.provisional is False  # full lookahead window was available


def test_neither_by_window_end_marks_to_last_close():
    bars = _bars([TRAP_BAR, TRIGGER, FILL, QUIET, QUIET])
    r = replay_rejection(_rej(), bars, lookahead_bars=4)
    assert r.outcome == "neither_by_window_end"
    assert r.exit_price == QUIET[3]
    assert r.r_multiple == pytest.approx((104.0 - 100.5) / RISK, abs=1e-4)


def test_gap_through_stop_after_entry_exits_at_open_not_stop():
    bars = _bars([TRAP_BAR, TRIGGER, FILL, (92.0, 93.0, 90.0, 91.0)])
    r = replay_rejection(_rej(), bars, lookahead_bars=50)
    assert r.outcome == "stop_loss"
    assert r.exit_price == 92.0  # gapped below the 95 stop -> worse fill, not 95
    assert r.r_multiple == pytest.approx((92.0 - 100.5) / RISK, abs=1e-4)


def test_fill_already_through_stop_exits_at_bar_low_not_zero_r():
    # Trigger close in zone, but next bar opens at 94 (< stop 95). "Exit at
    # open" would equal the fill and hide the loss as R=0. Must use the low.
    bars = _bars([TRAP_BAR, TRIGGER, (94.0, 96.0, 92.0, 95.5)])
    r = replay_rejection(_rej(), bars, lookahead_bars=50)
    assert r.outcome == "stop_loss"
    assert r.fill_price == 94.0
    assert r.exit_price == 92.0
    assert r.r_multiple < 0


def test_event_older_than_fetched_history_is_skipped_not_replayed_late():
    bars = _bars([TRIGGER, FILL, TP_BAR])
    r = replay_rejection(_rej(T0 - timedelta(hours=5)), bars, lookahead_bars=50)
    assert r.status == "skipped"
    assert r.skip_reason == "event_predates_fetched_history"
    assert r.r_multiple is None


def test_event_with_no_closed_bar_after_it_is_skipped():
    bars = _bars([TRAP_BAR])
    r = replay_rejection(_rej(), bars, lookahead_bars=50)
    assert r.status == "skipped"
    assert r.skip_reason == "too_recent_no_closed_bars_after_event"


def test_non_finite_candle_in_window_skips_rather_than_computes():
    bars = _bars([TRAP_BAR, TRIGGER, (float("nan"), 106.0, 100.0, 105.0), TP_BAR])
    r = replay_rejection(_rej(), bars, lookahead_bars=50)
    assert r.status == "skipped"
    assert r.skip_reason == "invalid_candle_data"


def test_lookahead_below_two_is_refused():
    with pytest.raises(ValueError):
        replay_rejection(_rej(), _bars([TRAP_BAR, TRIGGER]), lookahead_bars=1)


def test_forming_bar_is_never_fetched_into_the_replay(tmp_path):
    # "now" is mid-way through the TP bar: ExchangeClient must drop it as still
    # forming, so the trade can't be marked take_profit on an unclosed candle.
    log = _write_log(tmp_path / "audit.jsonl", [_event("TP/USDT", COOLDOWN_REASON)])
    now = T0 + timedelta(hours=4, minutes=30)
    r = _results_by_symbol(build_counterfactual_report(log, _exchange(now=now), "1h", lookahead_bars=50))["TP/USDT"]
    assert r["outcome"] == "neither_by_window_end"
    assert r["provisional"] is True


# --- parsing ----------------------------------------------------------------


def test_only_allowed_false_events_count_as_rejections_and_bad_plans_are_excluded(tmp_path):
    bad_plan = dict(PLAN)
    bad_plan.pop("stop_loss")
    inverted = {**PLAN, "stop_loss": 102.0}  # stop above entry: impossible for a long
    events = [
        _event("A/USDT", "ok", allowed=True),
        {"symbol": "B/USDT", "timestamp": T0.isoformat(), "action": "none"},  # no risk check reached
        {"symbol": "C/USDT", "timestamp": T0.isoformat(), "risk_decision": {"allowed": None, "reason": "?"}},
        _event("D/USDT", COOLDOWN_REASON, plan=bad_plan),
        _event("E/USDT", COOLDOWN_REASON, plan=inverted),
        {**_event("F/USDT", COOLDOWN_REASON), "timestamp": "not-a-time"},
        "{not json",
        _event("G/USDT", COOLDOWN_REASON),
    ]
    parsed = load_rejections(_write_log(tmp_path / "audit.jsonl", events))
    assert parsed.raw_rejection_events == 4  # D, E, F, G
    assert parsed.malformed_rejections == 3  # D, E, F
    assert parsed.unreadable_lines == 1
    assert [r.symbol for r in parsed.rejections] == ["G/USDT"]


def test_repeated_identical_plan_is_collapsed_to_earliest(tmp_path):
    # Same setup re-rejected on every 5-minute poll must count once, not 12x.
    events = [_event("TP/USDT", COOLDOWN_REASON, minutes_after_t0=m) for m in (10, 15, 20, 25)]
    parsed = load_rejections(_write_log(tmp_path / "audit.jsonl", events))
    assert len(parsed.rejections) == 1
    assert parsed.duplicates_collapsed == 3
    assert parsed.rejections[0].timestamp == T0 + timedelta(minutes=10)


def test_missing_audit_log_degrades_without_touching_exchange(tmp_path):
    exchange = _exchange()
    report = build_counterfactual_report(tmp_path / "nope.jsonl", exchange, "1h")
    assert exchange.client.fetch_calls == []
    assert report["audit_log_found"] is False
    assert report["overall"]["rejections"] == 0
    assert report["overall"]["verdict"] == "insufficient_data"
    assert report["overall"]["stats"] == {}
    assert "No risk-engine rejections found" in format_report(report)


def test_reason_categories_match_real_risk_engine_strings(tmp_path):
    # Generate each reason from the real RiskEngine, so a wording change there
    # breaks this test instead of silently landing in "other: ...".
    def engine(**overrides):
        kwargs = dict(
            starting_cash_usd=10_000, max_position_pct=0.1, max_total_exposure_pct=0.4,
            max_loss_per_trade_pct=0.01, max_daily_loss_pct=0.05, max_volatility_pct=0.08,
            min_risk_reward=1.5, cooldown_minutes=60, kill_switch_path=tmp_path / "KILL_SWITCH",
            symbol_whitelist=["BTC/USDT"],
        )
        kwargs.update(overrides)
        return RiskEngine(**kwargs)

    base = dict(portfolio_equity=10_000, cash_available=10_000)
    reasons = {}
    reasons["symbol_whitelist"] = engine().check_new_trade("DOGE/USDT", "buy", **base).reason
    e = engine()
    e.check_new_trade("BTC/USDT", "buy", **base)
    reasons["daily_loss_limit"] = e.check_new_trade("BTC/USDT", "buy", portfolio_equity=9_000, cash_available=9_000).reason
    e = engine()
    e.record_trade("BTC/USDT")
    reasons["cooldown"] = e.check_new_trade("BTC/USDT", "buy", **base).reason
    reasons["volatility"] = engine().check_new_trade("BTC/USDT", "buy", atr_pct=0.2, **base).reason
    reasons["min_risk_reward"] = engine().check_new_trade("BTC/USDT", "buy", risk_reward=1.0, **base).reason
    reasons["no_sizing_room"] = engine().check_new_trade(
        "BTC/USDT", "buy", open_positions_value=10_000, **base
    ).reason
    (tmp_path / "KILL_SWITCH").touch()
    reasons["kill_switch"] = engine().check_new_trade("BTC/USDT", "buy", **base).reason

    for expected, reason in reasons.items():
        assert classify_reason(reason) == expected, reason
    assert classify_reason("something new") == "other: something new"
    assert classify_reason(None) == "unknown"


# --- aggregation & by-reason breakdown --------------------------------------


def _bulk_scripts():
    """5 cooldown rejections that would have entered (3 TP, 2 SL) plus 2
    volatility rejections (1 SL, 1 never entered), each on its own symbol."""
    scripts = {}
    events = []
    for i, outcome_scripts in enumerate(["TP/USDT", "TP/USDT", "TP/USDT", "SL/USDT", "SL/USDT"]):
        sym = f"CD{i}/USDT"
        scripts[sym] = SCRIPTS[outcome_scripts]
        events.append(_event(sym, COOLDOWN_REASON))
    scripts["VOL0/USDT"] = SCRIPTS["SL/USDT"]
    scripts["VOL1/USDT"] = SCRIPTS["NEVER/USDT"]
    events += [_event("VOL0/USDT", VOL_REASON), _event("VOL1/USDT", VOL_REASON)]
    return scripts, events


def test_aggregation_and_by_reason_breakdown(tmp_path):
    assert MIN_REJECTIONS_FOR_STATS == 5  # fixture below is sized against this
    scripts, events = _bulk_scripts()
    log = _write_log(tmp_path / "audit.jsonl", events)
    report = build_counterfactual_report(log, _exchange(scripts), "1h", lookahead_bars=50)

    tp_r = (113.0 - 100.5) / RISK
    sl_r = (95.0 - 100.5) / RISK

    overall = report["overall"]
    assert overall["rejections"] == 7
    assert overall["replayed"] == 7
    assert overall["skipped"] == 0
    assert overall["would_have_entered"] == 6
    assert (overall["take_profit"], overall["stop_loss"], overall["neither_by_window_end"]) == (3, 3, 0)
    assert overall["not_entered_window_ended"] == 1
    assert overall["stats"]["gross_net_r"] == pytest.approx(3 * tp_r + 3 * sl_r, abs=1e-3)
    assert overall["stats"]["win_rate_resolved"] == 0.5

    assert set(report["by_reason"]) == {"cooldown", "volatility"}
    cooldown = report["by_reason"]["cooldown"]
    assert cooldown["would_have_entered"] == 5
    assert (cooldown["take_profit"], cooldown["stop_loss"]) == (3, 2)
    assert cooldown["stats"]["gross_net_r"] == pytest.approx(3 * tp_r + 2 * sl_r, abs=1e-3)
    assert cooldown["stats"]["gross_net_r"] > 0
    assert cooldown["verdict"] == "rejections_may_have_cost_opportunity"
    assert cooldown["stats"]["win_rate_resolved"] == 0.6

    volatility = report["by_reason"]["volatility"]
    assert volatility["rejections"] == 2
    assert volatility["would_have_entered"] == 1
    assert volatility["stop_loss"] == 1
    assert volatility["not_entered_window_ended"] == 1
    assert volatility["stats"] == {}  # below MIN_REJECTIONS_FOR_STATS: no noisy number
    assert volatility["verdict"] == "insufficient_data"

    text = format_report(report)
    assert "GROSS" in text and "no fees" in text
    assert "- cooldown" in text and "- volatility" in text


def test_net_losing_rejections_get_protective_verdict(tmp_path):
    scripts = {f"S{i}/USDT": SCRIPTS["SL/USDT"] for i in range(MIN_REJECTIONS_FOR_STATS)}
    events = [_event(sym, VOL_REASON) for sym in scripts]
    report = build_counterfactual_report(_write_log(tmp_path / "a.jsonl", events), _exchange(scripts), "1h", 50)
    assert report["by_reason"]["volatility"]["verdict"] == "rejections_avoided_net_losers"
    assert report["by_reason"]["volatility"]["stats"]["gross_net_r"] < 0


def test_too_few_rejections_reports_insufficient_data(tmp_path):
    events = [_event("TP/USDT", COOLDOWN_REASON), _event("SL/USDT", COOLDOWN_REASON)]
    report = build_counterfactual_report(_write_log(tmp_path / "a.jsonl", events), _exchange(), "1h", 50)
    assert report["overall"]["would_have_entered"] == 2
    assert report["overall"]["stats"] == {}
    assert report["overall"]["verdict"] == "insufficient_data"
    assert "not enough data" in format_report(report)


def test_exchange_failure_skips_only_that_symbol(tmp_path):
    events = [_event("TP/USDT", COOLDOWN_REASON), _event("SL/USDT", COOLDOWN_REASON)]
    report = build_counterfactual_report(
        _write_log(tmp_path / "a.jsonl", events), _exchange(fail_symbols={"SL/USDT"}), "1h", 50
    )
    by_sym = _results_by_symbol(report)
    assert by_sym["TP/USDT"]["status"] == "replayed"
    assert by_sym["SL/USDT"]["status"] == "skipped"
    assert by_sym["SL/USDT"]["skip_reason"] == "exchange_fetch_failed: RuntimeError"
    assert report["overall"]["skip_reasons"] == {"exchange_fetch_failed: RuntimeError": 1}


# --- CLI --------------------------------------------------------------------


def test_cli_runs_on_missing_audit_log_without_network(tmp_path, capsys):
    from scripts.replay_risk_rejections import main

    main(["--audit-log", str(tmp_path / "missing.jsonl")])
    out = capsys.readouterr().out
    assert "DIAGNOSTIC ONLY" in out
    assert "(NOT FOUND)" in out

    main(["--audit-log", str(tmp_path / "missing.jsonl"), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["overall"]["verdict"] == "insufficient_data"
    assert data["r_multiples_are_gross"] is True


def test_cli_rejects_lookahead_below_two(tmp_path):
    from scripts.replay_risk_rejections import main

    with pytest.raises(SystemExit):
        main(["--audit-log", str(tmp_path / "x.jsonl"), "--lookahead-bars", "1"])
