"""Tests for src/market_data.py's MarketDataCache.

Two groups:
  1. Unit tests on MarketDataCache's own mechanics (due/not-due, priority
     order, call budget, staleness, snapshot shape, stale-serving on a
     failed refetch) -- these never touch src/bot.py.
  2. The P1a parity test: runs the real src.bot.run_cycle twice against the
     same deterministic OHLCV fixture, once through a cache configured to
     behave exactly like today (bar_close_aligned=False -- refetches every
     call, matching ExchangeClient.fetch_ohlcv_df being called directly every
     cycle) and once through the real default (bar_close_aligned=True --
     only refetches once a bar has actually closed). Asserts the sequence of
     audit events -- candidates found, signal, plan, risk decision, action --
     is identical between the two, while the aligned cache makes strictly
     fewer exchange calls across ticks that fall inside the same bar.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.bot import build_components, run_cycle
from src.config import Config
from src.exchange import ExchangeClient
from src.market_data import MarketDataCache

TF_SECONDS = 3600
TF_MS = TF_SECONDS * 1000


# --- 1. MarketDataCache unit tests -----------------------------------------


class FakeCcxtClient:
    """Minimal fake exposing just what MarketDataCache/ExchangeClient need:
    fetch_ohlcv, parse_timeframe, milliseconds (same trio tests/test_exchange.py
    fakes). `rows` is the full, fixed universe of candles; `fetch_ohlcv` only
    ever reveals rows that have at least opened by `now_ms`, mirroring a real
    exchange -- ExchangeClient.fetch_ohlcv_df is what actually drops whichever
    trailing row hasn't CLOSED yet.
    """

    def __init__(self, rows: list[list[float]], now_ms: int) -> None:
        self.rows = rows
        self.now_ms = now_ms
        self.fetch_calls = 0
        self.fail_next = False

    def fetch_ohlcv(self, symbol, timeframe=None, limit=None):
        self.fetch_calls += 1
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated exchange error")
        visible = [r for r in self.rows if r[0] <= self.now_ms]
        n = min(limit, len(visible)) if limit is not None else len(visible)
        return visible[-n:]

    def parse_timeframe(self, timeframe):
        return TF_SECONDS

    def milliseconds(self):
        return self.now_ms


def _flat_rows(n: int, start_price: float = 100.0) -> list[list[float]]:
    rows = []
    price = start_price
    for i in range(n):
        wiggle = 0.4 if i % 2 == 0 else -0.4
        o, c = price, price + wiggle
        h, l = max(o, c) + 0.3, min(o, c) - 0.3
        rows.append([i * TF_MS, o, h, l, c, 100.0])
        price = c
    return rows


def _make_cache(rows, now_ms, **kwargs) -> tuple[MarketDataCache, FakeCcxtClient]:
    fake = FakeCcxtClient(rows, now_ms)
    exchange = ExchangeClient(exchange_id="kraken", testnet=False)
    exchange.client = fake
    cache = MarketDataCache(exchange, "1h", history_bars=50, **kwargs)
    return cache, fake


def test_first_refresh_always_fetches():
    rows = _flat_rows(25)
    cache, fake = _make_cache(rows, now_ms=24 * TF_MS)
    result = cache.refresh(["BTC/USDT"])
    assert fake.fetch_calls == 1
    assert "BTC/USDT" in result
    assert cache.last_refresh_stats["fetched"] == ["BTC/USDT"]


def test_bar_close_aligned_skips_refetch_within_same_bar():
    rows = _flat_rows(25)
    cache, fake = _make_cache(rows, now_ms=24 * TF_MS)
    df1 = cache.refresh(["BTC/USDT"])["BTC/USDT"]
    assert fake.fetch_calls == 1

    fake.now_ms += 5 * 60_000  # 5 minutes later, well inside the same closed bar's next-bar window
    df2 = cache.refresh(["BTC/USDT"])["BTC/USDT"]
    assert fake.fetch_calls == 1  # no new exchange call
    assert cache.last_refresh_stats["served_from_cache"] == ["BTC/USDT"]
    pd.testing.assert_frame_equal(df1, df2)


def test_bar_close_aligned_refetches_once_a_new_bar_closes():
    rows = _flat_rows(25)
    cache, fake = _make_cache(rows, now_ms=24 * TF_MS)
    cache.refresh(["BTC/USDT"])
    assert fake.fetch_calls == 1

    fake.now_ms = 26 * TF_MS  # a full extra bar period later -> a new bar has closed
    cache.refresh(["BTC/USDT"])
    assert fake.fetch_calls == 2
    assert cache.last_refresh_stats["fetched"] == ["BTC/USDT"]


def test_not_bar_close_aligned_refetches_every_call():
    # bar_close_aligned=False reproduces today's "fetch every cycle" behavior
    # through the same code path -- this is the "before" half of the parity
    # test in the second half of this file.
    rows = _flat_rows(25)
    cache, fake = _make_cache(rows, now_ms=24 * TF_MS, bar_close_aligned=False)
    cache.refresh(["BTC/USDT"])
    fake.now_ms += 60_000
    cache.refresh(["BTC/USDT"])
    fake.now_ms += 60_000
    cache.refresh(["BTC/USDT"])
    assert fake.fetch_calls == 3


def test_priority_order_fetches_active_symbols_first():
    rows_a = _flat_rows(25, start_price=100.0)
    rows_b = _flat_rows(25, start_price=200.0)
    now_ms = 24 * TF_MS
    fake_a = FakeCcxtClient(rows_a, now_ms)
    fake_b = FakeCcxtClient(rows_b, now_ms)

    class MultiFake:
        def __init__(self):
            self.order: list[str] = []

        def fetch_ohlcv(self, symbol, timeframe=None, limit=None):
            self.order.append(symbol)
            fake = fake_a if symbol == "BTC/USDT" else fake_b
            return fake.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

        def parse_timeframe(self, timeframe):
            return TF_SECONDS

        def milliseconds(self):
            return now_ms

    multi = MultiFake()
    exchange = ExchangeClient(exchange_id="kraken", testnet=False)
    exchange.client = multi
    cache = MarketDataCache(exchange, "1h", history_bars=50)

    # BTC is second in `symbols` config order, but has an active plan -> fetched first.
    cache.refresh(["ETH/USDT", "BTC/USDT"], active_symbols={"BTC/USDT"})
    assert multi.order == ["BTC/USDT", "ETH/USDT"]


def test_call_budget_caps_fetches_and_serves_stale_for_the_rest():
    rows_a = _flat_rows(25, start_price=100.0)
    now_ms = 24 * TF_MS

    class MultiFake:
        def __init__(self):
            self.calls = 0

        def fetch_ohlcv(self, symbol, timeframe=None, limit=None):
            self.calls += 1
            visible = [r for r in rows_a if r[0] <= now_ms]
            n = min(limit, len(visible)) if limit is not None else len(visible)
            return visible[-n:]

        def parse_timeframe(self, timeframe):
            return TF_SECONDS

        def milliseconds(self):
            return now_ms

    multi = MultiFake()
    exchange = ExchangeClient(exchange_id="kraken", testnet=False)
    exchange.client = multi
    cache = MarketDataCache(exchange, "1h", history_bars=50, call_budget=1)

    result = cache.refresh(["BTC/USDT", "ETH/USDT", "SOL/USDT"])
    assert multi.calls == 1
    assert cache.last_refresh_stats["calls_made"] == 1
    # Only the first symbol got fetched this pass; the rest have no prior
    # cache entry at all yet, so they're simply absent (not stale-served,
    # since there's nothing to serve) -- matches today's "no data this cycle"
    # behavior for a symbol that was never successfully fetched.
    assert set(result) == {"BTC/USDT"}
    assert cache.last_refresh_stats["budget_skipped"] == ["ETH/USDT", "SOL/USDT"]


def test_failed_refetch_serves_stale_cache_and_is_flagged_stale():
    rows = _flat_rows(25)
    cache, fake = _make_cache(rows, now_ms=24 * TF_MS)
    cache.refresh(["BTC/USDT"])
    assert cache.is_stale("BTC/USDT", max_age_seconds=10_000) is False

    fake.now_ms = 26 * TF_MS  # due for refetch...
    fake.fail_next = True  # ...but the exchange call fails
    result = cache.refresh(["BTC/USDT"])
    assert "BTC/USDT" in result  # served from the stale cache rather than dropped
    assert cache.last_refresh_stats["failed"] == ["BTC/USDT"]
    assert cache.last_refresh_stats["fetched"] == []


def test_is_stale_true_for_unknown_symbol():
    rows = _flat_rows(25)
    cache, _fake = _make_cache(rows, now_ms=24 * TF_MS)
    assert cache.is_stale("BTC/USDT", max_age_seconds=1) is True


def test_write_snapshot_shape(tmp_path):
    rows = _flat_rows(25)
    cache, _fake = _make_cache(rows, now_ms=24 * TF_MS)
    cache.refresh(["BTC/USDT"])

    out = tmp_path / "market_snapshot.json"
    cache.write_snapshot(out)

    import json

    payload = json.loads(out.read_text())
    assert payload["timeframe"] == "1h"
    assert "generated_at" in payload
    symbol_entry = payload["symbols"]["BTC/USDT"]
    assert isinstance(symbol_entry["price"], float)
    assert "updated_at" in symbol_entry
    assert len(symbol_entry["candles"]) > 0
    candle = symbol_entry["candles"][0]
    assert set(candle) == {"time", "open", "high", "low", "close"}


# --- 2. Parity test: cache-mediated run_cycle vs today's direct-fetch run_cycle ---


def _breakout_rows(n_flat: int = 30) -> list[list[float]]:
    """n_flat bars of mild oscillation around 100 (enough history for every
    scanner detector's lookback), then one breakout bar (price clears recent
    resistance on a volume spike), then one plain continuation bar. Fully
    deterministic -- reused, unmodified, by both pipelines below."""
    rows = _flat_rows(n_flat)
    price = rows[-1][4]

    resistance = max(r[2] for r in rows[-20:])
    o = price
    c = resistance + 5.0
    h, l = c + 0.5, o - 0.2
    rows.append([n_flat * TF_MS, o, h, l, c, 500.0])  # breakout: big volume spike

    o2 = c
    c2 = c + 0.2
    h2, l2 = max(o2, c2) + 0.3, min(o2, c2) - 0.3
    rows.append([(n_flat + 1) * TF_MS, o2, h2, l2, c2, 120.0])  # plain continuation bar
    return rows


def _raw_config(logs_dir: Path) -> dict:
    return {
        "mode": "paper",
        "exchange": {"id": "kraken", "testnet": False},
        "market": {"symbols": ["BTC/USDT"], "timeframe": "1h", "history_bars": 50},
        "indicators": {"fast_ema": 12, "slow_ema": 26, "rsi_period": 14},
        # Disabled so this test doesn't also need to mock a second,
        # independent daily-OHLCV fetch path (src/htf_filter.py) -- out of
        # scope for this change and untouched by it either way.
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
            "kill_switch_file": str(logs_dir / "KILL_SWITCH"),
        },
        "logging": {
            "audit_log_path": str(logs_dir / "audit_log.jsonl"),
            "trade_log_path": str(logs_dir / "trades.csv"),
            "trade_plan_current_path": str(logs_dir / "trade_plans_current.json"),
            "trade_plan_history_path": str(logs_dir / "trade_plans_history.jsonl"),
            "market_snapshot_path": str(logs_dir / "market_snapshot.json"),
        },
    }


def _strip_wall_clock(event: dict) -> dict:
    """Deep-copies an audit event with wall-clock-only fields removed, so two
    runs executed a few milliseconds apart in real time can still be compared
    for decision equality. `src/bot.py`'s TradePlan/Signal/Candidate/
    RiskDecision dataclasses carry no timestamps of their own (verified by
    reading src/trade_planner.py, src/signal_engine.py, src/scanner.py,
    src/risk_engine.py) -- the event dict's own "timestamp"
    (src.portfolio.now_iso(), stamped once per event in run_cycle's
    _record() closure) and a fill's "execution"/"monitor_action" timestamp
    (src.broker_paper.FillResult.timestamp) are the only wall-clock fields."""
    stripped = dict(event)
    stripped.pop("timestamp", None)
    if "execution" in stripped:
        stripped["execution"] = {k: v for k, v in stripped["execution"].items() if k != "timestamp"}
    return stripped


def _run_pipeline(rows: list[list[float]], now_ms: int, logs_dir: Path, bar_close_aligned: bool):
    cfg = Config(raw=_raw_config(logs_dir))
    exchange, risk_engine, portfolio, broker, store, audit = build_components(cfg)
    fake = FakeCcxtClient(rows, now_ms)
    exchange.client = fake
    market_data = MarketDataCache(exchange, cfg.timeframe, cfg.history_bars, bar_close_aligned=bar_close_aligned)

    captured: list[dict] = []
    audit.write = captured.append  # capture in-memory instead of round-tripping through JSON

    def run_tick(tick_now_ms: int) -> None:
        fake.now_ms = tick_now_ms
        run_cycle(cfg, exchange, risk_engine, portfolio, broker, store, audit, market_data)

    return run_tick, captured, fake


def test_cache_mediated_run_cycle_matches_direct_fetch_run_cycle(tmp_path):
    rows = _breakout_rows(n_flat=30)

    # Four ticks: three fall inside the breakout bar's closed-bar window
    # (nothing about the latest CLOSED data changes between them -- this
    # mirrors the real 1h-timeframe/300s-poll situation the redesign doc
    # measured), the fourth is after the next bar has actually closed.
    breakout_open_ms = 30 * TF_MS
    t1 = breakout_open_ms + TF_MS + 100_000   # breakout bar just closed
    t2 = t1 + 5 * 60_000                      # +5 min, same closed bar
    t3 = t1 + 10 * 60_000                     # +10 min, still the same closed bar
    t4 = breakout_open_ms + 2 * TF_MS + 100_000  # the NEXT bar has now closed too
    ticks = [t1, t2, t3, t4]

    legacy_tick, legacy_events, legacy_fake = _run_pipeline(
        rows, t1, tmp_path / "legacy", bar_close_aligned=False
    )
    aligned_tick, aligned_events, aligned_fake = _run_pipeline(
        rows, t1, tmp_path / "aligned", bar_close_aligned=True
    )

    for tick_ms in ticks:
        legacy_tick(tick_ms)
        aligned_tick(tick_ms)

    assert len(legacy_events) == len(aligned_events) == len(ticks)
    for i, (legacy_event, aligned_event) in enumerate(zip(legacy_events, aligned_events)):
        assert _strip_wall_clock(legacy_event) == _strip_wall_clock(aligned_event), f"tick {i} diverged"

    # Sanity: this fixture actually exercises the pipeline, not just no-ops --
    # a BREAKOUT candidate/signal must have fired on the first tick on BOTH
    # paths, or the parity assertion above would be vacuously true.
    assert legacy_events[0]["symbol"] == "BTC/USDT"
    assert any(c["pattern"] == "BREAKOUT" for c in legacy_events[0].get("candidates", []))
    assert "signal" in legacy_events[0]

    # The actual point of the change: the legacy (direct-fetch-every-cycle)
    # path makes one exchange call per tick; the cache-mediated path only
    # refetches when a bar has genuinely closed (ticks 1 and 4 here).
    assert legacy_fake.fetch_calls == 4
    assert aligned_fake.fetch_calls == 2
