"""mutation_check_live_readiness.py - issue #99 (docs/live_readiness_design.md): every deliberate
bug below must be caught by at least one test of the live-readiness suite.

Each mutation is applied to a fresh temporary copy of src/, tests/, config/, scripts/ and
.github/ (docs/, research/, strategy_pack_run/, web/, deploy/ are symlinked, read-only), then
`pytest -x` runs the live-readiness test files there. A mutation is KILLED if the suite fails.
The working tree is never modified. No network, no keys: every test uses fake exchanges.

    python scripts/mutation_check_live_readiness.py [--jobs 4] [--only M01 ...] [--json-out FILE]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
BL, BOT, STORE = "src/broker_live.py", "src/bot.py", "src/trade_plan_store.py"

# (id, description, file, old, new). `old` must occur exactly once in `file`.
MUTATIONS = [
    ("M01", "item 1: in-flight marker cleared before booking (right after the order returns)", BL,
     "        self._mark_placed(coid, order)\n        try:\n            filled_price, filled_amount, quote, ledger",
     "        self._drop_entry(coid)\n        try:\n            filled_price, filled_amount, quote, ledger"),
    ("M02", "item 1: marker write failure swallowed (order placed without a record)", BL,
     "            raise LiveOrderStateError(\n                f\"{side} {symbol}: write-ahead order record could not",
     "            return coid\n            raise LiveOrderStateError(\n                f\"{side} {symbol}: write-ahead order record could not"),
    ("M03", "item 1: wrong clientOrderId derivation (side left out)", BL,
     'f"{plan_ref}|{side}|{int(attempt)}"', 'f"{plan_ref}|{int(attempt)}"'),
    ("M04", "item 1: attempt not consumed when an order is booked (id reused)", BL,
     "            s[\"inflight\"][coid].update(state=\"booked\", fill=fill, remaining_amount=remaining_amount)\n"
     "            s[\"attempts\"][key] = max(int(s[\"attempts\"].get(key, 0)), int(entry[\"attempt\"]) + 1)\n",
     "            s[\"inflight\"][coid].update(state=\"booked\", fill=fill, remaining_amount=remaining_amount)\n"),
    ("M05", "item 2: reconcile treats a failed/ambiguous lookup as 'never placed'", BL,
     "            return self._unknown(entry, f\"exchange lookup failed ({type(exc).__name__})\")",
     "            order = None"),
    ("M06", "item 2: reconcile treats a still-open (unknown) order as filled", BL,
     "        if status == \"open\":\n            return self._unknown(entry, \"the order is still OPEN",
     "        if status == \"open\":\n            order = dict(order, status=\"closed\", filled=order.get(\"amount\"))\n"
     "            status = \"closed\"\n        if False:\n            return self._unknown(entry, \"the order is still OPEN"),
    ("M07", "item 2: 'not found' trusted inside the visibility grace period", BL,
     "            if (now - created).total_seconds() < RECONCILE_NOT_FOUND_GRACE_SECONDS:",
     "            if False:"),
    ("M08", "item 3: sell not capped at the free balance", BL,
     "        sent = self._capped_sell_amount(symbol, held)", "        sent = held"),
    ("M09", "item 4: partial-sell remainder not persisted to the plan store", BOT,
     "                store.update_open_amount(action.symbol, remaining)\n                _settle(broker, action.symbol)",
     "                pass"),
    ("M10", "item 4: halted gate on planning removed", BOT,
     "    if getattr(c.broker, \"halted\", False):", "    if False:"),
    ("M11", "item 2: halt alert never queued", BL,
     "        self._alert_keys.add(key)\n        self._alerts.append(alert)", "        self._alert_keys.add(key)"),
    ("M12", "item 2: queued alerts never sent to Telegram", BOT,
     "        if bot_token and chat_id:\n            notifier.send_telegram_message(bot_token, chat_id, text)",
     "        pass"),
    ("M13", "item 5: filled-only sell response treated as a full close again", BL,
     "            ordered is None and remaining is None and order.get(\"filled\") is not None",
     "            False and ordered is None and remaining is None"),
    ("M14", "item 1: unresolved in-flight record at startup does not latch/persist the halt", BL,
     "        unresolved = self._unresolved_entries()\n        if unresolved:",
     "        unresolved = {}\n        if unresolved:"),
    ("M15", "item 1: sell allowed on a symbol whose order outcome is unknown", BL,
     "            if state in _SYMBOL_BLOCKING_STATES:", "            if False:"),
    ("M16", "item 2: trades.csv dedup removed (a reconciled fill booked twice)", BL,
     "        booked = self._trade_log_fill(entry, order_id)", "        booked = None"),
    ("M17", "item 1: corrupt order-state file silently treated as empty", BL,
     "            self._state_error = f\"{type(exc).__name__}: {exc}\"", "            self._state = _empty_order_state()"),
    ("M18", "item 2: exchange secrets not redacted from alerts", BOT,
     "            text = text.replace(secret, \"<redacted>\")", "            pass"),
    ("M19", "store: update_open_amount does not append to history.jsonl", STORE,
     "        record.fill_amount = float(fill_amount)\n        self._persist_current()\n        self._append_history(record)",
     "        record.fill_amount = float(fill_amount)\n        self._persist_current()"),
    ("M20", "item 2: record cleared although the halt is not on disk (auto-resume after restart)", BL,
     "        if not self._halt_is_durable():\n            self._halt(\n                f\"{entry['side']} {entry['symbol']}: order",
     "        if False:\n            self._halt(\n                f\"{entry['side']} {entry['symbol']}: order"),
    ("M21", "item 4: base-asset fee ignored on a buy", BL,
     "                amount = filled_amount - base_fee", "                amount = filled_amount"),
    ("M22", "item 1: leftover booked record does not block its symbol", BL,
     "            if state == \"booked\" and coid not in self._session_booked:", "            if False:"),
    ("M23", "item 1: settle before the plan store moved", BOT,
     "        fill = broker.buy(action.symbol, quote_amount, action.price, **_order_kwargs(broker, record))\n",
     "        fill = broker.buy(action.symbol, quote_amount, action.price, **_order_kwargs(broker, record))\n"
     "        _settle(broker, action.symbol)\n"),
    ("M24", "item 2: run_cycle no longer reconciles before acting", BOT,
     "        _reconcile_live_orders(broker, store)\n        _send_broker_alerts(cfg, broker)\n        _run_cycle_body",
     "        _send_broker_alerts(cfg, broker)\n        _run_cycle_body"),
]

TEST_FILES = [
    "tests/test_live_readiness.py", "tests/test_live_reconcile.py", "tests/test_trade_plan_store.py",
    "tests/test_exchange.py", "tests/test_paper_isolation.py", "tests/test_live_smoke.py", "tests/test_broker_live.py",
]
COPY_DIRS = ("src", "tests", "config", "scripts", ".github")
LINK_DIRS = ("docs", "research", "strategy_pack_run", "web", "deploy")


def _make_copy(root: Path) -> None:
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for d in COPY_DIRS:
        shutil.copytree(_REPO / d, root / d, ignore=ignore)
    for d in LINK_DIRS:
        if (_REPO / d).exists():
            (root / d).symlink_to(_REPO / d)
    shutil.copy(_REPO / "pytest.ini", root / "pytest.ini")


def run_one(mut) -> dict:
    mid, desc, fname, old, new = mut
    with tempfile.TemporaryDirectory(prefix=f"mut_{mid}_") as d:
        root = Path(d)
        _make_copy(root)
        path = root / fname
        text = path.read_text()
        n = text.count(old)
        if n != 1:
            return dict(id=mid, desc=desc, status=f"PATTERN x{n}", killed=False)
        path.write_text(text.replace(old, new))
        t0 = time.time()
        r = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *TEST_FILES],
                           cwd=root, capture_output=True, text=True)
        # pytest's summary lines only (captured log lines also start with "ERROR").
        lines = [ln for ln in r.stdout.splitlines() if ln.startswith("FAILED tests/") or ln.startswith("ERROR tests/")]
        return dict(id=mid, desc=desc, killed=r.returncode != 0, seconds=round(time.time() - t0, 1),
                    caught_by=lines[0] if lines else r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--json-out")
    a = ap.parse_args(argv)
    muts = [m for m in MUTATIONS if not a.only or m[0] in a.only]
    with ThreadPoolExecutor(a.jobs) as ex:
        res = list(ex.map(run_one, muts))
    for r in res:
        print(f"{r['id']} {'KILLED' if r['killed'] else 'SURVIVED'} {r.get('seconds', '')}s  {r['desc']}  "
              f"<- {r.get('caught_by', r.get('status'))}")
    print(f"killed {sum(r['killed'] for r in res)} / {len(res)}")
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(res, indent=1))
    return 0 if all(r["killed"] for r in res) else 1


if __name__ == "__main__":
    sys.exit(main())
