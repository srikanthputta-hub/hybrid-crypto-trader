"""FROZEN, test-only copy of src/bot.py's run_cycle exactly as it was on origin/main
immediately before the bar-parity change (P1-P5, docs/bot_bar_parity.md; origin/main at
fd5cd8e) -- the "before" half of tests/test_bot_bar_parity.py's default-mode parity test.

Do not edit this to track src/bot.py: its whole value is that it does NOT change. With
loop.bar_parity off (the default), the refactored src/bot.py run_cycle must write exactly the
same audit events as this function, cycle by cycle. It shares every pipeline stage and helper
(scanner, signal engine, htf filter, planner, risk engine, monitor, _execute_monitor_action,
_maybe_notify, _held_symbols, _refresh_universe, _base_asset) with the live code; the per-base
helper is frozen below because the change gave it an extra (default-empty) argument. Safe to
delete once bar parity has shipped and been observed in paper.
"""
from __future__ import annotations

import logging

import src.bot as _bot
from src import claude_advisor, htf_filter, monitor, scanner, signal_engine, trade_planner
from src.audit import AuditLog
from src.bot import (
    ACTIVE_PLAN_STATUSES,
    _base_asset,
    _execute_monitor_action,
    _held_symbols,
    _maybe_notify,
    _refresh_universe,
)
from src.config import Config
from src.indicators import add_indicators
from src.market_data import MarketDataCache
from src.portfolio import Portfolio, now_iso
from src.risk_engine import RiskEngine
from src.trade_plan_store import TradePlanStore

logger = logging.getLogger("pre_bar_parity_bot")


def _frozen_active_plan_on_same_base(store: TradePlanStore, symbol: str) -> str | None:
    base = _base_asset(symbol)
    for other, record in store.all_current().items():
        if other != symbol and record.status in ACTIVE_PLAN_STATUSES and _base_asset(other) == base:
            return other
    return None


def pre_bar_parity_run_cycle(
    cfg: Config,
    exchange,
    risk_engine: RiskEngine,
    portfolio: Portfolio,
    broker,
    store: TradePlanStore,
    audit: AuditLog,
    market_data: MarketDataCache,
) -> None:
    def _record(event: dict) -> None:
        audit.write(event)
        _maybe_notify(cfg, event)

    indicators_cfg = cfg.indicators
    enriched_by_symbol = {}
    current_prices: dict[str, float] = {}

    # Held symbols are resolved BEFORE the fetch below so they're always in the
    # fetch set: a held symbol outside the universe must still get a current
    # price, or it would be valued at its stale entry_price in `equity` and
    # `other_positions_value` (Portfolio.equity's fallback) and its stop-loss/
    # take-profit could never fire. `cycle_symbols` (fetch + walk set) and
    # `held_symbols` (fetch priority) are exactly the two inputs P1a's
    # MarketDataCache.refresh(symbols, active_symbols=...) takes -- merged here
    # per the P1b review's explicit note: feed the cache the P1b-resolved
    # cycle_symbols/held_symbols (which include held-but-delisted positions),
    # not the narrower cfg.symbols/active_symbols P1a computed on its own
    # branch, or held-only symbols would silently stop being monitored again.
    held_symbols = _held_symbols(store, portfolio)
    held_set = set(held_symbols)
    cycle_symbols = _refresh_universe(cfg, exchange, risk_engine, held_symbols)

    ohlcv_by_symbol = market_data.refresh(cycle_symbols, active_symbols=held_symbols)
    log_cfg = cfg.logging_cfg
    try:
        market_data.write_snapshot(_bot.PROJECT_ROOT / log_cfg.get("market_snapshot_path", "logs/market_snapshot.json"))
    except Exception:
        # Display-only (scripts/export_dashboard_data.py reads this) -- must
        # never be able to abort the cycle before the monitor below gets to
        # check every OPEN position's stop-loss/take-profit.
        logger.exception("failed to write market data snapshot")

    for symbol in cycle_symbols:
        if symbol not in ohlcv_by_symbol:
            continue
        try:
            enriched = add_indicators(
                ohlcv_by_symbol[symbol], indicators_cfg["fast_ema"], indicators_cfg["slow_ema"], indicators_cfg["rsi_period"]
            )
            enriched_by_symbol[symbol] = enriched
            current_prices[symbol] = float(enriched["close"].iloc[-1])
        except Exception:
            logger.exception("failed to enrich market data for %s", symbol)

    equity = portfolio.equity(current_prices)

    for symbol in cycle_symbols:
        event: dict = {"symbol": symbol, "mode": cfg.mode, "timestamp": now_iso()}
        if symbol not in enriched_by_symbol:
            event["action"] = "none"
            event["error"] = "market data unavailable this cycle"
            if symbol in held_set:
                # Countable from the audit log (design doc §4.6's "zero 'held symbol
                # not priced' events" acceptance check). This position's stops can't
                # be checked this cycle and it's valued at entry_price in equity/
                # exposure until a fetch succeeds again.
                event["held_symbol_unpriced"] = True
                logger.warning("held symbol %s not priced this cycle -- stop/target not checked", symbol)
            _record(event)
            continue

        try:
            enriched = enriched_by_symbol[symbol]
            price = current_prices[symbol]

            record = store.get(symbol)
            if record is not None:
                # Runs for every held symbol, whether it's a tradeable universe member
                # or only walked because it's held (source="held") -- see
                # _execute_monitor_action for why the whitelist isn't consulted here.
                action = monitor.check_pending(record, price) or monitor.check_open(record, price)
                if action is not None:
                    _execute_monitor_action(action, record, broker, store, risk_engine, event)
                else:
                    event["action"] = "none"
                    event["note"] = f"{record.status} plan active for {symbol}, no trigger this cycle"
                _record(event)
                continue

            # Monitor-only symbol (walked because something is held on it, but not
            # one the risk engine may open NEW plans on): don't scan it for a new
            # setup. The risk engine's whitelist would reject any plan anyway; this
            # just skips the wasted scan/daily-trend fetch/planning. Can't trigger on
            # any symbol that's in market.symbols today.
            if symbol not in risk_engine.symbol_whitelist:
                event["action"] = "none"
                event["note"] = f"{symbol} is monitor-only (not in the tradeable universe); no new plan is opened on it"
                _record(event)
                continue

            # One active plan per BASE asset, not just per symbol (design doc §4.1):
            # e.g. an OPEN BTC/USDT plan blocks a new BTC/USD plan, so a USD/USDT
            # quote migration can never hold the same coin twice. Checked before the
            # scan, the same way the per-symbol rule above skips scanning a symbol
            # that already has a plan.
            same_base = _frozen_active_plan_on_same_base(store, symbol)
            if same_base is not None:
                event["action"] = "none"
                event["note"] = (
                    f"blocked: {same_base} already has an active plan on base asset "
                    f"{_base_asset(symbol)} (one active plan per base asset)"
                )
                _record(event)
                continue

            # No active plan for this symbol or its base — look for a brand new setup.
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
