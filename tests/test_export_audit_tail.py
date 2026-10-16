"""scripts/export_dashboard_data.py reads the audit log from the END, in chunks, instead of loading the whole file.

The contract is "the same web/data.json as before, in bounded memory". The "before" is a frozen copy of
origin/main's script (tests/_legacy_export_dashboard_data.py); these tests run old and new on the same inputs:

  A. the line reader / `_read_audit_tail` against the legacy whole-file read on an edge-case corpus (empty file, no
     trailing newline, partial last line, corrupt lines, CRLF / CR / exotic separators, leading blanks and
     whitespace, very long lines, multi-byte characters at every chunk edge), at many chunk sizes and line caps,
     plus a randomised fuzz;
  B. the streaming reducers (`_scan_audit`) against the legacy `_role_statuses` / `_signal_history` on the
     committed fixtures, a generated trial log and a multi-month log, below and above the 200,000-line cap;
  C. the whole `main()` run old vs new, byte-for-byte on web/data.json (1h config);
  D. peak memory, and the byte cap.
"""
from __future__ import annotations

import copy
import json
import random
import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import scripts.export_dashboard_data as new
import tests._legacy_export_dashboard_data as old
from src.config import Config, load_config

NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
CHUNKS = (1, 2, 3, 5, 8, 13, 64, 4096, 1 << 20)
LINE_CAPS = (1, 2, 3, 7, 50, 200_000)


def _both(path: Path, max_lines: int, chunk: int) -> tuple[list, list]:
    expected = old._read_audit_tail(path, max_lines)
    actual = list(reversed(list(new._iter_audit_events_reversed(path, max_lines, chunk, None))))
    return expected, actual


