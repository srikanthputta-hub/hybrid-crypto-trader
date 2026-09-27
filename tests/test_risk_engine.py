from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.risk_engine import RiskEngine


def make_engine(tmp_path: Path, **overrides) -> RiskEngine:
    defaults = dict(
        starting_cash_usd=10000,
        max_position_pct=0.20,
        max_total_exposure_pct=0.50,
        max_loss_per_trade_pct=0.01,
        max_daily_loss_pct=0.05,
        max_volatility_pct=0.08,
        min_risk_reward=1.5,
        cooldown_minutes=60,
        kill_switch_path=tmp_path / "KILL_SWITCH",
        symbol_whitelist=["BTC/USDT", "ETH/USDT"],
    )
    defaults.update(overrides)
    return RiskEngine(**defaults)


def test_rejects_symbol_not_in_whitelist(tmp_path):
    engine = make_engine(tmp_path)
    decision = engine.check_new_trade("DOGE/USDT", "buy", 10000, 10000)
    assert not decision.allowed
    assert "whitelist" in decision.reason


def test_kill_switch_blocks_new_buys(tmp_path):
    engine = make_engine(tmp_path)
    engine.kill_switch_path.touch()
    decision = engine.check_new_trade("BTC/USDT", "buy", 10000, 10000)
    assert not decision.allowed
    assert "kill switch" in decision.reason


def test_kill_switch_never_blocks_sells(tmp_path):
    engine = make_engine(tmp_path)
    engine.kill_switch_path.touch()
    decision = engine.check_new_trade("BTC/USDT", "sell", 10000, 0)
    assert decision.allowed


def test_daily_loss_limit_blocks_new_buys(tmp_path):
    engine = make_engine(tmp_path)
    now = datetime.now(timezone.utc)
    engine.check_new_trade("BTC/USDT", "buy", 10000, 10000, now=now)  # seeds day-start equity
    decision = engine.check_new_trade("BTC/USDT", "buy", 9400, 9400, now=now)  # -6% > 5% limit
    assert not decision.allowed
    assert "daily loss limit" in decision.reason


def test_cooldown_blocks_rapid_repeat_trades(tmp_path):
    engine = make_engine(tmp_path, cooldown_minutes=60)
    now = datetime.now(timezone.utc)
    engine.check_new_trade("BTC/USDT", "buy", 10000, 10000, now=now)
    engine.record_trade("BTC/USDT", now=now)
    decision = engine.check_new_trade("BTC/USDT", "buy", 10000, 10000, now=now + timedelta(minutes=5))
    assert not decision.allowed
    assert "cooldown" in decision.reason


def test_high_volatility_blocks_new_entries(tmp_path):
    engine = make_engine(tmp_path, max_volatility_pct=0.05)
    decision = engine.check_new_trade("BTC/USDT", "buy", 10000, 10000, atr_pct=0.10)
    assert not decision.allowed
    assert "volatility" in decision.reason


def test_poor_risk_reward_is_rejected(tmp_path):
    engine = make_engine(tmp_path, min_risk_reward=2.0)
    decision = engine.check_new_trade("BTC/USDT", "buy", 10000, 10000, risk_reward=1.2)
    assert not decision.allowed
    assert "risk/reward" in decision.reason


def test_position_sized_by_per_trade_risk_budget(tmp_path):
    # equity=10000, max_loss_per_trade_pct=0.01 -> risk budget = $100.
    # entry=50000, stop=49000 -> stop distance = 2% -> risk-sized spend = 100 / 0.02 = $5000.
    engine = make_engine(tmp_path, max_loss_per_trade_pct=0.01, max_position_pct=0.99)
    decision = engine.check_new_trade(
        "BTC/USDT", "buy", portfolio_equity=10000, cash_available=10000,
        entry_price=50000, stop_loss=49000,
    )
    assert decision.allowed
    assert decision.max_quote_amount == pytest.approx(5000.0, rel=0.01)


