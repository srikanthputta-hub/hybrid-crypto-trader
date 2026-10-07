# P2.0 follow-up: why the live strategy's edge shrinks at 1h (diagnosis, not a fix)

**Status:** investigation complete. Every number below is real output from
`strategy_pack_run/diag_1h_edge.py` on this branch (`claude/p2-0-1h-edge-diagnosis`). This follows
up `docs/p2_0_1h_rebaseline.md`, whose corrected version on `main` left two things open: **(a)**
1h stops are about 3.3x tighter in percentage terms, so the same fixed costs eat about 3.5x more R
(-0.33R vs -0.09R), and **(b)** independently of costs, the pre-cost edge is smaller (+0.09R vs
+0.28R). **Nothing under `src/`, `config/config.yaml`, `engine.py`, `strategies.py` or
`hyperopt_param.py` was changed.** This document is a written reason for a possible future change.
It is not that change.

> **Independent review (Architect, 2026-10-07): see §8 "Review corrections".** Every table in §1-§5
> reproduced exactly. Two claims are overstated and one context point was missing: the "same edge per
> day held at both resolutions" rate holds for portfolio trades only, not for the signal sets (§8 R2);
> Model I is disfavoured, not rejected (§8 R3); the daily kraken_720 profit comes from one quarter
> (§8 R4). The bottom line below (no edge at 1h at today's geometry) stands. The original text is kept
> unedited.

## Bottom line (read this first)

**The evidence points to "this strategy has no edge at a 1h trading horizon, regardless of
parameter tuning".** The only edge it has anywhere is a modest multi-day trend-holding edge, and
that edge is not yet statistically established at any resolution on this 2-year window.

1. **(a) and (b) are one effect, not two: time in trade.** Before costs, the strategy earns roughly
   **+0.10 to +0.15% of price per day a position is held**, at both resolutions (§1). Sizing the
   stop off a 1h ATR makes the bracket about 3.3x narrower, so trades end about 8-10x sooner (mean
   50h vs 411-477h). Each 1h trade therefore earns about **0.21% gross against a 0.86% round-trip
   taker cost**. Expressed in R, that one fact gives both (b) (pre-cost R about 3x smaller) and
   (a) (cost drag about 3.5x larger).
2. **Nothing inside the 1h signal set rescues it.** Every one of the five patterns is net-negative
   at 1h (§2), and the per-pattern differences flip sign between the two halves of the window. The
   htf gate *helps* at 1h (+0.08R; §3). The planner's R:R math behaves the same at both resolutions
   (§4). Even the **upper** end of the 95% CI on 1h pre-cost R (+0.19R) is below the 1h cost drag
   (0.33R). At today's stop geometry, no pattern filter, gate or R:R tweak can close that gap.
3. **Widening the 1h stop fixes the cost problem, but only by turning the strategy into the daily
   one** (§5). A pre-registered test matched the time-in-trade model's predictions (-0.05 / +0.05 /
   +0.20 avg R at `stop_atr_mult` 3.0 / 4.5 / 7.0, against predicted -0.06 / +0.05 / +0.18). At
   daily-equivalent stop widths (6.0-8.0), avg R sits around **+0.07 to +0.20**:
   - The gate scores are **2/5, 4/5 and 4/5**, never 5/5, and the 4/5 results fail different
     gates at 6.0 and 7.0.
   - The one CI-low pass (7.0) clears zero by **+0.005**, and its dollar-risk-weighted R is only
     +0.05.
   - Median hold at 7.0 is 11 days, the same as the daily replay's. That is the daily strategy with
     hourly entry timing, and it does no better than simply running daily (154 trades, +0.18R, 4/5).

**The concrete path, if anyone pursues it, is a design decision, not a 1h parameter tweak.** It
means accepting that this strategy only works as a multi-day hold (daily-scale stops, however they
are computed), which is a question for the Architect and the user. Section 6 lists what that would
need before going near `config/config.yaml`.

## 0. Method, data, and fidelity checks

**Data:** the 1h parquet files P2.0 derived from Coinbase 15m data, copied from the P2.0 worktree,
not re-fetched. I checked coverage myself rather than trusting the summary. All 16 symbols run
`2024-10-12 01:00` to `2026-09-30 00:00` UTC (17,217 global 1h bars). BNB starts `2025-10-22`
(listing date). XTZ is missing 2,153 1h bars, SHIB 209 and ALGO 154 (P2.0's zero-volume-omission
artifact); every other symbol is missing 14-30. This matches P2.0 §1 exactly.

**Three replays, each run through the real pipeline:**

| name | data | why |
|---|---|---|
| `1h` | `data_15m/*_1h` (+ real `*_1d` closes for the htf gate) | the P2.0 re-baseline |
| `1d_kraken` | `data/*_1d.csv` (Kraken) | the on-record current-config daily baseline |
| `1d_cb` | `data_15m/*_1d` (**same Coinbase source as 1h**) | new same-source control. If the 1h/daily gap were a data-source effect, this would show it. It doesn't: +0.22R, 127 trades. |

**How each part of the analysis is done:**

- **Signals:** `backtest_live_bot.compute_symbol_signals`, run once with the htf gate off (every
  bullish signal). Each signal is then classified with `htf_confirmed_at`, which uses the same
  rule and inputs as the harness. The script **asserts** that the confirmed subset equals
  `compute_symbol_signals(htf_enabled=True)` exactly (same timestamps, same `Signal` payloads) for
  every symbol in all three datasets. That lets htf-*rejected* signals be studied without
  approximating the gate.
- **Portfolio replay:** the real `simulate_portfolio`. To learn which pattern and plan produced
  each trade, `recording()` temporarily swaps `backtest_live_bot.trade_planner` and `RiskEngine`
  for pass-through wrappers that call the real implementation and log what they saw. A test proves
  the replay output is byte-identical with and without them.
