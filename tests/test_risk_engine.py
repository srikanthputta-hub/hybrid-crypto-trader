from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.risk_engine import RiskDecision, RiskEngine


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
    # Fail-closed: a NaN atr_pct must NOT produce a NaN risk budget — min(spend, nan)
    # silently returns spend, which would drop the per-trade risk cap and size the trade
    # at the flat max_position_pct ceiling ($9900 here).
    #
    # Expectation changed by issue #87 (the OLD behaviour was the bug): this used to assert
    # the trade was ALLOWED, sized at the volatility floor. But the hard volatility gate
    # (`atr_pct > max_volatility_pct`) is False for NaN, so a NaN ATR silently bypassed
    # check 3 entirely -- an unmeasurable volatility could be above the cap. A NaN atr_pct
    # is now rejected outright. (With clean OHLCV data the scanner needs more bars than the
    # 14-bar ATR warm-up before it emits any candidate, so NaN only arrives via bad data.)
    engine = _vol_engine(tmp_path)
    decision = _vol_trade(engine, atr_pct=float("nan"))
    assert not decision.allowed
    assert decision.max_quote_amount is None
    assert "invalid risk input" in decision.reason and "atr_pct" in decision.reason
    # The sizing helper's own NaN guard stays in place as defence in depth.
    assert engine._volatility_scale(float("nan")) == engine.volatility_scale_floor


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


# --- runtime symbol whitelist replacement (set_symbol_whitelist, redesign P1b) ---
#
# The constructor's symbol_whitelist stays the starting whitelist (make_engine uses
# ["BTC/USDT", "ETH/USDT"]). set_symbol_whitelist() replaces it wholesale, e.g. on a
# universe refresh. It only gates whether a NEW position may be opened on a symbol:
# sells, sizing, cooldowns and the global new-entry window are untouched by it.

def _sized_buy(engine, symbol, **overrides):
    # A buy that exercises every sizing input, so any change to sizing would show up.
    params = dict(portfolio_equity=10000, cash_available=10000, entry_price=50000, stop_loss=49000,
                  risk_reward=2.0, open_positions_value=1000, atr_pct=0.06, now=T0)
    params.update(overrides)
    return engine.check_new_trade(symbol, "buy", **params)


def test_set_whitelist_rejects_symbol_not_in_new_whitelist(tmp_path):
    engine = make_engine(tmp_path)
    engine.set_symbol_whitelist(["SOL/USD"])
    decision = _sized_buy(engine, "BTC/USDT")  # was allowed by the constructor whitelist
    assert not decision.allowed
    assert decision.max_quote_amount is None
    assert decision.reason == "BTC/USDT is not in the configured symbol whitelist"


def test_set_whitelist_rejection_is_classified_as_whitelist_not_sizing(tmp_path):
    # The audit trail / counterfactual replay groups rejections by reason substring;
    # a runtime-whitelist rejection must land in the same bucket as the constructor's.
    from src.risk_counterfactual import classify_reason

    engine = make_engine(tmp_path)
    engine.set_symbol_whitelist(["SOL/USD"])
    assert classify_reason(_sized_buy(engine, "BTC/USDT").reason) == "symbol_whitelist"
    assert classify_reason(make_engine(tmp_path).check_new_trade("DOGE/USDT", "buy", 10000, 10000).reason) \
        == "symbol_whitelist"


