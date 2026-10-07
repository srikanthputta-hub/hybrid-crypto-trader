# `engine_score` saturation: diagnosis, pre-registered rank-key candidates, results

- **Role:** Market Analyst (`src/signal_engine.py`, `src/scanner.py`). Harness edits: additive only.
- **Branch:** `claude/engine-score-saturation`, off `origin/main` at `73bf73c`.
- **Background:** docs/p1c_rank_signals_validation.md finding A; docs/p1e_universe_gate_run.md §3.7 / §6 / §7.
- **Scope:** `rank_signals` ordering only. `engine_score` itself, `evaluate()`'s output, the scanner's
  strengths, trade plans and risk limits are not changed by anything pre-registered here.

## 1. Diagnosis (written before any candidate was run)

Produced by `strategy_pack_run/run_engine_score_diagnosis.py` (committed in `66ab9a2`). It reruns the
real scanner on every bar with `scanner._clip01` replaced by the identity, so each candidate's
pre-clip ("raw") strength is visible, re-derives the clipped strength as `clip01(raw)` (identical to
the unpatched detector, because `_clip01` is the last operation in every strength formula and never
feeds a detection condition), then runs the real `evaluate()` and HTF gate. It asserts the resulting
Signals equal `compute_symbol_signals`' own output on `data/` and `data_long/` (they do). It also
records every `rank_signals` call with >= 2 competing symbols from a canonical legacy
`use_rank_order=True` run.

Datasets: `data/` (static-16, 719 bars), `data_long/` (static-16, 4091 bars), and
`kraken_720_universe` restricted to P1e's `$1M` candidate (ever-eligible symbols, P1e mask and tiered
costs; counts below are signals on bars where the symbol was eligible).

### 1.1 Where the 1.0s come from

| | data/ | data_long/ | kraken_720_universe $1M |
|---|---|---|---|
| bullish HTF-confirmed signals | 1482 | 5996 | 3154 (eligible) |
| `engine_score == 1.0` | 66.3% | 68.5% | 72.0% (76.3% incl. ineligible bars) |
| primary candidate's raw strength >= 1 (clipped in the scanner) | 63.9% | 66.1% | 70.0% |
| saturated signals where the primary was < 1 (confluence bonus pushed it over) | 3.7% | 3.6% | 2.9% |
| MOMENTUM is the primary pattern | 89.8% | 91.3% | 91.9% |
| agreeing patterns per signal (n=1/2/3/4) | 1017/229/222/14 | 4254/856/796/90 | 2286/417/406/45 |

Raw (pre-clip) strength per pattern, all agreeing candidates:

| pattern | data_long n | share raw >= 1 | q10 / median / q90 | max |
|---|---|---|---|---|
| MOMENTUM | 5717 | 69.1% | 0.77 / 1.24 / 2.44 | 17.8 |
| PULLBACK | 1274 | 24.7% | 0.66 / 0.86 / 1.17 | 1.7 |
| BREAKOUT | 1167 | 14.2% | 0.56 / 0.72 / 1.06 | 2.1 |
| TREND_CONTINUATION | 556 | 5.9% | 0.58 / 0.68 / 0.91 | 1.8 |

(`data/` and the kraken universe show the same pattern: MOMENTUM 67.7% / 73.4% raw >= 1, median raw
1.22 / 1.36; the others 4-29%.)

**Cause.** Saturation is almost entirely the MOMENTUM detector's own clip, not the confluence bonus.
`_detect_momentum` scores `0.4 + |move| / momentum_return_threshold * 0.2 (+0.08 ADX bonus)` with
`momentum_lookback: 5`, `momentum_return_threshold: 0.03`. On **daily** bars a 5-bar move of 9%
(6.6% with the ADX bonus) already reaches 1.0, and the median momentum signal's move is about 12%.
The bonus explains only ~3-4% of saturated signals; removing the final clamp in `evaluate()` alone
leaves the signal at its clipped primary strength of 1.0. Consequence: 40-48% of all signals share
exactly the same unclamped value (primary 1.0, n=1).

**Caveat that matters for any fix.** `config/config.yaml` runs the live bot on `market.timeframe: "1h"`.
All datasets here are daily. A 3%-in-5-bars threshold means something very different on 1h bars,
so the saturation rate measured here (and anything selected on it) may not transfer to live. No 1h
dataset is used in this study.

