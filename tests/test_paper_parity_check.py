"""scripts/paper_parity_check.py: the weekly paper-trial parity check (docs/multiday_hold_design.md §5/§6).

Groups:
  A. Pure helpers and equivalence rules (cross-checked against tests/test_daily_live_parity.py).
  B. `compare()` / `collect_bot()` on hand-made decisions: every mismatch kind, explanations, the
     "downstream" rule, window filtering.
  C. The end-to-end fixture: the REAL bot loop (src.bot.run_cycle, loop.bar_parity on, 1d, paper broker,
     FakeCcxt serving the committed Kraken daily data bar by bar, closed_bars_dir set, notifier.label
     set) run for TRIAL_BARS bars, then the checker on the logs it wrote. Expected: PASS, with entries AND
     exits on both sides (not vacuous), plus the Telegram messages that run sent.
  D. Mutated copies of that fixture's logs: each fault must be reported (FAIL / explained WARN).
  E. Short / empty / corrupt inputs: clear message, exit 0 ("insufficient data") or exit 2.
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import yaml

import src.bot as bot
from scripts import paper_parity_check as ppc
from src import htf_filter, notifier
from src.config import Config, load_config
from tests.test_daily_live_parity import D_MS, DailyFakeCcxt, load_rows, same_decision
from tests.test_bot_universe_wiring import _Clock, _FakeDatetime

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "paper_parity_check.py"

# 15 bars starting at data bar 350 (2025-09-28): 7 entries and 2+ stop/target exits across 16 symbols,
# including exits decided on the final bar (so the checker's trailing-bar padding is exercised), and bars where
# several symbols are approved at once so count_same_bar_approvals changes the harness's decisions.
TRIAL_START_BAR = 350
TRIAL_BARS = 15
LABEL = "PAPER TRIAL"
DELAY_MINUTES = 60


def T(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


# --------------------------------------------------------------------------- #
# C. the generated fixture
# --------------------------------------------------------------------------- #
def _generate_trial(directory: Path, start: int = TRIAL_START_BAR, bars: int = TRIAL_BARS, ticks: int = 2,
                    floor: float = 10.0, label: str | None = LABEL) -> SimpleNamespace:
    """Runs the real bot loop and returns where its logs landed and which Telegram messages it sent."""
    mp = pytest.MonkeyPatch()
    messages: list[str] = []
    try:
        directory.mkdir(parents=True, exist_ok=True)
        rows, axis = load_rows(start + bars + 1, gapless=False)
        raw = copy.deepcopy(load_config().raw)
        raw["mode"] = "paper"
        raw["advisor"] = {"enabled": False}
        raw["market"].update(symbols=sorted(rows), timeframe="1d", history_bars=200)
        # A $10 order floor, as in tests/test_daily_live_parity.py's realistic-config run: the committed
        # floor of 0 produces known float-phantom divergences (docs/bot_bar_parity.md §4.2).
        raw["risk"]["min_order_quote_usd"] = floor
        raw["risk"]["kill_switch_file"] = str(directory / "NO_KILL_SWITCH")
        raw["loop"] = {"poll_interval_seconds": 300, "bar_parity": True, "max_entry_delay_minutes": DELAY_MINUTES}
        raw["notifier"] = {"label": label}
        raw["logging"] = {k: str(directory / v) for k, v in {
            "audit_log_path": "audit.jsonl", "trade_log_path": "trades.csv",
            "trade_plan_current_path": "plans.json", "trade_plan_history_path": "plans.jsonl",
            "market_snapshot_path": "snapshot.json", "bar_decisions_path": "bar_decisions.json",
            "closed_bars_dir": "daily_bars",
        }.items()}
        raw["logging"]["retention_days"] = 0
        mp.setenv("TELEGRAM_BOT_TOKEN", "token")
        mp.setenv("TELEGRAM_CHAT_ID", "12345")
        mp.setattr(notifier, "send_telegram_message", lambda token, chat, text: messages.append(text))
        mp.setattr(htf_filter, "_cache", {})
        mp.setattr("src.config.DEFAULT_BAR_DECISIONS_PATH", str(directory / "default_bar_decisions.json"))
        mp.setattr("src.risk_engine.datetime", _FakeDatetime)
        bot._reset_bar_parity_state()

        cfg = Config(raw=raw)
        fake = DailyFakeCcxt(rows)
        exchange, risk_engine, portfolio, broker, store, audit = bot.build_components(cfg)
        exchange.client = fake
        market_data = bot.build_market_data(cfg, exchange)

        memo: dict[int, tuple] = {}
        real_add = bot.add_indicators

        def cached(df, *args):  # speed only: the frame object is the same for both ticks of a bar
            hit = memo.get(id(df))
            if hit is not None and hit[0] is df:
                return hit[1]
            out = real_add(df, *args)
            memo[id(df)] = (df, out)
            return out

        mp.setattr(bot, "add_indicators", cached)
        for ts in axis[start:start + bars]:
            for k in range(ticks):
                fake.now_ms = ts + D_MS + 60_000 + k * 300_000
                _Clock.now = datetime.fromtimestamp(fake.now_ms / 1000, tz=timezone.utc)
                bot.run_cycle(cfg, exchange, risk_engine, portfolio, broker, store, audit, market_data)
        (directory / "config.yaml").write_text(yaml.safe_dump(raw))
    finally:
        mp.undo()
        bot._reset_bar_parity_state()
    return SimpleNamespace(dir=directory, config=directory / "config.yaml", messages=messages)


@pytest.fixture(scope="module")
def trial(tmp_path_factory):
    return _generate_trial(tmp_path_factory.mktemp("trial"))


def _argv(root: Path, *extra: str) -> list[str]:
    """Explicit paths for a (copied) trial directory; strategy parameters come from the fixture config."""
    return [
        "--config", str(root / "config.yaml"),
        "--audit-log", str(root / "audit.jsonl"), "--trade-log", str(root / "trades.csv"),
        "--bar-decisions", str(root / "bar_decisions.json"), "--bars-dir", str(root / "daily_bars"),
        *extra,
    ]


def run_check(root: Path, *extra: str) -> dict:
    return ppc.build_report(ppc.parse_args(_argv(root, *extra)))


@pytest.fixture(autouse=True)
def _memoize_harness_signals(monkeypatch):
    """Speed only: the harness's indicator + signal pass (~15 s over 216 bars x 16 symbols) is a pure function of
    the bar frames and the strategy config, and most tests here change only the audit log. Memoised per process
    on the exact frame contents, so a tampered archive still recomputes; nothing in scripts/ is patched."""
    from strategy_pack_run import backtest_live_bot as harness

    real = harness.build_enriched_and_signals

    def memoised(raw_frames, *args, **kwargs):
        key = hashlib.sha256(json.dumps(
            [[s, df.index.astype("int64").tolist(), df.to_numpy().tobytes().hex()] for s, df in sorted(raw_frames.items())]
            + [args, kwargs], default=str, sort_keys=True).encode()).hexdigest()
        if key not in _SIGNAL_MEMO:
            _SIGNAL_MEMO[key] = real(raw_frames, *args, **kwargs)
        return _SIGNAL_MEMO[key]

    monkeypatch.setattr(harness, "build_enriched_and_signals", memoised)


_SIGNAL_MEMO: dict = {}


@pytest.fixture
def work(trial, tmp_path):
    """A private copy of the generated logs to mutate."""
    dest = tmp_path / "work"
    shutil.copytree(trial.dir, dest)
    return dest


def read_events(root: Path) -> list[dict]:
    return [json.loads(line) for line in (root / "audit.jsonl").read_text().splitlines() if line.strip()]


def write_events(root: Path, events: list[dict]) -> None:
    (root / "audit.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))


def planned_events(events):
    return [e for e in events if e.get("action") == "planned" and "bar_close" in e]


def exit_events(events):
    return [e for e in events if (e.get("monitor_action") or {}).get("action") in ("STOP_LOSS", "TAKE_PROFIT") and e.get("execution")]


def digest(root: Path) -> dict[str, tuple[str, int]]:
    return {
        str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*")) if p.is_file()
    }


# ----- C. PASS on the real loop's logs -----


def test_real_bot_loop_logs_pass_and_are_not_vacuous(trial):
    report = run_check(trial.dir, "--days", str(TRIAL_BARS))
    assert report["status"] == ppc.PASS, report["message"] + json.dumps(report["mismatches"], indent=1)[:2000]
    assert report["exit_code"] == 0 and report["mismatch_count"] == 0
    # Not vacuous: entries and exits on both sides, agreeing 100%.
    for side in ("entries", "exits"):
        s = report[side]
        assert s["harness"] >= 3 and s["harness"] == s["bot"] == s["matched"], (side, s)
        assert s["agreement_rate"] == 1.0 and s["mismatch_rate"] == 0.0
    assert report["decisions_compared"] >= 15
    assert report["trial_to_date"]["unexplained_count"] == 0
    assert report["window"]["decided_bars"] == TRIAL_BARS and report["window"]["symbols"] == 16
    # The audit log holds both kinds of exit, and one is decided on the final bar (the padding bar's job).
    exits = exit_events(read_events(trial.dir))
    assert {e["monitor_action"]["action"] for e in exits} == {"STOP_LOSS", "TAKE_PROFIT"}
    last_bar = max(pd.Timestamp(e["bar_close"]) for e in read_events(trial.dir) if "bar_close" in e)
    assert any(pd.Timestamp(e["bar_close"]) == last_bar for e in exits)


def test_default_seven_bar_window_passes_and_json_is_strict(trial):
    report = run_check(trial.dir)
    assert report["status"] == ppc.PASS
    assert report["window"]["decided_bars"] == 7
    assert report["trial_to_date"]["decisions_compared"] > report["decisions_compared"]  # trial-to-date is wider
    text = json.dumps(ppc._clean(report), allow_nan=False)  # no NaN / non-serialisable values
    assert json.loads(text)["status"] == "PASS"


def test_check_is_read_only_and_deterministic(trial):
    before = digest(trial.dir)
    first = ppc._clean(run_check(trial.dir, "--days", str(TRIAL_BARS)))
    second = ppc._clean(run_check(trial.dir, "--days", str(TRIAL_BARS)))
    assert first == second
    assert digest(trial.dir) == before  # no byte or mtime changed, no file created (e.g. no __pycache__ in logs)


def test_kill_switch_file_and_live_state_do_not_affect_the_replay(trial, tmp_path):
    root = tmp_path / "ks"
    shutil.copytree(trial.dir, root)
    present = root / "KILL_SWITCH_PRESENT"
    present.write_text("x")
    raw = yaml.safe_load((root / "config.yaml").read_text())
    raw["risk"]["kill_switch_file"] = str(present)
    (root / "config.yaml").write_text(yaml.safe_dump(raw))
    assert run_check(root, "--days", str(TRIAL_BARS))["status"] == ppc.PASS


def test_the_run_sent_labelled_telegram_messages_with_valid_until(trial):
    msgs = trial.messages
    buys = [m for m in msgs if "BUY signal" in m]
    sells = [m for m in msgs if "SELL" in m]
    assert len(buys) >= 3 and len(sells) >= 1
    assert all(m.startswith(f"*{LABEL}* · ") for m in msgs)
    planned = planned_events(read_events(trial.dir))
    assert len(planned) == len(buys)
    for event, msg in zip(planned, buys):
        close = pd.Timestamp(event["bar_close"])
        assert f"Valid until: {(close + pd.Timedelta(minutes=DELAY_MINUTES)):%Y-%m-%d %H:%M} UTC" in msg
        assert f"closed {close:%Y-%m-%d %H:%M} UTC" in msg
    assert not any("Valid until" in m for m in msgs if "BUY signal" not in m)


def test_main_text_and_json_output(trial, capsys):
    assert ppc.main(_argv(trial.dir, "--days", str(TRIAL_BARS))) == 0
    out = capsys.readouterr().out
    assert out.startswith("PAPER TRIAL PARITY CHECK -- PASS")
    assert "budget 10%" in out and "an unexplained mismatch pauses the trial" in out and "trial to date" in out
    assert ppc.main(_argv(trial.dir, "--days", str(TRIAL_BARS), "--format", "json")) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PASS"


def test_json_out_is_written_and_refuses_to_overwrite_an_input(trial, tmp_path, capsys):
    out = tmp_path / "report.json"
    assert ppc.main(_argv(trial.dir, "--json-out", str(out))) == 0
    assert json.loads(out.read_text())["status"] == "PASS"
    capsys.readouterr()
    for victim in (trial.dir / "audit.jsonl", trial.dir / "daily_bars" / "BTC_USD_1d.csv", trial.dir / "config.yaml"):
        before = victim.read_bytes()
        assert ppc.main(_argv(trial.dir, "--json-out", str(victim))) == ppc.EXIT_INPUT_ERROR
        assert victim.read_bytes() == before
    assert "would overwrite an input" in capsys.readouterr().err


def test_cli_exit_codes_via_subprocess(trial, work, tmp_path):
    env_py = [sys.executable, str(SCRIPT)]

    def run(root, *extra):
        return subprocess.run(env_py + _argv(root, *extra), capture_output=True, text=True, cwd=tmp_path, timeout=300)

    ok = run(trial.dir, "--days", str(TRIAL_BARS))
    assert ok.returncode == 0 and "-- PASS" in ok.stdout, ok.stderr
    events = read_events(work)
    flipped = planned_events(events)[0]
    flipped["risk_decision"]["allowed"] = False
    flipped["action"] = "none"
    write_events(work, events)
    bad = run(work, "--days", str(TRIAL_BARS))
    assert bad.returncode == 1 and "-- FAIL" in bad.stdout and "UNEXPLAINED" in bad.stdout
    (work / "audit.jsonl").write_text("{not json\n")
    err = run(work)
    assert err.returncode == 2 and err.stdout == "" and "audit.jsonl:1" in err.stderr


# ----- C2. a realistic run: 110 bars, $10 floor (B1) -----


@pytest.fixture(scope="module")
def long_trial(tmp_path_factory):
    """110 daily bars from data bar 200 (2025-05-01 .. 2025-08-18), one tick per bar, 16 symbols, the real bot loop at a
    $10 order floor and the live 200-bar history. Real opens and the 0.10% paper fee, so every fill-price residual is
    present (the short fixture is too short to show them)."""
    return _generate_trial(tmp_path_factory.mktemp("long"), start=200, bars=110, ticks=1, floor=10.0, label=None)


def test_a_correct_bot_passes_over_110_real_loop_bars_at_a_10_dollar_floor(long_trial):
    r = run_check(long_trial.dir, "--days", "110")
    assert r["status"] == ppc.PASS, r["message"] + json.dumps(r["mismatches"][:3], indent=1)[:1500]
    assert r["window"]["decided_bars"] == 110
    assert r["entries"]["harness"] >= 20 and r["entries"]["harness"] == r["entries"]["bot"] == r["entries"]["matched"]
    assert r["exits"]["harness"] >= 15 and r["exits"]["harness"] == r["exits"]["bot"] == r["exits"]["matched"]
    assert r["budget_evaluated"] is True and r["trial_to_date"]["unexplained_count"] == 0
    # the fill-price residual is visible but small; the report says how large it got
    assert 0.0 <= r["size_drift_max"] < ppc.DEFAULT_SIZE_RTOL
    # ... and rejected-for-size decisions carry different sized amounts on the two sides (the old B1 failure)
    events = [json.loads(l) for l in (long_trial.dir / "audit.jsonl").read_text().splitlines()]
    assert any("sized order" in str((e.get("risk_decision") or {}).get("reason")) for e in events)


def test_the_floor_zero_warning_is_a_note(trial, work):
    raw = yaml.safe_load((work / "config.yaml").read_text())
    raw["risk"]["min_order_quote_usd"] = 0
    (work / "config.yaml").write_text(yaml.safe_dump(raw))
    r = run_check(work, "--days", str(TRIAL_BARS))
    assert any("min_order_quote_usd is 0" in n and "needs a floor > 0" in n for n in r["notes"])
    assert not any("min_order_quote_usd" in n for n in run_check(trial.dir)["notes"])


def test_harness_riskengine_patch_is_restored_after_the_replay_and_after_a_failure(trial, monkeypatch):
    from strategy_pack_run import backtest_live_bot as harness

    original = harness.RiskEngine
    run_check(trial.dir)
    assert harness.RiskEngine is original
    real = harness.simulate_portfolio

    def boom(*a, **kw):
        raise RuntimeError("replay blew up")

    monkeypatch.setattr(harness, "simulate_portfolio", boom)
    with pytest.raises(RuntimeError, match="blew up"):
        run_check(trial.dir)
    monkeypatch.setattr(harness, "simulate_portfolio", real)
    assert harness.RiskEngine is original


# ----- D. mutated copies: each fault is reported -----


def test_a_flipped_entry_decision_fails_and_is_listed_with_both_sides(work):
    events = read_events(work)
    target = planned_events(events)[0]
    target["risk_decision"] = {"allowed": False, "reason": "position would exceed the cap", "max_quote_amount": None}
    target["action"] = "none"
    target.pop("execution", None)
    write_events(work, events)
    report = run_check(work, "--days", str(TRIAL_BARS))
    assert report["status"] == ppc.FAIL and report["exit_code"] == 1
    first = report["mismatches"][0]
    assert (first["kind"], first["symbol"], first["bar"]) == ("entry_decision", target["symbol"], pd.Timestamp(target["bar_close"]).isoformat())
    assert first["explained_by"] is None
    assert first["harness"]["allowed"] is True and first["bot"]["allowed"] is False
    assert "position would exceed the cap" in first["detail"]
    assert report["unexplained_count"] >= 1 and "unexplained" in report["message"]


def test_a_different_signal_pattern_fails(work):
    events = read_events(work)
    target = planned_events(events)[0]
    old = target["signal"]["pattern"]
    target["signal"]["pattern"] = "PULLBACK" if old != "PULLBACK" else "BREAKOUT"
    write_events(work, events)
    report = run_check(work, "--days", str(TRIAL_BARS))
    assert report["status"] == ppc.FAIL
    first = report["mismatches"][0]
    assert first["kind"] == "pattern" and first["symbol"] == target["symbol"] and old in first["detail"]


def test_harness_replay_counts_same_bar_approvals(trial, monkeypatch):
    """The replay must be the P6 harness (count_same_bar_approvals=True), and on this window that flag changes
    the harness's own decisions, so a replay without it could not pass against the bot."""
    from strategy_pack_run import backtest_live_bot as harness

    seen = []
    real = harness.simulate_portfolio
    monkeypatch.setattr(harness, "simulate_portfolio", lambda *a, **kw: (seen.append(kw), real(*a, **kw))[1])
    assert run_check(trial.dir, "--days", str(TRIAL_BARS))["status"] == ppc.PASS
    assert seen and all(kw.get("count_same_bar_approvals") is True for kw in seen)


