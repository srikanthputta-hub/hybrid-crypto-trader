#!/usr/bin/env python3
"""Weekly paper-trial parity check (docs/multiday_hold_design.md §5/§6, docs/bot_bar_parity.md §2).

Compares what the live PAPER bot decided with what the P6 backtest harness
(`strategy_pack_run/backtest_live_bot.py`, `count_same_bar_approvals=True`) would have decided on
the SAME closed daily bars:

    PYTHONPATH=. python scripts/paper_parity_check.py                      # last 7 bars, text report
    PYTHONPATH=. python scripts/paper_parity_check.py --days 14 --json-out parity.json
    PYTHONPATH=. python scripts/paper_parity_check.py --format json

What it reads (all read-only; paths default to the `logging:` block of --config):
  - the audit log (`logging.audit_log_path`): one line per symbol per cycle; with `loop.bar_parity: true`
    every event of a symbol with data carries `bar_close`, and entry events carry `risk_decision` + `plan`;
  - the closed-bar archive (`logging.closed_bars_dir`): `<BASE>_<QUOTE>_1d.csv` in the backtest CSV
    format, i.e. the exact bars the bot saw (written by the bot when `closed_bars_dir` is set);
  - the trade log (`logging.trade_log_path`): cross-checked against the audit log's fills;
  - the bar-decision ledger (`logging.bar_decisions_path`): consistency check only.

What it does: replays the harness over the archived bars from the trial's first decided bar (a fresh
`risk.starting_cash_usd` portfolio, signals before the trial start dropped), records every risk check
and every stop-loss / take-profit exit, and compares them per (bar, symbol) with the bot's audit
events inside the window:
  - entry decision: allowed vs not, rejection reason up to the first ';' (the same `same_decision`
    equivalence as tests/test_daily_live_parity.py), plan entry/stop/risk-reward to a relative 1e-6
    (the design's "plan levels" rule; live indicators use a 200-bar fetch, the archive replay uses all
    archived bars, so warm-up leaves ~1e-7), approved size within a loose tolerance (fill-price residual);
  - exits: (decision bar, symbol, stop_loss | take_profit);
  - integrity: a bar decided twice (P1 violation), audit fills missing from trades.csv and the reverse.

Every mismatch is listed with its reason and, when the audit log itself proves a documented cause, an
`explained_by` tag (staleness guard, bot gap/fetch failure, kill switch, advisor veto, float phantom,
or "downstream" of an earlier explained mismatch, since the two portfolios have diverged since).

Status (design §5: "at most 10% explained mismatches; an unexplained mismatch pauses the trial"):
  PASS               no mismatch.
  WARN               mismatches exist, every one is explained, and each rate is within the budget.
  FAIL               any unexplained mismatch, or an entry / exit / overall mismatch rate above the budget.
                     Exit code 1. An implementation finding, not a strategy verdict.
  INSUFFICIENT DATA  no decided bars in the window, or fewer than --min-bars. Exit code 0.
Exit code 2: unreadable / corrupt input (the message says which file and why).

No network, no writes to the bot's state (only --json-out, which must not be an input file).

Requirements for a meaningful run (flagged in docs/paper_parity_check.md): the bot ran with
`loop.bar_parity: true` and `logging.closed_bars_dir` set; the portfolio was reset at trial start; the
audit log still reaches back to the trial start (or pass --trial-start): `logging.retention_days`
prunes it after 30 days by default.
"""
from __future__ import annotations

import argparse
import inspect
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import yaml  # noqa: E402

SUPPORTED_TIMEFRAME = "1d"
TF_DELTA = pd.Timedelta(days=1)
DEFAULT_WINDOW_BARS = 7
DEFAULT_MIN_BARS = 3
DEFAULT_BUDGET = 0.10          # design §5
DEFAULT_LEVEL_RTOL = 1e-6      # design §5: plan levels match to a relative 1e-6
# Approved size: the paper broker fills at the close, the harness at the next open, so equity (and so
# risk-based sizes) drift a little; docs/bot_bar_parity.md §4.2 measured decisions identical with sizes
# differing slightly. A size off by more than this fraction is reported as a mismatch.
DEFAULT_SIZE_RTOL = 0.25
PHANTOM_USD = 1e-6             # an approved quote below this is a float residue, not a trade
NO_ROOM = "no room left to size this trade"
BELOW_MIN = "below the minimum order size"
STALE_NOTE = "stale bar: entries skipped"
ALREADY_DECIDED = "already decided"
NOT_A_PROJECT_ROOT = Path("/nonexistent-paper-parity-check")  # the harness's kill-switch lookup lands nowhere

EXPLAINED_STALE = "staleness_guard"
EXPLAINED_GAP = "bot_gap"
EXPLAINED_KILL = "kill_switch"
EXPLAINED_ADVISOR = "advisor_veto"
EXPLAINED_PHANTOM = "float_phantom"
EXPLAINED_DOWNSTREAM = "downstream_of_explained_mismatch"
# Mismatches that are never relabelled "downstream": they are bugs in the bot's own bookkeeping.
INTEGRITY_KINDS = {"duplicate_decision", "audit_trade_log", "trade_log_audit"}

PASS, WARN, FAIL, INSUFFICIENT = "PASS", "WARN", "FAIL", "INSUFFICIENT DATA"
EXIT_CODES = {PASS: 0, WARN: 0, INSUFFICIENT: 0, FAIL: 1}
EXIT_INPUT_ERROR = 2


