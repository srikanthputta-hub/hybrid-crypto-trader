"""run_intraday_prereg.py - the one entry point of docs/intraday_harness_changes.md H9: runs the
registered P2 evaluation of all three families, in order (snapshot check -> 72 base runs ->
champions -> champion cost / exit / fill views -> placebo -> bootstrap -> gates / verdicts ->
strategy_pack_run/intraday_p2_results.json and docs/intraday_p2_family{1,2,3}.md).

    python strategy_pack_run/run_intraday_prereg.py --workers 4

Flags: --workers and output paths (and the data root) only; every parameter, gate, window, seed,
cost and universe is a module constant copied from docs/intraday_prereg.md. Run exactly once, by P2.
"""
from __future__ import annotations

import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import run_intraday_p2  # noqa: E402

if __name__ == "__main__":
    run_intraday_p2.main(["--family", "all", *sys.argv[1:]])
