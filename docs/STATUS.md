# Project status (as of 2026-10-16)

Read this first when picking the repo up cold. It summarises the "system redesign" work and what is
waiting on whom. The detail lives in the linked docs; the PR/issue numbers are GitHub numbers.

> **DO NOT MERGE `claude/daily-bar-paper-trial` outside the deploy window.** The VM auto-pulls `main`; merging
> before the 1h bot is stopped and disabled would put the daily config under the 1h state at the next restart.
> Follow `deploy/PAPER_TRIAL_RUNBOOK.md` section 3 (stop + disable, archive/reset, *then* merge, then pull and start
> between 00:01 and 00:45 UTC).

**Paper daily-bar trial (PR #106, branch `claude/daily-bar-paper-trial`, waiting for the owner's deploy window; not merged):** the shipped config
switches the **paper** bot to daily bars with bar parity on and the 16 coins on their `*/USD` pairs (the evidence's
bars), with no strategy or risk parameter change and `mode: paper`. Operator steps: [../deploy/PAPER_TRIAL_RUNBOOK.md](../deploy/PAPER_TRIAL_RUNBOOK.md);
pre-flight risks, the per-key timeframe audit and the kill-threshold re-simulation the design requires before
freezing: [daily_trial_preflight.md](daily_trial_preflight.md). The tooling it needs is all on `main` now (#103,
#104, #105, #107 below); PR #106 is the last piece and waits for the owner's deploy window. Until it is merged and
deployed, the paragraph below describes the running bot.

**The bot is unchanged in behaviour by the redesign so far:** paper mode, static 16-symbol
universe (`market.symbols`), 1h candles, daily-trend gate on, Claude advisor off. Nothing below has
promoted a strategy or a universe change to the live config. The one risk-parameter change is
`risk.min_order_quote_usd: 0 -> 10` (dust/phantom hygiene, not an edge change; see "Risk floor"
below; merged, #104). It moved the harness baselines; the current numbers are in
[min_order_floor_10_rebaseline.md](min_order_floor_10_rebaseline.md).

## Standing rules

- **Paper only.** No live orders, no `mode: live`, no change to the two-switch gate
  (`mode: live` + `LIVE_TRADING_CONFIRM`) without the user. See `CLAUDE.md`.
- **No live/paper config or strategy change without the user's explicit approval**, and without a
  written reason plus an out-of-sample test (`CLAUDE.md` trading rules).
- **CI is dead until 2026-11-01** (GitHub Actions minutes are exhausted). Run `python -m pytest -q`
  and `PYTHONPATH=. python scripts/validate_invariants.py` locally, and say so in the PR description.
  `ci.yml` already skips dashboard-data-only commits (#93).
- The "daily backtest sweep" promotion PRs **#78, #82, #83, #85, #88** and PR **#29** (Cloudflare
  Cron Trigger) are open and **await the user's decision**. They are not to be merged by an agent.
- `docs/system-redesign-2026-10.md` (the design doc most code comments cite by section) lives only
  on the branch `claude/system-redesign-design`, not on `main`.

## Merged on main

| Item | What it is | Doc |
|---|---|---|
| P1a | `MarketDataCache`: bar-close-aligned OHLCV cache, `logs/market_snapshot.json`; dashboard export reads it | `src/market_data.py` |
| P1b | `src/universe.py` (static mode only), risk-engine whitelist set from the universe, one active plan per base asset, held-symbol monitoring | `src/universe.py`, `src/bot.py` |
| P1c | `signal_engine.rank_signals()` added and validated; **inert** (not called by `bot.py`) | [p1c_rank_signals_validation.md](p1c_rank_signals_validation.md) |
| P1d | Universe datasets, point-in-time eligibility harness, tiered slippage (build only) | [p1d_universe_datasets.md](p1d_universe_datasets.md) |
| P1e | Gate run for `universe.mode: discover` at the $1M and $100k floors: **both FAIL**, evidence only, no promotion (#89) | [p1e_universe_gate_run.md](p1e_universe_gate_run.md) |
| P2.0 | Live strategy re-baselined at 1h: **FAIL** the 5-gate bar at its own resolution; follow-up diagnosis of why the 1h edge shrinks | [p2_0_1h_rebaseline.md](p2_0_1h_rebaseline.md), [p2_0_1h_edge_diagnosis.md](p2_0_1h_edge_diagnosis.md) |
| engine_score study | `engine_score` saturates; no pre-registered rank key qualified. **No change to `src/`** (#91) | [engine_score_saturation.md](engine_score_saturation.md) |
| Harness follow-ups | `min_order_quote_usd` forwarded to the harness; cost-wiring t-1 tests (#90) | `strategy_pack_run/` |
| Risk floor | `risk.min_order_quote_usd` minimum order size, rejects never resizes up (#86; partial for issue #81: Kraken per-pair minimums are not done, issue stays open). Shipped as 0 (disabled); **set to $10** per the floor study's recommendation (#102), merged to `main` as #104, paper mode. Fixed dollar amount: re-run the study if `starting_cash_usd` drops below ~$2,500 or equity passes ~$50k | `src/risk_engine.py`, [min_order_floor_study.md](min_order_floor_study.md), [min_order_floor_10_rebaseline.md](min_order_floor_10_rebaseline.md) |
| Multi-day design | Design only for a multi-day-hold redesign of the strategy; no code or config change (#92) | [multiday_hold_design.md](multiday_hold_design.md) |
| Bar parity | Opt-in live-loop parity rules P1-P5 (`loop.bar_parity`, default off; no behaviour change) with the closed-bar archive (#101) | [bot_bar_parity.md](bot_bar_parity.md) |
| `min_order_quote_usd` study | Pre-registered rule recommends a $10 floor; **evidence only, not applied** (#102) | [min_order_floor_study.md](min_order_floor_study.md) |
| CI tweak | Push CI skips `web/data.json`-only commits (#93) | `.github/workflows/ci.yml` |
| Notifier security | The Telegram bot token / heartbeat URL are no longer logged; Markdown is escaped so BUY alerts are not lost (#103). Rotate the token per `deploy/README.md` | `src/notifier.py`, `deploy/README.md` |
| $10 order floor | `risk.min_order_quote_usd` 0 -> 10, paper mode (#104) | [min_order_floor_study.md](min_order_floor_study.md), [min_order_floor_10_rebaseline.md](min_order_floor_10_rebaseline.md) |
| Parity check + label | `scripts/paper_parity_check.py` and the optional `notifier.label` / 1d "Valid until" lines (#105); inert until a trial starts | [paper_parity_check.md](paper_parity_check.md) |
| Ops memory + 1d dashboard | Bounded-memory dashboard export (tail-read) and streaming parity check: the audit log is no longer a memory constraint (export ~33 MB constant; parity check peaks ~0.67 GB on an 18-month log, so on a 1 GB `E2.1.Micro` it still runs off the VM, fine on the 6 GB shape); dashboard "prices as of" label, label banner, P/L opens on Daily at 1d. No change to any decision the bot makes (#107) | [ops_memory_followup.md](ops_memory_followup.md) |
| Roster | `CLAUDE.md` assigns an owner to every module (market_data, universe, scripts, web, ...) (#95) | [../CLAUDE.md](../CLAUDE.md) |

Background research: [crypto_bot_upgrade_playbook.md](crypto_bot_upgrade_playbook.md).

## Pending (not done, not scheduled by this doc)

- **P6 harness fix** - done (`count_same_bar_approvals`), see [p6_harness_exposure_fix.md](p6_harness_exposure_fix.md).
- **#87** - `risk_engine` fails open on NaN `portfolio_equity` / `cash_available` (open issue). It should fail closed.
  Touches `src/risk_engine.py`: Opus-level review required.
- **Weekly parity check + Telegram label** - merged (#105). `scripts/paper_parity_check.py` and `notifier.label` / the
  1d "Valid until" lines, with tests and [paper_parity_check.md](paper_parity_check.md). Both are inert until the user
  starts a trial: the label ships `null`, and the check needs `loop.bar_parity` + `logging.closed_bars_dir` on (the
  trial config on #106 sets both). Before a trial: set `retention_days: 0` and restart the trial if it started with the
  default (pruning cannot be repaired afterwards); `risk.min_order_quote_usd` is 10 on main (#104), which the parity
  budget needs (> 0). The check evaluates the pre-registered 10% budget from the first entry by default;
  `--relaxed-early-budget` (from 20 entries) is a loosening that needs the user's acceptance and is off.
- **Paper trial** - Option A (daily bars) approved by the user as a paper-only switch; config change and runbook on
  `claude/daily-bar-paper-trial` (PR #106, not merged, waiting for the owner's deploy window). Before day 0 the user picks the kill thresholds
  (recommended re-simulated set, `daily_trial_preflight.md` §5; USER MUST CONFIRM) and the fee tier the trial is judged
  at. The Daily Backtester Routine must be paused and its promotion PRs left unmerged for the trial (CI is dead, so the
  pin test cannot block a merge).
- Not built: `universe.mode: discover` (raises `NotImplementedError`), wiring `rank_signals` into `bot.py`,
  passing `risk.volatility_scale_floor` through `bot.py` (the engine's 0.5 default applies).

## Where things are

- Architecture and config reference: [../README.md](../README.md). Roles and non-negotiables: [../CLAUDE.md](../CLAUDE.md).
- Backtest harness and its reports: `strategy_pack_run/README.md`.
- Always-on host setup: `deploy/README.md`.