def test_total_exposure_cap_limits_spend(tmp_path):
    # equity=10000, max_total_exposure_pct=0.50 -> max total exposure = $5000.
    # already $4500 committed to other positions -> only $500 of room left.
    engine = make_engine(tmp_path, max_total_exposure_pct=0.50, max_position_pct=0.99)
    decision = engine.check_new_trade(
        "BTC/USDT", "buy", portfolio_equity=10000, cash_available=10000,
        open_positions_value=4500,
    )
    assert decision.allowed
    assert decision.max_quote_amount == pytest.approx(500.0, rel=0.01)


def test_exposure_cap_already_exhausted_rejects_trade(tmp_path):
    engine = make_engine(tmp_path, max_total_exposure_pct=0.50)
    decision = engine.check_new_trade(
        "BTC/USDT", "buy", portfolio_equity=10000, cash_available=10000,
        open_positions_value=5000,
    )
    assert not decision.allowed


def test_sell_never_blocked_by_any_check(tmp_path):
    engine = make_engine(tmp_path)
    decision = engine.check_new_trade("NOT/WHITELISTED", "sell", 10000, 0)
    assert decision.allowed


# --- volatility-scaled position sizing (soft taper below the hard volatility gate) ---
#
# Shared scenario: equity=10000, max_loss_per_trade_pct=0.01 -> base risk budget $100;
# entry=50000, stop=49000 -> 2% stop distance -> unscaled risk-sized spend = $5000.
# max_position_pct=0.99 and max_total_exposure_pct=0.99 so the risk-based sizing is the
# binding constraint (otherwise a flat cap would mask the taper entirely).
# max_volatility_pct=0.08 -> taper starts at 0.04, reaches volatility_scale_floor (0.5) at 0.08.

UNSCALED_RISK_SPEND = 5000.0


def _vol_engine(tmp_path, **overrides):
    params = dict(max_loss_per_trade_pct=0.01, max_position_pct=0.99, max_total_exposure_pct=0.99,
                  max_volatility_pct=0.08)
    params.update(overrides)
    return make_engine(tmp_path, **params)


def _vol_trade(engine, atr_pct):
    return engine.check_new_trade(
        "BTC/USDT", "buy", portfolio_equity=10000, cash_available=10000,
        entry_price=50000, stop_loss=49000, atr_pct=atr_pct,
    )


def test_pre_upgrade_risk_sizing_unchanged_without_atr_pct(tmp_path):
    # Backward-compat regression: hand-recompute the exact pre-upgrade number for the
    # test_position_sized_by_per_trade_risk_budget scenario (no atr_pct passed).
    # risk budget = 10000 * 0.01 = 100; stop distance = (50000 - 49000) / 50000 = 0.02;
    # spend = min(10000 * 0.99, cash 10000, 100 / 0.02, 10000 * 0.50 - 0) = min(9900, 10000, 5000, 5000) = 5000.
    engine = make_engine(tmp_path, max_loss_per_trade_pct=0.01, max_position_pct=0.99)
    decision = engine.check_new_trade(
        "BTC/USDT", "buy", portfolio_equity=10000, cash_available=10000,
        entry_price=50000, stop_loss=49000,
    )
    assert decision.allowed
    assert engine.volatility_scale_floor == 0.5  # new default
    assert decision.max_quote_amount == pytest.approx(5000.0, abs=1e-9)


@pytest.mark.parametrize("atr_pct", [0.0, 0.01, 0.03, 0.04])  # 0.04 == exactly half of max_volatility_pct
def test_low_volatility_sizes_identically_to_no_atr_baseline(tmp_path, atr_pct):
    engine = _vol_engine(tmp_path)
    baseline = _vol_trade(engine, atr_pct=None)
    decision = _vol_trade(engine, atr_pct=atr_pct)
    assert baseline.allowed and decision.allowed
    assert baseline.max_quote_amount == pytest.approx(UNSCALED_RISK_SPEND, abs=1e-9)
    assert decision.max_quote_amount == baseline.max_quote_amount