def test_set_whitelist_member_decides_exactly_as_without_runtime_whitelist(tmp_path):
    # Same trade, same state: an engine whose whitelist came from set_symbol_whitelist
    # gives the identical RiskDecision (allowed, reason AND size) as one constructed
    # with that whitelist directly. The whitelist must never touch sizing.
    # max_position_pct=0.99 so the (volatility-scaled) risk-sized spend is what binds.
    reference = make_engine(tmp_path, max_position_pct=0.99, symbol_whitelist=["SOL/USD", "BTC/USDT"])
    engine = make_engine(tmp_path, max_position_pct=0.99)
    engine.set_symbol_whitelist(["SOL/USD", "BTC/USDT"])
    for symbol in ("SOL/USD", "BTC/USDT"):
        for overrides in ({}, dict(atr_pct=None), dict(open_positions_value=4900), dict(atr_pct=0.079)):
            expected = _sized_buy(reference, symbol, **overrides)
            actual = _sized_buy(engine, symbol, **overrides)
            assert actual == expected, (symbol, overrides)
    assert _sized_buy(engine, "SOL/USD").allowed
    # 1% of 10000 = 100 risk budget, scaled by atr_pct 0.06 (frac 0.5 -> 0.75) = 75, / 2% stop = 3750;
    # min(flat 9900, cash 10000, risk 3750, exposure room 5000 - 1000 = 4000) = 3750
    assert _sized_buy(engine, "SOL/USD").max_quote_amount == pytest.approx(3750.0, rel=1e-9)


def test_set_whitelist_replaces_rather_than_merges(tmp_path):
    engine = make_engine(tmp_path)  # constructor whitelist: BTC/USDT, ETH/USDT
    engine.set_symbol_whitelist(["SOL/USD", "ADA/USD"])
    assert not _sized_buy(engine, "BTC/USDT").allowed  # constructor-only symbol now refused
    assert _sized_buy(engine, "SOL/USD").allowed
    engine.set_symbol_whitelist(["ADA/USD", "XRP/USD"])
    old_only = _sized_buy(engine, "SOL/USD")
    assert not old_only.allowed and "symbol whitelist" in old_only.reason
    assert _sized_buy(engine, "ADA/USD").allowed  # in both
    assert _sized_buy(engine, "XRP/USD").allowed  # new-only
    assert engine.symbol_whitelist == frozenset({"ADA/USD", "XRP/USD"})


def test_set_whitelist_accepts_one_shot_iterables(tmp_path):
    engine = make_engine(tmp_path)
    engine.set_symbol_whitelist(sym for sym in ["SOL/USD", "ADA/USD"])
    assert engine.symbol_whitelist == frozenset({"SOL/USD", "ADA/USD"})
    assert _sized_buy(engine, "ADA/USD").allowed


def test_empty_whitelist_blocks_every_new_buy_but_never_a_sell(tmp_path):
    engine = make_engine(tmp_path)
    engine.set_symbol_whitelist([])
    for symbol in ("BTC/USDT", "ETH/USDT", "SOL/USD"):
        assert not _sized_buy(engine, symbol).allowed
        sell = engine.check_new_trade(symbol, "sell", 10000, 0, now=T0)
        assert sell.allowed and sell.reason == "ok"


def test_symbol_dropped_from_whitelist_can_still_be_sold(tmp_path):
    # A held position whose symbol leaves the universe must always be closable,
    # including while the kill switch is engaged.
    engine = make_engine(tmp_path)
    engine.set_symbol_whitelist(["SOL/USD"])
    assert engine.check_new_trade("BTC/USDT", "sell", 10000, 0, now=T0).allowed
    engine.kill_switch_path.touch()
    assert engine.check_new_trade("BTC/USDT", "sell", 10000, 0, now=T0).allowed


def test_kill_switch_still_takes_precedence_over_runtime_whitelist(tmp_path):
    engine = make_engine(tmp_path)
    engine.set_symbol_whitelist(["SOL/USD"])
    engine.kill_switch_path.touch()
    for symbol in ("SOL/USD", "BTC/USDT"):
        decision = _sized_buy(engine, symbol)
        assert not decision.allowed and "kill switch" in decision.reason


def test_dropping_and_readding_a_symbol_does_not_reset_its_cooldown(tmp_path):
    # Fail closed: churning a symbol out of and back into the whitelist must not hand
    # it a fresh cooldown, nor free its slot in the global new-entry window.
    engine = make_engine(tmp_path, cooldown_minutes=60, max_new_entries_per_window=2)
    engine.record_trade("BTC/USDT", now=T0)
    engine.record_trade("ETH/USDT", now=T0)
    engine.set_symbol_whitelist(["SOL/USD"])
    engine.set_symbol_whitelist(["BTC/USDT", "ETH/USDT", "SOL/USD"])
    cooldown = _sized_buy(engine, "BTC/USDT", now=T0 + timedelta(minutes=5))
    assert not cooldown.allowed and "cooldown" in cooldown.reason
    capped = _sized_buy(engine, "SOL/USD", now=T0 + timedelta(minutes=5))
    assert not capped.allowed and "new-entry rate cap" in capped.reason


