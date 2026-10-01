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


def _bullish_pullback_ohlcv(current_volume: float = 130.0):
    """Uptrend, a 3-bar dip toward ema_fast, then TWO consecutive bullish bounce
    bars: `prev` closes above its own prior bar's high (the dip's last bar) and
    above its own open, and `current` extends that by closing above `prev`'s
    high and above its own open. Represents what a genuine, now-qualifying
    pullback entry looks like under the two-bar-confirmation rule."""
    closes = [100 + 0.3 * i for i in range(35)]  # steady uptrend establishes ema_fast > ema_slow
    closes += [closes[-1] - 1.0, closes[-1] - 2.0, closes[-1] - 2.5]  # 3-bar dip back toward ema_fast
    opens = list(closes)
    highs = [c * 1.002 for c in closes]
    lows = [c * 0.998 for c in closes]

    d3_close, d3_high = closes[-1], highs[-1]

    b1_open = d3_close
    b1_close = d3_high + 1.0  # bar 1 (becomes `prev`): confirms above d3's high and its own open
    b1_high = b1_close + 0.1
    b1_low = min(b1_open, d3_close) - 0.1

    b2_open = b1_close - 0.2
    b2_close = b1_high + 1.0  # bar 2 (becomes `current`): extends above b1's high and its own open
    b2_high = b2_close + 0.1
    b2_low = min(b2_open, b1_close) - 0.1

    closes += [b1_close, b2_close]
    opens += [b1_open, b2_open]
    highs += [b1_high, b2_high]
    lows += [b1_low, b2_low]
    volumes = [100.0] * 39 + [current_volume]
    return closes, opens, highs, lows, volumes


def _bearish_pullback_ohlcv(current_volume: float = 130.0):
    """Mirror of _bullish_pullback_ohlcv: downtrend, a 3-bar rally toward
    ema_fast, then two consecutive bearish rejection bars."""
    closes = [100 - 0.3 * i for i in range(35)]  # steady downtrend establishes ema_fast < ema_slow
    closes += [closes[-1] + 1.0, closes[-1] + 2.0, closes[-1] + 2.5]  # 3-bar rally back toward ema_fast
    opens = list(closes)
    highs = [c * 1.002 for c in closes]
    lows = [c * 0.998 for c in closes]

    d3_close, d3_low = closes[-1], lows[-1]

    b1_open = d3_close
    b1_close = d3_low - 1.0  # bar 1 (becomes `prev`): confirms below d3's low and its own open
    b1_low = b1_close - 0.1
    b1_high = max(b1_open, d3_close) + 0.1

    b2_open = b1_close + 0.2
    b2_close = b1_low - 1.0  # bar 2 (becomes `current`): extends below b1's low and its own open
    b2_low = b2_close - 0.1
    b2_high = max(b2_open, b1_close) + 0.1

    closes += [b1_close, b2_close]
    opens += [b1_open, b2_open]
    highs += [b1_high, b2_high]
    lows += [b1_low, b2_low]
    volumes = [100.0] * 39 + [current_volume]
    return closes, opens, highs, lows, volumes


def test_pullback_bullish_detected_after_dip_and_two_bar_bounce():
    closes, opens, highs, lows, volumes = _bullish_pullback_ohlcv(current_volume=130.0)
    df = _enrich(_make_df(closes, volumes=volumes, opens=opens, highs=highs, lows=lows))
    hits = _patterns(scan(df, {}), "PULLBACK", "bullish")
    assert len(hits) == 1


def test_pullback_bearish_detected_after_rally_and_two_bar_rejection():
    closes, opens, highs, lows, volumes = _bearish_pullback_ohlcv(current_volume=130.0)
    df = _enrich(_make_df(closes, volumes=volumes, opens=opens, highs=highs, lows=lows))
    hits = _patterns(scan(df, {}), "PULLBACK", "bearish")
    assert len(hits) == 1


def test_pullback_bullish_single_bar_whipsaw_no_longer_detected():
    """The old single-bar-spike shape (dip for 3 bars, then one sharp bounce
    candle with no confirming bar before it) must NOT produce a PULLBACK
    candidate anymore — this is exactly the low-conviction entry the two-bar
    confirmation rule exists to filter out."""
    closes = [100 + 0.3 * i for i in range(35)]
    closes += [closes[-1] - 1.0, closes[-1] - 2.0, closes[-1] - 2.5]
    opens = list(closes)
    bounce_open = closes[-1]
    bounce_close = closes[-1] + 2.0  # single sharp bullish bounce candle, no prior confirming bar
    closes.append(bounce_close)
    opens.append(bounce_open)
    highs = [c * 1.002 for c in closes[:-1]] + [bounce_close + 0.1]
    lows = [c * 0.998 for c in closes[:-1]] + [bounce_open - 0.1]
    df = _enrich(_make_df(closes, opens=opens, highs=highs, lows=lows))
    hits = _patterns(scan(df, {}), "PULLBACK", "bullish")
    assert len(hits) == 0


def test_pullback_bullish_two_bar_bounce_with_below_average_volume_not_detected():
    """Same genuinely two-bar-confirmed bounce shape, but current's volume is
    below the recent lookback window's average — must NOT produce a candidate."""
    closes, opens, highs, lows, volumes = _bullish_pullback_ohlcv(current_volume=50.0)
    df = _enrich(_make_df(closes, volumes=volumes, opens=opens, highs=highs, lows=lows))
    hits = _patterns(scan(df, {}), "PULLBACK", "bullish")
    assert len(hits) == 0


