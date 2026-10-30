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
  disk even if the halt marker could not be written); intent/placed also blocks sells on
  that symbol (could double-sell). A booked entry left over from before a restart blocks
  every order on its symbol until it is settled. An unreadable state file blocks EVERY
  order and is never overwritten.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ccxt

from src.exchange import ExchangeClient
from src.portfolio import Portfolio, Position, _is_finite_number, now_iso

logger = logging.getLogger(__name__)

HALT_MARKER_NAME = "live_broker_halt.json"
ORDER_STATE_NAME = "live_order_state.json"

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
# Entries whose outcome is unknown: they also block sells on their symbol (a re-sell could double-sell).
_SYMBOL_BLOCKING_STATES = ("intent", "placed")


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
    return {"version": 1, "inflight": {}, "attempts": {}}


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
    return {"version": 1, "inflight": inflight, "attempts": attempts}

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
        # Halt state (see module docstring). Set up only AFTER the gate above passed.
        self._halt_reason: str | None = None
        self._alerts: list[dict] = []        # drained by bot.py (pop_alerts) and sent to Telegram
        self._alert_keys: set = set()        # one alert per distinct event per process
        trade_log_path = getattr(portfolio, "trade_log_path", None)
        self._halt_marker_path: Path | None = (
            Path(trade_log_path).parent / HALT_MARKER_NAME if trade_log_path is not None else None
        )
        # Write-ahead order record (module docstring). Without a trade_log_path (tests only --
        # build_components always sets one) it lives in memory, exactly like the halt latch.
        self._state_path: Path | None = (
            Path(trade_log_path).parent / ORDER_STATE_NAME if trade_log_path is not None else None
        )
        self._state: dict = _empty_order_state()
        self._state_error: str | None = None
        self._session_booked: set[str] = set()      # booked by THIS process, not yet seen by a reconcile pass
        self._awaiting_settle: dict[str, str] = {}  # symbol -> clientOrderId booked, waiting for bot.py's settle()
        self._load_order_state()
        if self._halt_marker_path is not None and self._halt_marker_path.exists():
            logger.critical(
                "live broker HALTED at startup: %s exists -- new buys are refused until a human "
                "reconciles the exchange account and deletes it", self._halt_marker_path,
            )
        if self._state_error is not None:
            self._halt(
                f"order state file {self._state_path} is unreadable ({self._state_error}); every order is "
                "refused until a human repairs it",
                bot_action="Every order (buys AND sells) is refused; the file is left untouched as evidence.",
                operator_check=(
                    "Check every open position and recent order on the exchange, repair or remove "
                    f"{ORDER_STATE_NAME} by hand, then delete {HALT_MARKER_NAME} and restart."
                ),
            )
        unresolved = self._unresolved_entries()
        if unresolved:
            for coid, entry in sorted(unresolved.items()):
                self._halt(
                    f"startup: in-flight {entry['side']} {entry['symbol']} order {coid} from before the restart "
                    "is unresolved; its outcome is unknown until reconciled",
                    symbol=entry["symbol"], side=entry["side"], client_order_id=coid,
                    requested_amount=entry.get("requested_amount"),
                )

    # ---------------------------------------------------------------- alerts

    def _queue_alert(self, key: object, **alert: object) -> None:
        if key in self._alert_keys:
            return
        self._alert_keys.add(key)
        self._alerts.append(alert)

    def pop_alerts(self) -> list[dict]:
        """Alerts raised since the last call (bot.py formats, escapes, redacts and sends them)."""
        alerts, self._alerts = self._alerts, []
        return alerts

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
        )

    def _halt(
        self, reason: str, *, bot_action: str | None = None, operator_check: str | None = None, **details: object,
    ) -> None:
        """Latch the halt in memory first (can't fail), queue an alert, then best-effort persist it."""
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
        if self._halt_marker_path is None:
            return
        try:
            payload = {"halted_at": now_iso(), "reason": reason, **{k: repr(v) for k, v in details.items()}}
            _atomic_write_json(self._halt_marker_path, payload)
        except Exception:  # noqa: BLE001 -- the in-memory latch above still holds for this process
            logger.exception("could not persist live broker halt marker %s", self._halt_marker_path)

    def _halt_is_durable(self) -> bool:
        """True when a restart would still be halted by the marker (or there is no disk at all)."""
        return self._halt_marker_path is None or self._halt_marker_path.exists()

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
                f"refusing to {side} {symbol}: the order state file is unreadable ({self._state_error}); no order placed"
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
                    f"refusing to {side} {symbol}: order {coid} ({entry['side']}) has an unknown outcome; "
                    "a second order could duplicate it; no order placed"
                )
            if state == "booked" and coid not in self._session_booked:
                raise LiveBrokerHalted(
                    f"refusing to {side} {symbol}: booked order {coid} is awaiting reconcile of the plan store; "
                    "no order placed"
                )

    def _record_intent(self, symbol: str, side: str, plan_ref: str, **fields: object) -> str:
        """Write-ahead: persist the order record and return its clientOrderId, or halt and
        raise LiveOrderStateError -- in which case NO order may be placed."""
        attempt = int(self._state["attempts"].get(self._attempt_key(plan_ref, side), 0))
        coid = client_order_id(plan_ref, side, attempt)
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
        """A definite rejection: nothing was placed, the id was never used (attempt NOT consumed)."""
        try:
            self._mutate_state(lambda s: s["inflight"].pop(coid, None))
        except Exception:  # noqa: BLE001 -- the intent stays; a reconcile will find nothing and clear it
            logger.exception("could not clear the order record %s after a definite rejection", coid)

    def _set_entry(self, coid: str, **fields: object) -> bool:
        """Best-effort update of an existing record (False + logged on failure)."""
        def change(s: dict) -> None:
            s["inflight"][coid].update(fields)

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
    ) -> bool:
        """The ledger + trades.csv now hold this fill: record it and consume the attempt (the id
        is used). On failure, halt -- the trades.csv dedup keeps a reconcile from booking it twice.

        in_session=False (reconcile): the record blocks its symbol until bot.py settles it."""
        entry = self._state["inflight"][coid]
        key = self._attempt_key(entry["plan_ref"], entry["side"])
        fill = {
            "price": result.price, "base_amount": result.base_amount, "quote_amount": result.quote_amount,
            "timestamp": result.timestamp, "order_id": result.order_id,
        }

        def change(s: dict) -> None:
            s["inflight"][coid].update(state="booked", fill=fill, remaining_amount=remaining_amount)
            s["attempts"][key] = max(int(s["attempts"].get(key, 0)), int(entry["attempt"]) + 1)

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
        unresolved one. On failure, halts and keeps the record (the next reconcile retries)."""
        entry = self._state["inflight"].get(coid)
        if entry is None:
            return True
        if entry["state"] != "booked":
            logger.error("refusing to settle order record %s in state %s", coid, entry["state"])
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
                raise  # the exchange answered and rejected it: nothing was placed
            self._halt(
                f"{side} {symbol}: create_market_order raised {type(exc).__name__}; "
                "the order may or may not be live on the exchange",
                symbol=symbol, side=side, requested_amount=amount, error=exc, client_order_id=coid,
                bot_action=(
                    "Booked NOTHING and HALTED: new buys and new plans are refused. The bot will look the order "
                    "up by its clientOrderId at the start of every cycle and book it if it filled. Until then no "
                    f"other order is placed on {symbol}."
                ),
            )
            raise LiveOrderUnreconciled(
                f"{side} {symbol}: order outcome unknown ({type(exc).__name__}: {exc}); nothing booked, new buys halted"
            ) from exc

    # ---------------------------------------------------------------- reconcile (issue #99 item 2)

    def reconcile(self, now: datetime | None = None) -> list[ReconcileResult]:
        """Resolve every in-flight order record (decision table: docs/live_readiness_design.md
        §2). Called by bot.py at the top of every cycle, i.e. before any order of that cycle.
        Never resumes buys: a halt stays until a human deletes the marker. No record -> no
        exchange call at all."""
        self._session_booked.clear()  # from now on, an unsettled booked record blocks its symbol
        if self._state_error is not None or not self._state["inflight"]:
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
                bot_action=_RECONCILE_ACTIONS.get(result.outcome, ""),
                operator_check=self._operator_check(result.symbol, result.side, coid),
            )
            results.append(result)
        return results

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
        if status not in ("closed", "canceled", "expired", "rejected"):
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
        booked = self._trade_log_fill(entry, order_id)
        if booked is not None:  # R4: already in trades.csv -- never book it twice
            remaining = None
            if entry["side"] == "sell":
                left = float(entry["held_at_send"]) - booked.base_amount
                remaining = left if left > float(entry["held_at_send"]) * _SELL_SHORTFALL_TOLERANCE else None
            if not self._mark_booked(entry["client_order_id"], booked, remaining, in_session=False):
                return self._unknown(entry, "found in trades.csv but the record could not be marked booked")
            return self._restore_booked(self._state["inflight"][entry["client_order_id"]])
        if entry["side"] == "buy":
            return self._book_found_buy(entry, order, order_id, float(filled))
        return self._book_found_sell(entry, order, order_id, float(filled))

    def _trade_log_fill(self, entry: dict, order_id: str | None) -> FillResult | None:
        """The trades.csv row already booked for this exchange order id, if any."""
        path = getattr(self.portfolio, "trade_log_path", None)
        if order_id is None or path is None or not Path(path).exists():
            return None
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("order_id") == order_id and row.get("symbol") == entry["symbol"] \
                        and row.get("side") == entry["side"]:
                    return FillResult(entry["symbol"], entry["side"], float(row["price"]), float(row["base_amount"]),
                                      float(row["quote_amount"]), row.get("timestamp") or now_iso(), order_id)
        return None

    def _book_found_buy(self, entry: dict, order: dict, order_id: str | None, filled: float) -> ReconcileResult:
        symbol = entry["symbol"]
        try:
            price, amount, quote, ledger = self._compute_buy(symbol, order, float(entry["requested_amount"]),
                                                             float(entry["price_hint"]))
        except _UnusableFill as exc:
            return self._unknown(entry, f"found filled but its fields are unusable ({exc})")
        self._commit_buy(symbol, ledger)
        result = FillResult(symbol, "buy", price, amount, quote, now_iso(), order_id)
        self._log_live_trade(result)
        if not self._mark_booked(entry["client_order_id"], result, None, in_session=False):
            return self._unknown(entry, "booked, but the record could not be marked booked")
        outcome = "filled" if filled >= float(entry["requested_amount"]) * (1 - _OVERFILL_TOLERANCE) else "partial"
        return self._result(entry, outcome, f"found {order.get('status')} on the exchange, filled {filled!r}; booked",
                            fill=result)

    def _book_found_sell(self, entry: dict, order: dict, order_id: str | None, filled: float) -> ReconcileResult:
        symbol = entry["symbol"]
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
        result = self._commit_sell(symbol, sale, order_id)
        if not self._mark_booked(entry["client_order_id"], result, sale.left if sale.partial else None,
                                 in_session=False):
            return self._unknown(entry, "booked, but the record could not be marked booked")
        return self._result(
            entry, "partial" if sale.partial else "filled",
            f"found {order.get('status')} on the exchange, filled {filled!r}; booked",
            fill=result, remaining_amount=sale.left if sale.partial else None,
        )

    def _restore_booked(self, entry: dict) -> ReconcileResult:
        """A booked record: the ledger and trades.csv already hold the fill; only the plan store
        may lag. After a restart the portfolio was rebuilt from the store, so re-apply the
        position side of the fill WITHOUT touching cash (reconstruct_cash replays trades.csv)."""
        fill = entry["fill"]
        symbol = entry["symbol"]
        result = FillResult(symbol, entry["side"], float(fill["price"]), float(fill["base_amount"]),
                            float(fill["quote_amount"]), fill.get("timestamp") or now_iso(), fill.get("order_id"))
        remaining = entry.get("remaining_amount")
        pos = self.portfolio.positions.get(symbol)
        held = entry.get("held_at_send")
        if entry["side"] == "buy":
            if pos is None:
                self.portfolio.positions[symbol] = Position(symbol, result.base_amount, result.price)
        elif pos is not None and held is not None and _same_amount(pos.amount, float(held)):
            # Still the pre-sell amount (the store never saw the sale): apply it now.
            if remaining is not None:
                pos.amount = float(remaining)
            else:
                del self.portfolio.positions[symbol]
        return self._result(entry, "booked", "already booked; plan store re-synced", fill=result,
                            remaining_amount=None if remaining is None else float(remaining))

    # ---------------------------------------------------------------- buy

    def _buy_ledger(self, symbol: str, price: float, amount: float) -> tuple[float, float, float] | None:
        """(new_cash, new_amount, new_entry_price) for booking `amount` at `price`, or None
        if any of it would be non-finite / non-positive. Pure -- mutates nothing. Same
        arithmetic, in the same order, as the pre-hardening code (byte-identical ledger)."""
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
        """(price, base_amount, quote_amount, ledger) for booking `order`, or _UnusableFill. Pure."""
        filled_price, filled_amount, _remaining = _parse_fill(order, requested, price_hint)
        ledger = self._buy_ledger(symbol, filled_price, filled_amount)
        if ledger is None:
            raise _UnusableFill(f"fill {filled_amount!r} @ {filled_price!r} would make the ledger non-finite")
        return filled_price, filled_amount, filled_amount * filled_price, ledger

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
        # Write-ahead: no record on disk -> no order (raises LiveOrderStateError).
        coid = self._record_intent(
            symbol, "buy", order_ref or f"{symbol}|adhoc",
            requested_amount=float(base_amount_estimate), price_hint=float(price_hint),
            quote_amount=float(quote_amount),
        )

        order = self._place(symbol, "buy", base_amount_estimate, coid)
        # ---- an order now exists on the exchange: from here on, never raise without
        # ---- booking it or halting.
        self._mark_placed(coid, order)
        try:
            filled_price, filled_amount, quote, ledger = self._compute_buy(
                symbol, order, base_amount_estimate, price_hint
            )
        except _UnusableFill as exc:
            # Conservative: assume the market order filled as requested (exactly the
            # risk-approved quote_amount), keep it tracked + stop-loss-protected, halt.
            filled_price, filled_amount = float(price_hint), float(base_amount_estimate)
            quote = filled_amount * filled_price
            ledger = self._buy_ledger(symbol, filled_price, filled_amount)
            self._halt(
                f"buy {symbol}: unusable exchange response ({exc}); booked the REQUESTED fill "
                f"{filled_amount!r} @ {filled_price!r} instead -- verify against the exchange",
                symbol=symbol, side="buy", order=_order_summary(order), client_order_id=coid,
                requested_amount=filled_amount,
                bot_action=(
                    "Booked the REQUESTED amount (conservative) and opened the plan so its stop-loss protects it; "
                    "HALTED new buys and new plans."
                ),
            )
            if ledger is None:  # unreachable: the same values passed the pre-order dry run
                raise LiveOrderUnreconciled(f"buy {symbol}: could not book even the requested fill; nothing booked")
        self._commit_buy(symbol, ledger)
        result = FillResult(
            symbol, "buy", filled_price, filled_amount, quote,
            now_iso(), str(order.get("id")) if isinstance(order, dict) else None,
        )
        self._log_live_trade(result)
        self._mark_booked(coid, result, None)
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
        )
        proceeds = filled_amount * filled_price
        new_cash = self.portfolio.cash + proceeds
        left = held - filled_amount
        if not (_is_finite_number(proceeds) and proceeds > 0):
            raise _UnusableFill(f"proceeds {proceeds!r} are not a finite number > 0")
        if _is_finite_number(self.portfolio.cash) and not _is_finite_number(new_cash):
            raise _UnusableFill(f"cash would become {new_cash!r}")
        if partial and not (_is_finite_number(left) and left > 0):
            raise _UnusableFill(f"remaining={remaining!r} reported but filled {filled_amount!r} >= held {held!r}")
        return _Sale(filled_price, filled_amount, proceeds, new_cash, left, partial)

    def _commit_sell(self, symbol: str, sale: "_Sale", order_id: str | None) -> FillResult:
        self.portfolio.cash = sale.new_cash
        if sale.partial:
            self.portfolio.positions[symbol].amount = sale.left
        else:
            del self.portfolio.positions[symbol]
        result = FillResult(symbol, "sell", sale.price, sale.filled, sale.proceeds, now_iso(), order_id)
        self._log_live_trade(result)
        return result

    def sell(
        self, symbol: str, price_hint: float, order_ref: str | None = None, exit_reason: str | None = None,
    ) -> FillResult:
        pos = self.portfolio.positions[symbol]
        if not (_is_finite_number(pos.amount) and pos.amount > 0):
            raise ValueError(f"refusing to sell {symbol}: held amount {pos.amount!r} is not a finite number > 0; no order placed")
        if not (_is_finite_number(price_hint) and price_hint > 0):
            raise ValueError(f"refusing to sell {symbol}: price_hint {price_hint!r} is not a finite number > 0; no order placed")
        held = pos.amount
        self._check_can_place(symbol, "sell")
        sent = held
        coid = self._record_intent(
            symbol, "sell", order_ref or f"{symbol}|adhoc",
            requested_amount=float(sent), held_at_send=float(held), price_hint=float(price_hint),
            exit_reason=exit_reason,
        )

        order = self._place(symbol, "sell", sent, coid)
        # ---- an order now exists on the exchange.
        self._mark_placed(coid, order)
        try:
            sale = self._compute_sell(symbol, order, held, sent, price_hint)
        except _UnusableFill as exc:
            # Conservative: the position stays exactly as it was (plan stays OPEN, the
            # monitor retries the exit), nothing is credited, new buys halt. The order
            # record becomes `unusable`: still reconciled, still halting buys, but it does
            # not block the re-sell (see module docstring).
            self._set_entry(coid, state="unusable")
            self._halt(
                f"sell {symbol}: unusable exchange response ({exc}); nothing booked, "
                f"position of {held!r} kept -- verify against the exchange",
                symbol=symbol, side="sell", order=_order_summary(order), client_order_id=coid,
                requested_amount=sent,
                bot_action=(
                    "Booked NOTHING; the position stays tracked and its exit is retried next cycle "
                    "(capped at the free balance). HALTED new buys and new plans."
                ),
            )
            raise LiveOrderUnreconciled(
                f"sell {symbol}: exchange response unusable ({exc}); nothing booked, position kept, new buys halted"
            ) from exc

        result = self._commit_sell(symbol, sale, str(order.get("id")))
        self._mark_booked(coid, result, sale.left if sale.partial else None)
        if sale.partial:
            self._halt(
                f"sell {symbol}: PARTIAL fill {sale.filled!r} of {held!r}; remainder {sale.left!r} kept open",
                symbol=symbol, side="sell", order=_order_summary(order), client_order_id=coid,
                requested_amount=sent,
                bot_action=(
                    f"Booked the filled part; the remainder {sale.left!r} stays OPEN with its stop-loss/target "
                    "and is persisted to the plan store. HALTED new buys and new plans."
                ),
            )
            raise LivePartialFill(
                f"sell {symbol} partially filled ({sale.filled!r} of {held!r}); booked, remainder "
                f"{sale.left!r} stays open for the monitor to exit",
                result,
                remaining_amount=sale.left,
            )
        return result

    # ---------------------------------------------------------------- trade log

    def _log_live_trade(self, result: FillResult) -> None:
        """Same CSV row as before the hardening. The order already happened and the
        in-memory ledger already reflects it; if persisting the row fails, halt (the
        trade log is what reconstruct_cash replays on restart) and re-raise as before."""
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
                    "order_id": result.order_id,
                }
            )
        except Exception as exc:
            self._halt(
                f"{result.side} {result.symbol}: filled and booked in memory but writing the trade log failed",
                fill=result, error=exc, symbol=result.symbol, side=result.side,
            )
            raise


@dataclass
class _Sale:
    price: float
    filled: float
    proceeds: float
    new_cash: float
    left: float
    partial: bool


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
