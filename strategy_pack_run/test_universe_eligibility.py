"""test_universe_eligibility.py - proves universe_eligibility.py's point-in-time properties on
small, hand-built series. Same spirit/style as test_pack.py's mechanics tests and
test_backtest_live_bot.py's truncation-invariance checks: these prove the MECHANICS (no
look-ahead, correct hysteresis transitions, correct age-gating, correct top-N ranking), not
anything about the live strategy's edge.

Run directly:  python strategy_pack_run/test_universe_eligibility.py
Run via pytest: pytest strategy_pack_run/test_universe_eligibility.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from backtest_live_bot import trailing_quote_volume  # noqa: E402

from universe_eligibility import (  # noqa: E402
    EXTRA_EXCLUDE_BASES,
    age_eligible_mask,
    eligible_mask_floor,
    eligible_mask_rank_topn,
    filter_extra_excluded_bases,
    listing_age_bars,
    volume_floor_mask_with_hysteresis,
)


# --------------------------------------------------------------------------- #
# listing_age_bars / age_eligible_mask
# --------------------------------------------------------------------------- #
def test_listing_age_bars_matches_liquidity_window_bars_scaling():
    # Same calendar-day -> bar-count conversion as backtest_live_bot.liquidity_window_bars, just
    # applied to a different day count -- confirm the arithmetic independently.
    assert listing_age_bars(60, "1d") == 60
    assert listing_age_bars(60, "1h") == 60 * 24
    assert listing_age_bars(60, "4h") == 60 * 6
    assert listing_age_bars(30, "15m") == 30 * 96


def test_age_eligible_mask_becomes_true_exactly_at_the_cutoff_bar():
    # first_valid_bar=2, min_listing_age_days=2 (1d -> 2 bars): eligible iff t - 2 >= 2, i.e. t >= 4.
    mask = age_eligible_mask(first_valid_bar=2, n_bars=7, min_listing_age_days=2, timeframe="1d")
    assert list(mask) == [False, False, False, False, True, True, True]


def test_age_eligible_mask_never_true_before_first_valid_bar():
    mask = age_eligible_mask(first_valid_bar=5, n_bars=6, min_listing_age_days=0, timeframe="1d")
    # min_listing_age_days=0 -> eligible as soon as the bar itself exists (t - 5 >= 0 -> t >= 5).
    assert list(mask) == [False, False, False, False, False, True]


# --------------------------------------------------------------------------- #
# volume_floor_mask_with_hysteresis
# --------------------------------------------------------------------------- #
def test_hysteresis_enters_at_floor_and_exits_only_below_exit_floor():
    # floor=100, exit_ratio=0.5 -> exit_floor=50. Verified by hand (see module docstring's own
    # worked trace) before transcribing:
    #   10  -> below floor, not yet eligible            -> False
    #   60  -> still below the ENTRY floor (100)        -> False
    #   120 -> crosses the entry floor                  -> True  (enters)
    #   80  -> eligible already, 80 >= exit_floor (50)   -> True  (hysteresis keeps it in)
    #   40  -> eligible already, 40 <  exit_floor (50)   -> False (drops out)
    #   90  -> not eligible, 90 <  ENTRY floor (100)     -> False (needs 100 to re-enter, not 50)
    #   30  -> not eligible, 30 <  ENTRY floor           -> False
    v = np.array([10.0, 60.0, 120.0, 80.0, 40.0, 90.0, 30.0])
    mask = volume_floor_mask_with_hysteresis(v, floor=100.0, exit_ratio=0.5)
    assert list(mask) == [False, False, True, True, False, False, False]


def test_hysteresis_nan_is_always_treated_as_below_floor():
    v = np.array([200.0, np.nan, 200.0])
    mask = volume_floor_mask_with_hysteresis(v, floor=100.0, exit_ratio=0.5)
    assert list(mask) == [True, False, True], "a NaN trailing-volume bar must never read as eligible"


def test_hysteresis_with_ratio_1_has_no_slack_floor_equals_exit_floor():
    # exit_ratio=1.0 -> exit_floor == floor, i.e. zero hysteresis slack (membership flips the
    # instant volume crosses the single floor value in either direction).
    v = np.array([150.0, 90.0, 150.0])
    mask = volume_floor_mask_with_hysteresis(v, floor=100.0, exit_ratio=1.0)
    assert list(mask) == [True, False, True]


# --------------------------------------------------------------------------- #
# eligible_mask_floor (age AND volume-with-hysteresis)
# --------------------------------------------------------------------------- #
def test_eligible_mask_floor_requires_both_age_and_volume():
    # Volume clears the floor from bar 0 onward, but the symbol isn't old enough until bar 2
    # (first_valid_bar=0, min_listing_age_days=2 at 1d -> age-eligible from t>=2).
    v = np.array([500.0, 500.0, 500.0, 500.0, 500.0])
    mask = eligible_mask_floor(
        v, first_valid_bar=0,
        min_listing_age_days=2, min_median_quote_volume_usd=100.0, exit_volume_ratio=0.5,
        timeframe="1d",
    )
    assert list(mask) == [False, False, True, True, True]


def test_eligible_mask_floor_old_enough_but_illiquid_never_eligible():
    v = np.array([1.0, 1.0, 1.0, 1.0, 1.0])
    mask = eligible_mask_floor(
        v, first_valid_bar=0,
        min_listing_age_days=0, min_median_quote_volume_usd=100.0, exit_volume_ratio=0.5,
        timeframe="1d",
    )
    assert not mask.any()


def test_eligible_mask_floor_truncation_invariant():
    """The no-look-ahead proof, modeled on test_pack.py::test_truncation_no_lookahead: bar i's
    eligibility must be identical whether or not the frame is truncated right after bar i -- i.e.
    nothing here ever uses data from bar i+1 onward to decide bar i."""
    rng = np.random.default_rng(7)
    n = 120
    idx = pd.date_range("2022-01-01", periods=n, freq="1D", tz="UTC")
    close = pd.Series(100.0 * np.exp(np.cumsum(0.01 * rng.standard_normal(n))), index=idx)
    volume = pd.Series(np.abs(rng.standard_normal(n)) * 1000.0 + 10.0, index=idx)
    # A few symbols "listed late" (leading NaNs) and some genuinely illiquid stretches, so the
    # age gate and the hysteresis state machine both actually get exercised.
    close.iloc[:15] = np.nan
    volume.iloc[:15] = np.nan
    volume.iloc[40:55] = 0.5  # a deep illiquid patch that should drop membership

    enriched_full = pd.DataFrame({"close": close, "volume": volume})
    full_trailing = trailing_quote_volume(enriched_full).to_numpy()
    first_valid_bar = int(np.argmax(~np.isnan(close.to_numpy())))
    full_mask = eligible_mask_floor(
        full_trailing, first_valid_bar,
        min_listing_age_days=20, min_median_quote_volume_usd=5000.0, exit_volume_ratio=0.5,
        timeframe="1d",
    )

    # Every bar, not just a handful of sampled cut points: a mutation-tested one-bar look-ahead
    # bug injected into the hysteresis loop (reading trailing_dollar_volume[t+1] instead of [t])
    # was NOT caught when this only sampled 5 cut points (it happened to land on bars where the
    # off-by-one didn't change the hysteresis state) -- see the Architect review that found this.
    # Looping every bar closes that gap.
    for cut in range(n):
        enriched_cut = enriched_full.iloc[: cut + 1]
        cut_trailing = trailing_quote_volume(enriched_cut).to_numpy()
        cut_close = close.to_numpy()[: cut + 1]
        cut_first_valid = int(np.argmax(~np.isnan(cut_close)))
        cut_mask = eligible_mask_floor(
            cut_trailing, cut_first_valid,
            min_listing_age_days=20, min_median_quote_volume_usd=5000.0, exit_volume_ratio=0.5,
            timeframe="1d",
        )
        assert cut_mask[cut] == full_mask[cut], (
            f"look-ahead in eligible_mask_floor at bar {cut}: full={full_mask[cut]} "
            f"truncated={cut_mask[cut]}"
        )


# --------------------------------------------------------------------------- #
# eligible_mask_rank_topn
# --------------------------------------------------------------------------- #
def test_rank_topn_picks_highest_volume_age_eligible_symbols():
    n = 3
    trailing = {
        "AAA": np.array([500.0, 500.0, 500.0]),
        "BBB": np.array([900.0, 900.0, 900.0]),
        "CCC": np.array([50.0, 50.0, 50.0]),
    }
    first_valid = {"AAA": 0, "BBB": 0, "CCC": 0}
    out = eligible_mask_rank_topn(
        trailing, first_valid, min_listing_age_days=0, top_n=2, timeframe="1d",
    )
    assert list(out["AAA"]) == [True, True, True]
    assert list(out["BBB"]) == [True, True, True]
    assert list(out["CCC"]) == [False, False, False], "lowest-volume symbol must miss the top-2 cut"


def test_rank_topn_excludes_not_yet_age_eligible_symbol_even_if_most_liquid():
    # DDD has by far the highest volume but isn't old enough yet (first_valid_bar=2, needs 2 bars
    # of age at 1d -> eligible from t>=4, out of range for n=3) -- it must never appear, no matter
    # how liquid, until it clears the age gate.
    trailing = {
        "AAA": np.array([10.0, 10.0, 10.0]),
        "DDD": np.array([99999.0, 99999.0, 99999.0]),
    }
    first_valid = {"AAA": 0, "DDD": 2}
    out = eligible_mask_rank_topn(
        trailing, first_valid, min_listing_age_days=2, top_n=1, timeframe="1d",
    )
    assert list(out["DDD"]) == [False, False, False], "DDD isn't age-eligible within this window (needs t>=4)"
    assert list(out["AAA"]) == [False, False, True], "AAA clears its own age gate at t=2 and then wins the only slot"


def test_rank_topn_mismatched_lengths_raises():
    trailing = {"AAA": np.array([1.0, 2.0]), "BBB": np.array([1.0, 2.0, 3.0])}
    first_valid = {"AAA": 0, "BBB": 0}
    try:
        eligible_mask_rank_topn(trailing, first_valid, min_listing_age_days=0, top_n=1)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on mismatched per-symbol array lengths")


def test_rank_topn_truncation_invariant():
    """Same no-look-ahead proof as eligible_mask_floor, but for the cross-symbol ranking rule:
    bar i's membership must not change if later bars are removed. Ranking itself is inherently
    per-bar (no state carried across bars, unlike the hysteresis mask), so this mainly proves
    trailing-volume truncation invariance flows through correctly.

    The original version of this fixture scaled each symbol's volume as `(k+1)*500` -- strictly
    monotonic across symbols, so relative rank order never actually changed between any two cut
    points. A deliberately-injected one-bar look-ahead bug in eligible_mask_rank_topn was NOT
    caught by that fixture, even sampling every bar, because swapping which bar's volume you read
    can never flip an order that's monotonically separated by a factor of 500+ to begin with (see
    the Architect review that found this). AAA/BBB below deliberately CROSS rank order at the
    midpoint instead (AAA is the highest-volume symbol for the first half, BBB for the second) --
    a real look-ahead bug would move the rank-1 slot across that crossover, which this fixture can
    actually expose."""
    n = 60
    idx = pd.date_range("2022-03-01", periods=n, freq="1D", tz="UTC")
    symbols = ["AAA", "BBB", "CCC", "DDD"]
    # AAA leads 0..29, BBB leads 30..59 -- the deliberate crossover. CCC/DDD sit at fixed, much
    # lower levels the whole time (never win rank 1) but still vary in listing age, so the age
    # gate's own truncation invariance stays exercised too.
    base_volume = {
        "AAA": np.concatenate([np.full(30, 1000.0), np.full(30, 1.0)]),
        "BBB": np.concatenate([np.full(30, 1.0), np.full(30, 1000.0)]),
        "CCC": np.full(n, 50.0),
        "DDD": np.full(n, 20.0),
    }
    lead_nan = {"AAA": 0, "BBB": 5, "CCC": 10, "DDD": 15}
    trailing_full = {}
    first_valid = {}
    frames = {}
    for s in symbols:
        close = pd.Series(1.0, index=idx)  # constant close -> trailing dollar volume == trailing volume
        volume = pd.Series(base_volume[s], index=idx)
        ln = lead_nan[s]
        close.iloc[:ln] = np.nan
        volume.iloc[:ln] = np.nan
        frames[s] = pd.DataFrame({"close": close, "volume": volume})
        trailing_full[s] = trailing_quote_volume(frames[s]).to_numpy()
        first_valid[s] = ln

    full_out = eligible_mask_rank_topn(trailing_full, first_valid, min_listing_age_days=10, top_n=1)

    # Confirm the fixture actually crosses rank order (the whole point of this redesign) before
    # relying on it for the truncation-invariance loop below.
    assert full_out["AAA"][29] and not full_out["BBB"][29], "AAA must still be rank-1 just before the crossover"
    assert full_out["BBB"][59] and not full_out["AAA"][59], "BBB must be rank-1 well after the crossover"

    for cut in range(n):
        trailing_cut = {s: trailing_quote_volume(frames[s].iloc[: cut + 1]).to_numpy() for s in symbols}
        cut_out = eligible_mask_rank_topn(trailing_cut, first_valid, min_listing_age_days=10, top_n=1)
        for s in symbols:
            assert cut_out[s][cut] == full_out[s][cut], (
                f"look-ahead in eligible_mask_rank_topn for {s} at bar {cut}: "
                f"full={full_out[s][cut]} truncated={cut_out[s][cut]}"
            )


# --------------------------------------------------------------------------- #
# filter_extra_excluded_bases / EXTRA_EXCLUDE_BASES (Architect review item 3: fetch_kraken.py's
# DEFAULT_EXCLUDE_BASES, frozen from an earlier ticker snapshot, misses ~22 bases now present in
# the already-fetched data_universe/raw_kraken + raw_long directories -- filtered here, at load
# time, rather than by refetching).
# --------------------------------------------------------------------------- #
def test_extra_exclude_bases_covers_the_reviewed_22_bases():
    # Transcribed directly from the Architect review's own list -- a regression guard against any
    # of these silently falling back out of the set.
    stablecoins = {"AUSD", "USDD", "OUSD", "USDGO", "USDPT", "USDSM", "USTABLES", "APXUSD", "USDR"}
    fiat_pegged = {"AUDX", "EUROP", "EURR", "QCAD", "TGBP", "MXNB"}
    wrapped_or_staked = {"WBTC", "CMETH", "LSETH", "METH", "LSSOL", "MSOL", "JITOSOL"}
    expected = stablecoins | fiat_pegged | wrapped_or_staked
    assert expected == set(EXTRA_EXCLUDE_BASES)
    assert len(EXTRA_EXCLUDE_BASES) == 22


def test_filter_extra_excluded_bases_drops_from_a_plain_list():
    bases = ["BTC", "ETH", "WBTC", "MSOL", "SOL"]
    assert filter_extra_excluded_bases(bases) == ["BTC", "ETH", "SOL"]


def test_filter_extra_excluded_bases_drops_from_a_loaded_symbol_mapping():
    # Mirrors backtest_live_bot.load_dir's return shape: {base: DataFrame-like}.
    loaded = {"BTC": "btc_frame", "WBTC": "wbtc_frame", "JITOSOL": "jitosol_frame", "ETH": "eth_frame"}
    out = filter_extra_excluded_bases(loaded)
    assert out == {"BTC": "btc_frame", "ETH": "eth_frame"}


def test_filter_extra_excluded_bases_is_case_insensitive_and_leaves_others_untouched():
    assert filter_extra_excluded_bases(["wbtc", "BTC"]) == ["BTC"]
    assert filter_extra_excluded_bases(["XRP", "ADA", "DOGE"]) == ["XRP", "ADA", "DOGE"]


def test_filter_extra_excluded_bases_would_exclude_wbtc_before_it_could_win_a_rank_topn_slot():
    """The concrete scenario the review flagged: WBTC's real trailing volume would otherwise rank
    #87 and could win a lenient rank-top-N cut -- proving the filter removes it from the candidate
    set BEFORE eligible_mask_rank_topn ever sees it, rather than relying on ranking to push it out
    (a tighter top_n candidate floor wouldn't necessarily do that)."""
    trailing = {"BTC": np.array([500.0]), "WBTC": np.array([999_999.0])}
    first_valid = {"BTC": 0, "WBTC": 0}
    filtered_symbols = filter_extra_excluded_bases(trailing)
    assert "WBTC" not in filtered_symbols, "WBTC must never reach eligible_mask_rank_topn's candidate set at all"
    out = eligible_mask_rank_topn(filtered_symbols, first_valid, min_listing_age_days=0, top_n=1)
    assert list(out["BTC"]) == [True], "with WBTC filtered out upstream, BTC wins the only slot"
    assert "WBTC" not in out


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("PASS", t.__name__)
    print(f"{len(tests)} tests passed")
