"""mutation_check_intraday.py - AT19 of docs/intraday_harness_changes.md: every deliberate bug below
must be caught by at least one test of the intraday suite.

Each mutation is applied to a fresh temporary copy of strategy_pack_run/ (src/, config/ and docs/
are symlinked, read-only), then `pytest -x` runs the intraday test files there. A mutation is
KILLED if the suite fails. The working tree is never modified.

    python strategy_pack_run/mutation_check_intraday.py [--jobs 3] [--only N ...]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent

# (id, description, file, old, new). `old` must occur exactly once in `file`.
MUTATIONS = [
    ("M01", "AT19: family-1 signal reads bar t+1 (look-ahead)", "intraday_families.py",
     'out["raw_entry"] = (df_tf["close"] > channel).to_numpy()',
     'out["raw_entry"] = (df_tf["close"].shift(-1) > channel).to_numpy()'),
    ("M02", "family-2 signal reads bar t+1 (look-ahead)", "intraday_families.py",
     'out["raw_entry"] = (z <= -z_e).to_numpy()', 'out["raw_entry"] = (z.shift(-1) <= -z_e).to_numpy()'),
    ("M03", "AT19: regime / daily inputs read the forming day (daily bars labelled by open time)", "intraday_data.py",
     "    out.index = out.index + _tf_delta(target_tf)\n", "    out.index = out.index\n"),
    ("M04", "AT19: entry fills at the signal close", "intraday_runner.py",
     "raw_px = float(m.O[s, j])", "raw_px = close_t"),
    ("M05", "AT19: P6 same-bar commitment not counted", "intraday_runner.py",
     "                pending_commit[s] = (j, quote)\n                committed += quote\n",
     "                pending_commit[s] = (j, 0.0)\n"),
    ("M06", "AT19: 15m mask rule off", "intraday_data.py",
     "    if not is_masked_coin(coin):\n        return np.ones(len(t), dtype=bool)\n    step",
     "    if True:\n        return np.ones(len(t), dtype=bool)\n    step"),
    ("M07", "AT19: placebo shifts the post-regime entries", "run_intraday_p2.py",
     "raw=st.shift_raw(sd.raw, d, sd.bpd_dec)", "raw=st.shift_raw(sd.raw & sd.regime[None, :], d, sd.bpd_dec)"),
    ("M08", "AT19: placebo shift not shared across coins", "intraday_stats.py",
     "    return np.roll(raw, d * bars_per_day, axis=1)",
     "    return np.stack([np.roll(x, (d + i) * bars_per_day) for i, x in enumerate(raw)])"),
    ("M09", "AT19: bootstrap resamples trades instead of blocks", "intraday_stats.py",
     "    means = _resample_means(sums, counts, n, seed)", "    means = _resample_means(R.copy(), np.ones(len(R)), n, seed)"),
    ("M10", "AT19: purge disabled", "intraday_stats.py",
     "    is_m = (e < c) & (x < c)", "    is_m = e < c"),
    ("M11", "AT19: tiered cost uses day D instead of D-1", "intraday_runner.py",
     "through_day.index = med.index - pd.Timedelta(days=1)", "through_day.index = med.index - pd.Timedelta(days=2)"),
    ("M12", "wrong cost side: buys filled below the open", "intraday_runner.py",
     "fill_px = raw_px * (1.0 + slip)", "fill_px = raw_px * (1.0 - slip)"),
    ("M13", "off-by-one in the OOS cut date (ceil instead of floor)", "intraday_stats.py",
     "math.floor(IS_FRACTION * days)", "math.ceil(IS_FRACTION * days)"),
    ("M14", "off-by-one at the cut boundary (entry exactly at the cut not OOS)", "intraday_stats.py",
     "    oos = e >= c\n", "    oos = e > c\n"),
    ("M15", "wrong G2 percentile constant (2.5th instead of 0.0694th)", "intraday_stats.py",
     "G2_PERCENTILE = 0.0694 ", "G2_PERCENTILE = 2.5 "),
    ("M16", "G2 gate wired to the uncorrected 2.5th percentile", "run_intraday_p2.py",
     'g2_lower=boot["pct"][st.G2_PERCENTILE]', 'g2_lower=boot["pct"][2.5]'),
    ("M17", "unmasked indicator window (n instead of n + 100 bars before t)", "intraday_data.py",
     "    lo = t - (n + MASK_WARMUP_EXTRA_BARS + 1) * step", "    lo = t - (n + 1) * step"),
    ("M18", "trailing stop allowed to move down", "intraday_runner.py",
     "                Sd = np.maximum.accumulate(np.maximum(cand, cur))",
     "                Sd = np.where(np.isfinite(cand), np.maximum(cand, stop0), cur)"),
    ("M19", "time stop one bar late", "intraday_runner.py",
     "k_time = int(bars[p + n - 1]) if p + n - 1 < len(bars) else None",
     "k_time = int(bars[p + n]) if p + n < len(bars) else None"),
    ("M20", "maker fill without the 5 bp trade-through", "intraday_runner.py",
     "seg = m.L[s, lo:hi_k + 1] <= limit * (1.0 - MAKER_TRADE_THROUGH)", "seg = m.L[s, lo:hi_k + 1] <= limit"),
    ("M21", "gapped stop filled at the stop instead of the open", "intraday_runner.py",
     "        e, px, end = self._exec_after(s, k)\n        return dict(exit_k=e, px=px, reason=\"end\" if end else (\"trail\"",
     "        e, px, end = self._exec_after(s, k)\n        px = max(px, stop_level)\n        return dict(exit_k=e, px=px, reason=\"end\" if end else (\"trail\""),
    ("M22", "eligibility ignores the 60-daily-bar listing age", "intraday_data.py",
     "return (np.asarray(n_daily_bars, dtype=float) >= MIN_DAILY_BARS) & np.asarray(family_warm, dtype=bool)",
     "return np.asarray(family_warm, dtype=bool)"),
    ("M23", "exits queued on this bar free room (coin flat one bar early)", "intraday_runner.py",
     'busy_until[s] = m.Tb + 1 if ex["kind"] == "end" else k_x', 'busy_until[s] = m.Tb + 1 if ex["kind"] == "end" else k_x - 1'),
    ("M24", "touch model: target checked before the stop in the same bar", "intraday_runner.py",
     "                if st[off]:\n                    px = o if o <= stop else stop",
     "                if st[off] and not tg[off]:\n                    px = o if o <= stop else stop"),
    ("M25", "review m2: close-trigger stop fires on `<` instead of `<=` (families 1/3, trailing segments)", "intraday_runner.py",
     "(C[k0:k1 + 1] <= stop_eff)", "(C[k0:k1 + 1] < stop_eff)"),
    ("M26", "review m2: close-trigger stop fires on `<` instead of `<=` (family 2)", "intraday_runner.py",
     "            st = C[seg] <= stop\n", "            st = C[seg] < stop\n"),
    ("M27", "review m1: tier volume = daily close x day volume instead of sum(1h close x 1h volume)", "intraday_runner.py",
     "    qv = idata.daily_quote_volume_from_1h(df_1h)\n",
     "    _d = idata.daily_from_1h(df_1h)\n    qv = (_d['close'] * _d['volume']).astype(float)\n"),
    ("M28", "review m5: loader reads the whole file (appended rows / duplicates) instead of the hashed prefix", "intraday_data.py",
     "    raw = pd.read_csv(path, nrows=n_rows)\n    if len(raw) != n_rows:",
     "    raw = pd.read_csv(path)\n    if len(raw) < n_rows:"),
]

TEST_FILES = [
    "test_intraday_constants.py", "test_intraday_data.py", "test_intraday_stats.py", "test_intraday_execution.py",
    "test_intraday_runner_p2.py", "test_intraday_reference.py", "test_intraday_families.py",
]
COPY_DIRS = ("data_intraday", "data_long", "data")


def _make_copy(root: Path) -> Path:
    spr = root / "strategy_pack_run"
    spr.mkdir(parents=True)
    for p in _THIS_DIR.glob("*.py"):
        shutil.copy(p, spr / p.name)
    for d in COPY_DIRS:
        shutil.copytree(_THIS_DIR / d, spr / d)
    for d in ("src", "config", "docs"):
        (root / d).symlink_to(_REPO_ROOT / d)
    return spr


def run_one(mut) -> dict:
    mid, desc, fname, old, new = mut
    with tempfile.TemporaryDirectory(prefix=f"mut_{mid}_") as d:
        spr = _make_copy(Path(d))
        path = spr / fname
        text = path.read_text()
        n = text.count(old)
        if n != 1:
            return dict(id=mid, desc=desc, status=f"PATTERN x{n}", killed=False)
        path.write_text(text.replace(old, new))
        t0 = time.time()
        r = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *TEST_FILES],
                           cwd=spr, capture_output=True, text=True)
        lines = [ln for ln in r.stdout.splitlines() if ln.startswith("FAILED") or ln.startswith("ERROR")]
        return dict(id=mid, desc=desc, killed=r.returncode != 0, seconds=round(time.time() - t0, 1),
                    caught_by=lines[0] if lines else r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--json-out")
    a = ap.parse_args(argv)
    muts = [m for m in MUTATIONS if not a.only or m[0] in a.only]
    with ThreadPoolExecutor(a.jobs) as ex:
        res = list(ex.map(run_one, muts))
    for r in res:
        print(f"{r['id']} {'KILLED' if r['killed'] else 'SURVIVED'} {r.get('seconds', '')}s  {r['desc']}  <- {r.get('caught_by', r.get('status'))}")
    print(f"killed {sum(r['killed'] for r in res)} / {len(res)}")
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(res, indent=1))
    return res


if __name__ == "__main__":
    main()