def test_a_missing_exit_fails(work):
    events = read_events(work)
    target = exit_events(events)[0]
    target.pop("monitor_action")
    target.pop("execution")
    write_events(work, events)
    report = run_check(work, "--days", str(TRIAL_BARS))
    assert report["status"] == ppc.FAIL
    kinds = {(m["kind"], m["symbol"]) for m in report["mismatches"]}
    assert ("exit_harness_only", target["symbol"]) in kinds


def test_a_wrong_exit_reason_fails(work):
    events = read_events(work)
    target = exit_events(events)[0]
    old = target["monitor_action"]["action"]
    target["monitor_action"]["action"] = "TAKE_PROFIT" if old == "STOP_LOSS" else "STOP_LOSS"
    write_events(work, events)
    report = run_check(work, "--days", str(TRIAL_BARS))
    assert report["status"] == ppc.FAIL
    assert any(m["kind"] == "exit_harness_only" and "bot exits on" in m["detail"] for m in report["mismatches"])


def test_a_staleness_skip_is_explained_and_the_strict_budget_is_the_default(work):
    events = read_events(work)
    target = planned_events(events)[0]
    drop_trade_rows(work, {target["execution"]["timestamp"]})  # a skipped entry never filled
    for key in ("risk_decision", "plan", "signal", "execution", "candidates", "daily_trend_confirmed"):
        target.pop(key, None)
    target["action"], target["note"], target["entry_delay_minutes"] = "none", "stale bar: entries skipped", 75.0
    write_events(work, events)
    # Default = the pre-registered rule (budget from the first entry): one missed entry out of ~7 is >10% -> FAIL.
    default = run_check(work, "--days", str(TRIAL_BARS))
    assert default["status"] == ppc.FAIL and "exceeds the 10% budget" in default["message"] and default["budget_evaluated"] is True
    first = default["mismatches"][0]
    assert first["explained_by"] == ppc.EXPLAINED_STALE and first["symbol"] == target["symbol"]
    assert default["unexplained_count"] == 0  # nothing unexplained: later fallout is "downstream"
    assert all(m["explained_by"] for m in default["mismatches"])
    # The relaxed rule (a loosening that needs the user's acceptance, explicit flag): fewer than 20 entries -> not yet
    # evaluable, explained mismatches are a WARN.
    relaxed = run_check(work, "--days", str(TRIAL_BARS), "--relaxed-early-budget")
    assert relaxed["status"] == ppc.WARN and relaxed["exit_code"] == 0 and relaxed["mismatch_count"] >= 1
    assert relaxed["budget_evaluated"] is False and any("budget not yet evaluable" in n for n in relaxed["notes"])
    # An explicit looser budget is a WARN too.
    assert run_check(work, "--days", str(TRIAL_BARS), "--budget", "0.5")["status"] == ppc.WARN


