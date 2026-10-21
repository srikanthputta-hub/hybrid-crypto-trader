# Intraday P2, family 1: FAIL

**Verdict: FAIL.** Survivorship: the coins are today's survivors, chosen with hindsight (prereg §1.2). Prints are Coinbase, not Kraken; Coinbase volume is 1-3x Kraken's and supports no capacity claim. No rule-0.2 rerun. Harness `intraday-prereg-v1 + p6-same-bar-exposure-v1`, code `df32b24b7d5defd625477dcbba860bcad5381ca4`, data provider `real`.

Harness source SHA-256 (review fix R3a; compare with the reviewed commit):

- `src/monitor.py` dcaafd80f4991cc3bf7e4f89791c677e99598f5a2ca09e47067a95b6f0e21b75
- `src/risk_engine.py` 63f5480b2d3be44be0b7e5a14273938ea71378369567a7e13de68e58f5d50de6
- `strategy_pack_run/intraday_data.py` a1d95aa01dc6f694ba5287325fa58cfbceb570f80efaa447f30c678c89cef7e4
- `strategy_pack_run/intraday_families.py` a44a41d515adc9348c5edc369f09bfa913535a2e2713e529ca4f7a5ae1a091c1
- `strategy_pack_run/intraday_fixtures.py` dd89288ff6acce8c8ed8833b39dfcf8dc5e3ff5abf8e17e7ea775521dd0ceb5b
- `strategy_pack_run/intraday_reference.py` c5328420fbde01abe5aa1fe2e5b64814cb3a5f3ad54b23d7e6b6bceedcf1646a
- `strategy_pack_run/intraday_runner.py` 04b51d26325db4bcd97bd8970177facb78c2c69d935b62e938a162c652118286
- `strategy_pack_run/intraday_stats.py` b1396a5d0de5224a2396b3a8dcadf01f2293cce0ae2200aa174642038352d3c9
- `strategy_pack_run/intraday_synthetic.py` f48b4355aeb1fd9b8ac124c47d9b536aa7e69c04924c76b02759b892a1c0e56a
- `strategy_pack_run/resample_ohlcv.py` ed3ffbad12841f8c4d14ea3398b093054a1d74e0b8d337befb3b50b937823fdd
- `strategy_pack_run/run_intraday_p2.py` d360699c0a0a0d6df3cdf27d4782e9f45c9b41b297b74116f6ab23a29f162fe6
- `strategy_pack_run/run_intraday_prereg.py` df5698c4dece1b13a279be79f4ec4c29cddf72e226967596d9afdaa1d4da1e82

Integrity: P/L reconciliation, 15m mask violations, approved trades with stop >= entry or non-finite R (count 0), deterministic reruns: all clean.

## 1. Grid table (kraken_T3_taker)

