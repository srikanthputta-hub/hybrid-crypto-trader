# Intraday P2, family 3: FAIL

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
| F3-01 | B | 2297 | 803 | 25.0% | -0.03 | 0.02 | 0.06 | -0.06 | -0.07 | 0.71 | -43.9% | -45.3% | -1.14 | 5.4% | 46.65 | 0.33 / 0.81 | 1580 / -0.03 | 717 / -0.05 |
| F3-02 | B | 1797 | 759 | 25.5% | -0.03 | 0.03 | 0.06 | -0.06 | -0.07 | 0.82 | -27.7% | -38.8% | -0.58 | 6.4% | 36.41 | 0.67 / 1.24 | 1214 / -0.02 | 583 / -0.05 |
| F3-03 | B | 3262 | 847 | 23.2% | -0.04 | 0.02 | 0.06 | -0.05 | -0.06 | 0.64 | -62.6% | -62.9% | -1.74 | 8.1% | 62.40 | 0.33 / 0.88 | 2237 / -0.03 | 1025 / -0.05 |
| F3-04 | B | 2557 | 816 | 25.8% | -0.03 | 0.02 | 0.06 | -0.05 | -0.07 | 0.74 | -45.4% | -46.8% | -0.97 | 8.8% | 48.37 | 0.67 / 1.27 | 1750 / -0.02 | 807 / -0.05 |
| F3-05 | B | 1605 | 663 | 26.0% | -0.02 | 0.04 | 0.06 | -0.04 | -0.06 | 0.86 | -18.6% | -31.9% | -0.37 | 5.9% | 33.56 | 0.33 / 1.23 | 1107 / -0.01 | 498 / -0.04 |
| F3-06 | B | 1054 | 570 | 28.6% | 0.03 | 0.09 | 0.06 | -0.03 | -0.06 | 1.11 | 14.1% | -20.0% | 0.31 | 7.3% | 22.21 | 1.00 / 2.31 | 713 / 0.05 | 341 / -0.02 |
| F3-07 | B | 2235 | 767 | 23.8% | -0.01 | 0.04 | 0.06 | -0.04 | -0.06 | 0.81 | -31.6% | -33.5% | -0.60 | 8.9% | 45.06 | 0.50 / 1.38 | 1543 / -0.01 | 692 / -0.04 |
| F3-08 | B | 1595 | 687 | 24.8% | 0.00 | 0.06 | 0.06 | -0.04 | -0.06 | 0.94 | -9.2% | -26.7% | -0.10 | 9.8% | 31.63 | 1.00 / 2.19 | 1078 / 0.02 | 517 / -0.03 |
| F3-09 | B | 1255 | 568 | 25.2% | -0.00 | 0.05 | 0.06 | -0.05 | -0.07 | 0.99 | -1.5% | -23.1% | 0.02 | 6.7% | 26.46 | 0.50 / 1.66 | 844 / 0.02 | 411 / -0.05 |
| F3-10 **(champion)** | B | 725 | 326 | 26.8% | 0.06 | 0.12 | 0.06 | -0.03 | -0.08 | 1.31 | 37.7% | -21.7% | 0.59 | 8.3% | 15.17 | 1.33 / 3.57 | 489 / 0.11 | 236 / -0.03 |
| F3-11 | B | 1673 | 673 | 25.7% | 0.00 | 0.06 | 0.06 | -0.04 | -0.06 | 0.97 | -4.8% | -28.6% | -0.01 | 9.9% | 32.80 | 0.50 / 1.95 | 1194 / 0.02 | 479 / -0.04 |
| F3-12 | B | 1097 | 561 | 26.4% | 0.04 | 0.09 | 0.06 | -0.03 | -0.06 | 1.20 | 31.1% | -27.4% | 0.44 | 10.9% | 21.31 | 1.00 / 3.43 | 752 / 0.07 | 345 / -0.03 |
| F3-01 | A | 1683 | 586 | 25.3% | -0.03 | 0.03 | 0.06 | -0.06 | -0.07 | 0.76 | -30.9% | -42.9% | -0.99 | 6.3% | 55.48 | 0.33 / 0.83 | 960 / -0.02 | 723 / -0.05 |
| F3-02 | A | 1312 | 551 | 26.2% | -0.02 | 0.04 | 0.06 | -0.05 | -0.07 | 0.89 | -14.2% | -33.8% | -0.36 | 7.5% | 42.11 | 0.67 / 1.27 | 748 / -0.00 | 564 / -0.04 |
| F3-03 | A | 2463 | 631 | 24.0% | -0.03 | 0.02 | 0.06 | -0.05 | -0.06 | 0.68 | -49.2% | -50.7% | -1.65 | 9.4% | 81.05 | 0.33 / 0.88 | 1431 / -0.02 | 1032 / -0.05 |
| F3-04 | A | 1958 | 616 | 25.2% | -0.03 | 0.03 | 0.06 | -0.05 | -0.07 | 0.74 | -38.5% | -45.8% | -1.12 | 10.2% | 61.29 | 0.67 / 1.25 | 1130 / -0.01 | 828 / -0.05 |
| F3-05 | A | 1243 | 496 | 26.9% | -0.01 | 0.04 | 0.06 | -0.04 | -0.06 | 0.91 | -10.5% | -33.2% | -0.25 | 7.0% | 40.91 | 0.33 / 1.21 | 721 / 0.01 | 522 / -0.04 |
| F3-06 | A | 803 | 425 | 29.8% | 0.04 | 0.10 | 0.06 | -0.02 | -0.06 | 1.19 | 19.5% | -19.9% | 0.56 | 8.6% | 25.80 | 1.00 / 2.32 | 456 / 0.09 | 347 / -0.02 |
| F3-07 | A | 1721 | 575 | 24.3% | -0.01 | 0.05 | 0.06 | -0.04 | -0.06 | 0.85 | -21.2% | -32.8% | -0.53 | 10.3% | 55.47 | 0.33 / 1.36 | 1000 / 0.01 | 721 / -0.03 |
| F3-08 | A | 1245 | 520 | 25.5% | 0.02 | 0.08 | 0.06 | -0.04 | -0.06 | 1.00 | 0.5% | -27.2% | 0.08 | 11.5% | 38.53 | 0.83 / 2.14 | 710 / 0.05 | 535 / -0.02 |
| F3-09 | A | 903 | 403 | 24.6% | 0.03 | 0.09 | 0.06 | -0.03 | -0.06 | 1.20 | 21.2% | -21.3% | 0.55 | 8.1% | 30.09 | 0.50 / 1.75 | 497 / 0.09 | 406 / -0.05 |
| F3-10 | A | 529 | 230 | 27.0% | 0.12 | 0.17 | 0.06 | -0.01 | -0.07 | 1.58 | 56.8% | -21.3% | 1.08 | 9.9% | 16.78 | 1.33 / 3.71 | 291 / 0.23 | 238 / -0.02 |
| F3-11 | A | 1232 | 489 | 25.4% | 0.03 | 0.09 | 0.06 | -0.03 | -0.06 | 1.21 | 32.1% | -27.8% | 0.61 | 12.0% | 37.01 | 0.50 / 2.02 | 737 / 0.08 | 495 / -0.04 |
| F3-12 | A | 819 | 273 | 26.6% | 0.08 | 0.14 | 0.06 | -0.01 | -0.06 | 1.45 | 60.8% | -27.3% | 0.94 | 13.1% | 23.73 | 1.17 / 3.46 | 480 / 0.16 | 339 / -0.03 |

