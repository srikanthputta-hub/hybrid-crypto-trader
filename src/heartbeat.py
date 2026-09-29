"""Optional dead-man's-switch heartbeat for scripts/run_live.py.

Enabled by setting HEALTHCHECK_PING_URL (see .env.example) -- a ping URL
from a service like healthchecks.io. With it unset, send_heartbeat_ping()
is a no-op. Pinged once per loop iteration regardless of whether that
cycle's trading logic or dashboard push succeeded: this confirms the
*process* is still alive and looping, not that a cycle traded anything --
that's what the configured monitoring service's own missed-ping alert is
for (VM crash, power loss, process death), something the bot's own
Telegram alerts can never report on, since they'd be as dead as the bot.

A ping failure (bad URL, network hiccup, monitoring service outage) is
logged and swallowed, never raised: a monitoring convenience must never
interrupt the trading loop.
"""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)


def send_heartbeat_ping(ping_url: str | None) -> None:
    if not ping_url:
        return
    try:
        response = requests.get(ping_url, timeout=10)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — a monitoring ping failure must never break the trading loop
        logger.warning("Heartbeat ping failed: %s", exc)
