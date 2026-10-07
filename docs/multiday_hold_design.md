# Multi-day hold redesign of the live strategy (design only)

- **Role:** Architect (Opus). Docs only. Nothing under `src/`, `config/` or `strategy_pack_run/*.py` was
  changed. **No live or paper config change is made or implied here. Every change below needs the
  user's explicit approval** (CLAUDE.md trading rules).
- **Branch:** `claude/multiday-hold-design`, off `origin/main` at `65a5bbe`, with the P2.0 diagnosis
  commits (`701cec8`, `3211007`) cherry-picked. Not pushed.
- **Inputs:** `docs/p2_0_1h_rebaseline.md`, `docs/p2_0_1h_edge_diagnosis.md` (and its §8 review
  corrections, added on this branch), `docs/p1c_rank_signals_validation.md`, `docs/p1e_universe_gate_run.md`,
  `docs/engine_score_saturation.md`, `docs/crypto_bot_upgrade_playbook.md`, the system-redesign draft
  (`docs/system-redesign-2026-10.md`; not on `main`, cited by section), `config/config.yaml`, `src/bot.py`,
  `src/monitor.py`, `src/trade_planner.py`, `src/htf_filter.py`, `src/market_data.py`.

## 0. Bottom line

1. **What "multi-day hold" should mean for the live bot: Option A.** Run the existing, unchanged
   pipeline on **daily bars** (`market.timeframe: "1d"`). Decide once per symbol per closed daily bar,
   right after 00:00 UTC. Stops are 1.5 x daily ATR, which is exactly the configuration all of the daily
   evidence was produced with. This needs no new pipeline stage and no dataclass contract change. It does
   need one real `bot.py` change: five parity rules (§3.1), chiefly "one decision per symbol per closed
   bar" and an entry-staleness guard. Without them, the live bot would re-scan the same daily bar about
   288 times a day, and its same-bar processing order and exposure accounting would differ from the
   backtest.
2. **Right now, independent of A: stop treating the 1h bot's signals as tradeable (Option C as an
   interim step).** This is the one strongly supported conclusion. The live 1h config's net expectancy at
   `kraken_T3_taker` is **-0.24R, 95% CI [-0.35, -0.13]**: negative, with the whole CI below zero. If the
   user trades the Telegram BUY signals by hand, that is the expectancy they get.
3. **Option B (1h scan, daily-ATR stops) is rejected.** Its proxy (1h `stop_atr_mult` 7.0) does no
   better than plain daily: +0.21R, CI-low +0.005, a single point whose neighbours fail. It costs a
   planner contract change, and it can't be validated beyond 2 years.
4. **The evidence for A is marginal, and this doc does not claim otherwise.** A passes 5/5 on 11 years
   of survivor coins. It fails CI-low on the recent 2 years (+0.18R, CI [-0.08, +0.46]), and that
   window's profit comes from one quarter (Q4-2024). At the 2-year edge, statistically confirming it
   would take about **360 trades, about 4.7 years** of forward trading. **So the proposed paper trial
   (§5) can catch implementation divergence and falsify a broken strategy. It cannot prove an edge
   unless the true edge is near the 11-year level.** Real-money use stays the user's risk decision either
   way.

## 1. What the evidence does and does not support

All numbers are `kraken_T3_taker` (0.38% fee + 0.05% slippage per side; spot, so no funding),
fixed-rule 70/30 split, 200-sim time-shift placebo. Each was re-run on this branch unless marked.