Selection: highest purged IS avg R among sets with >= 150 purged IS trades (ties: more IS trades, lower set number). Champion: **F3-10**. Universe A IS/OOS columns use B's cut (information only). Share of the 12 sets with positive B avg R: 41.7%

## 2. Gate table (champion, universe B, kraken_T3_taker)

| gate | numbers | pass |
|---|---|---|
| G1 | 725 trades, 326 entry blocks (need 300 / 100) | PASS |
| G2 | 0.0694th pct of block bootstrap avg R = -0.0781 (L_b = 2 d); info: 0.139th -0.0725, 2.5th -0.0323 | FAIL |
| G3 | OOS n 236 avg R -0.03; folds scored 5, losing 2 | FAIL |
| G4 | draw-champions >= real: avg R 0, return 0 (max 2); draws without a champion 0 (counted as NOT beaten, Amendment 2 (a)) | PASS |
| G5 | return 37.7%, Sharpe 0.59 vs bench 0.06; +25% return 26.9% | PASS |
| R1 | A avg R 0.12, return 56.8% | PASS |
| R2 | neighbours ['F3-06', 'F3-09', 'F3-12']: 2 of 3 positive (need 2) | PASS |

| benchmark | return | maxDD | Sharpe |
|---|---|---|---|
| EW buy&hold B | -58.7% | -82.6% | 0.06 |
| EW buy&hold A | 19.4% | -75.7% | 0.41 |
| BTC buy&hold B | 86.7% | -76.7% | 0.50 |

