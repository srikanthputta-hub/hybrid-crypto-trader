#!/usr/bin/env python3
"""check_pair_minimums.py -- issue #81 monitoring: are the exchange's PER-PAIR minimum orders still
comfortably below the bot's own order floor (risk.min_order_quote_usd)?

Kraken publishes a minimum per market (ccxt `limits.amount.min`, in BASE units -- e.g. SHIB/USD
770,000 SHIB -- and `limits.cost.min`, in quote units). A base-unit minimum moves with the price:
if a coin rallies, its minimum order in USD rises, and once it exceeds the $10 floor the live broker
would refuse (src/broker_live.py, LiveOrderBelowMinimum) a buy that the risk engine approved. This
script spots that drift BEFORE it happens. It is read-only monitoring and changes no trading
decision:

* public market data only (load_markets + tickers through ccxt); NO API keys: the exchange client
  is built without credentials and .env is never loaded;
* every request is bounded by the ccxt timeout (--timeout, default 15 s);
* prints one row per configured symbol and exits
    0  every pair's minimum order is below 80% of risk.min_order_quote_usd,
    1  at least one pair's minimum exceeds the floor or is within 20% of it (--margin),
    2  at least one pair could not be checked (market not listed, limits malformed, no price,
       markets/tickers could not be loaded) -- unknown is never reported as fine.

    python scripts/check_pair_minimums.py [--config config/config.yaml] [--margin 0.2] [--timeout 15]

scripts/run_live.py runs it at most once per UTC day as a separate, time-limited subprocess and sends
a Telegram warning when it exits non-zero (deploy/README.md).
"""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import PROJECT_ROOT, Config, _load_yaml  # noqa: E402 -- _load_yaml: no .env, no secrets
from src.exchange import ExchangeClient, MarketLimitsUnavailable, parse_market_limits  # noqa: E402

DEFAULT_MARGIN = 0.20
DEFAULT_TIMEOUT_SECONDS = 15.0
# Quote currencies whose `limits.cost.min` is taken as US dollars.
USD_QUOTES = frozenset({"USD", "USDT", "USDC"})

EXIT_OK, EXIT_NEAR_OR_ABOVE, EXIT_UNKNOWN = 0, 1, 2


@dataclass
class Row:
    symbol: str
    status: str                       # OK | NEAR | ABOVE | UNKNOWN
    price: float | None = None
    amount_min: float | None = None   # base units
    cost_min: float | None = None     # quote units
    min_usd: float | None = None      # the smallest order the exchange accepts, in USD
    detail: str = ""


