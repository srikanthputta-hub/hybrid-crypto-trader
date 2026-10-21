"""Tests for the pre-run review fixes (Architect M1, m4; Risk Officer integrity / information rows)
on the intraday P2 runner. Synthetic data and stubs only; nothing reads a real-data result.

Run: pytest strategy_pack_run/test_intraday_review_fixes.py -q
"""
from __future__ import annotations

import json
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

import intraday_families as fam  # noqa: E402
import intraday_fixtures as fx  # noqa: E402
import intraday_runner as ir  # noqa: E402
import intraday_stats as st  # noqa: E402
import intraday_synthetic as syn  # noqa: E402
import run_intraday_p2 as p2  # noqa: E402
from src.risk_engine import RiskEngine  # noqa: E402

SPEC = {s.set_id: s for g in fam.GRIDS.values() for s in g}
COINS = ("BTC", "ETH", "SOL", "ADA")


def _provider():
    data = syn.universe_1h(COINS[1:], "2020-09-01", 24 * 2240, seed=11, kind="regime_switch", sigma=0.006, mu=0.0004)
    return p2.SyntheticProvider(lambda u, tf: data, coins={u: {"1h": COINS, "15m": COINS} for u in ("B", "A")})


def _stub(family, provider, workers, logs_dir, n_placebo=st.N_PLACEBO, code_label=None):
    return dict(family=family, verdict="FAIL", grid={"B": {}, "A": {}}, champion=None, integrity=[],
                harness_version=ir.HARNESS_VERSION, code_commit=code_label, provider=provider.kind)


@pytest.fixture
def clean(monkeypatch, tmp_path):
    monkeypatch.setattr(p2, "code_commit", lambda *a, **k: "0123abc")
    monkeypatch.setattr(p2, "PARTIAL_DIR", tmp_path / "PARTIAL")
    return tmp_path


# --------------------------------------------------------------------------- #
# M1 (i): clean tree required
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("label", ["0123abc+dirty", "unknown"])
def test_m1_refuses_to_start_on_a_dirty_or_unknown_code_label(monkeypatch, tmp_path, label):
    monkeypatch.setattr(p2, "code_commit", lambda *a, **k: label)

    def never(*a, **k):
        raise AssertionError("evaluation must not start")

    with pytest.raises(SystemExit):
        p2.run([1], False, 1, provider=p2.SyntheticProvider(lambda u, tf: None), evaluate=never,
               out_json=tmp_path / "x.json", docs_dir=tmp_path, logs_dir=tmp_path)
    assert not list(tmp_path.iterdir())


def test_m1_code_label_covers_docs():
    import inspect
    assert '"docs"' in inspect.getsource(p2.code_commit)


# --------------------------------------------------------------------------- #
# M1 (iv): official outputs only from the official entry point
# --------------------------------------------------------------------------- #
def test_m1_partial_runs_write_only_labelled_scratch_outputs(clean):
    official_json, official_docs = clean / "official.json", clean / "docs"
    official_docs.mkdir()
    rec = p2.run([1], False, 1, provider=p2.SyntheticProvider(lambda u, tf: None), evaluate=_stub,
                 out_json=official_json, docs_dir=official_docs, logs_dir=clean / "logs")
    assert rec["run_kind"] == "PARTIAL" and not official_json.exists() and not list(official_docs.iterdir())
    part = json.loads((clean / "PARTIAL" / "PARTIAL_intraday_p2_results.json").read_text())
    assert part["run_kind"] == "PARTIAL" and part["results"]["family1"]["run_kind"] == "PARTIAL"
    rep = (clean / "PARTIAL" / "PARTIAL_intraday_p2_family1.md").read_text()
    assert rep.startswith("> **PARTIAL run")


def test_m1_family_all_via_run_intraday_p2_is_still_partial(clean, monkeypatch):
    seen = {}

    def fake_run(fams, official, *a, **k):
        seen.update(fams=fams, official=official)

    monkeypatch.setattr(p2, "run", fake_run)
    p2.main(["--family", "all"])
    assert seen == dict(fams=[1, 2, 3], official=False)
    p2.main([], official=True)
    assert seen == dict(fams=[1, 2, 3], official=True)
    with pytest.raises(SystemExit):
        p2.main(["--family", "2"], official=True)


