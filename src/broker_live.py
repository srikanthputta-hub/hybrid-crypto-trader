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
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import ccxt

from src.exchange import ExchangeClient
from src.portfolio import Portfolio, Position, _is_finite_number, now_iso

logger = logging.getLogger(__name__)

HALT_MARKER_NAME = "live_broker_halt.json"
ORDER_STATE_NAME = "live_order_state.json"

_CLIENT_ID_PREFIX = "hct"
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
            )
        unresolved = self._unresolved_entries()
        if unresolved:
            self._halt(
                f"startup: {len(unresolved)} in-flight order(s) from before the restart are unresolved "
                f"({', '.join(sorted(unresolved))}); their outcome is unknown until reconciled",
                client_order_ids=sorted(unresolved),
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

    def _halt(self, reason: str, **details: object) -> None:
        """Latch the halt in memory first (can't fail), then best-effort persist it."""
        self._halt_reason = reason
        logger.critical("LIVE BROKER HALTED -- new buys refused until a human reconciles: %s %s", reason, details)
        if self._halt_marker_path is None:
            return
        try:
            payload = {"halted_at": now_iso(), "reason": reason, **{k: repr(v) for k, v in details.items()}}
            _atomic_write_json(self._halt_marker_path, payload)
        except Exception:  # noqa: BLE001 -- the in-memory latch above still holds for this process
            logger.exception("could not persist live broker halt marker %s", self._halt_marker_path)

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
                symbol=symbol, side=side, client_order_id=coid,
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

    def _mark_booked(self, coid: str, result: "FillResult", remaining_amount: float | None) -> None:
        """The ledger + trades.csv now hold this fill: record it and consume the attempt (the id
        is used). On failure, halt -- the trades.csv dedup keeps a reconcile from booking it twice."""
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
            )
            return
        self._session_booked.add(coid)
        self._awaiting_settle[entry["symbol"]] = coid

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
            )
            raise LiveOrderUnreconciled(
                f"{side} {symbol}: order outcome unknown ({type(exc).__name__}: {exc}); nothing booked, new buys halted"
            ) from exc

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
        problem: str | None = None
        try:
            filled_price, filled_amount, _remaining = _parse_fill(order, base_amount_estimate, price_hint)
            ledger = self._buy_ledger(symbol, filled_price, filled_amount)
            if ledger is None:
                problem = f"fill {filled_amount!r} @ {filled_price!r} would make the ledger non-finite"
        except _UnusableFill as exc:
            problem = str(exc)
        if problem is not None:
            # Conservative: assume the market order filled as requested (exactly the
            # risk-approved quote_amount), keep it tracked + stop-loss-protected, halt.
            filled_price, filled_amount = float(price_hint), float(base_amount_estimate)
            ledger = self._buy_ledger(symbol, filled_price, filled_amount)
            self._halt(
                f"buy {symbol}: unusable exchange response ({problem}); booked the REQUESTED fill "
                f"{filled_amount!r} @ {filled_price!r} instead -- verify against the exchange",
                symbol=symbol, order=_order_summary(order), client_order_id=coid,
            )
            if ledger is None:  # unreachable: the same values passed the pre-order dry run
                raise LiveOrderUnreconciled(f"buy {symbol}: could not book even the requested fill; nothing booked")
        new_cash, new_amount, new_entry_price = ledger

        self.portfolio.cash = new_cash
        existing = self.portfolio.positions.get(symbol)
        if existing is None:
            self.portfolio.positions[symbol] = Position(symbol, new_amount, new_entry_price)
        else:
            existing.entry_price = new_entry_price
            existing.amount = new_amount
        result = FillResult(
            symbol, "buy", filled_price, filled_amount, filled_amount * filled_price,
            now_iso(), str(order.get("id")) if isinstance(order, dict) else None,
        )
        self._log_live_trade(result)
        self._mark_booked(coid, result, None)
        return result

    # ---------------------------------------------------------------- sell

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
        coid = self._record_intent(
            symbol, "sell", order_ref or f"{symbol}|adhoc",
            requested_amount=float(held), held_at_send=float(held), price_hint=float(price_hint),
            exit_reason=exit_reason,
        )

        order = self._place(symbol, "sell", held, coid)
        # ---- an order now exists on the exchange.
        self._mark_placed(coid, order)
        problem: str | None = None
        partial = False
        try:
            filled_price, filled_amount, remaining = _parse_fill(order, held, price_hint)
            # Partial = the exchange says some is unfilled (remaining > 0), OR it filled less
            # than the amount IT echoes back as ordered. That echoed amount is already
            # truncated to the market's precision by ccxt/the exchange, so legitimate
            # rounding dust (held - amount, e.g. 0.9 SHIB) never counts as partial; only
            # float noise is tolerated between filled and amount. No echoed amount ->
            # remaining-only, exactly as before.
            ordered = order.get("amount")
            if ordered is not None and not (
                _is_finite_number(ordered) and 0 < ordered <= held * (1 + _OVERFILL_TOLERANCE)
            ):
                raise _UnusableFill(f"amount={ordered!r} is not a finite number in (0, held {held!r}]")
            partial = (remaining is not None and remaining > 0) or (
                ordered is not None and filled_amount < ordered * (1 - _OVERFILL_TOLERANCE)
            )
            proceeds = filled_amount * filled_price
            new_cash = self.portfolio.cash + proceeds
            left = held - filled_amount
            if not (_is_finite_number(proceeds) and proceeds > 0):
                problem = f"proceeds {proceeds!r} are not a finite number > 0"
            elif _is_finite_number(self.portfolio.cash) and not _is_finite_number(new_cash):
                problem = f"cash would become {new_cash!r}"
            elif partial and not (_is_finite_number(left) and left > 0):
                problem = f"remaining={remaining!r} reported but filled {filled_amount!r} >= held {held!r}"
        except _UnusableFill as exc:
            problem = str(exc)
        if problem is not None:
            # Conservative: the position stays exactly as it was (plan stays OPEN, the
            # monitor retries the exit), nothing is credited, new buys halt. The order
            # record becomes `unusable`: still reconciled, still halting buys, but it does
            # not block the re-sell (see module docstring).
            self._set_entry(coid, state="unusable")
            self._halt(
                f"sell {symbol}: unusable exchange response ({problem}); nothing booked, "
                f"position of {held!r} kept -- verify against the exchange",
                symbol=symbol, order=_order_summary(order), client_order_id=coid,
            )
            raise LiveOrderUnreconciled(
                f"sell {symbol}: exchange response unusable ({problem}); nothing booked, position kept, new buys halted"
            )

        self.portfolio.cash = new_cash
        if partial:
            pos.amount = left
        else:
            del self.portfolio.positions[symbol]
        result = FillResult(
            symbol, "sell", filled_price, filled_amount, proceeds, now_iso(), str(order.get("id"))
        )
        self._log_live_trade(result)
        self._mark_booked(coid, result, left if partial else None)
        if partial:
            self._halt(
                f"sell {symbol}: PARTIAL fill {filled_amount!r} of {held!r}; remainder {left!r} kept open",
                symbol=symbol, order=_order_summary(order), client_order_id=coid,
            )
            raise LivePartialFill(
                f"sell {symbol} partially filled ({filled_amount!r} of {held!r}); booked, remainder "
                f"{left!r} stays open for the monitor to exit",
                result,
                remaining_amount=left,
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
                fill=result, error=exc,
            )
            raise
