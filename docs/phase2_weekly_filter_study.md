# Phase 2 (re-scoped): weekly-trend and BTC-regime confirmation for the daily pipeline (study)

- **Role:** Market Analyst (Opus). **Research only.** Only new harness/test/doc files are added under
  `strategy_pack_run/` and `docs/`. Nothing under `src/` or `config/` changes. `risk_engine.py`,
  `broker_live.py` and the live-trading gate are untouched, and so is every existing harness module.
  **The daily paper trial's strategy is frozen. Nothing here changes it.** Any future change would need
  the user's approval after the trial.
- **Branch:** `claude/phase2-weekly-filter-study`, off `origin/main` at `0e0959c`. Local only, not pushed.
- **Design references:** `docs/multiday_hold_design.md` §7 (Phase 2 re-scoped to "1d base + 1w bias",
  later a BTC regime gate). `docs/system-redesign-2026-10.md` §5 is on branch
  `origin/claude/system-redesign-design` (MTF stage design and the look-ahead hazard).
  `docs/p6_harness_exposure_fix.md` and `docs/min_order_floor_10_rebaseline.md` cover the harness and
  baselines. `docs/crypto_bot_upgrade_playbook.md` §4 Layer 2 covers the regime filter.

## 0. Pre-registration (committed BEFORE any candidate was run)

Written and committed before running any candidate. Sections 1+ are added afterwards and do not edit
this section. If a bug forces a rerun, the post-results sections say so.

### 0.1 Question

Does adding (W1) a **weekly** higher-timeframe trend confirmation, (W2) a **BTC daily regime** veto, or
(W3) both, to the **unchanged** daily live pipeline improve it under the 5-gate method? The answer is
informational either way.

### 0.2 Already known before writing this (disclosed, not hidden)

- **Baselines** (P6 ON, `min_order_quote_usd` 10, `kraken_T3_taker`; `docs/min_order_floor_10_rebaseline.md`):
  - `data/`: 132 trades, avg R 0.22 [-0.09, 0.53], return 14.3%, Sharpe 0.51, 4/5 (fails CI-low).
  - `data_long/`: 546 trades, avg R 0.50 [0.34, 0.65], return 342.9%, Sharpe 1.01, 5/5.
  - Before writing this, I re-ran only the two baseline simulations, to time the harness. They
    reproduce exactly: 132 / 0.2158 and 546 / 0.4965. No candidate has been run.
- **Fragility** (reported by the CEO session; not yet reproduced here): dropping the first 14 bars of
  `data/` moves the baseline from 132 trades / +0.216R to 114 trades / -0.006R.
- **Regime facts known from earlier docs.** These motivate W2, so hindsight bias is a real risk:
  - The strategy lost money in 2018, 2022 and 2025 (multiday design E4/R4). These are broadly
    BTC bear years.
  - On `data/`, the profit comes mostly from Q4-2024 (29 trades, +1.32R). 2025 lost (-0.32R).
  - Random entries during htf-up periods already earn +0.20R net on `data_long/` (E4).
  - W2's rule comes from the playbook and the literature, not from these numbers. But the hypothesis
    is not independent of this data, and the reader should discount it accordingly.
- **Data facts verified before writing this** (alignment and seed checks only, no strategy run):
  - **Weekly bins.** On pandas 3.0.6, `resample("168h", origin="epoch")` and
    `resample("W-THU", closed="left", label="left")` give identical Thursday-00:00 bins on daily
    bar-open times. `resample("7D", origin="epoch")` ignores `origin` (RuntimeWarning) and gives
    Saturday bins on `data/`, which is the trap the design doc warned about.
  - **Kraken check.** I fetched Kraken's public native 1w candles once, for verification only; they
    are not a study input. All **1605 of 1605** full weeks across the 16 `data/` symbols have
    weekly closes resampled from the committed Kraken daily bars that equal the native 1w close
    exactly (rel. diff 0). Native weekly history starts in 2013-2021 for every symbol except BNB
    (first week 2025-04-17). So the live bot could compute a fully warmed weekly EMA for 15 of 16
    symbols on day 1 of `data/`.
  - **Coinbase vs Kraken over the 2-year overlap.** Median relative daily-close difference is
    0.01-0.09% per symbol. The W1 verdict (as defined below) is identical in **1580 of 1595**
    symbol-weeks between Coinbase-seeded and Coinbase-only weekly series. The only disagreements are
    BNB, where the Coinbase history is shorter. The BTC SMA200 regime is identical on **719 of 719**
    days.

