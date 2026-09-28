import pytest

from src.portfolio import Portfolio, reconstruct_cash


def test_buy_reduces_cash_and_opens_position():
    p = Portfolio(cash=10000)
    base_amount = p.apply_buy("BTC/USDT", quote_amount=1000, price=50000, fee_pct=0.001)
    assert p.cash == pytest.approx(9000)
    assert "BTC/USDT" in p.positions
    assert p.positions["BTC/USDT"].amount == pytest.approx(base_amount)
    assert base_amount == pytest.approx(999 / 50000)  # fee deducted before conversion


def test_buy_twice_averages_entry_price():
    p = Portfolio(cash=10000)
    p.apply_buy("BTC/USDT", quote_amount=1000, price=50000, fee_pct=0.0)
    p.apply_buy("BTC/USDT", quote_amount=1000, price=60000, fee_pct=0.0)
    pos = p.positions["BTC/USDT"]
    expected_amount = 1000 / 50000 + 1000 / 60000
    assert pos.amount == pytest.approx(expected_amount)
    assert pos.entry_price == pytest.approx(2000 / expected_amount)


def test_sell_closes_position_and_credits_cash():
    p = Portfolio(cash=10000)
    p.apply_buy("BTC/USDT", quote_amount=1000, price=50000, fee_pct=0.0)
    proceeds = p.apply_sell("BTC/USDT", price=55000, fee_pct=0.0)
    assert "BTC/USDT" not in p.positions
    assert proceeds == pytest.approx(1100)
    assert p.cash == pytest.approx(9000 + 1100)


def test_buy_more_than_cash_raises():
    p = Portfolio(cash=100)
    with pytest.raises(ValueError):
        p.apply_buy("BTC/USDT", quote_amount=1000, price=50000)


def test_sell_without_position_raises():
    p = Portfolio(cash=10000)
    with pytest.raises(ValueError):
        p.apply_sell("BTC/USDT", price=50000)


def test_equity_includes_open_positions_at_current_price():
    p = Portfolio(cash=9000)
    p.positions["BTC/USDT"] = __import__("src.portfolio", fromlist=["Position"]).Position(
        "BTC/USDT", amount=0.02, entry_price=50000
    )
    equity = p.equity({"BTC/USDT": 60000})
    assert equity == pytest.approx(9000 + 0.02 * 60000)


def test_reconstruct_cash_no_log_file_returns_starting_cash(tmp_path):
    assert reconstruct_cash(tmp_path / "trades.csv", 10000) == 10000


def test_reconstruct_cash_replays_buys_and_sells(tmp_path):
    path = tmp_path / "trades.csv"
    path.write_text(
        "timestamp,mode,symbol,side,price,base_amount,quote_amount\n"
        "t1,paper,BTC/USDT,buy,50000,0.02,1000\n"
        "t2,paper,BTC/USDT,sell,55000,0.02,1100\n"
        "t3,paper,ETH/USDT,buy,3000,0.5,1500\n"
    )
    assert reconstruct_cash(path, 10000) == pytest.approx(10000 - 1000 + 1100 - 1500)
