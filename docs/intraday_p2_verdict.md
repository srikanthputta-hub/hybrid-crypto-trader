# Intraday P2: verdict (commentary on the registered one-shot run)

**Run:** `run_intraday_prereg.py --workers 4`, OFFICIAL, clean `main` at `df32b24b`, all data hashes verified, no integrity
failures, exit 0. Raw outputs (committed unmodified first, PR #115): `strategy_pack_run/intraday_p2_results.json`,
`docs/intraday_p2_family{1,2,3}.md`. This note is commentary only; nothing here changes a verdict or a parameter.

## Result: all three registered families FAIL. Under the ship rule (plan section 5) nothing intraday ships.

Universe B (14 coins, 2021-10-01 to 2026-10-20), `kraken_T3_taker` costs (0.86% round trip), champion per family.
Equal-weight buy-and-hold over the same window: **-58.7%**, max drawdown -82.6%.

| | Family 1 trend/breakout (1h/4h) | Family 2 range mean-reversion (15m/1h) | Family 3 cross-sectional momentum (4h) |
|---|---|---|---|
| Champion | F1-09 | F2-07 | F3-10 |
| Trades | 455 | 515 | 725 |
| Avg R, net (in-sample / OOS) | +0.004 (+0.023 / -0.044, 124 OOS) | -0.457 (-0.442 / -0.490) | +0.064 (+0.108 / -0.028, 236 OOS) |
| Gross avg R before costs | +0.085 | -0.022 | +0.121 |
| Net return / max drawdown | -8.3% / -32.4% | -31.3% / -32.7% | +37.7% / -21.7% |
| G1 trades and blocks | pass | pass | pass |
| G2 corrected CI lower bound > 0 | **fail** (-0.31) | **fail** (-0.78) | **fail** (-0.078) |
| G3 OOS avg R > 0, at most 1 losing year | **fail** | **fail** | **fail** (OOS -0.028, 2 of 5 years losing) |
| G4 placebo (beat >= 198 of 200) | fail (p 0.82) | fail | **pass** (beat all 200, p 0.005) |
| G5 net > 0, Sharpe > buy-and-hold, +25% costs | fail | fail | pass |
| Verdict | FAIL | FAIL | FAIL |

## Reading it honestly
- **Family 2** loses to costs as the prereg predicted: gross edge is zero (-0.02R) and costs take 0.43R per trade.
- **Family 1** has a tiny gross edge (+0.085R) that costs consume; out of sample it is negative.
- **Family 3 is the near-miss, not a pass.** It beats every placebo draw and is positive net over the whole window,
  so there is probably some real cross-sectional momentum signal. But it fails the two gates that decide whether the
  edge persists and is large enough to trust: the corrected confidence bound is below zero, and the later-30%
  out-of-sample window is slightly negative (-0.028R on 236 trades) with two losing years. G5 is also a low bar in this
  window (buy-and-hold lost 59%). Do not read the +37.7% as an expected return.
- Maker-fill sensitivity (information only, never gating) improves all families by roughly 0.02-0.03R and changes no
  verdict.

## What is allowed next
The result has been seen once. Re-running with different parameters, windows or gates on this data is not allowed
(prereg rule 0.1). Options that remain legitimate are in the report to the owner: stay with the daily trial; collect
fresh forward data and pre-register a new round; or run family 3's champion as a clearly-labelled exploratory paper
experiment that cannot change this verdict.