### 0.3 Candidates (at most three; fixed; nothing else will be tried or reported as a candidate)

All three are **entry vetoes only**: a signal that fails the gate never becomes a plan. They never
originate or resize a trade. Open/pending positions, exits, stops, sizing and every other stage stay
unchanged. Each gate acts on the final bullish, htf-confirmed signal set (stage 2b output), as a
further context gate, the same slot the design's `mtf_confluence` would take.

| id | rule at daily decision bar t (close time t) | written reason |
|---|---|---|
| **W1** weekly trend gate | `src.htf_filter.daily_trend_confirmed_from_df(W.tail(31), 12, 26)` is True. `W` is the series of the symbol's **closed weekly closes** with weekly close time <= t (§0.4). That means fast EMA(12) > slow EMA(26) on the last closed weekly bar, failing closed with fewer than 26 weekly bars. | **Scaling rule:** the same function, the same EMA bar counts (`indicators.fast_ema`/`slow_ema` = 12/26) and the same live fetch-length rule `max(slow+5, 30)` = 31 bars that `htf_filter.daily_trend_confirmed` uses, with the timeframe moved up one step, 1d -> 1w. Horizons scale with the timeframe (x7), which is the standard top-down MTF ratio. This is exactly design §7's "weekly fast > slow EMA, required", i.e. the `GateVerdict` slot-ii design with different timeframes. Why: at 1d, today's htf gate tests the same frame as the scanner and passes about 95% of signals (design §3.1), so it adds almost no independent information. A weekly bias is the next-slower trend horizon. No parameter is fitted. |
| **W2** BTC regime veto | `research.v2_signals.regime_on(BTC daily closes, [t])` is True: the latest **completed** BTC daily close at or before t is above its SMA(200), failing closed while the SMA has fewer than 200 bars. Applies to every symbol, BTC included. | Playbook §4 Layer 2: "BTC close above its 200-day SMA". It is the repo's own already-tested implementation (`research/v2_signals.py` self-tests), reused unchanged. A market-wide bear regime hurts long-only trend entries across all coins (playbook §2/§3). Entry veto only: the playbook's "exit / tighten stops when the regime flips OFF" is a monitor/exit change and is **not** tested here. |
| **W3** both | W1 AND W2 | Tests whether the two vetoes are complementary (coin-level trend vs market-level regime). |

**Explicitly not candidates** (not run, not reported as candidates): other EMA/SMA lengths; the
redesign's "close > slow EMA" extra condition; 4h/15m layers; a coin's own SMA100; regime-off exits;
weekly bars from a different week anchor.

### 0.4 No look-ahead: data availability rules

- **Daily axis.** The harness indexes each daily bar by its CLOSE time (`load_csv`: open + 1 day).
  Decisions are made at bar t's close. Fills happen at t+1's open (unchanged harness).
- **Weekly bars.** Daily bars are binned by their **open** time with `resample("168h", origin="epoch")`,
  which gives Thursday 00:00 UTC bins (the Kraken 1w anchor, verified in §0.2). The weekly close is
  the last daily close in the bin. The weekly bar's close time is bin start + 7 days, i.e. the next
  Thursday 00:00. Bins with no daily bar (data gaps) are dropped, not filled.
- **Availability.** At decision time t, a weekly bar is usable iff its close time <= t. Example: a
  daily decision at Monday 00:00 sees only the weekly bar that closed the previous Thursday 00:00.
  The decision at Thursday 00:00 (the close of Wednesday's daily bar) sees the week that closed at
  that same instant, because all 7 of its daily bars are closed. This is the same rule as
  `compute_symbol_signals`' `daily_closes.loc[:as_of]` and as live `fetch_ohlcv_df` (the forming bar is
  dropped). A forming week at the dataset's end is never usable, since its close time is after the
  last daily close.
- **BTC regime.** `regime_on` forward-fills the flag from BTC daily close times onto the decision
  times, so only BTC daily bars with close time <= t are used.
- **Warm-up seed (`data/` only).** Live can fetch years of Kraken 1w/1d history (§0.2), but `data/`
  starts on 2024-10-12. Without a seed, W1 would be blocked for the first 26 weeks and W2 for 200
  days. That would be an artifact of the dataset's start, not something the live bot would see, and
  it would remove Q4-2024, the window that drives the `data/` result.
  - **Rule:** each gate's input close series is `data_long/` (Coinbase) closes **strictly before** the
    symbol's first `data/` close time, then `data/` (Kraken) closes from there on.
  - Gate inputs only. The scanner, indicators and portfolio are not seeded.
  - BNB has no earlier Coinbase history, so it is not seeded. That matches live, where Kraken BNB
    history starts in 2025-04.
  - `data_long/` gates use only `data_long/`, with no seed.
  - **Information-only row (not a candidate, not used in the verdict):** W1/W2/W3 on `data/` *without*
    the seed, so the size of the warm-up effect is visible.
