# `risk.min_order_quote_usd` floor study (Risk Officer, Opus)

- **Branch:** `claude/min-order-floor-study`, off `origin/main` at `a0e0ef5`. Local only, not pushed.
- **Scope:** evidence and a **proposed** config value only. `config/config.yaml`, `src/`,
  `src/broker_live.py` and the live-trading gate are not changed on this branch. Applying any floor
  is a risk-parameter change that needs the user's approval.
- **Background:** `docs/p6_harness_exposure_fix.md` §3 (small residual approvals, float phantoms,
  live fee-slack dust), PR #86 (the floor, shipped at 0), PR #90 (harness forwards the key),
  `docs/bot_bar_parity.md` §4.2 on the unmerged `claude/bot-bar-parity` branch (live loop vs P6
  harness: 82% entry mismatch at floor 0, 4.5% at $1, 0% at $10, data/ only).

## 0. Pre-registration (committed before any run of this study)

Written and committed before running anything for this study. Sections 1+ are added afterwards and
do not edit this section.

**Already known before writing this (disclosed, not hidden).** Floor 0 on the P6 harness at
`kraken_T3_taker`: `data/` 137 trades, avg R 0.17 [-0.11, 0.47], 4/5 gates (fails CI-low);
`data_long/` 552 trades, avg R 0.49, 5/5 (P6 doc §5). The parity branch measured floors 0, $1 and $10
on `data/` only (82% / 4.5% / 0% entry mismatch). No floor > 0 has been gate-run before.

### 0.1 Candidates (fixed; no others will be tried or reported as candidates)

| floor (USD/USDT quote) | % of `starting_cash_usd` (10,000) | % of a full-size position (10% cap = $1,000) |
|---|---|---|
| 0 (control, current config) | 0 | 0 |
| 5 | 0.05% | 0.5% |
| 10 | 0.10% | 1.0% |
| 25 | 0.25% | 2.5% |

Sizing context: `max_position_pct` 0.10 ($1,000 at start), `max_loss_per_trade_pct` 0.01 ($100 risk,
tapered to as little as $50 by `volatility_scale_floor` 0.5), `max_total_exposure_pct` 0.40 ($4,000).
With the daily median stop of ~10% (multiday design doc) the risk-sized and flat caps both land near
$1,000. The report will also give the measured median approved spend per run.

Nothing else is changed: every other config value, the harness code and the data are as on
`origin/main` `a0e0ef5`. The floor is injected only into the in-memory config that the harness reads
(no file edit), via a study driver that calls the unmodified `run_live_bot_report.main()`.

### 0.2 Harness, data, costs

- **Primary (gates):** `strategy_pack_run/run_live_bot_report.py --count-same-bar-approvals`
  (P6, harness_version `p6-same-bar-exposure-v1`, mirrors live same-bar room/cash), `--cost
  kraken_T3_taker` (fee 0.38%, slippage 5 bp), `--equity 10000`, `--sims 200` (time-shift placebo,
  seed 0 as hard-coded; same placebo offsets for every floor, so the comparison is paired), sorted
  order. Datasets `data/` (Kraken daily, 2024-10-13..2026-10-01, 719 bars) and `data_long/`
  (Coinbase daily, ~11 years).
- **Fee note.** The live paper broker charges 0.10% (`paper_today_0.10%`). The gates use the T3
  taker fee (0.38%) as every recent gate run did. The report's own per-cost table (all four presets)
  is reported too; only T3 taker decides.
- **Secondary (dust divergence, live vs harness):** the parity replay from
  `claude/bot-bar-parity` at `472bdc0` (`tests/test_daily_live_parity.py`'s `run_harness` /
  `run_bot`, used unmodified from an extracted copy outside both worktrees): `bot.run_cycle` with
  `loop.bar_parity` on at 1d vs `simulate_portfolio(count_same_bar_approvals=True)`, real opens,
  **0.10% paper fee on both sides**, 200-bar live history, same floor on both sides. Run on `data/`
  (all 719 bars) for each candidate. Also attempted on `data_long/` with the same functions and a
  loader pointed at that directory; if it cannot run in reasonable time it is reported as not done
  and criterion (ii-b) on `data_long/` falls back to the analytic bound in 0.4. This replay is from an
  unmerged branch: it is evidence about the mechanism, not a merged test.

### 0.3 Metrics, per floor and dataset

