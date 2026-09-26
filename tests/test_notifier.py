from unittest.mock import patch

from src.notifier import send_telegram_message


def test_noop_when_bot_token_missing():
    with patch("src.notifier.requests.post") as mock_post:
        send_telegram_message(None, "12345", "hello")
    mock_post.assert_not_called()


def test_noop_when_chat_id_missing():
    with patch("src.notifier.requests.post") as mock_post:
        send_telegram_message("token", None, "hello")
    mock_post.assert_not_called()


def test_sends_expected_payload_when_configured():
    with patch("src.notifier.requests.post") as mock_post:
        send_telegram_message("token", "12345", "hello")
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == "https://api.telegram.org/bottoken/sendMessage"
    assert kwargs["json"] == {"chat_id": "12345", "text": "hello", "parse_mode": "Markdown"}


def test_swallows_request_exceptions():
    with patch("src.notifier.requests.post", side_effect=RuntimeError("network down")):
        send_telegram_message("token", "12345", "hello")  # must not raise


def test_swallows_non_2xx_responses():
    with patch("src.notifier.requests.post") as mock_post:
        mock_post.return_value.raise_for_status.side_effect = RuntimeError("400 Bad Request")
        send_telegram_message("token", "12345", "hello")  # must not raise