- **Gate series vs offsets.** Each gate's verdict series is computed from the full input history. By
  the truncation-invariance property tested in §0.9, the verdict at t is identical to one computed
  from data up to t only. So start offsets (§0.6) change only the pipeline's own warm-up and the
  portfolio path, never what the gate knows at t. Live, too, knows the pre-start weekly history.

### 0.5 Harness, data, costs, placebo, split (identical for baseline and candidates)

- **Harness.** `strategy_pack_run/backtest_live_bot.py`, unmodified:
  - `simulate_portfolio(..., count_same_bar_approvals=True)` (P6, harness_version
    `p6-same-bar-exposure-v1`), sorted order, flat costs, `kraken_T3_taker` (0.38% fee + 5 bp
    slippage per side; spot, so no funding).
  - `config/config.yaml` as on `origin/main`: `risk.min_order_quote_usd` 10, every other value
    unchanged. Equity 10,000, PPY 365.
  - Signals come from `run_live_bot_report.build_live_signals` (scanner -> signal_engine -> htf, at
    1d). A candidate is the same signal dict with vetoed (symbol, bar) entries removed. Nothing else
    changes.
- **Datasets.** `data/` (Kraken USD daily, 2 years) and `data_long/` (Coinbase USD daily, ~11 years),
  as committed.
- **Placebo.** `placebo_run(..., n_sims=200, seed=0, count_same_bar_approvals=True)` on each run's
  **own final signal set**, the same convention as the baseline's htf gate: the gate is not
  re-applied at shifted landing bars. The axis length is the same for a baseline and its candidates
  at the same offset, so the 200 shift offsets are identical (paired).
- **Split.** Later 30% = trades with entry time >= t0 + 0.7 (t1 - t0) on that run's axis, as in
  `run_live_bot_report.py`.
- **Five gates**, as in `run_live_bot_report.py`:
  1. >= 100 trades.
  2. iid bootstrap avg-R CI low > 0 (`engine.bootstrap_ci`, 5000 draws, seed 0).
  3. Later-30% avg R > 0.
  4. Placebo >= 90th percentile on both return and avg R.
  5. Return > 0 and Sharpe > equal-weight buy&hold Sharpe on the same frames.

### 0.6 Metrics and start offsets

- **Per run:** trades; avg R with the iid bootstrap CI; avg R with a **day-block** bootstrap CI
  (whole UTC entry days resampled with replacement, 5000 draws, seed 0, percentile 2.5/97.5, as in
  the multiday design); return; Sharpe; max DD; later-30% n / avg R; placebo percentiles; gates 1-5.
- **Also reported:**
  - The veto rate (share of baseline signals each gate removes) and the share of days each gate is ON.
  - A per-entry-year table (n, avg R) for the baseline and each candidate (offset 0).
  - Trades removed vs added relative to the baseline, matched by (symbol, entry_time), with their
    avg R.
- **Start offsets** k in {0, 7, 14, 21}: every symbol's raw daily frame drops its own first k rows
  (`df.iloc[k:]`) before indicators are computed. On `data/` this equals starting the backtest k days
  later (BNB starts k bars after its own listing). On `data_long/` it shifts each coin's listing
  warm-up. Baseline and candidates are run at every offset with identical settings, the full 5 gates
  and the 200-sim placebo. The caller's 14-bar result (114 / -0.006) is expected to reproduce under
  this definition. If it does not, that is reported and the definition above still stands.

### 0.7 Decision rule (fixed before results)

**Notation.** B(D,k) is the baseline and C(D,k) the candidate, on dataset D at offset k.
Δ(D,k) = avgR_C - avgR_B.

A candidate is **"promising"** only if **all** of the following hold:

- **(i) No gate regression.** For both datasets and **every** offset, no gate that B(D,k) passes is
  failed by C(D,k).
- **(ii) avg R improves beyond the day-block noise on BOTH datasets (offset 0).**
  - **Tolerance:** the **paired day-block bootstrap** 95% CI of Δ(D,0) must have its lower bound
    above 0.
  - **Method:** blocks are the union of both runs' UTC entry days. 5000 resamples, seed 0. In each
    resample, the same drawn days (with multiplicity) give mean R over B's trades and over C's trades
    on those days, and Δ is their difference. Resamples where either side has no trade are skipped
    and counted.
  - **Why paired:** the trade lists overlap heavily, so the unpaired CIs (each about ±0.3R on
    `data/`) would hide any realistic improvement.
