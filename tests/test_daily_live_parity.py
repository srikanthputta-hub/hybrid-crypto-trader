"""Design test 7 (docs/multiday_hold_design.md §6, docs/bot_bar_parity.md §4): the live loop,
replayed bar by bar over the committed Kraken daily data, makes the same decisions as the P6
backtest harness.

A fake exchange serves strategy_pack_run/data/*_1d.csv bar by bar. src.bot.run_cycle runs with
loop.bar_parity on at market.timeframe "1d" (paper broker, tmp logs, two cycles per bar: 1 min
and 6 min after the close). strategy_pack_run.backtest_live_bot.simulate_portfolio runs with
count_same_bar_approvals=True (P6) over the same bars. Compared:
  - every risk-engine check, keyed by (bar, symbol): approved or not, size, equity, entry, stop;
  - every exit, keyed by (decision bar, symbol, stop_loss/take_profit).

Why the exact run uses a transformed copy of the data and a zero fee (design test 7: "exact
equality against today's default harness is not achievable"):
  - fill price: the paper broker fills at the last close, the harness at the next open. Setting
    each bar's open to the previous close ("gapless") makes the two the same number, so cash and
    equity stay identical on both sides. Both sides see the same transformed bars.
  - fee: with a fee, a paper position is worth quote*(1-fee) while the harness counts the gross
    quote (P6 doc §0.3 item 6), so ~0.1% of each same-bar fill reappears as room in live only.
  - float residue: at a zero fee, room left after a same-bar fill is ~1e-12 USD on one side and
    exactly 0 on the other (base*price vs the quote). min_order_quote_usd = 0.01 is set on BOTH
    sides so neither approves such a phantom; a check where one side says "no room" and the other
    "below the minimum order size" for a sub-cent size is the same decision.
  - indicator warm-up: market.history_bars covers the whole fixture, so live indicators equal the
    harness's full-history ones.
The realistic configuration (real opens, the 0.10% paper fee, the live 200-bar history) is
measured against the design's 10% explained-mismatch budget by
test_realistic_config_with_a_dust_floor_is_within_the_mismatch_budget, and the known float-phantom
divergence with no floor (min_order_quote_usd: 0, the committed value until it was set to 10 per
docs/min_order_floor_study.md) is pinned by test_committed_config_divergence_is_a_float_phantom.
Every test here passes the floor explicitly; none reads it from config/config.yaml.

The 719-bar full replay takes a few minutes; it runs when HCT_FULL_PARITY=1. The default run
covers the first DEFAULT_BARS bars (which already contain same-bar contention for the exposure
cap, stop-losses, take-profits and same-bar exit+entry bars, asserted below).
"""
from __future__ import annotations

import copy
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import src.bot as bot
import src.risk_engine as risk_module
from src import htf_filter
from src.config import Config, load_config
from strategy_pack_run import backtest_live_bot as harness
from tests.test_bot_universe_wiring import _Clock, _FakeDatetime

REPO = Path(__file__).resolve().parent.parent
D_MS = 86_400_000
DEFAULT_BARS = 130
FULL = os.environ.get("HCT_FULL_PARITY") == "1"
PHANTOM_USD = 0.01
NO_ROOM = "no room left to size this trade"
BELOW_MIN = "below the minimum order size"

# Design §5 budget (docs/bot_bar_parity.md §4.3): harness entries (bar, symbol) with no matching
# live entry, plus live-only entries, over the harness entry count; same for exits.
REALISTIC_ENTRY_MISMATCH_BUDGET = 0.10


class DailyFakeCcxt:
    def __init__(self, rows: dict[str, list[list[float]]]) -> None:
        self.rows = rows
        self.now_ms = 0
        self.calls: list[tuple[str, str, int]] = []

    def fetch_ohlcv(self, symbol, timeframe=None, limit=None):
        self.calls.append((symbol, timeframe, limit))
        visible = [r for r in self.rows.get(symbol, []) if r[0] <= self.now_ms]
        if not visible:
            raise RuntimeError(f"BadSymbol / not listed yet: {symbol}")
        return visible[-limit:] if limit else visible

    def parse_timeframe(self, timeframe):
        return {"1d": 86400, "1h": 3600}[timeframe]

    def milliseconds(self):
        return self.now_ms


