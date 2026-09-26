"""Optional Telegram notifications for significant bot events.

Enabled by setting both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (see
.env.example) -- with either missing, send_telegram_message() is a no-op.
A notification failure (bad token, network hiccup, Telegram outage) is
logged and swallowed, never raised: a monitoring convenience must never
interrupt a trading cycle.
"""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"


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