Deflated Sharpe (N = 36, information only): 0.017

## 3. Per-year table

| period | trades | avg R | return | maxDD | regime-ON share |
|---|---|---|---|---|---|
| F1 | 40 | -0.04 | -0.9% | -5.0% | 18.2% |
| F2 | 243 | 0.06 | 13.7% | -17.1% | 80.3% |
| F3 | 175 | 0.26 | 32.7% | -21.7% | 80.1% |
| F4 | 210 | -0.06 | -8.8% | -12.3% | 73.4% |
| F5 | 57 | 0.00 | 1.1% | -6.1% | 20.9% |
| 2021 | 40 | -0.04 | -0.9% | -5.0% | 90.2% |
| 2022 | 0 | n/a | 0.0% | 0.0% | 0.0% |
| 2023 | 243 | 0.06 | 13.7% | -17.1% | 80.3% |
| 2024 | 175 | 0.26 | 32.7% | -21.7% | 80.1% |
| 2025 | 210 | -0.06 | -8.8% | -12.3% | 73.4% |
| 2026 | 57 | 0.00 | 1.1% | -6.1% | 20.9% |

## 4. Cost, exit-model and fill-model sensitivity (champion)

| U | view | trades | avg R | CI 2.5-97.5 | return | Sharpe |
|---|---|---|---|---|---|---|
| B | paper_today_0.10% | 725 | 0.11 | [0.01, 0.22] | 77.4% | 1.01 |
| B | kraken_T3_maker | 725 | 0.08 | [-0.01, 0.20] | 55.7% | 0.79 |
| B | kraken_T3_taker | 725 | 0.06 | [-0.03, 0.18] | 37.7% | 0.59 |
| B | kraken_T1_taker | 725 | 0.01 | [-0.09, 0.12] | 0.0% | 0.06 |
| B | kraken_T3_taker_+25% | 725 | 0.05 | [-0.05, 0.16] | 26.9% | 0.45 |
| B | kraken_T3_taker_tiered | 725 | 0.05 | [-0.05, 0.16] | 26.0% | 0.44 |
| B | touch_exit | 734 | 0.05 | [-0.04, 0.15] | 28.3% | 0.49 |
| B | maker_fill | 723 | 0.07 | [-0.02, 0.19] | 46.0% | 0.69 |
| A | paper_today_0.10% | 529 | 0.16 | [0.03, 0.31] | 88.6% | 1.49 |
| A | kraken_T3_maker | 529 | 0.14 | [0.01, 0.29] | 71.5% | 1.28 |
| A | kraken_T3_taker | 529 | 0.12 | [-0.01, 0.27] | 56.8% | 1.08 |
| A | kraken_T1_taker | 529 | 0.06 | [-0.07, 0.21] | 24.2% | 0.55 |
| A | kraken_T3_taker_+25% | 529 | 0.10 | [-0.02, 0.25] | 47.7% | 0.94 |
| A | kraken_T3_taker_tiered | 529 | 0.10 | [-0.02, 0.25] | 47.9% | 0.94 |
| A | touch_exit | 535 | 0.10 | [-0.01, 0.23] | 47.7% | 0.99 |
| A | maker_fill | 528 | 0.13 | [-0.00, 0.28] | 62.9% | 1.16 |

Maker model (B, prereg §5.3): orders 749, fill rate 96.5%, missed 26 (25 matched to taker-run trades, their taker avg R -0.04).

| adverse selection | n | mean post-fill return 1h | 4h |
|---|---|---|---|
| filled maker orders | 723 | -0.0% | -0.1% |
| taker trades matching missed orders | 25 | 0.8% | 1.8% |

