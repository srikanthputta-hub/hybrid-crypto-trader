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
