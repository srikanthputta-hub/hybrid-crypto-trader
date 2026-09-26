"""Signal engine: "filters noise and generates high-probability trade signals."

Takes the raw list of pattern Candidates the scanner found and turns them into
at most one prioritized Signal — or None, if nothing here clears the bar.

Rules, in order:
1. Drop any candidate below `min_strength` (weak/noisy detections).
2. If the surviving candidates disagree on direction (some bullish, some
   bearish), that's conflicting evidence — treat it as noise and return None,
   rather than guessing which side is "more right."
3. Otherwise, candidates agreeing on direction reinforce each other
   (confluence): the engine score is the strongest single candidate's
   strength, boosted a bit for each additional pattern that agrees, capped
   at 1.0.
4. The single candidate with the highest strength becomes the signal's
   "primary pattern" — that's what src/trade_planner.py uses to compute the
   entry zone/stop/target (each pattern type implies a different kind of
   level). Every pattern name that agreed is kept in `supporting_patterns`
   for the rationale/audit trail.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.scanner import Candidate

DEFAULT_PATTERN_PRIORITY = [
    "BREAKOUT",
    "TREND_CONTINUATION",
    "MOMENTUM",
    "PULLBACK",
    "REVERSAL",
]


@dataclass
class Signal:
    direction: str              # "bullish" | "bearish"
    primary_pattern: str        # the Candidate.pattern used to build the trade plan
    engine_score: float         # 0.0-1.0, confluence-adjusted
    supporting_patterns: list[str] = field(default_factory=list)
    primary_details: dict[str, Any] = field(default_factory=dict)
    rationale: dict[str, Any] = field(default_factory=dict)


def evaluate(
    candidates: list[Candidate],
    min_strength: float = 0.5,
    confluence_bonus: float = 0.08,
    pattern_priority: list[str] | None = None,
) -> Signal | None:
    pattern_priority = pattern_priority or DEFAULT_PATTERN_PRIORITY
    strong = [c for c in candidates if c.strength >= min_strength]
    if not strong:
        return None

    bullish = [c for c in strong if c.direction == "bullish"]
    bearish = [c for c in strong if c.direction == "bearish"]
    if bullish and bearish:
        # Conflicting signals on the same bar — treat as noise, not a trade.
        return None

    group = bullish or bearish
    direction = "bullish" if bullish else "bearish"

    def priority_key(c: Candidate) -> tuple[int, float]:
        try:
            rank = pattern_priority.index(c.pattern)
        except ValueError:
            rank = len(pattern_priority)
        return (rank, -c.strength)

    # Highest strength wins; ties broken by the configured pattern priority order.
    primary = min(group, key=lambda c: (-c.strength, priority_key(c)[0]))

    engine_score = primary.strength + confluence_bonus * (len(group) - 1)
    engine_score = max(0.0, min(1.0, engine_score))

    return Signal(
        direction=direction,
        primary_pattern=primary.pattern,
        engine_score=engine_score,
        supporting_patterns=[c.pattern for c in group],
        primary_details=primary.details,
        rationale={
            "candidates": [
                {"pattern": c.pattern, "direction": c.direction, "strength": round(c.strength, 3)}
                for c in candidates
            ],
            "min_strength": min_strength,
        },
    )