def test_whitelist_rejection_has_no_side_effects(tmp_path):
    # A whitelist-refused check must not start a cooldown or consume a cap slot.
    engine = make_engine(tmp_path, max_new_entries_per_window=1)
    engine.set_symbol_whitelist(["SOL/USD"])
    for _ in range(5):
        assert not _sized_buy(engine, "BTC/USDT").allowed
    assert _sized_buy(engine, "SOL/USD").allowed
    engine.set_symbol_whitelist(["BTC/USDT"])
    assert _sized_buy(engine, "BTC/USDT").allowed


def test_held_off_whitelist_position_still_counts_toward_exposure(tmp_path):
    # Removing a held symbol from the whitelist must not free up total-exposure room:
    # open_positions_value is whatever the caller passes, independent of the whitelist.
    engine = make_engine(tmp_path, max_position_pct=0.99)
    engine.set_symbol_whitelist(["SOL/USD"])  # BTC/USDT (held, worth 4500) dropped
    decision = engine.check_new_trade("SOL/USD", "buy", portfolio_equity=10000, cash_available=10000,
                                      open_positions_value=4500, now=T0)
    assert decision.allowed
    assert decision.max_quote_amount == pytest.approx(500.0, rel=1e-9)  # 50% of 10000 - 4500


@pytest.mark.parametrize("bad", ["BTC/USDT", b"BTC/USDT", None, 42, ["BTC/USDT", None], ["BTC/USDT", 7],
                                 ["BTC/USDT", ""], ["  "], [["BTC/USDT"]]])
def test_nonsensical_whitelist_is_rejected_and_previous_one_kept(tmp_path, bad):
    # A bare string would otherwise become a whitelist of its characters; None or
    # non-string/blank entries mean the caller is confused. Refuse rather than guess,
    # and leave the previously set whitelist intact (the replacement is all-or-nothing).
    engine = make_engine(tmp_path)
    engine.set_symbol_whitelist(["SOL/USD"])
    with pytest.raises((TypeError, ValueError)):
        engine.set_symbol_whitelist(bad)
    assert engine.symbol_whitelist == frozenset({"SOL/USD"})
    assert _sized_buy(engine, "SOL/USD").allowed
    assert not _sized_buy(engine, "BTC/USDT").allowed


# --- minimum order / notional size floor (risk.min_order_quote_usd, issue #81) ---
#
# After every sizing cap has been applied, a buy whose final spend is below
# min_order_quote_usd is REJECTED -- never resized up to the floor. 0 (the
# default) disables the check entirely, preserving pre-#81 behaviour.
#
# Shared scenario: max_position_pct=0.99, max_total_exposure_pct=0.50, equity=10000,
# so remaining exposure room = 5000 - open_positions_value, and (with no entry/stop
# passed) that room is the binding cap. This makes the final spend easy to pin.

def _floor_buy(engine, symbol="BTC/USDT", **overrides):
    params = dict(portfolio_equity=10000, cash_available=10000, now=T0)
    params.update(overrides)
    return engine.check_new_trade(symbol, "buy", **params)


def _floor_engine(tmp_path, **overrides):
    kwargs = dict(max_position_pct=0.99, max_total_exposure_pct=0.50)
    kwargs.update(overrides)
    return make_engine(tmp_path, **kwargs)


def test_order_sized_below_min_order_floor_is_rejected(tmp_path):
    # Exposure room = 5000 - 4500 = $500 < $1000 floor -> rejected outright.
    engine = _floor_engine(tmp_path, min_order_quote_usd=1000)
    decision = _floor_buy(engine, open_positions_value=4500)
    assert not decision.allowed
    assert decision.max_quote_amount is None  # never resized up to the floor
    assert "minimum order size" in decision.reason


