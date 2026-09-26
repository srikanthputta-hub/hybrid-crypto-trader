"""Walk-forward backtest of the full pattern pipeline over historical OHLCV.

Replays the same scanner -> signal engine -> trade planner -> monitor logic
src/bot.py uses live, bar by bar. Deliberately simplified versus the live bot
in two ways: it does not call the Claude advisor (a live-decision aid, not
meaningful to replay against history) and it does not run the risk engine's
position sizing (it goes all-in/all-out per trade so `return_pct` is directly
comparable across runs) — it's for sanity-checking the pattern/planning logic
before you let it anywhere near even paper trading, not a substitute for one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from src.indicators import add_indicators
from src.signal_engine import evaluate
from src.scanner import scan
from src.trade_planner import build_plan


@dataclass
class BacktestResult:
    final_equity: float
    starting_equity: float
    num_trades: int
    trade_log: list[dict] = field(default_factory=list)

    @property
    def return_pct(self) -> float:
        return (self.final_equity - self.starting_equity) / self.starting_equity


def run_backtest(
    ohlcv: pd.DataFrame,
    starting_cash: float,
    indicators_cfg: dict[str, Any],
    scanner_cfg: dict[str, Any] | None = None,
    signal_engine_cfg: dict[str, Any] | None = None,
    trade_planner_cfg: dict[str, Any] | None = None,
    fee_pct: float = 0.001,
    warmup_bars: int = 40,
) -> BacktestResult:
    scanner_cfg = scanner_cfg or {}
    signal_engine_cfg = signal_engine_cfg or {}
    trade_planner_cfg = trade_planner_cfg or {}

    enriched = add_indicators(
        ohlcv, indicators_cfg["fast_ema"], indicators_cfg["slow_ema"], indicators_cfg["rsi_period"]
    )
    cash = starting_cash
    position_amount = 0.0
    status: str | None = None  # None | "PENDING" | "OPEN"
    plan: dict[str, Any] | None = None
    trade_log: list[dict] = []

    def _fill(price: float, ts) -> None:
        nonlocal cash, position_amount, status
        spend = cash
        fee = spend * fee_pct
        position_amount = (spend - fee) / price
        cash = 0.0
        status = "OPEN"
        trade_log.append({"timestamp": ts, "side": "buy", "price": price, "amount": position_amount})

    def _close(price: float, ts, reason: str) -> None:
        nonlocal cash, position_amount, status, plan
        gross = position_amount * price
        fee = gross * fee_pct
        cash = gross - fee
        trade_log.append({"timestamp": ts, "side": "sell", "price": price, "amount": position_amount, "reason": reason})
        position_amount = 0.0
        status = None
        plan = None

    for i in range(warmup_bars, len(enriched)):
        window = enriched.iloc[: i + 1]
        price = float(window["close"].iloc[-1])
        ts = window["timestamp"].iloc[-1]

        if status == "PENDING":
            entry_low, entry_high = plan["entry_zone"]
            if price <= plan["invalidation"]:
                status, plan = None, None
            elif entry_low <= price <= entry_high:
                _fill(price, ts)
            continue

        if status == "OPEN":
            if price <= plan["stop_loss"] or price >= plan["take_profit"]:
                reason = "stop_loss" if price <= plan["stop_loss"] else "take_profit"
                _close(price, ts, reason)
            continue

        # No active plan — look for a new setup.
        candidates = scan(window, scanner_cfg)
        signal = evaluate(candidates, **signal_engine_cfg)
        if signal is None or signal.direction == "bearish":
            continue

        atr = float(window["atr"].iloc[-1])
        new_plan = build_plan(signal, "BACKTEST", price, atr, **trade_planner_cfg)
        if new_plan is None:
            continue
        plan = dict(new_plan.__dict__)
        status = "PENDING"
        entry_low, entry_high = plan["entry_zone"]
        if entry_low <= price <= entry_high:
            _fill(price, ts)

    final_price = float(enriched["close"].iloc[-1])
    final_equity = cash + position_amount * final_price
    return BacktestResult(
        final_equity=final_equity,
        starting_equity=starting_cash,
        num_trades=len(trade_log),
        trade_log=trade_log,
    )
