from unittest.mock import MagicMock, patch

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


def _mock_response(text: str) -> MagicMock:
    block = MagicMock()
    block.text = text
    response = MagicMock()
    response.content = [block]
    return response


def test_happy_path_approve():
    with patch("src.claude_advisor.anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_response(
            '{"verdict": "APPROVE", "confidence_stars": 4.5, "reasoning": "Looks solid."}'
        )
        mock_anthropic.return_value = mock_client

        verdict = review_trade_plan(api_key="fake-key", fail_mode="closed", **BASE_KWARGS)

    assert verdict.verdict == "APPROVE"
    assert verdict.approved is True
    assert verdict.confidence_stars == 4.5


def test_output_config_json_schema_sent_to_api():
    with patch("src.claude_advisor.anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_response(
            '{"verdict": "APPROVE", "confidence_stars": 4.0, "reasoning": "ok"}'
        )
        mock_anthropic.return_value = mock_client

        review_trade_plan(api_key="fake-key", fail_mode="closed", **BASE_KWARGS)

    _, kwargs = mock_client.messages.create.call_args
    output_config = kwargs["output_config"]
    assert output_config["format"]["type"] == "json_schema"
    required = output_config["format"]["schema"]["required"]
    assert "verdict" in required
    assert "confidence_stars" in required
    assert "reasoning" in required


def test_happy_path_veto():
    with patch("src.claude_advisor.anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_response(
            '{"verdict": "VETO", "confidence_stars": 2.0, "reasoning": "Poor risk/reward."}'
        )
        mock_anthropic.return_value = mock_client

        verdict = review_trade_plan(api_key="fake-key", fail_mode="closed", **BASE_KWARGS)

    assert verdict.verdict == "VETO"
    assert verdict.approved is False


def test_confidence_stars_clamped_above_range():
    with patch("src.claude_advisor.anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_response(
            '{"verdict": "APPROVE", "confidence_stars": 7.0, "reasoning": "Very confident."}'
        )
        mock_anthropic.return_value = mock_client

        verdict = review_trade_plan(api_key="fake-key", fail_mode="closed", **BASE_KWARGS)

    assert verdict.confidence_stars == 5.0


def test_confidence_stars_clamped_below_range():
    with patch("src.claude_advisor.anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_response(
            '{"verdict": "APPROVE", "confidence_stars": 0.5, "reasoning": "Barely confident."}'
        )
        mock_anthropic.return_value = mock_client

        verdict = review_trade_plan(api_key="fake-key", fail_mode="closed", **BASE_KWARGS)

    assert verdict.confidence_stars == 1.0


def test_malformed_json_response_fails_closed():
    with patch("src.claude_advisor.anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_response("not json")
        mock_anthropic.return_value = mock_client

        verdict = review_trade_plan(api_key="fake-key", fail_mode="closed", **BASE_KWARGS)

    assert verdict.verdict == "VETO"
    assert verdict.approved is False
    assert verdict.confidence_stars is None


def test_unexpected_verdict_value_fails_closed():
    with patch("src.claude_advisor.anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_response(
            '{"verdict": "MAYBE", "confidence_stars": 3.0, "reasoning": "Unsure."}'
        )
        mock_anthropic.return_value = mock_client

        verdict = review_trade_plan(api_key="fake-key", fail_mode="closed", **BASE_KWARGS)

    assert verdict.verdict == "VETO"
    assert verdict.approved is False
    assert verdict.confidence_stars is None
