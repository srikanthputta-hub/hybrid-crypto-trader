"""Deterministic dashboard JSON fixtures: a busy paper trial (open / pending / closed trades, every kind of decision), a
stale one, one that breaches the kill rule, and a second (intraday) instance. They have the shape
scripts/export_dashboard_data.py writes (the `ops` block is built with the exporter's own R maths), anchored to a
caller-supplied "now" so a screenshot or a test can pin every "N min ago".

Used by tests/test_dashboard_display.py (the page run in node) and docs/dashboard/shoot.mjs (screenshots):

    python tests/dashboard_fixture.py <outdir>      # writes busy.json, stale.json, breach.json, intraday.json, evil.json
"""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import scripts.export_dashboard_data as exporter  # noqa: E402

KINDS = ("busy", "stale", "breach", "intraday", "evil")
DEFAULT_NOW = datetime(2026, 11, 21, 21, 10, tzinfo=timezone.utc)
SYMBOLS = ["BTC/USD", "ETH/USD", "XRP/USD", "SOL/USD", "ADA/USD", "DOGE/USD", "AVAX/USD", "LINK/USD", "BNB/USD", "LTC/USD", "DOT/USD",
           "ATOM/USD", "ALGO/USD", "BCH/USD", "XTZ/USD", "SHIB/USD"]
BASE_PRICE = {"BTC/USD": 98000.0, "ETH/USD": 3400.0, "XRP/USD": 2.4, "SOL/USD": 210.0, "ADA/USD": 0.82, "DOGE/USD": 0.27, "AVAX/USD": 41.0,
              "LINK/USD": 17.5, "BNB/USD": 610.0, "LTC/USD": 105.0, "DOT/USD": 7.3, "ATOM/USD": 6.9, "ALGO/USD": 0.31, "BCH/USD": 470.0,
              "XTZ/USD": 1.05, "SHIB/USD": 0.0000245}
FEE_BOOKED = 0.001
KILL_RULE = {"avg_r": [{"n": 20, "max": -0.57}, {"n": 30, "max": -0.43}, {"n": 40, "max": -0.34}, {"n": 50, "max": -0.30}], "max_drawdown": 0.30}


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _candles(symbol: str, now: datetime, rng: random.Random, bars: int = 150) -> list[dict]:
    last_open = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()) - 86400
    price = BASE_PRICE[symbol] * 0.8
    out = []
    for k in range(bars):
        o = price
        c = o * (1 + rng.gauss(0.002, 0.03))
        hi, lo = max(o, c) * (1 + abs(rng.gauss(0, 0.01))), min(o, c) * (1 - abs(rng.gauss(0, 0.01)))
        out.append({"time": last_open - (bars - 1 - k) * 86400, "open": o, "high": hi, "low": lo, "close": c})
        price = c
    return out


def _closed_trades(rng: random.Random, now: datetime, wins: int, losses: int, span_days: int) -> list[dict]:
    """Chronological round-trips with raw prices, the plan risk, and the R the exporter would compute."""
    outcomes = [True] * wins + [False] * losses
    rng.shuffle(outcomes)
    rows = []
    for i, win in enumerate(outcomes):
        symbol = rng.choice(SYMBOLS)
        entry = BASE_PRICE[symbol] * rng.uniform(0.85, 1.1)
        rho = rng.uniform(0.025, 0.05)  # stop distance as a fraction of the entry
        risk = entry * rho
        exit_ = entry * (1 + rho * rng.uniform(1.3, 3.0)) if win else entry * (1 - rho * rng.uniform(1.0, 1.25))
        quote = rng.uniform(700, 1000)
        qty = quote / (entry * (1 + FEE_BOOKED))
        pnl = qty * exit_ * (1 - FEE_BOOKED) - quote
        closed = now - timedelta(days=span_days * (1 - (i + 1) / (len(outcomes) + 1)), hours=rng.uniform(0, 1))
        closed = closed.replace(hour=0, minute=rng.randint(5, 9), second=rng.randint(0, 59), microsecond=0)
        rows.append({
            "symbol": symbol, "opened_at": _iso(closed - timedelta(days=rng.randint(1, 9))), "closed_at": _iso(closed),
            "reason": "take_profit" if win else "stop_loss", "entry": round(entry, 6), "exit": round(exit_, 6),
            "pnl": round(pnl, 2), "pnl_pct": round(pnl / quote, 4),
            "r_booked": round(pnl / (qty * risk), 4),
            "r_recosted": round(exporter._r_recosted(entry, exit_, risk, exporter.RECOST_FEE, exporter.RECOST_SLIPPAGE), 4),
        })
    return rows


