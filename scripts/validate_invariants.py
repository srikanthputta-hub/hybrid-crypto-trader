"""Safety invariant validation: runs on every CI push to catch safety-gate regressions.

This script enforces hard behavioral and configuration invariants that CLAUDE.md
flags as "Non-negotiables" — not just test coverage, but mechanical checks on
config values and the actual runtime behavior of safety-critical modules.

Each invariant maps to something that could break the bot's core safety properties:
  1. Live-trading two-switch gate — the only thing between a typo and real money.
  2. Claude advisor fail-closed — advisor errors must never silently approve a trade.
  3. Config mode is paper — live mode must only be set by explicit operator action.
  4. Risk config sanity bounds — malformed risk values disable every safety check.
  5. Environment docs — LIVE_TRADING_CONFIRM must remain discoverable in .env.example.
  6. Bar-parity loop keys well-formed — a bad value stops the bot at startup (restart loop).

Exit 0 if all pass, 1 if any fail.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class InvariantResult:
    name: str
    passed: bool
    message: str


def check_live_trading_two_switch_gate() -> InvariantResult:
    """Verify LiveBroker raises on both missing-confirmation and wrong-mode cases."""
    try:
        from src.broker_live import LiveBroker, LiveTradingDisabled

        # Test 1: mode="live" without confirmation should raise
        try:
            LiveBroker(exchange=None, portfolio=None, mode="live", live_trading_confirmed=False)
            return InvariantResult(
                "live_trading_gate",
                False,
                "FAIL: LiveBroker(mode='live', live_trading_confirmed=False) did not raise LiveTradingDisabled",
            )
        except LiveTradingDisabled:
            pass  # Expected

        # Test 2: mode != "live" should always raise, even with confirmation
        try:
            LiveBroker(exchange=None, portfolio=None, mode="paper", live_trading_confirmed=True)
            return InvariantResult(
                "live_trading_gate",
                False,
                "FAIL: LiveBroker(mode='paper', live_trading_confirmed=True) did not raise LiveTradingDisabled",
            )
        except LiveTradingDisabled:
            pass  # Expected

        # Test 3: mode="live" WITH confirmation should NOT raise (the gate is open).
        try:
            LiveBroker(exchange=None, portfolio=None, mode="live", live_trading_confirmed=True)
            # It should have succeeded — the gate is open. If we got here, the gate works.
            pass
        except LiveTradingDisabled as e:
            return InvariantResult(
                "live_trading_gate",
                False,
                f"FAIL: LiveBroker(mode='live', live_trading_confirmed=True) raised LiveTradingDisabled when it shouldn't: {e}",
            )

        return InvariantResult("live_trading_gate", True, "PASS: Live-trading two-switch gate intact")

    except Exception as e:
        return InvariantResult("live_trading_gate", False, f"FAIL: Unexpected error: {e}")


def check_claude_advisor_fail_closed() -> InvariantResult:
    """Verify advisor fails closed by default and only says APPROVE or VETO."""
    try:
        from src.claude_advisor import _fallback, AdvisorVerdict, review_trade_plan

        # Test 1: _fallback in "closed" mode should return VETO
        result = _fallback("closed", "test reasoning", "test error")
        if result.verdict != "VETO" or result.approved is not False:
            return InvariantResult(
                "advisor_fail_closed",
                False,
                f"FAIL: _fallback('closed', ...) should return VETO, got {result.verdict}",
            )

        # Test 2: _fallback in "open" mode should return APPROVE
        result = _fallback("open", "test reasoning", "test error")
        if result.verdict != "APPROVE":
            return InvariantResult(
                "advisor_fail_closed",
                False,
                f"FAIL: _fallback('open', ...) should return APPROVE, got {result.verdict}",
            )

        # Test 3: review_trade_plan's default fail_mode must be "closed"
        import inspect

        sig = inspect.signature(review_trade_plan)
        fail_mode_param = sig.parameters.get("fail_mode")
        if fail_mode_param is None:
            return InvariantResult(
                "advisor_fail_closed",
                False,
                "FAIL: review_trade_plan has no fail_mode parameter",
            )
        if fail_mode_param.default != "closed":
            return InvariantResult(
                "advisor_fail_closed",
                False,
                f"FAIL: review_trade_plan fail_mode default is {fail_mode_param.default!r}, not 'closed'",
            )

        return InvariantResult("advisor_fail_closed", True, "PASS: Claude advisor fails closed by default")

    except Exception as e:
        return InvariantResult("advisor_fail_closed", False, f"FAIL: Unexpected error: {e}")


def check_config_mode_is_paper() -> InvariantResult:
    """Verify config/config.yaml has mode: paper, not mode: live."""
    try:
        config_path = Path("config/config.yaml")
        if not config_path.exists():
            return InvariantResult("config_mode_paper", False, f"FAIL: {config_path} not found")

        with open(config_path) as f:
            config = yaml.safe_load(f)

        mode = config.get("mode", "").strip().lower()
        if mode != "paper":
            return InvariantResult(
                "config_mode_paper",
                False,
                f"FAIL: config.yaml mode is {mode!r}, not 'paper'. Live mode must only be set by explicit operator action.",
            )

        return InvariantResult("config_mode_paper", True, "PASS: config.yaml mode is paper")

    except Exception as e:
        return InvariantResult("config_mode_paper", False, f"FAIL: Unexpected error: {e}")


def _window_representable(minutes: float) -> bool:
    """Mirror of src/risk_engine.py's own check: the window must convert to a strictly
    positive timedelta (not overflow, not round down to zero at microsecond resolution)."""
    from datetime import timedelta

    try:
        return timedelta(minutes=minutes) > timedelta(0)
    except OverflowError:
        return False


def _is_finite_config_number(value: Any) -> bool:
    """Mirror of src/risk_engine.py's config rule: an int or float (bool excluded -- it is an
    int subclass), and finite (an int too large for a float counts as not finite)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _cooldown_representable(minutes: float) -> bool:
    """Mirror of src/risk_engine.py: cooldown_minutes must convert to a timedelta."""
    from datetime import timedelta

    try:
        timedelta(minutes=minutes)
        return True
    except OverflowError:
        return False


