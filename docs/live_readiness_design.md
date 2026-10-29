# Live-readiness fixes for issue #99: design note

Status: design for branch `claude/live-readiness-99`. It changes **no** config value, keeps `mode: paper`,
and leaves the two-switch gate in `LiveBroker.__init__` (`mode: live` + `LIVE_TRADING_CONFIRM=YES-I-UNDERSTAND-THE-RISK`)
exactly as it is. Everything below runs only inside `LiveBroker` (imported only when `mode: live`) or behind
`getattr(broker, ...)` checks in `src/bot.py` that a `PaperBroker` never satisfies. The paper bot's behaviour
does not change.

This work makes live trading **safer**. It does **not** make live trading **ready**. Section 9 lists what is still open.

## 0. Vocabulary and files

| File (all next to `logs/trades.csv`) | Written by | Content | Write discipline |
|---|---|---|---|
| `logs/live_broker_halt.json` (existing) | `LiveBroker._halt` | why new buys are refused | now atomic (tmp + fsync + `os.replace` + dir fsync); best effort, the in-memory latch always holds |
| `logs/live_order_state.json` (**new**) | `LiveBroker` only | `{"version": 1, "inflight": {clientOrderId: entry}, "attempts": {"<plan_ref>\|<side>": n}}` | atomic (tmp + fsync + `os.replace` + dir fsync); **a failed write before an order means no order** |
| `logs/trades.csv` (existing, format unchanged) | `LiveBroker._log_live_trade` | one row per booked fill | append (unchanged) |
| `logs/trade_plans_current.json` / `_history.jsonl` (existing) | `TradePlanStore` | plan lifecycle | new transition `update_open_amount` (current + history, like every other transition) |

* `plan_ref` = `"<symbol>|<PlanRecord.created_at>"`, passed by `bot.py` to the live broker (unique per plan:
  `created_at` is a microsecond ISO timestamp). Direct broker calls without a plan use `"<symbol>|adhoc"`.
* `attempt` = `attempts["<plan_ref>|<side>"]` (0 if absent). It is **consumed** (incremented) only once an order
  with that id is known to exist on the exchange (booked, or found by reconcile in any status). It is **not**
  consumed when the exchange definitely rejected the order or when reconcile proves the order was never placed,
  so a retry re-sends the **same** id.
* `clientOrderId` = `"hct" + sha256(f"{plan_ref}|{side}|{attempt}").hexdigest()[:15]`: 18 characters of
  `[a-z0-9]`, a pure function of (plan, side, attempt). It fits Kraken's free-text `cl_ord_id` (max 18 ASCII)
  and Binance's `newClientOrderId` (max 36, `[.A-Z:/a-z0-9_-]`). ccxt maps the unified `clientOrderId` param to
  both.

Honest limit: on most spot venues a client order id only has to be unique among **open** orders. A market
order fills or dies at once, so the exchange will usually **accept** a second order that has the same id. The id
makes an order **findable** by a reconcile. It is **not** an exchange-side duplicate guard. The guard against
duplicates is the write-ahead marker.

## 1. Item 1 (HIGH): write-ahead in-flight marker + clientOrderId

### Mechanism
Every live order (buy or sell) goes through one lifecycle. Each arrow is one atomic write of
`live_order_state.json`.

```
 (no entry) --write-ahead--> intent --create_market_order returned--> placed --ledger + trades.csv--> booked --store updated by bot.py--> (entry removed = cleared)
                               |                                         (best effort; an "intent" entry
                               |                                          is reconciled the same way)
                               +-- definite exchange rejection (InsufficientFunds, InvalidOrder, ...) -> entry removed, attempt NOT consumed
                               +-- unknown outcome (timeout, bare ExchangeError, crash, ...) -> entry stays, broker HALTED, reconcile later
```

