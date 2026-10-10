"""test_same_bar_exposure.py - P6 (docs/p6_harness_exposure_fix.md): simulate_portfolio's
`count_same_bar_approvals` option makes the harness enforce `risk.max_total_exposure_pct` and cash
across several approvals on ONE bar the way src/bot.py does (it fills each plan immediately, so the
next symbol in the same cycle sees less room and cash).

Hand-built frames: constant prices, hand-built bullish Signals, zero costs unless stated, so every
expected number below is plain arithmetic on the risk config (written out next to each assert).

Run: pytest strategy_pack_run/test_same_bar_exposure.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _p in (_REPO_ROOT, _THIS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from engine import Costs  # noqa: E402

import backtest_live_bot as blb  # noqa: E402
from backtest_live_bot import placebo_run, simulate_portfolio  # noqa: E402
from src import trade_planner  # noqa: E402
from src.monitor import check_pending  # noqa: E402
from src.portfolio import Portfolio  # noqa: E402
from src.risk_engine import RiskEngine  # noqa: E402
from src.signal_engine import Signal  # noqa: E402
from src.trade_plan_store import PlanRecord  # noqa: E402

ZERO = Costs(0.0, 0.0)
EQUITY = 10_000.0

PLANNER_CFG = dict(
    entry_buffer_atr_mult=0.1,
    stop_atr_mult=1.0,
    invalidation_atr_mult=0.5,
    min_risk_reward=2.0,
    adx_trend_threshold=25,
    trend_stop_atr_bonus=0.3,
)

# Position cap binds (15% = $1500 at $10k); the per-trade risk budget is generous enough never to
# bind (50% of equity / ~2.2% stop distance); the 40% exposure cap is the one under test.
RISK_CFG = dict(
    starting_cash_usd=EQUITY,
    max_position_pct=0.15,
    max_total_exposure_pct=0.40,
    max_loss_per_trade_pct=0.5,
    max_daily_loss_pct=0.9,
    max_volatility_pct=1.0,
    min_risk_reward=1.0,
    cooldown_minutes=0,
    max_new_entries_per_window=50,
    new_entry_window_minutes=1,
)


def _idx(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2024-01-02", periods=n, freq="1D", tz="UTC")


def _sig(score: float = 0.8) -> Signal:
    return Signal(direction="bullish", primary_pattern="BREAKOUT", engine_score=score,
                  supporting_patterns=["BREAKOUT"], primary_details={}, rationale={})


def _flat_frame(n: int, price: float = 100.0, atr: float = 2.0) -> pd.DataFrame:
    idx = _idx(n)
    return pd.DataFrame(
        {"open": price, "high": price, "low": price, "close": price, "volume": 10.0, "atr": atr}, index=idx
    )


def _universe(symbols, n=6, prices=None, atrs=None):
    prices = prices or {}
    atrs = atrs or {}
    return {s: _flat_frame(n, prices.get(s, 100.0), atrs.get(s, 2.0)) for s in symbols}


def _run(eg, sigs, risk_cfg=RISK_CFG, costs=ZERO, **kw):
    return simulate_portfolio(eg, sigs, costs, risk_cfg, PLANNER_CFG, EQUITY, 365.0, _REPO_ROOT, **kw)


def _entries(res) -> dict[str, float]:
    """symbol -> quote actually spent at entry (zero costs: qty * entry_px)."""
    return {r.symbol: float(r.qty * r.entry_px) for r in res.trades.itertuples()}


def _capture_checks(fn):
    """Run fn() with RiskEngine.check_new_trade wrapped read-only; return (result, log)."""
    log: list[dict] = []
    original = RiskEngine.check_new_trade

    def wrapped(self, symbol, side, *args, **kwargs):
        d = original(self, symbol, side, *args, **kwargs)
        log.append(dict(symbol=symbol, now=kwargs.get("now"), allowed=d.allowed, quote=d.max_quote_amount,
                        open_value=kwargs["open_positions_value"], cash=kwargs["cash_available"],
                        equity=kwargs["portfolio_equity"], reason=d.reason))
        return d

    with patch.object(RiskEngine, "check_new_trade", wrapped):
        res = fn()
    return res, log


# --------------------------------------------------------------------------- #
# 1. Exposure room is consumed live-style within one bar
# --------------------------------------------------------------------------- #
def test_on_same_bar_approvals_never_exceed_the_exposure_cap():
    syms = ["A", "B", "C", "D"]
    eg = _universe(syms)
    sigs = {s: {eg[s].index[1]: _sig()} for s in syms}

    res = _run(eg, sigs, count_same_bar_approvals=True)
    got = _entries(res)
    # room = 40% * 10k = 4000: A 1500 (pos cap), B 1500, C min(1500, 4000-3000)=1000, D 0 -> rejected
    assert got == pytest.approx({"A": 1500.0, "B": 1500.0, "C": 1000.0})
    assert sum(got.values()) <= RISK_CFG["max_total_exposure_pct"] * EQUITY + 1e-9
    assert res.exposure.max() <= RISK_CFG["max_total_exposure_pct"] + 1e-12


def test_off_reproduces_the_legacy_over_commitment():
    """Non-regression of the default: every candidate saw the same room, so all four got 1500
    (60% of equity, over the 40% cap). This is the B1 behaviour, kept reachable on purpose."""
    syms = ["A", "B", "C", "D"]
    eg = _universe(syms)
    sigs = {s: {eg[s].index[1]: _sig()} for s in syms}
    implicit = _run(eg, sigs)
    explicit = _run(eg, sigs, count_same_bar_approvals=False)
    pd.testing.assert_frame_equal(implicit.trades, explicit.trades)
    pd.testing.assert_series_equal(implicit.equity, explicit.equity)
    pd.testing.assert_series_equal(implicit.exposure, explicit.exposure)
    assert _entries(implicit) == pytest.approx({s: 1500.0 for s in syms})
    assert implicit.exposure.max() == pytest.approx(0.60)


def test_later_risk_checks_on_the_bar_see_committed_value_and_reduced_cash():
    syms = ["A", "B", "C", "D"]
    eg = _universe(syms)
    sigs = {s: {eg[s].index[1]: _sig()} for s in syms}
    _, log = _capture_checks(lambda: _run(eg, sigs, count_same_bar_approvals=True))
    assert [e["symbol"] for e in log] == syms
    assert [e["open_value"] for e in log] == pytest.approx([0.0, 1500.0, 3000.0, 4000.0])
    assert [e["cash"] for e in log] == pytest.approx([10_000.0, 8500.0, 7000.0, 6000.0])
    assert [e["equity"] for e in log] == pytest.approx([EQUITY] * 4), "equity is not refreshed inside the bar (bot.py)"
    assert [e["allowed"] for e in log] == [True, True, True, False]
    assert "no room" in log[-1]["reason"]


# --------------------------------------------------------------------------- #
# 2. Cash is consumed within one bar
# RiskEngine (PR #96/#97) only accepts max_total_exposure_pct in (0, 1]. At the maximum, 1.0, the
# exposure room (equity - open value - committed) equals cash - committed, so cash and room bind
# together; the test asserts the cash_available actually passed, which is what P6 changes.
# --------------------------------------------------------------------------- #
def test_on_cash_is_consumed_within_the_bar_without_relying_on_the_fill_clip():
    syms = ["A", "B", "C", "D"]
    eg = _universe(syms)
    sigs = {s: {eg[s].index[1]: _sig()} for s in syms}
    cfg = {**RISK_CFG, "max_position_pct": 0.40, "max_total_exposure_pct": 1.0}

    _, log_on = _capture_checks(lambda: _run(eg, sigs, cfg, count_same_bar_approvals=True))
    # A 4000, B 4000, C min(4000, cash 2000)=2000, D cash 0 -> rejected
    assert [e["quote"] for e in log_on[:3]] == pytest.approx([4000.0, 4000.0, 2000.0])
    assert [e["cash"] for e in log_on] == pytest.approx([10_000.0, 6000.0, 2000.0, 0.0])
    assert log_on[3]["allowed"] is False

    res_off, log_off = _capture_checks(lambda: _run(eg, sigs, cfg))
    # legacy: every check sees the full 10k of cash and approves 4000; the excess is only clipped
    # at the next open (docstring note 5), and D's approved-but-unfunded plan silently vanishes.
    assert [e["cash"] for e in log_off] == pytest.approx([10_000.0] * 4)
    assert all(e["allowed"] for e in log_off)
    assert _entries(res_off) == pytest.approx({"A": 4000.0, "B": 4000.0, "C": 2000.0})


def test_on_fills_are_never_clipped_by_cash():
    """With the option on, every fill spends exactly what the risk engine approved."""
    syms = list("ABCDEFG")
    eg = _universe(syms, atrs={s: 1.0 + i for i, s in enumerate(syms)})
    sigs = {s: {eg[s].index[1]: _sig()} for s in syms}
    cfg = {**RISK_CFG, "max_position_pct": 0.30, "max_total_exposure_pct": 1.0}
    res, log = _capture_checks(lambda: _run(eg, sigs, cfg, count_same_bar_approvals=True))
    approved = {e["symbol"]: e["quote"] for e in log if e["allowed"]}
    assert len(approved) == 4  # 3000 + 3000 + 3000 + 1000: the bar's cash is fully used
    assert _entries(res) == pytest.approx(approved)
    assert sum(approved.values()) <= EQUITY + 1e-9


# --------------------------------------------------------------------------- #
# 3. Order: alphabetical by default, rank order when use_rank_order=True
# --------------------------------------------------------------------------- #
def test_alphabetical_order_decides_who_gets_the_room_regardless_of_dict_order():
    syms = ["D", "C", "B", "A"]  # insertion order deliberately reversed
    eg = _universe(syms)
    sigs = {s: {eg[s].index[1]: _sig()} for s in syms}
    got = _entries(_run(eg, sigs, count_same_bar_approvals=True))
    assert got == pytest.approx({"A": 1500.0, "B": 1500.0, "C": 1000.0})


def test_rank_order_decides_who_gets_the_room_when_enabled():
    syms = ["A", "B", "C", "D"]
    eg = _universe(syms)
    scores = {"A": 0.6, "B": 0.7, "C": 0.8, "D": 0.9}  # rank order: D, C, B, A
    sigs = {s: {eg[s].index[1]: _sig(scores[s])} for s in syms}
    got = _entries(_run(eg, sigs, use_rank_order=True, count_same_bar_approvals=True))
    assert got == pytest.approx({"D": 1500.0, "C": 1500.0, "B": 1000.0})


# --------------------------------------------------------------------------- #
# 4. The running total is per bar; exits do not free room on their own bar
# --------------------------------------------------------------------------- #
def test_committed_total_resets_each_bar():
    """Bar 1: A, B approved (1000 each at a 10% position cap). They fill at bar 2's open, so on
    bar 3 they are OPEN positions (2000) and must not ALSO be counted as bar-3 commitments."""
    syms = ["A", "B", "C", "D", "E"]
    eg = _universe(syms, n=8)
    idx = eg["A"].index
    sigs = {"A": {idx[1]: _sig()}, "B": {idx[1]: _sig()},
            "C": {idx[3]: _sig()}, "D": {idx[3]: _sig()}, "E": {idx[3]: _sig()}}
    cfg = {**RISK_CFG, "max_position_pct": 0.10}
    res = _run(eg, sigs, cfg, count_same_bar_approvals=True)
    # bar 3: room 4000 - open 2000 -> C 1000, D 1000, E 0
    assert _entries(res) == pytest.approx({"A": 1000.0, "B": 1000.0, "C": 1000.0, "D": 1000.0})


def test_exit_queued_on_the_bar_does_not_free_room_on_that_bar():
    """Design P3, unchanged by P6: X's stop-loss triggers on bar 3's close and only executes at
    bar 4's open, so a bar-3 candidate still sees X as held."""
    n = 7
    idx = _idx(n)
    x = _flat_frame(n)
    x.loc[idx[3], "close"] = 90.0  # below X's stop (planned on bar 1 at 100, atr 2)
    eg = {"X": x, "A": _flat_frame(n), "B": _flat_frame(n), "C": _flat_frame(n)}
    sigs = {"X": {idx[1]: _sig()}, "A": {idx[3]: _sig()}, "B": {idx[3]: _sig()}, "C": {idx[3]: _sig()}}
    res, log = _capture_checks(lambda: _run(eg, sigs, count_same_bar_approvals=True))
    bar3 = [e for e in log if e["now"] == idx[3]]
    # X (qty 15, marked at the bar-3 close of 90 -> 1350) still counted. equity = 8500 + 1350 = 9850,
    # room = 0.40*9850 - 1350 = 2590: A min(0.15*9850=1477.5, 2590), B 2590-1477.5=1112.5, C none.
    assert [e["symbol"] for e in bar3] == ["A", "B", "C"]
    assert [e["open_value"] for e in bar3] == pytest.approx([1350.0, 2827.5, 3940.0])
    assert [e["quote"] for e in bar3] == pytest.approx([1477.5, 1112.5, None])
    assert res.trades.set_index("symbol").loc["X", "reason"] == "stop_loss"


