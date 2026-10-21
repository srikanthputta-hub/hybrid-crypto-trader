# Intraday P1 harness: what was built, how it was verified, open points

- **Role:** QA/Tester (owner of `strategy_pack_run/` and its tests). Branch `claude/intraday-harness` off `origin/main`
  `afb73fb8`. Builds `docs/intraday_harness_changes.md` (H1-H9) for `docs/intraday_prereg.md`. Research code only:
  nothing under `src/`, `config/`, `scripts/` or the live bot changed, and no committed report changed.
- **No registered evaluation was run, and no real-data strategy result was looked at** (prereg §0 rules 1 and 3).
  On the real snapshot only the loaders, the prefix-hash guard and the daily-vs-`data_long` cross-check ran, plus AT18's
  trade-list reconciliation on BTC/ETH/LTC bars before 2021-03-01 (allowed by rule 3; trade lists only).

## Files (all under `strategy_pack_run/`)

| file | role |
|---|---|
| `intraday_data.py` | H1/H7: loader (`load_native`, close-time index, no fill), `verify_snapshot` (prefix rule), `daily_from_1h` / `four_hour_from_1h`, mixed-source guard, universes, 15m masks, eligibility, BTC regime, `align` |
| `resample_ohlcv.py` | H2: one additive keyword `source_minutes` (default 15, byte-identical for every existing caller) |
| `intraday_families.py` | H3: frozen grids, `family1_signals`, `family2_signals`, `family3_state`, rank helper, R2 neighbour rule |
| `intraday_runner.py` | H4/H5: `Market`, `SetData`, `Simulator` (close / touch exits, taker / maker fills, P6 accounting, real `RiskEngine`), cost views incl. tiered, H8 trade log |
| `intraday_stats.py` | H6: purge, champion, folds, block bootstrap, Sharpe, benchmark, placebo shifts, DSR, gates, verdict |
| `run_intraday_p2.py` | the registered sequence for one family or all (`--family`), JSON + report + gitignored logs |
| `run_intraday_prereg.py` | H9 entry point: all three families |
| `intraday_reference.py` | independent bar-by-bar reference written from the prereg text (tests only) |
| `intraday_synthetic.py`, `intraday_fixtures.py` | synthetic universes for tests and validation |
| `at1_baselines.py`, `mutation_check_intraday.py`, `validate_intraday_synthetic.py` | AT1 runner, AT19 mutation runner, planted-edge / noise validation |
| `test_intraday_*.py` | AT1-AT20 (map below) |

Run: `python strategy_pack_run/run_intraday_prereg.py --workers 4` (data root: `--data-root`, else `$HCT_INTRADAY_DATA`,
else `/home/user/hct-data`). It verifies the snapshot first and writes VOID on a mismatch.

## Acceptance tests

| AT | where |
|---|---|
| AT1 | `test_intraday_at1_baselines.py` (full run opt-in: `HCT_RUN_AT1=1`), `at1_baselines.py` |
| AT2 | the existing suites, `scripts/validate_invariants.py`, `research/v2_signals.py` (run, unmodified) |
| AT3-AT7, H7 | `test_intraday_data.py` (+ simulator parts of AT7 in `test_intraday_execution.py`) |
| AT8 | `test_intraday_families.py` (every one of the last 600 bars, 15m / 1h / 4h, all families) + simulator-level future-perturbation test |
| AT9, AT10, AT12-AT14, AT17 | `test_intraday_execution.py` |
| AT11, AT18 | `test_intraday_reference.py` |
| AT15, AT16, gates / verdict | `test_intraday_stats.py`, `test_intraday_runner_p2.py` |
| AT19 | `mutation_check_intraday.py` (24 mutations) |
| AT20 | `test_intraday_constants.py` (each constant and the same literal in the prereg text) |

## Readings taken where the docs are silent, ambiguous or inconsistent (all chosen to be the conservative one)

1. **Runner name.** The brief asks for `run_intraday_p2.py`; H9 names `run_intraday_prereg.py`. Both exist; the latter
   calls the former with `--family all`.
2. **AT1 command line.** The committed `report_kraken_720.md` / `report_long.md` say "200 sims"; `run_report.py`'s
   default is 100, so AT1's command as written cannot reproduce them. `at1_baselines.py` passes `--sims 200`. With it,
   all six committed daily reports reproduce byte-identically on the pre-change commit and after the change: no
   pre-existing drift.
3. **Warm-up vs. "AVAX/SHIB join about 60 days in".** §1.2's own rule (Wilder series need >= 100 + 14 bars) applies
   to ATR_D / ADX_D, which every family uses, so a coin needs >= 114 complete daily bars, not 60. Implemented the
   stricter rule.
