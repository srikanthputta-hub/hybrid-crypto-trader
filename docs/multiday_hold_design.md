# Multi-day hold redesign of the live strategy (design only)

- **Role:** Architect (Opus). Docs only. Nothing under `src/` or `config/` was changed, and no existing
  harness module was changed. The branch does carry two **new** files from the cherry-picked diagnosis
  commits: `strategy_pack_run/diag_1h_edge.py` and its test. **No live or paper config change is made or implied here. Every change below needs the
  user's explicit approval** (CLAUDE.md trading rules).
- **Branch:** `claude/multiday-hold-design`, off `origin/main` at `65a5bbe`, with the P2.0 diagnosis
  commits cherry-picked. They are `701cec8`/`3211007` on the local-only branch
  `claude/p2-0-1h-edge-diagnosis`, and `2e8c2e6`/`3c2e680` here. Not pushed.
- **Revision 2 (2026-10-07):** an independent Opus review raised blocking points B1 (a harness exposure-cap
  divergence) and B2 (correlated trades), plus 11 corrections. I re-ran each one before applying it. Where
  I disagree, the text says so: point 9 and point 5, partly.
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
   need one real `bot.py` change: parity rules P1-P5 (§3.1), chiefly "one decision per symbol per
   closed bar" and an entry-staleness guard. Without them, the live bot would re-scan the same daily bar
   about 288 times a day, and its same-bar processing order and exposure accounting would differ from the
   backtest. **It also needs one harness fix first (P6, B1).** The backtest lets same-bar entries exceed
   the 40% total-exposure cap; live cannot. Re-gated live-style, `data/` gives 137 trades, +0.17R, 13.1%
   return (vs 154 / +0.18R / 22.5%). The evidence has to be re-run on the corrected harness before
   anything goes live.
2. **Right now, independent of A: stop treating the 1h bot's signals as tradeable (Option C as an
   interim step).** This is the one strongly supported conclusion. The live 1h config's net expectancy at
   `kraken_T3_taker` is **-0.24R, 95% CI [-0.35, -0.13]**: negative, with the whole CI below zero. If the
   user trades the Telegram BUY signals by hand, that is the expectancy they get.
3. **Option B (1h scan, daily-ATR stops) is rejected.** Its proxy (1h `stop_atr_mult` 7.0) does no
   better than plain daily: +0.21R, CI-low +0.005, a single point whose neighbours fail. It costs a
   planner contract change, and it can't be validated beyond 2 years.
4. **The evidence for A is marginal, and this doc does not claim otherwise.** A passes 5/5 on 11 years
   of survivor coins. It fails CI-low on the recent 2 years (+0.18R, CI [-0.08, +0.46]), and that
   window's profit comes from one quarter (Q4-2024). Same-day trades are correlated. At the 2-year edge,
   statistically confirming it would take about **750 trades, about 10 years** of forward trading.
   **So the proposed paper trial (§5) is an implementation-parity check plus a catastrophe guard. It is
   not an edge test.** Simulated with correlated trades: even at the 11-year edge it ends "confirmed" in only
   about a third of runs, and it stops a -0.24R strategy only about half the time (46%). Real-money use stays the user's risk decision either
   way.

## 1. What the evidence does and does not support

All numbers are `kraken_T3_taker` (0.38% fee + 0.05% slippage per side; spot, so no funding),
fixed-rule 70/30 split, 200-sim time-shift placebo. Each was re-run on this branch unless marked.

