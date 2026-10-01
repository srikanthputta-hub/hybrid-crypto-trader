import csv
from datetime import datetime, timedelta, timezone

from src.pnl import ClosedTrade
from src.risk_analytics import (
    MIN_TRADES_FOR_STATS,
    build_risk_report,
    drawdown_from_trades,
    historical_var_cvar,
    win_rate_significance,
)

NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)


def _trade(pnl_pct: float, minutes_ago: int = 0, symbol: str = "BTC/USDT") -> ClosedTrade:
    sell_time = NOW - timedelta(minutes=minutes_ago)
    cost = 100.0
    pnl = cost * pnl_pct
    return ClosedTrade(
        symbol=symbol,
        buy_time=sell_time - timedelta(minutes=5),
        sell_time=sell_time,
        cost=cost,
        proceeds=cost + pnl,
        pnl=pnl,
        pnl_pct=pnl_pct,
    )


# --- historical_var_cvar ---------------------------------------------------


def test_var_cvar_empty_below_min_trades():
    trades = [_trade(0.05, minutes_ago=i) for i in range(MIN_TRADES_FOR_STATS - 1)]
    assert historical_var_cvar(trades) == {}


def test_var_cvar_on_known_distribution():
    # 20 trades, returns -0.20 .. +0.19 step 0.02 roughly -- a clean sorted
    # ramp so the percentile math is easy to hand-check.
    returns = [round(-0.20 + 0.02 * i, 2) for i in range(20)]  # -0.20 ... 0.18
    trades = [_trade(r, minutes_ago=i) for i, r in enumerate(returns)]

    result = historical_var_cvar(trades, confidence_levels=(0.95,))

    assert "95" in result
    # 5th percentile of 20 sorted values at rank 0.05*19=0.95 -> interpolated
    # between index 0 (-0.20) and index 1 (-0.18).
    assert result["95"]["var_pct"] == round(-0.20 + (-0.18 - -0.20) * 0.95, 4)
    # CVaR at 95% averages the worst tail_cutoff_rank=max(1, round(0.05*20))=1 value.
    assert result["95"]["cvar_pct"] == -0.20


def test_var_cvar_worse_tail_for_skewed_losses():
    # Mostly small wins, one catastrophic loss -- CVaR should capture the
    # catastrophe even though VaR (the threshold) might not.
    trades = [_trade(0.01, minutes_ago=i) for i in range(9)] + [_trade(-0.50, minutes_ago=9)]
    result = historical_var_cvar(trades, confidence_levels=(0.90,))
    assert result["90"]["cvar_pct"] == -0.50


# --- drawdown_from_trades ---------------------------------------------------


def test_drawdown_no_trades_returns_none_fields():
    report = drawdown_from_trades([], starting_cash=10_000)
    assert report.max_drawdown_pct is None
    assert report.current_drawdown_pct is None


def test_drawdown_tracks_peak_to_trough():
    # Equity path: 10000 -> 11000 (win) -> 9900 (loss) -> 10450 (win)
    # Peak is 11000; trough after the loss is 9900 -> drawdown = 1100/11000 = 10%.
    # Current equity 10450 is still below the 11000 peak -> current drawdown = 550/11000 = 5%.
    # The _trade() helper fixes cost=100 regardless of pnl_pct, which doesn't
    # let us control absolute dollar pnl precisely for this multi-step
    # scenario -- build the trades directly instead.
    t1 = ClosedTrade("BTC/USDT", NOW - timedelta(minutes=35), NOW - timedelta(minutes=30), cost=10_000, proceeds=11_000, pnl=1_000, pnl_pct=0.10)
    t2 = ClosedTrade("BTC/USDT", NOW - timedelta(minutes=25), NOW - timedelta(minutes=20), cost=11_000, proceeds=9_900, pnl=-1_100, pnl_pct=-0.10)
    t3 = ClosedTrade("BTC/USDT", NOW - timedelta(minutes=15), NOW - timedelta(minutes=10), cost=9_900, proceeds=10_450, pnl=550, pnl_pct=0.0556)

    report = drawdown_from_trades([t1, t2, t3], starting_cash=10_000)

    assert report.peak_equity == 11_000
    assert report.max_drawdown_pct == round(1_100 / 11_000, 4)
    assert report.current_drawdown_pct == round(550 / 11_000, 4)


