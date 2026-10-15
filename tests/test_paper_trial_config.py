"""The PAPER daily-bar trial, end to end, on the SHIPPED config/config.yaml.

docs/multiday_hold_design.md §5 freezes the strategy for the trial ("any change restarts the clock") and
deploy/PAPER_TRIAL_RUNBOOK.md is how it is run. This file holds two kinds of test:

1. ``test_shipped_config_pins_the_trial_values``: the exact trial values, including every strategy
   parameter the daily evidence was produced with. Any edit to config/config.yaml that changes one of them
   (a Daily Backtester Routine promotion, a hand edit, a merge) fails here. That is intended: during the
   trial such a change needs the user's decision and restarts the trial (design §5). Update this test only
   together with that decision.

2. ``test_shipped_config_runs_the_daily_trial_end_to_end``: loads the real config/config.yaml, overrides
   ONLY file paths (logging.* and risk.kill_switch_file, asserted below) into tmp_path, builds the real
   components with ``build_components`` (the startup validation the VM runs), and drives the real
   ``run_cycle`` over the committed Kraken daily bars (strategy_pack_run/data, served bar by bar through a fake
   ccxt client, mapped from each config symbol's base: BTC/USDT gets BTC/USD's bars) for TRIAL_BARS bars, two
   cycles per bar (+1 and +6 min after the 00:00 UTC close) and one simulated restart. Then it runs the weekly
   ``scripts/paper_parity_check.py`` on the produced logs and expects PASS.

Not covered here (by design): the real exchange, the real Telegram API (messages are captured), the dashboard
export and git push of scripts/run_live.py, and wall-clock behaviour of the systemd loop.
"""
from __future__ import annotations

import copy
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
import yaml

import src.bot as bot
from scripts import paper_parity_check as ppc
from src import htf_filter, notifier
from src.config import BarParitySettings, Config, load_config, parse_bar_parity_settings, parse_notifier_settings
from tests.test_bot_universe_wiring import _Clock, _FakeDatetime
from tests.test_daily_live_parity import D_MS, DailyFakeCcxt, load_rows

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "config" / "config.yaml"

# 45 daily bars from data bar 300 (2025-08-09 .. 2025-09-22): entries, stop-losses and take-profits on the
# shipped config, with the live 200-bar history fully available from the first bar.
TRIAL_START_BAR = 300
TRIAL_BARS = 45
RESTART_AFTER = 20  # simulate a process restart between the two ticks of this bar (0-based within the trial)
LABEL = "PAPER TRIAL"
DELAY_MINUTES = 60

# Every key the e2e test may override: file locations only. Nothing that changes a decision.
PATH_KEYS = {
    "logging.audit_log_path", "logging.trade_log_path", "logging.trade_plan_current_path",
    "logging.trade_plan_history_path", "logging.market_snapshot_path", "logging.bar_decisions_path",
    "logging.closed_bars_dir", "risk.kill_switch_file",
}