| # | Evidence | Result | Supports | Does not support |
|---|---|---|---|---|
| E1 | 1h re-baseline, live config (P2.0; re-run) | 959 trades, **-0.24R [-0.35, -0.13]**, 2/5. Resampling whole entry days gives [-0.40, -0.09]; whole weeks gives [-0.47, -0.03] | 1h at today's geometry has negative net expectancy. Strong: the CI stays below 0 under day and week block resampling. | "1h signals are information-free". Pre-cost they beat random timing (isolated: +0.083 vs -0.018R). |
| E2 | 1h `stop_atr_mult` 3.0/4.5/7.0 (+6.0/8.0 post hoc) (re-run) | 2/5, 3/5, **4/5 (CI-low +0.005)**, 4/5, 2/5 | Wider stops cut cost drag as about 1/width. | Any specific multiplier. No plateau: 6.0/8.0 give +0.07/+0.08R. $-weighted R at 7.0 is only +0.05. |
| E3 | Daily replay, `data/` = Kraken 1d, 2024-10 to 2026-10 (re-run) | 154 trades, **+0.18R [-0.08, +0.46]**, 4/5 (CI-low FAIL), placebo 97.5/91, later-30% +0.42R | A positive point estimate on the venue's own daily bars, with timing beating the placebo. | A statistically established edge. **By entry year: Q4-2024 29 trades +1.32R; 2025 64 trades -0.32R; 2026 61 trades +0.17R; ex-Q4-2024 -0.08R.** |
| E4 | Daily replay, `data_long/` = Coinbase 1d, 2015-2026 (re-run) | 562 trades, **+0.52R [+0.36, +0.68]**, 5/5, placebo 100/98 | Long-run multi-regime edge for this coin set. | Forward expectation. Same 16 survivors. Random entries in htf-up periods already earn **+0.20R net**. Losing years: 2018, 2022 (-0.58R), 2025 (-0.33R). |
| E5 | P1e, static-16 on the same harness (rank order + tiered slippage) (not re-run) | `kraken_720_universe` 134 trades +0.15R [-0.14, +0.46] 4/5; `long_universe` 572 trades +0.52R 5/5 | E3/E4 are robust to harness version. | Universe expansion. Added coins earned 0.23R vs 0.55R, which is consistent with survivorship inflating E4. |
| E6 | Per-signal daily, every htf-confirmed signal isolated (this review) | `data/`: +0.112R gross, **+0.02R net** (n=1480); non-overlapping +0.22R net (n=225) | The 2-year daily edge depends on which signals the one-plan-per-symbol rule takes. | A broad per-signal edge on the recent window (diagnosis §8 R2). |
| E7 | Cost sensitivity, daily (re-run) | `kraken_T1_taker` (0.80% fee): `data/` +0.09R [-0.18, +0.36]; `data_long/` +0.44R [+0.28, +0.60] | A survives a 2x fee on 11 years. | A on the recent window at retail-tier fees. |
| E8 | htf parity at daily (this review) | 31-bar live fetch vs the harness's 150-bar window: ~3% of signals differ (`data/` 26 vs 24, `data_long/` 93 vs 91). Replay with the live behaviour: `data/` 150 trades +0.22R, `data_long/` 595 trades +0.47R [+0.31, +0.62] | Verdicts don't hinge on it. | Running live at 1d **unchanged** and calling it "the backtested strategy". The trade lists differ. |
| E9 | **Exposure cap on same-bar entries (B1, re-run here)** | Harness: on `data/`, 26 of 69 approval bars commit past the 40% cap (peak 70%); on `data_long/`, 69 of 347 (peak 73%). Counting same-bar approvals live-style: `data/` **137 trades +0.17R [-0.11, +0.48], return 13.1%, Sharpe 0.48** (85 trades in common with the harness run); `data_long/` **552 trades +0.49R [+0.34, +0.65], return 340.5%, Sharpe 1.01** (413 in common) | The verdicts' direction: `data/` still fails CI-low, `data_long/` still clears it. | Treating E3-E5/E7's return, Sharpe and placebo numbers as what the live bot would produce. Gates were **not** re-run here (§6, P6). |

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
| **Fill timing / paper broker** | The paper broker fills at `current_prices` = the **last closed close**. The harness fills at the next open. For 24/7 crypto, the 00:00 open is roughly the prior close, so fills in the first cycle (within 5 min) match. A plan created hours later would book a fictitious stale price. A live market order would fill at a real price the plan never saw. | **Entry-staleness guard:** no new plan if `now - bar_close > max_entry_delay_minutes`. The value 60 is **untuned**: a judgement call, not a measurement. Exits are never blocked (risk-reducing). This covers restarts: after a restart the in-memory cache is empty, every symbol "sees a new bar", and the guard stops stale entries. A skipped bar still counts as **decided** under P1, so the bot doesn't re-try it later. In paper the broker fills at the last close regardless of delay, so the guard only changes **which** trades exist, not their price. Its real purpose is live / manual fidelity. |
| **Monitor triggers** | Close-only, checked once per daily close (the price is the last daily close). This is exactly what E3/E4 model. An intraday crash that recovers by the close won't stop out. One that closes below the stop exits at about 00:05. Realized stop-loss is -1.23R net on average at daily (diagnosis §4). | Unchanged. An intrabar "catastrophe stop" would be a **new strategy element** needing its own pre-registered backtest (the 15m dataset can model it). Not part of A. |
| **htf_filter** | At 1d the gate tests the same frame and EMAs as the scanner. It passes about 95% of signals and 100% of PULLBACK / TREND_CONTINUATION, so it is mostly redundant, but it is in the evidence (gate OFF: +0.12R vs ON: +0.18R on `data/`). Live it fetches only 31 daily bars, while the harness's daily branch uses the 150-bar scan window (E8). | **Keep it enabled.** At `timeframe == "1d"`, `bot.py` evaluates `htf_filter.daily_trend_confirmed_from_df(enriched.tail(150), ...)` on the scan frame, with no fetch. The harness recomputes the EMAs on a 150-bar window (`SCAN_TAIL_BARS`) and live has 200 bars, so `tail(150)` is what makes it exact. This reproduces the on-record daily htf decisions and saves 16 calls a day. (Alternative: keep the 31-bar fetch and re-gate the E8 variant first.) |
| **ATR / stop semantics** | ATR(14) is daily. The median stop is about 10% of price (vs 3% at 1h). Risk sizing of 1% equity / 10% stop gives about 10% notional, so `max_position_pct` and the risk budget bind about equally. Dust fills fall from 62% to 8%. | No planner change. `stop_atr_mult: 1.5` was chosen **on daily data** (config comment), so A is the setting it was evidenced for. |
| **Risk gates at daily resolution** | `cooldown_minutes` (60) and `max_daily_loss_pct` (5%) are **structurally inert**. The daily baseline resets at 00:00 UTC, right before the day's only entry pass. They are also inert at 1h (diagnosis §4). `max_volatility_pct` (8%) becomes **active** (97 rejections on `data/`) and the volatility taper applies. Both are in the evidence. The new-entry cap becomes "at most 5 entries per daily bar". | `risk_engine.py` untouched. Flag for the **Risk Officer (Opus)**: at 1d no live circuit breaker can fire within a day. A drawdown halt (playbook Layer 7) would be a new risk parameter needing its own written reason and OOS test. It is not needed for parity and not proposed here. |
| **Cost model** | Cost drag is about 0.09R/trade at T3 taker vs 0.33R at 1h. At about 6.4 trades/month, fees are a small share of R. | Evaluate the trial at the user's **actual execution venue and fee tier** (§8 decision 8). E7 shows the T1 taker tier is materially worse on the 2-year window. |
| **Data fetch / MarketDataCache** | Already bar-close aligned. It refetches once `last_open + 2 x tf` has passed, i.e. at 00:00 UTC for the just-closed bar, and drops the forming bar. `history_bars: 200` is at most Kraken's 720 and at least the scan tail (150) plus EMA warm-up. There's no settle delay, so a fetch at 00:00:05 may miss the last seconds of trades. That is negligible at 1d. | No `market_data.py` change. The bar gate reads each symbol's last closed bar timestamp (`df["timestamp"].iloc[-1]`). |
| **Dashboard / alerts** | The snapshot "current price" is up to 24h old. P&L defaults to hourly buckets. Telegram sends about 6 BUY messages a month, each right after 00:00 UTC. **Paper mode doesn't suppress BUY messages.** | Display-only follow-ups for the Platform Engineer: label prices "as of last daily close", default to daily buckets, put the bar date and "valid until" in BUY messages, and prefix every message with **"PAPER TRIAL"** for the duration of the trial. |
| **Pipeline shape / contracts** | Same five stages plus 2b. No dataclass field changes. `run_cycle`'s signature is unchanged (bar-gate state is module-level, like `htf_filter._cache`). | **Not a new stage.** This is a cadence rule inside `bot.py` orchestration, which is where per-cycle sequencing already lives. |

