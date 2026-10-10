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

---

_Everything below was added after the pre-registration commit (`fb66858`)._

## 1. What was built

| File | Change |
|---|---|
| `strategy_pack_run/backtest_live_bot.py` | New `count_same_bar_approvals=False` on `simulate_portfolio` and `placebo_run` (forwarded to every draw). Implements §0.3 exactly: a per-bar running total of quote committed by FILLs queued earlier in the bar's step-3 pass, added to `open_positions_value` and subtracted from `cash_available`. Module docstring note 5 corrected (its "mirrors src/bot.py" claim was wrong); the `use_rank_order` docstring now says the order-independence of the exposure term only holds with the option off. |
| `strategy_pack_run/run_live_bot_report.py` | `--count-same-bar-approvals` (default off). When on, forwards the kwarg to every `simulate_portfolio`/`placebo_run` call and prints a `Harness version: p6-same-bar-exposure-v1` line. Without the flag the report is byte-identical to before. |
| `strategy_pack_run/run_universe_gate.py` | Same flag for the P1e runner: forwarded to the canonical, placebo, order-seed, superset-check and legacy runs; `params.harness_version = "p1e-universe-v1 + p6-same-bar-exposure-v1"` in the JSON. |
| `strategy_pack_run/test_same_bar_exposure.py` (new, 17 tests) | §0.8 tests 1-7 plus a real-data invariant on the committed `data/` set. |
| `scripts/backtest_summary.json` | Text-only: `candidates_schema.harness_version` lists the known ids. No candidate or baseline entry changed. |

Not changed: anything under `src/`, `config/`, `engine.py`, `tiered_slippage.py`, `universe_*.py`, the
committed reports, `web/`.

## 2. Verification

**Option OFF is bit-for-bit the old harness.** Before touching the code, trades, equity and exposure
were captured from the unmodified harness for 16 configurations (`data/`, `data_long/` x T3/T1 taker x
sorted/rank order x flat/tiered costs) plus a 3-draw placebo per dataset. After the change, the same
18 outputs with the option omitted and with `count_same_bar_approvals=False` were compared with
`DataFrame.equals` / `Series.equals`: **18/18 identical, both ways.** The full OFF gate runs in §3-§4
also reproduce every on-record number (154 / 0.18, 562 / 0.52, P1e static-16 134 / 0.15 and 572 /
0.52, and the P1e candidates).

**Option ON reproduces the independent `b1_check.py` numbers (§0.6):** `data/` 137 trades, avg R
0.174, CI low -0.109, return 13.1%, Sharpe 0.48; `data_long/` 552 trades, 0.493 [0.337, 0.654],
340.5%, Sharpe 1.01. Trades in common with the OFF run: 85 of 154 / 413 of 562, as in E9. The only
printed difference is E3's CI high: 0.4748 here, shown as 0.47; E9 printed +0.48. That is a rounding
difference in the review's write-up, not a different result.

**The cap now holds** (committed = open value seen by a bar's first check + every approval on that
bar, over equity):

| dataset | OFF: approval bars over 40% / all approval bars, peak | ON: bars over 40%, peak |
|---|---|---|
| `data/` | 26 / 69, peak 69.7% | 0 / 98, peak 40.0% |
| `data_long/` | 69 / 347, peak 72.7% | 0 / 421, peak 40.0% |

**Tests:** `strategy_pack_run/test_same_bar_exposure.py` has 17 tests. The parity tests use a
reference loop that mirrors `src/bot.py` ~492-563 with the real `RiskEngine` and `Portfolio`
(`equity` once per cycle, `other_positions_value` from the portfolio's positions,
`cash_available=portfolio.cash`, `record_trade`, then immediate `apply_buy`). The scenario is a
pre-existing position plus 8 same-bar candidates at mixed prices and ATR%, so risk sizing, the
volatility taper, the position cap, a partial exposure squeeze, rejections and the 5-entry cap all
take part. Result: identical `(symbol, allowed, max_quote_amount)` decisions at a zero paper fee
(rel. 1e-12), and the legacy harness fails the same comparison. At the paper broker's 0.10% fee the
harness is never looser: same symbols checked, every harness approval is also a live approval and is
no larger.

**Mutation check** (each mutation applied by hand to the fix, then restored; diff confirmed clean):

| mutation | tests failing (of 17) |
|---|---|
| M1: committed quote not added to `open_positions_value` | 9 |
| M2: committed quote not subtracted from `cash_available` | 3 |
| M3: running total not reset per bar | 4 |
| M4: a PENDING plan's monitor FILL not counted | 1 (`test_pending_plan_fill_trigger_counts_like_a_fresh_fill`) |
| M5: a fresh plan's immediate FILL not counted | 10 |
| M6: `placebo_run` does not forward the flag | 3 |

