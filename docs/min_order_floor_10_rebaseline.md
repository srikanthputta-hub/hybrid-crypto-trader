# Re-baseline after `risk.min_order_quote_usd: 0 -> 10` (Risk Officer, Opus)

- **Change:** `config/config.yaml` `risk.min_order_quote_usd` 0 -> 10, as recommended by the
  pre-registered study `docs/min_order_floor_study.md` (PR #102). Branch `claude/min-order-floor-10`
  off `origin/main` `c76dec3`. Paper mode only; `src/`, `src/broker_live.py`, the live-trading gate and
  every other config value are unchanged (parsed-YAML diff vs `origin/main` is exactly
  `risk.min_order_quote_usd: 0 -> 10`).
- **Why the baselines move:** the backtest harness (`strategy_pack_run/backtest_live_bot.py`) builds its
  `RiskEngine` from `config/config.yaml`'s `risk` section and forwards `min_order_quote_usd` (PR #90), so
  every harness run made after this change uses the $10 floor. Numbers measured at floor 0 are not
  comparable with numbers measured at $10.
- **What it is:** hygiene and live/harness fidelity, not an edge change. It removes sub-$10 "dust" approvals
  and the ~1e-12 USD float phantoms (study §2.2, §3.1). Gate patterns do not change.

## Commands (run from `strategy_pack_run/`, config at floor 10, 2026-10-12)

    python run_live_bot_report.py --data-dir data      --cost kraken_T3_taker --sims 200 --equity 10000                            --out min_order_floor_study/rebaseline_floor10/report_data_p6off.md
    python run_live_bot_report.py --data-dir data      --cost kraken_T3_taker --sims 200 --equity 10000 --count-same-bar-approvals --out min_order_floor_study/rebaseline_floor10/report_data_p6on.md
    python run_live_bot_report.py --data-dir data_long --cost kraken_T3_taker --sims 200 --equity 10000                            --out min_order_floor_study/rebaseline_floor10/report_data_long_p6off.md
    python run_live_bot_report.py --data-dir data_long --cost kraken_T3_taker --sims 200 --equity 10000 --count-same-bar-approvals --out min_order_floor_study/rebaseline_floor10/report_data_long_p6on.md

The four reports are committed under `strategy_pack_run/min_order_floor_study/rebaseline_floor10/`.
P6 OFF is the original harness (what the Daily Backtester Routine runs). P6 ON is harness_version
`p6-same-bar-exposure-v1`. Placebo seed 0 as hard-coded.

## Results (kraken_T3_taker; "floor 0" rows are the on-record numbers from `docs/p6_harness_exposure_fix.md` §2, E3/E4)

| dataset | harness | floor | trades | win% | avg R [95% CI] | return | maxDD | Sharpe (EW bench) | later-30% n / avg R | placebo pct ret / avg R | gates 1-5 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `data/` | OFF | 0 | 154 | 40.3% | 0.18 [-0.08, 0.46] | 22.5% | -24.1% | 0.68 (0.28) | 52 / 0.42 | 98 / 91 | P F P P P |
| `data/` | OFF | **10** | **154** | 40.3% | **0.18 [-0.08, 0.46]** | 22.5% | -24.1% | 0.68 (0.28) | 52 / 0.42 | 98 / 91 | P F P P P |
| `data/` | ON | 0 | 137 | 40.1% | 0.17 [-0.11, 0.47] | 13.1% | -24.3% | 0.48 (0.28) | 46 / 0.43 | 96 / 94 | P F P P P |
| `data/` | ON | **10** | **132** | 40.9% | **0.22 [-0.09, 0.53]** | 14.3% | -23.5% | 0.51 (0.28) | 43 / 0.50 | 96 / 96 | P F P P P |
| `data_long/` | OFF | 0 | 562 | 48.0% | 0.52 [0.36, 0.68] | 387.5% | -29.2% | 1.00 (0.81) | 246 / 0.34 | 100 / 98 | P P P P P |
| `data_long/` | OFF | **10** | **565** | 48.3% | **0.53 [0.37, 0.69]** | 406.2% | -27.7% | 1.03 (0.81) | 249 / 0.37 | 100 / 98 | P P P P P |
| `data_long/` | ON | 0 | 552 | 47.6% | 0.49 [0.34, 0.65] | 340.5% | -28.7% | 1.01 (0.81) | 238 / 0.25 | 100 / 98 | P P P P P |
| `data_long/` | ON | **10** | **546** | 47.8% | **0.50 [0.34, 0.65]** | 342.9% | -28.7% | 1.01 (0.81) | 233 / 0.26 | 100 / 98 | P P P P P |

Every floor-10 row reproduces the study's predictions exactly (study §2.1 for P6 ON, §2.5 for P6 OFF).
`data/` P6 OFF is unchanged in every reported figure.

Paper-fee (0.10%) rows, information only: `data/` OFF 154 / 0.25 / 29.9%; `data/` ON 132 / 0.29 / 20.1%;
the `data_long/` rows are in the committed reports.

## Current baselines (use these from now on)

| harness | `data/` | `data_long/` |
|---|---|---|
| original (P6 OFF), the Routine's | 154 trades, avg R 0.18 [-0.08, 0.46], return 22.5%, Sharpe 0.68, maxDD -24.1%, 4/5 (unchanged) | **565 trades, avg R 0.53 [0.37, 0.69], return 406.2%, Sharpe 1.03, maxDD -27.7%, 5/5** (was 562 / 0.52 [0.36, 0.68] / 387.5% / 1.00 / -29.2%) |
| P6 ON (`p6-same-bar-exposure-v1`) | **132 trades, avg R 0.22 [-0.09, 0.53], return 14.3%, Sharpe 0.51, 4/5** (was 137 / 0.17) | **546 trades, avg R 0.50 [0.34, 0.65], return 342.9%, Sharpe 1.01, 5/5** (was 552 / 0.49) |

## What is NOT re-baselined, and why

- **`scripts/backtest_summary.json`: not edited.** It holds no live-strategy baseline field. The
  "Baseline ... 562 trades, avg R 0.52 ..." / "154 trades, avg R 0.18 ..." strings are inside the
  `notes` of past `candidates` entries. Those are append-only historical records of what each candidate
  was compared against at the time (floor 0), and the Routine reads the array to avoid retesting old
  parameters. Rewriting them would falsify history. New candidates must be compared against the
  table above.
- **Historical reports** (`strategy_pack_run/report_p6_live_strategy_*.md`, `report_live_strategy_*.md`,
  `report_p1e_universe_gate.md`) and docs that quote 154 / 0.18 and 562 / 0.52 as measured at the
  time (`p1c_rank_signals_validation.md`, `p1e_universe_gate_run.md`, `p2_0_*`,
  `engine_score_saturation.md`, `multiday_hold_design.md`) record floor-0 measurements and are left as
  they are.
- **`strategy_pack_run/run_rank_key_eval.py`** (one-off evidence script, not the Routine) hard-codes an
  integrity check `S2_sorted == (562, 0.52)` (and other floor-0 values). Re-running it against this config
  will now stop at that check. Re-run it with the floor overridden to 0, or update its expectations in a
  separate change if it is ever reused.
- **The Daily Backtester Routine's stored prompt** lives outside the repo and cannot be edited from here.
  If it cites `data_long/` 562 / 0.52 (or 387% / Sharpe 1.00 / maxDD -29.2%) as the live baseline, that
  citation is now stale: use 565 / 0.53 [0.37, 0.69] / 406.2% / 1.03 / -27.7%. Its `data/` citation
  (154 / 0.18) stays correct.

## Caveats carried over from the study (§5)

- $10 is a fixed dollar amount. Re-run the study if `starting_cash_usd` drops below about $2,500 (it
  starts removing real trades) or equity passes about $50k or the fee assumption changes (fee-slack dust
  grows as fee x `max_position_pct` x equity).
- Kraken per-pair minimums (`limits.cost.min` / `limits.amount.min`) are not verified. $10 is not a
  per-pair check; issue #81 stays open.
- Daily bars only. The live bot runs 1h with `loop.bar_parity` off, where dust may be more frequent than
  measured.