| # | Evidence | Result | Supports | Does not support |
|---|---|---|---|---|
| E1 | 1h re-baseline, live config (P2.0; re-run) | 959 trades, **-0.24R [-0.35, -0.13]**, 2/5 | 1h at today's geometry has negative net expectancy. Strong, since the whole CI is below 0. | "1h signals are information-free". Pre-cost they beat random timing (isolated: +0.083 vs -0.018R). |
| E2 | 1h `stop_atr_mult` 3.0/4.5/7.0 (+6.0/8.0 post hoc) (re-run) | 2/5, 3/5, **4/5 (CI-low +0.005)**, 4/5, 2/5 | Wider stops cut cost drag as about 1/width. | Any specific multiplier. No plateau: 6.0/8.0 give +0.07/+0.08R. $-weighted R at 7.0 is only +0.05. |
| E3 | Daily replay, `data/` = Kraken 1d, 2024-10 to 2026-10 (re-run) | 154 trades, **+0.18R [-0.08, +0.46]**, 4/5 (CI-low FAIL), placebo 97.5/91, later-30% +0.42R | A positive point estimate on the venue's own daily bars, with timing beating the placebo. | A statistically established edge. **By entry year: Q4-2024 29 trades +1.32R; 2025 64 trades -0.32R; 2026 61 trades +0.17R; ex-Q4-2024 -0.08R.** |
| E4 | Daily replay, `data_long/` = Coinbase 1d, 2015-2026 (re-run) | 562 trades, **+0.52R [+0.36, +0.68]**, 5/5, placebo 100/98 | Long-run multi-regime edge for this coin set. | Forward expectation. Same 16 survivors. Random entries in htf-up periods already earn **+0.20R net**. Losing years: 2018, 2022 (-0.58R), 2025 (-0.33R). |
| E5 | P1e, static-16 on the same harness (rank order + tiered slippage) (not re-run) | `kraken_720_universe` 134 trades +0.15R [-0.14, +0.46] 4/5; `long_universe` 572 trades +0.52R 5/5 | E3/E4 are robust to harness version. | Universe expansion. Added coins earned 0.23R vs 0.55R, which is consistent with survivorship inflating E4. |
| E6 | Per-signal daily, every htf-confirmed signal isolated (this review) | `data/`: +0.112R gross, **+0.02R net** (n=1480); non-overlapping +0.22R net (n=225) | The 2-year daily edge depends on which signals the one-plan-per-symbol rule takes. | A broad per-signal edge on the recent window (diagnosis §8 R2). |
| E7 | Cost sensitivity, daily (re-run) | `kraken_T1_taker` (0.80% fee): `data/` +0.09R [-0.18, +0.36]; `data_long/` +0.44R [+0.28, +0.60] | A survives a 2x fee on 11 years. | A on the recent window at retail-tier fees. |
| E8 | htf parity at daily (this review) | 31-bar live fetch vs the harness's 150-bar window: ~3% of signals differ (`data/` 26 vs 24, `data_long/` 93 vs 91). Replay with the live behaviour: `data/` 150 trades +0.22R, `data_long/` 595 trades +0.47R [+0.31, +0.62] | Verdicts don't hinge on it. | Running live at 1d **unchanged** and calling it "the backtested strategy". The trade lists differ. |

**Survivorship and regime, stated once.** Both static datasets are today's 16 survivors. The strategy is
long-only trend/momentum. It made money in 2017, 2019-21 and 2023-24, and lost in 2018, 2022 and 2025.
That is the playbook's "works in bull markets only" profile. Nothing here tells us which regime comes
next.

## 2. Independent verification of the diagnosis (summary; details in its §8)

- **Reproduced exactly:** every table in §1-§5. That includes the per-day-held gross figures for
  portfolio trades (0.102% / 0.148% / 0.131%), mean hold of 50h vs 411h / 477h, and 0.21% gross per 1h
  trade against a 0.86% round-trip cost (2 x (0.38% + 0.05%)). The three pre-registered `stop_atr_mult`
  results (-0.05 / +0.05 / +0.20 vs the predicted -0.06 / +0.05 / +0.18) and all six gate tables, placebo
  percentiles included, also match. The trade lists are R-identical to the original cache.
- **Overstated:** (R2) the "same edge per day held at both resolutions" rate holds only for
  portfolio trades. On full signal sets, 1h earns 0.20%/day and daily earns about 0.0%/day on the
  2-year window. (R3) Model I is disfavoured, not rejected: the 3.0/6.0/8.0 CIs contain its
  predictions, and the variants aren't independent tests.
- **Missing context:** (R4) E3's profit is concentrated in Q4-2024.
- **Unchanged:** the diagnosis's conclusion that 1h has no edge at today's geometry, and its
  cost-geometry mechanism.

## 3. Options

### 3.1 Option A: run the existing pipeline on daily bars

