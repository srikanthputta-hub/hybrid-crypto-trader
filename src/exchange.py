"""Thin, unified wrapper around ccxt so the rest of the bot never touches ccxt directly.

Handles both read (OHLCV/ticker) and, for live mode, order placement. Paper mode
only ever uses the read side of this class — see broker_paper.py.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

import ccxt
import pandas as pd

logger = logging.getLogger(__name__)


class ExchangeClient:
    def __init__(
        self,
        exchange_id: str,
        api_key: str | None = None,
        api_secret: str | None = None,
        api_password: str | None = None,
        testnet: bool = True,
    ) -> None:
        exchange_class = getattr(ccxt, exchange_id)
        params: dict[str, Any] = {"enableRateLimit": True}
        if api_key and api_secret:
            params["apiKey"] = api_key
            params["secret"] = api_secret
            if api_password:
                params["password"] = api_password
        self.client: ccxt.Exchange = exchange_class(params)
        if testnet:
            try:
                self.client.set_sandbox_mode(True)
            except ccxt.NotSupported:
                logger.warning(
                    "%s does not support ccxt sandbox mode; testnet=true had no effect. "
                    "Double-check whether this exchange needs a separate testnet API key/URL.",
                    exchange_id,
                )
        self.exchange_id = exchange_id

    def fetch_ohlcv_df(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        """Fetch up to `limit` genuinely CLOSED candles.

        ccxt's fetch_ohlcv does not guarantee the most recent bar it returns has
        actually closed — on most exchanges it's the still-forming current candle
        if you call mid-period. This bot polls every 5 minutes against 1h candles
        (see config/config.yaml), so on most cycles the naive last row would still
        be forming, not decided. src/scanner.py's pattern detectors treat
        df.iloc[-1] as "the current, decided candle", so a forming last row lets a
        detected signal flicker in and out of existence as that candle's close
        price moves ("repainting") and lets the bot act on a snapshot that
        wouldn't have been true once the candle actually closed. To avoid that, we
        over-fetch by one bar, drop any bar whose close time (open time +
        timeframe duration) is still in the future relative to the exchange's own
        clock, and only return bars that have truly closed. See
        research/v2_signals.py::fetch_closed_ohlcv for the reference
        implementation this mirrors (same exchange-clock approach, different
        return shape — this method keeps its existing contract: a plain
        DataFrame with `timestamp` holding each bar's OPEN time, not an index).
        """
        raw = self.client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit + 1)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        tf_ms = int(self.client.parse_timeframe(timeframe) * 1000)
        close_ms = df["timestamp"] + tf_ms
        df = df[close_ms <= self.client.milliseconds()]
        df = df.iloc[-limit:].reset_index(drop=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df

    def fetch_ticker_price(self, symbol: str) -> float:
        ticker = self.client.fetch_ticker(symbol)
        price = ticker.get("last") or ticker.get("close")
        if price is None:
            raise RuntimeError(f"Could not read a price from ticker for {symbol}: {ticker}")
        return float(price)

    def create_market_order(
        self, symbol: str, side: str, amount: float, client_order_id: str | None = None
    ) -> dict[str, Any]:
        """Places a REAL order. Only ever called by broker_live.py, which gates on config+env.

        `client_order_id` (issue #99) is sent as ccxt's unified `clientOrderId` param (Kraken
        `cl_ord_id`, Binance `newClientOrderId`) so a reconcile can find the order after a
        crash. Without it the call is exactly the pre-#99 one."""
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
        if client_order_id is None:
            return self.client.create_order(symbol=symbol, type="market", side=side, amount=amount)
        return self.client.create_order(
            symbol=symbol, type="market", side=side, amount=amount, params={"clientOrderId": client_order_id}
        )

    def load_markets(self) -> dict[str, Any]:
        return self.client.load_markets()

    # ------------------------------------------------------------------ live-only reads (issue #99)
    # Called only by broker_live.py. They place nothing; they let the live broker reconcile an
    # order whose outcome it could not observe, and cap a sell at what the account really holds.

    def find_order(
        self,
        symbol: str,
        client_order_id: str,
        exchange_order_id: str | None = None,
        since_ms: int | None = None,
        side: str | None = None,
    ) -> dict[str, Any] | None:
        """The exchange's own record of one of our orders, or None ONLY when the exchange
        positively listed this symbol's orders and none of them can be it.

        Raises (-> the caller must treat the outcome as UNKNOWN) on any lookup error, when
        the exchange can't list orders, when several orders carry this clientOrderId, or
        when an order of the same side without any clientOrderId is in the window (it could
        be ours on an exchange that doesn't echo the id). With an exchange order id known,
        fetch_order is authoritative and any error -- OrderNotFound included -- propagates:
        we know that order was placed.

        Kraken's ccxt fetch_order(clientOrderId=...) maps to the numeric `userref`, not the
        `cl_ord_id` we send, so the lookup by client id lists orders instead -- with the
        clientOrderId as a SERVER-SIDE filter (ccxt 4.5.90 kraken fetch_open_orders /
        fetch_closed_orders map params.clientOrderId to `cl_ord_id`), so a busy account's first
        result page can't hide it. Exchanges without such a filter are listed with a large
        explicit limit (never ccxt's default page)."""
        if exchange_order_id is not None:
            order = self.client.fetch_order(exchange_order_id, symbol)
            if not isinstance(order, dict):
                raise OrderLookupAmbiguous(f"fetch_order returned {type(order).__name__}, not an order")
            return order
        has = getattr(self.client, "has", None) or {}
        listings: list[Any] = []
        by_client_id = {"clientOrderId": client_order_id}
        if has.get("fetchOpenOrders") and has.get("fetchClosedOrders") and self.exchange_id in _SERVER_SIDE_CLIENT_ID:
            listings.append(self.client.fetch_open_orders(symbol, since_ms, None, by_client_id))
            listings.append(self.client.fetch_closed_orders(symbol, since_ms, None, by_client_id))
        elif has.get("fetchOrders"):
            listings.append(self.client.fetch_orders(symbol, since_ms, _LISTING_LIMIT))
        elif has.get("fetchOpenOrders") and has.get("fetchClosedOrders"):
            listings.append(self.client.fetch_open_orders(symbol, since_ms, _LISTING_LIMIT))
            listings.append(self.client.fetch_closed_orders(symbol, since_ms, _LISTING_LIMIT))
            if has.get("fetchCanceledOrders"):
                listings.append(self.client.fetch_canceled_orders(symbol, since_ms, _LISTING_LIMIT))
        else:
            raise OrderLookupAmbiguous(f"{self.exchange_id} cannot list orders; the outcome can't be determined")
        matches: dict[str, dict[str, Any]] = {}
        unattributable = False
        for orders in listings:
            if not isinstance(orders, list):
                raise OrderLookupAmbiguous(f"order listing returned {type(orders).__name__}, not a list")
            for order in orders:
                if not isinstance(order, dict):
                    raise OrderLookupAmbiguous(f"order listing contained {type(order).__name__}")
                coid = order.get("clientOrderId")
                if coid == client_order_id:
                    matches[str(order.get("id"))] = order
                elif not coid and (side is None or order.get("side") == side):
                    unattributable = True
        if len(matches) > 1:
            raise OrderLookupAmbiguous(f"{len(matches)} orders carry clientOrderId {client_order_id}")
        if matches:
            return next(iter(matches.values()))
        if unattributable:
            raise OrderLookupAmbiguous(
                f"a {side or ''} {symbol} order without a clientOrderId is in the window; it can't be ruled out"
            )
        return None

    def fetch_free_balance(self, asset: str) -> float | None:
        """Free (not locked in open orders) balance of `asset`, or None when the exchange
        doesn't report one (some exchanges omit zero balances -- None is 'unknown', never 0)."""
        balance = self.client.fetch_balance()
        free = balance.get("free") if isinstance(balance, dict) else None
        value = free.get(asset) if isinstance(free, dict) else None
        if value is None:
            return None
        return float(value)

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        """`amount` truncated to the market's amount precision -- what ccxt itself sends."""
        self.client.load_markets()
        return float(self.client.amount_to_precision(symbol, amount))

    def market_limits(self, symbol: str) -> "MarketLimits":
        """The market's published minimum order size (issue #81), for a BUY that must fail
        closed: raises MarketLimitsUnavailable (or whatever load_markets raised) when the
        markets can't be loaded, the symbol isn't listed, or its limits are malformed. Read-only."""
        markets = self.client.load_markets()
        if not isinstance(markets, dict) or symbol not in markets:
            raise MarketLimitsUnavailable(f"{self.exchange_id} lists no market {symbol!r}; its limits are unknown")
        return parse_market_limits(symbol, markets[symbol])

    def _market_limit(self, symbol: str, kind: str) -> float | None:
        # The sell-side dust check's view of the same parser (market_limit): a symbol that
        # isn't listed has no published minimum (None, as before) -- the exchange is the arbiter
        # of a protective exit. Malformed limits raise; the sell path treats that as unknown too.
        markets = self.client.load_markets()
        market = markets.get(symbol) if isinstance(markets, dict) else None
        if market is None:
            return None
        return market_limit(symbol, market, kind, require_limits=False)

    def min_order_amount(self, symbol: str) -> float | None:
        """The market's minimum order amount (base units), or None when not published."""
        return self._market_limit(symbol, "amount")

    def min_order_cost(self, symbol: str) -> float | None:
        """The market's minimum order cost (quote units), or None when not published."""
        return self._market_limit(symbol, "cost")


# ---------------------------------------------------------------------- per-pair minimums (issue #81)


class MarketLimitsUnavailable(RuntimeError):
    """A market's minimum order size could not be determined: a live BUY must be refused."""


@dataclass(frozen=True)
class MarketLimits:
    """ccxt `limits.amount.min` (base units) and `limits.cost.min` (quote units) of one market.
    None = the exchange publishes no such minimum (a published 0 is also no constraint)."""

    symbol: str
    amount_min: float | None
    cost_min: float | None


def _limit_value(symbol: str, kind: str, value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise MarketLimitsUnavailable(f"{symbol}: limits.{kind}.min={value!r} is not a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MarketLimitsUnavailable(f"{symbol}: limits.{kind}.min={value!r} is not a number") from exc
    if not (math.isfinite(number) and number >= 0):
        raise MarketLimitsUnavailable(f"{symbol}: limits.{kind}.min={value!r} is not a finite number >= 0")
    return number


def market_limit(symbol: str, market: object, kind: str, require_limits: bool = True) -> float | None:
    """One minimum (`kind` "amount" or "cost") of one ccxt market dict, or MarketLimitsUnavailable.
    Pure. ccxt always fills `limits` for a loaded market; a market without it (require_limits) or
    with a non-dict / non-finite / negative / non-numeric minimum is UNKNOWN, never 'no minimum'.
    An absent `limits.<kind>` entry, or min None, means the exchange publishes none."""
    if not isinstance(market, dict):
        raise MarketLimitsUnavailable(f"{symbol}: market is {type(market).__name__}, not a dict")
    limits = market.get("limits")
    if limits is None and not require_limits:
        limits = {}
    if not isinstance(limits, dict):
        raise MarketLimitsUnavailable(f"{symbol}: market has no usable 'limits' ({type(limits).__name__})")
    entry = limits.get(kind)
    if entry is None:
        return None
    if not isinstance(entry, dict):
        raise MarketLimitsUnavailable(f"{symbol}: limits.{kind} is {type(entry).__name__}, not a dict")
    return _limit_value(symbol, kind, entry.get("min"))


def parse_market_limits(symbol: str, market: object) -> MarketLimits:
    """Both minimums of one ccxt market dict (market_limit), or MarketLimitsUnavailable. Pure."""
    return MarketLimits(symbol, market_limit(symbol, market, "amount"), market_limit(symbol, market, "cost"))


def order_minimum_violation(
    amount: float, price: float, amount_min: float | None, cost_min: float | None, truncated: float | None,
) -> str | None:
    """Why an order of `amount` (base) at `price` is below the market's minimum, else None. Pure.

    `truncated` is the amount at the market's precision when known (None = unknown). Shared by
    the live BUY guard (issue #81: amount = the precision-truncated amount) and the sell dust
    check (review F8c: amount = the amount sent). Only ever a reason to REFUSE an order; nothing
    here can resize one."""
    if truncated is not None and math.isfinite(truncated) and truncated == 0:
        return f"{amount!r} truncates to 0 at the market's precision"
    if amount_min is not None and math.isfinite(amount_min) and amount_min > 0 and amount < amount_min:
        return f"{amount!r} is below the exchange minimum amount {amount_min!r}"
    if cost_min is not None and math.isfinite(cost_min) and cost_min > 0 and amount * price < cost_min:
        return f"{amount!r} x {price!r} is below the exchange minimum cost {cost_min!r}"
    return None


# Exchanges whose ccxt fetch_open_orders/fetch_closed_orders filter by params.clientOrderId
# server-side (verified in the installed ccxt 4.5.90 source for kraken).
_SERVER_SIDE_CLIENT_ID = frozenset({"kraken"})
# Explicit page size for listings without a server-side filter (ccxt's default can be 50).
_LISTING_LIMIT = 1000


class OrderLookupAmbiguous(RuntimeError):
    """find_order could neither pin down nor rule out an order: its outcome stays UNKNOWN."""
