"""universe_gate_inputs.py - P1e builder: turns a universe dataset + floor candidate into the inputs
`backtest_live_bot.simulate_portfolio` / `placebo_run` already accept.

P1e of docs/system-redesign-2026-10.md (§4.4, §8). P1d built the pieces (universe_eligibility.py,
tiered_slippage.py, the `eligible_mask_by_symbol` / `costs_by_symbol` parameters) but nothing wired
them to the real datasets. This module does that, and nothing else:

  load_universe_raw()        raw CSVs -> {base: DataFrame}, with the load-time exclusions applied
  build_universe_inputs()    {base: DataFrame} -> enriched frames on ONE shared global axis, the
                             per-symbol point-in-time eligibility mask for a floor candidate, and
                             the per-bar tiered cost series

The pre-registered rules these implement are in docs/p1e_universe_gate_run.md §0 -- read that, not
this docstring, for the "why". Every rule here is a pure function of data up to and including bar
t (eligibility) or bar t-1 (costs, via tiered_slippage). test_universe_gate_inputs.py proves that on
a real dataset slice by truncation, and proves the check itself catches an injected look-ahead.

No src/ change, no change to backtest_live_bot.py / universe_eligibility.py / tiered_slippage.py /
engine.py: all of them are imported, not modified.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from backtest_live_bot import (  # noqa: E402
    LIQUIDITY_WINDOW_DAYS,
    compute_symbol_signals,
    load_dir,
    trailing_quote_volume,
)
from fetch_kraken import DEFAULT_EXCLUDE_BASES  # noqa: E402
from src.indicators import add_indicators  # noqa: E402
from tiered_slippage import tiered_costs_series_by_symbol  # noqa: E402
from universe_eligibility import (  # noqa: E402
    EXTRA_EXCLUDE_BASES,
    eligible_mask_floor,
    eligible_mask_rank_topn,
    listing_age_bars,
)

# Pre-registered in docs/p1e_universe_gate_run.md §0.2 (item 3), decided from a price-behaviour
# hygiene scan before any backtest ran: USD-pegged stablecoins (median price ~1.000, daily log-return
# std <= 0.1%), a BRL-pegged fiat stablecoin, a tokenized commodity, and an empty stablecoin file.
P1E_EXCLUDE_BASES: frozenset[str] = frozenset({
    "CASH", "FIDD", "SOFID", "USAT", "FRNT",  # USD-pegged stablecoins
    "BRL1",                                   # BRL-pegged fiat stablecoin
    "XU3O8",                                  # tokenized uranium (commodity, not crypto)
    "USDSUI",                                 # stablecoin by name; file is empty anyway
})

ALL_EXCLUDE_BASES: frozenset[str] = frozenset(DEFAULT_EXCLUDE_BASES) | EXTRA_EXCLUDE_BASES | P1E_EXCLUDE_BASES

DATASET_RULES = {"kraken_720_universe": "floor", "long_universe": "rank_topn"}


def original_bases(symbols: list[str]) -> list[str]:
    """`market.symbols` ("BTC/USDT", ...) -> bases ("BTC", ...), config order preserved."""
    return [s.split("/")[0].upper() for s in symbols]


def load_universe_raw(data_dir: str | Path, timeframe: str = "1d") -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Loads every `*_{timeframe}.csv` under `data_dir` (backtest_live_bot.load_dir, keyed by base),
    then drops (a) every base in ALL_EXCLUDE_BASES and (b) files with zero rows. Returns
    (frames, dropped) where `dropped` maps base -> reason, for the report."""
    raw = load_dir(data_dir, timeframe)
    frames: dict[str, pd.DataFrame] = {}
    dropped: dict[str, str] = {}
    for base, df in raw.items():
        if base.upper() in ALL_EXCLUDE_BASES:
            dropped[base] = "excluded base (non-tradable: stablecoin/fiat/wrapped/commodity)"
        elif len(df) == 0:
            dropped[base] = "empty file"
        else:
            frames[base] = df
    return frames, dropped


def eligibility_volume(native: pd.DataFrame, window: int = LIQUIDITY_WINDOW_DAYS) -> pd.Series:
    """Trailing 30-bar median of close*volume, through bar t INCLUSIVE, requiring a full 30 bars
    (`min_periods=window`) -- a "30-day median" needs 30 days (pre-registered §0.4). Computed on the
    symbol's own native bars; bars it doesn't have become NaN after reindexing -> ineligible."""
    return (native["close"] * native["volume"]).rolling(window=window, min_periods=window).median()


def first_valid_bars(enriched_global: dict[str, pd.DataFrame], min_listing_age_days: int, timeframe: str = "1d") -> dict[str, int]:
    """Index (on the shared axis) of each symbol's first real close, with the pre-registered
    left-censoring rule (§0.4): a symbol whose first bar is the dataset's first bar is treated as
    already `min_listing_age_days` old there (its real listing predates the window)."""
    age_bars = listing_age_bars(min_listing_age_days, timeframe)
    out: dict[str, int] = {}
    for s, enr in enriched_global.items():
        valid = np.flatnonzero(~np.isnan(enr["close"].to_numpy()))
        if len(valid) == 0:
            out[s] = len(enr)  # never valid -> never age-eligible
            continue
        first = int(valid[0])
        out[s] = -age_bars if first == 0 else first
    return out


