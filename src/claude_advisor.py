"""Optional second opinion from Claude on a trade plan the algo pipeline wants to execute.

Design intent: Claude can only make the bot MORE conservative, never less.
- The scanner + signal engine + trade planner (src/scanner.py, src/signal_engine.py,
  src/trade_planner.py) are the only things that can originate a trade plan (entry
  zone, stop loss, target, invalidation level). Claude never sees a blank slate.
- This module is consulted only when a full trade plan already exists, and it can
  VETO that plan (turn it into no-trade) or APPROVE it. It cannot invent a new
  trade, and it cannot change the plan's entry/stop/target/size — only whether the
  plan proceeds at all.
- It also assigns its OWN 1-5 star confidence rating, shown side by side with the
  rule-based confidence score computed in src/trade_planner.py, so you can see
  where the two agree or disagree.
- If the API call fails, times out, or returns something we can't parse, the
  default (config `advisor.fail_mode`) is "closed" — treat it as a veto —
  because a broken advisor should never silently let more trades through.
  confidence_stars is always None in that case (we didn't get a real opinion,
  so we don't fabricate one).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a cautious risk-review assistant embedded in an automated crypto \
trading bot. A separate, deterministic pipeline (pattern scanner -> signal engine -> trade \
planner) has already built a complete candidate trade plan from technical indicators — entry \
zone, stop loss, take profit target, and an invalidation level where the whole thesis would be \
considered wrong. Your job has two parts:

1. Decide APPROVE or VETO for this specific plan. You cannot originate a different trade, and \
you cannot change its entry/stop/target/size — only whether it proceeds at all.
2. Give your OWN confidence rating for this setup, 1.0-5.0 stars (a rule-based score is computed \
separately; yours is shown alongside it, not averaged with it, so give your independent read).

Consider: does the proposed plan plausibly match the indicator/pattern data given? Is there \
anything in the provided context (e.g. unusually extreme readings, poor risk/reward, conflicting \
signals) that suggests this is a low-quality or suspicious setup? You are not a market oracle and \
have no live news feed unless one is given to you in the message — do not invent facts about \
current events.

Your response is constrained by a JSON schema: "verdict" must be "APPROVE" or "VETO", \
"confidence_stars" is a number from 1.0-5.0, and "reasoning" is one or two sentences.
"""


@dataclass
class AdvisorVerdict:
    verdict: str  # "APPROVE" or "VETO"
    confidence_stars: float | None  # 1.0-5.0; None when we didn't get a real opinion
    reasoning: str
    raw_error: str | None = None

    @property
    def approved(self) -> bool:
        return self.verdict == "APPROVE"


def _fallback(fail_mode: str, reasoning: str, error: str) -> AdvisorVerdict:
    verdict = "APPROVE" if fail_mode == "open" else "VETO"
    return AdvisorVerdict(verdict=verdict, confidence_stars=None, reasoning=reasoning, raw_error=error)


def review_trade_plan(
    *,
    api_key: str | None,
    model: str,
    symbol: str,
    side: str,
    pattern: str,
    entry_zone: tuple[float, float],
    stop_loss: float,
    take_profit: float,
    invalidation: float,
    risk_reward: float,
    rule_based_confidence_stars: float,
    rationale: dict[str, Any],
    portfolio_context: dict[str, Any],
    fail_mode: str = "closed",
    timeout_seconds: int = 20,
) -> AdvisorVerdict:
    if not api_key:
        return _fallback(fail_mode, "advisor enabled but ANTHROPIC_API_KEY is not set", "missing_api_key")

    plan_summary = {
        "symbol": symbol,
        "side": side,
        "pattern": pattern,
        "entry_zone": entry_zone,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "invalidation": invalidation,
        "risk_reward": round(risk_reward, 2),
        "rule_based_confidence_stars": rule_based_confidence_stars,
    }
    user_message = (
        f"Proposed trade plan: {json.dumps(plan_summary)}\n"
        f"Strategy/pattern rationale: {json.dumps(rationale)}\n"
        f"Current portfolio context: {json.dumps(portfolio_context)}\n"
        "Give your verdict and your own confidence rating on this specific plan only."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=timeout_seconds)
        response = client.messages.create(
            model=model,
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "verdict": {"type": "string", "enum": ["APPROVE", "VETO"]},
                            "confidence_stars": {"type": "number"},
                            "reasoning": {"type": "string"},
                        },
                        "required": ["verdict", "confidence_stars", "reasoning"],
                        "additionalProperties": False,
                    },
                }
            },
        )
        text = "".join(block.text for block in response.content if hasattr(block, "text")).strip()
        data = json.loads(text)
        verdict = str(data["verdict"]).upper()
        if verdict not in ("APPROVE", "VETO"):
            raise ValueError(f"unexpected verdict value: {verdict!r}")
        stars = float(data.get("confidence_stars", 3.0))
        stars = max(1.0, min(5.0, stars))
        return AdvisorVerdict(
            verdict=verdict,
            confidence_stars=stars,
            reasoning=str(data.get("reasoning", "")),
        )
    except Exception as exc:  # noqa: BLE001 — any failure here must fail safe, not crash the bot
        logger.warning("Claude advisor call failed, falling back (%s): %s", fail_mode, exc)
        return _fallback(fail_mode, "advisor call failed, see raw_error", str(exc))
