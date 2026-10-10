from src.monitor import check_open, check_pending, sweep
from src.trade_plan_store import PlanRecord

PLAN = {
    "entry_zone": (100.0, 102.0),
    "stop_loss": 97.0,
    "take_profit": 110.0,
    "invalidation": 95.0,
}


def _record(status: str, symbol: str = "BTC/USDT", **plan_overrides) -> PlanRecord:
    plan = dict(PLAN, **plan_overrides)
    return PlanRecord(symbol=symbol, status=status, plan=plan)


def test_pending_fills_when_price_enters_entry_zone():
    record = _record("PENDING")
    action = check_pending(record, current_price=101.0)
    assert action is not None
    assert action.action == "FILL"


def test_pending_discarded_when_invalidation_hit_first():
    record = _record("PENDING")
    action = check_pending(record, current_price=94.0)
    assert action is not None
    assert action.action == "DISCARD"


def test_pending_does_nothing_while_price_outside_zone_and_above_invalidation():
    record = _record("PENDING")
    assert check_pending(record, current_price=105.0) is None


def test_open_position_hits_stop_loss():
    record = _record("OPEN")
    action = check_open(record, current_price=96.5)
    assert action is not None
    assert action.action == "STOP_LOSS"


def test_open_position_hits_take_profit():
    record = _record("OPEN")
    action = check_open(record, current_price=111.0)
    assert action is not None
    assert action.action == "TAKE_PROFIT"


def test_open_position_no_action_while_between_stop_and_target():
    record = _record("OPEN")
    assert check_open(record, current_price=105.0) is None


def test_check_pending_ignores_open_records_and_vice_versa():
    open_record = _record("OPEN")
    pending_record = _record("PENDING")
    assert check_pending(open_record, current_price=101.0) is None
    assert check_open(pending_record, current_price=96.5) is None


def test_sweep_handles_mixed_records_and_missing_prices():
    records = {
        "BTC/USDT": _record("PENDING", symbol="BTC/USDT"),
        "ETH/USDT": _record("OPEN", symbol="ETH/USDT"),
        "SOL/USDT": _record("OPEN", symbol="SOL/USDT"),
    }
    prices = {"BTC/USDT": 101.0, "ETH/USDT": 96.0}  # SOL/USDT price missing on purpose
    actions = sweep(records, prices)
    symbols_acted_on = {a.symbol for a in actions}
    assert symbols_acted_on == {"BTC/USDT", "ETH/USDT"}


# --- non-finite prices never trigger anything (follow-up to #87 / PR #96) ---
#
# Before: +inf satisfied `price >= take_profit` (TAKE_PROFIT) and -inf satisfied
# `price <= stop_loss` / `price <= invalidation` (STOP_LOSS / DISCARD), and the paper broker
# then booked the exit at +-inf (cash became +-inf). NaN already compared False everywhere.

import logging  # noqa: E402

import pytest  # noqa: E402

_NON_FINITE = [float("inf"), float("-inf"), float("nan")]


@pytest.mark.parametrize("price", _NON_FINITE)
@pytest.mark.parametrize("status", ["PENDING", "OPEN"])
def test_non_finite_price_never_triggers(price, status, caplog):
    record = _record(status)
    with caplog.at_level(logging.WARNING, logger="src.monitor"):
        assert check_pending(record, price) is None
        assert check_open(record, price) is None
        assert sweep({"BTC/USDT": record}, {"BTC/USDT": price}) == []
    # Logged distinctly (once per check that owns this status, i.e. twice: direct + sweep).
    msgs = [r.getMessage() for r in caplog.records if r.name == "src.monitor"]
    assert len(msgs) == 2 and all("non-finite price" in m and "BTC/USDT" in m for m in msgs)


def test_non_finite_price_is_skipped_even_with_extreme_plan_levels():
    # A plan whose levels are themselves +-inf must still not fire on a +-inf price.
    record = _record("OPEN", stop_loss=float("-inf"), take_profit=float("inf"))
    assert check_open(record, float("inf")) is None
    assert check_open(record, float("-inf")) is None


def test_finite_prices_still_trigger_after_a_non_finite_one():
    record = _record("OPEN")
    assert check_open(record, float("-inf")) is None
    assert check_open(record, 96.5).action == "STOP_LOSS"
    assert check_open(record, 110.0).action == "TAKE_PROFIT"
    pending = _record("PENDING")
    assert check_pending(pending, float("inf")) is None
    assert check_pending(pending, 101.0).action == "FILL"
    assert check_pending(pending, 95.0).action == "DISCARD"


def test_non_matching_status_does_not_log(caplog):
    # check_pending on an OPEN plan (and vice versa) returns before the price is looked at,
    # so bot.py's `check_pending(...) or check_open(...)` logs exactly once per record.
    with caplog.at_level(logging.WARNING, logger="src.monitor"):
        assert check_pending(_record("OPEN"), float("inf")) is None
        assert check_open(_record("PENDING"), float("inf")) is None
    assert [r for r in caplog.records if r.name == "src.monitor"] == []
