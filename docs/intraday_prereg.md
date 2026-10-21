# P2 pre-registration: three intraday candidate families (frozen before any intraday run)

- **Role:** Architect (Opus). Docs only. Nothing under `src/`, `config/`, `strategy_pack_run/` code, `tests/` or the
  live bot is touched. No backtest of any family below has been run, by anyone, on any data, before this commit.
- **Parent plan:** `docs/intraday_research_plan.md` (phases P0-P5, families §3, gates §4, ship rule §5).
  Data: `docs/intraday_data.md` (P0, branch `claude/intraday-data-p0`). Harness work this needs:
  `docs/intraday_harness_changes.md` (P1).
- **Evidence this builds on:** `docs/p2_0_1h_edge_diagnosis.md` (the 1h FAIL is a time-in-trade / cost-geometry effect),
  `docs/multiday_hold_design.md`, `docs/p6_harness_exposure_fix.md`, `docs/bot_bar_parity.md`,
  `docs/p1d_universe_datasets.md`, `docs/crypto_bot_upgrade_playbook.md`.
- **What this document fixes:** for each family, the exact rules, the 12 parameter sets and why each value, the cost
  model, the data windows and splits, the placebo and bootstrap, the multiple-testing rule, the pass rule of each gate,
  the report contents and the verdict logic. **After this commit none of it may change.** A later implementer only
  builds what is written here; a later reviewer checks the run against it.

> **Amendment 1 (pre-run, 2026-10-20).** Made on the orchestrator's decisions on the open questions of §9, **before
> any family has been run and before any result has been seen** (the harness of `docs/intraday_harness_changes.md`
> does not exist yet), so it is a legitimate pre-registration edit, not a post-hoc change. The original text is in
> commit `fb6485c`. Changes: (1) G2's multiple-testing budget is **36** (3 families x 12 sets), not 72, at
> alpha 0.025 / 36 = the **0.0694th percentile** (z about 3.20), consistent with the earlier gates' "95% CI lower
> bound > 0"; a first draft of this amendment (commit `16730f6`) used 0.05 / 36 (0.139th percentile), corrected
> before any run, and that value is now an informational printout only (§6.5, §6.6);
> (2) `risk_reward=None` and daily ATR% stay, marked as P4 prerequisites (§1.4, §8.8); (3) separate paper portfolios,
> combined-exposure cap is a P4 decision, live with two instances out of scope until issue #99 (§8.1); (4) the
> shortened IS window of 15m mid-cap sets is stated in the report (§7); (5) fee tier = Kraken T3 taker, Coinbase
> prints are research evidence only (§5.1); (6) placebo results must not depend on parallelism (§6.3); (7) daily
> baselines use the committed datasets and reports (harness doc AT1). Everything else is unchanged and frozen.

## 0. Rules of the pre-registration itself

1. **One shot.** Each family is run once, with the 12 sets below, on the frozen data snapshot (§4.1), with the code
   commit that passed the acceptance tests in `docs/intraday_harness_changes.md`. A result that has been seen can
   never be re-run with a modified parameter, rule, gate threshold, window, universe, cost or seed. Anything not in
   this document needs a new written reason **and a fresh out-of-sample window that did not exist when the reason
   was written** (plan §3), i.e. forward data after 2026-10-20.
2. **Bugs.** If a mechanics bug is found after results were seen, the fix is written down first as a fidelity fix
   (what was wrong, why the fix is not tuning; same pattern as `docs/p6_harness_exposure_fix.md` §0), committed, then
   every family is re-run in full. Both the old and the new numbers are reported. Parameters, gates and seeds stay.
3. **No peeking during the build.** While building the harness, strategy code may be run only on synthetic
   fixtures, or on BTC/ETH/LTC/BCH bars **before 2021-03-01** (outside both evaluation windows and before the
   warm-up period of window B). Such smoke runs may print trade lists for reconciliation; they must not compute or
   print avg R, return, Sharpe or win rate.
4. **Honest disclosure of prior exposure.** The 2024-10..2026-09 part of the window has been studied heavily for the
   *live* strategy (P2.0, diagnosis, multi-day design): its author knows Q4-2024 was strong and 2025 weak for trend
   systems. None of the three families below has ever been backtested here, and no grid value was chosen from any
   intraday result. Every value is justified from the playbook, Wilder's conventions, the risk engine's existing
   limits, or the cost geometry measured in `p2_0_1h_edge_diagnosis.md` §1. Reviewers should still treat that
   sub-window as "seen" and look at the per-year table (§6.2) with this in mind.
5. **Paper only.** Nothing here can change `config/config.yaml`, `mode: paper`, the two-switch live gate, the
   advisor, the risk limits or the running daily trial. A PASS only allows phase P3 (independent review) and then
   P4 (a separate paper instance), each with the owner's decision.

## 1. Common definitions (apply to every family)

### 1.1 Bars, closed-bar rule, execution