def test_order_just_below_min_order_floor_is_rejected(tmp_path):
    engine = _floor_engine(tmp_path, min_order_quote_usd=10)
    decision = _floor_buy(engine, open_positions_value=4990.01)  # room ~= $9.99
    assert not decision.allowed
    assert decision.max_quote_amount is None
    assert "minimum order size" in decision.reason


def test_order_sized_exactly_at_min_order_floor_is_allowed(tmp_path):
    # Room = 5000 - 4990 = exactly $10.00 == floor -> allowed (floor is inclusive).
    engine = _floor_engine(tmp_path, min_order_quote_usd=10)
    decision = _floor_buy(engine, open_positions_value=4990)
    assert decision.allowed
    assert decision.reason == "ok"
    assert decision.max_quote_amount == 10.0


@pytest.mark.parametrize("overrides", [
    dict(),                                              # flat cap binds ($5000 exposure room)
    dict(open_positions_value=4000),                     # exposure cap binds ($1000)
    dict(cash_available=250),                            # cash binds ($250)
    dict(entry_price=50000, stop_loss=49000),            # risk-sized spend binds ($5000 -> capped)
    dict(entry_price=50000, stop_loss=49000, atr_pct=0.07, open_positions_value=1000),
])
def test_order_above_min_order_floor_is_decided_exactly_as_without_floor(tmp_path, overrides):
    # The floor only ever rejects: above it, allowed/reason/size are identical to an
    # engine with no floor at all -- it never changes the size of an allowed trade.
    reference = _floor_engine(tmp_path)
    engine = _floor_engine(tmp_path, min_order_quote_usd=100)
    expected = _floor_buy(reference, **overrides)
    assert expected.allowed and expected.max_quote_amount >= 100  # precondition: above floor
    assert _floor_buy(engine, **overrides) == expected


def test_min_order_floor_defaults_to_disabled(tmp_path):
    engine = make_engine(tmp_path)
    assert engine.min_order_quote_usd == 0.0


def test_default_config_floor_decides_exactly_as_before(tmp_path):
    # The value shipped in config/config.yaml must leave every decision identical to an
    # engine built without the key (pre-#81 behaviour) -- including dust-sized spends,
    # which were allowed before this change and must still be allowed by default.
    import yaml

    repo_root = Path(__file__).resolve().parent.parent
    risk_cfg = yaml.safe_load((repo_root / "config" / "config.yaml").read_text())["risk"]
    assert "min_order_quote_usd" in risk_cfg
    shipped = make_engine(tmp_path, max_position_pct=0.99,
                          min_order_quote_usd=risk_cfg["min_order_quote_usd"])
    before = make_engine(tmp_path, max_position_pct=0.99)
    for overrides in (
        dict(),
        dict(open_positions_value=4999.99),       # $0.01 dust spend
        dict(open_positions_value=4990),          # $10 spend
        dict(cash_available=0.5),                 # $0.50 spend
        dict(open_positions_value=5000),          # zero room -> "no room left" rejection
        dict(entry_price=50000, stop_loss=49000, atr_pct=0.06, open_positions_value=1000),
    ):
        assert _floor_buy(shipped, **overrides) == _floor_buy(before, **overrides), overrides
    assert _floor_buy(shipped, open_positions_value=4999.99).allowed


def test_min_order_floor_rejects_when_exposure_cap_trims_spend_below_it(tmp_path):
    # The exposure cap is applied first; the floor must not "rescue" a trimmed trade by
    # sizing it back up past the cap -- the only outcomes are the trimmed size or no trade.
    engine = _floor_engine(tmp_path, min_order_quote_usd=600)
    trimmed = _floor_buy(engine, open_positions_value=4500)   # room $500 < $600
    assert not trimmed.allowed and trimmed.max_quote_amount is None
    roomy = _floor_buy(engine, open_positions_value=4300)     # room $700 >= $600
    assert roomy.allowed
    assert roomy.max_quote_amount == pytest.approx(700.0, rel=1e-12)  # still capped by exposure