**Suites:** root `python -m pytest -q`: 385 passed. `cd strategy_pack_run && python -m pytest -q`:
144 passed (incl. the 17 new tests and the universe real-data tests, data present).
`PYTHONPATH=. python scripts/validate_invariants.py`: 5/5. `python research/v2_signals.py`: 7 passed.

## 3. Re-gated results: E3, E4, E7 (live strategy on the static-16 datasets)

`run_live_bot_report.py`, `kraken_T3_taker` unless stated, sorted order, flat costs, 200-sim placebo
(seed 0). OFF and ON were run with identical tooling.

| id | harness | trades | win% | avg R [95% CI] | PF | return | maxDD | Sharpe (bench) | exposure | later-30% n / avg R | placebo pct ret / avgR | gates |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| E3 `data/` | OFF | 154 | 40.3% | 0.18 [-0.08, 0.46] | 1.33 | 22.5% | -24.1% | 0.68 (0.28) | 22.1% | 52 / 0.42 | 98 / 91 | 4/5 (CI-low FAIL) |
| E3 `data/` | **ON** | **137** | 40.1% | **0.17 [-0.11, 0.47]** | 1.24 | **13.1%** | -24.3% | **0.48** (0.28) | 19.7% | 46 / 0.43 | **96 / 94** | **4/5 (CI-low FAIL)** |
| E4 `data_long/` | OFF | 562 | 48.0% | 0.52 [0.36, 0.68] | 1.49 | 387.5% | -29.2% | 1.00 (0.81) | 15.8% | 246 / 0.34 | 100 / 98 | 5/5 |
| E4 `data_long/` | **ON** | **552** | 47.6% | **0.49 [0.34, 0.65]** | 1.49 | **340.5%** | -28.7% | **1.01** (0.81) | 14.4% | 238 / **0.25** | 100 / 98 | **5/5** |

E7 (as defined: the `kraken_T1_taker` row of the same runs, 0.80% fee):

| dataset | OFF | ON |
|---|---|---|
| `data/` | 154 trades, 0.09 [-0.18, 0.36], return 13.6%, Sharpe 0.46 | 136 trades, **0.10 [-0.18, 0.40]**, return **6.5%**, Sharpe **0.28** |
| `data_long/` | 561 trades, 0.44 [0.28, 0.60], return 259.3%, Sharpe 0.83 | 553 trades, **0.41 [0.25, 0.57]**, return 240.6%, Sharpe 0.85 |

E7g (informational: full 5 gates at `kraken_T1_taker`, placebo at T1):

| dataset | harness | later-30% avg R | placebo ret / avgR | gates |
|---|---|---|---|---|
| `data/` | OFF | 0.32 | 98 / 92 | 4/5 (CI-low FAIL) |
| `data/` | ON | 0.37 | 96 / 94 | 4/5 (CI-low FAIL). Gate 5 passes by a hair: Sharpe **0.2833 vs benchmark 0.2777** |
| `data_long/` | OFF | 0.25 | 100 / 98 | 5/5 (Sharpe 0.8266 vs 0.8121, also thin) |
| `data_long/` | ON | 0.16 | 100 / 98 | 5/5 (Sharpe 0.8478 vs 0.8121) |

**No E3/E4/E7 gate verdict changes.** E3 stays 4/5, failing only CI-low. E4 stays 5/5, so the design
§4 item-0 stop rule is not triggered. What moves is the money: E3's return falls from 22.5% to 13.1%
and Sharpe from 0.68 to 0.48; E4's from 387.5% to 340.5%, with Sharpe unchanged. Avg R barely moves
(-0.01 / -0.03), because R is per trade and the fix mostly changes position **sizes** and which
same-bar trades exist. E4's later-30% avg R drops from 0.34 to 0.25 (still > 0).

**Risk-engine rejection mix** (`kraken_T3_taker` canonical runs). The exposure cap now does the
rationing that the new-entry rate cap used to do:

| dataset | harness | checks | approved | "no room" | rate cap | volatility |
|---|---|---|---|---|---|---|
| `data/` | OFF | 669 | 154 | 380 | 38 | 97 |
| `data/` | ON | 709 | 137 | 462 | 1 | 109 |
| `data_long/` | OFF | 2405 | 562 | 876 | 78 | 889 |
| `data_long/` | ON | 2388 | 552 | 984 | 9 | 843 |

