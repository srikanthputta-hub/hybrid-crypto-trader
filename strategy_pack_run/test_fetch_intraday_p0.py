"""test_fetch_intraday_p0.py - fetch_intraday_p0.py on hand-built fake Coinbase responses (no network).

Run: python -m pytest -q strategy_pack_run/test_fetch_intraday_p0.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd
import pytest

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import fetch_intraday_p0 as f  # noqa: E402

H = 3_600_000
T0 = 1_600_000_000_000 // H * H  # an hour-aligned instant (2020-09-13 12:00 UTC)


class FakeResp:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._p, self.text = status, payload, text

    def json(self):
        return self._p


class FakeSession:
    """Serves Coinbase-style candles ([t_s, low, high, open, close, vol], newest first) from a
    {open_ms: (o,h,l,c,v)} book for whatever [start,end] a request asks for. `script` is an optional
    list of statuses served before the real data (to exercise retry paths)."""

    def __init__(self, book, tf_ms, script=()):
        self.book, self.tf_ms, self.script, self.calls = book, tf_ms, list(script), []
        self.trust_env = False

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        if self.script:
            st = self.script.pop(0)
            return FakeResp(st, text="err")
        s = pd.Timestamp(params["start"]).value // 10**6
        e = pd.Timestamp(params["end"]).value // 10**6
        rows = [[t // 1000, v[2], v[1], v[0], v[3], v[4]] for t, v in sorted(self.book.items(), reverse=True) if s <= t <= e]
        return FakeResp(200, rows)


def _bar(i):
    o = 100.0 + i
    return (o, o + 2, o - 1, o + 1, 10.0 + i)  # (open, high, low, close, volume)


class NoWait(f.RateLimiter):
    def __init__(self):
        super().__init__(1e9)


def _ms(iso):
    return pd.Timestamp(iso).value // 10**6


def test_window_end_is_inclusive_last_of_300_bars():
    assert f.window_end_ms(0, H) == 299 * H


def test_parse_candles_reorders_columns_sorts_and_filters_range():
    raw = [[T0 // 1000 + 7200, 9.0, 12.0, 10.0, 11.0, 5.0],   # [t, low, high, open, close, vol]
           [T0 // 1000, 1.0, 4.0, 2.0, 3.0, 6.0],
           [T0 // 1000 - 3600, 0.5, 0.6, 0.55, 0.56, 1.0]]    # before range
    out = f.parse_candles(raw, T0, T0 + 2 * H)
    assert out == [[T0, 2.0, 4.0, 1.0, 3.0, 6.0], [T0 + 2 * H, 10.0, 12.0, 9.0, 11.0, 5.0]]


def test_parse_candles_dedupes_timestamps():
    row = [T0 // 1000, 1.0, 4.0, 2.0, 3.0, 6.0]
    assert len(f.parse_candles([row, list(row)], T0, T0)) == 1


def test_drop_unclosed_keeps_only_bars_whose_close_has_passed():
    rows = [[T0, 1, 1, 1, 1, 1], [T0 + H, 1, 1, 1, 1, 1]]
    assert f.drop_unclosed(rows, H, T0 + H) == [rows[0]]          # second bar closes at T0+2H
    assert f.drop_unclosed(rows, H, T0 + 2 * H) == rows


def test_fetch_window_retries_429_then_succeeds():
    sess = FakeSession({T0: _bar(0)}, H, script=[429, 503])
    sleeps = []
    rows = f.fetch_window(sess, NoWait(), "BTC-USD", "1h", T0, T0 + 299 * H, sleep=sleeps.append)
    assert len(rows) == 1 and len(sess.calls) == 3
    assert sleeps == [1.5, 3.0]  # exponential back-off


def test_fetch_window_404_is_empty_not_error():
    sess = FakeSession({}, H, script=[404])
    assert f.fetch_window(sess, NoWait(), "XYZ-USD", "1h", T0, T0 + H, sleep=lambda s: None) == []


def test_fetch_window_non_retryable_status_raises_immediately():
    sess = FakeSession({}, H, script=[400])
    with pytest.raises(RuntimeError, match="non-retryable"):
        f.fetch_window(sess, NoWait(), "BTC-USD", "1h", T0, T0 + H, sleep=lambda s: None)
    assert len(sess.calls) == 1


def test_fetch_window_gives_up_after_max_retries():
    sess = FakeSession({}, H, script=[429] * f.MAX_RETRIES)
    with pytest.raises(RuntimeError, match="gave up"):
        f.fetch_window(sess, NoWait(), "BTC-USD", "1h", T0, T0 + H, sleep=lambda s: None)
    assert len(sess.calls) == f.MAX_RETRIES


def _run_job(tmp_path, monkeypatch, book, until_ms, daily_first=T0):
    daily = tmp_path / "daily"
    if not daily.exists():
        daily.mkdir()
        pd.DataFrame({"timestamp": [daily_first], "open": [1], "high": [1], "low": [1], "close": [1],
                      "volume": [1]}).to_csv(daily / "BTC_USD_1d.csv", index=False)
    sess = FakeSession(book, H)
    monkeypatch.setattr(f.requests, "Session", lambda: sess)
    res = f.fetch_job("BTC", "1h", tmp_path / "out", daily, "USD", NoWait(), until_ms, log=lambda *a, **k: None)
    return res, sess


def test_fetch_job_spans_multiple_windows_no_duplicates_and_keeps_gaps(tmp_path, monkeypatch):
    n = 700  # > 2 windows of 300
    holes = (10, 299, 300, 450)  # includes a window boundary
    book = {T0 + i * H: _bar(i) for i in range(n) if i not in holes}
    _, sess = _run_job(tmp_path, monkeypatch, book, until_ms=T0 + n * H)
    df = pd.read_csv(tmp_path / "out" / "1h" / "BTC_USD_1h.csv")
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(df) == n - 4 and df["timestamp"].is_unique and df["timestamp"].is_monotonic_increasing
    assert not set(df["timestamp"]) & {T0 + i * H for i in holes}  # holes are not invented/filled
    assert df.iloc[0].tolist() == [T0, 100.0, 102.0, 99.0, 101.0, 10.0]  # O,H,L,C order preserved
    starts = [_ms(p["start"]) for p in sess.calls]
    assert starts[:3] == [T0, T0 + 300 * H, T0 + 600 * H]  # contiguous, non-overlapping windows


def test_fetch_job_starts_at_first_daily_bar_floored_to_timeframe(tmp_path, monkeypatch):
    book = {T0 + i * H: _bar(i) for i in range(5)}
    _, sess = _run_job(tmp_path, monkeypatch, book, until_ms=T0 + 5 * H, daily_first=T0 + 1234)
    assert _ms(sess.calls[0]["start"]) == T0


def test_fetch_job_resume_after_interruption_appends_without_duplicates(tmp_path, monkeypatch):
    n = 650
    book = {T0 + i * H: _bar(i) for i in range(n)}
    _run_job(tmp_path, monkeypatch, book, until_ms=T0 + 300 * H)          # first run covers window 1 only
    df1 = pd.read_csv(tmp_path / "out" / "1h" / "BTC_USD_1h.csv")
    assert len(df1) == 300
    _, sess = _run_job(tmp_path, monkeypatch, book, until_ms=T0 + n * H)  # rerun extends
    df2 = pd.read_csv(tmp_path / "out" / "1h" / "BTC_USD_1h.csv")
    assert len(df2) == n and df2["timestamp"].is_unique
    assert _ms(sess.calls[0]["start"]) == T0 + 300 * H  # resumed, did not restart


def test_fetch_job_crash_between_csv_append_and_state_write_does_not_duplicate(tmp_path, monkeypatch):
    book = {T0 + i * H: _bar(i) for i in range(400)}
    _run_job(tmp_path, monkeypatch, book, until_ms=T0 + 400 * H)
    # simulate a crash that wrote rows but not the state: rewind the cursor one window
    (tmp_path / "out" / "_state" / "BTC_USD_1h.json").write_text('{"cursor_ms": %d}' % (T0 + 300 * H))
    _run_job(tmp_path, monkeypatch, book, until_ms=T0 + 400 * H)
    df = pd.read_csv(tmp_path / "out" / "1h" / "BTC_USD_1h.csv")
    assert len(df) == 400 and df["timestamp"].is_unique


def test_fetch_job_never_writes_unclosed_bar(tmp_path, monkeypatch):
    cur_open = int(time.time() * 1000) // H * H
    book = {cur_open - 2 * H: _bar(0), cur_open - H: _bar(1), cur_open: _bar(2)}  # last one is still forming
    _run_job(tmp_path, monkeypatch, book, until_ms=None, daily_first=cur_open - 2 * H)
    df = pd.read_csv(tmp_path / "out" / "1h" / "BTC_USD_1h.csv")
    assert df["timestamp"].tolist() == [cur_open - 2 * H, cur_open - H]


def test_last_csv_timestamp(tmp_path):
    p = tmp_path / "x.csv"
    assert f.last_csv_timestamp(p) is None
    p.write_text("timestamp,open,high,low,close,volume\n")
    assert f.last_csv_timestamp(p) is None
    p.write_text("timestamp,open,high,low,close,volume\n5,1,1,1,1,1\n9,1,1,1,1,1\n")
    assert f.last_csv_timestamp(p) == 9


def test_rate_limiter_spaces_calls():
    lim = f.RateLimiter(50.0)  # 20 ms apart
    t0 = time.monotonic()
    for _ in range(6):
        lim.wait()
    assert time.monotonic() - t0 >= 5 * 0.02 * 0.9
