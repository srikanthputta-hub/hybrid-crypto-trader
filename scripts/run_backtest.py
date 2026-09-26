#!/usr/bin/env python3
"""CLI: fetch historical OHLCV from an exchange and backtest the configured strategy.

Usage:
    python scripts/run_backtest.py --symbol BTC/USDT --days 90
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtest import run_backtest
from src.config import load_config
from src.exchange import ExchangeClient


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default=None, help="defaults to the first symbol in config.yaml")
    parser.add_argument("--bars", type=int, default=1000, help="number of historical bars to fetch")
    args = parser.parse_args()

    cfg = load_config()
    symbol = args.symbol or cfg.symbols[0]

    exchange = ExchangeClient(cfg.exchange_id, testnet=False)  # historical data doesn't need sandbox
    ohlcv = exchange.fetch_ohlcv_df(symbol, cfg.timeframe, args.bars)

    result = run_backtest(
        ohlcv,
        starting_cash=cfg.risk["starting_cash_usd"],
        indicators_cfg=cfg.indicators,
        scanner_cfg=cfg.scanner,
        signal_engine_cfg=cfg.signal_engine,
        trade_planner_cfg=cfg.trade_planner,
    )

    print(f"Symbol:          {symbol}")
    print(f"Bars:             {len(ohlcv)}")
    print(f"Starting equity:  {result.starting_equity:.2f}")
    print(f"Final equity:     {result.final_equity:.2f}")
    print(f"Return:           {result.return_pct:.2%}")
    print(f"Number of trades: {result.num_trades}")
    if result.trade_log:
        print("\nLast few trades:")
        for t in result.trade_log[-6:]:
            print(f"  {t['timestamp']}  {t['side']:<4} @ {t['price']:.2f}" + (f"  ({t.get('reason')})" if t.get("reason") else ""))


if __name__ == "__main__":
    main()