def test_pending_plan_fill_trigger_counts_like_a_fresh_fill():
    """Live fills a PENDING plan the moment its FILL trigger fires (_execute_monitor_action), so a
    later symbol on that bar sees the room taken. Force A's planning-bar immediate check to say
    "not yet" so A is still PENDING on bar 2, where its FILL trigger fires before B is checked."""
    syms = ["A", "B"]
    eg = _universe(syms, n=6)
    idx = eg["A"].index
    sigs = {"A": {idx[1]: _sig()}, "B": {idx[2]: _sig()}}
    cfg = {**RISK_CFG, "max_position_pct": 0.10, "max_total_exposure_pct": 0.15}
    real = blb.check_pending
    calls = {"A": 0}

    def delayed(record, price):
        if record.symbol == "A":
            calls["A"] += 1
            if calls["A"] == 1:
                return None
        return real(record, price)

    with patch.object(blb, "check_pending", side_effect=delayed):
        on = _entries(_run(eg, sigs, cfg, count_same_bar_approvals=True))
    calls["A"] = 0
    with patch.object(blb, "check_pending", side_effect=delayed):
        off = _entries(_run(eg, sigs, cfg))
    assert on == pytest.approx({"A": 1000.0, "B": 500.0})  # room 1500 - A's 1000
    assert off == pytest.approx({"A": 1000.0, "B": 1000.0})


