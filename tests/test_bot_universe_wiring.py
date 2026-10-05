"""P1b wiring tests for src/bot.py (docs/system-redesign-2026-10.md §4.1, §8 P1b):
universe.refresh() + RiskEngine.set_symbol_whitelist() + the per-base one-plan
rule + held-symbol monitoring.

Three groups:
  1. Parity: on today's real config/config.yaml (16 static symbols, real scanner/
     signal/planner/risk/htf settings), the wired run_cycle makes exactly the same
     decisions, tick by tick, as the frozen pre-wiring run_cycle
     (tests/_legacy_run_cycle.py), over a multi-day synthetic replay that actually
     plans, fills, and closes trades.
  2. Held-symbol monitoring -- the safety-critical part: a plan/position on a
     symbol NOT in market.symbols is still fetched, priced, and has its
     fill/stop-loss/take-profit checked every cycle, even if universe.refresh()
     fails; and it's never whitelisted for a NEW plan.
  3. The per-base one-plan rule.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from src import htf_filter
from src.bot import build_components, run_cycle
from src.config import Config, load_config
from tests._legacy_run_cycle import legacy_run_cycle

H_MS = 3_600_000
D_MS = 24 * H_MS
# Synthetic epoch for the replay: hourly bars start here; daily bars end here.
T0_MS = 400 * D_MS
N_HOURLY = 330
FIRST_TICK_BAR = 205  # >= market.history_bars (200) closed bars before the first tick
# Replay window for the parity test: by this bar the fixture has already produced
# plans with immediate fills, stop-losses, a take-profit, htf vetoes and risk-engine
# rejections (checked at the end of the test), at about a third of the runtime of
# replaying every bar.
LAST_TICK_BAR = 250


# --- shared fakes ------------------------------------------------------------


class FakeCcxt:
    """Just what ExchangeClient.fetch_ohlcv_df needs (fetch_ohlcv, parse_timeframe,
    milliseconds), serving per-symbol 1h and 1d rows. Like a real exchange it only
    reveals bars that have opened by `now_ms`; ExchangeClient drops the still-
    forming one. A symbol with no rows raises, like ccxt's BadSymbol."""

    def __init__(self, hourly: dict[str, list], daily: dict[str, list], now_ms: int = 0) -> None:
        self.hourly = hourly
        self.daily = daily
        self.now_ms = now_ms
        self.calls: list[tuple[str, str]] = []
        self.failing: set[str] = set()

    def fetch_ohlcv(self, symbol, timeframe=None, limit=None):
        self.calls.append((symbol, timeframe))
        if symbol in self.failing:
            raise RuntimeError(f"simulated fetch failure for {symbol}")
        book = self.hourly if timeframe == "1h" else self.daily
        if symbol not in book:
            raise RuntimeError(f"BadSymbol: {symbol}")
        visible = [r for r in book[symbol] if r[0] <= self.now_ms]
        return visible[-limit:] if limit else visible

    def parse_timeframe(self, timeframe):
        return {"1h": 3600, "1d": 86400}[timeframe]

    def milliseconds(self):
        return self.now_ms


class _Clock:
    """Drives RiskEngine's wall clock (cooldowns, new-entry window, daily-loss
    baseline) from the replay's own time, so both pipelines see identical,
    realistic timing instead of a few real milliseconds for the whole replay."""

    now = datetime.fromtimestamp(T0_MS / 1000, tz=timezone.utc)


class _FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: D401 -- mirrors datetime.now's signature
        return _Clock.now


@pytest.fixture(autouse=True)
def _isolate_global_state(monkeypatch):
    # htf_filter caches per (symbol, real date) at module level; give every test
    # (and so both halves of the parity test, which see identical daily data) a
    # clean cache. Freeze the risk engine's clock to the replay clock.
    monkeypatch.setattr(htf_filter, "_cache", {})
    monkeypatch.setattr("src.risk_engine.datetime", _FakeDatetime)
    _Clock.now = datetime.fromtimestamp(T0_MS / 1000, tz=timezone.utc)