From the primary harness: trades, win%, avg R with 95% bootstrap CI, total return, Sharpe, max
drawdown, later-30% n and avg R, placebo percentiles (return, avg R), the 5 gates, the equal-weight
benchmark Sharpe. Plus, from an instrumented replay of the same T3 run (the `RiskEngine` subclassed
only to record each `check_new_trade` call and its decision; verified to give the same trade count):
- approvals, and approvals with spend below $0.01 (phantoms), $5, $10, $25, $50; median spend;
- rejections by `min_order_size`;
- **plans blocked by dust** (on the floor-0 run, for each candidate F): (a) signal bars of a symbol
  that fall strictly inside the life of that symbol's own sub-F plan (approval bar < signal bar <
  exit-execution bar), i.e. signals never evaluated because a dust plan held the symbol's single plan
  slot; (b) rate-cap rejections on a bar on which a sub-F approval was made earlier in the same bar
  (a new-entry slot used by dust). These are upper bounds: a blocked signal might have been rejected
  anyway.
- paired trade diff floor F vs floor 0: trades (symbol, entry time) only at F, only at 0.

From the secondary replay: entry and exit mismatch (design §5 metric: symmetric difference over the
harness count), live-only approvals and their sizes, and how many live-only approvals sit on a
(bar, symbol) the harness rejected for "no room" / "below the minimum order size" (fee-slack dust).

### 0.4 Decision rule (fixed now)

A floor F in {5, 10, 25} is **recommendable** only if, on **both** datasets, compared with floor 0 on
the same harness:

- **(i) No 5-gate regression:** every gate that passes at floor 0 also passes at F (count can stay
  equal or rise; any gate flip PASS -> FAIL is a fail of (i)).
- **(ii) Dust eliminated:**
  (ii-a) primary harness: zero approvals with spend < F (mechanical check that the floor is wired,
  includes the ~1e-12 USD phantoms);
  (ii-b) parity replay at the 0.10% fee: zero live-only approvals on a (bar, symbol) the harness
  rejected for "no room" / "below the minimum order size", and entry mismatch <= the design's 10%
  budget. If the `data_long/` replay cannot run, (ii-b) there is judged against the analytic bound:
  live fee slack on one bar is at most `fee * max_total_exposure_pct * equity` = 0.04% of equity, so F
  must exceed 0.0004 x the run's peak equity; if it does not, (ii-b) fails there.
- **(iii) No material loss beyond noise:** avg R at F lies inside floor 0's 95% bootstrap CI, and
  trades at F >= 90% of trades at floor 0.

Among recommendable floors, the **smallest** is recommended (the floor exists to remove dust; a
higher one only rejects more real small trades and bites harder on smaller accounts). If none passes
cleanly, the verdict says so and no change is proposed. Kraken per-pair exchange minimums are not a
gate (not verifiable offline here); they are reported as a caveat.

The floor choice is a selection among four fixed values on the full sample; the later-30% split
(gate 3) and the paired placebo are the out-of-sample / chance controls. Nothing is tuned after
seeing results; the candidate set and rule above are final.

## 1. Exact commands

Setup: `git -C /home/user/hybrid-crypto-trader worktree add /home/user/hct-min-order-study -b
claude/min-order-floor-study origin/main` (`a0e0ef5`). No file under `config/` or `src/` was edited.

**Primary gate runs (8):** from `strategy_pack_run/`, for `DS` in `data data_long` and `F` in `0 5 10 25`:

    python run_min_order_floor_study.py --data-dir $DS --floor $F --out <report.md> --json <metrics.json>

The driver sets `risk.min_order_quote_usd = F` in an in-memory copy of the config, then calls the
unmodified `run_live_bot_report.main()` with
`--data-dir $DS --count-same-bar-approvals --cost kraken_T3_taker --sims 200 --equity 10000.0`.
It then replays the same T3 simulation once with a `RiskEngine` subclass that only records each
check. Outputs: `strategy_pack_run/min_order_floor_study/report_<DS>_floor<F>.md` (the report text
does not print the floor; it is in the file name) and `results.json`. Each run takes about 10 minutes.

**Secondary parity replays (8):** `git -C /home/user/hct-bar-parity archive 472bdc0 | tar -x -C <src>`, then

    python strategy_pack_run/min_order_floor_study/parity_floor.py <src> data $F <out.json> 719
    python strategy_pack_run/min_order_floor_study/parity_floor.py <src> data_long $F <out.json>

