"""tiered_slippage.py - liquidity-tiered slippage for the universe-dataset harness.

P1d of docs/system-redesign-2026-10.md (§4.4 rule 3, §8's "P1d universe datasets + point-in-time
harness + tiered slippage (calibrated, then frozen)"). NEW module, importing (not modifying)
engine.py's `Costs`/`COST_PRESETS` -- per this repo's standing rule, engine.py itself is
import-only for strategy_pack_run work.

Why this exists: `COST_PRESETS["kraken_T3_taker"]`'s flat 5bp slippage was calibrated for the
original 16 liquid majors (BTC, ETH, ...). A much bigger, far more liquid-tail-heavy universe
(kraken_720_universe / long_universe) needs slippage that actually scales with a symbol's own
liquidity -- a flat 5bp wildly understates the real cost of trading a $50k/day microcap.

Tiers below are PRE-REGISTERED A PRIORI (set before looking at any gate result, per the design
doc's "calibrated, then frozen" instruction -- not tuned after seeing backtest numbers):
    median daily quote volume (USD) >= $10,000,000  -> 5 bp   (same as today's flat preset)
    $1,000,000 <= ... < $10,000,000                 -> 20 bp
    ... < $1,000,000                                -> 50 bp
Calibrated ONCE against real Kraken order-book spreads (see calibrate_tiered_slippage.py) before
being used for any actual run against the universe datasets, then FROZEN -- the calibration check
reports how the a priori tiers compare to realized spreads, it does not feed back into the tier
values themselves (design doc: "report, don't silently adjust the tiers based on it").

Missing/unknown liquidity (`None`) is mapped to the WORST (highest-slippage) tier -- the same
conservative-by-default convention `src.signal_engine.rank_signals` uses for missing liquidity on
its own tie-break (treat "don't know" as "assume the least favorable case", never as "assume it's
fine").

POINT-IN-TIME NOTE (fixed after an Architect review found this static-per-symbol form was a real
look-ahead/correctness bug on long multi-year runs): `tiered_costs`/`tiered_costs_by_symbol` below
assign ONE static tier per symbol from ONE volume number -- fine for a present-day snapshot (e.g.
calibrate_tiered_slippage.py), but WRONG for `backtest_live_bot.simulate_portfolio`'s
`costs_by_symbol` over a long backtest, where a symbol's liquidity tier genuinely changes across
its own history. `tiered_costs_series_by_symbol` below is the point-in-time-safe, PER-BAR form
`simulate_portfolio` actually needs -- it recomputes the tier at every bar from trailing volume
through bar t-1 (not t).
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np

from engine import COST_PRESETS, Costs

# (floor_usd, slippage_fraction) pairs, ordered from HIGHEST floor to LOWEST ("first floor this
# volume clears, scanning top-down, wins" -- see tiered_slippage_fraction). slippage_fraction is
# a fraction of price (0.0005 == 5bp), matching Costs.slippage's own units exactly.
TIERS_USD_VOLUME_TO_SLIPPAGE: tuple[tuple[float, float], ...] = (
    (10_000_000.0, 0.0005),  # >= $10M median daily $ volume: 5 bp (same as today's flat preset)
    (1_000_000.0, 0.0020),   # $1M .. <$10M: 20 bp
    (0.0, 0.0050),           # <$1M: 50 bp (also the fallback for unknown/None liquidity)
)


def tiered_slippage_fraction(median_quote_volume_usd: float | None) -> float:
    """The frozen a-priori tier for one symbol's trailing median daily quote volume (USD). `None`
    (unknown liquidity) maps to the worst tier (50bp) -- conservative-by-default, matching
    rank_signals' own missing-liquidity convention (see module docstring)."""
    if median_quote_volume_usd is None:
        return TIERS_USD_VOLUME_TO_SLIPPAGE[-1][1]
    for floor, slippage in TIERS_USD_VOLUME_TO_SLIPPAGE:
        if median_quote_volume_usd >= floor:
            return slippage
    return TIERS_USD_VOLUME_TO_SLIPPAGE[-1][1]  # unreachable (last floor is 0.0) but no silent fallthrough