**New side effect: small residual approvals.** When a bar's room is nearly used up, the next
candidate is approved for the remainder, however small. `data/` ON: 6 of 137 approvals are under
$10, and 14 under $50 (OFF: 0 and 2). `data_long/` ON: 8 under $10, 16 under $50 (OFF: 1 and 3).
Live does exactly the same at `risk.min_order_quote_usd: 0`. Each such approval uses a new-entry slot
and starts a cooldown, and an exchange would probably reject it. Because R is per trade, these dust
trades count fully in avg R. Not changed here (that is a `config.yaml`/Risk Officer question,
`min_order_quote_usd`, see §6).

## 4. Re-gated results: E5 (the P1e gate run, all P1e §0 rules unchanged)

`run_universe_gate.py` run twice, OFF and with `--count-same-bar-approvals`: 200 placebo sims, seed 0,
10 order seeds, rank order, frozen tiered slippage, SHA256SUMS 966/966 OK. **The OFF rerun reproduces
every field of the committed `p1e_universe_gate_results.json`** (all 10 cases incl. placebo, order
seeds, decomposition, risk diagnostics, legacy rows, superset check). The ON output is committed as
`strategy_pack_run/report_p6_universe_gate.md` and `p6_universe_gate_results.json`
(`params.harness_version = "p1e-universe-v1 + p6-same-bar-exposure-v1"`).

### 4.1 Headline (kraken_T3_taker + tiered slippage, rank order)

| case | harness | trades | avg R [95% CI] | return | maxDD | Sharpe (bench) | later-30% n / avg R | placebo ret / avgR | gates 1-5 | all 5 |
|---|---|---|---|---|---|---|---|---|---|---|
| static-16, kraken_720_universe | OFF | 134 | 0.15 [-0.14, 0.46] | 18.6% | -24.0% | 0.58 (0.27) | 42 / 0.45 | 97 / 91 | P F P P P | FAIL (4/5) |
| static-16, kraken_720_universe | **ON** | 125 | 0.08 [-0.21, 0.39] | 7.2% | -20.9% | 0.32 (0.27) | 44 / 0.36 | 92 / **85** | P F P **F** P | FAIL (3/5) |
| $1M, kraken_720_universe | OFF | 172 | 0.41 [0.15, 0.69] | 38.9% | -21.4% | 0.99 (0.83) | 83 / 0.56 | 100 / 100 | P P P P P | **PASS** |
| $1M, kraken_720_universe | **ON** | 167 | **0.19 [-0.07, 0.46]** | 16.9% | -18.8% | **0.59 (0.83)** | 71 / 0.32 | 98 / 92 | P **F** P P **F** | **FAIL (3/5)** |
| $100k, kraken_720_universe | OFF | 264 | 0.21 [-0.04, 0.45] | 16.5% | -27.4% | 0.47 (0.27) | 121 / 0.06 | 96 / 95 | P F P P P | FAIL (4/5) |
| $100k, kraken_720_universe | **ON** | 258 | 0.12 [-0.12, 0.37] | 14.0% | -25.0% | 0.46 (0.27) | 114 / 0.29 | 95 / **88** | P F P **F** P | FAIL (3/5) |
| static-16, long_universe | OFF | 572 | 0.52 [0.37, 0.68] | 312.8% | -30.7% | 0.92 (0.82) | 262 / 0.31 | 100 / 98 | P P P P P | PASS |
| static-16, long_universe | **ON** | 550 | 0.50 [0.34, 0.66] | 333.9% | -24.3% | 1.04 (0.82) | 240 / 0.32 | 100 / 98 | P P P P P | PASS |
| $1M, long_universe | OFF | 838 | 0.40 [0.27, 0.53] | 371.3% | -33.8% | 0.90 (0.82) | 418 / 0.16 | 100 / 88.5 | P P P F P | FAIL (4/5) |
| $1M, long_universe | **ON** | 788 | 0.40 [0.26, 0.53] | 412.8% | -29.8% | 1.04 (0.82) | 369 / 0.21 | 100 / 89.5 | P P P F P | FAIL (4/5) |
| $100k, long_universe | OFF | 980 | 0.40 [0.28, 0.52] | 444.6% | -36.0% | 0.93 (0.82) | 534 / 0.25 | 100 / 94 | P P P P P | **PASS** |
| $100k, long_universe | **ON** | 965 | **0.32 [0.20, 0.44]** | 525.1% | -32.4% | 1.11 (0.82) | 497 / 0.21 | 100 / **86** | P P P **F** P | **FAIL (4/5)** |

