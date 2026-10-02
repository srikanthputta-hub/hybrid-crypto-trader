"""Trade planner: "creates the trade plan with entry, target, stop loss, and invalidation."

Turns a single Signal (from src.signal_engine) into a concrete, fully-specified
TradePlan: an entry zone (not a single price — a range you'd be comfortable
entering across), a stop loss, a take-profit target, an invalidation level, the
resulting risk/reward ratio, and a rule-based 1-5 star confidence score.

This bot is long-only spot, so a plan is only ever built for a bullish signal —
a bearish signal means "exit an existing long if you're holding one," which is
handled directly in the main loop (src/bot.py), not as a new trade plan.

Terminology, matching the diagram this design is based on:
  entry_zone   — a small range around the trigger level you'd accept a fill in.
  stop_loss    — where you cut the trade because it didn't work out (a risk exit).
  invalidation — a level further out where the underlying thesis itself is wrong
                 (e.g. the broken resistance completely fails as support again).
                 Distinct from stop_loss on purpose: stop_loss is *when* you exit
                 to cap risk; invalidation is *why* the setup no longer applies.
  take_profit  — the target, sized off the configured minimum risk/reward.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.signal_engine import Signal

# Which key in a Candidate's `details` dict holds "the level" for each pattern —
# i.e. the price the entry zone is built around. Patterns without a natural
# level (pure momentum, for instance) fall back to the current price.
_LEVEL_KEYS = {
    "BREAKOUT": "level",
    "PULLBACK": "pullback_level",
    "TREND_CONTINUATION": "consolidation_high",  # bearish plans aren't built (long-only), so only the bullish key is needed
    "REVERSAL": "swing_low",
}


@dataclass
class TradePlan:
    symbol: str
    side: str  # always "buy" — this planner only ever originates long entries
    pattern: str
    entry_zone: tuple[float, float]
    stop_loss: float
    take_profit: float
    invalidation: float
    risk_reward: float
    rule_based_confidence_stars: float
    supporting_patterns: list[str] = field(default_factory=list)
    rationale: dict[str, Any] = field(default_factory=dict)


def _primary_level(pattern: str, details: dict[str, Any], current_price: float, atr: float) -> float:
    key = _LEVEL_KEYS.get(pattern)
    level = details.get(key) if key else None
    if level is None:
        return current_price
    # Keep the level from landing exactly on the current price (zero-width zone).
    if abs(level - current_price) < atr * 0.05:
        return current_price - atr * 0.05
    return float(level)


def build_plan(
    signal: Signal,
    symbol: str,
    current_price: float,
    atr: float,
    entry_buffer_atr_mult: float = 0.1,
    stop_atr_mult: float = 1.5,
    invalidation_atr_mult: float = 0.5,
    min_risk_reward: float = 2.0,
    adx_trend_threshold: float = 25,
    trend_stop_atr_bonus: float = 0.3,
) -> TradePlan | None:
    if signal.direction != "bullish":
        return None  # long-only: a bearish signal is handled as an exit, not a new plan

    atr = atr if atr and atr > 0 else current_price * 0.01
    level = _primary_level(signal.primary_pattern, signal.primary_details, current_price, atr)

    entry_low = min(level, current_price) - entry_buffer_atr_mult * atr
    entry_high = max(level, current_price) + entry_buffer_atr_mult * atr

    # Regime filter: an ADX-confirmed trend (see scanner.py, which populates
    # primary_details["adx"] for BREAKOUT/MOMENTUM/TREND_CONTINUATION) widens
    # the stop modestly. In a real trend, normal pullback noise is wider than
    # in a choppy market, so a fixed ATR stop that's fine in chop shakes
    # traders out of genuinely-working trend trades; this only ever widens,
    # never tightens, and only activates on explicit ADX confirmation.
    adx_value = signal.primary_details.get("adx")
    adx_trend_confirmed = adx_value is not None and adx_value >= adx_trend_threshold
    effective_stop_atr_mult = stop_atr_mult + trend_stop_atr_bonus if adx_trend_confirmed else stop_atr_mult

    stop_loss = entry_low - effective_stop_atr_mult * atr
    invalidation = stop_loss - invalidation_atr_mult * atr
    risk_per_unit = entry_high - stop_loss
    take_profit = entry_high + min_risk_reward * risk_per_unit
    risk_reward = (take_profit - entry_high) / risk_per_unit if risk_per_unit > 0 else 0.0

    rr_component = min(1.0, risk_reward / 3.0)
    score = 0.6 * signal.engine_score + 0.4 * rr_component
    stars = round((1.0 + 4.0 * score) * 2) / 2  # nearest 0.5, clamped to [1.0, 5.0] by construction

    rationale = dict(signal.rationale)
    rationale["stop_atr_mult_used"] = effective_stop_atr_mult
    rationale["adx_trend_confirmed"] = adx_trend_confirmed

    return TradePlan(
        symbol=symbol,
        side="buy",
        pattern=signal.primary_pattern,
        entry_zone=(round(entry_low, 8), round(entry_high, 8)),
        stop_loss=round(stop_loss, 8),
        take_profit=round(take_profit, 8),
        invalidation=round(invalidation, 8),
        risk_reward=round(risk_reward, 3),
        rule_based_confidence_stars=stars,
        supporting_patterns=list(signal.supporting_patterns),
        rationale=rationale,
    )