- **Isolated outcomes:** each plan traded alone, with no portfolio and no risk engine. It fills at
  the next open, and exits at the next open after the first close at or beyond the stop or target.
  A test shows this agrees trade-for-trade with a loop over the real `src.monitor.check_open`, and
  that it is truncation-invariant (no look-ahead).
- **Random-timing baseline:** a plan at every 4th 1h bar (every daily bar), built with the
  harness's own placebo recipe (`_placebo_signal_at`). It shows what this bracket earns with *no*
  signal.
- **Variants:** a copy of `cfg.trade_planner` with one key overridden. The placebo uses the same
  rng, seed and offset draws as `placebo_run`, fanned out over processes.

**Fidelity checks (all passed):**

- The attributed 1h replay matches P2.0's `_diag_1h_trades.csv` **trade-for-trade**: 959 trades,
  every R identical to 1e-9, 8073 confirmed signals.
- The daily Kraken replay reproduces the on-record baseline: 154 trades, avg R +0.18.
- The parallel placebo on the live 1.5 config reproduces P2.0's gate table exactly: 2/5, placebo
  percentiles 92nd/100th.
- Each trade's R recomputed from its linked plan matches the harness's own R (asserted).

**R definitions:** `R` is the harness's own (net of `kraken_T3_taker` fees and slippage, per unit
of planned risk `entry_high - stop_loss`). `R_gross` is the same price move with zero costs.
`cost drag` = R - R_gross. Unless stated otherwise, costs are `kraken_T3_taker`, the gating
preset.

## 1. The core mechanism: the edge accrues per day held, and 1h trades aren't held long enough

| replay | trades | avg R | pre-cost R [95% CI] | cost drag | median stop % | mean hold | gross price return / trade | **gross return per 24h held** |
|---|---|---|---|---|---|---|---|---|
| 1h | 959 | -0.243 | +0.086 [-0.02, +0.19] | -0.329 | 3.05% | 50h | 0.21% | **0.102%** |
| daily (Kraken) | 154 | +0.182 | +0.276 [+0.01, +0.55] | -0.094 | 10.07% | 411h | 2.53% | **0.148%** |
| daily (Coinbase) | 127 | +0.221 | +0.314 [+0.01, +0.62] | -0.093 | 10.33% | 477h | 2.60% | **0.131%** |

The drift earned while in a position is about the same at both resolutions: 1h earns 70-80% of
daily's rate. What changes is **how long a position is held**, and that follows mechanically from
`trade_planner.py`. Stop distance is `entry_low - stop_atr_mult * ATR(14)` on the scan timeframe,
and the median 1h ATR is 1.23% of price against 5.3% at daily. A 2:1 bracket about 3.3x narrower
in price is exited about 3.3² ≈ 11x sooner by a random-walk argument (observed: 8-10x). So:

- **Per-trade price edge:** about 0.2% at 1h vs about 2.5% at daily. The round-trip
  `kraken_T3_taker` cost is 2 x (0.38% + 0.05%) = 0.86%: 4x the 1h edge, a third of the daily edge.