def _equity_points(rng: random.Random, start: datetime, now: datetime, starting: float, final: float, floor_dd: float | None) -> list[list]:
    """A daily random walk from `starting` to `final`, with samples at 00:05 UTC, plus the live point at `now`."""
    days = max(2, int((now - start).days))
    path = [starting]
    for _ in range(days):
        path.append(path[-1] * (1 + rng.gauss(0, 0.012)))
    path = [p + (final - path[-1]) * i / (len(path) - 1) for i, p in enumerate(path)]  # end exactly at the live equity
    if floor_dd:  # force a deep trough so the drawdown line has something to show
        peak = max(path)
        mid = len(path) * 2 // 3
        path[mid] = min(path[mid], peak * (1 - floor_dd))
        for j in range(mid + 1, len(path)):
            path[j] = path[mid] + (final - path[mid]) * (j - mid) / (len(path) - 1 - mid)
    out = [[_iso(start), round(starting, 2), round(starting, 2)]]
    for i in range(1, len(path)):
        t = (start + timedelta(days=i)).replace(hour=0, minute=5, second=0, microsecond=0)
        out.append([_iso(t), round(path[i], 2), round(path[i] * 0.62, 2)])
    out.append([_iso(now), round(final, 2), round(final * 0.62, 2)])
    return out


DECISION_TEMPLATES = [
    ("scan", "scanned: no setup", None), ("scan", "1 candidate(s) (MOMENTUM 0.31), no signal", "MOMENTUM"),
    ("blocked", "daily trend not confirmed bullish (higher-timeframe filter)", "BREAKOUT"),
    ("blocked", "risk check said no: max total exposure reached (40% of equity committed)", "PULLBACK"),
    ("skip", "bearish signal logged only (long-only bot)", "REVERSAL"), ("skip", "stale bar: entries skipped (75.0 min after the close)", None),
    ("holding", "open position, no stop/target trigger this cycle", None), ("entered", "bought at $209.40", "BREAKOUT"),
]


