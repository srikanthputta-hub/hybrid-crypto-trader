from src.trade_plan_store import TradePlanStore

SAMPLE_PLAN = {
    "symbol": "BTC/USDT",
    "side": "buy",
    "pattern": "BREAKOUT",
    "entry_zone": (118.4, 119.1),
    "stop_loss": 117.4,
    "take_profit": 122.5,
    "invalidation": 116.9,
    "risk_reward": 2.0,
    "rule_based_confidence_stars": 4.0,
}


def _store(tmp_path):
    return TradePlanStore(tmp_path / "current.json", tmp_path / "history.jsonl")


def test_set_pending_then_get(tmp_path):
    store = _store(tmp_path)
    store.set_pending("BTC/USDT", SAMPLE_PLAN)
    record = store.get("BTC/USDT")
    assert record is not None
    assert record.status == "PENDING"
    assert record.plan["pattern"] == "BREAKOUT"


def test_mark_open_updates_status_fill_price_and_amount(tmp_path):
    store = _store(tmp_path)
    store.set_pending("BTC/USDT", SAMPLE_PLAN)
    store.mark_open("BTC/USDT", fill_price=118.9, fill_amount=8.4)
    record = store.get("BTC/USDT")
    assert record.status == "OPEN"
    assert record.fill_price == 118.9
    assert record.fill_amount == 8.4


def test_mark_closed_removes_from_current(tmp_path):
    store = _store(tmp_path)
    store.set_pending("BTC/USDT", SAMPLE_PLAN)
    store.mark_open("BTC/USDT", fill_price=118.9)
    store.mark_closed("BTC/USDT", reason="take_profit", price=122.6)
    assert store.get("BTC/USDT") is None


def test_discard_pending_removes_from_current(tmp_path):
    store = _store(tmp_path)
    store.set_pending("BTC/USDT", SAMPLE_PLAN)
    store.discard_pending("BTC/USDT", reason="invalidated", price=116.5)
    assert store.get("BTC/USDT") is None


def test_state_persists_across_store_instances(tmp_path):
    store1 = _store(tmp_path)
    store1.set_pending("ETH/USDT", SAMPLE_PLAN)
    store2 = TradePlanStore(tmp_path / "current.json", tmp_path / "history.jsonl")
    record = store2.get("ETH/USDT")
    assert record is not None
    assert record.status == "PENDING"


def test_history_log_gets_one_line_per_transition(tmp_path):
    store = _store(tmp_path)
    store.set_pending("BTC/USDT", SAMPLE_PLAN)
    store.mark_open("BTC/USDT", fill_price=118.9)
    store.mark_closed("BTC/USDT", reason="stop_loss", price=117.0)
    lines = (tmp_path / "history.jsonl").read_text().strip().split("\n")
    assert len(lines) == 3