| set | U | trades | blocks | win% | avg R | avg R gross | drag R | CI 2.5% | CI 0.0694% | PF | return | maxDD | Sharpe | expo | turnover/y | hold med/mean d | IS n / avg R | OOS n / avg R |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| F1-01 | B | 827 | 265 | 30.2% | -0.15 | 0.02 | 0.17 | -0.29 | -0.37 | 0.67 | -46.7% | -50.3% | -0.91 | 10.5% | 26.98 | 2.21 / 2.71 | 612 / -0.19 | 215 / -0.04 |
| F1-02 | B | 608 | 173 | 32.2% | -0.06 | 0.05 | 0.11 | -0.22 | -0.31 | 0.85 | -21.0% | -33.9% | -0.26 | 13.2% | 16.49 | 4.25 / 5.70 | 438 / -0.09 | 170 / 0.00 |
| F1-03 | B | 515 | 118 | 33.0% | 0.01 | 0.09 | 0.08 | -0.18 | -0.27 | 0.97 | -3.9% | -30.8% | 0.02 | 14.7% | 11.32 | 7.62 / 9.41 | 367 / -0.00 | 148 / 0.03 |
| F1-04 | B | 594 | 254 | 29.5% | -0.11 | 0.07 | 0.18 | -0.27 | -0.37 | 0.74 | -31.3% | -39.6% | -0.65 | 6.5% | 20.10 | 1.83 / 2.26 | 448 / -0.18 | 146 / 0.12 |
| F1-05 | B | 428 | 159 | 32.9% | -0.04 | 0.07 | 0.12 | -0.22 | -0.32 | 0.92 | -9.4% | -26.0% | -0.11 | 9.0% | 12.35 | 3.62 / 5.00 | 317 / -0.11 | 111 / 0.14 |
| F1-06 | B | 361 | 114 | 32.1% | -0.03 | 0.05 | 0.08 | -0.24 | -0.34 | 0.96 | -4.1% | -28.1% | -0.01 | 10.6% | 8.91 | 6.04 / 8.18 | 262 / -0.04 | 99 / -0.02 |
| F1-07 | B | 682 | 248 | 31.4% | -0.14 | 0.03 | 0.17 | -0.29 | -0.38 | 0.66 | -43.6% | -52.2% | -0.85 | 10.5% | 22.23 | 2.58 / 3.28 | 519 / -0.20 | 163 / 0.07 |
| F1-08 | B | 536 | 166 | 34.3% | 0.00 | 0.11 | 0.11 | -0.19 | -0.30 | 0.91 | -12.3% | -26.6% | -0.11 | 13.1% | 14.43 | 4.67 / 6.42 | 388 / -0.01 | 148 / 0.04 |
| F1-09 **(champion)** | B | 455 | 107 | 31.2% | 0.00 | 0.08 | 0.08 | -0.20 | -0.31 | 0.93 | -8.3% | -32.4% | -0.04 | 14.5% | 10.35 | 7.88 / 10.00 | 331 / 0.02 | 124 / -0.04 |
| F1-10 | B | 499 | 202 | 30.9% | -0.11 | 0.06 | 0.18 | -0.30 | -0.40 | 0.72 | -31.7% | -37.3% | -0.71 | 6.6% | 16.93 | 2.12 / 2.71 | 380 / -0.18 | 119 / 0.09 |
| F1-11 | B | 374 | 160 | 29.7% | -0.06 | 0.06 | 0.12 | -0.25 | -0.36 | 0.87 | -13.6% | -26.0% | -0.20 | 9.1% | 10.85 | 4.00 / 5.61 | 282 / -0.09 | 92 / 0.04 |
| F1-12 | B | 322 | 110 | 30.4% | -0.02 | 0.07 | 0.08 | -0.24 | -0.35 | 0.94 | -6.0% | -27.9% | -0.04 | 10.6% | 8.00 | 6.88 / 9.08 | 245 / -0.01 | 77 / -0.02 |
| F1-01 | A | 644 | 197 | 32.1% | -0.08 | 0.09 | 0.17 | -0.24 | -0.34 | 0.82 | -25.4% | -31.1% | -0.55 | 12.1% | 34.43 | 2.06 / 2.62 | 424 / -0.11 | 220 / -0.02 |
| F1-02 | A | 474 | 127 | 34.6% | 0.02 | 0.13 | 0.11 | -0.18 | -0.29 | 0.96 | -5.3% | -29.4% | -0.03 | 15.2% | 20.70 | 4.12 / 5.42 | 301 / 0.01 | 173 / 0.03 |
| F1-03 | A | 416 | 89 | 31.2% | 0.02 | 0.10 | 0.08 | -0.21 | -0.33 | 1.02 | 2.2% | -28.2% | 0.12 | 17.0% | 14.35 | 7.27 / 8.61 | 266 / 0.02 | 150 / 0.02 |
| F1-04 | A | 471 | 191 | 31.8% | -0.01 | 0.16 | 0.18 | -0.20 | -0.30 | 0.92 | -9.4% | -26.4% | -0.19 | 7.5% | 25.58 | 1.62 / 2.14 | 320 / -0.07 | 151 / 0.11 |
| F1-05 | A | 342 | 126 | 35.4% | 0.04 | 0.16 | 0.12 | -0.17 | -0.29 | 1.08 | 8.9% | -25.1% | 0.27 | 10.4% | 15.63 | 3.38 / 4.63 | 226 / 0.01 | 116 / 0.10 |
| F1-06 | A | 294 | 90 | 31.6% | 0.01 | 0.10 | 0.09 | -0.25 | -0.40 | 0.96 | -3.5% | -27.6% | -0.01 | 12.2% | 11.50 | 5.44 / 7.37 | 195 / -0.00 | 99 / 0.03 |
| F1-07 | A | 526 | 185 | 33.1% | -0.05 | 0.12 | 0.17 | -0.24 | -0.35 | 0.79 | -24.8% | -34.7% | -0.56 | 12.3% | 28.38 | 2.42 / 3.23 | 355 / -0.11 | 171 / 0.07 |
| F1-08 | A | 419 | 125 | 34.4% | 0.05 | 0.16 | 0.11 | -0.17 | -0.29 | 1.04 | 4.4% | -26.5% | 0.16 | 15.1% | 17.73 | 4.38 / 6.09 | 267 / 0.07 | 152 / 0.03 |
| F1-09 | A | 376 | 91 | 30.3% | -0.02 | 0.07 | 0.08 | -0.25 | -0.37 | 0.84 | -16.2% | -31.5% | -0.26 | 16.9% | 13.20 | 7.38 / 9.15 | 251 / -0.00 | 125 / -0.04 |
| F1-10 | A | 380 | 154 | 34.2% | 0.03 | 0.21 | 0.18 | -0.20 | -0.33 | 0.92 | -8.5% | -24.5% | -0.18 | 7.7% | 21.02 | 2.02 / 2.61 | 257 / 0.01 | 123 / 0.07 |
| F1-11 | A | 290 | 121 | 31.7% | 0.03 | 0.14 | 0.12 | -0.21 | -0.34 | 0.98 | -1.6% | -26.8% | 0.02 | 10.4% | 13.62 | 3.96 / 5.19 | 195 / 0.02 | 95 / 0.04 |
| F1-12 | A | 258 | 90 | 29.8% | 0.05 | 0.14 | 0.09 | -0.25 | -0.40 | 1.01 | 1.3% | -26.7% | 0.10 | 12.3% | 10.22 | 5.92 / 8.10 | 178 / 0.07 | 80 / 0.01 |