**Parity rules. By default, the live loop conforms to the harness, because the harness is what produced
E3/E4.** Changing harness semantics puts gate results back in question. **There is one exception, P6:**
where the harness breaks a risk limit that live enforces, the harness gets fixed and re-gated. Live is
never loosened to match.
- **P1:** one decision per symbol per closed bar (above).
- **P2:** within a bar, new-entry decisions run in `sorted(symbol)` order, after every monitor check
  for that bar. Live code today walks `market.symbols` config order. **In the current harness, order only
  changes who wins the 5-entry cap.** Exposure room is the same for every candidate on a bar, because of
  B1. After the P6 fix, order also decides who gets the exposure room. sorted() vs config order has never
  been gated (P1c only gated `rank_signals`), so matching the evidence is the only defensible choice.
- **P3:** room for new entries (`open_positions_value`) is computed against positions held **at the bar
  close, before that bar's exits execute**. The harness queues exits to the next open and decides
  entries with the exiting position still counted. Live code today sells inside the per-symbol loop, so a
  later symbol in the same cycle sees freed room.
- **P4:** the staleness guard.
- **P5:** htf on the scan frame (`tail(150)`) at 1d.
- **P6 (B1; harness fix, not a live change):** `simulate_portfolio` computes `other_value =
  invested_value(prices_now)`, which counts **OPEN** positions only. Plans approved earlier on the same bar
  are still PENDING (they fill at the next open), so every later candidate on that bar sees the same free
  room and cash. `docs/p1c_rank_signals_validation.md` already documents this. Live (`src/bot.py`
  ~500-563) fills each plan immediately, so the next symbol sees less room. Making live match would mean
  breaching `max_total_exposure_pct`, which is **not acceptable**. Fix: the harness counts quote amounts
  approved earlier on the same bar, in both `open_positions_value` and `cash_available`. **Pre-register
  this as a fidelity fix, not tuning,** then re-gate E3, E4, E5 and E7 on the fixed harness. Those
  re-gated results replace the numbers in §1 as the evidence for A. E9 gives the expected direction.
  Owner: QA/Tester for the harness, Architect for the semantics. The Risk Officer confirms the
  live-style accounting matches `risk_engine` intent (§8 decision 12).
