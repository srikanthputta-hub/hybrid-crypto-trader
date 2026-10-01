import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.bot import _fmt_price, _maybe_notify, build_components
from src.config import Config
from src.portfolio import Portfolio

CONFIGURED = SimpleNamespace(telegram_bot_token="token", telegram_chat_id="12345")
UNCONFIGURED = SimpleNamespace(telegram_bot_token=None, telegram_chat_id=None)


def test_fmt_price_scales_decimals_to_stay_readable():
    # A fixed 2-decimal format would render a sub-cent coin like SHIB as
    # "$0.00" -- useless. Precision should scale up as price shrinks.
    assert _fmt_price(50000.0) == "$50,000.00"
    assert _fmt_price(0.1234) == "$0.1234"
    assert _fmt_price(0.00001234) == "$0.000012"


def test_noop_when_telegram_not_configured():
    event = {"symbol": "BTC/USDT", "action": "planned", "plan": {"pattern": "BREAKOUT"}}
    with patch("src.bot.notifier.send_telegram_message") as mock_send:
        _maybe_notify(UNCONFIGURED, event)
    mock_send.assert_not_called()


def test_noop_action_sends_nothing():
    event = {"symbol": "BTC/USDT", "action": "none"}
    with patch("src.bot.notifier.send_telegram_message") as mock_send:
        _maybe_notify(CONFIGURED, event)
    mock_send.assert_not_called()


def test_new_plan_sends_signal_notification():
    event = {
        "symbol": "BTC/USDT",
        "action": "planned",
        "plan": {
            "pattern": "BREAKOUT",
            "entry_zone": (49500.0, 50000.0),
            "stop_loss": 48000.0,
            "take_profit": 53000.0,
            "risk_reward": 2.3,
            "rule_based_confidence_stars": 4.0,
        },
    }
    with patch("src.bot.notifier.send_telegram_message") as mock_send:
        _maybe_notify(CONFIGURED, event)
    mock_send.assert_called_once()
    text = mock_send.call_args[0][2]
    assert "BUY signal" in text
    assert "BTC/USDT" in text
    assert "BREAKOUT" in text
    assert "$49,500.00" in text and "$50,000.00" in text  # entry zone
    assert "$48,000.00" in text  # stop-loss
    assert "$53,000.00" in text  # target


def test_fill_sends_filled_notification():
    event = {
        "symbol": "BTC/USDT",
        "action": "none",
        "monitor_action": {"action": "FILL"},
        "execution": {"price": 50000.0, "base_amount": 0.01},
    }
    with patch("src.bot.notifier.send_telegram_message") as mock_send:
        _maybe_notify(CONFIGURED, event)
    mock_send.assert_called_once()
    text = mock_send.call_args[0][2]
    assert "Bought" in text
    assert "$50,000.00" in text


def test_stop_loss_close_sends_closed_notification():
    event = {
        "symbol": "BTC/USDT",
        "action": "none",
        "monitor_action": {"action": "STOP_LOSS"},
        "execution": {"price": 47000.0, "base_amount": 0.01},
    }
    with patch("src.bot.notifier.send_telegram_message") as mock_send:
        _maybe_notify(CONFIGURED, event)
    mock_send.assert_called_once()
    text = mock_send.call_args[0][2]
    assert "closed" in text
    assert "Stop-loss hit" in text
    assert "$47,000.00" in text


def test_take_profit_close_sends_closed_notification():
    event = {
        "symbol": "BTC/USDT",
        "action": "none",
        "monitor_action": {"action": "TAKE_PROFIT"},
        "execution": {"price": 53000.0, "base_amount": 0.01},
    }
    with patch("src.bot.notifier.send_telegram_message") as mock_send:
        _maybe_notify(CONFIGURED, event)
    mock_send.assert_called_once()
    text = mock_send.call_args[0][2]
    assert "closed" in text
    assert "Target reached" in text
    assert "$53,000.00" in text


def test_discard_sends_nothing():
    event = {"symbol": "BTC/USDT", "action": "none", "monitor_action": {"action": "DISCARD"}}
    with patch("src.bot.notifier.send_telegram_message") as mock_send:
        _maybe_notify(CONFIGURED, event)
    mock_send.assert_not_called()


