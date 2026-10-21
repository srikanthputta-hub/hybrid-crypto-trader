"""End-to-end checks of run_intraday_p2.evaluate_family on SYNTHETIC data over the frozen windows
(never the real evaluation data): the whole results JSON is byte-identical for 1 and 3 workers,
the gates are wired to the registered statistics (G2 = the 0.0694th percentile of the champion's
block bootstrap, G4 = draw-champion counts, ...), the verdict follows the gates, and every report
section is rendered. A small placebo (n_placebo) keeps it fast; the registered 200 is a constant the
CLI cannot change.

Run: pytest strategy_pack_run/test_intraday_runner_p2.py -q
"""
from __future__ import annotations

import functools
import sys
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import intraday_stats as st  # noqa: E402
import intraday_synthetic as syn  # noqa: E402
import run_intraday_p2 as p2  # noqa: E402

COINS = ("BTC", "ETH", "SOL", "ADA")


def _factory(u, tf):
    return _data(tf)


@functools.lru_cache(maxsize=None)
def _data(tf):
    assert tf == "1h"
    return syn.universe_1h(COINS[1:], "2020-09-01", 24 * 2240, seed=11, kind="regime_switch", sigma=0.006, mu=0.0004)


def _provider():
    return p2.SyntheticProvider(_factory, coins={u: {"1h": COINS, "15m": COINS} for u in ("B", "A")})


@functools.lru_cache(maxsize=None)
def _run(family, workers):
    return p2.evaluate_family(family, _provider(), workers=workers, n_placebo=3, verbose=False)


def test_results_json_is_byte_identical_for_1_and_3_workers():
    assert p2.dumps(_run(1, 1)) == p2.dumps(_run(1, 3))


@pytest.mark.parametrize("family", [1, 3])
def test_gates_are_wired_to_the_registered_statistics(family):
    r = _run(family, 3)
    assert r["integrity"] == [] and r["harness_version"] == "intraday-prereg-v1 + p6-same-bar-exposure-v1"
    assert r["placebo_shifts_days"] == st.placebo_days()[:3]
    ch = r["champion_set"]
    sel = st.champion(r["selection"])
    assert f"F{family}-{sel['set_no']:02d}" == ch
    g, b = r["gates"], r["bootstrap"]
    assert g["G2_lower"] == b["gate"][st.G2_PERCENTILE] and g["G2"] == (g["G2_lower"] > 0)
    assert b["gate"][st.G2_PERCENTILE] < b["gate"][2.5]  # the gate is the corrected (lower) bound
    cB = r["grid"]["B"][ch]
    assert g["G1"] == (cB["trades"] >= 300 and b["blocks"] >= 100)
    assert g["G4"] == (r["g4"]["ge_avgR"] <= 2 and r["g4"]["ge_return"] <= 2)
    assert g["G5"] == (cB["total_return"] > 0 and cB["sharpe_daily"] > r["benchmark_B"]["sharpe"]
                       and r["views"]["B"]["kraken_T3_taker_+25%"]["total_return"] > 0)
    assert g["R1"] == (r["grid"]["A"][ch]["avgR"] > 0 and r["grid"]["A"][ch]["total_return"] > 0)
    assert r["verdict"] == st.verdict(g)
    assert r["views"]["B"]["kraken_T3_taker"]["final_equity"] == cB["final_equity"]


def test_report_has_every_registered_section():
    txt = p2.render_report(_run(1, 3))
    for i in range(1, 12):
        assert f"## {i}." in txt
    assert "## 13." in txt and "Survivorship" in txt and "Coinbase, not Kraken" in txt


def test_universe_override_is_refused_on_real_data(tmp_path):
    class Fake(p2.RealProvider):
        coins = {"B": {"1h": COINS}}

        def verify(self, family):
            return {}

    with pytest.raises(ValueError):  # review n5: never touches the real data root
        p2.evaluate_family(1, Fake(tmp_path), workers=1, n_placebo=1, verbose=False)
