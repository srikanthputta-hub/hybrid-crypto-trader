# Live-bot bar parity: rules P1-P5 in `src/bot.py` (behind `loop.bar_parity`, default off)

- **Role:** Architect (Opus). `src/bot.py` orchestration, `src/config.py` key parsing, tests, docs.
  **Not touched:** `src/risk_engine.py`, `src/broker_live.py` (and the two-switch gate),
  `src/monitor.py`, `src/htf_filter.py`, `src/market_data.py`, any dataclass, `run_cycle`'s
  signature, the backtest harness. No config **value** changes: the new keys are spelled out in
  `config/config.yaml` at their off values.
- **Branch:** `claude/bot-bar-parity`, off `origin/main` at `fd5cd8e` (P6 merged). Local only.
- **Spec:** `docs/multiday_hold_design.md` §3.1 (P1-P5 and the cadence/htf rows), §5 (parity
  check), §6 (`src/bot.py` row, tests 1-8). Background: `docs/p6_harness_exposure_fix.md`.
- **Status:** the flags are off, so merging changes nothing in the running paper bot. Switching
  `loop.bar_parity` on, setting `max_entry_delay_minutes`, and moving to `market.timeframe: "1d"`
  are later, separately approved steps (design §8 decisions 3-5).

## 1. What was built

| File | Change |
|---|---|
| `src/config.py` | `BarParitySettings` + `parse_bar_parity_settings` (strict startup validation, below) and the `Config.bar_parity` property. |
| `src/bot.py` | `run_cycle`'s per-symbol body extracted into `_monitor_held_symbol` and `_evaluate_new_entry` with the exact original control flow (default path unchanged). New `_run_bar_parity_passes` (P1-P5), `BarDecisionLedger` (P1 state, persisted), `_archive_closed_bars`, `HTF_SCAN_TAIL_BARS = 150`. `build_components` validates the new keys first. `_active_plan_on_same_base` gets an optional, default-empty `also_taken` argument (P3). |
| `config/config.yaml` | `loop.bar_parity: false`, `loop.max_entry_delay_minutes: null`, `logging.bar_decisions_path: "logs/bar_decisions.json"`, `logging.closed_bars_dir: null`, with comments. |
| `scripts/validate_invariants.py` | New 6th check `bar_parity_config`: the committed config must parse with the bot's own startup parser. Tightening only; it does not require any on/off setting. Now prints 6/6. |
| `.gitignore` | `logs/daily_bars/` (top-level `logs/*.csv` did not cover a subdirectory). |
| `README.md` | Architecture diagram (bar gate, 2b at 1d, stage 5 close-only), cycle paragraph, config table, layout. |
| `tests/_pre_bar_parity_run_cycle.py` | Frozen copy of `origin/main`'s `run_cycle` (the "before" half of the default-mode identity tests). |
| `tests/test_bot_bar_parity.py` | 80 tests: validation, default-mode identity, P1-P5, gross same-bar accounting, ledger robustness, archive. |
| `tests/test_daily_live_parity.py` | Design test 7: `bot.run_cycle` vs `simulate_portfolio(count_same_bar_approvals=True)` on the committed Kraken daily data. |
| `tests/test_validate_invariants.py` | 11 tests for the new check. |

## 2. Semantics, rule by rule (and every deviation from the design text)

### Keys and validation (`parse_bar_parity_settings`, run in `build_components`)

| key | absent | valid values | rejected |
|---|---|---|---|
| `loop.bar_parity` | `false` | `true` / `false` (a real YAML bool) | `1`, `0`, `"true"`, `null`, anything else |
| `loop.max_entry_delay_minutes` | off | `null` (off), int/float, finite, `> 0`, timedelta-representable | `0`, negatives, NaN, inf, bool, strings, huge ints; **any non-null value while `bar_parity` is false** |
| `logging.bar_decisions_path` | `logs/bar_decisions.json` | non-empty string | `""`, `null`, non-strings |
| `logging.closed_bars_dir` | off | `null` (off) or non-empty string | `""`, non-strings; **any value while `bar_parity` is false** |

