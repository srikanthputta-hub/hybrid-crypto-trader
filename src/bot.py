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

from src import claude_advisor, monitor, notifier, scanner, signal_engine, trade_planner
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


def _fmt_price(price: float) -> str:
    """USD-formats a price with enough decimals to stay readable at any scale --
    a fixed 2 decimals reads as "$0.00" for a sub-cent coin like SHIB, which is
    useless. $1+ gets 2 decimals, 1c-$1 gets 4, anything smaller gets 6."""
    if price >= 1:
        return f"${price:,.2f}"
    if price >= 0.01:
        return f"${price:,.4f}"
    return f"${price:,.6f}"


def _maybe_notify(cfg: Config, event: dict) -> None:
    """Sends a Telegram message for the events worth interrupting someone's day for:
    a new signal, a fill, or a close. Plain-language/USD-formatted, since these are
    read by a person deciding whether to place the same trade on their own exchange,
    not just logged for a machine. Everything else (no-op cycles, discards, vetoes)
    stays in the audit log only. No-ops entirely if Telegram isn't configured (see
    src/notifier.py)."""
    bot_token, chat_id = cfg.telegram_bot_token, cfg.telegram_chat_id
    if not bot_token or not chat_id:
        return

    if event.get("action") == "planned" and "plan" in event:
        plan = event["plan"]
        entry_low, entry_high = plan.get("entry_zone", (None, None))
        notifier.send_telegram_message(
            bot_token,
            chat_id,
            f"🔔 *BUY signal*: {event['symbol']} ({plan.get('pattern')})\n"
            f"Buy between {_fmt_price(entry_low)} and {_fmt_price(entry_high)}\n"
            f"Stop-loss: {_fmt_price(plan.get('stop_loss'))} (sell here to cap the loss)\n"
            f"Target: {_fmt_price(plan.get('take_profit'))} (take profit here)\n"
            f"Risk:Reward 1:{plan.get('risk_reward')}  Confidence: {plan.get('rule_based_confidence_stars')}/5",
        )

    execution = event.get("execution")
    monitor_action = (event.get("monitor_action") or {}).get("action")
    if execution and monitor_action in ("STOP_LOSS", "TAKE_PROFIT"):
        reason = "Stop-loss hit" if monitor_action == "STOP_LOSS" else "Target reached"
        notifier.send_telegram_message(
            bot_token,
            chat_id,
            f"🏁 *SELL* — trade closed: {event['symbol']}\n"
            f"Reason: {reason}\n"
            f"Price: {_fmt_price(execution.get('price'))}",
        )
    elif execution:
        # A monitor-triggered FILL, or the immediate-fill-on-plan path (monitor_action is
        # unset there since it bypasses monitor.check_pending's normal call site).
        notifier.send_telegram_message(
            bot_token,
            chat_id,
            f"✅ *Bought*: {event['symbol']}\n"
            f"Price: {_fmt_price(execution.get('price'))}  Amount: {execution.get('base_amount')}",
        )


def _send_heartbeat(cfg: Config, current_prices: dict[str, float], portfolio: Portfolio, equity: float) -> None:
    """Sends one per-cycle Telegram status update (current prices + portfolio equity),
    regardless of whether anything actionable happened this cycle -- distinct from
    _maybe_notify's event-driven alerts. No-ops if Telegram isn't configured, or if
    market data failed for every symbol this cycle (nothing useful to report)."""
    bot_token, chat_id = cfg.telegram_bot_token, cfg.telegram_chat_id
    if not bot_token or not chat_id or not current_prices:
        return

    starting_cash = cfg.risk["starting_cash_usd"]
    pnl_pct = (equity - starting_cash) / starting_cash if starting_cash else 0.0
    price_lines = "\n".join(f"{symbol}: {_fmt_price(price)}" for symbol, price in current_prices.items())
    notifier.send_telegram_message(
        bot_token,
        chat_id,
        f"📊 *Heartbeat* (mode={cfg.mode})\n"
        f"{price_lines}\n"
        f"Equity: {equity:,.2f}  Cash: {portfolio.cash:,.2f}  PnL: {pnl_pct:+.2%}",
    )


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
    def _record(event: dict) -> None:
        audit.write(event)
        _maybe_notify(cfg, event)

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
    _send_heartbeat(cfg, current_prices, portfolio, equity)

    for symbol in cfg.symbols:
        event: dict = {"symbol": symbol, "mode": cfg.mode, "timestamp": now_iso()}
        if symbol not in enriched_by_symbol:
            event["action"] = "none"
            event["error"] = "market data unavailable this cycle"
            _record(event)
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
                _record(event)
                continue

            # No active plan for this symbol — look for a brand new setup.
            candidates = scanner.scan(enriched, cfg.scanner)
            event["candidates"] = [
                {"pattern": c.pattern, "direction": c.direction, "strength": round(c.strength, 3)} for c in candidates
            ]

            signal = signal_engine.evaluate(candidates, **cfg.signal_engine)
            if signal is None:
                event["action"] = "none"
                _record(event)
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
                _record(event)
                continue

            atr = float(enriched["atr"].iloc[-1])
            plan = trade_planner.build_plan(signal, symbol, price, atr, **cfg.trade_planner)
            if plan is None:
                event["action"] = "none"
                _record(event)
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
                _record(event)
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
                    _record(event)
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

        _record(event)


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