These use `tests/test_daily_live_parity.py`'s `run_harness`/`run_bot` from that branch as they are.
Runtime is about 8 minutes on `data/` and about 17 minutes on `data_long/`.

**Reproduction checks (all exact):**
- Floor 0 reproduces the P6 doc's E3/E4 ON numbers: `data/` 137 trades, 0.17 [-0.11, 0.47], 709
  checks, 6 approvals under $10 and 14 under $50. `data_long/` 552 trades, 0.49, 3 float phantoms.
- The floor-0 parity replay on `data/` reproduces bar-parity B0: 135/174 entries, 99 in common, 82%
  mismatch, first divergence 2024-11-08 ETH at $0.999 live-only.
- The floor-10 replay reproduces B2: 132/132, 0%.

**Post-hoc / information-only scripts (not pre-registered, not used for the verdict):**
`min_order_floor_study/slack_diag.py` (size of fee-slack dust), `small_acct.py` (smaller starting
equity), `off_info.py` (original harness with P6 OFF).

## 2. Results

### 2.1 Primary: 5 gates (kraken_T3_taker, P6 ON, 200 placebo sims, same placebo offsets for every floor)

| dataset | floor | trades | win% | avg R [95% CI] | return | Sharpe (EW bench) | maxDD | later-30% n / avg R | placebo pct ret / avg R | gates 1-5 |
|---|---|---|---|---|---|---|---|---|---|---|
| `data/` | 0 | 137 | 40.1% | 0.17 [-0.11, 0.47] | 13.1% | 0.48 (0.28) | -24.3% | 46 / 0.43 | 96 / 94 | P F P P P |
| `data/` | 5 | 132 | 40.9% | 0.22 [-0.09, 0.53] | 14.3% | 0.51 | -23.5% | 43 / 0.50 | 96 / 96 | P F P P P |
| `data/` | 10 | 132 | 40.9% | 0.22 [-0.09, 0.53] | 14.3% | 0.51 | -23.5% | 43 / 0.50 | 96 / 96 | P F P P P |
| `data/` | 25 | 132 | 40.9% | 0.21 [-0.09, 0.53] | 14.1% | 0.51 | -23.6% | 43 / 0.50 | 98 / 97 | P F P P P |
| `data_long/` | 0 | 552 | 47.6% | 0.49 [0.34, 0.65] | 340.5% | 1.01 (0.81) | -28.7% | 238 / 0.25 | 100 / 98 | P P P P P |
| `data_long/` | 5 | 548 | 47.6% | 0.49 [0.33, 0.65] | 342.1% | 1.01 | -28.7% | 235 / 0.25 | 100 / 98 | P P P P P |
| `data_long/` | 10 | 546 | 47.8% | 0.50 [0.34, 0.65] | 342.9% | 1.01 | -28.7% | 233 / 0.26 | 100 / 98 | P P P P P |
| `data_long/` | 25 | 544 | 47.8% | 0.50 [0.34, 0.65] | 342.4% | 1.01 | -28.7% | 232 / 0.26 | 100 / 98 | P P P P P |

Paper-fee (0.10%) rows from the same reports, information only. `data/`: floor 0 gives 135 trades,
0.27, 20.1%; floor 10 gives 132, 0.29, 20.1%. `data_long/`: floor 0 gives 546, 0.58, 435.3%; floor 10
gives 545, 0.57, 444.0%.

### 2.2 Approvals and dust (recording replay of the same T3 run)

| dataset | floor | checks | approvals | < $0.01 | < $5 | < $10 | < $25 | < $50 | min spend | p10 | median | `min_order_size` rejections |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `data/` | 0 | 709 | 137 | 0 | 5 | 6 | 8 | 14 | $1.23 | $49 | $574 | 0 |
| `data/` | 5 | 721 | 132 | 0 | 0 | 1 | 1 | 7 | $9.14 | $119 | $619 | 16 |
| `data/` | 10 | 722 | 132 | 0 | 0 | 0 | 1 | 7 | $14.71 | $119 | $619 | 21 |
| `data/` | 25 | 723 | 132 | 0 | 0 | 0 | 0 | 6 | $25.95 | $119 | $619 | 27 |
| `data_long/` | 0 | 2388 | 552 | **3** (~1.8e-12) | 5 | 8 | 9 | 16 | 1.8e-12 | $365 | $1,491 | 0 |
| `data_long/` | 5 | 2407 | 548 | 0 | 0 | 2 | 4 | 11 | $5.79 | $378 | $1,515 | 13 |
| `data_long/` | 10 | 2410 | 546 | 0 | 0 | 0 | 2 | 9 | $10.02 | $377 | $1,512 | 10 |
| `data_long/` | 25 | 2416 | 544 | 0 | 0 | 0 | 0 | 7 | $30.33 | $383 | $1,514 | 18 |