@pytest.mark.parametrize("fams,kind,n", [([1], "real", st.N_PLACEBO), ([1, 2, 3], "synthetic", st.N_PLACEBO),
                                        ([1, 2, 3], "real", 3)])
def test_m1_official_run_must_be_all_families_real_data_full_placebo(clean, fams, kind, n):
    prov = p2.SyntheticProvider(lambda u, tf: None)
    prov.kind = kind
    with pytest.raises(SystemExit):
        p2.run(fams, True, 1, provider=prov, evaluate=_stub, n_placebo=n, out_json=clean / "o.json",
               docs_dir=clean, logs_dir=clean)


# --------------------------------------------------------------------------- #
# M1 (iii): results JSON written atomically after each family
# --------------------------------------------------------------------------- #
def test_m1_results_json_is_written_after_each_family_so_a_crash_leaves_a_consistent_record(clean):
    def flaky(family, *a, **k):
        if family == 3:
            raise RuntimeError("crash in family 3")
        return _stub(family, *a, **k)

    with pytest.raises(RuntimeError):
        p2.run([1, 3], False, 1, provider=p2.SyntheticProvider(lambda u, tf: None), evaluate=flaky)
    rec = json.loads((clean / "PARTIAL" / "PARTIAL_intraday_p2_results.json").read_text())
    assert rec["complete"] is False and list(rec["results"]) == ["family1"]
    assert not list((clean / "PARTIAL").glob("*.tmp"))


def test_m1_complete_flag_set_only_after_the_last_family(clean):
    rec = p2.run([1, 3], False, 1, provider=p2.SyntheticProvider(lambda u, tf: None), evaluate=_stub)
    assert rec["complete"] is True and list(rec["results"]) == ["family1", "family3"]


