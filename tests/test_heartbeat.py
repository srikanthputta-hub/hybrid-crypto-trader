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


# ---- ping URL (UUID == credential) must never reach the logs ---------------
import logging

import pytest
import requests

UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"
PING_URL = f"https://hc-ping.com/{UUID}"


def _assert_clean(caplog, capsys):
    out = capsys.readouterr()
    for blob in (caplog.text, out.out, out.err):
        assert UUID not in blob
        assert UUID[-12:] not in blob and UUID[:8] not in blob
        assert PING_URL not in blob
    for r in caplog.records:
        assert r.exc_info is None and not r.exc_text


class _Resp:
    status_code = 503

    def raise_for_status(self):
        raise requests.HTTPError(f"503 Server Error: Service Unavailable for url: {PING_URL}", response=self)


@pytest.mark.parametrize(
    "exc",
    [
        requests.ConnectionError(f"HTTPSConnectionPool(host='hc-ping.com'): Max retries exceeded with url: /{UUID}"),
        requests.Timeout(f"Read timed out. (url: {PING_URL})"),
        RuntimeError(PING_URL),
    ],
)
def test_ping_failure_does_not_log_url_or_uuid(exc, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    with patch("src.heartbeat.requests.get", side_effect=exc):
        send_heartbeat_ping(PING_URL)  # must not raise
    assert f"Heartbeat ping failed: {type(exc).__name__}" in caplog.text
    _assert_clean(caplog, capsys)


def test_ping_http_error_logs_class_and_status_only(caplog, capsys):
    caplog.set_level(logging.DEBUG)
    with patch("src.heartbeat.requests.get", return_value=_Resp()):
        send_heartbeat_ping(PING_URL)
    assert "Heartbeat ping failed: HTTPError (HTTP 503)" in caplog.text
    _assert_clean(caplog, capsys)


def test_ping_failure_with_ping_key_slug_url_is_redacted(caplog, capsys):
    url = "https://hc-ping.com/abcdefSECRETKEY123/my-job-slug"
    caplog.set_level(logging.DEBUG)
    with patch("src.heartbeat.requests.get", side_effect=requests.ConnectionError(url)):
        send_heartbeat_ping(url)
    assert "SECRETKEY123" not in caplog.text and "my-job-slug" not in caplog.text


def test_redact_helper_strips_url_segments_and_bare_uuid():
    from src.heartbeat import _redact

    out = _redact(f"oops {PING_URL} and {UUID} and /{UUID}/start", PING_URL)
    assert UUID not in out and UUID[:8] not in out


def test_ping_pathological_response_never_escapes():
    class Weird(requests.HTTPError):
        @property
        def response(self):
            raise RuntimeError("boom")

        @response.setter
        def response(self, v):
            pass

    with patch("src.heartbeat.requests.get", side_effect=Weird(PING_URL)):
        send_heartbeat_ping(PING_URL)  # must not raise


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
def test_ping_keyboard_interrupt_propagates(exc_type):
    with patch("src.heartbeat.requests.get", side_effect=exc_type()):
        with pytest.raises(exc_type):
            send_heartbeat_ping(PING_URL)


def test_redact_helper_strips_ping_key_slug_segments_and_query():
    from src.heartbeat import _redact

    url = "https://hc-ping.com/abcdefSECRETKEY123/my-job-slug?create=1&rid=zzzzzz"
    out = _redact(f"x abcdefSECRETKEY123 y my-job-slug z {url}", url)
    assert "SECRETKEY123" not in out and "my-job-slug" not in out and "create=1" not in out


def test_redact_helper_strips_whole_url_and_foreign_uuids():
    from src.heartbeat import _redact

    other = "11111111-2222-3333-4444-555555555555"
    out = _redact(f"see {PING_URL} and {other}", PING_URL)
    assert "hc-ping.com" not in out  # whole URL replaced, not just the secret segment
    assert other not in out  # a UUID that is not the configured one is still scrubbed
