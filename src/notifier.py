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
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"

_REDACTED = "<redacted>"
# A Telegram bot token is "<numeric bot id>:<35-char secret>"; in a URL it follows "bot".
_BOT_URL_TOKEN_RE = re.compile(r"bot\d+(?::|%3[Aa])[A-Za-z0-9_-]+")
_BARE_TOKEN_RE = re.compile(r"\b\d{5,}(?::|%3[Aa])[A-Za-z0-9_-]{20,}")
_MIN_SECRET_LITERAL = 6  # don't blank-out tiny literals (test tokens) -- real secrets are ~35 chars
_MD_SPECIAL_RE = re.compile(r"([_*`\[])")
_MD_ESCAPED_RE = re.compile(r"\\([_*`\[])")
_MAX_DESCRIPTION_CHARS = 200


def escape_md(value: object) -> str:
    """Escapes the legacy-Markdown specials (_ * ` [) in a dynamic field so it
    renders literally.  Strings without those characters are returned unchanged."""
    return _MD_SPECIAL_RE.sub(r"\\\1", str(value))


def redact_secrets(text: str, bot_token: str | None = None) -> str:
    """Strips bot-token patterns (and the configured token value itself, its
    secret half, and their URL-encoded ``%3A`` forms) from text."""
    text = _BOT_URL_TOKEN_RE.sub("bot" + _REDACTED, text)
    text = _BARE_TOKEN_RE.sub(_REDACTED, text)
    if bot_token:
        literals = {bot_token, bot_token.replace(":", "%3A"), bot_token.replace(":", "%3a")}
        if ":" in bot_token:
            literals.add(bot_token.split(":", 1)[1])
        for literal in sorted(literals, key=len, reverse=True):
            if len(literal) >= _MIN_SECRET_LITERAL or literal == bot_token:
                text = text.replace(literal, _REDACTED)
    return text


class _RedactTokenFilter(logging.Filter):
    """Defence in depth: urllib3 logs ``"POST /bot<TOKEN>/sendMessage ..."`` at
    DEBUG; if anyone ever raises the log level, the token must still not land in a file.
    Covers only records emitted through the ``urllib3.connectionpool`` logger -- NOT
    ``http.client``'s ``debuglevel`` output, which uses print() straight to stdout."""

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
    try:
        status, description = _error_details(exc, bot_token)
    except Exception:  # noqa: BLE001 — a pathological exc.response must not escape
        return False
    return status == 400 and "can't parse entities" in description.lower()


def _post(bot_token: str, payload: dict, timeout: float = 10) -> None:
    response = requests.post(
        f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage",
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()


def label_prefix(label: str | None) -> str:
    """Prefix for a message when `notifier.label` is set (e.g. "PAPER TRIAL"); "" when it is not,
    so the default message text is byte-identical to what it was before labels existed. The label
    is validated at startup (src/config.py parse_notifier_settings): no Markdown specials."""
    return f"*{label}* · " if label else ""


def bar_validity_text(bar_close: str | None, max_entry_delay_minutes: float | None) -> str:
    """The "Bar:" / "Valid until:" lines appended to a daily-bar BUY message (design §3.1/§6).

    `bar_close` is the audit event's `bar_close` (ISO close time of the daily bar the signal was
    decided on). The signal is acted on until the staleness guard would skip it
    (`loop.max_entry_delay_minutes` after that close) or, when the guard is off, until the next
    daily close. Returns "" (nothing appended) when there is no usable bar_close. Plain text only:
    safe inside Markdown parse mode."""
    if not bar_close:
        return ""
    try:
        close = datetime.fromisoformat(str(bar_close))
        if close.tzinfo is None:
            close = close.replace(tzinfo=timezone.utc)
        close = close.astimezone(timezone.utc)
        until = close + (timedelta(minutes=max_entry_delay_minutes) if max_entry_delay_minutes else timedelta(days=1))
    except (ValueError, TypeError, OverflowError):
        return ""
    bar_date = (close - timedelta(days=1)).date().isoformat()
    return (
        f"\nBar: {bar_date} (daily, closed {close:%Y-%m-%d %H:%M} UTC)"
        f"\nValid until: {until:%Y-%m-%d %H:%M} UTC"
    )


def send_telegram_message(bot_token: str | None, chat_id: str | None, text: str, timeout: float = 10) -> bool:
    """True once Telegram accepted the message; False when not configured or the send failed
    (never raises). Existing callers ignore the result; the live broker's alert path uses it
    so a failed send is retried instead of being marked delivered (issue #99 review F10)."""
    if not bot_token or not chat_id:
        return False
    try:
        try:
            _post(bot_token, {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout)
        except Exception as exc:  # noqa: BLE001
            if not _is_parse_error(exc, bot_token):
                raise
            logger.warning("Telegram rejected Markdown (can't parse entities) -- retrying once as plain text")
            # Plain text: drop parse_mode, and un-escape our own backslash escapes so
            # they don't show up as literal backslashes.
            _post(bot_token, {"chat_id": chat_id, "text": _MD_ESCAPED_RE.sub(r"\1", text)}, timeout)
        return True
    except Exception as exc:  # noqa: BLE001 — a notification failure must never break a trading cycle
        # Deliberately NOT logging str(exc) / exc_info: requests' exception text contains the
        # request URL, and with it the bot token.  Class name + status + description only.
        try:
            status, description = _error_details(exc, bot_token)
        except Exception:  # noqa: BLE001 — a pathological exc.response must never escape the notifier
            status, description = None, ""
        logger.warning(
            "Telegram notification failed: %s%s%s",
            type(exc).__name__,
            f" (HTTP {status})" if status is not None else "",
            f": {description}" if description else "",
        )
        return False
