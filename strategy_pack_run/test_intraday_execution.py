"""Execution-model acceptance tests for intraday_runner (docs/intraday_harness_changes.md AT7
(simulator part), AT9, AT10, AT12, AT13, AT14, AT17, plus a simulator-level no-look-ahead check:
perturbing every bar after time T changes no decision made before T).

Hand-built paths are written into a real Market/SetData built from a synthetic fixture, so the
exit scanners run on exactly the arrays they see in a run; expected values are worked out next to
each assert.

Run: pytest strategy_pack_run/test_intraday_execution.py -q
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import intraday_data as idata  # noqa: E402
import intraday_families as fam  # noqa: E402
import intraday_fixtures as fx  # noqa: E402
import intraday_runner as ir  # noqa: E402
import intraday_synthetic as syn  # noqa: E402
import run_intraday_p2 as p2  # noqa: E402
from src.risk_engine import RiskEngine  # noqa: E402
from tiered_slippage import tiered_slippage_fraction  # noqa: E402

SPEC = {s.set_id: s for g in fam.GRIDS.values() for s in g}


@contextmanager
def risk_log():
    log = []
    orig = RiskEngine.check_new_trade

    def wrapped(self, symbol, side, *a, **k):
        d = orig(self, symbol, side, *a, **k)
        log.append(dict(symbol=symbol, now=k["now"], allowed=d.allowed, quote=d.max_quote_amount, reason=d.reason,
                        cash=k["cash_available"], open_value=k["open_positions_value"], equity=k["portfolio_equity"]))
        return d

    with patch.object(RiskEngine, "check_new_trade", wrapped):
        yield log


def _setup(spec_id, data=None, start=fx.WIN_START, days=fx.WIN_DAYS, costs=fx.TAKER, **kw):
    data = data if data is not None else fx.trend_1h()
    m = ir.Market(data, start, days)
    sd = ir.SetData(m, SPEC[spec_id])
    return m, sd, ir.Simulator(m, sd, costs, fx.risk_cfg(), **kw)


def _write_path(m, sd, s, j, rows):
    """rows: list of (open, high, low, close) for base bars j, j+1, ...; decision closes (r=1) too."""
    for i, (o, h, lo, c) in enumerate(rows):
        k = j + i
        m.O[s, k], m.H[s, k], m.L[s, k], m.C[s, k] = o, h, lo, c
        m.has[s, k] = True
        if sd.r == 1:
            sd.close_dec[s, k] = c
        elif k % sd.r == 0:
            sd.close_dec[s, k // sd.r] = c


def _clean_coin(m, sd, s, j, n=200, px=100.0):
    _write_path(m, sd, s, j, [(px, px + 0.5, px - 0.5, px)] * n)


# --------------------------------------------------------------------------- #
# AT10 exit models
# --------------------------------------------------------------------------- #
def test_at10_close_trigger_exits_at_next_open_and_a_gap_fills_at_that_open():
    m, sd, sim = _setup("F3-01")
    s, j = m.coins.index("ADA"), 41
    _clean_coin(m, sd, s, j)
    _write_path(m, sd, s, j, [(100, 101, 99, 100), (100, 100, 80, 99), (99, 99, 93, 94), (90, 91, 89, 90)])
    flag = np.zeros(sd.Td, dtype=bool)
    ex = sim._exit_f13(s, j, 95.0, None, flag)
    # j+1: low 80 touches 95 but closes 99 -> no exit in the close model; j+2 closes 94 < 95 -> exit at
    # the open of j+3 = 90 (gap below the stop fills at the open, never at 95)
    assert (ex["exit_k"], ex["px"], ex["reason"], ex.get("trigger_k")) == (j + 3, 90.0, "stop", j + 2)


@pytest.mark.parametrize("offset", [0, 1, 5])
def test_m2_a_close_exactly_at_the_stop_triggers_the_close_model_exit(offset):
    """src/monitor.py fires on `current_price <= stop_loss`; the harness must too (review fix m2),
    both before the first decision close (stop0 segment) and on later trailing segments."""
    m, sd, sim = _setup("F3-01")
    s, j = m.coins.index("ADA"), 41
    _clean_coin(m, sd, s, j)
    rows = [(100, 101, 99, 100)] * offset + [(100, 100, 95, 95.0), (96, 97, 95, 96)]
    _write_path(m, sd, s, j, rows)
    ex = sim._exit_f13(s, j, 95.0, None, np.zeros(sd.Td, dtype=bool))
    assert (ex["trigger_k"], ex["exit_k"], ex["reason"]) == (j + offset, j + offset + 1, "stop")
    _write_path(m, sd, s, j + offset, [(100, 100, 95, 95.0 + 1e-9)])
    ex = sim._exit_f13(s, j, 95.0, None, np.zeros(sd.Td, dtype=bool))
    assert ex.get("trigger_k") != j + offset  # strictly above the stop: no exit


def test_m2_family2_close_exactly_at_the_stop_triggers():
    m, sd, sim = _setup("F2-07", data=fx.noise_1h())
    s, j = m.coins.index("ADA"), 41
    _clean_coin(m, sd, s, j)
    _write_path(m, sd, s, j, [(100, 100.5, 99.5, 100), (100, 100, 95, 95.0)])
    ex = sim._exit_f2(s, j, 95.0, 500.0, maker_target=False)
    assert (ex["trigger_k"], ex["reason"]) == (j + 1, "stop")


def test_at10_touch_model_fills_at_stop_or_at_a_gapped_open():
    m, sd, sim = _setup("F3-01", exit_model="touch")
    s, j = m.coins.index("ADA"), 41
    _clean_coin(m, sd, s, j)
    _write_path(m, sd, s, j, [(100, 101, 99, 100), (100, 100, 94, 99)])
    ex = sim._exit_f13(s, j, 95.0, None, np.zeros(sd.Td, dtype=bool))
    assert (ex["exit_k"], ex["px"], ex["reason"]) == (j + 1, 95.0, "stop")
    _write_path(m, sd, s, j + 1, [(93, 99, 92, 98)])
    ex = sim._exit_f13(s, j, 95.0, None, np.zeros(sd.Td, dtype=bool))
    assert (ex["exit_k"], ex["px"], ex["reason"]) == (j + 1, 93.0, "stop_gap")


def test_at10_touch_model_books_the_stop_when_stop_and_target_touch_in_one_bar():
    m, sd, sim = _setup("F2-07", data=fx.noise_1h(), exit_model="touch")
    s, j = m.coins.index("ADA"), 41
    _clean_coin(m, sd, s, j)
    _write_path(m, sd, s, j, [(100, 100.5, 99.5, 100), (100, 106, 94, 100)])
    ex = sim._exit_f2(s, j, 95.0, 105.0, maker_target=False)
    assert (ex["exit_k"], ex["px"], ex["reason"]) == (j + 1, 95.0, "stop")
    _write_path(m, sd, s, j + 1, [(100, 106, 96, 100)])
    ex = sim._exit_f2(s, j, 95.0, 105.0, maker_target=False)
    assert (ex["exit_k"], ex["px"], ex["reason"]) == (j + 1, 105.0, "target")


def test_at10_4h_position_exits_on_the_1h_sequence():
    m, sd, sim = _setup("F1-07")
    assert sd.r == 4
    s, d0 = m.coins.index("ADA"), 10
    j = 4 * d0 + 1  # fill at the 1h open right after the 4h decision close
    _clean_coin(m, sd, s, j)
    sd.atr_d[s, :] = 1.0
    _write_path(m, sd, s, j, [(100, 101, 99, 100), (100, 100, 90, 90), (91, 92, 90, 91)])
    flag = np.zeros(sd.Td, dtype=bool)
    ex = sim._exit_f13(s, j, 95.0, 1.5, flag)
    # j+1 (01:00-02:00 inside the 4h bin) closes 90 < 95: exit at the open of j+2, two hours before the
    # 4h close at 4*(d0+1)
    assert (ex["exit_k"], ex["px"]) == (j + 2, 91.0) and ex["exit_k"] < 4 * (d0 + 1)


def test_at10_trailing_stop_never_moves_down():
    m, sd, sim = _setup("F1-01")
    s, j = m.coins.index("ADA"), 41
    _clean_coin(m, sd, s, j)
    sd.atr_d[s, :] = 2.0
    up = [(100 + i, 101 + i, 99 + i, 101 + i) for i in range(10)]          # closes 101..110
    _write_path(m, sd, s, j, up)
    sd.atr_d[s, j + 10:] = 10.0  # later daily ATR jumps: HC - 1*ATR = 100 < 108, must not lower the stop
    _write_path(m, sd, s, j + 10, [(110, 110, 107.5, 109), (109, 109, 107, 107.5), (107, 107, 106, 106)])
    ex = sim._exit_f13(s, j, 90.0, 1.0, np.zeros(sd.Td, dtype=bool))
    # stop after the close of j+9 = 110 - 2 = 108; j+10 close 109 >= 108; j+11 close 107.5 < 108 -> exit at j+12
    assert (ex["exit_k"], ex["reason"], ex["final_stop"]) == (j + 12, "trail", 108.0)


def test_at10_regime_exit_is_a_close_trigger_at_a_decision_close():
    m, sd, sim = _setup("F1-01")
    s, j = m.coins.index("ADA"), 41
    _clean_coin(m, sd, s, j)
    flag = np.zeros(sd.Td, dtype=bool)
    flag[j + 5] = True
    ex = sim._exit_f13(s, j, 50.0, None, flag)
    assert (ex["exit_k"], ex["reason"], ex["trigger_k"]) == (j + 6, "regime", j + 5)


def test_at10_time_stop_after_exactly_n_decision_bars_counting_coin_bars():
    data = fx.noise_1h()
    m, sd, sim = _setup("F2-07", data=data)
    n = sd.n
    s, j = m.coins.index("ADA"), 41
    _clean_coin(m, sd, s, j, n=60)
    ex = sim._exit_f2(s, j, 50.0, 500.0, maker_target=False)
    # held bars j..j+n-1 (n of them); the time stop fires at the close of j+n-1 and fills at the open of j+n
    assert (ex["exit_k"], ex["reason"], ex["trigger_k"]) == (j + n, "time", j + n - 1)
    # with the coin's own gaps the count is in the coin's bars: ETH has a 7-bar hole in this window
    e = m.coins.index("ETH")
    holes = np.flatnonzero(~m.has[e])
    jj = int(holes[0]) - 5
    m.C[e, jj:jj + 80] = np.where(m.has[e, jj:jj + 80], 100.0, np.nan)
    ex = sim._exit_f2(e, jj, 1.0, 1e9, maker_target=False)
    seen, k = 0, jj
    while True:
        if m.has[e, k]:
            seen += 1
            if seen == n:
                break
        k += 1
    nxt = k + 1
    while not m.has[e, nxt]:
        nxt += 1
    assert (ex["trigger_k"], ex["exit_k"]) == (k, nxt) and nxt - jj > n


# --------------------------------------------------------------------------- #
# AT9 execution timing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("spec_id", ["F1-01", "F1-07", "F3-01"])
def test_at9_entries_fill_at_the_next_base_open_never_the_signal_bar(spec_id):
    m, sd, sim = _setup(spec_id)
    res = sim.run()
    tf = ir.trades_frame(res, m, sd, "syn")
    assert len(tf) > 5
    raw_1h = fx.trend_1h()["native_1h"]
    for r in tf.itertuples():
        assert r.entry_time >= r.signal_time                      # fill opens at or after the signal close
        fill_bar_close = r.entry_time + pd.Timedelta(hours=1)
        assert r.entry_px_raw == raw_1h[r.symbol].loc[fill_bar_close, "open"]
        if spec_id != "F1-01":
            assert r.signal_time.hour % 4 == 0
    assert (tf["entry_time"] == tf["signal_time"]).mean() > 0.9


def test_at9_a_4h_decision_closing_at_0800_fills_at_the_0800_1h_open():
    m, sd, sim = _setup("F1-07")
    res = sim.run()
    tf = ir.trades_frame(res, m, sd, "syn")
    at8 = tf[tf["signal_time"].dt.hour == 8]
    assert len(at8)
    for r in at8.itertuples():
        assert r.entry_time == r.signal_time  # bar 08:00-09:00, opened at 08:00
        assert r.entry_px_raw == fx.trend_1h()["native_1h"][r.symbol].loc[r.signal_time + pd.Timedelta(hours=1), "open"]


# --------------------------------------------------------------------------- #
# AT12 P/L reconciliation and cost application
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("spec_id,data", [("F1-02", "trend"), ("F2-09", "noise"), ("F3-03", "trend"), ("F1-08", "trend")])
def test_at12_pl_reconciles_and_r_recomputes_at_every_cost_view(spec_id, data):
    d = fx.trend_1h() if data == "trend" else fx.noise_1h()
    m = ir.Market(d, fx.WIN_START, fx.WIN_DAYS)
    sd = ir.SetData(m, SPEC[spec_id])
    views = dict(ir.cost_views())
    views["kraken_T3_taker_tiered"] = ir.tiered_cost_model(m)
    for name, cm in views.items():
        for exit_model, fill_model in (("close", "taker"), ("touch", "taker"), ("close", "maker")):
            res = ir.simulate(m, sd, cm, fx.risk_cfg(), exit_model, fill_model)
            tf = ir.trades_frame(res, m, sd, "syn")
            assert abs(tf["net_pnl"].sum() - (res.final_equity - 10_000.0)) <= 1e-9 * 10_000
            assert res.equity[-1] == pytest.approx(res.final_equity, rel=1e-12)
            if not len(tf):
                continue
            assert np.allclose(tf["R"], tf["net_pnl"] / tf["planned_risk_usd"])
            assert np.allclose(tf["net_pnl"], tf["qty"] * tf["exit_px"] - tf["fee_exit"] - tf["approved_quote"])
            notional = tf["approved_quote"] - tf["fee_entry"]
            assert np.allclose(tf["qty"] * tf["entry_px"], notional)
            if fill_model == "taker":
                assert np.allclose(tf["fee_entry"], notional * cm._fee) and (tf["fee_entry"] > 0).all() == (cm._fee > 0)
            else:
                assert np.allclose(tf["fee_entry"], notional * ir.MAKER_FEE) and (tf["entry_px"] == tf["entry_px_raw"]).all()
            taker_exit = tf["exit_reason"] != "target" if fill_model == "maker" else np.ones(len(tf), bool)
            assert np.allclose(tf.loc[taker_exit, "fee_exit"], tf.loc[taker_exit, "qty"] * tf.loc[taker_exit, "exit_px"] * cm._fee)
            if np.isscalar(cm._slip) and fill_model == "taker":
                assert np.allclose(tf["entry_px"], tf["entry_px_raw"] * (1 + cm._slip))
                assert np.allclose(tf["exit_px"], tf["exit_px_raw"] * (1 - cm._slip))


def test_at12_stress_view_charges_exactly_125_percent():
    m, sd, _ = _setup("F1-02")
    base = ir.simulate(m, sd, ir.CostModel.flat(ir.COST_PRESETS["kraken_T3_taker"]), fx.risk_cfg())
    stress = ir.simulate(m, sd, ir.CostModel.flat(ir.STRESS_COSTS), fx.risk_cfg())
    a, b = ir.trades_frame(base, m, sd, "B"), ir.trades_frame(stress, m, sd, "B")
    for df, mult in ((a, 1.0), (b, 1.25)):
        fee_rate = df["fee_entry"] / (df["approved_quote"] - df["fee_entry"])
        slip_rate = df["entry_px"] / df["entry_px_raw"] - 1
        assert np.allclose(fee_rate, 0.0038 * mult, rtol=1e-12) and np.allclose(slip_rate, 0.0005 * mult, rtol=1e-9)
        assert np.allclose(df["fee_exit"] / (df["qty"] * df["exit_px"]), 0.0038 * mult, rtol=1e-12)
    assert b["net_pnl"].sum() < a["net_pnl"].sum()


def test_at12_zero_cost_r_equals_r_gross():
    m, sd, _ = _setup("F1-02")
    res = ir.simulate(m, sd, fx.ZERO, fx.risk_cfg())
    tf = ir.trades_frame(res, m, sd, "B")
    assert len(tf) and np.allclose(tf["R"], tf["R_gross"])


# --------------------------------------------------------------------------- #
# AT13 tiered costs from DAILY volume, day D uses day D-1
# --------------------------------------------------------------------------- #
def _tier_fixture():
    d = syn.universe_1h(["ETH"], fx.DATA_START, 24 * 360, seed=21, sigma=0.004)
    raw = d["native_1h"]["ETH"].copy()
    switch = pd.Timestamp("2021-03-20", tz="UTC")
    # daily quote volume 36M (/3 = 12M -> 5bp) before the switch, 2.4M (/3 = 0.8M -> 50bp) after; per
    # hour that is 1.5M / 0.1M (a 30-bar hourly median would put every bar in the 20bp / 50bp tiers)
    per_hour = np.where(raw.index <= switch, 36e6 / 24, 2.4e6 / 24)
    raw["volume"] = per_hour / raw["close"]
    raw.attrs = d["native_1h"]["ETH"].attrs
    d["native_1h"]["ETH"] = raw
    d["base"]["ETH"] = raw
    d["daily"]["ETH"] = idata.daily_from_1h(raw)
    return d


def test_at13_tier_of_day_d_uses_day_d_minus_1_median_of_daily_volume_over_3():
    d = _tier_fixture()
    m = ir.Market(d, pd.Timestamp("2021-03-25", tz="UTC"), 20)
    cm = ir.tiered_cost_model(m)
    s = m.coins.index("ETH")
    # independent: complete UTC days from the 1h frame, sum(close*volume), 30-day median, / 3
    h = d["native_1h"]["ETH"]
    opens = h.index - pd.Timedelta(hours=1)
    g = (h["close"] * h["volume"]).groupby(opens.floor("1D"))
    qv = g.sum()[g.size() == 24]
    med = qv.rolling(30, min_periods=1).median() / 3.0
    for k in range(1, m.Tb):
        day = pd.Timestamp(int(m.base_ns[k] - m.step), tz="UTC").floor("1D")
        prev = med[med.index <= day - pd.Timedelta(days=1)]
        assert cm._slip[s, k] == tiered_slippage_fraction(float(prev.iloc[-1])), k
    # before the switch the daily median / 3 is 12M -> 5bp; a 30-BAR median of hourly quote volume
    # (1.5M) would have put those bars in the 20bp tier, so hourly volume is not what is fed in
    # (through 03-30 at most 10 of the trailing 30 days are low-volume, so the median is still 36M)
    pre = np.array([pd.Timestamp(int(t), tz="UTC") <= pd.Timestamp("2021-03-31", tz="UTC") for t in m.base_ns[1:]])
    assert pre.any() and (cm._slip[s, 1:][pre] == 0.0005).all()
    assert cm._slip[s, -1] == 0.005


def test_at13_switch_day_keeps_the_old_tier_until_the_next_day():
    d = _tier_fixture()
    m = ir.Market(d, pd.Timestamp("2021-03-25", tz="UTC"), 20)
    cm = ir.tiered_cost_model(m)
    s = m.coins.index("ETH")
    slip = cm._slip[s]
    changes = [k for k in range(2, m.Tb) if slip[k] != slip[k - 1]]
    assert len(changes) >= 2  # 5bp -> (even-split day: 20bp) -> 50bp
    for k in changes:  # tiers only change at UTC day boundaries (a whole day shares day D-1's tier)
        assert pd.Timestamp(int(m.base_ns[k] - m.step), tz="UTC").hour == 0
    # the switch day itself (2021-03-21 is the first low-volume day) is still costed from the
    # median through 03-20: a tier change can only show up from the day after a day's volume is known
    first_change_day = pd.Timestamp(int(m.base_ns[changes[0]] - m.step), tz="UTC").floor("1D")
    assert first_change_day >= pd.Timestamp("2021-03-22", tz="UTC")


def test_m1_daily_quote_volume_is_the_sum_of_hourly_close_times_hourly_volume():
    """Review fix m1: every UTC day trades its volume at 100 for 23 hours and closes at 200 in the
    last hour (negligible volume). Registered definition: sum(1h close x 1h volume) = 23*100*v;
    the wrong one (daily close x day volume) would be 2x that. v is chosen so the two land in
    different tiers: 2300 v / 3 = 6M -> 20bp (registered) vs 12M -> 5bp (wrong)."""
    v = 6e6 * 3 / 2300
    idx = pd.date_range("2021-01-01", periods=24 * 60, freq="1h", tz="UTC")
    last = idx.hour == 23
    px = np.where(last, 200.0, 100.0)
    raw = {c: pd.DataFrame({"open": px, "high": px, "low": px, "close": px, "volume": np.where(last, 1e-9, v)}, index=idx)
           for c in ("BTC", "ETH")}
    d = syn.make_data(raw, "1h")
    qv = idata.daily_quote_volume_from_1h(d["native_1h"]["ETH"])
    assert qv.iloc[0] == pytest.approx(2300 * v + 200 * 1e-9)
    assert (d["daily"]["ETH"]["close"] * d["daily"]["ETH"]["volume"]).iloc[0] == pytest.approx(2 * qv.iloc[0])
    m = ir.Market(d, pd.Timestamp("2021-02-15", tz="UTC"), 10)
    cm = ir.tiered_cost_model(m)
    assert (cm._slip[m.coins.index("ETH"), 1:] == 0.0020).all()
    with pytest.raises(idata.MixedSourceError):
        idata.daily_quote_volume_from_1h(d["daily"]["ETH"])  # never from daily (or 15m) bars


def test_r2_an_incomplete_utc_day_has_no_daily_quote_volume_and_never_sets_a_tier():
    """Review fix R2: only complete UTC days (24 of 24 hourly bars) count. Day 2021-01-20 loses one
    hour; it must be absent from the daily series (not a 23-hour sum), and the tier of the next day
    must come from the last COMPLETE day's 30-day median."""
    idx = pd.date_range("2021-01-01", periods=24 * 60, freq="1h", tz="UTC")
    raw = {}
    for c in ("BTC", "ETH"):
        df = pd.DataFrame({"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1.0}, index=idx)
        raw[c] = df
    gap = pd.Timestamp("2021-01-20 05:00", tz="UTC")
    raw["ETH"] = raw["ETH"].drop(gap)
    d = syn.make_data(raw, "1h")
    qv = idata.daily_quote_volume_from_1h(d["native_1h"]["ETH"])
    day_close = pd.Timestamp("2021-01-21", tz="UTC")              # close time of the 2021-01-20 day
    assert day_close not in qv.index
    assert qv.loc[pd.Timestamp("2021-01-20", tz="UTC")] == pytest.approx(24 * 100.0)
    assert qv.loc[pd.Timestamp("2021-01-22", tz="UTC")] == pytest.approx(24 * 100.0)
    assert list(qv.index) == list(d["daily"]["ETH"].index)        # same complete-day index as daily_from_1h
    # a day-long volume spike on the INCOMPLETE day must not move any tier
    spiky = raw["ETH"].copy()
    spiky.loc[spiky.index.floor("1D") == pd.Timestamp("2021-01-20", tz="UTC"), "volume"] = 1e9
    d2 = syn.make_data({"BTC": raw["BTC"], "ETH": spiky}, "1h")
    m1 = ir.Market(d, pd.Timestamp("2021-02-15", tz="UTC"), 10)
    m2 = ir.Market(d2, pd.Timestamp("2021-02-15", tz="UTC"), 10)
    assert np.array_equal(ir.tiered_cost_model(m1)._slip, ir.tiered_cost_model(m2)._slip)
    assert idata.daily_quote_volume_from_1h(d2["native_1h"]["ETH"]).max() == pytest.approx(2400.0)


# --------------------------------------------------------------------------- #
# AT14 maker model
# --------------------------------------------------------------------------- #
def _rising_universe(n_hours=24 * 360, coins=("ETH", "SOL")):
    idx = pd.date_range(fx.DATA_START, periods=n_hours, freq="1h", tz="UTC")
    raw = {}
    for i, c in enumerate(("BTC",) + coins):
        close = 100.0 * np.exp(np.arange(1, n_hours + 1) * (0.0004 + 0.0001 * i))
        o = np.r_[100.0, close[:-1]]
        raw[c] = pd.DataFrame({"open": o, "high": close * 1.0001, "low": o, "close": close, "volume": 1e6}, index=idx)
    return syn.make_data(raw, "1h")


def test_at14_maker_order_misses_without_trade_through_and_releases_its_quote():
    d = _rising_universe()
    m = ir.Market(d, fx.WIN_START, 10)
    sd = ir.SetData(m, SPEC["F1-01"])
    with risk_log() as log:
        res = ir.simulate(m, sd, fx.TAKER, fx.risk_cfg(), fill_model="maker")
    # every bar is a new high (an F1 breakout) but no low ever trades 5 bp through the limit (low ==
    # open == the previous close == the limit): every order misses, nothing fills
    assert len(res.trades) == 0 and len(res.missed) == res.approvals > 10
    by_t = {}
    for row in log:
        by_t.setdefault(row["now"], []).append(row)
    for rows in by_t.values():
        committed = 0.0
        for row in rows:  # the reserved quote is released at the next decision close
            assert row["cash"] == pytest.approx(10_000.0 - committed) and row["open_value"] == pytest.approx(committed)
            if row["allowed"]:
                committed += row["quote"]


def test_at14_maker_fill_at_limit_with_maker_fee_no_slippage_within_one_decision_bar():
    n_total, n_missed = 0, 0
    for spec_id, data in (("F1-01", fx.trend_1h()), ("F1-07", fx.trend_1h()), ("F2-09", fx.noise_1h()),
                          ("F2-07", fx.noise_1h())):
        m = ir.Market(data, fx.WIN_START, fx.WIN_DAYS)
        sd = ir.SetData(m, SPEC[spec_id])
        res = ir.simulate(m, sd, fx.TAKER, fx.risk_cfg(), fill_model="maker")
        fi = ir.TRADE_FIELDS.index
        n_total += len(res.trades)
        n_missed += len(res.missed)
        for t in res.trades:
            s, dsig, k = t[fi("s")], t[fi("d_signal")], t[fi("fill_k")]
            limit = sd.close_dec[s, dsig]
            bi = dsig * sd.r
            assert bi < k <= bi + sd.r                               # within one decision bar
            assert m.L[s, k] <= limit * (1 - 0.0005)                 # traded through
            assert not (m.L[s, bi + 1:k] <= limit * (1 - 0.0005)).any()  # first such bar
            assert t[fi("entry_px")] == limit == t[fi("entry_px_raw")]
            notional = t[fi("quote")] - t[fi("fee_entry")]
            assert t[fi("fee_entry")] == pytest.approx(notional * 0.0022)
            if t[fi("reason")] == "target":
                assert sd.family == 2 and t[fi("exit_px")] == t[fi("target")] == t[fi("exit_px_raw")]
                assert t[fi("fee_exit")] == pytest.approx(t[fi("qty")] * t[fi("exit_px")] * 0.0022)
            else:
                assert t[fi("exit_px")] == pytest.approx(t[fi("exit_px_raw")] * (1 - 0.0005))
                assert t[fi("fee_exit")] == pytest.approx(t[fi("qty")] * t[fi("exit_px")] * 0.0038)
        for s, dd, limit in res.missed:
            bi = dd * sd.r
            hi = min(bi + sd.r, m.Tb - 1)
            assert not (m.L[s, bi + 1:hi + 1] <= limit * (1 - 0.0005)).any()
    assert n_total > 10 and n_missed > 0


# --------------------------------------------------------------------------- #
# AT17 sanity (playbook §2 check 1)
# --------------------------------------------------------------------------- #
def test_at17_clean_uptrend_family1_goes_long_and_wins_at_zero_cost():
    d = _rising_universe()
    m = ir.Market(d, fx.WIN_START, 20)
    sd = ir.SetData(m, SPEC["F1-02"])
    res = ir.simulate(m, sd, fx.ZERO, fx.risk_cfg())
    assert len(res.trades) > 0 and res.final_equity > 10_000.0


def test_at17_downtrend_or_regime_off_no_family1_or_family3_position():
    up = _rising_universe()
    btc = up["native_1h"]["BTC"]
    down = btc.copy()
    down[["open", "high", "low", "close"]] = 1e4 / btc[["open", "low", "high", "close"]].to_numpy()
    raw = {c: (f.copy() for f in [up["native_1h"][c]]).__next__() for c in up["native_1h"]}
    for c in raw:
        raw[c].index = raw[c].index - pd.Timedelta(hours=1)
    down.index = down.index - pd.Timedelta(hours=1)
    raw["BTC"] = down[["open", "high", "low", "close", "volume"]]
    d = syn.make_data(raw, "1h")
    m = ir.Market(d, fx.WIN_START, 20)
    assert not ir.SetData(m, SPEC["F1-01"]).regime.any()
    for sid in ("F1-01", "F1-12", "F3-01", "F3-12"):
        res = ir.simulate(m, ir.SetData(m, SPEC[sid]), fx.ZERO, fx.risk_cfg())
        assert len(res.trades) == 0 and res.checks == 0, sid


def test_at17_mean_reverting_range_family2_is_gross_positive_at_zero_cost():
    rng = np.random.default_rng(4)
    n = 24 * 360
    idx = pd.date_range(fx.DATA_START, periods=n, freq="1h", tz="UTC")
    raw = {"BTC": syn.ohlcv_from_logpath(idx, np.cumsum(rng.normal(0.00004, 0.002, n)), rng)}
    for c in ("ETH", "SOL", "ADA"):
        x = np.zeros(n)
        eps = rng.normal(0, 0.01, n)
        for t in range(1, n):
            x[t] = 0.85 * x[t - 1] + eps[t]
        raw[c] = syn.ohlcv_from_logpath(idx, x, rng, wick=0.001)
    d = syn.make_data(raw, "1h")
    m = ir.Market(d, fx.WIN_START, 60)
    tot = 0.0
    for sid in ("F2-07", "F2-09"):
        res = ir.simulate(m, ir.SetData(m, SPEC[sid]), fx.ZERO, fx.risk_cfg())
        fi = ir.TRADE_FIELDS.index
        assert len(res.trades) > 10
        tot += sum(t[fi("gross_pnl")] for t in res.trades)
        assert res.final_equity > 10_000.0
    assert tot > 0


# --------------------------------------------------------------------------- #
# AT7 simulator part: masks, listing age
# --------------------------------------------------------------------------- #
def test_at7_no_15m_entry_or_open_position_of_a_masked_coin_inside_a_mask():
    d = fx.noise_15m()
    m = ir.Market(d, pd.Timestamp("2023-02-25", tz="UTC"), 16)
    blocked_total = 0
    for sid in ("F2-01", "F2-05"):
        sd = ir.SetData(m, SPEC[sid])
        res = ir.simulate(m, sd, fx.TAKER, fx.risk_cfg(), log_rejections=True)
        assert p2.mask_violations(res, m) == 0
        blocked_total += sum(1 for r in res.rejections if r[2] == "blocked" and r[3])
        assert sd.mask_ok[m.coins.index("BTC")].all()
        for c in ("SOL", "ADA"):
            assert not sd.mask_ok[m.coins.index(c)].all()
        # positions of masked coins never overlap 2023-03-04 (the fixture's mask day)
        for t in res.trades:
            c = m.coins[t[0]]
            if c in ("SOL", "ADA"):
                a = pd.Timestamp(t[ir.TRADE_FIELDS.index("entry_ns")], tz="UTC")
                b = pd.Timestamp(t[ir.TRADE_FIELDS.index("exit_ns")], tz="UTC")
                assert b <= pd.Timestamp("2023-03-04", tz="UTC") or a >= pd.Timestamp("2023-03-05", tz="UTC")
    assert blocked_total > 0


def test_at7_a_coin_with_fewer_than_60_daily_bars_never_enters():
    d = syn.universe_1h(["ETH", "SOL"], fx.DATA_START, 24 * 360, seed=13, sigma=0.012)
    late = d["native_1h"]["SOL"]
    late = late[late.index >= fx.WIN_START - pd.Timedelta(days=50)]
    late.attrs = d["native_1h"]["SOL"].attrs
    d["native_1h"]["SOL"] = d["base"]["SOL"] = late
    d["daily"]["SOL"] = idata.daily_from_1h(late)
    m = ir.Market(d, fx.WIN_START, 9)  # SOL has < 60 complete days for the whole window
    for sid in ("F1-01", "F2-07", "F3-01"):
        sd = ir.SetData(m, SPEC[sid])
        assert not sd.elig[m.coins.index("SOL")].any()
        res = ir.simulate(m, sd, fx.TAKER, fx.risk_cfg())
        assert all(m.coins[t[0]] != "SOL" for t in res.trades)


# --------------------------------------------------------------------------- #
# Simulator-level no look-ahead: perturbing the future changes no earlier decision
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("spec_id,which", [("F1-01", "trend"), ("F1-07", "trend"), ("F2-07", "noise"), ("F3-01", "trend")])
def test_perturbing_bars_after_t_changes_no_decision_before_t(spec_id, which):
    d = fx.trend_1h() if which == "trend" else fx.noise_1h()
    T = fx.WIN_START + pd.Timedelta(days=22)
    rng = np.random.default_rng(1)
    raw = {}
    for c, f in d["native_1h"].items():
        g = f.copy()
        late = g.index > T
        k = rng.uniform(0.7, 1.3, late.sum())
        for col in ("open", "high", "low", "close"):
            g.loc[late, col] = g.loc[late, col] * k
        g["high"] = g[["open", "high", "low", "close"]].max(axis=1)
        g["low"] = g[["open", "high", "low", "close"]].min(axis=1)
        g.index = g.index - pd.Timedelta(hours=1)
        raw[c] = g
    d2 = syn.make_data(raw, "1h")
    out = []
    for data in (d, d2):
        m = ir.Market(data, fx.WIN_START, fx.WIN_DAYS)
        sd = ir.SetData(m, SPEC[spec_id])
        with risk_log() as log:
            res = ir.simulate(m, sd, fx.TAKER, fx.risk_cfg())
        tf = ir.trades_frame(res, m, sd, "B")
        out.append((log, tf))
    (la, ta), (lb, tb) = out
    pre_a = [(r["symbol"], r["now"], r["allowed"], r["quote"]) for r in la if r["now"] <= T]
    pre_b = [(r["symbol"], r["now"], r["allowed"], r["quote"]) for r in lb if r["now"] <= T]
    assert pre_a == pre_b and len(pre_a) > 3
    # an exit stamped at time T fills at the open of the bar closing at T+1h, which is perturbed
    done_a = ta[ta["exit_time"] < T].reset_index(drop=True)
    done_b = tb[tb["exit_time"] < T].reset_index(drop=True)
    pd.testing.assert_frame_equal(done_a, done_b)
    assert len(la) != len(lb) or not ta.equals(tb)  # the perturbation did change the future