1. **Write-ahead (`intent`)**: `{client_order_id, plan_ref, symbol, side, attempt, requested_amount, price_hint,
   quote_amount (buy), held_at_send + exit_reason (sell), state: "intent", created_at}` is persisted **before**
   `create_market_order`. If this write fails, the broker raises `LiveOrderStateError`, latches an in-memory halt,
   queues an alert, and **places no order**. This is fail closed, and it applies to protective sells too: an order
   whose existence could not be recorded is worse than a late exit.
2. **Order**: `ExchangeClient.create_market_order(symbol, side, amount, client_order_id=...)` sends
   `params={"clientOrderId": id}`. With `client_order_id=None` it makes byte-for-byte the old call.
3. **`placed`**: the exchange order id is recorded (best effort; a failure is logged, and the `intent` entry
   already lets a reconcile find the order through the clientOrderId).
4. **Booking**: unchanged ledger arithmetic, then the trades.csv row. Then the entry becomes **`booked`** with
   the fill `{price, base_amount, quote_amount, order_id, remaining}`, in the same write that consumes the
   attempt. If this write fails, the broker halts (in memory) and returns the fill normally.
5. **Clear (`settle`)**: `bot.py` applies the plan-store transition (`mark_open` / `mark_closed` /
   `update_open_amount`) and only **then** calls `broker.settle(symbol)`, which removes the entry. If settle
   fails, the broker halts. The `booked` entry then stays and the next reconcile pass finishes the job.

### Blocking rules (checked before any order, after the existing input guards)
* State file unreadable or corrupt (at startup or later): **every** order (buy and sell) is refused and an
  alert is sent. The file is never overwritten, so the evidence is kept.
* An `intent`/`placed` entry for any symbol: the broker is **halted**. Every buy is refused. A sell on **that**
  symbol is refused (it could double-sell). Sells on other symbols are allowed (protective exits keep running).
* A `booked` entry that this process did not just create (a leftover from before a restart, or one that survived
  a reconcile pass): every order on **that symbol** is refused until reconcile settles it. A `booked` entry this
  process created and has not yet handed to a reconcile pass does **not** block. The caller is still in the middle
  of its own flow, and that is how the existing direct-call tests use the broker.
* At startup, any `intent`/`placed` entry latches the halt and writes the halt marker (a crash happened mid-order;
  a human must look). A leftover `booked` entry does not halt: its outcome is known and reconcile repairs the store
  deterministically. It does block its symbol and sends an alert.

### Crash windows and what happens on restart

| Crash point | On disk after crash | Restart behaviour |
|---|---|---|
| before the intent write completes | nothing | normal; no order was sent |
| after intent, before the request left | `intent` | halted; reconcile: not found after grace -> entry cleared (attempt kept); halt stays for the human |
| request sent, response lost / SIGKILL / OOM | `intent` (or `placed`) | halted; reconcile finds the order by clientOrderId (or exchange id) and books it (row R5/R6) |
| after trades.csv row, before `booked` write | `placed` + trades.csv row | reconcile finds the order; its exchange id is **already in trades.csv** -> no second booking (R4) |
| after `booked`, before the store update | `booked` | reconcile: re-applies the store transition (buy: position re-added without touching cash, plan -> OPEN), then settles (R1-R3) |
| after the store update, before settle | `booked`, store already moved | reconcile sees the store already transitioned and only settles |
| halt-marker write failed, then restart | the in-flight entry is still on disk | restart sees the entry and halts again. This closes the "failed halt-marker write + restart = duplicate" hole from the issue. |

## 2. Item 2 (HIGH): alert on halt + reconcile