A dependent key without `bar_parity: true` is a startup error rather than silently ignored.
With `bar_parity: true` at `market.timeframe: "1d"`, `market.history_bars` (default 200) must be an
integer >= 150, or P5's htf window would be shorter than the backtest's (review item 4).
A `loop:`/`logging:` section that is not a mapping is also refused (it would crash later anyway).
**Deviation:** design §6's `src/config.py` row says "default 60 if absent" for
`max_entry_delay_minutes`. Here absent means **off**, because the task's merge-safety constraint
requires absent keys to reproduce today's behaviour. The design's own config row sets 60
explicitly in `config.yaml` at flip time, so nothing is lost.

### Cycle shape with `bar_parity: true` (`_run_bar_parity_passes`)

Market-data refresh, indicator enrichment and `equity` are exactly as before. Then:

- **Pass 1 (monitor), walk order.** Symbols without data: the usual "market data unavailable"
  event. Held symbols (a plan record exists): the bar is recorded as decided, then the unchanged
  monitor check runs (every cycle; idempotent within a bar since the price is the last close).
  FLAT symbols are deferred to pass 2.
- **Pass 2 (entries), `sorted(symbol)` order.** For each deferred symbol:
  1. **P1 bar gate:** if its latest closed bar is not newer than the recorded one, a
     `none` event with note "bar closing ... already decided ...", and nothing else.
  2. Otherwise the bar is recorded as decided **before** anything else happens.
  3. **P4:** if `max_entry_delay_minutes` is set and `now - bar_close` exceeds it: `none` event,
     `note: "stale bar: entries skipped"`, plus `entry_delay_minutes`.
  4. Stages 1-4b exactly as before, with P3 and P5 below.
- Still exactly one audit line per walked symbol per cycle. Every event of a symbol with data
  carries `bar_close` (ISO), for the weekly parity check. Held-symbol events come first, then
  entries in sorted order.

### P1: one decision per symbol per closed bar

- Key: the close time of the last closed bar (`timestamp.iloc[-1] + timeframe`, the cache frame's
  timestamps are bar OPEN times).
- "Decided" = a monitor check of a held symbol on that bar (trigger or not), or the start of the
  entry pass for a FLAT symbol, whatever its outcome (no signal, htf veto, risk rejection, stale
  skip, or an exception mid-pipeline). A failed fetch decides nothing, so the next tick with data
  decides the bar (subject to P4).
- Consequences: no re-scan of the same bar (design (a)/(c)); no re-entry on the exit bar, because
  the exit's monitor check already decided the bar (design (a)); no rate-cap refill on the same
  bar's stale signals (design (c)).
- **Persisted** (write-through, temp file + `replace`; `logs/*.tmp` is gitignored) to
  `logging.bar_decisions_path` as `{"timeframe": <market.timeframe>, "bars": {symbol: ISO bar close}}`. **Deviation (additive):** the design's §6 sketch keeps this in a
  module-level dict and relies on the staleness guard for restarts. The task requires
  persistence, so a restart mid-bar never re-decides a bar even with P4 off. The state is still
  module-level (keyed by file path), so `run_cycle`'s signature is unchanged.
- **Untrustworthy ledger content is treated as unreadable, never as a block** (review item 3):
  an unparseable file, the flat pre-timeframe form, a missing or different `timeframe` (e.g. a 1h
  ledger after switching to 1d, whose hourly closes would otherwise block every daily bar until
  the next day), and per entry a null/empty/non-string/unparseable/NaT value (NaT would make
  `is_new` False forever) or a time later than the exchange clock (no closed bar can be). Each is
  logged (warning) and that symbol's (or every symbol's) current bar is decided once more. With
  P4 on, a late re-decision is still skipped as stale. A failed write is logged and the
  in-memory state still gates the process. Naive timestamps are read as UTC.