def test_downstream_label_only_follows_an_explained_mismatch(work):
    events = read_events(work)
    for target in exit_events(events)[:1]:  # an unexplained first mismatch must never be relabelled
        target.pop("monitor_action")
        target.pop("execution")
    write_events(work, events)
    report = run_check(work, "--days", str(TRIAL_BARS), "--budget", "1.0")
    assert report["status"] == ppc.FAIL
    assert report["mismatches"][0]["explained_by"] is None
    assert not any(m["explained_by"] == ppc.EXPLAINED_DOWNSTREAM for m in report["mismatches"][:1])


def drop_trade_rows(work, stamps):
    lines = (work / "trades.csv").read_text().splitlines()
    (work / "trades.csv").write_text("\n".join(l for l in lines if l.split(",")[0] not in stamps) + "\n")


def _drop_bar_events(work, bar_close):
    """Removes every audit line of one bar and the trade-log rows of the fills they reported (the bot was down)."""
    events = read_events(work)
    gone = [e for e in events if e.get("bar_close") == bar_close]
    stamps = {e["execution"]["timestamp"] for e in gone if e.get("execution")}
    write_events(work, [e for e in events if e.get("bar_close") != bar_close])
    drop_trade_rows(work, stamps)


def test_a_bar_with_no_bot_events_at_all_is_a_bot_gap(work):
    target = planned_events(read_events(work))[0]
    _drop_bar_events(work, target["bar_close"])
    report = run_check(work, "--days", str(TRIAL_BARS), "--budget", "1.0")
    first = report["mismatches"][0]
    assert first["kind"] == "harness_only_check" and first["explained_by"] == ppc.EXPLAINED_GAP
    assert report["unexplained_count"] == 0 and report["status"] == ppc.WARN


def test_one_symbol_missing_from_a_bar_the_bot_otherwise_decided_is_not_a_gap(work):
    events = read_events(work)
    target = planned_events(events)[0]
    key = (target["bar_close"], target["symbol"])
    write_events(work, [e for e in events if (e.get("bar_close"), e.get("symbol")) != key])
    report = run_check(work, "--days", str(TRIAL_BARS), "--budget", "1.0")
    assert report["status"] == ppc.FAIL
    first = report["mismatches"][0]
    assert (first["kind"], first["symbol"], first["explained_by"]) == ("harness_only_check", target["symbol"], None)
    assert not any(m["explained_by"] == ppc.EXPLAINED_GAP for m in report["mismatches"])


def test_a_lost_deciding_line_with_a_surviving_echo_is_an_integrity_failure(work):
    events = read_events(work)
    echo = next(e for e in events if "already decided" in str(e.get("note")))
    key = (echo["bar_close"], echo["symbol"])
    deciding = [e for e in events if (e.get("bar_close"), e.get("symbol")) == key and "candidates" in e]
    assert len(deciding) == 1
    write_events(work, [e for e in events if e is not deciding[0]])
    report = run_check(work, "--days", str(TRIAL_BARS), "--budget", "1.0")
    assert report["status"] == ppc.FAIL
    lost = [m for m in report["mismatches"] if m["kind"] == "missing_decision_event"]
    assert len(lost) == 1 and lost[0]["symbol"] == echo["symbol"] and lost[0]["explained_by"] is None
    assert not any(m["explained_by"] == ppc.EXPLAINED_GAP for m in report["mismatches"])


