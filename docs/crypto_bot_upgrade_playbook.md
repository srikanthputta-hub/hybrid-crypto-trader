# Crypto Bot Upgrade Playbook (16-coin system)

Prepared 1 Oct 2026. Research summary plus copy-paste specs and prompts for your Claude agents.

**Read this first.** This is research, not financial advice, and I'm not a financial advisor. Every performance figure below comes from a backtest or a practitioner write-up, and backtests routinely fail live (see Section 6). Nothing here guarantees profit. Crypto trading can lose all of your capital, so keep size small until each gate in Section 6 is passed.

---

## 1. Bottom line

1. **Losing 19 of 20 trades is a bug or a structural flaw, not a missing indicator.** If your entries were no better than a coin flip you would expect about 10 wins in 20. The chance of 1 or fewer wins in 20 trades is about 0.002% at a true 50% win rate, about 0.05% at 40%, about 0.8% at 30%, and about 7% even at 20%. Something systematic is wrong (inverted signal, stops inside normal noise, costs larger than the target, look-ahead in the backtest, bad fills). Fix this before adding anything.
2. **More indicators usually make it worse.** In one honest 3-year test of a simple Donchian bot, adding indicators cut results from +3.0% to -2.4%, and spreading across more coins lowered Sharpe from 0.38 to 0.30. Simple rules, few parameters, slow timeframe.
3. **The best-evidenced approach for crypto is long-biased trend following on liquid coins, sized by volatility, with a market regime filter and trailing stops.** It wins 30-45% of the time (typical for trend systems, my general knowledge, not from the sources below) but its winners are several times larger than its losers. A win rate near 5% is a signature of something broken, while a win rate near 40% with 3x winners is healthy.
4. **Do not let the LLM pick trades.** Use Claude agents to build, audit, monitor and review. Keep entries, sizing and risk limits in deterministic code (Section 5).
5. **Costs decide everything on fast timeframes.** Move to 4h or daily bars, trade on bar close, and use maker orders where you can.

---

## 2. Step 0: diagnose the 19/20 before you change strategy

Run these checks in order. Each is a known cause of an extremely low win rate.

| # | Check | What it looks like | Fix |
|---|---|---|---|
| 1 | Signal direction / sign bug | Long when the rule says short, or exit logic that fires the instant you enter | Unit-test with a synthetic uptrend: the bot must go long and profit |
| 2 | Stop inside the noise | Stop distance below roughly 1x ATR, so normal wiggle stops you out | Set stops from ATR (see Section 4), never a fixed % |
| 3 | Costs larger than the target | Round trip of 2 x 0.055% taker (Bybit USDT perp base tier) plus an assumed 0.05% slippage per side is about 0.21%. On a 1% stop, 21% of your risk unit is gone before price moves. At 5 round trips a day that is about 380% of position size per year | Slower bars, fewer trades, post-only maker orders (0.02% on the same venue) |
| 4 | Repainting / forming-candle signals | Signal reads the current unfinished candle, flips, and you enter on a fake signal | Act only on closed candles |
| 5 | Backtest/live mismatch | Backtest looks great, live loses: look-ahead bias, no fees, fills at the exact signal price | Section 6 gates |
| 6 | Buying "oversold" in a downtrend | Mean-reversion entries (RSI, Bollinger) in a falling market keep getting run over | Regime filter, or drop mean reversion |
| 7 | Limit orders that only fill when price is moving against you | Fills cluster on losing trades | Log fill vs. subsequent price; use limit orders only with a defined timeout |
| 8 | 16 coins are mostly one bet | Most coins move with BTC, so one BTC drop stops out many positions at once | Measure correlations from your own data; cap simultaneous positions |
| 9 | Too few trades to judge | 20 trades is a small sample, but 1 win in 20 is already far outside chance | Log every signal, not only fills, and test the signal in isolation |

Paste Prompt A (Section 7) into Claude Code with your trade log to run this audit automatically.

---

## 3. What the evidence says (ranked by how much weight I'd give it)

**Stronger sources (papers/preprints), still backtests:**

