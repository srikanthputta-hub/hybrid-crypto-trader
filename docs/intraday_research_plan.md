# Intraday (faster, 24/7) bot: research plan (pre-registered, paper only)

**Status:** plan only. No `src/`, config or live-bot change. The daily-bar paper trial keeps running untouched.
Owner direction (2026-10-20): the goal of the system is a faster, 24/7 intraday bot. This document says how we get
there without fooling ourselves. Evidence rules are `CLAUDE.md` + the 5-gate method used in every earlier study.

## 1. The constraint everything is judged against

Kraken T3 taker costs 0.38% fee + 0.05% slippage per side = **0.86% round trip** (`kraken_T3_taker`). The existing
1h strategy earned about **0.21% gross per trade** (`p2_0_1h_edge_diagnosis.md`), i.e. it lost -0.24R net. Trading
faster only helps if the gross edge per trade clears that cost, or if the cost per trade drops. A candidate
therefore has to show, up front: **gross edge per trade (in % of price) > 0.86%** at taker costs, OR a measured
maker-fill model (entry limit orders: fill rate, adverse selection, missed trades) that makes the net positive.
Maker numbers are *reported* for sensitivity but **gate 5 is always judged at taker costs** until a real maker-fill
record exists (paper fills would flatter it).

## 2. Phases (each ends with a written report and an independent review)

| Phase | What | Output / pass condition |
|---|---|---|
| P0 | **Data feasibility.** Kraken's public OHLC returns only the last ~720 bars per pair; 15m/1h history of useful length needs another source or trade-tape reconstruction. Decide the source, check it against Kraken USD bars (same close within tolerance), record survivorship limits. | `docs/intraday_data.md`; at least 3 years of 15m/1h for the 16 coins, or a clear "not available" |
| P1 | **Intraday harness.** Reuse `strategy_pack_run` engine; add timeframe-parametric bars, per-bar funding/slippage tiers, turnover and capacity reports. Parity rules P1-P6 apply to any live version. | harness tests green; byte-identical rebaseline on the daily config |
| P2 | **Pre-registered candidates** (section 3). Grid sizes and gates fixed before any run. | one report per family, honest FAILs included |
| P3 | **Gate run + review.** Winners (if any) get a differential review by independent Opus reviewers. | PASS/FAIL table |
| P4 | **Paper trial as a second instance** (own process, own `logs/` dir, own portfolio, own Telegram label), never replacing the daily one. | parity check PASS, kill rule frozen before day 0 |
| P5 | **Live readiness** (issue #99, Kraken minimums #81). Real orders only with the owner's explicit go-ahead. | owner decision |

## 3. Candidate families (limit multiple-testing: three families, at most 12 parameter sets each)

1. **Intraday trend/breakout** on 1h and 4h (Donchian-style channel + ATR stop, regime filter from BTC daily trend).
   Wider stops so time-in-trade is long enough for the edge per trade to exceed cost.
2. **Range mean-reversion** on 15m/1h, active only when the daily regime filter says "no trend"; short holding times,
   so it is the family most exposed to cost.
3. **Cross-sectional momentum, rebalanced every 4h** across the 16 coins (rank by trailing return, hold the top few):
   fewer, larger decisions than 1 and 2, turnover capped.

Anything not in this list (new indicators, parameter changes after seeing results) needs a new written reason and a
fresh out-of-sample window before it is allowed to count.

## 4. Gates (stricter than the daily ones, because intraday gives more trades and more ways to overfit)

1. >= 300 trades (daily bar was 100).
2. Bootstrap avg-R 95% CI lower bound > 0, day-block bootstrap.
3. Later-30% out-of-sample avg R > 0, and walk-forward by year: no more than one losing year.
4. 200-simulation time-shift placebo at >= 95th percentile (daily was 90th).
5. Net return > 0 and Sharpe above buy-and-hold at `kraken_T3_taker` costs, and still > 0 at costs +25%.
Extra reports, not gates: turnover, max drawdown, fraction of trades in the top 3 days, slippage tier sensitivity.

## 5. Ship rule (the honest default)

**If no family passes all gates, nothing intraday ships.** The result is reported as a FAIL with the numbers, and the
decision about what to do next (different data, a lower-cost execution model, or staying with the daily strategy)
comes back to the owner. A strategy is never promoted because it is faster, only because it passes.

## 6. What stays unchanged

`mode: paper`, the two-switch live gate, the advisor's veto-only/fail-closed behaviour, risk limits in code, no API
keys in logs or commits, the daily-bar trial and its frozen kill rule. A change to `src/risk_engine.py` or
`src/broker_live.py` always gets an Opus-level read-through.