- **Known residual divergences, measured by test 7, not fixed:** the fill price (last close vs next
  open), and the cash and equity differences that follow from it.

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
the trial ends and the user decides on its report.** Once P1-P5 are in live and P6 is in the harness, A is the only option whose live decisions can be
matched to a backtest, on the venue's own bars, with zero strategy-parameter changes. "Matched" means up
to the fill-price residual and an explained-mismatch budget (test 7). It is not byte-identical behaviour,
and before P6 it doesn't hold at all (E9). B is strictly worse. C alone is a stop, not a strategy.

## 4. Minimum additional evidence, and why a forward window can't settle the edge

Sample size for an expected bootstrap CI-low > 0, given the observed per-trade R mean and std (normal
approximation): n = (1.96 σ / μ)². For 80% power: n = ((1.96 + 0.84) σ / μ)². Trades entered on the
same day are correlated (market-wide moves). The **design effect** is the variance of the mean under
whole-entry-day block resampling divided by the variance assuming independent trades. It multiplies the
required n.

| basis | μ | σ | entry days / trades | design effect | n, E[CI-low] > 0 (independent → **adjusted**) | n, 80% power (adjusted) | trade rate | calendar time, adjusted (E / 80%) |
|---|---|---|---|---|---|---|---|---|
| `data/` 2-year | 0.182 | 1.760 | 69 / 154 | 2.11 | 359 → **757** | 1546 | 6.4 / 30 d | **~9.8 y** / ~20 y |
| `data_long/` 11-year | 0.518 | 1.910 | 347 / 562 | 1.65 | 52 → **86** | 176 | 4.1 / 30 d (5.3 over its last 2 y) | 1.3-1.7 y / 2.7-3.6 y |