# --------------------------------------------------------------------------- #
# 5. Parity with a bot.py-style loop built from src RiskEngine + Portfolio
# --------------------------------------------------------------------------- #
PARITY_RISK = dict(
    starting_cash_usd=EQUITY,
    max_position_pct=0.15,
    max_total_exposure_pct=0.40,
    max_loss_per_trade_pct=0.004,
    max_daily_loss_pct=0.05,
    max_volatility_pct=0.08,
    min_risk_reward=1.5,
    cooldown_minutes=60,
    max_new_entries_per_window=5,
    new_entry_window_minutes=120,
)
PARITY_PRICES = {"A": 100.0, "B": 50.0, "C": 20.0, "D": 7.5, "E": 3.0, "F": 1.25, "G": 0.4, "H": 250.0, "Z": 40.0}
PARITY_ATR_PCT = {"A": 0.02, "B": 0.035, "C": 0.05, "D": 0.065, "E": 0.03, "F": 0.075, "G": 0.045, "H": 0.06, "Z": 0.03}


def _parity_setup():
    """Bar 1: Z alone (fills at bar 2's open, so on bar 3 it is a pre-existing position).
    Bar 3: A..H all signal; mixed price levels and ATR% so risk sizing, the volatility taper,
    the position cap and the exposure cap all take turns binding; 8 candidates also hit the
    5-per-window new-entry cap."""
    n = 6
    eg = {s: _flat_frame(n, PARITY_PRICES[s], PARITY_ATR_PCT[s] * PARITY_PRICES[s]) for s in PARITY_PRICES}
    idx = eg["A"].index
    sigs = {s: {idx[3]: _sig()} for s in "ABCDEFGH"}
    sigs["Z"] = {idx[1]: _sig()}
    return eg, sigs, idx


