from src.scanner import Candidate
from src.signal_engine import evaluate


def test_no_candidates_returns_none():
    assert evaluate([]) is None


def test_all_candidates_below_threshold_returns_none():
    candidates = [Candidate("BREAKOUT", "bullish", 0.3), Candidate("MOMENTUM", "bullish", 0.2)]
    assert evaluate(candidates, min_strength=0.5) is None


def test_conflicting_directions_returns_none():
    candidates = [Candidate("BREAKOUT", "bullish", 0.8), Candidate("REVERSAL", "bearish", 0.7)]
    assert evaluate(candidates, min_strength=0.5) is None


def test_single_strong_candidate_becomes_signal():
    candidates = [Candidate("MOMENTUM", "bullish", 0.7, {"return_pct": 0.05})]
    signal = evaluate(candidates, min_strength=0.5)
    assert signal is not None
    assert signal.direction == "bullish"
    assert signal.primary_pattern == "MOMENTUM"
    assert signal.engine_score == 0.7
    assert signal.supporting_patterns == ["MOMENTUM"]


def test_agreeing_candidates_boost_engine_score_via_confluence():
    candidates = [
        Candidate("BREAKOUT", "bullish", 0.6),
        Candidate("MOMENTUM", "bullish", 0.55),
        Candidate("TREND_CONTINUATION", "bullish", 0.5),
    ]
    signal = evaluate(candidates, min_strength=0.5, confluence_bonus=0.08)
    assert signal is not None
    assert signal.engine_score > 0.6  # boosted above the strongest single candidate
    assert set(signal.supporting_patterns) == {"BREAKOUT", "MOMENTUM", "TREND_CONTINUATION"}


def test_pattern_priority_breaks_ties_on_equal_strength():
    candidates = [Candidate("REVERSAL", "bullish", 0.6), Candidate("BREAKOUT", "bullish", 0.6)]
    signal = evaluate(candidates, min_strength=0.5, pattern_priority=["BREAKOUT", "REVERSAL"])
    assert signal.primary_pattern == "BREAKOUT"


def test_engine_score_never_exceeds_one():
    candidates = [Candidate(f"P{i}", "bullish", 0.95) for i in range(6)]
    signal = evaluate(candidates, min_strength=0.5, confluence_bonus=0.2, pattern_priority=[])
    assert signal.engine_score == 1.0
