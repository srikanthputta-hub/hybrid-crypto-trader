# Intraday P2: the one-shot run protocol

The registered evaluation of `docs/intraday_prereg.md` (with Amendments 1 and 2) is run **exactly once**
(prereg §0 rule 1). This is the procedure. It changes no parameter, gate, window, seed, cost or universe; those
are constants in the code and cannot be set from the command line.

## Before the run

1. The P1 harness branch has been reviewed (Architect, Risk Officer) and **merged to `main`**; the reviewed
   commit is known.
2. On the machine with the data (`/home/user/hct-data`, or `$HCT_INTRADAY_DATA`): `git checkout main`,
   `git pull`, and confirm `git rev-parse HEAD` equals `origin/main`'s tip **and** the reviewed commit.
3. `git status` shows nothing uncommitted under `strategy_pack_run/`, `src/`, `config/` or `docs/` (gitignored
   files excepted). The runner refuses to start otherwise.
4. **Never run `run_intraday_p2.py --family N` (or `--family all`) on the real data before the official run.**
   Such a run is labelled PARTIAL and writes only to `strategy_pack_run/intraday_p2_logs/PARTIAL/`, but its
   numbers would still be a look at the evaluation data. Also never run any strategy code on real data after
   2021-03-01 outside the official run (prereg §0 rule 3).

## The run

```
python strategy_pack_run/run_intraday_prereg.py --workers 4
```

This is the only command. In order, it:
- refuses to start unless the tree is clean and HEAD equals `origin/main`'s tip after a `git fetch` (it prints
  both hashes when it refuses);
- verifies the snapshot prefix hashes (a mismatch makes that family VOID);
- records the code commit **once**, before any output exists, and stamps it on every family;
- records the SHA-256 of every harness source file (`strategy_pack_run/intraday_*.py`, `run_intraday_*.py`,
  `resample_ohlcv.py`, `src/risk_engine.py`, `src/monitor.py`) in the JSON and every report header. A hash that
  differs from the reviewed commit's file means the run is not the reviewed code: treat it as VOID;
- runs families 1, 2, 3. After each family it atomically writes `strategy_pack_run/intraday_p2_results.json`
  (`complete: false` until family 3 is done) and `docs/intraday_p2_family{N}.md`;
- stops a family at its first integrity failure and keeps only its integrity record (VOID, no numbers). The
  checks are: P/L reconciliation, 15m mask violation, approved trades with stop >= entry or non-finite R,
  non-deterministic rerun, snapshot mismatch.

Expected wall time on the 4-core / 15 GB machine: about 20-40 minutes (measured on full-size synthetic data).

## After the run

1. **Commit the outputs unmodified, before any interpretation**: `strategy_pack_run/intraday_p2_results.json`
   and `docs/intraday_p2_family{1,2,3}.md`, exactly as written. The per-trade and rejected-signal logs stay
   gitignored; their SHA-256 is in the JSON. Any commentary goes in a separate, later commit.
2. Review: P3 differential review of any PASS by independent Opus reviewers who re-run it (plan §2, prereg §6.7).
   The Architect checks that the recorded commit and source hashes are the reviewed ones. FAILs and
   INCONCLUSIVEs go to the owner with the numbers (ship rule, plan §5).

## If the run crashes or is interrupted

1. **Report whatever was already written and seen** (the JSON has every completed family, `complete: false`).
   Nothing seen may be un-seen; do not quietly restart.
2. A re-run is allowed only under prereg rule 0.2: write down first, in a committed note, what went wrong and why
   the fix is not tuning (a mechanics bug or an infrastructure failure, never a parameter, gate, window, universe,
   cost or seed change); commit the fix if there is one; then re-run **all three families in full** with
   `run_intraday_prereg.py`. Both the partial and the new outputs are reported.
3. The reviewers of step "After the run" also review the crash note and the re-run.
