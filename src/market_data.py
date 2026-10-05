"""In-memory OHLCV cache for src/bot.py, keyed by (symbol, timeframe).

This is Phase 1a of docs/system-redesign-2026-10.md's redesign (see its §4.1
and §4.3): pure plumbing, not a decision change. `src/bot.py`'s `run_cycle`
used to call `ExchangeClient.fetch_ohlcv_df` fresh for every symbol on every
poll tick, even though 11 of every 12 ticks (at a 300s poll / 1h timeframe)
land inside a bar that's already closed and whose closed-bar data therefore
cannot have changed since the last fetch. This module caches that result and
only refetches a (symbol, timeframe) key once a *new* bar has actually
closed -- it never reimplements "what counts as closed," it just calls
through to `ExchangeClient.fetch_ohlcv_df`, which already drops the forming
bar (see that method's own docstring, issue #46).

Strategy-neutral by construction: this module contains no pattern, signal,
risk, or sizing logic whatsoever. It only decides *when* to make another
exchange call and *what DataFrame a caller gets back* -- the DataFrame shape
and contents are identical to what a direct `fetch_ohlcv_df` call would have
returned at that moment, so every downstream stage (scanner, signal_engine,
trade_planner, risk_engine) sees the same inputs it always has and makes the
same decisions from them. See tests/test_market_data.py's parity test.

Also writes a per-refresh-pass snapshot to `logs/market_snapshot.json`
(current price -- the last closed bar's close -- plus recent candles per
symbol) that `scripts/export_dashboard_data.py` reads directly instead of
making its own, uncoordinated exchange calls.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SNAPSHOT_PATH = PROJECT_ROOT / "logs" / "market_snapshot.json"

# How many of the most recent candles go into the snapshot per symbol -- matches
# the candle count scripts/export_dashboard_data.py used to fetch directly for
# an active plan's annotated chart, so the dashboard's chart sizing is unchanged.
SNAPSHOT_CANDLE_COUNT = 150


@dataclass
class _CacheEntry:
    df: pd.DataFrame
    fetched_at: datetime  # wall-clock time this entry was last actually fetched (not reused from cache)
    next_due_ms: int      # exchange-clock ms at/after which the next bar may have closed


class MarketDataCache:
    """Bar-close-aligned OHLCV cache for one (exchange, timeframe) pair.

    `bar_close_aligned=True` (the default, and what src/bot.py uses in
    production) only refetches a symbol once a new bar of `timeframe` has
    closed since its last fetch. `bar_close_aligned=False` refetches on
    every `refresh()` call regardless -- i.e. reproduces today's
    direct-fetch-every-cycle behavior exactly, through the same code path.
    This isn't a debugging knob left lying around: tests/test_market_data.py
    uses the two modes side by side as the "before" and "after" halves of
    its parity test, so the comparison runs through one real implementation
    instead of a hand-maintained duplicate of the old fetch loop.
    """

    def __init__(
        self,
        exchange: Any,
        timeframe: str,
        history_bars: int,
        call_budget: int | None = None,
        bar_close_aligned: bool = True,
    ) -> None:
        self.exchange = exchange
        self.timeframe = timeframe
        self.history_bars = history_bars
        # Per-refresh-pass cap on exchange calls. None = unlimited, which is
        # always fine for today's static ~16-symbol watchlist (config/config.yaml
        # market.symbols) -- this exists so a future, much larger universe
        # (see the redesign doc's later phases) has somewhere to plug a budget
        # in without this module's shape changing again.
        self.call_budget = call_budget
        self.bar_close_aligned = bar_close_aligned
        self._cache: dict[str, _CacheEntry] = {}
        self._tf_ms: int | None = None
        # Populated by the most recent refresh() call -- introspection for
        # callers/tests, not consumed by any decision logic.
        self.last_refresh_stats: dict[str, Any] = {}

    def _timeframe_ms(self) -> int:
        if self._tf_ms is None:
            self._tf_ms = int(self.exchange.client.parse_timeframe(self.timeframe) * 1000)
        return self._tf_ms

    def _now_ms(self) -> int:
        return int(self.exchange.client.milliseconds())

    def _is_due(self, symbol: str) -> bool:
        if not self.bar_close_aligned:
            return True
        entry = self._cache.get(symbol)
        if entry is None:
            return True
        return self._now_ms() >= entry.next_due_ms

    def is_stale(self, symbol: str, max_age_seconds: float) -> bool:
        """True if `symbol` has no cached entry, or its entry's last actual
        fetch (wall-clock) is older than `max_age_seconds`. This is how a
        caller tells cached-fresh apart from cached-stale-but-served-anyway
        (e.g. a refetch was due but failed and the cache fell back to the
        last good data, or a call-budget cap skipped it this pass)."""
        entry = self._cache.get(symbol)
        if entry is None:
            return True
        age = (datetime.now(timezone.utc) - entry.fetched_at).total_seconds()
        return age > max_age_seconds

    def last_updated(self, symbol: str) -> datetime | None:
        entry = self._cache.get(symbol)
        return entry.fetched_at if entry is not None else None

    @staticmethod
    def _priority_order(symbols: list[str], active_symbols: Iterable[str]) -> list[str]:
        """Fetch priority: symbols with an active (OPEN/PENDING) plan first,
        then the rest in `market.symbols` config order -- there's no liquidity
        ranking yet (see the redesign doc's later phases), so config order is
        the only remaining signal. Pure reordering; doesn't change `symbols`."""
        active_set = set(active_symbols)
        active = [s for s in symbols if s in active_set]
        rest = [s for s in symbols if s not in active_set]
        return active + rest

    def refresh(self, symbols: list[str], active_symbols: Iterable[str] = ()) -> dict[str, pd.DataFrame]:
        """Returns {symbol: DataFrame} for every symbol in `symbols` that has
        usable data (freshly fetched, or served from cache). A symbol that
        isn't due for refetch is served straight from the cache with zero
        exchange calls. A symbol whose fetch fails is logged and falls back
        to its last cached DataFrame if one exists, otherwise it's simply
        omitted from the result -- matching today's behavior of a failed
        symbol being skipped for the cycle when there's nothing to fall back
        on."""
        ordered = self._priority_order(list(symbols), active_symbols)
        calls_made = 0
        fetched: list[str] = []
        served_from_cache: list[str] = []
        failed: list[str] = []
        budget_skipped: list[str] = []
        result: dict[str, pd.DataFrame] = {}

        for symbol in ordered:
            due = self._is_due(symbol)
            budget_exhausted = self.call_budget is not None and calls_made >= self.call_budget
            if due and budget_exhausted:
                budget_skipped.append(symbol)
            elif due:
                try:
                    df = self.exchange.fetch_ohlcv_df(symbol, self.timeframe, self.history_bars)
                    calls_made += 1
                    if len(df) == 0:
                        raise RuntimeError(f"no closed bars returned for {symbol}")
                    last_open_ms = int(df["timestamp"].iloc[-1].value // 1_000_000)
                    self._cache[symbol] = _CacheEntry(
                        df=df,
                        fetched_at=datetime.now(timezone.utc),
                        next_due_ms=last_open_ms + 2 * self._timeframe_ms(),
                    )
                    fetched.append(symbol)
                except Exception:
                    logger.exception("failed to fetch market data for %s", symbol)
                    failed.append(symbol)
                    # falls through to serve the stale cache entry below, if any

            entry = self._cache.get(symbol)
            if entry is not None:
                result[symbol] = entry.df
                if symbol not in fetched:
                    served_from_cache.append(symbol)

        self.last_refresh_stats = {
            "calls_made": calls_made,
            "fetched": fetched,
            "served_from_cache": served_from_cache,
            "failed": failed,
            "budget_skipped": budget_skipped,
        }
        return result

    def write_snapshot(self, path: Path | str = DEFAULT_SNAPSHOT_PATH, max_candles: int = SNAPSHOT_CANDLE_COUNT) -> None:
        """Writes current price (last closed close) + recent candles for every
        currently-cached symbol to `path`, in the shape
        scripts/export_dashboard_data.py reads directly. Called by
        src/bot.py's run_cycle after each refresh() pass. Written via a
        temp-file-then-replace so a reader never sees a half-written file."""
        symbols_out: dict[str, Any] = {}
        for symbol, entry in self._cache.items():
            df = entry.df
            if len(df) == 0:
                continue
            tail = df.tail(max_candles)
            symbols_out[symbol] = {
                "price": float(df["close"].iloc[-1]),
                "updated_at": entry.fetched_at.isoformat(),
                "candles": [
                    {
                        "time": int(row.timestamp.timestamp()),
                        "open": round(float(row.open), 8),
                        "high": round(float(row.high), 8),
                        "low": round(float(row.low), 8),
                        "close": round(float(row.close), 8),
                    }
                    for row in tail.itertuples()
                ],
            }

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "timeframe": self.timeframe,
            "symbols": symbols_out,
        }

        out_path = Path(path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2))
        tmp_path.replace(out_path)