The day-block 95% CIs are [-0.23, +0.58] on `data/` and [+0.32, +0.72] on `data_long/`. R is heavily
skewed (median trade -1.0R, take-profits around +2.4R).

**Before any change to the paper bot (minimum):**
0. The **P6 harness fix** must be in and E3/E4/E5/E7 re-gated on it (§3.1). If the re-gated `data_long/`
   no longer passes 5/5, stop and return to the user before going further.
1. The §6 `bot.py` parity rules (P1-P5) and the **daily parity test** (§6, test 7) must pass. The live loop, replayed
   over `data/`, must make the same decisions as `simulate_portfolio`.
2. The user must approve the config switch and the trial protocol (§8).

**Before any real-money use (minimum):** the 0-60 min fill-delay cost must be measured on the 15m data
(§6), and the §5 trial must end in "confirmed", or the user must accept
"not falsified" as enough evidence, knowing it isn't statistical confirmation. Optional harness work that
would strengthen the case without forward time: a **point-in-time universe** version of E4 (P1e's
`long_universe` static-16 already passes, but survivorship remains), and an explicit **bear-regime split**
of E4 (2018, 2022, 2025).

## 5. Pre-registered paper-trial protocol (freeze before start; any change restarts the clock)

**What this trial is:** an **implementation-parity check plus a catastrophe guard**. 50 trades is only
about 25 distinct entry days. That is enough to check that live decisions match the backtest, but not
enough to establish an edge. All operating characteristics below come from simulating the protocol 4,000
times per scenario. Each simulated run resamples whole entry days of the E3 (`data/`) or E4 (`data_long/`)
trades, shifted to the stated true mean, with checks at 20/30/40/50 trades. The script is a scratch script
outside the repo (§9).