| Strategy | Reported result | Caveats |
|---|---|---|
| Time-series momentum, long-only (ACFR/AUT paper) | Best setup: 28-day lookback, 5-day hold, Sharpe 1.51 after 15 bps costs vs market 0.85 | Works in bull markets only. Short-only loses in most cases and long-short underperforms long-only. Edge concentrated in large caps; small caps showed reversal instead |
| Cross-sectional momentum (same paper) | Only 6 of 21 portfolios beat the market; best was 14-day lookback / 7-day hold, Sharpe 1.28 vs 1.01 | Weak evidence. Shorts hurt by jump risk. Skip as a first strategy |
| Donchian-channel ensemble with volatility sizing (Zarattini, Pagani, Barbon, SSRN 2025) | Top-20 liquid coins since 2015, Sharpe above 1.5, about 10.8% annualized alpha vs BTC, net of fees | The paper itself says costs matter. I could not retrieve the exact lookback set or cost assumptions, so treat the lookback grid in Section 4 as my test grid, not the paper's values |
| Moving-average trend on BTC (Grayscale research, 2012-2023) | 50-day MA: Sharpe 1.9 vs 1.3 buy-and-hold. 20/100 crossover: 116% annualized, Sharpe 1.7. 150-day EMA: 126%, Sharpe 1.9 | BTC only, and the sample includes the huge early years. The main benefit is smaller drawdowns |
| AdaptiveTrend (arXiv 2602.11708) | 6-hour bars, ATR trailing stops, monthly rebalancing, 150+ Binance perps, 2022-24: Sharpe 2.41, max drawdown -12.7%, still above 2.0 at 8 bps costs | Single preprint with in-sample calibration. Claimed 1.07 Sharpe from monthly re-optimization is the kind of number that often fails live. Treat as a hypothesis |

**Practitioner write-ups (useful, lower rigor):**

- **Regime dominates everything.** In 78 mean-reversion backtests, average return was +16.3% in bull markets, -2.2% sideways and -40.6% in bear markets. Fourteen of the 78 had 65%+ win rates and still lost money, because small frequent wins are erased by rare large losses. Faster timeframes lost more (15m average -14.4%, 1h -8.1%, 4h -9.5%, daily -4.0%). Many cells had only 2-6 trades, and the tests deliberately had no stops or trend filter, so read as direction, not proof.
- **A BTC 200-day filter is a seatbelt, not an engine.** On ten large-cap pairs it cut max drawdown from -89.8% (naive) to -53.8% and lifted return from -7.7% to +10.8% annualized, but the equal-weight buy-and-hold benchmark still did better (+35.6%). The author's conclusion: defensive overlay, no alpha.
- **Keep filters plain.** A study of 20 regime filters across 2,700+ backtests (SPY, QQQ, BTC) found fancy smoothing such as TEMA and Hull MA among the worst, costing about 4% a year in whipsaw for little protection.
- **Simple trend can still lose to HODL after fees.** A 20-day Donchian breakout bot returned +4.33% in 3 years vs +127.77% for buy-and-hold BTC, with 0.26% taker fees. Daily bars: 14 trades and 1.3% annual fees. 4h: 61 trades, 11.4% fees, -6.01%. 1-minute: 52 trades, zero winners. Trend following mainly reduces drawdown and risk, so judge it on risk-adjusted return, and compare to buy-and-hold honestly.

**What does not have support:**

- LLM agents making the trading decisions. A long-horizon evaluation (FINSABER, tested on equities, 100+ symbols, up to 20 years) found LLM strategies do not sustainably beat the market. They were too conservative in bull markets and too aggressive in bear markets. A survey of 19 LLM trading systems found only 2 used time-consistent data splits and 1 modelled transaction costs.
- High-frequency or sub-hour strategies for a retail bot (fees dominate), martingale/grid, and stacking indicators.
- Funding-rate and open-interest signals: plenty of commentary, but I found no rigorous evidence in this pass. If you test them, use them only as a **risk filter** (reduce size when longs are crowded), never as a standalone entry.

---

## 4. Recommended v2 design (every number is a starting value to TEST, not a proven setting)

**Layer 1: Universe.** Rank your 16 coins by liquidity (30-day dollar volume) and market cap. Trade trend signals only on the large, liquid tier, since the momentum paper found small caps revert instead of trend. Put the rest on paper trading until they pass the same gates, or drop them. Compute the correlation matrix and the effective number of independent bets.

