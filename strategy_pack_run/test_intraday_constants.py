"""AT20 (docs/intraday_harness_changes.md): every frozen module constant equals the value written in
docs/intraday_prereg.md. Each test checks the constant AND that the same literal appears in the
prereg text, so a drift on either side fails.

Run: pytest strategy_pack_run/test_intraday_constants.py -q
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import intraday_data as idata  # noqa: E402
import intraday_families as fam  # noqa: E402
import intraday_runner as ir  # noqa: E402
import intraday_stats as st  # noqa: E402

PREREG = (_REPO_ROOT / "docs" / "intraday_prereg.md").read_text()
HARNESS = (_REPO_ROOT / "docs" / "intraday_harness_changes.md").read_text()


def _grid_rows(prefix: str) -> list[list[str]]:
    rows = []
    for line in PREREG.splitlines():
        m = re.match(rf"\| {prefix}-(\d\d) \|(.*)\|\s*$", line)
        if m:
            rows.append([c.strip() for c in m.group(2).split("|")])
    return rows


def test_family1_grid_matches_prereg_table():
    rows = _grid_rows("F1")
    assert len(rows) == 12
    for spec, (tf, n, m) in zip(fam.F1_GRID, rows):
        assert spec.tf == tf
        assert spec.params[0] == int(n.split()[0])
        assert spec.params[1] == float(m)
    assert [fam.bars_in(s.tf, s.params[0]) for s in fam.F1_GRID if s.tf == "4h"][::3] == [18, 60]


def test_family2_grid_matches_prereg_table():
    rows = _grid_rows("F2")
    assert len(rows) == 12
    for spec, (tf, n, z, ts) in zip(fam.F2_GRID, rows):
        assert (spec.tf, spec.params) == (tf, (int(n), float(z)))
        assert int(ts.split()[0]) == int(n)  # time stop = n decision bars
    assert fam.F2_STOP_ATR_MULT == 2.0 and "2.0 * ATR_TF(14)" in PREREG
    assert fam.F2_ADX_MAX == 20.0 and "ADX_D(coin, latest completed day) < 20" in PREREG


def test_family3_grid_matches_prereg_table():
    rows = _grid_rows("F3")
    assert len(rows) == 12
    for spec, (L, K, b) in zip(fam.F3_GRID, rows):
        assert spec.params == (int(L.split()[0]), int(K), int(b))
        assert spec.tf == "4h"
    assert fam.F3_STOP_ATR_D_MULT == 3.0 and "3.0 * ATR_D" in PREREG


def test_windows_universes_and_cut():
    assert idata.B_START == pd.Timestamp("2021-10-01", tz="UTC") and idata.B_DAYS == 1845
    assert idata.A_START == pd.Timestamp("2023-07-14", tz="UTC") and idata.A_DAYS == 1194
    assert idata.WINDOW_END == pd.Timestamp("2026-10-20", tz="UTC")
    assert idata.B_START + pd.Timedelta(days=idata.B_DAYS) == idata.WINDOW_END
    assert idata.A_START + pd.Timedelta(days=idata.A_DAYS) == idata.WINDOW_END
    assert "2021-10-01 00:00 UTC to 2026-10-20 00:00 UTC | 1,845" in PREREG
    assert "2023-07-14 00:00 UTC to 2026-10-20 00:00 UTC | 1,194" in PREREG
    assert "14: BTC ETH SOL ADA DOGE AVAX LINK LTC DOT ATOM ALGO BCH XTZ SHIB" in PREREG
    assert sorted(idata.UNIVERSE_B) == sorted("BTC ETH SOL ADA DOGE AVAX LINK LTC DOT ATOM ALGO BCH XTZ SHIB".split())
    assert sorted(idata.UNIVERSE_A) == sorted(idata.UNIVERSE_B + ("XRP",))
    assert "XTZ" not in idata.UNIVERSE_B_15M and "XTZ" not in idata.UNIVERSE_A_15M and len(idata.UNIVERSE_B_15M) == 13
    assert "BNB" in idata.EXCLUDED_COINS and "BNB" not in idata.UNIVERSE_A
    assert st.CUT_B == pd.Timestamp("2025-04-14", tz="UTC") and "**2025-04-14 00:00 UTC**" in PREREG
    assert st.oos_cut() == st.CUT_B  # window start + floor(0.7 x 1845) days


def test_masks_and_eligibility_constants():
    assert idata.MASK_RANGES_15M == (("2023-03-04", "2023-03-04"), ("2024-08-13", "2024-09-03"), ("2025-01-01", "2025-04-30"))
    assert "2023-03-04, 2024-08-13..2024-09-03 and 2025-01-01..2025-04-30" in PREREG
    assert idata.MASK_EXEMPT_15M == ("BTC", "ETH") and idata.MASK_WARMUP_EXTRA_BARS == 100
    assert idata.MIN_DAILY_BARS == 60 and "min_listing_age_days: 60" in PREREG
    assert idata.WARMUP_EXTRA == 100 and ">= 100 + their length bars" in PREREG
    assert idata.XRP_CONTINUOUS_FROM == pd.Timestamp("2023-07-13 20:00", tz="UTC")


def test_folds_and_thresholds():
    assert [f[0] for f in st.FOLDS] == ["F1", "F2", "F3", "F4", "F5"]
    assert st.FOLDS[0][1] == pd.Timestamp("2021-10-01", tz="UTC") and st.FOLDS[0][2] == pd.Timestamp("2023-01-01", tz="UTC")
    assert st.FOLDS[4][2] == pd.Timestamp("2026-10-20", tz="UTC")
    assert "F1 = 2021-10-01..2022-12-31" in PREREG and "F5 = 2026-01-01..2026-10-20" in PREREG
    assert (st.G1_MIN_TRADES, st.G1_MIN_BLOCKS, st.G3_MIN_OOS, st.MIN_IS_TRADES, st.FOLD_MIN_TRADES) == (300, 100, 60, 150, 30)
    assert ">= 300 closed trades on B's full window **and** >= 100 distinct bootstrap blocks" in PREREG
    assert ">= 60 trades and avg R > 0" in PREREG and "**>= 150 purged IS trades**" in PREREG
    assert "A fold is **scored** if it has >= 30 trades" in PREREG
    assert st.FOLDS_MIN_SCORED == 3 and st.FOLDS_MAX_LOSING == 1 and st.TIE_EPS == 1e-9


def test_seeds_draws_resamples_and_percentiles():
    assert (st.N_BOOT, st.BOOT_SEED) == (50_000, 1) and "50,000 resamples, `numpy.random.default_rng(1)`" in PREREG
    assert (st.N_PLACEBO, st.PLACEBO_SEED, st.PLACEBO_LO, st.PLACEBO_HI) == (200, 0, 184, 1660)
    assert "**200 draws**, `numpy.random.default_rng(0)`, `d = rng.integers(184, 1660)`" in PREREG
    assert st.N_TRIALS_G2 == 36 and abs(st.G2_ALPHA - 0.025 / 36) < 1e-15
    assert st.G2_PERCENTILE == 0.0694 and "**0.0694th percentile**" in PREREG
    assert abs(st.G2_PERCENTILE - 100 * st.G2_ALPHA) < 1e-4 and st.G2_PERCENTILE <= 100 * st.G2_ALPHA
    assert st.INFO_PERCENTILES == (0.139, 2.5, 97.5) and "0.139th percentile" in PREREG
    assert st.G4_MAX_DRAWS_GE == 2 and "at most 2 of 200 draw-champions >= the real champion" in PREREG
    assert st.INFO_BLOCK_DAYS == 7 and "7-day blocks" in PREREG
    assert "0.0694 (alpha 0.025/36)" in HARNESS and "198-of-200" in HARNESS


def test_costs_and_harness_label():
    t = ir.COST_PRESETS[ir.GATING_COST]
    assert (t.fee, t.slippage) == (0.0038, 0.0005) and "Fee **0.38%** of notional per side" in PREREG
    assert (ir.STRESS_COSTS.fee, ir.STRESS_COSTS.slippage) == (0.00475, 0.000625)
    assert abs(ir.STRESS_COSTS.fee / t.fee - 1.25) < 1e-12 and abs(ir.STRESS_COSTS.slippage / t.slippage - 1.25) < 1e-12
    assert ir.MAKER_FEE == 0.0022 and ir.MAKER_TRADE_THROUGH == 0.0005 and "`low <= limit * (1 - 0.0005)`" in PREREG
    assert ir.TIER_VOLUME_DIVISOR == 3.0 and ir.TIER_MEDIAN_DAYS == 30 and "**divided by 3**" in PREREG
    assert ir.HARNESS_VERSION == "intraday-prereg-v1 + p6-same-bar-exposure-v1"
    assert f"`{ir.HARNESS_VERSION}`" in PREREG


def test_placebo_shifts_are_the_registered_draws():
    import numpy as np

    d = st.placebo_days()
    assert len(d) == 200 and all(184 <= x < 1660 for x in d)
    assert d == np.random.default_rng(0).integers(184, 1660, size=200).tolist()  # same stream either way
    assert d[:5] == [1439, 1124, 938, 582, 638]


def test_amendment_2_records_the_readings_the_harness_implements():
    a2 = PREREG.split("## 10. Amendment 2 (pre-run, 2026-10-21)")
    assert len(a2) == 2
    text = a2[1]
    for phrase in ("counts as a draw the real champion does **NOT** beat", "up to K + b coins", ">= 114 complete daily bars",
                   "(`close <= stop`)", "marked at the coin's last close, until the window's last base close",
                   "P4 prerequisites", "**SYNTHETIC series only**", "(f) VOID triggers", "**snapshot mismatch**",
                   "**P/L reconciliation**", "**15m mask violation**", "stop >= entry or a non-finite R",
                   "**a non-deterministic rerun**", "docs/intraday_p2_protocol.md"):
        assert phrase in text, phrase
    assert idata.WARMUP_EXTRA + idata.WILDER_LEN == 114


def test_no_cli_flag_can_change_a_registered_value():
    import run_intraday_p2 as p2

    ap_flags = re.findall(r'add_argument\("(--[a-z-]+)"', (_THIS_DIR / "run_intraday_p2.py").read_text())
    assert sorted(ap_flags) == sorted(["--family", "--workers", "--data-root", "--out-json", "--docs-dir", "--logs-dir"])
    assert p2.UNIVERSES["B"]["start"] == idata.B_START and p2.UNIVERSES["A"]["days"] == idata.A_DAYS
