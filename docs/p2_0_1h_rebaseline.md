# P2.0 checkpoint: does the live strategy survive the 5-gate backtest at its OWN live
resolution (1h), with no multi-timeframe change?

**Status:** investigation complete, numbers below are real backtest output from this branch,
**independently reproduced byte-for-byte by the Architect role** (full report matched exactly;
the harness change and the `htf_filter` point-in-time fix were verified correct; a real test-rigor
gap in one new test was found and has since been fixed on this branch — see §8). **This is the
design doc's (`docs/system-redesign-2026-10.md` §5.4, §8 `P2.0`) explicit stop-and-report
checkpoint, and the verdict is FAIL: the live strategy does not clear the 5-gate bar at 1h, its
own live resolution.** The review also ran two independent sensitivity checks against the one real
data-quality artifact it found (see §1) — **the FAIL verdict does not depend on that artifact**,
removing it entirely still fails. Per the design doc's own instruction — "If the live strategy
fails the gates at its own live resolution, stop and bring it to the user before building MTF on
top of it. That finding outranks Phase 2." — no attempt was made to fix, tune, or build anything on
top of this result. **This does NOT mean multi-timeframe confluence (P2a) is itself invalidated**
— it means the open question "is the live strategy fundamentally sound at its own real resolution"
is now answered, and the answer is no, independent of MTF.

**Branch:** `claude/redesign-p2-0-intraday-rebaseline`, off `origin/main`, built in its own git
worktree (not the shared checkout).

## What this checkpoint is and isn't

Per the design doc's §5.4 point 3 (verbatim): "Every gate result so far was on daily bars. At 1h,
`risk.cooldown_minutes` and `max_daily_loss_pct` become binding for the first time... If the live
strategy fails the gates at its own live resolution, stop and bring it to the user before building
MTF on top of it." Every previous live-strategy gate run in this repo
(`report_live_strategy_kraken_720.md`, `report_live_strategy_long.md`) replayed `src/scanner.py ->
src/signal_engine.py -> src/htf_filter.py -> src/trade_planner.py -> src/risk_engine.py ->
src/monitor.py` on **daily** bars, even though `config/config.yaml`'s `market.timeframe` is `"1h"`
— because no intraday history existed to backtest against. This task built that history and reran
the exact same pipeline, exact same config, exact same 5-gate methodology, at the strategy's own
real 1h cadence. **No multi-timeframe confluence logic was added or tested** — same single-
timeframe pipeline as every other live-strategy report, just replayed at a different resolution.

## 1. The new dataset

