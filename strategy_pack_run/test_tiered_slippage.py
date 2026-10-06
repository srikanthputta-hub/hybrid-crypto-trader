"""test_tiered_slippage.py - proves tiered_slippage.py's tier boundaries and Costs plumbing.
Same style as test_pack.py: small, hand-computed expectations, no network calls (the real
calibration check against Kraken order books is calibrate_tiered_slippage.py, a one-off script,
not a pytest-collected test -- it needs live network access and is run manually/once).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from engine import COST_PRESETS  # noqa: E402

from tiered_slippage import (  # noqa: E402
    tiered_costs,
    tiered_costs_by_symbol,
    tiered_costs_series_by_symbol,
    tiered_slippage_fraction,
)


def test_tier_boundaries_exact():
    assert tiered_slippage_fraction(10_000_000.0) == 0.0005, "exactly at the $10M floor must get the best tier"
    assert tiered_slippage_fraction(9_999_999.0) == 0.0020, "just under $10M must fall to the middle tier"
    assert tiered_slippage_fraction(1_000_000.0) == 0.0020, "exactly at the $1M floor must get the middle tier"
    assert tiered_slippage_fraction(999_999.0) == 0.0050, "just under $1M must fall to the worst tier"
    assert tiered_slippage_fraction(0.0) == 0.0050
    assert tiered_slippage_fraction(50_000_000.0) == 0.0005, "well above $10M still gets the best tier"


def test_unknown_liquidity_maps_to_worst_tier():
    assert tiered_slippage_fraction(None) == 0.0050


def test_tiered_costs_only_changes_slippage_not_fee():
    base = COST_PRESETS["kraken_T3_taker"]
    costs = tiered_costs(500_000.0, base=base)
    assert costs.slippage == 0.0050
    assert costs.fee == base.fee, "tiered_costs must never touch the fee, only slippage"
    assert costs.name != base.name, "tiered costs should be distinguishable by name from the flat preset"


def test_tiered_costs_by_symbol_maps_each_symbol_independently():
    volumes = {"BTC": 50_000_000.0, "SHIB": 500_000.0, "OBSCURE": None}
    out = tiered_costs_by_symbol(volumes)
    assert out["BTC"].slippage == 0.0005
    assert out["SHIB"].slippage == 0.0050
    assert out["OBSCURE"].slippage == 0.0050
    assert all(c.fee == COST_PRESETS["kraken_T3_taker"].fee for c in out.values())


# --------------------------------------------------------------------------- #
# tiered_costs_series_by_symbol (Architect review item 2: per-bar, point-in-time-safe costs --
# the tier must be computed fresh at each bar from trailing volume through bar t-1, NOT a single
# static Costs for the whole run, and NOT bar t's own (not-yet-known-at-fill-time) volume).
# --------------------------------------------------------------------------- #
def test_series_uses_trailing_volume_through_t_minus_1_not_t():
    """Bar t's Costs must reflect trailing_dollar_volume[t-1], never trailing_dollar_volume[t] --
    this is the actual fix: bar t's own volume isn't known yet at its open, when a fill executes."""
    # Volume jumps from the <$1M tier straight to the >=$10M tier at bar 2. If bar t's OWN volume
    # were (incorrectly) used, bar 2 would already show the best tier; using t-1 (bar 1's volume,
    # still in the worst tier), bar 2 must still show the worst tier -- the jump only shows up
    # starting bar 3 (t-1 = bar 2).
    vol = np.array([500_000.0, 500_000.0, 50_000_000.0, 50_000_000.0])
    out = tiered_costs_series_by_symbol({"X": vol})["X"]
    assert out[0].slippage == 0.0050, "bar 0 has no t-1 -> unknown liquidity -> worst tier"
    assert out[1].slippage == 0.0050, "bar 1's t-1 (bar 0) was still in the worst tier"
    assert out[2].slippage == 0.0050, (
        "bar 2's t-1 (bar 1) was STILL in the worst tier, even though bar 2's OWN volume jumped -- "
        "using bar 2's own volume here would be the one-bar look-ahead this function exists to avoid"
    )
    assert out[3].slippage == 0.0005, "bar 3's t-1 (bar 2) is finally in the best tier"


def test_series_bar_zero_always_worst_tier_regardless_of_its_own_volume():
    vol = np.array([50_000_000.0, 50_000_000.0])
    out = tiered_costs_series_by_symbol({"X": vol})["X"]
    assert out[0].slippage == 0.0050, "bar 0 has no preceding bar at all -> must still be the worst tier"


def test_series_only_changes_slippage_never_fee():
    base = COST_PRESETS["kraken_T3_taker"]
    out = tiered_costs_series_by_symbol({"X": np.array([1.0, 50_000_000.0])}, base=base)["X"]
    assert all(c.fee == base.fee for c in out), "per-bar tiering must never touch the fee, only slippage"


def test_series_each_symbol_computed_independently():
    vols = {"LIQUID": np.array([50_000_000.0, 50_000_000.0]), "THIN": np.array([1.0, 1.0])}
    out = tiered_costs_series_by_symbol(vols)
    # Both bar 0s are the forced worst tier (no t-1 yet); bar 1 reflects each symbol's OWN bar-0
    # volume independently of the other symbol.
    assert out["LIQUID"][1].slippage == 0.0005
    assert out["THIN"][1].slippage == 0.0050


def test_series_empty_array_returns_empty_array():
    out = tiered_costs_series_by_symbol({"X": np.array([])})["X"]
    assert len(out) == 0


def test_series_a_static_tiered_costs_would_misprice_a_symbols_own_early_history():
    """Concretely demonstrates the bug the per-bar form fixes: a symbol that's highly liquid by
    the END of a long run but was NOT at the START must NOT get the end-of-run tier applied to its
    early bars (which a single static `tiered_costs_by_symbol` value, held constant for the whole
    run, would do)."""
    vol = np.concatenate([np.full(10, 1.0), np.full(10, 50_000_000.0)])  # thin early, liquid late
    per_bar = tiered_costs_series_by_symbol({"X": vol})["X"]
    assert per_bar[1].slippage == 0.0050, "an early bar must be costed at the symbol's OWN early (thin) liquidity"
    assert per_bar[-1].slippage == 0.0005, "a late bar must be costed at the symbol's OWN late (liquid) liquidity"
    # The static per-symbol form (what P1d originally shipped) would instead apply ONE tier -- from
    # whichever single volume number it was given -- to the entire run, misclassifying the early
    # bars if given the symbol's current/latest volume.
    static_from_latest = tiered_costs_by_symbol({"X": float(vol[-1])})["X"]
    assert static_from_latest.slippage != per_bar[1].slippage, (
        "a single static Costs built from the symbol's LATEST volume must disagree with the "
        "per-bar series at an EARLY bar -- that disagreement is exactly the bug this fix closes"
    )


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("PASS", t.__name__)
    print(f"{len(tests)} tests passed")
