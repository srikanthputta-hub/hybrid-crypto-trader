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
