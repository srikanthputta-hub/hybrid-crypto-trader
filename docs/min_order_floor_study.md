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
