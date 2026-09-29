import csv
from datetime import datetime, timedelta, timezone

from src.pnl import build_pnl_report, load_closed_trades


def _write_trades(path, rows):
    fieldnames = ["timestamp", "mode", "symbol", "side", "price", "base_amount", "quote_amount"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_load_closed_trades_missing_file_returns_empty(tmp_path):
    assert load_closed_trades(tmp_path / "nope.csv") == []


def test_load_closed_trades_pairs_buy_and_sell(tmp_path):
    path = tmp_path / "trades.csv"
    buy_ts = "2026-09-29T10:00:00+00:00"
    sell_ts = "2026-09-29T11:00:00+00:00"
    _write_trades(
        path,
        [
            {"timestamp": buy_ts, "mode": "paper", "symbol": "BTC/USDT", "side": "buy", "price": "100", "base_amount": "1", "quote_amount": "100"},
            {"timestamp": sell_ts, "mode": "paper", "symbol": "BTC/USDT", "side": "sell", "price": "110", "base_amount": "1", "quote_amount": "109"},
        ],
    )

    trades = load_closed_trades(path)

    assert len(trades) == 1
    t = trades[0]
    assert t.symbol == "BTC/USDT"
    assert t.cost == 100.0
    assert t.proceeds == 109.0
    assert t.pnl == 9.0
    assert round(t.pnl_pct, 4) == 0.09


def test_load_closed_trades_skips_unmatched_sell(tmp_path):
    path = tmp_path / "trades.csv"
    _write_trades(
        path,
        [
            {"timestamp": "2026-09-29T10:00:00+00:00", "mode": "paper", "symbol": "ETH/USDT", "side": "sell", "price": "10", "base_amount": "1", "quote_amount": "10"},
        ],
    )
    assert load_closed_trades(path) == []


def test_load_closed_trades_leaves_open_buy_unmatched(tmp_path):
    path = tmp_path / "trades.csv"
    _write_trades(
        path,
        [
            {"timestamp": "2026-09-29T10:00:00+00:00", "mode": "paper", "symbol": "ETH/USDT", "side": "buy", "price": "10", "base_amount": "1", "quote_amount": "10"},
        ],
    )
    assert load_closed_trades(path) == []  # no sell yet -- not a realized trade


def test_build_pnl_report_summary_and_buckets(tmp_path):
    path = tmp_path / "trades.csv"
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    win_buy = (now - timedelta(minutes=30)).isoformat()
    win_sell = (now - timedelta(minutes=10)).isoformat()
    loss_buy = (now - timedelta(days=2)).isoformat()
    loss_sell = (now - timedelta(days=2) + timedelta(minutes=5)).isoformat()
    _write_trades(
        path,
        [
            {"timestamp": win_buy, "mode": "paper", "symbol": "BTC/USDT", "side": "buy", "price": "100", "base_amount": "1", "quote_amount": "100"},
            {"timestamp": win_sell, "mode": "paper", "symbol": "BTC/USDT", "side": "sell", "price": "120", "base_amount": "1", "quote_amount": "119"},
            {"timestamp": loss_buy, "mode": "paper", "symbol": "ETH/USDT", "side": "buy", "price": "50", "base_amount": "2", "quote_amount": "100"},
            {"timestamp": loss_sell, "mode": "paper", "symbol": "ETH/USDT", "side": "sell", "price": "45", "base_amount": "2", "quote_amount": "89"},
        ],
    )

    report = build_pnl_report(path, now=now)

    assert report["summary"]["total_trades"] == 2
    assert report["summary"]["total_realized_pnl"] == 8.0  # +19 - 11
    assert report["summary"]["win_rate"] == 0.5
    assert report["summary"]["avg_win"] == 19.0
    assert report["summary"]["avg_loss"] == -11.0
    assert report["summary"]["best_trade"] == 19.0
    assert report["summary"]["worst_trade"] == -11.0

    assert len(report["hourly"]) == 24
    assert len(report["daily"]) == 30
    assert len(report["weekly"]) == 12
    # the win landed within the last hour
    assert report["hourly"][-1]["pnl"] == 19.0
    assert report["hourly"][-1]["trades"] == 1
    # the loss landed 2 days ago -- shows up in the daily buckets, not hourly
    assert sum(b["trades"] for b in report["hourly"]) == 1
    assert sum(b["pnl"] for b in report["daily"]) == 8.0


def test_build_pnl_report_empty_when_no_trades(tmp_path):
    report = build_pnl_report(tmp_path / "nope.csv")
    assert report["summary"]["total_trades"] == 0
    assert report["summary"]["win_rate"] is None
    assert report["summary"]["avg_win"] is None
    assert all(b["pnl"] == 0.0 and b["trades"] == 0 for b in report["daily"])