| Aspect | What changes at `market.timeframe: "1d"` | Design decision |
|---|---|---|
| **Cadence** | Inputs change once a day (Kraken 1d bars open and close at 00:00 UTC, checked live on 2026-10-07). `run_cycle` currently scans every FLAT symbol on **every** 300 s tick. At 1d that is about 288 re-scans of the same closed bar. Three divergences from the harness, which decides each symbol exactly once per bar: (a) **re-entry on the exit bar**: a stop fires at 00:05, and at 00:10 the now-FLAT symbol is scanned again on the same bar. The harness's earliest re-plan is the next bar. (b) **Room freed by exits on the same bar**: symbol A is rejected "no room", then B stops out, and A is accepted 5 min later on the same bar. Within one cycle, a symbol later in the loop already sees B's freed room (rule P3 below). (c) **The new-entry cap (5 per 120 min) refills every 2h**, so the same bar's stale signals can be accepted all day. | **One decision per symbol per closed bar.** A FLAT symbol is scanned only on the first cycle that sees a new closed bar for it. If that same cycle runs a monitor action on the symbol, the symbol is done for that bar. Monitor checks keep running every tick (idempotent, since the price doesn't change within a bar). Timeframe-agnostic: this also fixes the same divergence at 1h. |
| **Fill timing / paper broker** | The paper broker fills at `current_prices` = the **last closed close**. The harness fills at the next open. For 24/7 crypto, the 00:00 open is roughly the prior close, so fills in the first cycle (within 5 min) match. A plan created hours later would book a fictitious stale price. A live market order would fill at a real price the plan never saw. | **Entry-staleness guard:** no new plan if `now - bar_close > max_entry_delay_minutes` (proposed 60). Exits are never blocked (risk-reducing). This covers restarts: after a restart the in-memory cache is empty, every symbol "sees a new bar", and the guard stops stale entries. |
| **Monitor triggers** | Close-only, checked once per daily close (the price is the last daily close). This is exactly what E3/E4 model. An intraday crash that recovers by the close won't stop out. One that closes below the stop exits at about 00:05. Realized stop-loss is -1.23R net on average at daily (diagnosis §4). | Unchanged. An intrabar "catastrophe stop" would be a **new strategy element** needing its own pre-registered backtest (the 15m dataset can model it). Not part of A. |
| **htf_filter** | At 1d the gate tests the same frame and EMAs as the scanner. It passes about 95% of signals and 100% of PULLBACK / TREND_CONTINUATION, so it is mostly redundant, but it is in the evidence (gate OFF: +0.12R vs ON: +0.18R on `data/`). Live it fetches only 31 daily bars, while the harness's daily branch uses the 150-bar scan window (E8). | **Keep it enabled.** At `timeframe == "1d"`, `bot.py` evaluates `htf_filter.daily_trend_confirmed_from_df(enriched, ...)` on the scan frame, with no fetch. That reproduces all on-record daily evidence exactly and saves 16 calls a day. (Alternative: keep the 31-bar fetch and re-gate the E8 variant first.) |
| **ATR / stop semantics** | ATR(14) is daily. The median stop is about 10% of price (vs 3% at 1h). Risk sizing of 1% equity / 10% stop gives about 10% notional, so `max_position_pct` and the risk budget bind about equally. Dust fills fall from 62% to 8%. | No planner change. `stop_atr_mult: 1.5` was chosen **on daily data** (config comment), so A is the setting it was evidenced for. |
| **Risk gates at daily resolution** | `cooldown_minutes` (60) and `max_daily_loss_pct` (5%) are **structurally inert**. The daily baseline resets at 00:00 UTC, right before the day's only entry pass. They are also inert at 1h (diagnosis §4). `max_volatility_pct` (8%) becomes **active** (97 rejections on `data/`) and the volatility taper applies. Both are in the evidence. The new-entry cap becomes "at most 5 entries per daily bar". | `risk_engine.py` untouched. Flag for the **Risk Officer (Opus)**: at 1d no live circuit breaker can fire within a day. A drawdown halt (playbook Layer 7) would be a new risk parameter needing its own written reason and OOS test. It is not needed for parity and not proposed here. |
| **Cost model** | Cost drag is about 0.09R/trade at T3 taker vs 0.33R at 1h. At about 6.4 trades/month, fees are a small share of R. | Evaluate the trial at the user's **actual** Kraken fee tier (E7 shows T1 taker is materially worse on the 2-year window). |
| **Data fetch / MarketDataCache** | Already bar-close aligned. It refetches once `last_open + 2 x tf` has passed, i.e. at 00:00 UTC for the just-closed bar, and drops the forming bar. `history_bars: 200` is at most Kraken's 720 and at least the scan tail (150) plus EMA warm-up. There's no settle delay, so a fetch at 00:00:05 may miss the last seconds of trades. That is negligible at 1d. | No `market_data.py` change. The bar gate reads each symbol's last closed bar timestamp (`df["timestamp"].iloc[-1]`). |
| **Dashboard / alerts** | The snapshot "current price" is up to 24h old. P&L defaults to hourly buckets. Telegram sends about 6 BUY messages a month, each right after 00:00 UTC. | Display-only follow-ups for the Platform Engineer: label prices "as of last daily close", default to daily buckets, put the bar date and "valid until" in BUY messages. |
| **Pipeline shape / contracts** | Same five stages plus 2b. No dataclass field changes. `run_cycle`'s signature is unchanged (bar-gate state is module-level, like `htf_filter._cache`). | **Not a new stage.** This is a cadence rule inside `bot.py` orchestration, which is where per-cycle sequencing already lives. |