class ParityInputError(Exception):
    """An input file is missing, unreadable or corrupt. Reported as exit code 2."""


# --------------------------------------------------------------------------- #
# Small pure helpers (equivalence rules shared with tests/test_daily_live_parity.py)
# --------------------------------------------------------------------------- #
def closed_bars_file(directory: Path, symbol: str, timeframe: str) -> Path:
    """`BTC/USD` -> `<dir>/BTC_USD_1d.csv`. The same naming as src.bot._closed_bars_file
    (tests/test_paper_parity_check.py asserts they agree)."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", symbol.replace("/", "_"))
    return directory / f"{name}_{timeframe}.csv"


def _reason_key(reason: str) -> str:
    """The decision part of a rejection reason, without clock-dependent text: the rate cap's
    "; next slot opens in <timedelta>" (pandas prints "0 days 02:00:00", datetime "2:00:00") and the
    cooldown's "<timedelta> remaining". Everything after the first ';' is dropped."""
    key = str(reason).split(";")[0]
    if key.startswith("cooldown active for "):
        key = key.split(":")[0]
    return key


def _phantom_size(reason: str) -> float:
    # "sized order 9.094947e-13 is below the minimum order size 0.01 ..."
    try:
        return float(reason.split("sized order ")[1].split(" ")[0])
    except (IndexError, ValueError):
        return float("inf")


def reasons_equivalent(harness_reason: str, bot_reason: str) -> bool:
    """tests/test_daily_live_parity.py::same_decision's rejection-reason rule: equal up to the first
    ';' (cooldown: up to the first ':'), or the same "nothing left" decision worded two ways when room
    is used up to ~1e-12 USD ("no room" vs "below the minimum order size" for a sub-cent size)."""
    if _reason_key(harness_reason) == _reason_key(bot_reason):
        return True
    reasons = {r.split(" (")[0].split(";")[0] for r in (harness_reason, bot_reason)}
    return (
        any(r.startswith(NO_ROOM) for r in reasons)
        and any(BELOW_MIN in r for r in reasons)
        and all(r.startswith(NO_ROOM) or _phantom_size(r) < 0.01 for r in reasons)
    )


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _close(a: float | None, b: float | None, rtol: float) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return math.isclose(a, b, rel_tol=rtol, abs_tol=0.0)


def _iso(ts: pd.Timestamp) -> str:
    return pd.Timestamp(ts).isoformat()