Selection: highest purged IS avg R among sets with >= 150 purged IS trades (ties: more IS trades, lower set number). Champion: **F1-09**. Universe A IS/OOS columns use B's cut (information only). Share of the 12 sets with positive B avg R: 25.0%

## 2. Gate table (champion, universe B, kraken_T3_taker)

| gate | numbers | pass |
|---|---|---|
| G1 | 455 trades, 107 entry blocks (need 300 / 100) | PASS |
| G2 | 0.0694th pct of block bootstrap avg R = -0.3065 (L_b = 8 d); info: 0.139th -0.2928, 2.5th -0.2010 | FAIL |
| G3 | OOS n 124 avg R -0.04; folds scored 3, losing 1 | FAIL |
| G4 | draw-champions >= real: avg R 164, return 175 (max 2); draws without a champion 0 (counted as NOT beaten, Amendment 2 (a)) | FAIL |
| G5 | return -8.3%, Sharpe -0.04 vs bench 0.06; +25% return -12.8% | FAIL |
| R1 | A avg R -0.02, return -16.2% | FAIL |
| R2 | neighbours ['F1-03', 'F1-08', 'F1-12']: 2 of 3 positive (need 2) | PASS |

| benchmark | return | maxDD | Sharpe |
|---|---|---|---|
| EW buy&hold B | -58.7% | -82.6% | 0.06 |
| EW buy&hold A | 19.4% | -75.7% | 0.41 |
| BTC buy&hold B | 86.7% | -76.7% | 0.50 |

