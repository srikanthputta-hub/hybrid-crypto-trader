"""Live broker: places REAL orders on a real exchange account. Guarded by two independent switches.

To actually execute an order this requires BOTH:
  1. config.yaml -> mode: live
  2. environment variable LIVE_TRADING_CONFIRM=YES-I-UNDERSTAND-THE-RISK

Missing either one raises LiveTradingDisabled instead of silently no-op'ing, so a
misconfiguration is loud rather than a bot that appears to run but never trades
(or worse, the reverse).

Order/ledger hardening (every guard below only ever REFUSES or HALTS; none of them
can make an order happen that the pre-hardening code wouldn't have placed):

* Pre-order: buy() refuses (ValueError, NO order placed) unless quote_amount and
  price_hint are finite numbers > 0 (bool / numpy.bool_ rejected, same rule as
  src.portfolio._is_finite_number) AND the ledger update for the requested fill
  would itself be finite. sell() refuses unless the held amount and price_hint are
  finite > 0. A NaN approved_quote_amount read back from the persisted plan JSON
  therefore never reaches the exchange.
* Post-order: an exchange fill field is used only if it is absent/None (falls back
  exactly as before: average -> price -> price_hint, filled -> requested amount),
  or 0 for a price field (same fallback as before), or a finite number > 0 (a
  filled amount must also not exceed what was requested). Anything else (NaN, inf,
  negative, bool, string, filled=0, an over-fill, a non-dict response) is UNUSABLE.
  The ledger is computed in full, validated finite, and only then mutated.
* Unusable response after an order was placed (the order may be live on the
  exchange -- NOT booking it is as dangerous as booking garbage):
    - buy: book the REQUESTED fill (requested base amount at price_hint, i.e.
      exactly the risk-approved quote_amount; validated finite before the order
      was sent) and return normally, so bot.py marks the plan OPEN and the
      monitor's stop-loss protects the position. Then HALT new buys (below).
      Over-stating the position/under-stating cash is the conservative direction.
    - sell: book NOTHING (the position stays tracked, so the plan stays OPEN and the
      monitor retries the exit next cycle; on spot, re-selling an already-sold
      amount is rejected by the exchange), HALT new buys, raise
      LiveOrderUnreconciled through bot.py's existing per-symbol error path.
* create_market_order raising: a specific ccxt.ExchangeError subclass
  (InsufficientFunds, InvalidOrder, BadSymbol, AuthenticationError, ...) is a definite
  rejection -> re-raised unchanged, nothing booked, no halt (unchanged behaviour).
  Anything else -- a BARE ccxt.ExchangeError (ccxt's catch-all for unmapped/internal
  exchange errors), ExchangeClosedByUser, DuplicateOrderId, ccxt.NetworkError/
  RequestTimeout/BadResponse, any non-ccxt error -- leaves it UNKNOWN whether the
  order was placed -> HALT new buys and raise LiveOrderUnreconciled; nothing booked.
* Partial sell (exchange reports remaining > 0, or filled < the amount it echoes as
  ordered): book exactly what filled, keep the remainder as the open position, HALT
  new buys, raise LivePartialFill so bot.py does NOT mark the plan CLOSED -- the
  monitor keeps managing the remainder.
* HALT = every later buy() raises LiveBrokerHalted before touching the exchange
  (sells -- risk-reducing -- stay allowed). Without it, bot.py would retry a PENDING
  plan's FILL next cycle and place a DUPLICATE real order. The halt is held in
  memory and, when the portfolio has a trade_log_path, persisted as
  HALT_MARKER_NAME next to the trade log so it survives a restart. To resume: check
  the exchange, reconcile logs/trades.csv and the plan store by hand, delete the
  marker file, restart the bot.

Write-ahead order record (issue #99 item 1, docs/live_readiness_design.md):

* Every order gets a deterministic clientOrderId (client_order_id(plan_ref, side, attempt))
  and an entry in ORDER_STATE_NAME (atomic + fsync'd) BEFORE create_market_order:
  intent -> placed -> booked -> cleared (settle(), called by bot.py AFTER the plan store
  moved). A sell whose response is unusable becomes `unusable` (position kept, see above).
  If the intent cannot be persisted, NO order is placed (LiveOrderStateError) -- fail closed.
* An intent/placed/unusable entry halts new buys (also after a restart: the entry is on
  disk even if the halt marker could not be written) and blocks every order on its symbol
  (a re-sell could double-sell). A booked entry left over from before a restart blocks
  every order on its symbol until it is settled. An unreadable state file blocks EVERY
  order and is never overwritten.

Review round (docs/live_readiness_design.md §10):

* trades.csv FIRST, memory second: a fill is written to trades.csv (fsync'd) before the
  in-memory ledger adopts it, and the order record + reconcile are the only path that books
  a fill (deduplicated on the exchange order id or `coid:<clientOrderId>`), so one real
  order can never be booked twice.
* An ack-only / still-open / unusable response is verified with an exchange lookup before
  booking; a terminal zero-fill order is NOT executed; a fill that could not be verified is
  booked conservatively, marked `unverified`, halted, alerted and re-checked every cycle.
* One live process per logs/ directory (exclusive flock on LOCK_NAME, taken AFTER the gate).
"""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ccxt

from src.exchange import ExchangeClient
from src.portfolio import Portfolio, Position, _is_finite_number, now_iso

logger = logging.getLogger(__name__)

HALT_MARKER_NAME = "live_broker_halt.json"
ORDER_STATE_NAME = "live_order_state.json"
LOCK_NAME = ".live_broker.lock"
# An alert for a condition that persists (halt, unresolved order, jammed exit) is re-sent at
# most this often; a failed Telegram send is retried every cycle until delivered.
ALERT_REMINDER_SECONDS = 1800

_CLIENT_ID_PREFIX = "hct"
# Reconcile (issue #99 item 2): an order the exchange does not list is only trusted to be
# "never placed" once its record is this old (exchange visibility lag), and order listings
# start this long before the record was written (clock skew).
RECONCILE_NOT_FOUND_GRACE_SECONDS = 120
RECONCILE_LOOKBACK_SECONDS = 600
# Sell balance cap (item 3): a free balance below the tracked amount by at most this
# fraction is fees/rounding (full close); a bigger shortfall is a mismatch (partial + halt).
_SELL_SHORTFALL_TOLERANCE = 0.005
_ENTRY_STATES = ("intent", "placed", "unusable", "booked")
# Entries whose outcome is not (fully) booked: they halt new buys.
_UNRESOLVED_STATES = ("intent", "placed", "unusable")
# Entries whose outcome is not booked block EVERY order on their symbol (a re-sell could double-sell).
_SYMBOL_BLOCKING_STATES = ("intent", "placed", "unusable")
_TERMINAL_STATUSES = ("closed", "canceled", "expired", "rejected")


def client_order_id(plan_ref: str, side: str, attempt: int) -> str:
    """Deterministic clientOrderId: a pure function of (plan, side, attempt), so a retry
    after a definite rejection / a never-placed order re-sends the SAME id and a reconcile
    can look it up. 18 chars of [a-z0-9]: fits Kraken's free-text cl_ord_id (<= 18) and
    Binance's newClientOrderId (<= 36)."""
    digest = hashlib.sha256(f"{plan_ref}|{side}|{int(attempt)}".encode()).hexdigest()
    return _CLIENT_ID_PREFIX + digest[:15]


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover - platforms without directory fds
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)


def _atomic_write_json(path: Path, payload: object) -> None:
    """tmp file in the same directory + flush + fsync + os.replace + directory fsync: a
    reader (or a restart after SIGKILL) sees either the old or the new content, never a
    torn file. Raises on any failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(payload, indent=2, sort_keys=True, default=repr) + "\n"
    with open(tmp, "w") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _empty_order_state() -> dict:
    return {"version": 1, "inflight": {}, "attempts": {}, "unverified": {}}


def _validate_order_state(raw: object) -> dict:
    """The parsed state file, or ValueError for anything that isn't exactly what this
    module writes (fail closed: an entry we can't read could be a live order)."""
    if not isinstance(raw, dict):
        raise ValueError("not a JSON object")
    inflight, attempts = raw.get("inflight"), raw.get("attempts")
    if not isinstance(inflight, dict) or not isinstance(attempts, dict):
        raise ValueError("missing 'inflight'/'attempts' objects")
    for coid, entry in inflight.items():
        if not (
            isinstance(entry, dict)
            and entry.get("client_order_id") == coid
            and entry.get("state") in _ENTRY_STATES
            and isinstance(entry.get("symbol"), str)
            and entry.get("side") in ("buy", "sell")
            and isinstance(entry.get("plan_ref"), str)
        ):
            raise ValueError(f"malformed in-flight entry {coid!r}")
    for key, value in attempts.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"malformed attempt counter {key!r}={value!r}")
    unverified = raw.get("unverified", {})
    if not isinstance(unverified, dict) or not all(isinstance(v, dict) for v in unverified.values()):
        raise ValueError("malformed 'unverified' object")
    return {"version": 1, "inflight": inflight, "attempts": attempts, "unverified": unverified}


# One live process per logs/ directory. Re-entrant within a process (restart simulations in
# tests construct a new broker while the old object still exists); a second PROCESS is refused.
_HELD_LOCKS: dict[str, list[int]] = {}  # resolved path -> [fd, number of live brokers holding it]


def _release_lock(key: str) -> None:
    held = _HELD_LOCKS.get(key)
    if held is None:
        return
    held[1] -= 1
    if held[1] <= 0:
        del _HELD_LOCKS[key]
        try:
            os.close(held[0])  # closing the descriptor releases the flock
        except OSError:  # pragma: no cover
            pass


def _acquire_lock(path: Path, owner: object) -> None:
    import weakref

    key = str(path.resolve())
    if key in _HELD_LOCKS:
        _HELD_LOCKS[key][1] += 1
        weakref.finalize(owner, _release_lock, key)
        return
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise LiveBrokerLocked(
            f"another live bot process holds {path}; refusing to start a second one on the same logs/ "
            "directory (two writers would corrupt the order record)"
        ) from exc
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    _HELD_LOCKS[key] = [fd, 1]
    weakref.finalize(owner, _release_lock, key)

# A reported filled amount may exceed the amount we asked for by at most this
# relative float-noise margin; anything larger is a units/parse error, not a fill.
_OVERFILL_TOLERANCE = 1e-9


class LiveTradingDisabled(RuntimeError):
    pass


