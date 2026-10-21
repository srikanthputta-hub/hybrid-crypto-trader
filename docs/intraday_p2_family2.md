# Intraday P2, family 2: FAIL

**Verdict: FAIL.** Survivorship: the coins are today's survivors, chosen with hindsight (prereg §1.2). Prints are Coinbase, not Kraken; Coinbase volume is 1-3x Kraken's and supports no capacity claim. 15m sets: every coin except BTC/ETH is masked on 2023-03-04, 2024-08-13..09-03 and 2025-01-01..04-30; masked windows are excluded from the 15m benchmark. No rule-0.2 rerun. Harness `intraday-prereg-v1 + p6-same-bar-exposure-v1`, code `df32b24b7d5defd625477dcbba860bcad5381ca4`, data provider `real`.

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
| F2-01 | B | 1799 | 455 | 30.1% | -1.07 | -0.06 | 1.01 | -1.20 | -1.29 | 0.23 | -79.1% | -79.3% | -4.68 | 1.3% | 63.50 | 0.19 / 0.14 | 1286 / -1.07 | 513 / -1.08 |
| F2-02 | B | 1342 | 406 | 30.0% | -1.08 | -0.08 | 1.00 | -1.24 | -1.35 | 0.24 | -70.0% | -70.3% | -4.00 | 1.0% | 49.12 | 0.19 / 0.14 | 998 / -1.06 | 344 / -1.13 |
| F2-03 | B | 2714 | 526 | 31.8% | -1.12 | -0.11 | 1.02 | -1.24 | -1.31 | 0.30 | -91.3% | -91.4% | -4.88 | 3.5% | 88.82 | 0.21 / 0.25 | 1939 / -1.15 | 775 / -1.05 |
| F2-04 | B | 2043 | 492 | 32.2% | -1.13 | -0.13 | 1.00 | -1.25 | -1.32 | 0.29 | -84.8% | -85.0% | -4.51 | 2.6% | 70.82 | 0.21 / 0.25 | 1488 / -1.14 | 555 / -1.08 |
| F2-05 | B | 2345 | 457 | 34.1% | -1.06 | -0.10 | 0.96 | -1.20 | -1.30 | 0.39 | -87.5% | -87.7% | -3.93 | 4.4% | 81.34 | 0.25 / 0.37 | 1726 / -1.07 | 619 / -1.04 |
| F2-06 | B | 1712 | 404 | 34.4% | -1.04 | -0.10 | 0.94 | -1.19 | -1.29 | 0.41 | -77.4% | -77.7% | -3.40 | 3.3% | 61.52 | 0.26 / 0.39 | 1262 / -1.02 | 450 / -1.10 |
| F2-07 **(champion)** | B | 515 | 184 | 40.6% | -0.46 | -0.02 | 0.43 | -0.66 | -0.78 | 0.54 | -31.3% | -32.7% | -1.45 | 1.6% | 18.69 | 0.83 / 0.60 | 356 / -0.44 | 159 / -0.49 |
| F2-08 | B | 382 | 152 | 37.2% | -0.59 | -0.15 | 0.44 | -0.81 | -0.95 | 0.42 | -32.9% | -34.0% | -1.69 | 1.2% | 14.49 | 0.83 / 0.60 | 274 / -0.52 | 108 / -0.75 |
| F2-09 | B | 840 | 237 | 39.5% | -0.52 | -0.08 | 0.45 | -0.72 | -0.84 | 0.51 | -54.3% | -56.2% | -1.87 | 3.9% | 28.26 | 0.83 / 1.01 | 595 / -0.55 | 245 / -0.46 |
| F2-10 | B | 602 | 195 | 39.2% | -0.52 | -0.08 | 0.45 | -0.74 | -0.87 | 0.52 | -43.9% | -46.3% | -1.60 | 3.0% | 21.62 | 0.88 / 1.01 | 440 / -0.54 | 162 / -0.49 |
| F2-11 | B | 707 | 211 | 35.1% | -0.59 | -0.15 | 0.43 | -0.82 | -0.95 | 0.51 | -52.4% | -54.8% | -1.69 | 5.0% | 23.55 | 0.96 / 1.52 | 519 / -0.68 | 188 / -0.34 |
| F2-12 | B | 492 | 157 | 35.0% | -0.56 | -0.13 | 0.43 | -0.83 | -0.98 | 0.56 | -39.6% | -41.2% | -1.36 | 3.7% | 17.38 | 0.96 / 1.56 | 367 / -0.61 | 125 / -0.44 |
| F2-01 | A | 1451 | 342 | 30.2% | -1.05 | -0.03 | 1.02 | -1.20 | -1.31 | 0.24 | -68.9% | -69.0% | -5.25 | 1.6% | 95.57 | 0.18 / 0.14 | 901 / -1.04 | 550 / -1.06 |
| F2-02 | A | 1069 | 302 | 30.6% | -1.07 | -0.06 | 1.01 | -1.26 | -1.40 | 0.26 | -59.5% | -59.6% | -4.35 | 1.2% | 70.73 | 0.18 / 0.14 | 701 / -1.03 | 368 / -1.14 |
| F2-03 | A | 2180 | 400 | 31.4% | -1.15 | -0.10 | 1.05 | -1.28 | -1.37 | 0.27 | -85.3% | -85.3% | -5.38 | 4.2% | 152.80 | 0.21 / 0.24 | 1362 / -1.19 | 818 / -1.07 |
| F2-04 | A | 1625 | 374 | 31.3% | -1.17 | -0.14 | 1.03 | -1.31 | -1.40 | 0.28 | -76.8% | -76.8% | -4.89 | 3.1% | 110.04 | 0.21 / 0.24 | 1026 / -1.21 | 599 / -1.09 |
| F2-05 | A | 1894 | 346 | 33.5% | -1.09 | -0.10 | 1.00 | -1.26 | -1.37 | 0.38 | -79.9% | -80.0% | -4.30 | 5.2% | 128.35 | 0.25 / 0.36 | 1213 / -1.12 | 681 / -1.04 |
| F2-06 | A | 1378 | 315 | 33.5% | -1.06 | -0.10 | 0.97 | -1.24 | -1.36 | 0.41 | -68.9% | -69.3% | -3.64 | 3.9% | 89.65 | 0.26 / 0.38 | 881 / -1.05 | 497 / -1.08 |
| F2-07 | A | 394 | 140 | 39.8% | -0.42 | 0.01 | 0.43 | -0.64 | -0.77 | 0.56 | -24.7% | -25.9% | -1.48 | 1.8% | 22.66 | 0.75 / 0.58 | 226 / -0.37 | 168 / -0.48 |
| F2-08 | A | 283 | 115 | 35.3% | -0.60 | -0.17 | 0.43 | -0.85 | -1.00 | 0.39 | -27.5% | -28.3% | -1.83 | 1.3% | 16.78 | 0.75 / 0.57 | 170 / -0.50 | 113 / -0.75 |
| F2-09 | A | 692 | 190 | 37.9% | -0.54 | -0.10 | 0.45 | -0.76 | -0.90 | 0.49 | -48.4% | -48.7% | -2.15 | 4.6% | 38.06 | 0.79 / 0.96 | 429 / -0.60 | 263 / -0.46 |
| F2-10 | A | 490 | 154 | 38.0% | -0.52 | -0.07 | 0.45 | -0.77 | -0.91 | 0.51 | -37.6% | -38.7% | -1.72 | 3.4% | 28.07 | 0.79 / 0.96 | 312 / -0.55 | 178 / -0.46 |
| F2-11 | A | 577 | 164 | 34.8% | -0.59 | -0.16 | 0.43 | -0.85 | -1.02 | 0.51 | -45.0% | -46.4% | -1.83 | 5.8% | 31.77 | 0.92 / 1.46 | 376 / -0.73 | 201 / -0.33 |
| F2-12 | A | 408 | 118 | 33.1% | -0.66 | -0.24 | 0.43 | -0.98 | -1.16 | 0.52 | -36.7% | -37.8% | -1.61 | 4.4% | 22.62 | 0.88 / 1.47 | 274 / -0.75 | 134 / -0.48 |

