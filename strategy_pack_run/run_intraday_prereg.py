"""run_intraday_prereg.py - the one entry point of docs/intraday_harness_changes.md H9 and the ONLY
writer of the official P2 outputs: runs the registered evaluation of all three families, in order
(snapshot check -> 72 base runs -> champions -> champion cost / exit / fill views -> placebo ->
bootstrap -> gates / verdicts), writing strategy_pack_run/intraday_p2_results.json atomically after
each family and docs/intraday_p2_family{1,2,3}.md.

    python strategy_pack_run/run_intraday_prereg.py --workers 4

Refuses to start on an uncommitted tree (strategy_pack_run/, src/, config/, docs/). Flags: --workers,
the data root and output paths only; every parameter, gate, window, seed, cost and universe is a
module constant copied from docs/intraday_prereg.md. Run exactly once, by P2. A
`run_intraday_p2.py --family N` run is PARTIAL and never writes these files.
"""
from __future__ import annotations

import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import run_intraday_p2  # noqa: E402

if __name__ == "__main__":
    run_intraday_p2.main(sys.argv[1:], official=True)
