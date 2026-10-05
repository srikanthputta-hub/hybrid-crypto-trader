"""Tests for src/universe.py -- Phase 1b of docs/system-redesign-2026-10.md
(see its §3, §4.1, §4.2). Covers `static` mode only: `mode: "discover"` isn't
implemented yet and must raise NotImplementedError rather than do anything
undefined.
"""
from __future__ import annotations

import yaml
import pytest

from src.config import Config
from src.universe import Universe, UniverseMember, refresh

PROJECT_SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "XRP/USDT",
    "SOL/USDT",
    "ADA/USDT",
    "DOGE/USDT",
    "AVAX/USDT",
    "LINK/USDT",
    "BNB/USDT",
    "LTC/USDT",
    "DOT/USDT",
    "ATOM/USDT",
    "ALGO/USDT",
    "BCH/USDT",
    "XTZ/USDT",
    "SHIB/USDT",
]


class ExchangeThatMustNotBeCalled:
    """A fake exchange client whose every method raises -- static mode must
    never touch `exchange` at all. Mirrors the ExchangeClient surface area
    (fetch_ohlcv_df, fetch_ticker_price, load_markets, create_market_order)."""

    def __getattr__(self, name):
        def _boom(*args, **kwargs):
            raise AssertionError(f"static mode must not call exchange.{name}()")

        return _boom


def _cfg(raw_extra: dict | None = None) -> Config:
    raw = {
        "mode": "paper",
        "exchange": {"id": "kraken", "testnet": False},
        "market": {"symbols": list(PROJECT_SYMBOLS), "timeframe": "1h", "history_bars": 200},
        "indicators": {"fast_ema": 12, "slow_ema": 26, "rsi_period": 14},
        "risk": {
            "starting_cash_usd": 10000,
            "max_position_pct": 0.1,
            "max_total_exposure_pct": 0.4,
            "max_loss_per_trade_pct": 0.01,
            "max_daily_loss_pct": 0.05,
            "max_volatility_pct": 0.08,
            "min_risk_reward": 1.0,
            "cooldown_minutes": 60,
        },
    }
    if raw_extra:
        raw.update(raw_extra)
    return Config(raw=raw)


# --- static mode: reproduces market.symbols exactly -------------------------


def test_static_mode_reproduces_market_symbols_exactly():
    cfg = _cfg({"universe": {"mode": "static"}})
    universe = refresh(ExchangeThatMustNotBeCalled(), cfg)

    assert isinstance(universe, Universe)
    assert {m.symbol for m in universe.members} == set(PROJECT_SYMBOLS)
    assert len(universe.members) == len(PROJECT_SYMBOLS)  # no duplicates
    assert all(m.source == "pinned" for m in universe.members)
    assert universe.excluded == {}


def test_static_mode_members_have_no_fetched_fields():
    cfg = _cfg({"universe": {"mode": "static"}})
    universe = refresh(ExchangeThatMustNotBeCalled(), cfg)

    for m in universe.members:
        assert isinstance(m, UniverseMember)
        assert m.median_quote_volume_30d is None
        assert m.cost_min is None
        assert m.amount_min is None


def test_static_mode_members_have_correct_base_quote():
    cfg = _cfg({"universe": {"mode": "static"}})
    universe = refresh(ExchangeThatMustNotBeCalled(), cfg)

    by_symbol = {m.symbol: m for m in universe.members}
    assert by_symbol["BTC/USDT"].base == "BTC"
    assert by_symbol["BTC/USDT"].quote == "USDT"


def test_static_mode_defaults_when_universe_block_is_absent():
    # config.universe_cfg defaults to {"mode": "static"} when the block is
    # omitted entirely -- matches today's "market.symbols is the whole
    # universe" behavior with no config change required.
    cfg = _cfg()
    universe = refresh(ExchangeThatMustNotBeCalled(), cfg)
    assert {m.symbol for m in universe.members} == set(PROJECT_SYMBOLS)


# --- held-symbol handling ----------------------------------------------------