def cap_top_n_per_bar(masks: dict[str, np.ndarray], vol: dict[str, np.ndarray], max_symbols: int) -> dict[str, np.ndarray]:
    """At each bar, keep only the `max_symbols` highest-trailing-volume symbols among those already
    eligible (ties by symbol name). No stickiness (§0.4). Pure per-bar function."""
    symbols = sorted(masks)
    if not symbols:
        return {}
    n = len(masks[symbols[0]])
    out = {s: np.zeros(n, dtype=bool) for s in symbols}
    m = np.vstack([masks[s] for s in symbols])
    v = np.vstack([np.nan_to_num(vol[s], nan=-np.inf) for s in symbols])
    counts = m.sum(axis=0)
    for t in range(n):
        if counts[t] <= max_symbols:
            for i in np.flatnonzero(m[:, t]):
                out[symbols[i]][t] = True
            continue
        cands = sorted((-v[i, t], symbols[i]) for i in np.flatnonzero(m[:, t]))
        for _, s in cands[:max_symbols]:
            out[s][t] = True
    return out


def enrich_native(frames: dict[str, pd.DataFrame], indicators_cfg: dict[str, Any]) -> dict[str, pd.DataFrame]:
    """add_indicators on each symbol's own native history -- the same first step
    backtest_live_bot.build_enriched_and_signals takes."""
    return {
        s: add_indicators(df, indicators_cfg["fast_ema"], indicators_cfg["slow_ema"], indicators_cfg["rsi_period"])
        for s, df in frames.items()
    }


def shared_axis(frames: dict[str, pd.DataFrame]) -> pd.DatetimeIndex:
    """Union of every symbol's dates (same rule as build_enriched_and_signals). Built from the FULL
    loaded dataset, so every candidate/baseline on a dataset shares one axis (same OOS cut, same
    placebo shift range) even when it only simulates a subset of symbols."""
    return pd.DatetimeIndex(sorted(set().union(*[f.index for f in frames.values()])))


def build_eligibility(
    rule: str,
    enriched_global: dict[str, pd.DataFrame],
    native: dict[str, pd.DataFrame],
    idx: pd.DatetimeIndex,
    *,
    floor: float,
    exit_volume_ratio: float,
    min_listing_age_days: int,
    max_symbols: int,
    top_n: int | None = None,
    vol_fn: Callable[[pd.DataFrame], pd.Series] = eligibility_volume,
    timeframe: str = "1d",
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], dict[str, int]]:
    """Returns (mask_by_symbol, eligibility_volume_by_symbol, first_valid_bar_by_symbol).

    rule "floor"     -> kraken_720_universe: age AND floor-with-hysteresis, then per-bar top-`max_symbols` cap.
    rule "rank_topn" -> long_universe: top-`top_n` age-eligible by trailing volume (P1d approximation).
    `vol_fn` is injectable only so the truncation test can prove it catches a look-ahead mutant."""
    vol = {s: vol_fn(native[s]).reindex(idx).to_numpy(dtype=float) for s in enriched_global}
    fvb = first_valid_bars(enriched_global, min_listing_age_days, timeframe)
    if rule == "floor":
        masks = {
            s: eligible_mask_floor(
                vol[s], fvb[s], min_listing_age_days=min_listing_age_days,
                min_median_quote_volume_usd=floor, exit_volume_ratio=exit_volume_ratio, timeframe=timeframe,
            )
            for s in enriched_global
        }
        masks = cap_top_n_per_bar(masks, vol, max_symbols)
    elif rule == "rank_topn":
        if top_n is None:
            raise ValueError("rank_topn eligibility needs top_n")
        masks = eligible_mask_rank_topn(vol, fvb, min_listing_age_days=min_listing_age_days, top_n=top_n, timeframe=timeframe)
    else:
        raise ValueError(f"unknown eligibility rule {rule!r}")
    return masks, vol, fvb


def build_costs(enriched_global: dict[str, pd.DataFrame]) -> dict[str, np.ndarray]:
    """Per-bar tiered `Costs` (frozen P1d tiers), from trailing_quote_volume on the shared axis,
    shifted to t-1 inside tiered_costs_series_by_symbol."""
    return tiered_costs_series_by_symbol(
        {s: trailing_quote_volume(enr).to_numpy() for s, enr in enriched_global.items()}
    )


def signals_for(
    native: dict[str, pd.DataFrame], symbols: list[str], cfg: Any,
) -> dict[str, dict[pd.Timestamp, Any]]:
    """Stage-A signals (scanner -> signal_engine -> htf gate) per symbol, on native bars --
    exactly backtest_live_bot.build_enriched_and_signals' per-symbol call. Per-symbol independent,
    so computing it for a subset of symbols is identical to computing it for all and subsetting."""
    htf_enabled = bool(cfg.htf_filter.get("enabled", True))
    ind = cfg.indicators
    return {
        s: compute_symbol_signals(native[s], cfg.scanner, cfg.signal_engine, htf_enabled, ind["fast_ema"], ind["slow_ema"])
        for s in symbols
    }
