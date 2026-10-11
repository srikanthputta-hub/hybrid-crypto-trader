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

SECURITY: the ping URL embeds the check's UUID/ping key, which works as a
credential, and requests' exception text includes the full URL.  So a failure
is logged as the exception class name (+ HTTP status code if there is one)
only -- never str(exc), the URL, or a traceback.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import unquote, urlsplit

import requests

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_MIN_SEGMENT = 6  # path segments shorter than this ("start", "fail", "1") are not secrets


def _redact(text: str, ping_url: str) -> str:
    """Removes the ping URL, its path segments / query, and any UUID from text."""
    parts = urlsplit(ping_url)
    literals = {ping_url, unquote(ping_url), parts.query}
    literals.update(seg for seg in parts.path.split("/") if len(seg) >= _MIN_SEGMENT)
    for literal in sorted((x for x in literals if x), key=len, reverse=True):
        text = text.replace(literal, "<redacted>")
    return _UUID_RE.sub("<redacted>", text)


def send_heartbeat_ping(ping_url: str | None) -> None:
    if not ping_url:
        return
    try:
        response = requests.get(ping_url, timeout=10)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — a monitoring ping failure must never break the trading loop
        # Deliberately NOT logging str(exc) / exc_info -- see the module docstring.
        try:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            status_text = f" (HTTP {int(status)})" if status is not None else ""
        except Exception:  # noqa: BLE001 — a pathological exc.response must never escape
            status_text = ""
        logger.warning("%s", _redact(f"Heartbeat ping failed: {type(exc).__name__}{status_text}", ping_url))
