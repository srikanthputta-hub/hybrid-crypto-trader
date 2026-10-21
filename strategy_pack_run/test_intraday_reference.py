"""AT11 (risk-engine parity) and AT18 (independent second implementation) of
docs/intraday_harness_changes.md.

intraday_reference.Ref is a deliberately slow bar-by-bar loop written from the prereg text; it
shares no code with the runner. AT11: for the same fixture, the (time, symbol, allowed,
max_quote_amount) sequence of RiskEngine.check_new_trade calls is identical between the runner and
the reference (which commits each approved quote immediately, apply_buy-style), including on
fixtures built so the 5-per-120-min cap, the cooldown and the daily-loss halt each fire. AT18: the
trade lists (symbol, entry/exit time, entry/exit raw price, reason) are identical, on synthetic
data with gaps and on real BTC+ETH+LTC bars before 2021-03-01 (the only real data allowed before
the P2 run, prereg §0 rule 3; trade lists only, no performance statistic is computed or printed).

Run: pytest strategy_pack_run/test_intraday_reference.py -q
"""
from __future__ import annotations

import sys
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
from intraday_reference import Ref  # noqa: E402
from src.risk_engine import RiskEngine  # noqa: E402

SPEC = {s.set_id: s for g in fam.GRIDS.values() for s in g}
FEE, SLIP = 0.0038, 0.0005


def _runner(data, start, days, spec, risk_cfg, touch=False, shift=0):
    m = ir.Market(data, start, days)
    sd = ir.SetData(m, spec)
    log = []
    orig = RiskEngine.check_new_trade

    def wrapped(self, symbol, side, *a, **k):
        d = orig(self, symbol, side, *a, **k)
        log.append((pd.Timestamp(k["now"]), symbol, d.allowed, d.max_quote_amount, k["risk_reward"], d.reason,
                    k["open_positions_value"], k["portfolio_equity"]))
        return d

    raw = rmat = None
    if shift:
        if spec.family == 3:
            rmat = np.roll(sd.rmat, shift * sd.bpd_dec, axis=1)
        else:
            raw = np.roll(sd.raw, shift * sd.bpd_dec, axis=1)
    with patch.object(RiskEngine, "check_new_trade", wrapped):
        res = ir.simulate(m, sd, ir.CostModel("t", FEE, SLIP), risk_cfg, "touch" if touch else "close", raw=raw, rmat=rmat)
    return ir.trades_frame(res, m, sd, "syn"), log, res


def _reference(data, start, days, spec, risk_cfg, touch=False, shift=0):
    log = []
    coins = sorted(data["base"])
    ref = Ref(data, coins, start, days, spec.family, spec.tf, spec.params, risk_cfg, FEE, SLIP, touch=touch,
              shift_days=shift, log_checks=log)
    return pd.DataFrame(ref.run()), log


def _same_trades(a: pd.DataFrame, b: pd.DataFrame):
    assert len(a) == len(b)
    if not len(a):
        return
    a = a.sort_values(["entry_time", "symbol"]).reset_index(drop=True)
    b = b.sort_values(["entry_time", "symbol"]).reset_index(drop=True)
    assert (a["symbol"] == b["symbol"]).all()
    assert (a["signal_time"] == b["signal_time"]).all()
    assert (a["entry_time"] == b["entry_time"]).all() and (a["exit_time"] == b["exit_time"]).all()
    assert np.allclose(a["entry_px_raw"], b["entry_px_raw"], rtol=0, atol=0)
    assert np.allclose(a["exit_px_raw"], b["exit_px_raw"], rtol=0, atol=1e-12)
    assert (a["exit_reason"].to_numpy() == b["reason"].to_numpy()).all()


def _compare(data, start, days, spec_id, risk_cfg=None, touch=False, shift=0):
    spec = SPEC[spec_id]
    risk_cfg = risk_cfg or fx.risk_cfg()
    ta, la, res = _runner(data, start, days, spec, risk_cfg, touch, shift)
    tb, lb = _reference(data, start, days, spec, risk_cfg, touch, shift)
    _same_trades(ta, tb)
    # identical decisions; quotes equal up to float summation order (equity is summed in a different
    # order by the two implementations: ~1e-13 relative), compared at 1e-9 relative
    assert [(t, s, ok) for t, s, ok, q, *_ in la] == [(t, s, ok) for t, s, ok, q in lb]
    for x, y in zip(la, lb):
        assert (x[3] is None and y[3] is None) or abs(x[3] - y[3]) <= 1e-9 * abs(y[3])
    return la, res


# --------------------------------------------------------------------------- #
# AT18 on synthetic data (all families, close and touch, real and shifted signals)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("spec_id,touch", [("F1-01", False), ("F1-01", True), ("F1-07", False), ("F1-11", True),
                                           ("F3-01", False), ("F3-04", True), ("F3-10", False)])
def test_at18_reference_reproduces_runner_on_trending_synthetic_with_gaps(spec_id, touch):
    la, _ = _compare(fx.trend_1h(), fx.WIN_START, 30, spec_id, touch=touch)
    assert sum(1 for x in la if x[2]) > 3


@pytest.mark.parametrize("spec_id,touch", [("F2-07", False), ("F2-07", True), ("F2-11", False)])
def test_at18_reference_reproduces_runner_family2_1h(spec_id, touch):
    la, _ = _compare(fx.noise_1h(sigma=0.005), fx.WIN_START, 30, spec_id, touch=touch)
    assert sum(1 for x in la if x[2]) > 3


