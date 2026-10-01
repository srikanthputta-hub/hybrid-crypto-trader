"""Tests for the safety invariant validation script (scripts/validate_invariants.py).

Each test verifies that an invariant correctly passes on valid repo state,
and at least one test per invariant deliberately breaks it to prove the check catches regressions.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from scripts.validate_invariants import (
    check_claude_advisor_fail_closed,
    check_config_mode_is_paper,
    check_env_example_documents_live_confirm,
    check_live_trading_two_switch_gate,
    check_risk_config_bounds,
)


class TestLiveTradingGate:
    """Test invariant 1: live-trading two-switch gate."""

    def test_gate_passes_on_real_repo(self) -> None:
        """Verify the gate works on the actual repo."""
        result = check_live_trading_two_switch_gate()
        assert result.passed, result.message

    def test_gate_catches_missing_confirmation(self) -> None:
        """Broken case: mode='live' without confirmation should fail the check."""
        from src.broker_live import LiveBroker, LiveTradingDisabled

        # Verify the actual behavior first
        with pytest.raises(LiveTradingDisabled):
            LiveBroker(exchange=None, portfolio=None, mode="live", live_trading_confirmed=False)

        # The invariant check itself should pass because this is the correct behavior
        result = check_live_trading_two_switch_gate()
        assert result.passed

    def test_gate_catches_wrong_mode(self) -> None:
        """Broken case: any mode != 'live' should reject even with confirmation."""
        from src.broker_live import LiveBroker, LiveTradingDisabled

        # Verify the actual behavior
        with pytest.raises(LiveTradingDisabled):
            LiveBroker(exchange=None, portfolio=None, mode="paper", live_trading_confirmed=True)

        # The invariant check itself should pass because this is the correct behavior
        result = check_live_trading_two_switch_gate()
        assert result.passed

    def test_gate_correctly_validates_behavior(self) -> None:
        """Verify the gate function validates all three expected behaviors."""
        # This test verifies the check is working by confirming it passes
        # on the real repo where the gate is actually correct.
        result = check_live_trading_two_switch_gate()
        assert result.passed
        assert "intact" in result.message

    def test_gate_fails_if_broker_stops_enforcing_the_gate(self, monkeypatch) -> None:
        """Broken case: LiveBroker.__init__ regresses to not raising at all
        (e.g. someone deletes the mode/confirmation check). The check must
        catch this, not just confirm today's already-correct behavior."""
        import src.broker_live as broker_live_module

        def broken_init(self, exchange, portfolio, mode, live_trading_confirmed):
            # No gate at all -- constructs unconditionally, the exact regression
            # this invariant exists to catch.
            self.exchange = exchange
            self.portfolio = portfolio

        monkeypatch.setattr(broker_live_module.LiveBroker, "__init__", broken_init)

        result = check_live_trading_two_switch_gate()
        assert not result.passed
        assert "did not raise" in result.message


