# P6: harness counts same-bar approvals against the exposure cap and cash (B1 fix)

- **Role:** Architect (Opus), acting as QA/Tester for the harness change. Harness, tests and docs only.
  `src/` (including `risk_engine.py` and `bot.py`), `broker_live.py` and `config/config.yaml` are
  **not touched**. Live is never loosened to match the harness. The harness is made to match live.
- **Branch:** `claude/p6-harness-same-bar-exposure`, off `origin/main` at `9fe5b47`. Local only, not pushed.
- **Design reference:** `docs/multiday_hold_design.md` §1 (E3, E4, E5, E7, E9), §3.1 (P2, P3, P6), §4
  item 0, §6 (harness row, test 9), §8 decision 12. Background: `docs/p1c_rank_signals_validation.md`
  ("every FLAT candidate sees the *identical* `open_positions_value`").

## 0. Pre-registration (committed BEFORE any harness code change or any re-gated result was seen)

Nothing in this section changes after results are seen. Section 1 onward is added afterwards. If a
bug forces a rerun, the post-results sections say so.

### 0.1 What this is

A **fidelity fix, not tuning.** No strategy parameter, risk limit, cost preset, gate threshold, placebo
setting, split rule, benchmark rule or P1e rule changes. The only thing that changes is how the
harness accounts for plans approved earlier on the same bar, so that it enforces
`risk.max_total_exposure_pct` (40%) and cash the way `src/bot.py` already does.

### 0.2 The divergence being fixed (verified in code before writing this)

- `src/bot.py` `run_cycle` (lines ~492-563): for each symbol in loop order, the risk check uses
  `open_positions_value = sum(pos.amount * current_prices[sym] for every portfolio position)` and
  `cash_available = portfolio.cash`. If the plan is approved and the price is already in the entry zone
  (always true for a fresh plan, see `backtest_live_bot` docstring note 3), it **fills immediately**
  (`broker.buy` -> `Portfolio.apply_buy`: `cash -= quote`, a position worth `quote*(1-fee)` at the
  current price is added). The same is true of a PENDING plan from an earlier cycle whose FILL
  trigger fires (`_execute_monitor_action`). So the next symbol in the same cycle sees less exposure
  room and less cash. `equity` is computed once per cycle before the loop and is not refreshed.
- `src/risk_engine.py` `check_new_trade`: `remaining_exposure_room = max(0, equity *
  max_total_exposure_pct - open_positions_value)`; `spend = min(equity*max_position_pct,
  cash_available, risk_sized_spend, remaining_exposure_room)`. Unchanged here.