def test_exhausted_exposure_keeps_no_room_reason_with_floor_enabled(tmp_path):
    # spend == 0 is still reported as "no room left to size", not as a min-order rejection,
    # so the audit trail keeps distinguishing the two.
    from src.risk_counterfactual import classify_reason

    engine = _floor_engine(tmp_path, min_order_quote_usd=10)
    decision = _floor_buy(engine, open_positions_value=5000)
    assert not decision.allowed
    assert classify_reason(decision.reason) == "no_sizing_room"


def test_min_order_rejection_is_classified_distinctly(tmp_path):
    from src.risk_counterfactual import classify_reason

    engine = _floor_engine(tmp_path, min_order_quote_usd=1000)
    assert classify_reason(_floor_buy(engine, open_positions_value=4500).reason) == "min_order_size"


def test_whitelist_rejection_takes_precedence_over_min_order_floor(tmp_path):
    from src.risk_counterfactual import classify_reason

    engine = _floor_engine(tmp_path, min_order_quote_usd=1000)
    decision = _floor_buy(engine, "DOGE/USDT", open_positions_value=4500)  # off-whitelist AND dust
    assert not decision.allowed
    assert classify_reason(decision.reason) == "symbol_whitelist"


def test_min_order_floor_applies_to_runtime_whitelisted_symbols(tmp_path):
    engine = _floor_engine(tmp_path, min_order_quote_usd=1000)
    engine.set_symbol_whitelist(["SOL/USD"])
    dust = _floor_buy(engine, "SOL/USD", open_positions_value=4500)
    assert not dust.allowed and "minimum order size" in dust.reason
    ok = _floor_buy(engine, "SOL/USD", open_positions_value=3000)
    assert ok.allowed and ok.max_quote_amount == pytest.approx(2000.0, rel=1e-12)
    # Dropped from the runtime whitelist -> whitelist reason, regardless of size.
    assert "whitelist" in _floor_buy(engine, "BTC/USDT").reason


def test_min_order_floor_never_blocks_sells(tmp_path):
    engine = _floor_engine(tmp_path, min_order_quote_usd=1_000_000)
    assert engine.check_new_trade("BTC/USDT", "sell", 10000, 0).allowed


def test_min_order_floor_rejects_nan_spend(tmp_path):
    # A NaN equity propagates to a NaN spend; with the floor enabled that must be a
    # rejection (NaN >= floor is False), never an approval of an unknown size.
    engine = _floor_engine(tmp_path, min_order_quote_usd=10)
    decision = engine.check_new_trade("BTC/USDT", "buy", portfolio_equity=float("nan"),
                                      cash_available=10000, now=T0)
    assert not decision.allowed
    assert decision.max_quote_amount is None


def test_min_order_rejection_has_no_side_effects(tmp_path):
    # A min-order rejection must not start a cooldown or take a global cap slot.
    engine = _floor_engine(tmp_path, min_order_quote_usd=1000, max_new_entries_per_window=1)
    assert not _floor_buy(engine, open_positions_value=4500).allowed
    assert _floor_buy(engine, open_positions_value=1000).allowed


@pytest.mark.parametrize("bad_floor", [-0.01, -100, float("nan"), float("inf"), True, "10", None])
def test_nonsensical_min_order_quote_usd_is_rejected(tmp_path, bad_floor):
    with pytest.raises(ValueError):
        make_engine(tmp_path, min_order_quote_usd=bad_floor)


# --- non-finite / impossible numeric inputs fail CLOSED (issue #87) ---
#
# Every limit in check_new_trade is a comparison a NaN silently passes (`nan <= x`,
# `nan < x`, `nan > x` are all False), and min()/max() silently drop a NaN operand
# depending on argument order. Before #87, each case below was APPROVED (or approved
# with a cap silently dropped). Now a buy is rejected with an "invalid risk input"
# reason, never resized. Sells are never affected.

NAN = float("nan")
INF = float("inf")


