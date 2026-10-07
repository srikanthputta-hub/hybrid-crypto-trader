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