**Layer 2: Regime filter (long entries allowed only when ON).**
- BTC close above its 200-day SMA (or 150-day EMA).
- Optional: coin's own close above its 100-day SMA. Test ADX(14) above about 20-25 as an extra "is it trending" gate only if it improves out-of-sample results.
- Regime OFF means no new entries and tighter trailing stops. Long-only first: the evidence for shorting crypto is poor.

**Layer 3: Entry (closed candles only, 4h or 1d).**
- Donchian breakout ensemble: enter when close exceeds the N-bar high for several N at once. Test grid: 20, 40, 60, 90, 150 bars. Scale into the position by the number of lookbacks that agree (for example 1/5 per signal), which reduces parameter risk.
- Alternative or addition: time-series momentum, long if the 28-day return is positive, re-evaluate every 5 days.

**Layer 4: Position sizing.**
- Risk per trade: 0.5-1% of equity (common rule of thumb; up to 2% is the usual ceiling). Size = (equity x risk%) / (stop distance in price).
- Stop distance = k x ATR(14), start k = 3 for daily bars. Alternatively volatility-target each position so every coin contributes similar risk.
- Hard caps: max exposure per coin, max total gross exposure, max positions in the same correlation cluster.
- Leverage: start at 1x (no leverage) until the gates pass. Leverage mainly amplifies the bug you haven't found yet.

**Layer 5: Exits.**
- ATR trailing stop (chandelier style): highest close since entry minus k x ATR. Let winners run, no fixed small take-profit. A fixed small target with a wide stop is how you get a high win rate and negative expectancy.
- Exit all if the regime filter flips OFF (or tighten stops).

**Layer 6: Execution.**
- Signals on bar close, orders placed on the next bar open. Post-only limit with a timeout, then a market fallback only for stops.
- Model fees and slippage per fill from the real fee schedule of your exchange (Bybit base tier for USDT perps is 0.02% maker / 0.055% taker; spot 0.10% per side; funding is paid every 8 hours on perps, so include it). Confirm your own tier on the exchange, since the figure above came from a fee-comparison site.

**Layer 7: Circuit breakers (in code, not in a prompt).**
- Daily loss limit (for example 3%), total drawdown halt (for example 10%, then manual review), max trades per day, heartbeat/connection check, stale-data check, reject any order above the position cap.

**The expectancy rule to keep on your wall.** Expectancy per trade (in R, where 1R is your risk) = win% x average win - loss% x average loss.
- 40% wins averaging 3R, 60% losses at 1R: +0.6R per trade.
- 70% wins averaging 0.3R, 30% losses at 1R: **-0.09R** per trade, despite a "great" 70% win rate.
Judge the system on expectancy, profit factor and max drawdown, never on win rate alone.

---

## 5. Where Claude agents belong

Research on LLM trading agents is consistent: keep the LLM out of the fast decision loop, keep deterministic state read-only to it, log everything, and be suspicious of any result without time-consistent splits and cost models. So:

| Agent | Job | Allowed to |
|---|---|---|
| Quant Researcher | Proposes and codes strategy variants, runs backtests | Read data, write research code. No live keys |
| Backtest Auditor | Hunts look-ahead, survivorship, cost omissions, overfitting | Read-only on code and results; must produce a pass/fail report |
| Risk Officer | Enforces limits | **Plain code**, not an LLM. The LLM can only propose changes, a human approves |
| Execution Monitor | Watches fills, slippage, API errors, drift vs. backtest | Alerts only; may pause trading, may not open positions |
| Post-Trade Reviewer | Weekly review of trades vs. rules | Writes a report; never changes live parameters on its own |

Never give any agent withdrawal permission. Use exchange API keys with trade-only rights and an IP allowlist.

---

## 6. Validation gates (no real money until each passes)

