"""Thin, unified wrapper around ccxt so the rest of the bot never touches ccxt directly.

Handles both read (OHLCV/ticker) and, for live mode, order placement. Paper mode
only ever uses the read side of this class — see broker_paper.py.
"""
from __future__ import annotations

import logging
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
        `cl_ord_id` we send, so the lookup by client id lists orders instead."""
        if exchange_order_id is not None:
            order = self.client.fetch_order(exchange_order_id, symbol)
            if not isinstance(order, dict):
                raise OrderLookupAmbiguous(f"fetch_order returned {type(order).__name__}, not an order")
            return order
        has = getattr(self.client, "has", None) or {}
        listings: list[Any] = []
        if has.get("fetchOrders"):
            listings.append(self.client.fetch_orders(symbol, since_ms))
        elif has.get("fetchOpenOrders") and has.get("fetchClosedOrders"):
            listings.append(self.client.fetch_open_orders(symbol, since_ms))
            listings.append(self.client.fetch_closed_orders(symbol, since_ms))
            if has.get("fetchCanceledOrders"):
                listings.append(self.client.fetch_canceled_orders(symbol, since_ms))
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


class OrderLookupAmbiguous(RuntimeError):
    """find_order could neither pin down nor rule out an order: its outcome stays UNKNOWN."""
