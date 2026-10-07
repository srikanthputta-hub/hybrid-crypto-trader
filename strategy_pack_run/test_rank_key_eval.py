"""Tests for run_rank_key_eval.py's runner-local rank-key injection (docs/engine_score_saturation.md §2).

The evaluation never changed src/; every candidate number depends on (a) the legacy path of the
runner's re-implemented ranker matching src.signal_engine.rank_signals exactly, (b) each candidate
key ordering as pre-registered, and (c) placebo signals carrying the original's key values.
"""
from __future__ import annotations

import random

import pandas as pd

import backtest_live_bot
from run_rank_key_eval import _placebo_carry_rk, make_rank
from src.signal_engine import Signal, rank_signals


def _sig(score: float, rk: dict | None = None, adx: float | None = None) -> Signal:
    details = {} if adx is None else {"adx": adx}
    rationale = {} if rk is None else {"_rk": rk}
    return Signal(direction="bullish", primary_pattern="MOMENTUM", engine_score=score,
                  supporting_patterns=["MOMENTUM"], primary_details=details, rationale=rationale)


def test_legacy_runner_ranker_matches_src_rank_signals_on_random_inputs():
    rnd = random.Random(1234)
    legacy = make_rank("legacy", None, None)
    for _ in range(500):
        n = rnd.randint(0, 9)
        syms = rnd.sample([f"S{i:02d}" for i in range(30)], n)
        # coarse score grid so engine_score ties (the case that matters) are frequent
        signals = {s: _sig(rnd.choice([0.5, 0.75, 0.9, 1.0, 1.0, 1.0])) for s in syms}
        liquidity = {}
        for s in syms + ["EXTRA"]:
            r = rnd.random()
            if r < 0.6:
                liquidity[s] = rnd.choice([0.0, 1e5, 1e6, 1e6, 5e7])
            elif r < 0.8:
                liquidity[s] = None
        liquidity = {k: v for k, v in liquidity.items() if v is not None}
        assert legacy(signals, liquidity) == rank_signals(signals, liquidity)


def test_candidate_A_breaks_saturated_ties_by_raw_and_keeps_unsaturated_order():
    rank_a = make_rank("A", None, None)
    signals = {
        "LOW": _sig(0.7, {"A": 0.7}),            # unsaturated: raw == engine_score
        "SAT_SMALL": _sig(1.0, {"A": 1.05}),
        "SAT_BIG": _sig(1.0, {"A": 2.4}),
        "MID": _sig(0.9, {"A": 0.9}),
    }
    liquidity = {"LOW": 1e9, "SAT_SMALL": 1e9, "SAT_BIG": 1.0, "MID": 1e9}
    assert rank_a(signals, liquidity) == ["SAT_BIG", "SAT_SMALL", "MID", "LOW"]


def test_candidate_B_and_C_keep_engine_score_first_and_missing_value_last_among_ties():
    for cand in ("B", "C"):
        rank = make_rank(cand, None, None)
        signals = {
            "MISSING": _sig(1.0, {cand: None}),
            "HIGH": _sig(1.0, {cand: 40.0}),
            "LOWVAL": _sig(1.0, {cand: 10.0}),
            "WEAK": _sig(0.8, {cand: 99.0}),      # higher term, lower engine_score -> still after all 1.0s
        }
        assert rank(signals, {}) == ["HIGH", "LOWVAL", "MISSING", "WEAK"], cand


def test_tie_log_counts_competitors_tied_at_top():
    log: list = []
    rank = make_rank("legacy", None, log)
    rank({"A": _sig(1.0), "B": _sig(1.0), "C": _sig(0.6)}, {})
    rank({"A": _sig(1.0), "B": _sig(0.9)}, {})
    rank({"A": _sig(1.0)}, {})  # single-symbol calls are not contested -> not logged
    assert log == [(3, 2), (2, 0)]


def test_placebo_signal_carries_original_rank_terms_and_nothing_else_changes():
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    enr = pd.DataFrame({"close": [1.0, 2.0, 3.0], "adx": [10.0, 20.0, 30.0]}, index=idx)
    original = _sig(1.0, {"A": 1.7, "B": 33.0, "C": 2.5}, adx=33.0)
    plain = backtest_live_bot._placebo_signal_at(original, enr, 2)
    carried = _placebo_carry_rk(original, enr, 2)
    assert carried.rationale["_rk"] == {"A": 1.7, "B": 33.0, "C": 2.5}
    assert carried.rationale["_rk"] is not original.rationale["_rk"]  # copied, not aliased
    carried.rationale.pop("_rk")
    assert carried == plain                    # landing-bar ADX etc. untouched
    assert carried.primary_details == {"adx": 30.0}
    # an original without "_rk" (legacy signal) passes through unchanged
    assert _placebo_carry_rk(_sig(1.0), enr, 2) == backtest_live_bot._placebo_signal_at(_sig(1.0), enr, 2)
