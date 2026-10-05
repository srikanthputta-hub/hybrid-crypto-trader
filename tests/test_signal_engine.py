from src.scanner import Candidate
from src.signal_engine import Signal, evaluate, rank_signals


def _signal(engine_score: float) -> Signal:
    return Signal(direction="bullish", primary_pattern="BREAKOUT", engine_score=engine_score)


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


# -- rank_signals ----------------------------------------------------------


def test_rank_signals_orders_by_engine_score_descending():
    signals = {
        "AAA": _signal(0.5),
        "BBB": _signal(0.9),
        "CCC": _signal(0.7),
    }
    liquidity = {"AAA": 1_000_000.0, "BBB": 1_000_000.0, "CCC": 1_000_000.0}
    assert rank_signals(signals, liquidity) == ["BBB", "CCC", "AAA"]


def test_rank_signals_tie_break_on_liquidity_higher_first():
    signals = {"AAA": _signal(0.8), "BBB": _signal(0.8)}
    liquidity = {"AAA": 500_000.0, "BBB": 5_000_000.0}
    assert rank_signals(signals, liquidity) == ["BBB", "AAA"]


def test_rank_signals_full_tie_breaks_alphabetically_by_symbol():
    signals = {"ZZZ": _signal(0.8), "AAA": _signal(0.8), "MMM": _signal(0.8)}
    liquidity = {"ZZZ": 1_000_000.0, "AAA": 1_000_000.0, "MMM": 1_000_000.0}
    assert rank_signals(signals, liquidity) == ["AAA", "MMM", "ZZZ"]

    # And again with all-None liquidity, same determinism.
    liquidity_none = {"ZZZ": None, "AAA": None, "MMM": None}
    assert rank_signals(signals, liquidity_none) == ["AAA", "MMM", "ZZZ"]


def test_rank_signals_none_liquidity_ranks_after_known_liquidity():
    signals = {"AAA": _signal(0.8), "BBB": _signal(0.8)}
    liquidity = {"AAA": None, "BBB": 1.0}  # BBB's liquidity is tiny but known
    assert rank_signals(signals, liquidity) == ["BBB", "AAA"]


def test_rank_signals_engine_score_dominates_liquidity():
    # A None-liquidity symbol with a strictly higher engine_score still outranks a
    # known-liquidity symbol with a lower one — liquidity is a tie-break, not a bucket.
    signals = {"AAA": _signal(0.9), "BBB": _signal(0.5)}
    liquidity = {"AAA": None, "BBB": 10_000_000.0}
    assert rank_signals(signals, liquidity) == ["AAA", "BBB"]


def test_rank_signals_empty_signals_returns_empty_list():
    assert rank_signals({}, {}) == []
    assert rank_signals({}, {"AAA": 1.0}) == []


def test_rank_signals_symbol_missing_from_liquidity_treated_as_none():
    signals = {"AAA": _signal(0.8), "BBB": _signal(0.8)}
    liquidity = {"BBB": 1.0}  # AAA absent entirely
    assert rank_signals(signals, liquidity) == ["BBB", "AAA"]


def test_rank_signals_liquidity_has_extra_unrelated_symbols():
    signals = {"AAA": _signal(0.8)}
    liquidity = {"AAA": 1.0, "ZZZ": 999_999_999.0, "YYY": None}
    assert rank_signals(signals, liquidity) == ["AAA"]


def test_rank_signals_is_deterministic_across_repeated_calls():
    signals = {
        "AAA": _signal(0.6),
        "BBB": _signal(0.9),
        "CCC": _signal(0.9),
        "DDD": _signal(0.3),
    }
    liquidity = {"AAA": 2.0, "BBB": None, "CCC": 2.0, "DDD": None}
    first = rank_signals(signals, liquidity)
    second = rank_signals(signals, liquidity)
    assert first == second
    assert first == ["CCC", "BBB", "AAA", "DDD"]


def test_rank_signals_does_not_mutate_inputs():
    signals = {"AAA": _signal(0.8), "BBB": _signal(0.5)}
    liquidity = {"AAA": 1.0, "BBB": None}
    signals_copy = dict(signals)
    liquidity_copy = dict(liquidity)

    rank_signals(signals, liquidity)

    assert signals == signals_copy
    assert signals["AAA"] is signals_copy["AAA"]
    assert liquidity == liquidity_copy