### Alerts
`LiveBroker` queues a structured alert every time a **new** halt reason latches, every time an order-state write
fails, every time a sell meets InsufficientFunds or a balance mismatch, and for every reconcile result (once per
clientOrderId and outcome, so an unresolved order does not spam every cycle). `bot.py` drains the queue
(`broker.pop_alerts()`) after the reconcile pass and after every audit event. It formats each alert and sends it
through the existing `notifier.send_telegram_message`. Every dynamic field goes through `notifier.escape_md`. The
whole text goes through `notifier.redact_secrets` with the bot token, plus a literal redaction of the configured
exchange key/secret/password. The text contains exception **class names** only, never exception text, URLs or
keys. Each alert states: symbol, side, amount, clientOrderId, what the bot did, and what the operator must check
on the exchange. Without Telegram configured, alerts still go to the log at CRITICAL.

### Reconcile (`LiveBroker.reconcile()`, called by `bot.py` at the top of every `run_cycle`)
It runs before any monitoring or planning, so before any order can be placed in that cycle. The first cycle after
a start is the "startup reconcile". With no entries it makes no exchange call at all. `bot.py` applies store
transitions for the results and then calls `broker.settle_order(client_order_id)`. Lookup
(`ExchangeClient.find_order`):

* exchange order id known -> `fetch_order(id, symbol)`. **Any** error, including OrderNotFound, means
  UNKNOWN: we know the order was placed.
* otherwise list the symbol's orders since `created_at - 10 min`: `fetch_orders` when the exchange has it,
  else `fetch_open_orders` + `fetch_closed_orders` (+ `fetch_canceled_orders` when available). Then match
  `clientOrderId`. Exactly one match -> that order. More than one -> UNKNOWN. None, but some order of the same
  side in the window carries **no** clientOrderId (unattributable) -> UNKNOWN. None and nothing unattributable
  -> NOT FOUND. Any exception -> UNKNOWN. (Kraken's ccxt `fetch_order(clientOrderId=...)` maps to the numeric
  `userref`, not `cl_ord_id`, so it can't be used for this. The listing parses `cl_ord_id` back into
  `clientOrderId`.)
* NOT FOUND is trusted only when the entry is older than `RECONCILE_NOT_FOUND_GRACE_SECONDS = 120`
  (exchange visibility lag). Younger entries count as UNKNOWN for this pass.

### Startup/cycle reconcile decision table (implemented in `LiveBroker._reconcile_entry` + `bot._apply_reconcile_result`)

"Tracked" = the symbol has a position in the broker's portfolio, which at a restart is rebuilt from the store's
OPEN records. In every row with an entry, the halt marker (if one was latched) is left for the human. **Reconcile
never resumes buys.** An entry is cleared only if the halt is durably on disk (or there is no halt), so a resolved
entry can never silently re-enable buys after a restart.