- **(iii) Out of sample and not one regime year (offset 0, both datasets).**
  - (a) Δ on the later-30% trades > 0.
  - (b) Leave-one-entry-year-out: for every calendar entry year y present in either run,
    Δ computed without year-y trades is > 0.
- **(iv) Trade count.** n_C(D,k) >= 0.7 n_B(D,k) for both datasets at every offset.
- **(v) Across offsets.** Δ(D,k) > 0 (point estimate) for both datasets at **every** k in
  {0, 7, 14, 21}. One offset is not enough.

**Outcomes:**
- If no candidate passes, the outcome is **"no change recommended"**.
- A "promising" result is still only a reason to propose a separately pre-registered paper shadow or
  trial **after** the current trial ends, with the user's approval. It is never a config or `src/`
  change from this study.
- No threshold, rule, parameter or candidate changes after results are seen. No candidates are added.
- **Multiple comparisons:** 3 candidates, each required to pass on both datasets and at 4 offsets.
  The rule is conjunctive, so a chance pass is unlikely. No further correction is applied.

### 0.8 What would make me distrust a pass (reported regardless)

- The improvement comes only from blocking 2018/2022/2025 on `data_long/`, with no gain elsewhere.
  Criterion (iii)(b) is the formal check.
- The gain on `data/` comes only from warm-up effects (compare with the information-only no-seed row).
- Most of the change in avg R comes from **added** path trades rather than removed bad ones.

### 0.9 Tests (written before the gate run)

New file `strategy_pack_run/test_phase2_weekly_filter.py`, with these tests:

1. **Weekly-bin alignment.**
   - Bins start on Thursday 00:00 UTC.
   - "168h"-epoch equals W-THU left/left.
   - A real-data check: weekly closes resampled from committed `data/` BTC equal Kraken's native
     1w closes (hard-coded values from the §0.2 fetch).
2. **Availability.** On a synthetic series built so the weekly verdict flips with the week that closes
   Thursday 00:00:
   - the gate at Wednesday 00:00 and at Thursday 00:00 minus one day still uses the previous week;
   - the flip happens exactly at the Thursday 00:00 decision.
3. **Truncation invariance** (W1, W2, W3). For many t, the verdict computed from data truncated at t
   equals the verdict from the full series. Same for the stitched (seeded) series.
4. **Fail-closed warm-up.** W1 is False with fewer than 26 closed weekly bars. W2 is False with fewer
   than 200 BTC bars.
5. **Seed stitching.** Seed rows at or after the primary start are never used. The primary is never
   overwritten.
6. **Signal filtering.** Vetoed (symbol, bar) entries are removed, others are kept by identity, and
   the input dict is not mutated. An all-True gate gives a `simulate_portfolio` result identical to
   the baseline.
7. **Statistics helpers.**
   - Day-block bootstrap on a hand-built list.
   - The paired-Δ bootstrap returns exactly 0 width when B == C.
   - Leave-one-year-out arithmetic.
8. **Mutation checks** (applied by hand, then reverted; reported in §2). Each must be caught by at
   least one test:
   - weekly bar usable at its open time instead of its close time;
   - "7D" bins instead of "168h"-epoch;
   - fail-open on short weekly history;
   - BTC regime using t+1's close;
   - seed overwriting primary rows;
   - gate filter a no-op.

Finally, run root `python -m pytest -q`, `cd strategy_pack_run && python -m pytest -q`,
`PYTHONPATH=. python scripts/validate_invariants.py` and `python research/v2_signals.py`.

---

_Everything below was added after the pre-registration commit (`3f48ff6`). §0 is unchanged._

## 1. What was built

