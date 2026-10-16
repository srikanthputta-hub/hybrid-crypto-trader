"""scripts/paper_parity_check.py streams the audit log instead of holding every line as a parsed dict.

The contract is "the same report, in bounded memory". The "before" is a frozen copy of origin/main's script
(tests/_legacy_paper_parity_check.py: reads the whole log into a list of dicts, ~1.1 KB a line). Old and new are
run on the same inputs and compared byte for byte (the cleaned JSON report, the text report) -- or, when the
input is bad, compared on the exact error message and line number:

  A. the pieces: `scan_audit` groups / evidence against the legacy `read_audit` + `collect_bot` + `check_trade_log`;
  B. whole reports on the generated trial fixtures (the real bot loop's logs) and on mutated copies of them,
     including the integrity faults that are found ONLY through the "already decided" echoes;
  C. a long synthetic log: the trial's decisions followed by a hundred-odd echo ticks per bar and symbol, with
     monotonic and with interleaved wall-clock stamps;
  D. error precedence, and peak memory.

tests/test_paper_parity_check.py is untouched and passes against the new code as it stands.
"""
from __future__ import annotations

import copy
import json
import random
import shutil
import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from scripts import paper_parity_check as new
import tests._legacy_paper_parity_check as old
from tests.test_paper_parity_check import (  # noqa: F401  (the autouse fixture speeds up the shared harness pass)
    TRIAL_BARS,
    _argv,
    _generate_trial,
    _memoize_harness_signals,
    planned_events,
    read_events,
    write_events,
)


def outcome(module, root: Path, *extra: str):
    """Everything an operator would see: the cleaned JSON report and the text report, or the error message."""
    try:
        report = module.build_report(new.parse_args(_argv(root, *extra)))
    except module.ParityInputError as exc:
        return ("error", str(exc))
    return ("report", json.dumps(module._clean(report), sort_keys=True, allow_nan=False, default=str), module.format_text(report))


def assert_same(root: Path, *extra: str):
    before, after = outcome(old, root, *extra), outcome(new, root, *extra)
    assert after == before
    return after


@pytest.fixture(scope="module")
def trial(tmp_path_factory):
    return _generate_trial(tmp_path_factory.mktemp("trial"))


@pytest.fixture
def work(trial, tmp_path):
    dest = tmp_path / "work"
    shutil.copytree(trial.dir, dest)
    return dest


# --------------------------------------------------------------------------- #
# A. the pieces
# --------------------------------------------------------------------------- #
def bot_view(info) -> dict:
    d = {k: getattr(info, k) for k in info.__dataclass_fields__}
    d["lines"] = list(info.lines)
    return d


def test_read_audit_is_unchanged(trial):
    assert new.read_audit(trial.dir / "audit.jsonl") == old.read_audit(trial.dir / "audit.jsonl")


def test_scan_groups_equal_collect_bot_on_the_full_event_list(trial):
    events = old.read_audit(trial.dir / "audit.jsonl")
    groups, bars = old.collect_bot(events)
    scan = new.scan_audit(trial.dir / "audit.jsonl")
    assert scan.all_bars == bars and scan.n_events == len(events)
    assert scan.bot.keys() == groups.keys()
    for key in groups:
        assert bot_view(scan.bot[key]) == bot_view(groups[key]), key
    # the list-form collect_bot kept for callers gives the same groups too
    again, _ = new.collect_bot(new.read_audit(trial.dir / "audit.jsonl"))
    assert {k: bot_view(v) for k, v in again.items()} == {k: bot_view(v) for k, v in groups.items()}


def test_scan_evidence_equals_what_the_legacy_derived_from_the_event_list(trial):
    events = old.read_audit(trial.dir / "audit.jsonl")
    scan = new.scan_audit(trial.dir / "audit.jsonl")
    ev = scan.evidence
    assert ev.first_ts == min(t for t in (old._try_ts(e.get("timestamp")) for e in events) if t is not None)
    assert ev.fetch_failed == {
        (t.floor("D"), e["symbol"]) for e in events
        if "bar_close" not in e and "market data unavailable" in str(e.get("error", "")) and isinstance(e.get("symbol"), str)
        for t in [old._try_ts(e.get("timestamp"))] if t is not None
    }
    rows = old.read_trade_log(trial.dir / "trades.csv")
    bars = scan.all_bars
    windows = [(bars[0], bars[-1]), (bars[3], bars[8]), (bars[0], bars[0]), (bars[-1], bars[-1]), (bars[5], bars[2])]
    for since, until in windows:
        for r in (rows, None, rows[1:], rows + [dict(rows[0], timestamp="2020-01-01T00:00:00+00:00")]):
            a = [m.to_json() for m in new.check_trade_log_evidence(ev, r, since, until)]
            b = [m.to_json() for m in old.check_trade_log(events, r, since, until)]
            c = [m.to_json() for m in new.check_trade_log(events, r, since, until)]
            assert a == b == c, (since, until)


