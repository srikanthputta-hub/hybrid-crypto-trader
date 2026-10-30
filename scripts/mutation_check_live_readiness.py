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
EX, PF, RESET = "src/exchange.py", "src/portfolio.py", "scripts/reset_portfolio.py"

# (id, description, file, old, new). `old` must occur exactly once in `file`.
MUTATIONS = [
    ("M01", "item 1: in-flight marker cleared before booking (right after the order returns)", BL,
     "        self._mark_placed(coid, response)\n        order, how = self._verify_response(symbol, \"buy\"",
     "        self._drop_entry(coid)\n        order, how = self._verify_response(symbol, \"buy\""),
    ("M02", "item 1: marker write failure swallowed (order placed without a record)", BL,
     "            raise LiveOrderStateError(\n                f\"{side} {symbol}: write-ahead order record could not",
     "            return coid\n            raise LiveOrderStateError(\n                f\"{side} {symbol}: write-ahead order record could not"),
    ("M03", "item 1: wrong clientOrderId derivation (side left out)", BL,
     'f"{plan_ref}|{side}|{int(attempt)}"', 'f"{plan_ref}|{int(attempt)}"'),
    ("M04", "item 1: attempt not consumed when an order is booked (id reused)", BL,
     "            s[\"attempts\"][key] = max(int(s[\"attempts\"].get(key, 0)), int(entry[\"attempt\"]) + 1)\n"
     "            if unverified:",
     "            if unverified:"),
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
     "                    store.update_open_amount(action.symbol, remaining)\n                except Exception as store_exc:",
     "                    pass\n                except Exception as store_exc:"),
    ("M10", "item 4: halted gate on planning removed", BOT,
     "    if _flag(c.broker, \"halted\"):", "    if False:"),
    ("M11", "item 2: halt alert never queued", BL,
     "        self._alert_last[key] = now\n        self._alerts.append({**alert, \"reminder\": last is not None})",
     "        self._alert_last[key] = now"),
    ("M12", "item 2: queued alerts never sent to Telegram", BOT,
     "        if bot_token and chat_id and notifier.send_telegram_message(bot_token, chat_id, text) is not True:",
     "        if False:"),
    ("M13", "item 5: filled-only sell response treated as a full close again", BL,
     "            ordered is None and remaining is None and order.get(\"filled\") is not None",
     "            False and ordered is None and remaining is None"),
    ("M14", "item 1: unresolved in-flight record at startup does not latch/persist the halt", BL,
     "            if entry[\"state\"] in _UNRESOLVED_STATES:\n                self._halt(\n                    f\"startup: in-flight",
     "            if False:\n                self._halt(\n                    f\"startup: in-flight"),
    ("M15", "item 1: sell allowed on a symbol whose order outcome is unknown", BL,
     "            if state in _SYMBOL_BLOCKING_STATES:", "            if False:"),
    ("M16", "item 2: trades.csv dedup removed (a reconciled fill booked twice)", BL,
     "        keys = {k for k in keys if k}\n", "        keys = set()\n"),
    ("M17", "item 1: corrupt order-state file silently treated as empty", BL,
     "            self._state_error = f\"{type(exc).__name__}: {exc}\"", "            self._state = _empty_order_state()"),
    ("M18", "item 2: exchange secrets not redacted from alerts", BOT,
     "            text = text.replace(secret, \"<redacted>\")", "            pass"),
    ("M19", "store: update_open_amount does not append to history.jsonl", STORE,
     "        record = replace(record, fill_amount=float(fill_amount))\n        self._commit({**self._current, symbol: record}, record)",
     "        record = replace(record, fill_amount=float(fill_amount))\n        self._persist_current({**self._current, symbol: record})\n"
     "        self._current = {**self._current, symbol: record}"),
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
    # review survivors (Architect X03/X05/X06/X08/X09/X11/X15)
    ("M25", "X03: not-found grace period 120 s -> 30 s", BL,
     "            if (now - created).total_seconds() < RECONCILE_NOT_FOUND_GRACE_SECONDS:",
     "            if (now - created).total_seconds() < RECONCILE_NOT_FOUND_GRACE_SECONDS / 4:"),
    ("M26", "X05: fsync of the order-state tmp file removed", BL,
     "        f.write(data)\n        f.flush()\n        os.fsync(f.fileno())", "        f.write(data)\n        f.flush()"),
    ("M27", "X06: reconcile only on the first cycle", BOT,
     "    reconcile = getattr(broker, \"reconcile\", None)\n    if not callable(reconcile):\n        return",
     "    reconcile = getattr(broker, \"reconcile\", None)\n    if not callable(reconcile) or getattr(broker, \"_once\", False):\n"
     "        return\n    try:\n        broker._once = True\n    except Exception:\n        pass"),
    ("M28", "X08: an `unusable` sell record no longer halts buys", BL,
     '_UNRESOLVED_STATES = ("intent", "placed", "unusable")', '_UNRESOLVED_STATES = ("intent", "placed")'),
    ("M29", "X09: plan_ref mismatch ignored when applying a reconcile result", BOT,
     "    matches = record is not None and _plan_ref(record) == result.plan_ref", "    matches = record is not None"),
    ("M30", "X11: _mutate_state adopts memory before the disk write", BL,
     "        if self._state_path is not None:\n            _atomic_write_json(self._state_path, new)\n        self._state = new",
     "        self._state = new\n        if self._state_path is not None:\n            _atomic_write_json(self._state_path, new)"),
    ("M31", "X15: listing lookback (clock-skew margin) removed", BL,
     "        booked = self._trade_log_fill(entry, {f\"coid:{coid}\", entry.get(\"exchange_order_id\") or \"\"})\n"
     "        if booked is not None:\n            return self._adopt_logged(entry, booked)\n        try:\n            order = self.exchange.find_order(\n"
     "                symbol, coid, exchange_order_id=entry.get(\"exchange_order_id\"),\n"
     "                since_ms=int((created - timedelta(seconds=RECONCILE_LOOKBACK_SECONDS)).timestamp() * 1000),",
     "        booked = self._trade_log_fill(entry, {f\"coid:{coid}\", entry.get(\"exchange_order_id\") or \"\"})\n"
     "        if booked is not None:\n            return self._adopt_logged(entry, booked)\n        try:\n            order = self.exchange.find_order(\n"
     "                symbol, coid, exchange_order_id=entry.get(\"exchange_order_id\"),\n"
     "                since_ms=int(created.timestamp() * 1000),"),
    # review fixes
    ("N01", "F1: in-memory ledger adopted BEFORE the trades.csv row", BL,
     "        self._log_live_trade(result, coid)\n        apply_to_memory()",
     "        apply_to_memory()\n        self._log_live_trade(result, coid)"),
    ("N02", "F2: marking a sell `unusable` does not consume its attempt", BL,
     "            self._set_entry(coid, consume_attempt=True, state=\"unusable\")",
     "            self._set_entry(coid, consume_attempt=False, state=\"unusable\")"),
    ("N03", "F2: _record_intent accepts an id that already has a record", BL,
     "        if coid in self._state[\"inflight\"]:\n            # Never re-send", "        if False:\n            # Never re-send"),
    ("N04", "F2: _drop_entry deletes any record (also one for a known order)", BL,
     "        if entry is None or entry[\"state\"] != \"intent\":", "        if entry is None:"),
    ("N05", "F2: an `unusable` record no longer blocks a same-symbol re-sell", BL,
     '_SYMBOL_BLOCKING_STATES = ("intent", "placed", "unusable")', '_SYMBOL_BLOCKING_STATES = ("intent", "placed")'),
    ("N06", "F3: plan store adopts memory before its disk write", STORE,
     "        self._persist_current(new_current)  # raises -> memory untouched\n        self._current = new_current",
     "        self._current = new_current\n        self._persist_current(new_current)  # raises -> memory untouched"),
    ("N07", "F3: settle without verifying the store transition on disk", BOT,
     "    if store is not None and expect is not None and not _persisted_as_expected(",
     "    if False and store is not None and expect is not None and not _persisted_as_expected("),
    ("N08", "F3: trades.csv append not fsync'd", PF,
     "            writer.writerow(row)\n            f.flush()\n            os.fsync(f.fileno())",
     "            writer.writerow(row)\n            f.flush()"),
    ("N09", "F3: live mode no longer quarantines a torn current.json (crash loop)", BOT,
     "        quarantine_corrupt=cfg.mode == \"live\",", "        quarantine_corrupt=False,"),
    ("N10", "F4: a booked record claims its halt reached disk", BL,
     "                s[\"inflight\"][coid][\"halt_durable\"] = self._halt_is_durable()",
     "                s[\"inflight\"][coid][\"halt_durable\"] = True"),
    ("N11", "F5: settle while the halt exists only in memory", BL,
     "        if not self._ensure_halt_durable():", "        if False:"),
    ("N12", "F5: a terminal zero-fill buy is booked instead of 'not executed'", BL,
     "        if how == \"not_executed\":\n            self._retire_unexecuted(coid, symbol, \"buy\"",
     "        if False:\n            self._retire_unexecuted(coid, symbol, \"buy\""),
    ("N13", "F6: DISCARD of a PENDING plan while its buy is unresolved", BOT,
     "        if _order_in_flight(broker, action.symbol):", "        if False:"),
    ("N14", "F7: an id-less fill gets no stable trades.csv key", BL,
     "    return f\"coid:{coid}\" if coid else None", "    return None"),
    ("N15", "F7: a paper-created trades.csv header is not extended (order_id dropped)", PF,
     "            if missing:\n                header = header + missing", "            if False:\n                header = header + missing"),
    ("N16", "F7: reset_portfolio.py resets while live state is unresolved", RESET,
     "    if unresolved and not a.include_live_state:", "    if False:"),
    ("N17", "F8a: Kraken lookup without the server-side clientOrderId filter", EX,
     "and self.exchange_id in _SERVER_SIDE_CLIENT_ID:", "and False:"),
    ("N18", "F8b: ack-only / unusable responses booked without an exchange lookup", BL,
     "        looked = self._lookup(symbol, coid,", "        looked = None and self._lookup(symbol, coid,"),
    ("N19", "F8c: dust sells sent to the exchange", BL,
     "        self._check_exit_size(symbol, sent, price_hint)", "        pass"),
    ("N20", "F8d: a precision-truncated full fill labelled partial", BL,
     "        reference = self._sent_reference(symbol, float(entry[\"requested_amount\"]))",
     "        reference = float(entry[\"requested_amount\"])"),
    ("N21", "F9: no single-instance lock", BL,
     "            _acquire_lock(logs / LOCK_NAME, self)", "            pass"),
    ("N22", "F10: a definitely-rejected exit only alerts on InsufficientFunds", BL,
     "                if side == \"sell\":\n                    self._exit_failed(",
     "                if side == \"sell\" and isinstance(exc, ccxt.InsufficientFunds):\n                    self._exit_failed("),
    ("N23", "F10: alerts never re-sent while the condition persists", BL,
     "        if last is not None and now - last < ALERT_REMINDER_SECONDS:", "        if last is not None:"),
    ("N24", "F10: a failed Telegram send is treated as delivered", BOT,
     "    if undelivered and callable(requeue):", "    if False:"),
    ("N25", "F10: alert length not capped", BOT,
     "    if len(text) > ALERT_MAX_CHARS:", "    if False:"),
    ("N26", "S1: any truthy `halted` (a MagicMock) blocks planning", BOT,
     "        return getattr(broker, name, False) is True", "        return bool(getattr(broker, name, False))"),
    ("N27", "fix round: halt not stashed before its marker write", BL,
     "        self._stash_pending_halt(reason)  # durable BEFORE the marker write", "        pass  #"),
    ("N28", "RO f6: free balance 0 -> the protective sell is never sent", BL,
     "            return held\n        if free < held * (1 - _SELL_SHORTFALL_TOLERANCE):",
     "            raise LiveOrderUnreconciled(\"no free balance\")\n        if free < held * (1 - _SELL_SHORTFALL_TOLERANCE):"),
]

TEST_FILES = [
    "tests/test_live_review_fixes.py", "tests/test_live_readiness.py", "tests/test_live_reconcile.py",
    "tests/test_trade_plan_store.py", "tests/test_exchange.py", "tests/test_paper_isolation.py",
    "tests/test_live_smoke.py", "tests/test_broker_live.py", "tests/test_bot.py", "tests/test_live_fuzz.py",
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
