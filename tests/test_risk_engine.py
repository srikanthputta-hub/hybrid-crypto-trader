from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.risk_engine import RiskEngine


def make_engine(tmp_path: Path, **overrides) -> RiskEngine:
    defaults = dict(
        starting_cash_usd=10000,
        max_position_pct=0.20,
        max_total_exposure_pct=0.50,
        max_loss_per_trade_pct=0.01,
        max_daily_loss_pct=0.05,
        max_volatility_pct=0.08,
        min_risk_reward=1.5,
        cooldown_minutes=60,
        kill_switch_path=tmp_path / "KILL_SWITCH",
        symbol_whitelist=["BTC/USDT", "ETH/USDT"],
    )
    defaults.update(overrides)
    return RiskEngine(**defaults)


def test_rejects_symbol_not_in_whitelist(tmp_path):
    engine = make_engine(tmp_path)
    decision = engine.check_new_trade("DOGE/USDT", "buy", 10000, 10000)
    assert not decision.allowed
    assert "whitelist" in decision.reason


def test_kill_switch_blocks_new_buys(tmp_path):
    engine = make_engine(tmp_path)
    engine.kill_switch_path.touch()
    decision = engine.check_new_trade("BTC/USDT", "buy", 10000, 10000)
    assert not decision.allowed
    assert "kill switch" in decision.reason


def test_kill_switch_never_blocks_sells(tmp_path):
    engine = make_engine(tmp_path)
    engine.kill_switch_path.touch()
    decision = engine.check_new_trade("BTC/USDT", "sell", 10000, 0)
    assert decision.allowed


def test_daily_loss_limit_blocks_new_buys(tmp_path):
    engine = make_engine(tmp_path)
    now = datetime.now(timezone.utc)
    engine.check_new_trade("BTC/USDT", "buy", 10000, 10000, now=now)  # seeds day-start equity
    decision = engine.check_new_trade("BTC/USDT", "buy", 9400, 9400, now=now)  # -6% > 5% limit
    assert not decision.allowed
    assert "daily loss limit" in decision.reason


def test_cooldown_blocks_rapid_repeat_trades(tmp_path):
    engine = make_engine(tmp_path, cooldown_minutes=60)
    now = datetime.now(timezone.utc)
    engine.check_new_trade("BTC/USDT", "buy", 10000, 10000, now=now)
    engine.record_trade("BTC/USDT", now=now)
    decision = engine.check_new_trade("BTC/USDT", "buy", 10000, 10000, now=now + timedelta(minutes=5))
    assert not decision.allowed
    assert "cooldown" in decision.reason


def test_high_volatility_blocks_new_entries(tmp_path):
    engine = make_engine(tmp_path, max_volatility_pct=0.05)
    decision = engine.check_new_trade("BTC/USDT", "buy", 10000, 10000, atr_pct=0.10)
    assert not decision.allowed
    assert "volatility" in decision.reason


def test_poor_risk_reward_is_rejected(tmp_path):
    engine = make_engine(tmp_path, min_risk_reward=2.0)
    decision = engine.check_new_trade("BTC/USDT", "buy", 10000, 10000, risk_reward=1.2)
    assert not decision.allowed
    assert "risk/reward" in decision.reason


def test_position_sized_by_per_trade_risk_budget(tmp_path):
    # equity=10000, max_loss_per_trade_pct=0.01 -> risk budget = $100.
    # entry=50000, stop=49000 -> stop distance = 2% -> risk-sized spend = 100 / 0.02 = $5000.
    engine = make_engine(tmp_path, max_loss_per_trade_pct=0.01, max_position_pct=0.99)
    decision = engine.check_new_trade(
        "BTC/USDT", "buy", portfolio_equity=10000, cash_available=10000,
        entry_price=50000, stop_loss=49000,
    )
    assert decision.allowed
    assert decision.max_quote_amount == pytest.approx(5000.0, rel=0.01)