4. **Placebo draw without a champion** (no set with >= 150 purged IS trades): not specified. Counted as a draw the real
   champion does NOT beat in G4; the count is reported. (On synthetic noise, shifted family-2 runs keep ~20-30% of
   the real run's trades because the target is re-set to SMA_n at the landing bar and the 1.5 R:R check vetoes most
   landings; draw champions still exist there, but family 2 is the family where this convention can bind.)
5. **Family 3 "holds at most K coins by construction" (§1.4)** is not true for b = 2 under §4's explicit rules (entries
   = target coins not held; held coins survive to rank K + b). Implemented §4's rules as written (up to K + b held);
   the 10% / 40% caps are unchanged.
6. **15m mask interval.** "t-(n+100) bars to t+H bars" is read on both bar-label conventions at once: the calendar span
   from t-(n+101)x15m to t+(H+1)x15m must not touch a masked day (also keeps the time-stop exit-fill bar out).
7. **Fee base.** "Fee charged on the filled notional" and "spend exactly max_quote_amount": notional = quote / (1 +
   fee), fee = notional x fee rate. (`backtest_live_bot` charges quote x fee; the difference is fee^2, ~0.0014%.)
8. **Fills queued on an earlier bar and not yet executed** (coin missing its next bar) count as committed quote in P6
   accounting, like same-bar ones.
9. **Trailing stop order.** On a close that is both a base and a decision close, the stop is checked against the stop in
   effect before that close; the update applies from the next base close ("after each decision-bar close").
10. **`atr_pct`** = ATR_D / the close of the same completed day.
11. **G2 percentile** = 0.0694 as printed (exact 100 x 0.025/36 = 0.069444...; the printed value is the lower one).
12. **Universe A IS/OOS columns** (no registered cut for A) use B's cut, labelled information only.
13. **Bootstrap design effect**: the iid comparison and the 7-day CI each use their own `default_rng(1)` (info only).
14. **Kill switch**: the harness uses a dedicated never-created path, so a stray `KILL_SWITCH` file in the repo root
    cannot silently empty a run.
15. **Maker model**: family 2's resting target cannot fill on the maker entry-fill bar (intrabar order unknown).

Readings 3, 4 and 5 above, the stop boundary and the end-of-series rule are now written into the prereg as
**Amendment 2 (pre-run, 2026-10-21)**, `docs/intraday_prereg.md` §10, together with the Risk Officer's P4
prerequisites (not part of P2).

## Review fixes (2026-10-21, before any run)

Risk Officer: ready for P2. Architect: ready after fixes. Applied on this branch, each with tests and a mutation:

| item | fix |
|---|---|
| M1 | `run()` refuses a dirty tree (strategy_pack_run/, src/, config/, docs/); an integrity failure stops right after the base runs and keeps only the integrity record (VOID, no number printed or rendered); the results JSON is written atomically after each family (`complete` false until the last); official outputs only from `run_intraday_prereg.py` (all families, real snapshot, 200 draws), `run_intraday_p2.py --family` writes a labelled PARTIAL record under `intraday_p2_logs/PARTIAL/` |
| m1 | tiered-slippage daily quote volume = sum over the day's 1h bars of (1h close x 1h volume), complete UTC days |
| m2 | close-trigger stop on `close <= stop` (as `src/monitor.py`), runner and reference |
| m3 | maker report: 1h / 4h post-fill returns of filled orders vs the taker run's matching trades of missed orders |
| m4 | tests for the mask-violation integrity check and its wiring |
| m5 | the loader reads only the hashed snapshot prefix (an appended duplicate cannot replace a verified bar) |
| n1 | a series ending before the window end stays open (marked at its last close) until the window end; runner == reference on seeded fuzz cases |
| n5 | the real-provider override test uses a temp root |
| Risk Officer | VOID on approved trades with stop >= entry or non-finite R; information row on losses beyond the volatility-scaled risk budget (count, worst multiple); `volatility_scale_floor` forwarded only if it equals the engine default; `atr_pct` never None (missing = NaN, rejected by the engine); max concurrent holdings, join dates and the no-champion "decisive" flag in the report |

Re-verification fixes (Architect, on 7095cb35):

| item | fix |
|---|---|
| R1 | the code label is computed once in `run()` before any output exists and stamped on every family (tested in a real temporary git repo with a bare origin) |
| R2 | incomplete-UTC-day test for the tiered daily quote volume |
| R3 | SHA-256 of every harness source file in the JSON and report headers; the official run refuses unless HEAD is `origin/main`'s tip after a fetch; `docs/intraday_p2_protocol.md` (the one-shot procedure) |
| R4 | the 3.5% wall-clock flip figure in the P4 prerequisites is marked synthetic-data-only |
| R5 | Amendment 2 (f) names the VOID triggers |

## What remains

P1 review (Architect on harness semantics, Risk Officer on the risk-engine wiring, H4), then P2: one run of
`run_intraday_prereg.py` on the committed hash, then the three reports. Measured performance and the synthetic
validation results are in the PR / hand-off report of this branch.
