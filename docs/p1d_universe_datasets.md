# P1d: universe datasets + point-in-time harness + tiered slippage (build only, no gate run)

**Status:** harness-semantics change. Per CLAUDE.md's QA/Tester roster note, `strategy_pack_run/`
changes need Architect review before anything built on top of this (P1e's gate run) proceeds.
**This doc does not run the 5-gate comparison and makes no promotion/expansion recommendation** --
that's P1e, a separate task gated on an explicit user decision (docs/system-redesign-2026-10.md
§8). This is "build what P1e will need."

Branch: `claude/redesign-p1d-universe-datasets`, built in its own git worktree (not the shared
checkout), off `origin/main`. Not pushed; no PR opened.

## 1. What was built

### 1.1 `fetch_kraken.py --universe discover`
Extended (not rewritten) `strategy_pack_run/fetch_kraken.py` with a `--universe {watchlist,discover}`
flag. `discover` mode calls `ccxt.kraken().load_markets()` live (never hardcodes the base list),
takes every USD-quoted spot market regardless of listing status (`online`/`cancel_only`/
`post_only`), excludes `DEFAULT_EXCLUDE_BASES` (stablecoin/fiat/wrapped), and reuses the exact same
per-symbol `fetch_ohlcv`/closed-candle-trim logic as the original watchlist path -- the only new
code is which base list the existing loop iterates over, plus retry-with-backoff (same pattern
already used in `fetch_coinbase_long.py`) and a `manifest.csv` (base, symbol, status, included,
exclude_reason) for provenance.

**Confirmed live** (2026-10-05, same session): 676 USD spot markets on Kraken --
632 `online`, 42 `cancel_only`, 2 `post_only` -- matching the design doc's own measured numbers
exactly. After excluding 18 stablecoin/fiat/wrapped bases, 658 bases were fetched.

**Note on `config/config.yaml`'s `universe.exclude_bases`:** the task described this as "added in
P1b, already merged" and the authoritative source to read rather than retype from memory. As
checked in this session, `config/config.yaml`'s `universe.exclude_bases` is still `[]` (its own
comment still says `NOT YET USED`) -- the stablecoin/fiat/wrapped list was never actually
populated into config.yaml, only specified in the design doc's §1 table. `fetch_kraken.py`'s
`DEFAULT_EXCLUDE_BASES` uses that design-doc table verbatim (the same list the P1d task
description itself gives) and documents this gap in its own module docstring rather than silently
reading the empty config list and excluding nothing. **Flagging for Architect/Platform Engineer:**
`config/config.yaml`'s `universe.exclude_bases` should probably be populated from this same list
at some point -- not done here, since `config/config.yaml` isn't in QA/Tester's ownership.