| # | Entry state | Exchange lookup | Side / tracked | Broker action | Store action (bot.py) | Entry | Halt |
|---|---|---|---|---|---|---|---|
| R0 | none | not queried | any | nothing | nothing | - | unchanged |
| R1 | booked | not queried | buy, tracked | nothing (ledger already has it) | PENDING(plan) -> `mark_open`; OPEN(plan) -> nothing | cleared | unchanged |
| R1b | booked | not queried | buy, not tracked | re-add position from the entry's fill, **no cash change, no trades.csv row** (trades.csv already has it) | PENDING(plan) -> `mark_open` | cleared | unchanged |
| R1c | booked | not queried | buy, store has no matching PENDING/OPEN plan | position kept | none possible | **kept** | **latched** + alert "position without a plan / stop-loss" |
| R2 | booked (full sell) | not queried | sell, tracked | drop the position, **no cash change** | OPEN(plan) -> `mark_closed(exit_reason)` | cleared | unchanged |
| R2b | booked (full sell) | not queried | sell, not tracked | nothing | no record / other plan -> nothing | cleared | unchanged |
| R3 | booked (partial sell) | not queried | sell, tracked | set position amount = recorded remainder | OPEN(plan) -> `update_open_amount(remainder)` | cleared | unchanged |
| R4 | intent/placed | FOUND, its exchange id already in trades.csv | any | treated as R1-R3 using the trades.csv row (no second booking) | as R1-R3 | cleared | unchanged |
| R5 | intent/placed | FOUND, terminal, filled = requested | buy (tracked or not) | book the fill (same ledger/fee code as a live buy), trades.csv row | PENDING(plan) -> `mark_open` | cleared | unchanged |
| R5s | intent/placed | FOUND, terminal, filled = requested | sell, tracked | book the sale (close; or partial if the send was capped below the tracked amount) | `mark_closed` / `update_open_amount` | cleared | unchanged |
| R5x | intent/placed | FOUND, filled > 0 | sell, **not tracked** | nothing (anomaly: sold something we do not track) | none | **kept** | latched + alert |
| R6 | intent/placed | FOUND, terminal, 0 < filled < requested | buy | book the filled part | `mark_open(filled)` | cleared | unchanged |
| R6s | intent/placed | FOUND, terminal, 0 < filled < requested | sell, tracked | book the filled part, keep the remainder | `update_open_amount(remainder)` | cleared | unchanged |
| R7 | intent/placed | FOUND, status `open` (still working) | any | nothing (**not** treated as filled) | none | kept | latched + alert (operator may cancel it) |
| R8 | intent/placed | FOUND, terminal (`canceled`/`expired`/`rejected`), filled = 0 | any | nothing | none | cleared, **attempt consumed** | unchanged |
| R9 | intent/placed | NOT FOUND, entry age >= grace | any | nothing | none | cleared, attempt **not** consumed (retry reuses the id) | unchanged |
| R10 | intent/placed | NOT FOUND, entry age < grace | any | nothing | none | kept | latched |
| R11 | intent/placed | error / unsupported / unusable fields / several matches / unattributable order | any | nothing (**no guessing**) | none | kept | latched + alert |
| R12 | state file unreadable | not queried | any | every order refused, file left untouched | none | - | latched + alert |

Once a human has checked the exchange and the ledger, they delete `logs/live_broker_halt.json` and restart. They
remove a stuck entry from `logs/live_order_state.json` by hand only after reconciling it on the exchange. They
delete only that entry, so the `attempts` counters survive.

## 3. Item 3 (HIGH): sells capped at the real free balance

Before a sell, `ExchangeClient.fetch_free_balance(base)` reads `fetch_balance()["free"][base]`. A missing asset,
a non-finite value or an error all count as **unknown**.

| free balance (`held` = tracked amount) | amount sent | booking | halt/alert |
|---|---|---|---|
| unknown | `held` (the old behaviour; the exchange is the arbiter) | as before | no |
| `>= held` | `held` (never more than tracked: other coins in the account are never sold) | as before | no |
| `held*(1-0.5%) <= free < held` | `free` | a full fill closes the whole position (the shortfall is base-asset fees / rounding) | no |
| `0 < free < held*(1-0.5%)` | `free` (protective) | a full fill is booked as **partial**: the remainder stays tracked | yes, before the order |
| `<= 0` | **nothing** | nothing booked, position kept | yes; `LiveOrderUnreconciled` |

A sell rejected with `InsufficientFunds` stays a definite rejection (nothing placed, nothing booked). It now also
**halts and alerts**, so the exit jam can't go unnoticed. Recommendation (docs and README only, no code
enforcement): run live in a **dedicated sub-account** that holds nothing but the bot's positions. With other
holdings of the same coin in the account, `free >= held` always holds, so the cap can't detect a phantom position.

## 4. Item 4 (MEDIUM): remainder persisted, live fees, halt gates planning

* **Partial-sell remainder**: new `TradePlanStore.update_open_amount(symbol, fill_amount)` (OPEN records only;
  finite > 0; updates `current.json` **and** appends to `history.jsonl`). `bot._execute_monitor_action` catches
  the partial (duck-typed `remaining_amount` attribute on `LivePartialFill`, so `bot.py` never imports
  `broker_live`), persists the remainder, settles, and re-raises (unchanged error path). A restart rebuilds the
  position from `fill_amount` = the remainder.