- **In R units** (÷ stop width): the pre-cost edge is about 3x smaller at 1h (that's (b)), and the
  cost drag is about 3.5x larger (that's (a)).

**How solid is (b) by itself?** On portfolio trades, the daily-minus-1h pre-cost gap is +0.19R
(Kraken; 95% CI [-0.11, +0.49], P(gap ≤ 0) = 0.10) or +0.23R (Coinbase; P = 0.08). That is
suggestive, not established, because the daily sample is small. On the larger non-overlapping
per-symbol sets (§3), it is +0.26R (P = 0.017) and +0.24R (P = 0.031). Note that P2.0's framing
of (b) as an "edge itself smaller" effect separate from (a) is not wrong, but per §1 both come
from the same time-in-trade cause.

**It is not the market regime.** A random-timing entry with the same bracket earns about zero or
less before costs in this window at both resolutions (1h: -0.018R in htf-up periods; daily: -0.014
to -0.084R). Whatever edge exists is tied to the signals, not to simply being long during this
2-year window. (Equal-weight buy & hold returned -9.7%; BTC +33%.)

## 2. Per-pattern breakdown

### 2.1 Portfolio trades (`kraken_T3_taker`)

| pattern | 1h n | 1h win% (net) | 1h pre-cost R | 1h cost drag | **1h avg R** | 1h median stop % | 1h median risk (ATR) | daily-Kraken n | daily-Kraken pre-cost R | daily-Coinbase n | daily-Coinbase pre-cost R |
|---|---|---|---|---|---|---|---|---|---|---|---|
| BREAKOUT | 213 | 36.2% | +0.156 | -0.373 | **-0.218** | 2.7% | 2.47 | 5 | +0.376 | 4 | +1.031 |
| MOMENTUM | 283 | 35.0% | +0.094 | -0.277 | **-0.183** | 3.2% | 2.00 | 133 | +0.219 | 109 | +0.317 |
| PULLBACK | 319 | 34.2% | +0.043 | -0.321 | **-0.278** | 3.4% | 3.08 | 12 | +0.438 | 12 | -0.007 |
| REVERSAL | 61 | 27.9% | -0.201 | -0.280 | **-0.481** | 3.2% | 2.92 | 0 | n/a | 0 | n/a |
| TREND_CONTINUATION | 83 | 41.0% | +0.257 | -0.459 | **-0.203** | 2.3% | 2.20 | 4 | +1.537 | 2 | +0.609 |

**Findings:**

- **The pattern mix is completely different at 1h.** Daily is a MOMENTUM strategy in practice
  (86% of trades). At 1h, all five detectors contribute. Each detector's lookbacks are configured
  in bars, not time, so they test different things at 1h. For example, MOMENTUM's "≥3% over 5
  bars with RSI>60" is a 5-day trend at daily but a 5-hour spike at 1h, and BREAKOUT's 20-bar high
  is a 20-day high at daily but a 20-hour high at 1h.
- **No pattern is net-positive at 1h.** The best pre-cost R (TREND_CONTINUATION +0.26, BREAKOUT
  +0.16) is below that pattern's own cost drag (-0.46, -0.37). TC and BREAKOUT have the *tightest*
  stops, so they pay the most for costs.
- **Pattern mix doesn't explain (b).** MOMENTUM, the only pattern with a usable daily sample, drops
  from +0.22 / +0.32 to +0.09 pre-cost by itself. If 1h had daily's mix, its pre-cost R would still
  be about +0.1.
- **The per-pattern differences at 1h are mostly not stable.** Pre-cost R by calendar half of the
  window:
  - BREAKOUT: +0.19 → +0.12
  - MOMENTUM: +0.06 → +0.22
  - PULLBACK: +0.18 → **-0.12**
  - TREND_CONTINUATION: **-0.04** → +0.42
  - REVERSAL: -0.25 → -0.15
- **REVERSAL is the only consistent per-pattern finding.** It is pre-cost negative in both halves,
  and at every stop width tested except 8.0 (§5.3). It is also a **1h-only pattern**: it is never
  htf-confirmed at daily, because "RSI < 30 within the last 10 bars" and "fast EMA > slow EMA" rarely
  coincide on the same frame. At 1h, an hourly oversold dip inside a daily uptrend is common. But
  there are only 61 trades, and removing them would move portfolio avg R by about +0.02R. It is a
  real finding, not a fix.
- **Daily per-pattern samples other than MOMENTUM are 2-12 trades.** No conclusion about "works
  daily, fails 1h" can be drawn for BREAKOUT, PULLBACK or TC from them.

### 2.2 Isolated signals (every htf-confirmed bullish signal, traded alone)

These are much larger samples, but they overlap heavily: one long trend produces many consecutive
signals with nearly identical outcomes. Pre-cost R:

| pattern | 1h confirmed n | 1h confirmed pre-cost R | daily-Kraken confirmed n | daily-Kraken pre-cost R |
|---|---|---|---|---|
| BREAKOUT | 1143 | +0.063 | 50 | +0.329 |
| MOMENTUM | 3083 | +0.122 | 1329 | +0.090 |
| PULLBACK | 2825 | +0.098 | 85 | +0.249 |
| REVERSAL | 74 | -0.135 | 0 | n/a |
| TREND_CONTINUATION | 948 | -0.050 | 16 | +0.558 |

When every overlapping signal is weighted equally, daily MOMENTUM (+0.09) is no better than 1h
MOMENTUM (+0.12). Daily's advantage only appears once trades are non-overlapping (§3's sequential
set, and the portfolio itself). That is consistent with §1: the daily edge comes from holding the
first entry of a move for weeks, not from daily bars making better individual signals. (A
"the first bar of a run is the good one" explanation was tested and **refuted**. Signals with no
bullish signal on the previous bar are not better than continuation signals. At daily they are
statistically indistinguishable: Kraken +0.10 vs +0.12, Coinbase -0.01 vs +0.02 pre-cost. At 1h,
continuation signals actually do *better*: +0.14 [+0.09, +0.20] vs +0.035 [-0.02, +0.08], though
these are overlapping-sample CIs and so optimistic.)

## 3. The htf (daily-trend) gate

| | 1h | daily (Kraken) | daily (Coinbase) |
|---|---|---|---|
| bullish signals before the gate | 18,576 | 1,554 | 1,392 |
| **share confirmed** | **43.5%** | 95.2% | 95.4% |
| confirmation rate by pattern | BO 41.6%, MOM 49.0%, PB 41.5%, REV 33.9%, TC 37.8% | BO 69%, MOM 96%, PB 100%, TC 100% | similar |
| pre-cost R, confirmed vs rejected (isolated, overlapping) | **+0.083 vs +0.004** | +0.112 vs +0.541 (n=74) | +0.014 vs +0.745 (n=64) |
| portfolio avg R, gate ON (live) vs OFF | **-0.243 vs -0.323** (1,662 trades with gate off) | +0.182 vs +0.120 | n/a |

- **The gate is far more selective at 1h, and it helps there.** It rejects 56.5% of bullish signals,
  and the rejected ones are about breakeven before costs. Turning it off makes the 1h portfolio
  0.08R worse (and 0.08R worse at `paper_today_0.10%` too: -0.018 → -0.095). **The gate is not a
  cause of the 1h shortfall.**
- **At daily, the gate is mostly redundant.** It runs on the same frame and the same EMAs as
  PULLBACK and TREND_CONTINUATION, which already require `ema_fast > ema_slow`, so it confirms 100%
  of those. That is why it passes about 95% of daily signals.
- **Side observation, out of scope.** Daily htf-*rejected* MOMENTUM signals look strong when traded
  alone (+0.74R / +1.02R pre-cost, n ≈ 50, heavily overlapping). Possibly early-trend momentum that
  the EMA cross hasn't caught yet. The sample is tiny and clustered. A question for the Market
  Analyst, not a finding.

**Non-overlapping view** (per symbol, a signal counts only once the previous one has exited,
i.e. the portfolio's one-plan-per-symbol rule without the cross-symbol caps). Pre-cost R:

| | 1h | daily (Kraken) | daily (Coinbase) |
|---|---|---|---|
| htf-confirmed signals, non-overlapping | +0.052 [-0.04, +0.14] (n=1450) | +0.315 [+0.08, +0.55] (n=225) | +0.291 [+0.05, +0.53] (n=202) |
| random-timing baseline, htf up | -0.018 | -0.014 | -0.084 |
| random-timing baseline, htf down | -0.032 | -0.124 | -0.115 |

## 4. The trade planner's risk/reward math at 1h

| | 1h | daily (Kraken) | daily (Coinbase) |
|---|---|---|---|
| realized take-profit R (pre-cost / net) | +2.38 / +2.02 | +2.53 / +2.42 | +2.59 / +2.47 |
| realized stop-loss R (pre-cost / net) | -1.16 / -1.47 | -1.14 / -1.23 | -1.11 / -1.19 |
| **breakeven take-profit share, pre-cost → net** | **32.7% → 42.1%** | 31.1% → 33.7% | 30.1% → 32.6% |
| actual take-profit share | 35.2% | 38.5% | 38.7% |
| fill vs planned `entry_high` | -0.04R | -0.05R | -0.05R |
| plans approved → trades filled | 959 → 959 | 154 → 154 | 129 → 127 |
| ADX trend-stop bonus applied | 34.7% | 49.4% | 43.3% |

- **The planner's math behaves the same at both resolutions; only the cost bite differs.**
  `min_risk_reward: 2.0` puts take-profits at 2R by construction, and realized TPs land at
  +2.4-2.6R pre-cost because exits fill at the next open. Stop overshoot is about -1.15R at all
  three (P2.0's correction holds). Before costs, 1h needs a 32.7% hit rate and gets 35.2%: a thin
  but real margin, the same shape as daily. **After costs, 1h needs 42.1% and still gets 35.2%.**
  Costs move daily's breakeven by under 3 points and 1h's by 9.4 points.
- **Level distance drives stop width, and the tightest-stop patterns pay the most for costs.**
  Pattern-anchored levels sit below the planning close (PULLBACK's fast EMA, REVERSAL's swing low,
  BREAKOUT's resistance), so those plans carry 2.5-3.1 ATR of total risk against 2.0 ATR for
  level-less MOMENTUM. That is why the tightest-stop patterns at 1h (TC 2.3%, BREAKOUT 2.7%) have
  the worst cost drag (-0.46R, -0.37R).
- **The entry zone and invalidation level are inert in practice.** `build_plan` centers the zone
  on `min/max(level, current_price)`, so the zone always contains the planning close, and the
  monitor queues an immediate FILL. Every approved plan fills at the next open. (Daily-Coinbase's
  two non-fills, ALGO and LINK, were both planned on the final bar, 2026-09-30, so there was no
  next open to fill on.) **No plan was ever DISCARDed at invalidation in any replay.** This holds at daily too, so it doesn't explain 1h vs
  daily. It does mean `invalidation_atr_mult` currently has no effect on outcomes, and `src/bot.py`
  has the same "check immediately" branch. Whether invalidation *should* matter is a design
  question about its meaning; I'm flagging it for the Architect, not changing it.
- **The ADX trend-stop bonus shows no consistent effect.** 1h pre-cost R with vs without the bonus:
  BREAKOUT -0.06 vs +0.35; TREND_CONTINUATION +0.51 vs -0.03; MOMENTUM +0.10 vs +0.09. Daily
  MOMENTUM: +0.10 vs +0.36. With about 40-190 trades per cell and the bonus confounded with a wider
  stop, that is noise, not a finding.

**Risk-engine rejection mix** (context for the Risk Officer, not part of the planner's math):

| | 1h | daily (Kraken) |
|---|---|---|
| plans reaching `check_new_trade` | 3,448 | 669 |
| rejected "no room" (exposure cap / cash) | 2,397 | 380 |
| rejected by the new-entry rate cap | 92 | 38 |
| rejected by volatility | 0 | 97 |
| **rejected by cooldown** | **0** | 0 |
| **rejected by daily loss limit** | **0** | 0 |
| positions with < $20 at risk (exposure-clipped "dust") | **62%** | 8% |

- **`risk.cooldown_minutes` and `risk.max_daily_loss_pct` never fired once at 1h.** P2.0's
  limitations text, and `docs/system-redesign-2026-10.md` §5.4, expected them to bind for the
  first time at 1h. They don't, for structural reasons:
  - **Cooldown:** a symbol's plan blocks re-scanning until it exits, and the earliest re-plan is 2
    bars (2h) later, longer than the 60-minute cooldown.
  - **Daily loss:** the 10% `max_position_pct` cap means a 3%-stop position risks about 0.3% of
    equity, so a 5% daily loss isn't reachable with at most 4 full positions.
- **At 1h the 40% exposure cap is saturated almost permanently.** 62% of fills are
  exposure-clipped scraps, the same "dust fill" effect `config.yaml`'s own `max_new_entries_per_window`
  comment already describes. This isn't driving avg R (dust trades' R ≈ full-size trades' R at both
  1.5 and 7.0), but it is a real property of the 1h run, and it belongs to the Risk Officer.

## 5. Is the stop formula fixable at 1h? Exploratory stop-multiplier test

### 5.1 Pre-registration: stop_atr_mult candidates at 1h (written before running any variant)

*(This subsection was committed verbatim in `701cec8` before any variant ran. Kept unedited.)*

Geometry facts used to choose candidates (from the live-config 1h replay, geometry only, no variant
outcomes): median 1h trade risk (entry_high - stop_loss) = **2.40 ATR(1h)**, median stop distance
3.05% of entry; daily median 10.1%. Current `stop_atr_mult` = 1.5 (1.8 with the ADX bonus on 35%
of 1h trades). Widening the stop by a factor F of the *total* risk distance needs
`stop_atr_mult = 1.5 + (F - 1) * 2.40`.

Candidates (chosen to span the hypothesis, not to fit an outcome):

| stop_atr_mult | F (total risk width vs today) | why this value |
|---|---|---|
| 3.0 | ~1.6x | modest widening; cost drag would fall from ~0.33R to ~0.20R |
| 4.5 | ~2.25x | midpoint; cost drag ~0.15R |
| 7.0 | ~3.3x | matches DAILY's median stop width (~10% of price), i.e. daily's cost geometry (~0.09-0.10R drag); the best case for "it's just the cost geometry" |

Two competing models, with numeric predictions for `kraken_T3_taker` avg R (live 1.5 = -0.24,
pre-cost +0.086, cost drag -0.33):

- **Model D (time-in-trade / persistent drift):** the gross edge accrues at a roughly constant rate
  per unit of time held (measured: +0.10%/day held at 1h vs +0.13-0.15%/day at daily). A wider
  bracket gives roughly F^2 longer holds, so pre-cost R grows roughly in proportion to F while cost
  drag shrinks as 1/F. Predicted avg R: **3.0: about -0.06; 4.5: about +0.05; 7.0: about +0.18.**
- **Model I (1h signal information decays within hours):** the per-trade price-term edge is fixed
  (about 0.2-0.3% of price, well under the 0.86% round-trip taker cost), so pre-cost R and cost drag
  both shrink as 1/F. Predicted avg R: **3.0: about -0.15; 4.5: about -0.11; 7.0: about -0.07**,
  always negative, approaching zero, and the time-shift placebo gate should get *harder* to pass
  (with a wider bracket, entry timing matters less relative to the trend).

Expected side effects regardless of model: fewer trades (longer holds against the same 40%
exposure cap), and larger $ risk per trade. At 1h today, a 3% stop makes risk-sizing ask for 33% of
equity, so every position is capped at `max_position_pct` = 10% notional, about 0.3% equity at
risk. At F = 3.3 the 1% risk budget binds instead. So return/maxDD are NOT like-for-like across
variants; avg R is the scale-free comparison.

Each candidate is run through the full 5-gate procedure (all four cost presets, `kraken_T3_taker`
gates, 70/30 fixed split, 200-sim time-shift placebo with the same seed/offsets as
`run_live_bot_report.py`), using a COPY of `cfg.trade_planner`. `config/config.yaml` is not touched.

### 5.2 Results: full gate tables (1h, same window, fixed-rule split at 2026-02-26)

Rows marked † are a **post-hoc neighborhood check** (6.0 and 8.0), run *after* seeing the 7.0
result, specifically to test whether 7.0 is a stable plateau or a lucky point. They are not
candidates.

**Gates (costs: `kraken_T3_taker`):**

| stop_atr_mult | ≥100 trades | avg R CI low > 0 | later 30% avg R > 0 | beats placebo ≥ 90th pct (return / avg R) | net return > 0 and Sharpe > buy&hold (0.266) | **total** |
|---|---|---|---|---|---|---|
| 1.5 (live) | PASS | FAIL | FAIL (-0.18) | PASS (92 / 100) | FAIL | **2/5** |
| 3.0 | PASS | FAIL | PASS (+0.07) | FAIL (86 / 98.5) | FAIL | **2/5** |
| 4.5 | PASS | FAIL | PASS (+0.31) | PASS (95.5 / 96) | FAIL | **3/5** |
| 6.0 † | PASS | FAIL | PASS (+0.21) | PASS (95 / 91.5) | PASS | **4/5** |
| 7.0 | PASS | PASS (**+0.005**) | PASS (+0.47) | PASS (91.5 / 96) | FAIL (Sharpe 0.25) | **4/5** |
| 8.0 † | PASS | FAIL | PASS (+0.32) | FAIL (92.5 / 86.5) | FAIL | **2/5** |
| *daily, current config (reference)* | PASS | FAIL | PASS | PASS (98 / 91) | PASS | *4/5* |

**Results by cost assumption:**

| stop_atr_mult | costs | trades | win% | avg R | avg R 95% CI | PF | return | maxDD | Sharpe |
|---|---|---|---|---|---|---|---|---|---|
| 1.5 | paper_today_0.10% | 959 | 34.3% | -0.02 | [-0.13, +0.09] | 0.88 | -24.2% | -50.9% | -0.59 |
| 1.5 | kraken_T3_maker | 965 | 34.8% | -0.13 | [-0.23, -0.02] | 0.78 | -41.7% | -59.9% | -1.25 |
| 1.5 | **kraken_T3_taker** | 959 | 35.0% | **-0.24** | [-0.35, -0.13] | 0.71 | -53.9% | -66.8% | -1.83 |
| 1.5 | kraken_T1_taker | 950 | 34.4% | -0.56 | [-0.67, -0.45] | 0.57 | -74.2% | -79.8% | -3.30 |
| 3.0 | paper_today_0.10% | 580 | 37.4% | +0.11 | [-0.02, +0.25] | 0.98 | -2.8% | -47.6% | 0.04 |
| 3.0 | kraken_T3_maker | 580 | 37.4% | +0.03 | [-0.11, +0.17] | 0.91 | -15.1% | -52.8% | -0.28 |
| 3.0 | **kraken_T3_taker** | 582 | 37.3% | **-0.05** | [-0.18, +0.09] | 0.85 | -24.9% | -57.1% | -0.57 |
| 3.0 | kraken_T1_taker | 593 | 37.1% | -0.26 | [-0.39, -0.13] | 0.74 | -42.7% | -64.7% | -1.20 |
| 4.5 | paper_today_0.10% | 369 | 39.6% | +0.18 | [+0.02, +0.34] | 1.17 | 24.4% | -33.2% | 0.60 |
| 4.5 | kraken_T3_maker | 372 | 39.2% | +0.11 | [-0.05, +0.28] | 1.10 | 14.7% | -37.0% | 0.42 |
| 4.5 | **kraken_T3_taker** | 372 | 39.2% | **+0.05** | [-0.11, +0.22] | 1.04 | 6.3% | -40.5% | 0.25 |
| 4.5 | kraken_T1_taker | 373 | 38.9% | -0.09 | [-0.25, +0.07] | 0.88 | -16.7% | -49.9% | -0.30 |
| 6.0 † | paper_today_0.10% | 249 | 40.2% | +0.19 | [-0.01, +0.39] | 1.21 | 24.0% | -32.8% | 0.59 |
| 6.0 † | kraken_T3_maker | 253 | 39.1% | +0.11 | [-0.08, +0.31] | 1.16 | 18.0% | -35.3% | 0.48 |
| 6.0 † | **kraken_T3_taker** | 253 | 38.7% | **+0.07** | [-0.12, +0.27] | 1.11 | 12.6% | -37.6% | 0.38 |
| 6.0 † | kraken_T1_taker | 252 | 39.3% | -0.02 | [-0.21, +0.17] | 1.00 | -0.4% | -43.1% | 0.11 |
| 7.0 | paper_today_0.10% | 227 | 43.2% | +0.27 | [+0.07, +0.47] | 1.18 | 17.1% | -31.9% | 0.48 |
| 7.0 | kraken_T3_maker | 226 | 43.4% | +0.24 | [+0.04, +0.45] | 1.11 | 10.4% | -33.9% | 0.34 |
| 7.0 | **kraken_T3_taker** | 226 | 43.4% | **+0.20** | [+0.005, +0.41] | 1.07 | 6.4% | -35.6% | 0.25 |
| 7.0 | kraken_T1_taker | 228 | 42.5% | +0.09 | [-0.10, +0.29] | 0.94 | -5.3% | -42.4% | -0.01 |
| 8.0 † | paper_today_0.10% | 202 | 39.1% | +0.13 | [-0.08, +0.35] | 1.14 | 11.9% | -33.3% | 0.37 |
| 8.0 † | kraken_T3_maker | 201 | 39.3% | +0.11 | [-0.11, +0.32] | 1.11 | 9.3% | -34.3% | 0.32 |
| 8.0 † | **kraken_T3_taker** | 201 | 38.8% | **+0.08** | [-0.14, +0.29] | 1.07 | 5.8% | -35.9% | 0.24 |
| 8.0 † | kraken_T1_taker | 207 | 38.2% | -0.03 | [-0.23, +0.18] | 0.93 | -6.1% | -42.0% | -0.05 |

**Later-30% split (`kraken_T3_taker`)**, first 70% → last 30%:

| stop_atr_mult | first 70%: trades, avg R | last 30%: trades, avg R |
|---|---|---|
| 1.5 | 644, -0.27 | 315, -0.18 |
| 3.0 | 389, -0.11 | 193, +0.07 |
| 4.5 | 264, -0.05 | 108, +0.31 |
| 6.0 † | 181, +0.01 | 72, +0.21 |
| 7.0 | 162, +0.10 | 64, +0.47 |
| 8.0 † | 140, -0.03 | 61, +0.32 |

Every widened variant is better in the last 30% than in the first 70%. The edge is unevenly
distributed in time, not steady.

### 5.3 Mechanism: why each variant's avg R moved

| stop_atr_mult | trades | pre-cost R | cost drag | avg R | median stop % | mean hold | gross return / trade | gross return / 24h held | $-risk-weighted R | random-timing baseline, htf up (pre-cost / net) | REVERSAL pre-cost R |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.5 | 959 | +0.086 | -0.329 | -0.243 | 3.05% | 50h | 0.21% | 0.102% | -0.269 | -0.018 / -0.520 | -0.20 (n=61) |
| 3.0 | 582 | +0.151 | -0.202 | -0.051 | 4.86% | 104h | 0.67% | 0.155% | -0.118 | -0.014 / -0.288 | -0.40 (46) |
| 4.5 | 372 | +0.198 | -0.145 | +0.053 | 6.66% | 204h | 0.83% | 0.097% | +0.031 | +0.017 / -0.173 | -0.46 (35) |
| 6.0 † | 253 | +0.179 | -0.109 | +0.070 | 8.85% | 386h | 1.55% | 0.097% | +0.078 | +0.033 / -0.112 | -0.23 (23) |
| 7.0 | 226 | +0.299 | -0.094 | +0.205 | 10.17% | 499h | 2.43% | 0.117% | +0.046 | +0.035 / -0.090 | -0.13 (17) |
| 8.0 † | 201 | +0.159 | -0.084 | +0.076 | 11.70% | 543h | 0.67% | 0.030% | +0.044 | +0.027 / -0.083 | +0.16 (13) |

**Reading the pre-registered predictions honestly:**

- **Model I is rejected.** It predicted -0.15 / -0.11 / -0.07, always negative. The actual 4.5 and
  7.0 results are positive, and the placebo gate still passes at 4.5 and 7.0. Model I said it
  would get harder to pass. (It did fail at 3.0 and at the post-hoc 8.0, so the placebo result is
  noisy across widths, not monotonic in either direction.)
- **Model D got the right direction and was numerically close at all three candidates** (-0.06 /
  +0.05 / +0.18 predicted vs -0.05 / +0.05 / +0.20 actual). Its cost-drag part is near-exact: drag
  falls as about 1/F (0.33 → 0.20 → 0.15 → 0.09), and hold time grows roughly as F² (50h → 499h at
  F ≈ 3.3).
- **But Model D's "pre-cost R keeps growing with F" part is wrong past about F ≈ 2.** Pre-cost R
  rises from +0.09 to about +0.2 by 4.5, then wanders between +0.16 and +0.30 (6.0: 0.18, 7.0:
  0.30, 8.0: 0.16) rather than rising toward Model D's +0.22-0.30 for 6.0-8.0. The gross return
  per day held drops to 0.03% at 8.0. **The pre-cost edge levels off at about daily's own value
  (+0.28 / +0.31). It does not keep compounding.** 7.0's close match to its prediction is partly
  luck: its two neighbors land at +0.07 and +0.08.
- **Timing still matters at wide stops.** At every width, the signal's pre-cost R beats the
  random-timing baseline's (+0.03 at 7.0) by +0.13 to +0.26R, and the placebo gate passes at 4.5,
  6.0 and 7.0. So the signals do carry information about multi-day moves. Widening the stop doesn't
  manufacture an edge out of drift alone; the edge just only pays at a multi-day horizon.

### 5.4 Caveats that keep this from being a recommendation to change `stop_atr_mult`

1. **No variant passes 5/5.** 6.0 and 7.0 are 4/5 but fail *different* gates. The single CI-low
   pass (7.0) clears zero by +0.005, and both neighbors fail it.
2. **The R edge barely turns into dollars at 7.0.** Dollar-risk-weighted avg R is +0.046 against
   +0.205 unweighted. The largest-dollar quartile of trades averaged -0.15R, and one quartile
   (+0.72R) carries the mean. Return is +6.4% over 2 years (BTC buy&hold +33%), PF 1.07, and
   Sharpe 0.25 is below equal-weight buy&hold. At daily, weighted and unweighted R agree (+0.23 vs
   +0.18).
3. **Multiple testing.** Three pre-registered candidates plus two post-hoc points: at least five
   looks at the same 2-year window. Only the time-in-trade *mechanism* (§5.3) is well supported.
   The exact best multiplier is not.
4. **One dataset, two years, Coinbase.** There is no 1h history for the 11-year window where the
   daily strategy passes 5/5. The 7.0 result does survive P2.0's data-artifact check: dropping
   XTZ/SHIB/ALGO gives +0.21R, CI [+0.001, +0.42], 212 trades (the live 1.5 gives -0.26R on the
   same subset, matching P2.0). 11 of 16 symbols are positive at 7.0.
5. **It converges to the daily strategy.** At 7.0, the median stop (10.2%), median hold (264h vs
   daily's 276h), pre-cost R and avg R all match the daily replay's. What remains is "daily
   strategy, entries timed on hourly bars", and it doesn't beat the plain daily replay on return,
   Sharpe or gates.

## 6. What this does and doesn't mean

- **Does mean:** at today's geometry, the 1h FAIL is structural. The per-trade edge (about 0.2% of
  price) is about a quarter of the round-trip taker cost (0.86%). Pattern selection, the htf gate,
  the R:R floor and the entry zone can't change that, and neither can any other 1h-local tweak this
  analysis could find. Even at `paper_today_0.10%` (0.20% round trip), 1h is -0.02R.
- **Does mean:** (a) and (b) share one root cause, the bracket's timescale. They aren't two
  separate problems needing two fixes.
- **Does NOT mean:** the scanner's 1h signals are information-free. They beat random timing at
  every stop width tested, and the placebo gate passes at the live config (92 / 100) and at most
  widened variants. The information is real but small per trade, and it only pays once trades are
  held long enough for it to exceed costs.
- **Does NOT mean:** `stop_atr_mult` should become 7.0 (or 6.0). See §5.4.
- **If someone wants to pursue "multi-day hold, intraday timing" as a real change**, these are the
  open questions (for the Architect and the user, not decided here):
  1. Size stops from **daily** ATR rather than a large multiple of hourly ATR. It's the same
     geometry, says what is actually meant, and isn't tied to how volatile one hourly bar is.
     That's a `trade_planner.py` formula change and needs its own design review.
  2. Validate on the 11-year window. That needs the deferred 11-year 15m fetch (P2.0 §1).
  3. Answer the obvious question: does hourly entry timing add anything over the plain daily
     replay? On this window, no.
  4. Settle the dollar-weighting gap (§5.4 item 2) and the 1h exposure saturation (62% dust fills)
     with the Risk Officer.
- **Separate items for other owners (found along the way, not acted on):**
  - **Architect / Execution Trader:** the invalidation level never fires, because fresh plans
    always fill immediately (§4).
  - **Risk Officer:** `risk.cooldown_minutes` and `risk.max_daily_loss_pct` never bind at 1h
    either, contrary to the P2.0 / design-doc expectation (§4).
  - **Market Analyst:** REVERSAL is a 1h-only, consistently negative pattern but small-sample
    (§2.1). Daily htf-rejected MOMENTUM looks strong (§3) but is a tiny, clustered sample.

## 7. Files, reproduction, validation

**Files:**

- `strategy_pack_run/diag_1h_edge.py` (new): analysis tooling. Imports the real pipeline
  read-only. The only new logic is `htf_confirmed_at` (verified against the harness at runtime),
  `isolated_outcomes`, the `recording()` pass-through shims, and a parallel copy of the placebo loop.
- `strategy_pack_run/test_diag_1h_edge.py` (new, 7 tests):
  - `isolated_outcomes` vs a loop over the real `src.monitor.check_open`, including a chunk-boundary
    case;
  - truncation invariance and next-open fills;
  - `htf_confirmed_at` vs `compute_symbol_signals` on both its branches;
  - `recording()` byte-identical pass-through with module names restored;
  - the attribution R cross-check.
- This file.
- **Not touched:** anything under `src/`, `config/config.yaml`, `strategy_pack_run/engine.py`,
  `strategies.py`, `hyperopt_param.py`, `backtest_live_bot.py`, `run_live_bot_report.py`, and
  `docs/p2_0_1h_rebaseline.md`.
- **Data:** the 1h/1d parquet files were copied into this worktree's gitignored
  `strategy_pack_run/data_15m/` and are not committed.

**Reproduce** (from `strategy_pack_run/`, with `PYTHONPATH=..:.`; `--cache-dir` must be outside git):

```
python3 diag_1h_edge.py --cache-dir <dir> --step signals      # ~6 min on 4 cores; asserts htf reclassification
python3 diag_1h_edge.py --cache-dir <dir> --step replay       # attributed replays, asserts R cross-check
python3 diag_1h_edge.py --cache-dir <dir> --step isolated     # isolated signals + random-timing baselines
python3 diag_1h_edge.py --cache-dir <dir> --step variants --datasets 1h --stop-mults 1.5,3.0,4.5,7.0
python3 diag_1h_edge.py --cache-dir <dir> --step variants --datasets 1h --stop-mults 6.0,8.0   # post-hoc
python3 diag_1h_edge.py --cache-dir <dir> --step mechanism --stop-mults 1.5,3.0,4.5,6.0,7.0,8.0
python3 diag_1h_edge.py --cache-dir <dir> --step report       # every table in §1-§5
```

The same caveat as P2.0 applies: the exact numbers depend on P2.0's actual `data_15m/` files,
which are not reproducible from git.

**Validation (run on this branch's final state):**

- `cd strategy_pack_run && PYTHONPATH=..:. python3 -m pytest -q .`: **52 passed** (45 pre-existing
  + 7 new in `test_diag_1h_edge.py`).
- `PYTHONPATH=. python3 -m pytest -q` (repo root): **352 passed**, unchanged vs `origin/main`.
  `tests/test_trade_planner.py`: 10 passed, untouched.
- `PYTHONPATH=. python3 scripts/validate_invariants.py`: **5/5 passed**.
- `git status` before committing: no data or cache files staged. `data_15m/` is gitignored, and
  analysis caches live outside the repo.

## 8. Review corrections (independent Architect review, 2026-10-07)

Reviewer: Architect role, branch `claude/multiday-hold-design` (this doc's two commits cherry-picked onto
`origin/main` at `65a5bbe`). Same `data_15m/` 1h/1d parquet files (md5-identical to the P2.0 worktree's),
same `data/`, fresh cache directory. Nothing in §1-§7 above was edited; corrections are listed here.

**R1. Reproduction: everything in §1-§5 reproduces exactly.** I re-ran `--step signals` (the htf
reclassification asserts passed), `replay`, `isolated`, `variants` (1.5, 3.0, 4.5, 7.0, then 6.0, 8.0),
`mechanism` and `report`. Every number in §1-§5 matched, including every gate verdict and placebo
percentile, and the T3-taker trade lists are R-identical to the original cache's. I also re-checked
§5.4's two numbers that `report` doesn't print: median hold at 7.0 is 264.5h, and dropping
XTZ/SHIB/ALGO gives 212 trades, +0.21R, CI [+0.001, +0.421] (vs 905 trades and -0.26R at 1.5).
`test_diag_1h_edge.py`: 7 passed. **On pre-registration:** git only shows the predictions were committed
at 22:08 UTC (`701cec8`) and the results at 22:37. It can't show the variants ran after 22:08. The original
cache's file times back up the claimed order: `gates_1h_stop{3.0,4.5,7.0}.pkl` were written at 22:14, 22:16
and 22:18, and the post-hoc 6.0/8.0 at 22:22/22:24. That supports the order but doesn't prove it.

**R2. Overstated: "the edge accrues per day held at about the same rate at both resolutions" (Bottom
line 1, §1).** The 0.10-0.15%/day figures hold for the **portfolio-selected trades**. They don't hold for
the signal sets themselves. Gross price return per 24h held, `kraken_T3_taker` brackets, isolated
trades as in §2.2:

| set | 1h | daily (Kraken) | daily (Coinbase) | daily `data_long` (11y) |
|---|---|---|---|---|
| portfolio trades (§1) | 0.102% | 0.148% | 0.131% | n/a |
| every htf-confirmed signal, isolated | **0.202%** | **0.021%** | **-0.017%** | 0.259% |
| non-overlapping htf-confirmed (§3) | n/a | 0.150% | n/a | 0.278% |
| random timing, htf up | -0.037% | -0.087% | -0.108% | 0.138% |

(The non-overlapping and `data_long` cells come from a scratch script, not from `diag_1h_edge.py`. Their
inputs are `classify_and_verify`, `isolated_for_signals`, `sequential_non_overlapping` and
`isolated_baseline`, all imported unchanged.) On the 2-year window, the average daily signal earns roughly
nothing per day held, either gross (+0.112R) or net (+0.021R). The daily edge there comes from
**which** signals the one-plan-per-symbol rule ends up taking. 225 non-overlapping trades earn +0.315R
gross, CI [+0.08, +0.55], and the all-signal mean sits inside that CI. That makes the 2-year daily edge
path-dependent and fragile, not a broad property of the signals. What survives: §1's cost-geometry
arithmetic (stop width sets hold time and cost drag) and the conclusion that 1h fails at today's
geometry. 1h's own CI is entirely below zero (-0.24R [-0.35, -0.13]). What doesn't survive: "1h earns
70-80% of daily's rate" as a property of the signals. On the full signal sets, 1h earns more per day held
than daily does on this window.

**R3. Overstated: "Model I is rejected" (§5.3).** Model I predicted -0.15 / -0.11 / -0.07. Per-point
95% CIs: 3.0 [-0.18, +0.09] contains -0.15. 4.5 [-0.107, +0.22] excludes -0.11 by only 0.003. 7.0
[+0.005, +0.41] excludes -0.07. The post-hoc 6.0 [-0.12, +0.27] and 8.0 [-0.14, +0.29] both contain
what Model I's 1/F shrinkage implies at those widths (about -0.08 / -0.06). All six variants also
re-bracket largely the same signals, so they aren't independent tests. Fair reading: the point estimates
favour Model D and disfavour Model I, but Model I isn't decisively rejected. The bottom line doesn't
change.

**R4. Context missing from "daily replay = 154 trades, +0.18R, 4/5" (§1, §5.4 item 5).** The 2-year daily
result is concentrated in time. By entry year (`kraken_T3_taker`): 2024 (Oct-Dec only) 29 trades, **+1.32R**;
2025 64 trades, **-0.32R**; 2026 61 trades, +0.17R. Without Q4-2024 the other 125 trades average
**-0.08R**. On `data_long` (562 trades, +0.52R, 5/5) the losing years are 2018 (-0.07R, n=10), 2022
(-0.58R, n=39) and 2025 (-0.33R, n=55). Random-timing entries in htf-up periods earn **+0.20R net** there,
which is survivor drift. The placebo gate (100th/98th) says signal timing adds something beyond that
drift. Even so, most of the 11-year R is being long surviving coins in up-trends. P1e's long-universe
finding points the same way: added coins earned 0.23R vs the original 16's 0.55R.

**R5. Not re-verified:** §2.1's per-pattern daily cells (2-12 trades; the doc already says not to read
them) and §0's data-coverage counts (taken from P2.0). Everything else was re-run.