There are more `min_order_size` rejections than removed approvals for a simple reason: a rejected
check records no plan, cooldown or new-entry slot, so the symbol is checked again on later bars while
room is still scarce.

**Plans blocked by dust (floor-0 runs, §0.3 definitions; these are upper bounds):**

| dataset | sub-F | sub-F approvals (all filled) | their avg R | their net P&L | signal bars inside a sub-F plan's life | rate-cap rejections after a same-bar sub-F approval |
|---|---|---|---|---|---|---|
| `data/` | $5 | 5 | -0.18 | -$0.53 | 17 | 0 |
| `data/` | $10 | 6 | -0.32 | -$1.86 | 23 | 0 |
| `data/` | $25 | 8 | -0.54 | -$7.90 | 31 | 0 |
| `data_long/` | $5 | 5 | -0.31 | -$0.38 | 18 | 4 |
| `data_long/` | $10 | 8 | -0.65 | -$3.79 | 21 | 4 |
| `data_long/` | $25 | 9 | -0.31 | +$4.63 | 26 | 4 |

**Paired trade diff vs floor 0** (symbol + entry date; trades only at F / only at 0, and the sum of
R of each set):
- `data/`: F5 +3 / -8 (+0.06 / -4.56); F10 +4 / -9 (-0.97 / -5.58); F25 +4 / -9 (-1.15 / -5.58).
- `data_long/`: F5 +6 / -10 (-4.40 / -1.06); F10 +9 / -15 (-8.59 / -7.49); F25 +9 / -17 (-8.59 / -6.45).

The trades the floor "unblocks" are not better on average. The `data/` avg-R rise from 0.17 to 0.22
mostly comes from dropping 6 dust trades. Each of those risked cents, but R is per trade, so each
counted at full weight. In money terms the change is +1.2 percentage points of return. **The floor is
a fidelity and hygiene change, not an edge improvement.**

### 2.3 Secondary: live loop vs harness (bar-parity branch `472bdc0`, 0.10% fee on both sides)

| dataset | floor | risk checks H / L | entries H / L (common) | entry mismatch | exits H / L | exit mismatch | live-only entries (on a harness "no room"/"below min" check) | harness-only |
|---|---|---|---|---|---|---|---|---|
| `data/` (719 bars) | 0 | 715 / 533 | 135 / 174 (99) | **82.2%** | 125 / 160 | 64.8% | 75 (65) | 36 |
| `data/` | 5 | 721 / 721 | 132 / 132 | 0% | 123 / 123 | 0% | 0 | 0 |
| `data/` | 10 | 722 / 722 | 132 / 132 | 0% | 123 / 123 | 0% | 0 | 0 |
| `data/` | 25 | 723 / 723 | 132 / 132 | 0% | 123 / 123 | 0% | 0 | 0 |
| `data_long/` (4091 bars) | 0 | 2397 / 1997 | 546 / 646 (443) | **56.0%** | 534 / 632 | 42.3% | 203 (149) | 103 |
| `data_long/` | 5 | 2410 / 2406 | 546 / 547 (544) | 0.9% | 534 / 535 | 0.9% | **3 (3)** | 2 |
| `data_long/` | 10 | 2411 / 2411 | 545 / 545 | 0% | 533 / 533 | 0% | 0 | 0 |
| `data_long/` | 25 | 2424 / 2424 | 543 / 542 (542) | 0.2% | 531 / 530 | 0.2% | 0 | 1 |

- **`data_long/` at $5:** the first divergence is 2025-07-18, AVAX approved live-only at **$5.33**. That
  is fee-slack dust just above the floor; the harness had no room. It then cascades into live-only DOGE
  ($175) and XRP ($3,043).