- **Native 1h** Coinbase bars are the primary resolution (orchestrator decision 1). **4h** decision bars are
  resampled from native 1h only, complete bins only (4 of 4 hourly bars), bins opening at 00/04/08/12/16/20 UTC (the
  same boundaries Kraken's native 4h uses, `resample_ohlcv.py` docstring). **Daily** bars for every daily input
  (regime, daily ATR, daily ADX, daily quote volume) are aggregated from native 1h to UTC days, complete days only
  (24 of 24). **15m** bars are used only by the 15m sets of family 2, never resampled into anything, and **native
  1h and 15m-derived 1h are never mixed in one run** (orchestrator decision 1, P0 §6).
- Bars are indexed by **close time** (open time + timeframe). A decision on a bar uses only bars whose close time is
  at or before that bar's close. Daily inputs are "the latest **completed** UTC day", i.e. the daily bar whose close
  time is <= the decision bar's close (`strategies.align_regime` / `v2_signals.regime_on` convention, forward-filled).
- **Entries** execute at the **next base bar's open** after the decision bar closes. Base bar = 1h for 1h/4h sets,
  15m for 15m sets. For a 4h decision closing at 08:00, the entry is the 08:00 1h open.
- **Exits, gating model ("close-trigger"):** every exit condition is evaluated on each **base bar close**; the exit
  executes at the **next base bar open**. If that open has already gapped through the stop, the fill is that open
  (worse than the stop), never the stop price. This is exactly what the live monitor can do today (close-only
  triggers, `src/monitor.py`), so a pass is paper-tradeable without exchange-resident orders. Within one base bar a
  close is a single price, so stop and target cannot both trigger: no intrabar ordering ambiguity in the gating model.
- **Exits, sensitivity model ("touch", reported, never gating):** stop fills on touch (`low <= stop`) at
  `min(open, stop)`; fixed target fills on touch (`high >= target`) at `max(open, target)`; when stop and target are
  both touched inside the same base bar, **the stop is assumed first** (pessimistic). For 4h decisions the walk is
  over the constituent native 1h bars in time order; no 15m bar is ever used inside a 1h run.
- **Missing bars** (Coinbase emits no candle when nothing traded, and has market-wide outages, P0 §4): never
  forward-filled. A coin with no base bar at t makes no decision and no monitor check at t; a queued fill or exit
  executes at that coin's next available open. Indicators run on each coin's own bar sequence (lookbacks count
  bars, as in `backtest_live_bot.py`).
- **Long-only spot, no leverage, no shorting.** One position (or pending plan) per coin at a time.

### 1.2 Universes and eligibility (orchestrator decision 3; P0 §6)

| id | coins | window (decision-bar close times) | days | role |
|---|---|---|---|---|
| **B** | 14: BTC ETH SOL ADA DOGE AVAX LINK LTC DOT ATOM ALGO BCH XTZ SHIB (no XRP, no BNB) | 2021-10-01 00:00 UTC to 2026-10-20 00:00 UTC | 1,845 (5.05 y) | **all five gates**, champion selection, placebo |
| **A** | 15: B + XRP (no BNB) | 2023-07-14 00:00 UTC to 2026-10-20 00:00 UTC | 1,194 (3.27 y) | robustness condition R1 only |

- **BNB is excluded everywhere** (363 days of history). **XRP** is only in A, which starts at the first full UTC day
  after its continuous relisting (2023-07-13 20:00).
- **15m sets** (family 2 only) use B minus XTZ (13 coins) and A minus XTZ (14 coins): XTZ is 1h-only (P0 tier C).
  On 15m, every coin except BTC and ETH is **masked** on 2023-03-04, 2024-08-13..2024-09-03 and 2025-01-01..2025-04-30
  (UTC dates, inclusive; P0's uniform rule, which also covers SOL's outage day and LTC). A masked coin may not enter
  at a 15m decision bar t unless the interval from `t - (n + 100)` 15m bars to `t + H` 15m bars (n = the set's
  lookback, H = its time stop) contains no masked bar. Because every family-2 position is closed by its time stop
  within H bars, **no masked-coin position is ever open inside a mask**, and no indicator that feeds an entry has
  seen masked bars in its last n + 100 bars. Masked windows are also excluded from the buy-and-hold benchmark of the
  15m runs (the masked coin's benchmark return is 0 inside its masks), and every 15m report says so.
- **Entry eligibility (all families):** a coin may open a new position at decision bar t only if it has >= 60
  complete daily bars before t (the existing `universe.min_listing_age_days: 60`) and every indicator the family uses
  is warm (rolling windows full; EMA/Wilder-smoothed series have >= 100 + their length bars). Warm-up uses data before
  the window start where it exists. AVAX (listed 2021-09-30) and SHIB (2021-09-09) therefore join B about 60 days in.
- **Survivorship (orchestrator decision 5; P0 §6):** the coins are today's survivors, chosen with hindsight; every
  report states this in its first paragraph. Window A is less affected than B; neither is immune.

### 1.3 Data snapshot (orchestrator decisions; P0 §3)

- Raw data: `/home/user/hct-data/1h/<BASE>_USD_1h.csv` and `/home/user/hct-data/15m/<BASE>_USD_15m.csv`
  (`strategy_pack_run/data_intraday` layout), outside git.
- **Prefix rule:** before any run, for each of the files used, the header plus the first `bars` data rows (`bars`
  from `strategy_pack_run/data_intraday/coverage_summary.json` for that coin and timeframe) must hash to the entry in
  `strategy_pack_run/data_intraday/SHA256SUMS.txt`. Rows after the snapshot (top-up appends) are allowed in the file
  but are cut off by the window end anyway. A mismatch makes the run **VOID** (§6.4).
- The window end, 2026-10-20 00:00 UTC, is before the snapshot's last bar (20:00 open) for every coin. No bar after it
  is ever read for evaluation.

### 1.4 Sizing, caps and risk limits (identical for every family)

Every entry goes through the real `src.risk_engine.RiskEngine.check_new_trade`, constructed from the **unchanged**
`risk:` block of `config/config.yaml` at the run commit (today: `max_position_pct` 0.10, `max_total_exposure_pct` 0.40,
`max_loss_per_trade_pct` 0.01, `max_daily_loss_pct` 0.05, `max_volatility_pct` 0.08, `volatility_scale_floor` 0.5
(engine default, as live), `min_risk_reward` 1.5, `cooldown_minutes` 60, `max_new_entries_per_window` 5 per
`new_entry_window_minutes` 120, `min_order_quote_usd` 10, `starting_cash_usd` 10,000). The position spend is exactly the
engine's `max_quote_amount`; nothing sizes a trade up. Inputs:

| argument | value |
|---|---|
| `portfolio_equity` | equity at the decision bar close (cash + open positions at that close) |
| `entry_price` | the decision bar's close (the plan reference; the fill is the next open) |
| `stop_loss` | the family's initial stop (below) |
| `risk_reward` | family 2: `(target - entry_price) / (entry_price - stop_loss)`; families 1 and 3 have no fixed target, so `None` (the engine then skips its R:R check). **P4 prerequisite (Amendment 1):** needs Risk Officer (Opus) review and owner sign-off before any paper instance is configured |
| `atr_pct` | **daily** ATR(14) / price from the latest completed day, for every family, so the 8% volatility cap and the taper keep the meaning they were calibrated with on daily data (an intraday ATR% of 0.6-2.5% would silently disable both; using the daily one can only reject or shrink more). **P4 prerequisite (Amendment 1):** needs Risk Officer (Opus) review and owner sign-off before any paper instance is configured |
| `open_positions_value`, `cash_available` | P6 accounting: open positions at the decision close plus gross quote already committed by fills queued earlier on the same decision bar; exits queued on this bar do not free room (`count_same_bar_approvals=True` semantics, `docs/p6_harness_exposure_fix.md` §0.3) |
| `now` | the decision bar's close time (so the cooldown, the 5-per-120-min cap and the UTC daily-loss baseline run on bar time) |

- **Per coin:** at most one open or pending position, and at most 10% of equity (engine cap). **Total:** at most 40%
  of equity committed (engine cap, P6 accounting). **Max concurrent positions:** no new limit is invented (it would be
  a new risk parameter needing its own reason and OOS test); the caps give about 4 full-size positions plus residuals
  of at least $10. Family 3 additionally holds at most K coins by construction.
- **Order of processing within a decision bar:** exits first (they are queued, not freeing room, P3), then
  candidates in `sorted(symbol)` order for families 1 and 2, and in **rank order** (descending trailing return,
  ties by symbol) for family 3. This is the order the live loop would use (`docs/bot_bar_parity.md` P2).
- **R** of a trade = net P/L after all costs / planned risk, where planned risk = `qty * (entry_price - stop_loss)`
  using the plan's reference `entry_price` (the `backtest_live_bot` convention, `planned_risk_per_unit`). `R_gross` is
  the same with zero costs.
- **Funding / overnight:** spot, long-only, unlevered: **no funding, borrow or overnight cost exists**, and none is
  charged. Idle cash earns 0%.
- **Harness version label:** `intraday-prereg-v1 + p6-same-bar-exposure-v1`. Results under another label are never
  compared with these.

### 1.5 Shared daily inputs (all computed on completed UTC days from native 1h)

- `ATR_D(c)` = `src.indicators.atr(daily_c, 14)` (Wilder). `ADX_D(c)` = `src.indicators.adx(daily_c, 14)`.
- **BTC regime** `REG_ON` = BTC daily close > `src.indicators.sma(BTC daily close, 200)` on the latest completed day
  (`research/v2_signals.regime_on` semantics; False during warm-up). Warm-up uses BTC native-1h history from 2015.
- Cross-check (acceptance test, not a gate): on 2021-10-01..2026-09-30 the aggregated daily closes must equal
  `strategy_pack_run/data_long` closes on >= 99.5% of complete days for every coin in B (P0 §5.3 measured 100% in 2025).

## 2. Family 1: intraday trend / breakout (1h and 4h decisions)

**Idea and why the stop is wide.** A Donchian breakout on hourly or 4-hourly closes, gated by the BTC daily regime,
exited only by an ATR chandelier trailing stop sized from **daily** ATR (diagnosis §6 item 1: "size stops from daily ATR
rather than a large multiple of hourly ATR"). The diagnosis showed the gross edge accrues per day held, and that a 3%
stop (1h ATR) turns a +0.09R pre-cost edge into -0.24R net. The stop widths below put cost drag at 0.08-0.16R.

**Signal (decision bar t, TF in {1h, 4h}, N in bars = hours / TF):**
```
entry_t = close_t > max(high_{t-N}, ..., high_{t-1})        # channel excludes bar t
          AND REG_ON(latest completed day <= t)
          AND coin eligible (§1.2) AND coin FLAT
```
**Initial stop:** `stop_0 = close_t - m * ATR_D` (ATR_D of the latest completed day at t).
**Trailing stop:** after each decision-bar close while open, `stop = max(stop, HC - m * ATR_D_now)`, where `HC` = highest
decision-bar close since entry (fill bar's decision bar included) and `ATR_D_now` = ATR_D of the latest completed day.
The stop never moves down (`v2_signals.trailing_stop` semantics). It is checked on every base (1h) close.
**Other exits:** regime exit: at the first decision-bar close with `REG_ON == False`, exit at the next base open.
**No target, no time stop** (playbook Layer 5: a fixed target or time stop truncates the right tail that is the
whole edge of trend following).

**Grid (12 sets = 2 x 2 x 3):**

| set | TF | N (hours) | m (x daily ATR) |
|---|---|---|---|
| F1-01 | 1h | 72 | 1.0 |
| F1-02 | 1h | 72 | 1.5 |
| F1-03 | 1h | 72 | 2.0 |
| F1-04 | 1h | 240 | 1.0 |
| F1-05 | 1h | 240 | 1.5 |
| F1-06 | 1h | 240 | 2.0 |
| F1-07 | 4h | 72 (18 bars) | 1.0 |
| F1-08 | 4h | 72 (18 bars) | 1.5 |
| F1-09 | 4h | 72 (18 bars) | 2.0 |
| F1-10 | 4h | 240 (60 bars) | 1.0 |
| F1-11 | 4h | 240 (60 bars) | 1.5 |
| F1-12 | 4h | 240 (60 bars) | 2.0 |

Rationale for each value:
- **TF 1h:** the plan's faster cadence; the resolution of the live-strategy 1h evidence, so results are comparable.
- **TF 4h:** the playbook's recommended intraday floor ("move to 4h or daily bars"); 6 decisions a day.
- **N = 72h (3 days):** the shortest channel that spans more than one daily cycle, so a breakout is not just an
  intraday session move; about 3x faster than the shortest daily lookback in `strategies.TrendParams` (10 days).
- **N = 240h (10 days):** equals `TrendParams`' shortest daily lookback (10 days), the bridge to the daily evidence.
- **m = 1.0:** stop about 5.3% of price (median daily ATR, diagnosis §1), between the 1h `stop_atr_mult` 3.0 and 4.5
  variants (4.9%, 6.7%); cost drag about 0.16R. The narrowest width with a plausible chance of clearing cost.
- **m = 1.5:** about 8% of price; the `stop_atr_mult` 6.0 region (8.9%); cost drag about 0.11R.
- **m = 2.0:** about 10.6% of price, the daily strategy's own median stop (10.1%); cost drag about 0.08R. The "same
  geometry as daily, intraday entry timing" end of the range.

**Reuses:** `src.indicators.atr`/`sma` (Wilder ATR, plain SMA); `research/v2_signals.regime_on` and `trailing_stop`
semantics (re-implemented on the daily-ATR input in the new family module, not modified); `src.risk_engine.RiskEngine`;
the P6 accounting rule; `engine.Costs`/`COST_PRESETS`; `resample_ohlcv.resample_ohlcv` (generalized to a 1h source,
harness doc H2). Not reused: `strategies.trend_ensemble` (TA-Lib dependency, `bars_per_day` scaling of lookbacks and
stops, vote-based sizing that the risk engine does not support).

**Honest expectation (not a prediction the test is judged against):** the cost hurdle is clearable here: at m = 1.0-2.0
the trade must earn > 0.86% gross on a 5-11% bracket, and the diagnosis measured 1.5-2.4% gross per trade at those widths
on the 1h live strategy. What is doubtful is the statistics: at daily-like widths the 1h live strategy converged to the
daily one (+0.07 to +0.20R, never 5/5), and the multiplicity-corrected gates (§5) are stricter than anything that
passed before. My expectation is net-positive point estimates for the m = 1.5/2.0 sets and a likely FAIL on gate 2
or gate 4; a PASS is possible, not likely.

## 3. Family 2: range mean-reversion (15m and 1h decisions), daily no-trend regime only

**Signal (decision bar t, TF in {15m, 1h}, lookback n bars):**
```
SMA_n,t = mean(close_{t-n+1..t});  SD_n,t = population std (ddof=0) of the same n closes
z_t     = (close_t - SMA_n,t) / SD_n,t                         (no entry if SD_n,t == 0)
entry_t = z_t <= -z_e
          AND ADX_D(coin, latest completed day) < 20          # daily "no trend"
          AND REG_ON                                          # BTC above its daily SMA(200)
          AND coin eligible, FLAT, and (15m sets) outside the mask rule of §1.2
```
**Initial stop (fixed, never trails):** `stop = close_t - 2.0 * ATR_TF(14)_t` (Wilder ATR on the decision timeframe).
**Target (fixed price set at the signal):** `target = SMA_n,t`. Triggered on a base close >= target (gating model).
**Time stop:** exit at the next base open after the position has been held n decision bars.
**No regime exit:** the regime only gates entries; open positions run to target, stop or time stop.
**R:R:** `(target - close_t) / (close_t - stop)` goes to the risk engine, which rejects anything below 1.5. With a
2-ATR stop that means the band distance must be >= 3 ATR; many signals will be vetoed, and the report counts them.
No unconstrained (risk-engine-off) variant is run: it would be a different risk policy.

**Grid (12 sets = 2 x 3 x 2):**

| set | TF | n | z_e | time stop |
|---|---|---|---|---|
| F2-01 | 15m | 20 | 2.0 | 20 bars (5h) |
| F2-02 | 15m | 20 | 2.5 | 20 bars |
| F2-03 | 15m | 48 | 2.0 | 48 bars (12h) |
| F2-04 | 15m | 48 | 2.5 | 48 bars |
| F2-05 | 15m | 96 | 2.0 | 96 bars (24h) |
| F2-06 | 15m | 96 | 2.5 | 96 bars |
| F2-07 | 1h | 20 | 2.0 | 20 bars (20h) |
| F2-08 | 1h | 20 | 2.5 | 20 bars |
| F2-09 | 1h | 48 | 2.0 | 48 bars (2d) |
| F2-10 | 1h | 48 | 2.5 | 48 bars |
| F2-11 | 1h | 96 | 2.0 | 96 bars (4d) |
| F2-12 | 1h | 96 | 2.5 | 96 bars |

Rationale for each value:
- **TF 15m / 1h:** the two timeframes the plan names; 15m runs on the masked universe of §1.2.
- **n = 20:** the standard Bollinger window, the conventional definition of a short-term mean.
- **n = 48 / n = 96:** half a day / one day at 15m and 2 / 4 days at 1h; longer windows mean wider bands and longer
  holds, which is the only lever this family has against cost (diagnosis Model D: time in trade).
- **z_e = 2.0:** the conventional 2-SD band. **z_e = 2.5:** a deeper stretch: fewer trades, larger distance to the
  mean per trade (gross per trade has to exceed 0.86%), and more plans clearing the 1.5 R:R floor.
- **Stop 2.0 x ATR_TF (fixed, not a grid parameter):** playbook §2 check 2 says stops under about 1 ATR sit in the noise;
  2 ATR is above it while still letting a 2-2.5 SD target clear the 1.5 R:R floor often enough to trade at all.
- **Time stop = n bars (fixed rule):** reversion is expected within the window that defines the mean; a position
  still below it after n bars is a failed setup.
- **ADX_D < 20 (fixed):** Wilder's "no trend" threshold, already the reading `src/indicators.adx` documents.
- **REG_ON required (fixed):** playbook §2 check 6 ("buying oversold in a downtrend") and its practitioner evidence
  (mean reversion lost -40.6% on average in bear markets). It is the same filter family 1 uses, so it adds no new
  indicator.

**Reuses:** `src.indicators.sma`/`atr`/`adx`; `RiskEngine`; P6 accounting; `engine.Costs`; the 15m loader.
Rolling SD is plain pandas (`rolling(n).std(ddof=0)`); no new indicator library.

**Honest expectation:** this is the family the cost hurdle most likely kills. Typical ATR is about 0.6% of price on
15m and 1.23% on 1h, so the 2-ATR stop is about 1.2% / 2.5% of price and the round-trip cost alone is **0.72R / 0.35R**.
The R:R floor forces targets of at least about 1.8% / 3.7%. Net of 0.86%, a full target win pays +0.94% / +2.84% and a
full stop loses -2.06% / -3.36%, so the hit rate needed just to break even is about **69% (15m) and 54% (1h)** before
time-stop exits. I expect a clear FAIL at taker costs on every 15m set and very likely on the 1h sets; the maker
sensitivity (§4.3) is where this family would show anything, and that number can never gate.

## 4. Family 3: cross-sectional momentum, rebalanced every 4h

**Signal (rebalance at every 4h decision close t; L = lookback in 4h bars):**
```
r_c,t     = close_c,t / close_c,t-L - 1          for each eligible coin c (both closes must exist)
candidates= eligible coins with r_c,t > 0         # absolute-momentum filter: never hold a falling coin
rank      = candidates ordered by r_c,t descending (ties: symbol)
target    = top K of rank, only if REG_ON; empty set if REG_ON is False
```
**Exits (queued at the 4h close, executed at the next 1h open):** a held coin is sold if `REG_ON` is False, or
`r_c,t <= 0`, or its rank position is worse than `K + b` (or it is no longer ranked). **Entries:** coins in `target`
not held, in rank order, each through the risk engine (§1.4); the 5-entries-per-120-min cap therefore limits a
rebalance to 5 buys. Positions are **not** resized between entry and exit (no partial rebalancing; turnover control).
**Initial stop (catastrophe stop, fixed):** `stop = close_t - 3.0 * ATR_D`, checked on every 1h close. It defines 1R
and bounds gap risk; it is expected to fire rarely. **No target, no time stop** (the rank exit is the exit rule).

**Grid (12 sets = 3 x 2 x 2):**

| set | L (4h bars / days) | K | b (buffer) |
|---|---|---|---|
| F3-01 | 18 / 3d | 3 | 0 |
| F3-02 | 18 / 3d | 3 | 2 |
| F3-03 | 18 / 3d | 5 | 0 |
| F3-04 | 18 / 3d | 5 | 2 |
| F3-05 | 42 / 7d | 3 | 0 |
| F3-06 | 42 / 7d | 3 | 2 |
| F3-07 | 42 / 7d | 5 | 0 |
| F3-08 | 42 / 7d | 5 | 2 |
| F3-09 | 84 / 14d | 3 | 0 |
| F3-10 | 84 / 14d | 3 | 2 |
| F3-11 | 84 / 14d | 5 | 0 |
| F3-12 | 84 / 14d | 5 | 2 |

Rationale for each value:
- **Rebalance every 4h (fixed):** the plan's definition of the family; aligned to the 4h bins (00/04/.../20 UTC).
- **L = 14d:** the best cross-sectional setup in the ACFR/AUT paper the playbook cites (14-day lookback).
- **L = 7d / 3d:** one and two halvings toward the faster horizon the owner wants; the shortest horizon at which a 4h
  rebalance still changes ranks slowly enough that turnover is not every bar.
- **K = 3:** about the top fifth of 14 coins, the most concentrated "hold the top few" reading of the plan.
- **K = 5:** about the top third; the most the 40% exposure cap fits at the typical risk-sized position (1% risk on a
  ~15% stop gives about 6.7% of equity per position; 5 x 6.7% = 33%).
- **b = 0:** pure top-K (the unbuffered textbook rule, maximum turnover). **b = 2:** a held coin survives until it falls
  below rank K + 2: the plan's "turnover capped", as a hysteresis band rather than a new risk limit.
- **Catastrophe stop 3.0 x ATR_D (fixed):** the playbook's daily `k = 3` starting value; wide enough not to act as a
  second exit rule, narrow enough that one coin's crash cannot cost more than the 1% risk budget plus gap.
- **r > 0 filter (fixed):** the playbook and ACFR both report long-only time-series momentum far stronger than
  long-short or pure relative ranking; it stops "least bad of a falling market" buys.

**Reuses:** `src.indicators.atr`; `RiskEngine` with rank-ordered processing (the `use_rank_order` pattern of
`backtest_live_bot.simulate_portfolio`, with this family's own rank key, since `signal_engine.rank_signals` ranks
`engine_score`); P6 accounting; `engine.Costs`; `resample_ohlcv` for 4h. Not reused: `strategies.tsmom` (its
rebalance schedule is `arange(len(df)) % k`, i.e. anchored to the frame's first row, not to the calendar; a placebo or
truncation would move it).

**Honest expectation:** the R unit is large (~15% stop), so cost per trade is only about 0.06R, but the real hurdle is
gross return per round trip > 0.86% with holds of probably 1-5 days, which needs about 0.2-0.8% of drift per day held.
The playbook's own evidence is weak for cross-sectional crypto momentum (6 of 21 portfolios beat the market). I expect
FAIL, most likely on gate 4 (timing does not beat shifted copies) or gate 5.

## 5. Cost model

### 5.1 Gating cost: `kraken_T3_taker`

- Fee **0.38%** of notional per side (`engine.COST_PRESETS["kraken_T3_taker"]`), slippage **0.05%** of price per side,
  on every fill: entries, stops, trailing stops, targets, time stops, regime and rank exits, and the end-of-window
  close. Buys fill at `open * (1 + 0.0005)`, sells at `px * (1 - 0.0005)`; the fee is charged on the filled notional.
  **Round trip 0.86%.** Gates 2, 3, 4 and 5 and the champion selection are all computed at this cost.
- No funding / overnight cost (spot, §1.4). **Fee tier (Amendment 1): the owner's tier is Kraken T3 taker**, already
  decided and the same tier the daily trial is judged at.
- **Venue substitution (Amendment 1):** every backtest here runs on Coinbase prints. They count as **research
  evidence only**. The check on the substitution is the P4 paper trial on Kraken prices (its parity check against
  the harness on the bars the bot actually saw); no Coinbase-only result is evidence about Kraken fills.

### 5.2 Stress and tiered slippage

- **+25% stress (gating, gate 5 only):** fee 0.475% and slippage 0.0625% per side (round trip **1.075%**); the
  champion's net return on B must stay > 0.
- **Tiered slippage (reported, not gating):** `tiered_slippage.tiered_costs_series_by_symbol` with the frozen P1d
  tiers (5 / 20 / 50 bp by trailing median daily quote volume >= $10M / >= $1M / below), applied per bar from the
  trailing 30-day median of **daily** quote volume (sum of native 1h `close * volume` per complete UTC day, through day
  t-1), **divided by 3**. The division is P0's upper estimate of Coinbase-vs-Kraken volume (1-3x, §5.2 there) and keeps
  the proxy conservative; orchestrator decision 4 forbids any capacity claim from Coinbase volume, and none is made.
  Note the harness must not feed per-1h-bar volume into the 30-*bar* median (that would be 1/24 of a day's volume and
  push every coin into the worst tier). If the champion's net return is <= 0 under the tiered schedule, its PASS
  carries a written liquidity caveat the owner must accept before P4.
- **Order size context:** at $10,000 equity a position is at most $1,000 (10% cap); market impact on these books at
  that size is assumed inside the 5 bp. No claim is made for larger accounts.

### 5.3 Maker-fill sensitivity (reported, NEVER gating; plan §1)

- **Fee 0.22% per side** (`kraken_T3_maker`), **slippage 0** on maker fills.
- **Entries:** a post-only limit buy at the decision bar's close price, placed at the decision close. **Filled only
  if** a later base bar trades through it: `low <= limit * (1 - 0.0005)` (5 bp trade-through, a queue-position proxy),
  within a **timeout of one decision bar** (15m / 1h / 4h). Fill price = limit. Not filled by the timeout = **missed
  trade**: no market fallback, the coin stays FLAT and may signal again later. The risk check happens at the decision
  close as in §1.4; the reserved quote is released if the order misses.
- **Adverse selection:** modelled by the trade-through rule itself (the limit fills only when price moves against the
  order) plus a report of mean 1h and 4h post-fill return for filled vs. the taker run's matching trades for missed
  orders (playbook §2 check 7). Missed trades' taker-run R is reported so the selection effect is visible.
- **Exits:** stops, trailing stops, time stops, regime and rank exits stay **taker** (0.38% + 5 bp). Family 2's fixed
  target is modelled as a resting limit sell at the target, filled when a base bar's `high >= target * (1 + 0.0005)`,
  at the target price, maker fee, before any later bar.
- Reported: fill rate, missed-trade count and their taker R, net avg R, return, Sharpe. A maker result can never turn
  a FAIL into a PASS: a maker-only edge would need a real maker-fill record first (plan §1), not paper fills.

## 6. Data windows, statistics, multiple testing and the gates

### 6.1 Splits

- **In-sample / out-of-sample (B):** cut = window start + floor(0.7 x 1,845) days = **2025-04-14 00:00 UTC**.
  IS = 2021-10-01..2025-04-14, OOS = 2025-04-14..2026-10-20 (554 days). A trade belongs to the period of its entry.
- **Purge:** a trade counts in the IS selection score only if its entry **and** exit are before the cut. A trade
  entered before and exited after the cut counts in neither the IS score nor the OOS gate (it does count in the
  full-window gates 1, 2, 4, 5). No embargo after the cut is needed: nothing is fitted on OOS, and every OOS trade's
  inputs are past data. The cut is at 00:00 UTC, so no bootstrap day block straddles it.
- **Walk-forward by calendar year (B), fixed rules, no refitting:** folds by entry date: F1 = 2021-10-01..2022-12-31
  (the 92-day 2021 stub is merged into 2022 because it is under 180 days), F2 = 2023, F3 = 2024, F4 = 2025,
  F5 = 2026-01-01..2026-10-20 (292 days, kept as its own fold). A fold is **scored** if it has >= 30 trades; a scored
  fold **loses** if its avg R < 0 at taker cost. Holding periods may cross fold boundaries; nothing is fitted per fold,
  so no purge is needed there.

### 6.2 Champion selection (one per family, on B in-sample only)

Every set is run on the full B window. The champion is the set with the **highest purged IS avg R at
`kraken_T3_taker`**, among sets with **>= 150 purged IS trades**. Ties within 1e-9: more IS trades, then lower set
number. If no set has 150 IS trades, the family fails gate 1. Only the champion is judged on the gates; all 12 sets
are reported in full. The selection is replicated inside every placebo draw (§6.3).

### 6.3 Placebo (gate 4)

- **200 draws**, `numpy.random.default_rng(0)`, `d = rng.integers(184, 1660)` days per draw (10%..90% of the 1,845-day
  B window), drawn once and **shared by all sets and all three families**.
- **Seeds fixed per draw (Amendment 1):** the 200 shifts `d_1..d_200` are generated once, in the main process, before
  any worker starts, and written to the results JSON. Draw i is a pure function of `(d_i, family, set, data)`: no
  random number is consumed inside a draw, and any later random step (the bootstrap) runs in the main process with
  its own fixed seed. Running the placebo in parallel is allowed; its results must be identical for any number of
  workers and any completion order (harness doc AT15).
- **What is shifted:** each coin's raw entry-signal series on the global decision-bar axis, circularly, by `d` whole
  days (`d * bars_per_day` bars). Raw = the family's entry condition **without** `REG_ON` and without the ADX filter;
  for family 3, the full ranking state (the vector of `r_c,t` over coins at each rebalance). **The same shift for every
  coin**, so cross-sectional structure (which coins signal together, how many at once, the rank vector) is **preserved
  deliberately**: the null is "the same signal pattern at a different time", not "independent noise per coin".
  Whole-day shifts keep each signal at its original hour of day, so intraday seasonality cannot make the placebo look
  worse or better.
- **What is not shifted:** prices, `REG_ON`, `ADX_D`, the 15m masks, eligibility, costs and the risk engine are real
  calendar facts and are re-applied at the landing bar (the P1d convention for masks/costs). Stops, targets, time
  stops and trailing are recomputed from the real prices from the landing entry. Consequence, stated plainly: gate 4
  tests the **timing of the entry signal within the regime**, not the value of the regime filter. (Shifting the
  post-regime entries instead would land many in regime-off periods and fire the regime exit immediately, an artifact
  that flatters the real run.)
- **Selection-aware statistic:** in each draw all 12 sets of a family are run on the shifted signals, the §6.2
  champion rule is applied to the draw's own purged IS results, and that draw-champion's **full-window avg R and total
  return** on B are recorded. The real champion is compared with these 200 values.
- Pass rule in §6.6.

### 6.4 Bootstrap (gate 2) and correlated trades

- **Blocks:** non-overlapping calendar blocks of `L_b` UTC days starting at the window start, where
  `L_b = max(1, ceil(median holding period of the champion's B trades, in days))`. With a median hold under 24h this
  is the plan's day-block bootstrap; with multi-day holds the block grows so that trades entered on adjacent days and
  open at the same time are resampled together. (The rule is fixed now; only its input is measured.)
- **Resampling:** 50,000 resamples, `numpy.random.default_rng(1)`; each resample draws, with replacement, as many
  blocks as there are blocks containing >= 1 entry, from those blocks; the statistic is total R / total trades over the
  drawn blocks. Percentiles with `numpy.percentile(..., method="linear")`.
- **Same-day, cross-coin correlation** (one BTC move stopping out many coins) is handled by the blocks: every trade
  entered in a block moves together. Reported alongside: n, number of entry blocks, the design effect
  `DE = Var_block / Var_iid` (both from the same 50,000-resample scheme; iid = trade-level) and `n_eff = n / DE`, plus
  the same CI with 7-day blocks (reported, not gating).

### 6.5 Multiple testing (registered trial count and what it does)

- **Evaluations run: 72** = 3 families x 12 sets x 2 universes (A and B). **Multiple-testing budget for G2: 36**
  (3 families x 12 sets; Amendment 1). Universe A is not counted as a trial for G2: it is a robustness requirement
  (R1, PASS needs A's avg R and return > 0), an extra hurdle on the same set, not an independent chance to pass.
  Cost presets, the +25% stress, the touch exit model, tiered slippage, maker fills, the 7-day block CI and the 15m
  core-only view are reports of the same evaluations, not extra trials; none of them can turn a FAIL into a PASS
  (only the +25% stress gates, and it can only remove a pass).
- **Gate 2 (Amendment 1):** Bonferroni over the 36 sets at the plan's one-sided 2.5% level (the lower bound of a
  95% CI, as in every earlier gate): alpha = 0.025 / 36 = **0.000694**, so the gate uses the **0.0694th percentile** of
  the bootstrap distribution (about the 35th smallest of 50,000), roughly z = 3.20 instead of 1.96. Why 36: the
  selection among the 12 sets inside a family is already penalised by G4, whose placebo re-runs the champion pick over
  all 12 sets, and A is a robustness condition, not a trial; the bound stays valid however the 36 sets are correlated
  and whichever one was selected. The uncorrected 2.5th percentile and the 0.139th percentile (alpha 0.05 / 36) are
  printed next to it for information only.
  (Superseded before any run: the original text, Bonferroni over 72 at the 0.0347th percentile, z about 3.39; and an
  intermediate Amendment-1 draft at alpha 0.05 / 36, the 0.139th percentile, z about 2.99, which did not match the
  plan's one-sided 2.5% level.)
- **Gate 4:** within a family, the selection-aware max statistic of §6.3 already accounts for picking the best of 12
  (it is the placebo distribution of "the best of 12 chosen the same way"); across the three families, Bonferroni
  m = 3: per-family alpha = 0.05 / 3 = 0.0167. With the add-one permutation p-value
  `p = (1 + #{draws >= real}) / 201`, this requires **at most 2 of 200 draw-champions >= the real champion**, i.e. the
  real champion strictly beats at least 198 of 200 (>= 99th percentile), on avg R **and** on total return. The A
  universe needs no placebo: it enters only as an AND-condition (R1), which cannot raise the false-positive rate.
- **Information only:** the Deflated Sharpe Ratio of each champion with N = 36 trials (playbook §6 item 8), and the
  share of the 12 sets with positive B avg R per family.
- What this does to power, stated now: with per-trade sigma about 1.8R and a design effect about 2, a champion needs
  avg R of about **0.47R at 300 trades, 0.26R at 1,000, 0.18R at 2,000** to clear gate 2 at z = 3.20 (Amendment 1;
  it was 0.50 / 0.27 / 0.19 at m = 72). The daily strategy's 11-year result (+0.49R on 552 trades) would clear it;
  its 2-year result (+0.17R on 137) would not.

### 6.6 The gates, exact pass rules (champion, universe B, `kraken_T3_taker` unless stated)

| gate | plan text | pass rule registered here |
|---|---|---|
| **G1** | >= 300 trades | >= 300 closed trades on B's full window **and** >= 100 distinct bootstrap blocks containing an entry |
| **G2** | bootstrap avg-R 95% CI lower bound > 0, day-block | the **0.0694th percentile** (alpha 0.025 / 36, Amendment 1) of the §6.4 block bootstrap of avg R is > 0 |
| **G3** | later-30% OOS avg R > 0; walk-forward by year: at most one losing year | OOS (entries >= 2025-04-14) has >= 60 trades and avg R > 0, **and** at least 3 of the 5 folds are scored, **and** at most 1 scored fold has avg R < 0 |
| **G4** | 200-sim time-shift placebo >= 95th percentile | the real champion strictly beats >= 198 of the 200 selection-aware draw-champions on full-window avg R **and** on total return (§6.5) |
| **G5** | net return > 0 and Sharpe above buy-and-hold at taker, still > 0 at +25% costs | net total return > 0 **and** Sharpe > the benchmark's Sharpe, both at `kraken_T3_taker`; **and** net total return > 0 at +25% costs. Sharpe for both = mean / std of **daily** (UTC close) equity returns x sqrt(365), whatever the decision timeframe. Benchmark = equal-weight buy-and-hold of the run's universe from the window start (each coin from its first eligible day), no rebalancing, no costs; 15m runs exclude masked windows (§1.2) |

Additional conditions for PASS (fixed now, beyond the five gates):
- **R1 robustness (universe A):** the same champion set, run on A's full window, has avg R > 0 **and** net total return
  > 0 at `kraken_T3_taker`. (A overlaps B's last 3.27 years, so this is a robustness check, not independent evidence.)
- **R2 plateau (playbook §6 item 6):** of the champion's grid neighbours (sets differing in exactly one parameter by
  one step; TF counts as a parameter), at least half, rounded up, have B full-window avg R > 0 at taker.

### 6.7 Verdicts

- **VOID:** any integrity check fails (snapshot prefix hash, an acceptance test of the harness doc, P/L reconciliation,
  the mixed-source guard). No number from a VOID run is interpreted; the fix follows rule 0.2.
- **PASS:** G1-G5, R1 and R2 all pass.
- **INCONCLUSIVE:** G1 fails, or G3 fails only because OOS has < 60 trades or fewer than 3 folds are scored, while every
  other gate that could be computed passes. Not shippable; the only way forward is new forward data, never a
  parameter change.
- **FAIL:** everything else, including "no set with 150 IS trades". A family's verdict is its champion's verdict.
- **Ship rule (plan §5), unchanged:** if no family is PASS, nothing intraday ships; the report goes to the owner. A PASS
  goes to P3 (differential review by independent Opus reviewers who re-run it) before anything else.

## 7. What each family's report must contain

One file per family, `docs/intraday_p2_family{1,2,3}.md`, plus the machine-readable
`strategy_pack_run/intraday_p2_results.json` (every set, both universes, every cost view, placebo arrays, bootstrap
percentiles, seeds, data hashes, code commit, harness version). The first paragraph of each report states: verdict,
survivorship (§1.2), Coinbase-not-Kraken prints and the volume caveat, the masks (15m), and any rule-0.2 rerun.

1. **Grid table**, all 12 sets x {B, A}: trades, entry blocks, win%, avg R, avg R_gross, cost drag (R), uncorrected and
   corrected CI, PF, return, maxDD, Sharpe (daily), exposure, turnover, median/mean hold, IS purged trades and avg R,
   OOS trades and avg R. The champion and the selection arithmetic are marked.
2. **Gate table** for the champion with the exact numbers behind every PASS/FAIL (G1-G5, R1, R2), the benchmark's
   return/maxDD/Sharpe (B and A), and BTC buy-and-hold for context.
3. **Per-year table** (folds F1-F5 and calendar years): trades, avg R, return, maxDD, regime-ON share of the year.
4. **Cost sensitivity:** paper 0.10%, T3 maker preset (taker-style fills, for comparability with old reports), T3
   taker, T1 taker, +25% stress, tiered slippage; plus the maker-fill model of §5.3 with fill rate, missed trades and
   adverse-selection numbers; plus the touch exit model. Each row: trades, avg R, CI, return, Sharpe.
5. **Cost geometry:** median initial stop in % of price, cost drag in R, gross % per trade, gross % per day held
   (the diagnosis §1 columns), so the time-in-trade mechanism can be read directly.
6. **Turnover:** traded notional / average equity, annualised; trades per week; fees paid as % of starting equity.
7. **Drawdown:** max drawdown, its dates and duration; worst day; worst 7 days.
8. **Concentration:** share of total net P/L from the top 3 days, and from the top 1% of trades; avg R and return
   without the top 3 entry days; per-coin trades and avg R; number of coins with positive avg R.
9. **Correlation:** design effect, n_eff, 1-day vs 7-day block CI, maximum and median number of positions opened in
   the same block.
10. **Risk-engine mix:** checks, approvals, rejections by reason (no room, rate cap, volatility, R:R, cooldown,
    daily loss, minimum order); dust share (< $50 positions).
11. **Placebo:** histogram summary of the 200 draw-champions (percentiles of avg R and return), which set each draw
    selected, the real champion's rank.
12. **15m-specific (family 2):** trades lost to the mask rule, and the core-only view (BTC, ETH, SOL, LTC) as a report.
    The report must state (Amendment 1) that the IS/OOS cut (2025-04-14) falls inside the 2025-01-01..04-30 mask, so
    the in-sample window of the 15m sets is shortened for every masked coin (no entries from about 2024-12-31 to the
    cut, i.e. its last ~3.5 IS months, plus the earlier masks), and give each 15m set's usable IS days per coin.
13. **Per-trade log** written with the columns listed in `docs/intraday_harness_changes.md` H8, and a rejected-signal
    log; both gitignored, with their SHA-256 in the JSON.

## 8. Live-parity notes (no code; what a PASS would need to be paper-traded without redesign)

These describe changes for a later, separately approved PR. They are the Architect's assessment; each code item has
its owner per `CLAUDE.md`, and `bot.py` orchestration gets an Opus read-through.

1. **Separate instance (plan P4):** its own process, `logs/` directory, portfolio, plan store, bar-decision ledger and
   Telegram label; the daily trial keeps running untouched. **Each instance is a separate paper portfolio with its own
   starting cash** (Amendment 1), and each instance's risk engine caps only its own portfolio. **Rules:** a
   combined-exposure cap across instances is a **P4 decision** (Risk Officer and owner), not something to solve inside
   the strategy; and **live (real-money) trading with two instances is out of scope until issue #99 is done.**
2. **Base feed and decision bars:** `market.timeframe` = the base timeframe (`"1h"` for a family-1/3 pass or a 1h
   family-2 pass; `"15m"` only for a 15m family-2 pass). 4h decision bars are built inside the bot from the cached 1h
   frame with the **same** resampling function as the harness (complete bins, 00/04/... UTC); Kraken's native 4h is not
   fetched, so there is one code path. Daily inputs (`REG_ON`, `ATR_D`, `ADX_D`) come from one `fetch_ohlcv(..., "1d",
   250)` per coin per UTC day (the `htf_filter` cache pattern), dropping the forming day. 250 > 200 (SMA) + warm-up.
3. **Bar parity, generalised:** P1 (one decision per coin per closed **decision** bar, persisted ledger), P2 (monitor
   pass first, then entries in sorted or rank order), P3 (this bar's exits do not free room), P4 (staleness guard), and
   the P6-style gross same-bar accounting, exactly as `docs/bot_bar_parity.md`, with one change: monitor triggers run
   on every closed **base** bar, entries only on closed **decision** bars. The P4 guard value per timeframe is
   untuned until measured: proposed 5 min (15m), 15 min (1h), 30 min (4h), to be checked against the cost of acting
   0-N minutes late on the 15m data before any real money.
4. **Poll interval and fetch timing:** `loop.poll_interval_seconds: 60` with the bar-close-aligned `MarketDataCache`
   (a coin is fetched only when a new base bar is due, so most polls make zero calls). The first fetch for a bar should
   wait about 30 s after the close so the exchange has finalised it (today's cache fetches at the first poll after the
   close; at 1d that was harmless, at 15m it is not). This is a `market_data.py` setting, Platform Engineer.
5. **Rate limits for 16 coins:** ccxt's built-in throttle for Kraken is 1 request/s (`ccxt.kraken().rateLimit == 1000`,
   ccxt 4.5.84 in this environment). Per base bar: 16 OHLC calls (one per coin, `limit` <= 720) = about 16 s of
   throttled wall time; per day: 16 daily calls. Hourly load: 16 calls (1h base) or 64 (15m base), far under 3,600/h.
   Kraken's exact public-endpoint limit per IP must be confirmed by the Platform Engineer before P4; the design keeps
   >= 10x headroom. Kraken's 720-bar cap: 720 x 1h = 30 days covers the longest intraday lookback here (family 3's 14
   days plus warm-up, family 1's 240h channel); 720 x 15m = 7.5 days covers n = 96 plus 100 warm-up bars.
6. **Fill model in paper:** the paper broker fills at the last closed close, the harness at the next open; for 24/7
   markets these differ by the close-to-open gap only (the residual measured in the daily parity work). Live would use
   taker market orders; the maker model is not implemented unless a maker-fill record exists.
7. **Contract changes this would need (Architect, additive only):** `TradePlan` gets optional fields with defaults that
   leave every existing plan unchanged, e.g. `trail_atr_mult: float | None = None`, `time_stop_at: str | None = None`,
   and "no fixed target" expressed as `take_profit = None` handled by a new monitor branch (not `inf`, which the monitor
   rejects as non-finite). `src/monitor.py` gets a trailing-stop update (persisted into `PlanRecord.plan`) and a
   time-stop trigger; `MonitorAction.action` would gain `"TIME_STOP"`/`"REGIME_EXIT"`/`"RANK_EXIT"`, so every consumer
   of action strings (`bot.py`, audit, dashboard export, parity check) must be traced first. Families 1 and 2 fit
   inside the existing stages (a new scanner detector + planner variant). **Family 3 does not:** its decision is
   portfolio-level (a ranked target set across coins), so it would be a new step between the signal engine and the
   planner, the slot `rank_signals` was built for; that is a new-stage decision, documented in README's architecture
   section in the same PR, and only made if family 3 passes.
8. **Risk inputs:** `bot.py` would pass the daily ATR% as `atr_pct` and `risk_reward=None` for no-target plans, as in
   §1.4. Both are **P4 prerequisites needing Risk Officer (Opus) review and owner sign-off before any paper instance is
   configured** (Amendment 1). The backtest uses them as written, so the paper instance must match or the evidence
   does not apply.
9. **Inert-gate note:** at 15m/1h/4h the cooldown, the daily-loss halt and the 5-per-120-min cap all bind in the
   backtest (unlike at 1d); the report's risk-engine mix shows how often. Live parity requires the same clock
   (decision-bar close time as `now`), which bar parity already provides.

## 9. Open questions and their resolution (Amendment 1, 2026-10-20, pre-run)

All six were decided by the orchestrator before any run; the decisions are written into the sections cited.

- **Q1 (resolved: kept, P4 prerequisite).** `risk_reward=None` for plans without a fixed target (families 1, 3) and
  daily ATR% as `atr_pct` (all) stay as written in §1.4. Both need Risk Officer (Opus) review and owner sign-off
  before any paper instance is configured (§1.4, §8.8).
- **Q2 (resolved).** Each instance is a separate paper portfolio with its own starting cash; a combined-exposure cap
  across instances is a P4 decision; live trading with two instances is out of scope until issue #99 is done (§8.1).
- **Q3 (resolved: m = 36).** G2's Bonferroni budget is 36 (3 families x 12 sets), alpha 0.025 / 36, the 0.0694th
  percentile, z about 3.20 (§6.5, §6.6); the 0.139th percentile (0.05 / 36) is printed for information only. 72 evaluations are still run. Frozen; no change after results.
- **Q4 (resolved: accepted).** The cut at 2025-04-14 inside the 2025-01-01..04-30 mask shortens the IS window of the
  15m sets for masked coins; the report states it with the usable IS days per coin (§7 item 12).
- **Q5 (resolved).** Fee tier = Kraken T3 taker. Coinbase prints are research evidence only; the P4 paper trial on
  Kraken prices is the check on that substitution (§5.1).
- **Q6 (resolved).** Parallel placebo is allowed; the 200 shifts are fixed before any worker starts and no random
  number is consumed inside a draw, so results cannot depend on the number of workers (§6.3, harness doc AT15). The
  compute (200 x 36 = 7,200 runs on B plus 72 base runs and the cost views) is still to be estimated on one set before
  launch; a partial placebo is not a gate result.

## 10. Amendment 2 (pre-run, 2026-10-21): clarifications accepted at the P1 review

Made after two independent reviews of the P1 harness (Risk Officer: ready for P2; Architect: ready after fixes),
**before any family has been run on evaluation data and before any result has been seen** (only synthetic data and
pre-2021-03-01 trade-list reconciliation have been run, §0 rule 3). These are clarifications of points the text
above left open or stated inconsistently; no parameter, gate threshold, window, universe, cost or seed changes.

- **(a) Placebo draw without a champion.** A placebo draw in which no set has >= 150 purged IS trades has no
  draw-champion; for G4 it counts as a draw the real champion does **NOT** beat (conservative: it can only turn a
  PASS into a FAIL, never the reverse). The report prints the number of such draws and flags it when it is
  decisive (G4 would pass if they counted as beaten). Note: the family-2 placebo resets the target at the landing
  bar (target = SMA_n there, §6.3), so the 1.5 R:R floor vetoes most shifted entries (about 15-45% of the real
  run's trades are kept on synthetic data); this reading can therefore decide family 2's G4.
- **(b) Family 3 holdings.** Family 3 may hold up to K + b coins, as the explicit rules of §4 imply (entries are the
  target coins not held; a held coin survives until rank K + b). §1.4's "at most K coins by construction" is a
  description that is false for b = 2; §4 governs. The 10% / 40% caps are unchanged. The report prints the maximum
  number of concurrent holdings.
- **(c) Warm-up.** The §1.2 warm-up rule (EMA/Wilder series need >= 100 + their length bars) applies to ATR_D and
  ADX_D, which every family uses, so a coin needs >= 114 complete daily bars (not "about 60 days" as §1.2 says for
  AVAX and SHIB). Knock-on: AVAX and SHIB join B about 114 days after their first complete day, XRP joins A around
  2023-11-05 (first complete day 2023-07-14 + 114 days), and the benchmark join dates of AVAX, SHIB and XRP shift
  accordingly. The report states the actual first-eligible and benchmark join dates of every coin.
- **(d) Stop boundary (review fix m2).** The close-trigger stop fires on a base close **at or below** the stop
  (`close <= stop`), exactly as `src/monitor.py` does; "a close below the stop" in §1.1 and AT10 reads as "at or
  below".
- **(e) A coin whose series ends before the window end (review fix n1).** An open position is not released early:
  it stays open, marked at the coin's last close, until the window's last base close, where it is closed at that
  last close (reason `end`, costs of the coin's last bar). This is the conservative reading (no exposure room or
  cash freed before the window ends); harness and reference implementation agree.

Also recorded (implementation, not interpretation): the tiered-slippage daily quote volume is the sum over the
day's native 1h bars of (1h close x 1h volume) per complete UTC day (§5.2, H5); the loader reads only the hashed
snapshot prefix (§1.3); an integrity failure makes the family VOID with only its integrity record kept (§6.7); and
the official outputs are written only by `run_intraday_prereg.py` on a clean committed tree.

### P4 prerequisites (Risk Officer; not part of P2, recorded so they are not lost)

1. The intraday paper instance must pass `now` = the decision-bar close to `check_new_trade` and `record_trade`
   (live passes the wall clock, so exact-boundary cooldown / entry-window cases differ: measured 3.5% of F2-15m
   approvals flip), with a parity test.
2. The risk engine must reject buys with `stop_loss` None or >= `entry_price` when `risk_reward` is None (test first,
   a separate Risk Officer PR).
3. `risk_reward=None` only when the plan type has no target, never as a fallback, asserted at the call site, and every
   decision where the R:R check was skipped is audit-logged.
4. Daily ATR% = ATR_D(14) / the close of the same completed day; missing = NaN (never None); up-to-24h staleness
   acknowledged; parity test on recorded bars.
5. One shared kill-switch file for both instances; per-instance caps accepted; a combined-exposure cap before any
   shared real-money account (issue #99); paper starting cash >= $2,500.
6. The daily-loss baseline (equity at the first buy check of the UTC day) is weaker at intraday cadence: owner
   decision; any change must be mirrored in the harness, with a re-run.
