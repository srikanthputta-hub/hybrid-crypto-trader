# P1e: 5-gate run for `universe.mode: discover` at the $1M and $100k floors

- **Role:** Architect (Opus). Harness/data/docs only. `src/` and `config/config.yaml` are not touched.
- **Branch:** `claude/redesign-p1e-gate-run`, off `origin/main` at `448787e` (P1d, PR #84).
- **Design reference:** `docs/system-redesign-2026-10.md` §4.4 (validation) and §7 (promotion
  discipline). That design doc is not on `main`; it's cited here by section, as P1c/P1d did.
- **Scope:** evidence only. **This doc does not decide P1f** (flipping `universe.mode`). The user does.

## 0. Pre-registration (written and committed BEFORE any gate code was run or any result seen)

Everything in this section was fixed before the runner existed. Section 3 onward was added
afterwards. If a bug forces a rerun, section 5 says so explicitly. Nothing here is changed after
looking at results.

### 0.1 Datasets (exact snapshot)

- `kraken_720_universe` = `strategy_pack_run/data_universe/raw_kraken/*_USD_1d.csv` (658 files).
- `long_universe` = `strategy_pack_run/data_universe/raw_long/*_USD_1d.csv` (308 files).
- Both must pass `cd strategy_pack_run/data_universe && sha256sum -c SHA256SUMS.txt` (966/966 OK)
  before the run. `sha256(SHA256SUMS.txt) = c13bdcf28d99d494c45b74c1d207c36d62b213f2f0c9c3eabdc5e44fb73363d7`.
  Verified 966/966 OK in this worktree on 2026-10-06 before anything else was done.
- `strategy_pack_run/data/` and `data_long/` (the static-16 legacy datasets) are only used for the
  legacy-reproduction check (0.6) and an informational same-harness row. They are not modified.

### 0.2 Load-time exclusions (non-tradable bases)

Applied when the raw CSVs are loaded, before any eligibility, signal or ranking computation:
1. `fetch_kraken.DEFAULT_EXCLUDE_BASES` (already excluded at fetch; re-applied defensively).
2. `universe_eligibility.EXTRA_EXCLUDE_BASES` (P1d's 22 stablecoin/fiat/wrapped bases).
3. **New in P1e, decided from a price-behaviour hygiene scan run before any backtest:**
   `CASH, FIDD, SOFID, USAT, FRNT` (USD-pegged stablecoins: median price 0.999-1.000, daily log-return
   std <= 0.1%), `BRL1` (BRL-pegged fiat stablecoin), `XU3O8` (tokenized uranium, a commodity, not a
   crypto asset), `USDSUI` (stablecoin name, empty file anyway). `FIDD` (trailing-30d median volume up
   to ~$544k) and `BRL1` (~$101k) would otherwise clear the $100k floor at some point.
4. Files with zero rows (`PNT`, `USDSUI`) are dropped.
`config/config.yaml`'s `universe.exclude_bases` is still `[]` and is not edited here (Platform Engineer
territory; flagged in P1d already).

### 0.3 Harness configuration (identical for every row unless stated)

- Pipeline: `strategy_pack_run/backtest_live_bot.py` `simulate_portfolio` / `placebo_run`, live
  `config/config.yaml` values for indicators/scanner/signal_engine/htf_filter/trade_planner/risk,
  1d bars, start equity $10,000, PPY 365.
- **Ranking parity (§4.4 rule 2):** `use_rank_order=True` in every gated row, including the static-16
  baseline (`src.signal_engine.rank_signals`, imported).
- **Costs (§4.4 rule 3):** `kraken_T3_taker` fee (0.38%) + tiered slippage, per bar, from
  `tiered_slippage.tiered_costs_series_by_symbol` (trailing-30-bar median dollar volume through bar
  t-1; >=$10M: 5bp, $1M-$10M: 20bp, <$1M: 50bp). **Tiers frozen at P1d's values, not retuned.**
- Placebo: 200 time-shift sims, `seed=0`, eligibility mask and per-bar costs forwarded unshifted
  (P1d's `placebo_run` semantics).
- Bootstrap: `engine.bootstrap_ci` (5000 resamples, seed 0) on per-trade R.

### 0.4 Candidate definitions (two candidates; no others will be run)

| Candidate id | Dataset | Eligibility at bar t |
|---|---|---|
| `universe-discover-1m` | `kraken_720_universe` | point-in-time floor: age-eligible AND trailing-30d median dollar volume >= $1,000,000 with hysteresis (stay until < $500,000, `exit_volume_ratio` 0.5), then capped at the top `max_symbols`=150 by trailing volume at that bar |
| `universe-discover-1m` | `long_universe` | rank-based: top-N age-eligible symbols by trailing-30d median dollar volume at bar t, no hysteresis (P1d's documented approximation) |
| `universe-discover-100k` | both | same, with $100,000 floor / $50,000 exit |

Details fixed now:
- **Trailing volume for eligibility** = 30-bar rolling median of `close*volume` with
  `min_periods=30` (a "30-day median" needs 30 days; before that the symbol is ineligible). Through
  bar t inclusive: eligibility is decided at bar t's close, which is when the live bot would decide.
- **Listing age** = `min_listing_age_days` 60 (config). **Left-censoring rule:** a symbol whose first
  bar equals its dataset's first bar (Kraken serves only ~720 bars, so its real listing predates the
  window) is treated as already >= 60 days old at the window start. This is what the live bot would
  see (it fetches up to 720 daily bars, reaching before the window start). Symbols first seen later
  get the real 60-day gate.
- **Original 16 are not pinned:** in discover mode they face the same floor as everything else
  (`include_symbols: []`).
- **N for `long_universe`** = the number of `kraken_720_universe` symbols eligible under the same
  floor on the **last bar** of the Kraken window (the universe the live bot would trade if flipped
  today), capped at 150. The median eligible count over the Kraken window is reported alongside.
  Formula fixed now; the value is a data property, computed before any simulation.
- Eligibility only gates opening NEW positions; held positions are monitored to completion (P1d).

### 0.5 Baselines

- **Primary static-16 baseline (criterion b)** = same dataset files restricted to the 16
  `market.symbols` bases (BTC ETH XRP SOL ADA DOGE AVAX LINK BNB LTC DOT ATOM ALGO BCH XTZ SHIB, USD
  pairs), always eligible (static mode has no floor), same harness (ranking + tiered slippage + same
  placebo). This gives a like-for-like comparison on identical bars.
- **Informational:** the same static-16 same-harness run on legacy `data/` and `data_long/`.
- **Flat-5bp info row:** each candidate re-run with flat `kraken_T3_taker` (5bp slippage for every
  symbol, `costs_by_symbol=None`). Comparison only; never used for a verdict.

### 0.6 Legacy reproduction check (harness integrity, must hold before any result is trusted)

`run_live_bot_report.py`'s path (sorted order, flat `kraken_T3_taker`, no mask) on `data/` must give
**154 trades / avg R 0.18**, and on `data_long/` **562 trades / avg R 0.52**. If either doesn't
reproduce, stop and investigate before reading any P1e number.

### 0.7 Gates (criterion a), per candidate per dataset

Identical to `run_live_bot_report.py` unless stated:
1. trades >= 100.
2. bootstrap avg-R 95% CI low > 0.
3. avg R of trades entered in the later 30% of the dataset window (`cut = t0 + 0.7*(t1-t0)`) > 0.
4. real run beats the 200-sim placebo at >= 90th percentile on **both** total return and avg R.
5. net return > 0 **and** Sharpe > benchmark Sharpe, where benchmark = the **higher** of (i)
   equal-weight buy&hold of the original-16 bases and (ii) equal-weight buy&hold of every symbol ever
   eligible in that candidate run, both over the same window (`engine.buy_hold`). Taking the higher
   one removes the benchmark choice as a degree of freedom. BTC buy&hold is reported for information.

A candidate passes (a) on a dataset only if all 5 gates PASS there. Per the Routine's confirmed rule
(design §11 item 4), (a) requires PASS on **both** datasets.

### 0.8 No regression (criterion b), per dataset, vs the primary static-16 baseline

Regression if either holds:
- (b1) the candidate FAILs a gate that the static-16 baseline PASSes; or
- (b2) the candidate is worse than the baseline on **both** avg R and Sharpe.

### 0.9 Tier decomposition (criterion c), per candidate per dataset

Trades split into original-16 bases vs added bases. For each: trade count, avg R, bootstrap 95% CI.
- Added-symbol avg R <= 0 on either dataset -> **FAIL of the expansion**, even if (a) and (b) pass.
- Added-symbol avg R > 0 but fewer than 30 added trades on a dataset -> (c) INCONCLUSIVE there.

### 0.10 Processing-order robustness

`engine_score` saturates at 1.0 for ~2/3 of signals (P1c finding A), so the liquidity tie-break in
`rank_signals` decides most slot contests. For each candidate/dataset, 10 alternate runs replace that
tie-break with a seeded random one (seeds 1-10; `engine_score` stays the primary key), via a
runner-local patch of `src.signal_engine.rank_signals` (no harness or src change). Reported: spread of
trades / avg R / return / Sharpe / added-symbol avg R, and gates 1, 2, 3, 5 and (c) per seed. Placebo
(gate 4) is not rerun per seed (cost).

### 0.11 Verdict rule per candidate

- **FAIL** if, on the canonical run (real liquidity tie-break), (a), (b) or (c) fails on either
  dataset. A canonical FAIL is never upgraded by an alternate ordering.
- **PASS** if (a), (b) and (c) all pass on both datasets on the canonical run **and** all 10
  alternate-order runs on both datasets still pass gates 1, 2, 3, 5 and (c).
- **INCONCLUSIVE** otherwise: canonical PASS but at least one alternate ordering fails one of those
  checks, or (c) is INCONCLUSIVE on a dataset with nothing else failing.

The verdict feeds a recommendation on P1f. It is not a decision.

### 0.12 What happens after results

No parameter, tier, floor, exclusion, benchmark or rule above is changed after results are seen. A
bug found after results gets fixed, every number is rerun, and section 5 records it. The two
candidates are recorded as `kind: new_strategy` entries in `scripts/backtest_summary.json` only if
that file's existing schema allows it without `src/` changes.

---

_Everything below was added after the pre-registration commit (`d869832`)._

## 1. What was built (prerequisites a-d from the P1d hand-off)

| File | What it is |
|---|---|
| `strategy_pack_run/universe_gate_inputs.py` (new) | The builder. `load_universe_raw` applies the §0.2 exclusions at load. `build_eligibility` produces `eligible_mask_by_symbol`: `"floor"` rule (kraken_720_universe: P1d's `eligible_mask_floor`, i.e. age AND floor-with-hysteresis, then a per-bar top-150 cap) or `"rank_topn"` rule (long_universe: P1d's `eligible_mask_rank_topn`). `build_costs` produces the per-bar tiered `costs_by_symbol` via P1d's `tiered_costs_series_by_symbol` (frozen 5/20/50bp, t-1 volume). Imports P1d's modules and `backtest_live_bot`; modifies none of them. |
| `strategy_pack_run/run_universe_gate.py` (new) | The runner. It verifies SHA256SUMS (refuses to run on a mismatch), builds one shared bar axis per dataset, computes Stage-A signals once per symbol (cached outside the repo), runs every case through `simulate_portfolio` / `placebo_run`, and writes `report_p1e_universe_gate.md` + `p1e_universe_gate_results.json`. |
| `strategy_pack_run/test_universe_gate_inputs.py` (new, 15 tests) | 9 synthetic unit tests (always run). 6 real-data tests (skip when the gitignored datasets are absent; note CI runs only `tests/` (`testpaths`), so no `strategy_pack_run` test runs in CI at all): every-bar truncation invariance of masks + per-bar slippage on a real 10-symbol kraken slice (floor rule) and a real 8-symbol long slice (rank rule); end-to-end builder -> signals -> `simulate_portfolio` truncation invariance on the real kraken slice (cut every 15 bars: entries, closed trades and the equity curve before the cut must match); two permanent mutation tests; and a check that the runner's own assembly reproduces `build_enriched_and_signals` exactly. |

- **(a) Truncation invariance + mutation check.** Mutation results, run by hand against the real
  builder (the file was restored afterwards and the restore was confirmed):
  - `eligibility_volume(...).shift(-1)` (peeks 1 bar ahead): **caught** by the every-bar mask test.
    **Not caught** by the end-to-end test. A one-bar peek only changes the truncated run at the cut
    bar itself, so it only shows up if a signal on a floor-crossing symbol lands exactly there.
  - `.shift(-3)`: caught by both the every-bar mask test and the end-to-end test.
  - centered 30-bar window: caught by the every-bar mask test. The original 3-cut end-to-end test
    missed it, so it was tightened to a cut every 15 bars plus an entries comparison. The
    tightened test catches it (entries differ at cut 540).
  - The two permanent mutation tests in the file inject a 1-bar eligibility peek and a 2-bar cost
    peek and assert the check flags them.
  Bottom line: the every-bar mask/cost check is the detector that matters. The end-to-end test is a
  coarser second layer.
- **(b) Flat-5bp info row:** `discover-*-flat5bp` cases, same masks, `costs_by_symbol=None`.
- **(c) Static-16 on the same harness:** `static16|<dataset>` cases (rank order + tiered slippage +
  same placebo). Legacy flat-cost reproduction is in the runner's last step (§3.6).
- **(d) Gate always on:** every candidate case passes its mask. Exclusions are applied at load.
- **Equivalence check (added after the smoke run, before the real run):** each candidate only
  simulates symbols that are eligible at least once. A symbol that is never eligible can never open
  a position, so dropping it should change nothing. The runner checks this by re-running the $1M
  candidate over the $100k candidate's larger symbol set and requiring identical trades.
- **Diagnostics (read-only):** a `RiskEngine.check_new_trade` wrapper (same pattern as
  `run_rank_order_validation.py`) counts rejections by reason. It also detects the same-bar cash-race
  clip, which P1c flagged as a latent order-dependent path that a larger universe could activate.

No `src/`, `config/config.yaml`, `engine.py`, `backtest_live_bot.py`, `universe_eligibility.py` or
`tiered_slippage.py` change.

## 2. Exact commands

```bash
git -C /home/user/hybrid-crypto-trader worktree add /home/user/hct-p1e -b claude/redesign-p1e-gate-run origin/main
# copy (not move) the gitignored raw CSVs from the P1d scratch worktree, then:
cd /home/user/hct-p1e/strategy_pack_run/data_universe && sha256sum -c SHA256SUMS.txt   # 966/966 OK
cd /home/user/hct-p1e/strategy_pack_run
PYTHONPATH=..:. python3 -m pytest -q test_universe_gate_inputs.py                     # 15 passed
# smoke run (2 placebo sims, 1 order seed) -- used only to find bugs, numbers discarded
PYTHONPATH=..:. python3 run_universe_gate.py --cache-dir <scratch> --workers 4 --placebo-sims 2 --order-seeds 1 --out-md <scratch>/smoke.md --out-json <scratch>/smoke.json
# the gate run
PYTHONPATH=..:. python3 -W ignore run_universe_gate.py --cache-dir <scratch> --workers 4
```

**Disclosure:** the smoke run printed real (2-sim placebo) numbers before the real run. Between the
smoke run and the real run the only changes were the diagnostics and the equivalence check
described in §1, plus a cosmetic fix to how rejection reasons are grouped. No rule, parameter,
tier, floor, exclusion or benchmark changed. The builder/runner/tests were first committed
(`903bd9a`) after the smoke runs and before the real run. That commit is exactly the code that
produced §3.

## 3. Results (real run: 200 placebo sims, 10 order seeds, 1963 s)

The full generated tables are in `strategy_pack_run/report_p1e_universe_gate.md`. The raw numbers,
including every order seed and the risk-engine diagnostics, are in
`strategy_pack_run/p1e_universe_gate_results.json`. Dataset snapshot: SHA256SUMS 966/966 OK.

### 3.1 Universe sizes (data properties, computed before any simulation)

| dataset | loaded (after exclusions) | window | floor | eligible on last bar | median eligible per bar | ever eligible (orig-16) |
|---|---|---|---|---|---|---|
| kraken_720_universe | 627 | 2024-10-17 to 2026-10-05 (719 bars) | $1M | 49 | 38 | 104 (16) |
| kraken_720_universe | 627 | same | $100k | 150 (cap binds) | 126 | 318 (16) |
| long_universe | 304 | 2015-07-21 to 2026-10-05 (4095 bars) | $1M -> N=49 | 49 | 31 | 204 (16) |
| long_universe | 304 | same | $100k -> N=150 | 150 | 31 | 288 (16) |

On long_universe the median is far below N because few of these bases existed before about 2018.
For much of that history, "top-N" in practice means "every age-eligible symbol".

### 3.2 Headline table (kraken_T3_taker + tiered slippage, rank order)

| case | trades | win% | avg R [95% CI] | return | maxDD | Sharpe | bench Sharpe | later-30% avg R (n) | placebo pct ret / avgR |
|---|---|---|---|---|---|---|---|---|---|
| static-16, kraken_720_universe | 134 | 40.3% | 0.15 [-0.14, 0.46] | 18.6% | -24.0% | 0.58 | 0.27 | 0.45 (42) | 97 / 91 |
| **$1M, kraken_720_universe** | 172 | 46.5% | 0.41 [0.15, 0.69] | 38.9% | -21.4% | 0.99 | 0.83 | 0.56 (83) | 100 / 100 |
| **$100k, kraken_720_universe** | 264 | 39.4% | 0.21 [**-0.04**, 0.45] | 16.5% | -27.4% | 0.47 | 0.27 | 0.06 (121) | 96 / 95 |
| static-16, long_universe | 572 | 48.4% | 0.52 [0.37, 0.68] | 312.8% | -30.7% | 0.92 | 0.82 | 0.31 (262) | 100 / 98 |
| **$1M, long_universe** | 838 | 43.9% | 0.40 [0.27, 0.53] | 371.3% | -33.8% | 0.90 | 0.82 | 0.16 (418) | 100 / **88** |
| **$100k, long_universe** | 980 | 44.2% | 0.40 [0.28, 0.52] | 444.6% | -36.0% | 0.93 | 0.82 | 0.25 (534) | 100 / 94 |
| _info: $1M flat 5bp, kraken_ | 172 | 46.5% | 0.44 [0.18, 0.72] | 42.3% | -20.5% | 1.06 | | 0.59 | not run |
| _info: $100k flat 5bp, kraken_ | 260 | 40.4% | 0.31 [0.06, 0.57] | 22.3% | -24.0% | 0.58 | | 0.28 | not run |
| _info: $1M flat 5bp, long_ | 835 | 44.7% | 0.44 [0.31, 0.58] | 390.5% | -35.2% | 0.92 | | 0.19 | not run |
| _info: $100k flat 5bp, long_ | 966 | 43.7% | 0.41 [0.28, 0.54] | 474.3% | -36.6% | 0.96 | | 0.22 | not run |

How "bench Sharpe" was chosen (§0.7): kraken $1M's benchmark is equal-weight buy&hold of the 104
ever-eligible symbols (0.83). Every other row uses equal-weight buy&hold of the original 16 (0.27 /
0.82), because that was the higher of the two. BTC buy&hold Sharpe: 0.50 on kraken, 1.10 on long
(information only).

### 3.3 Gates (criterion a)

| case | >=100 trades | CI low > 0 | later-30% > 0 | placebo >= 90th | ret>0 & Sharpe>bench | all 5 |
|---|---|---|---|---|---|---|
| static-16, kraken_720_universe | PASS | FAIL | PASS | PASS | PASS | FAIL (4/5) |
| $1M, kraken_720_universe | PASS | PASS | PASS | PASS | PASS | **PASS** |
| $100k, kraken_720_universe | PASS | **FAIL** | PASS | PASS | PASS | **FAIL** |
| static-16, long_universe | PASS | PASS | PASS | PASS | PASS | PASS |
| $1M, long_universe | PASS | PASS | PASS | **FAIL** (avg R 88th) | PASS | **FAIL** |
| $100k, long_universe | PASS | PASS | PASS | PASS | PASS | **PASS** |

### 3.4 No regression vs same-harness static-16 (criterion b)

| candidate / dataset | b1: fails a gate the baseline passes? | b2: worse on BOTH avg R and Sharpe? | regression? |
|---|---|---|---|
| $1M / kraken_720_universe | no (baseline already fails CI) | no (0.41 vs 0.15; 0.99 vs 0.58) | no |
| $1M / long_universe | **yes** (placebo) | **yes** (0.40 vs 0.52; 0.90 vs 0.92) | **YES** |
| $100k / kraken_720_universe | no (both fail only CI) | no (avg R 0.21 vs 0.15 better; Sharpe 0.47 vs 0.58 worse) | no |
| $100k / long_universe | no | no, narrowly (avg R 0.40 vs 0.52 worse; Sharpe 0.93 vs 0.92 better) | no |

### 3.5 Tier decomposition (criterion c)

| candidate / dataset | orig-16 trades | orig-16 avg R [CI] | added trades (symbols) | added avg R [CI] | (c) |
|---|---|---|---|---|---|
| $1M / kraken_720_universe | 82 | 0.33 [-0.06, 0.72] | 90 (30) | 0.49 [0.13, 0.85] | PASS |
| $100k / kraken_720_universe | 85 | 0.14 [-0.22, 0.52] | 179 (67) | 0.24 [-0.07, 0.57] | PASS |
| $1M / long_universe | 472 | 0.55 [0.38, 0.71] | 366 (74) | 0.22 [0.01, 0.42] | PASS |
| $100k / long_universe | 449 | 0.58 [0.40, 0.76] | 531 (146) | 0.24 [0.08, 0.41] | PASS |

Added symbols have positive avg R everywhere, so criterion (c) never fails. On long_universe,
though, the added symbols earn less than half the original-16's avg R (0.22-0.24 vs 0.55-0.58). On
the 11-year history the expansion dilutes edge per trade; it doesn't add to it. On
kraken_720_universe at $1M it's the reverse (0.49 vs 0.33). That's a two-year window, and §3.7
shows the ordering behind it is fragile.

### 3.6 Legacy reproduction and legacy same-harness rows

| dataset | run | trades | avg R [CI] | return | Sharpe | gates |
|---|---|---|---|---|---|---|
| `data/` | legacy flat 5bp, sorted order | **154** | **0.18** [-0.08, 0.46] | 22.5% | 0.68 | (reproduces the record) |
| `data/` | static-16, same harness | 137 | 0.08 [-0.20, 0.38] | 13.6% | 0.46 | PASS FAIL PASS FAIL(97/88) PASS |
| `data_long/` | legacy flat 5bp, sorted order | **562** | **0.52** [0.36, 0.68] | 387.5% | 1.00 | (reproduces the record) |
| `data_long/` | static-16, same harness | 572 | 0.52 [0.36, 0.68] | 307.3% | 0.92 | 5/5 PASS |

The §0.6 integrity check passes: both legacy baselines reproduce exactly.

### 3.7 Processing-order robustness (10 random liquidity tie-breaks; gates 1, 2, 3, 5 and (c))

| case | canonical avg R | alt avg R min / median / max | alt Sharpe min / max | seeds failing a check | which check |
|---|---|---|---|---|---|
| $1M / kraken_720_universe | 0.41 | 0.23 / 0.33 / 0.38 | 0.64 / 1.07 | **6 of 10** | 5x gate 5 (Sharpe below the 0.83 benchmark), 1x CI low |
| $100k / kraken_720_universe | 0.21 | 0.06 / 0.12 / 0.29 | 0.16 / 0.75 | 9 of 10 | CI low (one of them also gate 5) |
| $1M / long_universe | 0.40 | 0.39 / 0.46 / 0.48 | 0.91 / 1.00 | none | - |
| $100k / long_universe | 0.40 | 0.31 / 0.37 / 0.41 | 0.79 / 0.98 | 1 of 10 | gate 5 |

**The only clean 5/5 gate pass on kraken data in this repo's history ($1M) doesn't survive a
change of tie-break.** The canonical liquidity tie-break gave a higher avg R (0.41) than all 10
random tie-breaks (max 0.38). That can't separate "preferring the most liquid coin genuinely helps"
from "luck of one ordering". The design doc's warning holds here: on kraken_720, order variance is
the same size as the gate margins. `engine_score` sits at exactly 1.0 for 77.8% (kraken) and 74.2%
(long) of signals, even more saturated than P1c measured (66-69%), so the tie-break decides most
slot contests.

### 3.8 Risk-engine diagnostics

- Same-bar cash-race clip (P1c's latent order-dependent path): **bound zero times** in every run.
- The binding constraints are the volatility filter (`risk.max_volatility_pct` 8%) and exposure/cash
  room, then the new-entry rate cap. In universe runs the volatility filter is the single largest
  rejection reason (e.g. long $100k: 12,262 volatility, 11,570 no-room, 2,615 rate-cap rejections
  out of 27,427 checks). The risk engine already removes much of the small-cap tail before it can
  trade.
- Superset equivalence: identical trades on both datasets. Simulating only ever-eligible symbols
  changes nothing.

## 4. Caveats (read before using any number above)

1. **Survivorship, worse than for the static-16 datasets.** `kraken_720_universe` includes
   `cancel_only` pairs, but anything delisted more than about 2 years ago is gone from both
   exchanges. `long_universe` is worse: its bases are **today's Kraken universe intersected with
   today's Coinbase listings**, replayed back to 2015. Every coin that died in 2017-2022 is missing,
   and the added symbols are survivors by construction. That flatters the added-symbol rows on long
   specifically, and it still couldn't produce a clean pass.
2. **long_universe eligibility is an approximation** (rank top-N on Coinbase volume, no hysteresis).
   N is taken from today's Kraken universe size and applied to 2015-2026.
3. **Processing-order variance** (§3.7) is large on kraken_720_universe and isn't captured by the
   bootstrap CI or the placebo.
4. **Tiered slippage understates the tail.** P1d's calibration found 25% of the <$1M tier with real
   half-spreads above 50bp, some near 100%. The $100k candidate trades in that tier. Costs take
   roughly 0.05-0.1R off, but the kraken CI failure is NOT mainly cost-driven: the independent
   review re-ran $100k kraken with flat 5bp under the same 10 alternate tie-break orders and 7 of 10
   still failed the CI gate (9 of 10 with tiered costs). It is mostly order/sample variance. True
   costs there are likely higher than modeled, not lower.
5. **Daily bars, not the live 1h feed** (same limitation as every live-strategy report), and the
   cooldown/daily-loss gates are inert at daily resolution.
6. **Gate-5 benchmark on kraken $1M** is equal-weight buy&hold of the symbols that became eligible
   at some point. That set is chosen with hindsight (coins that got liquid tend to be coins that
   pumped), so it's a deliberately strict bar. It was pre-registered as "the higher of the two" and
   is not revisited. With the original-16 benchmark (0.27) instead, the 5 seeds that failed only on
   gate 5 would have passed it.
7. **Smoke-run disclosure** (§2): smoke numbers were seen before the real run. No rule changed.
8. **Minimum order notional** (design §4.5, open question 11) is still unimplemented. A larger
   universe makes dust fills more likely live; the backtest doesn't model exchange minimums.

## 5. Bugs found and reruns

None after the real run. Problems found and fixed before it, none of which changed a rule:
- a `global` SyntaxError in the runner;
- the end-to-end truncation test was too coarse (3 cuts) to catch a centered-window look-ahead, so
  it was tightened (§1);
- rejection-reason grouping was cosmetic-only.
The real run was executed once.

## 6. Verdict (per the pre-registered rule §0.11) and recommendation

| candidate | (a) both datasets 5/5 | (b) no regression | (c) added avg R > 0 | order robustness | **verdict** |
|---|---|---|---|---|---|
| `universe-discover-1m` | FAIL (long: placebo avg R 88th < 90th) | FAIL (long: b1 and b2) | PASS / PASS | kraken: 6/10 seeds fail | **FAIL** |
| `universe-discover-100k` | FAIL (kraken: CI low -0.04) | PASS (long only narrowly) | PASS / PASS | kraken: 9/10 seeds fail | **FAIL** |

**Both candidates FAIL.** Neither meets the Routine's rule (5/5 on both datasets). $1M also
regresses against the same-harness static-16 baseline on the long history.

**Recommendation on P1f (the user's decision, not made here): do not flip `universe.mode:
discover` on this evidence.** Reasons, in order of weight:
1. Neither floor passes on both datasets. The one kraken pass ($1M) depends on processing order:
   6 of 10 alternate tie-breaks fail it.
2. On the 11-year long history, adding symbols lowers avg R (0.52 -> 0.40) and gives no Sharpe gain
   over static-16. The added coins earn about 0.23R vs the majors' 0.55R, and that's with
   survivorship flattering them.
3. This matches the playbook's prior (small caps revert; more coins lowered Sharpe), which design
   §4.5 called out as a plausible, legitimate outcome.

What this does **not** show: that the expansion is harmful on a pure liquidity-ranked basis
(kraken $1M is genuinely better than static-16 on the canonical run). If the user wants to pursue
the universe anyway, the honest next step is **not** a third floor (that would break the
"at most two a-priori floors" pre-registration and inflate false-pass odds). Two options instead:
(i) fix `engine_score` saturation first (P1c finding A, Market Analyst), so slot allocation isn't
mostly a tie-break, then re-run these same two pre-registered candidates; or (ii) treat order
variance as part of the gate (e.g. require the gates across the order-seed distribution), which is
a methodology change for the user/Architect to pre-register, not something to apply retroactively
here. Separately, the same-harness static-16 on legacy `data/` now fails the placebo gate (97th /
88th), a reminder that the live strategy's own kraken evidence is marginal regardless of universe.

### `scripts/backtest_summary.json`

The existing schema supports this without `src/` changes. `kind: new_strategy`, `config_key: null`
and `config_value: null` are already defined, and both consumers (`export_dashboard_data.py`,
`web/app.js`) tolerate unknown dataset ids. Recorded: four entries (two candidates x two datasets,
same id per candidate, matching the existing per-dataset pattern), plus the two dataset ids in
`datasets` and an optional, additive `harness_version` field in `candidates_schema`, per design §7.
The Routine's promotion rule looks for `kraken_720` + `long`, so these entries can't be
auto-promoted, and as `new_strategy` with null `config_key` there's no mechanical path anyway.


## 7. Post-review corrections (independent Opus review of this branch; §0 stays frozen)
- **Order fragility cuts both ways.** On long_universe $1M, all 10 alternate tie-break orders give a
  higher avg R (0.39-0.48, median 0.46) than the canonical order (0.40), and several beat the baseline
  Sharpe (0.92). The long placebo FAIL and the regression vs static-16 are therefore probably
  order-dependent too, not only the Kraken result. The pre-registered rule still yields FAIL.
- **Cost attribution:** see §4 caveat 4 (7/10 alternate orders still fail the $100k CI gate at flat 5bp).
- **"Static-16 fails the placebo gate on the same harness"** holds on legacy `data/` only (97th/87.5th,
  and it also fails CI). On `kraken_720_universe` it passes placebo (97/91) and fails only CI.
- **Exclusions:** §0.2's FRNT description is wrong: its daily log-return std is 11.3% (spiky prints),
  not <= 0.1%. The price-only hygiene scan flagged only CASH/FIDD/SOFID/USAT; FRNT, BRL1 and XU3O8 were
  identified by name/peg inspection, also before any backtest. LCAP (tracks a BTC/ETH ratio) was not
  excluded, inconsistent with XU3O8's reasoning, but it has 0 trades so no verdict is affected.
  Reviewer confirmed the 8 exclusions cannot change any verdict (264 trades / avg R 0.209 / CI low -0.039
  identical with and without them).
- **Test blind spot:** a 1-bar look-ahead in the cost wiring (bar t costed on its own volume) passes
  all 15 tests, because truncating at t keeps bar t; the permanent mutation test uses a 2-bar peek.
  The t-1 shift itself is unit-tested in `test_tiered_slippage.py`; the P1e wiring assertion
  `build_costs(eg)[s][t].slippage == tiered_slippage_fraction(trailing_quote_volume(eg[s])[t-1])`
  is a recorded follow-up. The every-bar check is the detector for eligibility, not for costs.
- **`backtest_summary.json`:** the four P1e entries carry `gates.overall: "FAIL"` (the candidate
  verdict) so the dashboard's Passed tab cannot show them as all-pass; per-dataset results remain in
  the gate fields and notes.
- `p1e_universe_gate_results.json` contains bare `NaN` tokens (not strict JSON; nothing reads it).