* **Fees**: when the order response reports fees (`fees` list, else `fee`): a buy fee in the **base** asset is
  subtracted from the booked amount; a buy fee in the **quote** asset is added to the cash debit (and to the
  trades.csv `quote_amount`, so `reconstruct_cash` replays it); a sell fee in the quote asset is subtracted from
  the proceeds; a sell fee in the base asset reduces a partial remainder. A fee in any other currency, or a
  malformed one, is ignored with a warning. It is not a reason to halt, and the balance cap absorbs the
  difference later. Responses without fee fields book exactly as before.
* **Halt gates planning**: `_evaluate_new_entry` returns "none" with the note `blocked: live broker halted ...`
  before scanning when `getattr(broker, "halted", False)`. No new PENDING plan, no cooldown or entry-cap slot
  spent. PENDING plans that already exist are still refused at `buy()` (no order) and are visible to the operator
  in the halt alert.

## 5. Item 5 (LOW): `filled` without `amount`/`remaining`

When a sell response reports `filled` but neither `amount` nor `remaining`, the reference is the
precision-truncated amount we sent (`ExchangeClient.amount_to_precision`, i.e. what ccxt itself sends), or the
raw amount when precision is unavailable. A `filled` below that reference (beyond float noise) is a **partial**:
the filled part is booked, the remainder kept, the broker halted (as for any partial). Precision dust (filled ==
truncated amount) is still a full close.

## 6. Failure modes (all fail closed)

| Failure | Result |
|---|---|
| intent write fails (disk full, permissions, read-only fs) | no order; in-memory halt; alert; `LiveOrderStateError` |
| state file corrupt / unreadable | every order refused; alert; file untouched |
| `placed` write fails | logged; the `intent` entry still reconciles through the clientOrderId |
| `booked` write fails | halt (in memory); fill returned; trades.csv dedup (R4) prevents a second booking |
| settle fails | halt; entry stays `booked`; the next reconcile settles it |
| reconcile lookup fails / ambiguous | entry kept, halted, alert, retried next cycle; never "assume filled" or "assume not placed" |
| halt marker write fails | in-memory latch; any `intent`/`placed` entry re-halts after a restart; entries are cleared only once the halt is on disk |
| Telegram down | `send_telegram_message` swallows it (existing); CRITICAL log line stays |

## 7. What stays exactly as it was

* The gate in `LiveBroker.__init__` (both switches, checked first, before any file is touched).
* `PaperBroker` and the paper path: no new call. `bot.py` passes order refs only to a broker that sets
  `TRACKS_ORDER_REFS`, and calls `reconcile`/`settle`/`pop_alerts` only through `getattr`.
* `src/broker_live.py` is still imported only inside `if cfg.mode == "live"` in `build_components`. A subprocess
  test proves that a paper build plus a cycle never imports it.
* trades.csv format; ledger arithmetic for responses without fee fields (the byte-for-byte parity tests against
  the frozen pre-hardening broker keep passing).

## 8. `.github/workflows/refresh-dashboard.yml`

That job runs `python -m src.bot --once` on `config/config.yaml`, which has `mode: paper`. `LIVE_TRADING_CONFIRM`
is read only by `Config.live_trading_confirmed`, which is used only in the `mode == "live"` branch of
`build_components`. The job therefore demonstrably does not need it. Removing it makes `mode: paper` no longer
the **only** switch protecting that job (a `mode: live` flip would then raise `LiveTradingDisabled` there). It
is removed in this branch. A test asserts that no workflow sets it.

## 9. Still NOT ready for live after this branch

See the branch report. In short: no live/testnet end-to-end run of these code paths; exchange-specific
`clientOrderId`/listing behaviour verified only against the installed ccxt source, not a real account; no
exchange-side stop orders (stop-loss is still the bot's own poll loop, so a dead process means no stop); a
reconcile only finds orders in the listing window; and the issue #87 risk-engine NaN fail-open is still open.