**Parity rules. The live loop conforms to the harness, not the other way round, because the harness is
what produced E3/E4.** Changing harness semantics would put every gate result back in question.
- **P1:** one decision per symbol per closed bar (above).
- **P2:** within a bar, new-entry decisions run in `sorted(symbol)` order, after every monitor check
  for that bar. Live code today walks `market.symbols` config order. That changes who wins the 5-entry
  cap and the exposure room whenever more signals than slots arrive on a bar. sorted() vs config order
  has never been gated (P1c only gated `rank_signals`), so matching the evidence is the only defensible
  choice.
- **P3:** room for new entries (`open_positions_value`) is computed against positions held **at the bar
  close, before that bar's exits execute**. The harness queues exits to the next open and decides
  entries with the exiting position still counted. Live code today sells inside the per-symbol loop, so a
  later symbol in the same cycle sees freed room.
- **P4:** the staleness guard.
- **P5:** htf on the scan frame at 1d.
- **Known residual divergences, measured by test 7, not fixed:** the fill price (last close vs next
  open), and the harness's same-bar cash-race clip (its docstring note 5).

P2 and P3 together turn `run_cycle` into "monitor pass, then entry pass". That is the same shape as the
redesign draft's planned three-pass cycle, minus ranking.

### 3.2 Option B: keep the 1h scan, size stops and targets off daily ATR

What it is: "the daily strategy with hourly entry timing" (diagnosis §5.4).
- **Evidence:** only the proxy, 1h `stop_atr_mult` 7.0, on 2 years. 226 trades, +0.21R, CI-low +0.005,
  4/5, Sharpe 0.25 (below buy-and-hold), $-weighted R +0.05. Its neighbours (6.0/8.0) give +0.07/+0.08R.
  The real formula (daily ATR) has **never been run**. No 1h history exists for the 11-year window
  (the deferred fetch is about 20k requests).
- **Costs:** a `trade_planner.build_plan` signature change (an additive `stop_atr` kwarg, which still
  needs an Architect contract review). `bot.py` and the harness both need a per-signal daily-ATR feed. It
  keeps the 1h signal mix (REVERSAL, 5-hour "momentum"), runs 24x the decision passes, and keeps the 40%
  exposure cap permanently saturated.
- **Verdict: reject.** It is more code and weaker evidence for something that matched, but didn't beat,
  plain daily on the one window where both exist.

### 3.3 Option C: keep 1h live, declare no edge, pause or paper only

- **Evidence:** E1 is the strongest result in this whole line of work.
- **Costs:** none, if the user simply stops acting on 1h BUY signals. A full pause (no new entries) is the
  `KILL_SWITCH` file on the VM (operational, no config change), which is still the user's call.
- **Verdict:** right as an **interim step now**, wrong as the end state (it leaves no strategy). Keeping
  the 1h paper bot running produces data about a strategy already shown to be negative. Switching it to A
  is more useful.

### 3.4 Recommendation

**C now, then A in paper under the pre-registered protocol in §5. No real-money use of A's signals until
the trial ends and the user decides on its report.** A is the only option whose live behaviour (once the
§3.1 parity rules are in) is the backtested behaviour, on the venue's own bars, with zero strategy-parameter
changes. B is strictly worse. C alone is a stop, not a strategy.

## 4. Minimum additional evidence, and why a forward window can't settle the edge

Sample size for an expected bootstrap CI-low > 0, given the observed per-trade R mean and std (normal
approximation): n = (1.96 σ / μ)². For 80% power: n = ((1.96 + 0.84) σ / μ)².

