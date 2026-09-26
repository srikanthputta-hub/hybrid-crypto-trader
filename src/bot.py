"""Main bot loop, wiring together the full pipeline:

  market data (ccxt) -> indicators -> scanner -> signal engine -> trade planner
  -> risk engine -> Claude advisor (optional) -> trade plan store (PENDING)
  -> 24/7 monitor (fills / discards / exits) -> paper or live broker

Every cycle, for every symbol, exactly one line is written to the audit log
regardless of what happened — so "why didn't it trade at 14:32" is always
answerable from logs/audit_log.jsonl alone.

Run one cycle: `python -m src.bot --once`
Run continuously at the configured poll interval: `python -m src.bot`
"""
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from src import claude_advisor, monitor, scanner, signal_engine, trade_planner
from src.audit import AuditLog
from src.broker_paper import PaperBroker
from src.config import Config, load_config
from src.exchange import ExchangeClient
from src.indicators import add_indicators
from src.portfolio import Portfolio, now_iso
from src.risk_engine import RiskEngine
from src.trade_plan_store import TradePlanStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("bot")

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def build_components(cfg: Config):
    exchange = ExchangeClient(
        exchange_id=cfg.exchange_id,
        api_key=cfg.exchange_api_key,
        api_secret=cfg.exchange_api_secret,
        api_password=cfg.exchange_api_password,
        testnet=cfg.testnet,
    )

    risk_cfg = cfg.risk
    risk_engine = RiskEngine(
        starting_cash_usd=risk_cfg["starting_cash_usd"],
        max_position_pct=risk_cfg["max_position_pct"],
        max_total_exposure_pct=risk_cfg["max_total_exposure_pct"],
        max_loss_per_trade_pct=risk_cfg["max_loss_per_trade_pct"],
        max_daily_loss_pct=risk_cfg["max_daily_loss_pct"],
        max_volatility_pct=risk_cfg["max_volatility_pct"],
        min_risk_reward=risk_cfg["min_risk_reward"],
        cooldown_minutes=risk_cfg["cooldown_minutes"],
        kill_switch_path=PROJECT_ROOT / risk_cfg.get("kill_switch_file", "KILL_SWITCH"),
        symbol_whitelist=cfg.symbols,
    )

    log_cfg = cfg.logging_cfg
    trade_log_path = PROJECT_ROOT / log_cfg.get("trade_log_path", "logs/trades.csv")
    portfolio = Portfolio(cash=risk_cfg["starting_cash_usd"], trade_log_path=trade_log_path)

    if cfg.mode == "live":
        from src.broker_live import LiveBroker  # imported lazily: paper mode never needs this

        broker = LiveBroker(exchange, portfolio, cfg.mode, cfg.live_trading_confirmed)
    else:
        broker = PaperBroker(portfolio)

    store = TradePlanStore(
        current_path=PROJECT_ROOT / log_cfg.get("trade_plan_current_path", "logs/trade_plans_current.json"),
        history_path=PROJECT_ROOT / log_cfg.get("trade_plan_history_path", "logs/trade_plans_history.jsonl"),
    )
    audit = AuditLog(PROJECT_ROOT / log_cfg.get("audit_log_path", "logs/audit_log.jsonl"))

    return exchange, risk_engine, portfolio, broker, store, audit


def _execute_monitor_action(action, record, broker, store, risk_engine, event) -> None:
    event["monitor_action"] = action.__dict__
    if action.action == "FILL":
        quote_amount = record.plan["approved_quote_amount"]
        fill = broker.buy(action.symbol, quote_amount, action.price)
        store.mark_open(action.symbol, fill_price=fill.price, fill_amount=fill.base_amount)
        risk_engine.record_trade(action.symbol)
        event["execution"] = fill.__dict__
    elif action.action == "DISCARD":
        store.discard_pending(action.symbol, reason="invalidated", price=action.price)
    elif action.action in ("STOP_LOSS", "TAKE_PROFIT"):
        fill = broker.sell(action.symbol, action.price)
        store.mark_closed(action.symbol, reason=action.action.lower(), price=action.price)
        event["execution"] = fill.__dict__


