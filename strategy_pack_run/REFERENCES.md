# References and what the code takes from them

Checked 1 Oct 2026. "Read" means I opened the page and took the numbers from it. Backtest results are not forecasts.

## Strategy 1: `trend_ensemble` (Donchian ensemble, trend following)

| Source | Timeframe | What it says | Read? |
|---|---|---|---|
| Zarattini, Pagani, Barbon, "Catching Crypto Trends" (April 2025). [SSRN 5209907](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5209907), [PDF](https://concretumgroup.com/wp-content/uploads/2026/02/Catching-Crypto-Trends.pdf) | **Daily OHLCV** | Nine Donchian lookbacks (5, 10, 20, 30, 60, 90, 150, 250, 360 days), equal-weighted. Trailing stop = max(previous stop, channel midpoint), never lowered. Top 20 liquid coins, selected monthly (median daily volume above $2M), monthly rebalance, 25% volatility target. Sample Jan 2015 to Mar 2025. **Costs assumed: 10 bps.** Headline: Sharpe above 1.5, about 10.8% annualized alpha vs Bitcoin | Yes (PDF); the 25% target is from the Concretum summary |
| Grayscale Research, ["The Trend is Your Friend"](https://research.grayscale.com/reports/the-trend-is-your-friend-managing-bitcoins-volatility-with-momentum-signals) | **Daily** moving averages on BTC (50-day, 20/100-day crossover, 150-day EMA), 2012-2023 | Sharpe 1.7-1.9 vs 1.3 for buy-and-hold; main benefit is smaller drawdowns | Yes |
| "AdaptiveTrend", [arXiv 2602.11708](https://arxiv.org/html/2602.11708v1) | **6-hour** candles, monthly rebalancing, 150+ Binance perps, 2022-24 | Sharpe 2.41 with ATR trailing stops. Single preprint, treat as a hypothesis | Yes |
| BTC 200-day trend filter test, [GitHub](https://github.com/IsaacDodds/crypto-momentum-backtest) | Daily, ten large-cap pairs | Filter cut max drawdown (-89.8% to -53.8%) but added no alpha. A hobby backtest, low rigor | Yes |

## Strategy 2: `tsmom` (time-series momentum)

| Source | Timeframe | What it says | Read? |
|---|---|---|---|
| ACFR/AUT, ["Time-Series and Cross-Sectional Momentum in the Cryptocurrency Market"](https://acfr.aut.ac.nz/__data/assets/pdf_file/0009/918729/Time_Series_and_Cross_Sectional_Momentum_in_the_Cryptocurrency_Market_with_IA.pdf) | **Daily** lookback (28 days) and holding (5 days) | Sharpe 1.51 after 15 bps costs vs 0.85 for the market. Works in bull markets, long-only beats long-short, strongest in large caps | Yes |
| Liu and Tsyvinski, "Risks and Returns of Cryptocurrency", [NBER w24877](https://www.nber.org/system/files/working_papers/w24877/w24877.pdf), [RFS 34(6), 2021](https://academic.oup.com/rfs/article-abstract/34/6/2689/5912024) | **Weekly** returns predicting 1-4 weeks ahead, also daily returns predicting 1-7 days | Momentum is stronger at short horizons, and weaker for Ethereum (reversals at 5 days). These are return-prediction tests, not a costed trading rule | Yes |
| Moskowitz, Ooi, Pedersen, ["Time Series Momentum"](https://w4.stern.nyu.edu/facdir/lpederse/papers/TimeSeriesMomentum.pdf), Journal of Financial Economics 104(2), 2012 | **Monthly** (12-month lookback, 1-month hold), 58 futures, not crypto | The original time-series momentum paper. Crypto's useful horizons look much shorter | Yes |

## Evidence on fast timeframes (why not 1h)

* Coinquant, [78 mean-reversion backtests](https://www.coinquant.ai/blog/building-a-mean-reversion-strategy-in-cryptocurrency-markets-evidence-from-78-backtests): average return by timeframe was 15m -14.4%, 1h -8.1%, 4h -9.5%, daily -4.0% (mean-reversion rules, many tiny samples).
* [A Donchian bot that lost to buy-and-hold](https://dev.to/nar1frames/i-built-a-crypto-trading-bot-it-lost-to-doing-nothing-355a): daily bars +4.33% with 14 trades; 4h -6.01% with 61 trades; 1-minute -25.18%, all driven by fees.
* I found no study showing a robust edge from 1h pattern rules in crypto after realistic costs.

## Found but not read

* Fieberg, Liedtke, Poddig, Walker, Zaremba, ["A Trend Factor for the Cross-Section of Cryptocurrency Returns"](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4601972) (page rate-limited).
* ["Cryptocurrency momentum has (not) its moments"](https://link.springer.com/article/10.1007/s11408-025-00474-9) (Springer, 2025; page rate-limited). Worth reading as a counterpoint.

## How `strategies.py` differs from the papers (my design choices, not published results)

| Piece | Published | In the pack |
|---|---|---|
| Donchian lookbacks | 9 horizons, 5 to 360 days | 5 horizons, 10 to 100 days |
| Entry rule | Each horizon is its own sub-portfolio | A position opens when at least 2 horizons are long |
| Stop | Donchian channel midpoint | 3 x ATR(14) chandelier |
| Sizing | 25% volatility target, monthly rebalance | 0.75% equity risk per position, 10% and 40% caps |
| Coin selection | Top 20 by volume, reselected monthly | Your fixed 16-coin watchlist |
| Regime filter | None | BTC above its 200-day SMA (from the BTC-filter test above) |
| Costs in the paper | 10 bps | Your costs: Kraken spot is 35-80 bps per side at the taker tiers |

So `trend_ensemble` is inspired by the Zarattini et al. design, not a replication. `tsmom` follows the AUT 28-day / 5-day setting with a regime filter and stop added.