def test_total_exposure_cap_limits_spend(tmp_path):
    # equity=10000, max_total_exposure_pct=0.50 -> max total exposure = $5000.
    # already $4500 committed to other positions -> only $500 of room left.
    engine = make_engine(tmp_path, max_total_exposure_pct=0.50, max_position_pct=0.99)
    decision = engine.check_new_trade(
        "BTC/USDT", "buy", portfolio_equity=10000, cash_available=10000,
        open_positions_value=4500,
    )
    assert decision.allowed
    assert decision.max_quote_amount == pytest.approx(500.0, rel=0.01)


def test_exposure_cap_already_exhausted_rejects_trade(tmp_path):
    engine = make_engine(tmp_path, max_total_exposure_pct=0.50)
    decision = engine.check_new_trade(
        "BTC/USDT", "buy", portfolio_equity=10000, cash_available=10000,
        open_positions_value=5000,
    )
    assert not decision.allowed


def test_sell_never_blocked_by_any_check(tmp_path):
    engine = make_engine(tmp_path)
    decision = engine.check_new_trade("NOT/WHITELISTED", "sell", 10000, 0)
    assert decision.allowed


# --- volatility-scaled position sizing (soft taper below the hard volatility gate) ---
#
# Shared scenario: equity=10000, max_loss_per_trade_pct=0.01 -> base risk budget $100;
# entry=50000, stop=49000 -> 2% stop distance -> unscaled risk-sized spend = $5000.
# max_position_pct=0.99 and max_total_exposure_pct=0.99 so the risk-based sizing is the
# binding constraint (otherwise a flat cap would mask the taper entirely).
# max_volatility_pct=0.08 -> taper starts at 0.04, reaches volatility_scale_floor (0.5) at 0.08.

UNSCALED_RISK_SPEND = 5000.0


def _vol_engine(tmp_path, **overrides):
    params = dict(max_loss_per_trade_pct=0.01, max_position_pct=0.99, max_total_exposure_pct=0.99,
                  max_volatility_pct=0.08)
    params.update(overrides)
    return make_engine(tmp_path, **params)


def _vol_trade(engine, atr_pct):
    return engine.check_new_trade(
        "BTC/USDT", "buy", portfolio_equity=10000, cash_available=10000,
        entry_price=50000, stop_loss=49000, atr_pct=atr_pct,
    )


def test_pre_upgrade_risk_sizing_unchanged_without_atr_pct(tmp_path):
    # Backward-compat regression: hand-recompute the exact pre-upgrade number for the
    # test_position_sized_by_per_trade_risk_budget scenario (no atr_pct passed).
    # risk budget = 10000 * 0.01 = 100; stop distance = (50000 - 49000) / 50000 = 0.02;
    # spend = min(10000 * 0.99, cash 10000, 100 / 0.02, 10000 * 0.50 - 0) = min(9900, 10000, 5000, 5000) = 5000.
    engine = make_engine(tmp_path, max_loss_per_trade_pct=0.01, max_position_pct=0.99)
    decision = engine.check_new_trade(
        "BTC/USDT", "buy", portfolio_equity=10000, cash_available=10000,
        entry_price=50000, stop_loss=49000,
    )
    assert decision.allowed
    assert engine.volatility_scale_floor == 0.5  # new default
    assert decision.max_quote_amount == pytest.approx(5000.0, abs=1e-9)


@pytest.mark.parametrize("atr_pct", [0.0, 0.01, 0.03, 0.04])  # 0.04 == exactly half of max_volatility_pct
def test_low_volatility_sizes_identically_to_no_atr_baseline(tmp_path, atr_pct):
    engine = _vol_engine(tmp_path)
    baseline = _vol_trade(engine, atr_pct=None)
    decision = _vol_trade(engine, atr_pct=atr_pct)
    assert baseline.allowed and decision.allowed
    assert baseline.max_quote_amount == pytest.approx(UNSCALED_RISK_SPEND, abs=1e-9)
    assert decision.max_quote_amount == baseline.max_quote_amount