def tiered_costs(
    median_quote_volume_usd: float | None, base: Costs = COST_PRESETS["kraken_T3_taker"]
) -> Costs:
    """`base` with its `slippage` replaced by the tiered value for this symbol's liquidity; `fee`
    and everything else about `base` (e.g. the Kraken T3 taker fee) is left untouched -- only
    slippage is tiered, per the design doc's rule 3 (the existing fee schedule is unaffected by
    universe size, only market-impact/slippage is).

    This is a single, STATIC classification for ONE volume number -- correct for a present-day
    snapshot use (e.g. calibrate_tiered_slippage.py classifying a symbol's CURRENT trailing volume
    against a live order book, where there's no backtest-over-time concern), but NOT point-in-time
    safe to use as a single `Costs` applied to an entire backtest's history -- see
    `tiered_costs_series_by_symbol` below for the per-bar form `backtest_live_bot.simulate_portfolio`
    actually needs."""
    return replace(base, slippage=tiered_slippage_fraction(median_quote_volume_usd), name=f"{base.name}_tiered")


def tiered_costs_by_symbol(
    median_quote_volume_by_symbol: dict[str, float | None],
    base: Costs = COST_PRESETS["kraken_T3_taker"],
) -> dict[str, Costs]:
    """Convenience: one STATIC `Costs` per symbol, from one present-day volume number per symbol.

    NOT point-in-time safe for a backtest run over a long history (see `tiered_costs` above and
    `tiered_costs_series_by_symbol` below) -- a symbol's liquidity tier genuinely changes over a
    multi-year backtest (e.g. `long_universe`, 2015-2026: a coin liquid TODAY was almost certainly
    far thinner in its early history), so applying ONE tier for the symbol's entire run both
    misprices its early era and leaks today's liquidity level backward in time. Kept here as a
    present-day classification helper (e.g. for a one-off report of "what tier is each symbol in
    right now") -- do NOT pass this dict's values directly as
    `backtest_live_bot.simulate_portfolio`'s `costs_by_symbol` (which now expects a per-bar array
    per symbol, not a single static `Costs`); use `tiered_costs_series_by_symbol` for that."""
    return {symbol: tiered_costs(volume, base) for symbol, volume in median_quote_volume_by_symbol.items()}


def tiered_costs_series_by_symbol(
    trailing_dollar_volume_by_symbol: dict[str, np.ndarray],
    base: Costs = COST_PRESETS["kraken_T3_taker"],
) -> dict[str, np.ndarray]:
    """The point-in-time-SAFE form for `backtest_live_bot.simulate_portfolio`'s `costs_by_symbol`
    parameter: one array of `Costs` per symbol, ONE ENTRY PER BAR on the shared global axis,
    computed fresh at each bar from that symbol's own trailing dollar volume.

    Critically, bar t's tier is decided from trailing volume THROUGH BAR t-1, not bar t itself --
    bar t's own volume isn't known yet at its open, which is when a FILL/exit queued from bar t's
    close-trigger actually executes (see simulate_portfolio's step 1). Using bar t's own trailing
    volume to cost bar t's fill would be a one-bar look-ahead, the same class of bug P1d's
    eligibility masks are built to avoid. Concretely: `tiered_slippage_fraction` is applied to
    `trailing_dollar_volume[t-1]` to produce bar t's `Costs`; bar 0 has no t-1, so it always gets
    the worst (most conservative) tier, the same "missing liquidity -> worst tier" convention
    `tiered_slippage_fraction(None)` already uses.

    `trailing_dollar_volume_by_symbol` should be `backtest_live_bot.trailing_quote_volume`'s output
    (already point-in-time via `rolling(..., min_periods=1)`) reindexed onto the shared global bar
    axis, one array per symbol, each of length T (the axis length) -- this function does not
    reindex or validate cross-symbol length equality itself (unlike
    `universe_eligibility.eligible_mask_rank_topn`, which does, since ranking is inherently
    cross-symbol; this function's math is purely per-symbol)."""
    out: dict[str, np.ndarray] = {}
    for symbol, vol in trailing_dollar_volume_by_symbol.items():
        vol = np.asarray(vol, dtype=float)
        if len(vol) == 0:
            out[symbol] = np.empty(0, dtype=object)
            continue
        shifted = np.empty_like(vol)
        shifted[0] = np.nan  # bar 0 has no t-1 -> unknown liquidity -> worst tier, by construction
        shifted[1:] = vol[:-1]
        costs_arr = np.empty(len(vol), dtype=object)
        for t, v in enumerate(shifted):
            frac = tiered_slippage_fraction(None if np.isnan(v) else float(v))
            costs_arr[t] = replace(base, slippage=frac, name=f"{base.name}_tiered")
        out[symbol] = costs_arr
    return out
