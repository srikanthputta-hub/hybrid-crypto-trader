"""AT1 (docs/intraday_harness_changes.md): existing baselines byte-identical, pre vs post.

Slow (six daily report runs with 200 placebo sims, twice, plus the 1h fixture): opt-in with
HCT_RUN_AT1=1. See at1_baselines.py for the exact commands and the `--sims 200` note.

Run: HCT_RUN_AT1=1 pytest strategy_pack_run/test_intraday_at1_baselines.py -q
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import at1_baselines as at1  # noqa: E402

PRE_COMMIT = "afb73fb8"


def _have_commit() -> bool:
    r = subprocess.run(["git", "-C", str(_THIS_DIR.parent), "cat-file", "-e", PRE_COMMIT], capture_output=True)
    return r.returncode == 0


@pytest.mark.skipif(os.environ.get("HCT_RUN_AT1") != "1", reason="slow: set HCT_RUN_AT1=1")
@pytest.mark.skipif(not _have_commit(), reason=f"pre-change commit {PRE_COMMIT} not available")
def test_at1_baselines_byte_identical_pre_and_post(tmp_path):
    res = at1.main(["--pre-commit", PRE_COMMIT, "--out", str(tmp_path / "at1")])
    assert all(res["pre"].values()), res["pre"]     # committed reports reproduce on the pre-change commit
    assert all(res["post"].values()), res["post"]   # ... and after the change
    assert res["daily_pre_vs_post_diffs"] == []
    assert res["h1_pre_vs_post_diffs"] == []


def test_at1_resample_default_unchanged_vs_pre_change_file():
    """Cheap always-on part of AT1: the only pre-existing file this work touches, resample_ohlcv.py,
    gives byte-identical derived files with its default (15m) source, pre vs post, on the fixture."""
    if not _have_commit():
        pytest.skip("pre-change commit not available")
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        pre = at1.export_commit(PRE_COMMIT, d / "pre")
        at1.fixture_15m(d / "raw")
        for name, root in (("pre", pre), ("post", _THIS_DIR.parent)):
            code = ("import sys; sys.path.insert(0, '.'); from resample_ohlcv import derive_all_timeframes; "
                    f"derive_all_timeframes(r'{d / 'raw'}', r'{d / name / 'out'}', out_format='csv')")
            subprocess.run([sys.executable, "-c", code], cwd=root / "strategy_pack_run", check=True)
        assert at1.compare_trees(d / "pre" / "out", d / "post" / "out") == []
        assert len(list((d / "post" / "out").glob("*.csv"))) == 12