def _bot_style_reference(eg, sigs, idx, fee_pct: float) -> list[tuple[str, pd.Timestamp, bool, float | None]]:
    """Mirror of src/bot.py run_cycle lines ~492-563 for a bar sequence: equity computed once per
    cycle, other_positions_value from the real Portfolio's positions at current prices,
    cash_available = portfolio.cash, approve -> record_trade -> immediate FILL via apply_buy."""
    syms = sorted(eg)
    re = RiskEngine(kill_switch_path=_REPO_ROOT / "KILL_SWITCH_P6_TEST_DOES_NOT_EXIST", symbol_whitelist=syms,
                    **{k: v for k, v in PARITY_RISK.items()})
    pf = Portfolio(cash=EQUITY)
    out = []
    for t, ts in enumerate(idx):
        prices = {s: float(eg[s]["close"].iloc[t]) for s in syms}
        equity = pf.equity(prices)
        for s in syms:
            sig = sigs.get(s, {}).get(ts)
            if sig is None or s in pf.positions:
                continue
            price, atr = prices[s], float(eg[s]["atr"].iloc[t])
            plan = trade_planner.build_plan(sig, s, price, atr, **PLANNER_CFG)
            other = sum(p.amount * prices.get(o, p.entry_price) for o, p in pf.positions.items() if o != s)
            d = re.check_new_trade(s, "buy", portfolio_equity=equity, cash_available=pf.cash,
                                   entry_price=plan.entry_zone[1], stop_loss=plan.stop_loss,
                                   risk_reward=plan.risk_reward, open_positions_value=other,
                                   atr_pct=atr / price, now=ts)
            out.append((s, ts, d.allowed, d.max_quote_amount))
            if not d.allowed:
                continue
            plan_dict = dict(plan.__dict__, approved_quote_amount=d.max_quote_amount)
            re.record_trade(s, now=ts)
            if check_pending(PlanRecord(symbol=s, status="PENDING", plan=plan_dict), price).action == "FILL":
                pf.apply_buy(s, d.max_quote_amount, price, fee_pct)
    return out


