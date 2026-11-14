"""The live-broker contract src/bot.py duck-types (issue #99 review S1).

bot.py never imports src/broker_live.py (paper mode must not load it -- tests/test_paper_isolation.py),
so it reaches the live broker's extra behaviour only through getattr. This neutral module (no
imports from either broker) writes that contract down in one place:

* the paper broker has NONE of these attributes, so every call site in bot.py is a no-op for it;
* src.broker_live.LiveBroker has ALL of them (tests/test_live_review_fixes.py checks both);
* `halted` / `TRACKS_ORDER_REFS` count only when they are a real `True` (bot._flag), so a stand-in
  that answers every attribute (a MagicMock) can never silently block planning.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LiveBrokerContract(Protocol):
    TRACKS_ORDER_REFS: bool

    @property
    def halted(self) -> bool: ...

    def reconcile(self) -> list[Any]: ...

    def settle(self, symbol: str) -> None: ...

    def settle_order(self, client_order_id: str) -> bool: ...

    def settle_unverified(self, client_order_id: str) -> bool: ...

    def report_unmatched(self, client_order_id: str, reason: str) -> None: ...

    def report_store_failure(self, symbol: str, detail: str) -> None: ...

    def order_in_flight(self, symbol: str) -> bool: ...

    def pop_alerts(self) -> list[dict]: ...

    def requeue_alerts(self, alerts: list[dict]) -> None: ...


LIVE_BROKER_ATTRIBUTES = (
    "TRACKS_ORDER_REFS", "halted", "reconcile", "settle", "settle_order", "settle_unverified", "report_unmatched",
    "report_store_failure", "order_in_flight", "pop_alerts", "requeue_alerts",
)