# --------------------------------------------------------------------------- #
# A. the reader
# --------------------------------------------------------------------------- #
J = '{"a": 1}'
K = '{"k": "日本語é😀"}'
CORPUS: dict[str, bytes] = {
    "empty": b"",
    "only_newlines": b"\n\n\n",
    "only_whitespace": b"  \n \t \n",
    "single_no_newline": J.encode(),
    "single_with_newline": (J + "\n").encode(),
    "no_trailing_newline": (J + "\n" + K).encode(),
    "trailing_blank_lines": (J + "\n" + K + "\n\n\n  \n").encode(),
    "partial_last_line": (J + "\n" + K + '\n{"ti').encode(),
    "corrupt_in_middle": (J + "\nnot json\n" + K + "\n{broken\n" + J + "\n").encode(),
    "blank_interior": (J + "\n\n\n" + K + "\n").encode(),
    "crlf": (J + "\r\n" + K + "\r\n" + J + "\r\n").encode(),
    "lone_cr": (J + "\r" + K + "\r" + J).encode(),
    "mixed_newlines": (J + "\r\n\r" + K + "\n\r\n" + J + "\n").encode(),
    "exotic_separators_raw": (J + "\x0b" + K + "\x0c" + J + "\x1c" + K + "\x85" + J + " " + K + " " + J + "\n").encode(),
    "leading_blank_lines": ("\n\n" + J + "\n" + K + "\n").encode(),
    "leading_nbsp_first_line": ("\xa0" + J + "\n" + K + "\n").encode(),
    "leading_blank_then_nbsp": ("\n  \n\xa0" + J + "\n" + K + "\n").encode(),
    "trailing_nbsp": (J + "\n" + K + "\xa0\xa0\n").encode(),
    "bom_first_line": b"\xef\xbb\xbf" + (J + "\n" + K + "\n").encode(),
    "non_dict_json": (J + "\n123\n[1,2]\n\"s\"\n" + K + "\n").encode(),
    "very_long_line": (J + "\n" + '{"x": "' + "y" * 3_000_000 + '"}\n' + K + "\n").encode(),
    "very_long_unterminated_tail": (J + "\n" + '{"x": "' + "y" * 500_000).encode(),
    "multibyte_everywhere": ("".join(json.dumps({"i": i, "s": "é日😀" * (i % 5 + 1)}, ensure_ascii=False) + "\n" for i in range(40))).encode(),
    "many_short": ("".join(json.dumps({"i": i}) + "\n" for i in range(500))).encode(),
}


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_reader_matches_the_whole_file_read_on_the_edge_corpus(name, tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_bytes(CORPUS[name])
    for chunk in (CHUNKS if len(CORPUS[name]) < 100_000 else (4096, 1 << 20)):
        for cap in LINE_CAPS:
            expected, actual = _both(path, cap, chunk)
            assert actual == expected, (name, chunk, cap)


def test_reader_matches_the_legacy_when_the_file_is_missing(tmp_path):
    path = tmp_path / "nope.jsonl"
    assert old._read_audit_tail(path) == new._read_audit_tail(path) == []


def test_read_audit_tail_wrapper_is_the_legacy_function(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_bytes(CORPUS["mixed_newlines"] + CORPUS["many_short"])
    for cap in (1, 5, 200_000):
        assert new._read_audit_tail(path, cap) == old._read_audit_tail(path, cap)


def test_reader_randomised_against_the_legacy(tmp_path):
    rng = random.Random(20261016)
    atoms = [J, K, "\n", "\n", "\r\n", "\r", " ", "\t", "é", "日", "😀", "\xa0", "\x85", " ", "\x0b", "{bad", "}", "123",
             '{"timestamp": "2026-10-16T00:00:00+00:00", "symbol": "BTC/USD", "signal": {"pattern": "x"}}']
    path = tmp_path / "fuzz.jsonl"
    for case in range(400):
        text = "".join(rng.choice(atoms) for _ in range(rng.randint(0, 40)))
        path.write_bytes(text.encode())
        chunk = rng.choice((1, 2, 3, 4, 7, 16))
        cap = rng.choice((1, 2, 3, 5, 100))
        expected, actual = _both(path, cap, chunk)
        assert actual == expected, (case, text.encode(), chunk, cap)


def test_every_chunk_edge_through_multibyte_characters(tmp_path):
    """A 4-byte character straddling a chunk boundary is decoded whole: the file is split on 0x0A, never mid-character."""
    text = "".join(json.dumps({"s": "😀é日" * 3, "i": i}, ensure_ascii=False) + "\n" for i in range(12))
    path = tmp_path / "mb.jsonl"
    path.write_text(text, encoding="utf-8")
    for chunk in range(1, 40):
        expected, actual = _both(path, 200_000, chunk)
        assert actual == expected and len(actual) == 12


def test_invalid_utf8_no_longer_aborts_the_export(tmp_path):
    """The one deliberate difference: a whole-file read raised UnicodeDecodeError and stopped the dashboard
    refresh; the reader replaces the bad bytes, so only the damaged line is lost."""
    path = tmp_path / "audit.jsonl"
    path.write_bytes(b'{"a": 1}\n{"b": "\xff\xfe"}\n{"c": 3}\n\xc3')  # bad bytes mid-file and a truncated multi-byte tail
    with pytest.raises(UnicodeDecodeError):
        old._read_audit_tail(path)
    got = new._read_audit_tail(path)
    assert {"a": 1} in got and {"c": 3} in got


def test_line_cap_counts_blank_lines_like_the_legacy(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text('{"i": 1}\n\n\n{"i": 2}\n{"i": 3}\n')
    for cap in range(1, 8):
        expected, actual = _both(path, cap, 4)
        assert actual == expected, cap


def test_byte_cap_yields_only_whole_newest_lines(tmp_path):
    path = tmp_path / "audit.jsonl"
    lines = [json.dumps({"i": i, "pad": "x" * 50}) for i in range(1000)]
    path.write_text("\n".join(lines) + "\n")
    for cap in (1, 100, 500, 4096, 10_000):
        got = list(new._iter_audit_events_reversed(path, None, 64, cap))
        assert [e["i"] for e in got] == list(range(999, 999 - len(got), -1))  # a newest-first suffix, no gaps
        assert len(got) * len(lines[0]) <= cap + 64 + len(lines[0])
    whole = list(new._iter_audit_events_reversed(path, None, 64, None))
    assert len(whole) == 1000


@pytest.mark.parametrize("chunk", [7, 64, 1000])
def test_byte_cap_never_yields_a_partial_raw_line(tmp_path, chunk):
    """Checked on the RAW lines (a partial JSON line would just fail to parse and hide the bug in the event test)."""
    path = tmp_path / "audit.jsonl"
    lines = [json.dumps({"i": i, "pad": "x" * (i % 40)}) for i in range(600)]
    path.write_text("\n".join(lines) + "\n")
    full = set(lines)
    for cap in (1, 5, 100, 777, 2000, 9000):
        got = list(new._iter_audit_lines_reversed(path, None, chunk, cap))
        assert all(line in full for line in got), (chunk, cap, [line for line in got if line not in full][:1])
        assert got == lines[len(lines) - len(got):][::-1]  # exactly the newest lines, in order, no gaps
        assert sum(len(line) + 1 for line in got) <= cap + chunk + max(map(len, lines))  # and it did stop near the cap


def test_chunks_read_never_exceed_the_chunk_size(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    path.write_text("".join(json.dumps({"i": i}) + "\n" for i in range(2000)))
    sizes = []
    real_open = open

    class Spy:
        def __init__(self, f):
            self.f = f

        def __enter__(self):
            self.f.__enter__()
            return self

        def __exit__(self, *a):
            return self.f.__exit__(*a)

        def __iter__(self):
            return iter(self.f)

        def __getattr__(self, name):
            return getattr(self.f, name)

        def read(self, n=-1):
            sizes.append(n)
            return self.f.read(n)

    monkeypatch.setattr(new, "open", lambda p, mode="r", *a, **k: Spy(real_open(p, mode, *a, **k)) if mode == "rb" else real_open(p, mode, *a, **k), raising=False)
    list(new._iter_audit_events_reversed(path, None, 1000, None))
    assert sizes and max(sizes) <= 1000 and -1 not in sizes


# --------------------------------------------------------------------------- #
# B. the reducers
# --------------------------------------------------------------------------- #
SYMBOLS = [f"S{i}/USD" for i in range(16)]


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def synthetic_events(days: int, per_day: int, end: datetime = NOW, advisor: bool = False, seed: int = 7):
    """Realistic mix: one decision line per symbol per day (some with plans, fills, exits, rejections, vetoes, htf
    rejections) and the rest "already decided" echoes, oldest first, ending at `end`."""
    rng = random.Random(seed)
    total_ticks = days * per_day
    step = timedelta(days=days) / total_ticks
    start = end - timedelta(days=days)
    out = []
    for t in range(total_ticks):
        ts = start + step * t
        day_start = ts.replace(hour=0, minute=0, second=0, microsecond=0)
        first_tick_of_day = t % per_day == 0
        for sym in SYMBOLS:
            base = {"symbol": sym, "mode": "paper", "timestamp": _iso(ts), "bar_close": _iso(day_start)}
            if not first_tick_of_day:
                out.append({**base, "action": "none", "note": f"bar closing {_iso(day_start)} already decided for {sym}; next entry decision on the next closed bar"})
                continue
            r = rng.random()
            ev = {**base, "candidates": [{"pattern": "BREAKOUT", "direction": "bullish", "strength": 0.6}]}
            if r < 0.15:
                ev.update(signal={"pattern": "BREAKOUT", "direction": "bullish"}, plan={"entry_zone": [100.0, 101.5], "stop_loss": 95.25, "pattern": "BREAKOUT"},
                          risk_decision={"allowed": True, "reason": "ok"}, action="planned")
                if advisor:
                    ev["advisor_verdict"] = {"verdict": "APPROVE", "confidence_stars": 4}
            elif r < 0.25:
                ev.update(signal={"pattern": "MOMENTUM", "direction": "bullish"}, risk_decision={"allowed": False, "reason": "exposure cap; next slot opens in 2:00:00"}, action="none")
            elif r < 0.30:
                ev.update(execution={"price": 0.0000123, "side": "sell", "timestamp": _iso(ts)}, monitor_action={"action": rng.choice(["STOP_LOSS", "TAKE_PROFIT"])})
            elif r < 0.34:
                ev.update(execution={"price": 1234.5, "side": "buy", "timestamp": _iso(ts)}, monitor_action={"action": "FILL"})
            elif r < 0.38:
                ev.update(daily_trend_confirmed=False)
            elif r < 0.42 and advisor:
                ev.update(advisor_verdict={"verdict": "VETO", "reasoning": "weak", "confidence_stars": 2})
            out.append(ev)
    return out


def write_events(path: Path, events: list[dict]) -> None:
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e, default=str) + "\n")


def assert_reducers_match(path: Path, *, advisor_enabled: bool, max_lines: int = 200_000, now: datetime = NOW,
                          backtest: dict | None = None, **history_kwargs):
    legacy_events = old._read_audit_tail(path, max_lines)
    expected_roles = old._role_statuses(legacy_events, now, advisor_enabled, backtest)
    expected_hist = old._signal_history(legacy_events, now, **history_kwargs)
    view = new._scan_audit(new._iter_audit_events_reversed(path, max_lines), now, **history_kwargs)
    assert new._role_statuses_from_view(view, now, advisor_enabled, backtest) == expected_roles
    assert view.history == expected_hist
    # and the list-form wrappers kept for callers
    assert new._role_statuses(legacy_events, now, advisor_enabled, backtest) == expected_roles
    assert new._signal_history(legacy_events, now, **history_kwargs) == expected_hist
    return expected_roles, expected_hist


@pytest.mark.parametrize("advisor", [False, True])
def test_reducers_match_on_a_multi_month_log(tmp_path, advisor):
    path = tmp_path / "audit.jsonl"
    write_events(path, synthetic_events(days=100, per_day=4, advisor=advisor))
    roles, hist = assert_reducers_match(path, advisor_enabled=advisor)
    assert hist and any(r["status"] == "working" for r in roles) in (True, False)
    assert {r["id"] for r in roles} >= {"boss", "market_analyst", "execution_trader", "backtester"}


@pytest.mark.parametrize("max_lines", [1, 50, 999, 1000, 1001, 5000])
def test_reducers_match_below_at_and_above_the_line_cap(tmp_path, max_lines):
    path = tmp_path / "audit.jsonl"
    write_events(path, synthetic_events(days=20, per_day=3))
    assert_reducers_match(path, advisor_enabled=True, max_lines=max_lines)


@pytest.mark.parametrize("max_items", [1, 5, 40, 3000])
@pytest.mark.parametrize("max_age", [timedelta(hours=1), timedelta(days=3), timedelta(days=14), timedelta(days=400)])
def test_history_cutoffs_match(tmp_path, max_items, max_age):
    path = tmp_path / "audit.jsonl"
    write_events(path, synthetic_events(days=30, per_day=2))
    assert_reducers_match(path, advisor_enabled=True, max_age=max_age, max_items=max_items)


def test_a_role_whose_last_event_is_older_than_the_history_window_is_still_found(tmp_path):
    """The role scan must go past the 14-day history cut-off (the advisor event here is 60 days old)."""
    events = synthetic_events(days=70, per_day=2, advisor=False)
    events[10]["advisor_verdict"] = {"verdict": "VETO", "reasoning": "old", "confidence_stars": 1}
    path = tmp_path / "audit.jsonl"
    write_events(path, events)
    roles, _ = assert_reducers_match(path, advisor_enabled=True)
    reviewer = next(r for r in roles if r["id"] == "compliance_reviewer")
    assert reviewer["last_active"] == events[10]["timestamp"]


def test_the_scan_stops_early_but_never_changes_the_answer_and_never_holds_the_events(tmp_path):
    path = tmp_path / "audit.jsonl"
    write_events(path, synthetic_events(days=60, per_day=3, advisor=True))
    consumed = 0

    def counting(it):
        nonlocal consumed
        for e in it:
            consumed += 1
            yield e

    view = new._scan_audit(counting(new._iter_audit_events_reversed(path)), NOW)
    total = sum(1 for _ in open(path))
    assert consumed < total  # stopped once the history cut-off passed and every role was found
    assert len(view.last_by_role) <= len(new.ROLE_MARKERS)
    assert view.history == old._signal_history(old._read_audit_tail(path), NOW)


def test_empty_and_missing_logs(tmp_path):
    path = tmp_path / "audit.jsonl"
    for payload in (None, b"", b"\n\n"):
        if payload is not None:
            path.write_bytes(payload)
        view = new._scan_audit(new._iter_audit_events_reversed(path), NOW)
        assert view.newest is None and view.history == []
        assert new._role_statuses_from_view(view, NOW, False, None) == old._role_statuses([], NOW, False, None)


def test_committed_fixture_logs_match(tmp_path):
    """The audit logs generated by the real bot loop (the parity-check trial fixture), 1d bars and 1h."""
    from tests.test_paper_parity_check import _generate_trial

    trial = _generate_trial(tmp_path / "trial", bars=6, ticks=3)
    events = old._read_audit_tail(trial.dir / "audit.jsonl")
    now = max(datetime.fromisoformat(e["timestamp"]) for e in events) + timedelta(minutes=3)
    for advisor in (False, True):
        assert_reducers_match(trial.dir / "audit.jsonl", advisor_enabled=advisor, now=now)
    # a corrupt line, a partial last line and no trailing newline in a real log
    raw = (trial.dir / "audit.jsonl").read_text().splitlines()
    raw.insert(len(raw) // 2, "{this line is corrupt")
    broken = tmp_path / "broken.jsonl"
    broken.write_text("\n".join(raw) + '\n{"timestamp": "2026-')
    assert_reducers_match(broken, advisor_enabled=False, now=now)


# --------------------------------------------------------------------------- #
# C. main(), old vs new, byte for byte
# --------------------------------------------------------------------------- #
def _fixed_datetime():
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    return FixedDatetime


def _snapshot(path: Path, timeframe: str, bars: int = 30) -> None:
    step = {"1h": 3600, "1d": 86400}[timeframe]
    last_open = int(NOW.timestamp()) // step * step - step  # the newest CLOSED bar's open
    symbols = {}
    for i, sym in enumerate(SYMBOLS[:4]):
        symbols[sym] = {
            "price": 100.0 + i,
            "updated_at": NOW.isoformat(),
            "candles": [{"time": last_open - (bars - 1 - k) * step, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5 + k * 0.01} for k in range(bars)],
        }
    path.write_text(json.dumps({"generated_at": NOW.isoformat(), "timeframe": timeframe, "symbols": symbols}))


def run_main(module, tmp: Path, raw: dict, monkeypatch) -> bytes:
    out = tmp / f"{module.__name__.rsplit('.', 1)[-1]}_data.json"
    monkeypatch.setattr(module, "load_config", lambda: Config(raw=copy.deepcopy(raw)))
    monkeypatch.setattr(module, "OUT_PATH", out)
    monkeypatch.setattr(module, "datetime", _fixed_datetime())
    module.main()
    return out.read_bytes()


def main_raw(tmp: Path, audit: Path, timeframe: str = "1h", label: str | None = None) -> dict:
    raw = copy.deepcopy(load_config().raw)
    raw["market"]["timeframe"] = timeframe
    raw["notifier"] = {"label": label}
    raw["logging"].update(
        audit_log_path=str(audit), trade_log_path=str(tmp / "trades.csv"), trade_plan_current_path=str(tmp / "plans.json"),
        trade_plan_history_path=str(tmp / "plans.jsonl"), market_snapshot_path=str(tmp / "snapshot.json"),
    )
    return raw


@pytest.mark.parametrize("advisor", [False, True])
def test_main_output_is_byte_identical_to_the_legacy_at_1h(tmp_path, monkeypatch, advisor):
    audit = tmp_path / "audit.jsonl"
    write_events(audit, synthetic_events(days=40, per_day=5, advisor=advisor))
    _snapshot(tmp_path / "snapshot.json", "1h")
    raw = main_raw(tmp_path, audit)
    raw["advisor"]["enabled"] = advisor
    before = run_main(old, tmp_path, raw, monkeypatch)
    after = run_main(new, tmp_path, raw, monkeypatch)
    assert after == before
    data = json.loads(after)
    assert "display" not in data  # the hourly export gained no key
    assert data["signal_history"] and data["roles"][0]["id"] == "boss"


def test_main_output_is_byte_identical_for_a_log_over_the_200k_line_cap(tmp_path, monkeypatch):
    """The real default cap: 210,000 compact lines, the oldest 10,000 carry the only advisor events (so the cap
    boundary decides one role's card)."""
    audit = tmp_path / "audit.jsonl"
    base = NOW - timedelta(days=30)
    with open(audit, "w") as f:
        for i in range(210_000):
            ts = base + timedelta(seconds=i * 12)
            ev = {"symbol": SYMBOLS[i % 16], "timestamp": _iso(ts), "action": "none", "note": "already decided"}
            if i == 9_999 or i == 10_001:
                ev["advisor_verdict"] = {"verdict": "VETO", "reasoning": f"line {i}", "confidence_stars": 1}
            if i % 40_000 == 3:
                ev.update(execution={"price": 2.5, "side": "buy"}, monitor_action={"action": "TAKE_PROFIT"})
            f.write(json.dumps(ev) + "\n")
    _snapshot(tmp_path / "snapshot.json", "1h")
    raw = main_raw(tmp_path, audit)
    raw["advisor"]["enabled"] = True
    before = run_main(old, tmp_path, raw, monkeypatch)
    after = run_main(new, tmp_path, raw, monkeypatch)
    assert after == before


def test_main_output_is_byte_identical_on_the_generated_trial_log(tmp_path, monkeypatch):
    from tests.test_paper_parity_check import _generate_trial

    trial = _generate_trial(tmp_path / "trial", bars=5, ticks=2)
    _snapshot(tmp_path / "snapshot.json", "1h")
    raw = main_raw(tmp_path, trial.dir / "audit.jsonl")
    assert run_main(new, tmp_path, raw, monkeypatch) == run_main(old, tmp_path, raw, monkeypatch)


@pytest.mark.parametrize("what", ["missing", "empty"])
def test_main_output_is_byte_identical_without_audit_events(tmp_path, monkeypatch, what):
    audit = tmp_path / "audit.jsonl"
    if what == "empty":
        audit.write_text("")
    _snapshot(tmp_path / "snapshot.json", "1h")
    raw = main_raw(tmp_path, audit)
    assert run_main(new, tmp_path, raw, monkeypatch) == run_main(old, tmp_path, raw, monkeypatch)


# --------------------------------------------------------------------------- #
# D. memory
# --------------------------------------------------------------------------- #
def _peak(fn) -> int:
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_peak_memory_is_bounded_and_independent_of_log_size(tmp_path):
    def build(path: Path, n: int) -> None:
        base = NOW - timedelta(days=3)
        with open(path, "w") as f:
            for i in range(n):
                ev = {"symbol": SYMBOLS[i % 16], "mode": "paper", "timestamp": _iso(base + timedelta(seconds=i)),
                      "bar_close": "2026-10-16T00:00:00+00:00", "action": "none",
                      "note": "bar closing 2026-10-16T00:00:00+00:00 already decided for X; next entry decision on the next closed bar"}
                f.write(json.dumps(ev) + "\n")

    small, big = tmp_path / "small.jsonl", tmp_path / "big.jsonl"
    build(small, 20_000)
    build(big, 80_000)  # 4x the file, ~21 MB

    def run_new(p: Path):
        return lambda: new._scan_audit(new._iter_audit_events_reversed(p), NOW)

    def run_old(p: Path):
        def go():
            ev = old._read_audit_tail(p)
            old._role_statuses(ev, NOW, False, None)
            old._signal_history(ev, NOW)
        return go

    new_small, new_big = _peak(run_new(small)), _peak(run_new(big))
    old_big = _peak(run_old(big))
    assert new_big < 4 * 1024 * 1024
    assert new_big < new_small * 1.5 + 1024 * 1024  # 4x the log, the same peak (up to noise)
    assert old_big > 10 * new_big, (old_big, new_big)