Flat-5bp info rows (never used for a verdict), ON: $1M kraken 0.22 [-0.04, 0.49]; $100k kraken
0.18 [-0.06, 0.43]; $1M long 0.41 [0.28, 0.54]; $100k long 0.34 [0.22, 0.47].

### 4.2 Criteria (b), (c) and order robustness

| candidate / dataset | (b) OFF | (b) ON | (c) added avg R OFF -> ON | alt orders failing gates 1,2,3,5/(c): OFF -> ON |
|---|---|---|---|---|
| $1M / kraken_720_universe | no regression | **REGRESSION** (b1: fails gate 5, which the static-16 baseline passes) | 0.49 -> 0.33 (PASS) | 6/10 -> **10/10** |
| $100k / kraken_720_universe | no | no (both fail CI and placebo; avg R 0.12 vs 0.08) | 0.24 -> 0.21 (PASS) | 9/10 -> 10/10 |
| $1M / long_universe | REGRESSION (b1, b2) | REGRESSION (b1; b2 too, by Sharpe 1.0367 vs 1.0382) | 0.22 -> 0.28 (PASS) | 0/10 -> 0/10 |
| $100k / long_universe | no (narrowly) | **REGRESSION** (b1: fails placebo, which the baseline passes) | 0.24 -> 0.22 (PASS) | 1/10 -> 0/10 |

Original-16 vs added split on kraken ON: $1M original-16 0.04 [-0.34, 0.42], added 0.33 [-0.03, 0.70];
$100k original-16 -0.05, added 0.21. On long ON: $1M 0.48 / 0.28; $100k 0.43 / 0.22.

### 4.3 P1e verdict (rule §0.11 of the P1e doc, unchanged)

| candidate | OFF (on record) | ON |
|---|---|---|
| `universe-discover-1m` | FAIL | **FAIL** (now fails (a) on BOTH datasets, (b) on both, and 10/10 kraken orders) |
| `universe-discover-100k` | FAIL | **FAIL** (now fails (a) on both datasets, and (b) on long) |

Legacy rows inside the P1e run, ON: `data/` flat/sorted 137 / 0.17 (= E3 ON); static-16 same harness
126 trades, 0.08 [-0.22, 0.38], 6.5%, gates P F P F P (unchanged pattern). `data_long/` flat/sorted
552 / 0.49 (= E4 ON); static-16 same harness 549, 0.49 [0.33, 0.66], 329.0%, 5/5 (unchanged).
Superset equivalence still holds; the same-bar cash-race clip binds 0 times (OFF and ON).

## 5. Verdict changes, stated plainly

**Changed (gate PASS/FAIL flips, OFF -> ON):**
1. **P1e `$1M` on `kraken_720_universe`: 5/5 PASS -> 3/5 FAIL** (CI low 0.15 -> -0.07; Sharpe 0.99 ->
   0.59, below the 0.83 benchmark). This was the only clean 5/5 on Kraken data in the repo's history.
   It doesn't survive the fidelity fix, and 10 of 10 alternate orders now fail it as well.
2. **P1e `$100k` on `long_universe`: 5/5 PASS -> 4/5 FAIL** (placebo avg-R percentile 93.5 -> 85.5).
3. **P1e static-16 on `kraken_720_universe`: newly fails the placebo gate** (avg-R pct 91 -> 85;
   3/5). Same for P1e `$100k` on Kraken (95 -> 87.5).
4. P1e criterion (b): `$1M` kraken and `$100k` long now regress against their static-16 baselines.

**Not changed:** E3 (`data/`) 4/5, failing only CI-low. **E4 (`data_long/`) 5/5**, so the design §4
item-0 stop rule is not triggered. E7 (both datasets, T1 taker) keeps its CI sign: `data/` still
straddles 0, `data_long/` still clears it. E7g keeps 4/5 and 5/5. P1e static-16 on `long_universe`
stays 5/5. Both P1e candidates stay FAIL, for more reasons than before.

**What this means for Option A (design doc §1, §4):** the two pieces of live-strategy evidence keep
their direction (E9 was right). The daily strategy's case on the venue's own 2-year window gets
weaker in money terms: E3 return 22.5% -> 13.1%, Sharpe 0.68 -> 0.48. E3's Sharpe still clears the
equal-weight benchmark (0.28), but its placebo avg-R percentile is only 94 and its CI still straddles 0.
On P1e's tiered-cost Kraken universe dataset, the static-16 strategy now fails 2 of 5 gates. The
11-year `data_long/` evidence is essentially unchanged (avg R 0.52 -> 0.49, Sharpe 1.00 -> 1.01, 5/5).
Per design §5 the paper-trial kill thresholds and power must now be **re-simulated on the ON trade
lists** before the protocol is frozen. Not done here (outside this task).