def load_rows(n_bars: int, gapless: bool) -> tuple[dict[str, list[list[float]]], list[int]]:
    rows = {}
    for path in sorted((REPO / "strategy_pack_run" / "data").glob("*_1d.csv")):
        base, quote = path.stem.split("_")[:2]
        df = pd.read_csv(path)
        r = [[int(x[0])] + [float(v) for v in x[1:]] for x in df[["timestamp", "open", "high", "low", "close", "volume"]].values.tolist()]
        if gapless:
            for i in range(1, len(r)):
                o = r[i - 1][4]
                r[i][1], r[i][2], r[i][3] = o, max(r[i][2], o), min(r[i][3], o)
        rows[f"{base}/{quote}"] = r
    axis = sorted({x[0] for rs in rows.values() for x in rs})[:n_bars]
    rows = {s: [x for x in rs if x[0] <= axis[-1]] for s, rs in rows.items()}
    return {s: rs for s, rs in rows.items() if rs}, axis


def _record(store: list, symbol, portfolio_equity, cash_available, kw, decision, bar_close):
    store.append(dict(
        bar=pd.Timestamp(bar_close).floor("D"), symbol=symbol, equity=float(portfolio_equity),
        cash=float(cash_available), open_value=float(kw["open_positions_value"]), entry=float(kw["entry_price"]),
        stop=float(kw["stop_loss"]), rr=float(kw["risk_reward"]), allowed=decision.allowed,
        reason=decision.reason, quote=None if decision.max_quote_amount is None else float(decision.max_quote_amount),
    ))


