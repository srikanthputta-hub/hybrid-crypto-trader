"""at1_baselines.py - AT1 of docs/intraday_harness_changes.md: the existing baselines must be
byte-identical after the intraday harness work.

Daily: the four run_live_bot_report.py commands of docs/min_order_floor_10_rebaseline.md and
run_report.py on data/ and data_long/, each output `cmp`-compared with the committed report.
NOTE: the committed report_kraken_720.md / report_long.md were produced with `--sims 200` (their
text says "200 sims"); run_report.py's default is 100, so the AT1 command line in the harness doc
(which omits the flag) cannot reproduce them. This script passes `--sims 200`, the only reading
under which the committed files are the reference.

1h: data_15m/ (P2.0's parquet files) is absent on this machine, so the 1h check runs on a
deterministic synthetic 15m fixture: derive 1h/1d with resample_ohlcv.derive_all_timeframes and
run `run_live_bot_report.py --timeframe 1h`, on the PRE-change checkout and on the current tree,
and `cmp` both the derived files and the reports.

    python strategy_pack_run/at1_baselines.py --pre-commit afb73fb8 --out /tmp/at1
"""
from __future__ import annotations

import argparse
import filecmp
import shutil
import subprocess
import sys
import tarfile
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent

DAILY = [
    ("report_data_p6off.md", ["run_live_bot_report.py", "--data-dir", "data", "--cost", "kraken_T3_taker", "--sims", "200", "--equity", "10000"],
     "min_order_floor_study/rebaseline_floor10/report_data_p6off.md"),
    ("report_data_p6on.md", ["run_live_bot_report.py", "--data-dir", "data", "--cost", "kraken_T3_taker", "--sims", "200", "--equity", "10000",
                             "--count-same-bar-approvals"], "min_order_floor_study/rebaseline_floor10/report_data_p6on.md"),
    ("report_data_long_p6off.md", ["run_live_bot_report.py", "--data-dir", "data_long", "--cost", "kraken_T3_taker", "--sims", "200",
                                   "--equity", "10000"], "min_order_floor_study/rebaseline_floor10/report_data_long_p6off.md"),
    ("report_data_long_p6on.md", ["run_live_bot_report.py", "--data-dir", "data_long", "--cost", "kraken_T3_taker", "--sims", "200",
                                  "--equity", "10000", "--count-same-bar-approvals"],
     "min_order_floor_study/rebaseline_floor10/report_data_long_p6on.md"),
    ("report_kraken_720.md", ["run_report.py", "--data-dir", "data", "--timeframe", "1d", "--sims", "200"], "report_kraken_720.md"),
    ("report_long.md", ["run_report.py", "--data-dir", "data_long", "--timeframe", "1d", "--sims", "200"], "report_long.md"),
]


def export_commit(commit: str, dest: Path) -> Path:
    """`git archive` of src/, config/ and strategy_pack_run/ at `commit` into dest (read-only on the repo)."""
    dest.mkdir(parents=True, exist_ok=True)
    blob = subprocess.run(["git", "-C", str(_REPO_ROOT), "archive", commit, "src", "config", "strategy_pack_run"],
                          capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        tf.extractall(dest, filter="data")
    return dest


def _run(spr: Path, args: list[str], out: Path) -> int:
    return subprocess.run([sys.executable, *args, "--out", str(out)], cwd=spr, capture_output=True).returncode


def run_daily(checkout: Path, out_dir: Path, workers: int = 4) -> dict[str, bool]:
    spr = checkout / "strategy_pack_run"
    out_dir.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(workers) as ex:
        codes = list(ex.map(lambda d: _run(spr, d[1], out_dir / d[0]), DAILY))
    assert all(c == 0 for c in codes), codes
    return {name: filecmp.cmp(out_dir / name, spr / ref, shallow=False) for name, _, ref in DAILY}


def fixture_15m(dest: Path, coins=("BTC", "ETH", "SOL"), days: int = 150, seed: int = 0) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    n = days * 96
    idx = pd.date_range("2025-01-01", periods=n, freq="15min", tz="UTC")
    for c in coins:
        close = 100 * np.exp(np.cumsum(rng.normal(0.00002, 0.004, n)))
        o = np.r_[close[0], close[:-1]]
        df = pd.DataFrame({"timestamp": idx.as_unit("ms").asi8, "open": o, "high": np.maximum(o, close) * 1.001,
                           "low": np.minimum(o, close) * 0.999, "close": close, "volume": 1000 * (1 + rng.random(n))})
        df = df.drop(index=rng.choice(n, 40, replace=False))  # a few holes, never filled
        df.to_csv(dest / f"{c}_USD_15m.csv", index=False)


def run_1h(checkout: Path, out_dir: Path) -> Path:
    """Derive 1h/1d from the synthetic 15m fixture with the checkout's resample_ohlcv and run the
    1h live-bot report on it. Returns the directory holding the derived files and the report."""
    spr = checkout / "strategy_pack_run"
    fx = out_dir / "fixture"
    fixture_15m(fx / "raw")
    code = ("import sys; sys.path.insert(0, '.'); from resample_ohlcv import derive_all_timeframes; "
            f"derive_all_timeframes(r'{fx / 'raw'}', r'{fx / 'derived'}', out_format='csv')")
    subprocess.run([sys.executable, "-c", code], cwd=spr, check=True, capture_output=True)
    rc = _run(spr, ["run_live_bot_report.py", "--data-dir", str(fx / "derived"), "--timeframe", "1h", "--sims", "20"],
              out_dir / "report_1h_fixture.md")
    assert rc == 0
    return out_dir


def compare_trees(a: Path, b: Path) -> list[str]:
    diffs = []
    for p in sorted(a.rglob("*")):
        if p.is_file():
            q = b / p.relative_to(a)
            if not q.exists() or not filecmp.cmp(p, q, shallow=False):
                diffs.append(str(p.relative_to(a)))
    return diffs


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pre-commit", default="afb73fb8", help="the commit before the intraday harness work")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    out = Path(a.out)
    if out.exists():
        shutil.rmtree(out)
    pre = export_commit(a.pre_commit, out / "pre_checkout")
    res = {"pre": run_daily(pre, out / "pre_daily"), "post": run_daily(_REPO_ROOT, out / "post_daily")}
    run_1h(pre, out / "pre_1h")
    run_1h(_REPO_ROOT, out / "post_1h")
    res["daily_pre_vs_post_diffs"] = compare_trees(out / "pre_daily", out / "post_daily")
    res["h1_pre_vs_post_diffs"] = compare_trees(out / "pre_1h", out / "post_1h")
    print(res)
    ok = all(res["post"].values()) and not res["daily_pre_vs_post_diffs"] and not res["h1_pre_vs_post_diffs"]
    print("AT1", "PASS" if ok else "FAIL")
    return res


if __name__ == "__main__":
    main()