def test_held_symbol_not_in_market_symbols_is_added_as_held():
    cfg = _cfg({"universe": {"mode": "static"}})
    universe = refresh(ExchangeThatMustNotBeCalled(), cfg, held_symbols=["FOO/USD"])

    held = [m for m in universe.members if m.symbol == "FOO/USD"]
    assert len(held) == 1
    assert held[0].source == "held"
    assert held[0].base == "FOO"
    assert held[0].quote == "USD"
    # didn't disturb the pinned members
    assert {m.symbol for m in universe.members if m.source == "pinned"} == set(PROJECT_SYMBOLS)
    assert len(universe.members) == len(PROJECT_SYMBOLS) + 1


def test_held_symbol_already_in_market_symbols_is_not_duplicated():
    cfg = _cfg({"universe": {"mode": "static"}})
    universe = refresh(ExchangeThatMustNotBeCalled(), cfg, held_symbols=["BTC/USDT"])

    matches = [m for m in universe.members if m.symbol == "BTC/USDT"]
    assert len(matches) == 1
    assert matches[0].source == "pinned"  # pinned wins, not downgraded to held
    assert len(universe.members) == len(PROJECT_SYMBOLS)


def test_multiple_held_symbols_including_a_repeat_are_deduped():
    cfg = _cfg({"universe": {"mode": "static"}})
    universe = refresh(
        ExchangeThatMustNotBeCalled(), cfg, held_symbols=["FOO/USD", "FOO/USD", "BAR/USD"]
    )

    held_symbols = sorted(m.symbol for m in universe.members if m.source == "held")
    assert held_symbols == ["BAR/USD", "FOO/USD"]
    assert len(universe.members) == len(PROJECT_SYMBOLS) + 2


# --- discover mode is explicitly not implemented -----------------------------


@pytest.mark.parametrize("mode", ["discover", "DISCOVER", "bogus", ""])
def test_non_static_mode_raises_not_implemented(mode):
    cfg = _cfg({"universe": {"mode": mode}})
    with pytest.raises(NotImplementedError):
        refresh(ExchangeThatMustNotBeCalled(), cfg)


def test_discover_mode_does_not_silently_no_op_or_crash_differently():
    cfg = _cfg({"universe": {"mode": "discover"}})
    with pytest.raises(NotImplementedError) as exc_info:
        refresh(ExchangeThatMustNotBeCalled(), cfg)
    assert "discover" in str(exc_info.value)


# --- no exchange calls in static mode ----------------------------------------


def test_static_mode_makes_zero_exchange_calls():
    # ExchangeThatMustNotBeCalled raises AssertionError on any attribute
    # access-then-call; refresh() succeeding at all here is the assertion.
    cfg = _cfg({"universe": {"mode": "static"}})
    universe = refresh(ExchangeThatMustNotBeCalled(), cfg, held_symbols=["FOO/USD"])
    assert len(universe.members) == len(PROJECT_SYMBOLS) + 1


# --- malformed symbol handling ------------------------------------------------


def test_malformed_held_symbol_raises_value_error():
    cfg = _cfg({"universe": {"mode": "static"}})
    with pytest.raises(ValueError):
        refresh(ExchangeThatMustNotBeCalled(), cfg, held_symbols=["NOTASYMBOL"])


# --- real config/config.yaml parity ------------------------------------------


def test_real_config_yaml_static_mode_matches_market_symbols():
    with open("config/config.yaml") as f:
        raw = yaml.safe_load(f)
    cfg = Config(raw=raw)
    assert cfg.universe_cfg.get("mode", "static") == "static"

    universe = refresh(ExchangeThatMustNotBeCalled(), cfg)
    assert {m.symbol for m in universe.members} == set(cfg.symbols)
    assert len(universe.members) == len(cfg.symbols)
    assert all(m.source == "pinned" for m in universe.members)
    assert universe.excluded == {}


def test_real_config_yaml_universe_block_present_and_shaped():
    with open("config/config.yaml") as f:
        raw = yaml.safe_load(f)
    universe_cfg = raw.get("universe")
    assert universe_cfg is not None, "config/config.yaml is missing the universe: block"
    for key in (
        "mode",
        "quotes",
        "prefer_quote",
        "min_median_quote_volume_usd",
        "exit_volume_ratio",
        "min_listing_age_days",
        "max_symbols",
        "include_symbols",
        "exclude_bases",
        "exclude_symbols",
        "quarantine_after_failures",
        "refresh_utc",
    ):
        assert key in universe_cfg, f"universe.{key} missing from config/config.yaml"
