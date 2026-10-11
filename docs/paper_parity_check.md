# Weekly paper-trial parity check and Telegram labelling

- **Role:** Platform Engineer (Sonnet). `scripts/paper_parity_check.py` (new, read-only), tests,
  `notifier.label` in `src/config.py` / `config/config.yaml` / `src/notifier.py`, a 7th tightening check in
  `scripts/validate_invariants.py`, and a 20-line change in `src/bot.py` (`_maybe_notify` + one validation line in
  `build_components`; flagged for the Architect, below).
- **Not touched:** `src/risk_engine.py`, `src/broker_live.py` and the two-switch gate, the trading pipeline, any config
  **value** (`notifier.label` ships as `null`), the harness.
- **Spec:** `docs/multiday_hold_design.md` §5 (Parity row), §6 (`scripts/paper_parity_check.py`, `bot.py` item 4),
  §3.1 (alerts), and `docs/bot_bar_parity.md` (what the bot persists).
- **Branch:** `claude/parity-check-and-label`, off `origin/main` at `af99b64`. Local only.

## 1. `scripts/paper_parity_check.py`

Compares what the live **paper** bot decided with what the P6 backtest harness
(`strategy_pack_run/backtest_live_bot.py`, `simulate_portfolio(count_same_bar_approvals=True)`) would have decided on the
**same closed daily bars**. No network. It never writes to the bot's state (only `--json-out`, which it refuses to point at
an input).

```
PYTHONPATH=. python scripts/paper_parity_check.py                       # last 7 decided bars, config/config.yaml paths
PYTHONPATH=. python scripts/paper_parity_check.py --days 14 --json-out logs/parity_week.json
PYTHONPATH=. python scripts/paper_parity_check.py --config my.yaml --since 2026-10-01 --until 2026-10-08
PYTHONPATH=. python scripts/paper_parity_check.py --format json
```

### Prerequisites (otherwise the result means nothing)

1. The bot runs with `loop.bar_parity: true`, `market.timeframe: "1d"`, **`logging.closed_bars_dir` set** (the design suggests
   `logs/daily_bars`), so every audit event carries `bar_close` and the exact bars are archived. The check refuses any other
   timeframe.
2. The paper portfolio was reset at trial start (design §5). The harness replay starts a fresh
   `risk.starting_cash_usd` portfolio at the **trial start** = the earliest `bar_close` in the audit log (or `--trial-start`).