### 1.2 What `rank_signals` actually sees (legacy, contested calls only)

| | data/ | data_long/ | kraken_720_universe $1M |
|---|---|---|---|
| `rank_signals` calls with >= 2 symbols | 128 | 470 | 265 |
| competitors in those calls | 687 | 1804 | 1957 |
| share of competitors tied with the call's top `engine_score` | 70.3% | 70.1% | 73.8% |
| share of calls whose #1 is decided by the tie-break | 71.9% | 77.7% | 79.6% |

## 2. Pre-registration (committed before any candidate was run; not edited after results)

### 2.1 Candidate rank keys (three; no others will be run)

All three are pure functions of data available at the signal bar's close (the same trailing window
the scanner already uses). All three keep `engine_score`'s ordering wherever it is not tied, keep
the existing liquidity tie-break (`-median_quote_volume_30d`, `None` last) after the new term, and
keep `symbol` as the final key, so the order is always fully determined. A missing/non-finite value
of the new term sorts after every known value (same convention as `None` liquidity).

| id | sort key | definition of the new term | written reason |
|---|---|---|---|
| **A `raw_score`** | `(-raw_score, liquidity, symbol)` | `max` over the agreeing (same-direction, `>= min_strength`) candidates of the detector's strength **before** `_clip01`, plus `confluence_bonus * (n - 1)`, no clamp. For every unsaturated signal this equals `engine_score` exactly (if any raw strength were >= 1 its clipped value 1.0 would be the primary), so A is `engine_score` with the 1.0 ties broken by pre-clip magnitude. | The brief's option (A), corrected by §1.1: the clamp that matters is the scanner's, not `evaluate()`'s. Removing both clamps is the most literal "stop throwing information away" fix. A-priori doubt: momentum's raw scale is threshold-relative and unbounded (max 21), so in practice A ranks saturated signals by raw 5-bar % move, which favours high-volatility coins; the playbook (§3) reports small caps tend to revert. |
| **B `adx`** | `(-engine_score, -adx, liquidity, symbol)` | `primary_details["adx"]` (Wilder ADX at the signal bar, already computed by `src/indicators.py` and already stored by BREAKOUT / MOMENTUM / TREND_CONTINUATION); missing for PULLBACK / REVERSAL primaries -> sorts last among ties. | The brief's option (B): a continuous, bounded (0-100), scale-free measure of trend quality that is already part of the scanner's own evidence (it is the ADX-confirmation bonus input in three of five detectors). Unlike A it does not reward raw volatility. Stays inside what the Signal already carries. |
| **C `move_atr`** | `(-engine_score, -move_atr, liquidity, symbol)` | `(close_t / close_{t-L} - 1) / (atr_t / close_t)` with `L = scanner.momentum_lookback` (5): the momentum detector's own `return_pct`, expressed in ATR units of that coin. Computed for every signal from the trailing frame (pattern-agnostic). | My option: the same momentum evidence A uses, but made scale-free the way the scanner already scales BREAKOUT / PULLBACK / TREND_CONTINUATION strength (distance in ATRs). Ranks saturated signals by how unusual the move is for that coin, not by how big it is in %, so it should not systematically favour the most volatile names. |

### 2.2 Harness and cases (fixed now)

Harness: `backtest_live_bot.simulate_portfolio` / `placebo_run`, live `config/config.yaml`, 1d bars,
$10,000, PPY 365, `use_rank_order=True` in every row. The candidate key is injected runner-locally
by patching `src.signal_engine.rank_signals` (same mechanism as P1e §0.10); no harness semantics
change. Fees + slippage are modelled; this is long-only spot, so there is no funding cost to model.

| case id | dataset | costs | universe |
|---|---|---|---|
| **S1** | `strategy_pack_run/data/` (static-16) | flat `kraken_T3_taker` (0.38% fee + 5bp) | always eligible |
| **S2** | `strategy_pack_run/data_long/` (static-16) | flat `kraken_T3_taker` | always eligible |
| **U1** | `kraken_720_universe`, P1e `$1M` candidate | `kraken_T3_taker` fee + P1e frozen tiered slippage | P1e mask, ever-eligible symbols, exclusions per P1e §0.2 |
| _info_ | `long_universe`, P1e `$1M` candidate | as U1 | P1e rank-top-N mask (N = 49) |