| Item | Rule |
|---|---|
| Prerequisite | P6 harness fix done and E3/E4 re-gated. **The kill thresholds and power below must be re-simulated on the re-gated trade lists before the protocol is frozen.** |
| Strategy under test | Option A exactly as specified in §6, with the current `config/config.yaml` values except `market.timeframe: "1d"` and the new `loop.max_entry_delay_minutes: 60`. No other parameter changes during the trial. **Daily Backtester Routine auto-promotion into `config.yaml` is frozen for the trial** (a promotion changes the strategy mid-trial). |
| Start state | The paper portfolio is reset to `risk.starting_cash_usd` (`scripts/reset_portfolio.py`). The 1h-era logs are archived first, not deleted. Trial day 0 = the first 00:00 UTC close after deploy. |
| Duration | Ends at **50 closed trades or 18 months**, whichever comes first. Minimum 6 months. Expected end is about 8-12 months, at 4-6 trades/month with a mean hold of 17-20 days. Positions still open at the end are reported marked-to-market, not counted. |
| Metric | Per closed trade: R recomputed from the paper fills, **re-costed at the user's actual venue and fee tier** (§8 decision 8; default `kraken_T3_taker`). The paper broker itself charges 0.10% and no slippage, which is not the gate cost. The denominator is planned risk (`entry_high - stop_loss`), as in the harness. |
| Parity (continuous; prerequisite for every verdict) | Weekly: re-run `compute_symbol_signals` + `build_plan` on the Kraken daily bars the bot used, and compare with the audit log. Pattern, direction and htf decision must match **100%**. Plan levels must match to a relative 1e-6. Live indicators are computed on a 200-bar fetch and the harness's on the full history, so EMA/ATR warm-up leaves differences of about 1e-7 relative. Any signal flip at a threshold must be explained. Re-run the **P6-fixed** `simulate_portfolio` from day 0 and compare trade entry bars. Every mismatch must be explained (restart, fetch failure, staleness guard, fill-price cash differences). There is a budget of at most 10% explained mismatches. Against the unfixed harness this check would fail by design (E9: only 85 of 154 trades in common). An unexplained mismatch **pauses the trial** until it's fixed. That counts as an implementation finding, not a strategy verdict. |
| Kill (catastrophe guard) | Stop if mean re-costed R falls below **-0.70 at 20 trades, -0.55 at 30, -0.45 at 40, or -0.40 at 50**. These thresholds were set from the day-block simulation as the 5th percentile, at each check, of a true +0.18R strategy. Or stop if paper equity drawdown exceeds **30%** (worst backtest maxDD is -29%). Simulated kill rates by true mean R (`data/` pool): +0.52 → 1%; **+0.18 → 11% (false stop)**; 0 → 22%; **-0.24 → 46% (power)**. The first version of this doc used the formula 0.18 - 1.645 x 1.8 / √n. Its σ = 1.8 was an unstated midpoint of 1.76 and 1.91. Under correlation that rule false-stops at 21-23% and catches -0.24R only 62-63% of the time, so it is dropped. **No 50-trade rule separates +0.18R from -0.24R well. This is a guard against a gross failure, not a test.** |
| Confirmed | At the end: the 95% CI-low of re-costed R, from a bootstrap that resamples **whole entry weeks**, is above 0, and parity held. Simulated chance of "confirmed" (simulated with day-block CIs; week blocks are a little more conservative, `data/` / `data_long/` pool): true +0.52R → 31% / 34%; +0.18R → 7% / 8%; 0 → 2-4%. The earlier "11% / 53%" figures assumed independent trades and are withdrawn. |
| Not falsified | The trial ends with no kill trigger and the CI includes 0. **This is not confirmation.** The default is to keep paper running. Moving to real money from this state is an explicit user risk decision. |
| Who decides | The user decides the start, any mid-trial change (which restarts the clock), and every end-of-trial action. The Architect writes the interim (every 10 trades) and final reports from `logs/trades.csv` plus the audit log. No Routine or agent promotes anything automatically. |

## 6. Implementation deltas for a later PR (Option A)

