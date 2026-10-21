"""AT15 (placebo), AT16 (bootstrap and stats) and the gate / verdict logic of
docs/intraday_prereg.md §6, on hand-computed cases.

Run: pytest strategy_pack_run/test_intraday_stats.py -q
"""
from __future__ import annotations

import math
import os
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

import intraday_families as fam  # noqa: E402
import intraday_fixtures as fx  # noqa: E402
import intraday_runner as ir  # noqa: E402
import intraday_stats as st  # noqa: E402
import run_intraday_p2 as p2  # noqa: E402

D = st.NS_DAY
S0 = st.B_START


def ns(ts: str) -> int:
    return pd.Timestamp(ts, tz="UTC").value


# --------------------------------------------------------------------------- #
# AT16 bootstrap, purge, folds
# --------------------------------------------------------------------------- #
def test_at16_blocks_are_built_from_the_window_start():
    e = np.array([S0.value + int(0.5 * D), S0.value + int(1.9 * D), S0.value + 2 * D, S0.value + int(5.99 * D)])
    assert st.block_ids(e, S0, 2).tolist() == [0, 0, 1, 2]
    assert st.block_ids(e, S0, 1).tolist() == [0, 1, 2, 5]


def test_at16_block_days_is_ceil_of_median_hold_at_least_one():
    e = np.zeros(3, dtype=np.int64)
    assert st.block_days_for(e, e + int(0.4 * D)) == 1
    assert st.block_days_for(e, np.array([int(2.2 * D), int(2.5 * D), int(9 * D)])) == 3
    assert st.block_days_for(e, np.array([2 * D, 2 * D, 2 * D])) == 2


def test_at16_bootstrap_is_the_registered_scheme_and_deterministic():
    rng = np.random.default_rng(5)
    e = S0.value + np.sort(rng.integers(0, 300, 200)) * D
    R = rng.normal(0.1, 1.5, 200)
    a = st.block_bootstrap(R, e, S0, 3, n=3000)
    b = st.block_bootstrap(R, e, S0, 3, n=3000)
    assert a["pct"] == b["pct"]
    ids = st.block_ids(e, S0, 3)
    u, inv = np.unique(ids, return_inverse=True)
    sums, cnt = np.bincount(inv, weights=R), np.bincount(inv).astype(float)
    idx = np.random.default_rng(1).integers(0, len(u), size=(3000, len(u)))  # one draw of nb blocks per resample
    means = sums[idx].sum(1) / cnt[idx].sum(1)
    for p in st.TRADES_PCTS:
        assert a["pct"][p] == np.percentile(means, p, method="linear")
    assert a["n_blocks"] == len(u)


def test_at16_design_effect_is_one_on_an_iid_fixture():
    rng = np.random.default_rng(2)
    R = rng.normal(0, 1, 400)
    e = S0.value + np.arange(400) * D  # one trade per day-block: block bootstrap == iid bootstrap
    b = st.block_bootstrap(R, e, S0, 1, n=5000)
    assert b["DE"] == pytest.approx(1.0, abs=1e-12)
    e2 = S0.value + np.sort(rng.integers(0, 250, 400)) * D  # 1-5 independent trades per block
    b2 = st.block_bootstrap(R, e2, S0, 1, n=20000)
    assert 0.85 < b2["DE"] < 1.15


def test_at16_design_effect_grows_with_same_block_correlation():
    rng = np.random.default_rng(3)
    days = np.repeat(np.arange(100), 5)
    shock = rng.normal(0, 1, 100)[days]
    R = shock + 0.3 * rng.normal(0, 1, 500)
    b = st.block_bootstrap(R, S0.value + days * D, S0, 1, n=20000)
    assert b["DE"] > 3 and b["n_eff"] < 500 / 3


def test_at16_purge_drops_straddlers_from_is_score_and_from_oos():
    cut = st.CUT_B
    e = np.array([ns("2025-04-10"), ns("2025-04-13 12:00"), ns("2025-04-14"), ns("2025-04-20"), ns("2025-04-13 23:00")])
    x = np.array([ns("2025-04-12"), ns("2025-04-14 01:00"), ns("2025-04-15"), ns("2025-05-01"), ns("2025-04-14")])
    is_m, oos, strad = st.purged_split(e, x, cut)
    assert is_m.tolist() == [True, False, False, False, False]
    assert oos.tolist() == [False, False, True, True, False]
    assert strad.tolist() == [False, True, False, False, True]


