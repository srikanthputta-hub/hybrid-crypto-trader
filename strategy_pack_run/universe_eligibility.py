"""universe_eligibility.py - point-in-time universe membership for the strategy-pack harness.

P1d of docs/system-redesign-2026-10.md (§4.4 rule 1, §8's "P1d universe datasets + point-in-time
harness + tiered slippage"). A NEW module, deliberately not bolted into backtest_live_bot.py's
core simulation loop or engine.py -- those files import this one, per the P1d task's own
instruction ("a new module, don't bolt it into engine.py/backtest_live_bot.py's core").

Rule, verbatim from the design doc (§4.4 rule 1): "At bar t, a symbol is eligible only if it has
at least min_listing_age_days of history before t and its trailing-30-day median close x volume
clears the floor, with the same hysteresis [exit_volume_ratio]. Using today's ticker volume to
choose the backtest universe is look-ahead."

Two eligibility rules are implemented here, matching the two datasets P1d builds:
  - `eligible_mask_floor` -- for kraken_720_universe: a real trailing-dollar-volume floor with
    hysteresis, using config/config.yaml's universe.min_median_quote_volume_usd /
    universe.exit_volume_ratio / universe.min_listing_age_days (P1b; NOT YET USED there, this is
    the first caller).
  - `eligible_mask_rank_topn` -- for long_universe: Coinbase quote volumes aren't Kraken's, so
    there's no shared floor to reuse verbatim. Eligibility there is RANK-BASED top-N by trailing
    dollar volume instead (N = whatever the live/kraken_720 universe size would be for the floor
    being tested). This is an explicitly documented APPROXIMATION, not a literal floor match --
    see that function's own docstring and the P1d report.

No look-ahead, enforced by construction: every function here is a pure function of a precomputed
trailing-dollar-volume ARRAY (the caller supplies it -- e.g. via
backtest_live_bot.trailing_quote_volume, whose own `rolling(..., min_periods=1)` is already
point-in-time, using only data up to and including each bar's own close) plus "how many bars of
real history precede bar t". Neither input ever needs data after bar t to produce bar t's answer.
See test_universe_eligibility.py's `test_*_truncation_invariant` tests for the actual proof,
modeled on strategy_pack_run/test_pack.py::test_truncation_no_lookahead.

Deliberately pure numpy, no pandas/engine.py/backtest_live_bot.py import -- keeps this testable in
complete isolation, and avoids a circular import (backtest_live_bot.py is the one that imports
FROM here, once it's wired into simulate_portfolio).
"""
from __future__ import annotations

from typing import Iterable, TypeVar

import numpy as np

# Same literal bar-seconds-per-timeframe mapping as backtest_live_bot.TF_SECONDS / strategies.py's
# TF_SECONDS -- duplicated here on purpose (see module docstring: no backtest_live_bot import),
# matching this repo's existing convention of duplicating this one small table across harness
# modules rather than creating a shared-import dependency for it. Keep in sync if a new timeframe
# is ever added anywhere.
TF_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}

# fetch_kraken.py's DEFAULT_EXCLUDE_BASES was taken from docs/system-redesign-2026-10.md §1's
# measured stablecoin/fiat/wrapped table, which was from an earlier ticker snapshot and misses
# bases now actually present in the already-fetched data_universe/raw_kraken (658 symbols) and
# data_universe/raw_long (308 symbols) directories -- an Architect review of P1d found 22 of them
# by diffing that table against what's actually on disk:
#   stablecoins:          AUSD, USDD, OUSD, USDGO, USDPT, USDSM, USTABLES, APXUSD, USDR
#   fiat-pegged:           AUDX, EUROP, EURR, QCAD, TGBP, MXNB
#   wrapped/liquid-staked: WBTC, CMETH, LSETH, METH, LSSOL, MSOL, JITOSOL
# This matters concretely for eligible_mask_rank_topn (long_universe's rule): WBTC, MSOL, JITOSOL
# and LSETH are ALSO in raw_long, and WBTC currently ranks #87 by Coinbase trailing volume -- at
# the $100k-floor rank-top-N candidate, the ranking would otherwise let in a near-duplicate of BTC
# that was never supposed to be part of the universe at all.
#
# Fixed here by filtering the ALREADY-FETCHED data at LOAD time (via filter_extra_excluded_bases
# below), not by re-running fetch_kraken.py/fetch_coinbase_long.py with an updated
# --exclude-bases -- this repo's standing P1d-fix-pass instruction was explicitly "don't refetch."
# A caller building the per-symbol trailing-volume dicts that feed eligible_mask_floor /
# eligible_mask_rank_topn (P1e's job, not yet wired here -- see docs/p1d_universe_datasets.md)
# should apply this filter to whatever bases/symbols it loaded from
# data_universe/raw_kraken/manifest.csv's `included` bases (or the equivalent already-loaded
# {base: DataFrame} dict backtest_live_bot.load_dir returns, keyed by base) BEFORE constructing
# those dicts -- neither eligibility function re-derives its own symbol list from a manifest, they
# only operate on whatever a caller already built.
EXTRA_EXCLUDE_BASES: frozenset[str] = frozenset({
    # stablecoins
    "AUSD", "USDD", "OUSD", "USDGO", "USDPT", "USDSM", "USTABLES", "APXUSD", "USDR",
    # fiat-pegged
    "AUDX", "EUROP", "EURR", "QCAD", "TGBP", "MXNB",
    # wrapped / liquid-staked
    "WBTC", "CMETH", "LSETH", "METH", "LSSOL", "MSOL", "JITOSOL",
})