def run_cycle(cfg: Config, exchange, risk_engine: RiskEngine, portfolio: Portfolio, broker, store: TradePlanStore, audit: AuditLog) -> None:
    indicators_cfg = cfg.indicators
    enriched_by_symbol = {}
    current_prices: dict[str, float] = {}

    for symbol in cfg.symbols:
        try:
            ohlcv = exchange.fetch_ohlcv_df(symbol, cfg.timeframe, cfg.history_bars)
            enriched = add_indicators(
                ohlcv, indicators_cfg["fast_ema"], indicators_cfg["slow_ema"], indicators_cfg["rsi_period"]
            )
            enriched_by_symbol[symbol] = enriched
            current_prices[symbol] = float(enriched["close"].iloc[-1])
        except Exception:
            logger.exception("failed to fetch/enrich market data for %s", symbol)

    equity = portfolio.equity(current_prices)

    for symbol in cfg.symbols:
        event: dict = {"symbol": symbol, "mode": cfg.mode, "timestamp": now_iso()}
        if symbol not in enriched_by_symbol:
            event["action"] = "none"
            event["error"] = "market data unavailable this cycle"
            audit.write(event)
            continue

        try:
            enriched = enriched_by_symbol[symbol]
            price = current_prices[symbol]

            record = store.get(symbol)
            if record is not None:
                action = monitor.check_pending(record, price) or monitor.check_open(record, price)
                if action is not None:
                    _execute_monitor_action(action, record, broker, store, risk_engine, event)
                else:
                    event["action"] = "none"
                    event["note"] = f"{record.status} plan active for {symbol}, no trigger this cycle"
                audit.write(event)
                continue

            # No active plan for this symbol — look for a brand new setup.
            candidates = scanner.scan(enriched, cfg.scanner)
            event["candidates"] = [
                {"pattern": c.pattern, "direction": c.direction, "strength": round(c.strength, 3)} for c in candidates
            ]

            signal = signal_engine.evaluate(candidates, **cfg.signal_engine)
            if signal is None:
                event["action"] = "none"
                audit.write(event)
                continue
            event["signal"] = {
                "direction": signal.direction,
                "pattern": signal.primary_pattern,
                "engine_score": round(signal.engine_score, 3),
                "supporting_patterns": signal.supporting_patterns,
            }

            if signal.direction == "bearish":
                # Long-only bot: a bearish signal isn't acted on directly — exits
                # are governed entirely by an open position's own stop/target
                # (src.monitor), which is more robust than reacting to every
                # bearish blip. It's still logged here for visibility.
                event["action"] = "none"
                event["note"] = "bearish signal logged only; exits are governed by the open plan's stop/target"
                audit.write(event)
                continue

            atr = float(enriched["atr"].iloc[-1])
            plan = trade_planner.build_plan(signal, symbol, price, atr, **cfg.trade_planner)
            if plan is None:
                event["action"] = "none"
                audit.write(event)
                continue
            event["plan"] = {**plan.__dict__}

            other_positions_value = sum(
                pos.amount * current_prices.get(sym, pos.entry_price)
                for sym, pos in portfolio.positions.items()
                if sym != symbol
            )
            atr_pct = atr / price if price else None
            decision = risk_engine.check_new_trade(
                symbol,
                "buy",
                portfolio_equity=equity,
                cash_available=portfolio.cash,
                entry_price=plan.entry_zone[1],
                stop_loss=plan.stop_loss,
                risk_reward=plan.risk_reward,
                open_positions_value=other_positions_value,
                atr_pct=atr_pct,
            )
            event["risk_decision"] = decision.__dict__
            if not decision.allowed:
                event["action"] = "none"
                audit.write(event)
                continue

            claude_verdict_dict = None
            advisor_cfg = cfg.advisor
            if advisor_cfg.get("enabled"):
                verdict = claude_advisor.review_trade_plan(
                    api_key=cfg.anthropic_api_key,
                    model=advisor_cfg.get("model", "claude-sonnet-4-5"),
                    symbol=symbol,
                    side="buy",
                    pattern=plan.pattern,
                    entry_zone=plan.entry_zone,
                    stop_loss=plan.stop_loss,
                    take_profit=plan.take_profit,
                    invalidation=plan.invalidation,
                    risk_reward=plan.risk_reward,
                    rule_based_confidence_stars=plan.rule_based_confidence_stars,
                    rationale=plan.rationale,
                    portfolio_context={"cash": portfolio.cash, "equity": equity},
                    fail_mode=advisor_cfg.get("fail_mode", "closed"),
                    timeout_seconds=advisor_cfg.get("timeout_seconds", 20),
                )
                claude_verdict_dict = verdict.__dict__
                event["advisor_verdict"] = claude_verdict_dict
                if not verdict.approved:
                    event["action"] = "none"
                    audit.write(event)
                    continue

            plan_dict = dict(plan.__dict__)
            plan_dict["approved_quote_amount"] = decision.max_quote_amount
            store.set_pending(symbol, plan_dict, claude_verdict_dict)
            risk_engine.record_trade(symbol)  # cooldown starts at planning time
            event["action"] = "planned"

            # If price is already inside the entry zone right now, don't wait
            # for the next tick to fill it.
            fresh_record = store.get(symbol)
            immediate = monitor.check_pending(fresh_record, price)
            if immediate is not None and immediate.action == "FILL":
                fill = broker.buy(symbol, plan_dict["approved_quote_amount"], price)
                store.mark_open(symbol, fill_price=fill.price, fill_amount=fill.base_amount)
                event["execution"] = fill.__dict__

        except Exception as exc:  # noqa: BLE001 — one symbol failing must not kill the whole cycle
            logger.exception("cycle failed for %s", symbol)
            event["error"] = str(exc)

        audit.write(event)


def main() -> None:
    parser = argparse.ArgumentParser(description="Hybrid rule-based + Claude-reviewed crypto trading bot")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument("--config", default=None, help="path to config.yaml (default: config/config.yaml)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    logger.info("starting bot: mode=%s exchange=%s symbols=%s", cfg.mode, cfg.exchange_id, cfg.symbols)

    exchange, risk_engine, portfolio, broker, store, audit = build_components(cfg)

    if args.once:
        run_cycle(cfg, exchange, risk_engine, portfolio, broker, store, audit)
        return

    interval = cfg.loop.get("poll_interval_seconds", 300)
    while True:
        run_cycle(cfg, exchange, risk_engine, portfolio, broker, store, audit)
        time.sleep(interval)


if __name__ == "__main__":
    main()