Selection: highest purged IS avg R among sets with >= 150 purged IS trades (ties: more IS trades, lower set number). Champion: **F2-07**. Universe A IS/OOS columns use B's cut (information only). Share of the 12 sets with positive B avg R: 0.0%

## 2. Gate table (champion, universe B, kraken_T3_taker)

| gate | numbers | pass |
|---|---|---|
| G1 | 515 trades, 184 entry blocks (need 300 / 100) | PASS |
| G2 | 0.0694th pct of block bootstrap avg R = -0.7754 (L_b = 1 d); info: 0.139th -0.7575, 2.5th -0.6554 | FAIL |
| G3 | OOS n 159 avg R -0.49; folds scored 3, losing 3 | FAIL |
| G4 | draw-champions >= real: avg R 3, return 188 (max 2); draws without a champion 0 (counted as NOT beaten, Amendment 2 (a)) | FAIL |
| G5 | return -31.3%, Sharpe -1.45 vs bench 0.06; +25% return -37.9% | FAIL |
| R1 | A avg R -0.42, return -24.7% | FAIL |
| R2 | neighbours ['F2-01', 'F2-08', 'F2-09']: 0 of 3 positive (need 2) | FAIL |

| benchmark | return | maxDD | Sharpe |
|---|---|---|---|
| EW buy&hold B | -58.7% | -82.6% | 0.06 |
| EW buy&hold A | 19.4% | -75.7% | 0.41 |
| BTC buy&hold B | 86.7% | -76.7% | 0.50 |