| File | What |
|---|---|
| `strategy_pack_run/phase2_weekly_filter.py` | The study-only gates, plus the statistics helpers (day-block CI, paired-Δ CI, leave-one-year-out, removed/added trades).<br>- **Weekly closes:** `weekly_closes`, 168h-epoch Thursday bins, indexed by weekly close time.<br>- **Seed:** `stitch_closes`.<br>- **W1:** `weekly_trend_at` / `weekly_gate_series`, calling `src.htf_filter.daily_trend_confirmed_from_df` unchanged on `tail(max(slow+5,30))`.<br>- **W2:** `btc_regime_series` (`research.v2_signals.regime_on` unchanged).<br>- **Veto:** `apply_gate`, which removes vetoed (symbol, bar) signals.<br>No `src/` import is modified. Nothing in `src/` imports it. |
| `strategy_pack_run/test_phase2_weekly_filter.py` | 17 tests, covering §0.9 items 1-7. |
| `strategy_pack_run/run_phase2_weekly_filter_study.py` | Driver. Unmodified `build_enriched_and_signals`, `simulate_portfolio` and `placebo_run` (P6 on), `kraken_T3_taker`, config floor 10 (asserted), 4 offsets x 2 datasets, 200-sim placebo, seed 0. |
| `strategy_pack_run/render_phase2_weekly_filter_study.py` | Renders `results.json` into the tables below and applies the §0.7 rule mechanically. |
| `strategy_pack_run/phase2_weekly_filter_study/` | `results.json`, `summary.md` (every table, including the full offset grid) and the offset-0 trade lists. |

Reproduce from `strategy_pack_run/`:

    python run_phase2_weekly_filter_study.py --out-dir phase2_weekly_filter_study
    python render_phase2_weekly_filter_study.py --out-dir phase2_weekly_filter_study

The run takes about 20 minutes on 4 cores.

## 2. Verification

- **Baselines reproduce exactly** (offset 0): `data/` 132 trades, 0.216 [-0.09, 0.53], 14.3%,
  Sharpe 0.51, 4/5; `data_long/` 546, 0.496 [0.34, 0.65], 342.9%, 1.01, 5/5.
- **The CEO session's fragility finding reproduces exactly** under the pre-registered offset
  definition (`df.iloc[k:]` per symbol): `data/` k=14 baseline gives **114 trades, -0.006R**.
- **Weekly alignment** (§0.2): 1605/1605 resampled weekly closes equal Kraken's native 1w closes.
- **Seed validation (added after the run; not a decision input).** Live would compute W1 from Kraken's
  native weekly history. I re-fetched that history once and ran the same `weekly_gate_series` on it.
  It gives the **same W1 verdict as the study's seeded series on 11,305 of 11,312 `data/` daily
  decisions** (99.94%). The only differences are 7 AVAX days. So on `data/`, the seeded W1 is
  effectively what the live bot would have seen.
- **Tests:** `test_phase2_weekly_filter.py` 17/17 pass.
- **Mutation check** (each applied by hand, tests run, then restored; `cmp` confirms byte-identical
  restore):

| mutation | tests failing (of 17) |
|---|---|
| M1 weekly bar usable at its open time (look-ahead) | 6 (alignment x2, availability, truncation x3) |
| M2 `"7D"` bins instead of `"168h"`-epoch | 3 (alignment x2, Monday/Thursday test) |
| M3 fail-open on short weekly history | 3 |
| M4 BTC regime uses t+1's close | 2 (truncation invariance) |
| M5 seed overwrites primary rows | 1 |
| M6 gate filter is a no-op | 2 |

## 3. Results (kraken_T3_taker, P6 on, floor 10, 200-sim placebo)

### 3.1 Offset 0 (canonical)

| dataset | run | veto | trades | avg R [iid CI] | day-block CI | return | maxDD | Sharpe (EW BH) | later-30% n / R | placebo ret / R | gates 1-5 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `data/` | **B** | - | 132 | 0.22 [-0.09, 0.53] | [-0.16, 0.59] | 14.3% | -23.5% | 0.51 (0.28) | 43 / 0.50 | 96 / 96 | P F P P P (4/5) |
| `data/` | W1 | 65.8% | 53 | 0.03 [-0.41, 0.48] | [-0.46, 0.54] | 1.1% | -18.4% | 0.11 | 7 / 0.36 | 64 / 66 | F F P F F (1/5) |
| `data/` | W2 | 17.2% | 96 | 0.32 [-0.02, 0.69] | [-0.12, 0.73] | 21.1% | -15.7% | 0.78 | 16 / 0.77 | 98 / 93 | **F** F P P P (3/5) |
| `data/` | W3 | 66.8% | 51 | 0.09 [-0.37, 0.56] | [-0.41, 0.62] | 3.7% | -16.4% | 0.22 | 7 / 0.36 | 74 / 74 | F F P F F (1/5) |
| `data_long/` | **B** | - | 546 | 0.50 [0.34, 0.65] | [0.33, 0.67] | 342.9% | -28.7% | 1.01 (0.81) | 233 / 0.26 | 100 / 98 | P P P P P (5/5) |
| `data_long/` | W1 | 50.0% | 293 | 0.54 [0.31, 0.76] | [0.30, 0.77] | 180.6% | -22.8% | 0.97 | 131 / **0.05** | 100 / 90 | P P P P P (5/5) |
| `data_long/` | W2 | 19.4% | 439 | 0.50 [0.32, 0.67] | [0.31, 0.68] | 297.1% | -21.7% | 1.06 | 209 / 0.28 | 100 / 92 | P P P P P (5/5) |
| `data_long/` | W3 | 52.0% | 284 | 0.54 [0.31, 0.77] | [0.30, 0.79] | 173.9% | -22.2% | 0.95 | 128 / 0.05 | 100 / **89** | P P P **F** P (4/5) |