def _harness_decisions(eg, sigs, flag: bool, costs: Costs = ZERO):
    _, log = _capture_checks(lambda: simulate_portfolio(
        eg, sigs, costs, PARITY_RISK, PLANNER_CFG, EQUITY, 365.0, _REPO_ROOT, count_same_bar_approvals=flag))
    return [(e["symbol"], e["now"], e["allowed"], e["quote"]) for e in log]


def _assert_same(a, b):
    assert [x[:3] for x in a] == [x[:3] for x in b]
    for x, y in zip(a, b):
        if x[3] is None or y[3] is None:
            assert x[3] is y[3]
        else:
            assert x[3] == pytest.approx(y[3], rel=1e-12, abs=1e-9)


def test_parity_with_bot_style_immediate_fill_at_zero_fee():
    eg, sigs, idx = _parity_setup()
    ref = _bot_style_reference(eg, sigs, idx, fee_pct=0.0)
    on = _harness_decisions(eg, sigs, True)
    # sanity: the scenario actually exercises the cap (some approvals squeezed, some rejected)
    bar3 = [r for r in ref if r[1] == idx[3]]
    assert any(not r[2] for r in bar3) and sum(r[2] for r in bar3) >= 3
    _assert_same(on, ref)


def test_parity_scenario_discriminates_the_legacy_harness():
    eg, sigs, idx = _parity_setup()
    ref = _bot_style_reference(eg, sigs, idx, fee_pct=0.0)
    off = _harness_decisions(eg, sigs, False)
    with pytest.raises(AssertionError):
        _assert_same(off, ref)