## 6. How the Daily Backtester Routine and recorded baselines are affected

- **Nothing changes unless someone opts in.** `count_same_bar_approvals` defaults to `False` in
  `simulate_portfolio`/`placebo_run`, and both runners need an explicit `--count-same-bar-approvals`.
  Every recorded number reproduces with the default: the 18-configuration bit-for-bit check, the
  E3/E4 OFF runs, and the full P1e OFF rerun. That holds before and after merging `origin/main`.
  The default `run_live_bot_report.py` output is byte-identical (the harness-version line is only
  printed when ON). So the Routine, whose stored prompt lives outside the repo and passes no such
  flag, measures exactly what it did before, and its baselines (154 / 0.18, 562 / 0.52) stay valid
  **as baselines of the original harness**.
- **Recommended default for new gate runs: ON**, labelled `harness_version:
  "p6-same-bar-exposure-v1"`. The original harness overstates what the live bot can do whenever
  several symbols signal on one bar.
- **Switching the Routine** is the user's call (or the CEO's with the user's approval). It means
  editing the stored Routine prompt to pass `--count-same-bar-approvals`, re-measuring its baselines
  on the fixed harness (`data/` 137 / 0.17, `data_long/` 552 / 0.49 at T3 taker), and tagging new
  candidates with `harness_version: "p6-same-bar-exposure-v1"`. Old candidates keep the absent label
  and are not compared against new ones (P1e convention, now listed in `candidates_schema`). Until
  then, a Routine-promoted parameter variant is being judged on a harness that over-commits exposure.
  Also, `min_risk_reward-2p44-20261002`'s recorded comparison is OFF vs OFF and remains internally
  consistent.
- `hyperopt_param.py` doesn't forward the flag (unchanged, out of scope). If the Routine switches,
  that is a one-line follow-up there.

## 7. `origin/main` merge note

After the gate runs, `origin/main` (PR #96/#97/#98: `risk_engine.py` config/input validation,
`portfolio.py`, `monitor.py`, `broker_live.py`, `validate_invariants.py`) was merged in (`4ee0046`).
- The harness builds its `RiskEngine` fine from `config/config.yaml` (config changes there are
  comments only).
- Two new P6 tests had used `max_total_exposure_pct: 5.0` to get the cap out of the way. That is now
  rejected at startup. They were rewritten at the 1.0 maximum (where cash and room bind together)
  and assert the `cash_available` passed per check. The mutation table in §2 was re-run on the merged
  code (M1 10, M2 2, M3 4, M4 1, M5 11, M6 3 failing tests; all caught).
- After the merge, the 18-configuration OFF check is still 18/18 identical to the pre-change capture,
  and the ON `data/` / `data_long/` numbers are unchanged. The new validation only rejects
  non-finite inputs, which these runs never produce. A P1e canonical-only rerun on the merged code
  (both modes; 1 placebo sim, no order seeds) matches the full-run JSONs exactly on trades, avg R, CI,
  return, maxDD, Sharpe, later-30%, decomposition and risk diagnostics for all 10 cases. So every number above holds on the merged branch.

## 8. Notes for the reviewer

- **Semantics to scrutinise:** gross vs net-of-fee counting (§0.3 item 6). Counting a PENDING plan's
  monitor FILL (never fires on these datasets, since fresh plans always fill at once; covered only by
  a patched test). No room credit for same-bar exits (P3). `equity` held fixed within the bar.
- **Small residual approvals (§3):** the fix creates more sub-$50 approvals, and live does too at
  `min_order_quote_usd: 0`. They count fully in avg R. A Risk Officer / config question, not changed.
- **Live-side observation (not changed, `src/` out of scope):** in the parity scenario at the paper
  fee, live approved two dust plans ($0.43 and ~$0.0004) out of fee slack. Each uses a new-entry slot
  and starts a cooldown.
- **Thin margins:** E7g `data/` ON gate 5 passes at Sharpe 0.2833 vs 0.2777. P1e `$1M` long ON misses
  the placebo gate at 89.5 vs 90, and its b2 regression is decided by Sharpe 1.0367 vs 1.0382.
- **Not done:** `hyperopt_param.py` flag; re-simulating the design §5 kill thresholds on the ON trade
  lists; design test 7 (live/harness daily parity through `bot.run_cycle`), which needs P1-P5 in
  `bot.py` first.
