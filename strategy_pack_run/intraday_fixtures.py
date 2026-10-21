"""intraday_fixtures.py - cached synthetic fixtures shared by the test_intraday_*.py files.

All fixtures are synthetic (docs/intraday_prereg.md §0 rule 3). Windows are short so the tests
stay fast; the data start (2020-06-01) leaves > 300 days of warm-up for the BTC SMA(200) regime
and the 114-day Wilder warm-up before any test window.
"""
from __future__ import annotations

import functools
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import intraday_runner as ir  # noqa: E402
import intraday_synthetic as syn  # noqa: E402

DATA_START = "2020-06-01"
WIN_START = pd.Timestamp("2021-04-01", tz="UTC")
WIN_DAYS = 40
ZERO = ir.CostModel("zero", 0.0, 0.0)
TAKER = ir.CostModel.flat(ir.COST_PRESETS["kraken_T3_taker"])


@functools.lru_cache(maxsize=None)
def risk_cfg() -> dict:
    return ir.load_risk_cfg()


def risk(**over) -> dict:
    d = dict(risk_cfg())
    d.update(over)
    return d


GAPS = {"ETH": [(24 * 300 + 5, 3), (24 * 310 + 1, 7)], "SOL": [(24 * 305, 30)]}


@functools.lru_cache(maxsize=None)
def trend_1h(seed: int = 5, gaps: bool = True):
    return syn.universe_1h(["ETH", "SOL", "ADA"], DATA_START, 24 * 360, seed=seed, kind="regime_switch",
                           sigma=0.006, mu=0.0008, gaps=GAPS if gaps else None)


@functools.lru_cache(maxsize=None)
def noise_1h(seed: int = 6, sigma: float = 0.01):
    return syn.universe_1h(["ETH", "SOL", "ADA"], DATA_START, 24 * 360, seed=seed, kind="random_walk", sigma=sigma, gaps=GAPS)


@functools.lru_cache(maxsize=None)
def many_1h(seed: int = 9, n: int = 7):
    names = ["ETH", "SOL", "ADA", "DOGE", "AVAX", "LINK", "LTC", "DOT"][:n]
    return syn.universe_1h(names, DATA_START, 24 * 360, seed=seed, kind="random_walk", sigma=0.012)


@functools.lru_cache(maxsize=None)
def noise_15m(seed: int = 7):
    """A 15m universe straddling the 2023-03-04 mask (window 2023-02-20 .. 2023-03-12)."""
    return syn.universe_15m(["ETH", "SOL", "ADA"], "2022-05-01", 96 * 320, seed=seed, sigma=0.004)


def market(data, start=WIN_START, days=WIN_DAYS):
    return ir.Market(data, start, days)
