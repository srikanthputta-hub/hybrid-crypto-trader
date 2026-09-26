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
        raw = self.client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df

    def fetch_ticker_price(self, symbol: str) -> float:
        ticker = self.client.fetch_ticker(symbol)
        price = ticker.get("last") or ticker.get("close")
        if price is None:
            raise RuntimeError(f"Could not read a price from ticker for {symbol}: {ticker}")
        return float(price)

    def create_market_order(self, symbol: str, side: str, amount: float) -> dict[str, Any]:
        """Places a REAL order. Only ever called by broker_live.py, which gates on config+env."""
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
        return self.client.create_order(symbol=symbol, type="market", side=side, amount=amount)

    def load_markets(self) -> dict[str, Any]:
        return self.client.load_markets()