A maker result can never turn a FAIL into a PASS.

## 5. Cost geometry

| U | median stop % price | cost drag R | gross % / trade | gross % / day held |
|---|---|---|---|---|
| B | 16.7% | 0.06 | 1.6% | 0.4% |
| A | 16.7% | 0.06 | 2.3% | 0.6% |

## 6. Turnover

Traded notional / average equity, annualised: 15.17; trades per week 2.75; fees paid 34.4% of starting equity.

## 7. Drawdown

Max drawdown -21.7% from 2024-03-05 02:00:00+00:00 to 2024-11-04 22:00:00+00:00, recovered 2024-11-23 04:00:00+00:00 (263 days); worst day -5.0%; worst 7 days -6.1%.

## 8. Concentration

Share of net P/L from the top 3 entry days 122.1%, from the top 1% of trades 192.1%; without the top 3 entry days avg R -0.01, return -8.3%; coins with positive avg R 9.

| coin | trades | avg R |
|---|---|---|
| ADA | 44 | 0.21 |
| ALGO | 43 | 0.22 |
| ATOM | 42 | -0.17 |
| AVAX | 40 | 0.13 |
| BCH | 61 | 0.03 |
| BTC | 91 | 0.00 |
| DOGE | 42 | 0.37 |
| DOT | 45 | -0.13 |
| ETH | 76 | 0.11 |
| LINK | 55 | -0.00 |
| LTC | 58 | -0.09 |
| SHIB | 27 | 0.36 |
| SOL | 69 | 0.10 |
| XTZ | 32 | -0.07 |

## 9. Correlation

Design effect 1.24, n_eff 585.1; 1-day-block CI lower 2.5% -0.0322 vs 7-day -0.0383; positions opened per block max 8, median 2.0.

## 10. Risk-engine mix

Checks 2316, approvals 725, rejections by reason {'min_order': 1, 'no_room': 2, 'volatility': 1588}; dust (< $50) share 0.0%.

Integrity row: approved trades with stop >= entry or non-finite R = 0 (a non-zero count makes the run VOID).
Information only: trades whose realised loss exceeded the engine's volatility-scaled risk budget = 10, worst multiple of the budget 1.35 (gap through the stop, close-trigger overshoot, costs).
Maximum concurrent positions (B, champion): 5 (family 3 may hold up to K + b, Amendment 2 (b)).

Eligibility / benchmark join dates (Amendment 2 (c); 114 complete daily bars of warm-up): B: ADA 2021-10-01, ALGO 2021-10-01, ATOM 2021-10-01, AVAX 2022-01-23, BCH 2021-10-01, BTC 2021-10-01, DOGE 2021-10-01, DOT 2021-10-09, ETH 2021-10-01, LINK 2021-10-01, LTC 2021-10-01, SHIB 2022-01-02, SOL 2021-10-10, XTZ 2021-10-01; A: ADA 2023-07-14, ALGO 2023-07-14, ATOM 2023-07-14, AVAX 2023-07-14, BCH 2023-07-14, BTC 2023-07-14, DOGE 2023-07-14, DOT 2023-07-14, ETH 2023-07-14, LINK 2023-07-14, LTC 2023-07-14, SHIB 2023-07-14, SOL 2023-07-14, XRP 2023-11-05, XTZ 2023-07-14

## 11. Placebo

200 draws (shifts [1439, 1124, 938, 582, 638]..., default_rng(0)); draw-champion avg R p5/p50/p95/p99 ['-0.04', '-0.02', '0.01', '0.02']; return p5/p50/p95/p99 ['-0.37', '-0.15', '0.06', '0.15']; selected sets {1: 1, 2: 1, 4: 4, 6: 22, 8: 17, 9: 3, 10: 100, 12: 52}; draws without a champion 0. Real champion: 0 draws >= on avg R, 0 on return.

## 13. Logs

Per-trade and rejected-signal logs (gitignored), SHA-256: {'family3_trades.csv': '5b8e2f8137dcda72c8d76f37acb3eb607fb42a396a3b1cad8b05fecda064ffba', 'family3_rejected.csv': 'cbf3d9136490ad4363a41a5164d17c0fe2b2855e597b5a5671cc2cccf52d60fd'}