def test_a_tampered_archive_bar_is_caught(work):
    events = read_events(work)
    target = planned_events(events)[0]
    path = work / "daily_bars" / (target["symbol"].replace("/", "_") + "_1d.csv")
    open_ms = int((pd.Timestamp(target["bar_close"]) - pd.Timedelta(days=1)).value // 1_000_000)
    lines = path.read_text().splitlines()
    out = []
    for line in lines:
        parts = line.split(",")
        if parts[0] == str(open_ms):
            parts[4] = repr(float(parts[4]) * 1.5)  # close
            parts[2] = repr(max(float(parts[2]), float(parts[4])))  # keep high >= close
        out.append(",".join(parts))
    path.write_text("\n".join(out) + "\n")
    report = run_check(work, "--days", str(TRIAL_BARS))
    assert report["status"] == ppc.FAIL and report["mismatches"]


def test_a_bar_decided_twice_is_an_unexplained_integrity_failure(work):
    events = read_events(work)
    target = planned_events(events)[0]
    events.insert(events.index(target) + 1, copy.deepcopy(target))
    write_events(work, events)
    report = run_check(work, "--days", str(TRIAL_BARS), "--budget", "1.0")
    assert report["status"] == ppc.FAIL
    dup = [m for m in report["mismatches"] if m["kind"] == "duplicate_decision"]
    assert len(dup) == 1 and dup[0]["explained_by"] is None and "P1" in dup[0]["detail"]


def test_trade_log_and_audit_log_must_agree(work):
    lines = (work / "trades.csv").read_text().splitlines()
    (work / "trades.csv").write_text("\n".join([lines[0]] + lines[2:]) + "\n")  # drop the first fill
    report = run_check(work, "--days", str(TRIAL_BARS), "--budget", "1.0")
    assert report["status"] == ppc.FAIL
    assert any(m["kind"] == "audit_trade_log" for m in report["mismatches"])


def test_a_trade_log_row_without_an_audit_fill_is_caught(work):
    lines = (work / "trades.csv").read_text().splitlines()
    fields = lines[1].split(",")
    # same symbol and side, a microsecond later: inside the audited wall-clock span, but never audited
    fields[0] = (pd.Timestamp(fields[0]) + pd.Timedelta(microseconds=1)).isoformat()
    (work / "trades.csv").write_text("\n".join(lines + [",".join(fields)]) + "\n")
    report = run_check(work, "--days", str(TRIAL_BARS), "--budget", "1.0")
    [m] = [m for m in report["mismatches"] if m["kind"] == "trade_log_audit"]
    assert report["status"] == ppc.FAIL
    # reported on the bar being decided when the row was written, not on the wall-clock date
    assert pd.Timestamp(m["bar"]) <= pd.Timestamp(report["window"]["until"]) and pd.Timestamp(m["bar"]).year == 2025


def _plan_levels_fault(work):
    """One unexplained mismatch that cannot cascade: an early plan whose stop is 2% off."""
    events = read_events(work)
    target = planned_events(events)[0]  # early in the trial
    target["plan"]["stop_loss"] = target["plan"]["stop_loss"] * 1.02
    write_events(work, events)
    return target


def test_an_unexplained_mismatch_that_left_the_window_still_pauses_the_trial(work):
    target = _plan_levels_fault(work)
    bar = pd.Timestamp(target["bar_close"])
    assert bar < pd.Timestamp(max(e["bar_close"] for e in read_events(work) if "bar_close" in e)) - pd.Timedelta(days=5)
    # last 3 bars only: the mismatch is not in the window ...
    late = run_check(work, "--days", "3", "--budget", "1.0")
    assert late["mismatch_count"] == 0 and late["mismatches"] == []
    assert late["trial_to_date"]["unexplained_count"] == 1
    # ... but the trial stays paused (FAIL) until it is fixed and acknowledged.
    assert late["status"] == ppc.FAIL and late["exit_code"] == 1
    assert "earlier in the trial still pause it" in late["message"] and target["symbol"] in late["message"]
    # The window that contains it reports it.
    inside = run_check(work, "--since", bar.isoformat(), "--until", bar.isoformat(), "--min-bars", "1")
    assert inside["status"] == ppc.FAIL and inside["mismatches"][0]["kind"] == "plan_levels"


def test_acknowledging_a_mismatch_is_explicit_listed_and_unmatched_acks_warn(work, tmp_path):
    target = _plan_levels_fault(work)
    day = pd.Timestamp(target["bar_close"]).strftime("%Y-%m-%d")
    spec = f"{day}:{target['symbol']}:plan_levels"
    ok = run_check(work, "--days", "3", "--ack", spec)
    assert ok["status"] == ppc.PASS and ok["acknowledged"] == [spec] and ok["trial_to_date"]["unexplained_count"] == 0
    # in a window containing it, it is listed as acknowledged, status WARN
    full = run_check(work, "--days", str(TRIAL_BARS), "--ack", spec)
    assert full["status"] == ppc.WARN and full["mismatches"][0]["explained_by"] == ppc.EXPLAINED_ACK
    # --ack-file: comments and blank lines allowed; an ack that matches nothing makes the status a WARN
    f = tmp_path / "acks.txt"
    f.write_text(f"# fixed in commit abc\n{spec}  # plan stop\n\n2025-10-03:ZZZ/USD:size\n")
    viafile = run_check(work, "--days", "3", "--ack-file", str(f))
    assert viafile["status"] == ppc.WARN and viafile["exit_code"] == 0 and viafile["unmatched_acks"] == ["2025-10-03:ZZZ/USD:size"]
    assert "match no mismatch" in viafile["message"] and "ZZZ/USD" in viafile["message"]
    # the KIND must match too: a wrong symbol, day or kind acknowledges nothing
    for wrong in (f"{day}:ETH/USD:plan_levels", f"{day}:{target['symbol']}:entry_decision", f"2025-10-03:{target['symbol']}:plan_levels"):
        assert run_check(work, "--days", "3", "--ack", wrong)["status"] == ppc.FAIL


def test_a_blanket_acknowledgement_file_is_refused(work, tmp_path):
    _plan_levels_fault(work)
    bars = pd.date_range("2025-09-28", periods=TRIAL_BARS, tz="UTC")
    symbols = sorted(p.name.replace("_1d.csv", "").replace("_", "/") for p in (work / "daily_bars").glob("*_1d.csv"))
    f = tmp_path / "blanket.txt"
    f.write_text("".join(f"{b:%Y-%m-%d}:{sym}:{kind}\n" for b in bars for sym in symbols
                         for kind in ("plan_levels", "entry_decision", "pattern", "size")))
    r = run_check(work, "--days", str(TRIAL_BARS), "--ack-file", str(f))
    assert r["status"] == ppc.FAIL and "blanket acknowledgement" in r["message"]
    assert r["acknowledged"]  # the real one matched, and the abuse is still a FAIL


def test_bad_acknowledgements_are_input_errors(work, capsys):
    for bad, why in (("nonsense", "BAR:SYMBOL:KIND"), ("2025-10-02:BTC/USD", "BAR:SYMBOL:KIND"), (":BTC/USD:size", "BAR:SYMBOL:KIND"),
                     ("notadate:BTC/USD:size", "acknowledgement"), ("2025-10-02:BTC/USD:nonsense_kind", "unknown kind"),
                     ("2025-10-02:BTC/USD:missing_decision_event", "cannot be acknowledged"),
                     ("2025-10-02:BTC/USD:trade_log_audit", "cannot be acknowledged"),
                     ("2025-10-02:BTC/USD:audit_trade_log", "cannot be acknowledged"),
                     ("2025-10-02:BTC/USD:duplicate_decision", "cannot be acknowledged"),
                     ("2031-01-01:BTC/USD:size", "dated after the newest decided bar")):
        assert ppc.main(_argv(work, "--ack", bad)) == ppc.EXIT_INPUT_ERROR, bad
        assert why in capsys.readouterr().err, bad
    assert ppc.main(_argv(work, "--ack", "2025-10-05:BTC/USD:size", "--until", "2025-10-03")) == ppc.EXIT_INPUT_ERROR
    assert "dated after --until" in capsys.readouterr().err
    assert ppc.main(_argv(work, "--ack-file", str(work / "missing.txt"))) == ppc.EXIT_INPUT_ERROR
    assert "--ack-file" in capsys.readouterr().err


def test_an_acknowledgement_does_not_explain_what_followed_it(work):
    events = read_events(work)
    target = exit_events(events)[0]  # an exit the bot "missed": unexplained
    drop_trade_rows(work, {target["execution"]["timestamp"]})
    target.pop("monitor_action"), target.pop("execution")
    write_events(work, events)
    spec = f"{pd.Timestamp(target['bar_close']).strftime('%Y-%m-%d')}:{target['symbol']}:exit_harness_only"
    r = run_check(work, "--days", str(TRIAL_BARS), "--ack", spec)
    explained = [m for m in r["mismatches"] if m["explained_by"] == ppc.EXPLAINED_ACK]
    assert [m["kind"] for m in explained] == ["exit_harness_only"] and explained[0]["symbol"] == target["symbol"]
    assert not any(m["explained_by"] == ppc.EXPLAINED_DOWNSTREAM for m in r["mismatches"])


def test_window_bounds_select_bars(trial):
    r = run_check(trial.dir, "--since", "2025-10-02T00:00:00+00:00", "--until", "2025-10-05T00:00:00+00:00")
    assert (r["window"]["since"], r["window"]["until"], r["window"]["decided_bars"]) == (
        "2025-10-02T00:00:00+00:00", "2025-10-05T00:00:00+00:00", 4)
    # a window before the trial started is clamped to the trial start
    r = run_check(trial.dir, "--since", "2025-01-01", "--until", "2025-10-02T00:00:00+00:00", "--min-bars", "1")
    assert r["window"]["since"].startswith("2025-09-28") and any("clamped" in n for n in r["notes"])


def test_an_explicit_trial_start_changes_the_replay(work):
    # Starting the replay later than the bot started: the harness has no positions from before, so exits of
    # the bot's early positions are bot-only mismatches.
    later = run_check(work, "--days", str(TRIAL_BARS), "--trial-start", "2025-10-04T00:00:00+00:00", "--budget", "1.0")
    assert later["status"] == ppc.FAIL
    assert any(m["kind"] in ("exit_bot_only", "bot_only_check") for m in later["mismatches"])


def test_pattern_and_plan_level_mismatches_are_never_tagged_downstream(work):
    events = read_events(work)
    planned = planned_events(events)
    first, second, third = planned[0], planned[1], planned[2]
    drop_trade_rows(work, {first["execution"]["timestamp"]})
    for key in ("risk_decision", "plan", "signal", "execution", "candidates", "daily_trend_confirmed"):
        first.pop(key, None)
    first["action"], first["note"] = "none", "stale bar: entries skipped"  # explained: starts the cascade
    second["signal"]["pattern"] = "PULLBACK" if second["signal"]["pattern"] != "PULLBACK" else "BREAKOUT"
    third["plan"]["stop_loss"] *= 1.02
    write_events(work, events)
    r = run_check(work, "--days", str(TRIAL_BARS), "--budget", "1.0")
    by_kind = {m["kind"]: m for m in r["mismatches"] if m["symbol"] in (second["symbol"], third["symbol"])}
    assert by_kind["pattern"]["explained_by"] is None and by_kind["plan_levels"]["explained_by"] is None
    assert r["mismatches"][0]["explained_by"] == ppc.EXPLAINED_STALE
    assert r["status"] == ppc.FAIL and r["exit_code"] == 1


def test_downstream_eligibility_by_kind_and_state():
    stale = bot_bar(B1, "X/USD", None, stale=True)
    first = {(B1, "X/USD"): stale}
    h = {(B1, "X/USD"): CHK}

    def after(bot_map, hmap, signal_keys=()):
        r = cmp({**first, **bot_map}, ppc.HarnessResult(checks={**h, **hmap}, exits=set(), last_bar={"X/USD": B4, "Y/USD": B4}, n_bars=4,
                                                         signal_keys=set(signal_keys)))
        return [(m.kind, m.symbol, m.explained_by) for m in r["mismatches"] if m.symbol == "Y/USD"]

    # pattern / plan levels: never
    p = dict(CHK, pattern="BREAKOUT")
    assert after({(B2, "Y/USD"): bot_bar(B2, "Y/USD", dict(CHK, pattern="PULLBACK"))}, {(B2, "Y/USD"): p}) == [("pattern", "Y/USD", None)]
    assert after({(B2, "Y/USD"): bot_bar(B2, "Y/USD", dict(CHK, stop=96.0))}, {(B2, "Y/USD"): CHK}) == [("plan_levels", "Y/USD", None)]
    # the harness had a check, the bot had no signal (state independent): not downstream
    assert after({(B2, "Y/USD"): bot_bar(B2, "Y/USD", None)}, {(B2, "Y/USD"): CHK}) == [("harness_only_check", "Y/USD", None)]
    # ... but if the bot was HOLDING a plan there, it is portfolio state: downstream
    held = bot_bar(B2, "Y/USD", None, held_note="OPEN plan active for Y/USD, no trigger this cycle")
    assert after({(B2, "Y/USD"): held}, {(B2, "Y/USD"): CHK}) == [("harness_only_check", "Y/USD", ppc.EXPLAINED_DOWNSTREAM)]
    # bot-only check: downstream only if the harness had a signal there (so it held a position instead)
    assert after({(B2, "Y/USD"): bot_bar(B2, "Y/USD", CHK)}, {}) == [("bot_only_check", "Y/USD", None)]
    assert after({(B2, "Y/USD"): bot_bar(B2, "Y/USD", CHK)}, {}, signal_keys=[(B2, "Y/USD")]) == [("bot_only_check", "Y/USD", ppc.EXPLAINED_DOWNSTREAM)]
    # entry decision / rejection reason / size are portfolio dependent
    assert after({(B2, "Y/USD"): bot_bar(B2, "Y/USD", REJ)}, {(B2, "Y/USD"): CHK}) == [("entry_decision", "Y/USD", ppc.EXPLAINED_DOWNSTREAM)]
    assert after({(B2, "Y/USD"): bot_bar(B2, "Y/USD", dict(CHK, quote=900.0))}, {(B2, "Y/USD"): CHK}) == [("size", "Y/USD", ppc.EXPLAINED_DOWNSTREAM)]


def test_gap_needs_proof_unit():
    ev_all = ppc.Evidence(bars_with_events={B1})
    ev_fetch = ppc.Evidence(bars_with_events={B1}, fetch_failed={(B1, "X/USD")})
    only = lambda ev, bot_map: cmp(bot_map, hres({(B1, "X/USD"): CHK}), evidence=ev)["mismatches"][0].explained_by  # noqa: E731
    assert only(ppc.Evidence(), {}) == ppc.EXPLAINED_GAP                                   # nobody decided this bar
    assert only(ev_all, {(B1, "Y/USD"): bot_bar(B1, "Y/USD")}) is None                     # others did: no gap
    assert only(ev_fetch, {(B1, "Y/USD"): bot_bar(B1, "Y/USD")}) == ppc.EXPLAINED_FETCH    # proven fetch failure
    echo_only = bot_bar(B1, "X/USD", None, n_decision_events=0)
    assert only(ev_fetch, {(B1, "X/USD"): echo_only}) is None                              # echo present: line lost


def test_a_fetch_failure_in_the_audit_log_explains_a_missing_decision(work):
    events = read_events(work)
    target = planned_events(events)[0]
    day = pd.Timestamp(target["bar_close"]).strftime("%Y-%m-%d")
    drop_trade_rows(work, {target["execution"]["timestamp"]})
    kept = [e for e in events if (e.get("bar_close"), e.get("symbol")) != (target["bar_close"], target["symbol"])]
    kept.append({"symbol": target["symbol"], "mode": "paper", "timestamp": f"{day}T00:05:00+00:00", "action": "none",
                 "error": "market data unavailable this cycle"})
    write_events(work, kept)
    r = run_check(work, "--days", str(TRIAL_BARS), "--budget", "1.0")
    first = r["mismatches"][0]
    assert (first["kind"], first["symbol"], first["explained_by"]) == ("harness_only_check", target["symbol"], ppc.EXPLAINED_FETCH)


def test_a_rescan_without_a_risk_check_is_a_duplicate_decision(work):
    events = read_events(work)
    scan = next(e for e in events if "candidates" in e and "risk_decision" not in e and e.get("action") == "none"
                and "already decided" not in str(e.get("note")))
    events.insert(events.index(scan) + 1, copy.deepcopy(scan))
    write_events(work, events)
    r = run_check(work, "--days", str(TRIAL_BARS), "--budget", "1.0")
    dup = [m for m in r["mismatches"] if m["kind"] == "duplicate_decision"]
    assert len(dup) == 1 and dup[0]["symbol"] == scan["symbol"] and r["status"] == ppc.FAIL


def test_size_tolerance_is_10_percent_with_a_dollar_floor():
    assert ppc.DEFAULT_SIZE_RTOL == 0.10
    b = lambda q: {(B1, "X/USD"): bot_bar(B1, "X/USD", dict(CHK, quote=q))}  # noqa: E731
    h = hres({(B1, "X/USD"): CHK})
    assert cmp(b(540.0), h)["mismatches"] == []                                        # +8%
    assert [m.kind for m in cmp(b(580.0), h)["mismatches"]] == ["size"]                # +16%
    tiny = dict(CHK, quote=0.40)
    assert cmp({(B1, "X/USD"): bot_bar(B1, "X/USD", dict(CHK, quote=0.22))}, hres({(B1, "X/USD"): tiny}))["mismatches"] == []
    assert cmp(b(540.0), h)["size_drift_max"] == pytest.approx(40 / 540)


# ----- truncated / pruned audit logs (B5, M6) -----


def test_a_missing_end_of_the_audit_log_is_an_input_error_naming_the_cause(work, capsys):
    events = read_events(work)
    bars = sorted({e["bar_close"] for e in events if "bar_close" in e})
    write_events(work, [e for e in events if e.get("bar_close") not in bars[-2:]])
    assert ppc.main(_argv(work)) == ppc.EXIT_INPUT_ERROR
    err = capsys.readouterr().err
    assert "END of the audit log is missing" in err and "ledger" in err and "retention" not in err


def test_trade_rows_after_the_audited_tail_are_caught_without_a_ledger(work):
    events = read_events(work)
    last = max(e["bar_close"] for e in events if "bar_close" in e)
    assert any(e.get("bar_close") == last and e.get("execution") for e in events)  # a fill on the final bar
    (work / "bar_decisions.json").unlink()
    write_events(work, [e for e in events if e.get("bar_close") != last])
    r = run_check(work, "--days", str(TRIAL_BARS), "--budget", "1.0")
    assert r["status"] == ppc.FAIL
    assert any(m["kind"] == "trade_log_audit" and m["explained_by"] is None for m in r["mismatches"])


def test_pruned_audit_log_older_trades_is_an_error_unless_trial_start_is_given(work, capsys):
    events = read_events(work)
    write_events(work, [e for e in events if "bar_close" not in e or pd.Timestamp(e["bar_close"]) >= T("2025-10-04")])
    assert ppc.main(_argv(work, "--days", str(TRIAL_BARS))) == ppc.EXIT_INPUT_ERROR
    err = capsys.readouterr().err
    assert "pruned" in err and "--trial-start" in err and "retention_days: 0" in err
    assert ppc.main(_argv(work, "--days", str(TRIAL_BARS), "--trial-start", "2025-10-04")) != ppc.EXIT_INPUT_ERROR


def test_a_partly_pruned_first_bar_is_skipped(work):
    events = read_events(work)
    first_bar = min(e["bar_close"] for e in events if "bar_close" in e)
    keep = [e for e in events if e.get("bar_close") != first_bar]
    keep += [e for e in events if e.get("bar_close") == first_bar][:3]
    write_events(work, keep)
    r = run_check(work, "--days", str(TRIAL_BARS))
    assert r["trial_start"].startswith("2025-09-29") and "partly in the audit log" in r["trial_start_source"]
    assert r["status"] == ppc.PASS


def test_trial_start_in_the_middle_of_a_bar_works(trial):
    # retention runs at arbitrary hours; the bar closing 2025-09-29 00:00 was decided ~00:05, i.e. BEFORE this time
    r = run_check(trial.dir, "--days", str(TRIAL_BARS), "--trial-start", "2025-09-29T14:20:00+00:00")
    assert r["trial_start"].startswith("2025-09-29") and r["status"] == ppc.PASS
    assert run_check(trial.dir, "--days", str(TRIAL_BARS), "--trial-start", "2025-09-29T00:03:00+00:00")["trial_start"].startswith("2025-09-29")


def test_trial_start_after_the_newest_bar_is_an_error(trial, capsys):
    assert ppc.main(_argv(trial.dir, "--trial-start", "2031-01-01")) == ppc.EXIT_INPUT_ERROR
    assert "after the newest decided bar" in capsys.readouterr().err


# ----- --json-out safety (M7) -----


def test_json_out_never_replaces_existing_files_without_force_nor_bot_files_with_it(trial, tmp_path, capsys):
    mine = tmp_path / "mine.json"
    mine.write_text("old")
    assert ppc.main(_argv(trial.dir, "--json-out", str(mine))) == ppc.EXIT_INPUT_ERROR
    assert "already exists" in capsys.readouterr().err and mine.read_text() == "old"
    assert ppc.main(_argv(trial.dir, "--json-out", str(mine), "--force")) == 0
    assert json.loads(mine.read_text())["status"] == "PASS"
    capsys.readouterr()
    # files the bot itself writes (the config's logging: block) are refused even with --force
    errors = ""
    for name in ("plans.json", "plans.jsonl", "snapshot.json", "trades.csv", "audit.jsonl", "bar_decisions.json", "daily_bars/new.json"):
        victim = trial.dir / name
        before = victim.read_bytes() if victim.exists() else None
        assert ppc.main(_argv(trial.dir, "--json-out", str(victim), "--force")) == ppc.EXIT_INPUT_ERROR, name
        assert (victim.read_bytes() if victim.exists() else None) == before, name
        errors += capsys.readouterr().err
    assert "the bot writes" in errors and "would overwrite an input" in errors


# ----- second review: B2-bis, bounded downstream, strict default budget, OSError, acks, protections -----


def test_pattern_and_levels_are_reported_even_when_the_decision_also_differs(work):
    """B2-bis: a different pattern / stop that coincides with the bot REJECTING must not hide as a downstream entry
    decision: pattern and plan_levels are always their own non-downstream mismatches."""
    events = read_events(work)
    planned = planned_events(events)
    first, second = planned[0], planned[1]
    drop_trade_rows(work, {first["execution"]["timestamp"], second["execution"]["timestamp"]})
    for key in ("risk_decision", "plan", "signal", "execution", "candidates", "daily_trend_confirmed"):
        first.pop(key, None)
    first["action"], first["note"] = "none", "stale bar: entries skipped"          # explained: starts the cascade
    second["signal"]["pattern"] = "PULLBACK" if second["signal"]["pattern"] != "PULLBACK" else "BREAKOUT"
    second["plan"]["stop_loss"] *= 1.02
    second["risk_decision"] = {"allowed": False, "reason": "position would exceed the cap", "max_quote_amount": None}
    second["action"] = "none"
    second.pop("execution")
    write_events(work, events)
    r = run_check(work, "--days", str(TRIAL_BARS))
    mine = {m["kind"]: m for m in r["mismatches"] if m["symbol"] == second["symbol"] and m["bar"] == pd.Timestamp(second["bar_close"]).isoformat()}
    assert mine["pattern"]["explained_by"] is None and mine["plan_levels"]["explained_by"] is None
    assert "stop" in mine["plan_levels"]["detail"] or "entry/stop/rr" in mine["plan_levels"]["detail"]
    assert mine["entry_decision"]["explained_by"] == ppc.EXPLAINED_DOWNSTREAM  # the decision may be fallout; the rest is not
    assert r["status"] == ppc.FAIL and r["unexplained_count"] >= 2
    # the pattern/levels mismatches are unexplained even with a looser budget and the relaxed flag
    assert run_check(work, "--days", str(TRIAL_BARS), "--budget", "1.0", "--relaxed-early-budget")["status"] == ppc.FAIL


def test_a_bot_event_without_a_signal_is_flagged_not_skipped(work):
    events = read_events(work)
    target = planned_events(events)[0]
    target.pop("signal")
    write_events(work, events)
    r = run_check(work, "--days", str(TRIAL_BARS))
    m = next(m for m in r["mismatches"] if m["kind"] == "pattern")
    assert "missing" in m["detail"] and m["explained_by"] is None and r["status"] == ppc.FAIL


def test_one_explained_event_followed_by_a_total_entry_divergence_fails_by_default(work):
    """The reviewer's scenario: every entry is a (documented) stale skip: all mismatches are explained or downstream,
    yet the bot made none of the harness's entries. The pre-registered budget is the default, so this is a FAIL."""
    events = read_events(work)
    stamps = set()
    for e in planned_events(events):
        stamps.add(e["execution"]["timestamp"])
        for key in ("risk_decision", "plan", "signal", "execution", "candidates", "daily_trend_confirmed"):
            e.pop(key, None)
        e["action"], e["note"] = "none", "stale bar: entries skipped"
    for e in exit_events(events):
        stamps.add(e["execution"]["timestamp"])
        e.pop("monitor_action"), e.pop("execution")
    write_events(work, events)
    drop_trade_rows(work, stamps)
    r = run_check(work, "--days", str(TRIAL_BARS))
    assert r["unexplained_count"] == 0 and r["trial_to_date"]["entries"]["mismatch_rate"] == 1.0
    assert r["status"] == ppc.FAIL and r["exit_code"] == 1 and "exceeds the 10% budget" in r["message"]
    # the relaxed flag (needs the user's acceptance) is the only way this is a WARN
    assert run_check(work, "--days", str(TRIAL_BARS), "--relaxed-early-budget")["status"] == ppc.WARN


def test_downstream_window_ends_when_both_sides_hold_the_same_positions_again():
    stale = bot_bar(B1, "X/USD", None, stale=True)
    iv = [("X/USD", B1, B2)]  # the harness entered X on B1 (the bot skipped it) and exited on B2

    def run(bot_map, hmap):
        h = ppc.HarnessResult(checks={(B1, "X/USD"): CHK, **hmap}, exits=set(), last_bar={"X/USD": B4, "Y/USD": B4}, n_bars=4, intervals=iv)
        return [(m.bar, m.kind, m.explained_by) for m in cmp({(B1, "X/USD"): stale, **bot_map}, h)["mismatches"] if m.symbol == "Y/USD"]

    during = run({(B2, "Y/USD"): bot_bar(B2, "Y/USD", REJ)}, {(B2, "Y/USD"): CHK})        # harness holds X, the bot does not
    after = run({(B3, "Y/USD"): bot_bar(B3, "Y/USD", REJ)}, {(B3, "Y/USD"): CHK})        # X closed on B2: same (empty) books
    assert during == [(B2, "entry_decision", ppc.EXPLAINED_DOWNSTREAM)]
    assert after == [(B3, "entry_decision", None)]


def test_bot_and_harness_position_timelines():
    bot = {(B1, "X/USD"): bot_bar(B1, "X/USD", CHK, entered=True), (B3, "X/USD"): bot_bar(B3, "X/USD", exit_reason="stop_loss"),
           (B2, "Y/USD"): bot_bar(B2, "Y/USD", CHK, entered=True)}
    iv = ppc._bot_intervals(bot)
    assert sorted(iv, key=lambda x: x[0]) == [("X/USD", B1, B3), ("Y/USD", B2, None)]
    assert ppc._held_before(iv, B1) == set() and ppc._held_before(iv, B2) == {"X/USD"}
    assert ppc._held_before(iv, B3) == {"X/USD", "Y/USD"} and ppc._held_before(iv, B4) == {"Y/USD"}


def test_a_fetch_failure_on_a_different_day_does_not_explain_a_mismatch():
    ev = ppc.Evidence(bars_with_events={B1, B2}, fetch_failed={(B2, "X/USD")})
    r = cmp({(B1, "Y/USD"): bot_bar(B1, "Y/USD")}, hres({(B1, "X/USD"): CHK}), evidence=ev)
    assert r["mismatches"][0].explained_by is None


# ----- --json-out failures and protections -----


def test_json_out_to_a_directory_or_under_a_file_is_exit_2_not_a_traceback(trial, tmp_path, capsys):
    directory = tmp_path / "adir"
    directory.mkdir()
    assert ppc.main(_argv(trial.dir, "--json-out", str(directory), "--force")) == ppc.EXIT_INPUT_ERROR
    assert capsys.readouterr().err.startswith("error: ")
    assert ppc.main(_argv(trial.dir, "--json-out", str(directory))) == ppc.EXIT_INPUT_ERROR  # exists, no --force
    regular = tmp_path / "afile"
    regular.write_text("x")
    assert ppc.main(_argv(trial.dir, "--json-out", str(regular / "sub" / "out.json"))) == ppc.EXIT_INPUT_ERROR
    assert capsys.readouterr().err.count("error: ") == 2


def test_force_never_overwrites_the_ack_file_dotenv_repo_config_or_the_kill_switch(trial, tmp_path, capsys):
    acks = tmp_path / "acks.txt"
    acks.write_text("# none\n")
    envfile = tmp_path / ".env"
    envfile.write_text("SECRET=1")
    for victim, extra in ((acks, ["--ack-file", str(acks)]), (envfile, []), (REPO / "config" / "config.yaml", []),
                          (REPO / ".env", []), (trial.dir / "NO_KILL_SWITCH", [])):
        before = victim.read_bytes() if victim.exists() else None
        assert ppc.main(_argv(trial.dir, "--json-out", str(victim), "--force", *extra)) == ppc.EXIT_INPUT_ERROR, victim
        assert (victim.read_bytes() if victim.exists() else None) == before, victim
        capsys.readouterr()


def test_the_default_kill_switch_path_is_protected_when_the_config_omits_the_key():
    paths = ppc.protected_paths({"logging": {"audit_log_path": "logs/a.jsonl"}, "risk": {}}, "acks.txt")
    assert (REPO / "KILL_SWITCH").resolve() in paths and (REPO / ".env").resolve() in paths
    assert (REPO / "config" / "config.yaml").resolve() in paths and Path("acks.txt").resolve() in paths
    assert (REPO / "logs" / "a.jsonl").resolve() in paths


# ----- ledger boundary, no-ledger note, narrow window -----


def test_ledger_exactly_at_the_newest_bar_is_fine_one_bar_ahead_is_not(work, capsys):
    ledger = json.loads((work / "bar_decisions.json").read_text())
    newest = max(pd.Timestamp(v) for v in ledger["bars"].values())
    ledger["bars"]["BTC/USD"] = newest.isoformat()
    (work / "bar_decisions.json").write_text(json.dumps(ledger))
    assert ppc.main(_argv(work, "--days", str(TRIAL_BARS))) == 0
    capsys.readouterr()
    ledger["bars"]["BTC/USD"] = (newest + pd.Timedelta(days=1)).isoformat()  # one symbol, one bar ahead
    (work / "bar_decisions.json").write_text(json.dumps(ledger))
    assert ppc.main(_argv(work)) == ppc.EXIT_INPUT_ERROR
    assert "BTC/USD" in capsys.readouterr().err


def test_exactly_one_missing_final_bar_is_detected_too(work, capsys):
    events = read_events(work)
    last = max(e["bar_close"] for e in events if "bar_close" in e)
    write_events(work, [e for e in events if e.get("bar_close") != last])
    assert ppc.main(_argv(work)) == ppc.EXIT_INPUT_ERROR
    assert "END of the audit log is missing" in capsys.readouterr().err


def test_no_ledger_prints_a_warn_note(work):
    (work / "bar_decisions.json").unlink()
    r = run_check(work, "--days", str(TRIAL_BARS))
    assert any(n.startswith("WARN: no bar-decision ledger") and "END of the audit log" in n for n in r["notes"])


def test_a_narrow_window_still_fails_on_an_unexplained_mismatch_earlier_in_the_trial(work):
    _plan_levels_fault(work)
    r = run_check(work, "--days", "2")
    assert r["status"] == ppc.FAIL and r["exit_code"] == 1 and "earlier in the trial still pause it" in r["message"]
    # without an unexplained mismatch the same window is INSUFFICIENT DATA (exit 0)


# ----- E. short / empty / corrupt inputs -----


def test_empty_audit_log_is_insufficient_data_exit_0(work, capsys):
    (work / "audit.jsonl").write_text("")
    assert ppc.main(_argv(work)) == 0
    out = capsys.readouterr().out
    assert "INSUFFICIENT DATA" in out and "insufficient data: the audit log is empty" in out


def test_audit_without_bar_close_is_insufficient_not_a_pass(work):
    events = [{k: v for k, v in e.items() if k != "bar_close"} for e in read_events(work)]
    write_events(work, events)
    report = run_check(work)
    assert report["status"] == ppc.INSUFFICIENT and "loop.bar_parity" in report["message"] and report["exit_code"] == 0


def test_short_window_is_insufficient_data(trial, work, capsys):
    report = run_check(trial.dir, "--days", "2")
    assert report["status"] == ppc.INSUFFICIENT and "need at least 3" in report["message"]
    assert run_check(trial.dir, "--days", "2", "--min-bars", "2")["status"] == ppc.PASS
    # a window with no decided bars at all
    r = run_check(trial.dir, "--since", "2031-01-01", "--until", "2031-01-05")
    assert r["status"] == ppc.INSUFFICIENT and r["exit_code"] == 0
    # only the first two bars of the trial exist
    events = [e for e in read_events(work) if "bar_close" not in e or pd.Timestamp(e["bar_close"]) <= T("2025-09-29")]
    write_events(work, events)
    (work / "bar_decisions.json").unlink()  # the ledger is (correctly) ahead of a truncated log
    (work / "trades.csv").write_text((work / "trades.csv").read_text().splitlines()[0] + "\n")  # and so are the later trades
    assert ppc.main(_argv(work)) == 0
    assert "insufficient data: only 2 decided bar(s)" in capsys.readouterr().out


def _expect_error(root, capsys, match, *extra):
    assert ppc.main(_argv(root, *extra)) == ppc.EXIT_INPUT_ERROR
    err = capsys.readouterr().err
    assert err.startswith("error: ") and match in err, err


def test_missing_audit_log_is_an_error_not_insufficient(work, capsys):
    (work / "audit.jsonl").unlink()
    _expect_error(work, capsys, "audit log not found")


def test_corrupt_audit_line_names_the_line(work, capsys):
    lines = (work / "audit.jsonl").read_text().splitlines()
    lines[41] = lines[41][:20]
    (work / "audit.jsonl").write_text("\n".join(lines) + "\n")
    _expect_error(work, capsys, "audit.jsonl:42: not valid JSON")


def test_audit_line_that_is_not_an_object(work, capsys):
    (work / "audit.jsonl").write_text("[1, 2]\n")
    _expect_error(work, capsys, "expected a JSON object")


def test_binary_garbage_audit(work, capsys):
    (work / "audit.jsonl").write_bytes(b"\xff\xfe\x00garbage\n")
    _expect_error(work, capsys, "audit")


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda e: e.update(bar_close="not a date"), "bar_close"),
        (lambda e: e.update(bar_close=12345), "bar_close"),
        (lambda e: e.update(symbol=None), "no symbol"),
        (lambda e: e.update(risk_decision=["allowed"]), "malformed risk_decision"),
        (lambda e: e.update(risk_decision={"reason": "x"}), "malformed risk_decision"),
    ],
)
def test_malformed_event_fields_are_errors(work, capsys, mutate, match):
    events = read_events(work)
    target = planned_events(events)[0]
    mutate(target)
    write_events(work, events)
    _expect_error(work, capsys, match, "--days", str(TRIAL_BARS))