# --------------------------------------------------------------------------- #
# M1 (ii): integrity failure -> VOID, integrity record only, no numbers
# --------------------------------------------------------------------------- #
def test_m1_integrity_failure_stops_after_the_base_runs_and_keeps_only_the_integrity_record(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(p2, "integrity_problems", lambda *a, **k: calls.append(1) or ["injected failure"])
    monkeypatch.setattr(p2, "run_placebo", lambda *a, **k: pytest.fail("placebo must not run after an integrity failure"))
    r = p2.evaluate_family(1, _provider(), workers=1, n_placebo=1, verbose=True)
    assert r["verdict"] == "VOID" and r["integrity"] == ["injected failure"]
    assert set(r) == {"family", "harness_version", "provider", "code_commit", "data_hashes", "verdict", "integrity"}
    out = capsys.readouterr().out
    assert "trades" not in out and "avg" not in out and len(calls) == 1
    rep = p2.render_report(r)
    assert "## 1." not in rep and "Integrity failures (VOID)" in rep and "injected failure" in rep


def test_m1_progress_lines_print_no_numbers_beyond_the_set_id(monkeypatch, capsys):
    monkeypatch.setattr(p2, "integrity_problems", lambda *a, **k: [])
    monkeypatch.setattr(p2, "run_summary", lambda *a, **k: dict(trades=0, is_n=0, is_avgR=float("nan"), avgR=float("nan")))
    p2.evaluate_family(1, _provider(), workers=1, n_placebo=1, verbose=True)
    for line in capsys.readouterr().out.splitlines():
        if line.startswith("F1 "):
            assert line.endswith(": done"), line


# --------------------------------------------------------------------------- #
# Integrity checks themselves (m4 + Risk Officer row)
# --------------------------------------------------------------------------- #
def _trade(s, entry_ns, exit_ns, kind="close", stop=95.0, ref=100.0, R=0.5, net=0.0):
    t = {k: 0.0 for k in ir.TRADE_FIELDS}
    t.update(s=s, d_signal=0, fill_k=1, exit_k=2, exit_kind=kind, entry_ns=entry_ns, exit_ns=exit_ns, initial_stop=stop,
             entry_ref=ref, R=R, net_pnl=net, reason="stop", maker_filled=False, time_stop_k=None)
    return tuple(t[k] for k in ir.TRADE_FIELDS) + (0.0,)


def _res(trades):
    r = ir.RunResult(fam.F2_GRID[0], "t", "close", "taker", 10_000.0)
    r.trades = trades
    r.final_equity = 10_000.0
    return r


def test_m4_mask_violation_check_counts_masked_coin_positions_overlapping_a_mask():
    m = ir.Market(fx.calm_noise_15m(), pd.Timestamp("2023-02-25", tz="UTC"), 10)
    sol, btc = m.coins.index("SOL"), m.coins.index("BTC")
    ns = lambda s: pd.Timestamp(s, tz="UTC").value  # noqa: E731
    inside = _trade(sol, ns("2023-03-03 20:00"), ns("2023-03-04 02:00"))
    clear = _trade(sol, ns("2023-03-02 20:00"), ns("2023-03-03 02:00"))
    edge = _trade(sol, ns("2023-03-03 20:00"), ns("2023-03-03 23:45"))   # its exit fill bar opens at 23:45 -> ends 00:00
    exempt = _trade(btc, ns("2023-03-03 20:00"), ns("2023-03-04 02:00"))
    assert p2.mask_violations(_res([inside]), m) == 1
    assert p2.mask_violations(_res([clear, exempt, edge]), m) == 0
    assert p2.mask_violations(_res([inside, inside, clear]), m) == 2


def test_m4_integrity_problems_wires_the_mask_check_for_15m_sets_only(monkeypatch):
    m = ir.Market(fx.calm_noise_15m(), pd.Timestamp("2023-02-25", tz="UTC"), 10)
    sd = ir.SetData(m, SPEC["F2-01"])
    res = ir.simulate(m, sd, fx.TAKER, fx.risk_cfg())
    assert p2.integrity_problems(res, m, sd, "B", fx.TAKER, fx.risk_cfg()) == []
    monkeypatch.setattr(p2, "mask_violations", lambda *a: 3)
    probs = p2.integrity_problems(res, m, sd, "B", fx.TAKER, fx.risk_cfg())
    assert probs == ["mask violation: F2-01 B: 3 trades"]
    m1 = ir.Market(fx.noise_1h(), fx.WIN_START, 10)
    sd1 = ir.SetData(m1, SPEC["F2-07"])
    assert p2.integrity_problems(ir.simulate(m1, sd1, fx.TAKER, fx.risk_cfg()), m1, sd1, "B", fx.TAKER, fx.risk_cfg()) == []


def test_risk_officer_integrity_stop_at_or_above_entry_or_nan_r_is_void():
    m = ir.Market(fx.trend_1h(), fx.WIN_START, 10)
    sd = ir.SetData(m, SPEC["F1-01"])
    res = ir.simulate(m, sd, fx.TAKER, fx.risk_cfg())
    assert p2.integrity_problems(res, m, sd, "B", fx.TAKER, fx.risk_cfg()) == []
    for bad in (_trade(0, 0, 1, stop=100.0, ref=100.0), _trade(0, 0, 1, R=float("nan"))):
        res2 = ir.simulate(m, sd, fx.TAKER, fx.risk_cfg())
        res2.trades.append(bad)
        probs = p2.integrity_problems(res2, m, sd, "B", fx.TAKER, fx.risk_cfg())
        assert any(p.startswith("approved trades with stop >= entry or non-finite R: F1-01 B: 1") for p in probs)


def test_integrity_detects_a_non_deterministic_rerun(monkeypatch):
    m = ir.Market(fx.trend_1h(), fx.WIN_START, 10)
    sd = ir.SetData(m, SPEC["F1-01"])
    res = ir.simulate(m, sd, fx.TAKER, fx.risk_cfg())
    res.final_equity += 1.0
    res.trades[0] = res.trades[0][:ir.TRADE_FIELDS.index("net_pnl")] + (res.trades[0][ir.TRADE_FIELDS.index("net_pnl")] + 1.0,) \
        + res.trades[0][ir.TRADE_FIELDS.index("net_pnl") + 1:]
    assert any(p.startswith("non-deterministic rerun") for p in p2.integrity_problems(res, m, sd, "B", fx.TAKER, fx.risk_cfg()))


# --------------------------------------------------------------------------- #
# Risk Officer information rows and asserts
# --------------------------------------------------------------------------- #
def test_budget_overshoot_counts_losses_beyond_the_volatility_scaled_budget():
    m = ir.Market(fx.trend_1h(), fx.WIN_START, 30)
    sd = ir.SetData(m, SPEC["F1-01"])
    rc = fx.risk(max_position_pct=1.0, max_total_exposure_pct=1.0)  # risk-based sizing binds
    res = ir.simulate(m, sd, fx.TAKER, rc)
    fi = ir.TRADE_FIELDS.index
    eng = ir.make_risk_engine(rc, ["X"])
    expect, worst = 0, 0.0
    for t in res.trades:
        b = t[fi("equity_signal")] * 0.01 * eng._volatility_scale(float(sd.atr_pct[t[fi("s")], t[fi("d_signal")]]))
        if -t[fi("net_pnl")] > b:
            expect, worst = expect + 1, max(worst, -t[fi("net_pnl")] / b)
    got = p2.budget_overshoot(res, sd, rc)
    assert got == dict(trades=expect, worst_multiple=worst)
    assert expect > 0  # close-trigger exits at the next open overshoot the stop on this fixture


def test_volatility_scale_floor_forwarded_only_if_it_equals_the_engine_default():
    ir.make_risk_engine(fx.risk(volatility_scale_floor=0.5), ["X"])
    with pytest.raises(ValueError):
        ir.make_risk_engine(fx.risk(volatility_scale_floor=0.6), ["X"])


def test_atr_pct_is_never_none_and_a_missing_daily_atr_reaches_the_engine_as_nan():
    m = ir.Market(fx.trend_1h(), fx.WIN_START, 10)
    sd = ir.SetData(m, SPEC["F1-01"])
    sd.atr_pct[:, :] = np.nan
    seen = []
    orig = RiskEngine.check_new_trade

    def wrapped(self, symbol, side, *a, **k):
        seen.append(k["atr_pct"])
        return orig(self, symbol, side, *a, **k)

    with patch.object(RiskEngine, "check_new_trade", wrapped):
        res = ir.simulate(m, sd, fx.TAKER, fx.risk_cfg())
    assert seen and all(x is not None and np.isnan(x) for x in seen)
    assert len(res.trades) == 0 and res.reject_reasons == {"invalid_input": len(seen)}


def test_m3_maker_report_compares_filled_orders_with_taker_trades_of_missed_orders():
    m = ir.Market(fx.trend_1h(), fx.WIN_START, fx.WIN_DAYS)
    sd = ir.SetData(m, SPEC["F1-07"])  # 4h decisions: four 1h bars per maker timeout
    mk = ir.simulate(m, sd, fx.TAKER, fx.risk_cfg(), fill_model="maker")
    tk = ir.simulate(m, sd, fx.TAKER, fx.risk_cfg())
    rep = p2.maker_detail(mk, m, sd, tk)
    fi = ir.TRADE_FIELDS.index
    tk_by = {(t[fi("s")], t[fi("d_signal")]): t for t in tk.trades}
    matched = [tk_by[(s, d)] for s, d, _ in mk.missed if (s, d) in tk_by]
    assert rep["filled"] == len(mk.trades) and rep["missed"] == len(mk.missed) > 0 and rep["missed_matched_taker_trades"] == len(matched) > 0

    def mean_post(trades, h):  # close of the 1h bar ending h hours after the fill bar opens
        v = [m.C[t[fi("s")], t[fi("fill_k")] + h - 1] / t[fi("entry_px")] - 1 for t in trades]
        return float(np.mean(v))

    assert rep["post_fill_ret_1h"] == pytest.approx(mean_post(mk.trades, 1))
    assert rep["post_fill_ret_4h"] == pytest.approx(mean_post(mk.trades, 4))
    assert rep["missed_taker_post_ret_1h"] == pytest.approx(mean_post(matched, 1))
    assert rep["missed_taker_post_ret_4h"] == pytest.approx(mean_post(matched, 4))
    assert rep["missed_taker_avgR"] == pytest.approx(np.mean([t[fi("R")] for t in matched]))


def test_g4_flags_when_the_no_champion_convention_is_decisive():
    draws = [dict(avgR=0.0, total_return=0.0)] * 196 + [None] * 4
    g = st.g4_counts(0.3, 0.2, draws)
    assert g["ge_avgR"] == 4 and g["no_champion_decisive"] is True
    g2 = st.g4_counts(0.3, 0.2, [dict(avgR=0.0, total_return=0.0)] * 199 + [None])
    assert g2["no_champion_decisive"] is False  # passes anyway