Deflated Sharpe (N = 36, information only): 0.000

## 3. Per-year table

| period | trades | avg R | return | maxDD | regime-ON share |
|---|---|---|---|---|---|
| F1 | 26 | -0.22 | -0.8% | -3.0% | 18.2% |
| F2 | 170 | -0.60 | -14.2% | -14.8% | 80.3% |
| F3 | 131 | -0.30 | -5.7% | -7.7% | 80.1% |
| F4 | 187 | -0.48 | -14.8% | -15.6% | 73.4% |
| F5 | 1 | 1.31 | 0.4% | -0.1% | 20.9% |
| 2021 | 26 | -0.22 | -0.8% | -3.0% | 90.2% |
| 2022 | 0 | n/a | 0.0% | 0.0% | 0.0% |
| 2023 | 170 | -0.60 | -14.2% | -14.8% | 80.3% |
| 2024 | 131 | -0.30 | -5.7% | -7.7% | 80.1% |
| 2025 | 187 | -0.48 | -14.8% | -15.6% | 73.4% |
| 2026 | 1 | 1.31 | 0.4% | -0.1% | 20.9% |

## 4. Cost, exit-model and fill-model sensitivity (champion)

| U | view | trades | avg R | CI 2.5-97.5 | return | Sharpe |
|---|---|---|---|---|---|---|
| B | paper_today_0.10% | 516 | -0.12 | [-0.32, 0.09] | -6.1% | -0.25 |
| B | kraken_T3_maker | 516 | -0.29 | [-0.49, -0.09] | -20.1% | -0.90 |
| B | kraken_T3_taker | 515 | -0.46 | [-0.66, -0.25] | -31.3% | -1.45 |
| B | kraken_T1_taker | 517 | -0.88 | [-1.08, -0.67] | -53.2% | -2.64 |
| B | kraken_T3_taker_+25% | 517 | -0.56 | [-0.76, -0.35] | -37.9% | -1.78 |
| B | kraken_T3_taker_tiered | 516 | -0.61 | [-0.80, -0.40] | -40.4% | -1.98 |
| B | touch_exit | 592 | -0.45 | [-0.59, -0.31] | -38.9% | -1.99 |
| B | maker_fill | 492 | -0.39 | [-0.58, -0.18] | -25.8% | -1.23 |
| A | paper_today_0.10% | 393 | -0.09 | [-0.32, 0.15] | -4.7% | -0.25 |
| A | kraken_T3_maker | 393 | -0.26 | [-0.49, -0.03] | -15.6% | -0.92 |
| A | kraken_T3_taker | 394 | -0.42 | [-0.64, -0.18] | -24.7% | -1.48 |
| A | kraken_T1_taker | 394 | -0.84 | [-1.06, -0.60] | -43.5% | -2.73 |
| A | kraken_T3_taker_+25% | 394 | -0.53 | [-0.75, -0.29] | -30.2% | -1.83 |
| A | kraken_T3_taker_tiered | 394 | -0.57 | [-0.79, -0.34] | -32.7% | -2.01 |
| A | touch_exit | 440 | -0.45 | [-0.60, -0.28] | -31.3% | -2.12 |
| A | maker_fill | 371 | -0.38 | [-0.59, -0.15] | -21.0% | -1.32 |