"veto" is the share of the baseline's bullish, htf-confirmed signals that the gate removes.

Information-only rows (no seed, `data/`; not candidates): W1 29 trades -0.22R (83% vetoed), W2 52 trades
+0.17R (47%), W3 27 trades -0.25R (85%). Without the seed, W1 and W2 are blocked for most of the window
by warm-up. That confirms the seed was needed to test the rule rather than the dataset's start date.

### 3.2 Decision inputs, offset 0 (Δ = candidate - baseline)

| dataset | cand | Δ avg R | paired day-block 95% CI of Δ | later-30% Δ | leave-one-year-out Δ (min, year) | n ratio | removed n / avg R | added n / avg R |
|---|---|---|---|---|---|---|---|---|
| `data/` | W1 | -0.191 | [-0.661, +0.271] | -0.136 | -0.309 (w/o 2024) | 0.40 | 105 / +0.33 | 26 / +0.27 |
| `data/` | W2 | **+0.105** | [-0.187, +0.401] | +0.275 | +0.020 (w/o 2024) | 0.73 | 49 / +0.15 | 13 / +0.75 |
| `data/` | W3 | -0.129 | [-0.607, +0.357] | -0.136 | -0.240 (w/o 2024) | 0.39 | 106 / +0.31 | 25 / +0.35 |
| `data_long/` | W1 | +0.041 | [-0.172, +0.263] | **-0.205** | -0.081 (w/o 2017) | 0.54 | 367 / +0.48 | 114 / +0.54 |
| `data_long/` | W2 | +0.001 | [-0.161, +0.161] | +0.027 | -0.087 (w/o 2022) | 0.80 | 256 / +0.52 | 149 / +0.55 |
| `data_long/` | W3 | +0.048 | [-0.183, +0.284] | -0.203 | -0.080 (w/o 2017) | 0.52 | 377 / +0.47 | 115 / +0.52 |

"Removed" and "added" are matched on (symbol, entry_time). "Added" trades are the portfolio-path
effect: room freed by a veto lets a different signal through.

### 3.3 Per entry year, offset 0 (n / avg R)

| year | `data/` B | W1 | W2 | W3 | | `data_long/` B | W1 | W2 | W3 |
|---|---|---|---|---|---|---|---|---|---|
| 2015 | | | | | | 5 / +1.70 | 0 | 0 | 0 |
| 2016 | | | | | | 15 / +0.35 | 11 / +0.55 | 16 / +0.45 | 11 / +0.55 |
| 2017 | | | | | | 37 / +1.46 | 37 / +1.87 | 37 / +1.46 | 37 / +1.87 |
| 2018 | | | | | | 10 / -0.07 | 3 / -0.07 | 1 / +2.36 | 1 / +2.36 |
| 2019 | | | | | | 39 / +0.46 | 15 / +0.09 | 25 / -0.12 | 13 / +0.03 |
| 2020 | | | | | | 78 / +1.31 | 49 / +1.41 | 72 / +1.10 | 50 / +1.36 |
| 2021 | | | | | | 56 / +0.83 | 28 / +0.49 | 50 / +0.52 | 27 / +0.31 |
| 2022 | | | | | | 40 / -0.62 | 2 / -1.15 | **0** | **0** |
| 2023 | | | | | | 93 / +0.47 | 51 / +0.17 | 79 / +0.40 | 49 / +0.15 |
| 2024 | 29 / +1.06 | 16 / +0.85 | 29 / +1.06 | 16 / +0.85 | | 72 / +0.41 | 64 / +0.02 | 89 / +0.33 | 63 / +0.04 |
| 2025 | 53 / -0.28 | 29 / -0.45 | 51 / -0.24 | 28 / -0.42 | | 50 / -0.46 | 25 / -0.49 | 48 / -0.43 | 25 / -0.49 |
| 2026 | 50 / +0.25 | 8 / +0.10 | 16 / +0.77 | 7 / +0.36 | | 51 / +0.23 | 8 / +0.36 | 22 / +0.56 | 8 / +0.36 |