def _nan_buy(engine, **overrides):
    # Every sizing input present and valid; each test overrides exactly one of them.
    params = dict(portfolio_equity=10000, cash_available=10000, entry_price=50000, stop_loss=49000,
                  risk_reward=2.0, open_positions_value=1000, atr_pct=0.03, now=T0)
    params.update(overrides)
    return engine.check_new_trade("BTC/USDT", "buy", **params)


def _nan_engine(tmp_path, **overrides):
    # Generous flat/exposure caps so that dropping the per-trade risk cap or the cash cap
    # would show up as a bigger approved size, not be masked by another cap.
    kwargs = dict(max_position_pct=0.99, max_total_exposure_pct=0.99)
    kwargs.update(overrides)
    return make_engine(tmp_path, **kwargs)


def _assert_invalid_input(decision, field):
    assert not decision.allowed, decision
    assert decision.max_quote_amount is None
    assert decision.reason.startswith("invalid risk input:"), decision.reason
    assert field in decision.reason, decision.reason


def test_valid_baseline_for_issue_87_scenario(tmp_path):
    # Precondition for every test below: with valid inputs the same call is approved,
    # bound by the per-trade risk cap (100 / 0.02 = 5000), so a rejection below is caused
    # by the one invalid input alone.
    decision = _nan_buy(_nan_engine(tmp_path))
    assert decision.allowed and decision.max_quote_amount == pytest.approx(5000.0, rel=1e-12)


# (a) portfolio_equity

@pytest.mark.parametrize("bad", [NAN, INF, -INF, 0, 0.0, -1.0, -10000, None, True, "10000", 10 ** 400])
def test_invalid_portfolio_equity_rejects_buy(tmp_path, bad):
    # Before #87: NaN equity -> spend NaN -> `nan <= 0` False -> approved, max_quote_amount=nan;
    # inf equity -> every cap inf -> approved for all available cash.
    _assert_invalid_input(_nan_buy(_nan_engine(tmp_path), portfolio_equity=bad), "portfolio_equity")


def test_nan_equity_rejected_even_without_min_order_floor(tmp_path):
    # The exact issue #87 repro: floor disabled (the default), no stop passed.
    engine = make_engine(tmp_path)
    assert engine.min_order_quote_usd == 0.0
    decision = engine.check_new_trade("BTC/USDT", "buy", portfolio_equity=NAN, cash_available=10000, now=T0)
    _assert_invalid_input(decision, "portfolio_equity")


@pytest.mark.parametrize("bad", [NAN, INF, -INF, 0.0, -500.0, None])
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_invalid_equity_never_becomes_the_daily_baseline(tmp_path, bad, side):
    # Before #87: a NaN (or inf) on the first call of a UTC day became that day's
    # baseline, every later daily_pnl_pct was NaN, `nan <= -limit` is False, and the
    # daily loss limit was off for the whole day. (A 0 baseline raised ZeroDivisionError.)
    engine = make_engine(tmp_path)
    engine.check_new_trade("BTC/USDT", side, portfolio_equity=bad, cash_available=10000, now=T0)
    assert engine._day_start_equity is None
    # First VALID equity of the day becomes the baseline...
    assert engine.check_new_trade("ETH/USDT", "buy", 10000, 10000, now=T0).allowed
    assert engine._day_start_equity == 10000
    # ...and the daily loss limit is live: -6% vs a 5% limit.
    decision = engine.check_new_trade("BTC/USDT", "buy", 9400, 9400, now=T0 + timedelta(minutes=1))
    assert not decision.allowed
    assert "daily loss limit" in decision.reason


def test_invalid_equity_on_a_new_day_neither_keeps_nor_poisons_the_baseline(tmp_path):
    engine = make_engine(tmp_path)
    assert engine.check_new_trade("BTC/USDT", "buy", 10000, 10000, now=T0).allowed  # day 1 baseline
    day2 = T0 + timedelta(days=1)
    _assert_invalid_input(
        engine.check_new_trade("BTC/USDT", "buy", NAN, 10000, now=day2), "portfolio_equity"
    )
    assert engine._day_start_equity == 10000 and engine._day_start_date == T0.date()  # untouched
    # Day 2's first valid equity (8000) becomes day 2's baseline -- not day 1's 10000.
    assert engine.check_new_trade("BTC/USDT", "buy", 8000, 8000, now=day2).allowed
    assert engine._day_start_equity == 8000 and engine._day_start_date == day2.date()
    assert not engine.check_new_trade("ETH/USDT", "buy", 7500, 7500, now=day2).allowed  # -6.25%