| File | Change | Owner / tier |
|---|---|---|
| `src/bot.py` | Implements parity rules P1-P5 (§3.1). `run_cycle` becomes a monitor pass (every held symbol), then an entry pass (FLAT symbols with a new bar, in `sorted()` order, with exposure snapshotted before this bar's exits). Details: (1) **Bar gate:** module-level `_last_decided_bar: dict[str, pd.Timestamp]`. A FLAT symbol is scanned only if its latest closed bar (`ohlcv["timestamp"].iloc[-1]`) is newer than the recorded one. Any monitor action or scan records the bar. (2) **Staleness guard:** no new plan when `now - (bar_open + tf) > loop.max_entry_delay_minutes`. Logged in the audit event as `note: "stale bar: entries skipped"`. Monitor actions are never gated. (3) **htf at 1d:** when `cfg.timeframe == "1d"`, call `htf_filter.daily_trend_confirmed_from_df(enriched.tail(150), fast, slow)` instead of the fetch wrapper. (4) BUY notification gets the bar date, "valid until" and the "PAPER TRIAL" prefix. (5) **Persist the bars behind each decision:** append each newly closed bar per symbol to `logs/daily_bars/` (or the full fetched frame), excluded from `log_retention` pruning. The weekly parity check needs the exact bars the bot saw. No dataclass or `run_cycle` signature change. | **Architect, Opus** (orchestration upstream of the risk engine) |
| `strategy_pack_run/backtest_live_bot.py` | **P6:** count quote amounts approved earlier on the same bar in `open_positions_value` and `cash_available`. Pre-registered as a fidelity fix. Re-gate E3, E4 (`run_live_bot_report.py`), E5 (P1e's static-16 rows) and E7. Optionally add a test-only `fill_at="close"` mode (default unchanged) so test 7 can compare live and harness exactly. | QA/Tester, Sonnet. Architect (Opus) reviews the semantics. Risk Officer confirms the accounting. |
| `scripts/paper_parity_check.py` (new) | The weekly parity check in §5: reads `logs/daily_bars/`, the audit log and `trades.csv`; re-runs signals, plans and the P6-fixed portfolio; writes a mismatch report. Read-only. | QA/Tester, Sonnet |
| `config/config.yaml` | `market.timeframe: "1d"`. New `loop.max_entry_delay_minutes: 60` (**untuned**). Its written reason: the evidence only covers entries at the bar close, so a late entry is outside the backtest. It can only remove entries. The daily harness assumes zero delay, but the P2.0 15m data **can** measure what acting 0-60 min after the close costs (§7). That measurement must be done **before any real-money use**. Update the stale comments in the `htf_filter` block ("1h only times entries") and on `max_new_entries_per_window` ("daily bars can't test the live 1h cadence"). **Requires user approval (live config).** | Platform Engineer, Sonnet. Sign-off: CEO + user |
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
7. **`tests/test_daily_live_parity.py`** (the key test): a fake exchange serves the committed `strategy_pack_run/data/` CSVs bar by bar. Drive `bot.run_cycle` (paper, tmp logs, 2+ cycles per bar) and `simulate_portfolio` (with `paper_today_0.10%` costs) over the same slice. Assert identical (symbol, plan bar, entry_zone, stop, take_profit) lists and identical exit bars. This must run against the **P6-fixed** harness, which is required. Fill prices legitimately differ (close vs next open). That changes cash and equity, which can flip small approvals near the caps. So either use the harness's test-only `fill_at="close"` mode for exact equality, or assert against an explicit, explained mismatch budget. Exact equality against today's default harness is not achievable. Run it twice: with `history_bars` covering the whole fixture, where equality must be exact, and with the live 200, where the number of differing decisions is reported and each one explained (indicator warm-up). This runs in CI because it lives in `tests/`.
8. A `scripts/validate_invariants.py` check stays 5/5. Run the full `pytest` at root, plus `strategy_pack_run` tests, plus `python research/v2_signals.py` (CLAUDE.md requires it for any change touching entry logic).
9. The P6 harness fix gets its own unit test: on a synthetic bar with several approvals, committed notional never exceeds `max_total_exposure_pct`. A truncation-invariance test is also re-run.

## 7. How this reshapes Phases 2 and 3 of the system redesign (proposals; the user decides)

- **Phase 2 (MTF).** With a daily base, today's 1d `htf_filter` mostly duplicates the scanner (§3.1), and
  the diagnosis found hourly timing adds nothing. So **1h/4h/15m confluence loses its rationale.** Proposed
  replacement: **1d base + 1w bias** (weekly fast > slow EMA, required). That is the same `GateVerdict`
  slot-ii design with different timeframes. A separate, later candidate is the playbook's **BTC regime
  gate** (BTC daily close > SMA(200)). Pre-register one candidate at a time. Data: resample from `data/`
  and `data_long/`, so no new fetch. **Kraken 1w candles open Thursday 00:00 UTC.** I checked this with a live
  public `fetch_ohlcv("BTC/USD", "1w")` from this session on 2026-10-07: the opens were Thu 09-17, 09-24
  and 10-01. The reviewer couldn't check without network access. It is consistent with epoch alignment
  (1970-01-01 was a Thursday). **Resampling trap (checked on the installed pandas 3.0.6):**
  `resample("7D", origin="epoch")` ignores `origin` (it only emits a RuntimeWarning) and starts bins on
  the data's first day. Use `resample("168h", origin="epoch")` (or `"W-THU", closed="left",
  label="left"`), which gives Thursday bins. Add a truncation-invariance test. Caveat: a 26-week EMA leaves only about 18 months of usable weekly bias
  on `kraken_720`, which puts the >= 100-trade gate there at risk. The P2.0 15m dataset keeps two uses:
  measuring the cost of acting 0-60 min after the close (required before real money, §6), and modelling
  an intrabar catastrophe stop.
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
3. Approve **in principle** a later move of the paper bot to `market.timeframe: "1d"` with an entry-delay
   guard? The actual config diff gets its own explicit approval when the PR exists. This is not
   pre-approval.
4. htf at 1d: use the **scan frame** (recommended, matches all on-record evidence), or keep the 31-bar
   fetch and re-gate the E8 variant first?
5. Freeze the §5 protocol as written (duration, kill rule, confirmed / not-falsified definitions)? Reset the
   paper portfolio and archive the 1h logs?
6. **Freeze Routine auto-promotion** into `config.yaml` for the trial?
7. Real money during the trial: **none**, per §3.4. Any exception would be a separate, explicit decision
   against this design's recommendation. Note that paper mode still sends Telegram BUY messages, hence
   the "PAPER TRIAL" label.
8. **Where and at what cost do you actually execute?** `config/config.yaml` (market.symbols comment)
   still says you trade these signals by hand on **Revolut X**. The 2026-10-05 redesign draft (not on
   `main`) records a move to Kraken. Evaluate at the real venue's fees and spread. If it's Revolut X, also
   expect price differences against the Kraken USDT bars the bot plans on. E7 shows fees decide whether
   the 2-year window is roughly breakeven.
9. Accept that signals arrive once a day at about 00:05 UTC, and that acting hours later is outside the
   evidence?
10. Re-scope Phase 2 to 1d + 1w (and later BTC regime), and demote CryptoPanic to archive-only?
11. **What evidence level will you act on?** At the 2-year edge, no practical forward window confirms it
    statistically (§4: about 750 correlated trades).
12. **B1 / P6:** approve the harness fix as a pre-registered fidelity fix, followed by re-gating E3/E4/E5/E7,
    with the Risk Officer confirming the accounting? The alternative is to drop A until it's done. The
    re-gated numbers may weaken A further (E9: `data/` return 22.5% → 13.1%).

## 9. Notes for the CEO / reviewer

- **CLAUDE.md roster drift (not edited here, out of scope):** `src/market_data.py`, `src/universe.py`,
  `scripts/` and `web/` appear in no role's "Owns" column. The redesign draft §9 proposes the Platform
  Engineer.
- **Reproduction:** the diagnosis tooling (`diag_1h_edge.py --step signals/replay/isolated/variants/
  mechanism/report`) with `data_15m/` copied from the P2.0 worktree (md5-identical). The E3/E4/E6/E8
  numbers come from a scratch script (kept outside the repo) that only calls existing functions:
  `diag_1h_edge.classify_and_verify("data"|"data_long", "1d")`, `gate_eval`, `isolated_for_signals`,
  `sequential_non_overlapping`, `isolated_baseline(every=1)`, and `compute_symbol_signals(...,
  daily_closes=enr["close"])` for the 31-bar htf variant. Revision 2 adds `b1_check.py` (a RiskEngine
  subclass that adds same-bar approved quote to `open_positions_value` and removes it from
  `cash_available`), plus `b2_check.py` / `b2_kill.py` (day/week block bootstrap and the protocol
  simulation). All are in the session scratchpad and not committed.
