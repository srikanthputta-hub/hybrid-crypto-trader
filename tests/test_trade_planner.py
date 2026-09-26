import pytest

from src.signal_engine import Signal
from src.trade_planner import build_plan


def test_bearish_signal_produces_no_plan():
    signal = Signal(direction="bearish", primary_pattern="REVERSAL", engine_score=0.8)
    assert build_plan(signal, "BTC/USDT", current_price=100.0, atr=1.0) is None


def test_breakout_plan_has_correctly_ordered_levels():
    signal = Signal(
        direction="bullish",
        primary_pattern="BREAKOUT",
        engine_score=0.9,
        supporting_patterns=["BREAKOUT", "MOMENTUM"],
        primary_details={"level": 118.5},
    )
    plan = build_plan(signal, "BTC/USDT", current_price=119.0, atr=1.0)
    assert plan is not None
    assert plan.side == "buy"
    entry_low, entry_high = plan.entry_zone
    # Every level must sit in the right order relative to the others.
    assert plan.invalidation < plan.stop_loss < entry_low <= entry_high < plan.take_profit


def test_risk_reward_matches_configured_minimum():
    signal = Signal(direction="bullish", primary_pattern="MOMENTUM", engine_score=0.7, primary_details={})
    plan = build_plan(signal, "ETH/USDT", current_price=2000.0, atr=20.0, min_risk_reward=2.5)
    assert plan.risk_reward == pytest.approx(2.5, abs=0.01)


def test_confidence_stars_are_in_range_and_step_of_half():
    signal = Signal(direction="bullish", primary_pattern="MOMENTUM", engine_score=0.95, primary_details={})
    plan = build_plan(signal, "BTC/USDT", current_price=100.0, atr=1.0)
    assert 1.0 <= plan.rule_based_confidence_stars <= 5.0
    assert (plan.rule_based_confidence_stars * 2) % 1 == 0  # multiple of 0.5


def test_higher_engine_score_yields_higher_or_equal_confidence():
    low = build_plan(
        Signal(direction="bullish", primary_pattern="MOMENTUM", engine_score=0.5, primary_details={}),
        "BTC/USDT", 100.0, 1.0,
    )
    high = build_plan(
        Signal(direction="bullish", primary_pattern="MOMENTUM", engine_score=0.95, primary_details={}),
        "BTC/USDT", 100.0, 1.0,
    )
    assert high.rule_based_confidence_stars >= low.rule_based_confidence_stars


def test_zero_or_missing_atr_falls_back_to_price_based_estimate():
    signal = Signal(direction="bullish", primary_pattern="MOMENTUM", engine_score=0.6, primary_details={})
    plan = build_plan(signal, "BTC/USDT", current_price=100.0, atr=0.0)
    assert plan is not None
    assert plan.stop_loss < plan.entry_zone[0]


def test_level_equal_to_price_still_produces_nonzero_width_entry_zone():
    signal = Signal(
        direction="bullish", primary_pattern="BREAKOUT", engine_score=0.7, primary_details={"level": 100.0}
    )
    plan = build_plan(signal, "BTC/USDT", current_price=100.0, atr=1.0)
    entry_low, entry_high = plan.entry_zone
    assert entry_high > entry_low
