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
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import ccxt

from src.exchange import ExchangeClient
from src.portfolio import Portfolio, Position, _is_finite_number, now_iso

logger = logging.getLogger(__name__)

HALT_MARKER_NAME = "live_broker_halt.json"

# A reported filled amount may exceed the amount we asked for by at most this
# relative float-noise margin; anything larger is a units/parse error, not a fill.
_OVERFILL_TOLERANCE = 1e-9


class LiveTradingDisabled(RuntimeError):
    pass


class LiveBrokerHalted(RuntimeError):
    """buy() refused: an earlier live order could not be reconciled. No order was placed."""


class LiveOrderUnreconciled(RuntimeError):
    """An order may be live on the exchange but its result could not be booked. New buys are halted."""


class LivePartialFill(RuntimeError):
    """A sell only partially filled; the filled part is booked, the remainder stays open.

    `fill` holds the FillResult for the part that did fill."""

    def __init__(self, message: str, fill: "FillResult") -> None:
        super().__init__(message)
        self.fill = fill


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
        if self._halt_marker_path is not None and self._halt_marker_path.exists():
            logger.critical(
                "live broker HALTED at startup: %s exists -- new buys are refused until a human "
                "reconciles the exchange account and deletes it", self._halt_marker_path,
            )

    # ---------------------------------------------------------------- halt latch

    @property
    def halted(self) -> bool:
        return self._halt_reason is not None or (
            self._halt_marker_path is not None and self._halt_marker_path.exists()
        )

    def _halt(self, reason: str, **details: object) -> None:
        """Latch the halt in memory first (can't fail), then best-effort persist it."""
        self._halt_reason = reason
        logger.critical("LIVE BROKER HALTED -- new buys refused until a human reconciles: %s %s", reason, details)
        if self._halt_marker_path is None:
            return
        try:
            self._halt_marker_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"halted_at": now_iso(), "reason": reason, **{k: repr(v) for k, v in details.items()}}
            self._halt_marker_path.write_text(json.dumps(payload, indent=2) + "\n")
        except Exception:  # noqa: BLE001 -- the in-memory latch above still holds for this process
            logger.exception("could not persist live broker halt marker %s", self._halt_marker_path)

    def _place(self, symbol: str, side: str, amount: float) -> object:
        """create_market_order; a definite exchange rejection is re-raised unchanged, any
        other failure leaves the order's fate unknown -> halt + LiveOrderUnreconciled."""
        try:
            return self.exchange.create_market_order(symbol, side, amount)
        except Exception as exc:
            if _is_definite_rejection(exc):
                raise  # the exchange answered and rejected it: nothing was placed
            self._halt(
                f"{side} {symbol}: create_market_order raised {type(exc).__name__}; "
                "the order may or may not be live on the exchange",
                symbol=symbol, side=side, requested_amount=amount, error=exc,
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

    def buy(self, symbol: str, quote_amount: float, price_hint: float) -> FillResult:
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

        order = self._place(symbol, "buy", base_amount_estimate)
        # ---- an order now exists on the exchange: from here on, never raise without
        # ---- booking it or halting.
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
                symbol=symbol, order=_order_summary(order),
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
        return result

    # ---------------------------------------------------------------- sell

    def sell(self, symbol: str, price_hint: float) -> FillResult:
        pos = self.portfolio.positions[symbol]
        if not (_is_finite_number(pos.amount) and pos.amount > 0):
            raise ValueError(f"refusing to sell {symbol}: held amount {pos.amount!r} is not a finite number > 0; no order placed")
        if not (_is_finite_number(price_hint) and price_hint > 0):
            raise ValueError(f"refusing to sell {symbol}: price_hint {price_hint!r} is not a finite number > 0; no order placed")
        held = pos.amount

        order = self._place(symbol, "sell", held)
        # ---- an order now exists on the exchange.
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
            # monitor retries the exit), nothing is credited, new buys halt.
            self._halt(
                f"sell {symbol}: unusable exchange response ({problem}); nothing booked, "
                f"position of {held!r} kept -- verify against the exchange",
                symbol=symbol, order=_order_summary(order),
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
        if partial:
            self._halt(
                f"sell {symbol}: PARTIAL fill {filled_amount!r} of {held!r}; remainder {left!r} kept open",
                symbol=symbol, order=_order_summary(order),
            )
            raise LivePartialFill(
                f"sell {symbol} partially filled ({filled_amount!r} of {held!r}); booked, remainder "
                f"{left!r} stays open for the monitor to exit",
                result,
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
