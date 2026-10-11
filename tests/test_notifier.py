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


# --------------------------------------------------------------------------
# Token never reaches logs / stdout / stderr; Markdown alerts never lost.
# --------------------------------------------------------------------------
import logging
import re
from types import SimpleNamespace

import pytest
import requests

from src import bot, notifier
from src.notifier import escape_md, redact_secrets

TOKEN = "123456789:AAFakeTokenSecretValue_abc-DEF1234567"
URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
CFG = SimpleNamespace(telegram_bot_token=TOKEN, telegram_chat_id="42")
PATTERNS = ["BREAKOUT", "PULLBACK", "MOMENTUM", "TREND_CONTINUATION", "REVERSAL"]
SYMBOLS = ["BTC/USD", "BTC/USDT", "ETH/USD", "1000SHIB/USD", "A_B/USD", "X*Y/USD"]


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} Client Error: Bad Request for url: {URL}", response=self
            )


def _markdown_error(text: str):
    """Mimics Telegram's legacy-Markdown entity parser closely enough: unescaped
    _ * ` must pair up and an unescaped [ is an unterminated link."""
    stripped = re.sub(r"\\.", "", text)
    for ch in "_*`":
        if stripped.count(ch) % 2:
            return f"Bad Request: can't parse entities: Can't find end of the entity starting at byte offset {stripped.index(ch)}"
    if "[" in stripped:
        return "Bad Request: can't parse entities: Can't find end of the entity starting at byte offset 0"
    return None


class FakeTelegram:
    """Stands in for requests.post; rejects bad legacy Markdown with 400 like the real API."""

    def __init__(self):
        self.delivered = []   # (text, parse_mode)
        self.calls = 0

    def __call__(self, url, json=None, timeout=None):
        self.calls += 1
        assert url == URL
        if json.get("parse_mode") == "Markdown":
            err = _markdown_error(json["text"])
            if err:
                return FakeResponse(400, {"ok": False, "error_code": 400, "description": err})
        self.delivered.append((json["text"], json.get("parse_mode")))
        return FakeResponse(200, {"ok": True})


def _assert_no_token(caplog, capsys):
    out = capsys.readouterr()
    for blob in (caplog.text, out.out, out.err, *[r.getMessage() for r in caplog.records]):
        assert TOKEN not in blob
        assert TOKEN.split(":")[1] not in blob
    for r in caplog.records:
        assert r.exc_info is None and not r.exc_text


# ---- (1) token leak -------------------------------------------------------