- **`data_long/` at $25:** the one harness-only entry is AVAX on 2023-07-16. The harness sized it at
  $29.74; live sized it at $21.91 and rejected it as below $25. The two sides size slightly differently
  (live fills at the last close, the harness at the next open), and the two sizes straddle the floor.
  This is not dust. A higher floor makes such straddles more likely.
- Live-side `data_long/` checks include the XRP gap (Coinbase delisting, 2021-01 to 2023) and
  symbols that are not yet listed. These cause no mismatches at $10.

### 2.4 Post-hoc diagnostic (not pre-registered): how big fee-slack dust can get

Live values a same-cycle fill at `quote*(1-f)`. When the last fill on a bar is clipped by the exposure
room, the clip absorbs the slack of the earlier fills. Only `f x (the clipped fill)` reappears as room,
so live-only dust is at most `f x max_position_pct x equity`: **0.01% of equity at the 0.10% paper
fee**. `slack_diag.py` measures it on the harness:
- `data/`, floor 0: 34 bars, median $0.39, max $1.05 (0.0099% of equity). This matches B1's $1.008
  live-only approval at a $1 floor.
- `data_long/`, floor 0: 81 bars, median $1.24, p90 $3.85, max $5.26 on 2025-07-18 (at the floor-10
  path, $5.34). This matches the $5.33 live-only approval in the parity replay at $5.

**Correction to §0.4's fallback bound.** The `fee x max_total_exposure_pct x equity` (0.04%) bound
was looser than needed. It was not used, because the `data_long/` replay ran. The P6 reviewer's
"<= ~$15 on data_long" came from a different replay and was not re-run here. The largest dust measured
here is $5.33.

### 2.5 Information only (not pre-registered)

**Smaller starting equity** (`small_acct.py`; T3, P6 ON, no placebo; trades and avg R):

| start equity | `data/` floor 0 / 5 / 10 / 25 | `data_long/` floor 0 / 5 / 10 / 25 |
|---|---|---|
| $1,000 | 137 (0.17) / 124 (0.15) / 122 (0.12) / 112 (0.11) | 561 (0.49) / 535 (0.51) / 536 (0.47) / 510 (0.50) |
| $2,500 | 137 / 132 (0.21) / 128 (0.18) / 122 (0.12) | 552 / 545 / 542 / 536 (0.47) |
| $10,000 | 137 / 132 / 132 / 132 | 552 / 548 / 546 / 544 |

At $1,000, a $10 floor drops 11% of `data/` trades, which would fail the §0.4 10% tolerance, and $25
drops 18%. The floor is an absolute USD amount and does not scale with the account.

**Original harness (P6 OFF, what the Daily Backtester Routine runs), T3:** `data/` floor 0 gives 154 /
0.18 / 22.5%, and floor 10 gives the same. `data_long/` floor 0 gives 562 / 0.52 / 387.5%; floor 10
gives 565 / 0.53 / 406.2%.

## 3. Verdict under the pre-registered rule (§0.4, unchanged)

| floor | `data/`: (i) gates / (ii-a) / (ii-b) / (iii) | `data_long/`: (i) / (ii-a) / (ii-b) / (iii) | recommendable |
|---|---|---|---|
| $5 | P / P / P / P | P / P / **F** (3 live-only on harness no-room checks; first is $5.33 fee-slack dust) / P | **no** |
| $10 | P / P / P / P | P / P / P / P | **yes** |
| $25 | P / P / P / P | P / P / P (0 live-only; 0.2% mismatch, within the 10% budget) / P | yes |

Details:
- **(i)** Gate patterns are unchanged at every floor: `data/` is P F P P P (CI-low still fails) and
  `data_long/` is P P P P P.
- **(iii) avg R:** `data/` 0.22 lies inside [-0.11, 0.47]; `data_long/` 0.50 lies inside [0.34, 0.65].
- **(iii) trades:** `data/` 132 >= 123.3; `data_long/` 546 >= 496.8. Both clear the 90% threshold.

**Recommended: $10**, the smallest recommendable floor, per the rule.

**Later note from the coordinator (does not change the rule or the verdict).** `claude/bot-bar-parity`
has since added `0836f5f`: in parity mode, same-bar fills count at their gross approved quote. That
makes live match the harness and removes fee-slack dust *in parity mode*. Once that merges and
`loop.bar_parity` is on, the floor's remaining job is:
(a) the float-rounding phantoms (3 on `data_long/`, ~1e-12 USD, at full weight in avg R);
(b) dust on the default path, because the paper bot runs with `bar_parity` off today, and that path
still has the fee slack;
(c) a portfolio-level margin above exchange minimums.

