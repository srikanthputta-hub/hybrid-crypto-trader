#!/usr/bin/env python3
"""CLI: replay trade plans the risk engine rejected, to see whether each risk
rule is actually saving money or just costing opportunity.

Usage:
    python scripts/replay_risk_rejections.py
    python scripts/replay_risk_rejections.py --lookahead-bars 100 --json

DIAGNOSTIC ONLY. Reads the audit log (logging.audit_log_path in config.yaml)
and fetches public OHLCV candles read-only. It never places an order, never
touches portfolio/position state, config, or anything else under logs/, and
never feeds back into src/risk_engine.py. The R-multiples it prints are GROSS
(no fees, no slippage) and directional only. See src/risk_counterfactual.py's
module docstring for the exact replay model and its limitations.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.exchange import ExchangeClient
from src.risk_counterfactual import (
    DEFAULT_LOOKAHEAD_BARS,
    DEFAULT_MAX_FETCH_BARS,
    build_counterfactual_report,
    format_report,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _at_least(minimum: int):
    def parse(value: str) -> int:
        n = int(value)
        if n < minimum:
            raise argparse.ArgumentTypeError(f"must be >= {minimum}, got {n}")
        return n

    return parse


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Counterfactual replay of risk-engine rejections (diagnostic only)")
    parser.add_argument("--config", default=None, help="path to config.yaml (default: config/config.yaml)")
    parser.add_argument("--audit-log", default=None, help="override logging.audit_log_path from config.yaml")
    parser.add_argument(
        "--lookahead-bars",
        type=_at_least(2),
        default=DEFAULT_LOOKAHEAD_BARS,
        help=f"max closed candles to replay after each rejection (default {DEFAULT_LOOKAHEAD_BARS})",
    )
    parser.add_argument(
        "--max-fetch-bars",
        type=_at_least(2),
        default=DEFAULT_MAX_FETCH_BARS,
        help=f"candles of history fetched per symbol (default {DEFAULT_MAX_FETCH_BARS}, Kraken's per-call cap); "
        "rejections older than this history are reported as skipped",
    )
    parser.add_argument("--json", action="store_true", help="print the full report (incl. per-rejection results) as JSON")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    audit_path = (
        Path(args.audit_log)
        if args.audit_log
        else PROJECT_ROOT / cfg.logging_cfg.get("audit_log_path", "logs/audit_log.jsonl")
    )

    # Public OHLCV needs no credentials, so none are passed. This is
    # deliberately stricter than src/bot.py's build_components: an
    # unauthenticated client cannot place an order even by accident.
    # testnet mirrors config so candles come from the same feed the bot planned on.
    exchange = ExchangeClient(cfg.exchange_id, testnet=cfg.testnet)

    report = build_counterfactual_report(
        audit_path,
        exchange,
        cfg.timeframe,
        lookahead_bars=args.lookahead_bars,
        max_fetch_bars=args.max_fetch_bars,
    )

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(format_report(report))


if __name__ == "__main__":
    main()