class LiveBrokerHalted(RuntimeError):
    """buy() refused: an earlier live order could not be reconciled. No order was placed."""


class LiveOrderUnreconciled(RuntimeError):
    """An order may be live on the exchange but its result could not be booked. New buys are halted."""


class LiveOrderStateError(RuntimeError):
    """The write-ahead order record could not be persisted. NO order was placed; new buys are halted."""


class LiveOrderNotExecuted(RuntimeError):
    """The exchange reports the order terminal with nothing filled: nothing was executed or booked."""


class LiveExitBlocked(RuntimeError):
    """A sell was not sent because the exchange would refuse it (below its minimum order size)."""


class LiveBrokerLocked(RuntimeError):
    """Another live process already owns this logs/ directory."""


class LivePartialFill(RuntimeError):
    """A sell only partially filled; the filled part is booked, the remainder stays open.

    `fill` holds the FillResult for the part that did fill; `remaining_amount` is the base
    amount still held (bot.py persists it to the plan store -- duck-typed, so bot.py never
    has to import this module)."""

    def __init__(self, message: str, fill: "FillResult", remaining_amount: float | None = None) -> None:
        super().__init__(message)
        self.fill = fill
        self.remaining_amount = remaining_amount


class _UnusableFill(Exception):
    """Internal: the exchange's order response can't be trusted for booking."""


def _price_field(order: dict, key: str) -> float | None:
    """None if not reported (None, or 0 -- the pre-hardening `or` chain also skipped a
    0 price, which some exchanges report for market orders); else must be finite > 0."""
    value = order.get(key)
    if value is None or (_is_finite_number(value) and value == 0):
        return None
    if not (_is_finite_number(value) and value > 0):
        raise _UnusableFill(f"{key}={value!r} is not a finite number > 0")
    return float(value)


def _parse_fill(order: object, requested_amount: float, price_hint: float) -> tuple[float, float, float | None]:
    """(filled_price, filled_amount, remaining) from a ccxt order response, or raises
    _UnusableFill. Missing fields fall back exactly as before the hardening."""
    if not isinstance(order, dict):
        raise _UnusableFill(f"order response is {type(order).__name__}, not a dict")
    price = _price_field(order, "average")
    if price is None:
        price = _price_field(order, "price")
    if price is None:
        price = float(price_hint)
    filled = order.get("filled")
    if filled is None:
        amount = float(requested_amount)
    elif _is_finite_number(filled) and 0 < filled <= requested_amount * (1 + _OVERFILL_TOLERANCE):
        amount = float(filled)
    else:
        raise _UnusableFill(
            f"filled={filled!r} is not a finite number in (0, requested {requested_amount!r}]"
        )
    remaining = order.get("remaining")
    if remaining is not None:
        if not (_is_finite_number(remaining) and remaining >= 0):
            raise _UnusableFill(f"remaining={remaining!r} is not a finite number >= 0")
        remaining = float(remaining)
    return price, amount, remaining


# ExchangeError subclasses that do NOT prove the order was rejected: ExchangeClosedByUser
# (client torn down mid-request) and DuplicateOrderId (an order with this id already exists,
# i.e. one may have been placed). A BARE ccxt.ExchangeError is ccxt's catch-all for unmapped /
# internal exchange errors (e.g. an exchange-side internal_server_error) -- also ambiguous.
_AMBIGUOUS_EXCHANGE_ERRORS = (ccxt.ExchangeClosedByUser, ccxt.DuplicateOrderId)


def _is_definite_rejection(exc: BaseException) -> bool:
    """True only for a specific, mapped ccxt.ExchangeError subclass (InsufficientFunds,
    InvalidOrder, BadRequest, BadSymbol, AuthenticationError, OperationRejected, ...):
    the exchange answered and refused the order, so nothing was placed."""
    return (
        isinstance(exc, ccxt.ExchangeError)
        and type(exc) is not ccxt.ExchangeError
        and not isinstance(exc, _AMBIGUOUS_EXCHANGE_ERRORS)
    )


def _order_summary(order: object) -> dict:
    """The unified order fields worth recording for a human reconciler (never the raw
    `info` payload), as repr strings so NaN/inf/odd types survive JSON."""
    if not isinstance(order, dict):
        return {"response": repr(order)}
    keys = ("id", "status", "side", "amount", "filled", "remaining", "average", "price", "cost")
    return {k: repr(order.get(k)) for k in keys}


@dataclass
class FillResult:
    symbol: str
    side: str
    price: float
    base_amount: float
    quote_amount: float
    timestamp: str
    order_id: str | None


@dataclass
class ReconcileResult:
    """One in-flight order record after a reconcile pass (docs/live_readiness_design.md §2).

    outcome: "booked" (already in the ledger; the plan store may lag), "filled"/"partial"
    (found on the exchange and booked now), "not_executed" (found, nothing filled; record
    cleared), "never_placed" (positively absent; record cleared), "unknown" (kept, halted),
    "anomaly" (found but inconsistent with what we track; kept, halted). bot.py applies the
    plan-store transition for a result with a `fill`, then calls settle_order()."""

    client_order_id: str
    symbol: str
    side: str
    plan_ref: str
    outcome: str
    detail: str
    fill: FillResult | None = None
    remaining_amount: float | None = None
    exit_reason: str | None = None