def test_the_stamp_collapse_answers_the_two_questions_exactly(trial):
    """`stamps()` keeps the first stamp of each run of equal bars: `_bar_at` and the first in-range stamp -- the only
    things asked of the full sorted list -- must not change, for stamps in any order and with ties."""
    rng = random.Random(5)
    base = pd.Timestamp("2026-10-01T00:00:00+00:00")
    for case in range(200):
        ev = new.AuditEvidence()
        bars = [base + pd.Timedelta(days=i) for i in range(rng.randint(1, 4))]
        full = []
        for _ in range(rng.randint(0, 40)):
            ts = base + pd.Timedelta(minutes=rng.randint(0, 30))
            bar = rng.choice(bars)
            ev.feed({"timestamp": ts.isoformat()}, bar)
            full.append((ts, bar))
        full.sort(key=lambda x: x[0])  # stable, as the legacy did
        collapsed = ev.stamps()
        for minute in range(-2, 34):
            when = base + pd.Timedelta(minutes=minute)
            assert new._bar_at(collapsed, when, base) == old._bar_at(full, when, base), (case, minute)
        for since, until in [(bars[0], bars[-1]), (bars[-1], bars[-1]), (bars[0], bars[0])]:
            first_full = [t for t, b in full if since <= b <= until][:1]
            first_collapsed = [t for t, b in collapsed if since <= b <= until][:1]
            assert first_full == first_collapsed, (case, since, until)


# --------------------------------------------------------------------------- #
# B. whole reports
# --------------------------------------------------------------------------- #
def test_the_trial_report_is_identical(trial):
    kind, js, text = assert_same(trial.dir, "--days", str(TRIAL_BARS))
    assert kind == "report" and json.loads(js)["status"] == "PASS" and text.startswith("PAPER TRIAL PARITY CHECK -- PASS")
    assert_same(trial.dir)  # the default 7-bar window
    assert_same(trial.dir, "--days", "3", "--min-bars", "5")  # insufficient data


def echo_only_bar(events: list[dict]) -> tuple[str, str]:
    for e in events:
        if "bar_close" in e and "already decided" not in str(e.get("note")) and e.get("action") == "none":
            return e["bar_close"], e["symbol"]
    raise AssertionError


def mutate_drop_deciding_line(events):
    bar, sym = echo_only_bar(events)
    return [e for e in events if not (e.get("bar_close") == bar and e["symbol"] == sym and "already decided" not in str(e.get("note")))]


def mutate_duplicate_decision(events):
    out = list(events)
    i = next(i for i, e in enumerate(out) if "bar_close" in e and "candidates" in e)
    out.insert(i + 1, copy.deepcopy(out[i]))
    return out


def mutate_flip_planned(events):
    events = copy.deepcopy(events)
    t = planned_events(events)[0]
    t["risk_decision"] = {"allowed": False, "reason": "position would exceed the cap", "max_quote_amount": None}
    t["action"] = "none"
    t.pop("execution", None)
    return events


def mutate_drop_all_echoes_of_a_bar(events):
    bar, _ = echo_only_bar(events)
    return [e for e in events if not (e.get("bar_close") == bar and "already decided" in str(e.get("note")))]


def mutate_only_echoes_remain_for_one_symbol(events):
    bar, sym = echo_only_bar(events)
    return [e for e in events if not (e.get("bar_close") == bar and e["symbol"] == sym and "already decided" not in str(e.get("note")))]


def mutate_fetch_failure(events):
    events = copy.deepcopy(events)
    sym = events[5]["symbol"]
    events.insert(7, {"symbol": sym, "timestamp": events[7]["timestamp"], "error": "market data unavailable: boom", "action": "error"})
    return events


def mutate_drop_the_end(events):
    return events[:-20]


def mutate_no_symbol(events):
    events = copy.deepcopy(events)
    events[10].pop("symbol")
    return events


