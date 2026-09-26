"""Pattern scanner: "scans the market 24/7 for potential setups."

Looks at one symbol's recent OHLCV (already enriched with EMA/RSI/ATR via
src.indicators.add_indicators) and reports every pattern candidate it currently
sees. This module never decides whether to trade — it only reports what it
found, with a 0-1 raw `strength` score. src/signal_engine.py is what filters
and prioritizes these candidates into an actual signal.

Five pattern types, matching the scanner design this bot follows:

  BREAKOUT           — price breaks a recent resistance/support with volume.
  PULLBACK           — an established trend dips to a moving average / prior
                        level and bounces (a "buy the dip" / "sell the rip" entry).
  MOMENTUM           — a strong, sustained directional move confirmed by RSI.
  TREND_CONTINUATION — an established trend consolidates, then resumes.
  REVERSAL           — an RSI extreme plus a reversal candle at a swing point.

Everything here is a deterministic, explainable rule over OHLCV + indicators —
no news, social, or on-chain data. Those are real extension points (see
README's "Extending it" section) but need their own data connectors and are
out of scope for this pass.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass
class Candidate:
    pattern: str          # "BREAKOUT" | "PULLBACK" | "MOMENTUM" | "TREND_CONTINUATION" | "REVERSAL"
    direction: str         # "bullish" | "bearish"
    strength: float        # 0.0-1.0, raw pattern-quality score (before signal engine filtering)
    details: dict[str, Any] = field(default_factory=dict)  # levels/values trade_planner will use


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _detect_breakout(df: pd.DataFrame, lookback: int, volume_multiplier: float) -> Candidate | None:
    window = df.iloc[-lookback - 1 : -1]  # exclude current bar from the level calculation
    current = df.iloc[-1]
    if len(window) < lookback or window["volume"].mean() == 0:
        return None

    resistance = window["high"].max()
    support = window["low"].min()
    avg_volume = window["volume"].mean()
    atr_val = current["atr"] if current["atr"] > 0 else current["close"] * 0.01
    volume_ok = current["volume"] > volume_multiplier * avg_volume

    if current["close"] > resistance and volume_ok:
        strength = _clip01(0.5 + (current["close"] - resistance) / atr_val * 0.25)
        return Candidate(
            "BREAKOUT", "bullish", strength,
            {"level": float(resistance), "atr": float(atr_val), "volume_ratio": float(current["volume"] / avg_volume)},
        )
    if current["close"] < support and volume_ok:
        strength = _clip01(0.5 + (support - current["close"]) / atr_val * 0.25)
        return Candidate(
            "BREAKOUT", "bearish", strength,
            {"level": float(support), "atr": float(atr_val), "volume_ratio": float(current["volume"] / avg_volume)},
        )
    return None


def _detect_pullback(df: pd.DataFrame, bounce_lookback: int, tolerance_pct: float) -> Candidate | None:
    current = df.iloc[-1]
    prev = df.iloc[-2]
    recent = df.iloc[-bounce_lookback - 1 : -1]
    if len(recent) < bounce_lookback:
        return None

    atr_val = current["atr"] if current["atr"] > 0 else current["close"] * 0.01
    uptrend = current["ema_fast"] > current["ema_slow"]
    downtrend = current["ema_fast"] < current["ema_slow"]

    touched_fast_ema_from_above = recent["low"].min() <= current["ema_fast"] * (1 + tolerance_pct)
    touched_fast_ema_from_below = recent["high"].max() >= current["ema_fast"] * (1 - tolerance_pct)

    bullish_bounce = current["close"] > prev["high"] and current["close"] > current["open"]
    bearish_bounce = current["close"] < prev["low"] and current["close"] < current["open"]

    if uptrend and touched_fast_ema_from_above and bullish_bounce:
        strength = _clip01(0.5 + (current["close"] - current["ema_fast"]) / atr_val * 0.2)
        return Candidate(
            "PULLBACK", "bullish", strength,
            {"pullback_level": float(current["ema_fast"]), "atr": float(atr_val)},
        )
    if downtrend and touched_fast_ema_from_below and bearish_bounce:
        strength = _clip01(0.5 + (current["ema_fast"] - current["close"]) / atr_val * 0.2)
        return Candidate(
            "PULLBACK", "bearish", strength,
            {"pullback_level": float(current["ema_fast"]), "atr": float(atr_val)},
        )
    return None


def _detect_momentum(df: pd.DataFrame, lookback: int, return_threshold: float) -> Candidate | None:
    current = df.iloc[-1]
    anchor = df.iloc[-lookback - 1]
    if anchor["close"] == 0:
        return None

    move = (current["close"] - anchor["close"]) / anchor["close"]

    if move > return_threshold and current["rsi"] > 60:
        strength = _clip01(0.4 + abs(move) / return_threshold * 0.2)
        return Candidate("MOMENTUM", "bullish", strength, {"return_pct": float(move), "rsi": float(current["rsi"])})
    if move < -return_threshold and current["rsi"] < 40:
        strength = _clip01(0.4 + abs(move) / return_threshold * 0.2)
        return Candidate("MOMENTUM", "bearish", strength, {"return_pct": float(move), "rsi": float(current["rsi"])})
    return None


def _detect_trend_continuation(
    df: pd.DataFrame, trend_confirm_bars: int, consolidation_bars: int, max_range_atr: float
) -> Candidate | None:
    current = df.iloc[-1]
    trend_window = df.iloc[-trend_confirm_bars:]
    consolidation = df.iloc[-consolidation_bars - 1 : -1]
    if len(trend_window) < trend_confirm_bars or len(consolidation) < consolidation_bars:
        return None

    atr_val = current["atr"] if current["atr"] > 0 else current["close"] * 0.01
    established_uptrend = (trend_window["ema_fast"] > trend_window["ema_slow"]).all()
    established_downtrend = (trend_window["ema_fast"] < trend_window["ema_slow"]).all()
    consolidation_range = consolidation["high"].max() - consolidation["low"].min()
    is_consolidating = consolidation_range <= max_range_atr * atr_val

    if established_uptrend and is_consolidating and current["close"] > consolidation["high"].max():
        strength = _clip01(0.55 + (current["close"] - consolidation["high"].max()) / atr_val * 0.2)
        return Candidate(
            "TREND_CONTINUATION", "bullish", strength,
            {"consolidation_high": float(consolidation["high"].max()), "atr": float(atr_val)},
        )
    if established_downtrend and is_consolidating and current["close"] < consolidation["low"].min():
        strength = _clip01(0.55 + (consolidation["low"].min() - current["close"]) / atr_val * 0.2)
        return Candidate(
            "TREND_CONTINUATION", "bearish", strength,
            {"consolidation_low": float(consolidation["low"].min()), "atr": float(atr_val)},
        )
    return None


def _detect_reversal(df: pd.DataFrame, swing_lookback: int, rsi_oversold: float, rsi_overbought: float) -> Candidate | None:
    current = df.iloc[-1]
    recent = df.iloc[-swing_lookback - 1 : -1]
    if len(recent) < swing_lookback:
        return None

    atr_val = current["atr"] if current["atr"] > 0 else current["close"] * 0.01
    was_oversold = recent["rsi"].min() < rsi_oversold
    was_overbought = recent["rsi"].max() > rsi_overbought
    made_new_low = current["low"] <= recent["low"].min()
    made_new_high = current["high"] >= recent["high"].max()
    bullish_reversal_candle = current["close"] > current["open"] and current["close"] > df.iloc[-2]["high"]
    bearish_reversal_candle = current["close"] < current["open"] and current["close"] < df.iloc[-2]["low"]

    if was_oversold and made_new_low and bullish_reversal_candle:
        strength = _clip01(0.5 + (rsi_oversold - recent["rsi"].min()) / rsi_oversold * 0.3)
        return Candidate("REVERSAL", "bullish", strength, {"swing_low": float(recent["low"].min()), "atr": float(atr_val)})
    if was_overbought and made_new_high and bearish_reversal_candle:
        strength = _clip01(0.5 + (recent["rsi"].max() - rsi_overbought) / (100 - rsi_overbought) * 0.3)
        return Candidate("REVERSAL", "bearish", strength, {"swing_high": float(recent["high"].max()), "atr": float(atr_val)})
    return None


def scan(df_with_indicators: pd.DataFrame, cfg: dict[str, Any]) -> list[Candidate]:
    """Run all five pattern detectors and return whichever candidates fired.

    `df_with_indicators` must already have ema_fast/ema_slow/rsi/atr columns
    (see src.indicators.add_indicators) and needs at least ~30 bars of history
    for the longer-lookback detectors to have anything to compare against.
    """
    min_bars = max(
        cfg.get("breakout_lookback", 20),
        cfg.get("pullback_bounce_lookback", 5),
        cfg.get("momentum_lookback", 5),
        cfg.get("trend_confirm_bars", 10) + cfg.get("consolidation_bars", 8),
        cfg.get("reversal_swing_lookback", 10),
    ) + 2
    if len(df_with_indicators) < min_bars:
        return []

    candidates: list[Candidate] = []
    detectors = [
        lambda: _detect_breakout(
            df_with_indicators, cfg.get("breakout_lookback", 20), cfg.get("breakout_volume_multiplier", 1.5)
        ),
        lambda: _detect_pullback(
            df_with_indicators, cfg.get("pullback_bounce_lookback", 5), cfg.get("pullback_tolerance_pct", 0.004)
        ),
        lambda: _detect_momentum(
            df_with_indicators, cfg.get("momentum_lookback", 5), cfg.get("momentum_return_threshold", 0.03)
        ),
        lambda: _detect_trend_continuation(
            df_with_indicators,
            cfg.get("trend_confirm_bars", 10),
            cfg.get("consolidation_bars", 8),
            cfg.get("consolidation_max_range_atr", 2.5),
        ),
        lambda: _detect_reversal(
            df_with_indicators,
            cfg.get("reversal_swing_lookback", 10),
            cfg.get("reversal_rsi_oversold", 30),
            cfg.get("reversal_rsi_overbought", 70),
        ),
    ]
    for detector in detectors:
        result = detector()
        if result is not None:
            candidates.append(result)
    return candidates