class LiveBroker:
    # bot.py passes order_ref/exit_reason only to a broker that declares this (never PaperBroker).
    TRACKS_ORDER_REFS = True

    def __init__(
        self,
        exchange: ExchangeClient,
        portfolio: Portfolio,
        mode: str,
        live_trading_confirmed: bool,
    ) -> None:
        if mode == "live" and not live_trading_confirmed:
            raise LiveTradingDisabled(
                "config.yaml has mode: live but LIVE_TRADING_CONFIRM env var is not set to "
                "'YES-I-UNDERSTAND-THE-RISK'. Refusing to construct a live broker. "
                "This is intentional — see .env.example."
            )
        if mode != "live":
            raise LiveTradingDisabled(
                f"LiveBroker instantiated but config.yaml mode is {mode!r}, not 'live'."
            )
        self.exchange = exchange
        self.portfolio = portfolio
        # Everything below runs only AFTER the gate above passed.
        trade_log_path = getattr(portfolio, "trade_log_path", None)
        logs = Path(trade_log_path).parent if trade_log_path is not None else None
        # Single-instance guard (review F9): a second live process on the same logs/ refuses to start.
        if logs is not None:
            _acquire_lock(logs / LOCK_NAME, self)
        self._clock = time.time                  # injectable (alert reminders)
        self._halt_reason: str | None = None
        self._alerts: list[dict] = []            # drained by bot.py (pop_alerts) and sent to Telegram
        self._alert_last: dict = {}              # alert key -> time last queued (reminder throttle)
        self._last_halt_reminder: float | None = None
        self._halt_marker_path: Path | None = logs / HALT_MARKER_NAME if logs is not None else None
        # Write-ahead order record (module docstring). Without a trade_log_path (tests only --
        # build_components always sets one) it lives in memory, exactly like the halt latch.
        self._state_path: Path | None = logs / ORDER_STATE_NAME if logs is not None else None
        self._state: dict = _empty_order_state()
        self._state_error: str | None = None
        self._session_booked: set[str] = set()      # booked by THIS process, not yet seen by a reconcile pass
        self._awaiting_settle: dict[str, str] = {}  # symbol -> clientOrderId booked, waiting for bot.py's settle()
        self._applied: set[str] = set()             # clientOrderIds whose fill this process applied to memory
        # trades.csv keys present when the portfolio was rebuilt (reconstruct_cash replayed exactly these).
        self._boot_trade_keys: set[str] = {k for k, _row in self._trade_rows()}
        self._load_order_state()
        if self._halt_marker_path is not None and self._halt_marker_path.exists():
            logger.critical(
                "live broker HALTED at startup: %s exists -- new buys are refused until a human "
                "reconciles the exchange account and deletes it", self._halt_marker_path,
            )
        if self._state_error is not None:
            self._halt(
                f"order state file {self._state_path} is unreadable ({self._state_error.split(':', 1)[0]}); every "
                "order is refused until a human repairs it",
                bot_action="Every order (buys AND sells) is refused; the file is left untouched as evidence.",
                operator_check=(
                    "Check every open position and recent order on the exchange, repair or remove "
                    f"{ORDER_STATE_NAME} by hand, then delete {HALT_MARKER_NAME} and restart."
                ),
            )
        for coid, entry in sorted(self._state["inflight"].items()):
            if entry["state"] in _UNRESOLVED_STATES:
                self._halt(
                    f"startup: in-flight {entry['side']} {entry['symbol']} order {coid} from before the restart "
                    "is unresolved; its outcome is unknown until reconciled",
                    symbol=entry["symbol"], side=entry["side"], client_order_id=coid,
                    requested_amount=entry.get("requested_amount"),
                )
            elif entry.get("halt_reason") and not entry.get("halt_durable"):
                # A booked record that carried a halt (partial fill, unverified fill, ...) whose
                # marker never reached the disk: the restart stays halted. (A marker that WAS on
                # disk and is gone now was deleted by the human: that is respected.)
                self._halt(
                    f"startup: {entry['halt_reason']}",
                    symbol=entry["symbol"], side=entry["side"], client_order_id=coid,
                )
        for coid, info in sorted(self._state["unverified"].items()):
            self._halt(
                f"startup: {info.get('side')} {info.get('symbol')} fill of order {coid} is still UNVERIFIED",
                symbol=info.get("symbol"), side=info.get("side"), client_order_id=coid,
            )

    # ---------------------------------------------------------------- alerts

    def _queue_alert(self, key: object, **alert: object) -> None:
        """Queue an alert unless the same one was queued less than ALERT_REMINDER_SECONDS ago:
        a condition that persists is re-announced (a reminder), never only once."""
        now = self._clock()
        last = self._alert_last.get(key)
        if last is not None and now - last < ALERT_REMINDER_SECONDS:
            return
        self._alert_last[key] = now
        self._alerts.append({**alert, "reminder": last is not None})

    def pop_alerts(self) -> list[dict]:
        """Alerts raised since the last call (bot.py formats, escapes, redacts and sends them),
        plus a 'still halted' reminder every ALERT_REMINDER_SECONDS while halted."""
        if self.halted:
            now = self._clock()
            if self._last_halt_reminder is None:
                self._last_halt_reminder = now
            elif now - self._last_halt_reminder >= ALERT_REMINDER_SECONDS:
                self._last_halt_reminder = now
                unresolved = sorted(self._unresolved_entries())
                self._alerts.append(dict(
                    kind="halt_reminder", reminder=True,
                    title=f"STILL HALTED: {self._halt_reason or self._halt_marker_path}",
                    symbol=None, side=None, amount=None,
                    client_order_id=", ".join(unresolved) if unresolved else None,
                    bot_action=f"New buys and new plans stay refused; {len(unresolved)} unresolved order record(s).",
                    operator_check=self._operator_check(None, None, None),
                ))
        alerts, self._alerts = self._alerts, []
        return alerts

    def requeue_alerts(self, alerts: list[dict]) -> None:
        """bot.py could not deliver these: they go back to the front of the queue (retried next cycle)."""
        self._alerts[:0] = list(alerts)

    def _action_suffix(self) -> str:
        return " Buys stay HALTED until a human deletes the halt marker." if self.halted else \
            " The broker is NOT halted (the outcome is fully known)."

    @staticmethod
    def _operator_check(symbol: object, side: object, coid: object) -> str:
        base = str(symbol).split("/")[0] if symbol else "the base asset"
        order = f"clientOrderId {coid}" if coid else f"any recent {side or ''} {symbol or ''} order"
        return (
            f"In the exchange's order history find {order}; compare the {base} balance and open orders with "
            f"logs/trades.csv and logs/trade_plans_current.json; fix the ledger by hand if they differ, then "
            f"delete logs/{HALT_MARKER_NAME} and restart the bot."
        )

    # ---------------------------------------------------------------- halt latch

    @property
    def halted(self) -> bool:
        return (
            self._halt_reason is not None
            or (self._halt_marker_path is not None and self._halt_marker_path.exists())
            or self._state_error is not None
            or bool(self._unresolved_entries())
            or bool(self._state.get("unverified"))
        )

    def _halt(
        self, reason: str, *, bot_action: str | None = None, operator_check: str | None = None, **details: object,
    ) -> None:
        """Latch the halt in memory first (can't fail), queue an alert, then persist it."""
        self._halt_reason = reason
        logger.critical("LIVE BROKER HALTED -- new buys refused until a human reconciles: %s %s", reason, details)
        symbol, side, coid = details.get("symbol"), details.get("side"), details.get("client_order_id")
        self._queue_alert(
            ("halt", reason),
            kind="halt", title=reason, symbol=symbol, side=side, client_order_id=coid,
            amount=details.get("requested_amount", details.get("amount")),
            bot_action=bot_action or (
                "HALTED: new buys and new plans are refused; stop-loss/take-profit exits keep running "
                "(except on a symbol whose order outcome is unknown)."
            ),
            operator_check=operator_check or self._operator_check(symbol, side, coid),
        )
        self._persist_halt(reason, details)

    def _persist_halt(self, reason: str, details: dict | None = None) -> bool:
        if self._halt_marker_path is None:
            return True
        try:
            payload = {"halted_at": now_iso(), "reason": reason, **{k: repr(v) for k, v in (details or {}).items()}}
            _atomic_write_json(self._halt_marker_path, payload)
            return True
        except Exception as exc:  # noqa: BLE001 -- the in-memory latch still holds for this process
            logger.exception("could not persist live broker halt marker %s", self._halt_marker_path)
            self._queue_alert(
                ("halt-marker-write-failed",),
                kind="halt_marker_failed", title=f"the halt marker could NOT be written ({type(exc).__name__})",
                symbol=details.get("symbol") if details else None, side=None, amount=None, client_order_id=None,
                bot_action="Halted in memory only. Order records are kept until the marker is on disk, so a "
                           "restart stays halted, but the disk is failing.",
                operator_check=f"Fix the disk/permissions of logs/ (disk full? read-only?); {HALT_MARKER_NAME} "
                               "must be writable.",
            )
            return False

    def _halt_is_durable(self) -> bool:
        """True when a restart would still be halted by the marker (or there is no disk at all)."""
        return self._halt_marker_path is None or self._halt_marker_path.exists()

    def _ensure_halt_durable(self) -> bool:
        """No halt latched, or the latched halt is on disk (re-persisting it if needed)."""
        if self._halt_reason is None or self._halt_is_durable():
            return True
        return self._persist_halt(self._halt_reason) and self._halt_is_durable()

    def require_human(self, reason: str, **details: object) -> None:
        """Public halt for conditions found outside the broker (e.g. a quarantined plan store)."""
        self._halt(reason, **details)

    def report_store_failure(self, symbol: str, detail: str) -> None:
        """bot.py could not persist a plan-store transition for an order this broker booked:
        the order record is kept (its symbol stays blocked), halt, alert."""
        self._halt(
            f"{symbol}: the plan store transition for a booked order was not persisted ({detail}); the order "
            "record is kept",
            symbol=symbol,
            bot_action="Kept the order record (no second order on this symbol), HALTED new buys and new plans. "
                       "The next cycles retry the plan-store update.",
            operator_check=f"Fix the disk/permissions for logs/trade_plans_current.json, then check {symbol} "
                           "on the exchange against logs/trades.csv and the plan store.",
        )

    # ---------------------------------------------------------------- write-ahead order record

    def _load_order_state(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            self._state = _validate_order_state(json.loads(self._state_path.read_text()))
        except Exception as exc:  # noqa: BLE001 -- unreadable = unknown = refuse everything
            self._state_error = f"{type(exc).__name__}: {exc}"
            logger.critical("live order state file %s is unreadable: %s", self._state_path, self._state_error)

    def _unresolved_entries(self) -> dict[str, dict]:
        return {c: e for c, e in self._state["inflight"].items() if e["state"] in _UNRESOLVED_STATES}

    def order_in_flight(self, symbol: str) -> bool:
        """True while any order record for `symbol` is unresolved or booked-but-unsettled
        (bot.py must not DISCARD its PENDING plan: the buy may have filled)."""
        return any(
            e["symbol"] == symbol and (e["state"] in _UNRESOLVED_STATES or c not in self._session_booked)
            for c, e in self._state["inflight"].items()
        )

    def _mutate_state(self, change) -> None:
        """Apply `change` to a copy, persist it, and only then adopt it in memory (memory
        always mirrors disk). Raises if the file is unreadable or the write fails."""
        if self._state_error is not None:
            raise LiveOrderStateError(f"order state file {self._state_path} is unreadable; refusing to overwrite it")
        new = copy.deepcopy(self._state)
        change(new)
        if self._state_path is not None:
            _atomic_write_json(self._state_path, new)
        self._state = new

    @staticmethod
    def _attempt_key(plan_ref: str, side: str) -> str:
        return f"{plan_ref}|{side}"

    def _check_can_place(self, symbol: str, side: str) -> None:
        """Refuse (LiveBrokerHalted, no order) whatever the order record says is unsafe."""
        if self._state_error is not None:
            raise LiveBrokerHalted(
                f"refusing to {side} {symbol}: the order state file is unreadable; no order placed"
            )
        for coid, entry in self._state["inflight"].items():
            state = entry["state"]
            if state in _UNRESOLVED_STATES and side == "buy":
                raise LiveBrokerHalted(
                    f"refusing to buy {symbol}: live broker is halted (order {coid} {entry['side']} "
                    f"{entry['symbol']} is unresolved); no order placed"
                )
            if entry["symbol"] != symbol:
                continue
            if state in _SYMBOL_BLOCKING_STATES:
                raise LiveBrokerHalted(
                    f"refusing to {side} {symbol}: order {coid} ({entry['side']}) is unresolved; "
                    "a second order could duplicate it; no order placed"
                )
            if state == "booked" and coid not in self._session_booked:
                raise LiveBrokerHalted(
                    f"refusing to {side} {symbol}: booked order {coid} is awaiting reconcile of the plan store; "
                    "no order placed"
                )

    def _record_intent(self, symbol: str, side: str, plan_ref: str, **fields: object) -> str:
        """Write-ahead: persist the order record and return its clientOrderId, or halt and
        raise -- in which case NO order may be placed."""
        attempt = int(self._state["attempts"].get(self._attempt_key(plan_ref, side), 0))
        coid = client_order_id(plan_ref, side, attempt)
        if coid in self._state["inflight"]:
            # Never re-send (or overwrite the record of) an id that may belong to a real order.
            self._halt(
                f"{side} {symbol}: clientOrderId {coid} already has an order record "
                f"({self._state['inflight'][coid]['state']}); NO order placed",
                symbol=symbol, side=side, client_order_id=coid,
            )
            raise LiveBrokerHalted(f"refusing to {side} {symbol}: clientOrderId {coid} is already in use; no order placed")
        entry = {
            "client_order_id": coid, "plan_ref": plan_ref, "symbol": symbol, "side": side, "attempt": attempt,
            "state": "intent", "created_at": now_iso(), "exchange_order_id": None, "fill": None,
            "remaining_amount": None, **fields,
        }
        try:
            self._mutate_state(lambda s: s["inflight"].__setitem__(coid, entry))
        except Exception as exc:
            self._halt(
                f"{side} {symbol}: could not persist the write-ahead order record ({type(exc).__name__}); "
                "NO order was placed",
                symbol=symbol, side=side, client_order_id=coid, requested_amount=fields.get("requested_amount"),
                bot_action=(
                    "Placed NO order (fail closed). New buys are halted; every further order fails the same way "
                    "until the order state file can be written again. A protective exit is NOT being executed."
                ),
                operator_check=(
                    f"Fix the disk/permissions for logs/{ORDER_STATE_NAME} (disk full? read-only?). If a stop-loss "
                    f"was due, consider closing the {symbol} position on the exchange by hand, then reconcile the "
                    f"logs, delete logs/{HALT_MARKER_NAME} and restart."
                ),
            )
            raise LiveOrderStateError(
                f"{side} {symbol}: write-ahead order record could not be persisted ({type(exc).__name__}); "
                "no order placed, new buys halted"
            ) from exc
        return coid

    def _drop_entry(self, coid: str) -> None:
        """A definite rejection: nothing was placed, the id was never used (attempt NOT consumed).
        Only an `intent` record can be dropped here -- never one for an order known to exist."""
        entry = self._state["inflight"].get(coid)
        if entry is None or entry["state"] != "intent":
            logger.error("not dropping order record %s in state %s", coid, None if entry is None else entry["state"])
            return
        try:
            self._mutate_state(lambda s: s["inflight"].pop(coid, None))
        except Exception:  # noqa: BLE001 -- the intent stays; a reconcile will find nothing and clear it
            logger.exception("could not clear the order record %s after a definite rejection", coid)

    def _set_entry(self, coid: str, consume_attempt: bool = False, **fields: object) -> bool:
        """Best-effort update of an existing record (False + logged on failure). With
        consume_attempt the id's attempt counter advances IN THE SAME WRITE."""
        entry = self._state["inflight"].get(coid)
        if entry is None:
            return False
        key = self._attempt_key(entry["plan_ref"], entry["side"])

        def change(s: dict) -> None:
            s["inflight"][coid].update(fields)
            if consume_attempt:
                s["attempts"][key] = max(int(s["attempts"].get(key, 0)), int(entry["attempt"]) + 1)

        try:
            self._mutate_state(change)
            return True
        except Exception:  # noqa: BLE001
            logger.exception("could not update the order record %s to %s", coid, fields)
            return False

    def _mark_placed(self, coid: str, order: object) -> None:
        exchange_id = order.get("id") if isinstance(order, dict) else None
        # Best effort: an `intent` record is reconciled through its clientOrderId just the same.
        self._set_entry(coid, state="placed", exchange_order_id=None if exchange_id is None else str(exchange_id))

    def _mark_booked(
        self, coid: str, result: "FillResult", remaining_amount: float | None, in_session: bool = True,
        halt_reason: str | None = None, unverified: bool = False,
    ) -> bool:
        """trades.csv + the ledger now hold this fill: record it and consume the attempt (the id
        is used). On failure, halt -- the trades.csv dedup keeps a reconcile from booking it twice.

        in_session=False (reconcile): the record blocks its symbol until bot.py settles it.
        halt_reason: carried by the record so a restart stays halted (a partial / unverified fill)."""
        entry = self._state["inflight"][coid]
        key = self._attempt_key(entry["plan_ref"], entry["side"])
        fill = {
            "price": result.price, "base_amount": result.base_amount, "quote_amount": result.quote_amount,
            "timestamp": result.timestamp, "order_id": result.order_id, "trade_key": _trade_key(result, coid),
        }

        def change(s: dict) -> None:
            s["inflight"][coid].update(state="booked", fill=fill, remaining_amount=remaining_amount)
            if halt_reason:
                s["inflight"][coid]["halt_reason"] = halt_reason
                s["inflight"][coid]["halt_durable"] = self._halt_is_durable()
            s["attempts"][key] = max(int(s["attempts"].get(key, 0)), int(entry["attempt"]) + 1)
            if unverified:
                s["unverified"][coid] = {
                    "symbol": entry["symbol"], "side": entry["side"], "plan_ref": entry["plan_ref"],
                    "booked_base_amount": result.base_amount, "requested_amount": entry.get("requested_amount"),
                    "exchange_order_id": result.order_id, "created_at": entry.get("created_at"),
                }

        try:
            self._mutate_state(change)
        except Exception as exc:  # noqa: BLE001
            self._halt(
                f"{entry['side']} {entry['symbol']}: filled and booked, but the order record {coid} could not be "
                f"marked booked ({type(exc).__name__})",
                symbol=entry["symbol"], side=entry["side"], client_order_id=coid,
                requested_amount=entry.get("requested_amount"),
            )
            return False
        if in_session:
            self._session_booked.add(coid)
            self._awaiting_settle[entry["symbol"]] = coid
        return True

    def settle(self, symbol: str) -> None:
        """Called by bot.py AFTER the plan store recorded the fill for `symbol`: clears the
        write-ahead record of the order this broker just booked for it."""
        coid = self._awaiting_settle.pop(symbol, None)
        if coid is not None:
            self.settle_order(coid)

    def settle_order(self, coid: str) -> bool:
        """Clear a BOOKED order record (the plan store has caught up). Never clears an
        unresolved one, and never while a latched halt exists only in memory (the record is
        what keeps the restart halted then). On failure, halts and keeps the record."""
        entry = self._state["inflight"].get(coid)
        if entry is None:
            return True
        if entry["state"] != "booked":
            logger.error("refusing to settle order record %s in state %s", coid, entry["state"])
            return False
        if not self._ensure_halt_durable():
            self._set_entry(coid, halt_reason=entry.get("halt_reason") or self._halt_reason, halt_durable=False)
            logger.critical("not settling order record %s: the halt is not on disk", coid)
            return False
        try:
            self._mutate_state(lambda s: s["inflight"].pop(coid, None))
        except Exception as exc:  # noqa: BLE001
            self._halt(
                f"{entry['side']} {entry['symbol']}: could not clear settled order record {coid} "
                f"({type(exc).__name__})",
                symbol=entry["symbol"], side=entry["side"], client_order_id=coid,
            )
            return False
        self._session_booked.discard(coid)
        for symbol, pending in list(self._awaiting_settle.items()):
            if pending == coid:
                del self._awaiting_settle[symbol]
        return True

    def report_unmatched(self, coid: str, reason: str) -> None:
        """bot.py could not apply a booked order to the plan store (e.g. no matching plan): keep
        the record (its symbol stays blocked), halt, alert."""
        entry = self._state["inflight"].get(coid) or {}
        self._halt(
            f"{entry.get('side')} {entry.get('symbol')}: order {coid} is booked but {reason}",
            symbol=entry.get("symbol"), side=entry.get("side"), client_order_id=coid,
            requested_amount=(entry.get("fill") or {}).get("base_amount"),
            bot_action=(
                "Kept the booked position in the ledger but could not attach it to a plan: it has NO "
                "stop-loss/take-profit. New buys and new orders on this symbol are refused."
            ),
        )

    def _exit_failed(self, symbol: str, amount: float, coid: str | None, why: str, bot_action: str) -> None:
        """Review F10: a protective sell that could not be executed is ALWAYS alerted (with reminders)."""
        self._queue_alert(
            ("exit-failed", symbol, why),
            kind="exit_failed", title=f"sell {symbol} FAILED: {why}",
            symbol=symbol, side="sell", amount=amount, client_order_id=coid,
            bot_action=bot_action,
            operator_check=(
                f"The {symbol} stop-loss/take-profit is NOT executing. Check the account and the {symbol.split('/')[0]} "
                "balance on the exchange; close the position by hand if needed, then fix logs/ to match."
            ),
        )

    def _place(self, symbol: str, side: str, amount: float, coid: str) -> object:
        """create_market_order; a definite exchange rejection is re-raised unchanged, any
        other failure leaves the order's fate unknown -> halt + LiveOrderUnreconciled.
        A BaseException (SIGKILL stand-in, KeyboardInterrupt) passes through untouched:
        the intent record stays on disk for the next start's reconcile."""
        try:
            return self.exchange.create_market_order(symbol, side, amount, client_order_id=coid)
        except Exception as exc:
            if _is_definite_rejection(exc):
                self._drop_entry(coid)
                if side == "sell":
                    self._exit_failed(
                        symbol, amount, coid, f"rejected by the exchange ({type(exc).__name__})",
                        "Placed nothing, booked nothing; the position stays OPEN and the exit is retried every "
                        "cycle (capped at the free balance when the exchange reports one).",
                    )
                raise  # the exchange answered and rejected it: nothing was placed
            self._halt(
                f"{side} {symbol}: create_market_order raised {type(exc).__name__}; "
                "the order may or may not be live on the exchange",
                symbol=symbol, side=side, requested_amount=amount, error=type(exc).__name__, client_order_id=coid,
                bot_action=(
                    "Booked NOTHING and HALTED: new buys and new plans are refused. The bot will look the order "
                    "up by its clientOrderId at the start of every cycle and book it if it filled. Until then no "
                    f"other order is placed on {symbol}."
                ),
            )
            raise LiveOrderUnreconciled(
                f"{side} {symbol}: order outcome unknown ({type(exc).__name__}: {exc}); nothing booked, new buys halted"
            ) from exc

    def _lookup(self, symbol: str, coid: str, exchange_order_id: str | None, side: str,
                created_at: str | None = None) -> dict | None:
        """The exchange's own record of an order, or None on ANY failure / ambiguity."""
        created = _parse_iso(created_at) or datetime.now(timezone.utc)
        try:
            order = self.exchange.find_order(
                symbol, coid, exchange_order_id=exchange_order_id,
                since_ms=int((created - timedelta(seconds=RECONCILE_LOOKBACK_SECONDS)).timestamp() * 1000),
                side=side,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("order lookup for %s %s failed (%s)", symbol, coid, type(exc).__name__)
            return None
        return order if isinstance(order, dict) else None

    def _verify_response(self, symbol: str, side: str, coid: str, order: object, requested: float,
                         price_hint: float, verify_unusable: bool) -> tuple[object, str]:
        """Review F5/F8b. (order to book, how):
        "response"     -- the create response is complete and usable: book it;
        "verified"     -- an ack-only / still-open (/ unusable, buys) response, replaced by the
                          exchange's own terminal record of the order;
        "not_executed" -- terminal with nothing filled: nothing to book;
        "unverified"   -- ack-only / open and the lookup failed: book the response's fallback, halt;
        "unusable"     -- unusable and not (or not successfully) verified."""
        if _terminal_zero_fill(order):
            return order, "not_executed"
        # Ack-only = neither filled nor remaining (Kraken: txid/descr only). A response with only
        # one of them is judged as it stands (remaining without filled is contradictory).
        complete = isinstance(order, dict) and order.get("status") != "open" and (
            order.get("filled") is not None or order.get("remaining") is not None
        )
        unusable = False
        if complete:
            try:
                _parse_fill(order, requested, price_hint)
                return order, "response"
            except _UnusableFill:
                unusable = True
        elif not isinstance(order, dict):
            unusable = True
        if unusable and not verify_unusable:
            return order, "unusable"
        entry = self._state["inflight"].get(coid, {})
        exchange_id = order.get("id") if isinstance(order, dict) else None
        looked = self._lookup(symbol, coid, None if exchange_id is None else str(exchange_id), side,
                              entry.get("created_at"))
        if looked is not None and looked.get("status") in _TERMINAL_STATUSES:
            if _terminal_zero_fill(looked):
                return looked, "not_executed"
            try:
                _parse_fill(looked, requested, price_hint)
                if looked.get("filled") is not None or looked.get("status") == "closed":
                    return looked, "verified"
            except _UnusableFill:
                pass
        return order, "unusable" if unusable else "unverified"

    def _retire_unexecuted(self, coid: str, symbol: str, side: str, amount: float, order: object) -> None:
        """Terminal zero-fill: the id exists on the exchange (attempt consumed) but nothing was
        executed. The record is cleared, nothing is booked, the caller raises LiveOrderNotExecuted."""
        entry = self._state["inflight"].get(coid)
        key = self._attempt_key(entry["plan_ref"], side) if entry else None

        def change(s: dict) -> None:
            s["inflight"].pop(coid, None)
            if key is not None:
                s["attempts"][key] = max(int(s["attempts"].get(key, 0)), int(entry["attempt"]) + 1)

        try:
            self._mutate_state(change)
        except Exception as exc:  # noqa: BLE001 -- the record stays (placed): reconcile resolves it (R8)
            logger.exception("could not retire unexecuted order record %s (%s)", coid, type(exc).__name__)
        status = order.get("status") if isinstance(order, dict) else None
        if side == "sell":
            self._exit_failed(symbol, amount, coid, f"not executed by the exchange (status {status})",
                              "Booked nothing; the position stays OPEN and the exit is retried next cycle.")
        else:
            self._queue_alert(
                ("buy-not-executed", coid), kind="not_executed",
                title=f"buy {symbol} not executed by the exchange (status {status}); nothing booked",
                symbol=symbol, side=side, amount=amount, client_order_id=coid,
                bot_action="Booked nothing; the plan stays PENDING and may be retried with a new clientOrderId.",
                operator_check=self._operator_check(symbol, side, coid),
            )

    # ---------------------------------------------------------------- trades.csv: the booking path

    def _trade_rows(self) -> list[tuple[str, dict]]:
        """(key, row) for every trades.csv row with a usable order key. Tolerates a header
        written by the paper broker (no order_id column): the extra value is the key."""
        path = getattr(self.portfolio, "trade_log_path", None)
        if path is None or not Path(path).exists():
            return []
        rows = []
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                key = row.get("order_id")
                if not key and isinstance(row.get(None), list) and row[None]:
                    key = row[None][0]
                if key and key != "None":
                    rows.append((key, row))
        return rows

    def _trade_log_fill(self, entry: dict, keys: set[str]) -> FillResult | None:
        """The trades.csv row already booked for this order (any of its keys), if any."""
        keys = {k for k in keys if k}
        for key, row in self._trade_rows():
            if key in keys and row.get("symbol") == entry["symbol"] and row.get("side") == entry["side"]:
                order_id = key if not key.startswith("coid:") else None
                return FillResult(entry["symbol"], entry["side"], float(row["price"]), float(row["base_amount"]),
                                  float(row["quote_amount"]), row.get("timestamp") or now_iso(), order_id)
        return None

    def _book(self, coid: str, result: FillResult, apply_to_memory, remaining: float | None,
              in_session: bool = True, halt_reason: str | None = None, unverified: bool = False) -> bool:
        """THE booking path (review F1): trades.csv row first (fsync'd), the in-memory ledger
        only after it succeeded, then the record says booked. A failed trades.csv write leaves
        memory untouched and the record unresolved (halted) -- the reconcile books it once."""
        self._log_live_trade(result, coid)
        apply_to_memory()
        self._applied.add(coid)
        return self._mark_booked(coid, result, remaining, in_session=in_session, halt_reason=halt_reason,
                                 unverified=unverified)

    # ---------------------------------------------------------------- reconcile (issue #99 item 2)

    def reconcile(self, now: datetime | None = None) -> list[ReconcileResult]:
        """Resolve every in-flight order record (decision table: docs/live_readiness_design.md
        §2) and re-check every unverified fill. Called by bot.py at the top of every cycle,
        i.e. before any order of that cycle. Never resumes buys: a halt stays until a human
        deletes the marker. No record -> no exchange call at all."""
        self._session_booked.clear()  # from now on, an unsettled booked record blocks its symbol
        if self._state_error is not None:
            return []
        now = now or datetime.now(timezone.utc)
        results: list[ReconcileResult] = []
        for coid in sorted(self._state["inflight"]):
            entry = copy.deepcopy(self._state["inflight"][coid])
            try:
                result = self._reconcile_entry(entry, now)
            except Exception as exc:  # noqa: BLE001 -- never guess: keep the record, stay halted
                logger.exception("reconcile of order record %s failed", coid)
                result = self._unknown(entry, f"reconcile raised {type(exc).__name__}")
            self._queue_alert(
                ("reconcile", coid, result.outcome),
                kind="reconcile", title=f"reconcile: {result.outcome} -- {result.detail}",
                symbol=result.symbol, side=result.side, client_order_id=coid,
                amount=result.fill.base_amount if result.fill is not None else entry.get("requested_amount"),
                bot_action=_RECONCILE_ACTIONS.get(result.outcome, "") + self._action_suffix(),
                operator_check=self._operator_check(result.symbol, result.side, coid),
            )
            results.append(result)
        self._recheck_unverified()
        return results

    def _recheck_unverified(self) -> None:
        """An unverified fill (booked from a fallback) is looked up again every cycle; a match
        clears the flag (the halt stays for the human), a mismatch is alerted (with reminders)."""
        for coid, info in sorted(self._state.get("unverified", {}).items()):
            looked = self._lookup(info.get("symbol"), coid, info.get("exchange_order_id"), info.get("side"),
                                  info.get("created_at"))
            if looked is None or looked.get("status") not in _TERMINAL_STATUSES:
                continue
            filled = looked.get("filled")
            booked = info.get("booked_base_amount")
            if _is_finite_number(filled) and _is_finite_number(booked) and _same_amount(filled, float(booked)):
                try:
                    self._mutate_state(lambda s: s["unverified"].pop(coid, None))
                except Exception:  # noqa: BLE001
                    logger.exception("could not clear unverified flag %s", coid)
                    continue
                outcome, detail = "verified", f"the exchange confirms the booked {booked!r}"
            else:
                outcome, detail = "verify_mismatch", f"booked {booked!r} but the exchange filled {filled!r}"
            self._queue_alert(
                ("unverified", coid, outcome), kind="unverified", title=f"unverified fill {outcome}: {detail}",
                symbol=info.get("symbol"), side=info.get("side"), amount=booked, client_order_id=coid,
                bot_action="Nothing changed in the ledger automatically." + self._action_suffix(),
                operator_check=self._operator_check(info.get("symbol"), info.get("side"), coid),
            )

    def _result(self, entry: dict, outcome: str, detail: str, **kw: object) -> ReconcileResult:
        return ReconcileResult(
            entry["client_order_id"], entry["symbol"], entry["side"], entry["plan_ref"], outcome, detail,
            exit_reason=entry.get("exit_reason"), **kw,
        )

    def _unknown(self, entry: dict, detail: str, outcome: str = "unknown") -> ReconcileResult:
        """Keep the record untouched; make sure the halt is latched and (re)persisted."""
        if self._halt_reason is None or not self._halt_is_durable():
            self._halt(
                f"{entry['side']} {entry['symbol']}: order {entry['client_order_id']} still unresolved ({detail})",
                symbol=entry["symbol"], side=entry["side"], client_order_id=entry["client_order_id"],
                requested_amount=entry.get("requested_amount"),
            )
        return self._result(entry, outcome, detail)

    def _reconcile_entry(self, entry: dict, now: datetime) -> ReconcileResult:
        if entry["state"] == "booked":
            return self._restore_booked(entry)
        symbol, side, coid = entry["symbol"], entry["side"], entry["client_order_id"]
        created = _parse_iso(entry.get("created_at"))
        if created is None:
            return self._unknown(entry, "the record has no readable created_at")
        # R4 first: a fill already in trades.csv (keyed by the clientOrderId even without an
        # exchange id) is never booked a second time, whatever the lookup says.
        booked = self._trade_log_fill(entry, {f"coid:{coid}", entry.get("exchange_order_id") or ""})
        if booked is not None:
            return self._adopt_logged(entry, booked)
        try:
            order = self.exchange.find_order(
                symbol, coid, exchange_order_id=entry.get("exchange_order_id"),
                since_ms=int((created - timedelta(seconds=RECONCILE_LOOKBACK_SECONDS)).timestamp() * 1000),
                side=side,
            )
        except Exception as exc:  # noqa: BLE001 -- OrderNotFound with a known id, network, ambiguity, ...
            return self._unknown(entry, f"exchange lookup failed ({type(exc).__name__})")
        if order is None:
            if entry["state"] != "intent" or entry.get("exchange_order_id"):
                return self._unknown(entry, "the exchange answered this order once but the lookup no longer finds it")
            if (now - created).total_seconds() < RECONCILE_NOT_FOUND_GRACE_SECONDS:
                return self._unknown(entry, "not visible on the exchange yet (inside the grace period)")
            return self._clear_unexecuted(entry, consumed=False, outcome="never_placed",
                                          detail="not on the exchange: it was never placed")
        if not isinstance(order, dict):
            return self._unknown(entry, f"lookup returned {type(order).__name__}")
        return self._resolve_found(entry, order)

    def _clear_unexecuted(self, entry: dict, consumed: bool, outcome: str, detail: str) -> ReconcileResult:
        """Nothing was executed: drop the record (consume the attempt if the id exists on the
        exchange). Only once the halt is on disk, so the restart after it is still halted."""
        if not self._halt_is_durable():
            self._halt(
                f"{entry['side']} {entry['symbol']}: order {entry['client_order_id']} resolved ({outcome}) -- "
                "waiting for a human before buys resume",
                symbol=entry["symbol"], side=entry["side"], client_order_id=entry["client_order_id"],
            )
            if not self._halt_is_durable():
                return self._unknown(entry, f"{detail}, but the halt marker is not on disk: record kept")
        coid = entry["client_order_id"]
        key = self._attempt_key(entry["plan_ref"], entry["side"])

        def change(s: dict) -> None:
            s["inflight"].pop(coid, None)
            if consumed:
                s["attempts"][key] = max(int(s["attempts"].get(key, 0)), int(entry["attempt"]) + 1)

        try:
            self._mutate_state(change)
        except Exception as exc:  # noqa: BLE001
            return self._unknown(entry, f"{detail}, but clearing the record failed ({type(exc).__name__})")
        return self._result(entry, outcome, detail)

    def _resolve_found(self, entry: dict, order: dict) -> ReconcileResult:
        status = order.get("status")
        requested = float(entry["requested_amount"])
        if status == "open":
            return self._unknown(entry, "the order is still OPEN on the exchange (not treated as filled)")
        if status not in _TERMINAL_STATUSES:
            return self._unknown(entry, f"unrecognised order status {status!r}")
        filled = order.get("filled")
        if filled is None and status == "closed":
            filled = order.get("amount")
        if not (_is_finite_number(filled) and 0 <= filled <= requested * (1 + _OVERFILL_TOLERANCE)):
            return self._unknown(entry, f"filled={filled!r} is not a finite number in [0, requested {requested!r}]")
        if filled == 0:
            if status == "closed":
                return self._unknown(entry, "status closed with nothing filled")
            return self._clear_unexecuted(entry, consumed=True, outcome="not_executed",
                                          detail=f"status {status} with nothing filled")
        order_id = None if order.get("id") is None else str(order.get("id"))
        booked = self._trade_log_fill(entry, {order_id or "", f"coid:{entry['client_order_id']}"})
        if booked is not None:  # R4: already in trades.csv -- never book it twice
            return self._adopt_logged(entry, booked)
        order = dict(order, filled=filled)
        if entry["side"] == "buy":
            return self._book_found_buy(entry, order, order_id, float(filled))
        return self._book_found_sell(entry, order, order_id, float(filled))

    def _adopt_logged(self, entry: dict, booked: FillResult) -> ReconcileResult:
        """R4: trades.csv already holds this order's fill; record it as booked and re-sync memory."""
        remaining = None
        if entry["side"] == "sell":
            left = float(entry["held_at_send"]) - booked.base_amount
            remaining = left if left > float(entry["held_at_send"]) * _SELL_SHORTFALL_TOLERANCE else None
        if not self._mark_booked(entry["client_order_id"], booked, remaining, in_session=False):
            return self._unknown(entry, "found in trades.csv but the record could not be marked booked")
        return self._restore_booked(self._state["inflight"][entry["client_order_id"]])

    def _book_found_buy(self, entry: dict, order: dict, order_id: str | None, filled: float) -> ReconcileResult:
        symbol = entry["symbol"]
        coid = entry["client_order_id"]
        try:
            price, amount, quote, ledger = self._compute_buy(symbol, order, float(entry["requested_amount"]),
                                                             float(entry["price_hint"]))
        except _UnusableFill as exc:
            return self._unknown(entry, f"found filled but its fields are unusable ({exc})")
        result = FillResult(symbol, "buy", price, amount, quote, now_iso(), order_id)
        try:
            ok = self._book(coid, result, lambda: self._commit_buy(symbol, ledger), None, in_session=False)
        except Exception as exc:  # noqa: BLE001 -- trades.csv failed: nothing booked, record kept
            return self._unknown(entry, f"found filled but trades.csv could not be written ({type(exc).__name__})")
        if not ok:
            return self._unknown(entry, "booked, but the record could not be marked booked")
        # Review F8d: a fill equal to the precision-truncated request is a full fill.
        reference = self._sent_reference(symbol, float(entry["requested_amount"]))
        outcome = "filled" if filled >= reference * (1 - _OVERFILL_TOLERANCE) else "partial"
        return self._result(entry, outcome, f"found {order.get('status')} on the exchange, filled {filled!r}; booked",
                            fill=result)

    def _book_found_sell(self, entry: dict, order: dict, order_id: str | None, filled: float) -> ReconcileResult:
        symbol = entry["symbol"]
        coid = entry["client_order_id"]
        held = float(entry["held_at_send"])
        pos = self.portfolio.positions.get(symbol)
        if pos is None or not _same_amount(pos.amount, held):
            return self._unknown(
                entry, f"found filled {filled!r}, but the tracked position ({None if pos is None else pos.amount!r}) "
                f"is not the {held!r} it was sent for", outcome="anomaly",
            )
        try:
            sale = self._compute_sell(symbol, order, held, float(entry["requested_amount"]),
                                      float(entry["price_hint"]))
        except _UnusableFill as exc:
            return self._unknown(entry, f"found filled but its fields are unusable ({exc})")
        halt_reason = None
        if sale.partial:  # review F4: halt FIRST, durably, then book
            halt_reason = f"sell {symbol}: PARTIAL fill {sale.filled!r} of {held!r} found by reconcile"
            self._halt(halt_reason, symbol=symbol, side="sell", client_order_id=coid, requested_amount=sale.filled)
        result = FillResult(symbol, "sell", sale.price, sale.filled, sale.proceeds, now_iso(), order_id)
        try:
            ok = self._book(coid, result, lambda: self._apply_sell(symbol, sale), sale.left if sale.partial else None,
                            in_session=False, halt_reason=halt_reason)
        except Exception as exc:  # noqa: BLE001
            return self._unknown(entry, f"found filled but trades.csv could not be written ({type(exc).__name__})")
        if not ok:
            return self._unknown(entry, "booked, but the record could not be marked booked")
        return self._result(
            entry, "partial" if sale.partial else "filled",
            f"found {order.get('status')} on the exchange, filled {filled!r}; booked",
            fill=result, remaining_amount=sale.left if sale.partial else None,
        )

    def _restore_booked(self, entry: dict) -> ReconcileResult:
        """A booked record: trades.csv already holds the fill; memory and the plan store may lag.
        Cash is re-applied only if neither this process applied it nor the startup replay of
        trades.csv contained it; the position side follows the same rule as before (restart:
        rebuilt from the store)."""
        fill = entry["fill"]
        symbol = entry["symbol"]
        coid = entry["client_order_id"]
        result = FillResult(symbol, entry["side"], float(fill["price"]), float(fill["base_amount"]),
                            float(fill["quote_amount"]), fill.get("timestamp") or now_iso(), fill.get("order_id"))
        trade_key = fill.get("trade_key") or _trade_key(result, coid)
        cash_applied = coid in self._applied or trade_key in self._boot_trade_keys
        remaining = entry.get("remaining_amount")
        pos = self.portfolio.positions.get(symbol)
        held = entry.get("held_at_send")
        if entry["side"] == "buy":
            if not cash_applied:
                self.portfolio.cash = self.portfolio.cash - result.quote_amount
            if pos is None:
                self.portfolio.positions[symbol] = Position(symbol, result.base_amount, result.price)
        else:
            if not cash_applied:
                self.portfolio.cash = self.portfolio.cash + result.quote_amount
            if pos is not None and held is not None and _same_amount(pos.amount, float(held)):
                # Still the pre-sell amount (the store never saw the sale): apply it now.
                if remaining is not None:
                    pos.amount = float(remaining)
                else:
                    del self.portfolio.positions[symbol]
        self._applied.add(coid)
        if entry.get("halt_reason") and not entry.get("halt_durable") and self._halt_reason is None:
            self._halt(entry["halt_reason"], symbol=symbol, side=entry["side"], client_order_id=coid)
        return self._result(entry, "booked", "already booked; plan store re-synced", fill=result,
                            remaining_amount=None if remaining is None else float(remaining))

    # ---------------------------------------------------------------- buy

    def _buy_ledger(
        self, symbol: str, price: float, amount: float, cost: float | None = None,
    ) -> tuple[float, float, float] | None:
        """(new_cash, new_amount, new_entry_price) for booking `amount` at `price`, or None
        if any of it would be non-finite / non-positive. Pure -- mutates nothing. Same
        arithmetic, in the same order, as the pre-hardening code (byte-identical ledger)
        unless the exchange reported fees (`cost` = the real cash debit, item 4)."""
        if cost is None:
            cost = amount * price
        new_cash = self.portfolio.cash - cost
        existing = self.portfolio.positions.get(symbol)
        if existing is None:
            new_amount, new_entry_price = amount, price
        else:
            total = existing.amount + amount
            new_entry_price = (existing.entry_price * existing.amount + price * amount) / total
            new_amount = total
        if not (
            _is_finite_number(cost) and cost > 0
            and _is_finite_number(new_cash)
            and _is_finite_number(new_amount) and new_amount > 0
            and _is_finite_number(new_entry_price) and new_entry_price > 0
        ):
            return None
        return new_cash, new_amount, new_entry_price

    def _compute_buy(
        self, symbol: str, order: object, requested: float, price_hint: float,
    ) -> tuple[float, float, float, tuple[float, float, float]]:
        """(price, base_amount, quote_amount, ledger) for booking `order`, or _UnusableFill. Pure.

        Item 4 (fees): a fee in the BASE asset comes out of the coins received (book the net
        amount); a fee in the QUOTE asset is debited on top of the cost (and logged in
        trades.csv's quote_amount, so reconstruct_cash replays it). No fee fields -> exactly
        the pre-#99 booking."""
        filled_price, filled_amount, _remaining = _parse_fill(order, requested, price_hint)
        base_fee, quote_fee = _order_fees(order, symbol)
        amount = filled_amount
        if base_fee:
            if base_fee < filled_amount:
                amount = filled_amount - base_fee
            else:
                logger.warning("buy %s: base fee %r >= filled %r -- ignored", symbol, base_fee, filled_amount)
        cost = filled_amount * filled_price
        if quote_fee:
            cost = cost + quote_fee
        ledger = self._buy_ledger(symbol, filled_price, amount, cost if (base_fee or quote_fee) else None)
        if ledger is None:
            raise _UnusableFill(f"fill {filled_amount!r} @ {filled_price!r} would make the ledger non-finite")
        return filled_price, amount, cost, ledger

    def _commit_buy(self, symbol: str, ledger: tuple[float, float, float]) -> None:
        new_cash, new_amount, new_entry_price = ledger
        self.portfolio.cash = new_cash
        existing = self.portfolio.positions.get(symbol)
        if existing is None:
            self.portfolio.positions[symbol] = Position(symbol, new_amount, new_entry_price)
        else:
            existing.entry_price = new_entry_price
            existing.amount = new_amount

    def buy(self, symbol: str, quote_amount: float, price_hint: float, order_ref: str | None = None) -> FillResult:
        if self.halted:
            raise LiveBrokerHalted(
                f"refusing to buy {symbol}: live broker is halted ({self._halt_reason or self._halt_marker_path}); "
                "no order placed"
            )
        if not (_is_finite_number(quote_amount) and quote_amount > 0):
            raise ValueError(f"refusing to buy {symbol}: quote_amount {quote_amount!r} is not a finite number > 0; no order placed")
        if not (_is_finite_number(price_hint) and price_hint > 0):
            raise ValueError(f"refusing to buy {symbol}: price_hint {price_hint!r} is not a finite number > 0; no order placed")
        base_amount_estimate = quote_amount / price_hint
        if not (_is_finite_number(base_amount_estimate) and base_amount_estimate > 0):
            raise ValueError(
                f"refusing to buy {symbol}: base amount {base_amount_estimate!r} "
                f"({quote_amount!r} / {price_hint!r}) is not a finite number > 0; no order placed"
            )
        # Dry-run the fallback booking BEFORE any order exists: if even the requested fill
        # can't be booked (cash or an existing position already corrupt), don't trade.
        if self._buy_ledger(symbol, float(price_hint), float(base_amount_estimate)) is None:
            raise ValueError(
                f"refusing to buy {symbol}: booking {base_amount_estimate!r} at {price_hint!r} would make "
                f"the ledger non-finite (cash={self.portfolio.cash!r}); no order placed"
            )
        self._check_can_place(symbol, "buy")
        # Write-ahead: no record on disk -> no order.
        coid = self._record_intent(
            symbol, "buy", order_ref or f"{symbol}|adhoc",
            requested_amount=float(base_amount_estimate), price_hint=float(price_hint),
            quote_amount=float(quote_amount),
        )

        response = self._place(symbol, "buy", base_amount_estimate, coid)
        # ---- an order now exists on the exchange: from here on, never raise without
        # ---- booking it or halting.
        self._mark_placed(coid, response)
        order, how = self._verify_response(symbol, "buy", coid, response, base_amount_estimate, price_hint,
                                           verify_unusable=True)
        if how == "not_executed":
            self._retire_unexecuted(coid, symbol, "buy", base_amount_estimate, order)
            raise LiveOrderNotExecuted(f"buy {symbol}: the exchange reports the order not executed; nothing booked")
        halt_reason = None
        unverified = how in ("unverified", "unusable")
        try:
            if how == "unusable":
                raise _UnusableFill(_unusable_reason(response, base_amount_estimate, price_hint))
            filled_price, filled_amount, quote, ledger = self._compute_buy(
                symbol, order, base_amount_estimate, price_hint
            )
            garbage = not isinstance(response, dict) or (
                response.get("filled") is not None and not _response_usable(response, base_amount_estimate, price_hint)
            )
            if how == "verified" and garbage:
                # The response itself was garbage (an ack-only one is not): still a halt.
                halt_reason = (f"buy {symbol}: unusable exchange response "
                               f"({_unusable_reason(response, base_amount_estimate, price_hint)}); booked the "
                               "fill VERIFIED by an exchange lookup")
            elif how == "unverified":
                halt_reason = (f"buy {symbol}: the exchange response carried no fill and the lookup failed; booked "
                               f"{filled_amount!r} @ {filled_price!r} UNVERIFIED -- verify against the exchange")
        except _UnusableFill as exc:
            # Conservative: assume the market order filled as requested (exactly the
            # risk-approved quote_amount), keep it tracked + stop-loss-protected, halt.
            filled_price, filled_amount = float(price_hint), float(base_amount_estimate)
            quote = filled_amount * filled_price
            ledger = self._buy_ledger(symbol, filled_price, filled_amount)
            unverified = True
            halt_reason = (f"buy {symbol}: unusable exchange response ({exc}); booked the REQUESTED fill "
                           f"{filled_amount!r} @ {filled_price!r} instead -- verify against the exchange")
            if ledger is None:  # unreachable: the same values passed the pre-order dry run
                raise LiveOrderUnreconciled(f"buy {symbol}: could not book even the requested fill; nothing booked")
        if halt_reason is not None:
            self._halt(
                halt_reason, symbol=symbol, side="buy", order=_order_summary(response), client_order_id=coid,
                requested_amount=filled_amount,
                bot_action=(
                    "Booked the fill and opened the plan so its stop-loss protects it; HALTED new buys and new "
                    "plans." + (" The fill is UNVERIFIED and re-checked every cycle." if unverified else "")
                ),
            )
        order_id = _order_id(order) or _order_id(response)
        result = FillResult(symbol, "buy", filled_price, filled_amount, quote, now_iso(), order_id)
        self._book(coid, result, lambda: self._commit_buy(symbol, ledger), None, halt_reason=halt_reason,
                   unverified=unverified)
        return result

    # ---------------------------------------------------------------- sell

    def _compute_sell(self, symbol: str, order: object, held: float, sent: float, price_hint: float) -> "_Sale":
        """How to book a sell response for a position of `held` of which `sent` was ordered,
        or _UnusableFill. Pure -- mutates nothing."""
        filled_price, filled_amount, remaining = _parse_fill(order, sent, price_hint)
        # Partial = the exchange says some is unfilled (remaining > 0), OR it filled less
        # than the amount IT echoes back as ordered. That echoed amount is already
        # truncated to the market's precision by ccxt/the exchange, so legitimate
        # rounding dust (held - amount, e.g. 0.9 SHIB) never counts as partial; only
        # float noise is tolerated between filled and amount. No echoed amount ->
        # remaining-only, exactly as before.
        ordered = order.get("amount")
        if ordered is not None and not (
            _is_finite_number(ordered) and 0 < ordered <= sent * (1 + _OVERFILL_TOLERANCE)
        ):
            raise _UnusableFill(f"amount={ordered!r} is not a finite number in (0, held {held!r}]")
        partial = (remaining is not None and remaining > 0) or (
            ordered is not None and filled_amount < ordered * (1 - _OVERFILL_TOLERANCE)
        ) or (
            # Item 5: `filled` reported but neither `amount` nor `remaining`: compare with what
            # was really sent (the market-precision truncation ccxt itself applies, else the raw
            # amount). Less than that is a partial, not a full close; precision dust is not.
            ordered is None and remaining is None and order.get("filled") is not None
            and filled_amount < self._sent_reference(symbol, sent) * (1 - _OVERFILL_TOLERANCE)
        ) or (
            # Item 3: the send was capped well below the tracked amount (balance mismatch):
            # whatever filled, the rest stays tracked for a human to reconcile.
            sent < held * (1 - _SELL_SHORTFALL_TOLERANCE)
        )
        proceeds = filled_amount * filled_price
        left = held - filled_amount
        base_fee, quote_fee = _order_fees(order, symbol)  # item 4
        if quote_fee and quote_fee < proceeds:
            proceeds = proceeds - quote_fee
        elif quote_fee:
            logger.warning("sell %s: quote fee %r >= proceeds %r -- ignored", symbol, quote_fee, proceeds)
        if base_fee and partial and base_fee < left:
            left = left - base_fee  # the fee came out of the coins still held
        new_cash = self.portfolio.cash + proceeds
        if not (_is_finite_number(proceeds) and proceeds > 0):
            raise _UnusableFill(f"proceeds {proceeds!r} are not a finite number > 0")
        if _is_finite_number(self.portfolio.cash) and not _is_finite_number(new_cash):
            raise _UnusableFill(f"cash would become {new_cash!r}")
        if partial and not (_is_finite_number(left) and left > 0):
            raise _UnusableFill(f"remaining={remaining!r} reported but filled {filled_amount!r} >= held {held!r}")
        return _Sale(filled_price, filled_amount, proceeds, new_cash, left, partial)

    def _apply_sell(self, symbol: str, sale: "_Sale") -> None:
        """Memory only (trades.csv is written first by _book). Cash is moved by the proceeds,
        not set from a snapshot, so a re-sync after a failed write stays correct."""
        self.portfolio.cash = self.portfolio.cash + sale.proceeds
        if sale.partial:
            self.portfolio.positions[symbol].amount = sale.left
        else:
            del self.portfolio.positions[symbol]

    def _sent_reference(self, symbol: str, sent: float) -> float:
        """`sent` truncated to the market's amount precision (what ccxt actually sent), or
        `sent` itself when precision is unavailable or nonsensical."""
        try:
            precise = self.exchange.amount_to_precision(symbol, sent)
        except Exception:  # noqa: BLE001 -- no market info: compare with the raw amount (stricter)
            return sent
        if _is_finite_number(precise) and 0 < precise <= sent * (1 + _OVERFILL_TOLERANCE):
            return float(precise)
        return sent

    def _check_exit_size(self, symbol: str, sent: float, price_hint: float) -> None:
        """Review F8c: never send a sell the exchange must refuse as dust (below its minimum
        amount / cost, or zero after precision truncation): alert 'cannot exit' instead."""
        try:
            minimum = self.exchange.min_order_amount(symbol)
        except Exception:  # noqa: BLE001 -- unknown minimums: the exchange is the arbiter
            minimum = None
        try:
            min_cost = self.exchange.min_order_cost(symbol)
        except Exception:  # noqa: BLE001
            min_cost = None
        try:
            truncated = self.exchange.amount_to_precision(symbol, sent)
        except Exception:  # noqa: BLE001
            truncated = None
        why = None
        if _is_finite_number(truncated) and truncated == 0:
            why = f"{sent!r} truncates to 0 at the market's precision"
        elif _is_finite_number(minimum) and minimum > 0 and sent < minimum:
            why = f"{sent!r} is below the exchange minimum amount {minimum!r}"
        elif _is_finite_number(min_cost) and min_cost > 0 and sent * price_hint < min_cost:
            why = f"{sent!r} x {price_hint!r} is below the exchange minimum cost {min_cost!r}"
        if why is None:
            return
        self._exit_failed(symbol, sent, None, f"cannot exit: {why}",
                          "Placed NO sell (the exchange would reject it as dust); the position stays OPEN.")
        raise LiveExitBlocked(f"sell {symbol}: {why}; no order placed")

    def _free_balance(self, symbol: str) -> float | None:
        """Free balance of `symbol`'s base asset, or None when unknown (error, not reported,
        non-finite, negative)."""
        base = symbol.split("/")[0]
        try:
            free = self.exchange.fetch_free_balance(base)
        except Exception as exc:  # noqa: BLE001 -- unknown balance: fall back to the tracked amount
            logger.warning("could not read the free %s balance (%s); selling the tracked amount", base,
                           type(exc).__name__)
            return None
        if free is None or not (_is_finite_number(free) and free >= 0):
            if free is not None:
                logger.warning("free %s balance %r is unusable; selling the tracked amount", base, free)
            return None
        return float(free)

    def _capped_sell_amount(self, symbol: str, held: float) -> float:
        """Issue #99 item 3: never sell more than the account really holds (nor more than we
        track). Design note §3: unknown balance -> held (the exchange is the arbiter);
        a shortfall within _SELL_SHORTFALL_TOLERANCE is fees/rounding; a bigger one halts and
        alerts BEFORE the (still protective) capped sell; nothing free -> no order at all."""
        free = self._free_balance(symbol)
        if free is None or free >= held:
            return held
        base = symbol.split("/")[0]
        if free <= 0:
            # Review (RO f6): a 0 could be a misreport (an asset held under another code, a
            # stale balance). The protective sell is still SENT for the tracked amount -- the
            # exchange is the arbiter: with truly nothing there it rejects it, nothing placed --
            # but the mismatch halts and alerts first.
            self._halt(
                f"sell {symbol}: tracked {held!r} {base} but the exchange reports a free balance of {free!r}; "
                "sending the protective sell for the tracked amount anyway",
                symbol=symbol, side="sell", requested_amount=held,
                bot_action=(
                    "Sent the stop-loss/take-profit sell for the TRACKED amount (the exchange rejects it if the "
                    "coins are really not there); HALTED new buys and new plans."
                ),
            )
            return held
        if free < held * (1 - _SELL_SHORTFALL_TOLERANCE):
            self._halt(
                f"sell {symbol}: tracked {held!r} {base} but only {free!r} is free on the exchange; selling "
                f"{free!r} and keeping the difference tracked",
                symbol=symbol, side="sell", requested_amount=free,
                bot_action=(
                    f"Sold only the free {free!r} (protective exit, capped); the shortfall stays OPEN in the ledger "
                    "for a human to reconcile. HALTED new buys and new plans."
                ),
            )
        return free

    def sell(
        self, symbol: str, price_hint: float, order_ref: str | None = None, exit_reason: str | None = None,
    ) -> FillResult:
        if symbol not in self.portfolio.positions and self.order_in_flight(symbol):
            # e.g. a booked exit whose plan-store update failed: the record explains it, not a KeyError
            self._check_can_place(symbol, "sell")
        pos = self.portfolio.positions[symbol]
        if not (_is_finite_number(pos.amount) and pos.amount > 0):
            raise ValueError(f"refusing to sell {symbol}: held amount {pos.amount!r} is not a finite number > 0; no order placed")
        if not (_is_finite_number(price_hint) and price_hint > 0):
            raise ValueError(f"refusing to sell {symbol}: price_hint {price_hint!r} is not a finite number > 0; no order placed")
        held = pos.amount
        try:
            self._check_can_place(symbol, "sell")
        except LiveBrokerHalted:
            self._exit_failed(symbol, held, None, "an earlier order on this symbol is unresolved",
                              "Placed NO sell (a second order could duplicate the unresolved one); the reconcile "
                              "retries every cycle.")
            raise
        sent = self._capped_sell_amount(symbol, held)
        self._check_exit_size(symbol, sent, price_hint)
        coid = self._record_intent(
            symbol, "sell", order_ref or f"{symbol}|adhoc",
            requested_amount=float(sent), held_at_send=float(held), price_hint=float(price_hint),
            exit_reason=exit_reason,
        )

        response = self._place(symbol, "sell", sent, coid)
        # ---- an order now exists on the exchange.
        self._mark_placed(coid, response)
        order, how = self._verify_response(symbol, "sell", coid, response, sent, price_hint, verify_unusable=False)
        if how == "not_executed":
            self._retire_unexecuted(coid, symbol, "sell", sent, order)
            raise LiveOrderNotExecuted(f"sell {symbol}: the exchange reports the order not executed; nothing booked")
        try:
            if how == "unusable":
                raise _UnusableFill(_unusable_reason(response, sent, price_hint))
            sale = self._compute_sell(symbol, order, held, sent, price_hint)
        except _UnusableFill as exc:
            # Conservative: the position stays exactly as it was (plan stays OPEN), nothing is
            # credited, new buys halt. The record becomes `unusable` -- consuming the attempt in
            # the same write (review F2) -- and blocks every order on the symbol until the
            # reconcile resolves it from the exchange's own record.
            self._set_entry(coid, consume_attempt=True, state="unusable")
            self._halt(
                f"sell {symbol}: unusable exchange response ({exc}); nothing booked, "
                f"position of {held!r} kept -- verify against the exchange",
                symbol=symbol, side="sell", order=_order_summary(response), client_order_id=coid,
                requested_amount=sent,
                bot_action=(
                    "Booked NOTHING; the position stays tracked; no other order on this symbol until the reconcile "
                    "finds this order on the exchange (every cycle). HALTED new buys and new plans."
                ),
            )
            raise LiveOrderUnreconciled(
                f"sell {symbol}: exchange response unusable ({exc}); nothing booked, position kept, new buys halted"
            ) from exc

        halt_reason = None
        if how == "unverified":
            halt_reason = (f"sell {symbol}: the exchange response carried no fill and the lookup failed; booked "
                           f"{sale.filled!r} @ {sale.price!r} UNVERIFIED -- verify against the exchange")
        elif sale.partial:
            halt_reason = f"sell {symbol}: PARTIAL fill {sale.filled!r} of {held!r}; remainder {sale.left!r} kept open"
        if halt_reason is not None:  # review F4: halt FIRST (durably), then book
            self._halt(
                halt_reason, symbol=symbol, side="sell", order=_order_summary(response), client_order_id=coid,
                requested_amount=sent,
                bot_action=(
                    f"Booked the filled part; the remainder {sale.left!r} stays OPEN with its stop-loss/target "
                    "and is persisted to the plan store. HALTED new buys and new plans."
                    if sale.partial else "Booked the fill UNVERIFIED (re-checked every cycle). HALTED new buys."
                ),
            )
        order_id = _order_id(order) or _order_id(response)
        result = FillResult(symbol, "sell", sale.price, sale.filled, sale.proceeds, now_iso(), order_id)
        self._book(coid, result, lambda: self._apply_sell(symbol, sale), sale.left if sale.partial else None,
                   halt_reason=halt_reason, unverified=how == "unverified")
        if sale.partial:
            raise LivePartialFill(
                f"sell {symbol} partially filled ({sale.filled!r} of {held!r}); booked, remainder "
                f"{sale.left!r} stays open for the monitor to exit",
                result,
                remaining_amount=sale.left,
            )
        return result

    # ---------------------------------------------------------------- trade log

    def _log_live_trade(self, result: FillResult, coid: str | None = None) -> None:
        """The CSV row, written (and fsync'd) BEFORE the in-memory ledger moves. order_id is
        the exchange id, or `coid:<clientOrderId>` when the exchange gave none (never the
        string "None"): the reconcile's dedup key. On failure: halt and re-raise; the record
        stays unresolved and memory untouched, so the reconcile books it exactly once."""
        try:
            self.portfolio.log_trade(
                {
                    "timestamp": result.timestamp,
                    "mode": "live",
                    "symbol": result.symbol,
                    "side": result.side,
                    "price": result.price,
                    "base_amount": result.base_amount,
                    "quote_amount": result.quote_amount,
                    "order_id": _trade_key(result, coid),
                }
            )
        except Exception as exc:
            self._halt(
                f"{result.side} {result.symbol}: filled, but writing the trade log failed ({type(exc).__name__}); "
                "nothing booked in memory -- the reconcile books it once the disk works",
                symbol=result.symbol, side=result.side, client_order_id=coid, requested_amount=result.base_amount,
            )
            raise


def _trade_key(result: "FillResult", coid: str | None) -> str | None:
    if result.order_id:
        return result.order_id
    return f"coid:{coid}" if coid else None


def _order_id(order: object) -> str | None:
    if not isinstance(order, dict) or order.get("id") is None:
        return None
    return str(order.get("id"))


def _terminal_zero_fill(order: object) -> bool:
    return (
        isinstance(order, dict)
        and order.get("status") in ("canceled", "expired", "rejected")
        and _is_finite_number(order.get("filled"))
        and order.get("filled") == 0
    )


def _response_usable(order: object, requested: float, price_hint: float) -> bool:
    try:
        _parse_fill(order, requested, price_hint)
        return True
    except _UnusableFill:
        return False


def _unusable_reason(order: object, requested: float, price_hint: float) -> str:
    try:
        _parse_fill(order, requested, price_hint)
    except _UnusableFill as exc:
        return str(exc)
    return "no usable fill"


@dataclass
class _Sale:
    price: float
    filled: float
    proceeds: float
    new_cash: float
    left: float
    partial: bool


def _order_fees(order: object, symbol: str) -> tuple[float, float]:
    """(fee in the base asset, fee in the quote asset) reported by a ccxt order (`fees`
    list, else `fee`). A fee in another currency (e.g. BNB) or a malformed one is ignored
    with a warning -- never a reason to halt; the sell balance cap absorbs the difference."""
    if not isinstance(order, dict):
        return 0.0, 0.0
    fees = order.get("fees")
    if not (isinstance(fees, list) and fees):
        fee = order.get("fee")
        fees = [fee] if isinstance(fee, dict) else []
    base, _, rest = symbol.partition("/")
    quote = rest.split(":")[0]
    base_fee = quote_fee = 0.0
    for fee in fees:
        if not isinstance(fee, dict) or fee.get("cost") is None:
            continue
        cost, currency = fee.get("cost"), fee.get("currency")
        if not (_is_finite_number(cost) and cost >= 0 and isinstance(currency, str)):
            logger.warning("%s: unusable fee %r ignored", symbol, fee)
        elif currency == base:
            base_fee += float(cost)
        elif currency == quote:
            quote_fee += float(cost)
        else:
            logger.warning("%s: fee in %s (not %s/%s) not booked", symbol, currency, base, quote)
    return base_fee, quote_fee


def _same_amount(a: object, b: float) -> bool:
    return _is_finite_number(a) and abs(float(a) - b) <= max(abs(b), 1e-300) * _OVERFILL_TOLERANCE


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


_RECONCILE_ACTIONS = {
    "booked": "The fill was already in the ledger; re-synced the plan store and cleared the record. Buys stay halted.",
    "filled": "Found the order filled on the exchange; booked it and updated the plan (its stop-loss now protects "
              "it). Buys stay halted.",
    "partial": "Found the order PARTIALLY filled; booked the filled part. Buys stay halted.",
    "not_executed": "Found the order on the exchange with nothing filled; cleared the record. Buys stay halted.",
    "never_placed": "The order is not on the exchange (never placed); cleared the record; a later retry reuses the "
                    "same clientOrderId. Buys stay halted.",
    "unknown": "Could NOT determine the outcome; booked nothing, kept the record, no other order on this symbol. "
               "Retrying the lookup every cycle. Buys stay halted.",
    "anomaly": "The exchange's record contradicts the tracked position; booked nothing, kept the record. "
               "Buys stay halted.",
}