def check_risk_config_bounds() -> InvariantResult:
    """Verify risk config values are in valid ranges."""
    try:
        config_path = Path("config/config.yaml")
        if not config_path.exists():
            return InvariantResult("risk_config_bounds", False, f"FAIL: {config_path} not found")

        with open(config_path) as f:
            config = yaml.safe_load(f)

        risk_config = config.get("risk", {})
        if not risk_config:
            return InvariantResult("risk_config_bounds", False, "FAIL: No risk config found")

        # Check parameters that must be in (0, 1]
        check_params_0_1 = [
            "max_position_pct",
            "max_total_exposure_pct",
            "max_loss_per_trade_pct",
            "max_daily_loss_pct",
            "max_volatility_pct",
        ]
        for param in check_params_0_1:
            value = risk_config.get(param)
            if value is None:
                return InvariantResult(
                    "risk_config_bounds", False, f"FAIL: Missing required risk parameter {param}"
                )
            # bool excluded (True == 1 would pass the range check); NaN/inf already fail
            # `0 < value <= 1`, the isfinite is explicit to match src/risk_engine.py.
            if not _is_finite_config_number(value) or not (0 < value <= 1):
                return InvariantResult(
                    "risk_config_bounds",
                    False,
                    f"FAIL: {param} = {value!r} is not in (0, 1]",
                )

        # Check volatility_scale_floor [0, 1]
        vsf = risk_config.get("volatility_scale_floor")
        if vsf is not None:
            if not _is_finite_config_number(vsf) or not (0 <= vsf <= 1):
                return InvariantResult(
                    "risk_config_bounds",
                    False,
                    f"FAIL: volatility_scale_floor = {vsf!r} is not in [0, 1]",
                )

        # Global new-entry rate cap (optional keys; src/risk_engine.py has defaults).
        # RiskEngine refuses to construct on a bad value, which on the VM would mean a
        # restart loop with no stop-losses being executed -- catch it in CI instead.
        mne = risk_config.get("max_new_entries_per_window")
        if mne is not None:
            if isinstance(mne, bool) or not isinstance(mne, int) or mne < 1:
                return InvariantResult(
                    "risk_config_bounds",
                    False,
                    f"FAIL: max_new_entries_per_window = {mne!r} is not an integer >= 1",
                )
        new_win = risk_config.get("new_entry_window_minutes")
        if new_win is not None:
            if (
                isinstance(new_win, bool)
                or not isinstance(new_win, (int, float))
                or not math.isfinite(new_win)
                or new_win <= 0
                or not _window_representable(new_win)
            ):
                return InvariantResult(
                    "risk_config_bounds",
                    False,
                    f"FAIL: new_entry_window_minutes = {new_win!r} is not a finite number > 0",
                )

        # Minimum order size floor (optional key; src/risk_engine.py defaults it to 0 =
        # disabled). Same restart-loop concern as above: RiskEngine refuses to construct
        # on a negative/non-finite/non-numeric value.
        mo = risk_config.get("min_order_quote_usd")
        if mo is not None:
            if isinstance(mo, bool) or not isinstance(mo, (int, float)) or not math.isfinite(mo) or mo < 0:
                return InvariantResult(
                    "risk_config_bounds",
                    False,
                    f"FAIL: min_order_quote_usd = {mo!r} is not a finite number >= 0",
                )

        # Check min_risk_reward >= 1.0
        mrr = risk_config.get("min_risk_reward")
        if mrr is None:
            return InvariantResult("risk_config_bounds", False, "FAIL: Missing min_risk_reward")
        # Written as `not (mrr >= 1.0)` plus an explicit finite check: `nan < 1.0` is False,
        # so the previous `mrr < 1.0` let a NaN (and inf) through.
        if not _is_finite_config_number(mrr) or not (mrr >= 1.0):
            return InvariantResult(
                "risk_config_bounds",
                False,
                f"FAIL: min_risk_reward = {mrr!r} is not >= 1.0",
            )

        # Required keys src/bot.py reads directly (a missing one is a KeyError at startup;
        # a bad one makes RiskEngine refuse to construct -- the same restart-loop concern
        # as above, so catch it in CI). Same bounds as RiskEngine.__init__.
        cooldown = risk_config.get("cooldown_minutes")
        if cooldown is None:
            return InvariantResult("risk_config_bounds", False, "FAIL: Missing cooldown_minutes")
        if not _is_finite_config_number(cooldown) or not (cooldown >= 0) or not _cooldown_representable(cooldown):
            return InvariantResult(
                "risk_config_bounds",
                False,
                f"FAIL: cooldown_minutes = {cooldown!r} is not a finite number >= 0",
            )
        cash = risk_config.get("starting_cash_usd")
        if cash is None:
            return InvariantResult("risk_config_bounds", False, "FAIL: Missing starting_cash_usd")
        if not _is_finite_config_number(cash) or not (cash > 0):
            return InvariantResult(
                "risk_config_bounds",
                False,
                f"FAIL: starting_cash_usd = {cash!r} is not a finite number > 0",
            )

        return InvariantResult("risk_config_bounds", True, "PASS: Risk config bounds are valid")

    except Exception as e:
        return InvariantResult("risk_config_bounds", False, f"FAIL: Unexpected error: {e}")