- `scripts/reset_portfolio.py` deliberately does not archive the ledger: keeping it means a reset
  mid-bar does not let the fresh portfolio enter on a bar the old one already decided (design §5:
  trial day 0 is the first close after deploy).

### P2: entries after every monitor check, in `sorted(symbol)` order

`sorted()` over the full `BASE/QUOTE` strings. The harness sorts base names (`BTC`, `ETH`, ...).
For one common quote these orders are identical, because `/` sorts before every letter and digit
(`"A/USD" < "AB/USD"` exactly when `"A" < "AB"`). Mixed quotes for one base have no harness
equivalent (the per-base rule allows only one anyway).

### P3: room, cash and base as held at the bar close, before this bar's exits

For each STOP_LOSS/TAKE_PROFIT executed in pass 1 that actually removed the position, the cycle
remembers the position's value at the current close (`amount * close`, the same term the
harness's `invested_value` uses), the portfolio cash it added, and the symbol. Every entry risk
check in pass 2 then gets `open_positions_value + exited_value` and `cash - exit_proceeds`, and
the per-base rule treats the exited symbols' bases as still taken.

- **Cash and base go beyond the design text** ("room ... `open_positions_value`"). They are
  needed for exact parity: the harness's `cash_available` excludes exit proceeds until the next
  open, and its exiting symbol still holds its slot on that bar. Both can only shrink or block an
  entry, never enlarge one.
- **Same-cycle FILLs count at their gross approved quote** (review item 2). A fresh plan's
  immediate fill, or a PENDING plan's monitor FILL in pass 1, is already in `portfolio.cash` at the
  gross quote (`Portfolio.apply_buy` debits it), but the paper broker books the position at
  `quote*(1-fee)`. The P6 harness counts the gross quote. In bar-parity mode, every later entry
  check in the cycle adds `max(0, gross_quote - booked_value)` per same-cycle fill to
  `open_positions_value`. That is orchestration only (no change to risk_engine, portfolio or
  broker), and it can only shrink or block an entry. The default path is unchanged.
- `equity` is computed once per cycle before any exit, as before (= the harness's `equity[t]`).

### P4: entry staleness guard (`loop.max_entry_delay_minutes`)

`now` is the exchange client's clock (`exchange.client.milliseconds()`, the same clock
`MarketDataCache` and `fetch_ohlcv_df` use; for ccxt it is the host clock). Exits are never
gated (pass 1 has already run). A skipped bar stays decided, also across a restart. **The value
is untuned**: the design's 60 is a judgement call, and in paper the broker fills at the last close
whatever the delay, so the guard only changes which trades exist. Its purpose is live/manual
fidelity. The 0-60 min fill-delay cost must be measured on the 15m data before any real-money use
(design §4/§6).

### P5: htf on the scan frame at 1d

Only when `bar_parity` is on **and** `market.timeframe == "1d"`:
`htf_filter.daily_trend_confirmed_from_df(enriched.tail(150), fast, slow)`, no 1d fetch, event
field `daily_trend_source: "scan_frame"`. The gate recomputes its EMAs on exactly these 150
closes, the harness's daily-branch window (`SCAN_TAIL_BARS`; a test asserts the constants
agree), so the decision is exact. At 1h, and with the flag off at 1d, the 31-bar fetch wrapper is
unchanged. `htf_filter.py` is not modified.

### Closed-bar archive (`logging.closed_bars_dir`, design §6 item 5)