| basis | μ | σ | n for E[CI-low] > 0 | n for 80% power | trade rate | calendar time (E / 80%) |
|---|---|---|---|---|---|---|
| `data/` 2-year | 0.182 | 1.760 | **359** | 734 | 6.4 / 30 d | **4.7 y** / 9.5 y |
| `data_long/` 11-year | 0.518 | 1.910 | **52** | 107 | 4.1 / 30 d (5.3 over its last 2 y) | 0.8-1.1 y / 1.7-2.2 y |

R is heavily skewed (median trade -1.0R, with take-profits around +2.4R). Same-day entries are
correlated, so the effective n is smaller than these numbers. Read them as optimistic.

**Before any change to the paper bot (minimum):**
1. The §6 `bot.py` parity rules (P1-P5) and the **daily parity test** (§6, test 7) must pass. The live loop, replayed
   over `data/`, must make the same decisions as `simulate_portfolio`.
2. The user must approve the config switch and the trial protocol (§8).

**Before any real-money use (minimum):** the §5 trial must end in "confirmed", or the user must accept
"not falsified" as enough evidence, knowing it isn't statistical confirmation. Optional harness work that
would strengthen the case without forward time: a **point-in-time universe** version of E4 (P1e's
`long_universe` static-16 already passes, but survivorship remains), and an explicit **bear-regime split**
of E4 (2018, 2022, 2025).

## 5. Pre-registered paper-trial protocol (freeze before start; any change restarts the clock)

| Item | Rule |
|---|---|
| Strategy under test | Option A exactly as specified in §6, with the current `config/config.yaml` values except `market.timeframe: "1d"` and the new `loop.max_entry_delay_minutes: 60`. No other parameter changes during the trial. **Daily Backtester Routine auto-promotion into `config.yaml` is frozen for the trial** (a promotion changes the strategy mid-trial). |
| Start state | The paper portfolio is reset to `risk.starting_cash_usd` (`scripts/reset_portfolio.py`). The 1h-era logs are archived first, not deleted. Trial day 0 = the first 00:00 UTC close after deploy. |
| Duration | Ends at **50 closed trades or 18 months**, whichever comes first. Minimum 6 months. Expected end is about 8-12 months, at 4-6 trades/month with a mean hold of 17-20 days. Positions still open at the end are reported marked-to-market, not counted. |
| Metric | Per closed trade: R recomputed from the paper fills, **re-costed at the user's actual Kraken fee tier** (default `kraken_T3_taker`). The paper broker itself charges 0.10% and no slippage, which is not the gate cost. The denominator is planned risk (`entry_high - stop_loss`), as in the harness. |
| Parity (continuous; prerequisite for every verdict) | Weekly: re-run `compute_symbol_signals` + `build_plan` on the Kraken daily bars the bot used, and compare with the audit log. Pattern, direction and htf decision must match **100%**. Plan levels must match to a relative 1e-6. Live indicators are computed on a 200-bar fetch and the harness's on the full history, so EMA/ATR warm-up leaves differences of about 1e-7 relative. Any signal flip at a threshold must be explained. Re-run `simulate_portfolio` from day 0 and compare trade entry bars: at least 95% agreement both ways, and every mismatch explained (restart, fetch failure, staleness guard). An unexplained mismatch **pauses the trial** until it's fixed. That counts as an implementation finding, not a strategy verdict. |
| Kill (falsified) | Checked every 10 closed trades from n = 20: **stop if mean R < 0.18 - 1.645 x 1.8 / √n** (n=20: -0.48; 30: -0.36; 40: -0.29; 50: -0.24). This is a one-sided test against the weaker backtest (E3). Also stop if paper equity drawdown exceeds **30%** (worst backtest maxDD is -29%). Repeated looks push the false-stop rate to about 10%. That's acceptable for a protective stop. |
| Confirmed | At the end: the 95% bootstrap CI-low of re-costed R is above 0, and parity held. Prior odds: about 11% if the true edge is the 2-year +0.18R, about 53% if it is the 11-year +0.52R. |
| Not falsified | The trial ends with no kill trigger and the CI includes 0. **This is not confirmation.** The default is to keep paper running. Moving to real money from this state is an explicit user risk decision. |
| Who decides | The user decides the start, any mid-trial change (which restarts the clock), and every end-of-trial action. The Architect writes the interim (every 10 trades) and final reports from `logs/trades.csv` plus the audit log. No Routine or agent promotes anything automatically. |

## 6. Implementation deltas for a later PR (Option A)