def test_high_but_allowed_volatility_shrinks_position(tmp_path):
    # atr_pct=0.072 is 90% of max_volatility_pct (still below the hard gate, so still allowed).
    # frac = (0.072 - 0.04) / (0.08 - 0.04) = 0.8 -> scale = 1 - 0.8 * 0.5 = 0.6 -> spend = 3000.
    engine = _vol_engine(tmp_path)
    baseline = _vol_trade(engine, atr_pct=None)
    low = _vol_trade(engine, atr_pct=0.01)
    decision = _vol_trade(engine, atr_pct=0.072)
    assert decision.allowed
    assert decision.max_quote_amount < baseline.max_quote_amount
    assert decision.max_quote_amount < low.max_quote_amount
    assert decision.max_quote_amount == pytest.approx(3000.0, rel=1e-9)


def test_position_size_non_increasing_as_volatility_rises(tmp_path):
    engine = _vol_engine(tmp_path)
    atr_levels = [0.02, 0.04, 0.05, 0.06, 0.07, 0.079, 0.08]
    sizes = []
    for atr_pct in atr_levels:
        decision = _vol_trade(engine, atr_pct=atr_pct)
        assert decision.allowed, f"atr_pct={atr_pct} should still clear the hard gate"
        sizes.append(decision.max_quote_amount)
    assert all(later <= earlier for earlier, later in zip(sizes, sizes[1:])), sizes
    assert sizes[-1] < sizes[0]


@pytest.mark.parametrize("floor", [0.0, 0.25, 0.5, 1.0])
def test_taper_never_overshoots_floor_near_hard_cap(tmp_path, floor):
    engine = _vol_engine(tmp_path, volatility_scale_floor=floor)
    for atr_pct in (0.0799, 0.079999999, 0.08):
        scale = engine._volatility_scale(atr_pct)
        assert floor <= scale <= 1.0
        decision = _vol_trade(engine, atr_pct=atr_pct)
        if floor == 0.0 and atr_pct == 0.08:
            # a zero floor sizes to nothing at the cap -> "no room" rejection, never a bigger trade
            assert not decision.allowed
            continue
        assert decision.allowed
        assert decision.max_quote_amount >= floor * UNSCALED_RISK_SPEND - 1e-9
        assert decision.max_quote_amount <= UNSCALED_RISK_SPEND
    # the helper itself stays bounded even for inputs the hard gate would normally reject first
    assert engine._volatility_scale(0.5) == floor
    assert engine._volatility_scale(-0.1) == 1.0


def test_hard_volatility_gate_still_rejects_above_cap_with_taper(tmp_path):
    engine = _vol_engine(tmp_path)
    decision = _vol_trade(engine, atr_pct=0.0801)
    assert not decision.allowed
    assert "volatility" in decision.reason


def test_nan_atr_pct_never_bypasses_risk_sizing(tmp_path):
    # Fail-closed: a NaN atr_pct (e.g. ATR not yet warmed up) must NOT produce a NaN risk
    # budget — min(spend, nan) silently returns spend, which would drop the per-trade risk
    # cap and size the trade at the flat max_position_pct ceiling ($9900 here).
    # Instead it is treated as maximally uncertain and sized at the floor.
    engine = _vol_engine(tmp_path)
    decision = _vol_trade(engine, atr_pct=float("nan"))
    assert decision.allowed
    assert decision.max_quote_amount == pytest.approx(0.5 * UNSCALED_RISK_SPEND, rel=1e-9)


def test_non_finite_max_volatility_pct_scales_to_floor(tmp_path):
    engine = _vol_engine(tmp_path, max_volatility_pct=float("nan"))
    assert engine._volatility_scale(0.01) == engine.volatility_scale_floor


@pytest.mark.parametrize("bad_floor", [1.5, -0.1, float("nan"), float("inf")])
def test_nonsensical_volatility_scale_floor_is_rejected(tmp_path, bad_floor):
    # A floor > 1 would *enlarge* positions; a negative/non-finite floor makes no sense.
    # Refuse to construct the engine rather than guess.
    with pytest.raises(ValueError):
        _vol_engine(tmp_path, volatility_scale_floor=bad_floor)