def test_corrupt_ledger_trade_log_and_config(work, capsys):
    (work / "bar_decisions.json").write_text("{nope")
    _expect_error(work, capsys, "bar-decision ledger")
    (work / "bar_decisions.json").write_text(json.dumps({"timeframe": "1h", "bars": {}}))
    _expect_error(work, capsys, "timeframe '1h'")
    (work / "bar_decisions.json").write_text(json.dumps({"bars": {}}))
    _expect_error(work, capsys, "timeframe None")
    (work / "bar_decisions.json").write_text(json.dumps({"timeframe": "1d", "bars": {"BTC/USD": "garbage"}}))
    _expect_error(work, capsys, "BTC/USD")
    (work / "bar_decisions.json").unlink()  # absent ledger is fine
    assert ppc.main(_argv(work, "--days", str(TRIAL_BARS))) == 0
    capsys.readouterr()
    (work / "trades.csv").write_text("timestamp,symbol,side\n2025-01-01,BTC/USD,buy,EXTRA\n")
    _expect_error(work, capsys, "trade log")
    (work / "trades.csv").write_text("timestamp,symbol\n2025-01-01,BTC/USD\n")
    _expect_error(work, capsys, "malformed row")
    (work / "config.yaml").write_text("risk: [unclosed")
    _expect_error(work, capsys, "config")
    (work / "config.yaml").write_text("- a\n- list\n")
    _expect_error(work, capsys, "mapping")
    (work / "config.yaml").write_text("market: {timeframe: 1d}\n")
    _expect_error(work, capsys, "missing or malformed 'indicators'")