| File | Change | Owner / tier |
|---|---|---|
| `src/bot.py` | Implements parity rules P1-P5 (§3.1). `run_cycle` becomes a monitor pass (every held symbol), then an entry pass (FLAT symbols with a new bar, in `sorted()` order, with exposure snapshotted before this bar's exits). Details: (1) **Bar gate:** module-level `_last_decided_bar: dict[str, pd.Timestamp]`. A FLAT symbol is scanned only if its latest closed bar (`ohlcv["timestamp"].iloc[-1]`) is newer than the recorded one. Any monitor action or scan records the bar. (2) **Staleness guard:** no new plan when `now - (bar_open + tf) > loop.max_entry_delay_minutes`. Logged in the audit event as `note: "stale bar: entries skipped"`. Monitor actions are never gated. (3) **htf at 1d:** when `cfg.timeframe == "1d"`, call `htf_filter.daily_trend_confirmed_from_df(enriched, fast, slow)` instead of the fetch wrapper. (4) BUY notification gets the bar date and "valid until". No dataclass or `run_cycle` signature change. | **Architect, Opus** (orchestration upstream of the risk engine) |
| `config/config.yaml` | `market.timeframe: "1d"`. New `loop.max_entry_delay_minutes: 60`. Its written reason: the evidence only covers entries at the bar close, the paper broker fills at the last close, and a late entry is therefore outside the backtest. This is a fidelity guard, not a tuned strategy parameter, and the harness (zero delay) can't test it. Update the stale comments in the `htf_filter` block ("1h only times entries") and on `max_new_entries_per_window` ("daily bars can't test the live 1h cadence"). **Requires user approval (live config).** | Platform Engineer, Sonnet. Sign-off: CEO + user |
| `src/config.py` | Expose `loop.max_entry_delay_minutes` (default 60 if absent). Validate `market.timeframe` in {1h, 1d}. | Platform Engineer, Haiku |
| `src/trade_planner.py`, `monitor.py`, `scanner.py`, `signal_engine.py`, `htf_filter.py`, `market_data.py`, `broker_*.py` | **No change.** | n/a |
| `src/risk_engine.py` | **No change.** The inert cooldown / daily-loss gates are flagged for the Risk Officer (§3.1), not changed. | (Risk Officer, Opus, only if the user opens that ticket) |
| `scripts/export_dashboard_data.py`, `web/app.js` | Display only: price "as of last daily close", default daily P&L buckets. | Platform Engineer, Sonnet (`web/` has no owner; see §9) |
| `README.md` | Stage 1: "evaluates each symbol once per closed `market.timeframe` bar". Stage 2b: the 1d scan-frame behaviour. Stage 5: close-only daily triggers. In the same PR. | Architect, Sonnet |

**Tests (QA/Tester, Sonnet; Architect reviews test 7):**
1. `test_scan_once_per_closed_bar`: two `run_cycle` calls on the same cached bar, so `scanner.scan` runs once per symbol.
2. `test_no_reentry_on_exit_bar`: a STOP_LOSS on bar t, then a second cycle on the same bar, gives no scan. Bar t+1 is scanned.
3. `test_stale_bar_blocks_entries_not_exits`: a bar older than the delay gives no plan, but OPEN stops still execute.
4. `test_entry_pass_sorted_and_pre_exit_room`: with two signals and one slot, the alphabetically first symbol wins regardless of config order (P2). A stop-out on the same bar does not free room for an entry on that bar (P3).
5. `test_htf_uses_scan_frame_at_1d`: at `timeframe: "1d"` there is no 1d fetch, and the decision equals the pure function on the scan frame. At 1h, behaviour is unchanged.
6. The existing `tests/` suite (including `_legacy_run_cycle.py` parity and `test_market_data.py`) passes unchanged at 1h.
7. **`tests/test_daily_live_parity.py`** (the key test): a fake exchange serves the committed `strategy_pack_run/data/` CSVs bar by bar. Drive `bot.run_cycle` (paper, tmp logs, 2+ cycles per bar) and `simulate_portfolio` (with `paper_today_0.10%` costs) over the same slice. Assert identical (symbol, plan bar, entry_zone, stop, take_profit) lists and identical exit bars. Fill prices legitimately differ (close vs next open). Run it twice: with `history_bars` covering the whole fixture, where equality must be exact, and with the live 200, where the number of differing decisions is reported and each one explained (indicator warm-up). This runs in CI because it lives in `tests/`.
8. A `scripts/validate_invariants.py` check stays 5/5. Run the full `pytest` at root, plus `strategy_pack_run` tests.

## 7. How this reshapes Phases 2 and 3 of the system redesign (proposals; the user decides)

- **Phase 2 (MTF).** With a daily base, today's 1d `htf_filter` mostly duplicates the scanner (§3.1), and
  the diagnosis found hourly timing adds nothing. So **1h/4h/15m confluence loses its rationale.** Proposed
  replacement: **1d base + 1w bias** (weekly fast > slow EMA, required). That is the same `GateVerdict`
  slot-ii design with different timeframes. A separate, later candidate is the playbook's **BTC regime
  gate** (BTC daily close > SMA(200)). Pre-register one candidate at a time. Data: resample from `data/`
  and `data_long/`, so no new fetch. **Kraken 1w candles open Thursday 00:00 UTC** (checked live on
  2026-10-07), so resample on epoch-aligned 7D bins, not pandas' default Sunday weeks, and add a
  truncation-invariance test. Caveat: a 26-week EMA leaves only about 18 months of usable weekly bias
  on `kraken_720`, which puts the >= 100-trade gate there at risk. The P2.0 15m dataset keeps two uses:
  measuring the cost of acting 0-60 min after the close, and modelling an intrabar catastrophe stop.
- **Phase 3 (sentiment).** Fear & Greed is one value per day, so it fits a daily base directly (lag 1 day;
  5-gate testable). CryptoPanic needs one batched call per day. The quota is no longer a constraint, but
  its §6.5 shadow window (≥ 100 scored and ≥ 30 would-veto trades) takes **16-24+ months** at 4-6
  trades/month. Proposal: run the archive collector only, and leave the gate unbuilt until the base
  strategy has finished its trial.
- **Phase 1 (universe).** The P1e FAIL stands. At 1d the rate-limit design (§4.3 there) is moot: one pass
  a day.
- **Sequencing.** Nothing in Phases 2 and 3 may change the live strategy during the §5 trial. Their harness
  research can run in parallel. A promoted change afterwards gets its own trial.

## 8. Decisions the user must make

1. **Now:** stop trading the 1h BUY signals with real money (E1)? Optionally pause the 1h paper bot
   (`KILL_SWITCH` file).
2. Adopt **Option A** as the design target (over B and C)?
3. Approve the later PR's **live-config change** (`market.timeframe: "1d"`, new
   `loop.max_entry_delay_minutes: 60`)?
