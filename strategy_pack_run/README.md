# strategy_pack

Two long-only, evidence-backed strategies for the 16-coin bot, plus a fee-aware backtester and a gate-based report.

| File | What it is |
|---|---|
| `strategies.py` | `trend_ensemble` (Donchian ensemble + BTC regime filter + ATR chandelier stop) and `tsmom` (time-series momentum). Pure functions: closed-candle DataFrame in, `enter` / `hold` / `rank` / `atr` columns out |
| `engine.py` | Multi-asset spot backtester. Next-open fills, fees and slippage on every fill, gap-aware stops, ATR risk sizing, exposure caps, rank-ordered capital allocation, time-shift placebo test, bootstrap confidence intervals |
| `run_report.py` | Runs both strategies under four cost presets (including your current 0.10% and Kraken's real tiers), prints benchmarks, a 70/30 split, the placebo test, a parameter-robustness grid and a PASS/FAIL gates table |
| `fetch_kraken.py` | Downloads closed candles for the 16 coins via CCXT (Kraken serves only the latest 720 candles per call) |
| `test_pack.py` | 6 tests: no look-ahead (truncation), next-open entry, gap and intrabar stop fills, P/L reconciliation, caps, and "no edge loses costs / trend market can win" |

## Run it

```
pip install ccxt TA-Lib pandas numpy
python test_pack.py                                   # mechanics must pass first
python fetch_kraken.py --timeframe 1d --quote USD --out data
python run_report.py --data-dir data --timeframe 1d   # writes report.md
```

Kraken's public API gives about 2 years of daily candles, which is thin: expect fewer than 100 trades, so the
">=100 trades" gate will fail by design. For a verdict, load multi-year history (CSV format: `timestamp` = bar open
in epoch ms, `open, high, low, close, volume`) named `<BASE>_<QUOTE>_<timeframe>.csv`.

## Rules the code enforces

* Signals use closed bars only and execute at the next bar's open.
* Costs are charged on every fill. Stops fill at the open on a gap, otherwise at the stop price.
* Long-only, no leverage. A new entry never exceeds the per-position or total exposure caps.

All parameters are a-priori playbook values, not tuned ones. Do not tune them on the data you judge them on.