def _flat(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict) and v:
            out.update(_flat(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


# --------------------------------------------------------------------------- #
# 1. The pinned trial values
# --------------------------------------------------------------------------- #
# The strategy under test (design §5 "Strategy under test": the current values, unchanged). These are the
# values the daily evidence was produced with (docs/min_order_floor_10_rebaseline.md, config at 2476986).
FROZEN_STRATEGY = {
    "indicators": {"fast_ema": 12, "slow_ema": 26, "rsi_period": 14},
    "scanner": {
        "breakout_lookback": 20, "breakout_volume_multiplier": 1.5, "pullback_bounce_lookback": 5,
        "pullback_tolerance_pct": 0.004, "momentum_lookback": 5, "momentum_return_threshold": 0.03,
        "trend_confirm_bars": 10, "consolidation_bars": 8, "consolidation_max_range_atr": 2.5,
        "reversal_swing_lookback": 10, "reversal_rsi_oversold": 30, "reversal_rsi_overbought": 70,
        "adx_trend_threshold": 25,
    },
    "signal_engine": {
        "min_strength": 0.5, "confluence_bonus": 0.08,
        "pattern_priority": ["BREAKOUT", "TREND_CONTINUATION", "MOMENTUM", "PULLBACK", "REVERSAL"],
    },
    "htf_filter": {"enabled": True},
    "trade_planner": {
        "entry_buffer_atr_mult": 0.1, "stop_atr_mult": 1.5, "invalidation_atr_mult": 0.5, "min_risk_reward": 2.0,
        "adx_trend_threshold": 25, "trend_stop_atr_bonus": 0.3,
    },
    "risk": {
        "starting_cash_usd": 10000, "max_position_pct": 0.10, "max_total_exposure_pct": 0.40,
        "max_loss_per_trade_pct": 0.01, "max_daily_loss_pct": 0.05, "max_volatility_pct": 0.08,
        "volatility_scale_floor": 0.5, "min_risk_reward": 1.5, "cooldown_minutes": 60,
        "max_new_entries_per_window": 5, "new_entry_window_minutes": 120, "min_order_quote_usd": 10,
        "kill_switch_file": "KILL_SWITCH",
    },
}
FROZEN_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "XRP/USDT", "SOL/USDT", "ADA/USDT", "DOGE/USDT", "AVAX/USDT", "LINK/USDT",
    "BNB/USDT", "LTC/USDT", "DOT/USDT", "ATOM/USDT", "ALGO/USDT", "BCH/USDT", "XTZ/USDT", "SHIB/USDT",
]


def test_shipped_config_pins_the_trial_values():
    raw = yaml.safe_load(CONFIG.read_text())

    # Paper only. The live gate itself is checked by scripts/validate_invariants.py.
    assert raw["mode"] == "paper"
    assert raw["exchange"] == {"id": "kraken", "testnet": False}
    assert raw["advisor"]["enabled"] is False  # not part of the evidence (the harness has no advisor)
    assert raw["universe"]["mode"] == "static"
    assert raw["market"]["symbols"] == FROZEN_SYMBOLS

    # The six trial switches (design §5/§6, docs/paper_parity_check.md prerequisites).
    assert raw["market"]["timeframe"] == "1d"
    assert raw["market"]["history_bars"] == 200  # >= 150 (P5, validated at startup), <= 720 (Kraken's limit)
    assert raw["loop"] == {"poll_interval_seconds": 300, "bar_parity": True, "max_entry_delay_minutes": 60}
    assert raw["logging"]["closed_bars_dir"] == "logs/daily_bars"
    assert raw["logging"]["retention_days"] == 0  # the audit log must reach back to trial day 0
    assert raw["notifier"] == {"label": LABEL}
    assert raw["logging"] == {
        "audit_log_path": "logs/audit_log.jsonl", "trade_log_path": "logs/trades.csv",
        "trade_plan_current_path": "logs/trade_plans_current.json",
        "trade_plan_history_path": "logs/trade_plans_history.jsonl",
        "market_snapshot_path": "logs/market_snapshot.json", "bar_decisions_path": "logs/bar_decisions.json",
        "closed_bars_dir": "logs/daily_bars", "retention_days": 0,
    }

    # The strategy under test, frozen for the trial.
    for section, expected in FROZEN_STRATEGY.items():
        assert raw[section] == expected, f"{section} changed: the trial's strategy is frozen (design §5)"

    # The bot's own startup parsers accept exactly these values.
    assert parse_bar_parity_settings(raw) == BarParitySettings(
        enabled=True, max_entry_delay_minutes=60.0, decisions_path="logs/bar_decisions.json",
        closed_bars_dir="logs/daily_bars",
    )
    assert parse_notifier_settings(raw).label == LABEL


def test_history_bars_floor_is_enforced_for_the_trial_config():
    raw = copy.deepcopy(yaml.safe_load(CONFIG.read_text()))
    raw["market"]["history_bars"] = 149
    with pytest.raises(ValueError, match="history_bars"):
        parse_bar_parity_settings(raw)


# --------------------------------------------------------------------------- #
# 2. End to end on the shipped config
# --------------------------------------------------------------------------- #
def _logs_listing() -> dict[str, tuple[int, int]]:
    logs = REPO / "logs"
    if not logs.exists():
        return {}
    return {str(p.relative_to(logs)): (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(logs.rglob("*")) if p.is_file()}


def _trial_raw(tmp: Path) -> dict:
    """The shipped config with ONLY its file paths moved into `tmp`."""
    raw = copy.deepcopy(load_config().raw)
    for key in ("audit_log_path", "trade_log_path", "trade_plan_current_path", "trade_plan_history_path",
                "market_snapshot_path", "bar_decisions_path", "closed_bars_dir"):
        raw["logging"][key] = str(tmp / Path(raw["logging"][key]).relative_to("logs"))
    raw["risk"]["kill_switch_file"] = str(tmp / "KILL_SWITCH")  # a stray repo-root KILL_SWITCH must not matter
    return raw


def _drive(tmp: Path, start: int, bars: int, first_tick_ms: int = 60_000, restart_after: int | None = None) -> dict:
    """Runs the real bot loop on the shipped config (paths moved into `tmp`) over `bars` committed daily bars from
    data bar `start`: two cycles per bar, at `first_tick_ms` after the 00:00 UTC close and 5 minutes later (on the
    first bar only `first_tick_ms` applies; later bars always tick at +1 / +6 min). `restart_after`: rebuild every
    component from the files between the two ticks of that bar (a systemd restart)."""
    mp = pytest.MonkeyPatch()
    messages: list[str] = []
    logs_before = _logs_listing()
    try:
        real = load_config().raw
        raw = _trial_raw(tmp)
        changed = {k for k in set(_flat(real)) | set(_flat(raw)) if _flat(real).get(k) != _flat(raw).get(k)}
        assert changed == PATH_KEYS, changed  # only paths moved

        usd_rows, axis = load_rows(start + bars + 1, gapless=False)
        by_base = {s.split("/")[0]: r for s, r in usd_rows.items()}
        rows = {s: by_base[s.split("/")[0]] for s in raw["market"]["symbols"]}
        assert len(rows) == 16

        mp.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        mp.setenv("TELEGRAM_CHAT_ID", "12345")
        mp.setattr(notifier, "send_telegram_message", lambda token, chat, text: messages.append(text))
        mp.setattr(htf_filter, "_cache", {})
        mp.setattr("src.risk_engine.datetime", _FakeDatetime)  # the risk engine's clock = the replay clock
        bot._reset_bar_parity_state()

        cfg = Config(raw=raw)
        fake = DailyFakeCcxt(rows)

        def start_bot():
            parts = bot.build_components(cfg)  # the VM's startup validation (bar parity, notifier, risk)
            parts[0].client = fake
            return parts, bot.build_market_data(cfg, parts[0])

        (exchange, risk_engine, portfolio, broker, store, audit), market_data = start_bot()

        memo: dict[int, tuple] = {}
        real_add = bot.add_indicators

        def cached(df, *args):  # speed only: the same frame object comes back for both ticks of a bar
            hit = memo.get(id(df))
            if hit is not None and hit[0] is df:
                return hit[1]
            out = real_add(df, *args)
            memo[id(df)] = (df, out)
            return out

        mp.setattr(bot, "add_indicators", cached)
        audit_path = Path(raw["logging"]["audit_log_path"])
        ticks = []  # (bar close, tick index, audit line range)
        for i, ts in enumerate(axis[start:start + bars]):
            for k in range(2):
                if i == restart_after and k == 1:
                    # systemd restart between two ticks of a bar: in-memory state gone, rebuilt from logs/
                    bot._reset_bar_parity_state()
                    (exchange, risk_engine, portfolio, broker, store, audit), market_data = start_bot()
                fake.now_ms = ts + D_MS + (first_tick_ms if i == 0 else 60_000) + k * 300_000
                _Clock.now = datetime.fromtimestamp(fake.now_ms / 1000, tz=timezone.utc)
                n0 = len(audit_path.read_text().splitlines()) if audit_path.exists() else 0
                bot.run_cycle(cfg, exchange, risk_engine, portfolio, broker, store, audit, market_data)
                n1 = len(audit_path.read_text().splitlines())
                ticks.append((pd.Timestamp(ts + D_MS, unit="ms", tz="UTC"), k, (n0, n1)))
        (tmp / "config.yaml").write_text(yaml.safe_dump(raw))
    finally:
        mp.undo()
        bot._reset_bar_parity_state()
    events = [json.loads(line) for line in audit_path.read_text().splitlines()]
    return dict(tmp=tmp, raw=raw, events=events, ticks=ticks, messages=messages, calls=list(fake.calls),
                logs_before=logs_before, logs_after=_logs_listing())


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    return _drive(tmp_path_factory.mktemp("paper_trial"), TRIAL_START_BAR, TRIAL_BARS, restart_after=RESTART_AFTER)


def test_only_paths_were_overridden_and_nothing_was_written_to_the_repo_logs(run):
    assert run["logs_after"] == run["logs_before"]
    for key in PATH_KEYS - {"risk.kill_switch_file"}:
        section, name = key.split(".")
        assert Path(run["raw"][section][name]).is_relative_to(run["tmp"])


def test_every_fetch_is_the_daily_market_data_fetch(run):
    # P5: no separate 31-bar htf fetch at 1d; every call is the cache's 1d fetch of history_bars + 1.
    assert {(tf, limit) for _sym, tf, limit in run["calls"]} == {("1d", 201)}


def test_one_decision_per_symbol_per_bar(run):
    events, ticks = run["events"], run["ticks"]
    by_key: dict[tuple, list[dict]] = {}
    for e in events:
        assert "bar_close" in e, e  # every event of a symbol with data carries its bar (bar parity is on)
        by_key.setdefault((e["bar_close"], e["symbol"]), []).append(e)
    bars = {t for t, _k, _r in ticks}
    assert len(by_key) == 16 * len(bars) == 16 * TRIAL_BARS
    for key, evs in by_key.items():
        assert len(evs) == 2, key  # one audit line per symbol per cycle, two cycles per bar
        monitor = [e for e in evs if "plan active for" in str(e.get("note")) or e.get("monitor_action")]
        echoes = [e for e in evs if ppc.ALREADY_DECIDED in str(e.get("note"))]
        entry = [e for e in evs if e not in monitor and e not in echoes]
        # A FLAT symbol: exactly one entry decision, then an "already decided" echo, or (when it was planned and
        # filled) the new position's monitor line. A symbol held at the close is monitored on every cycle
        # (idempotent within a bar) and never scanned; if it exits, the bar is already decided (echo next tick).
        assert len(entry) <= 1 and sum("candidates" in e for e in evs) <= 1, key
        assert entry or monitor, (key, evs)  # the bar was decided once, never only echoed
        exited = any((e.get("monitor_action") or {}).get("action") in ("STOP_LOSS", "TAKE_PROFIT") for e in evs)
        assert not (exited and entry), (key, evs)  # no re-entry on the exit bar (P1)
    # The second tick of every bar (including the one right after the simulated restart) scans nothing.
    for _bar, k, (n0, n1) in ticks:
        if k == 1:
            assert not any("candidates" in e for e in events[n0:n1])
            assert not any(e.get("action") == "planned" for e in events[n0:n1])


def test_ledger_and_closed_bar_archive_are_written_under_tmp(run):
    tmp, raw, ticks = run["tmp"], run["raw"], run["ticks"]
    last_close = max(t for t, _k, _r in ticks)
    ledger = json.loads(Path(raw["logging"]["bar_decisions_path"]).read_text())
    assert ledger["timeframe"] == "1d"
    assert set(ledger["bars"]) == set(FROZEN_SYMBOLS)
    assert {pd.Timestamp(v) for v in ledger["bars"].values()} == {last_close}

    bars_dir = Path(raw["logging"]["closed_bars_dir"])
    assert bars_dir.is_relative_to(tmp)
    files = sorted(p.name for p in bars_dir.iterdir())
    assert files == sorted(f"{s.replace('/', '_')}_1d.csv" for s in FROZEN_SYMBOLS)
    first_open_ms = int((min(t for t, _k, _r in ticks) - pd.Timedelta(days=1)).value // 1_000_000)
    usd_rows, _axis = load_rows(TRIAL_START_BAR + TRIAL_BARS + 1, gapless=False)
    for name in files:
        rows = list(csv.reader((bars_dir / name).open()))
        assert rows[0] == ["timestamp", "open", "high", "low", "close", "volume"]
        # first write: the (up to) 200 closed bars the first decision used -- BNB/USD only has ~110 at this
        # point of the committed data -- then exactly one new bar per daily close, no duplicates
        available = sum(r[0] <= first_open_ms for r in usd_rows[name.split("_")[0] + "/USD"])
        assert len(rows) - 1 == min(200, available) + TRIAL_BARS - 1, name
        stamps = [int(r[0]) for r in rows[1:]]
        assert stamps == sorted(set(stamps))
        assert stamps[-1] == int((last_close - pd.Timedelta(days=1)).value // 1_000_000)


def test_entries_exits_and_order_floor(run):
    events = run["events"]
    planned = [e for e in events if e.get("action") == "planned"]
    exits = [e for e in events if (e.get("monitor_action") or {}).get("action") in ("STOP_LOSS", "TAKE_PROFIT")]
    assert len(planned) >= 5 and {e["monitor_action"]["action"] for e in exits} == {"STOP_LOSS", "TAKE_PROFIT"}
    assert all(e["risk_decision"]["max_quote_amount"] >= 10 for e in planned)  # risk.min_order_quote_usd
    with open(Path(run["raw"]["logging"]["trade_log_path"]), newline="") as f:
        buys = [r for r in csv.DictReader(f) if r["side"] == "buy"]
    assert len(buys) == len(planned) and all(float(r["quote_amount"]) >= 10 for r in buys)
    # Entries are decided on the first tick after the close (+1 min), well inside the 60-min guard, so no
    # bar of this in-window run is skipped as stale. P5: the htf gate ran on the scan frame.
    assert not any(ppc.STALE_NOTE in str(e.get("note")) for e in events)
    first_ticks = {t: r for t, k, r in run["ticks"] if k == 0}
    for e in planned:
        n0, n1 = first_ticks[pd.Timestamp(e["bar_close"])]
        assert any(x is e for x in events[n0:n1])
    assert all(e.get("daily_trend_source") == "scan_frame" for e in planned)


def test_telegram_messages_carry_the_label_and_the_validity_lines(run):
    msgs, events = run["messages"], run["events"]
    assert msgs and all(m.startswith(f"*{LABEL}* · ") for m in msgs)
    buys = [m for m in msgs if "BUY signal" in m]
    planned = [e for e in events if e.get("action") == "planned"]
    assert len(buys) == len(planned) >= 5
    for event, msg in zip(planned, buys):
        close = pd.Timestamp(event["bar_close"])
        assert f"Bar: {(close - pd.Timedelta(days=1)):%Y-%m-%d} (daily, closed {close:%Y-%m-%d %H:%M} UTC)" in msg
        assert f"Valid until: {(close + pd.Timedelta(minutes=DELAY_MINUTES)):%Y-%m-%d %H:%M} UTC" in msg
    assert any("SELL" in m for m in msgs) and any("Bought" in m for m in msgs)
    assert not any("Valid until" in m for m in msgs if "BUY signal" not in m)


def test_weekly_parity_check_passes_on_the_produced_logs(run, capsys):
    tmp, raw = run["tmp"], run["raw"]
    argv = ["--config", str(tmp / "config.yaml")]  # every path comes from the (tmp) config, as on the VM
    full = ppc.build_report(ppc.parse_args(argv + ["--days", str(TRIAL_BARS)]))
    assert full["status"] == ppc.PASS, full["message"] + json.dumps(ppc._clean(full["mismatches"]), indent=1)[:2000]
    assert full["trial_start"] == pd.Timestamp(run["ticks"][0][0]).isoformat()
    assert full["entries"]["harness"] == full["entries"]["bot"] == full["entries"]["matched"] >= 5
    assert full["exits"]["harness"] == full["exits"]["bot"] == full["exits"]["matched"] >= 2
    assert full["trial_to_date"]["unexplained_count"] == 0 and full["budget_evaluated"] is True
    assert not any("min_order_quote_usd" in n for n in full["notes"])  # the shipped $10 floor is > 0
    # The weekly command of the runbook: `python scripts/paper_parity_check.py --days 7`.
    assert ppc.main(argv + ["--days", "7"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("PAPER TRIAL PARITY CHECK -- PASS")


def test_a_first_start_outside_the_00_to_01_utc_window_contaminates_the_trial_start(run, tmp_path):
    """deploy/PAPER_TRIAL_RUNBOOK.md §2: the first cycle must run between 00:00 and 01:00 UTC. Started 6 hours
    after a close, the first cycle skips every entry of that bar as stale (and marks it decided); the parity check
    then starts its replay on that bar, where the harness enters, so the check cannot PASS. Uses the first bar of
    the main run that has an entry, so the harness provably has one there."""
    first_entry_bar = min(pd.Timestamp(e["bar_close"]) for e in run["events"] if e.get("action") == "planned")
    offset = next(i for i, (t, _k, _r) in enumerate(run["ticks"]) if t == first_entry_bar) // 2
    late = _drive(tmp_path / "late", TRIAL_START_BAR + offset, 8, first_tick_ms=6 * 3_600_000)
    first = [e for e in late["events"] if pd.Timestamp(e["bar_close"]) == first_entry_bar]
    deciding = [e for e in first if ppc.ALREADY_DECIDED not in str(e.get("note"))]
    assert len(deciding) == 16 and all(ppc.STALE_NOTE in e["note"] for e in deciding)
    assert not any(e.get("action") == "planned" or "candidates" in e for e in first)
    report = ppc.build_report(ppc.parse_args(["--config", str(late["tmp"] / "config.yaml"), "--days", "8"]))
    assert report["trial_start"] == first_entry_bar.isoformat()
    assert report["status"] != ppc.PASS
    assert any(m["explained_by"] == "staleness_guard" and m["bar"] == first_entry_bar.isoformat()
               for m in ppc._clean(report["mismatches"]))