def test_wrong_timeframe_config_is_refused(work, capsys):
    raw = yaml.safe_load((work / "config.yaml").read_text())
    raw["market"]["timeframe"] = "1h"
    (work / "config.yaml").write_text(yaml.safe_dump(raw))
    _expect_error(work, capsys, "1d")


def test_missing_or_corrupt_bar_archive_is_an_error(work, capsys):
    path = work / "daily_bars" / "ETH_USD_1d.csv"
    good = path.read_text()
    path.write_text("not,a,bar,file\n1,2,3,4\n")
    _expect_error(work, capsys, "missing the required column 'timestamp'", "--days", str(TRIAL_BARS))
    last = good.splitlines()[-1].split(",")
    path.write_text(good.replace(",".join(last), ",".join(last[:4] + ["nan"] + last[5:])))  # close = NaN
    _expect_error(work, capsys, "(ETH/USD) has missing or non-finite", "--days", str(TRIAL_BARS))
    path.write_text("timestamp,open,high,low,close,volume\n")
    _expect_error(work, capsys, "(ETH/USD) has no bars", "--days", str(TRIAL_BARS))
    path.unlink()
    _expect_error(work, capsys, "no file for ETH/USD", "--days", str(TRIAL_BARS))
    shutil.rmtree(work / "daily_bars")
    _expect_error(work, capsys, "archive directory not found", "--days", str(TRIAL_BARS))


