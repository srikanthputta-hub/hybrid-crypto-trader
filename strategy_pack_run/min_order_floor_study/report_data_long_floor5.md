# Live-strategy backtest report

Data: data_long (16 symbols, 1d bars). Bot pipeline: src/scanner.py -> src/signal_engine.py -> src/htf_filter.py (enabled=True) -> src/trade_planner.py -> src/risk_engine.py -> src/monitor.py, imported read-only and unmodified. Long-only spot, next-open fills for every trigger (FILL/DISCARD/STOP_LOSS/TAKE_PROFIT).


Harness version: p6-same-bar-exposure-v1 (same-bar approvals count against the exposure cap and cash, as in src/bot.py; docs/p6_harness_exposure_fix.md).


Window: 2015-07-21 to 2026-10-01 (4091 bars). Raw bullish+HTF-confirmed signals detected across all symbols before any risk-engine gating: 5996.

## Important limits on what this report can tell you

- **Daily bars, not the live 1h feed.** `config/config.yaml` runs `market.timeframe: "1h"`; only
  daily history this deep was available to backtest against, so this evaluates the live
  pipeline's *decision logic* (same scanner/signal-engine/HTF-filter/trade-planner/risk-engine/
  monitor code, same configured thresholds) at daily resolution, not an exact replay of live 1h
  behavior. Patterns that only resolve within a day, and the bot's actual hour-by-hour monitoring
  cadence, are invisible here.
- **Two risk-engine gates are structurally non-binding at this resolution.** `risk.cooldown_minutes`
  (60) is far shorter than the ~1440 minutes between daily evaluations, so the cooldown gate can
  never fire in this replay (it still can, briefly, live at 1h spacing). `risk.max_daily_loss_pct`'s
  baseline resets every time a new calendar date is seen, which -- at one evaluation per day --
  means it always resets immediately before that day's own check, so it also never fires here.
  Neither is a bug in this harness; both are genuine artifacts of testing an hourly strategy on
  daily bars. See `backtest_live_bot.py`'s module docstring for the full list of simplifications.
- **The risk engine's own R:R gate (`risk.min_risk_reward=1.5`) is never the binding constraint**,
  here or live: `trade_planner.py` solves `take_profit` to hit exactly its OWN configured target
  (`trade_planner.min_risk_reward=2.0`), so every plan's risk/reward comes out at essentially 2.0
  by construction -- comfortably above the risk engine's 1.5 floor.
- **Survivorship warning** (same as the strategy-pack reports): the universe is today's 16 coins
  only. Coins that were delisted or collapsed are missing, which flatters results.


## Results, by cost assumption

| costs             | trades | win%  | avg R | avg R 95% CI | PF   | return | maxDD  | Sharpe | exposure |
|-------------------|--------|-------|-------|--------------|------|--------|--------|--------|----------|
| paper_today_0.10% | 546    | 47.8% | 0.56  | [0.40, 0.72] | 1.58 | 444.0% | -26.4% | 1.14   | 14.4%    |
| kraken_T3_maker   | 546    | 47.8% | 0.53  | [0.37, 0.69] | 1.53 | 389.3% | -27.6% | 1.08   | 14.4%    |
| kraken_T3_taker   | 548    | 47.6% | 0.49  | [0.33, 0.65] | 1.49 | 342.1% | -28.7% | 1.01   | 14.4%    |
| kraken_T1_taker   | 549    | 47.5% | 0.40  | [0.25, 0.57] | 1.38 | 236.5% | -31.4% | 0.84   | 14.4%    |


Benchmarks over the same window (no costs):

| benchmark             | return   | maxDD  | Sharpe |
|-----------------------|----------|--------|--------|
| BTC buy&hold          | 29741.5% | -83.8% | 1.10   |
| equal-weight buy&hold | 3255.3%  | -84.9% | 0.81   |


Fixed-rule split at 2023-05-22 (costs: kraken_T3_taker; nothing was tuned, so out-of-sample = later trades):

| period    | trades | win%  | avg R |
|-----------|--------|-------|-------|
| first 70% | 313    | 52.4% | 0.67  |
| last 30%  | 235    | 41.3% | 0.25  |


Time-shift placebo (200 sims -- adapted from engine.placebo_baseline's pattern, see backtest_live_bot.placebo_run's docstring for exactly how: same per-symbol signal timing, circularly shifted by one random offset shared by all symbols, plans rebuilt against the REAL price/ATR at each shifted landing bar, costs kraken_T3_taker): the real strategy's return ranks at the 100th percentile and avg R at the 98th percentile of the placebos.



## GATES (costs: kraken_T3_taker)

| strategy      | >=100 trades | avg R CI low > 0 | later 30% avg R > 0 | beats placebo >=90th pct | net return > 0 and Sharpe > buy&hold |
|---------------|--------------|------------------|---------------------|--------------------------|--------------------------------------|
| live_strategy | PASS         | PASS             | PASS                | PASS                     | PASS                                 |


This is a diagnostic backtest of the bot's OWN live strategy (not a candidate replacement). All 5 gates passing is evidence the strategy's logic has held up reasonably well on this data; any FAIL is a specific, named thing to investigate before trusting the live paper results -- not an instruction to change scanner/signal-engine/trade-planner/risk-engine code without its own written reason and out-of-sample test, per this repo's CLAUDE.md.
