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
