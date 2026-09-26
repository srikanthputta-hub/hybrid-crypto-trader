import pandas as pd

from src.indicators import add_indicators
from src.scanner import scan


def _make_df(closes, volumes=None, highs=None, lows=None, opens=None) -> pd.DataFrame:
    n = len(closes)
    volumes = volumes or [100.0] * n
    highs = highs or [c * 1.002 for c in closes]
    lows = lows or [c * 0.998 for c in closes]
    opens = opens or closes
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC"),
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    )


def _enrich(df: pd.DataFrame) -> pd.DataFrame:
    return add_indicators(df, fast_ema=12, slow_ema=26, rsi_period=14)


def _patterns(candidates, pattern, direction=None):
    return [c for c in candidates if c.pattern == pattern and (direction is None or c.direction == direction)]


def test_not_enough_history_returns_no_candidates():
    df = _enrich(_make_df([100.0] * 5))
    assert scan(df, {}) == []


def test_flat_market_finds_nothing():
    df = _enrich(_make_df([100.0] * 60))
    assert scan(df, {}) == []


def test_breakout_bullish_detected_on_volume_confirmed_break():
    closes = [100 + (i % 3 - 1) * 0.5 for i in range(25)]  # oscillates 99.5-100.5, a clear range
    closes.append(110.0)  # decisive break above the range on the last bar
    volumes = [100.0] * 25 + [300.0]  # 3x average volume confirms it
    highs = [c * 1.002 for c in closes[:-1]] + [111.0]
    lows = [c * 0.998 for c in closes[:-1]] + [109.0]
    df = _enrich(_make_df(closes, volumes, highs, lows))
    hits = _patterns(scan(df, {}), "BREAKOUT", "bullish")
    assert len(hits) == 1
    assert hits[0].details["level"] < 110.0


def test_pullback_bullish_detected_after_dip_and_bounce():
    closes = [100 + 0.3 * i for i in range(35)]  # steady uptrend establishes ema_fast > ema_slow
    closes += [closes[-1] - 1.0, closes[-1] - 2.0, closes[-1] - 2.5]  # dip back toward ema_fast
    opens = list(closes)
    bounce_open = closes[-1]
    bounce_close = closes[-1] + 2.0  # sharp bullish bounce candle
    closes.append(bounce_close)
    opens.append(bounce_open)
    highs = [c * 1.002 for c in closes[:-1]] + [bounce_close + 0.1]
    lows = [c * 0.998 for c in closes[:-1]] + [bounce_open - 0.1]
    df = _enrich(_make_df(closes, opens=opens, highs=highs, lows=lows))
    hits = _patterns(scan(df, {}), "PULLBACK", "bullish")
    assert len(hits) == 1


def test_momentum_bullish_detected_on_sharp_recent_move():
    closes = [100.0] * 20
    for _ in range(7):
        closes.append(closes[-1] * 1.012)  # ~1.2%/bar, last 5 bars comfortably clears the 3% threshold
    df = _enrich(_make_df(closes))
    hits = _patterns(scan(df, {}), "MOMENTUM", "bullish")
    assert len(hits) == 1
    assert hits[0].details["return_pct"] > 0.03


def test_trend_continuation_bullish_detected_after_consolidation_breakout():
    closes = [100 + 0.4 * i for i in range(30)]  # well-established uptrend
    base = closes[-1]
    closes += [base + (i % 2) * 0.2 - 0.1 for i in range(8)]  # tight 8-bar consolidation
    closes.append(max(closes[-8:]) + 3.0)  # clears the consolidation high decisively
    df = _enrich(_make_df(closes))
    hits = _patterns(scan(df, {}), "TREND_CONTINUATION", "bullish")
    assert len(hits) == 1


def test_reversal_bullish_detected_after_capitulation_and_snapback():
    closes = [100.0]
    for _ in range(24):
        closes.append(closes[-1] * 0.99)  # steady decline drives RSI deeply oversold
    opens = list(closes)
    highs = [c * 1.001 for c in closes]
    lows = [c * 0.999 for c in closes]
    new_low = closes[-1] * 0.995
    rev_close = closes[-1] * 1.03  # sharp bullish reversal candle off a fresh low
    closes.append(rev_close)
    opens.append(new_low)
    lows.append(new_low * 0.999)
    highs.append(rev_close * 1.001)
    df = _enrich(_make_df(closes, opens=opens, highs=highs, lows=lows))
    hits = _patterns(scan(df, {}), "REVERSAL", "bullish")
    assert len(hits) == 1