# (c) cash_available

@pytest.mark.parametrize("bad", [NAN, INF, -INF, None, "10000", True])
def test_invalid_cash_available_rejects_buy(tmp_path, bad):
    # Before #87: min(max_position_usd, nan) returns max_position_usd -- the cash cap was
    # silently dropped and the buy approved.
    _assert_invalid_input(_nan_buy(_nan_engine(tmp_path), cash_available=bad), "cash_available")


def test_nan_cash_never_drops_the_cash_cap_without_a_stop(tmp_path):
    engine = make_engine(tmp_path)  # max_position_pct=0.20 -> $2000 flat cap
    decision = engine.check_new_trade("BTC/USDT", "buy", portfolio_equity=10000, cash_available=NAN, now=T0)
    _assert_invalid_input(decision, "cash_available")


@pytest.mark.parametrize("cash", [0.0, -0.0, -250.0])
def test_finite_non_positive_cash_keeps_its_existing_no_room_reason(tmp_path, cash):
    # Zero cash is a normal state, not invalid data: unchanged behaviour and reason.
    decision = _nan_buy(_nan_engine(tmp_path), cash_available=cash)
    assert not decision.allowed
    assert decision.reason.startswith("no room left to size")


@pytest.mark.parametrize("floor", [0.0, 10.0])
def test_infinite_equity_and_cash_never_pass_the_min_order_floor(tmp_path, floor):
    # Before #87: spend = inf, `inf <= 0` False, `inf >= floor` True -> approved with
    # max_quote_amount=inf whether or not the floor was enabled.
    engine = make_engine(tmp_path, min_order_quote_usd=floor)
    decision = engine.check_new_trade("BTC/USDT", "buy", portfolio_equity=INF, cash_available=INF, now=T0)
    _assert_invalid_input(decision, "portfolio_equity")


# prices / stop / risk-reward / ATR / exposure

@pytest.mark.parametrize("bad", [NAN, INF, -INF, 0.0, -50000.0, True])
def test_invalid_entry_price_rejects_buy(tmp_path, bad):
    # Before #87: `nan > stop_loss` is False, so the per-trade risk cap was silently skipped
    # and the buy sized at the flat ceiling ($9900 here instead of $5000).
    _assert_invalid_input(_nan_buy(_nan_engine(tmp_path), entry_price=bad), "entry_price")


@pytest.mark.parametrize("bad", [NAN, INF, -INF, "49000"])
def test_invalid_stop_loss_rejects_buy(tmp_path, bad):
    _assert_invalid_input(_nan_buy(_nan_engine(tmp_path), stop_loss=bad), "stop_loss")


@pytest.mark.parametrize("bad", [NAN, INF, -INF])
def test_invalid_risk_reward_rejects_buy(tmp_path, bad):
    # Before #87: `nan < min_risk_reward` is False -> the R/R check passed.
    _assert_invalid_input(_nan_buy(_nan_engine(tmp_path), risk_reward=bad), "risk_reward")


@pytest.mark.parametrize("bad", [NAN, INF, -INF, -0.01])
def test_invalid_atr_pct_rejects_buy(tmp_path, bad):
    # NaN passed the hard volatility gate (`nan > max` is False); a negative ATR sized as
    # the calmest possible market (scale 1.0).
    _assert_invalid_input(_nan_buy(_nan_engine(tmp_path), atr_pct=bad), "atr_pct")


@pytest.mark.parametrize("bad", [NAN, INF, -INF, -1000.0, None])
def test_invalid_open_positions_value_rejects_buy(tmp_path, bad):
    # Before #87: -inf (or any negative value) inflated the remaining exposure room past
    # max_total_exposure_pct, i.e. dropped the total-exposure cap.
    _assert_invalid_input(_nan_buy(_nan_engine(tmp_path), open_positions_value=bad), "open_positions_value")