Small enough to include. Each cycle, for every symbol with data, bars newer than the last one
written are appended to `<dir>/<BASE>_<QUOTE>_<tf>.csv` (the whole fetched frame the first time:
200 bars of warm-up history), in the `strategy_pack_run/data` CSV format (`timestamp` = bar open
in epoch ms, `repr()` floats, exact round trip), so `backtest_live_bot.load_dir/load_csv` read it
directly. Bars missed while the bot was down are filled in from the next fetch. Never pruned
(`src/log_retention.py` only prunes the audit log and plan history). Bars are recorded as first
seen; a later exchange revision of an already-written bar is not rewritten (a residual for the
parity check to report, not hide). An archive failure is logged and never stops the monitor.
`scripts/paper_parity_check.py` (the reader) is the next PR, as in the design.

### Not done here (design §6 `bot.py` item 4)

The BUY-notification text (bar date, "valid until", "PAPER TRIAL" prefix) is display-only and
needs its own flag; left for the Platform Engineer with the dashboard follow-ups.

## 3. Default-mode evidence (flags off = byte-identical)

`tests/test_bot_bar_parity.py`, group 1, against the frozen `origin/main` `run_cycle`:

| replay | ticks | compared | result |
|---|---|---|---|
| today's `config/config.yaml` (16 symbols, 1h, htf on), random-walk fixture, bars 205-250 | 2 per bar (92 cycles) | every audit event (minus wall-clock fields), the exchange call log call-for-call, final cash (exact) and plan set | identical; replay has >= 5 plans, immediate fills, stop-losses, htf vetoes, risk rejections |
| held symbol outside the universe; per-base rule within a cycle; a stop that frees a base for another quote, each with `loop` absent and with an explicit `bar_parity: false` | 2 | every event | identical |
| committed Kraken 1d data (16 symbols), htf 31-bar fetch, 20 bars | 2 per bar | every event, call log | identical; the second tick of each bar re-scans (today's behaviour) |

Also: no `bar_close` field, no ledger file, no archive when off. The pre-existing
`tests/test_bot_universe_wiring.py` parity test against the pre-P1b `tests/_legacy_run_cycle.py`
still passes unchanged.

## 4. ON-mode evidence

### 4.1 Unit tests (fixtures; `tests/test_bot_bar_parity.py` group 2)

P1: scanned once per closed bar across ticks at +1/+5/+10/+55 min, again on the next bar; no scan
on the exit bar, scan on the next one (and the flags-off control re-scans); a decision made before
a restart (kill switch on) is not re-made after it (kill switch off); unreadable ledger; ledger
write failure. P2: `["XRP", "ADA"]` config order, cap 1 -> ADA wins (off: XRP); held symbol's
monitor event precedes an alphabetically earlier entry. P3: a 3500 USD position stopping out on the
bar -> the entry check sees `open_positions_value` 3500 and cash 6000 (pre-exit) and is rejected at
a 35% cap (off: approved); at 40% it gets exactly the 300 USD of pre-exit room; a same-bar exit
still holds its base. P4: 61 min -> "stale bar: entries skipped", stop still executes, skipped bar
stays decided (also after restart); 59 min -> planned; guard null -> planned. P5: at 1d no htf
fetch (only the market-data call), decision = pure function on the last 150 closes, on a fixture
where the 31-bar fetch gives the opposite answer (flags off: blocked); unchanged at 1h. Archive:
header + 50 bars, +1 per new bar, no duplicates across ticks/restarts, a missed bar filled in,
exact floats via `load_csv`; failure does not stop the monitor.

### 4.2 End-to-end vs the P6 harness (design test 7, `tests/test_daily_live_parity.py`)

Fake exchange serving `strategy_pack_run/data/*_1d.csv` bar by bar; `bot.run_cycle` with
`bar_parity: true`, `max_entry_delay_minutes: 60`, 1d, real config otherwise, two ticks per bar
(+1 and +6 min); harness `simulate_portfolio(..., count_same_bar_approvals=True)`, sorted order.
Compared: every risk check keyed by (bar, symbol) (approved, size, equity, entry, stop, R:R), the
per-bar order of checks, and every exit (decision bar, symbol, reason).

Results over the **full 719 bars** (scratch runs of the same code; the default test run uses the
first 130 bars, `HCT_FULL_PARITY=1` runs all 719: 5 passed). Rows A-B5 and B0/B1 were measured
before the gross same-bar accounting (review item 2); the "gross" rows after it:

| run | data | paper fee / harness fee | live history | min order (both) | risk checks H / B | entries H / B, mismatch | exits H / B, mismatch |
|---|---|---|---|---|---|---|---|
| **A (exact)** | gapless (open = prev close) | 0 / 0 | covers fixture | 0.01 | 721 / 721, same keys, all decisions equal (15 are the same "nothing left" rejection worded as "no room" vs "below minimum 0.01" for a ~9e-13 USD size; 1, SOL/USD 2026-03-17, is the same rate-cap rejection whose "next slot opens in" text prints a pandas `0 days 02:00:00` in the harness vs `2:00:00` in the bot -- the first version of the test compared that text verbatim and failed the full run; reasons are now compared up to the first `;`) | 133 / 133, 0% | 124 / 124, 0% |
| B5 (warm-up only) | gapless | 0 / 0 | 200 | 0.01 | 721 / 721 | 133 / 133, 0% | 124 / 124, 0% |
| B4 (fill price only) | real | 0 / 0 | covers | 0.01 | 721 / 721 | 133 / 133, 0% | 124 / 124, 0% |
| B3 (fee only) | gapless | 0.10% / 0.10% | covers | 10 | 722 / 722 | 132 / 132, 0% | 123 / 123, 0% |
| **B2 (realistic + floor)** | real | 0.10% / 0.10% | 200 | 10 | 722 / 722 | 132 / 132, 0% | 123 / 123, 0% |
| B1 | real | 0.10% / 0.10% | 200 | 1 | 721 / 720 | 133 / 135, **4.5%** (first: 2026-05-07, a live-only $1.008 ALGO approval) | 124 / 126, 3.2% |
| **B0 (committed config), net commit** | real | 0.10% / 0.10% | 200 | **0** | 715 / 533 | 135 / 174, **82%** (99 in common) | 125 / 160, **65%** |
| **gross, floor 0** | real | 0.10% / 0.10% | 200 | 0 | 715 / 702 | 135 / 137, **13.3%** (first: 2024-11-12 AVAX, a live-only 9.1e-13 USD phantom) | 125 / 128, 7.2% |
| gross, floor $0.01 | real | 0.10% / 0.10% | 200 | 0.01 | 720 / 720 | 134 / 134, **0%** | 125 / 125, 0% |
| gross, floor $1 | real | 0.10% / 0.10% | 200 | 1 | 721 / 721 | 133 / 133, **0%** | 124 / 124, 0% |
| gross, floor $10 | real | 0.10% / 0.10% | 200 | 10 | 722 / 722 | 132 / 132, **0%** | 123 / 123, 0% |
| gross, zero fee, floor 0 | gapless | 0 / 0 | covers | 0 | 706 / 707 | 135 / 134, 11.1% (first: a live-only 9.1e-13 phantom) | 125 / 125, 6.4% |

"Mismatch" is the design §5 metric: symmetric difference of (bar, symbol) entries over the
harness count. In B2-B5 sizes differ slightly (fill price, warm-up ~1e-7), decisions do not.

What this shows:
1. **Structural parity holds exactly** (run A): P1-P5 plus P6 make the live loop decide the same
   symbols, on the same bars, in the same order, at the same sizes, with the same exits, as the
   harness that produced the evidence. The second tick of every bar scans nothing, and no htf
   fetch happens.
2. **The fill-price residual, the paper fee and the 200-bar warm-up each change no decision**
   over 719 bars once dust is excluded (B2-B5).
3. **With net commit (the first version) and the committed `min_order_quote_usd: 0`, the live
   loop did not stay within the design's 10% budget (B0).** The first divergence (2024-11-08, bar ~27) is a
   live-only approval of a $0.999 and then a $0.001 plan: a paper position is worth
   `quote*(1-fee)`, so ~0.1% of every same-bar fill reappears as exposure room in live only (P6
   doc §3 predicted this). Each dust plan holds its symbol's plan slot until it closes, the
   portfolios diverge, and it cascades. This is not a defect of P1-P5; it is the live/harness
   accounting residual that P6 deliberately left (gross vs net). The independent review proved the
   whole gap is this: with the harness patched to commit net-of-fee, the mismatch is 0.0%.
4. **Fixed in bar-parity mode by committing same-bar fills gross (the orchestration-only
   alternative, §2 P3).** Floor 0 drops from 82% to 13.3% (entries) and 65% to 7.2% (exits).
   What remains is **float phantoms**, not fee slack: when the room is used up exactly, it comes
   out at ~1e-12 USD on one side and 0 on the other (different summation order), and with no order
   floor the side with 1e-12 approves it as a full plan, which holds a slot and cascades. It
   happens at zero fee too (last row). Any positive floor removes it: $0.01, $1 and $10 all give
   0% over 719 bars. `tests/test_daily_live_parity.py` pins that the floor-0 first divergence is
   such a phantom (quote < 1e-6) and every earlier decision agrees, and checks the budget at $1
   and $10.
5. **A small `risk.min_order_quote_usd` floor therefore remains advisable before the §5 trial**
   (even a cent removes the phantoms; the P6 doc's ~$10 also covers exchange minimums). Not
   changed here: it is a risk-parameter change owned by a separate study (written reason + gate
   run; the harness already mirrors the key).

## 5. Mutation check

Each guard broken by hand (`/tmp` script, applied to `src/bot.py`, tests run, file restored and
`git diff` confirmed clean):

Unit tests = `tests/test_bot_bar_parity.py` (60 tests at M1-M15; M16-M22 run it plus
`tests/test_validate_invariants.py`, 146 tests); e2e = the exact 130-bar
`test_live_loop_matches_p6_harness_exactly_on_gapless_zero_fee_data`.

| mutation | unit tests failing | e2e exact test |
|---|---|---|
| M1 P1 bar gate never blocks | 5 | fails |
| M2 P1 held symbol's bar not recorded (re-entry on exit bar) | 2 | not run |
| M3 P1 ledger never read back (no persistence) | 2 | not run |
| M4 P2 config order instead of sorted | 1 | passes (its symbols are configured sorted, so it cannot see this; the unit test does) |
| M5 P2 entries inline in walk order (no monitor-first pass) | 7 | fails |
| M6 P3 exited value not credited to room | 2 | fails |
| M7 P3 exit proceeds not removed from cash | 1 | passes (cash never binds on this data; the unit test pins it) |
| M8 P3 exited base freed for another quote | 1 | not run |
| M9 P4 guard never fires | 1 | not run |
| M10 P4 stale bar not recorded as decided | 5 | not run |
| M11 P5 31-bar fetch at 1d | 1 | fails |
| M12 P5 window 31 instead of 150 | 1 | fails |
| M13 archive writes duplicates | 1 | not run |
| M14 parity path taken with the flag off | 12 (all default-identity tests) | not run |
| M15 P3 exit bookkeeping disabled | 4 | fails |
| M16 same-bar fill not counted at its gross quote | 2 | not run |
| M17 PENDING monitor FILL not counted gross | 1 | not run |
| M18 ledger NaT accepted | 1 | not run |
| M19 ledger future time accepted | 1 | not run |
| M20 ledger timeframe ignored | 2 | not run |
| M21 history_bars >= 150 not checked (`src/config.py`) | 2 (incl. `validate_invariants` test) | not run |
| M22 ledger empty/non-string value accepted | 1 | not run |

Every mutation is caught by at least one test.

## 6. Residual risks and notes

- **Float phantoms** (§4.2): with the gross same-bar accounting, the only measured source of
  live/harness decision divergence. Removed by any positive `min_order_quote_usd` (a separate
  risk-parameter decision).
- **P3 only credits exits from the same cycle.** If a FLAT symbol's bar is first decided on a
  later tick than a same-bar exit (e.g. its fetch failed on the first tick after the close), that
  later cycle no longer knows about the exit, so the symbol sees the freed room and cash. This
  happens in live only (the harness decides every symbol at once) and is bounded by P4 when it is on.
- **Live-only P3 gap on partial LiveBroker sells.** If a live sell fills only partly, the position
  stays (so P3 credits nothing) while cash has grown, so the freed room leaks to same-bar entries.
  Not reachable in paper (PaperBroker sells the whole position), and live mode is untouched.
- **The ledger is last-writer-wins.** Two processes on the same `bar_decisions_path` (e.g. the VM
  loop and a manual `--once`) can overwrite each other's marks, which re-decides a bar. Run one
  process, as `scripts/run_live.py` already requires.
- **Timeframe switch.** A ledger written at another timeframe is ignored at startup (logged), so
  after 1h -> 1d every symbol's current daily bar is decided once more. Do the switch right after
  00:00 UTC (or with P4 on) so that this re-decision is the day's normal one.
- **PENDING fills ordering.** The harness interleaves a PENDING plan's monitor FILL with the
  bar's new-entry checks in sorted order; live runs every monitor check first (design P2). At 1d
  a fresh plan always fills at once (its entry zone contains the close), so a PENDING plan only
  exists after a crash between `set_pending` and `buy`; then live is stricter for entries sorted
  before it. Never occurs on the datasets.
- **Turning `bar_parity` on at 1h changes the 1h paper bot too** (one scan per hourly bar instead
  of ~12; sorted order; pre-exit room). That is the point of the rules, but it is a behaviour
  change of its own: flip it together with the 1d switch, or on its own only with approval.
- **Clock.** P4 uses the host clock via ccxt; NTP drift of minutes is irrelevant at a 60-min
  guard. The exchange's bar timestamps are trusted as bar opens (as `fetch_ohlcv_df` already does).
- **Ledger loss** (deleted/corrupt/untrustworthy file) during a bar re-decides that bar once; with
  P4 on it is then skipped as stale unless within the delay.
- **Startup strictness:** `build_components` now raises on a malformed `loop:`/`logging:` section
  or bad bar-parity key, before anything else. `validate_invariants.py` check 6 catches the same in
  CI (6/6 instead of 5/5).
- **Archive revisions:** first-seen bars only (above). The first file write includes the 200-bar
  history the first decision used.
- **At 1d, cooldown and the daily-loss halt are inert** (design §3.1, unchanged, flagged for the
  Risk Officer).
- **Test runtime:** root suite +~3 min (identity replays and the 130-bar e2e runs).

## 7. What an independent Opus reviewer should scrutinise

1. The extraction of `run_cycle`'s body (`_monitor_held_symbol`, `_evaluate_new_entry`): the
   early-return/record/except structure must equal the old `continue` structure; the identity
   tests compare every event, but read it for paths the fixtures don't hit (advisor enabled,
   exceptions inside `_record`).
2. P3 credit: computed from `portfolio.cash` deltas and position removal around
   `_execute_monitor_action`, so a failed sell credits nothing. Check the LiveBroker path would
   behave the same (it updates the same `Portfolio`; not exercised, live mode untouched).
3. "Decided" timing: marked before the scan and before the staleness check (an exception
   mid-pipeline does not retry the bar). Confirm this is the wanted trade-off versus retrying a
   transient error.
4. The e2e test's equivalence rule for float residue (`same_decision`): only "no room" vs
   "below minimum 0.01" for a sub-cent size counts as equal.
5. The B0 finding and the `min_order_quote_usd` recommendation (§4.2 item 4).
6. Deviations: P4 default off; persistence of P1; P3 also covering cash and base.