def _parse_ts(value: Any, what: str) -> pd.Timestamp:
    if not isinstance(value, str) or not value.strip():
        raise ParityInputError(f"{what}: expected an ISO timestamp string, got {value!r}")
    try:
        ts = pd.Timestamp(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ParityInputError(f"{what}: {value!r} is not a timestamp ({exc})") from exc
    if pd.isna(ts):
        raise ParityInputError(f"{what}: {value!r} is not a timestamp")
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


# --------------------------------------------------------------------------- #
# Input loading
# --------------------------------------------------------------------------- #
def read_audit(path: Path) -> list[dict]:
    if not path.exists():
        raise ParityInputError(f"audit log not found: {path}")
    events: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ParityInputError(f"{path}:{n}: not valid JSON ({exc.msg}); the audit log is corrupt") from exc
                if not isinstance(event, dict):
                    raise ParityInputError(f"{path}:{n}: expected a JSON object per line, got {type(event).__name__}")
                event["_line"] = n
                events.append(event)
    except (OSError, UnicodeDecodeError) as exc:
        raise ParityInputError(f"cannot read audit log {path}: {exc}") from exc
    return events


def read_ledger(path: Path | None) -> dict[str, pd.Timestamp] | None:
    """The bar-decision ledger ({"timeframe": ..., "bars": {symbol: ISO}}), or None when absent."""
    if path is None or not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParityInputError(f"bar-decision ledger {path} is unreadable or corrupt: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("bars"), dict):
        raise ParityInputError(f"bar-decision ledger {path} has no 'bars' mapping (is it the flat pre-timeframe form?)")
    if raw.get("timeframe") != SUPPORTED_TIMEFRAME:
        raise ParityInputError(
            f"bar-decision ledger {path} was written for timeframe {raw.get('timeframe')!r}; "
            f"this check covers {SUPPORTED_TIMEFRAME!r} only"
        )
    return {str(s): _parse_ts(v, f"{path}: bars[{s!r}]") for s, v in raw["bars"].items()}


def read_trade_log(path: Path | None) -> list[dict] | None:
    if path is None or not path.exists():
        return None
    import csv

    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise ParityInputError(f"trade log {path} is unreadable or corrupt: {exc}") from exc
    for n, row in enumerate(rows, 2):
        if None in row or not all(k in row and row[k] not in (None, "") for k in ("timestamp", "symbol", "side")):
            raise ParityInputError(f"trade log {path}:{n}: malformed row (needs timestamp, symbol, side): {row}")
    return rows


def read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ParityInputError(f"config not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ParityInputError(f"config {path} is unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise ParityInputError(f"config {path} must be a mapping at the top level")
    for key in ("indicators", "risk", "market"):
        if not isinstance(raw.get(key), dict):
            raise ParityInputError(f"config {path}: missing or malformed '{key}' section")
    return raw


def _resolve(value: str) -> Path:
    """Config-relative paths resolve against the project root, exactly like src.bot._resolve_log_path."""
    p = Path(value)
    return p if p.is_absolute() else PROJECT_ROOT / p


# --------------------------------------------------------------------------- #
# The bot's side: audit events grouped per (bar close, symbol)
# --------------------------------------------------------------------------- #
@dataclass
class BotBar:
    bar: pd.Timestamp
    symbol: str
    check: dict | None = None          # the entry decision: risk_decision + plan levels
    n_checks: int = 0
    planned: bool = False
    exit_reason: str | None = None     # stop_loss | take_profit (executed)
    stale: bool = False
    error: str | None = None
    advisor_veto: bool = False
    n_decision_events: int = 0         # events that are not "already decided" echoes
    n_events: int = 0
    held_note: str | None = None
    note: str | None = None
    signal: dict | None = None
    lines: list[int] = field(default_factory=list)


def _bot_check(event: dict) -> dict:
    rd, plan = event["risk_decision"], event.get("plan") or {}
    if not isinstance(rd, dict) or "allowed" not in rd:
        raise ParityInputError(f"audit line {event.get('_line')}: malformed risk_decision {rd!r}")
    zone = plan.get("entry_zone")
    entry = _num(zone[1]) if isinstance(zone, (list, tuple)) and len(zone) == 2 else None
    return dict(
        allowed=bool(rd["allowed"]), reason=str(rd.get("reason", "")), quote=_num(rd.get("max_quote_amount")),
        entry=entry, stop=_num(plan.get("stop_loss")), rr=_num(plan.get("risk_reward")),
    )


def collect_bot(events: list[dict]) -> tuple[dict[tuple[pd.Timestamp, str], BotBar], list[pd.Timestamp]]:
    """Groups the audit events that carry `bar_close` by (bar, symbol). Returns the groups and every
    bar close seen (any symbol)."""
    groups: dict[tuple[pd.Timestamp, str], BotBar] = {}
    for event in events:
        if "bar_close" not in event:
            continue
        line = event.get("_line")
        symbol = event.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise ParityInputError(f"audit line {line}: event with bar_close has no symbol")
        bar = _parse_ts(event["bar_close"], f"audit line {line}: bar_close")
        info = groups.setdefault((bar, symbol), BotBar(bar=bar, symbol=symbol))
        info.n_events += 1
        info.lines.append(line)
        note = event.get("note")
        if isinstance(note, str) and ALREADY_DECIDED in note:
            continue  # the second+ tick of a bar: nothing decided here
        info.n_decision_events += 1
        if "risk_decision" in event:
            info.n_checks += 1
            if info.check is None:
                info.check = _bot_check(event)
        if event.get("action") == "planned":
            info.planned = True
        if "advisor_verdict" in event and event.get("action") != "planned":
            info.advisor_veto = True
        if isinstance(note, str):
            info.note = info.note or note
            if note.startswith(STALE_NOTE):
                info.stale = True
            if "plan active for" in note:
                info.held_note = note
        if event.get("error"):
            info.error = str(event["error"])
        if "signal" in event and isinstance(event["signal"], dict):
            info.signal = event["signal"]
        action = (event.get("monitor_action") or {}).get("action") if isinstance(event.get("monitor_action"), dict) else None
        if action in ("STOP_LOSS", "TAKE_PROFIT") and event.get("execution"):
            info.exit_reason = action.lower()
    return groups, sorted({b for b, _ in groups})


# --------------------------------------------------------------------------- #
# The harness's side
# --------------------------------------------------------------------------- #
@dataclass
class HarnessResult:
    checks: dict[tuple[pd.Timestamp, str], dict]
    exits: set[tuple[pd.Timestamp, str, str]]
    last_bar: dict[str, pd.Timestamp]
    n_bars: int


def load_frames(bars_dir: Path, symbols: list[str], harness) -> dict[str, pd.DataFrame]:
    if not bars_dir.is_dir():
        raise ParityInputError(f"closed-bar archive directory not found: {bars_dir} (set logging.closed_bars_dir)")
    frames: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        path = closed_bars_file(bars_dir, symbol, SUPPORTED_TIMEFRAME)
        if not path.exists():
            raise ParityInputError(f"closed-bar archive has no file for {symbol}: {path}")
        try:
            df = harness.load_csv(path, SUPPORTED_TIMEFRAME)
        except KeyError as exc:
            raise ParityInputError(
                f"closed-bar file {path} ({symbol}) is missing the required column {exc} "
                f"(expected timestamp,open,high,low,close,volume)"
            ) from exc
        except Exception as exc:  # noqa: BLE001 -- any parse problem is "corrupt input"
            raise ParityInputError(f"closed-bar file {path} ({symbol}) is unreadable or corrupt: {exc}") from exc
        if len(df) == 0:
            raise ParityInputError(f"closed-bar file {path} ({symbol}) has no bars")
        if not np.isfinite(df.to_numpy()).all():
            raise ParityInputError(f"closed-bar file {path} ({symbol}) has missing or non-finite values")
        frames[symbol] = df
    return frames


def run_harness(
    frames: dict[str, pd.DataFrame], raw_cfg: dict[str, Any], trial_start: pd.Timestamp, fee: float, harness
) -> HarnessResult:
    last_bar = {s: df.index[-1] for s, df in frames.items()}
    final = max(last_bar.values())
    # One flat bar after the last real one, for symbols that have the last real bar: the harness executes a
    # stop/target decided on bar t at bar t+1's open, so without it an exit decided on the newest bar would
    # be invisible. Its checks and exits are outside the compared range.
    padded = {}
    for s, df in frames.items():
        if df.index[-1] == final:
            row = pd.DataFrame(
                {"open": df["close"].iloc[-1], "high": df["close"].iloc[-1], "low": df["close"].iloc[-1],
                 "close": df["close"].iloc[-1], "volume": 0.0},
                index=pd.DatetimeIndex([final + TF_DELTA], name=df.index.name),
            )
            df = pd.concat([df, row])
        padded[s] = df
    htf_enabled = bool((raw_cfg.get("htf_filter") or {}).get("enabled", True))
    enriched, signals, _ = harness.build_enriched_and_signals(
        padded, raw_cfg["indicators"], raw_cfg.get("scanner") or {}, raw_cfg.get("signal_engine") or {}, htf_enabled
    )
    signals = {s: {ts: sig for ts, sig in per.items() if ts >= trial_start} for s, per in signals.items()}

    checks: dict[tuple[pd.Timestamp, str], dict] = {}

    class _Recording(harness.RiskEngine):
        def check_new_trade(self, symbol, side, portfolio_equity, cash_available, **kw):
            decision = super().check_new_trade(symbol, side, portfolio_equity, cash_available, **kw)
            checks[(pd.Timestamp(kw["now"]), symbol)] = dict(
                allowed=bool(decision.allowed), reason=decision.reason,
                quote=_num(decision.max_quote_amount), entry=_num(kw["entry_price"]),
                stop=_num(kw["stop_loss"]), rr=_num(kw["risk_reward"]),
            )
            return decision

    risk_cfg = {**raw_cfg["risk"], "kill_switch_file": "KILL_SWITCH"}  # the live KILL_SWITCH file must not matter
    original = harness.RiskEngine
    harness.RiskEngine = _Recording
    try:
        result = harness.simulate_portfolio(
            enriched, signals, harness.Costs(fee, 0.0, "paper"), risk_cfg, raw_cfg.get("trade_planner") or {},
            float(raw_cfg["risk"]["starting_cash_usd"]), 365.0, NOT_A_PROJECT_ROOT,
            count_same_bar_approvals=True,
        )
    finally:
        harness.RiskEngine = original
    exits = {
        (pd.Timestamp(t.exit_time) - TF_DELTA, t.symbol, t.reason)
        for t in result.trades.itertuples()
        if t.reason in ("stop_loss", "take_profit")
    }
    n_bars = len(next(iter(enriched.values())).index)
    return HarnessResult(checks=checks, exits=exits, last_bar=last_bar, n_bars=n_bars)


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #
@dataclass
class Mismatch:
    kind: str
    bar: pd.Timestamp
    symbol: str
    detail: str
    harness: dict | None = None
    bot: dict | None = None
    explained_by: str | None = None

    def to_json(self) -> dict:
        return {"kind": self.kind, "bar": _iso(self.bar), "symbol": self.symbol, "detail": self.detail,
                "harness": self.harness, "bot": self.bot, "explained_by": self.explained_by}


def _explain_entry(h: dict | None, b: dict | None, info: BotBar | None) -> str | None:
    """A documented cause the audit log itself proves, or None."""
    if info is None or info.n_decision_events == 0:
        return EXPLAINED_GAP
    if info.stale:
        return EXPLAINED_STALE
    if b is not None and b["reason"].startswith("kill switch"):
        return EXPLAINED_KILL
    for side in (h, b):
        if side is not None and side["allowed"] and side["quote"] is not None and side["quote"] < PHANTOM_USD:
            return EXPLAINED_PHANTOM
    return None


def compare(
    bot: dict[tuple[pd.Timestamp, str], BotBar],
    hres: HarnessResult,
    since: pd.Timestamp,
    until: pd.Timestamp,
    *,
    level_rtol: float,
    size_rtol: float,
) -> dict[str, Any]:
    def in_window(bar: pd.Timestamp) -> bool:
        return since <= bar <= until

    H = {k: c for k, c in hres.checks.items() if in_window(k[0]) and k[0] <= hres.last_bar.get(k[1], until)}
    B = {k: info for k, info in bot.items() if in_window(k[0]) and info.check is not None}
    mismatches: list[Mismatch] = []

    for key in sorted(set(H) | set(B), key=lambda k: (k[0], k[1])):
        bar, symbol = key
        h, info = H.get(key), bot.get(key)
        b = B[key].check if key in B else None
        if h is not None and b is not None:
            if h["allowed"] != b["allowed"]:
                kind, detail = "entry_decision", (
                    f"harness {'approved' if h['allowed'] else 'rejected'}"
                    f" ({h['reason'] if not h['allowed'] else 'ok'}); bot {'approved' if b['allowed'] else 'rejected'}"
                    f" ({b['reason'] if not b['allowed'] else 'ok'})"
                )
            elif any(not _close(h[k], b[k], level_rtol) for k in ("entry", "stop", "rr")):
                kind, detail = "plan_levels", (
                    f"entry/stop/rr differ beyond rel {level_rtol:g}: harness "
                    f"({h['entry']}, {h['stop']}, {h['rr']}) vs bot ({b['entry']}, {b['stop']}, {b['rr']})"
                )
            elif not h["allowed"] and not reasons_equivalent(h["reason"], b["reason"]):
                kind, detail = "rejection_reason", f"harness: {h['reason']!r}; bot: {b['reason']!r}"
            elif h["allowed"] and not _close(h["quote"], b["quote"], size_rtol) and not (
                (h["quote"] or 0) < PHANTOM_USD and (b["quote"] or 0) < PHANTOM_USD
            ):
                kind, detail = "size", f"approved quote differs beyond rel {size_rtol:g}: harness {h['quote']} vs bot {b['quote']}"
            else:
                continue
            mismatches.append(Mismatch(kind, bar, symbol, detail, h, b, _explain_entry(h, b, info)))
        elif h is not None:
            why = "no bot decision event for this bar" if info is None or info.n_decision_events == 0 else (
                info.note or "bot ran no entry check (no signal / vetoed / blocked / held)"
            )
            if info is not None and info.error:
                why = f"bot error: {info.error}"
            mismatches.append(Mismatch(
                "harness_only_check", bar, symbol,
                f"harness {'approved' if h['allowed'] else 'rejected'} an entry ({h['reason']}); bot: {why}",
                h, None, _explain_entry(h, None, info),
            ))
        else:
            mismatches.append(Mismatch(
                "bot_only_check", bar, symbol,
                f"bot {'approved' if b['allowed'] else 'rejected'} an entry ({b['reason']}); the harness ran no check "
                f"(no signal / no plan)", None, b, _explain_entry(None, b, info),
            ))

    # Entry approvals: harness allowed vs the bot actually planning. A risk approval that never became a
    # plan is the advisor's veto (or an error mid-pipeline).
    H_entries = {k for k, c in H.items() if c["allowed"]}
    B_entries = {k for k, info in bot.items() if in_window(k[0]) and info.planned}
    seen = {(m.bar, m.symbol) for m in mismatches}
    for key in sorted(H_entries - B_entries, key=lambda k: (k[0], k[1])):
        info = bot.get(key)
        if key in seen or info is None or info.check is None or not info.check["allowed"]:
            continue
        if info.error:
            mismatches.append(Mismatch("bot_error", key[0], key[1], f"bot approved but failed: {info.error}", H[key], info.check, None))
        else:
            mismatches.append(Mismatch(
                "advisor_veto", key[0], key[1], "bot's risk check approved but no plan was made (advisor veto)",
                H[key], info.check, EXPLAINED_ADVISOR if info.advisor_veto else None,
            ))

    # Bar decided twice (P1 says exactly once).
    for key, info in sorted(bot.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if in_window(key[0]) and info.n_checks > 1:
            mismatches.append(Mismatch(
                "duplicate_decision", key[0], key[1],
                f"{info.n_checks} entry decisions for one bar (audit lines {info.lines}); P1 allows one", None, info.check, None,
            ))

    # Exits.
    Hx = {x for x in hres.exits if in_window(x[0]) and x[0] <= hres.last_bar.get(x[1], until)}
    Bx = {(k[0], k[1], info.exit_reason) for k, info in bot.items() if in_window(k[0]) and info.exit_reason}
    for bar, symbol, reason in sorted(Hx - Bx, key=lambda x: (x[0], x[1], x[2])):
        other = next((r for b2, s2, r in Bx if (b2, s2) == (bar, symbol)), None)
        info = bot.get((bar, symbol))
        if other is not None:
            detail, explained = f"harness exits on {reason}; bot exits on {other}", None
        else:
            detail = f"harness exits ({reason}); the bot did not"
            explained = EXPLAINED_GAP if info is None else None
            if info is not None and info.error:
                detail += f" (bot error: {info.error})"
        mismatches.append(Mismatch("exit_harness_only", bar, symbol, detail, {"reason": reason}, {"reason": other}, explained))
    for bar, symbol, reason in sorted(Bx - Hx, key=lambda x: (x[0], x[1], x[2])):
        if any((b2, s2) == (bar, symbol) for b2, s2, _ in Hx):
            continue  # reported above as a reason difference
        mismatches.append(Mismatch("exit_bot_only", bar, symbol, f"bot exits ({reason}); the harness did not", None, {"reason": reason}, None))

    mismatches.sort(key=lambda m: (m.bar, m.symbol, m.kind))
    cascade = False
    for m in mismatches:
        if m.explained_by is None and cascade and m.kind not in INTEGRITY_KINDS:
            m.explained_by = EXPLAINED_DOWNSTREAM
        elif m.explained_by is not None and m.explained_by != EXPLAINED_DOWNSTREAM:
            cascade = True

    return {
        "mismatches": mismatches,
        "H": H, "B": {k: v.check for k, v in B.items()},
        "H_entries": H_entries, "B_entries": B_entries, "Hx": Hx, "Bx": Bx,
    }


def side_stats(h: set, b: set) -> dict:
    union = len(h | b)
    return {"harness": len(h), "bot": len(b), "matched": len(h & b), "mismatched": len(h ^ b),
            "agreement_rate": None if union == 0 else len(h & b) / union,
            "mismatch_rate": len(h ^ b) / max(1, len(h))}  # design §5: symmetric difference over harness count


def summarize(cmp: dict[str, Any], mismatches: list[Mismatch], since: pd.Timestamp) -> dict[str, Any]:
    """Entry / exit agreement and the mismatch counts for the bars at or after `since`, out of a comparison that
    may have been run over a longer span (so "downstream" labelling sees the whole trial)."""
    def keep(bar: pd.Timestamp) -> bool:
        return bar >= since

    H_e = {k for k in cmp["H_entries"] if keep(k[0])}
    B_e = {k for k in cmp["B_entries"] if keep(k[0])}
    Hx = {x for x in cmp["Hx"] if keep(x[0])}
    Bx = {x for x in cmp["Bx"] if keep(x[0])}
    n_decisions = len({k for k in set(cmp["H"]) | set(cmp["B"]) if keep(k[0])}) + len(Hx | Bx)
    mine = [m for m in mismatches if keep(m.bar)]
    return {
        "entries": side_stats(H_e, B_e), "exits": side_stats(Hx, Bx),
        "decisions_compared": n_decisions,
        "mismatch_count": len(mine), "unexplained_count": sum(m.explained_by is None for m in mine),
        "mismatch_rate": len(mine) / max(1, n_decisions),
    }


def check_trade_log(
    bot: dict[tuple[pd.Timestamp, str], BotBar], events: list[dict], rows: list[dict] | None,
    since: pd.Timestamp, until: pd.Timestamp,
) -> list[Mismatch]:
    """Every fill the audit log reports inside the window must be a trades.csv row (same symbol, side and
    timestamp string -- the bot logs the FillResult's own timestamp), and the reverse."""
    out: list[Mismatch] = []
    fills: list[tuple[str, str, str, pd.Timestamp]] = []  # (timestamp, symbol, side, bar)
    for ev in events:
        if "bar_close" not in ev or not isinstance(ev.get("execution"), dict):
            continue
        bar = _parse_ts(ev["bar_close"], f"audit line {ev.get('_line')}: bar_close")
        if not (since <= bar <= until):
            continue
        ex = ev["execution"]
        fills.append((str(ex.get("timestamp")), str(ev.get("symbol")), str(ex.get("side")), bar))
    if not fills:
        return out
    if rows is None:
        for ts, sym, side, bar in fills:
            out.append(Mismatch("audit_trade_log", bar, sym, f"audit {side} at {ts} but no trade log was found", None, None, None))
        return out
    logged = {(r["timestamp"], r["symbol"], r["side"]) for r in rows}
    for ts, sym, side, bar in fills:
        if (ts, sym, side) not in logged:
            out.append(Mismatch("audit_trade_log", bar, sym, f"audit reports a {side} at {ts} that is not in the trade log", None, None, None))
    # Reverse direction, restricted to the wall-clock span of the window's audited fills.
    stamps = sorted(pd.Timestamp(ts) for ts, _, _, _ in fills if ts != "None")
    audited = {(ts, sym, side) for ts, sym, side, _ in fills}
    if stamps:
        lo, hi = stamps[0], stamps[-1]
        for r in rows:
            try:
                t = _parse_ts(r["timestamp"], "trade log timestamp")
            except ParityInputError:
                continue
            if lo <= t <= hi and (r["timestamp"], r["symbol"], r["side"]) not in audited:
                out.append(Mismatch("trade_log_audit", t.floor("D"), r["symbol"], f"trade log {r['side']} at {r['timestamp']} has no audit-log fill", None, None, None))
    return out


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #
def _rate_text(rate: float | None) -> str:
    return "n/a (nothing to compare)" if rate is None else f"{rate * 100:.1f}%"


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    cfg_path = Path(args.config) if args.config else PROJECT_ROOT / "config" / "config.yaml"
    raw_cfg = read_config(cfg_path)
    logging_cfg = raw_cfg.get("logging") or {}
    timeframe = (raw_cfg.get("market") or {}).get("timeframe")
    if timeframe != SUPPORTED_TIMEFRAME:
        raise ParityInputError(
            f"this check covers the {SUPPORTED_TIMEFRAME!r} paper trial only; {cfg_path} has market.timeframe {timeframe!r}"
        )

    def pick(cli: str | None, key: str, default: str | None) -> Path | None:
        if cli:
            return Path(cli)
        value = logging_cfg.get(key, default)
        return _resolve(value) if value else None

    audit_path = pick(args.audit_log, "audit_log_path", "logs/audit_log.jsonl")
    trade_path = pick(args.trade_log, "trade_log_path", "logs/trades.csv")
    ledger_path = pick(args.bar_decisions, "bar_decisions_path", "logs/bar_decisions.json")
    bars_dir = pick(args.bars_dir, "closed_bars_dir", None)

    report: dict[str, Any] = {
        "status": None, "exit_code": None, "message": "", "budget": args.budget,
        "inputs": {"config": str(cfg_path), "audit_log": str(audit_path), "trade_log": str(trade_path),
                   "bar_decisions": str(ledger_path), "closed_bars_dir": str(bars_dir) if bars_dir else None},
        "window": None, "notes": [], "mismatches": [],
    }

    events = read_audit(audit_path)
    bot, all_bars = collect_bot(events)
    if not events:
        return _insufficient(report, "the audit log is empty: no decisions to compare yet")
    if not all_bars:
        return _insufficient(
            report,
            f"none of the {len(events)} audit lines carries bar_close: the bot was not running with "
            f"loop.bar_parity: true, so there are no per-bar decisions to compare",
        )

    until = _parse_ts(args.until, "--until") if args.until else all_bars[-1]
    since = _parse_ts(args.since, "--since") if args.since else until - (args.days - 1) * TF_DELTA
    trial_start = _parse_ts(args.trial_start, "--trial-start") if args.trial_start else all_bars[0]
    report["trial_start"] = _iso(trial_start)
    report["trial_start_source"] = "--trial-start" if args.trial_start else "earliest bar_close in the audit log"
    if since < trial_start:
        report["notes"].append(f"window start {_iso(since)} is before the trial start; clamped to {_iso(trial_start)}")
        since = trial_start
    window_bars = [b for b in all_bars if since <= b <= until]
    report["window"] = {"since": _iso(since), "until": _iso(until), "decided_bars": len(window_bars)}
    if len(window_bars) < args.min_bars:
        return _insufficient(
            report,
            f"only {len(window_bars)} decided bar(s) between {_iso(since)} and {_iso(until)}; need at least "
            f"{args.min_bars} (--min-bars). Run again once more daily closes have been decided",
        )

    ledger = read_ledger(ledger_path)
    if ledger is not None:
        latest: dict[str, pd.Timestamp] = {}
        for (bar, sym), _info in bot.items():
            latest[sym] = max(latest.get(sym, bar), bar)
        behind = sorted(s for s, t in ledger.items() if latest.get(s) is not None and t > latest[s])
        if behind:
            report["notes"].append(
                f"the bar-decision ledger is ahead of the audit log for {behind[:5]}: audit lines may have been "
                f"pruned (logging.retention_days) or lost"
            )

    symbols = sorted({sym for (_bar, sym) in bot})
    report["window"]["symbols"] = len(symbols)
    from strategy_pack_run import backtest_live_bot as harness  # heavy import: after the cheap exits above

    frames = load_frames(bars_dir, symbols, harness) if bars_dir else _no_archive()
    # The comparison always runs over the whole trial to date (the portfolio is path dependent, and the design's
    # 10% budget is cumulative); the window is the part reported as "this week".
    cum_since = max(trial_start, all_bars[0])
    for (bar, sym), info in sorted(bot.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if cum_since <= bar <= until and bar not in frames[sym].index:
            raise ParityInputError(
                f"closed-bar archive for {sym} has no bar closing {_iso(bar)}, which the bot decided on "
                f"(archive gap: the bot logs and ignores archive write failures)"
            )

    try:
        fee = args.fee if args.fee is not None else _paper_fee()
        hres = run_harness(frames, raw_cfg, trial_start, fee, harness)
    except KeyError as exc:
        raise ParityInputError(f"config is missing a key the harness needs: {exc}") from exc
    cmp = compare(bot, hres, cum_since, until, level_rtol=args.level_rtol, size_rtol=args.size_rtol)
    mismatches: list[Mismatch] = cmp["mismatches"]
    mismatches += check_trade_log(bot, events, read_trade_log(trade_path), cum_since, until)
    mismatches.sort(key=lambda m: (m.bar, m.symbol, m.kind))

    week = summarize(cmp, mismatches, since)
    trial = summarize(cmp, mismatches, cum_since)
    in_window = [m for m in mismatches if m.bar >= since]
    report.update({
        "fee": fee, "trial_to_date_since": _iso(cum_since),
        "entries": week["entries"], "exits": week["exits"],
        "decisions_compared": week["decisions_compared"],
        "mismatch_count": week["mismatch_count"], "unexplained_count": week["unexplained_count"],
        "mismatch_rate": week["mismatch_rate"],
        "trial_to_date": trial,
        "mismatches": [m.to_json() for m in in_window],
    })
    earlier_unexplained = trial["unexplained_count"] - week["unexplained_count"]
    if earlier_unexplained:
        report["notes"].append(
            f"{earlier_unexplained} unexplained mismatch(es) from before this window are still in the trial-to-date "
            f"record and count toward the budget (use --days/--since to list them)"
        )
    reasons = []
    if week["unexplained_count"]:
        reasons.append(f"{week['unexplained_count']} unexplained mismatch(es) in the window: an unexplained mismatch pauses the trial until fixed")
    # The design's budget is over the trial to date (cumulative from day 0), not a single week.
    for label, rate in (("entry", trial["entries"]["mismatch_rate"]), ("exit", trial["exits"]["mismatch_rate"]),
                        ("overall", trial["mismatch_rate"])):
        if rate > args.budget:
            reasons.append(f"trial-to-date {label} mismatch rate {rate * 100:.1f}% exceeds the {args.budget * 100:.0f}% budget")
    if reasons:
        status = FAIL
        report["message"] = "; ".join(reasons)
    elif in_window:
        status = WARN
        report["message"] = (f"{len(in_window)} mismatch(es) in the window, all explained; trial-to-date mismatch rates are "
                             f"within the {args.budget * 100:.0f}% budget")
    else:
        status = PASS
        vacuous = week["decisions_compared"] == 0
        report["message"] = (
            "no entry or exit decisions on either side in the window (nothing to compare; agreement is vacuous)"
            if vacuous else "the bot's decisions match the harness's"
        )
        report["vacuous"] = vacuous
    report["status"] = status
    report["exit_code"] = EXIT_CODES[status]
    return report


def _no_archive():
    raise ParityInputError("no closed-bar archive: set logging.closed_bars_dir in the config or pass --bars-dir")


def _paper_fee() -> float:
    from src.broker_paper import PaperBroker

    return float(inspect.signature(PaperBroker).parameters["fee_pct"].default)


def _insufficient(report: dict, why: str) -> dict:
    report["status"] = INSUFFICIENT
    report["exit_code"] = EXIT_CODES[INSUFFICIENT]
    report["message"] = f"insufficient data: {why}"
    return report


def format_text(r: dict[str, Any]) -> str:
    lines = [f"PAPER TRIAL PARITY CHECK -- {r['status']}", r["message"]]
    w = r.get("window")
    if w:
        lines.append(f"window: {w['since'][:10]} .. {w['until'][:10]}  ({w['decided_bars']} decided bars"
                     + (f", {w['symbols']} symbols)" if "symbols" in w else ")"))
    if r.get("trial_start"):
        lines.append(f"trial start: {r['trial_start'][:10]} ({r['trial_start_source']})")
    if r["status"] in (PASS, WARN, FAIL):
        for name in ("entries", "exits"):
            s = r[name]
            lines.append(
                f"{name:<8}: harness {s['harness']}, bot {s['bot']}, matched {s['matched']}, mismatched {s['mismatched']}; "
                f"agreement {_rate_text(s['agreement_rate'])}, mismatch {s['mismatch_rate'] * 100:.1f}% "
                f"(budget {r['budget'] * 100:.0f}%)"
            )
        lines.append(
            f"decisions compared: {r['decisions_compared']}; mismatches: {r['mismatch_count']} "
            f"(unexplained {r['unexplained_count']}); overall mismatch rate {r['mismatch_rate'] * 100:.1f}%"
        )
        t = r["trial_to_date"]
        lines.append(
            f"trial to date (since {r['trial_to_date_since'][:10]}): entries mismatch {t['entries']['mismatch_rate'] * 100:.1f}% "
            f"({t['entries']['mismatched']}/{max(1, t['entries']['harness'])}), exits mismatch {t['exits']['mismatch_rate'] * 100:.1f}% "
            f"({t['exits']['mismatched']}/{max(1, t['exits']['harness'])}), {t['mismatch_count']} mismatches "
            f"({t['unexplained_count']} unexplained) in {t['decisions_compared']} decisions"
        )
        lines.append(f"paper fee assumed: {r['fee'] * 100:.2f}%; budget: <= {r['budget'] * 100:.0f}% mismatches, "
                     f"an unexplained mismatch pauses the trial")
        if r["mismatches"]:
            lines.append("mismatches:")
            for m in r["mismatches"]:
                tag = f"explained: {m['explained_by']}" if m["explained_by"] else "UNEXPLAINED"
                lines.append(f"  {m['bar'][:10]}  {m['symbol']:<10} {m['kind']:<20} [{tag}]")
                lines.append(f"      {m['detail']}")
    for note in r.get("notes", []):
        lines.append(f"note: {note}")
    return "\n".join(lines)


def _clean(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items() if k != "_line"}
    if isinstance(obj, (list, tuple, set)):
        return [_clean(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, pd.Timestamp):
        return _iso(obj)
    return obj


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="config yaml (default: config/config.yaml); supplies the strategy parameters and log paths")
    p.add_argument("--audit-log", help="override logging.audit_log_path")
    p.add_argument("--trade-log", help="override logging.trade_log_path")
    p.add_argument("--bar-decisions", help="override logging.bar_decisions_path")
    p.add_argument("--bars-dir", help="override logging.closed_bars_dir")
    p.add_argument("--days", type=int, default=DEFAULT_WINDOW_BARS, help="window length in daily bars ending at the newest decided bar (default 7)")
    p.add_argument("--since", help="window start (bar close, ISO); overrides --days")
    p.add_argument("--until", help="window end (bar close, ISO); default the newest decided bar")
    p.add_argument("--trial-start", help="first bar close of the trial (ISO); default the earliest bar_close in the audit log. "
                   "Required if the audit log has been pruned past the trial start")
    p.add_argument("--min-bars", type=int, default=DEFAULT_MIN_BARS, help="fewer decided bars than this in the window is 'insufficient data'")
    p.add_argument("--budget", type=float, default=DEFAULT_BUDGET, help="mismatch budget (default 0.10, design §5)")
    p.add_argument("--fee", type=float, default=None, help="paper fee fraction (default: PaperBroker's)")
    p.add_argument("--level-rtol", type=float, default=DEFAULT_LEVEL_RTOL, help="relative tolerance for plan entry/stop/R:R (default 1e-6)")
    p.add_argument("--size-rtol", type=float, default=DEFAULT_SIZE_RTOL, help="relative tolerance for approved size (default 0.25)")
    p.add_argument("--format", choices=("text", "json"), default="text")
    p.add_argument("--json-out", help="also write the JSON report here (must not be one of the input files)")
    args = p.parse_args(argv)
    if args.days < 1 or args.min_bars < 1:
        p.error("--days and --min-bars must be >= 1")
    if not (0 <= args.budget <= 1):
        p.error("--budget must be within [0, 1]")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = build_report(args)
        if args.json_out:
            out = Path(args.json_out).resolve()
            inputs = [Path(v).resolve() for v in report["inputs"].values() if v]
            if out in inputs or any(parent in inputs for parent in out.parents):
                raise ParityInputError(f"--json-out {out} would overwrite an input; refusing")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(_clean(report), indent=2, allow_nan=False) + "\n", encoding="utf-8")
    except ParityInputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    print(json.dumps(_clean(report), indent=2, allow_nan=False) if args.format == "json" else format_text(report))
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
