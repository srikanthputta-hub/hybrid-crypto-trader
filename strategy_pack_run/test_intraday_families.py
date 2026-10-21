"""AT8 (docs/intraday_harness_changes.md): no look-ahead in the three signal builders. For every
family and timeframe, every output column at bar i is identical whether the inputs are truncated
right after bar i (decision frame cut at i, daily frames cut to days CLOSED at or before bar i's
close, the BTC regime recomputed from that truncated daily history) or not -- checked at EVERY one
of the last 600 bars of the fixture (the test_universe_eligibility truncation pattern).

Run: pytest strategy_pack_run/test_intraday_families.py -q
"""
from __future__ import annotations

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
import intraday_families as fam  # noqa: E402
import intraday_fixtures as fx  # noqa: E402

N_CHECK = 600


def _frames(tf: str):
    if tf == "15m":
        d = fx.noise_15m()
        return d["base"]["SOL"], d["daily"]["SOL"], d["daily"]["BTC"]
    d = fx.noise_1h()
    f = d["native_1h"]["SOL"]
    if tf == "4h":
        f = idata.four_hour_from_1h(f)
    return f, d["daily"]["SOL"], d["daily"]["BTC"]


def _assert_rows_equal(a: pd.Series, b: pd.Series, where):
    for k in a.index:
        x, y = a[k], b[k]
        if isinstance(x, float) and np.isnan(x):
            assert isinstance(y, float) and np.isnan(y), (where, k, x, y)
        else:
            assert x == y, (where, k, x, y)


def _check(tf: str, build):
    df, daily, btc_daily = _frames(tf)
    # end mid-day so the last rows sit next to a forming day; keep a fixed 1,500-bar lead-in (the
    # same start for the full and every truncated run, so only the right-hand cut differs)
    df = df.iloc[max(0, len(df) - 3 - N_CHECK - 1500): len(df) - 3]
    full = build(df, daily, idata.btc_regime(btc_daily))
    assert full["eligible"].iloc[-N_CHECK:].any(), "fixture must be warm in the checked range"
    for i in range(len(df) - N_CHECK, len(df)):
        t = df.index[i]
        d_cut, b_cut = daily[daily.index <= t], btc_daily[btc_daily.index <= t]
        part = build(df.iloc[: i + 1], d_cut, idata.btc_regime(b_cut))
        _assert_rows_equal(part.iloc[-1], full.iloc[i], (tf, i))


@pytest.mark.parametrize("tf", ["1h", "4h"])
def test_at8_family1_no_lookahead_every_bar(tf):
    N = fam.bars_in(tf, 72)
    _check(tf, lambda df, d, r: fam.family1_signals(df, d, r, N, 1.5))


@pytest.mark.parametrize("tf", ["15m", "1h"])
def test_at8_family2_no_lookahead_every_bar(tf):
    _check(tf, lambda df, d, r: fam.family2_signals(df, d, r, 20, 2.0))


def test_at8_family3_state_no_lookahead_every_bar():
    d = fx.noise_1h()
    c4 = {c: idata.four_hour_from_1h(d["native_1h"][c])["close"] for c in ("ETH", "SOL", "ADA")}
    daily = {c: d["daily"][c] for c in c4}
    reg = idata.btc_regime(d["daily"]["BTC"])
    full = fam.family3_state(c4, daily, reg, 18)
    axis = full["axis"]
    checked = 0
    for i in range(len(axis) - N_CHECK, len(axis)):
        t = axis[i]
        part = fam.family3_state({c: s[s.index <= t] for c, s in c4.items()},
                                 {c: f[f.index <= t] for c, f in daily.items()},
                                 idata.btc_regime(d["daily"]["BTC"][d["daily"]["BTC"].index <= t]), 18, axis=axis[: i + 1])
        for key in ("r", "eligible", "atr_d", "atr_pct"):
            a, b = part[key].iloc[-1], full[key].iloc[i]
            pd.testing.assert_series_equal(a, b, check_names=False)
        assert part["regime_on"][-1] == full["regime_on"][i]
        checked += 1
    assert checked == N_CHECK and full["eligible"].iloc[-N_CHECK:].to_numpy().any()


def test_at8_daily_inputs_use_only_days_closed_at_or_before_the_decision_close():
    df, daily, btc_daily = _frames("1h")
    out = fam.family1_signals(df, daily, idata.btc_regime(btc_daily), 72, 1.0)
    di = idata.daily_inputs(daily)
    for t in df.index[-200:]:
        closed = di[di.index <= t]
        assert out.loc[t, "atr_d"] == closed["atr_d"].iloc[-1]
    # perturbing the forming day (closes after t) changes nothing at t
    t = df.index[-30]
    d2 = daily.copy()
    d2.loc[d2.index > t, ["high", "close"]] *= 3.0
    out2 = fam.family1_signals(df, d2, idata.btc_regime(btc_daily), 72, 1.0)
    pd.testing.assert_frame_equal(out.loc[:t], out2.loc[:t])


def test_family3_ranks_order_ties_by_symbol_and_skip_non_positive():
    r = np.array([[0.05, 0.05, -0.01, 0.10, np.nan]])
    el = np.array([[True, True, True, True, True]])
    ranks = fam.family3_ranks(r, el, None)
    assert ranks.tolist() == [[2, 3, 0, 1, 0]]
    el2 = np.array([[True, False, True, True, True]])
    assert fam.family3_ranks(r, el2, None).tolist() == [[2, 0, 0, 1, 0]]


def test_neighbour_rule_counts_one_step_in_exactly_one_parameter():
    f1 = {s.set_id: s for s in fam.F1_GRID}
    assert sorted(s.set_id for s in fam.neighbours(f1["F1-02"])) == ["F1-01", "F1-03", "F1-05", "F1-08"]
    assert sorted(s.set_id for s in fam.neighbours(f1["F1-01"])) == ["F1-02", "F1-04", "F1-07"]
    f3 = {s.set_id: s for s in fam.F3_GRID}
    assert sorted(s.set_id for s in fam.neighbours(f3["F3-05"])) == ["F3-01", "F3-06", "F3-07", "F3-09"]
    f2 = {s.set_id: s for s in fam.F2_GRID}
    assert sorted(s.set_id for s in fam.neighbours(f2["F2-03"])) == ["F2-01", "F2-04", "F2-05", "F2-09"]