def _finite_positive(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def evaluate(symbols: list[str], markets: object, prices: dict[str, object], floor_usd: float,
             margin: float = DEFAULT_MARGIN) -> tuple[list[Row], int]:
    """(rows, exit code) for `symbols`. Pure: `markets` is ccxt's load_markets() result, `prices`
    maps symbol -> last price (missing/garbage = unknown). The minimum order in USD is the larger of
    amount.min x price and cost.min; NEAR = at or above (1 - margin) x floor, ABOVE = above floor."""
    rows: list[Row] = []
    threshold = floor_usd * (1.0 - margin)
    for symbol in symbols:
        quote = symbol.split("/")[1].split(":")[0] if "/" in symbol else ""
        if not isinstance(markets, dict):
            rows.append(Row(symbol, "UNKNOWN", detail="the exchange's markets could not be loaded"))
            continue
        market = markets.get(symbol)
        if market is None:
            rows.append(Row(symbol, "UNKNOWN", detail="market not listed by the exchange"))
            continue
        try:
            limits = parse_market_limits(symbol, market)
        except MarketLimitsUnavailable as exc:
            rows.append(Row(symbol, "UNKNOWN", detail=str(exc)))
            continue
        price = prices.get(symbol)
        if not _finite_positive(price):
            rows.append(Row(symbol, "UNKNOWN", amount_min=limits.amount_min, cost_min=limits.cost_min,
                            detail=f"no usable price ({price!r})"))
            continue
        price = float(price)
        if quote not in USD_QUOTES:
            rows.append(Row(symbol, "UNKNOWN", price, limits.amount_min, limits.cost_min,
                            detail=f"quote {quote!r} is not a USD quote; minimum not converted"))
            continue
        candidates = [0.0]
        if limits.amount_min:
            candidates.append(limits.amount_min * price)
        if limits.cost_min:
            candidates.append(limits.cost_min)
        min_usd = max(candidates)
        if min_usd > floor_usd:
            status = "ABOVE"
        elif min_usd >= threshold:
            status = "NEAR"
        else:
            status = "OK"
        detail = "" if floor_usd <= 0 else f"{min_usd / floor_usd:.0%} of the floor"
        rows.append(Row(symbol, status, price, limits.amount_min, limits.cost_min, min_usd, detail))
    if any(r.status == "UNKNOWN" for r in rows):
        code = EXIT_UNKNOWN
    elif any(r.status in ("NEAR", "ABOVE") for r in rows):
        code = EXIT_NEAR_OR_ABOVE
    else:
        code = EXIT_OK
    return rows, code


def _num(value: float | None, fmt: str = ".6g") -> str:
    return "-" if value is None else format(value, fmt)


def format_table(rows: list[Row], floor_usd: float, margin: float) -> str:
    header = f"{'symbol':<12} {'status':<8} {'price':>12} {'amount.min':>12} {'cost.min':>9} {'min USD':>9}  detail"
    lines = [
        f"per-pair exchange minimums vs risk.min_order_quote_usd = {floor_usd:g} USD "
        f"(warn at >= {floor_usd * (1 - margin):g} USD, i.e. within {margin:.0%})",
        header, "-" * len(header),
    ]
    for r in rows:
        lines.append(f"{r.symbol:<12} {r.status:<8} {_num(r.price):>12} {_num(r.amount_min):>12} "
                     f"{_num(r.cost_min):>9} {_num(r.min_usd, '.2f'):>9}  {r.detail}")
    counts = {s: sum(r.status == s for r in rows) for s in ("OK", "NEAR", "ABOVE", "UNKNOWN")}
    lines.append("summary: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    return "\n".join(lines)


def fetch_market_data(client: ExchangeClient, symbols: list[str]) -> tuple[object, dict[str, object], list[str]]:
    """(markets, prices, errors) through public endpoints only. A failure is recorded, never raised:
    the affected rows come out UNKNOWN."""
    errors: list[str] = []
    try:
        markets = client.client.load_markets()
    except Exception as exc:  # noqa: BLE001
        return None, {}, [f"load_markets failed ({type(exc).__name__})"]
    prices: dict[str, object] = {}
    listed = [s for s in symbols if isinstance(markets, dict) and s in markets]
    has = getattr(client.client, "has", None) or {}
    if listed and has.get("fetchTickers"):
        try:
            tickers = client.client.fetch_tickers(listed)
            for s in listed:
                t = (tickers or {}).get(s) or {}
                prices[s] = t.get("last") or t.get("close")
        except Exception as exc:  # noqa: BLE001 -- fall back to one ticker per symbol
            errors.append(f"fetch_tickers failed ({type(exc).__name__}); fetching one by one")
    for s in listed:
        if prices.get(s) is None:
            try:
                prices[s] = client.fetch_ticker_price(s)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{s}: ticker failed ({type(exc).__name__})")
    return markets, prices, errors


def build_client(cfg: Config, timeout_seconds: float) -> ExchangeClient:
    """Public-data client: credentials are NEVER passed (read-only by construction)."""
    client = ExchangeClient(cfg.exchange_id, api_key=None, api_secret=None, api_password=None, testnet=cfg.testnet)
    client.client.timeout = int(timeout_seconds * 1000)  # ccxt: per-request timeout in ms
    return client


def main(argv: list[str] | None = None, client: ExchangeClient | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default=str(PROJECT_ROOT / "config" / "config.yaml"))
    ap.add_argument("--margin", type=float, default=DEFAULT_MARGIN)
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    a = ap.parse_args(argv)
    cfg = Config(raw=_load_yaml(Path(a.config)))
    floor = float(cfg.risk.get("min_order_quote_usd") or 0.0)
    symbols = list(cfg.symbols)
    if client is None:
        client = build_client(cfg, a.timeout)
    markets, prices, errors = fetch_market_data(client, symbols)
    rows, code = evaluate(symbols, markets, prices, floor, a.margin)
    print(format_table(rows, floor, a.margin))
    for e in errors:
        print(f"note: {e}")
    if floor <= 0:
        print("note: risk.min_order_quote_usd is 0 (no floor): every pair counts as at/above it")
    print({EXIT_OK: "RESULT: OK", EXIT_NEAR_OR_ABOVE: "RESULT: WARN -- a pair's minimum is at/near/above the floor",
           EXIT_UNKNOWN: "RESULT: UNKNOWN -- a pair could not be checked"}[code])
    return code


if __name__ == "__main__":
    sys.exit(main())
