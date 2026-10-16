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
  - entry decision: signal pattern, allowed vs not, rejection reason up to the first ';' (the same `same_decision`
    equivalence as tests/test_daily_live_parity.py), plan entry/stop/risk-reward to a relative 1e-6
    (the design's "plan levels" rule; live indicators use a 200-bar fetch, the archive replay uses all
    archived bars, so warm-up leaves ~1e-7), approved size within a loose tolerance (fill-price residual);
  - exits: (decision bar, symbol, stop_loss | take_profit);
  - integrity: a bar decided twice (P1 violation), audit fills missing from trades.csv and the reverse.

Every mismatch is listed with its reason and, when the audit log itself proves a documented cause, an
`explained_by` tag: staleness guard; bot gap (NO symbol has a decision event for that bar) or fetch failure
(that symbol's "market data unavailable" that day); kill switch; advisor veto; float phantom; or "downstream" of
an earlier explained mismatch (only for portfolio-dependent kinds: entry / size / rejection / exit; never
pattern or plan levels, which depend on the bars alone). `--ack BAR:SYMBOL:KIND` marks a fixed, reported mismatch
as acknowledged (listed in the report; it explains nothing that followed it).

Status (design §5: "at most 10% explained mismatches; an unexplained mismatch pauses the trial"):
  PASS               no mismatch in the window and none unexplained anywhere in the trial to date.
  WARN               mismatches in the window, every one explained or acknowledged, budget respected.
  FAIL               any unexplained mismatch in the trial to date (in the window, or earlier and not yet
                     acknowledged), or a trial-to-date entry / exit / overall mismatch rate above the budget (the
                     budget is evaluated from the first harness entry, as pre-registered; --relaxed-early-budget
                     evaluates it from 20 entries on: a loosening that needs the user's acceptance). Exit code 1. An implementation finding,
                     not a strategy verdict.
  INSUFFICIENT DATA  no decided bars in the window, or fewer than --min-bars. Exit code 0.
Exit code 2: unreadable / corrupt input, an audit log whose END is missing (the bar-decision ledger is ahead of
it), a pruned audit log older than the trade log, a bad --json-out (the message says which file and why).

No network. No writes to the bot's state: only --json-out, which refuses inputs, anything in the config's
`logging:` block (even with --force), and existing files unless --force.

Requirements for a meaningful run (docs/paper_parity_check.md): the bot ran with `loop.bar_parity: true` and
`logging.closed_bars_dir` set; the portfolio was reset at trial start; `logging.retention_days: 0` for the trial
(the audit log is pruned after 30 days by default and the replay needs it from day 0); `risk.min_order_quote_usd` > 0.
"""
from __future__ import annotations

import argparse
import inspect
import json
import math
import re
import sys
from array import array
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
# differing slightly. A size off by more than this fraction AND more than DEFAULT_SIZE_ATOL USD is reported as a mismatch.
DEFAULT_SIZE_RTOL = 0.10       # measured real residual <= 7.5% (p90 1.4%) over 110 bars at a $10 floor
DEFAULT_SIZE_ATOL = 1.0        # USD: leftover-room "dust" sizes differ a lot in relative terms and mean nothing
DEFAULT_BUDGET_MIN_ENTRIES = 0   # the pre-registered rule (design §5): budget evaluated from the first entry
RELAXED_BUDGET_MIN_ENTRIES = 20  # --relaxed-early-budget: a LOOSENING that needs the user's acceptance
MAX_UNMATCHED_ACKS = 5           # more acknowledgements than this matching nothing = a blanket ack list: FAIL
ACKABLE_KINDS = {"entry_decision", "size", "rejection_reason", "pattern", "plan_levels", "exit_harness_only",
                 "exit_bot_only", "harness_only_check", "bot_only_check", "advisor_veto", "bot_error"}
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
EXPLAINED_ACK = "acknowledged"
EXPLAINED_FETCH = "fetch_failure"
EXPLAINED_DOWNSTREAM = "downstream_of_explained_mismatch"
# Mismatches that are never relabelled "downstream": they are bugs in the bot's own bookkeeping.
INTEGRITY_KINDS = {"duplicate_decision", "audit_trade_log", "trade_log_audit", "missing_decision_event"}
# Only these depend on portfolio state (cash, exposure, open positions), so only these can be a consequence of an
# earlier divergence. Pattern and plan levels are functions of the bars alone: the design requires 100% there.
PORTFOLIO_KINDS = {"entry_decision", "size", "rejection_reason", "exit_harness_only", "exit_bot_only", "advisor_veto",
                   "harness_only_check", "bot_only_check"}  # the last two only when the Mismatch says it is state dependent

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
    if key.startswith("sized order "):
        # "sized order 7.67 is below the minimum order size 10 (...)": the leftover room that was sized
        # differs by the fill-price residual between the paper broker (close) and the harness (next open);
        # the DECISION (below the configured minimum) is the same. The minimum itself stays in the key.
        key = re.sub(r"^sized order \S+", "sized order #", key)
    return key


def _phantom_size(reason: str) -> float:
    # "sized order 9.094947e-13 is below the minimum order size 0.01 ..."
    try:
        return float(reason.split("sized order ")[1].split(" ")[0])
    except (IndexError, ValueError):
        return float("inf")


def reasons_equivalent(harness_reason: str, bot_reason: str) -> bool:
    """tests/test_daily_live_parity.py::same_decision's rejection-reason rule: equal up to the first
    ';' (cooldown: up to the first ':'; the sized amount in "sized order X is below the minimum" is ignored,
    a deliberate loosening of same_decision for the fill-price residual on real opens), or the same "nothing left" decision worded two ways when room
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
def iter_audit(path: Path):
    """The audit log's events one at a time as `(line number, event)`, in file order. Same checks, same error
    messages and same line numbers as the former whole-file `read_audit`; only the accumulation is gone.
    (A plain function wrapping a generator so a missing file raises at call time, as before.)"""
    if not path.exists():
        raise ParityInputError(f"audit log not found: {path}")
    return _iter_audit(path)


def _iter_audit(path: Path):
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
                yield n, event
    except (OSError, UnicodeDecodeError) as exc:
        raise ParityInputError(f"cannot read audit log {path}: {exc}") from exc


def read_audit(path: Path) -> list[dict]:
    """Every event as a list, each tagged with its `_line`. Memory grows with the log (about 1.1 KB per line):
    `build_report` does not use this any more (it uses `scan_audit`); it stays for tests and ad-hoc use."""
    events: list[dict] = []
    for n, event in iter_audit(path):
        event["_line"] = n
        events.append(event)
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
    entered: bool = False              # a buy was executed on this bar (immediate fill or a PENDING plan's FILL)
    stale: bool = False
    error: str | None = None
    advisor_veto: bool = False
    n_decision_events: int = 0         # events that are not "already decided" echoes
    n_scans: int = 0                   # events that ran the scanner (carry `candidates`)
    n_events: int = 0
    held_note: str | None = None
    note: str | None = None
    signal: dict | None = None
    lines: Any = field(default_factory=list)   # audit line numbers: a list, or a compact array('I') from scan_audit


def _bot_check(event: dict, line: Any = None) -> dict:
    rd, plan = event["risk_decision"], event.get("plan") or {}
    if not isinstance(rd, dict) or "allowed" not in rd:
        raise ParityInputError(f"audit line {line if line is not None else event.get('_line')}: malformed risk_decision {rd!r}")
    zone = plan.get("entry_zone")
    entry = _num(zone[1]) if isinstance(zone, (list, tuple)) and len(zone) == 2 else None
    signal = event.get("signal")
    return dict(
        allowed=bool(rd["allowed"]), reason=str(rd.get("reason", "")), quote=_num(rd.get("max_quote_amount")),
        entry=entry, stop=_num(plan.get("stop_loss")), rr=_num(plan.get("risk_reward")),
        pattern=signal.get("pattern") if isinstance(signal, dict) else None,
    )


class _BotCollector:
    """`collect_bot`'s loop body, fed one event at a time (so the audit log never has to be held in memory).
    `compact` stores each group's audit line numbers in an array('I') (4 bytes each instead of ~36)."""

    def __init__(self, compact: bool = False) -> None:
        self.groups: dict[tuple[pd.Timestamp, str], BotBar] = {}
        self.compact = compact
        self._bars: dict[str, pd.Timestamp] = {}  # bar_close string -> parsed (the same few dozen strings repeat)

    def parse_bar(self, event: dict, line: Any) -> pd.Timestamp:
        raw = event["bar_close"]
        if isinstance(raw, str):
            bar = self._bars.get(raw)
            if bar is None:
                bar = self._bars[raw] = _parse_ts(raw, f"audit line {line}: bar_close")
            return bar
        return _parse_ts(raw, f"audit line {line}: bar_close")

    def feed(self, event: dict, line: Any) -> pd.Timestamp | None:
        """Returns the event's parsed bar close (None when it has no `bar_close`)."""
        if "bar_close" not in event:
            return None
        symbol = event.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise ParityInputError(f"audit line {line}: event with bar_close has no symbol")
        bar = self.parse_bar(event, line)
        info = self.groups.get((bar, symbol))
        if info is None:
            info = self.groups[(bar, symbol)] = BotBar(bar=bar, symbol=symbol)
            if self.compact:
                info.lines = array("I")
        info.n_events += 1
        info.lines.append(line)
        note = event.get("note")
        if isinstance(note, str) and ALREADY_DECIDED in note:
            return bar  # the second+ tick of a bar: nothing decided here
        info.n_decision_events += 1
        if "candidates" in event:
            info.n_scans += 1
        if "risk_decision" in event:
            info.n_checks += 1
            if info.check is None:
                info.check = _bot_check(event, line)
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
        ex = event.get("execution")
        if isinstance(ex, dict) and ex.get("side") == "buy" and (event.get("action") == "planned" or action == "FILL"):
            info.entered = True
        return bar

    def result(self) -> tuple[dict[tuple[pd.Timestamp, str], BotBar], list[pd.Timestamp]]:
        return self.groups, sorted({b for b, _ in self.groups})


def collect_bot(events: list[dict]) -> tuple[dict[tuple[pd.Timestamp, str], BotBar], list[pd.Timestamp]]:
    """Groups the audit events that carry `bar_close` by (bar, symbol). Returns the groups and every
    bar close seen (any symbol)."""
    collector = _BotCollector()
    for event in events:
        collector.feed(event, event.get("_line"))
    return collector.result()


@dataclass
class AuditScan:
    """What the whole check needs from the audit log, built in ONE streaming pass by `scan_audit`: the
    per-(bar, symbol) groups, every bar close, and the compact cross-check evidence. Its memory is the
    groups (a few hundred bytes per (bar, symbol)) plus 16 bytes per audit line, not ~1.1 KB per line."""

    bot: dict[tuple[pd.Timestamp, str], BotBar]
    all_bars: list[pd.Timestamp]
    evidence: AuditEvidence

    @property
    def n_events(self) -> int:
        return self.evidence.n_events


def scan_audit(path: Path) -> AuditScan:
    """Streams the audit log once. Equivalent to `read_audit` + `collect_bot` + the evidence the later steps
    read from the full event list, including error precedence: a corrupt / unreadable line anywhere in the file
    is reported before a problem `collect_bot` finds in an earlier line (the whole file used to be read first),
    so the first such problem is held back until the file has been read to its end."""
    collector, evidence = _BotCollector(compact=True), AuditEvidence()
    deferred: ParityInputError | None = None
    for n, event in iter_audit(path):
        if deferred is not None:
            continue  # keep reading: a corrupt line further down takes precedence over this error
        try:
            bar = collector.feed(event, n)
        except ParityInputError as exc:
            deferred = exc
            continue
        evidence.feed(event, bar)
    if deferred is not None:
        raise deferred
    bot, all_bars = collector.result()
    return AuditScan(bot=bot, all_bars=all_bars, evidence=evidence)


# --------------------------------------------------------------------------- #
# The harness's side
# --------------------------------------------------------------------------- #
@dataclass
class HarnessResult:
    checks: dict[tuple[pd.Timestamp, str], dict]
    exits: set[tuple[pd.Timestamp, str, str]]
    last_bar: dict[str, pd.Timestamp]
    n_bars: int
    # (symbol, entry decision bar, exit decision bar or None while open): when the harness held each position.
    # None = unknown (hand-made results in unit tests): the downstream window is then never reset.
    intervals: list[tuple[str, pd.Timestamp, pd.Timestamp | None]] | None = None
    signal_keys: set[tuple[pd.Timestamp, str]] = field(default_factory=set)  # (bar, symbol) the harness had a bullish signal for


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
    for (bar, symbol), check in checks.items():
        sig = signals.get(symbol, {}).get(bar)
        check["pattern"] = getattr(sig, "primary_pattern", None)
    exits = {
        (pd.Timestamp(t.exit_time) - TF_DELTA, t.symbol, t.reason)
        for t in result.trades.itertuples()
        if t.reason in ("stop_loss", "take_profit")
    }
    n_bars = len(next(iter(enriched.values())).index)
    signal_keys = {(ts, sym) for sym, per in signals.items() for ts in per}
    intervals = [
        (t.symbol, pd.Timestamp(t.entry_time) - TF_DELTA,
         pd.Timestamp(t.exit_time) - TF_DELTA if t.reason in ("stop_loss", "take_profit") else None)
        for t in result.trades.itertuples()
    ]
    return HarnessResult(checks=checks, exits=exits, last_bar=last_bar, n_bars=n_bars, signal_keys=signal_keys,
                         intervals=intervals)


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
    eligible_downstream: bool = True  # may be relabelled "downstream" (decided in compare(), see PORTFOLIO_KINDS)

    def to_json(self) -> dict:
        return {"kind": self.kind, "bar": _iso(self.bar), "symbol": self.symbol, "detail": self.detail,
                "harness": self.harness, "bot": self.bot, "explained_by": self.explained_by}


@dataclass
class Evidence:
    """What the audit log can prove about WHY the bot has no decision for a bar."""

    bars_with_events: set[pd.Timestamp] = field(default_factory=set)       # bars any symbol has a decision event for
    fetch_failed: set[tuple[pd.Timestamp, str]] = field(default_factory=set)  # "market data unavailable" that day


def _gap_reason(key: tuple[pd.Timestamp, str], ev: Evidence) -> str | None:
    """`bot_gap` / `fetch_failure` only with proof: NO symbol has a decision event for the bar (the bot was not
    running / not cycling), or this symbol's fetch failed that day. One symbol missing from a bar the bot
    otherwise decided is not a gap: it is unexplained (or, if an echo proves it, a lost line)."""
    if key in ev.fetch_failed:
        return EXPLAINED_FETCH
    if key[0] not in ev.bars_with_events:
        return EXPLAINED_GAP
    return None


def _explain_entry(h: dict | None, b: dict | None, info: BotBar | None, key: tuple[pd.Timestamp, str], ev: Evidence) -> str | None:
    """A documented cause the audit log itself proves, or None."""
    if info is None or info.n_decision_events == 0:
        # info with only "already decided" echoes: the deciding line was lost -> missing_decision_event (integrity)
        return _gap_reason(key, ev) if info is None else None
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
    size_atol: float = DEFAULT_SIZE_ATOL,
    evidence: Evidence | None = None,
) -> dict[str, Any]:
    ev = evidence if evidence is not None else Evidence(bars_with_events={k[0] for k, i in bot.items() if i.n_decision_events})

    def in_window(bar: pd.Timestamp) -> bool:
        return since <= bar <= until

    H = {k: c for k, c in hres.checks.items() if in_window(k[0]) and k[0] <= hres.last_bar.get(k[1], until)}
    B = {k: info for k, info in bot.items() if in_window(k[0]) and info.check is not None}
    mismatches: list[Mismatch] = []
    drift_max = 0.0

    for key in sorted(set(H) | set(B), key=lambda k: (k[0], k[1])):
        bar, symbol = key
        h, info = H.get(key), bot.get(key)
        b = B[key].check if key in B else None
        if h is not None and b is not None:
            if h["allowed"] and b["allowed"] and h["quote"] and b["quote"] and max(h["quote"], b["quote"]) >= size_atol:
                drift_max = max(drift_max, abs(h["quote"] - b["quote"]) / max(h["quote"], b["quote"]))
            # Pattern and plan levels are functions of the bars alone: ALWAYS checked first, each its own mismatch,
            # never explained and never "downstream", whatever the allowed/rejected outcome is (the design requires
            # 100% there). A bot event with no signal is flagged, not skipped.
            if h.get("pattern") and h["pattern"] != b.get("pattern"):
                mismatches.append(Mismatch(
                    "pattern", bar, symbol,
                    f"signal pattern differs: harness {h['pattern']} vs bot {b.get('pattern') or 'missing (the bot event has no signal)'}",
                    h, b, None, eligible_downstream=False,
                ))
            if any(not _close(h[k], b[k], level_rtol) for k in ("entry", "stop", "rr")):
                mismatches.append(Mismatch(
                    "plan_levels", bar, symbol,
                    f"entry/stop/rr differ beyond rel {level_rtol:g}: harness "
                    f"({h['entry']}, {h['stop']}, {h['rr']}) vs bot ({b['entry']}, {b['stop']}, {b['rr']})",
                    h, b, None, eligible_downstream=False,
                ))
            if h["allowed"] != b["allowed"]:
                kind, detail = "entry_decision", (
                    f"harness {'approved' if h['allowed'] else 'rejected'}"
                    f" ({h['reason'] if not h['allowed'] else 'ok'}); bot {'approved' if b['allowed'] else 'rejected'}"
                    f" ({b['reason'] if not b['allowed'] else 'ok'})"
                )
            elif not h["allowed"] and not reasons_equivalent(h["reason"], b["reason"]):
                kind, detail = "rejection_reason", f"harness: {h['reason']!r}; bot: {b['reason']!r}"
            elif h["allowed"] and not _size_close(h["quote"], b["quote"], size_rtol, size_atol) and not (
                (h["quote"] or 0) < PHANTOM_USD and (b["quote"] or 0) < PHANTOM_USD
            ):
                kind, detail = "size", (
                    f"approved quote differs beyond rel {size_rtol:g} and {size_atol:g} USD: "
                    f"harness {h['quote']} vs bot {b['quote']}"
                )
            else:
                continue
            m = Mismatch(kind, bar, symbol, detail, h, b, _explain_entry(h, b, info, key, ev))
        elif h is not None:
            why = "no bot decision event for this bar" if info is None or info.n_decision_events == 0 else (
                info.note or "bot ran no entry check (no signal / vetoed / blocked / held)"
            )
            if info is not None and info.error:
                why = f"bot error: {info.error}"
            m = Mismatch(
                "harness_only_check", bar, symbol,
                f"harness {'approved' if h['allowed'] else 'rejected'} an entry ({h['reason']}); bot: {why}",
                h, None, _explain_entry(h, None, info, key, ev),
                # state dependent only if the bot was holding a plan on the symbol; "no signal" is not
                eligible_downstream=info is not None and info.held_note is not None,
            )
        else:
            m = Mismatch(
                "bot_only_check", bar, symbol,
                f"bot {'approved' if b['allowed'] else 'rejected'} an entry ({b['reason']}); the harness ran no check "
                f"(no signal / no plan)", None, b, _explain_entry(None, b, info, key, ev),
                # state dependent only if the harness HAD a signal there but held a position (no plan built)
                eligible_downstream=(bar, symbol) in hres.signal_keys,
            )
        mismatches.append(m)

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

    # Integrity: a bar decided twice or re-scanned (P1 says exactly once); a deciding line lost while its
    # "already decided" echo survives.
    for key, info in sorted(bot.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if not in_window(key[0]):
            continue
        if info.n_checks > 1 or info.n_scans > 1:
            mismatches.append(Mismatch(
                "duplicate_decision", key[0], key[1],
                f"{max(info.n_checks, info.n_scans)} entry scans/decisions for one bar (audit lines {list(info.lines)}); P1 allows one",
                None, info.check, None,
            ))
        if info.n_events and info.n_decision_events == 0:
            mismatches.append(Mismatch(
                "missing_decision_event", key[0], key[1],
                f"only 'already decided' echoes remain for this bar (audit lines {list(info.lines)}): the line that decided it is missing",
                None, None, None,
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
            explained = _gap_reason((bar, symbol), ev) if info is None else None
            if info is not None and info.error:
                detail += f" (bot error: {info.error})"
        mismatches.append(Mismatch("exit_harness_only", bar, symbol, detail, {"reason": reason}, {"reason": other}, explained))
    for bar, symbol, reason in sorted(Bx - Hx, key=lambda x: (x[0], x[1], x[2])):
        if any((b2, s2) == (bar, symbol) for b2, s2, _ in Hx):
            continue  # reported above as a reason difference
        mismatches.append(Mismatch("exit_bot_only", bar, symbol, f"bot exits ({reason}); the harness did not", None, {"reason": reason}, None))

    mismatches.sort(key=lambda m: (m.bar, m.symbol, m.kind))
    # "Downstream" is bounded: it starts at an explained mismatch and ENDS as soon as both sides hold the same open
    # positions again (checked at the start of each later bar). Downstream-tagged mismatches still count toward the
    # budget like any other (summarize counts every mismatch), so the tag can never hide an unbounded divergence.
    bot_iv = _bot_intervals(bot) if hres.intervals is not None else None
    cascade, last_bar = False, None
    for m in mismatches:
        if cascade and bot_iv is not None and m.bar != last_bar:
            if _held_before(bot_iv, m.bar) == _held_before(hres.intervals, m.bar):
                cascade = False
        last_bar = m.bar
        if m.explained_by is None and cascade and m.kind in PORTFOLIO_KINDS and m.eligible_downstream:
            m.explained_by = EXPLAINED_DOWNSTREAM
        elif m.explained_by is not None and m.explained_by != EXPLAINED_DOWNSTREAM:
            cascade = True

    return {
        "mismatches": mismatches,
        "H": H, "B": {k: v.check for k, v in B.items()},
        "H_entries": H_entries, "B_entries": B_entries, "Hx": Hx, "Bx": Bx,
        "size_drift_max": drift_max,
    }


def _bot_intervals(bot: dict[tuple[pd.Timestamp, str], BotBar]) -> list[tuple[str, pd.Timestamp, pd.Timestamp | None]]:
    """When the bot held each position, from its audit events: opened on a bar with an executed buy, closed on the
    bar of an executed stop-loss / take-profit."""
    out: list[tuple[str, pd.Timestamp, pd.Timestamp | None]] = []
    open_at: dict[str, pd.Timestamp] = {}
    for (bar, symbol), info in sorted(bot.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if info.entered and symbol not in open_at:
            open_at[symbol] = bar
        if info.exit_reason and symbol in open_at:
            out.append((symbol, open_at.pop(symbol), bar))
    out += [(sym, e, None) for sym, e in open_at.items()]
    return out


def _held_before(intervals: list[tuple[str, pd.Timestamp, pd.Timestamp | None]], bar: pd.Timestamp) -> set[str]:
    """Symbols held when `bar` starts: entered on an earlier bar and not yet exited before this one."""
    return {s for s, e, x in intervals if e < bar and (x is None or x >= bar)}


def _size_close(a: float | None, b: float | None, rtol: float, atol: float) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= max(atol, rtol * max(abs(a), abs(b)))


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


def _bar_at(stamped: list[tuple[pd.Timestamp, pd.Timestamp]], when: pd.Timestamp, default: pd.Timestamp) -> pd.Timestamp:
    """The bar a wall-clock time belongs to: the bar of the latest audit line written at or before it."""
    best = None
    for ts, bar in stamped:
        if ts > when:
            break
        best = bar
    return best if best is not None else (stamped[0][1] if stamped else default)


class AuditEvidence:
    """Everything the cross-checks need from the audit log besides the per-(bar, symbol) groups, kept compact:
    the event count, the earliest event time, the "market data unavailable" days, the executed fills, and one
    (wall-clock time, bar) stamp per bar-carrying line as two flat arrays (12 bytes a line) that
    `stamps()` later collapses to one entry per run of equal bars. Fed one event at a time."""

    def __init__(self) -> None:
        self.n_events = 0
        self.first_ts: pd.Timestamp | None = None            # earliest parseable `timestamp` of ANY event
        self.fetch_failed: set[tuple[pd.Timestamp, str]] = set()
        self.fills: list[tuple[str, str, str, pd.Timestamp]] = []  # (timestamp, symbol, side, bar): ALL bars, filtered by the caller
        self._stamp_ns = array("q")
        self._stamp_bar = array("i")
        self._bars: list[pd.Timestamp] = []
        self._bar_index: dict[pd.Timestamp, int] = {}

    def feed(self, event: dict, bar: pd.Timestamp | None) -> None:
        """`bar` is the event's parsed `bar_close`, or None when it has none."""
        self.n_events += 1
        ts = _try_ts(event.get("timestamp"))
        if ts is not None and (self.first_ts is None or ts < self.first_ts):
            self.first_ts = ts
        if bar is None:
            if ts is not None and "market data unavailable" in str(event.get("error", "")) and isinstance(event.get("symbol"), str):
                self.fetch_failed.add((ts.floor("D"), event["symbol"]))
            return
        if ts is not None:  # the old code required a str timestamp that parsed; a parseable one is always a str
            idx = self._bar_index.get(bar)
            if idx is None:
                idx = self._bar_index[bar] = len(self._bars)
                self._bars.append(bar)
            self._stamp_ns.append(ts.value)
            self._stamp_bar.append(idx)
        if isinstance(event.get("execution"), dict):
            ex = event["execution"]
            self.fills.append((str(ex.get("timestamp")), str(event.get("symbol")), str(ex.get("side")), bar))

    def stamps(self) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
        """The (time, bar) stamps sorted by time (stable), collapsed to the first stamp of each run of equal
        bars. `_bar_at` (the last stamp at or before a time) and the first in-range stamp -- the only two
        questions asked of the full list -- have the same answer on the collapsed list."""
        if not len(self._stamp_ns):
            return []
        ns = np.frombuffer(self._stamp_ns, dtype=np.int64)
        bars = np.frombuffer(self._stamp_bar, dtype=np.int32)
        order = np.argsort(ns, kind="stable")
        ns, bars = ns[order], bars[order]
        starts = np.concatenate(([True], bars[1:] != bars[:-1]))
        return [(pd.Timestamp(int(t), tz="UTC"), self._bars[int(i)]) for t, i in zip(ns[starts], bars[starts])]


def check_trade_log(
    events: list[dict], rows: list[dict] | None, since: pd.Timestamp, until: pd.Timestamp,
) -> list[Mismatch]:
    """List form: see `check_trade_log_evidence`."""
    evidence = AuditEvidence()
    for ev in events:
        bar = _parse_ts(ev["bar_close"], f"audit line {ev.get('_line')}: bar_close") if "bar_close" in ev else None
        evidence.feed(ev, bar)
    return check_trade_log_evidence(evidence, rows, since, until)


def check_trade_log_evidence(
    evidence: AuditEvidence, rows: list[dict] | None, since: pd.Timestamp, until: pd.Timestamp,
) -> list[Mismatch]:
    """Every fill the audit log reports for bars in [since, until] must be a trades.csv row (same symbol, side and
    timestamp string -- the bot logs the FillResult's own timestamp), and every trades.csv row written at or after
    the first audited event of that range, and belonging to a bar up to `until`, must be an audited fill.
    (trades.csv is never pruned, so a missing audit TAIL shows up as trade rows with no audit fill; a row is
    assigned to the bar of the latest audit line written before it.)"""
    out: list[Mismatch] = []
    fills = [f for f in evidence.fills if since <= f[3] <= until]  # (timestamp, symbol, side, bar)
    stamped = evidence.stamps()                                    # (wall-clock time of an audit line, its bar close)
    if rows is None:
        for ts, sym, side, bar in fills:
            out.append(Mismatch("audit_trade_log", bar, sym, f"audit {side} at {ts} but no trade log was found", None, None, None))
        return out
    logged = {(r["timestamp"], r["symbol"], r["side"]) for r in rows}
    for ts, sym, side, bar in fills:
        if (ts, sym, side) not in logged:
            out.append(Mismatch("audit_trade_log", bar, sym, f"audit reports a {side} at {ts} that is not in the trade log", None, None, None))
    in_range = [t for t, bar in stamped if since <= bar <= until]
    if not in_range:
        return out
    lo = in_range[0]
    audited = {(ts, sym, side) for ts, sym, side, _ in fills}
    for r in rows:
        try:
            t = _parse_ts(r["timestamp"], "trade log timestamp")
        except ParityInputError:
            continue
        if t < lo or (r["timestamp"], r["symbol"], r["side"]) in audited:
            continue
        bar = _bar_at(stamped, t, until)
        if bar <= until:
            out.append(Mismatch("trade_log_audit", bar, r["symbol"], f"trade log {r['side']} at {r['timestamp']} has no audit-log fill", None, None, None))
    return out


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #
def _rate_text(rate: float | None) -> str:
    return "n/a (nothing to compare)" if rate is None else f"{rate * 100:.1f}%"


def pick_trial_start(
    bot: dict[tuple[pd.Timestamp, str], BotBar], all_bars: list[pd.Timestamp], arg: pd.Timestamp | None
) -> tuple[pd.Timestamp, str]:
    """The first bar the replay decides on. Without --trial-start: the earliest bar in the audit log. With it
    (any time, e.g. the moment the audit log was pruned mid-bar): the first audited bar at or after that time's
    bar boundary, since the bot decides a bar a few minutes AFTER its close. Either way a first bar that only
    some of the symbols survive (a retention cut through the cycle) is skipped."""
    candidates = [b for b in all_bars if arg is None or b >= arg.floor("D")]
    if not candidates:
        raise ParityInputError(f"--trial-start {_iso(arg)} is after the newest decided bar {_iso(all_bars[-1])}")
    note = "--trial-start" if arg is not None else "earliest bar_close in the audit log"
    symbols = {b: {sym for (bb, sym) in bot if bb == b} for b in candidates[:2]}
    if len(candidates) > 1 and symbols[candidates[0]] < symbols[candidates[1]]:
        return candidates[1], f"{note}; the first bar {_iso(candidates[0])} is only partly in the audit log (retention cut), skipped"
    return candidates[0], note


def parse_acks(values: list[str], ack_file: str | None) -> dict[tuple[pd.Timestamp, str, str], str]:
    """`--ack BAR:SYMBOL:KIND` (BAR a date or ISO close time, KIND a mismatch kind such as `plan_levels`) and
    `--ack-file` (one per line, `#` comments). Integrity kinds (a lost or duplicated decision line, audit / trade log
    disagreement) cannot be acknowledged: they are faults in the bot's own logs."""
    raw = list(values or [])
    if ack_file:
        try:
            raw += [line.split("#", 1)[0].strip() for line in Path(ack_file).read_text(encoding="utf-8").splitlines()]
        except (OSError, UnicodeDecodeError) as exc:
            raise ParityInputError(f"cannot read --ack-file {ack_file}: {exc}") from exc
    out: dict[tuple[pd.Timestamp, str, str], str] = {}
    for entry in raw:
        if not entry:
            continue
        parts = entry.rsplit(":", 2)
        if len(parts) != 3 or not all(parts):
            raise ParityInputError(f"bad acknowledgement {entry!r}: expected BAR:SYMBOL:KIND, e.g. 2026-10-12:BTC/USD:plan_levels")
        bar, symbol, kind = parts
        if kind in INTEGRITY_KINDS:
            raise ParityInputError(f"acknowledgement {entry!r}: {kind} is an integrity fault in the bot's own logs and cannot be acknowledged; fix it")
        if kind not in ACKABLE_KINDS:
            raise ParityInputError(f"acknowledgement {entry!r}: unknown kind {kind!r} (one of {sorted(ACKABLE_KINDS)})")
        out[(_parse_ts(bar, f"acknowledgement {entry!r}"), symbol, kind)] = entry
    return out


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
    acks = parse_acks(args.ack, args.ack_file)

    report: dict[str, Any] = {
        "status": None, "exit_code": None, "message": "", "budget": args.budget,
        "inputs": {"config": str(cfg_path), "audit_log": str(audit_path), "trade_log": str(trade_path),
                   "bar_decisions": str(ledger_path), "closed_bars_dir": str(bars_dir) if bars_dir else None},
        "window": None, "notes": [], "mismatches": [],
        "_protected": protected_paths(raw_cfg, args.ack_file),
    }

    scan = scan_audit(audit_path)
    bot, all_bars = scan.bot, scan.all_bars
    if not scan.n_events:
        return _insufficient(report, "the audit log is empty: no decisions to compare yet")
    if not all_bars:
        return _insufficient(
            report,
            f"none of the {scan.n_events} audit lines carries bar_close: the bot was not running with "
            f"loop.bar_parity: true, so there are no per-bar decisions to compare",
        )

    until = _parse_ts(args.until, "--until") if args.until else all_bars[-1]
    since = _parse_ts(args.since, "--since") if args.since else until - (args.days - 1) * TF_DELTA
    trial_start, source = pick_trial_start(bot, all_bars, _parse_ts(args.trial_start, "--trial-start") if args.trial_start else None)
    report["trial_start"] = _iso(trial_start)
    report["trial_start_source"] = source
    if since < trial_start:
        report["notes"].append(f"window start {_iso(since)} is before the trial start; clamped to {_iso(trial_start)}")
        since = trial_start
    window_bars = [b for b in all_bars if since <= b <= until]
    report["window"] = {"since": _iso(since), "until": _iso(until), "decided_bars": len(window_bars)}
    for (ack_bar, _sym, _kind), entry in acks.items():
        if ack_bar > all_bars[-1] or (args.until and ack_bar > until):
            raise ParityInputError(
                f"acknowledgement {entry!r} is dated after "
                + (f"--until {_iso(until)}" if args.until and ack_bar > until else f"the newest decided bar {_iso(all_bars[-1])}")
                + ": it cannot refer to a mismatch that exists"
            )
    # The END of the audit log must be intact: the ledger is written before each decision's audit line.
    ledger = read_ledger(ledger_path)
    if ledger is None:
        report["notes"].append(
            f"WARN: no bar-decision ledger found at {ledger_path}: the check that the END of the audit log is intact was skipped"
        )
    if ledger is not None:
        latest: dict[str, pd.Timestamp] = {}
        for (bar, sym), _info in bot.items():
            latest[sym] = max(latest.get(sym, bar), bar)
        ahead = sorted(s for s, t in ledger.items() if s not in latest or t > latest[s])
        if ahead:
            s0 = ahead[0]
            raise ParityInputError(
                f"the bar-decision ledger {ledger_path} records {s0} decided at {_iso(ledger[s0])} (and {len(ahead) - 1} other "
                f"symbol(s) ahead) but the newest audit line for it is "
                f"{_iso(latest[s0]) if s0 in latest else 'absent'}: the END of the audit log is missing (truncated, copied "
                f"mid-write, or lines lost), so the newest decisions cannot be checked. Re-run while the bot is idle, "
                f"or restore the log"
            )
    # trades.csv is never pruned; the audit log is (logging.retention_days). Older trades than audit lines means
    # the audit log no longer reaches back to the start of the trade record.
    trade_rows = read_trade_log(trade_path)
    if trade_rows and not args.trial_start:
        first_event = scan.evidence.first_ts
        first_trade = min((t for t in (_try_ts(r["timestamp"]) for r in trade_rows) if t is not None), default=None)
        if first_event is not None and first_trade is not None and first_trade < first_event:
            raise ParityInputError(
                f"the trade log starts at {_iso(first_trade)}, before the audit log ({_iso(first_event)}): the audit log "
                f"has been pruned (logging.retention_days, default 30) so the trial cannot be replayed from day 0. Pass "
                f"--trial-start <first close of the trial> to compare from where the audit log starts, and set "
                f"logging.retention_days: 0 for the rest of the trial"
            )

    symbols = sorted({sym for (_bar, sym) in bot})
    report["window"]["symbols"] = len(symbols)
    from strategy_pack_run import backtest_live_bot as harness  # heavy import: after the cheap exits above

    frames = load_frames(bars_dir, symbols, harness) if bars_dir else _no_archive()
    # The comparison always runs over the whole trial to date (the portfolio is path dependent, and the design's
    # 10% budget is cumulative); the window is the part reported as "this week".
    cum_since = trial_start
    for (bar, sym), info in sorted(bot.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if cum_since <= bar <= until and bar not in frames[sym].index:
            raise ParityInputError(
                f"closed-bar archive for {sym} has no bar closing {_iso(bar)}, which the bot decided on "
                f"(archive gap: the bot logs and ignores archive write failures)"
            )

    evidence = Evidence(
        bars_with_events={bar for (bar, _s), i in bot.items() if i.n_decision_events},
        fetch_failed=scan.evidence.fetch_failed,
    )
    try:
        fee = args.fee if args.fee is not None else _paper_fee()
        hres = run_harness(frames, raw_cfg, trial_start, fee, harness)
    except KeyError as exc:
        raise ParityInputError(f"config is missing a key the harness needs: {exc}") from exc
    cmp = compare(bot, hres, cum_since, until, level_rtol=args.level_rtol, size_rtol=args.size_rtol,
                  size_atol=args.size_atol, evidence=evidence)
    mismatches: list[Mismatch] = cmp["mismatches"]
    mismatches += check_trade_log_evidence(scan.evidence, trade_rows, cum_since, until)
    mismatches.sort(key=lambda m: (m.bar, m.symbol, m.kind))

    # Acknowledgements (explicit, per bar and symbol, listed in the report) are applied after the downstream
    # labelling, so acknowledging a mismatch never silently explains what followed it.
    used = set()
    for m in mismatches:
        entry = acks.get((m.bar, m.symbol, m.kind))
        if entry is not None:
            used.add(entry)
            if m.explained_by is None:
                m.explained_by = EXPLAINED_ACK
    report["acknowledged"] = sorted(set(acks.values()) & used)
    unmatched = sorted(set(acks.values()) - used)
    report["unmatched_acks"] = unmatched

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
        "size_drift_max": cmp["size_drift_max"],
        "mismatches": [m.to_json() for m in in_window],
    })
    if (raw_cfg.get("risk") or {}).get("min_order_quote_usd", 0) in (0, None):
        report["notes"].append(
            "risk.min_order_quote_usd is 0: with no order floor a correct bot still diverges from the harness on float "
            "'phantom' plans (docs/bot_bar_parity.md §4.2) and the trial parity budget will not hold. The trial needs a "
            "floor > 0 (the min-order study recommends 10; that decision is the user's)"
        )
    reasons = []
    if week["unexplained_count"]:
        reasons.append(f"{week['unexplained_count']} unexplained mismatch(es) in the window: an unexplained mismatch pauses the trial until fixed")
    earlier = [m for m in mismatches if m.explained_by is None and m.bar < since]
    if earlier:
        first = earlier[0]
        reasons.append(
            f"{len(earlier)} unexplained mismatch(es) earlier in the trial still pause it (first: {_iso(first.bar)[:10]} "
            f"{first.symbol} {first.kind}); once fixed, acknowledge each with --ack BAR:SYMBOL:KIND"
        )
    if len(unmatched) > MAX_UNMATCHED_ACKS:
        reasons.append(
            f"{len(unmatched)} acknowledgements match no mismatch (more than {MAX_UNMATCHED_ACKS}): a blanket acknowledgement "
            f"list is not accepted; acknowledge only specific reported mismatches"
        )
    # The design's budget is over the trial to date (cumulative from day 0), not a single week. By default it is
    # the pre-registered rule: evaluated from the first harness entry. --relaxed-early-budget (a loosening that
    # needs the user's acceptance) evaluates it only from RELAXED_BUDGET_MIN_ENTRIES entries on.
    n_entries = trial["entries"]["harness"]
    budget_evaluable = n_entries >= args.budget_min_entries
    report["budget_evaluated"] = budget_evaluable
    if budget_evaluable:
        for label, rate in (("entry", trial["entries"]["mismatch_rate"]), ("exit", trial["exits"]["mismatch_rate"]),
                            ("overall", trial["mismatch_rate"])):
            if rate > args.budget:
                reasons.append(f"trial-to-date {label} mismatch rate {rate * 100:.1f}% exceeds the {args.budget * 100:.0f}% budget")
    else:
        report["notes"].append(
            f"budget not yet evaluable: {n_entries} harness entries to date, evaluated from {args.budget_min_entries} "
            f"(--relaxed-early-budget is a loosening of the pre-registered rule and needs the user's acceptance). "
            f"Unexplained mismatches still fail"
        )
    short = len(window_bars) < args.min_bars
    report["window"]["short"] = short
    if reasons:
        status = FAIL
        report["message"] = "; ".join(reasons)
    elif short:
        status = INSUFFICIENT
        report["message"] = (
            f"insufficient data: only {len(window_bars)} decided bar(s) between {_iso(since)} and {_iso(until)}; need at least "
            f"{args.min_bars} (--min-bars). No unexplained mismatch anywhere in the trial to date. Run again once more daily closes have been decided"
        )
    elif in_window or unmatched:
        status = WARN
        bits = []
        if in_window:
            bits.append(f"{len(in_window)} mismatch(es) in the window, all explained or acknowledged")
        if unmatched:
            bits.append(f"{len(unmatched)} acknowledgement(s) match no mismatch: {', '.join(unmatched)}")
        report["message"] = "; ".join(bits) + "; trial-to-date " + (
            f"mismatch rates are within the {args.budget * 100:.0f}% budget" if budget_evaluable else "budget not yet evaluable")
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


def _try_ts(value: Any) -> pd.Timestamp | None:
    try:
        return _parse_ts(value, "timestamp")
    except ParityInputError:
        return None


def protected_paths(raw_cfg: dict[str, Any], ack_file: str | None = None) -> list[Path]:
    """Everything --json-out may never point at or inside, --force or not: the files and directories the bot writes
    (the config's `logging:` block, the kill switch at its configured OR default path), the --ack-file, the repo's
    `.env` and `config/config.yaml` (whichever --config was given)."""
    values = [v for v in (raw_cfg.get("logging") or {}).values() if isinstance(v, str) and v.strip()]
    kill = (raw_cfg.get("risk") or {}).get("kill_switch_file", "KILL_SWITCH")
    values.append(kill if isinstance(kill, str) and kill.strip() else "KILL_SWITCH")
    out = [_resolve(v).resolve() for v in values]
    out += [(PROJECT_ROOT / ".env").resolve(), (PROJECT_ROOT / "config" / "config.yaml").resolve()]
    if ack_file:
        out.append(Path(ack_file).resolve())
    return out


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
        lines.append(f"paper fee assumed: {r['fee'] * 100:.2f}%; max approved-size drift vs harness {r['size_drift_max'] * 100:.1f}%; "
                     f"budget: <= {r['budget'] * 100:.0f}% mismatches"
                     + ("" if r.get("budget_evaluated", True) else " (not yet evaluable)") + ", an unexplained mismatch pauses the trial")
        if r.get("acknowledged"):
            lines.append("acknowledged: " + ", ".join(r["acknowledged"]))
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
        return {str(k): _clean(v) for k, v in obj.items() if k not in ("_line", "_protected")}
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
    p.add_argument("--size-rtol", type=float, default=DEFAULT_SIZE_RTOL, help="relative tolerance for approved size (default 0.10)")
    p.add_argument("--size-atol", type=float, default=DEFAULT_SIZE_ATOL, help="absolute USD tolerance for approved size (default 1.0)")
    p.add_argument("--budget-min-entries", type=int, default=DEFAULT_BUDGET_MIN_ENTRIES,
                   help="evaluate the mismatch budget only once the trial has this many harness entries "
                        "(default 0 = the pre-registered rule: from the first entry)")
    p.add_argument("--relaxed-early-budget", action="store_true",
                   help="LOOSENING of the pre-registered rule, needs the user's acceptance: evaluate the budget only from "
                        f"{RELAXED_BUDGET_MIN_ENTRIES} harness entries on (same as --budget-min-entries {RELAXED_BUDGET_MIN_ENTRIES})")
    p.add_argument("--ack", action="append", default=[], metavar="BAR:SYMBOL:KIND",
                   help="acknowledge a fixed, previously reported unexplained mismatch (e.g. 2026-10-12:BTC/USD:plan_levels); repeatable. "
                        "Listed in the report; it explains nothing that follows it")
    p.add_argument("--ack-file", help="file of BAR:SYMBOL:KIND acknowledgements, one per line, '#' comments")
    p.add_argument("--format", choices=("text", "json"), default="text")
    p.add_argument("--json-out", help="also write the JSON report here: refuses an existing file unless --force, and any input "
                   "or path in the config's logging: block regardless of --force")
    p.add_argument("--force", action="store_true", help="allow --json-out to replace an existing file (never a bot or input file)")
    args = p.parse_args(argv)
    if args.relaxed_early_budget:
        args.budget_min_entries = RELAXED_BUDGET_MIN_ENTRIES
    if args.budget_min_entries < 0:
        p.error("--budget-min-entries must be >= 0")
    if args.days < 1 or args.min_bars < 1:
        p.error("--days and --min-bars must be >= 1")
    if not (0 <= args.budget <= 1):
        p.error("--budget must be within [0, 1]")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = build_report(args)
        protected = report.pop("_protected", [])
        if args.json_out:
            out = Path(args.json_out).resolve()
            inputs = [Path(v).resolve() for v in report["inputs"].values() if v]
            if out in inputs or any(parent in inputs for parent in out.parents):
                raise ParityInputError(f"--json-out {out} would overwrite an input; refusing")
            if out.name.startswith(".env") or out in protected or any(parent in protected for parent in out.parents):
                raise ParityInputError(f"--json-out {out} is a file or directory the bot writes (config logging: block, kill switch), the --ack-file, a .env or the repo config; refusing, --force does not apply")
            if out.exists() and not args.force:
                raise ParityInputError(f"--json-out {out} already exists; refusing to overwrite it without --force")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(_clean(report), indent=2, allow_nan=False) + "\n", encoding="utf-8")
    except (ParityInputError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    print(json.dumps(_clean(report), indent=2, allow_nan=False) if args.format == "json" else format_text(report))
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