### 3.4 Start-offset sensitivity (every symbol drops its first k bars; full grid in `summary.md`)

| dataset | k | B trades / avg R / gates | W1 trades / Δ / gates | W2 trades / Δ / gates | W3 trades / Δ / gates |
|---|---|---|---|---|---|
| `data/` | 0 | 132 / +0.216 / 4 | 53 / -0.191 / 1 | 96 / **+0.105** / 3 | 51 / -0.129 / 1 |
| `data/` | 7 | 126 / +0.067 / 4 | 51 / -0.224 / 1 | 89 / +0.069 / 2 | 49 / -0.168 / 1 |
| `data/` | 14 | 114 / -0.006 / 2 | 48 / -0.148 / 1 | 77 / +0.045 / 1 | 46 / -0.088 / 1 |
| `data/` | 21 | 112 / -0.054 / 2 | 46 / -0.214 / 1 | 75 / +0.023 / 1 | 44 / -0.156 / 1 |
| `data_long/` | 0 | 546 / +0.496 / 5 | 293 / +0.041 / 5 | 439 / +0.001 / 5 | 284 / +0.048 / 4 |
| `data_long/` | 7 | 545 / +0.506 / 5 | 293 / +0.031 / 5 | 439 / +0.000 / 5 | 284 / +0.038 / 5 |
| `data_long/` | 14 | 543 / +0.494 / 5 | 293 / +0.043 / 5 | 436 / +0.017 / 4 | 284 / +0.050 / 4 |
| `data_long/` | 21 | 541 / +0.499 / 5 | 293 / +0.038 / 5 | 435 / +0.015 / 5 | 284 / +0.046 / 4 |

Notes on the offset grid:
- **Baseline fragility on `data/` is large.** 7 days of start shift move avg R from +0.22 to +0.07.
  At 14 days the baseline itself drops to 2/5 gates (placebo and Sharpe fail).
- **On `data_long/`, offsets barely matter.** W1 is identical at every k: each coin's first 26 weekly
  bars are vetoed anyway, so the dropped warm-up bars never produce a trade.
- **Every W2 Δ on `data/` is positive, but it shrinks** with the offset: +0.105, +0.069, +0.045,
  +0.023. Every paired CI spans 0 (`summary.md`).

Share of symbol-days each gate is ON: `data/` W1 31%, W2 54%, W3 26%; `data_long/` W1 36%, W2 54%,
W3 31%.

## 4. Verdict: **no change recommended**

The pre-registered rule (§0.7) was applied mechanically by `render_phase2_weekly_filter_study.py`.
**No candidate is "promising".**

- **W1 (weekly trend): NOT PROMISING.**
  - On `data/` it is clearly harmful: Δ -0.19, 60% fewer trades, and it loses gates 1, 4 and 5. The
    signals it vetoes earned +0.33R on average, *more* than the ones it keeps.
  - On `data_long/`: Δ +0.04, well inside the noise (paired CI [-0.17, +0.26]). It halves the trade
    count (0.54) and the return (343% to 181%). The later-30% avg R falls from 0.26 to **0.05**.
  - It fails (ii), (iii-a), (iii-b), (iv) on both datasets and (v) on `data/`.
- **W2 (BTC > SMA200): NOT PROMISING.** It is the closest of the three, but it fails:
  - **(i)**: on `data/` it drops to 96 trades, below the 100-trade gate, at every offset; it also
    fails the placebo gate at k=7, and on `data_long/` at k=14 (88th percentile).
  - **(ii)**: the paired Δ CI spans 0 on both datasets ([-0.19, +0.40]; [-0.16, +0.16]).
  - **(iii-b)** on `data_long/`: its whole avg-R effect is 2022. Without 2022, Δ = -0.087.
  - **(iv)** at k = 14 and 21 on `data/` (0.68 and 0.67).
  - On 11 years its avg-R effect is **+0.001**. It vetoes the 2022 losers (40 trades, -0.62R) but
    also removes profitable early-recovery entries made before BTC is back above SMA200 (2019:
    +0.46 to -0.12; 2021: +0.83 to +0.52).
- **W3 (both): NOT PROMISING.** It inherits W1's trade loss and adds a placebo-gate regression on
  `data_long/`.

**So: the evidence does not support adding a weekly confirmation or a BTC regime veto to the daily
pipeline.** This is the pre-registered "no change recommended" outcome. Nothing about the frozen paper
trial changes.

