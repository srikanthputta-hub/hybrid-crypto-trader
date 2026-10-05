# Live-strategy backtest report

Data: data_15m (16 symbols, 1h bars). Bot pipeline: src/scanner.py -> src/signal_engine.py -> src/htf_filter.py (enabled=True) -> src/trade_planner.py -> src/risk_engine.py -> src/monitor.py, imported read-only and unmodified. Long-only spot, next-open fills for every trigger (FILL/DISCARD/STOP_LOSS/TAKE_PROFIT).


Window: 2024-10-12 to 2026-09-30 (17217 bars). Raw bullish+HTF-confirmed signals detected across all symbols before any risk-engine gating: 8073.

## Important limits on what this report can tell you

- **1h bars, matching the live feed's own resolution (`config/config.yaml`'s `market.timeframe:
  "1h"`) for the first time** -- this is the Phase 2.0 re-baseline
  (docs/system-redesign-2026-10.md §5.4), not the earlier daily-bar diagnostic. The 1h candles
  here are DERIVED by resampling Coinbase Exchange 15m history (see resample_ohlcv.py), not
  separately fetched, so they're internally consistent but still a Coinbase replay, not a replay
  of actual Kraken fills -- same caveat class as the daily `long` dataset.
- **`risk.cooldown_minutes` and `risk.max_daily_loss_pct` are BINDING here, for the first time.**
  At daily resolution both were structurally inert (see `backtest_live_bot.py`'s module
  docstring); at 1h spacing, a cooldown can span multiple bars and a day can contain many
  evaluations against one fixed daily-loss baseline, so this report is the first real test of
  whether those two risk-engine gates behave sanely at the strategy's own live cadence.
- **The risk engine's own R:R gate (`risk.min_risk_reward=1.5`) is never the binding constraint**,
  here or live: `trade_planner.py` solves `take_profit` to hit exactly its OWN configured target
  (`trade_planner.min_risk_reward=2.0`), so every plan's risk/reward comes out at essentially 2.0
  by construction -- comfortably above the risk engine's 1.5 floor.
- **Survivorship warning** (same as the strategy-pack reports): the universe is today's 16 coins
  only. Coins that were delisted or collapsed are missing, which flatters results.
- **No multi-timeframe change.** This is the SAME scanner/signal-engine/htf_filter/trade-planner/
  risk-engine/monitor pipeline as every other live-strategy report, just replayed at 1h instead of
  1d. It deliberately does not add or test any multi-timeframe confluence logic (that's a later,
  separate phase, gated on this report's own result).


## Results, by cost assumption

| costs             | trades | win%  | avg R | avg R 95% CI   | PF   | return | maxDD  | Sharpe | exposure |
|-------------------|--------|-------|-------|----------------|------|--------|--------|--------|----------|
| paper_today_0.10% | 959    | 34.3% | -0.02 | [-0.13, 0.09]  | 0.88 | -24.2% | -50.9% | -0.59  | 22.2%    |
| kraken_T3_maker   | 965    | 34.8% | -0.13 | [-0.23, -0.02] | 0.78 | -41.7% | -59.9% | -1.25  | 22.2%    |
| kraken_T3_taker   | 959    | 35.0% | -0.24 | [-0.35, -0.13] | 0.71 | -53.9% | -66.8% | -1.83  | 22.2%    |
| kraken_T1_taker   | 950    | 34.4% | -0.56 | [-0.67, -0.45] | 0.57 | -74.2% | -79.8% | -3.30  | 22.1%    |


Benchmarks over the same window (no costs):

| benchmark             | return | maxDD  | Sharpe |
|-----------------------|--------|--------|--------|
| BTC buy&hold          | 33.1%  | -53.9% | 0.55   |
| equal-weight buy&hold | -9.7%  | -77.0% | 0.27   |


Fixed-rule split at 2026-02-26 (costs: kraken_T3_taker; nothing was tuned, so out-of-sample = later trades):

| period    | trades | win%  | avg R |
|-----------|--------|-------|-------|
| first 70% | 644    | 33.4% | -0.27 |
| last 30%  | 315    | 38.4% | -0.18 |


Time-shift placebo (200 sims -- adapted from engine.placebo_baseline's pattern, see backtest_live_bot.placebo_run's docstring for exactly how: same per-symbol signal timing, circularly shifted by one random offset shared by all symbols, plans rebuilt against the REAL price/ATR at each shifted landing bar, costs kraken_T3_taker): the real strategy's return ranks at the 92th percentile and avg R at the 100th percentile of the placebos.



## GATES (costs: kraken_T3_taker)

| strategy      | >=100 trades | avg R CI low > 0 | later 30% avg R > 0 | beats placebo >=90th pct | net return > 0 and Sharpe > buy&hold |
|---------------|--------------|------------------|---------------------|--------------------------|--------------------------------------|
| live_strategy | PASS         | FAIL             | FAIL                | PASS                     | FAIL                                 |


This is a diagnostic backtest of the bot's OWN live strategy (not a candidate replacement). All 5 gates passing is evidence the strategy's logic has held up reasonably well on this data; any FAIL is a specific, named thing to investigate before trusting the live paper results -- not an instruction to change scanner/signal-engine/trade-planner/risk-engine code without its own written reason and out-of-sample test, per this repo's CLAUDE.md.
