"""Tests for universe_gate_inputs.py (P1e builder) and run_universe_gate.py's pure helpers.

Two groups:
  1. Synthetic unit tests -- always run.
  2. REAL-dataset truncation-invariance tests on a slice of data_universe/raw_kraken and raw_long:
     decisions/results at bar t must be identical whether or not later bars exist. Plus mutation
     tests proving the check itself catches an injected one-bar look-ahead. The raw universe CSVs
     are gitignored (P1d storage decision), so this group SKIPS when they're absent (e.g. CI).
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

from backtest_live_bot import build_enriched_and_signals, simulate_portfolio, trailing_quote_volume  # noqa: E402
from engine import COST_PRESETS  # noqa: E402
from run_universe_gate import criterion_c, gates, make_random_tiebreak  # noqa: E402
from src.config import load_config  # noqa: E402
from src.signal_engine import Signal  # noqa: E402
from tiered_slippage import tiered_costs_series_by_symbol  # noqa: E402
from universe_eligibility import EXTRA_EXCLUDE_BASES  # noqa: E402
from universe_gate_inputs import (  # noqa: E402
    ALL_EXCLUDE_BASES,
    P1E_EXCLUDE_BASES,
    build_costs,
    build_eligibility,
    cap_top_n_per_bar,
    eligibility_volume,
    enrich_native,
    first_valid_bars,
    load_universe_raw,
    original_bases,
    shared_axis,
    signals_for,
)

RAW_KRAKEN = _THIS_DIR / "data_universe" / "raw_kraken"
RAW_LONG = _THIS_DIR / "data_universe" / "raw_long"
HAVE_REAL = (RAW_KRAKEN / "BTC_USD_1d.csv").exists() and (RAW_LONG / "BTC_USD_1d.csv").exists()
needs_real = pytest.mark.skipif(not HAVE_REAL, reason="raw universe datasets are gitignored; not present here")


# --------------------------------------------------------------------------- #
# 1. synthetic unit tests
# --------------------------------------------------------------------------- #
def test_p1e_exclusions_are_in_the_combined_list_and_cover_the_preregistered_bases():
    assert P1E_EXCLUDE_BASES == {"CASH", "FIDD", "SOFID", "USAT", "FRNT", "BRL1", "XU3O8", "USDSUI"}
    assert EXTRA_EXCLUDE_BASES <= ALL_EXCLUDE_BASES and P1E_EXCLUDE_BASES <= ALL_EXCLUDE_BASES
    assert {"USDT", "USDC", "WBTC"} <= ALL_EXCLUDE_BASES
    assert "BTC" not in ALL_EXCLUDE_BASES


def test_load_universe_raw_drops_excluded_and_empty(tmp_path):
    rows = "timestamp,open,high,low,close,volume\n1700000000000,1,1,1,1,1\n1700086400000,1,1,1,1,1\n"
    (tmp_path / "BTC_USD_1d.csv").write_text(rows)
    (tmp_path / "FIDD_USD_1d.csv").write_text(rows)
    (tmp_path / "WBTC_USD_1d.csv").write_text(rows)
    (tmp_path / "PNT_USD_1d.csv").write_text("timestamp,open,high,low,close,volume\n")
    frames, dropped = load_universe_raw(tmp_path)
    assert set(frames) == {"BTC"}
    assert set(dropped) == {"FIDD", "WBTC", "PNT"}
    assert dropped["PNT"] == "empty file"


def test_eligibility_volume_needs_a_full_30_bars():
    idx = pd.date_range("2025-01-01", periods=40, freq="D", tz="UTC")
    df = pd.DataFrame({"close": 2.0, "volume": 10.0}, index=idx)
    v = eligibility_volume(df)
    assert v.iloc[:29].isna().all()
    assert v.iloc[29] == 20.0


def test_first_valid_bars_left_censoring_rule():
    idx = pd.date_range("2025-01-01", periods=100, freq="D", tz="UTC")
    a = pd.DataFrame({"close": np.ones(100)}, index=idx)
    b = pd.DataFrame({"close": np.r_[np.full(10, np.nan), np.ones(90)]}, index=idx)
    c = pd.DataFrame({"close": np.full(100, np.nan)}, index=idx)
    fvb = first_valid_bars({"A": a, "B": b, "C": c}, min_listing_age_days=60)
    assert fvb["A"] == -60  # first bar == dataset first bar -> already 60 days old
    assert fvb["B"] == 10   # listed inside the window -> real age gate
    assert fvb["C"] == 100  # never valid -> never age-eligible


def test_cap_top_n_per_bar_keeps_highest_volume_and_breaks_ties_by_name():
    masks = {"A": np.array([True, True]), "B": np.array([True, True]), "C": np.array([True, False])}
    vol = {"A": np.array([1.0, 5.0]), "B": np.array([3.0, 5.0]), "C": np.array([2.0, 9.0])}
    out = cap_top_n_per_bar(masks, vol, max_symbols=2)
    assert [out[s][0] for s in "ABC"] == [False, True, True]
    assert [out[s][1] for s in "ABC"] == [True, True, False]  # C ineligible; A/B under the cap


def test_original_bases_from_market_symbols():
    assert original_bases(["BTC/USDT", "eth/USDT"]) == ["BTC", "ETH"]


def _sig(score: float) -> Signal:
    return Signal(direction="bullish", primary_pattern="Breakout", engine_score=score,
                  supporting_patterns=[], primary_details={}, rationale={})


def test_random_tiebreak_keeps_engine_score_primary_and_is_seed_deterministic():
    sigs = {"A": _sig(1.0), "B": _sig(1.0), "C": _sig(0.5), "D": _sig(1.0)}
    r1 = make_random_tiebreak(3)(sigs, {})
    r2 = make_random_tiebreak(3)(sigs, {})
    assert r1 == r2
    assert r1[-1] == "C"
    orders = {tuple(make_random_tiebreak(s)(sigs, {})) for s in range(1, 11)}
    assert len(orders) > 1  # the tie-break actually varies across seeds


def test_criterion_c_rules():
    def m(n, r):
        return {"decomposition": {"added": {"trades": n, "avg_R": r}}}
    assert criterion_c(m(50, 0.1)) == "PASS"
    assert criterion_c(m(50, 0.0)) == "FAIL"
    assert criterion_c(m(10, -0.2)) == "FAIL"
    assert criterion_c(m(10, 0.3)) == "INCONCLUSIVE"
    assert criterion_c(m(0, float("nan"))) == "INCONCLUSIVE"


def test_gates_match_run_live_bot_report_definitions():
    m = {"trades": 120, "ci": [0.01, 0.5], "oos_avg_R": 0.1, "total_return": 0.2, "sharpe": 0.9}
    g = gates(m, bench_sharpe=0.8, pr_ret=0.95, pr_R=0.90)
    assert all(g.values())
    g = gates(m, bench_sharpe=0.9, pr_ret=0.95, pr_R=0.89)
    assert g["beats_placebo_90th_pct"] is False and g["net_return_and_sharpe_gt_buy_hold"] is False


# --------------------------------------------------------------------------- #
# 2. REAL-dataset truncation invariance
# --------------------------------------------------------------------------- #
# Slice chosen before looking at any gate result: two left-censored majors, original-16 members
# that can sit either side of $1M, and several later-listed / thinner Kraken names.
KRAKEN_SLICE = ["BTC", "ETH", "XTZ", "ALGO", "ATOM", "AAVE", "ONDO", "PENGU", "TRUMP", "BONK"]
LONG_SLICE = ["BTC", "ETH", "SOL", "XTZ", "ALGO", "AAVE", "ONDO", "BONK"]


def _real_frames(root: Path, symbols: list[str], since: str | None = None) -> dict[str, pd.DataFrame]:
    frames, _ = load_universe_raw(root)
    out = {s: frames[s] for s in symbols if s in frames}
    if since is not None:
        out = {s: f[f.index >= pd.Timestamp(since, tz="UTC")] for s, f in out.items()}
        out = {s: f for s, f in out.items() if len(f)}
    return out


def _truncate(frames: dict[str, pd.DataFrame], last_ts: pd.Timestamp) -> dict[str, pd.DataFrame]:
    out = {s: f[f.index <= last_ts] for s, f in frames.items()}
    return {s: f for s, f in out.items() if len(f)}


def _masks_and_slippage(frames, rule, floor=1_000_000.0, top_n=3, vol_fn=eligibility_volume, costs_fn=build_costs):
    # Eligibility and costs only read close/volume, so the raw frames stand in for the enriched
    # ones here (add_indicators adds columns, never changes close/volume) -- keeps the every-bar
    # loop fast. The end-to-end test below uses the real enriched path.
    native = frames
    idx = shared_axis(frames)
    eg = {s: native[s].reindex(idx) for s in native}
    masks, _, _ = build_eligibility(rule, eg, native, idx, floor=floor, exit_volume_ratio=0.5,
                                    min_listing_age_days=60, max_symbols=150, top_n=top_n, vol_fn=vol_fn)
    costs = costs_fn(eg)
    slip = {s: np.array([c.slippage for c in arr]) for s, arr in costs.items()}
    return idx, masks, slip


def _truncation_mismatches(frames, rule, stop_at_first=False, **kw) -> list[tuple]:
    """EVERY bar t: masks and per-bar slippage on bars [0..t] computed from data truncated at t must
    equal the full-data values on the same bars. Returns the list of mismatches."""
    idx, masks_full, slip_full = _masks_and_slippage(frames, rule, **kw)
    bad = []
    for t in range(len(idx)):
        if stop_at_first and bad:
            break
        tr = _truncate(frames, idx[t])
        _, m_t, s_t = _masks_and_slippage(tr, rule, **kw)
        for s in m_t:
            if not np.array_equal(m_t[s], masks_full[s][: t + 1]):
                bad.append(("mask", s, t))
            if not np.array_equal(s_t[s], slip_full[s][: t + 1]):
                bad.append(("slippage", s, t))
    return bad


@needs_real
def test_real_kraken_floor_masks_and_costs_are_truncation_invariant_every_bar():
    frames = _real_frames(RAW_KRAKEN, KRAKEN_SLICE)
    idx, masks, _ = _masks_and_slippage(frames, "floor")
    # non-vacuous: at least one symbol actually enters/leaves the universe inside the window
    assert any(m.any() and not m.all() for m in masks.values())
    assert _truncation_mismatches(frames, "floor") == []


@needs_real
def test_real_long_rank_topn_masks_and_costs_are_truncation_invariant_every_bar():
    frames = _real_frames(RAW_LONG, LONG_SLICE, since="2024-06-01")
    idx, masks, _ = _masks_and_slippage(frames, "rank_topn", top_n=3)
    assert sum(1 for m in masks.values() if m.any() and not m.all()) >= 2  # membership rotates
    assert _truncation_mismatches(frames, "rank_topn", top_n=3) == []


@needs_real
def test_truncation_check_catches_injected_eligibility_lookahead():
    """Mutation test: a builder whose trailing volume peeks ONE bar ahead must be caught."""
    frames = _real_frames(RAW_KRAKEN, KRAKEN_SLICE)
    def peek(native):
        return eligibility_volume(native).shift(-1)
    assert _truncation_mismatches(frames, "floor", stop_at_first=True, vol_fn=peek)


@needs_real
def test_truncation_check_catches_injected_cost_lookahead():
    """Mutation test: a cost builder tiering bar t on volume through t+1 must be caught."""
    frames = _real_frames(RAW_KRAKEN, KRAKEN_SLICE)
    def peek_costs(eg):
        return tiered_costs_series_by_symbol({s: trailing_quote_volume(e).shift(-2).to_numpy() for s, e in eg.items()})
    assert any(kind == "slippage" for kind, *_ in _truncation_mismatches(frames, "floor", stop_at_first=True, costs_fn=peek_costs))


def _full_pipeline(frames, cfg, sigs=None):
    native = enrich_native(frames, cfg.indicators)
    idx = shared_axis(frames)
    eg = {s: native[s].reindex(idx) for s in native}
    masks, _, _ = build_eligibility("floor", eg, native, idx, floor=1_000_000.0, exit_volume_ratio=0.5,
                                    min_listing_age_days=60, max_symbols=150)
    if sigs is None:
        sigs = signals_for(native, sorted(native), cfg)
    else:  # full-data signals, restricted to bars that exist in the truncated data
        sigs = {s: {ts: v for ts, v in m.items() if ts <= idx[-1]} for s, m in sigs.items() if s in native}
    res = simulate_portfolio(eg, sigs, COST_PRESETS["kraken_T3_taker"], cfg.risk, cfg.trade_planner, 10_000.0, 365.0,
                             _REPO_ROOT, use_rank_order=True, eligible_mask_by_symbol=masks, costs_by_symbol=build_costs(eg))
    return idx, res, sigs


@needs_real
def test_real_kraken_end_to_end_results_unchanged_when_later_bars_are_dropped():
    """Builder -> signals -> simulate_portfolio on the real slice, full vs truncated at a cut every
    15 bars. At each cut c: every position ENTERED by bar c (same symbol/entry bar/entry price),
    every trade CLOSED by bar c, and the equity curve before c must be identical. Masks and costs
    are rebuilt from the truncated data at every cut. Signals are recomputed from truncated data at
    one cut (and must equal the full-data signals up to it); the other cuts reuse the full-data
    signals restricted to bars <= c, since Stage-A signal truncation-invariance is already covered
    by test_pack.py and this test is about the P1e eligibility/cost wiring into the simulation."""
    cfg = load_config()
    frames = _real_frames(RAW_KRAKEN, KRAKEN_SLICE)
    idx, full, full_sigs = _full_pipeline(frames, cfg)
    assert len(full.trades) >= 10  # non-vacuous
    c0 = int(len(idx) * 0.6)
    _, _, sigs_t = _full_pipeline(_truncate(frames, idx[c0]), cfg)
    assert {s: sorted(v) for s, v in sigs_t.items()} == {
        s: sorted(ts for ts in v if ts <= idx[c0]) for s, v in full_sigs.items()
    }
    closed_key = ["symbol", "entry_time", "exit_time", "reason", "R"]
    entry_key = ["symbol", "entry_time", "entry_px"]
    for c in range(60, len(idx) - 1, 15):
        _, tr, _ = _full_pipeline(_truncate(frames, idx[c]), cfg, sigs=full_sigs)
        got = tr.trades[tr.trades.reason != "end"][closed_key].reset_index(drop=True)
        want = full.trades[full.trades.exit_time <= idx[c]][closed_key].reset_index(drop=True)
        pd.testing.assert_frame_equal(got, want, obj=f"closed trades at cut {c}")
        got_e = tr.trades[entry_key].sort_values(entry_key).reset_index(drop=True)
        want_e = full.trades[full.trades.entry_time <= idx[c]][entry_key].sort_values(entry_key).reset_index(drop=True)
        pd.testing.assert_frame_equal(got_e, want_e, obj=f"entries at cut {c}")
        np.testing.assert_allclose(tr.equity.to_numpy()[:c], full.equity.to_numpy()[:c], err_msg=f"equity at cut {c}")


@needs_real
def test_builder_assembly_matches_build_enriched_and_signals_on_real_data():
    """The runner assembles enriched frames/signals itself (to share one axis per dataset); on a
    real subset it must reproduce backtest_live_bot.build_enriched_and_signals exactly."""
    cfg = load_config()
    frames = _real_frames(RAW_KRAKEN, ["BTC", "ONDO", "TRUMP"])
    eg_ref, sig_ref, idx_ref = build_enriched_and_signals(frames, cfg.indicators, cfg.scanner, cfg.signal_engine,
                                                          bool(cfg.htf_filter.get("enabled", True)))
    native = enrich_native(frames, cfg.indicators)
    idx = shared_axis(frames)
    assert idx.equals(idx_ref)
    for s in frames:
        pd.testing.assert_frame_equal(native[s].reindex(idx), eg_ref[s])
    sigs = signals_for(native, sorted(frames), cfg)
    assert {s: sorted(v) for s, v in sigs.items()} == {s: sorted(v) for s, v in sig_ref.items()}