def test_at16_fold_f1_includes_the_2021_stub_and_scoring_rules():
    e = np.array([ns("2021-10-02")] * 10 + [ns("2022-06-01")] * 25 + [ns("2023-05-01")] * 30 + [ns("2026-05-01")] * 40)
    R = np.array([-1.0] * 10 + [0.5] * 25 + [-0.1] * 30 + [0.2] * 40)
    f = {x["fold"]: x for x in st.folds(e, R)}
    assert f["F1"]["n"] == 35 and f["F1"]["scored"] and not f["F1"]["losing"]
    assert f["F1"]["avgR"] == pytest.approx((-10 + 12.5) / 35)
    assert f["F2"]["n"] == 30 and f["F2"]["scored"] and f["F2"]["losing"]
    assert f["F3"]["n"] == 0 and not f["F3"]["scored"]
    assert f["F5"]["n"] == 40 and f["F5"]["scored"]
    assert st.fold_of(np.array([ns("2021-12-31"), ns("2026-10-19 23:00")])).tolist() == ["F1", "F5"]


def test_daily_sharpe_and_drawdown_hand_values():
    eq = np.array([100.0, 101.0, 100.0, 102.0])
    r = np.array([0.01, -1 / 101, 0.02])
    assert st.daily_sharpe(eq) == pytest.approx(r.mean() / r.std(ddof=1) * math.sqrt(365))
    dd, pk, tr = st.max_drawdown(np.array([1.0, 2.0, 1.5, 3.0, 1.0]))
    assert (dd, pk, tr) == (pytest.approx(-2 / 3), 3, 4)


def test_buy_hold_sleeves_join_on_first_eligible_day_and_masked_days_return_zero():
    closes = np.array([[1.0, 2.0, 4.0, 4.0], [10.0, 10.0, 20.0, 40.0]])
    eq = st.buy_hold_daily(closes, np.array([0, 2]), None, 100.0)
    # sleeve 0: 50 -> 100 -> 200 -> 200 ; sleeve 1 cash until index 2, then x2 on the last day
    assert eq.tolist() == [100.0, 150.0, 250.0, 300.0]
    masked = np.array([[False, True, False, False], [False, False, False, False]])
    eq2 = st.buy_hold_daily(closes, np.array([0, 2]), masked, 100.0)
    assert eq2.tolist() == [100.0, 150.0, 150.0, 200.0]


# --------------------------------------------------------------------------- #
# Champion rule
# --------------------------------------------------------------------------- #
def test_champion_highest_is_avg_r_with_150_trades_ties_more_trades_then_lower_set():
    rows = [dict(set_no=1, is_n=149, is_avgR=9.0), dict(set_no=2, is_n=150, is_avgR=0.2),
            dict(set_no=3, is_n=400, is_avgR=0.2 + 5e-10), dict(set_no=4, is_n=400, is_avgR=0.2)]
    assert st.champion(rows)["set_no"] == 3
    rows[2]["is_n"] = 300
    assert st.champion(rows)["set_no"] == 4  # tie within 1e-9 -> more IS trades
    rows[3]["is_n"] = 300
    assert st.champion(rows)["set_no"] == 3  # still tied -> lower set number
    assert st.champion([dict(set_no=1, is_n=10, is_avgR=1.0)]) is None


# --------------------------------------------------------------------------- #
# Gates and verdict (hand-computed)
# --------------------------------------------------------------------------- #
def _gi(**over):
    folds = [dict(fold=f"F{i}", n=50, avgR=0.1, scored=True, losing=False) for i in range(1, 6)]
    base = dict(n_trades=300, n_blocks=100, g2_lower=0.01, oos_n=60, oos_avgR=0.05, folds=folds,
                g4=dict(ge_avgR=2, ge_return=2), ret_taker=0.1, sharpe=1.0, bench_sharpe=0.9, ret_stress=0.01,
                a_avgR=0.01, a_ret=0.01, neighbour_avgR=[0.1, -0.1, 0.2])
    base.update(over)
    return st.GateInputs(**base)


def test_gates_all_pass_at_the_exact_boundaries():
    g = st.evaluate_gates(_gi())
    assert all(g[k] for k in ("G1", "G2", "G3", "G4", "G5", "R1", "R2")) and st.verdict(g) == "PASS"


