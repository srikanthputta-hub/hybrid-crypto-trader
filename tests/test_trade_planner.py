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


def test_adx_confirmed_trend_widens_stop_loss_vs_unconfirmed():
    # Same setup, differing only in ADX: high-ADX (confirmed trend) should
    # push the stop loss further away (wider risk distance) than a low-ADX
    # or missing-ADX signal, per the regime-filter research finding.
    high_adx_signal = Signal(
        direction="bullish",
        primary_pattern="BREAKOUT",
        engine_score=0.9,
        primary_details={"level": 118.5, "adx": 30},
    )
    low_adx_signal = Signal(
        direction="bullish",
        primary_pattern="BREAKOUT",
        engine_score=0.9,
        primary_details={"level": 118.5, "adx": 10},
    )
    no_adx_signal = Signal(
        direction="bullish",
        primary_pattern="BREAKOUT",
        engine_score=0.9,
        primary_details={"level": 118.5},
    )

    high_plan = build_plan(high_adx_signal, "BTC/USDT", current_price=119.0, atr=1.0)
    low_plan = build_plan(low_adx_signal, "BTC/USDT", current_price=119.0, atr=1.0)
    no_adx_plan = build_plan(no_adx_signal, "BTC/USDT", current_price=119.0, atr=1.0)

    high_distance = high_plan.entry_zone[0] - high_plan.stop_loss
    low_distance = low_plan.entry_zone[0] - low_plan.stop_loss
    no_adx_distance = no_adx_plan.entry_zone[0] - no_adx_plan.stop_loss

    assert high_distance > low_distance
    assert high_distance > no_adx_distance
    assert low_distance == pytest.approx(no_adx_distance)

    assert high_plan.rationale["adx_trend_confirmed"] is True
    assert low_plan.rationale["adx_trend_confirmed"] is False
    assert no_adx_plan.rationale["adx_trend_confirmed"] is False


def test_adx_confirmed_trend_keeps_level_ordering_invariant():
    signal = Signal(
        direction="bullish",
        primary_pattern="BREAKOUT",
        engine_score=0.9,
        primary_details={"level": 118.5, "adx": 40},
    )
    plan = build_plan(signal, "BTC/USDT", current_price=119.0, atr=1.0)
    entry_low, entry_high = plan.entry_zone
    assert plan.invalidation < plan.stop_loss < entry_low <= entry_high < plan.take_profit


def test_missing_adx_key_is_byte_identical_to_pre_upgrade_behavior():
    # Backward-compatibility regression test: a signal with no "adx" key at
    # all (e.g. PULLBACK/REVERSAL patterns, or any hand-built Signal from
    # before this upgrade) must produce numerically identical output to the
    # pre-upgrade formula, i.e. effective stop multiplier == stop_atr_mult.
    signal = Signal(
        direction="bullish",
        primary_pattern="BREAKOUT",
        engine_score=0.9,
        supporting_patterns=["BREAKOUT", "MOMENTUM"],
        primary_details={"level": 118.5},
    )
    plan = build_plan(signal, "BTC/USDT", current_price=119.0, atr=1.0)

    # Recompute what pre-upgrade build_plan would have produced by hand.
    atr = 1.0
    current_price = 119.0
    level = 118.5
    entry_buffer_atr_mult = 0.1
    stop_atr_mult = 1.0
    invalidation_atr_mult = 0.5
    min_risk_reward = 2.0

    entry_low = min(level, current_price) - entry_buffer_atr_mult * atr
    entry_high = max(level, current_price) + entry_buffer_atr_mult * atr
    stop_loss = entry_low - stop_atr_mult * atr
    invalidation = stop_loss - invalidation_atr_mult * atr
    risk_per_unit = entry_high - stop_loss
    take_profit = entry_high + min_risk_reward * risk_per_unit
    risk_reward = (take_profit - entry_high) / risk_per_unit

    assert plan.entry_zone == (round(entry_low, 8), round(entry_high, 8))
    assert plan.stop_loss == round(stop_loss, 8)
    assert plan.take_profit == round(take_profit, 8)
    assert plan.invalidation == round(invalidation, 8)
    assert plan.risk_reward == round(risk_reward, 3)