def build(kind: str = "busy", now: datetime = DEFAULT_NOW) -> dict:
    """kind: busy | stale | breach | intraday | evil (busy, with markup in every string the page prints)."""
    rng = random.Random({"busy": 7, "stale": 7, "breach": 11, "intraday": 5, "evil": 7}[kind])
    intraday = kind == "intraday"
    wins, losses = {"busy": (8, 16), "stale": (8, 16), "breach": (3, 19), "intraday": (4, 8), "evil": (8, 16)}[kind]
    age = {"stale": 95}.get(kind, 3)  # minutes since the dashboard file was generated
    generated = now - timedelta(minutes=age)
    timeframe = "1h" if intraday else "1d"
    bar_close = (generated.replace(minute=0, second=0, microsecond=0) if intraday else generated.replace(hour=0, minute=0, second=0, microsecond=0))
    starting = 10000.0
    trial_start = now - timedelta(days=32 if not intraday else 6)

    trades = _closed_trades(rng, generated, wins, losses, 30 if not intraday else 5)
    r_series = [t["r_recosted"] for t in trades]
    known = [r for r in r_series if r is not None]
    booked = [t["r_booked"] for t in trades]
    n = len(trades)
    realised = sum(t["pnl"] for t in trades)

    held = ["SOL/USD", "LINK/USD", "AVAX/USD"]
    positions = []
    open_value = 0.0
    for sym in held:
        fill = BASE_PRICE[sym] * rng.uniform(0.9, 1.0)
        price = fill * rng.uniform(0.94, 1.08)
        amount = rng.uniform(700, 1000) / fill
        stop = fill * 0.955
        positions.append({
            "symbol": sym, "pattern": rng.choice(["BREAKOUT", "TREND_CONTINUATION", "PULLBACK"]), "entry_zone": [fill * 0.995, fill * 1.002],
            "stop_loss": stop, "take_profit": fill * 1.09, "invalidation": stop * 0.99, "risk_reward": 2.0, "confidence_rule_based": 3,
            "confidence_claude": None, "fill_price": fill, "fill_amount": amount, "current_price": price,
            "timestamp": _iso(generated - timedelta(days=rng.randint(1, 6), hours=20)), "unrealized_pnl_pct": (price - fill) / fill,
        })
        open_value += price * amount
    pending = []
    for sym in ["XRP/USD", "DOT/USD"]:
        p = BASE_PRICE[sym] * 0.97
        pending.append({
            "symbol": sym, "pattern": "PULLBACK", "entry_zone": [p * 0.99, p], "stop_loss": p * 0.95, "take_profit": p * 1.1, "invalidation": p * 0.94,
            "risk_reward": 2.2, "confidence_rule_based": 4, "confidence_claude": None, "fill_price": None, "fill_amount": None,
            "current_price": p * 1.01, "timestamp": _iso(generated - timedelta(hours=3)),
        })
    cash = {"busy": 6100.0, "stale": 6100.0, "breach": 5200.0, "intraday": 7300.0, "evil": 6100.0}[kind]
    equity = round(cash + open_value, 2)
    curve = _equity_points(rng, trial_start, generated, starting, equity, {"breach": 0.33, "intraday": None}.get(kind, 0.11))
    curve_equities = [p[1] for p in curve]
    peak = starting
    max_dd = 0.0
    for e in curve_equities:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak)

    decisions = []
    states = [DECISION_TEMPLATES[i % len(DECISION_TEMPLATES)] for i in range(len(SYMBOLS))]
    rng.shuffle(states)
    for sym, (state, reason, pattern) in zip(SYMBOLS, states):
        if sym in held:
            state, reason, pattern = "holding", "open position, no stop/target trigger this cycle", None
        elif sym in ("XRP/USD", "DOT/USD"):
            state, reason, pattern = "holding", "pending plan, no entry trigger this cycle", "PULLBACK"
        elif state == "holding":
            state, reason = "scan", "scanned: no setup"
        decisions.append({"symbol": sym, "state": state, "reason": reason, "bar_close": _iso(bar_close),
                          "timestamp": _iso(bar_close + timedelta(minutes=5)), "pattern": pattern})
    stale_skips = sum(d["state"] == "skip" and "stale bar" in d["reason"] for d in decisions)

    summary = {"total_trades": n, "total_realized_pnl": round(realised, 2), "win_rate": round(wins / n, 4) if n else None,
               "avg_win": None, "avg_loss": None, "best_trade": max(t["pnl"] for t in trades), "worst_trade": min(t["pnl"] for t in trades)}
    data = {
        "generated_at": _iso(generated), "mode": "paper", "exchange_id": "kraken", "symbols": list(SYMBOLS),
        "portfolio": {"cash": round(cash, 2), "equity": equity, "pnl_pct": round((equity - starting) / starting, 4)},
        "roles": [
            {"id": "boss", "label": "CEO / Orchestrator", "model_tier": "opus", "status": "idle", "last_active": _iso(generated), "detail": None},
            {"id": "market_analyst", "label": "Market Analyst", "model_tier": "sonnet", "status": "idle", "last_active": _iso(bar_close), "detail": "XTZ/USD: checked, nothing worth trading"},
            {"id": "risk_officer", "label": "Risk Officer", "model_tier": "opus", "status": "working" if kind == "busy" else "idle", "last_active": _iso(generated), "detail": None},
            {"id": "compliance_reviewer", "label": "Compliance Reviewer", "model_tier": "sonnet", "status": "disabled", "last_active": None, "detail": None},
        ],
        "positions": positions, "pending": pending,
        "signal_history": [
            {"timestamp": _iso(bar_close + timedelta(minutes=5)), "symbol": "SOL/USD", "kind": "filled", "summary": "Bought at $209.40"},
            {"timestamp": _iso(bar_close - timedelta(days=1) + timedelta(minutes=5)), "symbol": "ADA/USD", "kind": "stop_loss", "summary": "Sold — stop-loss hit at $0.7612"},
            {"timestamp": _iso(bar_close - timedelta(days=2) + timedelta(minutes=5)), "symbol": "BTC/USD", "kind": "rejected", "summary": "Skipped — daily trend not confirmed bullish (higher-timeframe filter)"},
        ],
        "charts": {sym: {"candles": _candles(sym, generated, rng)} for sym in ["SOL/USD", "LINK/USD", "XRP/USD"]},
        "pnl": {"summary": summary, "hourly": [], "daily": [], "weekly": []},
        "risk": {"sample_size": n, "min_trades_for_stats": 5,
                 "var_cvar": {"95": {"var_pct": -0.052, "cvar_pct": -0.071}, "99": {"var_pct": -0.071, "cvar_pct": -0.083}},
                 "drawdown": {"max_drawdown_pct": round(max_dd * 0.8, 4), "current_drawdown_pct": 0.05, "peak_equity": 10240.0, "trough_equity": 9400.0},
                 "significance": {"verdict": "unremarkable", "observed_wins": wins, "sample_size": n, "p_value": 0.31}},
        "backtest": None,
        "display": {"timeframe": timeframe, "label": "PAPER INTRADAY" if intraday else "PAPER TRIAL",
                    "price_as_of": _iso(bar_close), "pnl_default_period": "hourly" if intraday else "daily"},
    }
    data["ops"] = {
        "schema": exporter.OPS_SCHEMA,
        "instance": {"id": "intraday" if intraday else "daily", "name": "Intraday" if intraday else "Daily trial",
                     "label": data["display"]["label"], "mode": "paper", "timeframe": timeframe, "exchange_id": "kraken"},
        "starting_cash": starting,
        "kill_switch": {"present": False, "file": "KILL_SWITCH"},
        "recost": {"label": exporter.RECOST_LABEL, "fee": exporter.RECOST_FEE, "slippage": exporter.RECOST_SLIPPAGE},
        "trade_stats": {"n": n, "wins": wins, "win_rate": round(wins / n, 4), "avg_r_booked": round(sum(booked) / n, 4),
                        "avg_r_recosted": round(sum(known) / len(known), 4), "r_known": len(known), "r_missing": n - len(known)},
        "r_recosted_series": r_series,
        "closed_trades": list(reversed(trades))[: exporter.CLOSED_TRADES_EXPORTED],
        "decisions": decisions,
        "health": {
            "expected_symbols": len(SYMBOLS),
            "latest_bar": {"bar_close": _iso(bar_close), "symbols_decided": len(SYMBOLS), "stale_skips": stale_skips, "errors": 0},
            "window": {"days": 7, "since": _iso(generated - timedelta(days=7)), "events": 32256, "stale_skips": stale_skips, "errors": 0, "unavailable": 2},
            "last_event_at": _iso(generated - timedelta(minutes=1)), "last_bar_close": _iso(bar_close),
            "poll_interval_seconds": 300, "max_entry_delay_minutes": 60,
        },
        "equity_curve": {"since": curve[0][0], "source": "samples kept by the dashboard export", "points": curve},
        "parity": {"status": "WARN", "message": "1 mismatch in the window, explained (staleness_guard); trial-to-date mismatch rates are within the 10% budget",
                   "window": {"since": _iso(now - timedelta(days=7)), "until": _iso(bar_close), "decided_bars": 7}, "trial_start": _iso(trial_start),
                   "budget": 0.1, "file": "parity_2026-11-20.json", "file_modified": _iso(now - timedelta(days=1))},
    }
    if kind == "evil":
        _make_evil(data)
    return data