def test_archive_missing_a_decided_bar_is_an_error(work, capsys):
    path = work / "daily_bars" / "ETH_USD_1d.csv"
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n")  # drop the newest bar
    _expect_error(work, capsys, "no bar closing", "--days", str(TRIAL_BARS))


def test_no_archive_configured_is_an_error(work, capsys):
    raw = yaml.safe_load((work / "config.yaml").read_text())
    raw["logging"]["closed_bars_dir"] = None
    (work / "config.yaml").write_text(yaml.safe_dump(raw))
    argv = ["--config", str(work / "config.yaml"), "--audit-log", str(work / "audit.jsonl"), "--trade-log", str(work / "trades.csv"),
            "--bar-decisions", str(work / "bar_decisions.json"), "--days", str(TRIAL_BARS)]
    assert ppc.main(argv) == ppc.EXIT_INPUT_ERROR
    assert "closed_bars_dir" in capsys.readouterr().err


def test_invalid_cli_values_are_rejected():
    for bad in (["--days", "0"], ["--budget", "2"], ["--min-bars", "0"], ["--format", "xml"]):
        with pytest.raises(SystemExit):
            ppc.parse_args(bad)


# ----- a hand-made world: vacuous PASS (no signals anywhere) -----


def test_quiet_market_is_a_vacuous_pass_not_a_failure(tmp_path):
    n = 230
    open_ms = [1_700_000_000_000 - (1_700_000_000_000 % D_MS) + i * D_MS for i in range(n)]
    close = [100.0 + (0.05 if i % 2 else -0.05) for i in range(n)]
    bars = tmp_path / "daily_bars"
    bars.mkdir()
    (bars / "AAA_USD_1d.csv").write_text(
        "timestamp,open,high,low,close,volume\n"
        + "".join(f"{t},{c!r},{c + 0.1!r},{c - 0.1!r},{c!r},10.0\n" for t, c in zip(open_ms, close))
    )
    raw = load_config().raw
    raw["market"].update(symbols=["AAA/USD"], timeframe="1d")
    raw["logging"] = {"audit_log_path": str(tmp_path / "audit.jsonl"), "trade_log_path": str(tmp_path / "trades.csv"),
                      "bar_decisions_path": str(tmp_path / "ledger.json"), "closed_bars_dir": str(bars)}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(raw))
    events = [
        {"symbol": "AAA/USD", "mode": "paper", "timestamp": "2026-01-01T00:01:00+00:00", "action": "none",
         "bar_close": pd.Timestamp(t + D_MS, unit="ms", tz="UTC").isoformat(), "candidates": []}
        for t in open_ms[-5:]
    ]
    write_events(tmp_path, events)
    report = ppc.build_report(ppc.parse_args(["--config", str(tmp_path / "config.yaml")]))
    assert report["status"] == ppc.PASS and report["vacuous"] is True and report["decisions_compared"] == 0
    assert "nothing to compare" in report["message"]
    assert ppc.format_text(report).splitlines()[0] == "PAPER TRIAL PARITY CHECK -- PASS"


# --------------------------------------------------------------------------- #
# A. helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("symbol", ["BTC/USD", "SHIB/USDT", "A/B", "WEIRD:1/USD", "x y/USD"])
def test_archive_file_naming_matches_the_bots(symbol, tmp_path):
    assert ppc.closed_bars_file(tmp_path, symbol, "1d") == bot._closed_bars_file(tmp_path, symbol, "1d")


CAP = "new-entry rate cap hit: 5 new entries already approved across all symbols in the last 120 minutes (max 5)"
EQUIVALENCE_CASES = [
    # (harness reason, bot reason)
    (CAP + "; next slot opens in 0 days 02:00:00", CAP + "; next slot opens in 1:59:00"),
    ("market volatility too high (9.00% > max 8.00%); skipping", "market volatility too high (9.00% > max 8.00%); skipping"),
    ("market volatility too high (9.00% > max 8.00%); skipping", "market volatility too high (7.00% > max 8.00%); skipping"),
    ("cooldown active for X/USD: 0 days 00:59:00 remaining", "cooldown active for X/USD: 0:58:00 remaining"),
    ("cooldown active for X/USD: 1:00:00 remaining", "cooldown active for Y/USD: 1:00:00 remaining"),
    ("no room left to size this trade (cap 0.4)", "sized order 9.094947e-13 is below the minimum order size 0.01 (x)"),
    ("no room left to size this trade", "sized order 5.0 is below the minimum order size 0.01"),
    ("kill switch file present at /a", "kill switch file present at /b"),
]


@pytest.mark.parametrize("h, b", EQUIVALENCE_CASES)
def test_reason_equivalence_matches_the_daily_live_parity_tests(h, b):
    base = dict(allowed=False, equity=1.0, entry=1.0, stop=0.9, rr=2.0, quote=None)
    assert ppc.reasons_equivalent(h, b) == same_decision({**base, "reason": h}, {**base, "reason": b})


def test_sized_order_amount_is_ignored_but_the_minimum_is_not():
    """Deliberate loosening of same_decision (B1): the leftover room that was sized differs by the fill-price
    residual between paper (close) and harness (next open); the decision is 'below the configured minimum'."""
    a = "sized order 0.4263005 is below the minimum order size 10 (min_order_quote_usd); rejected, not resized up"
    b = "sized order 0.22184695 is below the minimum order size 10 (min_order_quote_usd); rejected, not resized up"
    assert ppc.reasons_equivalent(a, b)
    assert not ppc.reasons_equivalent(a, b.replace("size 10", "size 25"))
    assert not ppc.reasons_equivalent(a, "market volatility too high (9.00% > max 8.00%); skipping")
    # other numbers in reasons are NOT normalised
    v = "market volatility too high (9.00% > max 8.00%); skipping"
    assert not ppc.reasons_equivalent(v, v.replace("9.00", "7.00"))


def test_reason_key_cuts_at_the_first_semicolon():
    assert ppc._reason_key("a; b; c") == "a"
    assert ppc._reason_key("cooldown active for X: 1:00:00 remaining") == "cooldown active for X"


def test_numeric_helpers():
    assert ppc._num("1.5") == 1.5 and ppc._num(None) is None and ppc._num(float("nan")) is None and ppc._num("x") is None
    assert ppc._close(1.0, 1.0 + 1e-7, 1e-6) and not ppc._close(1.0, 1.0 + 1e-5, 1e-6)
    assert ppc._close(None, None, 1e-6) and not ppc._close(None, 1.0, 1e-6)


# --------------------------------------------------------------------------- #
# B. compare() on hand-made decisions
# --------------------------------------------------------------------------- #
B1, B2, B3, B4 = (T(f"2026-03-0{d}T00:00:00") for d in (1, 2, 3, 4))
CHK = dict(allowed=True, reason="ok", quote=500.0, entry=100.0, stop=95.0, rr=2.0)
REJ = dict(allowed=False, reason="market volatility too high (9.00% > max 8.00%); skipping", quote=None, entry=100.0, stop=95.0, rr=2.0)


def bot_bar(bar, symbol, check=None, **kw) -> ppc.BotBar:
    info = ppc.BotBar(bar=bar, symbol=symbol, check=check, n_decision_events=kw.pop("n_decision_events", 1), n_events=1)
    info.n_checks = 1 if check is not None else 0
    info.planned = kw.pop("planned", bool(check and check["allowed"]))
    for k, v in kw.items():
        setattr(info, k, v)
    return info


def hres(checks=None, exits=None, last=None) -> ppc.HarnessResult:
    return ppc.HarnessResult(checks=checks or {}, exits=exits or set(), last_bar=last or {"X/USD": B4, "Y/USD": B4}, n_bars=4)


def cmp(bot_map, h, since=B1, until=B4, **kw):
    return ppc.compare(bot_map, h, since, until, level_rtol=kw.get("level_rtol", 1e-6),
                       size_rtol=kw.get("size_rtol", ppc.DEFAULT_SIZE_RTOL), evidence=kw.get("evidence"))


def kinds(result):
    return [(m.kind, m.symbol, m.explained_by) for m in result["mismatches"]]


def test_identical_decisions_have_no_mismatches():
    h = hres({(B1, "X/USD"): CHK, (B2, "X/USD"): REJ}, {(B3, "X/USD", "stop_loss")})
    b = {(B1, "X/USD"): bot_bar(B1, "X/USD", CHK), (B2, "X/USD"): bot_bar(B2, "X/USD", REJ),
         (B3, "X/USD"): bot_bar(B3, "X/USD", exit_reason="stop_loss")}
    r = cmp(b, h)
    assert r["mismatches"] == [] and r["H_entries"] == r["B_entries"] == {(B1, "X/USD")}


def test_rejection_text_after_the_semicolon_is_not_a_mismatch_but_before_it_is():
    cap = dict(REJ, reason=CAP + "; next slot opens in 0 days 02:00:00")
    cap_b = dict(REJ, reason=CAP + "; next slot opens in 1:59:00")
    assert cmp({(B1, "X/USD"): bot_bar(B1, "X/USD", cap_b)}, hres({(B1, "X/USD"): cap}))["mismatches"] == []
    other = dict(REJ, reason="cooldown active for X/USD: 1:00:00 remaining")
    r = cmp({(B1, "X/USD"): bot_bar(B1, "X/USD", other)}, hres({(B1, "X/USD"): cap}))
    assert kinds(r) == [("rejection_reason", "X/USD", None)]


