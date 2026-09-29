"""Higher-timeframe (daily) trend confirmation gate for pipeline stage 2.

Standard top-down multi-timeframe practice, same shape whether it's a
discretionary trader or a systematic one: a higher timeframe sets the
directional bias, a lower timeframe only times entries *within* that bias
-- it never overrides it. This bot's scanner/signal engine run entirely on
`market.timeframe` (1h by default); this module adds the missing higher-
timeframe check on top, using the daily chart.

This bot is long-only, so the only bias that matters is "is the daily trend
actually up?" -- fast EMA above slow EMA on the most recent daily candle,
the same EMA periods already configured for indicators (`indicators.fast_ema`
/ `indicators.slow_ema`). A signal whose direction disagrees with (or can't
confirm) the daily trend is blocked from ever becoming a trade plan; this
module never originates or resizes a trade, only vetoes one -- same
"can only say no" shape as src.risk_engine and src.claude_advisor.

Fails closed on anything uncertain (too little daily history, a fetch
error): no confirmed uptrend means no trade, same philosophy as every other
gate in this pipeline.

Daily candles only change once a day, so re-fetching them every 5-minute
cycle (this bot's poll interval) would be pure waste -- the per-symbol
result is cached for the rest of the current UTC day.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import pandas as pd

from src.indicators import ema

logger = logging.getLogger(__name__)

# (symbol, date) -> confirmed. Module-level cache: daily candles are the same
# for every call within a given UTC day, so this avoids re-fetching them
# every 5-minute cycle. Entries for a symbol's prior dates are dropped as
# soon as a new date is seen for that symbol, so this never grows unbounded.
_cache: dict[tuple[str, date], bool] = {}


def daily_trend_confirmed_from_df(daily_df: pd.DataFrame, fast_ema: int, slow_ema: int) -> bool:
    """Pure check: True if the daily close is in an uptrend (fast EMA > slow
    EMA on the most recent daily bar). Needs at least `slow_ema` bars of
    history to be a meaningful EMA; fewer than that fails closed (False).
    """
    if len(daily_df) < slow_ema:
        return False
    fast = ema(daily_df["close"], fast_ema)
    slow = ema(daily_df["close"], slow_ema)
    return bool(fast.iloc[-1] > slow.iloc[-1])


def daily_trend_confirmed(
    exchange,
    symbol: str,
    fast_ema: int,
    slow_ema: int,
    today: date | None = None,
) -> bool:
    """Cached, fetch-backed wrapper around daily_trend_confirmed_from_df().
    A fetch/data error fails closed (False) and is logged, never raised --
    a higher-timeframe check must never crash the cycle for every symbol.
    """
    today = today or datetime.now(timezone.utc).date()
    key = (symbol, today)
    if key in _cache:
        return _cache[key]

    try:
        daily_df = exchange.fetch_ohlcv_df(symbol, "1d", max(slow_ema + 5, 30))
        confirmed = daily_trend_confirmed_from_df(daily_df, fast_ema, slow_ema)
    except Exception:
        logger.exception("daily trend check failed for %s -- treating as not confirmed", symbol)
        confirmed = False

    _cache[key] = confirmed
    for stale_key in [k for k in _cache if k[0] == symbol and k[1] != today]:
        del _cache[stale_key]
    return confirmed