- `strategy_pack_run/backtest_live_bot.py` `simulate_portfolio` step 3: `other_value =
  invested_value(prices_now)` counts **OPEN** positions only, and `cash_available=cash`. A plan approved
  earlier on the same bar is still PENDING (its FILL is queued for the next bar's open), so every later
  candidate on that bar sees the same room and cash. Same-bar approvals can therefore jointly exceed
  the 40% cap (independent review: `data/` 26 of 69 approval bars, peak 70%; `data_long/` 69 of 347,
  peak 73%).

### 0.3 Exact semantics of the fix

New keyword argument on `simulate_portfolio` and `placebo_run` (forwarded unchanged to every placebo
draw): **`count_same_bar_approvals: bool = False`**.

With `count_same_bar_approvals=True`, during step 3 of bar t the harness keeps a running total
`C_t` (reset to 0 at the start of each bar's step 3):
1. Whenever step 3 queues a **FILL** for a symbol (a fresh plan's immediate FILL check, or a PENDING
   plan's monitor FILL trigger), `C_t += plan["approved_quote_amount"]`. This mirrors live, where
   both paths call `broker.buy` immediately.
2. Every risk check later in the same bar's step 3 is called with
   `open_positions_value = invested_value(close_t) + C_t` and `cash_available = cash - C_t`.
   `portfolio_equity` stays `equity[t]` (live does not refresh `equity` inside the loop either).
3. **Not counted:** a plan approved but not immediately filled (stays PENDING; live doesn't spend on it
   either), DISCARDs, and exits. **Exits queued on bar t do not free room on bar t** (P3: the harness
   decides entries with the exiting position still counted). Exit-queue semantics are unchanged.
4. **Processing order unchanged:** `sorted(symbols)` by default, `rank_signals` order when
   `use_rank_order=True` (P1e cases). With the fix, order now also decides who gets the exposure
   room (P2), not only who wins the 5-entry cap.
5. Fills still execute at bar t+1's open with `min(approved, cash)`; position size is still fixed at
   approval. With the fix, the same-bar cash-race clip (docstring note 5) can no longer bind (the sum
   of a bar's approvals is at most that bar's cash, and exits only add cash before the next open).
6. **Known, deliberate residual vs live:** `C_t` adds the **gross** approved quote. Live's paper
   broker adds a position worth `quote*(1-fee)` (0.10%), so the harness is stricter by at most
   0.1% of each same-bar position (about 0.01% of equity per approval). Chosen because it is what
   design §3.1 P6 specifies ("quote amounts approved"), it does not depend on the broker's fee
   model, and it can never make the harness looser than live. The parity test checks exact
   equality at a zero paper fee, and "never looser" at 0.10%.

With `count_same_bar_approvals=False` (the default), the harness is **bit-for-bit identical** to
`origin/main` at `9fe5b47`: same trades, equity and exposure series for every combination of
`use_rank_order`, `costs_by_symbol`, cost preset and placebo already used by committed reports. Checked
against outputs captured from the unmodified code before the change.

### 0.4 Harness version label

`harness_version` (the optional field P1e added to `scripts/backtest_summary.json`'s
`candidates_schema`): **`p6-same-bar-exposure-v1`** for any result produced with
`count_same_bar_approvals=True`. P1e-style runs with the fix: `p1e-universe-v1 + p6-same-bar-exposure-v1`.
Absent means the original harness. Results with different `harness_version` values are never compared
against each other as baselines.

### 0.5 Default and the Daily Backtester Routine

- **Code default stays `False`.** The Routine's stored prompt lives outside the repo and calls
  `run_live_bot_report.py` / `hyperopt_param.py` without this flag, and compares candidates against
  baselines measured on the original harness. Flipping the default would silently change what the
  Routine measures and break comparability with every recorded candidate. That is not done here.
- **Recommended for every NEW gate run** (anything used as evidence for Option A, the §5 paper trial,
  or a promotion decision): `count_same_bar_approvals=True`, labelled `p6-same-bar-exposure-v1`.
- The Routine switches only when the user (or the CEO session with the user's approval) updates the
  Routine's stored prompt to pass `--count-same-bar-approvals`, re-measures its baselines on the
  fixed harness, and tags new candidates with the new `harness_version`. Old candidates stay as they
  are, under the old (absent) label.

### 0.6 Gates re-run (option ON), and the "old" column

Every run below is done twice with identical tooling, option OFF ("old") and ON ("new"). OFF must
reproduce the on-record numbers before any ON number is read.

| id | what | tool | settings | on-record OFF result that must reproduce |
|---|---|---|---|---|
| E3 | live strategy, `data/` (Kraken 1d) | `run_live_bot_report.py --data-dir data` | `kraken_T3_taker`, sorted order, flat costs, 200 placebo sims, seed 0 | 154 trades, avg R 0.18 [-0.08, 0.46], return 22.5%, Sharpe 0.68, 4/5 (CI-low FAIL) |
| E4 | live strategy, `data_long/` (Coinbase 1d) | same, `--data-dir data_long` | same | 562 trades, avg R 0.52 [0.36, 0.68], return 387.5%, Sharpe 1.00, 5/5 |
| E7 | cost sensitivity | the `kraken_T1_taker` row of the E3/E4 results tables (as E7 was defined: trades, avg R, CI) | same runs | `data/` +0.09R [-0.18, +0.36]; `data_long/` +0.44R [+0.28, +0.60] |
| E7g | (informational) full 5 gates at T1 | `run_live_bot_report.py --cost kraken_T1_taker` | 200 sims | no on-record value; old column produced here |
| E5 | P1e static-16 baselines **and** both P1e candidates ($1M, $100k), both universe datasets, flat-5bp info rows, 10 order seeds, legacy rows | `run_universe_gate.py` (P1e runner, all §0 rules of `docs/p1e_universe_gate_run.md` unchanged) | 200 placebo sims, seed 0, rank order, tiered slippage | static-16 `kraken_720_universe` 134 trades 0.15, `long_universe` 572 trades 0.52; candidates as in P1e §3.2-3.7 |

Not re-run: E1/E2 (1h; the cap is permanently saturated there and not the question), E6 (per-signal
isolated, no portfolio), E8 (htf parity; orthogonal).

**Independent cross-check (pre-registered):** the review's scratch `b1_check.py` (a different
implementation: a `RiskEngine` subclass adding same-bar approved quote) gave, for E3/E4 at
`kraken_T3_taker`: `data/` **137 trades, +0.17R [-0.11, +0.48], return 13.1%, Sharpe 0.48**;
`data_long/` **552 trades, +0.49R [+0.34, +0.65], return 340.5%, Sharpe 1.01**. The ON run is expected
to reproduce these. If it doesn't, the difference is investigated and explained before any verdict
is written.

### 0.7 Verdict rules (unchanged thresholds)

- Per dataset: the same 5 gates as `run_live_bot_report.py` (>= 100 trades; bootstrap avg-R CI low
  > 0; later-30% avg R > 0; placebo >= 90th percentile on both return and avg R; return > 0 and
  Sharpe > equal-weight buy&hold). P1e cases: P1e §0.7-§0.11 exactly as written.
- Any gate whose PASS/FAIL changes between OFF and ON is reported plainly as a verdict change.
- Design §4 item 0 stop rule: **if `data_long/` (E4) no longer passes 5/5 with the option ON, stop and
  return to the user** before anything else proceeds on Option A.
- No threshold, parameter or rule is changed after seeing results. A bug found afterwards is fixed,
  everything is rerun, and the post-results sections record it.

### 0.8 Tests to be added (before the gate run)

1. Hand-built frame, several symbols approved on one bar: with the option ON, cumulative approved
   quote on that bar never exceeds `equity * max_total_exposure_pct - open value`, later symbols get
   less room (live-style), and the symbol that loses out is the alphabetically later one.
2. Cash consumption: with the cash cap binding, later same-bar candidates see `cash - C_t`.
3. Order: alphabetical order decides who gets the room (and rank order when `use_rank_order=True`).
4. Non-regression: option OFF (implicit and explicit) is identical to the pre-change behaviour on the
   same frame, and the old over-commitment is reproduced there.
5. Parity: a reference loop built from `src.risk_engine.RiskEngine` + `src.portfolio.Portfolio`
   (`apply_buy` immediately on approval, `bot.py` style) on the same bar gives identical
   `(symbol, allowed, max_quote_amount)` decisions at zero paper fee, and the harness is never looser
   at 0.10%.
6. `placebo_run` forwards the flag to every draw.
7. Mutation check: at least 2 deliberate mutations of the fix (e.g. not adding `C_t` to
   `open_positions_value`; not subtracting it from cash; not resetting it per bar) must each be caught.