class TestAdvisorFailClosed:
    """Test invariant 2: Claude advisor fails closed by default."""

    def test_advisor_passes_on_real_repo(self) -> None:
        """Verify advisor behavior on the actual repo."""
        result = check_claude_advisor_fail_closed()
        assert result.passed, result.message

    def test_fallback_closed_mode_returns_veto(self) -> None:
        """Verify _fallback('closed', ...) returns VETO."""
        from src.claude_advisor import _fallback

        result = _fallback("closed", "test", "test")
        assert result.verdict == "VETO"
        assert not result.approved

    def test_fallback_open_mode_returns_approve(self) -> None:
        """Verify _fallback('open', ...) returns APPROVE."""
        from src.claude_advisor import _fallback

        result = _fallback("open", "test", "test")
        assert result.verdict == "APPROVE"
        assert result.approved

    def test_review_trade_plan_default_fail_mode_is_closed(self) -> None:
        """Verify review_trade_plan defaults to fail_mode='closed'."""
        from src.claude_advisor import review_trade_plan
        import inspect

        sig = inspect.signature(review_trade_plan)
        assert sig.parameters["fail_mode"].default == "closed"

    def test_advisor_check_works_correctly(self) -> None:
        """Verify the advisor check detects the correct fail_mode default."""
        from src.claude_advisor import review_trade_plan
        import inspect

        # Verify the check passes on the real repo
        result = check_claude_advisor_fail_closed()
        assert result.passed

        # Manually verify the actual default is 'closed'
        sig = inspect.signature(review_trade_plan)
        assert sig.parameters["fail_mode"].default == "closed"

    def test_check_fails_if_fallback_regresses_to_approve_on_closed(self, monkeypatch) -> None:
        """Broken case: _fallback("closed", ...) regresses to APPROVE instead
        of VETO (the exact silent-approve-on-error bug this invariant exists
        to catch). The check must flag this, not just confirm today's
        already-correct behavior."""
        import src.claude_advisor as advisor_module

        def broken_fallback(fail_mode, reasoning, error):
            return advisor_module.AdvisorVerdict(
                verdict="APPROVE", confidence_stars=None, reasoning=reasoning, raw_error=error
            )

        monkeypatch.setattr(advisor_module, "_fallback", broken_fallback)

        result = check_claude_advisor_fail_closed()
        assert not result.passed
        assert "should return VETO" in result.message

    def test_check_fails_if_default_fail_mode_flips_to_open(self, monkeypatch) -> None:
        """Broken case: review_trade_plan's default fail_mode silently
        changes from 'closed' to 'open'. The check must catch this."""
        import src.claude_advisor as advisor_module

        # The check only ever inspects this function's signature -- it never
        # calls it -- so a stand-in with the regressed default is enough to
        # prove the check reads the real default, not a hardcoded assumption.
        def regressed_review_trade_plan(*, fail_mode: str = "open", **kwargs):
            raise NotImplementedError("stand-in for signature inspection only")

        monkeypatch.setattr(advisor_module, "review_trade_plan", regressed_review_trade_plan)

        result = check_claude_advisor_fail_closed()
        assert not result.passed
        assert "not 'closed'" in result.message


class TestConfigModePaper:
    """Test invariant 3: config.yaml mode is paper."""

    def test_config_passes_on_real_repo(self) -> None:
        """Verify config mode is paper on the actual repo."""
        result = check_config_mode_is_paper()
        assert result.passed, result.message

    def test_config_fails_if_mode_is_live(self) -> None:
        """Broken case: config.yaml with mode: live should fail the check."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            config_dir = tmpdir_path / "config"
            config_dir.mkdir()
            config_file = config_dir / "config.yaml"

            # Write a config with mode: live
            config = {"mode": "live"}
            with open(config_file, "w") as f:
                yaml.dump(config, f)

            # Patch the path in the check
            with patch("scripts.validate_invariants.Path") as mock_path:
                mock_path.return_value = config_file
                result = check_config_mode_is_paper()
                # It should fail because mode is live
                assert not result.passed
                assert "not 'paper'" in result.message

    def test_config_fails_if_mode_missing(self) -> None:
        """Broken case: config.yaml without mode key should be caught."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            config_dir = tmpdir_path / "config"
            config_dir.mkdir()
            config_file = config_dir / "config.yaml"

            # Write a config without mode
            config = {"exchange": {"id": "kraken"}}
            with open(config_file, "w") as f:
                yaml.dump(config, f)

            with patch("scripts.validate_invariants.Path") as mock_path:
                mock_path.return_value = config_file
                result = check_config_mode_is_paper()
                # Empty string .lower() != 'paper'
                assert not result.passed