def test_at18_reference_reproduces_runner_family2_15m_across_a_mask():
    la, _ = _compare(fx.calm_noise_15m(), pd.Timestamp("2023-03-01", tz="UTC"), 12, "F2-01")
    assert sum(1 for x in la if x[2]) > 3


@pytest.mark.parametrize("spec_id,shift", [("F1-01", 7), ("F3-02", 11), ("F2-07", 5)])
def test_at18_reference_reproduces_runner_on_shifted_placebo_signals(spec_id, shift):
    data = fx.noise_1h(sigma=0.005) if spec_id.startswith("F2") else fx.trend_1h()
    _compare(data, fx.WIN_START, 30, spec_id, shift=shift)


REAL = idata.data_root()
HAVE_REAL = (REAL / "1h" / "BTC_USD_1h.csv").exists()


@pytest.mark.skipif(not HAVE_REAL, reason="intraday snapshot not present")
@pytest.mark.parametrize("spec_id", ["F1-01", "F2-09", "F3-05"])
def test_at18_reference_reproduces_runner_on_real_btc_eth_ltc_before_2021_03_01(spec_id):
    cut = pd.Timestamp("2021-03-01", tz="UTC")
    n1h = {c: idata.load_native(c, "1h", REAL, end=cut) for c in ("BTC", "ETH", "LTC")}
    assert all(f.index.max() <= cut for f in n1h.values())
    data = dict(tf="1h", universe=("BTC", "ETH", "LTC"), native_1h=n1h, base=n1h,
                daily={c: idata.daily_from_1h(f) for c, f in n1h.items()})
    start, days = pd.Timestamp("2020-09-01", tz="UTC"), 181
    assert start + pd.Timedelta(days=days) == cut
    la, _ = _compare(data, start, days, spec_id)
    assert len(la) > 0


# --------------------------------------------------------------------------- #
# AT11 risk-engine parity and binding limits
# --------------------------------------------------------------------------- #
def test_at11_committed_notional_never_exceeds_40_percent_and_matches_reference():
    la, res = _compare(fx.many_1h(), fx.WIN_START, 20, "F1-01")
    approved = [x for x in la if x[2]]
    assert len(approved) > 10
    for t, s, ok, q, rr, reason, open_value, equity in approved:
        assert open_value + q <= 0.40 * equity + 1e-9
    assert any(x[5].startswith("no room left") for x in la)  # the 40% cap binds on this fixture


def test_at11_p6_same_bar_approvals_commit_room_immediately_and_match_reference():
    """Correlated breakouts: several approvals on ONE decision bar while the 40% cap binds. Each
    later check on that bar sees the earlier approved gross quotes in open_positions_value and out of
    cash_available (P6), so the bar's commitments never exceed 40% of equity."""
    la, res = _compare(fx.correlated_1h(), fx.WIN_START, 30, "F1-01")
    by_t = {}
    for x in la:
        by_t.setdefault(x[0], []).append(x)
    multi = 0
    for t, rows in by_t.items():
        approved = [x for x in rows if x[2]]
        if len(approved) >= 2:
            multi += 1
        base_open = rows[0][6]
        acc = 0.0
        for x in rows:
            assert x[6] == pytest.approx(base_open + acc, rel=1e-12, abs=1e-9)   # earlier same-bar quotes counted
            if x[2]:
                acc += x[3]
                assert x[6] + x[3] <= 0.40 * x[7] + 1e-9
    assert multi >= 2 and any(x[5].startswith("no room left") for x in la)


def test_at11_rate_cap_fires_and_matches_reference():
    rc = fx.risk(max_total_exposure_pct=1.0, max_position_pct=0.05)
    la, res = _compare(fx.correlated_1h(), fx.WIN_START, 20, "F1-01", risk_cfg=rc)
    assert any(x[5].startswith("new-entry rate cap") for x in la)
    assert res.reject_reasons.get("rate_cap", 0) > 0


def test_at11_cooldown_fires_and_matches_reference():
    # family 2 re-signals within hours of a quick target / stop exit; a 600-minute cooldown binds
    rc = fx.risk(cooldown_minutes=600, max_total_exposure_pct=1.0, max_position_pct=0.05,
                 max_new_entries_per_window=50)
    la, res = _compare(fx.correlated_1h(), fx.WIN_START, 30, "F2-07", risk_cfg=rc, touch=True)
    assert res.reject_reasons.get("cooldown", 0) > 0


def test_at11_daily_loss_halt_fires_and_matches_reference():
    rc = fx.risk(max_daily_loss_pct=0.002)
    la, res = _compare(fx.many_1h(), fx.WIN_START, 20, "F1-01", risk_cfg=rc)
    assert res.reject_reasons.get("daily_loss", 0) > 0


def test_at11_risk_reward_none_skips_only_the_rr_check():
    rc = fx.risk(min_risk_reward=50.0)  # no fixed-target plan could ever pass an R:R check this high
    la1, r1 = _compare(fx.trend_1h(), fx.WIN_START, 20, "F1-01", risk_cfg=rc)
    la3, r3 = _compare(fx.trend_1h(), fx.WIN_START, 20, "F3-01", risk_cfg=rc)
    assert all(x[4] is None for x in la1 + la3)
    assert sum(1 for x in la1 if x[2]) > 0 and sum(1 for x in la3 if x[2]) > 0
    la2, r2 = _compare(fx.noise_1h(sigma=0.005), fx.WIN_START, 20, "F2-07", risk_cfg=rc)
    assert all(x[4] is not None for x in la2) and not any(x[2] for x in la2)
    rr_checked = [x for x in la2 if not x[5].startswith("market volatility")]  # vol check runs first
    assert r2.reject_reasons.get("risk_reward", 0) == len(rr_checked) > 0
