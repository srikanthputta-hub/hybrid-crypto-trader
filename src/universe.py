"""Tradeable-symbol universe, keyed off `config/config.yaml`'s `universe:` block.

This is Phase 1b of docs/system-redesign-2026-10.md's redesign (see its §3,
§4.1, §4.2, §8). It introduces the `UniverseMember` / `Universe` shared
contract and `refresh()` in `mode: "static"` only -- the mode that reproduces
today's exact behavior (`market.symbols`, unchanged) with zero drift and zero
extra exchange calls. `mode: "discover"` (point-in-time liquidity ranking
across the full Kraken USD/USDT market) is a separately-gated, much bigger
decision (P1e in the design doc's sequencing) and is deliberately NOT
implemented here -- calling `refresh()` with any mode other than "static"
raises `NotImplementedError` rather than silently doing something undefined.

Strategy-neutral by construction: this module only decides which symbols the
rest of the pipeline is allowed to look at. It contains no pattern, signal,
risk, or sizing logic, and (in static mode) makes no exchange calls at all --
it never even touches the `exchange` argument today. That argument and the
`cfg`/`held_symbols` signature already match what `mode: "discover"` will
need later, so this function's call sites (src/bot.py, once wired up in a
later change) don't have to change shape again when discover mode lands.

Nothing in this repo calls `refresh()` yet -- the per-base one-plan rule and
held-symbol monitoring in src/bot.py, and RiskEngine.set_symbol_whitelist(),
are sibling pieces of P1b being built separately and wired together later.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Literal

SOURCE = Literal["discovered", "pinned", "held"]


@dataclass
class UniverseMember:
    symbol: str
    base: str
    quote: str
    median_quote_volume_30d: float | None
    cost_min: float | None
    amount_min: float | None
    source: SOURCE


@dataclass
class Universe:
    members: list[UniverseMember]
    generated_at: str
    excluded: dict[str, str] = field(default_factory=dict)


def _split_symbol(symbol: str) -> tuple[str, str]:
    """`"BTC/USDT"` -> `("BTC", "USDT")`. Kraken/ccxt unified symbols are
    always `BASE/QUOTE` (settlement-currency suffixes like `:USD` only show
    up on derivatives, which this bot never trades); a symbol that doesn't
    split cleanly is a config error worth failing loudly on, not guessing
    about."""
    parts = symbol.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"universe: cannot parse {symbol!r} as a BASE/QUOTE symbol")
    return parts[0], parts[1]


def _refresh_static(cfg: Any, held_symbols: Iterable[str]) -> Universe:
    """Wraps `cfg.symbols` (today's `market.symbols` list) into `Universe`
    verbatim -- no exchange calls, no filtering, no ranking. Any held symbol
    not already in `cfg.symbols` is added with `source="held"` so a position
    held on a symbol that's left the static list is still representable (this
    only matters once a future mode change can actually remove a symbol from
    the list out from under an open position; static mode alone, with a
    config list a human edits directly, never exercises it on its own)."""
    pinned_symbols = list(cfg.symbols)
    members = [
        UniverseMember(
            symbol=symbol,
            base=_split_symbol(symbol)[0],
            quote=_split_symbol(symbol)[1],
            median_quote_volume_30d=None,
            cost_min=None,
            amount_min=None,
            source="pinned",
        )
        for symbol in pinned_symbols
    ]

    pinned_set = set(pinned_symbols)
    for symbol in held_symbols:
        if symbol in pinned_set:
            continue
        base, quote = _split_symbol(symbol)
        members.append(
            UniverseMember(
                symbol=symbol,
                base=base,
                quote=quote,
                median_quote_volume_30d=None,
                cost_min=None,
                amount_min=None,
                source="held",
            )
        )
        pinned_set.add(symbol)  # dedupe a held symbol repeated in held_symbols itself

    return Universe(
        members=members,
        generated_at=datetime.now(timezone.utc).isoformat(),
        excluded={},
    )


def refresh(exchange: Any, cfg: Any, held_symbols: Iterable[str] = ()) -> Universe:
    """Builds today's tradeable `Universe`.

    `mode: "static"` (the only implemented mode) never touches `exchange` --
    see the module docstring. Any other `universe.mode` value raises
    `NotImplementedError`: universe *expansion* (`mode: "discover"`) is a
    separately-gated, much bigger decision (P1e in the design doc) that isn't
    built yet, and nothing should be able to flip that switch by accident.
    """
    mode = cfg.universe_cfg.get("mode", "static")
    if mode != "static":
        raise NotImplementedError(
            f"universe.mode={mode!r} not yet implemented; only 'static' is supported"
        )
    return _refresh_static(cfg, held_symbols)
