"""Optional Telegram notifications for significant bot events.

Enabled by setting both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (see
.env.example) -- with either missing, send_telegram_message() is a no-op.
A notification failure (bad token, network hiccup, Telegram outage) is
logged and swallowed, never raised: a monitoring convenience must never
interrupt a trading cycle.

SECURITY: requests' exception text embeds the full request URL, which for
Telegram is ``.../bot<TOKEN>/sendMessage``.  Nothing in this module ever logs
a raw exception string, URL, or traceback; failures are logged as the exception
class name plus (for HTTP errors) the status code and Telegram's ``description``
field, with any ``bot<id>:<secret>`` pattern and the configured token itself
redacted first.

Messages use Telegram's legacy ``Markdown`` parse mode, where an unpaired
``_ * ` [`` in a dynamic field (e.g. the ``TREND_CONTINUATION`` pattern name)
makes Telegram answer HTTP 400 "can't parse entities".  Callers should wrap
dynamic fields in escape_md(); as a safety net, a send rejected for that
reason is retried ONCE as plain text so an alert is never lost.
"""
from __future__ import annotations

import logging
import re

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"

_REDACTED = "<redacted>"
# A Telegram bot token is "<numeric bot id>:<35-char secret>"; in a URL it follows "bot".
_BOT_URL_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_BARE_TOKEN_RE = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}")
_MD_SPECIAL_RE = re.compile(r"([_*`\[])")
_MD_ESCAPED_RE = re.compile(r"\\([_*`\[])")
_MAX_DESCRIPTION_CHARS = 200


def escape_md(value: object) -> str:
    """Escapes the legacy-Markdown specials (_ * ` [) in a dynamic field so it
    renders literally.  Strings without those characters are returned unchanged."""
    return _MD_SPECIAL_RE.sub(r"\\\1", str(value))


def redact_secrets(text: str, bot_token: str | None = None) -> str:
    """Strips bot-token patterns (and the configured token value itself) from text."""
    text = _BOT_URL_TOKEN_RE.sub("bot" + _REDACTED, text)
    text = _BARE_TOKEN_RE.sub(_REDACTED, text)
    if bot_token:
        text = text.replace(bot_token, _REDACTED)
    return text


class _RedactTokenFilter(logging.Filter):
    """Defence in depth: urllib3 logs ``"POST /bot<TOKEN>/sendMessage ..."`` at
    DEBUG; if anyone ever raises the log level, the token must still not land in a file."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — never let a bad log call break logging
            return True
        redacted = redact_secrets(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


logging.getLogger("urllib3.connectionpool").addFilter(_RedactTokenFilter())


def _error_details(exc: BaseException, bot_token: str | None) -> tuple[object, str]:
    """(status code or None, redacted Telegram description or "") -- never the exception text."""
    response = getattr(exc, "response", None)
    if response is None:
        return None, ""
    status = getattr(response, "status_code", None)
    description = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            description = str(body.get("description") or "")
    except Exception:  # noqa: BLE001 — body may be empty / not JSON
        pass
    description = redact_secrets(description, bot_token)[:_MAX_DESCRIPTION_CHARS]
    return status, description


def _is_parse_error(exc: BaseException, bot_token: str | None) -> bool:
    status, description = _error_details(exc, bot_token)
    return status == 400 and "can't parse entities" in description.lower()


def _post(bot_token: str, payload: dict) -> None:
    response = requests.post(
        f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage",
        json=payload,
        timeout=10,
    )
    response.raise_for_status()


def send_telegram_message(bot_token: str | None, chat_id: str | None, text: str) -> None:
    if not bot_token or not chat_id:
        return
    try:
        try:
            _post(bot_token, {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
        except Exception as exc:  # noqa: BLE001
            if not _is_parse_error(exc, bot_token):
                raise
            logger.warning("Telegram rejected Markdown (can't parse entities) -- retrying once as plain text")
            # Plain text: drop parse_mode, and un-escape our own backslash escapes so
            # they don't show up as literal backslashes.
            _post(bot_token, {"chat_id": chat_id, "text": _MD_ESCAPED_RE.sub(r"\1", text)})
    except Exception as exc:  # noqa: BLE001 — a notification failure must never break a trading cycle
        # Deliberately NOT logging str(exc) / exc_info: requests' exception text contains the
        # request URL, and with it the bot token.  Class name + status + description only.
        status, description = _error_details(exc, bot_token)
        logger.warning(
            "Telegram notification failed: %s%s%s",
            type(exc).__name__,
            f" (HTTP {status})" if status is not None else "",
            f": {description}" if description else "",
        )
