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


def rank_signals(signals: dict[str, Signal], liquidity: dict[str, float]) -> list[str]:
    """Deterministically order symbols by signal strength, for scarce-slot processing order.

    Pure function — no I/O, no mutation, no randomness, no wall-clock dependency. This is
    Phase 1c of the system redesign (docs/system-redesign-2026-10.md §4.1): when
    `max_total_exposure_pct` or `max_new_entries_per_window` bind, something has to decide
    which symbols get the remaining slots. Today that's `market.symbols` config order,
    an unexamined selection rule once the universe grows past a handful of symbols. This
    function is registered here, pre-tuning, so it can be validated against the existing
    backtest datasets before (a separate decision) anything wires it into the live cycle.

    Sort key: `(-engine_score, -median_quote_volume_30d, symbol)` — highest engine_score
    first, ties broken by higher liquidity, remaining ties broken alphabetically by symbol
    so the order is always fully determined (no two inputs can compare equal).

    Missing/``None`` liquidity (e.g. every `UniverseMember` in today's static mode, which
    always has `median_quote_volume_30d=None`) is treated as strictly lower priority than
    any known liquidity value, including zero or a very small one — not as equivalent to
    zero (which would rank it ahead of a negative-volume value, impossible in practice but
    semantically wrong) and not as an error. Concretely, a missing/`None` entry sorts as
    though its liquidity were `+inf` on the `-median_quote_volume_30d` sort key, i.e. always
    last among ties on `engine_score` — this is a tie-break, so `engine_score` still
    dominates: a `None`-liquidity symbol with a higher `engine_score` still outranks a
    known-liquidity symbol with a lower one.

    Only symbols present in `signals` are returned. `liquidity` may contain extra symbols
    (e.g. the whole universe) or be missing entries for some signaled symbols — either is
    fine and treated as unknown liquidity for that symbol.
    """

    def sort_key(symbol: str) -> tuple[float, float, str]:
        volume = liquidity.get(symbol)
        liquidity_key = -volume if volume is not None else float("inf")
        return (-signals[symbol].engine_score, liquidity_key, symbol)

    return sorted(signals, key=sort_key)