Deflated Sharpe (N = 36, information only): 0.037

## 3. Per-year table

| period | trades | avg R | return | maxDD | regime-ON share |
|---|---|---|---|---|---|
| F1 | 29 | -0.25 | -4.2% | -10.0% | 18.2% |
| F2 | 118 | 0.26 | 17.1% | -12.7% | 80.3% |
| F3 | 146 | 0.03 | -2.3% | -29.2% | 80.1% |
| F4 | 133 | -0.25 | -19.0% | -23.2% | 73.4% |
| F5 | 29 | 0.24 | 3.3% | -8.8% | 20.9% |
| 2021 | 29 | -0.25 | -4.2% | -10.0% | 90.2% |
| 2022 | 0 | n/a | 0.0% | 0.0% | 0.0% |
| 2023 | 118 | 0.26 | 17.1% | -12.7% | 80.3% |
| 2024 | 146 | 0.03 | -2.3% | -29.2% | 80.1% |
| 2025 | 133 | -0.25 | -19.0% | -23.2% | 73.4% |
| 2026 | 29 | 0.24 | 3.3% | -8.8% | 20.9% |

## 4. Cost, exit-model and fill-model sensitivity (champion)

| U | view | trades | avg R | CI 2.5-97.5 | return | Sharpe |
|---|---|---|---|---|---|---|
| B | paper_today_0.10% | 452 | 0.07 | [-0.14, 0.30] | 9.4% | 0.19 |
| B | kraken_T3_maker | 454 | 0.03 | [-0.18, 0.26] | 0.3% | 0.08 |
| B | kraken_T3_taker | 455 | 0.00 | [-0.20, 0.23] | -8.3% | -0.04 |
| B | kraken_T1_taker | 459 | -0.08 | [-0.28, 0.15] | -25.6% | -0.32 |
| B | kraken_T3_taker_+25% | 455 | -0.01 | [-0.22, 0.21] | -12.8% | -0.11 |
| B | kraken_T3_taker_tiered | 458 | -0.03 | [-0.23, 0.20] | -14.7% | -0.14 |
| B | touch_exit | 494 | -0.01 | [-0.19, 0.18] | -3.7% | 0.02 |
| B | maker_fill | 461 | 0.01 | [-0.20, 0.24] | -7.2% | -0.03 |
| A | paper_today_0.10% | 373 | 0.05 | [-0.18, 0.31] | -3.7% | 0.01 |
| A | kraken_T3_maker | 376 | 0.02 | [-0.22, 0.27] | -10.3% | -0.13 |
| A | kraken_T3_taker | 376 | -0.02 | [-0.25, 0.24] | -16.2% | -0.26 |
| A | kraken_T1_taker | 378 | -0.09 | [-0.33, 0.16] | -29.8% | -0.60 |
| A | kraken_T3_taker_+25% | 377 | -0.03 | [-0.27, 0.22] | -19.9% | -0.35 |
| A | kraken_T3_taker_tiered | 377 | -0.04 | [-0.27, 0.21] | -21.3% | -0.38 |
| A | touch_exit | 396 | 0.01 | [-0.20, 0.24] | -8.5% | -0.10 |
| A | maker_fill | 379 | -0.01 | [-0.24, 0.25] | -16.2% | -0.26 |

Maker model (B, prereg §5.3): orders 497, fill rate 92.8%, missed 36 (29 matched to taker-run trades, their taker avg R -0.02).

| adverse selection | n | mean post-fill return 1h | 4h |
|---|---|---|---|
| filled maker orders | 461 | -0.0% | -0.0% |
| taker trades matching missed orders | 29 | 1.1% | 2.2% |

A maker result can never turn a FAIL into a PASS.

## 5. Cost geometry