_T = TypeVar("_T")


def filter_extra_excluded_bases(
    bases_or_mapping: Iterable[str] | dict[str, _T],
    extra_exclude: frozenset[str] = EXTRA_EXCLUDE_BASES,
) -> list[str] | dict[str, _T]:
    """Drops `extra_exclude` (default EXTRA_EXCLUDE_BASES above) from an already-loaded base list
    OR an already-loaded `{base: ...}` mapping (e.g. `backtest_live_bot.load_dir`'s return value,
    keyed by base -- see that function's `p.stem.split("_")[0].upper()`). Matching is on the
    UPPERCASED base itself (not a "BASE/QUOTE" symbol string or a raw filename) -- pass plain bases
    like "WBTC", matching both `fetch_kraken.py`'s manifest.csv `base` column and `load_dir`'s dict
    keys directly. Returns the same shape it was given (list in, list out; dict in, dict out) so it
    composes directly with either a manifest-derived base list or an already-loaded symbol dict."""
    if isinstance(bases_or_mapping, dict):
        return {k: v for k, v in bases_or_mapping.items() if k.upper() not in extra_exclude}
    return [b for b in bases_or_mapping if b.upper() not in extra_exclude]


def listing_age_bars(min_listing_age_days: int, timeframe: str = "1d") -> int:
    """How many `timeframe` bars span `min_listing_age_days` calendar days -- same calendar-day-
    to-bar-count conversion as backtest_live_bot.liquidity_window_bars, applied to
    config/config.yaml's universe.min_listing_age_days instead of the liquidity window."""
    return min_listing_age_days * 86400 // TF_SECONDS[timeframe]


def age_eligible_mask(
    first_valid_bar: int, n_bars: int, min_listing_age_days: int, timeframe: str = "1d"
) -> np.ndarray:
    """True at bar t iff there are at least `min_listing_age_days` of REAL history strictly before
    t, i.e. `t - first_valid_bar >= listing_age_bars`. `first_valid_bar` is the index (on the
    symbol's own, or the shared global, axis -- caller's choice, as long as it's consistent with
    `n_bars`) of the symbol's first real (non-NaN) close: bars before it don't exist for this
    symbol at all (pre-listing / not-yet-reindexed-in), so they're never eligible regardless of
    age."""
    age_bars = listing_age_bars(min_listing_age_days, timeframe)
    t = np.arange(n_bars)
    return (t - first_valid_bar) >= age_bars


def volume_floor_mask_with_hysteresis(
    trailing_dollar_volume: np.ndarray, floor: float, exit_ratio: float
) -> np.ndarray:
    """Sticky membership: a symbol ENTERS eligibility once `trailing_dollar_volume >= floor`, and
    STAYS eligible until it drops below `floor * exit_ratio` (exit_ratio < 1 widens the exit band
    below the entry floor so membership doesn't flap at the line -- config/config.yaml's
    universe.exit_volume_ratio, P1b). NaN trailing volume (e.g. before the rolling window has any
    real data at all) is always treated as "below floor", never as eligible.

    This is a STATE MACHINE -- today's eligibility depends on yesterday's -- computed with a
    single forward pass where bar t only ever reads trailing_dollar_volume[t] and the previous
    bar's own eligibility state, never anything from bar t+1 onward. That makes it point-in-time
    by construction: see test_universe_eligibility.py's truncation-invariance test."""
    n = len(trailing_dollar_volume)
    out = np.zeros(n, dtype=bool)
    exit_floor = floor * exit_ratio
    eligible = False
    for t in range(n):
        v = trailing_dollar_volume[t]
        if np.isnan(v):
            eligible = False
        elif eligible:
            eligible = bool(v >= exit_floor)
        else:
            eligible = bool(v >= floor)
        out[t] = eligible
    return out