The info row gets a canonical run and 10 order seeds, no placebo, and has **no bearing on the
verdict**.

**Placebo:** 200 time-shift sims, seed 0, as before. A placebo signal already carries the original
signal's `engine_score` and patterns; for a candidate key it also carries the original signal's
value of the new term (runner-local patch of `_placebo_signal_at`). Legacy placebo is untouched.

**Gates:** identical to P1e §0.7 (>= 100 trades; bootstrap avg-R CI low > 0; later-30% avg R > 0;
placebo >= 90th pct on both return and avg R; return > 0 and Sharpe > benchmark Sharpe). Benchmark:
S1/S2 equal-weight buy&hold of the 16 (as `run_live_bot_report.py`); U1 per P1e (higher of
original-16 and ever-eligible).

**Order seeds:** seeds 1-10. Each replaces the liquidity tie-break with a seeded random draw
(P1e's `make_random_tiebreak` RNG pattern), keeping the case's own signal key (legacy:
`engine_score`; A: `raw_score`; B: `engine_score, adx`; C: `engine_score, move_atr`) as the
leading terms.

**Integrity check (must hold before any candidate number is read):** legacy S1 = 137 trades /
avg R 0.11 (P1c `rank_signals` leg), S2 = 572 / 0.53, U1 = 172 / 0.41 with 5/5 gates (P1e), and
the sorted-order flat runs reproduce 154 / 0.18 and 562 / 0.52.

### 2.3 Acceptance criteria (per candidate; all must hold on each of S1, S2, U1)

- **(i) Ties.** In the candidate's canonical run, the share of competitors in `rank_signals` calls
  with >= 2 symbols that tie with the call's top on the candidate's signal key (every term before
  liquidity) is **<= 20%** (legacy: 70.3 / 70.1 / 73.8%).
- **(ii) No gate regression.** The candidate does not FAIL any of the 5 gates that legacy PASSES on
  that dataset.
- **(iii) Order robustness.** Across the 10 order seeds: the avg-R range (max - min) is no larger
  than legacy's on that dataset, and the number of seeds failing any of gates 1, 2, 3, 5 is no
  larger than legacy's; on U1 it must be **strictly** smaller (legacy measured 6/10 in P1e).
- **(iv) Out-of-sample, not in-sample.** Later-30% avg R > 0 (gate 3, already in ii) **and**
  later-30% avg R >= legacy's later-30% avg R - 0.10R. The 0.10R tolerance is the size of the
  processing-order noise band P1c measured (about ±0.09R), so a candidate is not rejected for a
  difference that order luck alone produces. Placebo (gate 4) is covered by (ii).

**Verdict rule.** A candidate QUALIFIES only if (i)-(iv) all hold on S1, S2 and U1. Full-sample
avg R is reported but never used to choose. If more than one qualifies, the one with the highest
mean later-30% avg R across S1, S2, U1 is implemented (exact tie: A before B before C). If none
qualifies, the outcome is **"no change; saturation documented"** and nothing in `src/` changes.

### 2.4 Implementation rule (only if a candidate qualifies)

Behind `signal_engine.rank_key` (values `legacy` | the winner's id), default `legacy`, with
`legacy` byte-identical to `origin/main`'s `rank_signals`. `config/config.yaml` stays `legacy`. The
`src/` implementation must reproduce the runner's canonical numbers for that candidate exactly. If it
cannot be implemented without changing the `Candidate` or `Signal` dataclass shape, stop and report.

---

_Everything below was added after the pre-registration commit._

## 3. What was run

| file | what it is |
|---|---|
| `strategy_pack_run/run_engine_score_diagnosis.py` | §1 diagnosis (read-only). |
| `strategy_pack_run/run_rank_key_eval.py` | §2 evaluation. Runner-local patches only (`rank_signals`, and `_placebo_signal_at` so placebo signals carry the original's key values). Legacy rows are not patched. Caches each job, so a run resumes after an interruption. |
| `strategy_pack_run/test_rank_key_eval.py` | 5 tests: the runner's legacy ranker equals `src.signal_engine.rank_signals` on 500 randomized inputs (ties, missing and `None` liquidity, extra symbols); A/B/C orderings as pre-registered; the tie counter; placebo carry. Mutation-checked by hand: `None` liquidity as 0, placebo not carrying the key, missing value sorted first. All three caught, file restored. |
| `strategy_pack_run/engine_score_rank_key_results.json` | Every number below, including all 10 seeds per case and candidate. |

```bash
cd strategy_pack_run   # raw universe CSVs copied from the P1e worktree, SHA256SUMS 966/966 OK
PYTHONPATH=..:. python3 -W ignore run_engine_score_diagnosis.py --out <scratch>/diag.pkl
PYTHONPATH=..:. python3 -W ignore run_rank_key_eval.py --cache-dir <scratch>/eval --integrity-only
PYTHONPATH=..:. python3 -W ignore run_rank_key_eval.py --cache-dir <scratch>/eval --workers 4
```

The integrity check passed before any candidate ran: S1 sorted 154 / 0.182, S2 sorted 562 / 0.518,
S1 legacy 137 / 0.114, S2 legacy 572 / 0.532, U1 legacy 172 / 0.412. The runner also asserts that
its own re-implemented legacy ranker gives identical trades to the unpatched code on S1, S2 and U1.

The evaluation was run once. The JSON was then regenerated from the same job cache with no rerun,
only to write `null` instead of bare `NaN` tokens. It was verified identical apart from that.

## 4. Results

Gates are in order: >= 100 trades / CI low > 0 / later-30% > 0 / placebo >= 90th / return > 0 and
Sharpe > benchmark. "Tied at top" is criterion (i). Seeds = the 10 random tie-break orders.

| case | key | trades | avg R [95% CI] | return | Sharpe (bench) | later-30% n / avg R | placebo pct ret / avgR | gates | tied at top | seed avg R min / max | seeds failing 1,2,3,5 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| S1 `data/` | legacy | 137 | 0.11 [-0.18, 0.41] | 16.2% | 0.52 (0.28) | 42 / 0.43 | 96 / 86.5 | P F P **F** P | 70.3% | 0.10 / 0.28 | 9 |
| S1 | A | 145 | 0.20 [-0.09, 0.51] | 20.9% | 0.62 | 42 / 0.39 | 98 / 94.0 | P F P P P | 0.0% | 0.20 / 0.20 | 10 |
| S1 | B | 145 | 0.16 [-0.12, 0.46] | 14.5% | 0.47 | 42 / 0.37 | 96 / 90.0 | P F P P P | 0.0% | 0.16 / 0.16 | 10 |
| S1 | C | 143 | 0.13 [-0.15, 0.43] | 18.6% | 0.57 | 42 / 0.39 | 97 / 87.5 | P F P F P | 0.0% | 0.13 / 0.13 | 10 |
| S2 `data_long/` | legacy | 572 | 0.53 [0.38, 0.69] | 327.0% | 0.94 (0.81) | 262 / 0.31 | 100 / 98.5 | P P P P P | 70.1% | 0.49 / 0.56 | 0 |
| S2 | A | 555 | 0.55 [0.39, 0.71] | 426.0% | 1.04 | 244 / 0.35 | 100 / 99.0 | P P P P P | 0.0% | 0.55 / 0.55 | 0 |
| S2 | B | 548 | 0.51 [0.35, 0.67] | 347.3% | 0.95 | 238 / 0.25 | 100 / 98.0 | P P P P P | 0.0% | 0.51 / 0.51 | 0 |
| S2 | C | 549 | 0.53 [0.37, 0.69] | 387.9% | 1.00 | 239 / 0.31 | 100 / 99.0 | P P P P P | 0.0% | 0.53 / 0.53 | 0 |
| U1 kraken $1M | legacy | 172 | 0.41 [0.15, 0.69] | 38.9% | 0.99 (0.83) | 83 / 0.56 | 100 / 100 | P P P P P | 73.8% | 0.23 / 0.38 | 6 |
| U1 | A | 185 | 0.19 [**-0.05**, 0.45] | 31.7% | 0.85 | 80 / 0.38 | 100 / 94.5 | P **F** P P P | 0.0% | 0.19 / 0.19 | 10 |
| U1 | B | 193 | 0.27 [0.01, 0.51] | 27.1% | **0.75** | 88 / 0.50 | 98 / 98.5 | P P P P **F** | 0.0% | 0.27 / 0.27 | 10 |
| U1 | C | 163 | 0.25 [**-0.02**, 0.52] | 33.9% | 0.89 | 70 / 0.43 | 100 / 97.0 | P **F** P P P | 0.0% | 0.25 / 0.25 | 10 |
| _info: long_universe $1M_ | legacy | 838 | 0.40 [0.27, 0.53] | 371.3% | 0.90 (0.82) | 418 / 0.16 | not run | P P P - P | 75.8% | 0.39 / 0.48 | 0 |
| _info_ | A | 864 | 0.33 [0.21, 0.46] | 415.4% | 0.93 | 433 / 0.08 | not run | P P P - P | 0.0% | 0.33 / 0.33 | 0 |
| _info_ | B | 847 | 0.38 [0.25, 0.51] | 385.9% | 0.91 | 424 / 0.18 | not run | P P P - P | 0.0% | 0.38 / 0.38 | 0 |
| _info_ | C | 859 | 0.43 [0.30, 0.56] | 409.0% | 0.92 | 447 / 0.24 | not run | P P P - P | 0.0% | 0.43 / 0.43 | 0 |

### 4.1 Criteria, applied mechanically

| key | dataset | (i) ties <= 20% | (ii) no gate regression | (iii) order robustness | (iv) later-30% >= legacy - 0.10 |
|---|---|---|---|---|---|
| A | S1 | PASS (0.0%) | PASS (placebo FAIL -> PASS) | FAIL (10 vs 9 seeds failing) | PASS (0.39 vs 0.43) |
| A | S2 | PASS | PASS | PASS | PASS (0.35 vs 0.31) |
| A | U1 | PASS | **FAIL** (CI low) | **FAIL** (10 vs 6) | **FAIL** (0.38 vs 0.56) |
| B | S1 | PASS | PASS (placebo FAIL -> PASS, at exactly 90.0) | FAIL (10 vs 9) | PASS (0.37 vs 0.43) |
| B | S2 | PASS | PASS | PASS | PASS (0.25 vs 0.31) |
| B | U1 | PASS | **FAIL** (gate 5: Sharpe 0.75 < 0.83) | **FAIL** (10 vs 6) | PASS (0.50 vs 0.56) |
| C | S1 | PASS | PASS | FAIL (10 vs 9) | PASS (0.39 vs 0.43) |
| C | S2 | PASS | PASS | PASS | PASS (0.31 vs 0.31) |
| C | U1 | PASS | **FAIL** (CI low) | **FAIL** (10 vs 6) | **FAIL** (0.43 vs 0.56) |

### 4.2 Where each canonical result sits in legacy's random-order spread (full-sample avg R; information only)

| dataset | legacy canonical | legacy 10 random orders | A | B | C |
|---|---|---|---|---|---|
| S1 | 0.11 | 0.10 - 0.28 | 0.20 (beats 9/10) | 0.16 (7/10) | 0.13 (3/10) |
| S2 | 0.53 | 0.49 - 0.56 | 0.55 (9/10) | 0.51 (6/10) | 0.53 (8/10) |
| U1 | 0.41 | 0.23 - 0.38 | 0.19 (0/10) | 0.27 (1/10) | 0.25 (1/10) |
| long_universe $1M (info) | 0.40 | 0.39 - 0.48 | 0.33 (0/10) | 0.38 (0/10) | 0.43 (3/10) |

## 5. Verdict

**No candidate qualifies. Outcome: no change; saturation documented.** Nothing in `src/` or
`config/config.yaml` changes. `rank_signals` stays exactly as on `origin/main`.

All three fail on U1, the dataset where slot contention matters most. The decisive failures are (ii)
and (iv): A and C newly fail the CI-low gate and fall more than 0.10R behind legacy out of sample,
and B newly fails gate 5 (Sharpe 0.75 against the 0.83 benchmark). Each of these on its own is enough.

**A flaw in my own criterion (iii), disclosed rather than reinterpreted.** Once a key has no ties,
all 10 seed runs are identical to the canonical run (the random draw only breaks ties). The
"seeds failing" count can then only be 0 or 10, depending on the canonical result. On S1 legacy's
canonical run already fails CI low, and so does every candidate, so (iii) records 10 vs 9 and fails.
That S1 failure is an artefact of how (iii) was written, not evidence against the keys. The verdict
does not depend on it: U1 fails (ii) for all three keys regardless. The avg-R range part of (iii)
did what it was meant to do: every candidate takes the order-variance range from 0.07-0.18R to zero.

## 6. What the results say (interpretation; not part of the verdict)

1. **Removing ties removes order luck, but the liquidity tie-break was not luck to be removed.**
   On U1, legacy's canonical run (liquidity tie-break, 0.41) beats all 10 random tie-breaks
   (0.23-0.38). All three strength-based tie-breaks land at or below the bottom of that random
   range (0.19-0.27). On the long_universe info row the picture is similar for A and B. Inside a broad
   universe, choosing among saturated signals by "stronger signal" (bigger move, higher ADX, bigger
   move in ATRs) picks worse trades than choosing the more liquid coin. That matches the playbook (§3):
   small caps revert, and edge sits in the liquid large caps.
2. **On the static 16 majors, A looks better** (S1 avg R 0.11 -> 0.20 and placebo FAIL -> PASS;
   S2 0.53 -> 0.55, Sharpe 0.94 -> 1.04). It reverses on both universe datasets. That is the pattern
   expected if "bigger 5-bar move" is a mild positive among liquid majors and a negative once small,
   volatile coins compete. It is not a robust edge, and S1 is the dataset P1c showed is dominated by
   order noise.
3. **C is the least uneven of the three.** It is the weakest candidate on S1 (0.13), but it is the only key that beats legacy
   on the long_universe info row (0.43 vs 0.40, later-30% 0.24 vs 0.16), and it matches legacy on S2.
   It still fails U1, and that row is not part of any criterion. Treat this as a lead, not a result.
4. **The saturation is a scanner-calibration problem, not a ranking problem.** `momentum_return_threshold:
   0.03` over 5 bars is crossed far more easily on daily bars than on the 1h bars the live bot uses.
   The momentum strength therefore clips for about 70% of daily signals. Fixing that properly means
   rescaling the MOMENTUM strength formula, for example in ATR units like the other detectors. That
   would change `engine_score`, the `min_strength` filter and `trade_planner`'s confidence stars,
   which means a strategy change with its own pre-registration, not a ranking flag.

## 7. Recommendation (the user's call)

- Do **not** wire any of A/B/C into `rank_signals`. Leave `rank_signals` and its liquidity
  tie-break as they are.
- Do not re-run P1e's universe candidates with a strength-based key on this evidence: every
  strength-based key tested here does worse than liquidity on `kraken_720_universe`.
- If order variance in the gate methodology is still a concern, that is P1e §6 option (ii) (treat
  order variance as part of the gate), a methodology change for the Architect. Note that a no-tie
  key makes the seed test degenerate, as §5 shows.
- If engine_score saturation itself is to be fixed, it should be done in the scanner: rescale the
  MOMENTUM strength so it is comparable across patterns and timeframes, pre-registered against a
  **1h** dataset as well as daily, because the live bot runs on 1h. That changes `engine_score` and
  the `min_strength` filter (Market Analyst) and the confidence stars (`trade_planner.py`, Trade
  Strategist), so it must be scoped as a strategy change, not a ranking tweak.
- Do not run a fourth key on these datasets. Three were pre-registered, and adding more after
  seeing these results would inflate the false-pass rate.

## 8. Caveats

1. Daily bars only; live runs on 1h (§1.1). Neither the saturation rate nor any ranking result is
   shown to transfer.
2. U1 is a 719-bar window, and the gate margins are the size of the order noise (P1e §3.7). U1's
   survivorship caveats (P1e §4) apply in full.
3. The placebo for candidate keys carries the original signal's key value (pre-registered). Legacy
   placebo percentiles reproduce P1c/P1e exactly (S1 96 / 86.5, U1 100 / 100).
4. Funding: long-only spot, so there is no funding cost to model. Fees and slippage are included
   (flat `kraken_T3_taker` on S1/S2, P1e tiered slippage on U1).
5. Criterion (iii) was badly designed for no-tie keys (§5). This does not change the verdict.
