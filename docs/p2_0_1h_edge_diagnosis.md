# P2.0 follow-up: why the live strategy's edge shrinks at 1h (diagnosis, not a fix)

**Status: IN PROGRESS. This commit only contains the pre-registration in §5.1, committed BEFORE any
stop-multiplier variant was run, so the git history shows the predictions came first.**

## 5.1 Pre-registration: stop_atr_mult candidates at 1h (written before running any variant)

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