3. **`logging.retention_days` prunes the audit log (30 days by default).** The comparison is path dependent (the harness
   portfolio must start where the bot's did), and the design's 10% budget is cumulative over the trial. For a trial longer
   than the retention, set `retention_days: 0` (keep everything) or archive the audit log, or pass `--trial-start` and
   accept that earlier decisions are not compared (the report says where the trial-to-date record starts).
4. The config passed to `--config` is the config the bot ran with (the trial freezes it). Note the committed
   `risk.min_order_quote_usd: 0` produces known float-phantom divergences (`docs/bot_bar_parity.md` §4.2); those are tagged
   `float_phantom` and count against the budget.

### What is compared (per bar close and symbol, over the trial to date; the window is the part reported as "this week")

| Item | Rule |
|---|---|
| Entry check | harness vs bot: signal **pattern**, allowed or rejected, rejection reason **up to the first `;`** (the `same_decision` equivalence of `tests/test_daily_live_parity.py`: clock-dependent text, cooldown remaining time, "no room" vs "below the minimum" for a ~1e-12 USD size), plan entry / stop / risk-reward to a relative **1e-6** (design: live indicators use a 200-bar fetch, the replay all archived bars, so warm-up leaves ~1e-7), approved size within 25% (fill-price residual) |
| Entries | harness-approved (bar, symbol) vs bot `planned`; design metric = symmetric difference / harness count |
| Exits | (decision bar, symbol, stop_loss or take_profit) harness vs bot. A flat padding bar is appended after the newest bar so an exit decided on the newest bar is visible (the harness executes exits at the next open) |
| Integrity | a bar decided twice (P1), audit fills missing from `trades.csv`, `trades.csv` rows with no audit fill |

Each mismatch is listed with both sides and a reason. `explained_by` is set only when the audit log itself proves a
documented cause: `staleness_guard` (`stale bar: entries skipped`), `bot_gap` (no bot event for that bar: down, restart, fetch
failure), `kill_switch`, `advisor_veto`, `float_phantom` (an approved quote under 1e-6 USD), or
`downstream_of_explained_mismatch` (later mismatches, after an earlier **explained** one, because the two portfolios have
diverged since; never applied after an unexplained one, and never to integrity mismatches). Anything else is unexplained.

### Status and exit codes

| Status | When | Exit |
|---|---|---|
| `PASS` | no mismatch in the window (flagged `vacuous` if neither side made an entry or exit decision) | 0 |
| `WARN` | mismatches in the window, all explained, trial-to-date rates within the budget | 0 |
| `FAIL` | any unexplained mismatch in the window (**pauses the trial**), or a trial-to-date entry / exit / overall mismatch rate above the budget (default 10%, `--budget`) | 1 |
| `INSUFFICIENT DATA` | empty audit log, no `bar_close` in it (bar parity was off), or fewer than `--min-bars` (3) decided bars in the window | 0 |
| input error | missing / unreadable / corrupt audit log, ledger, trade log, config or bar archive; a decided bar missing from the archive; a timeframe other than 1d; a bad `--json-out`. The message names the file and line | 2 |

A single explained mismatch is already above 10% of the first ~10 entries, so early in a trial a `WARN`-level event shows up
as a budget `FAIL`. That is the pre-registered rule (design §5), not a bug: look at the listed mismatches.

### Example (real bot loop on the committed Kraken daily data, 16 bars; see `tests/test_paper_parity_check.py`)

```
PAPER TRIAL PARITY CHECK -- PASS
the bot's decisions match the harness's
window: 2025-09-28 .. 2025-10-13  (16 decided bars, 16 symbols)
trial start: 2025-09-28 (earliest bar_close in the audit log)
entries : harness 7, bot 7, matched 7, mismatched 0; agreement 100.0%, mismatch 0.0% (budget 10%)
exits   : harness 7, bot 7, matched 7, mismatched 0; agreement 100.0%, mismatch 0.0% (budget 10%)
decisions compared: 16; mismatches: 0 (unexplained 0); overall mismatch rate 0.0%
trial to date (since 2025-09-28): entries mismatch 0.0% (0/7), exits mismatch 0.0% (0/7), 0 mismatches (0 unexplained) in 16 decisions
paper fee assumed: 0.10%; budget: <= 10% mismatches, an unexplained mismatch pauses the trial
```

With one entry flipped in the audit log (exit code 1):

```
PAPER TRIAL PARITY CHECK -- FAIL
2 unexplained mismatch(es) in the window: an unexplained mismatch pauses the trial until fixed; trial-to-date entry mismatch rate 14.3% exceeds the 10% budget; ...
mismatches:
  2025-10-02  BTC/USD    entry_decision       [UNEXPLAINED]
      harness approved (ok); bot rejected (position would exceed the cap)
  2025-10-02  BTC/USD    trade_log_audit      [UNEXPLAINED]
      trade log buy at 2026-10-11T17:08:00.395494+00:00 has no audit-log fill
```

### Limits

- Cost: one run replays the harness over the whole archive (about 15 s for 216 bars x 16 symbols; it grows with the trial).
- The audit log does not record equity, so equity / cash are compared only through their effect (approvals and sizes).
- The 1e-6 level tolerance and the 25% size tolerance are options (`--level-rtol`, `--size-rtol`); the size tolerance is loose
  because the paper broker fills at the close and the harness at the next open.
- It checks implementation parity, not strategy edge (design §5). Kill thresholds and the end-of-trial bootstrap are
  separate (Architect reports from `logs/trades.csv`).
- Restart / partial-day behaviours that change which bar the bot decides on show up as `bot_gap` or `staleness_guard`; a gap
  the audit log cannot distinguish from "the bot never looked at this symbol" is also tagged `bot_gap`. The budget bounds
  how many of those are tolerated.

## 2. Telegram labelling (`notifier.label`)

```yaml
notifier:
  label: null        # or e.g. "PAPER TRIAL"
```

- `null` / absent: **today's exact message text** (byte-identical; `tests/test_notifier_label.py` compares every message
  with a frozen copy of `origin/main`'s `_maybe_notify`).
- A string: prefixed to every BUY signal, SELL (stop-loss / target) and "Bought" fill message as `*PAPER TRIAL* · `.
- With `loop.bar_parity: true` at `market.timeframe: "1d"` (independent of the label), BUY signal messages end with

  ```
  Bar: 2026-10-12 (daily, closed 2026-10-13 00:00 UTC)
  Valid until: 2026-10-13 01:00 UTC
  ```

  "Valid until" = the bar close + `loop.max_entry_delay_minutes` when set (what the staleness guard allows), else the next
  daily close. Other messages never get these lines.
- Validated at startup (`src.config.parse_notifier_settings`, called in `build_components`): null or a string, non-empty,
  no leading / trailing whitespace, at most 32 characters, no control / format / line-separator characters, none of the
  characters `*`, `_`, a backtick, `[`, `]` or a backslash (legacy Markdown: an unbalanced one makes Telegram reject the
  whole message, which the notifier swallows, so a bad label would silently turn every alert off), and no keys other than `label` in the block. The label comes only from
  the YAML (it is not a secret and there is no env var; `.env.example` is unchanged).
- `scripts/validate_invariants.py` has a 7th check, `notifier_config`, that runs the bot's own parser on the committed
  config (tightening only: `null` is valid).

## 3. `src/bot.py` change (Architect: please look)

Two hunks, display only: (1) `build_components` evaluates `cfg.notifier` next to `cfg.bar_parity` so a bad label stops the
process at startup; (2) `_maybe_notify` reads the label and, at 1d with bar parity on, the validity text, via
`getattr` so bare `SimpleNamespace` configs (as in `tests/test_bot.py`) behave as before. No stage, ordering or contract
changes; the pre-existing identity tests against the frozen `run_cycle` copies pass unchanged.

## 4. What an independent reviewer should check

1. `run_harness`: signals before the trial start are dropped, the flat padding bar, `count_same_bar_approvals=True`, the
   live `KILL_SWITCH` file cannot influence the replay (`risk_cfg["kill_switch_file"]` is overridden and the project root
   does not exist), and the harness `RiskEngine` patch is restored in `finally`.
2. The "explained" rules, especially `downstream_of_explained_mismatch` and `bot_gap`: are they too generous? The cumulative
   budget is the only bound on them.
3. Cumulative vs window: the status uses an unexplained mismatch in the **window** and the **trial-to-date** budget.
4. The size tolerance (25%) and whether an approved quote under 1e-6 USD should be `float_phantom` or a plain mismatch.
5. `_maybe_notify`'s `getattr` handling and that default messages are byte-identical.