def _tick_to(fake: FakeCcxt, bar_index: int) -> None:
    """Advance to just after hourly bar `bar_index` has closed."""
    fake.now_ms = T0_MS + (bar_index + 1) * H_MS + 60_000
    _Clock.now = datetime.fromtimestamp(fake.now_ms / 1000, tz=timezone.utc)


def _random_walk_hourly(seed: int, start_price: float) -> list[list[float]]:
    """Deterministic 1h OHLCV with alternating drift regimes and occasional volume
    spikes -- enough structure for breakouts/pullbacks/momentum to fire, and for
    later bars to hit entry zones, stops and targets."""
    rng = np.random.default_rng(seed)
    rows = []
    price = start_price
    for i in range(N_HOURLY):
        regime = (i // 40) % 3  # up, chop, down
        drift = {0: 0.004, 1: 0.0, 2: -0.003}[regime]
        ret = drift + rng.normal(0, 0.008)
        o = price
        c = max(o * (1 + ret), 1e-9)
        h = max(o, c) * (1 + abs(rng.normal(0, 0.003)))
        low = min(o, c) * (1 - abs(rng.normal(0, 0.003)))
        vol = 100.0 * (4.0 if rng.random() < 0.08 else 1.0) * (1 + abs(rng.normal(0, 0.2)))
        rows.append([T0_MS + i * H_MS, o, h, low, c, vol])
        price = c
    return rows


def _daily(start_price: float, uptrend: bool) -> list[list[float]]:
    """60 closed daily bars ending at T0 (+ a few more that close during the replay)."""
    rows = []
    price = start_price
    for i in range(-60, 15):
        c = price * (1.01 if uptrend else 0.99)
        rows.append([T0_MS + i * D_MS, price, max(price, c) * 1.005, min(price, c) * 0.995, c, 1000.0])
        price = c
    return rows


def _isolated_raw(raw: dict, logs_dir: Path) -> dict:
    raw = copy.deepcopy(raw)
    logs_dir.mkdir(parents=True, exist_ok=True)
    raw["mode"] = "paper"
    raw["advisor"] = {"enabled": False}
    raw["risk"]["kill_switch_file"] = str(logs_dir / "KILL_SWITCH")
    raw["logging"] = {
        "audit_log_path": str(logs_dir / "audit_log.jsonl"),
        "trade_log_path": str(logs_dir / "trades.csv"),
        "trade_plan_current_path": str(logs_dir / "trade_plans_current.json"),
        "trade_plan_history_path": str(logs_dir / "trade_plans_history.jsonl"),
    }
    return raw


def _components(raw: dict, fake: FakeCcxt):
    cfg = Config(raw=raw)
    exchange, risk_engine, portfolio, broker, store, audit = build_components(cfg)
    exchange.client = fake
    events: list[dict] = []
    audit.write = events.append  # capture in-memory
    return cfg, (exchange, risk_engine, portfolio, broker, store, audit), events


def _strip_wall_clock(event: dict) -> dict:
    """Same rule as P1a's parity test: the event's own `timestamp` and a fill's
    `execution.timestamp` are the only wall-clock (not replay-clock) fields."""
    stripped = json.loads(json.dumps(event, default=str))
    stripped.pop("timestamp", None)
    if "execution" in stripped:
        stripped["execution"].pop("timestamp", None)
    return stripped


# --- 1. parity on today's real config ----------------------------------------


def test_wired_run_cycle_matches_pre_wiring_run_cycle_on_todays_config(tmp_path):
    real_raw = load_config().raw
    symbols = list(real_raw["market"]["symbols"])
    assert len(symbols) == 16 and len({s.split("/")[0] for s in symbols}) == 16  # all-unique bases today

    hourly = {s: _random_walk_hourly(seed=i, start_price=10.0 * (i + 1)) for i, s in enumerate(symbols)}
    # Most symbols in a daily uptrend (htf filter passes); a few not, so the htf
    # veto path is exercised too.
    daily = {s: _daily(10.0 * (i + 1), uptrend=(i % 4 != 3)) for i, s in enumerate(symbols)}

    legacy_fake = FakeCcxt(hourly, daily)
    wired_fake = FakeCcxt(hourly, daily)
    legacy_cfg, legacy_parts, legacy_events = _components(_isolated_raw(real_raw, tmp_path / "legacy"), legacy_fake)
    wired_cfg, wired_parts, wired_events = _components(_isolated_raw(real_raw, tmp_path / "wired"), wired_fake)

    # Static mode: the universe-derived whitelist is exactly what the constructor
    # was given before this change.
    assert wired_parts[1].symbol_whitelist == set(symbols)

    # Run sequentially, each with a fresh htf_filter module cache -- interleaving
    # would let one pipeline warm the other's daily-trend cache and hide (or fake)
    # a difference in exchange traffic.
    for fake, cycle, cfg, parts in (
        (legacy_fake, legacy_run_cycle, legacy_cfg, legacy_parts),
        (wired_fake, run_cycle, wired_cfg, wired_parts),
    ):
        htf_filter._cache.clear()
        for bar in range(FIRST_TICK_BAR, LAST_TICK_BAR + 1):
            _tick_to(fake, bar)
            cycle(cfg, *parts)

    assert len(legacy_events) == len(wired_events) == 16 * (LAST_TICK_BAR + 1 - FIRST_TICK_BAR)
    for i, (before, after) in enumerate(zip(legacy_events, wired_events)):
        assert _strip_wall_clock(before) == _strip_wall_clock(after), f"event {i} ({before['symbol']}) diverged"

    # Same exchange traffic, call for call (no extra or missing fetches).
    assert legacy_fake.calls == wired_fake.calls
    # Same end state.
    assert legacy_parts[2].cash == pytest.approx(wired_parts[2].cash)
    assert set(legacy_parts[2].positions) == set(wired_parts[2].positions)
    assert set(legacy_parts[4].all_current()) == set(wired_parts[4].all_current())

    # Not vacuous: the replay really planned, filled, and closed trades, and hit
    # the htf veto and risk-engine rejections along the way.
    actions = [e.get("monitor_action", {}).get("action") for e in wired_events]
    assert sum(e.get("action") == "planned" for e in wired_events) >= 5
    assert any(e.get("action") == "planned" and "execution" in e for e in wired_events)  # immediate fills
    assert {"STOP_LOSS", "TAKE_PROFIT"} <= set(actions)
    assert any(e.get("daily_trend_confirmed") is False for e in wired_events)
    assert any(e.get("risk_decision", {}).get("allowed") is False for e in wired_events)


def test_wired_fetch_failure_on_held_pinned_symbol_matches_legacy_plus_flag(tmp_path):
    # A held symbol that's also in market.symbols failing to fetch: same decision
    # as before ("market data unavailable"), with one additive audit key so the
    # §4.6 "held symbol not priced" ops check is countable from the audit log.
    raw = _isolated_raw(_small_raw(["BTC/USDT", "ETH/USDT"]), tmp_path / "wired")
    _seed_open_position(tmp_path / "wired", "BTC/USDT", fill_price=100.0, stop=90.0, target=130.0)
    fake = FakeCcxt({"ETH/USDT": _flat_hourly(100.0)}, {})
    cfg, parts, events = _components(raw, fake)
    fake.failing.add("BTC/USDT")
    _tick_to(fake, 210)
    run_cycle(cfg, *parts)

    btc = next(e for e in events if e["symbol"] == "BTC/USDT")
    assert btc["action"] == "none"
    assert btc["error"] == "market data unavailable this cycle"
    assert btc["held_symbol_unpriced"] is True
    # A non-held symbol's failure doesn't get the flag.
    fake.failing = {"ETH/USDT"}
    events.clear()
    _tick_to(fake, 211)
    run_cycle(cfg, *parts)
    eth = next(e for e in events if e["symbol"] == "ETH/USDT")
    assert "held_symbol_unpriced" not in eth


# --- helpers for the targeted tests below --------------------------------------


def _small_raw(symbols: list[str]) -> dict:
    return {
        "mode": "paper",
        "exchange": {"id": "kraken", "testnet": False},
        "market": {"symbols": list(symbols), "timeframe": "1h", "history_bars": 50},
        "indicators": {"fast_ema": 12, "slow_ema": 26, "rsi_period": 14},
        "htf_filter": {"enabled": False},
        "risk": {
            "starting_cash_usd": 10000,
            "max_position_pct": 0.1,
            "max_total_exposure_pct": 0.4,
            "max_loss_per_trade_pct": 0.01,
            "max_daily_loss_pct": 0.05,
            "max_volatility_pct": 0.08,
            "min_risk_reward": 1.0,
            "cooldown_minutes": 60,
        },
    }


def _flat_hourly(level: float, last_close: float | None = None, n: int = 260) -> list[list[float]]:
    """Mild oscillation around `level` (no setups fire); the last bar closes at
    `last_close` if given -- the price the monitor will see once it has closed."""
    rows = []
    for i in range(n):
        c = level + (0.05 if i % 2 == 0 else -0.05)
        o = level
        rows.append([T0_MS + i * H_MS, o, max(o, c) + 0.02, min(o, c) - 0.02, c, 100.0])
    if last_close is not None:
        o = rows[-1][4]
        rows[-1] = [rows[-1][0], o, max(o, last_close) + 0.01, min(o, last_close) - 0.01, last_close, 100.0]
    return rows


def _breakout_hourly(n_flat: int = 250) -> list[list[float]]:
    """Flat oscillation, then a high-volume breakout bar -- the same shape P1a's
    parity fixture uses -- so a bullish BREAKOUT signal fires on its close."""
    rows = []
    price = 100.0
    for i in range(n_flat):
        wiggle = 0.4 if i % 2 == 0 else -0.4
        o, c = price, price + wiggle
        rows.append([T0_MS + i * H_MS, o, max(o, c) + 0.3, min(o, c) - 0.3, c, 100.0])
        price = c
    resistance = max(r[2] for r in rows[-20:])
    o, c = price, resistance + 5.0
    rows.append([T0_MS + n_flat * H_MS, o, c + 0.5, o - 0.2, c, 500.0])
    return rows


def _write_record(logs_dir: Path, symbol: str, record: dict) -> None:
    path = logs_dir / "trade_plans_current.json"
    current = json.loads(path.read_text()) if path.exists() else {}
    current[symbol] = record
    path.write_text(json.dumps(current))


def _seed_open_position(logs_dir: Path, symbol: str, fill_price: float, stop: float, target: float) -> None:
    """An OPEN plan + its buy in the trade log, exactly what build_components
    reconstructs a position from after a restart."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    amount = 1.0
    trades = logs_dir / "trades.csv"
    if not trades.exists():
        trades.write_text("timestamp,mode,symbol,side,price,base_amount,quote_amount\n")
    with open(trades, "a") as f:
        f.write(f"t0,paper,{symbol},buy,{fill_price},{amount},{fill_price * amount}\n")
    _write_record(logs_dir, symbol, {
        "symbol": symbol, "status": "OPEN",
        "plan": {"entry_zone": [fill_price * 0.99, fill_price * 1.01], "stop_loss": stop,
                 "take_profit": target, "invalidation": stop * 0.98, "approved_quote_amount": fill_price},
        "claude_verdict": None, "created_at": "2000-01-01T00:00:00+00:00",
        "filled_at": "2000-01-01T00:00:00+00:00", "fill_price": fill_price, "fill_amount": amount,
        "closed_at": None, "close_reason": None, "close_price": None,
    })


def _seed_pending_plan(logs_dir: Path, symbol: str, entry_zone: tuple[float, float], invalidation: float) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    _write_record(logs_dir, symbol, {
        "symbol": symbol, "status": "PENDING",
        "plan": {"entry_zone": list(entry_zone), "stop_loss": invalidation * 1.01, "take_profit": entry_zone[1] * 1.3,
                 "invalidation": invalidation, "approved_quote_amount": 500.0},
        "claude_verdict": None, "created_at": "2000-01-01T00:00:00+00:00",
        "filled_at": None, "fill_price": None, "fill_amount": None,
        "closed_at": None, "close_reason": None, "close_price": None,
    })


def _events_for(events: list[dict], symbol: str) -> list[dict]:
    return [e for e in events if e["symbol"] == symbol]


# --- 2. held-symbol monitoring (the safety-critical part) ------------------------


@pytest.mark.parametrize(
    "last_close, expected_action, close_reason",
    [(85.0, "STOP_LOSS", "stop_loss"), (135.0, "TAKE_PROFIT", "take_profit")],
)
def test_position_on_symbol_outside_market_symbols_still_hits_stop_and_target(
    tmp_path, last_close, expected_action, close_reason
):
    # BTC/USDT has an OPEN position but is NOT in market.symbols (e.g. a future
    # universe refresh dropped it, or a human edited the list). Before P1b,
    # run_cycle only walked market.symbols, so this position was never priced and
    # its stop-loss/take-profit could never fire.
    logs = tmp_path / "logs"
    _seed_open_position(logs, "BTC/USDT", fill_price=100.0, stop=90.0, target=130.0)
    raw = _isolated_raw(_small_raw(["ETH/USDT"]), logs)
    fake = FakeCcxt({"ETH/USDT": _flat_hourly(50.0), "BTC/USDT": _flat_hourly(100.0, last_close=last_close)}, {})
    cfg, parts, events = _components(raw, fake)
    _exchange, risk_engine, portfolio, _broker, store, _audit = parts
    assert "BTC/USDT" in portfolio.positions
    assert "BTC/USDT" not in risk_engine.symbol_whitelist

    _tick_to(fake, 259)
    run_cycle(cfg, *parts)

    assert ("BTC/USDT", "1h") in fake.calls  # it was fetched/priced
    btc = _events_for(events, "BTC/USDT")
    assert len(btc) == 1
    assert btc[0]["monitor_action"]["action"] == expected_action
    assert btc[0]["execution"]["price"] == pytest.approx(last_close)
    assert store.get("BTC/USDT") is None
    assert "BTC/USDT" not in portfolio.positions
    history = [json.loads(line) for line in (logs / "trade_plans_history.jsonl").read_text().splitlines()]
    assert history[-1]["symbol"] == "BTC/USDT" and history[-1]["close_reason"] == close_reason
    # And now that it's closed, it's no longer walked at all next cycle.
    events.clear()
    _tick_to(fake, 259)
    run_cycle(cfg, *parts)
    assert _events_for(events, "BTC/USDT") == []


def test_legacy_run_cycle_never_monitored_a_held_symbol_outside_market_symbols(tmp_path):
    # Documents the gap the wiring closes, against the frozen pre-P1b code.
    logs = tmp_path / "logs"
    _seed_open_position(logs, "BTC/USDT", fill_price=100.0, stop=90.0, target=130.0)
    raw = _isolated_raw(_small_raw(["ETH/USDT"]), logs)
    fake = FakeCcxt({"ETH/USDT": _flat_hourly(50.0), "BTC/USDT": _flat_hourly(100.0, last_close=85.0)}, {})
    cfg, parts, events = _components(raw, fake)
    _tick_to(fake, 259)
    legacy_run_cycle(cfg, *parts)
    assert _events_for(events, "BTC/USDT") == []
    assert ("BTC/USDT", "1h") not in fake.calls
    assert parts[4].get("BTC/USDT").status == "OPEN"  # stop blown through, never closed


def test_held_symbol_outside_universe_is_priced_into_equity_and_exposure(tmp_path, monkeypatch):
    # Stale-price risk: without a fetch, Portfolio.equity and run_cycle's
    # other_positions_value value the position at entry_price (100). It must be
    # valued at its real current price (120) when a new trade is risk-checked.
    logs = tmp_path / "logs"
    _seed_open_position(logs, "BTC/USDT", fill_price=100.0, stop=90.0, target=130.0)
    raw = _isolated_raw(_small_raw(["ETH/USDT"]), logs)
    fake = FakeCcxt({"ETH/USDT": _breakout_hourly(), "BTC/USDT": _flat_hourly(120.0)}, {})
    cfg, parts, events = _components(raw, fake)
    risk_engine = parts[1]

    seen = {}
    original = risk_engine.check_new_trade

    def spy(symbol, side, portfolio_equity, cash_available, **kwargs):
        seen.update(symbol=symbol, equity=portfolio_equity, cash=cash_available, other=kwargs["open_positions_value"])
        return original(symbol, side, portfolio_equity, cash_available, **kwargs)

    monkeypatch.setattr(risk_engine, "check_new_trade", spy)
    _tick_to(fake, 250)
    run_cycle(cfg, *parts)

    assert seen["symbol"] == "ETH/USDT"
    btc_price = 120.0 + (0.05 if 250 % 2 == 0 else -0.05)
    assert seen["other"] == pytest.approx(1.0 * btc_price)
    assert seen["equity"] == pytest.approx(seen["cash"] + 1.0 * btc_price)


def test_pending_plan_on_symbol_off_the_whitelist_still_fills(tmp_path):
    # Deliberate P1b decision: the whitelist only gates ORIGINATING a plan. A
    # PENDING plan already approved on a symbol that has since left the
    # tradeable universe still fills when price enters its entry zone.
    logs = tmp_path / "logs"
    _seed_pending_plan(logs, "BTC/USDT", entry_zone=(99.0, 101.0), invalidation=90.0)
    raw = _isolated_raw(_small_raw(["ETH/USDT"]), logs)
    fake = FakeCcxt({"ETH/USDT": _flat_hourly(50.0), "BTC/USDT": _flat_hourly(100.0)}, {})
    cfg, parts, events = _components(raw, fake)
    _tick_to(fake, 259)
    run_cycle(cfg, *parts)

    btc = _events_for(events, "BTC/USDT")
    assert btc[0]["monitor_action"]["action"] == "FILL"
    assert parts[4].get("BTC/USDT").status == "OPEN"
    assert "BTC/USDT" in parts[2].positions


def test_held_symbol_is_never_whitelisted_for_new_plans(tmp_path):
    logs = tmp_path / "logs"
    _seed_open_position(logs, "BTC/USDT", fill_price=100.0, stop=90.0, target=130.0)
    raw = _isolated_raw(_small_raw(["ETH/USDT"]), logs)
    fake = FakeCcxt({"ETH/USDT": _flat_hourly(50.0), "BTC/USDT": _flat_hourly(100.0)}, {})
    cfg, parts, events = _components(raw, fake)
    _tick_to(fake, 259)
    run_cycle(cfg, *parts)
    assert parts[1].symbol_whitelist == {"ETH/USDT"}
    assert not parts[1].check_new_trade("BTC/USDT", "buy", portfolio_equity=10000, cash_available=9000).allowed


def test_monitor_only_symbol_without_a_plan_is_priced_but_not_scanned(tmp_path):
    # A position with no plan record (shouldn't happen -- positions are a subset of
    # OPEN records -- but if it ever did) on a symbol outside the universe: it's
    # still fetched so it's priced into equity, but never scanned for a new plan.
    raw = _isolated_raw(_small_raw(["ETH/USDT"]), tmp_path / "logs")
    fake = FakeCcxt({"ETH/USDT": _flat_hourly(50.0), "BTC/USDT": _breakout_hourly()}, {})
    cfg, parts, events = _components(raw, fake)
    from src.portfolio import Position

    parts[2].positions["BTC/USDT"] = Position(symbol="BTC/USDT", amount=1.0, entry_price=100.0)
    _tick_to(fake, 250)
    run_cycle(cfg, *parts)

    btc = _events_for(events, "BTC/USDT")
    assert len(btc) == 1
    assert btc[0]["action"] == "none"
    assert "monitor-only" in btc[0]["note"]
    assert "candidates" not in btc[0]
    assert parts[4].get("BTC/USDT") is None


@pytest.mark.parametrize("failure", ["refresh", "whitelist"])
def test_held_symbol_still_monitored_when_universe_refresh_fails(tmp_path, monkeypatch, failure):
    logs = tmp_path / "logs"
    _seed_open_position(logs, "BTC/USDT", fill_price=100.0, stop=90.0, target=130.0)
    raw = _isolated_raw(_small_raw(["ETH/USDT"]), logs)
    fake = FakeCcxt({"ETH/USDT": _flat_hourly(50.0), "BTC/USDT": _flat_hourly(100.0, last_close=85.0)}, {})
    cfg, parts, events = _components(raw, fake)
    risk_engine = parts[1]
    whitelist_before = set(risk_engine.symbol_whitelist)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated universe failure")

    if failure == "refresh":
        monkeypatch.setattr("src.bot.universe.refresh", boom)
    else:
        monkeypatch.setattr(risk_engine, "set_symbol_whitelist", boom)

    _tick_to(fake, 259)
    run_cycle(cfg, *parts)

    assert _events_for(events, "BTC/USDT")[0]["monitor_action"]["action"] == "STOP_LOSS"
    assert parts[4].get("BTC/USDT") is None
    assert _events_for(events, "ETH/USDT")  # the pinned list is still walked
    assert set(risk_engine.symbol_whitelist) == whitelist_before  # previous whitelist kept


def test_startup_refuses_an_unimplemented_universe_mode(tmp_path):
    raw = _isolated_raw(_small_raw(["ETH/USDT"]), tmp_path / "logs")
    raw["universe"] = {"mode": "discover"}
    with pytest.raises(NotImplementedError):
        build_components(Config(raw=raw))


def test_startup_is_not_blocked_by_a_malformed_held_symbol(tmp_path):
    # Held symbols are deliberately not passed to the startup refresh, so a
    # corrupted trade_plans_current.json key can't stop the bot from starting
    # (and monitoring everything else).
    logs = tmp_path / "logs"
    _seed_open_position(logs, "BTC/USDT", fill_price=100.0, stop=90.0, target=130.0)
    _seed_pending_plan(logs, "NOSLASH", entry_zone=(1.0, 2.0), invalidation=0.5)
    raw = _isolated_raw(_small_raw(["ETH/USDT"]), logs)
    fake = FakeCcxt({"ETH/USDT": _flat_hourly(50.0), "BTC/USDT": _flat_hourly(100.0, last_close=85.0)}, {})
    cfg, parts, events = _components(raw, fake)
    _tick_to(fake, 259)
    run_cycle(cfg, *parts)  # per-cycle refresh raises on NOSLASH -> fallback path
    assert _events_for(events, "BTC/USDT")[0]["monitor_action"]["action"] == "STOP_LOSS"
    noslash = _events_for(events, "NOSLASH")[0]
    assert noslash["error"] == "market data unavailable this cycle"
    assert noslash["held_symbol_unpriced"] is True


# --- 3. per-base one-plan rule ---------------------------------------------------


def test_active_plan_on_one_quote_blocks_a_new_plan_on_another_quote_of_same_base(tmp_path):
    logs = tmp_path / "logs"
    _seed_open_position(logs, "BTC/USDT", fill_price=100.0, stop=90.0, target=130.0)
    raw = _isolated_raw(_small_raw(["BTC/USD", "BTC/USDT", "ETH/USDT"]), logs)
    fake = FakeCcxt(
        {"BTC/USD": _breakout_hourly(), "BTC/USDT": _flat_hourly(100.0), "ETH/USDT": _breakout_hourly()}, {}
    )
    cfg, parts, events = _components(raw, fake)
    _tick_to(fake, 250)
    run_cycle(cfg, *parts)

    btc_usd = _events_for(events, "BTC/USD")[0]
    assert btc_usd["action"] == "none"
    assert "BTC/USDT" in btc_usd["note"] and "base asset BTC" in btc_usd["note"]
    assert "candidates" not in btc_usd
    assert parts[4].get("BTC/USD") is None
    # A different base on the very same breakout data is not blocked.
    assert _events_for(events, "ETH/USDT")[0]["action"] == "planned"


def test_per_base_rule_applies_within_a_single_cycle(tmp_path):
    raw = _isolated_raw(_small_raw(["BTC/USD", "BTC/USDT"]), tmp_path / "logs")
    fake = FakeCcxt({"BTC/USD": _breakout_hourly(), "BTC/USDT": _breakout_hourly()}, {})
    cfg, parts, events = _components(raw, fake)
    _tick_to(fake, 250)
    run_cycle(cfg, *parts)

    assert _events_for(events, "BTC/USD")[0]["action"] == "planned"
    second = _events_for(events, "BTC/USDT")[0]
    assert second["action"] == "none" and "BTC/USD" in second["note"]
    assert set(parts[4].all_current()) == {"BTC/USD"}


def test_per_base_rule_ignores_bases_that_merely_share_a_prefix(tmp_path):
    # "BTC" vs "BTCB" / "WBTC": different base assets, never blocked by each other.
    logs = tmp_path / "logs"
    _seed_open_position(logs, "WBTC/USD", fill_price=100.0, stop=90.0, target=130.0)
    raw = _isolated_raw(_small_raw(["WBTC/USD", "BTC/USD"]), logs)
    fake = FakeCcxt({"WBTC/USD": _flat_hourly(100.0), "BTC/USD": _breakout_hourly()}, {})
    cfg, parts, events = _components(raw, fake)
    _tick_to(fake, 250)
    run_cycle(cfg, *parts)
    assert _events_for(events, "BTC/USD")[0]["action"] == "planned"


def test_closed_plan_frees_the_base_on_the_next_symbol_in_the_same_cycle(tmp_path):
    # BTC/USDT's stop fires earlier in the cycle; BTC/USD (later in config order)
    # is then free to plan in the same cycle. Per-symbol cooldowns don't carry
    # across quotes (design doc §4.5 accepts this).
    logs = tmp_path / "logs"
    _seed_open_position(logs, "BTC/USDT", fill_price=100.0, stop=90.0, target=130.0)
    raw = _isolated_raw(_small_raw(["BTC/USDT", "BTC/USD"]), logs)
    breakout = _breakout_hourly()
    fake = FakeCcxt({"BTC/USDT": _flat_hourly(100.0, last_close=85.0, n=251), "BTC/USD": breakout}, {})
    cfg, parts, events = _components(raw, fake)
    _tick_to(fake, 250)
    run_cycle(cfg, *parts)
    assert _events_for(events, "BTC/USDT")[0]["monitor_action"]["action"] == "STOP_LOSS"
    assert _events_for(events, "BTC/USD")[0]["action"] == "planned"
