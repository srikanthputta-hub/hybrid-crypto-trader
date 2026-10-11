"""Main bot loop, wiring together the full pipeline:

  universe (src/universe.py) -> market data (ccxt) -> indicators -> scanner
  -> signal engine -> trade planner -> risk engine -> Claude advisor (optional)
  -> trade plan store (PENDING) -> 24/7 monitor (fills / discards / exits)
  -> paper or live broker

Which symbols a cycle walks is decided by the universe (infrastructure, not a
pipeline stage -- see docs/system-redesign-2026-10.md §2.1): its tradeable
members (today: exactly `market.symbols`, `universe.mode: static`) are what the
risk engine's whitelist allows NEW plans on, and every symbol the bot currently
holds a plan or position on is always walked too, in or out of the universe,
so its stop-loss/take-profit keeps being checked until it closes.

Every cycle, for every walked symbol, exactly one line is written to the audit
log regardless of what happened — so "why didn't it trade at 14:32" is always
answerable from logs/audit_log.jsonl alone.

`loop.bar_parity` (default false = the behaviour above, unchanged) makes a
cycle decide the way the strategy_pack_run backtest harness does: one decision
per symbol per CLOSED bar (persisted, so restarts don't re-decide), a monitor
pass over every held symbol first, then new entries in sorted(symbol) order with
room/cash as held before this bar's exits, an optional entry-staleness guard
(`loop.max_entry_delay_minutes`), and at 1d the htf gate on the scan frame. See
_run_bar_parity_passes and docs/bot_bar_parity.md.

Run one cycle: `python -m src.bot --once`
Run continuously at the configured poll interval: `python -m src.bot`
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from src import claude_advisor, htf_filter, monitor, notifier, scanner, signal_engine, trade_planner, universe
from src.audit import AuditLog
from src.broker_paper import PaperBroker
from src.config import HTF_SCAN_TAIL_BARS as _CONFIG_HTF_SCAN_TAIL_BARS
from src.config import BarParitySettings, Config, load_config
from src.exchange import ExchangeClient
from src.indicators import add_indicators
from src.market_data import MarketDataCache
from src.portfolio import Portfolio, Position, now_iso, reconstruct_cash
from src.risk_engine import RiskEngine
from src.trade_plan_store import TradePlanStore

# The one BASE/QUOTE parser in this repo. Imported (not reimplemented) so the
# per-base one-plan rule below can never disagree with the universe about what
# "BTC/USDT"'s base asset is -- UniverseMember.base is filled by this same
# function. Private by name only; promoting it to a public `split_symbol` is a
# Platform Engineer follow-up (src/universe.py is out of this change's scope).
from src.universe import _split_symbol

# Plan statuses the per-base one-plan rule counts as "this base is taken".
ACTIVE_PLAN_STATUSES = ("PENDING", "OPEN")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("bot")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# P5 (docs/multiday_hold_design.md §3.1 "htf_filter" row): at market.timeframe
# "1d" with loop.bar_parity on, the daily-trend gate is evaluated on the last
# this-many bars of the scan frame instead of a separate 31-bar 1d fetch. It is
# the backtest harness's own window (strategy_pack_run/backtest_live_bot.py
# SCAN_TAIL_BARS, which the harness's daily htf branch also uses); the gate
# recomputes its EMAs on exactly these closes, so the same 150 closes give the
# same decision. tests/test_bot_bar_parity.py asserts the two constants agree.
HTF_SCAN_TAIL_BARS = _CONFIG_HTF_SCAN_TAIL_BARS  # 150; defined in src/config.py, which validates history_bars against it


def build_components(cfg: Config):
    # Bar-parity keys (loop.bar_parity, loop.max_entry_delay_minutes,
    # logging.bar_decisions_path, logging.closed_bars_dir) are validated first, so a
    # bad value stops the process at startup instead of failing every cycle.
    _ = cfg.bar_parity
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
        # Optional keys: only passed when present, so RiskEngine's own defaults (and its
        # own bounds validation) stay the single source of truth when they're omitted.
        **{
            key: risk_cfg[key]
            for key in ("max_new_entries_per_window", "new_entry_window_minutes", "min_order_quote_usd")
            if key in risk_cfg
        },
    )
    # Startup universe resolution (docs/system-redesign-2026-10.md §3: the whitelist
    # is "built in bot.build_components; re-set on every universe refresh"). Called
    # with no held symbols and deliberately NOT wrapped in a try/except: a universe
    # config error (an unimplemented `universe.mode`, a `market.symbols` entry that
    # isn't BASE/QUOTE) fails the process at startup, the same way a nonsensical
    # risk config already does above -- rather than the bot quietly running on a
    # universe the operator didn't ask for. Held symbols are left out on purpose so
    # the contents of logs/trade_plans_current.json can never block startup (and
    # with it, monitoring of every other position); run_cycle passes them every
    # cycle instead, with a fallback that keeps monitoring going (see
    # _refresh_universe). In static mode this whitelist is exactly cfg.symbols,
    # i.e. the same set the constructor was just given -- no behavior change yet.
    startup_universe = universe.refresh(exchange, cfg)
    risk_engine.set_symbol_whitelist(_tradeable_symbols(startup_universe))

    log_cfg = cfg.logging_cfg
    trade_log_path = PROJECT_ROOT / log_cfg.get("trade_log_path", "logs/trades.csv")

    store = TradePlanStore(
        current_path=PROJECT_ROOT / log_cfg.get("trade_plan_current_path", "logs/trade_plans_current.json"),
        history_path=PROJECT_ROOT / log_cfg.get("trade_plan_history_path", "logs/trade_plans_history.jsonl"),
    )
    # The risk engine's global new-entry window is in-memory; without this a restart
    # (deploy, crash, systemd Restart=always) would forget entries accepted minutes ago
    # and re-open every slot mid-burst. Every still-active plan's acceptance time is
    # replayed into it (old ones simply fall outside the window and don't count).
    risk_engine.seed_recent_entries(record.created_at for record in store.all_current().values())

    # Portfolio itself is in-memory only (see reconstruct_cash's docstring in
    # src/portfolio.py) -- rebuild it from what's actually happened so far
    # instead of a blank slate, or every scheduled run "forgets" every prior
    # fill. Without this, the paper broker's sell() KeyErrors the moment an
    # OPEN position's stop-loss/take-profit fires (that symbol was never bought
    # into *this* process's Portfolio), the plan can never actually close, and
    # the symbol stays locked out of new signals forever since run_cycle below
    # never re-scans a symbol that still has an active plan.
    cash = reconstruct_cash(trade_log_path, risk_cfg["starting_cash_usd"])
    positions = {
        symbol: Position(symbol=symbol, amount=record.fill_amount, entry_price=record.fill_price)
        for symbol, record in store.all_current().items()
        if record.status == "OPEN" and record.fill_amount and record.fill_price
    }
    portfolio = Portfolio(cash=cash, positions=positions, trade_log_path=trade_log_path)

    if cfg.mode == "live":
        from src.broker_live import LiveBroker  # imported lazily: paper mode never needs this

        broker = LiveBroker(exchange, portfolio, cfg.mode, cfg.live_trading_confirmed)
    else:
        broker = PaperBroker(portfolio)

    audit = AuditLog(PROJECT_ROOT / log_cfg.get("audit_log_path", "logs/audit_log.jsonl"))

    return exchange, risk_engine, portfolio, broker, store, audit


def build_market_data(cfg: Config, exchange: ExchangeClient) -> MarketDataCache:
    """Kept separate from build_components() rather than folded into its
    return tuple: build_components' 6-item return is unpacked positionally
    in several places (scripts/run_live.py, tests/test_bot.py) -- appending a
    7th item would silently break every one of those call sites. Call this
    right after build_components() instead (see main() / scripts/run_live.py)."""
    return MarketDataCache(exchange, cfg.timeframe, cfg.history_bars)


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


def _tradeable_symbols(resolved: universe.Universe) -> list[str]:
    """The universe members NEW plans may be opened on: everything except
    `source="held"` members, which are in the universe only so an existing plan
    on them keeps being monitored to completion (design doc §4.1: "add held
    symbols as source="held" (monitor-only)"). A held symbol that is also a
    pinned/discovered member is listed under that source instead (universe.py
    never double-lists it), so it stays tradeable."""
    return [m.symbol for m in resolved.members if m.source != "held"]


def _held_symbols(store: TradePlanStore, portfolio: Portfolio) -> list[str]:
    """Every symbol the bot currently has something at stake in, in a stable
    order: every record in the trade plan store (anything store.get() returns --
    exactly the set run_cycle's monitor branch acts on), then any portfolio
    position not already covered. Positions are a subset of OPEN records by
    construction (build_components, broker.buy+mark_open, broker.sell+
    mark_closed); they're included anyway so a position can never go unpriced,
    and so never be valued at its stale entry price in equity/exposure, even if
    its record were somehow missing."""
    held = list(store.all_current())
    seen = set(held)
    for symbol in portfolio.positions:
        if symbol not in seen:
            held.append(symbol)
            seen.add(symbol)
    return held


def _refresh_universe(cfg: Config, exchange, risk_engine: RiskEngine, held_symbols: list[str]) -> list[str]:
    """Per-cycle universe refresh: resolves the universe with the current held
    symbols, re-sets the risk engine's whitelist from it, and returns the
    symbols this cycle fetches and walks (universe members, in universe order,
    then any held symbol the universe somehow didn't include).

    Runs every cycle rather than once a day because `held_symbols` changes
    every cycle, and in `universe.mode: static` refresh() is a pure function of
    (cfg, held_symbols) with zero exchange calls. When `discover` mode lands,
    its expensive membership step (load_markets, 1d OHLCV) gets the daily
    `universe.refresh_utc` gate; the held-symbol union below must stay
    per-cycle regardless.

    Never raises -- a failed refresh must not stop held positions from being
    monitored. On a failed refresh() the cycle falls back to `market.symbols`
    and the risk engine keeps its previous whitelist untouched (set_symbol_
    whitelist swaps atomically, so a failure inside it also leaves the old one
    in place). The held-symbol union at the end is independent of refresh()
    entirely: whatever happens above, every held symbol is walked this cycle.
    """
    symbols: list[str]
    try:
        resolved = universe.refresh(exchange, cfg, held_symbols=held_symbols)
    except Exception:
        logger.exception(
            "universe refresh failed -- falling back to market.symbols for this cycle; "
            "risk engine whitelist left unchanged; held symbols are still monitored"
        )
        symbols = list(cfg.symbols)
    else:
        symbols = [m.symbol for m in resolved.members]
        try:
            risk_engine.set_symbol_whitelist(_tradeable_symbols(resolved))
        except Exception:
            logger.exception("setting the risk engine whitelist from the universe failed -- previous whitelist kept")

    walked = set(symbols)
    for symbol in held_symbols:
        if symbol not in walked:
            symbols.append(symbol)
            walked.add(symbol)
    return symbols


def _base_asset(symbol: str) -> str:
    """BASE of a BASE/QUOTE symbol via universe.py's own parser. A symbol that
    doesn't parse is treated as its own base, so it can only ever collide with
    itself -- which is just the per-symbol rule run_cycle already enforces --
    rather than raising mid-cycle or matching something it shouldn't. Bases are
    compared exactly as ccxt spells them (no case-folding or XBT/BTC aliasing;
    ccxt's unified symbols already normalize both)."""
    try:
        return _split_symbol(symbol)[0]
    except ValueError:
        return symbol


def _active_plan_on_same_base(store: TradePlanStore, symbol: str, also_taken=()) -> str | None:
    """Another symbol sharing `symbol`'s base asset with a PENDING/OPEN plan,
    or None. Read live from the store at check time (not snapshotted at cycle
    start), so a plan opened earlier in this same cycle on e.g. BTC/USD blocks
    BTC/USDT later in the same cycle too.

    `also_taken` (bar parity only, P3): symbols whose position was closed
    earlier in THIS cycle. They still count as holding their base for the rest
    of the bar, the same way their exposure still counts (the backtest only
    executes an exit at the next bar's open). Empty by default = unchanged."""
    base = _base_asset(symbol)
    for other, record in store.all_current().items():
        if other != symbol and record.status in ACTIVE_PLAN_STATUSES and _base_asset(other) == base:
            return other
    for other in also_taken:
        if other != symbol and _base_asset(other) == base:
            return other
    return None


# --------------------------------------------------------------------------- #
# Bar parity (P1-P5, docs/multiday_hold_design.md §3.1 and §6; docs/bot_bar_parity.md)
# --------------------------------------------------------------------------- #
class BarDecisionLedger:
    """P1 state: the close time of the last bar each symbol was DECIDED on.

    A symbol's bar counts as decided once the bot has acted on it for that bar
    in any way: a monitor check of a held symbol (trigger or not), or the start
    of the entry pass for a FLAT symbol (whatever the outcome, including the P4
    staleness skip and an exception mid-pipeline). A FLAT symbol is only scanned
    when its latest closed bar is newer than the recorded one.

    Persisted (write-through, temp file + replace) to `path` as
    {"timeframe": <market.timeframe>, "bars": {symbol: ISO bar-close time}}, so a
    restart in the middle of a bar does not decide it again. A missing file means
    "nothing decided yet".

    Anything that cannot be trusted is treated as unreadable -- logged, and that
    symbol's (or the whole file's) current bar is decided once more -- rather
    than allowed to block a symbol: an unparseable file, a different or missing
    timeframe (e.g. a 1h ledger after a switch to 1d: its hourly closes would
    otherwise block every daily bar until the next day), and per entry a
    null/empty/unparseable/NaT value (NaT would make is_new() False forever) or
    a time later than `now` (no closed bar can be). Re-deciding once is safe: the
    P4 staleness guard, when on, still skips a late entry. A failed write is
    logged and the in-memory state still gates this process."""

    def __init__(self, path: Path, timeframe: str, now: pd.Timestamp | None = None) -> None:
        self.path = Path(path)
        self.timeframe = timeframe
        self._last: dict[str, pd.Timestamp] = {}
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            if not isinstance(raw, dict) or not isinstance(raw.get("bars"), dict):
                raise ValueError('not a {"timeframe": ..., "bars": {...}} JSON object')
        except Exception:
            logger.exception(
                "could not read bar decision ledger %s -- starting empty (every symbol's current bar is decided again)",
                self.path,
            )
            return
        if raw.get("timeframe") != timeframe:
            logger.warning(
                "bar decision ledger %s was written for timeframe %r, running %r -- ignoring it "
                "(every symbol's current bar is decided again)", self.path, raw.get("timeframe"), timeframe,
            )
            return
        for sym, value in raw["bars"].items():
            ts = self._parse(value)
            if ts is None:
                logger.warning("bar decision ledger %s: unreadable entry %r for %s -- deciding its bar again",
                               self.path, value, sym)
                continue
            if now is not None and ts > now:
                logger.warning("bar decision ledger %s: entry %s for %s is later than now (%s) -- deciding its bar again",
                               self.path, ts.isoformat(), sym, now.isoformat())
                continue
            self._last[str(sym)] = ts

    @staticmethod
    def _parse(value) -> pd.Timestamp | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            ts = pd.Timestamp(value)
        except (ValueError, TypeError, OverflowError):
            return None
        if pd.isna(ts):
            return None
        return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")

    def last(self, symbol: str) -> pd.Timestamp | None:
        return self._last.get(symbol)

    def is_new(self, symbol: str, bar_close: pd.Timestamp) -> bool:
        previous = self._last.get(symbol)
        return previous is None or bar_close > previous

    def mark(self, symbol: str, bar_close: pd.Timestamp) -> None:
        previous = self._last.get(symbol)
        if previous is not None and previous >= bar_close:
            return
        self._last[symbol] = bar_close
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            payload = {"timeframe": self.timeframe, "bars": {s: t.isoformat() for s, t in sorted(self._last.items())}}
            tmp.write_text(json.dumps(payload, indent=1))
            tmp.replace(self.path)
        except Exception:
            logger.exception("failed to persist bar decision ledger %s (in-memory state still applies)", self.path)


# Module-level, like htf_filter._cache: run_cycle's signature stays unchanged
# (design doc §3.1 "Pipeline shape / contracts"). Keyed by resolved file path so
# two configs (e.g. two tests) never share state.
_bar_ledgers: dict[str, BarDecisionLedger] = {}
# closed-bar archive: (dir, symbol, timeframe) -> last bar OPEN time written, ms.
_archived_through_ms: dict[tuple[str, str, str], int] = {}


def _resolve_log_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _bar_ledger(settings: BarParitySettings, timeframe: str, now: pd.Timestamp) -> BarDecisionLedger:
    """The process's ledger for this file, loaded on first use (validated against
    `timeframe` and `now`, the exchange clock)."""
    path = _resolve_log_path(settings.decisions_path)
    key = str(path.resolve())
    ledger = _bar_ledgers.get(key)
    if ledger is None:
        ledger = BarDecisionLedger(path, timeframe, now)
        _bar_ledgers[key] = ledger
    return ledger


def _reset_bar_parity_state() -> None:
    """Forget all in-memory bar-parity state (tests, and simulating a restart)."""
    _bar_ledgers.clear()
    _archived_through_ms.clear()


def _timeframe_ms(exchange, timeframe: str) -> int:
    return int(exchange.client.parse_timeframe(timeframe) * 1000)


def _last_bar_close(ohlcv: pd.DataFrame, tf_ms: int) -> pd.Timestamp:
    """Close time of the last CLOSED bar in a fetch_ohlcv_df frame (its
    `timestamp` column is each bar's OPEN time; the forming bar is already
    dropped)."""
    return pd.Timestamp(ohlcv["timestamp"].iloc[-1]) + pd.Timedelta(milliseconds=tf_ms)


_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _closed_bars_file(directory: Path, symbol: str, timeframe: str) -> Path:
    # BTC/USDT -> BTC_USDT_1d.csv: the same <BASE>_<QUOTE>_<tf>.csv naming as
    # strategy_pack_run/data*/, so backtest_live_bot.load_dir can read it as is.
    name = _UNSAFE_FILENAME_CHARS.sub("_", symbol.replace("/", "_"))
    return directory / f"{name}_{timeframe}.csv"


def _archive_closed_bars(directory: Path, symbol: str, timeframe: str, ohlcv: pd.DataFrame) -> None:
    """Appends every closed bar in `ohlcv` newer than what was already written
    for this symbol (the whole fetched frame the first time), in the
    strategy_pack_run data-CSV format: timestamp = bar OPEN time in epoch ms,
    then open/high/low/close/volume as repr() floats (exact round trip). The
    weekly parity check (design §5/§6) needs the exact bars the bot decided on.
    Never pruned by log retention (src/log_retention.py only touches the audit
    log and plan history). Bars are recorded as first seen: a later exchange
    revision of an already-written bar is not rewritten."""
    path = _closed_bars_file(directory, symbol, timeframe)
    key = (str(directory), symbol, timeframe)
    last_ms = _archived_through_ms.get(key)
    if last_ms is None and path.exists():
        with open(path) as f:
            lines = [line for line in f.read().splitlines() if line.strip()]
        if len(lines) > 1:
            last_ms = int(lines[-1].split(",", 1)[0])
    if len(ohlcv) == 0:
        return
    # pd.Timestamp(...).value is always nanoseconds, whatever resolution the column has.
    open_ms = [int(pd.Timestamp(ts).value // 1_000_000) for ts in ohlcv["timestamp"]]
    rows = []
    for ms, (_, row) in zip(open_ms, ohlcv.iterrows()):
        if last_ms is not None and ms <= last_ms:
            continue
        rows.append(
            f"{ms},{float(row['open'])!r},{float(row['high'])!r},{float(row['low'])!r},"
            f"{float(row['close'])!r},{float(row['volume'])!r}\n"
        )
    if not rows:
        _archived_through_ms[key] = last_ms if last_ms is not None else open_ms[-1]
        return
    directory.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    with open(path, "a") as f:
        if write_header:
            f.write("timestamp,open,high,low,close,volume\n")
        f.writelines(rows)
        f.flush()
        os.fsync(f.fileno())
    _archived_through_ms[key] = max(open_ms)


@dataclass
class _Cycle:
    """Everything one run_cycle pass shares between the per-symbol helpers."""

    cfg: Config
    exchange: object
    risk_engine: RiskEngine
    portfolio: Portfolio
    broker: object
    store: TradePlanStore
    record: object  # callable(event) -> None
    current_prices: dict[str, float]
    equity: float
    # Bar parity only (P3): exposure value / net cash of positions closed earlier
    # in this cycle, credited back so new entries see the room held at the bar
    # close, before this bar's exits; and those symbols, for the per-base rule.
    parity: bool = False
    exited_value: float = 0.0
    exit_proceeds: float = 0.0
    exited_symbols: list[str] = field(default_factory=list)
    # Bar parity only (P3/P6): gross approved quote of every fill made earlier in this
    # cycle (a PENDING plan's monitor FILL or a fresh plan's immediate fill), by symbol.
    # The paper broker books such a position at quote*(1-fee); the harness counts the
    # gross quote. Entry checks count the gross quote (never less than the booked value).
    same_bar_fills: dict[str, float] = field(default_factory=dict)
    htf_from_scan_frame: bool = False


def _execute_monitor_action(action, record, broker, store, risk_engine, event) -> None:
    # Deliberate (P1b): this path never consults the risk engine's symbol
    # whitelist. The whitelist only gates ORIGINATING a new plan (the same scope
    # RiskEngine.set_symbol_whitelist documents), so a PENDING plan that was
    # approved while its symbol was tradeable still fills normally even if the
    # symbol has since left the universe, and an OPEN position on such a symbol
    # still hits its stop-loss/take-profit. The plan already passed every risk
    # check when it was accepted; leaving the universe is a liquidity/listing
    # judgment about future entries, not an invalidation of this plan's thesis
    # (which has its own invalidation level, checked by monitor.check_pending).
    event["monitor_action"] = action.__dict__
    if action.action == "FILL":
        quote_amount = record.plan["approved_quote_amount"]
        fill = broker.buy(action.symbol, quote_amount, action.price)
        store.mark_open(action.symbol, fill_price=fill.price, fill_amount=fill.base_amount)
        # Refreshes the per-symbol cooldown as before. new_entry=False: this plan was
        # already counted toward the global new-entry cap when it was accepted (the
        # record_trade call in run_cycle), so its later fill must not count it twice.
        risk_engine.record_trade(action.symbol, new_entry=False)
        event["execution"] = fill.__dict__
    elif action.action == "DISCARD":
        store.discard_pending(action.symbol, reason="invalidated", price=action.price)
    elif action.action in ("STOP_LOSS", "TAKE_PROFIT"):
        fill = broker.sell(action.symbol, action.price)
        store.mark_closed(action.symbol, reason=action.action.lower(), price=action.price)
        event["execution"] = fill.__dict__


def _monitor_held_symbol(c: _Cycle, symbol: str, record, price: float, event: dict) -> None:
    """Stage 5 for a symbol with an active plan: runs for every held symbol,
    whether it's a tradeable universe member or only walked because it's held
    (source="held") -- see _execute_monitor_action for why the whitelist isn't
    consulted here. Records the event itself.

    Bar parity (P3): an exit executed here is remembered on `c` so the entry
    pass can still count the closed position's exposure (and not its sale
    proceeds) for the rest of this bar."""
    action = monitor.check_pending(record, price) or monitor.check_open(record, price)
    if action is not None:
        if c.parity and action.action in ("STOP_LOSS", "TAKE_PROFIT"):
            pos = c.portfolio.positions.get(symbol)
            held_value = pos.amount * c.current_prices.get(symbol, pos.entry_price) if pos is not None else 0.0
            cash_before = c.portfolio.cash
            try:
                _execute_monitor_action(action, record, c.broker, c.store, c.risk_engine, event)
            finally:
                if pos is not None and symbol not in c.portfolio.positions:
                    c.exited_value += held_value
                    c.exit_proceeds += c.portfolio.cash - cash_before
                    c.exited_symbols.append(symbol)
        else:
            _execute_monitor_action(action, record, c.broker, c.store, c.risk_engine, event)
            if c.parity and action.action == "FILL" and symbol in c.portfolio.positions:
                c.same_bar_fills[symbol] = float(record.plan["approved_quote_amount"])
    else:
        event["action"] = "none"
        event["note"] = f"{record.status} plan active for {symbol}, no trigger this cycle"
    c.record(event)


def _evaluate_new_entry(c: _Cycle, symbol: str, enriched, price: float, event: dict) -> bool:
    """Stages 1-4b (+ immediate fill) for a symbol with no active plan.

    Returns True when it has already recorded the event (every "action: none"
    outcome), False on the "planned" path, where the caller records it. That is
    exactly the control flow the loop body had before this was extracted, so an
    exception anywhere in here still reaches the caller's per-symbol except."""
    cfg = c.cfg
    indicators_cfg = cfg.indicators

    # Monitor-only symbol (walked because something is held on it, but not
    # one the risk engine may open NEW plans on): don't scan it for a new
    # setup. The risk engine's whitelist would reject any plan anyway; this
    # just skips the wasted scan/daily-trend fetch/planning. Can't trigger on
    # any symbol that's in market.symbols today.
    if symbol not in c.risk_engine.symbol_whitelist:
        event["action"] = "none"
        event["note"] = f"{symbol} is monitor-only (not in the tradeable universe); no new plan is opened on it"
        c.record(event)
        return True

    # One active plan per BASE asset, not just per symbol (design doc §4.1):
    # e.g. an OPEN BTC/USDT plan blocks a new BTC/USD plan, so a USD/USDT
    # quote migration can never hold the same coin twice. Checked before the
    # scan, the same way the per-symbol rule above skips scanning a symbol
    # that already has a plan.
    same_base = _active_plan_on_same_base(c.store, symbol, c.exited_symbols)
    if same_base is not None:
        event["action"] = "none"
        event["note"] = (
            f"blocked: {same_base} already has an active plan on base asset "
            f"{_base_asset(symbol)} (one active plan per base asset)"
        )
        c.record(event)
        return True

    # No active plan for this symbol or its base — look for a brand new setup.
    candidates = scanner.scan(enriched, cfg.scanner)
    event["candidates"] = [
        {"pattern": cand.pattern, "direction": cand.direction, "strength": round(cand.strength, 3)}
        for cand in candidates
    ]

    signal = signal_engine.evaluate(candidates, **cfg.signal_engine)
    if signal is None:
        event["action"] = "none"
        c.record(event)
        return True
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
        c.record(event)
        return True

    # Pipeline stage 2b — higher-timeframe (daily) trend confirmation gate
    # (see src/htf_filter.py): top-down multi-timeframe practice, the
    # daily trend sets bias, this 1h signal only times entries within it.
    # Never originates a trade, only blocks one -- same "can only say no"
    # shape as the risk engine and the Claude advisor below.
    if cfg.htf_filter.get("enabled", True):
        if c.htf_from_scan_frame:
            # P5 (bar parity at market.timeframe "1d"): the scan frame IS the
            # daily chart, so evaluate the gate on its last HTF_SCAN_TAIL_BARS
            # closes -- the backtest's window -- instead of a separate 31-bar
            # 1d fetch (which differs on ~3% of signals, design doc E8).
            daily_confirmed = htf_filter.daily_trend_confirmed_from_df(
                enriched.tail(HTF_SCAN_TAIL_BARS), indicators_cfg["fast_ema"], indicators_cfg["slow_ema"]
            )
            event["daily_trend_source"] = "scan_frame"
        else:
            daily_confirmed = htf_filter.daily_trend_confirmed(
                c.exchange, symbol, indicators_cfg["fast_ema"], indicators_cfg["slow_ema"]
            )
        event["daily_trend_confirmed"] = daily_confirmed
        if not daily_confirmed:
            event["action"] = "none"
            event["note"] = "blocked: daily trend not confirmed bullish (higher-timeframe filter)"
            c.record(event)
            return True

    atr = float(enriched["atr"].iloc[-1])
    plan = trade_planner.build_plan(signal, symbol, price, atr, **cfg.trade_planner)
    if plan is None:
        event["action"] = "none"
        c.record(event)
        return True
    event["plan"] = {**plan.__dict__}

    other_positions_value = sum(
        pos.amount * c.current_prices.get(sym, pos.entry_price)
        for sym, pos in c.portfolio.positions.items()
        if sym != symbol
    )
    cash_available = c.portfolio.cash
    if c.parity:
        # P3: room and cash as held at the bar close, before this bar's exits
        # (the backtest executes them at the next open). Only ever subtracts
        # room/cash, so it can only shrink or reject a plan, never grow one.
        # Same-cycle FILLs are already in portfolio.positions/cash (the P6
        # harness accounting matches this, see docs/p6_harness_exposure_fix.md).
        other_positions_value += c.exited_value
        cash_available -= c.exit_proceeds
        # Same-bar fills at their gross approved quote, as the P6 harness counts them
        # (count_same_bar_approvals): the paper position is worth quote*(1-fee), so without
        # this ~0.1% of every same-bar fill reappears as room for live-only "dust" plans
        # (docs/bot_bar_parity.md §4.2). max(0, ...): can only shrink room, never grow it.
        # Cash needs no adjustment: Portfolio.apply_buy already debits the gross quote.
        for sym, gross_quote in c.same_bar_fills.items():
            pos = c.portfolio.positions.get(sym)
            if sym == symbol or pos is None:
                continue
            booked = pos.amount * c.current_prices.get(sym, pos.entry_price)
            other_positions_value += max(0.0, gross_quote - booked)
    atr_pct = atr / price if price else None
    decision = c.risk_engine.check_new_trade(
        symbol,
        "buy",
        portfolio_equity=c.equity,
        cash_available=cash_available,
        entry_price=plan.entry_zone[1],
        stop_loss=plan.stop_loss,
        risk_reward=plan.risk_reward,
        open_positions_value=other_positions_value,
        atr_pct=atr_pct,
    )
    event["risk_decision"] = decision.__dict__
    if not decision.allowed:
        event["action"] = "none"
        c.record(event)
        return True

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
            portfolio_context={"cash": c.portfolio.cash, "equity": c.equity},
            fail_mode=advisor_cfg.get("fail_mode", "closed"),
            timeout_seconds=advisor_cfg.get("timeout_seconds", 20),
        )
        claude_verdict_dict = verdict.__dict__
        event["advisor_verdict"] = claude_verdict_dict
        if not verdict.approved:
            event["action"] = "none"
            c.record(event)
            return True

    plan_dict = dict(plan.__dict__)
    plan_dict["approved_quote_amount"] = decision.max_quote_amount
    c.store.set_pending(symbol, plan_dict, claude_verdict_dict)
    c.risk_engine.record_trade(symbol)  # cooldown starts, and the global new-entry cap counts it, at planning time
    event["action"] = "planned"

    # If price is already inside the entry zone right now, don't wait
    # for the next tick to fill it.
    fresh_record = c.store.get(symbol)
    immediate = monitor.check_pending(fresh_record, price)
    if immediate is not None and immediate.action == "FILL":
        fill = c.broker.buy(symbol, plan_dict["approved_quote_amount"], price)
        if c.parity:
            c.same_bar_fills[symbol] = float(plan_dict["approved_quote_amount"])
        c.store.mark_open(symbol, fill_price=fill.price, fill_amount=fill.base_amount)
        event["execution"] = fill.__dict__
    return False


def _unavailable_event(event: dict, symbol: str, held_set: set[str]) -> dict:
    event["action"] = "none"
    event["error"] = "market data unavailable this cycle"
    if symbol in held_set:
        # Countable from the audit log (design doc §4.6's "zero 'held symbol
        # not priced' events" acceptance check). This position's stops can't
        # be checked this cycle and it's valued at entry_price in equity/
        # exposure until a fetch succeeds again.
        event["held_symbol_unpriced"] = True
        logger.warning("held symbol %s not priced this cycle -- stop/target not checked", symbol)
    return event


def run_cycle(
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

    parity = cfg.bar_parity
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
        market_data.write_snapshot(PROJECT_ROOT / log_cfg.get("market_snapshot_path", "logs/market_snapshot.json"))
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
    cycle = _Cycle(
        cfg=cfg, exchange=exchange, risk_engine=risk_engine, portfolio=portfolio, broker=broker, store=store,
        record=_record, current_prices=current_prices, equity=equity,
    )

    if parity.enabled:
        _run_bar_parity_passes(cycle, parity, cycle_symbols, held_set, ohlcv_by_symbol, enriched_by_symbol)
        return

    for symbol in cycle_symbols:
        event: dict = {"symbol": symbol, "mode": cfg.mode, "timestamp": now_iso()}
        if symbol not in enriched_by_symbol:
            _record(_unavailable_event(event, symbol, held_set))
            continue

        try:
            enriched = enriched_by_symbol[symbol]
            price = current_prices[symbol]

            record = store.get(symbol)
            if record is not None:
                _monitor_held_symbol(cycle, symbol, record, price, event)
                continue

            if _evaluate_new_entry(cycle, symbol, enriched, price, event):
                continue

        except Exception as exc:  # noqa: BLE001 — one symbol failing must not kill the whole cycle
            logger.exception("cycle failed for %s", symbol)
            event["error"] = str(exc)

        _record(event)


def _run_bar_parity_passes(
    c: _Cycle,
    settings: BarParitySettings,
    cycle_symbols: list[str],
    held_set: set[str],
    ohlcv_by_symbol: dict,
    enriched_by_symbol: dict,
) -> None:
    """run_cycle with loop.bar_parity on (docs/multiday_hold_design.md §3.1,
    docs/bot_bar_parity.md): the backtest harness's per-bar order of work.

    Pass 1 (monitor), in walk order: every held symbol's fill/discard/exit
    check, every cycle (idempotent within a bar: the price is the last close).
    Its bar is recorded as decided (P1), so a symbol that exits on this bar is
    not re-scanned until the next closed bar.

    Pass 2 (entries), in sorted(symbol) order (P2, the harness's order): each
    FLAT symbol whose latest closed bar has not been decided yet is recorded as
    decided and then, unless the bar is older than loop.max_entry_delay_minutes
    (P4), run through stages 1-4b with room/cash as held before this bar's exits
    (P3) and, at 1d, the htf gate on the scan frame (P5). An already-decided bar
    produces a "none" event. Still exactly one audit line per walked symbol."""
    cfg = c.cfg
    c.parity = True
    c.htf_from_scan_frame = cfg.timeframe == "1d"
    tf_ms = _timeframe_ms(c.exchange, cfg.timeframe)
    now_ms = int(c.exchange.client.milliseconds())
    ledger = _bar_ledger(settings, cfg.timeframe, pd.Timestamp(now_ms, unit="ms", tz="UTC"))

    if settings.closed_bars_dir is not None:
        bars_dir = _resolve_log_path(settings.closed_bars_dir)
        for symbol in cycle_symbols:
            if symbol not in ohlcv_by_symbol:
                continue
            try:
                _archive_closed_bars(bars_dir, symbol, cfg.timeframe, ohlcv_by_symbol[symbol])
            except Exception:
                # Audit-only output: never allowed to stop the monitor below.
                logger.exception("failed to archive closed bars for %s", symbol)

    bar_close: dict[str, pd.Timestamp] = {}
    deferred: list[str] = []

    # ---- pass 1: monitor every held symbol, in walk order ----
    for symbol in cycle_symbols:
        event: dict = {"symbol": symbol, "mode": cfg.mode, "timestamp": now_iso()}
        if symbol not in enriched_by_symbol:
            c.record(_unavailable_event(event, symbol, held_set))
            continue
        try:
            bar_close[symbol] = _last_bar_close(ohlcv_by_symbol[symbol], tf_ms)
            event["bar_close"] = bar_close[symbol].isoformat()
            record = c.store.get(symbol)
            if record is None:
                deferred.append(symbol)  # FLAT: decided in pass 2, which writes its event
                continue
            ledger.mark(symbol, bar_close[symbol])
            _monitor_held_symbol(c, symbol, record, c.current_prices[symbol], event)
            continue
        except Exception as exc:  # noqa: BLE001 — one symbol failing must not kill the whole cycle
            logger.exception("cycle failed for %s", symbol)
            event["error"] = str(exc)
        c.record(event)

    # ---- pass 2: new entries, in sorted(symbol) order, after every monitor check ----
    for symbol in sorted(deferred):
        event = {"symbol": symbol, "mode": cfg.mode, "timestamp": now_iso()}
        try:
            closed_at = bar_close[symbol]
            event["bar_close"] = closed_at.isoformat()
            if not ledger.is_new(symbol, closed_at):
                event["action"] = "none"
                event["note"] = (
                    f"bar closing {closed_at.isoformat()} already decided for {symbol}; "
                    f"next entry decision on the next closed bar"
                )
                c.record(event)
                continue
            ledger.mark(symbol, closed_at)  # decided from here on, whatever happens below (P1)

            if settings.max_entry_delay_minutes is not None:
                delay_minutes = (now_ms - closed_at.value // 1_000_000) / 60_000
                if delay_minutes > settings.max_entry_delay_minutes:
                    # P4: an entry this long after the close is outside the evidence
                    # (the backtest enters at the close / next open). Exits are never
                    # gated -- pass 1 already ran them.
                    event["action"] = "none"
                    event["note"] = "stale bar: entries skipped"
                    event["entry_delay_minutes"] = round(delay_minutes, 2)
                    c.record(event)
                    continue

            if _evaluate_new_entry(c, symbol, enriched_by_symbol[symbol], c.current_prices[symbol], event):
                continue
        except Exception as exc:  # noqa: BLE001 — one symbol failing must not kill the whole cycle
            logger.exception("cycle failed for %s", symbol)
            event["error"] = str(exc)
        c.record(event)


def main() -> None:
    parser = argparse.ArgumentParser(description="Hybrid rule-based + Claude-reviewed crypto trading bot")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument("--config", default=None, help="path to config.yaml (default: config/config.yaml)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    logger.info("starting bot: mode=%s exchange=%s symbols=%s", cfg.mode, cfg.exchange_id, cfg.symbols)

    exchange, risk_engine, portfolio, broker, store, audit = build_components(cfg)
    market_data = build_market_data(cfg, exchange)

    if args.once:
        run_cycle(cfg, exchange, risk_engine, portfolio, broker, store, audit, market_data)
        return

    interval = cfg.loop.get("poll_interval_seconds", 300)
    while True:
        run_cycle(cfg, exchange, risk_engine, portfolio, broker, store, audit, market_data)
        time.sleep(interval)


if __name__ == "__main__":
    main()