# --- global new-entry rate cap (correlated-burst guard) ---
#
# Motivating incident (2026-09-29, live paper bot): 11 different symbols were approved
# for new long entries between 11:10 and 12:47 UTC on one market-wide pullback signal,
# then 9 were stopped out together a few hours later. cooldown_minutes is per-symbol and
# cannot see that; this cap counts new entries across ALL symbols in a rolling window.

CAP_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "ADA/USDT", "DOT/USDT", "AVAX/USDT"]
T0 = datetime(2026, 9, 29, 11, 10, tzinfo=timezone.utc)


def _cap_engine(tmp_path, **overrides):
    params = dict(symbol_whitelist=CAP_SYMBOLS, max_new_entries_per_window=3, new_entry_window_minutes=120)
    params.update(overrides)
    return make_engine(tmp_path, **params)


def _buy(engine, symbol, now):
    return engine.check_new_trade(symbol, "buy", portfolio_equity=10000, cash_available=10000, now=now)


def _approve_and_record(engine, symbol, now):
    decision = _buy(engine, symbol, now)
    if decision.allowed:
        engine.record_trade(symbol, now=now)
    return decision


def test_entry_beyond_global_cap_within_window_is_rejected(tmp_path):
    # Three DIFFERENT symbols accepted within 20 minutes; a fourth, never-traded symbol
    # (so no per-symbol cooldown applies) must be rejected by the global cap.
    engine = _cap_engine(tmp_path)
    for i, sym in enumerate(["BTC/USDT", "ETH/USDT", "SOL/USDT"]):
        engine.record_trade(sym, now=T0 + timedelta(minutes=10 * i))
    decision = _buy(engine, "ADA/USDT", T0 + timedelta(minutes=30))
    assert not decision.allowed
    assert decision.max_quote_amount is None
    assert "new-entry rate cap" in decision.reason
    assert "3 new entries" in decision.reason
    assert "last 120 minutes" in decision.reason
    assert "max 3" in decision.reason
    # oldest entry was at T0, window is 120 min, now is T0+30 -> next slot in 90 minutes
    assert "next slot opens in 1:30:00" in decision.reason


def test_entries_below_global_cap_are_all_approved(tmp_path):
    engine = _cap_engine(tmp_path)
    decisions = [
        _approve_and_record(engine, sym, T0 + timedelta(minutes=i))
        for i, sym in enumerate(["BTC/USDT", "ETH/USDT", "SOL/USDT"])
    ]
    assert all(d.allowed for d in decisions), [d.reason for d in decisions]
    assert all(d.max_quote_amount and d.max_quote_amount > 0 for d in decisions)
    # ...and the very next distinct symbol is the first one refused.
    fourth = _approve_and_record(engine, "ADA/USDT", T0 + timedelta(minutes=3))
    assert not fourth.allowed
    assert "new-entry rate cap" in fourth.reason


def test_replays_the_2026_09_29_burst(tmp_path):
    # The actual live entry times from the incident (minutes after 11:10 UTC). With
    # N=3 per 120 min only the first three are approved; the rest are refused.
    offsets = {"AVAX/USDT": 0, "ETH/USDT": 23, "ALGO/USDT": 28, "ADA/USDT": 34, "DOT/USDT": 34,
               "ATOM/USDT": 74, "XTZ/USDT": 74, "SHIB/USDT": 74, "XRP/USDT": 97, "SOL/USDT": 97,
               "DOGE/USDT": 97}
    engine = _cap_engine(tmp_path, symbol_whitelist=list(offsets))
    approved = [sym for sym, m in offsets.items()
                if _approve_and_record(engine, sym, T0 + timedelta(minutes=m)).allowed]
    assert approved == ["AVAX/USDT", "ETH/USDT", "ALGO/USDT"]