### 1.2 `fetch_coinbase_long.py --universe discover`
Extended the same file (already carrying P2.0's `--since`/`--until`/`--resume`/retry-with-backoff,
read first so this doesn't duplicate that work) with `--universe discover --kraken-manifest
<path>`: reads `fetch_kraken.py`'s manifest, takes the bases marked `included`, keeps only the ones
Coinbase Exchange also lists under `--quote`, and fetches each with the existing
`fetch_paginated`/retry logic unchanged. This is "the Coinbase bases that are also in the Kraken
universe" (`long_universe`), not Coinbase's full own listing.

### 1.3 Datasets (new directory: `strategy_pack_run/data_universe/`)
- `data_universe/raw_kraken/` -- `kraken_720_universe`: **658 symbols'** Kraken 1d OHLCV
  (`<BASE>_USD_1d.csv`, 20MB total), plus `manifest.csv` (676 USD markets total: 632 `online` +
  42 `cancel_only` + 2 `post_only`, minus 18 excluded stablecoin/fiat/wrapped bases = 658 fetched).
- `data_universe/raw_long/` -- `long_universe`: **308 symbols'** Coinbase Exchange 1d OHLCV
  (18MB total, 342,433 candles total, earliest first-date 2015-07-20, 9 symbols with a gap), plus
  `data_sources.md` (provenance/gap report, same format as the original `data_long/`). 350 of the
  658 Kraken-universe bases are not listed on Coinbase Exchange under USD at all (full list in
  `data_sources.md`'s own trailing note) -- excluded before any fetch attempt, not a fetch failure.
- **`strategy_pack_run/data/` and `strategy_pack_run/data_long/` (the static-16 baseline) were not
  touched.**

**Storage / reproducibility (decided by the user: gitignore the raw CSVs).** The 966 raw per-symbol
CSVs (~38MB) are **not committed** (`.gitignore`, same precedent as `data_15m/`). Committed instead:
`raw_kraken/manifest.csv`, `raw_long/data_sources.md`, `tiered_slippage_calibration.md` and
`data_universe/SHA256SUMS.txt` (SHA-256 of every raw CSV the P1e gate numbers will be produced from).
Caveats, stated plainly: (1) the snapshot is **not durable** -- it lives only on the machine that
fetched it; if lost, a refetch will NOT be byte-identical. (2) Kraken's OHLC endpoint serves a rolling
~720-bar window, so a later refetch shifts the window start and adds new bars; `cancel_only`/delisted
markets can also disappear. (3) Therefore any P1e gate run must either use this exact snapshot
(verify with `cd data_universe && sha256sum -c SHA256SUMS.txt`) or record a fresh SHA256SUMS.txt and
treat results as a new dataset, not a replication.

### 1.4 Point-in-time universe eligibility: `strategy_pack_run/universe_eligibility.py` (NEW module)
Implements design doc §4.4 rule 1, as a standalone module.

**Correction (post-Architect-review, this fix pass):** the line above used to say this module is
"imported by `backtest_live_bot.py`" -- that was wrong/misleading as written: **nothing in this
repo actually imports `universe_eligibility.py` from `backtest_live_bot.py` or anywhere else that
runs today.** `backtest_live_bot.simulate_portfolio` only accepts a generic `eligible_mask_by_symbol`
parameter (a plain `{symbol: bool array}`, with no dependency on this module at all) -- it's
`test_universe_eligibility.py` and the one-off `calibrate_tiered_slippage.py`-style calibration
work that actually import and exercise this module today. **Turning the universe datasets into an
actual `eligible_mask_by_symbol`/`costs_by_symbol` for a real gate run is P1e's job, not built
here** -- see §4 below, "Explicitly NOT done here."
- `age_eligible_mask` -- true at bar *t* iff at least `min_listing_age_days` of real history
  precedes *t* (config/config.yaml's `universe.min_listing_age_days` = 60, `universe.
  exit_volume_ratio` = 0.5, `universe.min_median_quote_volume_usd` = $1,000,000 -- all read from
  config, not invented).
- `volume_floor_mask_with_hysteresis` -- sticky floor membership (enter at the floor, exit only
  below `floor * exit_volume_ratio`), a single forward-only pass (today's state depends only on
  yesterday's state and today's own trailing volume).
- `eligible_mask_floor` -- age AND volume-with-hysteresis, ANDed per bar. Used for
  `kraken_720_universe`.
- `eligible_mask_rank_topn` -- Coinbase quote volumes aren't Kraken's, so `long_universe`'s
  eligibility is rank-based top-N by trailing dollar volume among age-eligible symbols (N = the
  live/kraken_720 universe size for the floor being tested), **explicitly documented as an
  approximation, not a literal floor match** (no hysteresis -- see the function's own docstring
  for why bolting hysteresis onto a rank rule would be a second, undocumented approximation).
- `EXTRA_EXCLUDE_BASES` / `filter_extra_excluded_bases` (added in this fix pass, item 3 of the
  Architect review): `fetch_kraken.py`'s `DEFAULT_EXCLUDE_BASES` was frozen from an earlier ticker
  snapshot and misses 22 bases now present in the already-fetched `data_universe/raw_kraken` /
  `data_universe/raw_long` directories (9 stablecoins, 6 fiat-pegged, 7 wrapped/liquid-staked --
  full list in the module). Concretely, `WBTC` ranks #87 by Coinbase trailing volume and is present
  in `raw_long` -- at a lenient rank-top-N candidate (e.g. the $100k floor), `eligible_mask_rank_topn`
  would otherwise let in a near-duplicate of BTC. Fixed by filtering the already-fetched data at
  LOAD time (a caller building the trailing-volume dicts these eligibility functions consume should
  apply this filter first), not by refetching -- `fetch_kraken.py`/`fetch_coinbase_long.py` and
  their already-written manifests/CSVs are untouched.

All of the above take a precomputed trailing-dollar-volume array as input (from
`backtest_live_bot.trailing_quote_volume`, already point-in-time via `rolling(...,
min_periods=1)`) -- no function here ever needs data after bar *t* to decide bar *t*.
`test_universe_eligibility.py` (18 tests as of this fix pass) includes
`test_eligible_mask_floor_truncation_invariant` (loops every bar, not a handful of sampled cut
points -- strengthened in this fix pass after mutation testing found the sampled version missed a
one-bar look-ahead bug) and `test_rank_topn_truncation_invariant` (fixture redesigned in this fix
pass to have per-symbol volumes that genuinely cross rank order partway through the window, after
mutation testing found the original monotonically-scaled fixture could never expose a reordering
bug no matter how many bars were sampled), modeled directly on
`test_pack.py::test_truncation_no_lookahead`'s pattern: decisions at bar *t* are proven identical
whether or not the frame is truncated right after *t*.

### 1.5 Ranking parity (design doc §4.4 rule 2)
Already built by the merged P1c work (`backtest_live_bot.simulate_portfolio`'s `use_rank_order`
flag, `src.signal_engine.rank_signals` imported, never reimplemented). P1d's own contribution is
wiring the NEW `eligible_mask_by_symbol` parameter into the same per-bar flow: a bar's
scarce-slot candidate set (`signals_t`, fed to `rank_signals`) now only includes symbols that are
both `FLAT` **and currently eligible** -- an ineligible symbol's signal, if any, is never acted on
and therefore never competes for a slot either. `eligible_mask_by_symbol` defaults to `None`
(every existing caller/test unaffected, byte-for-byte) and, per the design doc's pipeline note
("the monitor always runs for every held symbol, in or out of the universe" §2), **only gates
opening a brand NEW position** -- an already-OPEN/PENDING symbol is monitored to completion
regardless of its current eligibility. Proven by
`test_eligible_mask_by_symbol_does_not_block_monitoring_an_already_open_position` in
`test_backtest_live_bot.py`.

**Correction (post-Architect-review, this fix pass):** the design doc's own §2 table phrases this
as `simulate_portfolio`'s `symbol_whitelist` being "updated per bar." That's not literally how this
was built, and the difference is worth being precise about so nobody goes looking for
per-bar-whitelist-mutation code that doesn't exist: `RiskEngine`'s `symbol_whitelist` is set ONCE,
at construction, to the full static list of every symbol in the run, and is never touched again.
The actual gating mechanism is a plain `if not is_eligible(s, t): continue` check in
`simulate_portfolio`'s FLAT branch, evaluated **before** `risk_engine.check_new_trade` is ever
called for that symbol at that bar -- so an ineligible symbol never even reaches the risk engine
to be considered for a new trade. Same net effect for gating new entries (an ineligible symbol can
never open a new position), but a different mechanism than literally mutating the whitelist.

### 1.6 Tiered slippage: `strategy_pack_run/tiered_slippage.py` (NEW module)
Pre-registered a priori (design doc §4.4 rule 3), by trailing median daily quote volume (USD):

| Tier | Slippage |
|---|---|
| >= $10,000,000 | 5 bp (same as today's flat `kraken_T3_taker` preset) |
| $1,000,000 – <$10,000,000 | 20 bp |
| < $1,000,000 (incl. unknown liquidity) | 50 bp |

`tiered_costs()`/`tiered_costs_by_symbol()` import (never modify) `engine.COST_PRESETS`/`Costs`,
replacing only `slippage` -- the Kraken T3 taker fee is untouched.

**Correction (post-Architect-review, this fix pass):** the original shipped form of
`costs_by_symbol` was ONE static `Costs` per symbol for the ENTIRE run -- flagged as a real
look-ahead/correctness bug on a long multi-year run like `long_universe` (2015-2026): a symbol
that's $5M/day liquid TODAY would get today's 20bp tier applied to its own 2017 dust-era trades
too, which both misprices that era's real cost and leaks today's liquidity backward in time.
Fixed by adding `tiered_costs_series_by_symbol()`, which returns one array of `Costs` PER SYMBOL,
ONE ENTRY PER BAR on the shared global axis, computed fresh at each bar from that symbol's own
trailing dollar volume **through bar t-1** (not bar t -- bar t's own volume isn't known yet at its
open, when a fill/exit actually executes). `backtest_live_bot.simulate_portfolio`'s
`costs_by_symbol` parameter now expects this per-bar form (`dict[str, np.ndarray of Costs]`, one
entry per bar), not the old static `dict[str, Costs]` -- `tiered_costs()`/`tiered_costs_by_symbol()`
remain as present-day snapshot helpers (e.g. for calibrate_tiered_slippage.py, which has no
backtest-over-time concern) but are explicitly documented as NOT safe to pass directly as
`costs_by_symbol` anymore. `simulate_portfolio` also now validates both `eligible_mask_by_symbol`
and `costs_by_symbol` arrays are exactly as long as the shared global bar axis, raising
`ValueError` on a mismatch rather than silently misaligning.

A symbol present in `costs_by_symbol` uses its own per-bar tiered `Costs` for that bar's
fills/exits; a symbol absent falls back to the single global `costs` at every bar, exactly as
before (default `None`, every existing caller unaffected byte-for-byte).

**Calibration against real Kraken order-book spreads:** see §2 below for the actual numbers.
Per the design doc, this is a report, not a feedback loop -- the tiers above are frozen regardless
of what the calibration check finds; any suggested mismatch is flagged for the user, not
auto-corrected.

**Also fixed in this pass:** `backtest_live_bot.placebo_run` did not forward either
`eligible_mask_by_symbol` or `costs_by_symbol` to its own internal `simulate_portfolio` call --
meaning a P1e gate run comparing a universe-gated, tiered-cost real run against placebos where
every symbol was always eligible and costs were flat would not have been a like-for-like
comparison (gate 4, "beats placebo," would have tested the wrong thing). Both are now accepted by
`placebo_run` (additive, default `None`) and forwarded to its internal `simulate_portfolio` call
**unchanged** -- i.e. NOT time-shifted the way the placebo's own signal timing is. This is
deliberate: the mask/costs reflect real calendar-time facts (was this symbol actually part of the
tradeable universe on this real date, what did it actually cost to trade then) that shouldn't move
just because the placebo is testing a counterfactual signal timing.

### 1.7 Survivorship caveat (explicit, per the task)
Including `cancel_only` pairs in `kraken_720_universe` helps (it's the wind-down population that
would otherwise silently vanish from a "today's listings only" universe) -- but coins delisted
more than roughly the last ~2 years are gone from **both** Kraken's and Coinbase's current data
entirely. This is a real, structural limitation of building a backtest universe from two live
exchanges' current APIs, not something fixable within this task's scope. A security this delisted
long ago simply isn't representable here at all, in either direction (it won't inflate results by
being included, but its absence also means the dataset can't be faulted for "should have shown
that coin's history and didn't" -- it's not available anywhere this task could reach).

## 2. Calibration check: a priori tiers vs. real Kraken order-book spreads

Ran `calibrate_tiered_slippage.py` against all 658 `kraken_720_universe` symbols (one
`fetch_order_book` call each, live, 2026-10-05). 637 returned usable order-book depth; 21
illiquid micro-caps (ACX, AI3, CLV, ESX, GHIBLI, HIPPO, HOUSE, KEY, KIN, KOBAN, KP3R, LOCKIN,
MNGO, NTRN, OMNI, SIDEKICK, SLAY, TREMP, U2U, WEN, YALA) returned no bid/ask depth at all and were
skipped -- itself informative about how thin the bottom of this universe is. Full per-symbol
table and methodology notes: `strategy_pack_run/data_universe/tiered_slippage_calibration.md`.

| Tier | n | assumed bp | median half-spread bp | p90 half-spread bp | max half-spread bp | n exceeding assumed | % exceeding |
|---|---|---|---|---|---|---|---|
| >=$10M (5bp) | 9 | 5.0 | 0.41 | 1.31 | 1.58 | 0 | 0.0% |
| $1M-$10M (20bp) | 38 | 20.0 | 1.89 | 4.44 | 8.21 | 0 | 0.0% |
| <$1M (50bp) | 590 | 50.0 | 15.47 | 165.64 | 9987.38 | 150 | 25.4% |

**Reading this (reported as a finding, tiers NOT adjusted, per the design doc's instruction):**
- The top two tiers (>=$1M median daily volume, 47 symbols) are comfortably conservative: even the
  worst observed half-spread (8.21bp) is well inside the assumed 20bp/5bp costs. No concern there.
- The bottom tier (<$1M, 590 of 637 symbols -- i.e. most of this universe lives here) is
  conservative **on median** (15.47bp realized vs. 50bp assumed) but has a **long illiquid tail**:
  150 symbols (25.4%) have a realized half-spread that individually EXCEEDS the flat 50bp
  assumption, some dramatically (BKS 9987bp, SCA 9960bp, GAIA 9561bp, XTER 9238bp, VANRY 8705bp --
  effectively "no real order book," spreads near 100% of price). **This is a genuine finding for
  the user, not something this task fixes or works around:** a single flat 50bp value for
  "everything under $1M" understates true trading cost for roughly a quarter of that tier's
  membership, by a lot in the worst cases. Whether that's addressed by a finer-grained tier split,
  a hard liquidity floor that excludes the worst of this tail outright (the $1M/$100k floor
  candidates P1e will test), or accepted as a known approximation is the user's call, flagged here
  per the task's instruction to report rather than silently adjust.

## 3. Validation run in this worktree

- `PYTHONPATH=. python3 -m pytest -q` (repo root, `tests/` only per `pytest.ini`): **352 passed.**
- `cd strategy_pack_run && PYTHONPATH=..:. python3 -m pytest -q .`: **67 passed** (20 pre-existing
  `test_backtest_live_bot.py` tests unchanged + 5 new ones for `eligible_mask_by_symbol`/
  `costs_by_symbol` + 13 new `test_universe_eligibility.py` + 4 new `test_tiered_slippage.py` +
  every other pre-existing `strategy_pack_run` test file untouched and passing).
- `PYTHONPATH=. python3 scripts/validate_invariants.py`: **5/5 invariants passed.**
- Zero `anthropic`/`openai`/LLM imports in any new or modified file: confirmed via grep (no
  matches).
- Confirmed untouched (`git status --short` against each): `src/` (every file), `strategy_pack_run/
  engine.py`, `strategy_pack_run/strategies.py`, `strategy_pack_run/hyperopt_param.py`,
  `config/config.yaml`, `strategy_pack_run/data/`, `strategy_pack_run/data_long/`.

## 4. Explicitly NOT done here (P1e, separate, gated on the user)
- No 5-gate backtest run against either new dataset.
- No comparison against the static-16 baseline.
- No tier decomposition (original 16 vs. added symbols).
- No floor-candidate sweep ($1M / $100k).
- No promotion/expansion recommendation of any kind.