16 Kraken-universe majors (`config/config.yaml`'s `market.symbols`, static, not the P1d universe
expansion — that's a separate, unrelated, currently-blocked task), 15m OHLCV from Coinbase Exchange
(`strategy_pack_run/fetch_coinbase_long.py`, extended with `--since`/`--until`/`--resume`/retry-
with-backoff, reusing its existing pagination logic unchanged otherwise), over the **same ~2-year
window as the existing `kraken_720` dataset** (`strategy_pack_run/data/`): `2024-10-12T00:00:00Z`
to `2026-09-30T00:00:00Z`, confirmed by inspecting that dataset's own first/last bar rather than
guessed. This is a deliberate scope reduction from the design doc's literal ask of ~11 years/~20k
requests (impractical for one task run) — **full 11-year 15m history is a sensible follow-up,
deferred, not forgotten.**

1h/4h/1d are **derived by resampling the 15m data** (`strategy_pack_run/resample_ohlcv.py`), not
separately fetched — this guarantees all four timeframes are consistent by construction, per the
design doc. Boundary alignment was verified against a **live** Kraken fetch
(`ex.fetch_ohlcv("BTC/USD", "4h")`, 2026-10-05): Kraken's own native 4h candles open at exactly
00:00/04:00/08:00/12:00/16:00/20:00 UTC, matching pandas' default subday resample anchor used here.
A resampled bin is only emitted if **every** one of its expected 15m rows is present — a bin
missing even one row (a live trailing partial bar, or an internal gap in the 15m source) is
dropped rather than computed from incomplete inputs; this is what makes resampling truncation-
invariant (`test_resample_ohlcv.py::test_resample_truncation_invariant`, modeled on
`test_pack.py::test_truncation_no_lookahead`).

Stored gitignored as **parquet** (`strategy_pack_run/data_15m/`, `pyarrow` added to
`requirements-backtest.txt`), not CSV committed to git, per the design doc. The raw 15m CSV fetch
output lives in `strategy_pack_run/data_15m_raw/` (also gitignored) as an intermediate artifact.
Neither `strategy_pack_run/data/` nor `strategy_pack_run/data_long/` was touched.

**Reproducibility caveat (Architect review):** the Architect review re-derived all 64 parquet files
in `data_15m/` from the raw CSVs in `data_15m_raw/` and got zero differences, confirming the
resampling step itself is deterministic and reproducible from the raw fetch. **The raw 15m CSVs
themselves are not reproducible from anything committed to git** — they only come from a live
Coinbase Exchange fetch, and Coinbase can backfill or revise its own history over time, so even
re-running the same fetch command later isn't guaranteed to reproduce byte-identical raw data.
Concretely: nobody without a copy of this task's actual `data_15m_raw/`/`data_15m/` directories can
exactly reproduce `report_live_strategy_1h_rebaseline.md`'s precise numbers — only the code path
and methodology, which the Architect review did independently verify by re-running the full report
end-to-end inside this same worktree.

**Per-symbol data quality, worth knowing before reading the results below:**

| Symbol | 1d bars (this dataset) | Note |
|---|---|---|
| BNB | 340 (from 2025-10-23) | Not listed on Coinbase Exchange until Oct 2025 — real listing-date limit, same treatment `data_long` already gives late-listed coins (leading NaNs, reindexed onto the shared global axis). |
| XTZ | 300 (from 2024-10-12, but only 300 of ~715 possible days survive — **58% of daily bars missing**) | **Identified cause (Architect review): not "sparse/incomplete history" as originally characterized here — Coinbase Exchange's API simply omits a 15m candle entirely when zero volume traded in that window** (confirmed: zero rows anywhere in the raw 15m fetch have `volume == 0`; a true no-trade bar is absent rather than present-with-zero-volume). The "drop incomplete bins" rule (above) then drops any resampled bin missing even one of its expected 15m rows, which compounds this into whole missing 1h/4h/1d bars for a thinly-traded symbol like XTZ on Coinbase. This is a real property of Coinbase's data, not a fetch error or a harness bug — but it does mean XTZ's derived daily htf-gate series covers irregularly-spaced calendar days unlike a live Kraken fetch, which returns a bar for every day regardless of volume. **Two independent reruns confirm this artifact does not drive the FAIL verdict** (see §4/§9): filling the missing 15m rows as flat zero-volume candles before resampling gives `kraken_T3_taker` avg R -0.26 (vs -0.24 in §3); dropping XTZ, SHIB and ALGO entirely gives avg R -0.26 on the remaining 13 symbols. |
| ALGO, SHIB | 643 / 630 (10-12% of daily bars missing) | Same zero-volume-omission effect as XTZ, smaller — ALGO and SHIB trade more often on Coinbase than XTZ does. |
| All others | 714-715 | Essentially complete over the full window. |

## 2. Harness generalization (what changed in `strategy_pack_run/`)

- `backtest_live_bot.py`: `TF_SECONDS` generalized to `{"15m", "1h", "4h", "1d"}`; `load_csv`/
  `load_dir` take a `timeframe` parameter (default `"1d"`, every existing caller unaffected) and
  auto-detect `.csv` vs `.parquet` by suffix; `liquidity_window_bars(timeframe)` added so
  `rank_signals`' 30-**calendar-day** liquidity tie-break window converts to the right **bar**
  count at any timeframe (720 bars at 1h, not 30 — this was a latent bug that only ever mattered
  once a sub-daily dataset existed; it doesn't affect this report, since `run_live_bot_report.py`
  never passes `use_rank_order=True`, but it would have silently misbehaved the moment anyone
  tried). `SCAN_TAIL_BARS` needed **no change** — every scanner/htf_filter lookback it bounds is
  configured in bar counts, not calendar time, so 150 bars of margin is already timeframe-agnostic.
- `run_live_bot_report.py`: `--timeframe` flag (default `"1d"`), `PPY_BY_TF` generalizing the old
  bare `PPY_1D` constant, a separate `LIMITATIONS_1H` block replacing the stale "daily bars, not
  the live 1h feed" caveat with one accurate to an actual 1h replay.
- **A real correctness fix, not just plumbing** (found while generalizing, not part of the design
  doc's explicit checklist): `src/htf_filter.py`'s **live** wrapper (`daily_trend_confirmed`)
  always fetches real `"1d"` candles, **regardless of `market.timeframe`** — it is never the same
  frame the scanner runs on. The daily-only harness got away with reusing the scan-timeframe
  `window` for the htf gate too, because at daily resolution those two frames are identical. At 1h
  they are **not** — reusing the hourly window would have silently checked an hourly EMA cross and
  called it "the daily trend gate," a real divergence from `src/bot.py`'s actual live behavior,
  not a harmless simplification. Fixed: `compute_symbol_signals`/`build_enriched_and_signals` now
  accept a real, separately-loaded daily `close` series per symbol (`daily_closes_by_symbol`,
  default `None` preserving every existing caller's behavior byte-for-byte) and apply the same
  look-ahead-safe, point-in-time rule the design doc's §5.4 calls out generically for 4h/1d bars
  ("the harness may use only bars whose close time is at or before *t*; the forming bar is
  excluded") — proven by two new tests
  (`test_compute_symbol_signals_daily_closes_overrides_window_trend`,
  `test_compute_symbol_signals_daily_closes_excludes_forming_daily_bar`). `run_live_bot_report.py`
  feeds this from the same `--data-dir`'s own real `*_1d` files whenever `--timeframe != "1d"`.
- **Performance**: the design doc flagged "the Phase 1 signal cache becomes mandatory" at 1h,
  anticipating P1d's universe-wide disk cache. Neither `src/market_data.py` (a live-fetch cache,
  not applicable to an offline replay) nor `hyperopt_param.py`'s "compute once, reuse across
  iterations" pattern needed a new caching layer for this specific job: measured,
  `build_enriched_and_signals` at 1h (17217 bars x 16 symbols) took about 9-10 minutes on this
  machine (vs ~25s at 719 daily bars — the ~24x bar-count ratio, as expected, since every lookback
  here is bar-count-bounded and `scanner.scan`/`signal_engine.evaluate` run on a fixed-size tail
  window regardless of total history length). That's within this task's own budget without a new
  cache; a disk-backed signal cache remains the right call for P1d's universe-wide expansion
  (10x more symbols), not deferred here for lack of trying, just not needed yet.
- **Reliability**: `fetch_coinbase_long.py` hit Coinbase Exchange's public rate limit partway
  through the 15m fetch (HTTP 429, uncaught `ccxt.RateLimitExceeded`, crashed the original run) —
  a 15m/2-year pull is ~230 paginated requests per coin, long enough to hit this where the
  original 1d-only fetch never did. Added exponential-backoff retry and a `--resume` flag (skip
  already-fetched symbols) rather than working around it manually; both default to the original
  behavior (`--resume` off, retries only matter once a 429 actually happens).

**No look-ahead** (the design doc's specific concern for this phase): a new truncation-invariance
test for resampling (`test_resample_ohlcv.py::test_resample_truncation_invariant`) proves a bin
present in a truncated prefix of the 15m frame is byte-identical to the same bin computed from the
full frame, across 1h/4h/1d, at 3 different cut points. Combined with the htf-gate point-in-time
fix above, both of the design doc's named look-ahead hazards for this phase are covered by tests.

**Explicitly out of scope, not built here** (hard constraint, confirmed): no
`mtf_confluence.py`, no `context_gates.py`, no multi-timeframe confluence logic of any kind. No
file under `src/`, `strategy_pack_run/engine.py`, `strategy_pack_run/strategies.py`, or
`strategy_pack_run/hyperopt_param.py` was touched. `strategy_pack_run/data/` and `data_long/` are
untouched. No live order, no live-trading config change.

## 3. The checkpoint result

```
PYTHONPATH=..:. python3 run_live_bot_report.py --data-dir data_15m --timeframe 1h --sims 200 \
    --out report_live_strategy_1h_rebaseline.md
```

Window: **2024-10-12 to 2026-09-30, 17217 1h bars** — the same calendar window as `kraken_720`
(719 1d bars), just at 24x the resolution. Raw bullish+HTF-confirmed signals before any
risk-engine gating: **8073** (vs 1482 on the daily `kraken_720` dataset — roughly proportional to
the ~24x more bars, not wildly disproportionate, which is at least consistent with the htf-gate
fix above still behaving as a real filter rather than rubber-stamping everything).

### Results, by cost assumption

| costs | trades | win% | avg R | avg R 95% CI | PF | return | maxDD | Sharpe | exposure |
|---|---|---|---|---|---|---|---|---|---|
| paper_today_0.10% | 959 | 34.3% | -0.02 | [-0.13, 0.09] | 0.88 | -24.2% | -50.9% | -0.59 | 22.2% |
| kraken_T3_maker | 965 | 34.8% | -0.13 | [-0.23, -0.02] | 0.78 | -41.7% | -59.9% | -1.25 | 22.2% |
| **kraken_T3_taker** | **959** | **35.0%** | **-0.24** | **[-0.35, -0.13]** | **0.71** | **-53.9%** | **-66.8%** | **-1.83** | **22.2%** |
| kraken_T1_taker | 950 | 34.4% | -0.56 | [-0.67, -0.45] | 0.57 | -74.2% | -79.8% | -3.30 | 22.1% |

Benchmarks over the same window (no costs): BTC buy&hold return 33.1% (Sharpe 0.55); equal-weight
buy&hold return -9.7% (Sharpe 0.27).

Fixed-rule OOS split at 2026-02-26 (costs: `kraken_T3_taker`): first 70% = 644 trades, 33.4% win,
avg R -0.27. Last 30% = 315 trades, 38.4% win, avg R **-0.18** (still negative, just less so).

Time-shift placebo (200 sims, `kraken_T3_taker`): real return ranks at the 92nd percentile, real
avg R at the 100th percentile of the placebos.

### GATES (costs: `kraken_T3_taker`)

| strategy | >=100 trades | avg R CI low > 0 | later 30% avg R > 0 | beats placebo >=90th pct | net return > 0 and Sharpe > buy&hold |
|---|---|---|---|---|---|
| **1h re-baseline (this report)** | PASS | **FAIL** | **FAIL** | PASS | **FAIL** |
| daily, current config (Architect-reproduced; see correction below) | PASS | FAIL | PASS | PASS | PASS |

**Correction (Architect review): the daily comparison above was originally the stale, committed
`report_live_strategy_kraken_720.md` (189 trades, 3/5 — predates the `stop_atr_mult` 1.0→1.5 change
and the 5/120 new-entry rate cap, neither caused by this branch, both already on `main`).** The
Architect review re-ran `run_live_bot_report.py --data-dir data` (no `--timeframe` flag, i.e. the
actual current-config daily replay) directly and confirmed it reproduces this repo's already-
on-record current baseline exactly: **154 trades, avg R 0.18 [-0.08, 0.46], return 22.5%, Sharpe
0.68, 4/5 gates** (fails only CI-low; beats placebo at the 98th/91st percentile). The table above
now uses that correct, current-config comparison.

**Only 2 of 5 gates pass at 1h, against 4 of 5 at daily — a larger drop than this doc originally
reported**, and in the worse direction: the daily replay has a positive, if not-yet-statistically-
significant, average edge (+0.18R) and clearly beats buy&hold's risk-adjusted return; the 1h replay
has a *negative* average edge and loses to buy&hold. **Verdict: FAIL.**

## 4. Why — characterized, not fixed

The headline number that matters most: **average R is negative at every single cost tier,
including `paper_today_0.10%`** (-0.02R), the cheapest cost assumption in this repo and one most
previous reports clear comfortably. That means the strategy's own entries/exits, judged purely on
R-multiples before realistic trading costs are even applied, are roughly breakeven-to-negative at
1h resolution over this window — costs then push every realistic tier further negative, and
`kraken_T3_taker` (the standard cost assumption this repo's 5-gate verdict uses) lands at -0.24R
average, -53.9% total return, and a maximum drawdown of -66.8%.

This is not the "cooldown/max_daily_loss become binding, trimming some trades" effect the design
doc anticipated as the main 1h-specific risk — that effect, by itself, would be expected to
**remove** trades (plausibly improving average R on the ones that remain, since a newly-binding
risk gate is risk-reducing, not edge-adding) rather than make the *average* trade unprofitable
even before costs. The 1h replay has ~5x more trades than the daily replay (959 vs 189) despite
only ~24x more bars (fewer trades per bar at finer resolution is expected, since the same
calendar-time patterns now take many more bars to resolve) — consistent with a real risk/signal
mix, not an obviously broken harness artifact.

**Diagnostic breakdown** (same `kraken_T3_taker` run, real trades, computed directly from
`_diag_1h_trades.csv` — the dataframe `simulate_portfolio` itself produced for this exact run, not
a re-derived approximation):

| Exit reason | Count | % of trades | Avg R |
|---|---|---|---|
| `stop_loss` | 617 | 64.3% | **-1.47** |
| `take_profit` | 335 | 34.9% | **+2.02** |
| `end` (data window closed while still open) | 7 | 0.7% | -0.35 |

**Correction (Architect review): stop-outs averaging -1.47R is NOT mainly "losses running past the
nominal stop"** — that was this doc's original, incorrect explanation. The review split each
trade's R into the price move itself ("gross", before costs) versus fees/slippage, and compared
directly against the current-config daily replay:

| | daily (current config) | 1h |
|---|---|---|
| median stop distance (% of entry price) | 10.1% | 3.05% |
| stop-out R, **before costs** | -1.14 | -1.16 |
| cost drag per trade | -0.09R | **-0.33R** |
| avg R, **before costs** (all trades) | +0.28 | +0.09 |

**Stop overshoot past the nominal -1.0R is essentially identical at both resolutions** (-1.14 vs
-1.16, before costs) — that part of the original explanation doesn't hold up. What actually
changed, two genuine and distinct effects of running at 1h: **(a)** this strategy's stops are
~3.3x tighter in percentage terms at 1h (3.05% vs 10.1% of entry — expected, since
`trade_planner.py`'s ATR-based stop sizing reacts to the much smaller ATR of an hourly bar), so the
*same* fixed-percentage trading costs eat roughly 3.5x more of each trade's R (-0.33R vs -0.09R);
**(b)** independent of costs, the strategy's own pre-cost edge is smaller at 1h (+0.09R vs +0.28R
avg R before costs) — entries/exits that work reasonably well judged day-to-day are closer to
breakeven judged hour-to-hour. Both are real, already-present properties of this strategy running
at its own real resolution, not a harness bug and not newly introduced by this task — practically,
**(a) means the live-vs-paper cost gap matters more at 1h than it looked like at daily**, since a
fixed percentage cost is a much bigger bite out of a proportionally tighter stop. Take-profits, when
they do land, are healthy (+2.02R, above the configured `min_risk_reward: 2.0` floor as expected) —
the imbalance is in how often and how costly the stop side is, not in the win side being too small.

Per-symbol (sorted, avg R): only **2 of 16 symbols are net positive** — XRP (+0.26R, 47 trades) and
ALGO (+0.22R, 69 trades). The other 14 are all negative, led by SHIB (-0.59R), ATOM (-0.55R), BCH
(-0.49R), BTC (-0.42R), LINK (-0.41R). This is a broad, universe-wide effect, not one or two
symbols dragging down an otherwise-fine set — consistent with something in the entry/stop mechanism
behaving differently against 1h noise generally, rather than a symbol-specific data or listing
issue (BNB and XTZ, the two symbols flagged for real data gaps in §1, are mid-pack, not the worst).

`bars_held`: median 27 bars (~27h), mean 50.4 bars (~50h) — the mean-over-median gap is a real
right skew. **Correction (Architect review): the max of 1630 bars (~68 days) is a single LTC
`stop_loss` trade, not one of the 7 `end`-reason trades** (the longest `end`-reason trade is only
227 bars) — a genuine case of a position sitting well inside both the stop and target for a long
stretch before the stop eventually caught it, not a data-window artifact.

Overall: avg R -0.24 (matches §3's `kraken_T3_taker` row exactly, as it should — same run), median R
**-1.23** — more than half of all trades lose at or beyond a full R, which is the same signal as the
stop_loss-reason average above, from a different angle.

What this does NOT show: it does not show *why* the per-trade edge collapsed at 1h relative to
daily — that would require decomposing which of the scanner's five pattern detectors, or which
part of `trade_planner.py`'s entry-zone/stop-loss sizing, behaves differently against
hour-to-hour noise than against day-to-day trend. That diagnosis is exactly the kind of "tune
around the FAIL" work the design doc's checkpoint explicitly says to stop short of here — it's a
Market Analyst/Trade Strategist question, with its own written reason and out-of-sample test, not
a QA/Tester harness-validation one.

## 5. What this does and doesn't mean

- **Does mean:** the live strategy, as currently configured, does not have a demonstrated edge at
  its own actual trading resolution (1h) on this ~2-year Coinbase-replay window, once realistic
  costs are applied. Per the design doc, this "outranks Phase 2" — building multi-timeframe
  confluence (P2a) on top of a foundation that doesn't itself clear the bar would mean validating
  an addition to a strategy whose own base case is unproven at its real resolution.
- **Does NOT mean:** that multi-timeframe confluence itself is bad — MTF was never built or
  tested here, by design. It also does not mean the EARLIER daily-bar findings (the current-config
  daily replay's own 4/5, `report_live_strategy_long.md`) are wrong — they are a different, coarser
  replay of the same logic, and this result doesn't retroactively invalidate them, it just answers
  a question they structurally could not: whether the SAME logic holds up once replayed at its
  actual intraday cadence. (The committed `report_live_strategy_kraken_720.md` specifically is
  separately stale on `main` — see the correction in §3 — but that staleness predates this branch
  and is unrelated to this checkpoint's own verdict.)
- **Does NOT mean:** the harness itself is broken. The same pipeline functions
  (`scanner.scan`, `signal_engine.evaluate`, `htf_filter.daily_trend_confirmed_from_df`,
  `trade_planner.build_plan`, `RiskEngine.check_new_trade`, `monitor.check_pending`/`check_open`)
  are imported read-only and unmodified, same as every other live-strategy report; the fixes made
  here (htf point-in-time daily series, timeframe-aware liquidity window) make the harness a
  **more faithful** replay of `src/bot.py`, not a different methodology.

## 6. Open question for the user (not decided here)

Per the design doc's own instruction, this is a stop-and-report checkpoint, not a decision point.
Routing this for Architect review (as the task that commissioned this work specified) is the
immediate next step; what happens after that — revisit `trade_planner`/`scanner` parameters with
their own written reason and OOS test, reconsider the strategy's viability at 1h generally, or
something else — is the user's call once the review is in hand.

## 7. Files touched

- `strategy_pack_run/fetch_coinbase_long.py` — `--since`/`--until`/`--resume`/`--sources-out`,
  retry-with-backoff. Every existing default preserves prior behavior byte-for-byte.
- `strategy_pack_run/resample_ohlcv.py` — new. Pure resampling + CSV/parquet I/O + CLI.
- `strategy_pack_run/backtest_live_bot.py` — `TF_SECONDS` generalized; `load_csv`/`load_dir`
  timeframe + parquet aware; `liquidity_window_bars()` (new); `simulate_portfolio`/`placebo_run`
  gain a `timeframe` kwarg (default `"1d"`); `compute_symbol_signals`/`build_enriched_and_signals`
  gain `daily_closes`/`daily_closes_by_symbol` (default `None`) for the htf point-in-time fix.
  Every new parameter defaults to the prior behavior.
- `strategy_pack_run/run_live_bot_report.py` — `--timeframe` flag, `PPY_BY_TF`, `LIMITATIONS_1H`,
  real-daily-series wiring for the htf gate at sub-daily timeframes.
- `strategy_pack_run/requirements-backtest.txt` — `pyarrow` added.
- `.gitignore` — `strategy_pack_run/data_15m/`, `strategy_pack_run/data_15m_raw/` (parquet/CSV
  intraday data, deliberately not committed, unlike `data/`/`data_long/`).
- `strategy_pack_run/test_resample_ohlcv.py` — new (11 tests: boundary alignment, completeness,
  OHLCV aggregation, truncation invariance, CSV/parquet round trip).
- `strategy_pack_run/test_backtest_live_bot.py` — new tests for timeframe/parquet-aware
  `load_csv`/`load_dir`, `liquidity_window_bars`, and the htf point-in-time fix (12 new tests,
  after the Architect-review fix below: the original look-ahead test used a gradual ramp whose
  first future value coincided with the flat past, so a one-day look-ahead leak was invisible to
  it; rebuilt with one decisive, unmistakable value instead, plus a new positive-control test
  proving the same value, moved to be visible, flips the result).
- `src/htf_filter.py` is **not** touched by this branch — the fix is entirely harness-side in
  `backtest_live_bot.compute_symbol_signals`/`build_enriched_and_signals` (a stricter, opt-in
  per-symbol check: once a caller passes `daily_closes_by_symbol` at all, a symbol silently
  missing from it now raises instead of quietly falling back to the sub-daily window for just
  that symbol — closing exactly the failure mode the point-in-time fix exists to prevent).
- `strategy_pack_run/data_15m/`, `strategy_pack_run/data_15m_raw/` — new datasets, gitignored,
  not committed (confirmed via `git status` before every commit on this branch).
- `strategy_pack_run/data_sources_15m.md` — provenance/gap report for the new 15m fetch (does
  not overwrite the existing `data_sources.md` for `data_long`).
- `report_live_strategy_1h_rebaseline.md` — the actual report `run_live_bot_report.py` wrote;
  same file whose tables are reproduced in §3 above.
- This file.

## 8. Validation run (after the Architect-review fixes applied directly to this branch)

The Architect review found two things worth fixing before this goes further, both applied to this
branch directly (same commit history, no separate round-trip): (1) the look-ahead test described
above rebuilt with a decisive value + a new positive control, since the original would not have
caught the exact mutation it was meant to catch; (2) `build_enriched_and_signals` now raises if a
symbol is missing from an explicitly-passed `daily_closes_by_symbol`, instead of silently falling
back to the sub-daily window for that one symbol (which would quietly reintroduce the bug the
point-in-time fix exists to close). Both fixes were verified by temporarily reintroducing the
original bug and confirming the strengthened/new test actually fails, then reverting.

- `PYTHONPATH=. python3 -m pytest -q` (repo root) — 352 passed (unchanged vs `origin/main`;
  `tests/` doesn't import anything under `strategy_pack_run/`).
- `PYTHONPATH=..:. python3 -m pytest -q .` (run from `strategy_pack_run/`) — **45 passed** (22
  pre-existing on `origin/main` + 23 new: 11 in `test_resample_ohlcv.py` + 12 in
  `test_backtest_live_bot.py`, confirmed by diffing against `origin/main` directly, not estimated).
- `PYTHONPATH=. python3 scripts/validate_invariants.py` — 5/5 passed.
- Zero `anthropic`/`claude_advisor`/LLM imports in any new or changed file (grepped explicitly).
- Independently reproduced by the Architect review: the full `report_live_strategy_1h_rebaseline.md`
  re-run matched the committed file byte-for-byte; the daily re-run (no `--timeframe` flag) matched
  this repo's current on-record baseline byte-for-byte; the diagnostic breakdown in §4 was
  recomputed independently from `_diag_1h_trades.csv` and matched exactly.