@pytest.mark.parametrize("field", ["entry_price", "stop_loss", "risk_reward", "atr_pct"])
def test_none_still_means_not_provided_for_optional_inputs(tmp_path, field):
    # None is NOT treated as invalid for the optional inputs -- it keeps its documented
    # "not provided" meaning, so existing callers decide exactly as before.
    decision = _nan_buy(_nan_engine(tmp_path), **{field: None})
    assert decision.allowed and decision.reason == "ok"


def test_nan_config_pct_never_approves_a_nan_or_uncapped_spend(tmp_path):
    # Defence in depth behind the input checks: a NaN max_position_pct used to give
    # spend=min(nan, cash)=nan -> approved with max_quote_amount=nan; a NaN
    # max_loss_per_trade_pct made min(spend, nan) silently drop the per-trade risk cap.
    d1 = _nan_buy(_nan_engine(tmp_path, max_position_pct=NAN))
    assert not d1.allowed and d1.max_quote_amount is None and "sized spend" in d1.reason
    d2 = _nan_buy(_nan_engine(tmp_path, max_loss_per_trade_pct=NAN))
    assert not d2.allowed and d2.max_quote_amount is None and "risk-sized spend" in d2.reason


def test_kill_switch_and_whitelist_still_take_precedence_over_invalid_input(tmp_path):
    engine = _nan_engine(tmp_path)
    assert "whitelist" in engine.check_new_trade("DOGE/USDT", "buy", NAN, NAN, now=T0).reason
    engine.kill_switch_path.touch()
    assert "kill switch" in _nan_buy(engine, portfolio_equity=NAN).reason


@pytest.mark.parametrize("overrides", [
    dict(portfolio_equity=NAN),
    dict(cash_available=NAN),
    dict(portfolio_equity=NAN, cash_available=NAN, entry_price=NAN, stop_loss=NAN,
         risk_reward=NAN, open_positions_value=NAN, atr_pct=NAN),
    dict(portfolio_equity=INF, cash_available=-INF),
])
def test_sells_are_never_blocked_by_invalid_inputs(tmp_path, overrides):
    # Closing risk must stay possible on a bad reading -- including off-whitelist, under a
    # kill switch, and right after a daily-loss halt.
    engine = _nan_engine(tmp_path)
    assert engine.check_new_trade("BTC/USDT", "buy", 10000, 10000, now=T0).allowed
    assert not engine.check_new_trade("BTC/USDT", "buy", 9000, 9000, now=T0).allowed  # daily halt
    engine.kill_switch_path.touch()
    for symbol in ("BTC/USDT", "NOT/WHITELISTED"):
        params = dict(portfolio_equity=10000, cash_available=0, now=T0)
        params.update(overrides)
        decision = engine.check_new_trade(symbol, "sell", **params)
        assert decision == RiskDecision(True, "ok")


def test_rejected_invalid_input_calls_leave_no_trace(tmp_path):
    # A rejected call must not start a cooldown, take a global-cap slot, or touch the
    # daily baseline: afterwards the engine decides exactly like one that never saw them.
    dirty = _nan_engine(tmp_path, max_new_entries_per_window=1)
    clean = _nan_engine(tmp_path, max_new_entries_per_window=1)
    for overrides in (dict(portfolio_equity=NAN), dict(cash_available=INF), dict(entry_price=NAN),
                      dict(risk_reward=NAN), dict(atr_pct=NAN), dict(open_positions_value=-INF)):
        assert not _nan_buy(dirty, **overrides).allowed
    assert dirty._last_trade_time == {} and dirty._recent_entry_times == []
    # The NaN-equity call came first and adopted nothing; the later rejected calls carried
    # a valid equity of 10000, which is exactly the baseline `clean` adopts on its first call.
    assert dirty._day_start_equity == 10000 and dirty._day_start_date == T0.date()
    for equity in (10000, 9600, 9400):
        assert _nan_buy(dirty, portfolio_equity=equity) == _nan_buy(clean, portfolio_equity=equity)