@pytest.mark.parametrize("over,gate", [
    (dict(n_trades=299), "G1"), (dict(n_blocks=99), "G1"), (dict(g2_lower=0.0), "G2"),
    (dict(oos_avgR=0.0), "G3"), (dict(g4=dict(ge_avgR=3, ge_return=0)), "G4"), (dict(g4=dict(ge_avgR=0, ge_return=3)), "G4"),
    (dict(ret_taker=0.0), "G5"), (dict(sharpe=0.9), "G5"), (dict(ret_stress=-1e-9), "G5"),
    (dict(a_avgR=0.0), "R1"), (dict(a_ret=-0.1), "R1"), (dict(neighbour_avgR=[0.1, -0.1, -0.2]), "R2"),
])
def test_each_gate_fails_just_past_its_boundary(over, gate):
    g = st.evaluate_gates(_gi(**over))
    assert not g[gate] and all(g[k] for k in ("G1", "G2", "G3", "G4", "G5", "R1", "R2") if k != gate)


def test_r2_needs_half_of_neighbours_rounded_up():
    assert st.evaluate_gates(_gi(neighbour_avgR=[0.1, -0.1, -0.1, 0.2]))["R2"]          # 2 of 4
    assert not st.evaluate_gates(_gi(neighbour_avgR=[0.1, -0.1, -0.1, -0.2]))["R2"]     # 1 of 4
    assert st.evaluate_gates(_gi(neighbour_avgR=[0.1, 0.1, -0.1]))["R2"]                # 2 of 3


def test_g3_fold_rules_scored_count_and_losing_count():
    folds = [dict(fold=f"F{i}", n=50, avgR=-0.1 if i < 3 else 0.1, scored=True, losing=i < 3) for i in range(1, 6)]
    g = st.evaluate_gates(_gi(folds=folds))
    assert not g["G3"] and not g["G3_count_only"] and st.verdict(g) == "FAIL"   # 2 losing folds
    folds2 = [dict(fold=f"F{i}", n=50 if i < 3 else 10, avgR=0.1, scored=i < 3, losing=False) for i in range(1, 6)]
    g2 = st.evaluate_gates(_gi(folds=folds2))
    assert not g2["G3"] and g2["G3_count_only"] and st.verdict(g2) == "INCONCLUSIVE"  # only 2 scored


def test_verdict_inconclusive_fail_void():
    assert st.verdict(st.evaluate_gates(_gi(n_trades=200))) == "INCONCLUSIVE"
    assert st.verdict(st.evaluate_gates(_gi(oos_n=59))) == "INCONCLUSIVE"
    assert st.verdict(st.evaluate_gates(_gi(oos_n=59, oos_avgR=-0.2))) == "FAIL"
    assert st.verdict(st.evaluate_gates(_gi(n_trades=200, g2_lower=-0.1))) == "FAIL"
    assert st.verdict(st.evaluate_gates(_gi(n_trades=200, oos_n=10))) == "INCONCLUSIVE"
    assert st.verdict(None) == "FAIL"
    assert st.verdict(st.evaluate_gates(_gi()), integrity_ok=False) == "VOID"


def test_g4_counts_draws_without_a_champion_as_not_beaten():
    draws = [dict(avgR=0.1, total_return=0.1)] * 197 + [None, dict(avgR=0.5, total_return=0.0), dict(avgR=0.3, total_return=0.3)]
    g = st.g4_counts(0.3, 0.2, draws)
    # >= real on avg R: None, 0.5, 0.3 (ties count as not beaten) = 3 ; on return: None, 0.3 = 2
    assert (g["ge_avgR"], g["ge_return"], g["no_champion"]) == (3, 2, 1)
    assert g["p_avgR"] == pytest.approx(4 / 201)


def test_g2_percentile_is_the_lower_tail():
    rng = np.random.default_rng(0)
    e = S0.value + np.arange(500) * D
    b = st.block_bootstrap(rng.normal(0.3, 1, 500), e, S0, 1, n=st.N_BOOT)
    p = b["pct"]
    assert p[st.G2_PERCENTILE] < p[0.139] < p[2.5] < p[97.5]


# --------------------------------------------------------------------------- #
# AT15 placebo
# --------------------------------------------------------------------------- #
def test_at15_shift_is_whole_days_circular_and_identical_for_every_coin():
    raw = np.random.default_rng(0).random((3, 24 * 10)) > 0.8
    out = st.shift_raw(raw, 3, 24)
    for s in range(3):
        assert np.array_equal(out[s], np.roll(raw[s], 72))
    t = 5
    assert out[1, (t + 72) % raw.shape[1]] == raw[1, t]  # lands at the same hour of day
    assert np.array_equal(st.shift_raw(raw, 10, 24), raw)  # a full-axis shift is the identity (circular)