def test_entry_is_approved_again_once_window_has_passed(tmp_path):
    engine = _cap_engine(tmp_path)
    for i, sym in enumerate(["BTC/USDT", "ETH/USDT", "SOL/USDT"]):
        engine.record_trade(sym, now=T0 + timedelta(minutes=10 * i))
    # One second before the oldest entry ages out: still capped.
    assert not _buy(engine, "ADA/USDT", T0 + timedelta(minutes=120) - timedelta(seconds=1)).allowed
    # Exactly when the oldest entry is a full window old it no longer counts
    # (same `elapsed < window` boundary as cooldown_minutes): one slot frees up.
    assert _approve_and_record(engine, "ADA/USDT", T0 + timedelta(minutes=120)).allowed
    # ...but only one slot: the 2nd/3rd original entries plus ADA fill the window again.
    blocked = _buy(engine, "DOT/USDT", T0 + timedelta(minutes=121))
    assert not blocked.allowed
    assert "new-entry rate cap" in blocked.reason
    # Long after everything has aged out, entries flow again.
    assert _buy(engine, "DOT/USDT", T0 + timedelta(hours=6)).allowed


def test_global_cap_never_blocks_sells(tmp_path):
    engine = _cap_engine(tmp_path)
    for i, sym in enumerate(["BTC/USDT", "ETH/USDT", "SOL/USDT"]):
        engine.record_trade(sym, now=T0 + timedelta(minutes=i))
    now = T0 + timedelta(minutes=5)
    assert not _buy(engine, "ADA/USDT", now).allowed  # cap is active right now...
    for sym in ["BTC/USDT", "ETH/USDT", "SOL/USDT", "ADA/USDT"]:
        decision = engine.check_new_trade(sym, "sell", 10000, 0, now=now)
        assert decision.allowed  # ...and closing any position is still always allowed
        assert decision.reason == "ok"


def test_checks_alone_do_not_consume_cap_slots(tmp_path):
    # Only an ACCEPTED plan (record_trade) consumes a slot. An approval that is then
    # vetoed downstream (e.g. by the Claude advisor) never reaches record_trade, so it
    # must not eat into the cap.
    engine = _cap_engine(tmp_path)
    for i in range(10):
        assert _buy(engine, CAP_SYMBOLS[i % len(CAP_SYMBOLS)], T0 + timedelta(seconds=i)).allowed


def test_reapproving_a_fill_does_not_double_count(tmp_path):
    # src/bot.py calls record_trade once when a plan is accepted and again when a
    # deferred PENDING plan fills; the fill-time call passes new_entry=False so one
    # plan only ever consumes one slot.
    engine = _cap_engine(tmp_path)
    engine.record_trade("BTC/USDT", now=T0)  # plan accepted
    engine.record_trade("BTC/USDT", now=T0 + timedelta(minutes=5), new_entry=False)  # same plan fills
    engine.record_trade("ETH/USDT", now=T0 + timedelta(minutes=6))
    assert _buy(engine, "SOL/USDT", T0 + timedelta(minutes=7)).allowed  # 2 of 3 slots used
    # The fill-time call still refreshes the per-symbol cooldown, exactly as before.
    cooldown = _buy(engine, "BTC/USDT", T0 + timedelta(minutes=64))
    assert not cooldown.allowed and "cooldown" in cooldown.reason


def test_record_trade_counts_by_default(tmp_path):
    # Fail-closed default: a caller that doesn't know about new_entry over-counts.
    engine = _cap_engine(tmp_path, max_new_entries_per_window=1)
    engine.record_trade("BTC/USDT", now=T0)
    assert not _buy(engine, "ETH/USDT", T0 + timedelta(minutes=1)).allowed


def test_future_dated_entry_still_holds_a_slot(tmp_path):
    # A recorded time later than `now` (clock step / out-of-order data) must never be
    # treated as "outside the window".
    engine = _cap_engine(tmp_path, max_new_entries_per_window=1)
    engine.record_trade("BTC/USDT", now=T0 + timedelta(hours=1))
    decision = _buy(engine, "ETH/USDT", T0)
    assert not decision.allowed
    assert "new-entry rate cap" in decision.reason


