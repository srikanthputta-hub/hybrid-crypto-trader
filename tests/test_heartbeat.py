from unittest.mock import MagicMock, patch

from src.heartbeat import send_heartbeat_ping


def test_send_heartbeat_ping_noop_when_url_missing():
    with patch("src.heartbeat.requests.get") as mock_get:
        send_heartbeat_ping(None)
        send_heartbeat_ping("")
    mock_get.assert_not_called()


def test_send_heartbeat_ping_calls_url():
    with patch("src.heartbeat.requests.get") as mock_get:
        mock_get.return_value = MagicMock(raise_for_status=MagicMock())
        send_heartbeat_ping("https://hc-ping.com/some-uuid")
    mock_get.assert_called_once_with("https://hc-ping.com/some-uuid", timeout=10)


def test_send_heartbeat_ping_swallows_errors():
    with patch("src.heartbeat.requests.get", side_effect=Exception("network error")):
        send_heartbeat_ping("https://hc-ping.com/some-uuid")  # must not raise
