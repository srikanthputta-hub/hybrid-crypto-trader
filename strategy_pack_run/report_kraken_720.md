# Backtest report

Data: data (16 symbols, 1d bars). Regime: BTC daily SMA(200). Long-only spot, next-open fills.

Survivorship warning: the universe is today's coins only. Coins that were delisted or collapsed are missing, which flatters results. Late-listed coins join the test when their data starts.


## trend_ensemble

Window after warm-up: 2025-04-30 to 2026-10-01 (520 bars, 16 symbols).

| costs             | trades | win%  | avg R | avg R 95% CI  | PF   | return | maxDD | Sharpe | exposure |
|-------------------|--------|-------|-------|---------------|------|--------|-------|--------|----------|
| paper_today_0.10% | 81     | 44.4% | 0.29  | [0.05, 0.55]  | 2.60 | 18.3%  | -8.3% | 0.94   | 14.8%    |
| kraken_T3_maker   | 81     | 44.4% | 0.26  | [0.03, 0.52]  | 2.43 | 17.0%  | -8.6% | 0.88   | 14.8%    |
| kraken_T3_taker   | 81     | 42.0% | 0.24  | [0.01, 0.50]  | 2.27 | 15.7%  | -8.9% | 0.83   | 14.8%    |
| kraken_T1_taker   | 81     | 42.0% | 0.18  | [-0.05, 0.44] | 1.92 | 12.6%  | -9.7% | 0.68   | 14.8%    |


Benchmarks over the same window (no costs):

| benchmark             | return | maxDD  | Sharpe |
|-----------------------|--------|--------|--------|
| BTC buy&hold          | -11.4% | -53.1% | 0.00   |
| equal-weight buy&hold | -28.9% | -68.5% | -0.12  |


Fixed-rule split at 2026-04-28 (costs: kraken_T3_taker; nothing was tuned, so out-of-sample = later trades):

| period    | trades | win%   | avg R |
|-----------|--------|--------|-------|
| first 70% | 70     | 32.9%  | 0.00  |
| last 30%  | 11     | 100.0% | 1.78  |


Time-shift placebo (200 sims: same signals shifted to random dates, same prices and costs kraken_T3_taker): the real signals' return ranks at the 92th percentile and avg R at the 85th percentile of the placebos.


Parameter robustness (atr_mult x min_votes). Look for a plateau, not one lucky cell:

| atr_mult | min_votes | trades | avg R | return | maxDD  |
|----------|-----------|--------|-------|--------|--------|
| 2        | 1         | 133    | 0.17  | 14.5%  | -11.3% |
| 2        | 2         | 111    | 0.18  | 14.4%  | -8.9%  |
| 2        | 3         | 97     | 0.17  | 12.7%  | -9.6%  |
| 3        | 1         | 110    | 0.18  | 11.6%  | -12.9% |
| 3        | 2         | 81     | 0.24  | 15.7%  | -8.9%  |
| 3        | 3         | 66     | 0.27  | 13.1%  | -10.0% |
| 4        | 1         | 98     | 0.14  | 7.9%   | -11.5% |
| 4        | 2         | 65     | 0.27  | 10.4%  | -9.6%  |
| 4        | 3         | 47     | 0.38  | 13.8%  | -8.1%  |

## tsmom

Window after warm-up: 2025-04-30 to 2026-10-01 (520 bars, 16 symbols).

| costs             | trades | win%  | avg R | avg R 95% CI | PF   | return | maxDD  | Sharpe | exposure |
|-------------------|--------|-------|-------|--------------|------|--------|--------|--------|----------|
| paper_today_0.10% | 109    | 60.6% | 0.28  | [0.12, 0.46] | 2.56 | 18.2%  | -8.3%  | 0.92   | 13.8%    |
| kraken_T3_maker   | 109    | 60.6% | 0.26  | [0.10, 0.44] | 2.38 | 16.6%  | -8.8%  | 0.85   | 13.8%    |
| kraken_T3_taker   | 109    | 59.6% | 0.24  | [0.08, 0.41] | 2.22 | 15.2%  | -9.2%  | 0.79   | 13.8%    |
| kraken_T1_taker   | 109    | 57.8% | 0.18  | [0.02, 0.36] | 1.85 | 11.4%  | -10.4% | 0.62   | 13.8%    |


Benchmarks over the same window (no costs):

| benchmark             | return | maxDD  | Sharpe |
|-----------------------|--------|--------|--------|
| BTC buy&hold          | -11.4% | -53.1% | 0.00   |
| equal-weight buy&hold | -28.9% | -68.5% | -0.12  |


Fixed-rule split at 2026-04-28 (costs: kraken_T3_taker; nothing was tuned, so out-of-sample = later trades):

| period    | trades | win%  | avg R |
|-----------|--------|-------|-------|
| first 70% | 88     | 54.5% | 0.09  |
| last 30%  | 21     | 81.0% | 0.87  |


Time-shift placebo (200 sims: same signals shifted to random dates, same prices and costs kraken_T3_taker): the real signals' return ranks at the 96th percentile and avg R at the 88th percentile of the placebos.



## GATES (costs: kraken_T3_taker)

| strategy       | >=100 trades | avg R CI low > 0 | later 30% avg R > 0 | beats placebo >=90th pct | net return > 0 and Sharpe > buy&hold |
|----------------|--------------|------------------|---------------------|--------------------------|--------------------------------------|
| trend_ensemble | FAIL         | PASS             | PASS                | FAIL                     | PASS                                 |
| tsmom          | PASS         | PASS             | PASS                | FAIL                     | PASS                                 |


A strategy is only a candidate for paper trading if every gate passes. With fewer than 100 trades, treat any result as a smoke test, not evidence.