def test_out_of_order_records_keep_the_newest_entries(tmp_path):
    engine = _cap_engine(tmp_path, max_new_entries_per_window=2)
    engine.record_trade("BTC/USDT", now=T0 + timedelta(minutes=100))
    engine.record_trade("ETH/USDT", now=T0 + timedelta(minutes=90))
    engine.record_trade("SOL/USDT", now=T0 - timedelta(days=1))  # stale, recorded last
    # The two recent entries must still both count; the stale one must not evict either.
    assert not _buy(engine, "ADA/USDT", T0 + timedelta(minutes=101)).allowed


def test_global_cap_defaults(tmp_path):
    engine = make_engine(tmp_path)
    assert engine.max_new_entries_per_window == 5
    assert engine.new_entry_window_minutes == 120


@pytest.mark.parametrize("bad_count", [0, -1, 2.5, 3.0, True, None, "3", float("nan")])
def test_nonsensical_max_new_entries_per_window_is_rejected(tmp_path, bad_count):
    with pytest.raises(ValueError):
        _cap_engine(tmp_path, max_new_entries_per_window=bad_count)


@pytest.mark.parametrize("bad_window", [0, -5, float("nan"), float("inf"), True, None, "120", 1e15,
                                        1e-12])  # 1e-12 min rounds to timedelta(0) -> would silently disable the cap
def test_nonsensical_new_entry_window_minutes_is_rejected(tmp_path, bad_window):
    with pytest.raises(ValueError):
        _cap_engine(tmp_path, new_entry_window_minutes=bad_window)


def test_fractional_window_minutes_is_accepted(tmp_path):
    engine = _cap_engine(tmp_path, max_new_entries_per_window=1, new_entry_window_minutes=0.5)
    engine.record_trade("BTC/USDT", now=T0)
    assert not _buy(engine, "ETH/USDT", T0 + timedelta(seconds=29)).allowed
    assert _buy(engine, "ETH/USDT", T0 + timedelta(seconds=30)).allowed


# --- restoring the window after a process restart ---

def test_seeded_entries_block_like_recorded_ones(tmp_path):
    engine = _cap_engine(tmp_path)
    now = T0 + timedelta(minutes=30)
    engine.seed_recent_entries(
        [(T0 + timedelta(minutes=m)).isoformat() for m in (0, 10, 20)], now=now
    )
    decision = _buy(engine, "ADA/USDT", now)
    assert not decision.allowed
    assert "new-entry rate cap" in decision.reason


def test_seeded_entries_outside_window_do_not_block(tmp_path):
    engine = _cap_engine(tmp_path)
    now = T0 + timedelta(days=2)
    engine.seed_recent_entries([T0, T0 + timedelta(minutes=1), (T0 + timedelta(minutes=2)).isoformat()], now=now)
    assert _buy(engine, "ADA/USDT", now).allowed


def test_seeding_does_not_start_per_symbol_cooldowns(tmp_path):
    engine = _cap_engine(tmp_path)
    engine.seed_recent_entries([T0], now=T0)
    assert _buy(engine, "BTC/USDT", T0 + timedelta(minutes=1)).allowed


@pytest.mark.parametrize("garbage", [None, "", "not-a-timestamp", "2026-09-29T11:10:00", datetime(2026, 9, 29, 11, 10), 12345])
def test_unparseable_or_naive_seed_counts_as_now(tmp_path, garbage):
    # Fail closed: an entry whose time can't be trusted is treated as just made.
    engine = _cap_engine(tmp_path, max_new_entries_per_window=1)
    now = T0 + timedelta(days=30)
    engine.seed_recent_entries([garbage], now=now)
    assert not _buy(engine, "ETH/USDT", now + timedelta(minutes=119)).allowed
    assert _buy(engine, "ETH/USDT", now + timedelta(minutes=120)).allowed


def test_future_seed_is_clamped_to_now(tmp_path):
    # A far-future created_at must hold a slot for one window from now, not forever.
    engine = _cap_engine(tmp_path, max_new_entries_per_window=1)
    engine.seed_recent_entries([datetime(2099, 1, 1, tzinfo=timezone.utc)], now=T0)
    assert not _buy(engine, "ETH/USDT", T0 + timedelta(minutes=119)).allowed
    assert _buy(engine, "ETH/USDT", T0 + timedelta(minutes=120)).allowed