EVIL = "<img src=x onerror=window.__pwned=1>"
EVIL2 = '"><script>window.__pwned=2</script>'


def _make_evil(data: dict) -> None:
    """Hostile text in every string field the page prints: it must come out as visible text, never as markup."""
    ops = data["ops"]
    data["display"]["label"] = EVIL
    ops["instance"]["label"] = EVIL
    ops["instance"]["name"] = EVIL2
    data["mode"] = EVIL
    data["positions"][0]["symbol"] = EVIL
    data["positions"][0]["pattern"] = EVIL2
    data["pending"][0]["pattern"] = EVIL
    ops["decisions"][0]["symbol"] = EVIL
    ops["decisions"][0]["reason"] = EVIL2
    ops["decisions"][0]["pattern"] = EVIL
    ops["decisions"][1]["state"] = EVIL  # an unknown state must degrade to "no data"
    ops["closed_trades"][0]["symbol"] = EVIL
    ops["closed_trades"][0]["reason"] = EVIL2
    ops["parity"]["message"] = EVIL
    ops["parity"]["status"] = EVIL2
    data["signal_history"][0]["summary"] = EVIL
    data["signal_history"][0]["symbol"] = EVIL2
    data["roles"][0]["label"] = EVIL
    data["roles"][0]["status"] = EVIL2
    data["roles"][0]["detail"] = EVIL2
    data["symbols"][0] = EVIL


def main(outdir: str) -> None:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    for kind in KINDS:
        (out / f"{kind}.json").write_text(json.dumps(build(kind), indent=1))
    print("wrote", ", ".join(f"{k}.json" for k in KINDS), "to", out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
