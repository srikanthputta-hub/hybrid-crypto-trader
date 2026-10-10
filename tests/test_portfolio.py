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


# --- apply_buy rejects invalid amounts without corrupting state (issue #87) ---
#
# Before #87, `nan > cash` was False, so a NaN quote_amount slipped past the cash check
# and made cash NaN permanently (and every later equity NaN). Every rejection below must
# raise ValueError AND leave cash/positions exactly as they were.

NAN = float("nan")
INF = float("inf")


def _snapshot(p):
    return p.cash, {s: (pos.amount, pos.entry_price) for s, pos in p.positions.items()}


def _portfolio_with_position():
    p = Portfolio(cash=10000)
    p.apply_buy("BTC/USDT", quote_amount=1000, price=50000, fee_pct=0.0)
    return p


@pytest.mark.parametrize("bad", [NAN, INF, -INF, -1.0, 0.0, None, True, "1000", 10 ** 400])
@pytest.mark.parametrize("symbol", ["BTC/USDT", "ETH/USDT"])  # existing position / new position
def test_apply_buy_rejects_invalid_quote_amount_without_touching_state(bad, symbol):
    p = _portfolio_with_position()
    before = _snapshot(p)
    with pytest.raises(ValueError):
        p.apply_buy(symbol, quote_amount=bad, price=50000)
    assert _snapshot(p) == before


@pytest.mark.parametrize("bad", [NAN, INF, -INF, 0.0, -50000.0, None, True])
def test_apply_buy_rejects_invalid_price_without_touching_state(bad):
    p = _portfolio_with_position()
    before = _snapshot(p)
    with pytest.raises(ValueError):
        p.apply_buy("BTC/USDT", quote_amount=100, price=bad)
    assert _snapshot(p) == before


@pytest.mark.parametrize("bad", [NAN, INF, -0.001, 1.0, 1.5, None])
def test_apply_buy_rejects_invalid_fee_pct_without_touching_state(bad):
    p = _portfolio_with_position()
    before = _snapshot(p)
    with pytest.raises(ValueError):
        p.apply_buy("BTC/USDT", quote_amount=100, price=50000, fee_pct=bad)
    assert _snapshot(p) == before


@pytest.mark.parametrize("bad_cash", [NAN, INF])
def test_apply_buy_refuses_when_cash_is_already_non_finite(bad_cash):
    # Refused up front with an explicit "cash is corrupt" reason (not as a side effect of
    # the post-computation check), so the audit log says what is actually wrong.
    p = Portfolio(cash=bad_cash)
    with pytest.raises(ValueError, match="portfolio cash"):
        p.apply_buy("BTC/USDT", quote_amount=100, price=50000)
    assert p.positions == {}


def test_apply_buy_over_cash_still_rejected_without_touching_state():
    p = _portfolio_with_position()
    before = _snapshot(p)
    with pytest.raises(ValueError, match="insufficient cash"):
        p.apply_buy("BTC/USDT", quote_amount=9000.01, price=50000)
    assert _snapshot(p) == before


def test_apply_buy_overflowing_base_amount_rejected_without_touching_state():
    # A subnormal price makes net_quote / price overflow to inf: refuse, don't store inf.
    p = _portfolio_with_position()
    before = _snapshot(p)
    with pytest.raises(ValueError):
        p.apply_buy("ETH/USDT", quote_amount=1000, price=5e-324)
    assert _snapshot(p) == before


def test_apply_buy_after_rejections_behaves_exactly_as_before():
    # Exact (not approx) equality: the valid-path arithmetic is unchanged.
    p = _portfolio_with_position()
    for bad in (NAN, INF, -1.0):
        with pytest.raises(ValueError):
            p.apply_buy("BTC/USDT", quote_amount=bad, price=50000)
    base = p.apply_buy("BTC/USDT", quote_amount=1000, price=60000, fee_pct=0.001)
    assert base == (1000 - 1000 * 0.001) / 60000
    assert p.cash == 10000 - 1000 - 1000
    expected_amount = 1000 / 50000 + base
    assert p.positions["BTC/USDT"].amount == expected_amount
    assert p.positions["BTC/USDT"].entry_price == (50000 * (1000 / 50000) + 60000 * base) / expected_amount