4. htf at 1d: use the **scan frame** (recommended, matches all on-record evidence), or keep the 31-bar
   fetch and re-gate the E8 variant first?
5. Freeze the §5 protocol as written (duration, kill rule, confirmed / not-falsified definitions)? Reset the
   paper portfolio and archive the 1h logs?
6. **Freeze Routine auto-promotion** into `config.yaml` for the trial?
7. Real money during the trial: none (recommended), or an explicitly capped amount?
8. Confirm your **actual Kraken fee tier**. It sets the evaluation cost and decides whether the 2-year
   window is roughly breakeven (E7).
9. Accept that signals arrive once a day at about 00:05 UTC, and that acting hours later is outside the
   evidence?
10. Re-scope Phase 2 to 1d + 1w (and later BTC regime), and demote CryptoPanic to archive-only?
11. **What evidence level will you act on?** At the 2-year edge, no practical forward window confirms it
    statistically (§4).

## 9. Notes for the CEO / reviewer

- **CLAUDE.md roster drift (not edited here, out of scope):** `src/market_data.py`, `src/universe.py`,
  `scripts/` and `web/` appear in no role's "Owns" column. The redesign draft §9 proposes the Platform
  Engineer.
- **Reproduction:** the diagnosis tooling (`diag_1h_edge.py --step signals/replay/isolated/variants/
  mechanism/report`) with `data_15m/` copied from the P2.0 worktree (md5-identical). The E3/E4/E6/E8
  numbers come from a scratch script (kept outside the repo) that only calls existing functions:
  `diag_1h_edge.classify_and_verify("data"|"data_long", "1d")`, `gate_eval`, `isolated_for_signals`,
  `sequential_non_overlapping`, `isolated_baseline(every=1)`, and `compute_symbol_signals(...,
  daily_closes=enr["close"])` for the 31-bar htf variant.