def test_high_but_allowed_volatility_shrinks_position(tmp_path):
    # atr_pct=0.072 is 90% of max_volatility_pct (still below the hard gate, so still allowed).
    # frac = (0.072 - 0.04) / (0.08 - 0.04) = 0.8 -> scale = 1 - 0.8 * 0.5 = 0.6 -> spend = 3000.
    engine = _vol_engine(tmp_path)
    baseline = _vol_trade(engine, atr_pct=None)
    low = _vol_trade(engine, atr_pct=0.01)
    decision = _vol_trade(engine, atr_pct=0.072)
    assert decision.allowed
    assert decision.max_quote_amount < baseline.max_quote_amount
    assert decision.max_quote_amount < low.max_quote_amount
    assert decision.max_quote_amount == pytest.approx(3000.0, rel=1e-9)


def test_position_size_non_increasing_as_volatility_rises(tmp_path):
    engine = _vol_engine(tmp_path)
    atr_levels = [0.02, 0.04, 0.05, 0.06, 0.07, 0.079, 0.08]
    sizes = []
    for atr_pct in atr_levels:
        decision = _vol_trade(engine, atr_pct=atr_pct)
        assert decision.allowed, f"atr_pct={atr_pct} should still clear the hard gate"
        sizes.append(decision.max_quote_amount)
    assert all(later <= earlier for earlier, later in zip(sizes, sizes[1:])), sizes
    assert sizes[-1] < sizes[0]


@pytest.mark.parametrize("floor", [0.0, 0.25, 0.5, 1.0])
def test_taper_never_overshoots_floor_near_hard_cap(tmp_path, floor):
    engine = _vol_engine(tmp_path, volatility_scale_floor=floor)
    for atr_pct in (0.0799, 0.079999999, 0.08):
        scale = engine._volatility_scale(atr_pct)
        assert floor <= scale <= 1.0
        decision = _vol_trade(engine, atr_pct=atr_pct)
        if floor == 0.0 and atr_pct == 0.08:
            # a zero floor sizes to nothing at the cap -> "no room" rejection, never a bigger trade
            assert not decision.allowed
            continue
        assert decision.allowed
        assert decision.max_quote_amount >= floor * UNSCALED_RISK_SPEND - 1e-9
        assert decision.max_quote_amount <= UNSCALED_RISK_SPEND
    # the helper itself stays bounded even for inputs the hard gate would normally reject first
    assert engine._volatility_scale(0.5) == floor
    assert engine._volatility_scale(-0.1) == 1.0


def test_hard_volatility_gate_still_rejects_above_cap_with_taper(tmp_path):
    engine = _vol_engine(tmp_path)
    decision = _vol_trade(engine, atr_pct=0.0801)
    assert not decision.allowed
    assert "volatility" in decision.reason


def test_nan_atr_pct_never_bypasses_risk_sizing(tmp_path):
    # Fail-closed: a NaN atr_pct (e.g. ATR not yet warmed up) must NOT produce a NaN risk
    # budget — min(spend, nan) silently returns spend, which would drop the per-trade risk
    # cap and size the trade at the flat max_position_pct ceiling ($9900 here).
    # Instead it is treated as maximally uncertain and sized at the floor.
    engine = _vol_engine(tmp_path)
    decision = _vol_trade(engine, atr_pct=float("nan"))
    assert decision.allowed
    assert decision.max_quote_amount == pytest.approx(0.5 * UNSCALED_RISK_SPEND, rel=1e-9)


def test_non_finite_max_volatility_pct_scales_to_floor(tmp_path):
    engine = _vol_engine(tmp_path, max_volatility_pct=float("nan"))
    assert engine._volatility_scale(0.01) == engine.volatility_scale_floor


@pytest.mark.parametrize("bad_floor", [1.5, -0.1, float("nan"), float("inf")])
def test_nonsensical_volatility_scale_floor_is_rejected(tmp_path, bad_floor):
    # A floor > 1 would *enlarge* positions; a negative/non-finite floor makes no sense.
    # Refuse to construct the engine rather than guess.
    with pytest.raises(ValueError):
        _vol_engine(tmp_path, volatility_scale_floor=bad_floor)
