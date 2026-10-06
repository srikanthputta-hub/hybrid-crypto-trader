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

_Sections 1-6 (method as built, commands, results, caveats, verdict) are added after the run._