def _minimal_raw_config() -> dict:
    return {
        "mode": "paper",
        "exchange": {"id": "kraken", "testnet": False},
        "market": {"symbols": ["BTC/USDT", "ETH/USDT"], "timeframe": "1h"},
        "indicators": {"fast_ema": 12, "slow_ema": 26, "rsi_period": 14},
        "risk": {
            "starting_cash_usd": 10000,
            "max_position_pct": 0.1,
            "max_total_exposure_pct": 0.4,
            "max_loss_per_trade_pct": 0.01,
            "max_daily_loss_pct": 0.05,
            "max_volatility_pct": 0.08,
            "min_risk_reward": 1.5,
            "cooldown_minutes": 60,
            "kill_switch_file": "KILL_SWITCH",
        },
        "logging": {
            "audit_log_path": "logs/audit_log.jsonl",
            "trade_log_path": "logs/trades.csv",
            "trade_plan_current_path": "logs/trade_plans_current.json",
            "trade_plan_history_path": "logs/trade_plans_history.jsonl",
        },
    }


def test_build_components_reconstructs_portfolio_from_persisted_state(tmp_path, monkeypatch):
    # Regression test: the paper broker's sell() used to KeyError on every
    # scheduled run for any already-OPEN position, because Portfolio was built
    # fresh (cash=starting_cash, no positions) instead of from what src/bot.py's
    # own trade log and trade-plan store already record. That silently stuck
    # every open position forever and locked its symbol out of new signals.
    monkeypatch.setattr("src.bot.PROJECT_ROOT", tmp_path)

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "trades.csv").write_text(
        "timestamp,mode,symbol,side,price,base_amount,quote_amount\n"
        "t1,paper,BTC/USDT,buy,50000,0.02,1000\n"
    )
    (logs_dir / "trade_plans_current.json").write_text(
        json.dumps(
            {
                "BTC/USDT": {
                    "symbol": "BTC/USDT",
                    "status": "OPEN",
                    "plan": {"stop_loss": 48000, "take_profit": 53000},
                    "claude_verdict": None,
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "filled_at": "2026-01-01T00:00:00+00:00",
                    "fill_price": 50000.0,
                    "fill_amount": 0.02,
                    "closed_at": None,
                    "close_reason": None,
                    "close_price": None,
                }
            }
        )
    )

    cfg = Config(raw=_minimal_raw_config())
    exchange, risk_engine, portfolio, broker, store, audit = build_components(cfg)

    assert portfolio.cash == pytest.approx(10000 - 1000)
    assert "BTC/USDT" in portfolio.positions
    assert portfolio.positions["BTC/USDT"].amount == pytest.approx(0.02)
    assert portfolio.positions["BTC/USDT"].entry_price == pytest.approx(50000.0)

    # This is exactly the call that used to KeyError on every scheduled run.
    fill = broker.sell("BTC/USDT", 53000.0)
    assert fill.base_amount == pytest.approx(0.02)
    assert "BTC/USDT" not in portfolio.positions


def test_build_components_ignores_pending_and_closed_records(tmp_path, monkeypatch):
    monkeypatch.setattr("src.bot.PROJECT_ROOT", tmp_path)

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "trade_plans_current.json").write_text(
        json.dumps(
            {
                "ETH/USDT": {
                    "symbol": "ETH/USDT",
                    "status": "PENDING",
                    "plan": {},
                    "claude_verdict": None,
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "filled_at": None,
                    "fill_price": None,
                    "fill_amount": None,
                    "closed_at": None,
                    "close_reason": None,
                    "close_price": None,
                }
            }
        )
    )

    cfg = Config(raw=_minimal_raw_config())
    _exchange, _risk_engine, portfolio, _broker, _store, _audit = build_components(cfg)

    assert portfolio.positions == {}
    assert portfolio.cash == pytest.approx(10000)


# --- global new-entry rate cap wiring (src/risk_engine.py) ---


def _plan_record(symbol: str, status: str, created_at: str) -> dict:
    return {
        "symbol": symbol,
        "status": status,
        "plan": {"stop_loss": 1.0, "take_profit": 3.0},
        "claude_verdict": None,
        "created_at": created_at,
        "filled_at": created_at if status == "OPEN" else None,
        "fill_price": 2.0 if status == "OPEN" else None,
        "fill_amount": 1.0 if status == "OPEN" else None,
        "closed_at": None,
        "close_reason": None,
        "close_price": None,
    }


def _cap_config(**risk_overrides) -> Config:
    raw = _minimal_raw_config()
    raw["market"]["symbols"] = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "ADA/USDT"]
    raw["risk"].update(risk_overrides)
    return Config(raw=raw)


