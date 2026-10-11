# Weekly paper-trial parity check and Telegram labelling

- **Role:** Platform Engineer (Sonnet). `scripts/paper_parity_check.py` (new, read-only), tests,
  `notifier.label` in `src/config.py` / `config/config.yaml` / `src/notifier.py`, a 7th tightening check in
  `scripts/validate_invariants.py`, and a 19-line change in `src/bot.py` (`_maybe_notify` + one validation line in
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
   `risk.starting_cash_usd` portfolio at the **trial start** = the earliest fully logged `bar_close` in the audit log (or
   `--trial-start`; a mid-bar time such as the moment of a retention cut is fine: the replay starts at the first audited bar
   at or after that time's bar boundary, and a first bar that only some symbols survive is skipped).
3. **Trial protocol: set `logging.retention_days: 0`** (keep everything) before the trial starts. The audit log is pruned
   after 30 days by default, but the comparison is path dependent (the harness portfolio must start where the bot's did) and
   the design's 10% budget is cumulative over the trial. `trades.csv` is never pruned, so if its first row is older than
   the audit log's first line the check stops with exit 2 and tells you to pass `--trial-start <first close of the trial>`
   (compare from where the audit log starts) and to set `retention_days: 0`.
4. **`risk.min_order_quote_usd` must be > 0** (the min-order study, #102, recommends 10; that decision is the user's and is
   not made here). With the committed 0, a *correct* bot hits float-phantom divergences (`docs/bot_bar_parity.md` §4.2:
   about 1e-12 USD "room" approved as a full plan on one side only) that cascade; over a 110-bar real-loop run they were
   tagged `float_phantom` and counted, and over 719 bars they exceed the 10% budget. The check prints a note when the
   floor is 0; it does not special-case the phantoms away.
5. The config passed to `--config` is the config the bot ran with (the trial freezes it).

### What is compared (per bar close and symbol, over the trial to date; the window is the part reported as "this week")

| Item | Rule |
|---|---|
| Entry check | harness vs bot: signal **pattern**, allowed or rejected, rejection reason **up to the first `;`** (the `same_decision` equivalence of `tests/test_daily_live_parity.py`: clock-dependent text, cooldown remaining time, "no room" vs "below the minimum" for a ~1e-12 USD size; **plus** the sized amount in "sized order X is below the minimum order size N" is ignored, because it is the leftover room, which carries the fill-price residual: the decision "below the minimum N" is what must match), plan entry / stop / risk-reward to a relative **1e-6** (design: live indicators use a 200-bar fetch, the replay all archived bars, so warm-up leaves ~1e-7), approved size within 10% or 1 USD (fill-price residual; the largest drift seen is printed; measured: 0.5% over 110 bars, <= 7.5% on the reviewer's runs) |
| Entries | harness-approved (bar, symbol) vs bot `planned`; design metric = symmetric difference / harness count |
| Exits | (decision bar, symbol, stop_loss or take_profit) harness vs bot. A flat padding bar is appended after the newest bar so an exit decided on the newest bar is visible (the harness executes exits at the next open) |
| Integrity | a bar decided or scanned twice (P1), a deciding line lost while its "already decided" echo survives, audit fills missing from `trades.csv`, and every `trades.csv` row written at or after the first audited line that has no audit fill (so a lost audit tail shows up) |

Each mismatch is listed with both sides and a reason. `explained_by` is set only when the audit log itself proves a
documented cause:

- `staleness_guard`: the bot's event says `stale bar: entries skipped`.
- `bot_gap`: **no symbol** has a decision event for that bar (the bot was not cycling). One symbol missing from a bar the
  bot otherwise decided is *not* a gap (unexplained); a deciding line lost while its "already decided" echo survives is an
  integrity failure (`missing_decision_event`).
- `fetch_failure`: that symbol has a `market data unavailable` event that day.
- `kill_switch`, `advisor_veto`, `float_phantom` (an approved quote under 1e-6 USD).
- `downstream_of_explained_mismatch`: only for portfolio-dependent kinds (entry decision, size, rejection reason, exits, and
  one-sided checks that are explained by one side holding a position), only after an earlier *explained* mismatch. **Never**
  for `pattern` or `plan_levels` (they depend on the bars alone; the design requires 100% there), never after an unexplained
  mismatch, never for integrity items.
- `acknowledged`: see below.

Anything else is unexplained.

**Unexplained mismatches pause the trial until fixed**: the status is FAIL while the *trial-to-date* count of unexplained
mismatches is above zero, even after the mismatch has left the 7-bar window. Once the cause is fixed, acknowledge it
explicitly: `--ack 2026-10-12:BTC/USD` (repeatable) or `--ack-file acks.txt` (one `BAR:SYMBOL` per line, `#` comments). Acks
are listed in the report, an ack that matches nothing is reported, and an ack never explains what followed the mismatch.

### Status and exit codes

| Status | When | Exit |
|---|---|---|
| `PASS` | no mismatch in the window and no unexplained mismatch anywhere in the trial to date (flagged `vacuous` if neither side made an entry or exit decision) | 0 |
| `WARN` | mismatches in the window, all explained or acknowledged, trial-to-date rates within the budget (or the budget not yet evaluable) | 0 |
| `FAIL` | any unexplained mismatch in the trial to date (**pauses the trial**), or a trial-to-date entry / exit / overall mismatch rate above the budget (default 10%, `--budget`) | 1 |
| `INSUFFICIENT DATA` | empty audit log, no `bar_close` in it (bar parity was off), or fewer than `--min-bars` (3) decided bars in the window | 0 |
| input error | missing / unreadable / corrupt audit log, ledger, trade log, config or bar archive; a decided bar missing from the archive; **the bar-decision ledger ahead of the audit log (the END of the audit log is missing: truncated, copied mid-write or lines lost)**; the trade log older than the audit log (pruned); a timeframe other than 1d; a bad `--ack`; a bad `--json-out`. The message names the file and why | 2 |

**The budget and early strictness.** The pre-registered rule (design §5) is "at most 10% explained mismatches" and one
explained mismatch among the first ~10 entries already exceeds that. **Proposed tightening of that early strictness, needing
the user's acceptance:** by default the budget is evaluated only once the trial has `--budget-min-entries` (20) harness
entries; before that the report says "budget not yet evaluable" (unexplained mismatches still FAIL, explained ones WARN).
`--strict-early-budget` restores the pre-registered rule from the first entry.

### Example (short fixture: the real bot loop on the committed Kraken daily data, 15 bars; `tests/test_paper_parity_check.py`)

```
PAPER TRIAL PARITY CHECK -- PASS
the bot's decisions match the harness's
window: 2025-09-28 .. 2025-10-12  (15 decided bars, 16 symbols)
trial start: 2025-09-28 (earliest bar_close in the audit log)
entries : harness 7, bot 7, matched 7, mismatched 0; agreement 100.0%, mismatch 0.0% (budget 10%)
exits   : harness 7, bot 7, matched 7, mismatched 0; agreement 100.0%, mismatch 0.0% (budget 10%)
decisions compared: 16; mismatches: 0 (unexplained 0); overall mismatch rate 0.0%
trial to date (since 2025-09-28): entries mismatch 0.0% (0/7), exits mismatch 0.0% (0/7), 0 mismatches (0 unexplained) in 16 decisions
paper fee assumed: 0.10%; max approved-size drift vs harness 1.1%; budget: <= 10% mismatches (not yet evaluable), an unexplained mismatch pauses the trial
note: budget not yet evaluable: 7 harness entries to date, evaluated from 20 (...)
```

Long run: the same real loop over **110 bars** (2025-05-01 .. 2025-08-18, 16 symbols, real opens, 0.10% fee, the live 200-bar
history, `min_order_quote_usd: 10`) also gives PASS: 27 entries and 21 exits agree 100%, max size drift 0.5%
(`test_a_correct_bot_passes_over_110_real_loop_bars_at_a_10_dollar_floor`). Before the sized-order normalisation this run
FAILED a correct bot with 23 `rejection_reason` mismatches. With the committed floor 0 the same run gives WARN with one
`float_phantom` mismatch on 2025-08-14 and the floor-0 note; over 719 bars the phantoms exceed the budget (§4.2 of
`docs/bot_bar_parity.md`).

With one plan's stop 2% off in the audit log (exit code 1):

```
PAPER TRIAL PARITY CHECK -- FAIL
1 unexplained mismatch(es) in the window: an unexplained mismatch pauses the trial until fixed
...
  2025-10-02  BTC/USD    plan_levels          [UNEXPLAINED]
      entry/stop/rr differ beyond rel 1e-06: harness (118848.28057627, 114625.81077963, 2.0) vs bot (118848.28055043, 116918.32741690081, 2.0)
```

### Limits

- Cost: one run replays the harness over the whole archive (about 15 s for 216 bars x 16 symbols; it grows with the trial).
- The audit log does not record equity, so equity / cash are compared only through their effect (approvals and sizes).
- The 1e-6 level tolerance and the 10% / 1 USD size tolerance are options (`--level-rtol`, `--size-rtol`, `--size-atol`); the size
  tolerance exists because the paper broker fills at the close and the harness at the next open.
- It checks implementation parity, not strategy edge (design §5). Kill thresholds and the end-of-trial bootstrap are
  separate (Architect reports from `logs/trades.csv`).
- A restart or outage shows up as `bot_gap` only if it spans a whole bar (no symbol decided); a late decision is
  `staleness_guard`; anything partial is unexplained and pauses the trial until acknowledged.

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
  daily close. It is the **staleness window for taking the entry by hand**, not an expiry of the paper plan: a PENDING plan
  can still fill on a later bar if price re-enters the entry zone. Other messages never get these lines.
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

0. The new behaviours: ledger-ahead exit 2, trade-log/audit-tail check, acknowledgements, `bot_gap` proof rules, downstream eligibility (`PORTFOLIO_KINDS`), sized-order normalisation, budget gating (20 entries) and its flag.
1. `run_harness`: signals before the trial start are dropped, the flat padding bar, `count_same_bar_approvals=True`, the
   live `KILL_SWITCH` file cannot influence the replay (`risk_cfg["kill_switch_file"]` is overridden and the project root
   does not exist), and the harness `RiskEngine` patch is restored in `finally`.
2. The "explained" rules, especially `downstream_of_explained_mismatch` and `bot_gap`: are they too generous? The cumulative
   budget is the only bound on them.
3. Cumulative vs window: the status uses an unexplained mismatch in the **window** and the **trial-to-date** budget.
4. The size tolerance (10% / 1 USD) and whether an approved quote under 1e-6 USD should be `float_phantom` or a plain mismatch.
5. `_maybe_notify`'s `getattr` handling and that default messages are byte-identical.
