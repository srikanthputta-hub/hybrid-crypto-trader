# Project status (as of 2026-10-11)

Read this first when picking the repo up cold. It summarises the "system redesign" work and what is
waiting on whom. The detail lives in the linked docs; the PR/issue numbers are GitHub numbers.

**The bot is unchanged in behaviour by the redesign so far:** paper mode, static 16-symbol
universe (`market.symbols`), 1h candles, daily-trend gate on, Claude advisor off. Nothing below has
promoted a strategy, a universe change or a risk-parameter change to the live config.

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
| Risk floor | `risk.min_order_quote_usd` minimum order size, rejects never resizes up. **Ships as 0 (disabled)** (#86; partial for issue #81: Kraken per-pair minimums are not done, issue stays open) | `src/risk_engine.py` |
| Multi-day design | Design only for a multi-day-hold redesign of the strategy; no code or config change (#92) | [multiday_hold_design.md](multiday_hold_design.md) |
| Bar parity | Opt-in live-loop parity rules P1-P5 (`loop.bar_parity`, default off; no behaviour change) with the closed-bar archive (#101) | [bot_bar_parity.md](bot_bar_parity.md) |
| `min_order_quote_usd` study | Pre-registered rule recommends a $10 floor; **evidence only, not applied** (#102) | [min_order_floor_study.md](min_order_floor_study.md) |
| CI tweak | Push CI skips `web/data.json`-only commits (#93) | `.github/workflows/ci.yml` |

Background research: [crypto_bot_upgrade_playbook.md](crypto_bot_upgrade_playbook.md).

## Pending (not done, not scheduled by this doc)

- **P6 harness fix** - done (`count_same_bar_approvals`), see [p6_harness_exposure_fix.md](p6_harness_exposure_fix.md).
- **#87** - `risk_engine` fails open on NaN `portfolio_equity` / `cash_available` (open issue). It should fail closed.
  Touches `src/risk_engine.py`: Opus-level review required.
- **Weekly parity check + Telegram label** - built on the local branch `claude/parity-check-and-label` (off `af99b64`), **not
  pushed, no PR**: `scripts/paper_parity_check.py` and `notifier.label` / the 1d "Valid until" lines, with tests and
  [paper_parity_check.md](paper_parity_check.md). Both are inert until the user starts a trial: the label ships `null`, and
  the check needs `loop.bar_parity` + `logging.closed_bars_dir` on. Touches `src/bot.py` (2 display-only hunks): Architect
  read-through requested. Before a trial: set `retention_days: 0` (or archive the audit log), and the `min_order_quote_usd`
  floor decision from the study above is still open.
- **Paper trial** - a time-boxed paper run of whatever candidate the user approves; none is approved today.
- Not built: `universe.mode: discover` (raises `NotImplementedError`), wiring `rank_signals` into `bot.py`,
  passing `risk.volatility_scale_floor` through `bot.py` (the engine's 0.5 default applies).

## Where things are

- Architecture and config reference: [../README.md](../README.md). Roles and non-negotiables: [../CLAUDE.md](../CLAUDE.md).
- Backtest harness and its reports: `strategy_pack_run/README.md`.
- Always-on host setup: `deploy/README.md`.
