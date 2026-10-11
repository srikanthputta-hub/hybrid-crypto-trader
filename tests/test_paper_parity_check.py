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

# 16 bars starting at data bar 330 (2025-09-08): 8 entries and 6 stop/target exits across 16 symbols,
# including exits decided on the final bar (so the checker's trailing-bar padding is exercised).
TRIAL_START_BAR = 330
TRIAL_BARS = 16
LABEL = "PAPER TRIAL"
DELAY_MINUTES = 60


def T(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


# --------------------------------------------------------------------------- #
# C. the generated fixture
# --------------------------------------------------------------------------- #
def _generate_trial(directory: Path) -> SimpleNamespace:
    """Runs the real bot loop and returns where its logs landed and which Telegram messages it sent."""
    mp = pytest.MonkeyPatch()
    messages: list[str] = []
    try:
        directory.mkdir(parents=True, exist_ok=True)
        rows, axis = load_rows(TRIAL_START_BAR + TRIAL_BARS + 1, gapless=False)
        raw = copy.deepcopy(load_config().raw)
        raw["mode"] = "paper"
        raw["advisor"] = {"enabled": False}
        raw["market"].update(symbols=sorted(rows), timeframe="1d", history_bars=200)
        # A $10 order floor, as in tests/test_daily_live_parity.py's realistic-config run: the committed
        # floor of 0 produces known float-phantom divergences (docs/bot_bar_parity.md §4.2).
        raw["risk"]["min_order_quote_usd"] = 10.0
        raw["risk"]["kill_switch_file"] = str(directory / "NO_KILL_SWITCH")
        raw["loop"] = {"poll_interval_seconds": 300, "bar_parity": True, "max_entry_delay_minutes": DELAY_MINUTES}
        raw["notifier"] = {"label": LABEL}
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
        for ts in axis[TRIAL_START_BAR:TRIAL_START_BAR + TRIAL_BARS]:
            for k in range(2):
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
    first = ppc._clean(run_check(trial.dir, "--days", "16"))
    second = ppc._clean(run_check(trial.dir, "--days", "16"))
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
    assert run_check(root, "--days", "16")["status"] == ppc.PASS


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
    assert ppc.main(_argv(trial.dir, "--days", "16")) == 0
    out = capsys.readouterr().out
    assert out.startswith("PAPER TRIAL PARITY CHECK -- PASS")
    assert "budget 10%" in out and "an unexplained mismatch pauses the trial" in out and "trial to date" in out
    assert ppc.main(_argv(trial.dir, "--days", "16", "--format", "json")) == 0
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

    ok = run(trial.dir, "--days", "16")
    assert ok.returncode == 0 and "-- PASS" in ok.stdout, ok.stderr
    events = read_events(work)
    flipped = planned_events(events)[0]
    flipped["risk_decision"]["allowed"] = False
    flipped["action"] = "none"
    write_events(work, events)
    bad = run(work, "--days", "16")
    assert bad.returncode == 1 and "-- FAIL" in bad.stdout and "UNEXPLAINED" in bad.stdout
    (work / "audit.jsonl").write_text("{not json\n")
    err = run(work)
    assert err.returncode == 2 and err.stdout == "" and "audit.jsonl:1" in err.stderr


# ----- D. mutated copies: each fault is reported -----


def test_a_flipped_entry_decision_fails_and_is_listed_with_both_sides(work):
    events = read_events(work)
    target = planned_events(events)[0]
    target["risk_decision"] = {"allowed": False, "reason": "position would exceed the cap", "max_quote_amount": None}
    target["action"] = "none"
    target.pop("execution", None)
    write_events(work, events)
    report = run_check(work, "--days", "16")
    assert report["status"] == ppc.FAIL and report["exit_code"] == 1
    first = report["mismatches"][0]
    assert (first["kind"], first["symbol"], first["bar"]) == ("entry_decision", target["symbol"], pd.Timestamp(target["bar_close"]).isoformat())
    assert first["explained_by"] is None
    assert first["harness"]["allowed"] is True and first["bot"]["allowed"] is False
    assert "position would exceed the cap" in first["detail"]
    assert report["unexplained_count"] >= 1 and "unexplained" in report["message"]


def test_a_missing_exit_fails(work):
    events = read_events(work)
    target = exit_events(events)[0]
    target.pop("monitor_action")
    target.pop("execution")
    write_events(work, events)
    report = run_check(work, "--days", "16")
    assert report["status"] == ppc.FAIL
    kinds = {(m["kind"], m["symbol"]) for m in report["mismatches"]}
    assert ("exit_harness_only", target["symbol"]) in kinds


def test_a_wrong_exit_reason_fails(work):
    events = read_events(work)
    target = exit_events(events)[0]
    old = target["monitor_action"]["action"]
    target["monitor_action"]["action"] = "TAKE_PROFIT" if old == "STOP_LOSS" else "STOP_LOSS"
    write_events(work, events)
    report = run_check(work, "--days", "16")
    assert report["status"] == ppc.FAIL
    assert any(m["kind"] == "exit_harness_only" and "bot exits on" in m["detail"] for m in report["mismatches"])


def test_a_staleness_skip_is_explained_but_counts_against_the_budget(work):
    events = read_events(work)
    target = planned_events(events)[0]
    for key in ("risk_decision", "plan", "signal", "execution", "candidates", "daily_trend_confirmed"):
        target.pop(key, None)
    target["action"], target["note"], target["entry_delay_minutes"] = "none", "stale bar: entries skipped", 75.0
    write_events(work, events)
    # Default 10% budget: one missed entry out of ~8 is 12.5% -> FAIL, naming the budget.
    strict = run_check(work, "--days", "16")
    assert strict["status"] == ppc.FAIL and "exceeds the 10% budget" in strict["message"]
    first = strict["mismatches"][0]
    assert first["explained_by"] == ppc.EXPLAINED_STALE and first["symbol"] == target["symbol"]
    assert strict["unexplained_count"] == 0  # nothing unexplained: later fallout is "downstream"
    assert all(m["explained_by"] for m in strict["mismatches"])
    # With a looser budget the same logs are a WARN (exit 0): explained, listed, not hidden.
    loose = run_check(work, "--days", "16", "--budget", "0.5")
    assert loose["status"] == ppc.WARN and loose["exit_code"] == 0 and loose["mismatch_count"] >= 1


def test_downstream_label_only_follows_an_explained_mismatch(work):
    events = read_events(work)
    for target in exit_events(events)[:1]:  # an unexplained first mismatch must never be relabelled
        target.pop("monitor_action")
        target.pop("execution")
    write_events(work, events)
    report = run_check(work, "--days", "16", "--budget", "1.0")
    assert report["status"] == ppc.FAIL
    assert report["mismatches"][0]["explained_by"] is None
    assert not any(m["explained_by"] == ppc.EXPLAINED_DOWNSTREAM for m in report["mismatches"][:1])


def test_a_bar_with_no_bot_events_is_a_bot_gap(work):
    events = read_events(work)
    target = planned_events(events)[0]
    key = (target["bar_close"], target["symbol"])
    events = [e for e in events if (e.get("bar_close"), e.get("symbol")) != key]
    write_events(work, events)
    report = run_check(work, "--days", "16", "--budget", "1.0")
    first = report["mismatches"][0]
    assert first["kind"] == "harness_only_check" and first["explained_by"] == ppc.EXPLAINED_GAP
    assert report["unexplained_count"] == 0 and report["status"] == ppc.WARN


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
    report = run_check(work, "--days", "16")
    assert report["status"] == ppc.FAIL and report["mismatches"]


def test_a_bar_decided_twice_is_an_unexplained_integrity_failure(work):
    events = read_events(work)
    target = planned_events(events)[0]
    events.insert(events.index(target) + 1, copy.deepcopy(target))
    write_events(work, events)
    report = run_check(work, "--days", "16", "--budget", "1.0")
    assert report["status"] == ppc.FAIL
    dup = [m for m in report["mismatches"] if m["kind"] == "duplicate_decision"]
    assert len(dup) == 1 and dup[0]["explained_by"] is None and "P1" in dup[0]["detail"]


def test_trade_log_and_audit_log_must_agree(work):
    lines = (work / "trades.csv").read_text().splitlines()
    (work / "trades.csv").write_text("\n".join([lines[0]] + lines[2:]) + "\n")  # drop the first fill
    report = run_check(work, "--days", "16", "--budget", "1.0")
    assert report["status"] == ppc.FAIL
    assert any(m["kind"] == "audit_trade_log" for m in report["mismatches"])


def test_a_trade_log_row_without_an_audit_fill_is_caught(work):
    lines = (work / "trades.csv").read_text().splitlines()
    fields = lines[1].split(",")
    # same symbol and side, a microsecond later: inside the audited wall-clock span, but never audited
    fields[0] = (pd.Timestamp(fields[0]) + pd.Timedelta(microseconds=1)).isoformat()
    (work / "trades.csv").write_text("\n".join(lines + [",".join(fields)]) + "\n")
    report = run_check(work, "--days", "16", "--budget", "1.0")
    assert any(m["kind"] == "trade_log_audit" for m in report["mismatches"])
    assert report["status"] == ppc.FAIL


def test_a_mismatch_outside_the_window_is_not_this_weeks_failure(work):
    events = read_events(work)
    target = planned_events(events)[0]  # early in the trial
    target["risk_decision"] = {"allowed": False, "reason": "position would exceed the cap", "max_quote_amount": None}
    target["action"] = "none"
    target.pop("execution", None)
    write_events(work, events)
    bar = pd.Timestamp(target["bar_close"])
    assert bar < pd.Timestamp(max(e["bar_close"] for e in events if "bar_close" in e)) - pd.Timedelta(days=5)
    # last 3 bars only: the early mismatch is not in the window ...
    late = run_check(work, "--days", "3", "--budget", "1.0")
    assert late["mismatch_count"] == 0 and late["mismatches"] == []
    assert late["trial_to_date"]["unexplained_count"] >= 1  # ... but is still in the trial record
    assert any("from before this window" in n for n in late["notes"])
    assert late["status"] == ppc.PASS
    # ... and it still counts against the cumulative budget.
    assert run_check(work, "--days", "3")["status"] == ppc.FAIL
    # The window that contains it reports it.
    assert run_check(work, "--since", bar.isoformat(), "--until", bar.isoformat(), "--min-bars", "1", "--budget", "1.0")["status"] == ppc.FAIL


def test_window_bounds_select_bars(trial):
    r = run_check(trial.dir, "--since", "2025-09-12T00:00:00+00:00", "--until", "2025-09-15T00:00:00+00:00")
    assert (r["window"]["since"], r["window"]["until"], r["window"]["decided_bars"]) == (
        "2025-09-12T00:00:00+00:00", "2025-09-15T00:00:00+00:00", 4)
    # a window before the trial started is clamped to the trial start
    r = run_check(trial.dir, "--since", "2025-01-01", "--until", "2025-09-12T00:00:00+00:00", "--min-bars", "1")
    assert r["window"]["since"].startswith("2025-09-08") and any("clamped" in n for n in r["notes"])


def test_an_explicit_trial_start_changes_the_replay(work):
    # Starting the replay later than the bot started: the harness has no positions from before, so exits of
    # the bot's early positions are bot-only mismatches.
    later = run_check(work, "--days", "16", "--trial-start", "2025-09-14T00:00:00+00:00", "--budget", "1.0")
    assert later["status"] == ppc.FAIL
    assert any(m["kind"] in ("exit_bot_only", "bot_only_check") for m in later["mismatches"])


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
    events = [e for e in read_events(work) if "bar_close" not in e or pd.Timestamp(e["bar_close"]) <= T("2025-09-09")]
    write_events(work, events)
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
    _expect_error(work, capsys, match, "--days", "16")


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
    assert ppc.main(_argv(work, "--days", "16")) == 0
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
    _expect_error(work, capsys, "missing the required column 'timestamp'", "--days", "16")
    last = good.splitlines()[-1].split(",")
    path.write_text(good.replace(",".join(last), ",".join(last[:4] + ["nan"] + last[5:])))  # close = NaN
    _expect_error(work, capsys, "(ETH/USD) has missing or non-finite", "--days", "16")
    path.write_text("timestamp,open,high,low,close,volume\n")
    _expect_error(work, capsys, "(ETH/USD) has no bars", "--days", "16")
    path.unlink()
    _expect_error(work, capsys, "no file for ETH/USD", "--days", "16")
    shutil.rmtree(work / "daily_bars")
    _expect_error(work, capsys, "archive directory not found", "--days", "16")


def test_archive_missing_a_decided_bar_is_an_error(work, capsys):
    path = work / "daily_bars" / "ETH_USD_1d.csv"
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n")  # drop the newest bar
    _expect_error(work, capsys, "no bar closing", "--days", "16")


def test_no_archive_configured_is_an_error(work, capsys):
    raw = yaml.safe_load((work / "config.yaml").read_text())
    raw["logging"]["closed_bars_dir"] = None
    (work / "config.yaml").write_text(yaml.safe_dump(raw))
    argv = ["--config", str(work / "config.yaml"), "--audit-log", str(work / "audit.jsonl"), "--trade-log", str(work / "trades.csv"),
            "--bar-decisions", str(work / "bar_decisions.json"), "--days", "16"]
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
    ("sized order 1e-13 is below the minimum order size 0.01", "sized order 2e-13 is below the minimum order size 0.01"),
    ("kill switch file present at /a", "kill switch file present at /b"),
]


@pytest.mark.parametrize("h, b", EQUIVALENCE_CASES)
def test_reason_equivalence_matches_the_daily_live_parity_tests(h, b):
    base = dict(allowed=False, equity=1.0, entry=1.0, stop=0.9, rr=2.0, quote=None)
    assert ppc.reasons_equivalent(h, b) == same_decision({**base, "reason": h}, {**base, "reason": b})


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
    return ppc.compare(bot_map, h, since, until, level_rtol=kw.get("level_rtol", 1e-6), size_rtol=kw.get("size_rtol", 0.25))


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
    assert one(bot_bar(B1, "X/USD", None, n_decision_events=0)).explained_by == ppc.EXPLAINED_GAP
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
    assert x.check == dict(allowed=True, reason="ok", quote=5.0, entry=2.0, stop=0.5, rr=2.0)
    assert groups[(T("2026-03-01"), "Y/USD")].held_note is not None