## 5. Observations outside the decision rule (post hoc; NOT a pass, NOT a recommendation)

- **W2 improves risk metrics consistently.** Its max drawdown is lower than the baseline's in every
  one of the 8 dataset/offset cells: `data/` about -13% to -16% vs -24% to -26%; `data_long/` -21.7%
  vs -28.7%.
  - Sharpe is higher in all 8 cells: `data/` 0.78 / 0.56 / 0.27 / 0.18 vs 0.51 / 0.31 / 0.05 / -0.04;
    `data_long/` 1.06-1.09 vs 1.01-1.03.
  - On `data_long/` the total return is lower (297% vs 343%).
  - Mechanism: fewer trades during BTC bear phases, so less capital at risk then. That is a
    drawdown/exposure effect, not better trade selection, which matches avg R being unchanged on 11
    years.
  - **This was not a pre-registered criterion,** and the regime facts that motivated W2 were known
    beforehand (§0.2). It cannot be counted as evidence of a pass.
  - If the user wants to pursue it, the honest next step is a **new** pre-registration with a
    drawdown/Sharpe criterion, tested on data not used here: the forward paper period after the
    current trial, or a point-in-time universe. That would be a Risk Officer question (portfolio-level
    exposure control) as much as a signal question.
- **The `data/` result is dominated by start-date path-dependence** (§3.4). Any future candidate on
  the 2-year window should be judged across offsets, as here. A single-start comparison on `data/`
  can move by more than any effect measured in this study.
- **The weekly trend disagrees with the daily htf gate most of the time on `data/`.** W1 is ON for only
  31% of symbol-days, and it vetoes 66% of signals the daily gate passed. Those vetoed signals were
  *not* worse trades.

## 6. Caveats

- **Survivorship (`data_long/`).** Same 16 coins that exist today. Delisted or collapsed coins are
  missing, which flatters every `data_long/` row equally, but it may interact with W2: survivors are
  the coins that recovered from bear markets.
- **2-year sample (`data/`).** It has 69-98 entry days, and its day-block CIs are about ±0.4R wide.
  The baseline itself swings from +0.22 to -0.05 avg R under a 21-day start shift. No candidate effect
  measured here is distinguishable from that noise.
- **Venue mix.** `data_long/` is Coinbase. The `data/` gate seed is Coinbase before 2024-10-12, but it
  was shown to reproduce Kraken-native W1 verdicts on 99.94% of decisions (§2).
- **Hindsight in W2** (§0.2), and W2 is tested as an entry veto only. A regime-off exit or stop-tighten
  is a different rule and was not tested.
- **Placebo convention.** The placebo shifts each run's own final signals and does not re-apply the
  gate at landing bars (§0.5). Re-applying it would test a different null.
- Daily close-only fills and `kraken_T3_taker` costs throughout, with no funding (spot). The P6
  harness's known residuals (gross vs net quote counting, sorted order vs live config order) apply
  equally to every row.

## 7. What an independent Opus reviewer should check

1. **Look-ahead in the weekly bar** (`phase2_weekly_filter.weekly_closes`):
   - binning by OPEN time (`close - 1 day`), then `+7 days` to get the close-time index;
   - `weekly.loc[:as_of]` with as_of = the daily close time;
   - the Thursday 00:00 decision sees the week closing at that instant, which I argue is legitimate
     (§0.4).
   Mutation M1 shows the tests catch an off-by-one-week error.
2. **The seed rule** (`stitch_closes`, `_gate_inputs`): seed strictly before the primary start, and
   only for gate inputs. Also confirm the scanner, indicators and portfolio are not seeded.
3. **Offsets**: the gates are computed on the full axis while the pipeline is truncated. Is that the
   right model of "live knows prior history"? (§0.4)
4. **The paired day-block bootstrap** (`paired_delta_ci`): blocks = the union of entry days, and the
   same draws for both runs. Is skipping resamples where one side has no trades harmless? It was 0
   skips in every run here; check `results.json`.
5. **`run_job`** builds signals with `build_enriched_and_signals` directly instead of
   `build_live_signals`. That is the same code path at 1d (`daily_closes=None`). Confirm it.
6. **Mechanical verdict:** re-run the renderer and confirm the §0.7 conditions are coded as written,
   especially that (i) is checked at every offset and (ii)/(iii) at offset 0 only.
7. **The post-hoc W2 risk observation** (§5): confirm it is reported as an observation and not used to
   soften the verdict.
