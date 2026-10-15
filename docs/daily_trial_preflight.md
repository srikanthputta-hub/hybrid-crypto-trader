# Paper daily-bar trial: pre-flight (PR description draft)

- **Role:** Architect (Opus). Branch `claude/daily-bar-paper-trial`. Local only: not pushed, no PR, not merged.
- **What:** switch the shipped **paper** config to the daily-bar trial of `docs/multiday_hold_design.md` (§3-§6),
  using the merged tooling: bar parity P1-P5 (#101, `docs/bot_bar_parity.md`), the weekly parity check and Telegram
  label (#105, `docs/paper_parity_check.md`), the $10 order floor (#104, `docs/min_order_floor_study.md`), the
  notifier fixes (#103). User-approved as a paper-only switch.
- **Not touched:** `src/` (nothing needed changing), `src/risk_engine.py`, `src/broker_live.py` and the two-switch
  gate, `mode: paper`, every strategy and risk parameter, the backtest harness.
- **Operator steps:** [deploy/PAPER_TRIAL_RUNBOOK.md](../deploy/PAPER_TRIAL_RUNBOOK.md).

## 1. The change

| file | change |
|---|---|
| `config/config.yaml` | six values: `market.timeframe` 1h -> 1d, `loop.bar_parity` false -> true, `loop.max_entry_delay_minutes` null -> 60, `logging.closed_bars_dir` null -> `logs/daily_bars`, `logging.retention_days` 30 -> 0, `notifier.label` null -> `PAPER TRIAL`. Comments: stale 1h wording in the `htf_filter` block and on `max_new_entries_per_window` updated (design §6 asks for both); inert-at-1d notes on `cooldown_minutes` and `max_daily_loss_pct`; growth note on `retention_days` |
| `.gitignore` | `logs/archive/` (the dated archives of `scripts/reset_portfolio.py` were not covered by `logs/*.csv` etc.) |
| `tests/test_paper_trial_config.py` (new) | pins the trial values and the frozen strategy; runs the real shipped config end to end (below) |
| `tests/test_bot_universe_wiring.py`, `test_bot_bar_parity.py`, `test_broker_live.py` | the "on today's config" identity/parity tests test the default (flags-off, hourly) code path with hourly fixtures; they now say so via `_pre_trial_hourly_raw()` (1h, bar parity off, no label). `_isolated_raw` also isolates `bar_decisions_path` (see §6 item 1) |
| `tests/test_notifier_label.py`, `test_validate_invariants.py` | "real config is off / null" -> "real config is the trial values" |
| `deploy/PAPER_TRIAL_RUNBOOK.md` (new), `deploy/README.md`, `README.md`, `docs/STATUS.md` | runbook + links; README config table and architecture notes reflect the shipped trial values |

## 2. Does the evidence correspond to this exact config?

The daily evidence (E3/E4 and their P6 / $10-floor re-gates, `docs/min_order_floor_10_rebaseline.md`) was produced
by `strategy_pack_run/run_live_bot_report.py`, which reads `indicators`, `scanner`, `signal_engine`,
`htf_filter.enabled`, `trade_planner` and the whole `risk` block from `config/config.yaml`. Checked three ways:

1. **Parsed-YAML diff vs `origin/main`:** exactly the six keys above. The `indicators`, `scanner`, `signal_engine`,
   `htf_filter`, `trade_planner`, `risk`, `advisor` and `universe` sections are identical to the config at
   `2476986` (the commit the floor-10 re-baseline was run on).
2. **Byte-identical reproduction:** the four committed re-baseline reports
   (`strategy_pack_run/min_order_floor_study/rebaseline_floor10/report_{data,data_long}_p6{on,off}.md`, 200 placebo
   sims, `kraken_T3_taker`) were re-run on this branch's config: all four `diff` identical.
3. **Keys the harness does not read**, and why they don't break the correspondence: `market.timeframe` (the harness
   is daily by construction; that is the point of the switch), `market.history_bars` (the harness uses full history
   and a 150-bar scan window; live uses 200 bars, which `tests/test_daily_live_parity.py` shows changes no decision
   over 719 bars), `loop.*` (P1-P5 make the live loop decide like the harness; P4 can only remove entries and the
   harness assumes zero delay), `notifier`/`logging` (display and files), `advisor` (off; the harness has none).

**Differences to flag:**

- **Symbols / quote:** the evidence bars are `*/USD` (Kraken for `data/`, Coinbase for `data_long/`); the bot trades
  `*/USDT` on Kraken (`market.symbols`). Same coins, different order books: USDT pairs are thinner and trade at the
  USDT/USD basis. The weekly parity check is unaffected (it replays the harness on the USDT bars the bot actually
  archived), but "the strategy under test is the evidenced one" holds only up to the bar source. Switching to
  `*/USD` is a separate config decision (the user's), not made here.
- **Harness report text:** `run_live_bot_report.py`'s fixed "Important limits" paragraph says "`config/config.yaml`
  runs `market.timeframe: "1h"`". After this change that sentence is stale. Left alone so the reports stay
  byte-reproducible; a QA follow-up.

## 3. Per-key timeframe audit

Every config key that depends on the timeframe or on 1h assumptions:

| key | value | verdict at 1d |
|---|---|---|
| `market.timeframe` | `"1d"` | **changed** (Option A) |
| `market.history_bars` | 200 | fine: >= 150 (P5, startup-validated), <= Kraken's 720; the value the daily parity tests ran with |
| `loop.poll_interval_seconds` | 300 | fine: one decision per symbol per bar (P1); first cycle after 00:00 UTC decides by ~00:05, inside the 60-min guard. Cost: ~287 echo lines/symbol/day (§6 item 4) |
| `loop.bar_parity` | true | **changed** |
| `loop.max_entry_delay_minutes` | 60 | **changed** (design value, untuned, only removes entries) |
| `htf_filter.enabled` | true | fine: at 1d + bar parity it runs on the scan frame's last 150 bars (P5), no fetch, exactly the harness |
| `indicators.*` (EMA 12/26, RSI 14) | unchanged | fine: bar counts; the evidence is at daily bars |
| `scanner.*` lookbacks (20/5/5/10/8/10 bars), `momentum_return_threshold` | unchanged | fine: bar counts, evidenced at daily (must not change: frozen) |
| `trade_planner.*` (stop 1.5 ATR etc.) | unchanged | fine: ATR is daily; `stop_atr_mult: 1.5` was chosen on daily data |
| `risk.cooldown_minutes` | 60 | inert at 1d (bars 1440 min apart, P1), as in the evidence; not retuned |
| `risk.max_daily_loss_pct` | 0.05 | inert at 1d (baseline resets at 00:00, right before the only entry pass), as in the evidence. Flag for the Risk Officer (design §3.1): no intraday circuit breaker exists at 1d |
| `risk.max_new_entries_per_window` / `new_entry_window_minutes` | 5 / 120 | becomes "5 entries per daily bar", exactly what the harness measured; comment updated |
| `risk.max_volatility_pct` | 0.08 | becomes **active** at 1d (daily ATR/price), as in the evidence |
| `risk.min_order_quote_usd` | 10 | fine: the parity budget needs > 0; 0% mismatch over 719 bars at $10 |
| `universe.refresh_utc`, other `universe.*` | unread | n/a (static mode) |
| exchange fetch limit | `history_bars + 1` = 201, `1d` | fine: one call per symbol per day (cache refetches at `last_open + 2 x tf`); no separate htf fetch (asserted by the e2e test) |
| `MarketDataCache` refetch rule | code | fine: due at the 00:00 close; first-seen bar is decided and archived (no settle delay; §6) |
| heartbeat (`HEALTHCHECK_PING_URL`) | env | fine: pinged every cycle (300 s), so healthchecks.io's 10-min period still fits |
| `export_dashboard_data.MARKET_SNAPSHOT_STALE_SECONDS` | 900 (code) | fine: compares the snapshot's write time, rewritten every cycle, not the bar time |
| export `RECENCY_WINDOW` 15 min (role "working") | code | display: roles show working only ~00:00-00:15 UTC. Fine |
| dashboard P/L default "Hourly", price without "as of" | `web/app.js` | display follow-up (design §6, Platform Engineer); runbook §10 tells the operator |
| `notifier.label` | `"PAPER TRIAL"` | **changed**; with bar parity at 1d BUY messages also get `Bar:` / `Valid until:` (close + 60 min) |
| `logging.closed_bars_dir` | `logs/daily_bars` | **changed** |
| `logging.retention_days` | 0 | **changed**; disk fine, memory is the constraint (§6 item 4) |
| `logging.bar_decisions_path` | default | fine: a 1h-era ledger (none exists: bar parity was off) would be ignored on the timeframe check |

## 4. What the first week can and cannot show

**Can:** that the deployment works end to end on the real exchange and VM: one decision per symbol per daily close
at ~00:05 UTC, nothing re-decided across restarts, the bar archive and ledger written, labelled messages with the
right `Valid until`, entries >= $10, and that the weekly parity check gives PASS (or INSUFFICIENT DATA in the
first 2-3 days) on real Kraken bars, i.e. that the live loop decides like the harness on the bars it saw.

**Cannot:** anything about edge or expectancy. At ~6 entries a month, week 1 has 0-3 entries and probably no closed
trade (mean hold 17-20 days). The kill rule's first check is at 20 closed trades (~3-4 months). Even the full trial
cannot confirm the 2-year edge (design §4: ~750 correlated trades needed). A quiet first week (PASS "vacuous") is
normal and says nothing either way.

## 5. Kill thresholds: the pre-freeze re-simulation the design requires

Design §5 quotes -0.70 / -0.55 / -0.45 / -0.40 at 20/30/40/50 trades and requires them to be "re-simulated on the
re-gated trade lists before the protocol is frozen"; `docs/p6_harness_exposure_fix.md` §8 lists that as not done.
Done here with the design's own method (its `b2_kill.py`: 4,000 runs, whole-entry-day resampling of the trade list
shifted to the true mean, checks at 20/30/40/50, threshold = 5th percentile of a true +0.18R strategy, seed 1);
scratch script, not committed. Pool = `kraken_T3_taker` trades (closed and end-of-data) from `simulate_portfolio` on
this config.

| pool | thresholds (20/30/40/50) | kill rate if true R = +0.52 / +0.22 / +0.18 / 0 / -0.24 |
|---|---|---|
| original harness, `data/` (154 trades): reproduction check | -0.67 / -0.55 / -0.44 / -0.39 | 1% / 10% / 11% / 25% / 48% (design: 1% / - / 11% / 22% / 46%) |
| **P6-fixed, $10 floor, `data/` (132 trades)** | **-0.57 / -0.43 / -0.34 / -0.30** | 1% / 9% / 11% / 27% / 54% |
| P6-fixed, `data/`, reference +0.22 (the re-gated point estimate) instead of +0.18 | -0.53 / -0.40 / -0.31 / -0.26 | 1% / 11% / 13% / 31% / 58% |
| design §5 thresholds applied to the P6-fixed `data/` pool | -0.70 / -0.55 / -0.45 / -0.40 | 0% / 4% / 5% / 16% / 42% |
| P6-fixed `data_long/` (546 trades), reference +0.18 | -0.56 / -0.44 / -0.36 / -0.29 | 1% / 8% / 10% / 24% / 52% |

The method reproduces the design's numbers on the original pool, so the P6-fixed row is what the design asked for:
**-0.57 / -0.43 / -0.34 / -0.30** keeps the pre-registered 11% false-stop rate and raises power against -0.24R from
46% to 54%. Keeping the design's thresholds on the corrected pool is more lenient (5% false stop, 42% power).
**The user picks one before day 0** (runbook §1 item 2); this branch does not change the protocol text. The 30%
equity-drawdown kill is unaffected.

## 6. Known residuals and risks

From `docs/bot_bar_parity.md` §6 (unchanged by this switch):
- **Fill price:** paper fills at the last daily close, the harness at the next open; changes sizes slightly (max
  drift 0.5-1.9% measured), not decisions over 719 bars.
- **P3 only credits same-cycle exits:** a symbol first decided on a later tick than a same-bar exit (its fetch failed
  at 00:05) sees the freed room. Live only; bounded by P4.
- **Ledger is last-writer-wins:** exactly one bot process (no manual `--once`, no `refresh-dashboard.yml` run, PR #29
  unmerged).
- **First-seen bars:** no settle delay; a fetch at 00:00:05 can miss the last seconds of trades and an exchange
  revision of a bar is never rewritten. The parity check uses the same archived bars, so it is not a parity issue.
- **Cooldown and daily-loss halt inert at 1d** (as in the evidence): no intraday circuit breaker; an intraday crash
  that recovers by the close does not stop out (close-only monitor).
- **Float phantoms** are removed by the $10 floor (0% over 719 bars).

Found here:
1. **Test isolation (fixed):** with bar parity on in the shipped config, tests that ran a cycle on the real config
   without a `bar_decisions_path` wrote the repo's `logs/bar_decisions.json` (a first full run did). `_isolated_raw`
   now isolates it; the e2e test asserts the repo `logs/` is untouched. On the VM the same rule applies: a manual
   `python -m src.bot --once` would write the live ledger (and re-decide nothing, but it is a second writer).
2. **Start window:** the first cycle must run between 00:00 and 01:00 UTC. A start later in the day stale-skips the
   bar it decides first, and the parity check (trial start = earliest `bar_close`) then cannot PASS when the harness
   had an entry there (pinned by `test_a_first_start_outside_the_00_to_01_utc_window_contaminates_the_trial_start`).
   Runbook §2/§3 step 8.
3. **Outages over midnight** (> 1 h) stale-skip that day's entries; explained, but counted against the strict 10%
   budget, which is evaluated from the first entry. Early in the trial one such day can produce FAIL on budget.
4. **Memory, not disk:** the audit log grows ~1.15 MB/day (measured: 16 x 288 lines/day at ~250 bytes). The dashboard
   export reads the whole file every cycle, and the parity check holds ~1.1 KB per line in RAM (~5 MB per trial
   day; ~2.7 GB at 18 months). Fine on the 6 GB Ampere VM; not on a 1 GB `E2.1.Micro`. Code fixes (tail-read in the
   export; skip echo lines before JSON parsing in the check) are follow-ups for the Platform Engineer / QA, not
   made here (`src/`/`scripts/` untouched).
5. **Bar source:** Kraken USDT bars vs the USD evidence bars (§2).

**Data and venue caveats:**
- The user's own venue is Revolut X (config comment). Revolut X prices are not the Kraken USDT bars the plan is
  computed on, and its fees differ from both the paper broker's 0.10% and the evidence's `kraken_T3_taker` (0.38% fee
  + 0.05% slippage per side). E7: at a 0.80% fee the 2-year window is roughly breakeven. The trial's R must be
  re-costed at the chosen venue (design §5 "Metric").
- Kraken per-pair minimums are still unverified (issue #81 open); irrelevant in paper.
- BNB/USDT has a shorter Kraken history than the others (in the committed USD data it starts 2025-04-22); the bot
  and the harness both use what exists.

**Not part of this:** issue #99 (live-readiness blockers: order write-ahead marker, halt alert + reconcile, sell
balance cap) stays open and untouched; live mode stays off. Also not here: the 0-60 min fill-delay cost on the 15m
data (required before any real money, design §4), an intrabar catastrophe stop, the Phase 2/3 work, the dashboard
display follow-ups, `volatility_scale_floor` pass-through.

## 7. Verification (local; CI is dead until 2026-11-01)

- `python -m pytest -q` (root): all passed, including 10 new tests in `tests/test_paper_trial_config.py`.
- `python -m pytest -q` in `strategy_pack_run/`: all passed.
- `PYTHONPATH=. python scripts/validate_invariants.py`: 7/7 (`bar_parity=True, max_entry_delay_minutes=60.0`,
  `label='PAPER TRIAL'`).
- `python research/v2_signals.py`: 7 tests passed.
- The four floor-10 re-baseline reports reproduce byte-identically (§2).

## 8. For the independent reviewer

1. `tests/test_paper_trial_config.py`: is "override only paths" really only paths (`PATH_KEYS`, asserted by a
   flattened diff)? Is the USD -> USDT bar mapping in the fake exchange acceptable for an e2e of the shipped
   symbols?
2. The `_pre_trial_hourly_raw` change to the default-path identity tests: confirm they still test what they were
   written to test, and that nothing now tests the shipped config only implicitly.
3. The start-window procedure (runbook §2-§4) and the claim that a late first start contaminates the check.
4. The kill-threshold re-simulation (§5) and whether the protocol should adopt it.
5. Memory growth (§6 item 4) against the VM's actual shape.
6. The USDT-vs-USD bar source (§2): accept for the trial, or switch symbols before day 0 (a separate decision).