1. **No look-ahead.** The strategy only ever receives `prices[:i+1]` at step `i`. Distrust smooth equity curves.
2. **Costs on from day one.** Real fees, funding, and slippage that grows with order size vs. book depth. One test showed 0.1% fees turning -6.3% into -11.3%.
3. **Out-of-sample.** Optimize on one period, judge only on unseen data (reserve about 30%). One author's tuned parameters made +2.4% in-sample and -1.5% on new data, and only 1 of 5 coins' parameter sets survived.
4. **Walk-forward** across several years covering bull, bear and sideways regimes.
5. **Include dead coins.** Backtesting on today's list of survivors inflates results.
6. **Parameter plateau.** Performance should hold across neighbouring parameter values. A single sharp peak is overfitting.
7. **Enough trades.** A result from 3 trades means nothing. Aim for 100+ trades across the whole portfolio and use a permutation/bootstrap test against random entries.
8. **Multiple-testing correction.** If you tried 50 variants, report the Deflated Sharpe Ratio / probability of backtest overfitting (Bailey and Lopez de Prado). Backtested Sharpe ratios are poor predictors of live results: a study of 888 algorithms found R-squared below 0.025.
9. **Paper trade 30+ days** and compare live fills to the backtest.
10. **Go live small**, with kill criteria written down in advance (for example, halt if drawdown exceeds the backtest's 95th-percentile drawdown).

Realistic expectation: live results usually come in well below the best published backtests, and crypto trend following has long flat or drawdown stretches. Plan for drawdowns much deeper than the -12.7% in the best preprint.

---

## 7. Copy-paste prompts for Claude Code

**Prompt A: Diagnose the 19/20 losses**

```
You are a trading-system auditor. Read [path to trade log / repo]. Do NOT change any strategy yet.
Goal: find out why about 19 of 20 recent trades lost.
1. For every trade compute: entry, exit, side, stop distance in ATR(14) units, fees paid, slippage vs signal price, holding time, R-multiple.
2. Test these hypotheses and report evidence for or against each: (a) signal direction/sign bug, (b) stop distance below 1x ATR, (c) round-trip cost as a % of stop distance, (d) signals computed on an unfinished candle, (e) backtest vs live fill mismatch, (f) entries against the BTC trend, (g) correlation: how many open positions were on highly correlated coins at the same time, (h) limit-order adverse selection.
3. Write a synthetic-uptrend and synthetic-downtrend unit test proving the bot buys/sells the correct way.
Output: ranked list of causes with evidence, then a minimal fix list. No new indicators.
```

**Prompt B: Backtest auditor**

```
Audit [backtest file/module] for look-ahead bias, survivorship bias, missing fees/funding/slippage, parameter overfitting, and too-small sample size.
Check that at step i the strategy only sees data up to and including bar i, and that orders execute at the NEXT bar's open, not at the signal bar's close.
Report pass/fail per item with file and line references, then re-run the backtest with: taker fees [0.055]%, slippage [0.05]% per side, funding included, 70/30 in-sample/out-of-sample split, and walk-forward over [start date] to [end date].
Report out-of-sample Sharpe, max drawdown, trade count, expectancy in R, and profit factor. Do not tune parameters on the out-of-sample set.
```

**Prompt C: Build the v2 strategy (deterministic rules)**

```
Implement a long-only trend-following strategy in [language/framework] with these rules and no others:
- Timeframe [4h or 1d], act only on closed candles, orders at next bar open.
- Universe: [list of liquid coins]. Regime ON only if BTC close > SMA(200) on daily bars.
- Entry: Donchian breakout ensemble with lookbacks [20,40,60,90,150]; position fraction = (number of lookbacks in agreement) / 5.
- Size: risk [0.75]% of equity per trade; stop distance = [3] x ATR(14); cap each coin at [x]% of equity and total exposure at [y]%.
- Exit: ATR chandelier trailing stop; exit if regime turns OFF.
- Costs: fees [..]%, slippage [..]%, funding included.
- Circuit breakers: daily loss [3]%, total drawdown [10]% halt, max [n] trades per day.
Write unit tests, then a walk-forward backtest report. Make every parameter a config value. Do not add indicators.
```

**Prompt D: Risk officer (deterministic module spec)**

```
Write a pure-Python risk module the order manager must call before every order. No LLM calls inside it.
It must reject an order if: position size exceeds cap, total exposure exceeds cap, daily loss limit reached, drawdown halt triggered, data is stale by more than [n] seconds, more than [k] positions already open in the same correlation cluster (correlation computed from the last [90] days of daily returns), or the exchange heartbeat failed.
Log every rejection with the reason. Include tests for each rule.
```

**Prompt E: Weekly post-trade review**

```
Read this week's trades and bot logs from [path]. Compare against the backtest: actual vs expected win rate, average R, fees and slippage per trade, and drawdown.
Flag anything outside the backtest's expected range. Suggest hypotheses only. Do not edit code or parameters. Output a one-page report with a clear "continue / pause / investigate" recommendation and the numbers behind it.
```

---

## 8. Suggested 30-day order of work

1. **Days 1-3:** Run Prompt A and fix whatever it finds. Stop live trading, or cut size to the minimum, while you do.
2. **Days 4-10:** Prompt B on the existing backtester until it passes the Section 6 gates 1-5.
3. **Days 10-20:** Build and test the v2 strategy (Prompt C) and the risk module (Prompt D). Compare it against buy-and-hold BTC on return and drawdown.
4. **Days 20-30+:** Paper trade, run the weekly review (Prompt E), and only then go live at small size.

---

## 9. Tooling: TA-Lib, Zipline, CCXT

Checked 1 Oct 2026. Companion file: `v2_signals.py` (starter module, 7 self-tests pass).

| Library | Verdict | Why |
|---|---|---|
| **CCXT** | Keep (data and orders) | One interface for 100+ exchanges. Perp symbols look like `BTC/USDT:USDT`. Has `fetchOHLCV` (paginate with `since`/`limit`), `fetchFundingRate` and `fetchFundingRateHistory`, `setSandboxMode(true)` for testnet (sandbox keys differ from live keys), and `enableRateLimit`. Websockets need the separate CCXT Pro package |
| **TA-Lib** | Keep (indicators: ATR, SMA, ADX) | `pip install TA-Lib` now works without compiling. Version 0.8.1 is on PyPI with prebuilt Linux, macOS and Windows wheels that bundle the C library (since 0.6.5), for Python 3.9-3.14. I installed and ran it in my sandbox. It has no dedicated Donchian function, so use rolling max/min (as `v2_signals.py` does). A big indicator library invites stacking indicators, which the evidence in Section 3 argues against |
| **Zipline** | Don't use as your core | The maintained fork (zipline-reloaded 3.1.1, July 2025, Python 3.10-3.13) is built around US-equity research. Two practitioner write-ups say it is awkward for crypto and arbitrary data sources because of its data-bundle pattern, and one says it is slower than vectorized tools for parameter sweeps. Its strengths (point-in-time data, corporate actions) matter little for crypto. It has no built-in model of 24/7 markets or perpetual funding |

**Cautions when using CCXT (from its manual):**
- The manual does not say whether the newest candle returned by `fetchOHLCV` is complete, so always drop the forming candle (the module does this).
- Use `amountToPrecision` and check the market's minimum amount before ordering. Precision and minimum limits are separate checks.
- Handle `InvalidNonce`, `DDoSProtection`, `ExchangeNotAvailable` and HTTP 429 with retries.
- If you backtest in Zipline, check its default commission and slippage models. They were designed for equities, so confirm they are replaced with your real exchange fees (I did not verify the defaults in this pass).

**Two sensible paths:**
1. **Own stack, fewer moving parts:** CCXT + TA-Lib + pandas, with a small backtester that executes at the next bar's open. Use vectorbt or backtesting.py only for fast parameter sweeps (vectorbt's open-source version is in maintenance mode, and backtesting.py has no live trading).
2. **Adopt Freqtrade:** it already combines CCXT, TA-Lib, backtesting, hyperopt, dry-run mode, futures/shorting support and lookahead-analysis and recursive-analysis tools. I only read its homepage and have not run it, so test it before relying on it.

