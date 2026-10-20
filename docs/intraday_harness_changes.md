# P1 harness changes needed to run the intraday pre-registration

- **Role:** Architect (Opus). Docs only: this is the build list and the acceptance tests for the P1 harness work
  that `docs/intraday_prereg.md` needs. Nothing here is built yet. Owner of the build: QA/Tester (Sonnet) for
  `strategy_pack_run/`; Architect reviews harness semantics (CLAUDE.md roster); Risk Officer confirms the risk-engine
  wiring (H4).
- **Ground rule:** new behaviour goes into **new modules**. `engine.py`, `strategies.py`, `backtest_live_bot.py`,
  `run_live_bot_report.py`, `run_report.py`, `tiered_slippage.py` and everything under `src/` stay untouched. The one
  existing file that changes is `resample_ohlcv.py` (one additive keyword, default = today's behaviour). This is what
  makes AT1 (byte-identical baselines) achievable by construction rather than by luck.
- **Order:** this list is built and every acceptance test is green, on a committed hash, **before** any family is run
  on evaluation data (`intraday_prereg.md` §0 rules 1 and 3). The run then records that hash.

## H1. `strategy_pack_run/intraday_data.py` (new): data, snapshot, universes, masks

- **Loader:** `load_native(base, tf, root="/home/user/hct-data")` for `tf in {"1h", "15m"}` reads
  `<root>/<tf>/<BASE>_USD_<tf>.csv` (`timestamp` = bar open, epoch ms) into the close-time-indexed frame used by
  `backtest_live_bot.load_csv` (same columns, `index = open + tf`). No forward fill, no interpolation, gaps kept.
- **Snapshot verification (prefix rule):** `verify_snapshot(root, coverage_json, sha_file)` hashes, per file used,
  the header plus the first `coins[BASE][tf]["bars"]` data lines (byte-exact, as stored) and compares with
  `strategy_pack_run/data_intraday/SHA256SUMS.txt`. Any mismatch raises; the runner refuses to start.
- **Window cut:** every frame is cut to decision-bar close times `<= 2026-10-20 00:00 UTC`; warm-up history before the
  window start is kept for indicators only.
- **Derived bars:** `daily_from_1h(df_1h)` (complete UTC days only, 24 of 24 hourly bars; OHLCV first/max/min/last/sum)
  and `four_hour_from_1h(df_1h)` via the generalized `resample_ohlcv` (H2). Never built from 15m.
- **Mixed-source guard:** every frame carries `attrs["source"] in {"native_1h", "native_15m", "1h->4h", "1h->1d"}`.
  A run whose decision or base frames combine `native_15m` with any `1h*` source for the same purpose raises
  (`intraday_prereg.md` §1.1; orchestrator decision 1). Daily inputs (`1h->1d`) are allowed in a 15m run because they
  are daily-only inputs, not a second intraday resolution; the guard enforces exactly that distinction.
- **Universes:** `UNIVERSE_B` (14 coins, 2021-10-01..2026-10-20), `UNIVERSE_A` (15 coins, 2023-07-14..2026-10-20),
  the 15m variants without XTZ, BNB never loaded. XRP is loaded only from 2023-07-13 20:00 (its continuous segment;
  the 2019-2021 segment is never read, never stitched).
- **Masks:** `MASK_DAYS_15M = ["2023-03-04", "2024-08-13".."2024-09-03", "2025-01-01".."2025-04-30"]` for every coin but
  BTC/ETH on 15m; `entry_allowed_15m(coin, t, n, H)` implements the "no masked bar in `[t-(n+100), t+H]`" rule.
- **Eligibility:** `eligible(coin, t)` = >= 60 complete daily bars before t and family indicators warm (the family
  passes its warm-up length).

## H2. `resample_ohlcv.py`: one additive keyword

`resample_ohlcv(df, target_tf, source_minutes=15)`. Today `SOURCE_MINUTES = 15` is hard-coded, so a 1h source would
expect 16 rows per 4h bin and drop every bin. With `source_minutes=60`, a 4h bin needs 4 rows and a 1d bin 24. Default
15 keeps every existing caller and test byte-identical. Bin anchor unchanged (`origin="start_day"`, 00/04/.../20 UTC).

## H3. `strategy_pack_run/intraday_families.py` (new): the three signal builders

Pure functions, closed-bar only, each returning a per-bar frame on the coin's decision-bar axis:
- `family1_signals(df_tf, daily, btc_reg, N)` -> `raw_entry`, `entry` (= raw & regime), `atr_d`, `stop0`, `regime_on`.
- `family2_signals(df_tf, daily, btc_reg, n, z_e)` -> `raw_entry`, `entry` (= raw & ADX_D < 20 & regime), `sma`,
  `sd`, `z`, `atr_tf`, `stop`, `target`, `rr`.
- `family3_state(closes_4h_by_coin, daily_by_coin, btc_reg, L)` -> per-rebalance `r_c,t` matrix, eligibility, regime.
  The K/b selection lives in the runner (it depends on holdings).
- Indicators only from `src.indicators` (`sma`, `atr`, `adx`) and pandas `rolling(n).std(ddof=0)`; the regime with
  `research/v2_signals.regime_on` semantics. No TA-Lib (the deploy VM has none; `strategies.py` needs it).
- `raw_entry` is what the placebo shifts; `regime_on`, ADX and eligibility are re-applied at landing (prereg §6.3).

## H4. `strategy_pack_run/intraday_runner.py` (new): the simulator

One function, `simulate(family, params, universe, base_frames, decision_frames, daily, costs_spec, exit_model,
fill_model, signals_override=None)`, walking the **base** bar axis (1h, or 15m for 15m sets) for all coins in lockstep:

1. **At each base open:** execute queued fills and exits (fill price = open +/- slippage; gap through a stop fills at
   the open). Missing base bar for a coin: its queue waits for that coin's next available open.
2. **Exit checks at each base close** (gating "close-trigger" model): stop, trailing stop, target, time stop, regime
   exit, rank exit, each queued for the next base open. **Touch model** (sensitivity): stop/target touched by the base
   bar's low/high; same-bar tie = stop first; 4h positions walked over their 1h bars in time order.
3. **Trailing update** (family 1) at each **decision** close: `stop = max(stop, HC - m * ATR_D_now)`.
4. **Decisions at each decision close:** exits first (queued; do not free room), then candidates in `sorted(symbol)`
   order (families 1, 2) or rank order (family 3), each through `src.risk_engine.RiskEngine.check_new_trade`
   constructed from `config/config.yaml`'s `risk:` block (all keys, including the optional ones
   `backtest_live_bot.simulate_portfolio` forwards), with the inputs of prereg §1.4: decision close as `entry_price`
   and `now`, daily ATR% as `atr_pct`, `risk_reward` per family, **P6 same-bar accounting** (gross committed quote added
   to `open_positions_value`, subtracted from `cash_available`, reset per decision bar, exits not credited),
   `record_trade(symbol, now)` on approval. Spend = `max_quote_amount`; never resized.