def test_at15_regime_adx_mask_and_eligibility_are_reapplied_at_landing():
    m = ir.Market(fx.calm_noise_15m(), pd.Timestamp("2023-02-20", tz="UTC"), 20)
    sd = ir.SetData(m, {s.set_id: s for s in fam.F2_GRID}["F2-01"])
    shifted = st.shift_raw(np.ones_like(sd.raw), 3, sd.bpd_dec)  # a raw signal everywhere
    cand, _, _ = sd.candidates(shifted)
    gate = sd.has_dec & sd.adx_ok & sd.regime[None, :] & sd.elig & sd.mask_ok
    assert np.array_equal(cand, gate) and (~sd.mask_ok).any()
    m1 = ir.Market(fx.trend_1h(), fx.WIN_START, fx.WIN_DAYS)
    sd1 = ir.SetData(m1, fam.F1_GRID[0])
    cand1, _, _ = sd1.candidates(np.ones_like(sd1.raw))
    assert np.array_equal(cand1, sd1.has_dec & sd1.regime[None, :] & sd1.elig)


def test_at15_placebo_task_shifts_the_raw_pre_regime_signal_and_matches_a_direct_run():
    m = ir.Market(fx.trend_1h(), fx.WIN_START, fx.WIN_DAYS)
    specs = [fam.F1_GRID[0], fam.F3_GRID[1]]
    sds = [ir.SetData(m, s) for s in specs]
    shifts = [3, 11]
    out = p2.run_placebo([m, m], sds, fx.TAKER, fx.risk_cfg(), st.CUT_B, shifts, workers=1)
    for i, d in enumerate(shifts):
        for j, sd in enumerate(sds):
            k = d * sd.bpd_dec
            if sd.family == 3:
                r = ir.simulate(m, sd, fx.TAKER, fx.risk_cfg(), rmat=np.roll(sd.rmat, k, axis=1), collect=False)
            else:
                r = ir.simulate(m, sd, fx.TAKER, fx.risk_cfg(), raw=np.roll(sd.raw, k, axis=1), collect=False)
            assert out[i][j] == p2.light_metrics(r, st.CUT_B)


def _small_placebo(workers, order_seed=None):
    m = ir.Market(fx.trend_1h(), fx.WIN_START, fx.WIN_DAYS)
    specs = list(fam.F1_GRID[:4]) + list(fam.F3_GRID[:2])
    sds = [ir.SetData(m, s) for s in specs]
    shifts = st.placebo_days()[:8]
    return p2.dumps(p2.run_placebo([m] * len(sds), sds, fx.TAKER, fx.risk_cfg(), st.CUT_B, shifts, workers, order_seed))


def test_at15_placebo_is_byte_identical_for_1_4_n_workers_and_any_completion_order():
    ref = _small_placebo(1)
    assert _small_placebo(4) == ref
    assert _small_placebo(os.cpu_count() or 2) == ref
    assert _small_placebo(4, order_seed=123) == ref
    assert _small_placebo(1, order_seed=7) == ref


def test_at15_no_random_number_is_consumed_inside_a_draw(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("RNG used inside a placebo draw")

    shifts = st.placebo_days()[:3]  # generated BEFORE any draw, in the main process
    m = ir.Market(fx.trend_1h(), fx.WIN_START, fx.WIN_DAYS)
    sds = [ir.SetData(m, fam.F1_GRID[0])]
    monkeypatch.setattr(np.random, "default_rng", boom)
    monkeypatch.setattr(np.random, "random", boom)
    p2.run_placebo([m], sds, fx.TAKER, fx.risk_cfg(), st.CUT_B, shifts, workers=1)


def test_at15_draw_champion_uses_the_same_purged_is_rule():
    specs = list(fam.F1_GRID[:3])
    pl = [[dict(n=500, avgR=0.1, total_return=0.2, is_n=200, is_avgR=0.05),
           dict(n=500, avgR=-0.3, total_return=-0.1, is_n=200, is_avgR=0.30),
           dict(n=500, avgR=0.9, total_return=0.9, is_n=149, is_avgR=0.99)],
          [dict(n=10, avgR=0.1, total_return=0.0, is_n=5, is_avgR=1.0)] * 3]
    dch = p2.draw_champions(pl, specs)
    # draw 0: set 3 has < 150 IS trades; set 2 has the best IS avg R -> its FULL-window numbers are recorded
    assert dch[0] == dict(set_no=2, avgR=-0.3, total_return=-0.1)
    assert dch[1] is None