**Fastest way to find a backtest bug (Prompt F):** run the same simple rules in two independent engines. If the trade lists differ, one has a bug, and with 19 of 20 losing, that is exactly what you need to find.

```
Take the strategy rules in [path/STRATEGY_SPEC.md]. Implement them twice: (1) in our existing backtester [path], (2) in an independent minimal pandas loop that executes at the next bar's open with fees [..]% and slippage [..]% per side.
Run both on [coin] [timeframe] from [start] to [end]. Diff the trade lists (entry time, exit time, side, price, size).
For every difference, find the cause and say which engine is wrong. Do not change the strategy rules. Output a table of mismatches and the fix.
```

**Using the starter module (the live exchange calls are untested; only the closed-candle logic was tested, using a fake exchange):**

```python
import ccxt
from research.v2_signals import fetch_closed_ohlcv, regime_on, build_signals, position_qty

ex = ccxt.bybit({"enableRateLimit": True})            # your exchange
btc_d = fetch_closed_ohlcv(ex, "BTC/USDT:USDT", "1d", 1000)
df = fetch_closed_ohlcv(ex, "ETH/USDT:USDT", "4h", 1000)
sig = build_signals(df, regime_on(btc_d, df.index))
last = sig.iloc[-1]                                   # last CLOSED bar
qty = position_qty(equity, last.close, last.atr, last.target_frac)
# Compare qty with your current position, then place the order at the NEXT bar open.
```

