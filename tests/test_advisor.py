from src.claude_advisor import review_trade_plan

BASE_KWARGS = dict(
    model="claude-sonnet-4-5",
    symbol="BTC/USDT",
    side="buy",
    pattern="BREAKOUT",
    entry_zone=(49500.0, 50000.0),
    stop_loss=48000.0,
    take_profit=53000.0,
    invalidation=47500.0,
    risk_reward=2.3,
    rule_based_confidence_stars=4.0,
    rationale={},
    portfolio_context={},
)


def test_missing_api_key_fails_closed_by_default():
    verdict = review_trade_plan(api_key=None, fail_mode="closed", **BASE_KWARGS)
    assert verdict.verdict == "VETO"
    assert not verdict.approved
    assert verdict.confidence_stars is None


def test_missing_api_key_fails_open_when_configured():
    verdict = review_trade_plan(api_key=None, fail_mode="open", **BASE_KWARGS)
    assert verdict.verdict == "APPROVE"
    assert verdict.approved
    assert verdict.confidence_stars is None