def mutate_bad_bar_close(events):
    events = copy.deepcopy(events)
    events[12]["bar_close"] = "not a time"
    return events


def mutate_malformed_risk_decision(events):
    events = copy.deepcopy(events)
    t = planned_events(events)[0]
    t["risk_decision"] = "nope"
    return events


def mutate_naive_and_odd_timestamps(events):
    events = copy.deepcopy(events)
    events[3]["timestamp"] = events[3]["timestamp"][:19]  # naive
    events[4]["timestamp"] = 12345                        # not a string
    events[6]["timestamp"] = "garbage"
    events[8].pop("timestamp")
    return events


def mutate_unseen_fill(events):
    events = copy.deepcopy(events)
    t = planned_events(events)[0]
    t["execution"] = dict(t["execution"], timestamp="2031-01-01T00:00:00+00:00")
    return events


MUTATIONS = [
    mutate_drop_deciding_line, mutate_duplicate_decision, mutate_flip_planned, mutate_drop_all_echoes_of_a_bar,
    mutate_only_echoes_remain_for_one_symbol, mutate_fetch_failure, mutate_drop_the_end, mutate_no_symbol,
    mutate_bad_bar_close, mutate_malformed_risk_decision, mutate_naive_and_odd_timestamps, mutate_unseen_fill,
]


@pytest.mark.parametrize("mutation", MUTATIONS, ids=lambda m: m.__name__)
def test_mutated_trials_give_the_identical_outcome(work, mutation):
    write_events(work, mutation(read_events(work)))
    assert_same(work, "--days", str(TRIAL_BARS))


def test_the_echo_only_faults_are_still_found(work):
    """Sanity of the equivalence above: these two faults exist ONLY because echo lines are kept."""
    write_events(work, mutate_only_echoes_remain_for_one_symbol(read_events(work)))
    kind, js, _ = assert_same(work, "--days", str(TRIAL_BARS))
    report = json.loads(js)
    miss = [m for m in report["mismatches"] if m["kind"] == "missing_decision_event"]
    assert miss and "audit lines [" in miss[0]["detail"]  # the echo line numbers are listed, exactly as before
    write_events(work, mutate_duplicate_decision(read_events(work)))
    _, js, _ = assert_same(work, "--days", str(TRIAL_BARS))
    assert any(m["kind"] == "duplicate_decision" for m in json.loads(js)["mismatches"])


def test_trade_log_variants_and_options(work):
    trades = work / "trades.csv"
    original = trades.read_text()
    lines = original.splitlines()
    for text in (lines[0] + "\n", "\n".join([lines[0]] + lines[2:]) + "\n", "\n".join(lines + [lines[1]]) + "\n"):
        trades.write_text(text)
        assert_same(work, "--days", str(TRIAL_BARS))
    trades.unlink()
    assert_same(work, "--days", str(TRIAL_BARS))
    trades.write_text(original)
    for extra in (["--trial-start", "2025-10-02"], ["--until", "2025-10-08"], ["--since", "2025-10-03", "--until", "2025-10-09"],
                  ["--relaxed-early-budget"], ["--ack", "2025-10-03:BTC/USD:plan_levels"]):
        assert_same(work, "--days", str(TRIAL_BARS), *extra)


@pytest.mark.parametrize("content", [
    b"", b"\n\n", b"{not json\n", b'{"a": 1}\n[1, 2]\n', b'{"symbol": "X", "timestamp": "2026-01-01T00:00:00+00:00"}\n',
    b'{"symbol": "X", "bar_close": "2026-01-01T00:00:00+00:00", "action": "none"}\n\xff\xfe\n',
])
def test_odd_audit_files_give_the_identical_outcome(work, content):
    (work / "audit.jsonl").write_bytes(content)
    assert_same(work, "--days", str(TRIAL_BARS))


def test_a_missing_audit_log_gives_the_identical_error(work):
    (work / "audit.jsonl").unlink()
    kind, message = assert_same(work)
    assert kind == "error" and "audit log not found" in message