**Note on this repo's copy (`research/v2_signals.py`):** the original module above used
TA-Lib for SMA/ATR. This repo's copy swaps those for `src/indicators.py`'s own
pure-pandas `sma`/`atr` instead (same Wilder-smoothing ATR convention, same plain
rolling-mean SMA), since `src/indicators.py` already exists specifically to avoid a
TA-Lib dependency, and the deploy VM doesn't have it installed. Verified numerically
equivalent before trusting it — see CLAUDE.md.

---

## Sources

- [TA-Lib on PyPI](https://pypi.org/project/TA-Lib/), [zipline-reloaded on PyPI](https://pypi.org/project/zipline-reloaded/), [CCXT manual](https://docs.ccxt.com/en/latest/manual.html), [Freqtrade docs](https://www.freqtrade.io/en/stable/)
- [Python Backtesting Frameworks (2026) compared](https://quanttradingtools.com/python-backtesting-frameworks/) and [What the 2026 Python Backtesting Lists Are Missing](https://dev.to/abed_builds/what-the-2026-python-backtesting-lists-are-missing-432c)
- [Time-Series and Cross-Sectional Momentum in the Cryptocurrency Market (ACFR/AUT)](https://acfr.aut.ac.nz/__data/assets/pdf_file/0009/918729/Time_Series_and_Cross_Sectional_Momentum_in_the_Cryptocurrency_Market_with_IA.pdf)
- [Catching Crypto Trends (Zarattini, Pagani, Barbon, SSRN)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5209907) and [Concretum summary](https://concretumgroup.com/catching-crypto-trends-a-tactical-approach-for-bitcoin-and-altcoins/)
- [The Trend is Your Friend (Grayscale Research)](https://research.grayscale.com/reports/the-trend-is-your-friend-managing-bitcoins-volatility-with-momentum-signals)
- [AdaptiveTrend: Systematic Trend-Following with Adaptive Portfolio Construction (arXiv)](https://arxiv.org/html/2602.11708v1)
- [Mean Reversion in Crypto: Evidence from 78 Backtests (Coinquant)](https://www.coinquant.ai/blog/building-a-mean-reversion-strategy-in-cryptocurrency-markets-evidence-from-78-backtests)
- [BTC 200-day filter momentum backtest (GitHub)](https://github.com/IsaacDodds/crypto-momentum-backtest)
- [I Tested 20 Trend-Based Regime Filters](https://setup4alpha.substack.com/p/i-tested-20-trend-based-regime-filters)
- [I built a crypto trading bot. It lost to doing nothing (DEV)](https://dev.to/nar1frames/i-built-a-crypto-trading-bot-it-lost-to-doing-nothing-355a)
- [Your Trading Bot's Backtest Is Lying to You (DEV)](https://dev.to/aurora_systems/your-trading-bots-backtest-is-lying-to-you-here-are-the-4-ways-5044)
- [Why Most Trading Bots Lose Money (ForTraders)](https://fortraders.com/blog/trading-bots-lose-money) (used only for the 888-algorithm study and generic risk rules; its other statistics are unverified and not used)
- [Agentic Trading: When LLM Agents Meet Financial Markets (arXiv survey)](https://arxiv.org/html/2605.19337v1)
- [Can LLM-based Financial Investing Strategies Outperform the Market in Long Run? (FINSABER, arXiv)](https://arxiv.org/html/2505.07078v5)
- [Bybit fees (Trade Reclaim)](https://trade-reclaim.com/en/blog/bybit-fees) (fee-comparison site, verify on the exchange)
- [The Probability of Backtest Overfitting (Bailey et al.)](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf) and [Deflated Sharpe Ratio](https://en.wikipedia.org/wiki/Deflated_Sharpe_ratio)
