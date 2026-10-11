"""Optional Telegram notifications for significant bot events.

Enabled by setting both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (see
.env.example) -- with either missing, send_telegram_message() is a no-op.
A notification failure (bad token, network hiccup, Telegram outage) is
logged and swallowed, never raised: a monitoring convenience must never
interrupt a trading cycle.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"


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


def send_telegram_message(bot_token: str | None, chat_id: str | None, text: str) -> None:
    if not bot_token or not chat_id:
        return
    try:
        response = requests.post(
            f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — a notification failure must never break a trading cycle
        logger.warning("Telegram notification failed: %s", exc)