def run_harness(rows, fee: float, min_order: float, tmp: Path):
    real = load_config().raw
    risk_cfg = {**real["risk"], "min_order_quote_usd": min_order, "kill_switch_file": str(tmp / "NO_KILL_SWITCH")}
    frames = {}
    for s, rs in rows.items():
        df = pd.DataFrame(rs, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df.index = pd.to_datetime(df["timestamp"], unit="ms", utc=True) + pd.Timedelta(days=1)
        df.index.name = "close_time"
        frames[s] = df[["open", "high", "low", "close", "volume"]].astype(float)
    enr, sigs, _ = harness.build_enriched_and_signals(
        frames, real["indicators"], real["scanner"], real["signal_engine"], bool(real["htf_filter"]["enabled"])
    )
    checks: list[dict] = []

    class RecordingRiskEngine(risk_module.RiskEngine):
        def check_new_trade(self, symbol, side, portfolio_equity, cash_available, **kw):
            decision = super().check_new_trade(symbol, side, portfolio_equity, cash_available, **kw)
            _record(checks, symbol, portfolio_equity, cash_available, kw, decision, kw["now"])
            return decision

    original = harness.RiskEngine
    harness.RiskEngine = RecordingRiskEngine
    try:
        res = harness.simulate_portfolio(
            enr, sigs, harness.Costs(fee, 0.0, "parity"), risk_cfg, real["trade_planner"], 10_000.0, 365.0, tmp,
            count_same_bar_approvals=True,
        )
    finally:
        harness.RiskEngine = original
    exits = {
        (pd.Timestamp(t.exit_time) - pd.Timedelta(days=1), t.symbol, t.reason)
        for t in res.trades.itertuples()
        if t.reason in ("stop_loss", "take_profit")
    }
    return checks, exits, res


def run_bot(rows, axis, fee: float, min_order: float, history_bars: int, tmp: Path, monkeypatch):
    real = load_config().raw
    raw = copy.deepcopy(real)
    raw["mode"] = "paper"
    raw["advisor"] = {"enabled": False}
    raw["market"]["symbols"] = sorted(rows)
    raw["market"]["timeframe"] = "1d"
    raw["market"]["history_bars"] = history_bars
    raw["risk"]["min_order_quote_usd"] = min_order
    raw["risk"]["kill_switch_file"] = str(tmp / "NO_KILL_SWITCH")
    raw["loop"] = {"poll_interval_seconds": 300, "bar_parity": True, "max_entry_delay_minutes": 60}
    raw["logging"] = {k: str(tmp / v) for k, v in {
        "audit_log_path": "audit.jsonl", "trade_log_path": "trades.csv",
        "trade_plan_current_path": "plans.json", "trade_plan_history_path": "plans.jsonl",
        "market_snapshot_path": "snapshot.json", "bar_decisions_path": "bar_decisions.json",
    }.items()}
    cfg = Config(raw=raw)
    fake = DailyFakeCcxt(rows)
    exchange, risk_engine, portfolio, broker, store, audit = bot.build_components(cfg)
    exchange.client = fake
    broker.fee_pct = fee
    market_data = bot.build_market_data(cfg, exchange)
    events: list[dict] = []
    audit.write = events.append
    checks: list[dict] = []
    original = risk_engine.check_new_trade

    def spy(symbol, side, portfolio_equity, cash_available, **kw):
        decision = original(symbol, side, portfolio_equity, cash_available, **kw)
        _record(checks, symbol, portfolio_equity, cash_available, kw, decision, _Clock.now)
        return decision

    risk_engine.check_new_trade = spy

    # add_indicators is pure and MarketDataCache returns the same frame object for both ticks
    # of a bar: cache by identity (speed only; the harness computes its own indicators).
    memo: dict[int, tuple] = {}
    real_add = bot.add_indicators

    def cached(df, *args):
        hit = memo.get(id(df))
        if hit is not None and hit[0] is df:
            return hit[1]
        out = real_add(df, *args)
        memo[id(df)] = (df, out)
        return out

    monkeypatch.setattr(bot, "add_indicators", cached)
    per_cycle_scans = []
    for ts in axis[3:]:
        for k in range(2):
            fake.now_ms = ts + D_MS + 60_000 + k * 300_000
            _Clock.now = datetime.fromtimestamp(fake.now_ms / 1000, tz=timezone.utc)
            n0 = len(events)
            bot.run_cycle(cfg, exchange, risk_engine, portfolio, broker, store, audit, market_data)
            per_cycle_scans.append((k, sum("candidates" in e for e in events[n0:])))
    exits = set()
    for e in events:
        action = (e.get("monitor_action") or {}).get("action")
        if action in ("STOP_LOSS", "TAKE_PROFIT"):
            exits.add((pd.Timestamp(e["bar_close"]), e["symbol"], action.lower()))
    return checks, exits, events, per_cycle_scans, fake


def same_decision(h: dict, b: dict) -> bool:
    if h["allowed"] != b["allowed"]:
        return False
    for key in ("equity", "entry", "stop", "rr"):
        if not np.isclose(h[key], b[key], rtol=1e-9, atol=0):
            return False
    if h["allowed"]:
        return bool(np.isclose(h["quote"], b["quote"], rtol=1e-9, atol=1e-9))
    if _reason_key(h["reason"]) == _reason_key(b["reason"]):
        return True
    # Float residue: the same "nothing left" decision worded two ways (see module docstring).
    reasons = {h["reason"].split(" (")[0].split(";")[0], b["reason"].split(" (")[0].split(";")[0]}
    return any(r.startswith(NO_ROOM) for r in reasons) and any(BELOW_MIN in r for r in reasons) and all(
        r.startswith(NO_ROOM) or _phantom_size(r) < PHANTOM_USD for r in reasons
    )


def _reason_key(reason: str) -> str:
    """The decision part of a rejection reason, without clock-dependent text: the rate cap's
    "; next slot opens in <timedelta>" (the harness passes `now` as a pandas Timestamp, so it
    prints "0 days 02:00:00"; the bot uses datetime and prints "2:00:00", one minute later) and
    the cooldown's "<timedelta> remaining"."""
    key = reason.split(";")[0]
    if key.startswith("cooldown active for "):
        key = key.split(":")[0]
    return key


def _phantom_size(reason: str) -> float:
    # "sized order 9.094947e-13 is below the minimum order size 0.01 ..."
    try:
        return float(reason.split("sized order ")[1].split(" ")[0])
    except (IndexError, ValueError):
        return float("inf")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(htf_filter, "_cache", {})
    monkeypatch.setattr("src.config.DEFAULT_BAR_DECISIONS_PATH", str(tmp_path / "default_bar_decisions.json"))
    monkeypatch.setattr("src.risk_engine.datetime", _FakeDatetime)
    bot._reset_bar_parity_state()
    yield
    bot._reset_bar_parity_state()


def test_live_loop_matches_p6_harness_exactly_on_gapless_zero_fee_data(tmp_path, monkeypatch):
    n_bars = 719 if FULL else DEFAULT_BARS
    rows, axis = load_rows(n_bars, gapless=True)
    h_checks, h_exits, res = run_harness(rows, fee=0.0, min_order=PHANTOM_USD, tmp=tmp_path / "h")
    b_checks, b_exits, events, per_cycle_scans, fake = run_bot(
        rows, axis, fee=0.0, min_order=PHANTOM_USD, history_bars=max(n_bars + 5, 200), tmp=tmp_path / "b", monkeypatch=monkeypatch
    )
    H = {(c["bar"], c["symbol"]): c for c in h_checks}
    B = {(c["bar"], c["symbol"]): c for c in b_checks}
    assert len(H) == len(h_checks) and len(B) == len(b_checks)  # one check per symbol per bar
    assert set(H) == set(B), f"only harness: {sorted(set(H) - set(B))[:5]}, only bot: {sorted(set(B) - set(H))[:5]}"
    differing = [k for k in sorted(H) if not same_decision(H[k], B[k])]
    assert differing == [], [(k, H[k], B[k]) for k in differing[:3]]
    # Same processing order within each bar (P2): the per-bar sequence of checked symbols.
    assert [(c["bar"], c["symbol"]) for c in h_checks] == [(c["bar"], c["symbol"]) for c in b_checks]
    assert h_exits == b_exits

    # Bar parity really engaged: the second tick of every bar scanned nothing (P1), and the
    # htf gate never fetched (P5) -- every fetch was the market-data one.
    assert all(n == 0 for k, n in per_cycle_scans if k == 1)
    assert {limit for _sym, _tf, limit in fake.calls} == {max(n_bars + 5, 200) + 1}

    # Not vacuous.
    approvals = [c for c in h_checks if c["allowed"]]
    by_bar = pd.Series([c["bar"] for c in approvals]).value_counts()
    no_room = [c for c in h_checks if c["reason"].startswith(NO_ROOM) or BELOW_MIN in c["reason"]]
    exit_bars = {bar for bar, _s, _r in h_exits}
    assert len(approvals) >= 30
    assert (by_bar >= 2).any()  # same-bar approvals compete for exposure room (P2/P6)
    assert len(no_room) >= 20  # the exposure cap binds
    assert any(c["bar"] in exit_bars for c in h_checks)  # an exit and an entry check on one bar (P3)
    assert {r for _b, _s, r in h_exits} == {"stop_loss", "take_profit"}
    assert any("volatility" in c["reason"] for c in h_checks)


def _entry_mismatch(h_checks, b_checks) -> tuple[float, set, set]:
    h = {(c["bar"], c["symbol"]) for c in h_checks if c["allowed"]}
    b = {(c["bar"], c["symbol"]) for c in b_checks if c["allowed"]}
    return len(h ^ b) / max(1, len(h)), h, b


@pytest.mark.parametrize("floor", [1.0, 10.0])
def test_realistic_config_with_a_dust_floor_is_within_the_mismatch_budget(tmp_path, monkeypatch, floor):
    """Real opens (fill-price residual), the 0.10% paper fee, the live 200-bar history
    (indicator warm-up), with min_order_quote_usd = floor on both sides. Measured over all 719
    bars (docs/bot_bar_parity.md §4.2): $1 and $10 both give identical entries and exits, a 0%
    mismatch; sizes differ slightly (fill price, warm-up), decisions don't. Budget: the design's
    10% explained-mismatch budget (§5)."""
    n_bars = 719 if FULL else DEFAULT_BARS
    rows, axis = load_rows(n_bars, gapless=False)
    h_checks, h_exits, _ = run_harness(rows, fee=0.001, min_order=floor, tmp=tmp_path / "h")
    b_checks, b_exits, _, _, _ = run_bot(
        rows, axis, fee=0.001, min_order=floor, history_bars=200, tmp=tmp_path / "b", monkeypatch=monkeypatch
    )
    share, h_entries, b_entries = _entry_mismatch(h_checks, b_checks)
    assert len(h_entries) >= 30
    assert share <= REALISTIC_ENTRY_MISMATCH_BUDGET, (share, sorted(h_entries ^ b_entries)[:5])
    assert len(h_exits ^ b_exits) <= REALISTIC_ENTRY_MISMATCH_BUDGET * max(1, len(h_exits))


def test_committed_config_divergence_is_a_float_phantom(tmp_path, monkeypatch):
    """The previously committed configuration (min_order_quote_usd: 0; config now ships 10, which
    test_realistic_config_with_a_dust_floor_is_within_the_mismatch_budget covers) with real opens, the 0.10% paper fee
    and the live 200-bar history. Bar parity counts same-bar fills at their gross quote like the
    harness, so the fee-slack dust that used to dominate (82% entry mismatch over 719 bars) is
    gone; what is left is float residue: when room is used up exactly, it comes out at ~1e-12 USD
    on one side and 0 on the other (different summation order), and with no order floor that
    phantom is approved as a full plan slot. Over 719 bars this still exceeds the 10% budget
    (13% entries; docs/bot_bar_parity.md §4.2), which is why a small min_order floor remains
    advisable. This test pins that the FIRST divergence is exactly such a phantom (never a
    structural difference) and that every decision before it agrees."""
    n_bars = 719 if FULL else DEFAULT_BARS
    rows, axis = load_rows(n_bars, gapless=False)
    h_checks, _, _ = run_harness(rows, fee=0.001, min_order=0.0, tmp=tmp_path / "h")
    b_checks, _, _, _, _ = run_bot(
        rows, axis, fee=0.001, min_order=0.0, history_bars=200, tmp=tmp_path / "b", monkeypatch=monkeypatch
    )
    H = {(c["bar"], c["symbol"]): c for c in h_checks}
    B = {(c["bar"], c["symbol"]): c for c in b_checks}
    keys = sorted(set(H) | set(B))
    first = next((k for k in keys if k not in H or k not in B or H[k]["allowed"] != B[k]["allowed"]), None)
    assert first is not None  # known not to match without a floor: that is the finding
    h, b = H.get(first), B.get(first)
    assert h is not None and b is not None, (first, h, b)
    approved = b if b["allowed"] else h
    assert approved["allowed"] and 0 < approved["quote"] < 1e-6, (first, h, b)  # a float phantom
    for k in keys:
        if k >= first:
            break
        assert H[k]["allowed"] == B[k]["allowed"], k


def test_same_decision_ignores_only_clock_dependent_reason_text():
    base = dict(allowed=False, equity=1.0, entry=1.0, stop=0.9, rr=2.0, quote=None)
    cap = "new-entry rate cap hit: 5 new entries already approved across all symbols in the last 120 minutes (max 5)"
    h = {**base, "reason": cap + "; next slot opens in 0 days 02:00:00"}
    b = {**base, "reason": cap + "; next slot opens in 1:59:00"}
    assert same_decision(h, b)
    assert not same_decision(h, {**base, "reason": "market volatility too high (9.00% > max 8.00%); skipping"})
    assert same_decision({**base, "reason": "cooldown active for X/USD: 0 days 00:59:00 remaining"},
                         {**base, "reason": "cooldown active for X/USD: 0:58:00 remaining"})
    assert not same_decision({**base, "reason": "cooldown active for X/USD: 1:00:00 remaining"},
                             {**base, "reason": "cooldown active for Y/USD: 1:00:00 remaining"})
