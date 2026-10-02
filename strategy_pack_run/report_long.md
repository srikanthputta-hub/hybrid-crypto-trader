# Backtest report

Data: data_long (16 symbols, 1d bars). Regime: BTC daily SMA(200). Long-only spot, next-open fills.

Survivorship warning: the universe is today's coins only. Coins that were delisted or collapsed are missing, which flatters results. Late-listed coins join the test when their data starts.


## trend_ensemble

Window after warm-up: 2016-02-05 to 2026-10-01 (3892 bars, 16 symbols).

| costs             | trades | win%  | avg R | avg R 95% CI | PF   | return | maxDD  | Sharpe | exposure |
|-------------------|--------|-------|-------|--------------|------|--------|--------|--------|----------|
| paper_today_0.10% | 683    | 34.8% | 0.25  | [0.12, 0.39] | 1.60 | 197.7% | -27.5% | 0.72   | 13.0%    |
| kraken_T3_maker   | 682    | 34.2% | 0.23  | [0.10, 0.37] | 1.54 | 174.5% | -29.0% | 0.67   | 13.0%    |
| kraken_T3_taker   | 682    | 33.7% | 0.21  | [0.09, 0.35] | 1.49 | 154.0% | -30.6% | 0.62   | 13.0%    |
| kraken_T1_taker   | 682    | 32.1% | 0.17  | [0.05, 0.31] | 1.35 | 107.3% | -34.5% | 0.50   | 13.0%    |


Benchmarks over the same window (no costs):

| benchmark             | return   | maxDD  | Sharpe |
|-----------------------|----------|--------|--------|
| BTC buy&hold          | 21435.6% | -83.8% | 1.09   |
| equal-weight buy&hold | 2736.2%  | -85.4% | 0.81   |


Fixed-rule split at 2023-07-21 (costs: kraken_T3_taker; nothing was tuned, so out-of-sample = later trades):

| period    | trades | win%  | avg R |
|-----------|--------|-------|-------|
| first 70% | 407    | 37.6% | 0.19  |
| last 30%  | 275    | 28.0% | 0.25  |


Time-shift placebo (200 sims: same signals shifted to random dates, same prices and costs kraken_T3_taker): the real signals' return ranks at the 97th percentile and avg R at the 98th percentile of the placebos.


Parameter robustness (atr_mult x min_votes). Look for a plateau, not one lucky cell:

| atr_mult | min_votes | trades | avg R | return | maxDD  |
|----------|-----------|--------|-------|--------|--------|
| 2        | 1         | 1193   | 0.14  | 112.1% | -35.2% |
| 2        | 2         | 1024   | 0.17  | 153.0% | -29.0% |
| 2        | 3         | 853    | 0.18  | 152.8% | -29.8% |
| 3        | 1         | 848    | 0.21  | 149.0% | -30.5% |
| 3        | 2         | 682    | 0.21  | 154.0% | -30.6% |
| 3        | 3         | 551    | 0.24  | 132.6% | -24.2% |
| 4        | 1         | 677    | 0.32  | 294.9% | -27.5% |
| 4        | 2         | 525    | 0.35  | 233.6% | -25.4% |
| 4        | 3         | 399    | 0.47  | 218.9% | -24.0% |

## tsmom

Window after warm-up: 2016-02-05 to 2026-10-01 (3892 bars, 16 symbols).

| costs             | trades | win%  | avg R | avg R 95% CI | PF   | return | maxDD  | Sharpe | exposure |
|-------------------|--------|-------|-------|--------------|------|--------|--------|--------|----------|
| paper_today_0.10% | 815    | 42.7% | 0.27  | [0.17, 0.39] | 1.95 | 281.3% | -24.7% | 0.89   | 11.6%    |
| kraken_T3_maker   | 815    | 41.2% | 0.25  | [0.15, 0.37] | 1.85 | 245.5% | -25.2% | 0.83   | 11.6%    |
| kraken_T3_taker   | 815    | 39.8% | 0.24  | [0.13, 0.36] | 1.75 | 214.6% | -25.7% | 0.77   | 11.6%    |
| kraken_T1_taker   | 814    | 36.7% | 0.19  | [0.09, 0.31] | 1.53 | 145.7% | -26.9% | 0.62   | 11.6%    |


Benchmarks over the same window (no costs):

| benchmark             | return   | maxDD  | Sharpe |
|-----------------------|----------|--------|--------|
| BTC buy&hold          | 21435.6% | -83.8% | 1.09   |
| equal-weight buy&hold | 2736.2%  | -85.4% | 0.81   |


Fixed-rule split at 2023-07-21 (costs: kraken_T3_taker; nothing was tuned, so out-of-sample = later trades):

| period    | trades | win%  | avg R |
|-----------|--------|-------|-------|
| first 70% | 488    | 43.0% | 0.19  |
| last 30%  | 327    | 34.9% | 0.30  |


Time-shift placebo (200 sims: same signals shifted to random dates, same prices and costs kraken_T3_taker): the real signals' return ranks at the 100th percentile and avg R at the 100th percentile of the placebos.



## GATES (costs: kraken_T3_taker)

| strategy       | >=100 trades | avg R CI low > 0 | later 30% avg R > 0 | beats placebo >=90th pct | net return > 0 and Sharpe > buy&hold |
|----------------|--------------|------------------|---------------------|--------------------------|--------------------------------------|
| trend_ensemble | PASS         | PASS             | PASS                | PASS                     | FAIL                                 |
| tsmom          | PASS         | PASS             | PASS                | PASS                     | FAIL                                 |


A strategy is only a candidate for paper trading if every gate passes. With fewer than 100 trades, treat any result as a smoke test, not evidence.