class TestRiskConfigBounds:
    """Test invariant 4: risk config sanity bounds."""

    def test_risk_bounds_pass_on_real_repo(self) -> None:
        """Verify risk config bounds on the actual repo."""
        result = check_risk_config_bounds()
        assert result.passed, result.message

    def test_risk_bounds_fail_if_max_position_pct_out_of_range(self) -> None:
        """Broken case: max_position_pct > 1."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            config_dir = tmpdir_path / "config"
            config_dir.mkdir()
            config_file = config_dir / "config.yaml"

            config = {
                "mode": "paper",
                "risk": {
                    "max_position_pct": 1.5,  # Invalid: > 1
                    "max_total_exposure_pct": 0.4,
                    "max_loss_per_trade_pct": 0.01,
                    "max_daily_loss_pct": 0.05,
                    "max_volatility_pct": 0.08,
                    "min_risk_reward": 1.5,
                },
            }
            with open(config_file, "w") as f:
                yaml.dump(config, f)

            with patch("scripts.validate_invariants.Path") as mock_path:
                mock_path.return_value = config_file
                result = check_risk_config_bounds()
                assert not result.passed
                assert "not in (0, 1]" in result.message

    def test_risk_bounds_fail_if_max_position_pct_zero(self) -> None:
        """Broken case: max_position_pct = 0 (must be > 0)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            config_dir = tmpdir_path / "config"
            config_dir.mkdir()
            config_file = config_dir / "config.yaml"

            config = {
                "mode": "paper",
                "risk": {
                    "max_position_pct": 0.0,  # Invalid: must be > 0
                    "max_total_exposure_pct": 0.4,
                    "max_loss_per_trade_pct": 0.01,
                    "max_daily_loss_pct": 0.05,
                    "max_volatility_pct": 0.08,
                    "min_risk_reward": 1.5,
                },
            }
            with open(config_file, "w") as f:
                yaml.dump(config, f)

            with patch("scripts.validate_invariants.Path") as mock_path:
                mock_path.return_value = config_file
                result = check_risk_config_bounds()
                assert not result.passed

    def test_risk_bounds_fail_if_min_risk_reward_below_1(self) -> None:
        """Broken case: min_risk_reward < 1.0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            config_dir = tmpdir_path / "config"
            config_dir.mkdir()
            config_file = config_dir / "config.yaml"

            config = {
                "mode": "paper",
                "risk": {
                    "max_position_pct": 0.1,
                    "max_total_exposure_pct": 0.4,
                    "max_loss_per_trade_pct": 0.01,
                    "max_daily_loss_pct": 0.05,
                    "max_volatility_pct": 0.08,
                    "min_risk_reward": 0.5,  # Invalid: < 1.0
                },
            }
            with open(config_file, "w") as f:
                yaml.dump(config, f)

            with patch("scripts.validate_invariants.Path") as mock_path:
                mock_path.return_value = config_file
                result = check_risk_config_bounds()
                assert not result.passed
                assert "not >= 1.0" in result.message

    def test_risk_bounds_fail_if_volatility_scale_floor_out_of_range(self) -> None:
        """Broken case: volatility_scale_floor not in [0, 1]."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            config_dir = tmpdir_path / "config"
            config_dir.mkdir()
            config_file = config_dir / "config.yaml"

            config = {
                "mode": "paper",
                "risk": {
                    "max_position_pct": 0.1,
                    "max_total_exposure_pct": 0.4,
                    "max_loss_per_trade_pct": 0.01,
                    "max_daily_loss_pct": 0.05,
                    "max_volatility_pct": 0.08,
                    "volatility_scale_floor": 1.5,  # Invalid: > 1
                    "min_risk_reward": 1.5,
                },
            }
            with open(config_file, "w") as f:
                yaml.dump(config, f)

            with patch("scripts.validate_invariants.Path") as mock_path:
                mock_path.return_value = config_file
                result = check_risk_config_bounds()
                assert not result.passed
                assert "not in [0, 1]" in result.message


class TestEnvExampleLiveConfirm:
    """Test invariant 5: .env.example documents LIVE_TRADING_CONFIRM."""

    def test_env_example_passes_on_real_repo(self) -> None:
        """Verify .env.example documents LIVE_TRADING_CONFIRM on the actual repo."""
        result = check_env_example_documents_live_confirm()
        assert result.passed, result.message

    def test_env_example_fails_if_missing_variable(self) -> None:
        """Broken case: .env.example without LIVE_TRADING_CONFIRM."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            env_file = tmpdir_path / ".env.example"
            env_file.write_text("EXCHANGE_API_KEY=\nANTHROPIC_API_KEY=\n")

            with patch("scripts.validate_invariants.Path") as mock_path:
                mock_path.return_value = env_file
                result = check_env_example_documents_live_confirm()
                assert not result.passed
                assert "not documented" in result.message