Any floor > 0 handles (a). In parity mode, $5's single failure on `data_long/` would disappear.
That was not the registered setup, so the verdict stays $10. A post-hoc replay at that commit is in §3.1.

## 4. Proposed change (NOT applied; needs the user's approval)

```diff
-  min_order_quote_usd: 0         # minimum order size (issue #81): reject (never resize up) any buy whose final
+  min_order_quote_usd: 10        # minimum order size (issue #81): reject (never resize up) any buy whose final
```

The comment block under it should also say why 10 (this doc) and when to revisit it (§5 items 2 and 4).

**One test must change with it.** Applying the change in a scratch copy gives root pytest 1 failed /
1029 passed; `validate_invariants.py` still gives 5/5. The failing test is
`tests/test_risk_engine.py::test_default_config_floor_decides_exactly_as_before`. It pins "the shipped
config behaves as if the floor were disabled" and asserts the $0.01 / $0.50 dust spends are allowed.
That expectation was correct while the decision was "ship disabled". It would change by decision, not
because the old behaviour was a bug.

The replacement should pin:
- the shipped floor rejects the $0.01 and $0.50 spends;
- it allows an exactly-$10 spend (the boundary is inclusive);
- it decides exactly as no floor for spends >= $10.

**Approvals needed:**
1. The user, for the risk-parameter change (CLAUDE.md).
2. An Opus read-through of the config + test diff.
3. Re-baselining, since the harness reads the key from config. The Daily Backtester Routine's
   baselines move: OFF `data_long/` 562 / 0.52 becomes 565 / 0.53; `data/` OFF is unchanged at 154 /
   0.18. The P6 ON baselines move too: 137 / 0.17 becomes 132 / 0.22, and 552 / 0.49 becomes 546 / 0.50.

## 5. Interactions and caveats

1. **Order of checks.** The floor runs last, after every cap and after the "no room" rejection, and
   it only rejects; it never sizes up. It cannot loosen any cap. A rejected check records no plan, no
   cooldown and no new-entry slot (`bot.py` calls `record_trade` only on approval). Sells are never
   checked.
2. **Fee-slack dust scales with equity and fee** (§2.4: <= fee x 10% x equity).
   - At the 0.10% paper fee, $10 covers equity up to about $100k.
   - If live accounting values positions net of fee the same way, $10 stops covering it at about $38k
     at a 0.26% taker fee and about $26k at T3's 0.38%. That is not verified: `broker_live.py` is out of
     scope.
   - Revisit the floor if equity passes about $50k or the fee assumption changes. A floor relative to
     equity would scale; that needs `src/` and is not proposed.
3. **Exchange minimums are unverified.** Kraken's per-pair `ordermin` (base units) and `costmin`
   (quote) map to ccxt `limits.amount.min` / `limits.cost.min`. Those are only filled from the live
   AssetPairs call. The installed ccxt 4.5.84 ships no offline market data, and no network was used.
   The configured pairs are `/USDT`; the backtest data are USD.
   **$10 is not a substitute for a per-pair check (issue #81 stays open).** Before live, verify each
   pair's minimum. If a minimum is >= $10, the floor needs headroom above it, because the fee comes out
   of the spend and the price moves between plan and fill.
4. **Account size.** $10 is 0.1% of the $10k starting equity. It is about 1.6% of the median `data/`
   approval ($574 to $619), about 0.7% of the `data_long/` median ($1,491 to $1,515), and 1% of a
   full-size $1,000 position. Below about $2,500 starting equity it starts to remove real trades
   (§2.5). Re-run this study if `starting_cash_usd` is lowered.
5. **Floor straddles.** The higher the floor, the more live-vs-harness sizing differences flip a
   decision (the AVAX case at $25). That is another reason to prefer the smallest passing floor.
6. **Resolution.** Results are on daily bars only. The live bot runs at 1h with up to 12 scans per bar
   when `bar_parity` is off, so live dust may be more frequent than measured. Not measured.
7. **Parity evidence comes from an unmerged branch** (`472bdc0`). The primary gates come from `main`'s
   harness.
8. **Not an edge fix.** `data/` still fails CI-low at every floor. Gates 1-5 barely move, so choosing
   among the four values carries little selection risk.