5. **Maker model** (sensitivity, `fill_model="maker"`): limit at decision close, filled if a base bar within one
   decision bar has `low <= limit*(1-0.0005)`, at the limit, fee 0.22%, slippage 0; otherwise missed and the reserved
   quote released. Family 2 target as resting limit (`high >= target*(1+0.0005)`), maker fee. All other exits taker.
6. **End of window:** open positions close at the last base close of the window, at taker cost, reason `end`.
7. Equity/exposure series on base closes, plus a daily (UTC close) equity series for Sharpe.

## H5. Costs

- `costs_spec` = a flat `engine.Costs` (gating `kraken_T3_taker`; `+25%` = `Costs(0.00475, 0.000625)`; the other
  presets for the sensitivity table) **or** per-bar tiered arrays built by
  `tiered_slippage.tiered_costs_series_by_symbol` from **daily** quote volume: 30-day trailing median of
  `sum(close*volume)` per complete UTC day from native 1h, divided by 3, shifted to day t-1, then mapped onto the base
  bar axis (every base bar of day D uses day D-1's value). Never the 30-*bar* median of hourly volume.
- Fees on every fill, both sides; slippage as a price adjustment; R and R_gross both recorded.

## H6. `strategy_pack_run/intraday_stats.py` (new)

- `purged_split(trades, cut)`: IS-score set (entry and exit < cut), OOS set (entry >= cut), straddlers flagged.
- `champion(results_by_set)`: highest purged IS avg R among sets with >= 150 purged IS trades; ties: more IS trades,
  lower set number.
- `block_bootstrap(trades, start, block_days, n=50_000, seed=1)` returning the percentile array needed (0.0347, 0.0694,
  2.5, 97.5 and the 7-day-block variant), the design effect and `n_eff`. `block_days = max(1, ceil(median hold days))`.
- `folds(trades)`: F1-F5 of prereg §6.1 with the scored/losing rules.
- `daily_sharpe(equity_daily)` = mean/std of daily returns x sqrt(365); `buy_hold_daily(universe, masks)` (equal
  weight from each coin's first eligible day, masked returns set to 0 for 15m runs).
- `placebo_days(seed=0, n=200, lo=184, hi=1660)`; `shift_raw(raw_by_coin, d, bars_per_day)` (circular, same shift for
  all coins, whole days); `deflated_sharpe(sr, n_trials=36, ...)` (information only).

## H7. Regime plumbing

`btc_regime(daily_btc)` -> Series indexed by **daily close time** (00:00 UTC of the next day), `True` iff close >
SMA(200) of daily closes, `False` during warm-up; `align(reg, decision_index)` = `reindex(method="ffill")` so a decision
closing at exactly 00:00 sees the day that closed at 00:00 (the completed day), never the forming one. Same for
`ATR_D`, `ADX_D`, daily quote volume. One helper, used by all three families and by the placebo's re-gating.

## H8. Per-trade log columns (one CSV per run, gitignored, SHA-256 in the results JSON)

`family, set_id, universe, cost_view, exit_model, fill_model, symbol, decision_tf, base_tf, signal_time,
entry_time, entry_px_raw, entry_px, qty, approved_quote, planned_risk_usd, initial_stop, target, time_stop_at,
exit_time, exit_px_raw, exit_px, exit_reason (stop|stop_gap|trail|target|time|regime|rank|end),
final_stop, bars_held_base, hours_held, fee_entry, fee_exit, slippage_usd, gross_pnl, net_pnl, R, R_gross,
expo_at_entry, equity_at_signal, regime_on, adx_d, atr_d, atr_tf, z_or_rank_or_breakout_level, rr_at_signal,
period (IS|OOS|straddle), fold, entry_day, entry_block, maker_filled, placebo_draw (empty for the real run)`.
Plus a rejected-signal log: `family, set_id, universe, symbol, signal_time, reason (risk-engine text up to the first
";"), mask_blocked, ineligible`.

## H9. `strategy_pack_run/run_intraday_prereg.py` (new): the one entry point

Runs, in this order and nothing else: snapshot check -> 72 base runs (3 families x 12 sets x {B, A}) at
`kraken_T3_taker` -> champion per family -> champion cost views (+25%, tiered, presets, maker, touch) on B and A ->
placebo (200 draws x 12 sets x 3 families on B, multiprocess, deterministic per draw) -> bootstrap -> gates/verdicts ->
`strategy_pack_run/intraday_p2_results.json` and the three `docs/intraday_p2_family{1,2,3}.md` reports. Prints
`Harness version: intraday-prereg-v1 + p6-same-bar-exposure-v1`, the code commit and the data hashes. No CLI flag can
change a parameter, gate, window, seed or universe (they are constants in the module, copied from the prereg);
flags exist only for `--workers` and output paths.

## Acceptance tests (all must pass on the committed hash before the run; new test files under `strategy_pack_run/`)

| id | test | pass condition |
|---|---|---|
| **AT1** | **Existing baselines byte-identical.** On the pre-change commit and the post-change commit, run: `run_live_bot_report.py --data-dir data` and `--data-dir data_long`, each with and without `--count-same-bar-approvals`; `run_report.py --data-dir data --timeframe 1d`; and the 1h baseline `run_live_bot_report.py --data-dir data_15m --timeframe 1h` on P2.0's parquet files | every report file `cmp`-identical between the two commits. If P2.0's `data_15m/` parquet files are not available on the machine, the 1h item is reported as "not reproducible here" and replaced by the same before/after comparison on a synthetic 1h fixture; that substitution is stated in the PR |
| AT2 | existing suites | root `python -m pytest -q`, `cd strategy_pack_run && python -m pytest -q`, `PYTHONPATH=. python scripts/validate_invariants.py`, `python research/v2_signals.py`: all pass, no existing test modified |
| AT3 | `resample_ohlcv` | `source_minutes=15` default output identical to before on the existing test fixtures; with `source_minutes=60`, a 4h bin needs exactly 4 hourly rows, bins open at 00/04/.../20 UTC, incomplete bins dropped, truncation-invariant at every bar |
| AT4 | snapshot | `verify_snapshot` passes on the 32 files; flipping one byte inside the prefix fails; appending rows after the prefix passes |
| AT5 | daily from 1h | complete-day rule; on 2021-10-01..2026-09-30 closes match `data_long` on >= 99.5% of complete days per coin in B |
| AT6 | mixed-source guard | a 1h run fed one 15m-derived frame raises; a 15m run with `1h->1d` daily inputs does not |
| AT7 | masks and eligibility | no 15m entry or open position of a non-BTC/ETH coin inside a mask; entries blocked when `[t-(n+100), t+H]` touches a mask; BTC/ETH unaffected; a coin with < 60 daily bars never enters; XRP has no bar before 2023-07-13 20:00 in any run |
| AT8 | no look-ahead | for every family, every output column at bar i is identical whether the frame is truncated right after bar i or not, checked at **every** bar of a 600-bar fixture per timeframe (the `test_universe_eligibility` pattern); daily inputs use only days closed at or before the decision close |
| AT9 | execution timing | no fill ever happens at the signal bar's close or open; entries fill at the next base open; a 4h decision at 08:00 fills at the 08:00 1h open |
| AT10 | exit models | close-trigger: a base close below the stop exits at the next open, a gap below fills at the open; touch model: a bar touching both stop and target books the stop; 4h position exits ordered by the 1h sequence; trailing stop never moves down; time stop after exactly n decision bars |
| AT11 | risk-engine parity | a reference loop calling `RiskEngine.check_new_trade` directly with `Portfolio.apply_buy`-style immediate commitment (the `test_same_bar_exposure.py` reference) gives identical `(symbol, allowed, max_quote_amount)` per decision bar; committed notional never exceeds 40%; the 5-per-120-min cap, the 60-min cooldown and the daily-loss halt each fire on a fixture built to trigger them; `risk_reward=None` skips only the R:R check |
| AT12 | P/L reconciliation | sum of net P/L = final equity - start equity (1e-9 relative); every trade's R recomputes from its logged fields; fees on both sides at every cost view; the +25% view charges exactly 1.25x |
| AT13 | tiered costs | the tier of base bar t on day D uses day D-1's 30-day median daily volume / 3; hourly volume is never fed in directly |
| AT14 | maker model | trade-through rule, timeout of one decision bar, missed order releases its quote, fill at the limit with 0.22% and no slippage, exits taker except family 2's target |
| AT15 | placebo | shifts are whole days, identical for all coins, circular; regime/ADX/masks/eligibility/costs re-applied at landing; the draw-champion is chosen by the same purged-IS rule; same seed -> identical results with 1 and with N workers |
| AT16 | bootstrap and stats | block construction from the window start; deterministic with seed 1; on an iid fixture the design effect is about 1; the purge drops straddling trades from both the IS score and OOS; fold F1 includes the 2021 stub |
| AT17 | sanity (playbook §2 check 1) | synthetic clean uptrend: family 1 goes long and ends net positive at zero cost; clean downtrend or regime off: no family-1 or family-3 position; synthetic range with mean reversion: family 2 is gross-positive at zero cost |
| AT18 | independent second implementation (playbook Prompt F) | for one set per family, a separate minimal pandas loop written from the prereg text alone reproduces the runner's trade list (symbol, entry/exit time, entry/exit price, reason) exactly on BTC+ETH+LTC data before 2021-03-01 (the only real data allowed before the run) |
| AT19 | mutation check | each mutation is caught by at least one test: signal reads bar t+1; regime reads the forming day; entry fills at the signal close; P6 commit not counted; mask rule off; placebo shifts the post-regime entries; placebo shift not shared across coins; bootstrap resamples trades instead of blocks; purge disabled; tiered cost uses day D instead of D-1 |
| AT20 | constants | a test asserts the module constants (grids, windows, cut 2025-04-14, folds, seeds 0/1, 200 draws, 50,000 resamples, percentiles 0.0347 / 198-of-200, thresholds 300/100/60/150/30) equal the values in `docs/intraday_prereg.md` |

## Notes for the builder and reviewer

- **Performance:** the base-bar loop over 14 coins x ~44,000 1h bars (or ~177,000 15m bars) is Python-heavy; the
  placebo is 7,200 such runs. Precompute signals once per set (vectorised), keep the loop to state updates, and
  parallelise over placebo draws. Estimate the wall time on one set before launching the full placebo (prereg Q6).
- **What the harness must not do:** fill missing bars, use 15m data in a 1h run, read past 2026-10-20 00:00 UTC, read
  BNB, stitch XRP's two segments, use Coinbase volume for anything but the /3 tier proxy, or accept a parameter from
  the command line.
- **Labels:** results carry `harness_version: "intraday-prereg-v1 + p6-same-bar-exposure-v1"` and are never compared
  with results produced under another label.