Maker model (B, prereg §5.3): orders 519, fill rate 94.8%, missed 27 (27 matched to taker-run trades, their taker avg R 0.27).

| adverse selection | n | mean post-fill return 1h | 4h |
|---|---|---|---|
| filled maker orders | 492 | -0.0% | -0.1% |
| taker trades matching missed orders | 27 | 0.7% | 1.1% |

A maker result can never turn a FAIL into a PASS.

## 5. Cost geometry

| U | median stop % price | cost drag R | gross % / trade | gross % / day held |
|---|---|---|---|---|
| B | 2.2% | 0.43 | 0.1% | 0.1% |
| A | 2.2% | 0.43 | 0.0% | 0.1% |

## 6. Turnover

Traded notional / average equity, annualised: 18.69; trades per week 1.95; fees paid 30.4% of starting equity.

## 7. Drawdown

Max drawdown -32.7% from 2021-10-18 01:00:00+00:00 to 2025-11-03 17:00:00+00:00, recovered None (1828 days); worst day -6.1%; worst 7 days -7.1%.

## 8. Concentration

Share of net P/L from the top 3 entry days -9.8%, from the top 1% of trades -11.1%; without the top 3 entry days avg R -0.50, return -34.3%; coins with positive avg R 0.

| coin | trades | avg R |
|---|---|---|
| ADA | 37 | -0.37 |
| ALGO | 44 | -0.35 |
| ATOM | 37 | -0.53 |
| AVAX | 35 | -0.40 |
| BCH | 51 | -0.59 |
| BTC | 41 | -0.78 |
| DOGE | 24 | -0.54 |
| DOT | 40 | -0.19 |
| ETH | 46 | -0.61 |
| LINK | 35 | -0.46 |
| LTC | 47 | -0.55 |
| SHIB | 24 | -0.10 |
| SOL | 25 | -0.52 |
| XTZ | 29 | -0.12 |

## 9. Correlation

Design effect 2.57, n_eff 200.7; 1-day-block CI lower 2.5% -0.6554 vs 7-day -0.6574; positions opened per block max 12, median 2.0.

## 10. Risk-engine mix

Checks 5648, approvals 515, rejections by reason {'daily_loss': 6, 'min_order': 25, 'no_room': 93, 'rate_cap': 42, 'risk_reward': 4089, 'volatility': 878}; dust (< $50) share 5.8%.

Integrity row: approved trades with stop >= entry or non-finite R = 0 (a non-zero count makes the run VOID).
Information only: trades whose realised loss exceeded the engine's volatility-scaled risk budget = 11, worst multiple of the budget 1.80 (gap through the stop, close-trigger overshoot, costs).
Maximum concurrent positions (B, champion): 6.

Eligibility / benchmark join dates (Amendment 2 (c); 114 complete daily bars of warm-up): B: ADA 2021-10-01, ALGO 2021-10-01, ATOM 2021-10-01, AVAX 2022-01-23, BCH 2021-10-01, BTC 2021-10-01, DOGE 2021-10-01, DOT 2021-10-09, ETH 2021-10-01, LINK 2021-10-01, LTC 2021-10-01, SHIB 2022-01-02, SOL 2021-10-10, XTZ 2021-10-01; A: ADA 2023-07-14, ALGO 2023-07-14, ATOM 2023-07-14, AVAX 2023-07-14, BCH 2023-07-14, BTC 2023-07-14, DOGE 2023-07-14, DOT 2023-07-14, ETH 2023-07-14, LINK 2023-07-14, LTC 2023-07-14, SHIB 2023-07-14, SOL 2023-07-14, XRP 2023-11-05, XTZ 2023-07-14

## 11. Placebo