def test_allowed_vs_rejected_is_an_entry_decision_mismatch_both_ways():
    r = cmp({(B1, "X/USD"): bot_bar(B1, "X/USD", REJ)}, hres({(B1, "X/USD"): CHK}))
    assert kinds(r) == [("entry_decision", "X/USD", None)]
    r = cmp({(B1, "X/USD"): bot_bar(B1, "X/USD", CHK)}, hres({(B1, "X/USD"): REJ}))
    assert kinds(r) == [("entry_decision", "X/USD", None)]


def test_plan_levels_tolerance():
    near = dict(CHK, entry=100.0 * (1 + 5e-7), stop=95.0 * (1 - 5e-7))
    far = dict(CHK, stop=95.0 * (1 + 1e-4))
    b = lambda c: {(B1, "X/USD"): bot_bar(B1, "X/USD", c)}  # noqa: E731
    assert cmp(b(near), hres({(B1, "X/USD"): CHK}))["mismatches"] == []
    assert kinds(cmp(b(far), hres({(B1, "X/USD"): CHK}))) == [("plan_levels", "X/USD", None)]
    assert cmp(b(far), hres({(B1, "X/USD"): CHK}), level_rtol=1e-3)["mismatches"] == []


def test_size_tolerance_and_phantoms():
    b = lambda c: {(B1, "X/USD"): bot_bar(B1, "X/USD", c)}  # noqa: E731
    assert cmp(b(dict(CHK, quote=520.0)), hres({(B1, "X/USD"): CHK}))["mismatches"] == []
    assert kinds(cmp(b(dict(CHK, quote=900.0)), hres({(B1, "X/USD"): CHK}))) == [("size", "X/USD", None)]
    # an approved quote below 1e-6 USD is a float phantom, whichever side has it
    ph = dict(CHK, quote=9e-13)
    r = cmp(b(CHK), hres({(B1, "X/USD"): REJ | {"allowed": False}}))
    assert kinds(r) == [("entry_decision", "X/USD", None)]
    r = cmp(b(ph), hres({(B1, "X/USD"): REJ}))
    assert kinds(r) == [("entry_decision", "X/USD", ppc.EXPLAINED_PHANTOM)]
    assert cmp(b(ph), hres({(B1, "X/USD"): dict(CHK, quote=1e-12)}))["mismatches"] == []


def test_one_sided_checks():
    r = cmp({(B1, "X/USD"): bot_bar(B1, "X/USD", None, note="blocked: daily trend not confirmed bullish (higher-timeframe filter)")},
            hres({(B1, "X/USD"): CHK}))
    [m] = r["mismatches"]
    assert m.kind == "harness_only_check" and m.explained_by is None and "daily trend not confirmed" in m.detail
    r = cmp({(B1, "X/USD"): bot_bar(B1, "X/USD", CHK)}, hres({}))
    assert kinds(r) == [("bot_only_check", "X/USD", None)]


def test_explanations():
    def one(info, h=CHK):
        return cmp({(B1, "X/USD"): info} if info else {}, hres({(B1, "X/USD"): h}))["mismatches"][0]
    assert one(bot_bar(B1, "X/USD", None, stale=True, note="stale bar: entries skipped")).explained_by == ppc.EXPLAINED_STALE
    assert one(None).explained_by == ppc.EXPLAINED_GAP
    # the bar has events for other symbols, or only 'already decided' echoes for this one: NOT a gap
    assert one(bot_bar(B1, "X/USD", None, n_decision_events=0)).explained_by is None
    kill = dict(REJ, reason="kill switch file present at KILL_SWITCH")
    assert one(bot_bar(B1, "X/USD", kill)).explained_by == ppc.EXPLAINED_KILL
    assert one(bot_bar(B1, "X/USD", None, held_note="OPEN plan active for X/USD, no trigger this cycle")).explained_by is None
    assert "bot error: boom" in one(bot_bar(B1, "X/USD", None, error="boom")).detail
    assert one(bot_bar(B1, "X/USD", None, error="boom")).explained_by is None  # an exception is never "explained"


def test_advisor_veto_is_explained_but_a_silent_non_plan_is_not():
    veto = bot_bar(B1, "X/USD", CHK, planned=False, advisor_veto=True)
    assert kinds(cmp({(B1, "X/USD"): veto}, hres({(B1, "X/USD"): CHK}))) == [("advisor_veto", "X/USD", ppc.EXPLAINED_ADVISOR)]
    silent = bot_bar(B1, "X/USD", CHK, planned=False)
    assert kinds(cmp({(B1, "X/USD"): silent}, hres({(B1, "X/USD"): CHK}))) == [("advisor_veto", "X/USD", None)]
    crashed = bot_bar(B1, "X/USD", CHK, planned=False, error="broker down")
    assert kinds(cmp({(B1, "X/USD"): crashed}, hres({(B1, "X/USD"): CHK}))) == [("bot_error", "X/USD", None)]


def test_downstream_follows_an_explained_mismatch_only_and_never_hides_integrity_kinds():
    stale = bot_bar(B1, "X/USD", None, stale=True)
    later = bot_bar(B3, "Y/USD", REJ)
    dup = bot_bar(B4, "Y/USD", CHK)
    dup.n_checks = 2
    r = cmp({(B1, "X/USD"): stale, (B3, "Y/USD"): later, (B4, "Y/USD"): dup}, hres({(B1, "X/USD"): CHK, (B3, "Y/USD"): CHK, (B4, "Y/USD"): CHK}))
    assert kinds(r) == [
        ("harness_only_check", "X/USD", ppc.EXPLAINED_STALE),
        ("entry_decision", "Y/USD", ppc.EXPLAINED_DOWNSTREAM),
        ("duplicate_decision", "Y/USD", None),
    ]
    # the same later mismatch with nothing explained before it stays unexplained
    r = cmp({(B3, "Y/USD"): later}, hres({(B3, "Y/USD"): CHK}))
    assert kinds(r) == [("entry_decision", "Y/USD", None)]


def test_exit_mismatch_kinds():
    h = hres(exits={(B2, "X/USD", "stop_loss"), (B3, "Y/USD", "take_profit")})
    b = {(B2, "X/USD"): bot_bar(B2, "X/USD", exit_reason="take_profit"), (B4, "Y/USD"): bot_bar(B4, "Y/USD", exit_reason="stop_loss"),
         (B3, "Y/USD"): bot_bar(B3, "Y/USD")}
    r = cmp(b, h)
    assert kinds(r) == [("exit_harness_only", "X/USD", None), ("exit_harness_only", "Y/USD", None), ("exit_bot_only", "Y/USD", None)]
    assert "harness exits on stop_loss; bot exits on take_profit" in r["mismatches"][0].detail
    # no bot events at all on the bar: a gap
    r = cmp({}, hres(exits={(B2, "X/USD", "stop_loss")}))
    assert kinds(r) == [("exit_harness_only", "X/USD", ppc.EXPLAINED_GAP)]


def test_window_and_last_bar_filtering():
    h = hres({(B1, "X/USD"): CHK, (B4, "X/USD"): CHK}, last={"X/USD": B3})
    # B4 is beyond the symbol's last real bar (the padding bar): never compared
    assert cmp({(B1, "X/USD"): bot_bar(B1, "X/USD", CHK)}, h)["mismatches"] == []
    # outside [since, until]: ignored on both sides
    assert cmp({(B1, "X/USD"): bot_bar(B1, "X/USD", REJ)}, hres({(B1, "X/USD"): CHK}), since=B2)["mismatches"] == []
    assert cmp({(B3, "X/USD"): bot_bar(B3, "X/USD", REJ)}, hres({(B3, "X/USD"): CHK}), until=B2)["mismatches"] == []


def test_summarize_window_vs_trial():
    h = hres({(B1, "X/USD"): CHK, (B3, "X/USD"): CHK})
    b = {(B1, "X/USD"): bot_bar(B1, "X/USD", REJ), (B3, "X/USD"): bot_bar(B3, "X/USD", CHK)}
    c = cmp(b, h)
    week = ppc.summarize(c, c["mismatches"], B3)
    trial = ppc.summarize(c, c["mismatches"], B1)
    assert (week["mismatch_count"], week["entries"]["harness"], week["entries"]["mismatch_rate"]) == (0, 1, 0.0)
    assert (trial["mismatch_count"], trial["entries"]["harness"], trial["entries"]["mismatch_rate"]) == (1, 2, 0.5)
    assert trial["entries"]["agreement_rate"] == 0.5 and trial["decisions_compared"] == 2


def test_side_stats_edges():
    assert ppc.side_stats(set(), set()) == {"harness": 0, "bot": 0, "matched": 0, "mismatched": 0, "agreement_rate": None, "mismatch_rate": 0.0}
    s = ppc.side_stats({1, 2, 3, 4}, {1, 2, 3, 5})
    assert s["matched"] == 3 and s["mismatched"] == 2 and s["mismatch_rate"] == 0.5 and s["agreement_rate"] == 0.6


def test_collect_bot_groups_events_and_ignores_already_decided_echoes():
    bar = "2026-03-01T00:00:00+00:00"
    events = [
        {"symbol": "X/USD", "bar_close": bar, "action": "planned", "risk_decision": {"allowed": True, "reason": "ok", "max_quote_amount": 5.0},
         "plan": {"entry_zone": [1.0, 2.0], "stop_loss": 0.5, "risk_reward": 2.0}, "_line": 1},
        {"symbol": "X/USD", "bar_close": bar, "action": "none", "note": f"bar closing {bar} already decided for X/USD; next", "_line": 2},
        {"symbol": "Y/USD", "bar_close": bar, "action": "none", "note": "OPEN plan active for Y/USD, no trigger this cycle", "_line": 3},
        {"symbol": "Z/USD", "action": "none", "error": "market data unavailable this cycle", "_line": 4},
    ]
    groups, bars = ppc.collect_bot(events)
    assert bars == [T("2026-03-01")] and len(groups) == 2
    x = groups[(T("2026-03-01"), "X/USD")]
    assert x.n_events == 2 and x.n_decision_events == 1 and x.n_checks == 1 and x.planned
    assert x.check == dict(allowed=True, reason="ok", quote=5.0, entry=2.0, stop=0.5, rr=2.0, pattern=None)
    assert groups[(T("2026-03-01"), "Y/USD")].held_note is not None