def test_drawdown_zero_when_only_winners():
    trades = [_trade(0.05, minutes_ago=i) for i in range(5)]
    report = drawdown_from_trades(trades, starting_cash=10_000)
    assert report.max_drawdown_pct == 0.0
    assert report.current_drawdown_pct == 0.0


# --- win_rate_significance ---------------------------------------------------


def test_significance_empty_below_min_trades():
    trades = [_trade(0.05, minutes_ago=i) for i in range(MIN_TRADES_FOR_STATS - 1)]
    assert win_rate_significance(trades, expected_win_rate=0.4) == {}


def test_significance_all_losses_is_surprising_against_decent_expected_rate():
    # 0/12 wins when the strategy is supposed to break even at 40% -- this
    # should be rare under the null and flagged "surprising".
    trades = [_trade(-0.05, minutes_ago=i) for i in range(12)]
    result = win_rate_significance(trades, expected_win_rate=0.40, n_simulations=5000, seed=1)
    assert result["observed_wins"] == 0
    assert result["observed_win_rate"] == 0.0
    assert result["p_value"] < 0.05
    assert result["verdict"] == "surprising"


def test_significance_decent_win_rate_is_unremarkable():
    # 6/12 wins against a 40% breakeven bar -- comfortably plausible by chance.
    trades = [_trade(0.05 if i % 2 == 0 else -0.05, minutes_ago=i) for i in range(12)]
    result = win_rate_significance(trades, expected_win_rate=0.40, n_simulations=5000, seed=1)
    assert result["observed_wins"] == 6
    assert result["verdict"] in ("plausible", "unremarkable")


def test_significance_deterministic_for_fixed_seed():
    trades = [_trade(-0.05, minutes_ago=i) for i in range(8)]
    r1 = win_rate_significance(trades, expected_win_rate=0.4, n_simulations=2000, seed=7)
    r2 = win_rate_significance(trades, expected_win_rate=0.4, n_simulations=2000, seed=7)
    assert r1 == r2


# --- build_risk_report (end-to-end via trades.csv) --------------------------


def _write_trades_csv(path, rows):
    fieldnames = ["timestamp", "mode", "symbol", "side", "price", "base_amount", "quote_amount"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_build_risk_report_empty_trade_log(tmp_path):
    report = build_risk_report(tmp_path / "nope.csv", starting_cash=10_000, min_risk_reward=1.5, now=NOW)
    assert report["sample_size"] == 0
    assert report["var_cvar"] == {}
    assert report["significance"] == {}
    assert report["drawdown"]["max_drawdown_pct"] is None


def test_build_risk_report_uses_breakeven_from_min_risk_reward(tmp_path):
    path = tmp_path / "trades.csv"
    rows = []
    for i in range(6):
        buy_ts = (NOW - timedelta(hours=i + 1)).isoformat()
        sell_ts = (NOW - timedelta(hours=i + 1) + timedelta(minutes=10)).isoformat()
        rows.append({"timestamp": buy_ts, "mode": "paper", "symbol": "BTC/USDT", "side": "buy", "price": "100", "base_amount": "1", "quote_amount": "100"})
        rows.append({"timestamp": sell_ts, "mode": "paper", "symbol": "BTC/USDT", "side": "sell", "price": "95", "base_amount": "1", "quote_amount": "95"})
    _write_trades_csv(path, rows)

    # min_risk_reward=1 -> breakeven win rate = 1/(1+1) = 0.5
    report = build_risk_report(path, starting_cash=10_000, min_risk_reward=1.0, now=NOW)

    assert report["sample_size"] == 6
    assert report["significance"]["expected_win_rate"] == 0.5
    assert report["significance"]["observed_wins"] == 0