@pytest.mark.parametrize(
    "make_exc",
    [
        lambda: requests.ConnectionError(f"HTTPSConnectionPool(host='api.telegram.org'): Max retries exceeded with url: /bot{TOKEN}/sendMessage"),
        lambda: requests.Timeout(f"Read timed out. (url: {URL})"),
        lambda: requests.HTTPError(f"500 Server Error for url: {URL}", response=FakeResponse(500, {"description": f"oops {URL}"})),
        lambda: requests.HTTPError(f"401 Client Error: Unauthorized for url: {URL}", response=FakeResponse(401, {"description": "Unauthorized"})),
        lambda: RuntimeError(URL),
    ],
)
def test_token_never_logged_on_failure(make_exc, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    with patch("src.notifier.requests.post", side_effect=make_exc()):
        send_telegram_message(TOKEN, "42", "hello")  # must not raise
    assert "Telegram notification failed" in caplog.text
    _assert_no_token(caplog, capsys)


def test_failure_log_has_class_status_and_description(caplog):
    caplog.set_level(logging.DEBUG)
    resp = FakeResponse(401, {"ok": False, "description": "Unauthorized"})
    with patch("src.notifier.requests.post", return_value=resp):
        send_telegram_message(TOKEN, "42", "hello")
    assert "HTTPError (HTTP 401): Unauthorized" in caplog.text


def test_description_is_redacted_and_truncated(caplog, capsys):
    caplog.set_level(logging.DEBUG)
    resp = FakeResponse(502, {"description": f"bad gateway {URL} " + "x" * 1000})
    with patch("src.notifier.requests.post", return_value=resp):
        send_telegram_message(TOKEN, "42", "hello")
    _assert_no_token(caplog, capsys)
    assert len(caplog.text) < 600


def test_non_json_error_body_is_handled(caplog, capsys):
    caplog.set_level(logging.DEBUG)
    with patch("src.notifier.requests.post", return_value=FakeResponse(503, None)):
        send_telegram_message(TOKEN, "42", "hello")
    assert "HTTP 503" in caplog.text
    _assert_no_token(caplog, capsys)


def test_redact_secrets_patterns():
    assert TOKEN not in redact_secrets(f"see {URL} and {TOKEN}")
    assert TOKEN not in redact_secrets(f"literal {TOKEN}", TOKEN)
    assert "short-token" not in redact_secrets("x short-token y", "short-token")
    assert redact_secrets("plain text") == "plain text"


def test_urllib3_debug_line_with_token_is_redacted(caplog, capsys):
    caplog.set_level(logging.DEBUG)
    logging.getLogger("urllib3.connectionpool").debug('%s "POST /bot%s/sendMessage HTTP/1.1" 200 %s', "https://api.telegram.org:443", TOKEN, 55)
    assert "sendMessage" in caplog.text and "200 55" in caplog.text
    _assert_no_token(caplog, capsys)


def test_bot_maybe_notify_failure_path_does_not_leak(caplog, capsys):
    caplog.set_level(logging.DEBUG)
    with patch("src.notifier.requests.post", side_effect=requests.ConnectionError(URL)):
        bot._maybe_notify(CFG, _plan_event("BTC/USD", "BREAKOUT"))
    _assert_no_token(caplog, capsys)


# ---- (2) Markdown ---------------------------------------------------------

def test_escape_md():
    assert escape_md("TREND_CONTINUATION") == "TREND\\_CONTINUATION"
    assert escape_md("a*b`c[d") == "a\\*b\\`c\\[d"
    assert escape_md("BTC/USD") == "BTC/USD"
    assert escape_md(None) == "None"
    assert escape_md(1.5) == "1.5"


def test_fake_telegram_rejects_unbalanced_markdown_like_the_real_api():
    # sanity check of the test double itself: the unescaped bug really is a 400
    fake = FakeTelegram()
    assert fake("%s" % URL, json={"text": "x (TREND_CONTINUATION)", "parse_mode": "Markdown"}).status_code == 400
    assert fake(URL, json={"text": "x (TREND\\_CONTINUATION)", "parse_mode": "Markdown"}).status_code == 200


def _plan_event(symbol, pattern):
    return {
        "symbol": symbol,
        "action": "planned",
        "plan": {
            "pattern": pattern,
            "entry_zone": (49500.0, 50000.0),
            "stop_loss": 48000.0,
            "take_profit": 53000.0,
            "risk_reward": 2.3,
            "rule_based_confidence_stars": 4.0,
        },
    }


@pytest.mark.parametrize("pattern", PATTERNS)
@pytest.mark.parametrize("symbol", SYMBOLS)
def test_buy_alert_delivered_as_markdown_for_every_pattern_and_symbol(pattern, symbol):
    fake = FakeTelegram()
    with patch("src.notifier.requests.post", fake):
        bot._maybe_notify(CFG, _plan_event(symbol, pattern))
    assert fake.calls == 1  # no failed attempt / retry needed: escaping works
    (text, mode), = fake.delivered
    assert mode == "Markdown"
    assert escape_md(symbol) in text and escape_md(pattern) in text


@pytest.mark.parametrize("action", ["STOP_LOSS", "TAKE_PROFIT", "FILL"])
def test_close_and_fill_alerts_delivered_with_underscore_symbol(action):
    fake = FakeTelegram()
    event = {"symbol": "A_B/USD", "action": "none", "monitor_action": {"action": action},
             "execution": {"price": 1.0, "base_amount": 0.01}}
    with patch("src.notifier.requests.post", fake):
        bot._maybe_notify(CFG, event)
    assert fake.calls == 1 and fake.delivered[0][1] == "Markdown"


def test_messages_unchanged_when_no_special_characters():
    # byte-identical to main's formatting for strings with nothing to escape
    sent = []
    with patch("src.bot.notifier.send_telegram_message", lambda t, c, text: sent.append(text)):
        bot._maybe_notify(CFG, _plan_event("BTC/USDT", "BREAKOUT"))
        bot._maybe_notify(CFG, {"symbol": "BTC/USDT", "action": "none", "monitor_action": {"action": "STOP_LOSS"},
                                "execution": {"price": 47000.0, "base_amount": 0.01}})
        bot._maybe_notify(CFG, {"symbol": "BTC/USDT", "action": "none", "monitor_action": {"action": "FILL"},
                                "execution": {"price": 50000.0, "base_amount": 0.01}})
    assert sent == [
        "🔔 *BUY signal*: BTC/USDT (BREAKOUT)\n"
        "Buy between $49,500.00 and $50,000.00\n"
        "Stop-loss: $48,000.00 (sell here to cap the loss)\n"
        "Target: $53,000.00 (take profit here)\n"
        "Risk:Reward 1:2.3  Confidence: 4.0/5",
        "🏁 *SELL* — trade closed: BTC/USDT\nReason: Stop-loss hit\nPrice: $47,000.00",
        "✅ *Bought*: BTC/USDT\nPrice: $50,000.00  Amount: 0.01",
    ]


def test_unescaped_markdown_400_is_retried_once_as_plain_text(caplog, capsys):
    caplog.set_level(logging.DEBUG)
    fake = FakeTelegram()
    with patch("src.notifier.requests.post", fake):
        send_telegram_message(TOKEN, "42", "BUY (TREND_CONTINUATION) \\_x")
    assert fake.calls == 2
    assert fake.delivered == [("BUY (TREND_CONTINUATION) _x", None)]  # parse_mode omitted
    _assert_no_token(caplog, capsys)


def test_plain_text_retry_payload_has_no_parse_mode():
    payloads = []

    def post(url, json=None, timeout=None):
        payloads.append(dict(json))
        return FakeResponse(400, {"description": "Bad Request: can't parse entities: x"}) if "parse_mode" in json else FakeResponse(200, {})

    with patch("src.notifier.requests.post", post):
        send_telegram_message(TOKEN, "42", "a_b")
    assert len(payloads) == 2
    assert payloads[0]["parse_mode"] == "Markdown" and "parse_mode" not in payloads[1]


def test_retry_happens_only_once_and_only_for_parse_errors(caplog, capsys):
    caplog.set_level(logging.DEBUG)
    calls = []

    def always_parse_400(url, json=None, timeout=None):
        calls.append(1)
        return FakeResponse(400, {"description": "Bad Request: can't parse entities: nope"})

    with patch("src.notifier.requests.post", always_parse_400):
        send_telegram_message(TOKEN, "42", "x")  # must not raise, must not loop
    assert len(calls) == 2

    calls.clear()

    def other_400(url, json=None, timeout=None):
        calls.append(1)
        return FakeResponse(400, {"description": "Bad Request: chat not found"})

    with patch("src.notifier.requests.post", other_400):
        send_telegram_message(TOKEN, "42", "x")
    assert len(calls) == 1  # a non-parse 400 is not retried
    _assert_no_token(caplog, capsys)
