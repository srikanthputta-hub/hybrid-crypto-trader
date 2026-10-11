"""Bar-parity rules P1-P5 in src/bot.py (docs/multiday_hold_design.md §3.1/§6,
docs/bot_bar_parity.md).

Groups:
  0. Startup validation of the new keys (loop.bar_parity, loop.max_entry_delay_minutes,
     logging.bar_decisions_path, logging.closed_bars_dir).
  1. Default-mode identity: with the keys absent (or bar_parity: false) the refactored
     run_cycle writes exactly the same audit events as the frozen pre-change copy
     (tests/_pre_bar_parity_run_cycle.py), cycle by cycle, on today's real config at 1h, on
     the held-symbol / per-base scenarios, and on a 1d replay of the committed Kraken data.
  2. Each rule with bar_parity on: P1 (once per closed bar, no re-entry on the exit bar,
     restart persistence), P2 (sorted entry pass after every monitor check), P3 (same-bar
     exits free no room/cash/base), P4 (staleness guard: entries only, skipped bar still
     decided), P5 (1d htf on the scan frame, no fetch), and the closed-bar archive.
The end-to-end comparison against the backtest harness is tests/test_daily_live_parity.py.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

import src.bot as bot
from src import htf_filter
from src.bot import build_components, build_market_data, run_cycle
from src.config import BarParitySettings, Config, load_config, parse_bar_parity_settings
from src.signal_engine import Signal
from tests._pre_bar_parity_run_cycle import pre_bar_parity_run_cycle
from tests.test_bot_universe_wiring import (
    FIRST_TICK_BAR,
    H_MS,
    T0_MS,
    FakeCcxt,
    _breakout_hourly,
    _Clock,
    _daily,
    _FakeDatetime,
    _flat_hourly,
    _isolated_raw,
    _random_walk_hourly,
    _seed_open_position,
    _small_raw,
    _strip_wall_clock,
    _tick_to,
)

REPO = Path(__file__).resolve().parent.parent
D_MS = 24 * H_MS


@pytest.fixture(autouse=True)
def _isolate_global_state(monkeypatch, tmp_path):
    monkeypatch.setattr(htf_filter, "_cache", {})
    # A config without logging.bar_decisions_path would otherwise use the repo's real
    # logs/bar_decisions.json (relevant only if the parity path ever ran with the flag off).
    monkeypatch.setattr("src.config.DEFAULT_BAR_DECISIONS_PATH", str(tmp_path / "default_bar_decisions.json"))
    monkeypatch.setattr("src.risk_engine.datetime", _FakeDatetime)
    _Clock.now = datetime.fromtimestamp(T0_MS / 1000, tz=timezone.utc)
    bot._reset_bar_parity_state()
    yield
    bot._reset_bar_parity_state()


@pytest.fixture
def memo_indicators(monkeypatch):
    """Test-only speed-up for the long replays: add_indicators is a pure function of its
    input frame, and MarketDataCache hands back the very same frame object on every tick
    inside one bar, so the result is cached by frame identity. Patched identically into the
    current and the frozen run_cycle, so it cannot create or hide a difference between them."""
    import tests._pre_bar_parity_run_cycle as frozen

    original = bot.add_indicators
    memo: dict[int, tuple] = {}

    def cached(df, *args):
        hit = memo.get(id(df))
        if hit is not None and hit[0] is df and hit[1] == args:
            return hit[2]
        out = original(df, *args)
        memo[id(df)] = (df, args, out)
        return out

    monkeypatch.setattr(bot, "add_indicators", cached)
    monkeypatch.setattr(frozen, "add_indicators", cached)
    return cached


def _components(raw: dict, fake: FakeCcxt):
    cfg = Config(raw=raw)
    exchange, risk_engine, portfolio, broker, store, audit = build_components(cfg)
    exchange.client = fake
    market_data = build_market_data(cfg, exchange)
    events: list[dict] = []
    audit.write = events.append
    return cfg, (exchange, risk_engine, portfolio, broker, store, audit, market_data), events


def _parity_raw(symbols, logs: Path, **loop) -> dict:
    raw = _isolated_raw(_small_raw(symbols), logs)
    raw["logging"]["bar_decisions_path"] = str(logs / "bar_decisions.json")
    raw["loop"] = {"poll_interval_seconds": 300, "bar_parity": True, **loop}
    return raw


def _set_now(fake: FakeCcxt, now_ms: int) -> None:
    fake.now_ms = now_ms
    _Clock.now = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)


def _events_for(events, symbol):
    return [e for e in events if e["symbol"] == symbol]


# --- 0. startup validation ----------------------------------------------------


def test_absent_keys_mean_today_behaviour(monkeypatch):
    monkeypatch.setattr("src.config.DEFAULT_BAR_DECISIONS_PATH", "logs/bar_decisions.json")  # undo the fixture
    assert parse_bar_parity_settings({}) == BarParitySettings()
    s = parse_bar_parity_settings({"loop": {"poll_interval_seconds": 300}, "logging": {}})
    assert s.enabled is False and s.max_entry_delay_minutes is None and s.closed_bars_dir is None
    assert s.decisions_path == "logs/bar_decisions.json"


def test_real_config_parses_to_defaults(monkeypatch):
    monkeypatch.setattr("src.config.DEFAULT_BAR_DECISIONS_PATH", "logs/bar_decisions.json")  # undo the fixture
    # Merging this branch must not change the running paper bot: config.yaml spells the
    # keys out with their off values.
    s = load_config().bar_parity
    assert s == BarParitySettings()


@pytest.mark.parametrize("bad", [1, 0, "true", "false", None, 1.0, [True]])
def test_bar_parity_must_be_a_real_bool(bad):
    with pytest.raises(ValueError, match="loop.bar_parity"):
        parse_bar_parity_settings({"loop": {"bar_parity": bad}})


@pytest.mark.parametrize(
    "bad", [0, -1, float("nan"), float("inf"), True, False, "60", [60], 10**400, 1e300]
)
def test_max_entry_delay_minutes_rejects_nonsense(bad):
    with pytest.raises(ValueError, match="max_entry_delay_minutes"):
        parse_bar_parity_settings({"loop": {"bar_parity": True, "max_entry_delay_minutes": bad}})


@pytest.mark.parametrize("good", [60, 0.5, 1440])
def test_max_entry_delay_minutes_accepts_positive_finite(good):
    s = parse_bar_parity_settings({"loop": {"bar_parity": True, "max_entry_delay_minutes": good}})
    assert s.max_entry_delay_minutes == float(good)


def test_null_delay_means_off():
    s = parse_bar_parity_settings({"loop": {"bar_parity": True, "max_entry_delay_minutes": None}})
    assert s.max_entry_delay_minutes is None


@pytest.mark.parametrize(
    "raw, match",
    [
        ({"loop": {"max_entry_delay_minutes": 60}}, "requires loop.bar_parity"),
        ({"loop": {"bar_parity": False, "max_entry_delay_minutes": 60}}, "requires loop.bar_parity"),
        ({"logging": {"closed_bars_dir": "logs/daily_bars"}}, "requires loop.bar_parity"),
        ({"loop": {"bar_parity": True}, "logging": {"closed_bars_dir": ""}}, "closed_bars_dir"),
        ({"loop": {"bar_parity": True}, "logging": {"closed_bars_dir": 5}}, "closed_bars_dir"),
        ({"loop": {"bar_parity": True}, "logging": {"bar_decisions_path": ""}}, "bar_decisions_path"),
        ({"loop": {"bar_parity": True}, "logging": {"bar_decisions_path": None}}, "bar_decisions_path"),
        ({"loop": ["bar_parity"]}, "mapping"),
    ],
)
def test_inconsistent_or_malformed_settings_are_refused(raw, match):
    with pytest.raises(ValueError, match=match):
        parse_bar_parity_settings(raw)


def test_build_components_refuses_bad_parity_settings_at_startup(tmp_path):
    raw = _parity_raw(["ETH/USDT"], tmp_path / "logs", max_entry_delay_minutes=float("nan"))
    with pytest.raises(ValueError, match="max_entry_delay_minutes"):
        build_components(Config(raw=raw))


def test_htf_scan_tail_matches_the_harness_window():
    from strategy_pack_run import backtest_live_bot

    assert bot.HTF_SCAN_TAIL_BARS == backtest_live_bot.SCAN_TAIL_BARS == 150


# --- 1. default mode is byte-identical to the pre-change run_cycle -----------------


def _run_pair(tmp_path, raw_factory, fixtures, ticks, seed=None):
    """Runs the frozen pre-change run_cycle and the current one over the same replay,
    each with its own fake exchange, logs and htf cache. Returns both event lists and
    the two component tuples."""
    out = []
    for name, cycle in (("before", pre_bar_parity_run_cycle), ("after", run_cycle)):
        logs = tmp_path / name
        if seed is not None:
            seed(logs)
        raw = raw_factory(logs)
        fake = FakeCcxt(*fixtures)
        cfg, parts, events = _components(raw, fake)
        htf_filter._cache.clear()
        for tick in ticks:
            tick(fake)
            cycle(cfg, *parts)
        out.append((events, parts, fake))
    return out


def _assert_identical(before, after):
    assert len(before) == len(after)
    for i, (b, a) in enumerate(zip(before, after)):
        assert _strip_wall_clock(b) == _strip_wall_clock(a), f"event {i} ({b['symbol']}) diverged"


def test_default_mode_matches_pre_change_run_cycle_on_todays_config(tmp_path, memo_indicators):
    real_raw = load_config().raw
    symbols = list(real_raw["market"]["symbols"])
    hourly = {s: _random_walk_hourly(seed=i, start_price=10.0 * (i + 1)) for i, s in enumerate(symbols)}
    daily = {s: _daily(10.0 * (i + 1), uptrend=(i % 4 != 3)) for i, s in enumerate(symbols)}

    def raw_factory(logs):
        return _isolated_raw(real_raw, logs)  # config.yaml's own loop block: bar_parity false

    # Two ticks per bar: the second one is the "same bar again" case P1 is about.
    ticks = []
    for bar in range(FIRST_TICK_BAR, 251):
        ticks.append(lambda f, b=bar: _tick_to(f, b))
        ticks.append(lambda f, b=bar: _tick_to(f, b))
    (before, b_parts, b_fake), (after, a_parts, a_fake) = _run_pair(tmp_path, raw_factory, (hourly, daily), ticks)

    _assert_identical(before, after)
    assert b_fake.calls == a_fake.calls  # same exchange traffic, call for call
    assert b_parts[2].cash == a_parts[2].cash
    assert set(b_parts[4].all_current()) == set(a_parts[4].all_current())
    # Non-vacuous: plans, immediate fills, exits, htf vetoes and risk rejections all happened,
    # and the second tick on a bar re-scanned (today's behaviour, which P1 changes when on).
    actions = {(e.get("monitor_action") or {}).get("action") for e in after}
    assert sum(e.get("action") == "planned" for e in after) >= 5
    assert {"STOP_LOSS"} <= actions
    assert any(e.get("daily_trend_confirmed") is False for e in after)
    assert any((e.get("risk_decision") or {}).get("allowed") is False for e in after)
    # No bar-parity artefacts when off.
    assert not any("bar_close" in e for e in after)
    assert not (tmp_path / "after" / "bar_decisions.json").exists()
    assert not (bot.PROJECT_ROOT / "logs" / "bar_decisions.json").exists()


@pytest.mark.parametrize("loop_block", [None, {"poll_interval_seconds": 300, "bar_parity": False}])
@pytest.mark.parametrize("scenario", ["held_outside_universe", "per_base_same_cycle", "stop_frees_base"])
def test_default_mode_matches_pre_change_run_cycle_on_wiring_scenarios(tmp_path, scenario, loop_block):
    if scenario == "held_outside_universe":
        symbols = ["ETH/USDT"]
        fixtures = ({"ETH/USDT": _breakout_hourly(), "BTC/USDT": _flat_hourly(100.0, last_close=85.0, n=251)}, {})

        def seed(logs):
            _seed_open_position(logs, "BTC/USDT", fill_price=100.0, stop=90.0, target=130.0)
    elif scenario == "per_base_same_cycle":
        symbols = ["BTC/USD", "BTC/USDT"]
        fixtures = ({"BTC/USD": _breakout_hourly(), "BTC/USDT": _breakout_hourly()}, {})
        seed = None
    else:
        symbols = ["BTC/USDT", "BTC/USD"]
        fixtures = ({"BTC/USDT": _flat_hourly(100.0, last_close=85.0, n=251), "BTC/USD": _breakout_hourly()}, {})

        def seed(logs):
            _seed_open_position(logs, "BTC/USDT", fill_price=100.0, stop=90.0, target=130.0)

    def raw_factory(logs):
        raw = _isolated_raw(_small_raw(symbols), logs)
        if loop_block is not None:
            raw["loop"] = dict(loop_block)
        return raw

    ticks = [lambda f: _tick_to(f, 250), lambda f: _tick_to(f, 250)]
    (before, _, _), (after, _, _) = _run_pair(tmp_path, raw_factory, fixtures, ticks, seed=seed)
    _assert_identical(before, after)
    assert any(e.get("action") == "planned" for e in after)


def _kraken_daily_rows(n_bars: int) -> dict[str, list[list[float]]]:
    """The committed Kraken 1d CSVs (strategy_pack_run/data), first n_bars of the shared axis."""
    rows = {}
    for path in sorted((REPO / "strategy_pack_run" / "data").glob("*_1d.csv")):
        base, quote = path.stem.split("_")[:2]
        df = pd.read_csv(path)
        rows[f"{base}/{quote}"] = [[int(r[0])] + [float(v) for v in r[1:]] for r in df[
            ["timestamp", "open", "high", "low", "close", "volume"]].values.tolist()]
    axis = sorted({r[0] for rs in rows.values() for r in rs})[:n_bars]
    return {s: [r for r in rs if r[0] <= axis[-1]] for s, rs in rows.items()}, axis


def test_default_mode_matches_pre_change_run_cycle_at_1d(tmp_path, memo_indicators):
    # Same identity at market.timeframe "1d" with the flags off: today's code re-scans the
    # same daily bar on every tick (exactly what P1 fixes when switched on), and the htf
    # gate fetches 31 daily bars. Both must be untouched by default.
    rows, axis = _kraken_daily_rows(50)
    symbols = sorted(rows)

    def raw_factory(logs):
        raw = _isolated_raw(load_config().raw, logs)
        raw["market"]["symbols"] = symbols
        raw["market"]["timeframe"] = "1d"
        return raw

    ticks = []
    for ts in axis[30:]:
        for k in range(2):
            ticks.append(lambda f, t=ts, k=k: _set_now(f, t + D_MS + 60_000 + k * 300_000))
    (before, _, b_fake), (after, _, a_fake) = _run_pair(tmp_path, raw_factory, ({}, rows), ticks)
    _assert_identical(before, after)
    assert b_fake.calls == a_fake.calls
    assert any(e.get("action") == "planned" for e in after)
    # The pre-change behaviour really does re-plan/re-scan within one daily bar: the second
    # tick of a bar scans every FLAT symbol again (the divergence P1 removes when on).
    scans_per_tick = [sum("candidates" in e for e in after[i:i + len(symbols)]) for i in range(0, len(after), len(symbols))]
    assert all(n > 0 for n in scans_per_tick[1::2])


# --- 2. bar parity ON -------------------------------------------------------------


def _scan_spy(monkeypatch):
    calls: list[str] = []
    original = bot.scanner.scan

    def spy(df, cfg):
        calls.append(df.attrs.get("_sym", "?"))
        return original(df, cfg)

    monkeypatch.setattr(bot.scanner, "scan", spy)
    return calls


def test_p1_flat_symbol_is_scanned_once_per_closed_bar(tmp_path, monkeypatch):
    raw = _parity_raw(["ETH/USDT", "SOL/USDT"], tmp_path / "logs")
    fake = FakeCcxt({"ETH/USDT": _flat_hourly(50.0), "SOL/USDT": _flat_hourly(20.0)}, {})
    cfg, parts, events = _components(raw, fake)
    calls = _scan_spy(monkeypatch)

    _tick_to(fake, 230)
    run_cycle(cfg, *parts)
    assert len(calls) == 2
    first = list(events)
    assert all(e["bar_close"] == pd.Timestamp(T0_MS + 231 * H_MS, unit="ms", tz="UTC").isoformat() for e in first)

    for minute in (5, 10, 55):  # the same closed bar, later ticks
        events.clear()
        _set_now(fake, T0_MS + 231 * H_MS + minute * 60_000)
        run_cycle(cfg, *parts)
        assert len(calls) == 2
        assert [e["symbol"] for e in events] == ["ETH/USDT", "SOL/USDT"]  # still one line per symbol
        assert all(e["action"] == "none" and "already decided" in e["note"] for e in events)

    events.clear()
    _tick_to(fake, 231)  # next closed bar
    run_cycle(cfg, *parts)
    assert len(calls) == 4
    assert all("candidates" in e for e in events)


def test_p1_no_reentry_on_the_exit_bar(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    _seed_open_position(logs, "BTC/USDT", fill_price=100.0, stop=90.0, target=130.0)
    raw = _parity_raw(["BTC/USDT"], logs)
    # Stop is hit on bar 250's close; bar 251 is a breakout that could plan again.
    hourly = _flat_hourly(100.0, last_close=85.0, n=251)
    breakout = _breakout_hourly(n_flat=251)
    hourly.append([T0_MS + 251 * H_MS] + breakout[-1][1:])
    fake = FakeCcxt({"BTC/USDT": hourly}, {})
    cfg, parts, events = _components(raw, fake)
    calls = _scan_spy(monkeypatch)

    _tick_to(fake, 250)
    run_cycle(cfg, *parts)
    assert events[0]["monitor_action"]["action"] == "STOP_LOSS"
    assert parts[4].get("BTC/USDT") is None
    for minute in (5, 30):
        events.clear()
        _set_now(fake, T0_MS + 251 * H_MS + minute * 60_000)
        run_cycle(cfg, *parts)
        assert events[0]["action"] == "none" and "already decided" in events[0]["note"]
    assert calls == []  # never scanned on the bar it exited on

    events.clear()
    _tick_to(fake, 251)
    run_cycle(cfg, *parts)
    assert len(calls) == 1 and "candidates" in events[0]


def test_p1_flags_off_reenters_on_the_exit_bar(tmp_path, monkeypatch):
    # Control for the test above: today's behaviour scans the just-closed symbol again on the
    # very next tick of the same bar.
    logs = tmp_path / "logs"
    _seed_open_position(logs, "BTC/USDT", fill_price=100.0, stop=90.0, target=130.0)
    raw = _isolated_raw(_small_raw(["BTC/USDT"]), logs)
    fake = FakeCcxt({"BTC/USDT": _flat_hourly(100.0, last_close=85.0, n=251)}, {})
    cfg, parts, events = _components(raw, fake)
    calls = _scan_spy(monkeypatch)
    _tick_to(fake, 250)
    run_cycle(cfg, *parts)
    _set_now(fake, T0_MS + 251 * H_MS + 5 * 60_000)
    run_cycle(cfg, *parts)
    assert len(calls) == 1


def test_p1_decisions_survive_a_restart(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    raw = _parity_raw(["ETH/USDT"], logs)
    (logs / "KILL_SWITCH").parent.mkdir(parents=True, exist_ok=True)
    (logs / "KILL_SWITCH").write_text("")
    fake = FakeCcxt({"ETH/USDT": _breakout_hourly()}, {})
    cfg, parts, events = _components(raw, fake)
    _tick_to(fake, 250)
    run_cycle(cfg, *parts)
    assert events[0]["action"] == "none" and "kill switch" in events[0]["risk_decision"]["reason"].lower()
    ledger = json.loads((logs / "bar_decisions.json").read_text())
    assert ledger == {"ETH/USDT": pd.Timestamp(T0_MS + 251 * H_MS, unit="ms", tz="UTC").isoformat()}

    # Restart mid-bar (new process: fresh in-memory state, fresh components), kill switch gone.
    (logs / "KILL_SWITCH").unlink()
    bot._reset_bar_parity_state()
    cfg, parts, events = _components(raw, fake)
    _set_now(fake, T0_MS + 251 * H_MS + 20 * 60_000)
    run_cycle(cfg, *parts)
    assert events[0]["action"] == "none" and "already decided" in events[0]["note"]
    assert parts[4].get("ETH/USDT") is None


def test_p1_unreadable_ledger_starts_empty_and_keeps_monitoring(tmp_path):
    logs = tmp_path / "logs"
    _seed_open_position(logs, "BTC/USDT", fill_price=100.0, stop=90.0, target=130.0)
    (logs / "bar_decisions.json").write_text("{not json")
    raw = _parity_raw(["BTC/USDT", "ETH/USDT"], logs)
    fake = FakeCcxt({"BTC/USDT": _flat_hourly(100.0, last_close=85.0, n=251), "ETH/USDT": _flat_hourly(50.0)}, {})
    cfg, parts, events = _components(raw, fake)
    _tick_to(fake, 250)
    run_cycle(cfg, *parts)
    assert _events_for(events, "BTC/USDT")[0]["monitor_action"]["action"] == "STOP_LOSS"
    assert "candidates" in _events_for(events, "ETH/USDT")[0]
    assert set(json.loads((logs / "bar_decisions.json").read_text())) == {"BTC/USDT", "ETH/USDT"}


def test_p2_entries_run_in_sorted_order_regardless_of_config_order(tmp_path):
    def run(parity: bool):
        logs = tmp_path / ("on" if parity else "off")
        raw = _parity_raw(["XRP/USDT", "ADA/USDT"], logs) if parity else _isolated_raw(
            _small_raw(["XRP/USDT", "ADA/USDT"]), logs)
        raw["risk"]["max_new_entries_per_window"] = 1
        fake = FakeCcxt({"XRP/USDT": _breakout_hourly(), "ADA/USDT": _breakout_hourly()}, {})
        cfg, parts, events = _components(raw, fake)
        _tick_to(fake, 250)
        run_cycle(cfg, *parts)
        return {e["symbol"]: e for e in events}, [e["symbol"] for e in events]

    off, off_order = run(False)
    on, on_order = run(True)
    assert off_order == ["XRP/USDT", "ADA/USDT"] and off["XRP/USDT"]["action"] == "planned"
    assert "rate cap" in off["ADA/USDT"]["risk_decision"]["reason"]
    assert on_order == ["ADA/USDT", "XRP/USDT"] and on["ADA/USDT"]["action"] == "planned"
    assert "rate cap" in on["XRP/USDT"]["risk_decision"]["reason"]


def test_p2_monitor_pass_runs_before_any_entry(tmp_path):
    # AAA (alphabetically first, FLAT, breakout) must be decided AFTER ZZZ's held-position
    # check, even though AAA comes first in config and sorted order.
    logs = tmp_path / "logs"
    _seed_open_position(logs, "ZZZ/USDT", fill_price=100.0, stop=90.0, target=130.0)
    raw = _parity_raw(["AAA/USDT", "ZZZ/USDT"], logs)
    fake = FakeCcxt({"AAA/USDT": _breakout_hourly(), "ZZZ/USDT": _flat_hourly(100.0, n=251)}, {})
    cfg, parts, events = _components(raw, fake)
    _tick_to(fake, 250)
    run_cycle(cfg, *parts)
    assert [e["symbol"] for e in events] == ["ZZZ/USDT", "AAA/USDT"]
    assert events[1]["action"] == "planned"


def _p3_setup(tmp_path, parity: bool, exposure: float):
    logs = tmp_path / ("on" if parity else "off")
    # A large BTC position (bought at 4000, 1 unit) stops out on this bar at 3500.
    _seed_open_position(logs, "BTC/USDT", fill_price=4000.0, stop=3600.0, target=5000.0)
    symbols = ["BTC/USDT", "ETH/USDT"]
    raw = _parity_raw(symbols, logs) if parity else _isolated_raw(_small_raw(symbols), logs)
    raw["risk"]["max_total_exposure_pct"] = exposure
    fake = FakeCcxt({"BTC/USDT": _flat_hourly(4000.0, last_close=3500.0, n=251), "ETH/USDT": _breakout_hourly()}, {})
    cfg, parts, events = _components(raw, fake)
    seen = {}
    original = parts[1].check_new_trade

    def spy(symbol, side, portfolio_equity, cash_available, **kwargs):
        seen.update(equity=portfolio_equity, cash=cash_available, other=kwargs["open_positions_value"])
        return original(symbol, side, portfolio_equity, cash_available, **kwargs)

    parts[1].check_new_trade = spy
    _tick_to(fake, 250)
    run_cycle(cfg, *parts)
    return {e["symbol"]: e for e in events}, seen, parts


def test_p3_same_bar_exit_frees_no_room_or_cash(tmp_path):
    # equity at the close = 6000 cash + 3500 BTC = 9500; cap 0.35 -> 3325 < 3500 held.
    on, seen_on, _ = _p3_setup(tmp_path, parity=True, exposure=0.35)
    assert on["BTC/USDT"]["monitor_action"]["action"] == "STOP_LOSS"
    assert seen_on["equity"] == pytest.approx(9500.0)
    assert seen_on["other"] == pytest.approx(3500.0)  # the exited position still counts
    assert seen_on["cash"] == pytest.approx(6000.0)  # its sale proceeds don't, yet
    assert on["ETH/USDT"]["action"] == "none"
    assert on["ETH/USDT"]["risk_decision"]["allowed"] is False

    off, seen_off, _ = _p3_setup(tmp_path, parity=False, exposure=0.35)
    assert seen_off["other"] == 0 and seen_off["cash"] == pytest.approx(6000.0 + 3500.0 * 0.999)
    assert off["ETH/USDT"]["action"] == "planned"


def test_p3_room_left_before_exits_is_still_usable(tmp_path):
    # cap 0.40 -> 3800 - 3500 = 300 of room at the close: ETH may use that, no more.
    on, seen_on, parts = _p3_setup(tmp_path, parity=True, exposure=0.40)
    assert on["ETH/USDT"]["action"] == "planned"
    assert on["ETH/USDT"]["risk_decision"]["max_quote_amount"] == pytest.approx(300.0)


def test_p3_same_bar_exit_still_holds_its_base(tmp_path):
    logs = tmp_path / "logs"
    _seed_open_position(logs, "BTC/USDT", fill_price=100.0, stop=90.0, target=130.0)
    raw = _parity_raw(["BTC/USDT", "BTC/USD"], logs)
    fake = FakeCcxt({"BTC/USDT": _flat_hourly(100.0, last_close=85.0, n=251), "BTC/USD": _breakout_hourly()}, {})
    cfg, parts, events = _components(raw, fake)
    _tick_to(fake, 250)
    run_cycle(cfg, *parts)
    assert _events_for(events, "BTC/USDT")[0]["monitor_action"]["action"] == "STOP_LOSS"
    btc_usd = _events_for(events, "BTC/USD")[0]
    assert btc_usd["action"] == "none" and "BTC/USDT" in btc_usd["note"] and "candidates" not in btc_usd


def _stale_setup(tmp_path, minutes_after_close: float, delay=60):
    logs = tmp_path / "logs"
    _seed_open_position(logs, "BTC/USDT", fill_price=100.0, stop=90.0, target=130.0)
    raw = _parity_raw(["BTC/USDT", "ETH/USDT"], logs, max_entry_delay_minutes=delay)
    # No newer bar ever arrives (e.g. the bot was down): bar 250 is the latest closed bar.
    fake = FakeCcxt({"BTC/USDT": _flat_hourly(100.0, last_close=85.0, n=251), "ETH/USDT": _breakout_hourly()}, {})
    cfg, parts, events = _components(raw, fake)
    _set_now(fake, T0_MS + 251 * H_MS + int(minutes_after_close * 60_000))
    run_cycle(cfg, *parts)
    return cfg, parts, events, fake


def test_p4_stale_bar_blocks_entries_but_not_exits(tmp_path, monkeypatch):
    calls = _scan_spy(monkeypatch)
    cfg, parts, events, fake = _stale_setup(tmp_path, 61)
    by = {e["symbol"]: e for e in events}
    assert by["BTC/USDT"]["monitor_action"]["action"] == "STOP_LOSS"  # exits never gated
    assert by["ETH/USDT"]["action"] == "none" and by["ETH/USDT"]["note"] == "stale bar: entries skipped"
    assert by["ETH/USDT"]["entry_delay_minutes"] == pytest.approx(61.0)
    assert calls == [] and parts[4].get("ETH/USDT") is None
    # The skipped bar counts as decided: it is not retried later on the same bar...
    events.clear()
    _set_now(fake, fake.now_ms + 5 * 60_000)
    run_cycle(cfg, *parts)
    assert "already decided" in _events_for(events, "ETH/USDT")[0]["note"]
    # ...nor after a restart.
    bot._reset_bar_parity_state()
    cfg2, parts2, events2 = _components(cfg.raw, fake)
    run_cycle(cfg2, *parts2)
    assert "already decided" in _events_for(events2, "ETH/USDT")[0]["note"]


def test_p4_fresh_bar_is_not_blocked(tmp_path):
    _, parts, events, _ = _stale_setup(tmp_path, 59)
    assert _events_for(events, "ETH/USDT")[0]["action"] == "planned"


def test_p4_guard_off_by_default_even_with_bar_parity(tmp_path):
    _, parts, events, _ = _stale_setup(tmp_path, 600, delay=None)
    assert _events_for(events, "ETH/USDT")[0]["action"] == "planned"


# --- P5: htf on the scan frame at 1d ---------------------------------------------------


def _daily_rows_with_htf_disagreement() -> list[list[float]]:
    """170 rising days then 30 slowly falling ones: the 150-bar window still has fast EMA >
    slow EMA, the 31-bar live fetch does not (the design doc's E8 mechanism)."""
    closes = [100.0]
    for _ in range(170):
        closes.append(closes[-1] * 1.01)
    for _ in range(30):
        closes.append(closes[-1] * 0.999)
    rows = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        rows.append([T0_MS + i * D_MS, o, max(o, c) * 1.003, min(o, c) * 0.997, c, 1000.0])
    return rows


def _forced_bullish_signal(*_args, **_kwargs):
    return Signal(direction="bullish", primary_pattern="TREND_CONTINUATION", engine_score=0.9,
                  supporting_patterns=[], primary_details={}, rationale={"test": True})


def _run_1d(tmp_path, monkeypatch, parity: bool):
    rows = _daily_rows_with_htf_disagreement()
    logs = tmp_path / ("on" if parity else "off")
    raw = _parity_raw(["ETH/USDT"], logs) if parity else _isolated_raw(_small_raw(["ETH/USDT"]), logs)
    raw["market"]["timeframe"] = "1d"
    raw["market"]["history_bars"] = 200
    raw["htf_filter"] = {"enabled": True}
    fake = FakeCcxt({}, {"ETH/USDT": rows})
    cfg, parts, events = _components(raw, fake)
    monkeypatch.setattr(bot.signal_engine, "evaluate", _forced_bullish_signal)
    wrapper_calls = []
    original_wrapper = htf_filter.daily_trend_confirmed
    monkeypatch.setattr(bot.htf_filter, "daily_trend_confirmed",
                        lambda *a, **k: wrapper_calls.append(a) or original_wrapper(*a, **k))
    _set_now(fake, rows[-1][0] + D_MS + 60_000)
    run_cycle(cfg, *parts)
    return events, wrapper_calls, fake, rows


def test_p5_daily_htf_uses_the_scan_frame_without_a_fetch(tmp_path, monkeypatch):
    events, wrapper_calls, fake, rows = _run_1d(tmp_path, monkeypatch, parity=True)
    assert wrapper_calls == []
    assert fake.calls == [("ETH/USDT", "1d")]  # only the market-data fetch
    ev = events[0]
    assert ev["daily_trend_source"] == "scan_frame"
    expected = htf_filter.daily_trend_confirmed_from_df(pd.DataFrame({"close": [r[4] for r in rows[-150:]]}), 12, 26)
    assert ev["daily_trend_confirmed"] is expected is True
    assert ev["action"] == "planned"


def test_p5_flags_off_keeps_the_31_bar_fetch(tmp_path, monkeypatch):
    events, wrapper_calls, fake, _ = _run_1d(tmp_path, monkeypatch, parity=False)
    assert len(wrapper_calls) == 1
    assert fake.calls == [("ETH/USDT", "1d"), ("ETH/USDT", "1d")]  # market data + htf fetch
    assert events[0]["daily_trend_confirmed"] is False and "daily_trend_source" not in events[0]


def test_p5_does_not_apply_at_1h(tmp_path, monkeypatch):
    raw = _parity_raw(["ETH/USDT"], tmp_path / "logs")
    raw["htf_filter"] = {"enabled": True}
    daily = _daily(10.0, uptrend=True)
    fake = FakeCcxt({"ETH/USDT": _breakout_hourly()}, {"ETH/USDT": daily})
    cfg, parts, events = _components(raw, fake)
    _tick_to(fake, 250)
    run_cycle(cfg, *parts)
    assert ("ETH/USDT", "1d") in fake.calls
    assert events[0]["daily_trend_confirmed"] is True and "daily_trend_source" not in events[0]


# --- closed-bar archive --------------------------------------------------------------


def test_closed_bar_archive_records_every_bar_once_in_harness_format(tmp_path):
    from strategy_pack_run.backtest_live_bot import load_csv

    logs = tmp_path / "logs"
    raw = _parity_raw(["ETH/USDT"], logs)
    raw["logging"]["closed_bars_dir"] = str(logs / "daily_bars")
    rows = _flat_hourly(50.0, n=260)
    fake = FakeCcxt({"ETH/USDT": rows}, {})
    cfg, parts, events = _components(raw, fake)
    path = logs / "daily_bars" / "ETH_USDT_1h.csv"

    _tick_to(fake, 230)
    run_cycle(cfg, *parts)
    run_cycle(cfg, *parts)  # same bar again: nothing new
    assert len(path.read_text().splitlines()) == 1 + 50  # header + the fetched history_bars
    _tick_to(fake, 231)
    run_cycle(cfg, *parts)
    bot._reset_bar_parity_state()  # restart: resumes from the file, no duplicates
    cfg, parts, events = _components(raw, fake)
    run_cycle(cfg, *parts)
    _tick_to(fake, 233)  # a missed bar (bot down at 232) is still filled in from the fetch
    run_cycle(cfg, *parts)

    lines = path.read_text().splitlines()
    assert lines[0] == "timestamp,open,high,low,close,volume" and len(lines) == 1 + 50 + 3
    df = load_csv(path, "1h")
    expected = [r for r in rows if T0_MS + 181 * H_MS <= r[0] <= T0_MS + 233 * H_MS]
    assert list(df["close"]) == [r[4] for r in expected]  # exact floats
    assert list(df.index) == [pd.Timestamp(r[0] + H_MS, unit="ms", tz="UTC") for r in expected]


def test_closed_bar_archive_failure_never_stops_the_monitor(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    _seed_open_position(logs, "BTC/USDT", fill_price=100.0, stop=90.0, target=130.0)
    raw = _parity_raw(["BTC/USDT"], logs)
    raw["logging"]["closed_bars_dir"] = str(logs / "daily_bars")
    fake = FakeCcxt({"BTC/USDT": _flat_hourly(100.0, last_close=85.0, n=251)}, {})
    cfg, parts, events = _components(raw, fake)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(bot, "_archive_closed_bars", boom)
    _tick_to(fake, 250)
    run_cycle(cfg, *parts)
    assert events[0]["monitor_action"]["action"] == "STOP_LOSS"


def test_ledger_write_failure_still_gates_in_memory(tmp_path, monkeypatch):
    raw = _parity_raw(["ETH/USDT"], tmp_path / "logs")
    fake = FakeCcxt({"ETH/USDT": _flat_hourly(50.0)}, {})
    cfg, parts, events = _components(raw, fake)
    calls = _scan_spy(monkeypatch)
    original_replace = Path.replace

    def failing_replace(self, target):
        if str(target).endswith("bar_decisions.json"):
            raise OSError("read-only")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", failing_replace)
    _tick_to(fake, 230)
    run_cycle(cfg, *parts)
    run_cycle(cfg, *parts)
    assert len(calls) == 1
