from types import SimpleNamespace
from unittest.mock import patch

from src.bot import _maybe_notify, _send_heartbeat
from src.portfolio import Portfolio

CONFIGURED = SimpleNamespace(telegram_bot_token="token", telegram_chat_id="12345")
UNCONFIGURED = SimpleNamespace(telegram_bot_token=None, telegram_chat_id=None)

HEARTBEAT_CONFIGURED = SimpleNamespace(
    telegram_bot_token="token", telegram_chat_id="12345", mode="paper", risk={"starting_cash_usd": 10000}
)
HEARTBEAT_UNCONFIGURED = SimpleNamespace(
    telegram_bot_token=None, telegram_chat_id=None, mode="paper", risk={"starting_cash_usd": 10000}
)


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
    assert "New signal" in text
    assert "BTC/USDT" in text
    assert "BREAKOUT" in text


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
    assert "Filled" in text
    assert "50000.0" in text


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
    assert "Closed" in text
    assert "STOP_LOSS" in text


def test_discard_sends_nothing():
    event = {"symbol": "BTC/USDT", "action": "none", "monitor_action": {"action": "DISCARD"}}
    with patch("src.bot.notifier.send_telegram_message") as mock_send:
        _maybe_notify(CONFIGURED, event)
    mock_send.assert_not_called()


def test_heartbeat_noop_when_telegram_not_configured():
    portfolio = Portfolio(cash=10000)
    with patch("src.bot.notifier.send_telegram_message") as mock_send:
        _send_heartbeat(HEARTBEAT_UNCONFIGURED, {"BTC/USDT": 50000.0}, portfolio, 10000.0)
    mock_send.assert_not_called()


def test_heartbeat_noop_when_no_prices():
    portfolio = Portfolio(cash=10000)
    with patch("src.bot.notifier.send_telegram_message") as mock_send:
        _send_heartbeat(HEARTBEAT_CONFIGURED, {}, portfolio, 10000.0)
    mock_send.assert_not_called()


def test_heartbeat_sends_prices_and_equity():
    portfolio = Portfolio(cash=9000)
    prices = {"BTC/USDT": 50000.0, "ETH/USDT": 3000.0}
    with patch("src.bot.notifier.send_telegram_message") as mock_send:
        _send_heartbeat(HEARTBEAT_CONFIGURED, prices, portfolio, 10500.0)
    mock_send.assert_called_once()
    text = mock_send.call_args[0][2]
    assert "Heartbeat" in text
    assert "BTC/USDT" in text and "50,000.00" in text
    assert "ETH/USDT" in text and "3,000.00" in text
    assert "9,000.00" in text  # cash
    assert "+5.00%" in text  # pnl_pct vs starting_cash_usd=10000
