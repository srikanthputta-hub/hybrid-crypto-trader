"""FROZEN, test-only copy of src/bot.py's run_cycle exactly as it was immediately
before P1b's universe wiring (commit 275e522, branch claude/redesign-p1b-bot-wiring
parent) -- the "before" half of tests/test_bot_universe_wiring.py's parity test.

Do not edit this to track src/bot.py: its whole value is that it does NOT change.
It walks `cfg.symbols` directly, has no per-base rule, no held-symbol union, and
never touches src/universe.py or RiskEngine.set_symbol_whitelist. It shares every
pipeline stage (scanner, signal engine, htf filter, planner, risk engine, monitor,
_execute_monitor_action, _maybe_notify) with the live code, so a parity failure
can only come from the wiring itself. Safe to delete once P1b has shipped and
been observed in paper.
"""
from __future__ import annotations

import logging

from src import claude_advisor, htf_filter, monitor, scanner, signal_engine, trade_planner
from src.audit import AuditLog
from src.bot import _execute_monitor_action, _maybe_notify
from src.config import Config
from src.indicators import add_indicators
from src.portfolio import Portfolio, now_iso
from src.risk_engine import RiskEngine
from src.trade_plan_store import TradePlanStore

logger = logging.getLogger("legacy_bot")


def legacy_run_cycle(cfg: Config, exchange, risk_engine: RiskEngine, portfolio: Portfolio, broker, store: TradePlanStore, audit: AuditLog) -> None:
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

            # Pipeline stage 2b — higher-timeframe (daily) trend confirmation gate
            # (see src/htf_filter.py): top-down multi-timeframe practice, the
            # daily trend sets bias, this 1h signal only times entries within it.
            # Never originates a trade, only blocks one -- same "can only say no"
            # shape as the risk engine and the Claude advisor below.
            if cfg.htf_filter.get("enabled", True):
                daily_confirmed = htf_filter.daily_trend_confirmed(
                    exchange, symbol, indicators_cfg["fast_ema"], indicators_cfg["slow_ema"]
                )
                event["daily_trend_confirmed"] = daily_confirmed
                if not daily_confirmed:
                    event["action"] = "none"
                    event["note"] = "blocked: daily trend not confirmed bullish (higher-timeframe filter)"
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
            risk_engine.record_trade(symbol)  # cooldown starts, and the global new-entry cap counts it, at planning time
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