| U | median stop % price | cost drag R | gross % / trade | gross % / day held |
|---|---|---|---|---|
| B | 11.7% | 0.08 | 0.3% | 0.0% |
| A | 11.7% | 0.08 | 0.1% | 0.0% |

## 6. Turnover

Traded notional / average equity, annualised: 10.35; trades per week 1.73; fees paid 19.5% of starting equity.

## 7. Drawdown

Max drawdown -32.4% from 2024-03-05 15:00:00+00:00 to 2025-06-27 01:00:00+00:00, recovered None (958 days); worst day -4.4%; worst 7 days -5.5%.

## 8. Concentration

Share of net P/L from the top 3 entry days -362.1%, from the top 1% of trades -383.3%; without the top 3 entry days avg R -0.10, return -38.2%; coins with positive avg R 6.

| coin | trades | avg R |
|---|---|---|
| ADA | 40 | -0.11 |
| ALGO | 28 | 0.19 |
| ATOM | 39 | -0.36 |
| AVAX | 27 | -0.18 |
| BCH | 51 | 0.19 |
| BTC | 44 | 0.30 |
| DOGE | 24 | 0.23 |
| DOT | 32 | -0.05 |
| ETH | 37 | -0.16 |
| LINK | 30 | 0.06 |
| LTC | 35 | -0.13 |
| SHIB | 18 | -0.17 |
| SOL | 31 | 0.37 |
| XTZ | 19 | -0.37 |

## 9. Correlation

Design effect 2.72, n_eff 167.5; 1-day-block CI lower 2.5% -0.1710 vs 7-day -0.1965; positions opened per block max 15, median 4.0.

## 10. Risk-engine mix

Checks 2659, approvals 455, rejections by reason {'min_order': 30, 'no_room': 1484, 'rate_cap': 46, 'volatility': 644}; dust (< $50) share 6.4%.

Integrity row: approved trades with stop >= entry or non-finite R = 0 (a non-zero count makes the run VOID).
Information only: trades whose realised loss exceeded the engine's volatility-scaled risk budget = 35, worst multiple of the budget 1.57 (gap through the stop, close-trigger overshoot, costs).
Maximum concurrent positions (B, champion): 11.

Eligibility / benchmark join dates (Amendment 2 (c); 114 complete daily bars of warm-up): B: ADA 2021-10-01, ALGO 2021-10-01, ATOM 2021-10-01, AVAX 2022-01-23, BCH 2021-10-01, BTC 2021-10-01, DOGE 2021-10-01, DOT 2021-10-09, ETH 2021-10-01, LINK 2021-10-01, LTC 2021-10-01, SHIB 2022-01-02, SOL 2021-10-10, XTZ 2021-10-01; A: ADA 2023-07-14, ALGO 2023-07-14, ATOM 2023-07-14, AVAX 2023-07-14, BCH 2023-07-14, BTC 2023-07-14, DOGE 2023-07-14, DOT 2023-07-14, ETH 2023-07-14, LINK 2023-07-14, LTC 2023-07-14, SHIB 2023-07-14, SOL 2023-07-14, XRP 2023-11-05, XTZ 2023-07-14

## 11. Placebo

200 draws (shifts [1439, 1124, 938, 582, 638]..., default_rng(0)); draw-champion avg R p5/p50/p95/p99 ['-0.03', '0.05', '0.18', '0.21']; return p5/p50/p95/p99 ['-0.13', '0.06', '0.34', '0.38']; selected sets {3: 21, 5: 4, 6: 22, 8: 5, 9: 59, 11: 13, 12: 76}; draws without a champion 0. Real champion: 164 draws >= on avg R, 175 on return.

## 13. Logs

Per-trade and rejected-signal logs (gitignored), SHA-256: {'family1_trades.csv': '03952740862ffe6cc95e0cb173262aad874791f5dc98bf7620eca0313ca44819', 'family1_rejected.csv': '41b633c16b9648edbaa713b4c0b96ebe9c1210743d84c2c54b660781f5e38538'}

