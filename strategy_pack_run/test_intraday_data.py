"""Data-layer acceptance tests (docs/intraday_harness_changes.md AT3-AT7, H1, H2, H7).

AT4 and AT5 read the real snapshot files (loader / validator only; no strategy is evaluated, which
docs/intraday_prereg.md §0 rule 3 allows); they skip if the data root is absent.

Run: pytest strategy_pack_run/test_intraday_data.py -q
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import intraday_data as idata  # noqa: E402
import intraday_fixtures as fx  # noqa: E402
import intraday_runner as ir  # noqa: E402
import intraday_synthetic as syn  # noqa: E402
from resample_ohlcv import resample_ohlcv  # noqa: E402

REAL = idata.data_root()
HAVE_REAL = (REAL / "1h" / "BTC_USD_1h.csv").exists()
needs_real = pytest.mark.skipif(not HAVE_REAL, reason=f"intraday snapshot not present under {REAL}")


def _hourly(start: str, n: int, seed: int = 0) -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="1h", tz="UTC")
    rng = np.random.default_rng(seed)
    c = 100 + np.cumsum(rng.normal(0, 0.3, n))
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + 0.1, "low": np.minimum(o, c) - 0.1, "close": c,
                         "volume": 1 + rng.random(n)}, index=idx)


# --------------------------------------------------------------------------- #
# AT3 resample_ohlcv source_minutes
# --------------------------------------------------------------------------- #
def test_at3_default_source_minutes_is_unchanged_15m_behaviour():
    idx = pd.date_range("2026-03-11 07:15", periods=4 * 24 * 3, freq="15min", tz="UTC")
    df = pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": np.arange(len(idx), dtype=float), "volume": 1.0}, index=idx)
    for tf in ("1h", "4h", "1d"):
        pd.testing.assert_frame_equal(resample_ohlcv(df, tf), resample_ohlcv(df, tf, source_minutes=15))


def test_at3_1h_source_4h_bin_needs_exactly_four_rows_on_utc_boundaries():
    h = _hourly("2026-03-11 03:00", 24 * 3)
    r4 = resample_ohlcv(h, "4h", source_minutes=60)
    assert set(r4.index.hour) <= {0, 4, 8, 12, 16, 20}
    assert r4.index[0] == pd.Timestamp("2026-03-11 04:00", tz="UTC")  # the 00:00 bin has only 03:00 -> dropped
    row = r4.loc[pd.Timestamp("2026-03-11 08:00", tz="UTC")]
    src = h.loc["2026-03-11 08:00":"2026-03-11 11:00"]
    assert row["open"] == src["open"].iloc[0] and row["close"] == src["close"].iloc[-1]
    assert row["high"] == src["high"].max() and row["low"] == src["low"].min() and row["volume"] == pytest.approx(src["volume"].sum())
    gappy = h.drop(pd.Timestamp("2026-03-11 13:00", tz="UTC"))
    assert pd.Timestamp("2026-03-11 12:00", tz="UTC") not in resample_ohlcv(gappy, "4h", source_minutes=60).index
    d1 = resample_ohlcv(h, "1d", source_minutes=60)
    assert list(d1.index) == [pd.Timestamp("2026-03-12", tz="UTC"), pd.Timestamp("2026-03-13", tz="UTC")]


def test_at3_1h_source_resample_is_truncation_invariant_at_every_bar():
    h = _hourly("2026-03-11 00:00", 24 * 4, seed=3)
    full = {tf: resample_ohlcv(h, tf, source_minutes=60) for tf in ("4h", "1d")}
    for i in range(1, len(h) + 1):
        for tf in ("4h", "1d"):
            part = resample_ohlcv(h.iloc[:i], tf, source_minutes=60)
            pd.testing.assert_frame_equal(part, full[tf].loc[part.index])


def test_at3_source_minutes_must_divide_the_target():
    with pytest.raises(ValueError):
        resample_ohlcv(_hourly("2026-01-01", 10), "4h", source_minutes=90)


# --------------------------------------------------------------------------- #
# AT4 snapshot prefix rule
# --------------------------------------------------------------------------- #
@needs_real
def test_at4_snapshot_verifies_on_all_32_files():
    got = idata.verify_snapshot(REAL)
    assert len(got) == 32


def _copy_two(tmp_path):
    rels = ["1h/LTC_USD_1h.csv", "15m/XTZ_USD_15m.csv"]
    for rel in rels:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REAL / rel, tmp_path / rel)
    return rels


@needs_real
def test_at4_flipping_one_byte_inside_the_prefix_fails(tmp_path):
    rels = _copy_two(tmp_path)
    assert len(idata.verify_snapshot(tmp_path, files=rels)) == 2
    p = tmp_path / rels[0]
    b = bytearray(p.read_bytes())
    pos = len(b) // 2
    b[pos] = ord("7") if b[pos] != ord("7") else ord("8")
    p.write_bytes(bytes(b))
    with pytest.raises(idata.SnapshotMismatch):
        idata.verify_snapshot(tmp_path, files=rels)


@needs_real
def test_at4_appending_rows_after_the_prefix_passes(tmp_path):
    rels = _copy_two(tmp_path)
    with open(tmp_path / rels[1], "ab") as f:
        f.write(b"1999999999999,1.0,1.0,1.0,1.0,1.0\n")
    assert len(idata.verify_snapshot(tmp_path, files=rels)) == 2


@needs_real
def test_m5_loader_reads_only_the_hashed_prefix_so_an_appended_duplicate_cannot_replace_a_bar(tmp_path):
    rels = _copy_two(tmp_path)
    before = idata.load_native("LTC", "1h", tmp_path)
    p = tmp_path / rels[0]
    lines = p.read_bytes().splitlines(keepends=True)
    n = idata.snapshot_rows("LTC", "1h")
    assert len(lines) == n + 1
    ts, o, h, lo, c, v = lines[-1000].decode().strip().split(",")
    with open(p, "ab") as f:  # duplicate timestamp of a verified bar, with a different close, appended
        f.write(f"{ts},{o},{h},{lo},{float(c) * 3},{v}\n".encode())
        f.write(b"1999999999999,1.0,1.0,1.0,1.0,1.0\n")
    assert len(idata.verify_snapshot(tmp_path, files=rels)) == 2   # appends are allowed by the prefix rule
    after = idata.load_native("LTC", "1h", tmp_path, end=None)
    pd.testing.assert_frame_equal(before, idata.load_native("LTC", "1h", tmp_path))
    assert len(after) == n and after.index.max() < pd.Timestamp("2030-01-01", tz="UTC")
    t = pd.Timestamp(int(ts), unit="ms", tz="UTC") + pd.Timedelta(hours=1)
    assert after.loc[t, "close"] == float(c)


@needs_real
def test_m5_a_file_shorter_than_its_snapshot_is_refused(tmp_path):
    rels = _copy_two(tmp_path)
    p = tmp_path / rels[0]
    p.write_bytes(b"".join(p.read_bytes().splitlines(keepends=True)[:-3]))
    with pytest.raises(idata.SnapshotMismatch):
        idata.load_native("LTC", "1h", tmp_path)


@needs_real
def test_at4_truncated_file_fails(tmp_path):
    rels = _copy_two(tmp_path)
    p = tmp_path / rels[0]
    p.write_bytes(p.read_bytes()[:-50])
    with pytest.raises(idata.SnapshotMismatch):
        idata.verify_snapshot(tmp_path, files=rels)


def test_at4_runner_refuses_to_start_on_a_mismatch(tmp_path):
    import run_intraday_p2 as p2

    (tmp_path / "1h").mkdir()
    (tmp_path / "1h" / "BTC_USD_1h.csv").write_text("timestamp,open,high,low,close,volume\n1,1,1,1,1,1\n")
    out = p2.evaluate_family(1, p2.RealProvider(tmp_path), workers=1, verbose=False)
    assert out["verdict"] == "VOID" and "snapshot" in out["integrity"][0]


# --------------------------------------------------------------------------- #
# AT5 daily from 1h
# --------------------------------------------------------------------------- #
def test_at5_daily_needs_24_of_24_hours():
    h = _hourly("2026-03-10 00:00", 24 * 3)
    h = h.drop(pd.Timestamp("2026-03-11 05:00", tz="UTC"))
    f = idata.frame_from_open_indexed(h, "1h", "ETH", end=None)
    d = idata.daily_from_1h(f)
    assert list(d.index) == [pd.Timestamp("2026-03-11", tz="UTC"), pd.Timestamp("2026-03-13", tz="UTC")]
    assert d.attrs["source"] == "1h->1d"
    day = h.loc["2026-03-10"]
    assert d["close"].iloc[0] == day["close"].iloc[-1] and d["open"].iloc[0] == day["open"].iloc[0]


@needs_real
def test_at5_daily_closes_match_data_long_on_995_of_days_per_coin_in_b():
    for c in idata.UNIVERSE_B:
        d = idata.daily_from_1h(idata.load_native(c, "1h", REAL))
        share = idata.cross_check_daily_vs_data_long(d, _THIS_DIR / "data_long" / f"{c}_USD_1d.csv")
        assert share >= 0.995, (c, share)


# --------------------------------------------------------------------------- #
# AT6 mixed-source guard
# --------------------------------------------------------------------------- #
def test_at6_1h_run_fed_a_15m_frame_raises():
    d15 = fx.noise_15m()
    d1 = fx.trend_1h()
    base = dict(d1["base"])
    base["ETH"] = d15["base"]["ETH"]
    with pytest.raises(idata.MixedSourceError):
        idata.assert_sources("1h", base, [], list(d1["daily"].values()))
    with pytest.raises(idata.MixedSourceError):
        idata.assert_sources("1h", d1["base"], [d15["base"]["SOL"]], list(d1["daily"].values()))
    bad = dict(d1, base=base)
    with pytest.raises(idata.MixedSourceError):
        ir.Market(bad, fx.WIN_START, 5)


def test_at6_15m_run_with_1h_daily_inputs_is_allowed_but_not_1h_intraday():
    d15 = fx.noise_15m()
    idata.assert_sources("15m", d15["base"], list(d15["base"].values()), list(d15["daily"].values()))
    with pytest.raises(idata.MixedSourceError):
        idata.assert_sources("15m", d15["base"], [d15["native_1h"]["ETH"]], list(d15["daily"].values()))
    with pytest.raises(idata.MixedSourceError):
        idata.daily_from_1h(d15["base"]["ETH"])  # daily bars are never built from 15m


# --------------------------------------------------------------------------- #
# AT7 masks, eligibility, XRP, BNB (data part; the simulator part is in test_intraday_execution)
# --------------------------------------------------------------------------- #
def test_at7_entry_mask_window_rule():
    n, H = 20, 20
    step = pd.Timedelta(minutes=15)
    mask_start = pd.Timestamp("2023-03-04", tz="UTC")
    mask_end = pd.Timestamp("2023-03-05", tz="UTC")
    # t + (H+1) bars reaching the mask start blocks; one bar earlier is allowed
    assert not idata.entry_allowed_15m("SOL", mask_start - (H + 1) * step + step, n, H)
    assert idata.entry_allowed_15m("SOL", mask_start - (H + 1) * step, n, H)
    # the (n + 100)-bar look-back must also clear the mask end
    assert not idata.entry_allowed_15m("SOL", mask_end + (n + 101) * step - step, n, H)
    assert idata.entry_allowed_15m("SOL", mask_end + (n + 101) * step, n, H)
    for c in ("BTC", "ETH"):
        assert idata.entry_allowed_15m(c, mask_start + pd.Timedelta(hours=5), n, H)
    assert not idata.entry_allowed_15m("LTC", pd.Timestamp("2025-02-10 12:00", tz="UTC"), n, H)


def test_at7_bar_mask_covers_whole_utc_days_for_masked_coins_only():
    idx = pd.date_range("2024-08-12 23:30", periods=8, freq="15min", tz="UTC") + pd.Timedelta(minutes=15)
    m = idata.bar_masked_15m("ADA", idx)
    assert list(m) == [False, False, True, True, True, True, True, True]
    assert not idata.bar_masked_15m("BTC", idx).any()


def test_at7_eligibility_needs_60_daily_bars_and_warm_indicators():
    assert list(idata.eligible([59, 60, 200], [True, True, False])) == [False, True, False]
    assert not idata.wilder_warm(113) and idata.wilder_warm(114)


def test_at7_xrp_continuous_segment_only_and_bnb_never_loaded():
    idx = pd.date_range("2020-01-01", periods=24 * 3, freq="1h", tz="UTC").append(
        pd.date_range("2023-07-13 18:00", periods=24, freq="1h", tz="UTC"))
    raw = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx)
    f = idata.frame_from_open_indexed(raw, "1h", "XRP", end=None)
    assert (f.index - pd.Timedelta(hours=1)).min() == idata.XRP_CONTINUOUS_FROM
    with pytest.raises(ValueError):
        idata.frame_from_open_indexed(raw, "1h", "BNB")
    with pytest.raises(ValueError):
        idata.load_native("BNB", "1h")


@needs_real
def test_at7_real_xrp_has_no_bar_before_relisting_and_window_end_is_respected():
    x = idata.load_native("XRP", "1h", REAL)
    assert (x.index - pd.Timedelta(hours=1)).min() >= idata.XRP_CONTINUOUS_FROM
    for c in ("BTC", "XRP"):
        assert idata.load_native(c, "1h", REAL).index.max() <= idata.WINDOW_END


# --------------------------------------------------------------------------- #
# H7 regime / daily alignment
# --------------------------------------------------------------------------- #
def test_h7_decision_at_midnight_sees_the_day_that_just_closed_never_the_forming_one():
    days = pd.date_range("2024-01-02", periods=5, freq="1D", tz="UTC")  # daily CLOSE times
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0], index=days)
    dec = pd.DatetimeIndex([pd.Timestamp("2024-01-03 00:00", tz="UTC"), pd.Timestamp("2024-01-03 23:00", tz="UTC"),
                            pd.Timestamp("2024-01-04 00:00", tz="UTC"), pd.Timestamp("2024-01-01 12:00", tz="UTC")])
    out = idata.align(s, dec.sort_values())
    assert list(out.to_numpy())[1:] == [2.0, 2.0, 3.0] and np.isnan(out.iloc[0])


def test_h7_btc_regime_false_during_warmup_and_true_above_sma():
    idx = pd.date_range("2023-01-02", periods=260, freq="1D", tz="UTC")
    btc = pd.DataFrame({"close": np.linspace(100, 300, 260)}, index=idx)
    reg = idata.btc_regime(btc)
    assert not reg.iloc[:199].any() and reg.iloc[199:].all()
    btc2 = pd.DataFrame({"close": np.linspace(300, 100, 260)}, index=idx)
    assert not idata.btc_regime(btc2).any()


def test_h1_frames_carry_source_tags_and_close_time_index():
    d = syn.universe_1h(["ETH"], "2021-01-01", 24 * 3, seed=1)
    f = d["native_1h"]["ETH"]
    assert f.attrs["source"] == "native_1h" and d["daily"]["ETH"].attrs["source"] == "1h->1d"
    assert f.index[0] == pd.Timestamp("2021-01-01 01:00", tz="UTC")
    assert idata.four_hour_from_1h(f).attrs["source"] == "1h->4h"