def test_harness_is_never_looser_than_bot_style_at_paper_fee():
    """Live's paper broker charges 0.10%, so its position is worth quote*(1-fee) and the next symbol
    sees slightly MORE room than the harness gives (the harness counts the gross quote). The harness
    gets the same 0.10% fee (no slippage) so Z's fill leaves identical equity on bar 3. Then: same
    symbols checked in the same order; every harness approval is also a live approval and is no
    larger, and smaller by at most 0.1% of the 40% cap; a live-only approval can only be fee-slack
    dust of at most that size."""
    eg, sigs, idx = _parity_setup()
    fee = 0.001
    ref = _bot_style_reference(eg, sigs, idx, fee_pct=fee)
    on = _harness_decisions(eg, sigs, True, Costs(fee, 0.0))
    assert [x[:2] for x in on] == [x[:2] for x in ref]
    slack = fee * PARITY_RISK["max_total_exposure_pct"] * EQUITY + 1e-9
    h_ok = {x[:2]: x[3] for x in on if x[2]}
    r_ok = {x[:2]: x[3] for x in ref if x[2]}
    assert set(h_ok) <= set(r_ok)
    for k, q in h_ok.items():
        assert r_ok[k] - slack <= q <= r_ok[k] + 1e-9
    for k in set(r_ok) - set(h_ok):
        assert r_ok[k] <= slack


# --------------------------------------------------------------------------- #
# 6. placebo_run forwards the flag
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("flag", [None, False, True])
def test_placebo_run_forwards_count_same_bar_approvals_to_every_draw(flag):
    syms = ["A", "B", "C", "D"]
    eg = _universe(syms, n=30)
    sigs = {s: {eg[s].index[5]: _sig()} for s in syms}
    real = blb.simulate_portfolio
    seen: list = []

    def spy(*args, **kwargs):
        seen.append(kwargs.get("count_same_bar_approvals"))
        return real(*args, **kwargs)

    extra = {} if flag is None else {"count_same_bar_approvals": flag}
    with patch.object(blb, "simulate_portfolio", side_effect=spy):
        placebo_run(eg, sigs, ZERO, RISK_CFG, PLANNER_CFG, EQUITY, 365.0, _REPO_ROOT, n_sims=4, seed=3, **extra)
    assert seen == [bool(flag)] * 4


# --------------------------------------------------------------------------- #
# 7. Real data (committed strategy_pack_run/data/, live config): the cap holds on every bar
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def kraken_daily():
    from run_live_bot_report import build_live_signals
    from src.config import load_config

    cfg = load_config()
    eg, sigs, _ = build_live_signals(str(_THIS_DIR / "data"), cfg)
    return cfg, eg, sigs


def _committed_fraction_by_bar(log) -> dict:
    """Per bar with >= 1 approval: (open value seen by the bar's first check + every approval on
    the bar) / equity -- i.e. what the bar commits once its fills land."""
    by_bar: dict = {}
    for e in log:
        by_bar.setdefault(e["now"], []).append(e)
    out = {}
    for now, es in by_bar.items():
        approved = sum(e["quote"] for e in es if e["allowed"])
        if approved > 0:
            out[now] = (es[0]["open_value"] + approved) / es[0]["equity"]
    return out


def test_real_kraken_daily_on_never_commits_past_the_exposure_cap(kraken_daily):
    from engine import COST_PRESETS

    cfg, eg, sigs = kraken_daily
    cap = float(cfg.risk["max_total_exposure_pct"])

    def run(flag):
        return _capture_checks(lambda: simulate_portfolio(
            eg, sigs, COST_PRESETS["kraken_T3_taker"], cfg.risk, cfg.trade_planner, EQUITY, 365.0, _REPO_ROOT,
            count_same_bar_approvals=flag))

    res_on, log_on = run(True)
    frac_on = _committed_fraction_by_bar(log_on)
    assert frac_on and max(frac_on.values()) <= cap + 1e-9
    approved_on = {(e["symbol"], e["now"]): e["quote"] for e in log_on if e["allowed"]}
    for tr in res_on.trades.itertuples():  # no fill is clipped by cash with the option on
        q = [v for (s, now), v in approved_on.items() if s == tr.symbol and now < tr.entry_time][-1]
        assert tr.qty * tr.entry_px / (1 - COST_PRESETS["kraken_T3_taker"].fee) == pytest.approx(q, rel=1e-9)

    _, log_off = run(False)
    frac_off = _committed_fraction_by_bar(log_off)
    assert max(frac_off.values()) > cap + 0.05, "the legacy harness over-commits on this data (B1)"