def test_pullback_bearish_two_bar_bounce_with_below_average_volume_not_detected():
    closes, opens, highs, lows, volumes = _bearish_pullback_ohlcv(current_volume=50.0)
    df = _enrich(_make_df(closes, volumes=volumes, opens=opens, highs=highs, lows=lows))
    hits = _patterns(scan(df, {}), "PULLBACK", "bearish")
    assert len(hits) == 0


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
    # This fixture is plain capitulation (one continuous decline into the
    # reversal candle), not real two-swing RSI divergence — see
    # test_reversal_bullish_with_real_divergence_scores_higher_than_without below.
    assert hits[0].details["divergence"] is False


def test_breakout_adx_confirmed_trend_scores_higher_than_choppy_breakout():
    """Same-shaped breakout (decisive close above resistance, 3x volume) but one
    fixture's preceding history is a strong sustained uptrend (high ADX) and the
    other's is a flat chop (low ADX). ADX confirmation should be a strength
    bonus on the trending case, not a gate — both still fire as BREAKOUT."""
    jump = 1.5

    # Choppy history: oscillates with no sustained direction -> low ADX.
    choppy_closes = [100 + (i % 3 - 1) * 0.5 for i in range(45)]
    choppy_closes.append(choppy_closes[-1] + jump)
    choppy_volumes = [100.0] * 45 + [300.0]  # 3x average volume confirms it
    choppy_highs = [c * 1.002 for c in choppy_closes[:-1]] + [choppy_closes[-1] + 0.2]
    choppy_lows = [c * 0.998 for c in choppy_closes[:-1]] + [choppy_closes[-1] - 0.2]
    choppy_df = _enrich(_make_df(choppy_closes, choppy_volumes, choppy_highs, choppy_lows))
    choppy_hits = _patterns(scan(choppy_df, {}), "BREAKOUT", "bullish")

    # Trending history: a clean 30-bar uptrend then a short tail -> high ADX.
    trend_closes = [100 + 0.6 * i for i in range(30)]
    trend_closes += [trend_closes[-1] + (i % 3 - 1) * 0.5 for i in range(15)]
    trend_closes.append(max(trend_closes[-20:]) + jump)
    trend_volumes = [100.0] * 45 + [300.0]
    trend_highs = [c * 1.002 for c in trend_closes[:-1]] + [trend_closes[-1] + 0.2]
    trend_lows = [c * 0.998 for c in trend_closes[:-1]] + [trend_closes[-1] - 0.2]
    trend_df = _enrich(_make_df(trend_closes, trend_volumes, trend_highs, trend_lows))
    trend_hits = _patterns(scan(trend_df, {}), "BREAKOUT", "bullish")

    assert len(choppy_hits) == 1 and len(trend_hits) == 1
    assert choppy_hits[0].details["adx"] < 25
    assert trend_hits[0].details["adx"] >= 25
    assert trend_hits[0].strength > choppy_hits[0].strength


def test_reversal_bullish_with_real_divergence_scores_higher_than_without():
    """A genuine two-swing bullish RSI divergence (second low undercuts the
    first swing low, but RSI at the second low is higher than RSI was at the
    first) should score higher than an otherwise-equivalent plain capitulation
    reversal with no real divergence, at the same swing_lookback."""
    swing_lookback = 14
    cfg = {"reversal_swing_lookback": swing_lookback}

    # Two-leg structure: sharp leg down (deep oversold swing low) -> bounce ->
    # mild leg down -> reversal candle that ekes out a marginal new low just
    # under the first swing low, while RSI has recovered well off its floor.
    closes = [100.0] * 20
    for _ in range(4):
        closes.append(closes[-1] * (1 - 0.035))  # leg 1: sharp decline
    leg1_low = closes[-1]
    for _ in range(5):
        closes.append(closes[-1] * (1 + 0.02))  # bounce
    for _ in range(5):
        closes.append(closes[-1] * (1 - 0.006))  # leg 2: mild decline
    opens = list(closes)
    highs = [c * 1.001 for c in closes]
    lows = [c * 0.999 for c in closes]
    new_low = leg1_low * (1 - 0.003)  # marginal new low, just under leg 1's low
    rev_close = closes[-1] * (1 + 0.02)
    closes.append(rev_close)
    opens.append(new_low * 1.001)
    lows.append(new_low)
    highs.append(rev_close * 1.001)
    divergence_df = _enrich(_make_df(closes, opens=opens, highs=highs, lows=lows))
    divergence_hits = _patterns(scan(divergence_df, cfg), "REVERSAL", "bullish")

    # Matched, same-length plain capitulation: one continuous decline straight
    # into the reversal candle (no real second swing) -> no divergence.
    closes2 = [100.0] * 20
    for _ in range(swing_lookback):
        closes2.append(closes2[-1] * (1 - 0.015))
    opens2 = list(closes2)
    highs2 = [c * 1.001 for c in closes2]
    lows2 = [c * 0.999 for c in closes2]
    new_low2 = closes2[-1] * 0.995
    rev_close2 = closes2[-1] * 1.02
    closes2.append(rev_close2)
    opens2.append(new_low2)
    lows2.append(new_low2 * 0.999)
    highs2.append(rev_close2 * 1.001)
    plain_df = _enrich(_make_df(closes2, opens=opens2, highs=highs2, lows=lows2))
    plain_hits = _patterns(scan(plain_df, cfg), "REVERSAL", "bullish")

    assert len(divergence_hits) == 1 and len(plain_hits) == 1
    assert divergence_hits[0].details["divergence"] is True
    assert plain_hits[0].details["divergence"] is False
    assert divergence_hits[0].strength > plain_hits[0].strength