def eligible_mask_floor(
    trailing_dollar_volume: np.ndarray,
    first_valid_bar: int,
    *,
    min_listing_age_days: int,
    min_median_quote_volume_usd: float,
    exit_volume_ratio: float,
    timeframe: str = "1d",
) -> np.ndarray:
    """kraken_720_universe's eligibility rule (design doc §4.4 rule 1): age-eligible AND clears
    the trailing-dollar-volume floor with hysteresis. Both conditions are computed independently
    then ANDed per bar -- a symbol that already satisfies the volume-hysteresis state machine
    before it's old enough becomes eligible exactly at the age cutoff, not later (the hysteresis
    state machine itself doesn't "wait" for the age gate -- that would let a symbol's volume
    history reset the state machine's memory for no principled reason)."""
    n = len(trailing_dollar_volume)
    age_mask = age_eligible_mask(first_valid_bar, n, min_listing_age_days, timeframe)
    vol_mask = volume_floor_mask_with_hysteresis(
        trailing_dollar_volume, min_median_quote_volume_usd, exit_volume_ratio
    )
    return age_mask & vol_mask


def eligible_mask_rank_topn(
    trailing_dollar_volume_by_symbol: dict[str, np.ndarray],
    first_valid_bar_by_symbol: dict[str, int],
    *,
    min_listing_age_days: int,
    top_n: int,
    timeframe: str = "1d",
) -> dict[str, np.ndarray]:
    """long_universe's eligibility rule (design doc §4.4 rule 1's documented APPROXIMATION):
    Coinbase quote volumes aren't Kraken quote volumes, so there's no principled shared dollar-
    volume floor to reuse verbatim from eligible_mask_floor. Instead, at each bar t, the top
    `top_n` AGE-ELIGIBLE symbols by trailing dollar volume are eligible that bar -- `top_n` should
    be set by the caller to whatever the live/kraken_720 universe size would be for the floor
    being tested (e.g. how many kraken_720_universe symbols eligible_mask_floor accepts on
    average); this function only ranks, it doesn't decide N.

    Deliberately NO hysteresis here (unlike eligible_mask_floor): a rank-based "top N" rule has no
    natural floor value to apply exit_volume_ratio against, so adding sticky membership on top of
    a pure ranking would be inventing a second, undocumented approximation on top of the first.
    This is exactly why the design doc calls this rule an approximation, not equivalent to rule
    1's literal floor-with-hysteresis -- flag this plainly in any report built on top of it.

    Ties in trailing dollar volume are broken by symbol name (ascending) purely so the member set
    at a rank boundary is deterministic across runs -- this never changes which symbols clear or
    miss by a wide margin, only who's picked among exact ties (e.g. two symbols both reporting
    literally zero volume on an illiquid day).

    Every value array in `trailing_dollar_volume_by_symbol` must be the same length (the shared
    global bar axis) -- this function does not reindex."""
    symbols = sorted(trailing_dollar_volume_by_symbol)
    out: dict[str, np.ndarray] = {s: np.zeros(0, dtype=bool) for s in symbols}
    if not symbols:
        return out
    n = len(next(iter(trailing_dollar_volume_by_symbol.values())))
    for s in symbols:
        if len(trailing_dollar_volume_by_symbol[s]) != n:
            raise ValueError(
                f"eligible_mask_rank_topn: {s!r} has {len(trailing_dollar_volume_by_symbol[s])} "
                f"bars, expected {n} (every symbol must share the same global bar axis -- "
                f"reindex before calling this function)"
            )
    age_masks = {
        s: age_eligible_mask(first_valid_bar_by_symbol[s], n, min_listing_age_days, timeframe)
        for s in symbols
    }
    out = {s: np.zeros(n, dtype=bool) for s in symbols}
    for t in range(n):
        candidates = []
        for s in symbols:
            if not age_masks[s][t]:
                continue
            v = trailing_dollar_volume_by_symbol[s][t]
            if np.isnan(v):
                continue
            candidates.append((-float(v), s))
        candidates.sort()
        for _, s in candidates[:top_n]:
            out[s][t] = True
    return out