# --------------------------------------------------------------------------- #
# C. a long synthetic log
# --------------------------------------------------------------------------- #
def inflate(events: list[dict], ticks: int, monotonic: bool) -> list[dict]:
    """Appends, after each bar's events, `ticks` further passes over that bar's symbols as "already decided" echoes
    -- what the bot writes on every later 5-minute tick of a daily bar."""
    by_bar: dict[str, list[dict]] = {}
    order: list[str] = []
    for e in events:
        key = e.get("bar_close", "")
        if key not in by_bar:
            by_bar[key] = []
            order.append(key)
        by_bar[key].append(e)
    out: list[dict] = []
    for key in order:
        group = by_bar[key]
        out.extend(group)
        if not key:
            continue
        decided = {e["symbol"]: e for e in group if "already decided" not in str(e.get("note"))}
        for i in range(1, ticks + 1):
            for sym, e in sorted(decided.items()):
                ts = datetime.fromisoformat(e["timestamp"]) + (timedelta(seconds=i) if monotonic else timedelta(minutes=5 * i))
                out.append({"symbol": sym, "mode": "paper", "timestamp": ts.isoformat(), "bar_close": key, "action": "none",
                            "note": f"bar closing {key} already decided for {sym}; next entry decision on the next closed bar"})
    return out


@pytest.fixture(scope="module")
def long_base(tmp_path_factory):
    return _generate_trial(tmp_path_factory.mktemp("long_base"), start=250, bars=45, ticks=1)


@pytest.mark.parametrize("monotonic", [True, False], ids=["monotonic-stamps", "interleaved-stamps"])
def test_a_long_log_with_many_echo_ticks_gives_the_identical_report(long_base, tmp_path, monotonic):
    root = tmp_path / "long"
    shutil.copytree(long_base.dir, root)
    events = inflate(read_events(root), ticks=120, monotonic=monotonic)
    write_events(root, events)
    assert len(events) > 80_000
    kind, js, _ = assert_same(root, "--days", "45")
    assert kind == "report"
    report = json.loads(js)
    assert report["window"]["decided_bars"] == 45
    # interleaved stamps put each bar's echoes minutes into the next bar's decisions: the trade-log cross-check
    # sees that and the report says so, identically in both implementations
    assert (report["status"] == "PASS") or not monotonic


# --------------------------------------------------------------------------- #
# D. error precedence and memory
# --------------------------------------------------------------------------- #
def test_a_corrupt_line_late_in_the_file_beats_an_earlier_collect_error(work):
    """The old code read the whole file first, so a JSON error on line 900 was reported even though line 10 would
    have failed `collect_bot`; the streaming scan holds the early error back until the file has been read."""
    events = mutate_no_symbol(read_events(work))
    text = "".join(json.dumps(e) + "\n" for e in events) + "{corrupt\n"
    (work / "audit.jsonl").write_text(text)
    kind, message = assert_same(work)
    assert kind == "error" and f"audit.jsonl:{len(events) + 1}: not valid JSON" in message
    (work / "audit.jsonl").write_text(text[:-len("{corrupt\n")])
    kind, message = assert_same(work)
    assert kind == "error" and "event with bar_close has no symbol" in message and "audit line 11" in message


def test_peak_memory_no_longer_grows_with_the_log(tmp_path):
    def build(path: Path, n: int) -> None:
        syms = [f"S{i}/USD" for i in range(16)]
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with open(path, "w") as f:
            for i in range(n):
                day, tick = divmod(i // 16, 288)
                bar = (base + timedelta(days=day)).isoformat()
                ts = (base + timedelta(days=day, minutes=5 * tick, seconds=i % 16)).isoformat()
                s = syms[i % 16]
                f.write(json.dumps({"symbol": s, "mode": "paper", "timestamp": ts, "bar_close": bar, "action": "none",
                                    "note": f"bar closing {bar} already decided for {s}; next entry decision on the next closed bar"}) + "\n")

    small, big = tmp_path / "small.jsonl", tmp_path / "big.jsonl"
    build(small, 10_000)
    build(big, 40_000)  # 4x, ~10 MB

    def peak(fn) -> int:
        tracemalloc.start()
        try:
            fn()
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    def run_old(p):
        events = old.read_audit(p)
        old.collect_bot(events)
        return events

    new_small, new_big = peak(lambda: new.scan_audit(small)), peak(lambda: new.scan_audit(big))
    old_big = peak(lambda: run_old(big))
    assert new_big < 12 * 1024 * 1024
    assert old_big > 8 * new_big, (old_big, new_big)
    # what still grows is 12 B a stamp + 4 B a line number (measured ~18 B/line), not ~1.1 KB
    assert (new_big - new_small) / 30_000 < 40