def test_build_components_passes_new_entry_cap_config(tmp_path, monkeypatch):
    monkeypatch.setattr("src.bot.PROJECT_ROOT", tmp_path)
    _exchange, risk_engine, *_ = build_components(
        _cap_config(max_new_entries_per_window=5, new_entry_window_minutes=45)
    )
    assert risk_engine.max_new_entries_per_window == 5
    assert risk_engine.new_entry_window_minutes == 45


def test_build_components_uses_engine_defaults_when_cap_keys_absent(tmp_path, monkeypatch):
    monkeypatch.setattr("src.bot.PROJECT_ROOT", tmp_path)
    _exchange, risk_engine, *_ = build_components(_cap_config())
    assert risk_engine.max_new_entries_per_window == 5
    assert risk_engine.new_entry_window_minutes == 120


def test_build_components_refuses_nonsensical_cap_config(tmp_path, monkeypatch):
    monkeypatch.setattr("src.bot.PROJECT_ROOT", tmp_path)
    with pytest.raises(ValueError):
        build_components(_cap_config(max_new_entries_per_window=0))


def test_build_components_restores_new_entry_window_after_restart(tmp_path, monkeypatch):
    # Three plans accepted minutes before a restart must still occupy the global cap,
    # or a mid-burst restart would silently re-open every slot. Cap pinned to 3 here
    # (independent of RiskEngine's own default) so this test's intent doesn't shift
    # if that default is ever retuned.
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr("src.bot.PROJECT_ROOT", tmp_path)
    now = datetime.now(timezone.utc)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "trade_plans_current.json").write_text(
        json.dumps(
            {
                "BTC/USDT": _plan_record("BTC/USDT", "OPEN", (now - timedelta(minutes=30)).isoformat()),
                "ETH/USDT": _plan_record("ETH/USDT", "PENDING", (now - timedelta(minutes=20)).isoformat()),
                "SOL/USDT": _plan_record("SOL/USDT", "OPEN", (now - timedelta(minutes=10)).isoformat()),
            }
        )
    )
    _exchange, risk_engine, *_ = build_components(_cap_config(max_new_entries_per_window=3))
    decision = risk_engine.check_new_trade("ADA/USDT", "buy", portfolio_equity=10000, cash_available=10000, now=now)
    assert not decision.allowed
    assert "new-entry rate cap" in decision.reason
    # Selling an existing position is still never blocked.
    assert risk_engine.check_new_trade("BTC/USDT", "sell", 10000, 0, now=now).allowed


def test_build_components_old_active_plans_do_not_hold_cap_slots(tmp_path, monkeypatch):
    monkeypatch.setattr("src.bot.PROJECT_ROOT", tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    old = "2026-01-01T00:00:00+00:00"
    (logs_dir / "trade_plans_current.json").write_text(
        json.dumps({s: _plan_record(s, "OPEN", old) for s in ("BTC/USDT", "ETH/USDT", "SOL/USDT")})
    )
    _exchange, risk_engine, *_ = build_components(_cap_config())
    assert risk_engine.check_new_trade("ADA/USDT", "buy", portfolio_equity=10000, cash_available=10000).allowed


def test_deferred_fill_does_not_count_twice_toward_new_entry_cap(tmp_path, monkeypatch):
    # A PENDING plan was counted once when accepted; when the monitor later fills it,
    # src.bot._execute_monitor_action must not count it as a second new entry.
    from src.bot import _execute_monitor_action
    from src.monitor import MonitorAction

    monkeypatch.setattr("src.bot.PROJECT_ROOT", tmp_path)
    _exchange, risk_engine, _portfolio, broker, store, _audit = build_components(
        _cap_config(max_new_entries_per_window=2)
    )
    store.set_pending("BTC/USDT", {"approved_quote_amount": 100.0, "stop_loss": 1.0, "take_profit": 3.0})
    risk_engine.record_trade("BTC/USDT")  # what run_cycle does on acceptance -> 1 of 2 slots

    event: dict = {}
    _execute_monitor_action(
        MonitorAction(symbol="BTC/USDT", action="FILL", price=2.0, reason="price inside entry zone"),
        store.get("BTC/USDT"), broker, store, risk_engine, event,
    )
    assert store.get("BTC/USDT").status == "OPEN"
    # Still only 1 of 2 slots used, so one more distinct symbol is allowed.
    assert risk_engine.check_new_trade("ETH/USDT", "buy", portfolio_equity=10000, cash_available=10000).allowed