def check_bar_parity_config() -> InvariantResult:
    """Verify the bar-parity loop keys (loop.bar_parity, loop.max_entry_delay_minutes,
    logging.bar_decisions_path, logging.closed_bars_dir) are well-formed. src/bot.py's
    build_components refuses to start on a bad value -- on the VM that is a restart loop with
    no stop-losses executed, so catch it in CI. Uses the bot's own startup parser
    (src.config.parse_bar_parity_settings), so this can never accept what the bot rejects.
    Only checks well-formedness: it does not require any particular on/off setting."""
    try:
        from src.config import parse_bar_parity_settings

        config_path = Path("config/config.yaml")
        if not config_path.exists():
            return InvariantResult("bar_parity_config", False, f"FAIL: {config_path} not found")
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
        try:
            settings = parse_bar_parity_settings(config)
        except ValueError as e:
            return InvariantResult("bar_parity_config", False, f"FAIL: {e}")
        return InvariantResult(
            "bar_parity_config",
            True,
            f"PASS: bar-parity keys valid (bar_parity={settings.enabled}, "
            f"max_entry_delay_minutes={settings.max_entry_delay_minutes})",
        )
    except Exception as e:
        return InvariantResult("bar_parity_config", False, f"FAIL: Unexpected error: {e}")


def check_env_example_documents_live_confirm() -> InvariantResult:
    """Verify .env.example documents LIVE_TRADING_CONFIRM."""
    try:
        env_example_path = Path(".env.example")
        if not env_example_path.exists():
            return InvariantResult(
                "env_example_live_confirm",
                False,
                f"FAIL: {env_example_path} not found",
            )

        with open(env_example_path) as f:
            content = f.read()

        if "LIVE_TRADING_CONFIRM" not in content:
            return InvariantResult(
                "env_example_live_confirm",
                False,
                "FAIL: LIVE_TRADING_CONFIRM not documented in .env.example",
            )

        return InvariantResult(
            "env_example_live_confirm", True, "PASS: .env.example documents LIVE_TRADING_CONFIRM"
        )

    except Exception as e:
        return InvariantResult(
            "env_example_live_confirm", False, f"FAIL: Unexpected error: {e}"
        )


def main() -> int:
    """Run all invariant checks and print results."""
    checks = [
        check_live_trading_two_switch_gate,
        check_claude_advisor_fail_closed,
        check_config_mode_is_paper,
        check_risk_config_bounds,
        check_env_example_documents_live_confirm,
        check_bar_parity_config,
    ]

    results = [check() for check in checks]

    # Print results table
    print("\n" + "=" * 70)
    print("SAFETY INVARIANT VALIDATION")
    print("=" * 70)
    for result in results:
        status = "✓ PASS" if result.passed else "✗ FAIL"
        print(f"{status}  {result.name:.<45} {result.message}")
    print("=" * 70)

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print(f"\nResult: {passed}/{total} invariants passed\n")

    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