200 draws (shifts [1439, 1124, 938, 582, 638]..., default_rng(0)); draw-champion avg R p5/p50/p95/p99 ['-1.11', '-0.87', '-0.49', '-0.45']; return p5/p50/p95/p99 ['-0.33', '-0.21', '-0.15', '-0.13']; selected sets {1: 106, 3: 4, 4: 7, 5: 2, 6: 17, 9: 10, 11: 54}; draws without a champion 0. Real champion: 3 draws >= on avg R, 188 on return.

## 12. 15m-specific

The IS/OOS cut (2025-04-14) falls inside the 2025-01-01..04-30 mask, so the in-sample window of the 15m sets is shortened for every masked coin (no entries from about 2024-12-31 to the cut, plus the earlier masks).

- F2-01: mask-blocked flat signals 2061; usable IS days per coin {'ADA': 1163, 'ALGO': 1163, 'ATOM': 1163, 'AVAX': 1163, 'BCH': 1163, 'BTC': 1291, 'DOGE': 1163, 'DOT': 1163, 'ETH': 1291, 'LINK': 1163, 'LTC': 1163, 'SHIB': 1163, 'SOL': 1163} of 1291; core-only (BTC, ETH, SOL, LTC) trades 688, avg R -1.18, return -45.4%
- F2-02: mask-blocked flat signals 641; usable IS days per coin {'ADA': 1163, 'ALGO': 1163, 'ATOM': 1163, 'AVAX': 1163, 'BCH': 1163, 'BTC': 1291, 'DOGE': 1163, 'DOT': 1163, 'ETH': 1291, 'LINK': 1163, 'LTC': 1163, 'SHIB': 1163, 'SOL': 1163} of 1291; core-only (BTC, ETH, SOL, LTC) trades 522, avg R -1.23, return -37.1%
- F2-03: mask-blocked flat signals 2555; usable IS days per coin {'ADA': 1163, 'ALGO': 1163, 'ATOM': 1163, 'AVAX': 1163, 'BCH': 1163, 'BTC': 1291, 'DOGE': 1163, 'DOT': 1163, 'ETH': 1291, 'LINK': 1163, 'LTC': 1163, 'SHIB': 1163, 'SOL': 1163} of 1291; core-only (BTC, ETH, SOL, LTC) trades 1059, avg R -1.16, return -58.1%
- F2-04: mask-blocked flat signals 1037; usable IS days per coin {'ADA': 1163, 'ALGO': 1163, 'ATOM': 1163, 'AVAX': 1163, 'BCH': 1163, 'BTC': 1291, 'DOGE': 1163, 'DOT': 1163, 'ETH': 1291, 'LINK': 1163, 'LTC': 1163, 'SHIB': 1163, 'SOL': 1163} of 1291; core-only (BTC, ETH, SOL, LTC) trades 816, avg R -1.06, return -46.5%
- F2-05: mask-blocked flat signals 2980; usable IS days per coin {'ADA': 1158, 'ALGO': 1158, 'ATOM': 1158, 'AVAX': 1158, 'BCH': 1158, 'BTC': 1291, 'DOGE': 1158, 'DOT': 1158, 'ETH': 1291, 'LINK': 1158, 'LTC': 1158, 'SHIB': 1158, 'SOL': 1158} of 1291; core-only (BTC, ETH, SOL, LTC) trades 926, avg R -1.14, return -56.0%
- F2-06: mask-blocked flat signals 1290; usable IS days per coin {'ADA': 1158, 'ALGO': 1158, 'ATOM': 1158, 'AVAX': 1158, 'BCH': 1158, 'BTC': 1291, 'DOGE': 1158, 'DOT': 1158, 'ETH': 1291, 'LINK': 1158, 'LTC': 1158, 'SHIB': 1158, 'SOL': 1158} of 1291; core-only (BTC, ETH, SOL, LTC) trades 695, avg R -1.09, return -46.1%

## 13. Logs

Per-trade and rejected-signal logs (gitignored), SHA-256: {'family2_trades.csv': 'dc02eccba654785e6da30b3b184d4db8b5794ebc309bbc3adcb1ebffe47eeb39', 'family2_rejected.csv': 'd09bcc25a23be5f4f47ee0037ace2de88f4ccfc6439b31a1b7e56a7767a2f222'}

