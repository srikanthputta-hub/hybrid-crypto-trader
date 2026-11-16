# Demo-exchange instance (OKX Demo Trading): design note

Status: **design only** (branch `claude/demo-exchange-design`). No `src/`, `scripts/`, `config/`, `deploy/` or `web/`
file changes in this branch. Author role: Architect. Implementer: Execution Trader (Opus), strictly from this note,
in the PRs of section 10.

Date: 2026-11-16. Installed ccxt verified against: **4.5.90** (the version `requirements.txt` resolves to on the VM).
Line numbers below are for `main` at `26c90066`.

## 0. Decision summary

1. A **second, separate bot instance** on the same VM: own config file (`config/demo.yaml`), own logs directory
   (`logs/demo/`), own entry script (`scripts/run_demo.py`), own systemd unit, own env file outside the repo. The paper
   trial process, `config/config.yaml`, `logs/`, `scripts/run_live.py`, `deploy/hybrid-crypto-trader.service` and the
   weekly parity check are **not modified**.
2. A third config mode, **`mode: demo`**, that runs the existing `LiveBroker` (write-ahead record, reconcile, halt,
   flock, unverified-fill handling, `_check_buy_minimums`) against a **`DemoExchangeClient`** whose sandbox state is
   verified at construction, at startup against the venue, and again before every private call.
3. `LiveBroker.__init__` gets a **separate demo door**, not a looser live door: the two-switch live gate is kept
   byte-for-byte; `mode: demo` is admitted only with a sandbox-verified `DemoExchangeClient` and only while
   `LIVE_TRADING_CONFIRM` is **not** set. `mode: live` refuses a demo client.
4. Demo keys come only from `DEMO_API_KEY` / `DEMO_API_SECRET` / `DEMO_API_PASSWORD`. In demo mode the
   `EXCHANGE_API_*` accessors raise. In any other mode the `DEMO_*` accessors raise, and `mode: live` refuses to start
   while any `DEMO_*` variable is set.
5. **Data and execution both on OKX** (`*/USDT`, 1d = `1Dutc`, same strategy parameters as the trial). No
   Kraken-signals/OKX-execution hybrid.
6. Sizing uses the **bot's own ledger** (`risk.starting_cash_usd: 10000` + realized P&L from `logs/demo/trades.csv`),
   never the demo account balance. That is already how the code works. A startup check refuses to start unless the
   demo account holds at least that much free USDT.
7. Dashboard: the demo exports to `web/data-demo.json` (one new tab in `web/instances.json`). The demo process **never
   runs git or wrangler**. The file goes live through the paper instance's existing direct deploy, which uploads all of
   `./web`.
8. Smallest safe first PR: the verified demo client, the config accessors and a **read-only** probe script. Nothing
   can place an order until PR 2, and nothing runs on the VM until PR 3 plus the user's own setup.

This does **not** make live trading ready and does not satisfy any condition in `docs/live_readiness_design.md` §11
(section 9).

## 1. Where this fits in the architecture (new stage or not)

This is **not a new pipeline stage**. Stages 1-5 and the bar gate are unchanged. `mode: demo` is a third way to
select the broker that executes stage 5's decisions, plus infrastructure (an exchange client, an entry script, a
systemd unit). The live reconcile step at the top of the cycle (`README.md` architecture, `src/bot.py:1157`) runs in
demo mode exactly as in live mode, because it is the same `LiveBroker`.

Rejected alternatives:

* **A new thin `DemoBroker`.** It would duplicate order logic, and the duplicate would be what gets tested, not
  `LiveBroker`. The point of a real demo API is to exercise the #99 code paths (ack-only verification, exchange
  lookups, fees, precision, minimums, reconcile after a crash, halt and resume) against a real matching engine and
  real API errors. A thin broker proves nothing about the code that would one day trade real money.
* **A subclass that calls `LiveBroker.__init__(..., "live", True)`.** That hard-codes passing the live gate. It is a
  bypass of the two-switch gate, whatever the subclass checks first. Rejected outright.
* **Running demo inside the paper process** (a second broker in one loop). That breaks "paper trial untouched" and
  couples the two failure domains. Rejected.

## 2. What the code does today (the facts this design rests on)

| Fact | Where |
|---|---|
| `Config.mode` accepts only `paper` / `live` | `src/config.py:199-204` |
| `load_config` always loads `PROJECT_ROOT/.env` before reading the yaml | `src/config.py:329-332` |
| Real exchange secrets come from `EXCHANGE_API_KEY/SECRET/PASSWORD`; Telegram from `TELEGRAM_*`; heartbeat from `HEALTHCHECK_PING_URL` | `src/config.py:292-322` |
| `ExchangeClient(testnet=True)` only **warns** when sandbox is unsupported (fail-open) | `src/exchange.py:36-43` |
| The bot builds one `ExchangeClient` from `EXCHANGE_API_*` and `exchange.testnet` | `src/bot.py:84-92` |
| Plan-store quarantine is live-only: `quarantine_corrupt=cfg.mode == "live"` | `src/bot.py:137` |
| Broker choice: `if cfg.mode == "live": LiveBroker(...)` else `PaperBroker` (so `demo` would silently be paper) | `src/bot.py:161-176` |
| Cash = `reconstruct_cash(trades.csv, risk.starting_cash_usd)`; the exchange balance is never read for sizing | `src/bot.py:153-159`, `:1035`, `:1206` |
| Alert redaction covers `exchange_api_*`, `anthropic_api_key` and tolerates accessor errors | `src/bot.py:740-755` |
| `bot.main` already has `--config` | `src/bot.py:1340-1348` |
| Every log path (trades, plans, audit, snapshot, bar decisions, closed-bar archive) comes from `logging.*`; the LiveBroker halt marker, order state and flock sit next to `trades.csv` | `src/bot.py:130-177`, `:1187`; `src/broker_live.py:447-460` |
| Two-switch gate | `src/broker_live.py:436-445` |
| clientOrderId = `"hct"` + 15 hex chars (18 chars of `[a-z0-9]`) | `src/broker_live.py:149-155` |
| `DuplicateOrderId` and bare `ExchangeError` are ambiguous (halt); any other `ExchangeError` subclass is a definite rejection | `src/broker_live.py:368-379` |
| Ack-only response -> lookup (`find_order` by exchange id) with bounded re-polls | `src/broker_live.py:990-1072` |
| `find_order`: Kraken uses a server-side `cl_ord_id` filter; others list orders with `limit=1000` | `src/exchange.py:106-171`, `:292-294` |
| Trade-log rows are written with `"mode": "live"` (no consumer reads the column) | `src/broker_live.py:1288`, `:1987`; `src/portfolio.py` `reconstruct_cash` ignores it |
| `scripts/run_live.py` and `scripts/run_paper.py` refuse any mode except `paper` | `scripts/run_live.py:252-257`, `scripts/run_paper.py:20-24` |
| `run_live.py` has no multi-instance support: no `--config`; it exports with no args, `git add web/data.json`, push state in `logs/dashboard_push_state.json`, deploy state in `logs/dashboard_deploy_state.json`, pair-minimums check without `--config`, heartbeat from `HEALTHCHECK_PING_URL` | `scripts/run_live.py:77-125`, `:192`, `:289`; `scripts/dashboard_push.py:33`; `scripts/dashboard_deploy.py:61` |
| The exporter **does** support a second instance: `--config --out --instance-id --instance-name`; the page has an instance switcher (`web/instances.json`) | `scripts/export_dashboard_data.py:1320-1336`, `:1297-1304`; `docs/dashboard/README.md` "Adding the intraday instance later" |
| The exporter writes its output non-atomically (`open(out, "w")` + `json.dump`) | `scripts/export_dashboard_data.py:1431-1434` |
| `.gitignore` covers only top-level `logs/*.json|*.jsonl|*.csv|*.tmp` (not `logs/demo/`) | `.gitignore` |
| Health check: exactly one process matching `scripts/run_live.py`; reads `config/config.yaml` and `logs/live_broker_halt.json` | `scripts/vm_health_check.sh:29`, `:39-44` |
| Invariants: live gate tests 1-3, `config/config.yaml` mode is paper | `scripts/validate_invariants.py:36-79`, `:128-148` |

So the intraday work added multi-instance support to the **exporter and the page only**. The host loop is
single-instance. This design reuses the exporter and the page, and adds a separate host loop for the demo rather than
generalising `run_live.py`. That keeps the trial's process byte-for-byte unchanged.

### 2.1 ccxt 4.5.90 facts for OKX (read from the installed source)

* `okx.set_sandbox_mode(True)` keeps the **same host** (`www.okx.com`; `myokx` = `eea.okx.com`), sets
  `isSandboxModeEnabled = True`, `options['sandboxMode'] = True` and `headers['x-simulated-trading'] = '1'`.
  `Exchange.prepare_request_headers` merges `self.headers` into **every** request, public and private. **The header
  is the only thing that separates demo from real money**, so it is what the verification checks.
* `okx.timeframes['1d'] == '1D'`. `fetch_ohlcv` appends `utc` when `options['fetchOHLCV']['timezone'] == 'UTC'` (the
  default) and the timeframe is at least 6h. So `1d` requests `bar=1Dutc`: bars open at 00:00 UTC, the same alignment
  as Kraken. With any other timezone option, `1D` is UTC+8-aligned (bars close at 16:00 UTC). `Candles` returns at
  most 300 bars per request.
* `create_order_request`, spot market buy: `tgtCcy` defaults to `base_ccy`, so `sz` is the **base** amount (what
  `LiveBroker` sends). `createMarketBuyOrderRequiresPrice` only matters for `tgtCcy=quote_ccy`, which is not used.
  `tdMode` is `'cash'` (no margin, no borrowing) unless margin params are passed (`LiveBroker` passes none).
  `clientOrderId` -> `clOrdId` verbatim (the `brokerId` + uuid fallback applies only when no id is given).
* `fetch_order(id, symbol, params)` accepts `params['clientOrderId']` -> `clOrdId` (server-side lookup by our id).
  Error `51603` -> `OrderNotFound`.
* `has['fetchOrders'] = False`. `fetch_open_orders` / `fetch_closed_orders` cap `limit` at 100, but
  `fetch_canceled_orders` passes `limit` through **uncapped**, so `find_order`'s generic listing path
  (`_LISTING_LIMIT = 1000`) would send `limit=1000` and almost certainly get `51000 BadRequest`. Every reconcile would
  then be `unknown`, a permanent halt.
* Error map: `51016` (duplicated `clOrdId`) -> `InvalidOrder`, which `LiveBroker` treats as a **definite rejection**.
  But a duplicate id means an order with that id may **exist**, so it must be ambiguous. `50101` (API key does not
  match the current environment) -> `AuthenticationError`. `51008` -> `InsufficientFunds`.
* Fees: OKX reports `fee` as a negative string (a charge); ccxt returns a positive `cost` in `feeCcy` (base asset on
  spot buys, quote on sells). `_order_fees` (`src/broker_live.py:2052`) already handles base/quote fees.
* Balance: the trading account (`free` = `availEq` or `availBal`); zero balances are omitted (-> `None`, which the
  broker already treats as "unknown", `src/broker_live.py:1827`).
* Market limits: `limits.amount.min` = `minSz`, precision from `lotSz`; usually no `cost.min`.
  `_check_buy_minimums` handles a `None` cost minimum.

## 3. Broker: `LiveBroker` in `mode: demo`

### 3.1 The gate (exact shape)

In `src/broker_live.py`, `LiveBroker.__init__` becomes:

```python
if mode == "demo":
    _require_demo_gate(exchange, live_trading_confirmed)          # new; raises LiveTradingDisabled
else:
    # ---- the two-switch live gate: these two statements stay byte-for-byte as they are today ----
    if mode == "live" and not live_trading_confirmed:
        raise LiveTradingDisabled(...)                             # unchanged text
    if mode != "live":
        raise LiveTradingDisabled(...)                             # unchanged text
    if _is_demo_client(exchange):                                  # new, refusal only
        raise LiveTradingDisabled("mode: live was given a demo/sandbox exchange client; refusing")
self.mode = mode                                                   # new; used for trades.csv / alert titles
```

```python
def _require_demo_gate(exchange, live_trading_confirmed: bool) -> None:
    from src.exchange_demo import DemoExchangeClient, SandboxNotVerified   # lazy, demo only
    if live_trading_confirmed:
        raise LiveTradingDisabled(
            "mode: demo refuses to run while LIVE_TRADING_CONFIRM is set: the demo process must never "
            "hold the live switch")
    if not isinstance(exchange, DemoExchangeClient):
        raise LiveTradingDisabled("mode: demo requires a DemoExchangeClient (sandbox-verified); refusing")
    try:
        exchange.assert_sandbox()
    except SandboxNotVerified as exc:
        raise LiveTradingDisabled(f"mode: demo but the exchange client is not verifiably in sandbox mode: {exc}") from exc

def _is_demo_client(exchange) -> bool:
    if exchange is None:
        return False
    from src.exchange_demo import DemoExchangeClient          # importing a class reads no env, no config
    return isinstance(exchange, DemoExchangeClient)
```

Properties:

* `mode: live` behaves exactly as today, plus one refusal (a demo client). `validate_invariants` tests 1-3
  (`exchange=None`) are unaffected.
* `mode: paper` (or any other string) still raises in the `else` branch, with today's message.
* `mode: demo` can never be satisfied by a plain `ExchangeClient`, by `None`, by a client whose sandbox flags or
  header were removed, or in a process that has the live switch set.
* Everything after the gate (lock, halt latch, order state) is unchanged. The flock is
  `logs/demo/.live_broker.lock`, distinct from any live/paper path.

Other `broker_live.py` changes (all small, all demo-neutral for live):

* `_log_live_trade` (`:1987`) and the unverified-buy `adjust` row (`:1288`) write `"mode": self.mode` instead of the
  literal `"live"`. Nothing reads the column; this keeps a demo `trades.csv` from ever looking like a live one.
* Alert titles get a `[DEMO] ` prefix when `self.mode == "demo"` (in `_queue_alert`; one line). Telegram messages
  already carry `notifier.label`, but the CRITICAL log lines do not.

### 3.2 Every place that branches on `mode` (grep of `src/` and `scripts/`)

| Location | Today | Demo change |
|---|---|---|
| `src/config.py:199-204` `Config.mode` | `paper`/`live` | accept `demo` |
| `src/config.py:292-322` secret accessors | env, mode-blind | mode-aware (section 4) |
| `src/bot.py:84-92` exchange construction | `ExchangeClient(EXCHANGE_API_*)` | `mode == "demo"`: use the passed-in `exchange` or `build_demo_exchange(cfg)`; `live`/`paper`: unchanged, plus `live` refuses a `demo:` block or any `DEMO_*` env var |
| `src/bot.py:137` plan-store quarantine | `cfg.mode == "live"` | `cfg.mode in ("live", "demo")` (same recovery behaviour as live) |
| `src/bot.py:161` broker choice | `== "live"` | `in ("live", "demo")`; `LiveBroker(exchange, portfolio, cfg.mode, cfg.live_trading_confirmed)` |
| `src/bot.py:740` `_SECRET_ATTRS` | exchange + anthropic | add `demo_api_key`, `demo_api_secret`, `demo_api_password` (the loop already tolerates accessors that raise) |
| `src/bot.py:1217/1286/1307` audit `"mode"` | `cfg.mode` | none (records `demo`) |
| `src/bot.py:1340` `main()` | any mode | refuse `mode: demo` (`SystemExit`: "use scripts/run_demo.py"); one entry point for demo |
| `src/broker_live.py:436-445` gate | live gate | section 3.1 |
| `src/broker_live.py:1288`, `:1987` trade rows | `"live"` | `self.mode` |
| `scripts/run_live.py:253`, `scripts/run_paper.py:20` | refuse != paper | **unchanged** (they already refuse demo; no edit) |
| `scripts/export_dashboard_data.py:1301`, `:1389` | `cfg.mode` | none (shows `mode=demo`) |
| `scripts/validate_invariants.py:36-79`, `:128-148` | live gate, config.yaml paper | add checks only (section 8) |
| `scripts/mutation_check_live_readiness.py:146` | quarantine mutation string | update its `old` string to the new expression (it must still occur exactly once) |

`build_components` gets one **additive** keyword: `build_components(cfg, exchange=None)`. `None` keeps today's
behaviour exactly; the 6-tuple return is unchanged. In demo mode a passed exchange must be a `DemoExchangeClient`
(re-asserted); in `paper`/`live` mode a passed `DemoExchangeClient` is refused.

## 4. Credentials and environment separation (both directions)

### 4.1 Config accessors (`src/config.py`)

| Accessor | `paper` | `live` | `demo` |
|---|---|---|---|
| `exchange_api_key/secret/password` | `EXCHANGE_API_*` (today) | `EXCHANGE_API_*` (today) | **raises** `RuntimeError("demo mode never reads EXCHANGE_API_*")` |
| `demo_api_key/secret/password` (new) | raises | raises | `DEMO_API_KEY` / `DEMO_API_SECRET` / `DEMO_API_PASSWORD` |
| `telegram_bot_token` / `telegram_chat_id` | `TELEGRAM_*` (today) | today | `DEMO_TELEGRAM_BOT_TOKEN` / `DEMO_TELEGRAM_CHAT_ID` (unset = no Telegram) |
| `healthcheck_ping_url` | `HEALTHCHECK_PING_URL` | today | `DEMO_HEALTHCHECK_PING_URL` (unset = no ping) |
| `live_trading_confirmed` | today | today | today (the demo gate refuses when it is True) |
| `demo` (new, the `demo:` block) | raises if a `demo:` block is present (`ValueError`) | same | returns the validated block |

Why the heartbeat must be separate: if both processes pinged the same healthchecks.io URL, a live demo process would
hide a dead paper process. Why Telegram defaults to separate/off: the user follows paper alerts by hand on Kraken
(`config/config.yaml` symbols comment). An OKX-USDT demo BUY in the same chat could be mistaken for a trial signal and
traded with real money.

`load_config` gets an **additive** keyword: `load_config(config_path=None, *, env_file=DEFAULT)`. `DEFAULT` loads
`PROJECT_ROOT/.env` (today). `scripts/run_demo.py` passes `env_file=None`, so the demo process **never reads the
paper's `.env`**: its environment is exactly the systemd `EnvironmentFile` (section 7).

### 4.2 Startup tripwires

* `scripts/run_demo.py` refuses to start (exit code 78, see section 7) if `LIVE_TRADING_CONFIRM` equals the
  confirmation phrase, or if any of `EXCHANGE_API_KEY` / `EXCHANGE_API_SECRET` / `EXCHANGE_API_PASSWORD` is set and
  non-empty in its environment. Because the demo process does not load `.env`, this does not conflict with whatever the
  paper `.env` contains.
* `build_components` in `mode: live` refuses to start if any environment variable starting with `DEMO_` is set, or the
  config has a `demo:` block. **Paper mode gets no such tripwire**: a refusal there could stop the trial at its next
  restart. Paper never reads `DEMO_*`, and the health check warns if `DEMO_*` appears in `.env`.
* Never logged: key values. The startup log says only `demo credentials: present (3/3)`.

## 5. The demo exchange client (`src/exchange_demo.py`, new module)

A new module, so `src/exchange.py`'s paper path is untouched and paper never imports it (extend
`tests/test_paper_isolation.py` to assert `src.exchange_demo` is not imported in paper).

```python
class SandboxNotVerified(RuntimeError): ...

class DemoExchangeClient(ExchangeClient):
    """Always sandboxed. No `testnet` parameter: there is no way to construct it un-sandboxed."""
    def __init__(self, exchange_id, api_key, api_secret, api_password): ...
        # 1. exchange_id must be in SANDBOX_VERIFIERS, else SandboxNotVerified (no fallback, no warning-and-continue)
        # 2. build the ccxt client with the keys; call client.set_sandbox_mode(True); NotSupported -> SandboxNotVerified
        # 3. apply venue adaptations (section 5.3)
        # 4. self.assert_sandbox()
    def assert_sandbox(self) -> None:          # re-derived from live ccxt object state every call, never a cached flag
    # Overrides that call self.assert_sandbox() FIRST, then super():
    #   create_market_order, find_order, fetch_free_balance, fetch_ohlcv_df, fetch_ticker_price, load_markets
```

`SANDBOX_VERIFIERS: dict[str, Callable[[ccxt.Exchange], None]]`. Phase 1 registers **only** `okx` and `myokx`
(OKX EEA, `eea.okx.com`; see open question 1). An id that is not registered is refused, including `kraken` (no sandbox)
and, for now, `bybit` / `binance`. The OKX verifier raises `SandboxNotVerified` unless **all** of these hold:

1. `isinstance(client, ccxt.okx)` and `client.id in {"okx", "myokx"}`;
2. `client.isSandboxModeEnabled is True` and `client.options.get("sandboxMode") is True`;
3. `client.headers.get("x-simulated-trading") == "1"`;
4. `client.prepare_request_headers({}).get("x-simulated-trading") == "1"` (the merged headers that are actually sent,
   which catches a session-level override);
5. `client.hostname in {"www.okx.com", "eea.okx.com"}` and `client.urls["api"]["rest"] == "https://{hostname}"`;
6. `(client.options.get("fetchOHLCV") or {}).get("timezone", "UTC") == "UTC"` (keeps `1d` = `1Dutc`).

Venue swap later (a config change plus a verifier PR with its own tests; until then refused):

* Bybit: `set_sandbox_mode(True)` (`api-testnet.bybit.com`, not real prices) or `enable_demo_trading(True)`
  (`api-demo.bybit.com`, real-price demo; the better match). Verifier: every URL in `client.urls["api"]` has a host
  starting with `api-demo.` (or `api-testnet.`), and none equals a production host.
* Binance spot testnet: every host in `client.urls["api"]` must be in an explicit testnet allowlist
  (`testnet.binance.vision`, ...). Testnet prices are not real-market prices.

### 5.1 Startup checks (`demo_startup_checks(exchange, cfg) -> list[str]`, called by `run_demo.py`)

In order; any failure is a refusal to start (exit 78), never a warning:

1. `exchange.assert_sandbox()`.
2. All three `DEMO_API_*` values are present.
3. **Authenticated read:** `client.fetch_balance()` succeeds. With the demo header present, OKX rejects anything but a
   demo-environment key (`50101`, `AuthenticationError`). So success proves both that the request went to the demo
   environment and that the key is a demo key, which cannot move real money even if a later bug dropped the header.
   On `AuthenticationError` the message says "the key was not created under Demo Trading, or the passphrase is wrong".
4. Free `demo.quote_currency` (USDT) is at least `risk.starting_cash_usd` (section 6).
5. `market.history_bars + 1 <= 300` (OKX `Candles` cap; a bigger request would silently return fewer bars).
6. `load_markets()`, then `filter_listed_symbols(cfg.symbols, markets)` (pure function, section 6.2). Refuse if none
   remain.
7. Log one INFO summary (no secrets): venue, sandbox verified, free USDT, symbols kept/dropped, and the **number** of
   other non-zero assets on the demo account (section 9, preloaded coins).

### 5.2 Market data: OKX end-to-end (recommended) vs Kraken signals + OKX execution

**Recommendation: OKX end-to-end.** Symbols `X/USDT`, `market.timeframe: "1d"`, every strategy/risk parameter equal
to `config/config.yaml`.

* The hybrid would compute entry zone, stop and invalidation on Kraken `X/USD` prices while the position and fills
  live on OKX `X/USDT`. The monitor would trigger on one feed and execute on another. `bot.run_cycle` has one
  `exchange` object for both; splitting it is an invasive `bot.py` contract change to buy a mismatch. Rejected.
* The demo's purpose is **execution evidence** (does `LiveBroker` behave against a real API?), not strategy evidence.
  The paper trial is the strategy evidence. Demo P&L is therefore never pooled with trial results, never judged
  against the kill rule, and never cited as performance.
* Bar alignment: with the timezone check above, `1d` -> `1Dutc` (00:00 UTC). `ExchangeClient.fetch_ohlcv_df` drops
  the forming bar by `open + parse_timeframe("1d")` (`src/exchange.py:47-74`), and `MarketDataCache` computes
  `next_due_ms = last_open + 2 * 1d` (`src/market_data.py:190`). Both are timezone-agnostic arithmetic on the bar
  open, so 00:00 UTC bars give the same once-a-day cadence as Kraken (decisions ~00:05 UTC). If someone set the
  timezone option to UTC+8, those two would still be internally consistent but every decision would move to 16:05 UTC;
  verifier item 6 prevents that.
* Demo endpoint and market data: there is no separate demo host. The ccxt sandbox header goes on public requests too
  (`prepare_request_headers`), so all market data the demo bot sees comes **from the demo environment** by
  construction, consistent with where it trades. I could not reach okx.com from this sandbox to confirm that every
  public endpoint (instruments, candles, tickers) answers with the header for every pair. The read-only probe (PR 1)
  checks this on the VM, and an empty or stale candle series for a symbol drops that symbol. **Fallback, only if the
  probe shows demo candles are unusable:** a keyless, non-sandboxed `ccxt.okx` used by `DemoExchangeClient` for
  `fetch_ohlcv_df` only (no credentials, so it cannot place or read orders; all private calls stay on the verified
  sandboxed client). That fallback is a separate, reviewed change, not part of PRs 1-3.

### 5.3 OKX adaptations `LiveBroker` needs

| # | Topic | Today | Needed | Where | Venue-generic? |
|---|---|---|---|---|---|
| A1 | Market buy semantics | sends base amount, no params but `clientOrderId` | **none**: ccxt okx defaults `tgtCcy=base_ccy`; never set `tgtCcy`/`createMarketBuyOrderRequiresPrice` | test pins the request shape | okx-specific pin |
| A2 | Spot, no margin | n/a | **none**: `tdMode='cash'`; test pins that no margin params are sent | test | okx-specific pin |
| A3 | clientOrderId format | 18 chars `hct[0-9a-f]{15}` | **none**: OKX `clOrdId` allows up to 32 alphanumerics; Bybit `orderLinkId` / Binance `newClientOrderId` up to 36 | test pins regex and length <= 32 | generic |
| A4 | Lookup by client id | generic listing with `limit=1000` breaks on okx (`fetch_canceled_orders` uncapped) | `find_order`: for `exchange_id in {"okx","myokx"}` and no exchange order id: `client.fetch_order(None, symbol, {"clientOrderId": coid})`. `OrderNotFound` -> `None` (positive absence: OKX answered "no such order"); any other exception propagates; a returned order whose `clientOrderId != coid` or `symbol != symbol` -> `OrderLookupAmbiguous`. With an exchange id: unchanged (`fetch_order(id, symbol)`). Kraken branch unchanged | `src/exchange.py` (new `_SERVER_SIDE_FETCH_BY_CLIENT_ID = {"okx","myokx"}` branch, before the Kraken one) | okx-specific; benefits a future live OKX equally |
| A5 | Duplicate client id | ccxt maps `51016` -> `InvalidOrder` = definite rejection | map `51016` -> `ccxt.DuplicateOrderId` (already in `_AMBIGUOUS_EXCHANGE_ERRORS`) by setting `client.exceptions["exact"]["51016"]` for okx clients | `DemoExchangeClient.__init__` (and `ExchangeClient.__init__` for okx, so a future live OKX gets it too) | okx-specific |
| A6 | Ack-only create response | handled (`_verify_response`) | **none**: OKX create returns only `ordId`/`clOrdId`, which takes the existing verify-by-lookup path with re-polls (the main path the demo will exercise) | - | generic |
| A7 | Fees | `_order_fees` books base fee on buys, quote fee on sells | **none**: ccxt returns positive `cost` + `feeCcy`; a negative (rebate) fee is ignored with a warning, as today | - | generic |
| A8 | Precision / minimums | `_check_buy_minimums` | **none**: `amount.min` = `minSz`, `cost.min` usually `None` | test with an okx-shaped market | generic |
| A9 | Balance | `fetch_free_balance` | **none**: trading-account `free`; omitted zero balance -> `None` -> "unknown" (sell the tracked amount) | - | generic |
| A10 | Order retention | reconcile grace 120 s, clears only `intent` records | **none**: OKX drops canceled orders with no fill from order details after about 2 h; such an order executed nothing, so "never placed" is the correct outcome. Filled orders stay findable for months | documented here | okx-specific note |
| A11 | Account mode | n/a | runbook: the demo account in **Spot mode** (no margin, no auto-borrow) | section 7 | okx-specific |

## 6. Capital: virtual cap vs the demo balance

* The bot never sizes from the exchange balance. Cash is `reconstruct_cash(logs/demo/trades.csv,
  risk.starting_cash_usd)` (`src/bot.py:153`); the risk engine gets `cash_available = c.portfolio.cash` (`:1035`) and
  `portfolio_equity = portfolio.equity(current_prices)` (`:1206`, `:1059`). With `risk.starting_cash_usd: 10000` in
  `config/demo.yaml`, a demo account showing 100,000 USDT or 1 BTC changes nothing about sizing. This is the §11
  item 11 "hard capital cap" analogue, and it already holds. A test pins it (section 8).
* Startup check 5.1-4 ensures the demo account can actually pay for what the ledger thinks it has: free USDT must be
  at least `starting_cash_usd`. Otherwise buys would hit `InsufficientFunds` (a definite rejection: no halt, the plan
  stays PENDING and is retried every cycle), which is noise, not evidence.
* §11 item 3 ("dedicated sub-account") analogue: OKX demo accounts come **pre-funded with several coins**. The
  free-balance sell cap (`_capped_sell_amount`, `src/broker_live.py:1843`) can only detect a phantom or stale position
  for a coin the account does not otherwise hold. For a pre-held coin it is blind, exactly as §11 item 3 warns for
  live. The startup log reports how many non-zero non-USDT assets exist; the runbook asks the user to note them. This
  limitation is accepted for demo and listed in section 9.

### 6.1 `config/demo.yaml` (PR 3)

A copy of `config/config.yaml` with **only** these differences (a test asserts every other strategy/risk key is
equal to `config/config.yaml`, so the demo can't drift into a different strategy unnoticed):

```yaml
mode: demo
exchange:
  id: okx                 # or myokx (open question 1); must be in SANDBOX_VERIFIERS
  testnet: true           # required true in demo mode (startup refuses false); the client sandboxes regardless
demo:
  quote_currency: "USDT"  # the currency the virtual capital is held in on the demo account (startup check 4)
market:
  symbols: [BTC/USDT, ETH/USDT, ...]   # the 16 bases on /USDT, minus any the PR-1 probe shows unlisted on demo
  timeframe: "1d"
  history_bars: 200       # <= 299 on okx (startup check 5)
risk:
  kill_switch_file: "KILL_SWITCH_DEMO"  # separate: the paper KILL_SWITCH does not stop demo, and vice versa
notifier:
  label: "OKX DEMO - fake money"
logging:                  # every path under logs/demo/ (run_demo.py refuses otherwise)
  audit_log_path: "logs/demo/audit_log.jsonl"
  trade_log_path: "logs/demo/trades.csv"            # => logs/demo/live_order_state.json, live_broker_halt.json, .live_broker.lock
  trade_plan_current_path: "logs/demo/trade_plans_current.json"
  trade_plan_history_path: "logs/demo/trade_plans_history.jsonl"
  market_snapshot_path: "logs/demo/market_snapshot.json"
  bar_decisions_path: "logs/demo/bar_decisions.json"
  closed_bars_dir: "logs/demo/daily_bars"
  retention_days: 30      # no parity check needs day-0 history here
```

`advisor.enabled: false` (as shipped). `starting_cash_usd: 10000` and `min_order_quote_usd: 10` as in the trial.

### 6.2 Symbol universe

The 16 trial bases on OKX `*/USDT` spot, from my knowledge (okx.com is unreachable from this sandbox, so this is not
verified): BTC, ETH, XRP, SOL, ADA, DOGE, AVAX, LINK, LTC, DOT, ATOM, ALGO, BCH, XTZ, SHIB are listed on OKX live spot
against USDT. **BNB is uncertain**: OKX has historically not listed it; expect it to be dropped. The **demo**
environment may list fewer pairs than live.

So nothing is hard-coded. `filter_listed_symbols(symbols, markets) -> (kept, dropped)` (pure, in
`src/exchange_demo.py`) keeps a symbol only if `markets[symbol]` exists, `spot is True`, `active is not False` and
`quote == demo.quote_currency`. Input order is preserved. Each dropped symbol is logged at WARNING with the reason, and
a single startup line lists them. `run_demo.py` then builds the in-memory `Config` with `market.symbols = kept` (a
deep copy; the yaml file is not touched) and passes the verified client into `build_components(cfg, exchange=...)`.
The universe stays `static` (`src/universe.py` unchanged), so this needs no change to `UniverseMember`/`Universe`.
The PR-1 probe prints the table so the committed `config/demo.yaml` lists only pairs that exist. The startup filter is
the guard if a listing changes later; at runtime a delisted symbol is the existing per-symbol "market data
unavailable" skip.

## 7. Ops: second instance on the same VM

### 7.1 `scripts/run_demo.py` (new; `run_live.py` untouched)

```
usage: run_demo.py --config PATH          (required; no default)
```

Startup (any failure: log the reason, `sys.exit(78)` = EX_CONFIG, which the unit maps to "do not restart"):

1. `cfg = load_config(args.config, env_file=None)`; require `cfg.mode == "demo"`; require the resolved path is not
   `config/config.yaml`.
2. **Log isolation:** every `logging.*` path (audit, trade log, plans current/history, snapshot, bar decisions,
   closed-bar dir) must resolve under `PROJECT_ROOT/logs/demo/` (configurable later; fixed for now), and
   `risk.kill_switch_file != "KILL_SWITCH"`. This makes "same `logs/` as the trial" a startup error, not a convention.
3. Tripwires (section 4.2).
4. `exchange = build_demo_exchange(cfg)`; `demo_startup_checks(exchange, cfg)`; filter symbols; build components
   with that exchange; `build_market_data(cfg, exchange)`.

Loop (same structure and sleep arithmetic as `run_live.py:275-305`):

1. `run_cycle(...)` (exceptions logged, as in `run_live.py`);
2. export: `python scripts/export_dashboard_data.py --config <cfg> --out logs/demo/data-demo.json --instance-id demo
   --instance-name "OKX demo"` with `HCT_DASHBOARD_PUSH_MINUTES=15` in the subprocess environment (freshness
   thresholds on the page; section 7.3), then `os.replace("logs/demo/data-demo.json", "web/data-demo.json")`. This is
   an atomic rename on the same filesystem, so a concurrent wrangler upload from the paper process never sees a torn
   file, and no temp file ever appears inside `web/`;
3. `send_heartbeat_ping(cfg.healthcheck_ping_url)` (the demo-only URL);
4. `run_log_retention(cfg)` once per UTC day.

It **never** runs `git`, `wrangler`, `dashboard_push` or `dashboard_deploy`, and does not run the pair-minimums
monitor in PRs 1-3. `LiveBroker` already refuses and alerts on a below-minimum buy, and the probe prints the minimums
table. The daily check can come later by passing `--config` through a demo-owned copy of the monitor; changing
`run_live.py` for it is out of scope.

### 7.2 systemd unit `deploy/hybrid-crypto-trader-demo.service` (new)

```ini
[Unit]
Description=hybrid-crypto-trader DEMO instance (OKX Demo Trading, fake money) -- separate from the paper trial
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/hybrid-crypto-trader
EnvironmentFile=/home/ubuntu/.config/hybrid-crypto-trader-demo/demo.env
ExecStart=/home/ubuntu/hybrid-crypto-trader/.venv/bin/python scripts/run_demo.py --config config/demo.yaml
Restart=always
RestartSec=60
RestartPreventExitStatus=78
# The paper trial has priority on this VM.
Nice=10
CPUWeight=50
MemoryMax=400M            # A1.Flex 6 GB: comfortable. On a 1 GB E2.1.Micro use 250M and watch `free -m`.

[Install]
WantedBy=multi-user.target
```

Same checkout, same `.venv`, same code on disk as the paper process. Consequence: merging a demo PR that touches
`src/` changes the code the **paper** process loads at its next restart. That is why every `src/` change in this
design is paper-neutral by construction and pinned by tests (section 8). Restart the paper service after such a merge
only inside the runbook's safe window (not 23:50-00:15 UTC), so the health check's "running code is the code on disk"
stays green.

### 7.3 Dashboard

* `web/instances.json` gets
  `{"id": "demo", "name": "OKX demo (fake money)", "data": "data-demo.json"}` with **no `kill_rule`** (the page then
  says "no rule"; the frozen trial thresholds stay on the `daily` entry only). The name passes the
  `[A-Za-z0-9._-]+.json` rule.
* Publication: the demo writes `web/data-demo.json` into the shared `./web`. The **paper** process's direct deploy
  (`scripts/dashboard_deploy.py`, `wrangler deploy` of all of `./web`) uploads it on the paper's next content change
  or 15-minute heartbeat. No change to the paper process; demo freshness on the site is at most about 15 min plus one
  export. **This requires direct deploy (#118) to be enabled on the VM** (open question 2). Without it the demo tab
  shows "no data", which is acceptable for phase 1.
* Rejected: the demo process doing its own `git add/commit/push` in the same checkout. It would race the paper loop's
  git commands on one index (`index.lock` failures, a commit picking up the other's staged file) and add Cloudflare
  build minutes. Also rejected: a second clone running its own `wrangler deploy`. Its `./web` would hold a stale
  `data.json` and overwrite the paper's fresh one on the site.
* `.gitignore` gets `web/data-demo.json`, `logs/demo/` and `KILL_SWITCH_DEMO` (today's patterns cover only top-level
  `logs/*.json` etc.). Before relying on it, verify that wrangler still uploads a gitignored file under `./web`
  (it uses `.assetsignore`, not `.gitignore`, as far as I know). The check is the first `curl -s
  https://<site>/data-demo.json` after enabling. If wrangler skips it, drop the `.gitignore` line and leave the file
  untracked (the health check counts tracked edits only).
* After a code-change Workers Build (from git), `data-demo.json` is absent until the next direct deploy (at most 15
  min). Accepted.
* Cosmetic, Platform Engineer, Haiku: a `.mode-demo` badge style in `web/style.css`. `ops.recost` still says Kraken
  T3 taker. That label is meaningless for the demo but harmless; leave it.

### 7.4 Health check (`scripts/vm_health_check.sh`, additive section)

The paper sections stay as they are. `pgrep -fc "scripts/run_live.py"` does not match `run_demo.py`, so "exactly one
bot process" keeps meaning the trial. New section `--- demo instance (optional)`:

* If `systemctl cat hybrid-crypto-trader-demo` fails: `INFO demo instance not installed` and skip the rest.
* Service active/enabled; `pgrep -fc "scripts/run_demo.py"` == 1; service start vs code-on-disk (same logic as paper).
* `config/demo.yaml` `mode: demo`.
* `logs/demo/live_broker_halt.json` present -> WARN. `logs/demo/live_order_state.json`: count of `inflight` entries
  in `intent|placed|unusable` and of `unverified` -> WARN if > 0 (python, prints counts only).
* `logs/demo/audit_log.jsonl` age <= 15 min; `web/data-demo.json` age <= 15 min.
* Env hygiene, **counts only, never values**: demo env file exists, mode `600`, owner `ubuntu`;
  `grep -c '^DEMO_' .env` > 0 -> WARN ("demo keys must not be in the shared .env");
  `grep -cE '^(EXCHANGE_API_KEY|EXCHANGE_API_SECRET|EXCHANGE_API_PASSWORD|LIVE_TRADING_CONFIRM)=.+' <demo env>` > 0
  -> WARN.
* Journal (24 h) for the demo unit: ERROR/Traceback count, restart count, any `exit 78` refusal.

### 7.5 Runbook for the user (goes into `deploy/DEMO_RUNBOOK.md` in PR 3)

Never paste a key, secret or passphrase into a chat, an issue, a commit or the dashboard.

1. Create or log in to an OKX account on the entity you are eligible for (from Ireland that is likely **OKX EEA,
   my.okx.com**, whose API host is `eea.okx.com` = ccxt `myokx`; open question 1). Do **not** fund the real account;
   the demo needs no real money, and an empty real account is one more layer between a bug and a loss.
2. Switch to **Demo Trading** (the toggle in the web/app UI). Set the demo account mode to **Spot mode** (no margin,
   no auto-borrow). Note the preloaded balances (USDT must be at least 10,000; if not, use the demo "reset/claim
   funds" option).
3. While in Demo Trading, create an API key: name `hct-demo`, permission **Trade** only (plus the default Read), **no
   Withdraw**, IP allowlist = the VM's public IP. Choose a passphrase of letters and digits only (avoids quoting issues
   in the systemd env file). OKX shows the secret once.
4. On the VM, over SSH (values typed, not echoed, never on a command line):
   ```bash
   install -d -m 700 ~/.config/hybrid-crypto-trader-demo
   umask 077
   read -r -s -p "OKX demo API key: " K; echo
   read -r -s -p "OKX demo secret: " S; echo
   read -r -s -p "OKX demo passphrase: " P; echo
   printf "DEMO_API_KEY='%s'\nDEMO_API_SECRET='%s'\nDEMO_API_PASSWORD='%s'\n" "$K" "$S" "$P" \
     > ~/.config/hybrid-crypto-trader-demo/demo.env
   unset K S P
   chmod 600 ~/.config/hybrid-crypto-trader-demo/demo.env
   ```
   Optional lines (same file): `DEMO_TELEGRAM_BOT_TOKEN=`, `DEMO_TELEGRAM_CHAT_ID=` (a separate chat is recommended),
   `DEMO_HEALTHCHECK_PING_URL=` (a **new** healthchecks.io check, never the paper one). Never put `DEMO_*` in the
   repo's `.env`, and never put `EXCHANGE_API_*` or `LIVE_TRADING_CONFIRM` in `demo.env`.
5. Read-only probe (PR 1):
   `set -a; . ~/.config/hybrid-crypto-trader-demo/demo.env; set +a; .venv/bin/python scripts/demo_probe.py --config config/demo.yaml; exec $SHELL`
   It must print `sandbox verified`, an authenticated USDT balance, the symbol table and the candle checks. It places
   nothing. (`exec $SHELL` drops the exported variables from the session.)
6. Optional smoke (PR 3): `scripts/demo_smoke.py` (section 10) **before** the service is first started.
7. `sudo cp deploy/hybrid-crypto-trader-demo.service /etc/systemd/system/ && sudo systemctl daemon-reload &&
   sudo systemctl enable --now hybrid-crypto-trader-demo`, then `journalctl -u hybrid-crypto-trader-demo -f`. Start it
   outside 23:50-00:15 UTC.
8. Halt in demo: follow `docs/live_readiness_design.md` §2 against `logs/demo/` and the demo account. This is
   deliberate practice for §11 item 8. A demo halt never affects the trial.
9. Rotate or revoke: delete the key in OKX Demo Trading, create a new one, repeat step 4, restart the demo unit.
10. Stop the experiment: `sudo systemctl disable --now hybrid-crypto-trader-demo`; remove the `instances.json`
    entry in a PR; archive `logs/demo/`.

## 8. Tests and invariants

New test files (all offline: fake ccxt objects or ccxt's own request builders with `set_markets`, no network, no
keys):

**`tests/test_demo_exchange.py`**

* okx and myokx: flags, header and `prepare_request_headers({})` carry `x-simulated-trading: 1`; the signed private
  request for `trade/order` (`client.sign(...)` + `prepare_request_headers`) carries it.
* Refusals (`SandboxNotVerified`): `kraken`, unknown id, `bybit`/`binance` (not registered yet), header removed after
  construction, `isSandboxModeEnabled=False`, `options["sandboxMode"]=False`, hostname changed, `fetchOHLCV.timezone =
  "UTC+8"`.
* Per-call guard: with the header removed, `create_market_order`, `find_order`, `fetch_free_balance`,
  `fetch_ohlcv_df` raise **before** any ccxt method is called (a fake records zero calls).
* `1d` -> request `bar == "1Dutc"` (intercept `publicGetMarketCandles`).
* Request shape for a market buy (ccxt `create_order_request` with a hand-built `BTC/USDT` market): `sz` = base
  amount, `tdMode == "cash"`, `clOrdId == coid`, no `tgtCcy=quote_ccy`, no `tag`.
* `51016` -> `DuplicateOrderId` on okx clients; a kraken client's exception map is unchanged.
* `find_order` okx branch: by client id via `fetch_order(None, symbol, {"clientOrderId": coid})`; `OrderNotFound` ->
  `None`; network error propagates; mismatched `clientOrderId`/symbol -> `OrderLookupAmbiguous`; exchange-id path
  unchanged; kraken path unchanged (existing tests).
* `filter_listed_symbols`: unlisted / inactive / non-spot / wrong quote dropped with reasons, order preserved, none
  left -> refusal in `run_demo`.
* `demo_startup_checks`: `AuthenticationError` -> refuse; free USDT below starting cash -> refuse; `history_bars=300`
  -> refuse; happy path returns the summary without any secret in it.
* clientOrderId: `client_order_id(...)` matches `^hct[0-9a-f]{15}$`, length <= 32.

**`tests/test_demo_mode.py`**

* `Config.mode` accepts `demo`, still rejects other strings. Accessor matrix of section 4.1 (each cell, including the
  raises). A `demo:` block in a paper/live config -> `ValueError`. `load_config(env_file=None)` does not read `.env`
  (temp `.env` with `TELEGRAM_BOT_TOKEN` -> `None` in demo).
* Gate: demo + verified fake demo client -> OK; demo + plain `ExchangeClient` / `None` / tampered client -> refused;
  demo + `live_trading_confirmed=True` -> refused; live + confirmed + demo client -> refused; the existing live-gate
  tests unchanged and passing.
* `build_components` in demo: `LiveBroker`, quarantine on, flock under `logs/demo/`, `trades.csv` rows `mode=demo`;
  passing a demo client in paper/live -> refused; live with any `DEMO_*` env -> refused; **paper with `DEMO_*` env ->
  identical components** (no refusal).
* **Virtual capital:** demo build against a fake account with 1e9 USDT -> `portfolio.cash == starting_cash_usd` and
  the risk engine's approved size equals the paper build's on the same plan.
* End-to-end demo cycle on a fake okx (extend `tests/_live_fakes.py`): ack-only create -> verified by lookup -> booked
  with the base fee; sell -> quote fee; crash after `intent` -> reconcile `never_placed` after the grace; crash after
  placement -> reconcile `filled` by client id.
* `bot.main` refuses demo; `run_live.main` / `run_paper.main` refuse demo (existing guards, new assertions).

**`tests/test_run_demo.py`**

* Refusals with exit 78: no `--config`; `config/config.yaml`; mode not demo; any logging path outside `logs/demo/`;
  `KILL_SWITCH` shared; `LIVE_TRADING_CONFIRM` set; any `EXCHANGE_API_*` set.
* The loop's subprocess calls never include `git` or `wrangler` (mock `subprocess.run`); export lands in
  `web/data-demo.json` by `os.replace`; the heartbeat uses `DEMO_HEALTHCHECK_PING_URL` only.

**Existing files extended:** `tests/test_paper_isolation.py` (paper never imports `src.exchange_demo`; paper result is
identical with `DEMO_*` set); `tests/test_paper_trial_config.py` (unchanged; it already pins `config/config.yaml`); a
new `tests/test_demo_config.py` pins `config/demo.yaml` (mode, venue, every `logging.*` under `logs/demo/`, label
contains `DEMO`, every strategy/risk key equal to `config/config.yaml` except `kill_switch_file`);
`tests/test_dashboard_page.py` / `test_dashboard_display_js.py` (a `demo` instance with no `kill_rule` renders "no
rule"; the `daily` entry's frozen thresholds unchanged); `tests/test_validate_invariants.py` (new checks and negative
fixtures).

**`scripts/validate_invariants.py`** (QA owns; Opus because it touches checks; **checks are added, none relaxed**):

* `check_live_trading_two_switch_gate`: tests 1-3 unchanged; add 4: `LiveBroker(None, None, "demo", False)` raises;
  5: `mode="demo"` with a plain `ExchangeClient("okx")` raises; 6: `mode="demo"` with `live_trading_confirmed=True`
  raises; 7: `mode="live"`, confirmed, with an (offline-constructed, key-less) `DemoExchangeClient("okx", None, None,
  None)` raises.
* New invariant 8 `demo_config_isolated` (only when `config/demo.yaml` exists): `mode: demo`, `exchange.testnet:
  true`, `exchange.id` in `SANDBOX_VERIFIERS`, every `logging.*` path under `logs/demo/`, `kill_switch_file !=
  KILL_SWITCH`, no key in the yaml whose name contains `key`/`secret`/`password`/`passphrase`, label contains `DEMO`.
* Invariant 3 (`config/config.yaml` is paper) unchanged.

**`scripts/mutation_check_live_readiness.py`: yes, new entries.** The demo door is in `broker_live.py`, so it gets
the same mutation discipline. Add the three new test files to the run list, update M-entry `:146` for the changed
quarantine expression, and add (each must be killed):

| id | mutation |
|---|---|
| D01 | `_require_demo_gate`: `isinstance(exchange, DemoExchangeClient)` check removed |
| D02 | `_require_demo_gate`: `assert_sandbox()` call removed |
| D03 | `_require_demo_gate`: `live_trading_confirmed` refusal removed |
| D04 | live branch: `_is_demo_client` refusal removed |
| D05 | okx verifier: header check (item 3) removed |
| D06 | okx verifier: merged-headers check (item 4) removed (`prepare_request_headers` tampering undetected) |
| D07 | okx verifier: timezone check (item 6) removed |
| D08 | `DemoExchangeClient.create_market_order`: per-call `assert_sandbox()` removed |
| D09 | `build_demo_exchange` reads `cfg.exchange_api_key` instead of `cfg.demo_api_key` (the accessor must raise -> killed) |
| D10 | `51016` mapping removed (duplicate id treated as a definite rejection) |
| D11 | okx `find_order` branch: `OrderNotFound` from a lookup **by exchange id** also mapped to `None` |
| D12 | `Config.mode` accepts any string |
| D13 | `run_demo` log-isolation check removed (paper `logs/` path accepted) |

Always: `python -m pytest -q`, `PYTHONPATH=. python scripts/validate_invariants.py`,
`python scripts/mutation_check_live_readiness.py --jobs 4` (all mutations killed), stated in each PR description.

## 9. Risks, and what the demo does NOT prove

What it does exercise for real: ccxt+OKX request signing, the ack-only -> lookup verification path, fee parsing,
precision/minimum checks, `clOrdId` round-trip, reconcile by client id after a restart, real API error mapping, the
halt/alert/resume procedure, and running `LiveBroker` unattended 24/7.

What it does **not** prove:

* **Not Kraken.** §11 item 7 (Kraken `find_order` with the server-side `cl_ord_id` filter, Kraken balances,
  precision, `cl_ord_id` echo) is untouched. Kraken's code path is not exercised at all. No §11 condition is met by
  this work.
* **Not real liquidity.** Demo matching fills market orders almost always instantly and in full, with unrealistic
  slippage and depth. The partial-fill, remainder and slow-ack paths will rarely or never trigger.
* **Not real fees or fee tiers**, and not the trial's Kraken T3 judging tier.
* **Not strategy evidence.** OKX USDT demo bars differ from Kraken USD bars (the trial preflight measured USD vs USDT
  close differences up to ~750 bp even on one venue). Demo entries will differ from trial entries. Never pool, compare
  for the kill rule or quote demo P&L.
* **Pre-funded demo coins** blind the free-balance sell cap (section 6) for those coins.
* **Demo environment behaviour** (resets, maintenance, outages, instruments missing from demo) can halt the demo.
  That is a correct response, but it means demo uptime is not live-uptime evidence.
* **Shared VM.** The demo competes with the trial for RAM/CPU around 00:00-00:05 UTC. Mitigated by
  `Nice`/`CPUWeight`/`MemoryMax`; watch `free -m` on a 1 GB shape.
* **Shared code on disk.** Demo PRs change `src/` the trial loads at its next restart. Mitigated by
  paper-neutral-by-construction changes, the paper isolation/parity/config tests, and restarting only in the safe
  window.
* **Dashboard dependency.** Demo data reaches the site only through the paper's direct deploy; if that breaks, the
  demo tab goes stale (the freshness chip shows it).
* **Human confusion.** Mitigated by the label, a separate (or no) Telegram chat, and a separate dashboard tab.
* **Keys.** A leaked demo key cannot move money, but the runbook still treats it as a secret (no chat, `read -s`, file
  `600`, IP allowlist, trade-only).
* **Residual real-money risk** would need all of: the header dropped (blocked by construction, startup and per-call
  checks), a real-environment key in `DEMO_API_*` (blocked: the authenticated startup read would fail with `50101`
  while the header is present), and funds in the real OKX account (runbook: keep it empty). No single bug reaches
  real money.

## 10. Phased plan

| Phase | Content | Can it trade? | Review |
|---|---|---|---|
| **PR 1** (smallest safe) | `src/exchange_demo.py` (`DemoExchangeClient`, `SANDBOX_VERIFIERS` okx/myokx, `assert_sandbox`, per-call guards, `51016` map, `filter_listed_symbols`, `demo_startup_checks`, `build_demo_exchange`); `src/config.py` (`mode: demo`, mode-aware accessors, `demo` block, `load_config(env_file=...)`); `scripts/demo_probe.py` (read-only: verification, authenticated balance, symbol/minimums table, last 3 `1Dutc` bars with 00:00 UTC alignment, last closed close vs `logs/daily_bars` Kraken USD close in bp); `tests/test_demo_exchange.py` + the config part of `test_demo_mode.py`; one guard line in `build_components` that refuses `mode: demo` until PR 2 (without it, a `demo` config would silently fall through to `PaperBroker`, `src/bot.py:161-176`). | No: no order method is reachable; the probe calls only `fetch_balance`, `load_markets`, `fetch_ohlcv` | Opus (sandbox verification is a safety switch) |
| **PR 2** | `broker_live.py` demo door + `mode` column + `[DEMO]` titles; `bot.py` wiring (section 3.2, `build_components(exchange=...)`, `main` refusal, `_SECRET_ATTRS`, live tripwire); `src/exchange.py` okx `find_order` branch and `51016` map for okx `ExchangeClient`; `validate_invariants` additions; mutation entries; `test_demo_mode.py` complete; paper-isolation extension; README architecture/config/layout updates in the same PR | Only via code; nothing deployed | Opus Architect + Risk Officer read-through (`broker_live.py`, `bot.py`) |
| **PR 3** | `scripts/run_demo.py`; `config/demo.yaml` (symbols from the PR-1 probe output); `deploy/hybrid-crypto-trader-demo.service`; `deploy/DEMO_RUNBOOK.md`; `.gitignore`; `vm_health_check.sh` demo section; `web/instances.json` demo entry (+ `.mode-demo` style); `scripts/demo_smoke.py` (one minimum-size BTC/USDT buy then sell through `LiveBroker` in demo mode, own `logs/demo_smoke/`, requires `--i-understand-this-places-demo-orders`, refuses if the demo service is active); `test_run_demo.py`, `test_demo_config.py`; `CLAUDE.md` roster rows (Platform Engineer: `exchange_demo.py`, `run_demo.py`, `demo.yaml`, the demo unit; Opus for the verifier/gate parts); `docs/STATUS.md` line | The user starts it | Opus for `run_demo.py` (safety guard) |
| **User** | Runbook steps 1-7 | Demo orders, fake money | - |
| **Later** | Demo-only read-only drift report (cash + all coin balances vs the ledger, §11 item 5 prototype); a demo pair-minimums monitor; Bybit demo / Binance testnet verifiers; the keyless market-data fallback if the probe requires it | - | per change |

Merge discipline for the trial: PRs 1-3 change no file the paper process reads at runtime except `src/config.py`,
`src/bot.py` and `src/exchange.py`, all paper-neutral and test-pinned. Merge them, then restart the paper service
inside the safe window (runbook), then confirm the next daily bar is decided normally before starting the demo unit.

## 11. Open questions for the user

1. **Which OKX entity is your account on**, global (`www.okx.com`, ccxt `okx`) or EEA (`my.okx.com` /
   `eea.okx.com`, ccxt `myokx`, likely from Ireland), and does that entity offer Demo Trading with demo API keys? The
   design supports both. The answer decides `exchange.id` in `config/demo.yaml`.
2. **Is the optional direct dashboard deploy (#118) enabled on the VM?** The demo tab depends on it. Without it, the
   demo runs but its tab shows "no data" until you enable it.
3. **Telegram for the demo:** a separate chat (recommended), the trial's chat with the `OKX DEMO - fake money` label,
   or none?
4. **Cadence:** keep the trial's daily strategy (recommended; few orders, about one decision burst a day, and
   `demo_smoke.py` gives immediate order coverage), or run the demo at 1h purely to generate more order traffic (a
   different strategy cadence, explicitly not evidence)?

## 12. Decisions on section 11 (orchestrator, 2026-11-16)

These override the corresponding text above where they differ.

1. **Venue: `myokx` (OKX EEA, `eea.okx.com`).** The user's account is on `my.okx.com/en-eu`. ccxt 4.5.90 `myokx`
   `set_sandbox_mode(True)` sets the same `x-simulated-trading: 1` header with host `eea.okx.com` (checked in the
   installed source). `config/demo.yaml` uses `exchange.id: myokx`; the verifier accepts both ids as specified in §5.
   Conditional on the user confirming that OKX EEA offers Demo Trading API keys. If it does not, the fallback is Bybit
   demo trading (`enable_demo_trading(True)`, `api-demo.bybit.com`), which needs its own verifier PR before use.
2. **Dashboard publication without direct deploy.** The VM health check shows direct deploy (#118) is NOT enabled and
   Node is not installed. So, instead of relying on direct deploy: the paper process's existing git push
   (`scripts/dashboard_push.py`) also stages `web/data-demo.json` when that file exists, and its content fingerprint
   includes it. The demo process still never runs git or wrangler (no index races). Consequently `web/data-demo.json`
   is NOT gitignored. This is a small, additive change to the paper process's dashboard push only (no trading code);
   it ships in PR 3 with a test that the push is unchanged when the file is absent. When direct deploy is enabled
   later, it uploads the same file with no further change.
3. **Telegram: none for the demo in phase 1** (`notifier` disabled in `config/demo.yaml`; the demo process does not
   read the Telegram env vars). The user trades the paper trial's alerts by hand, so fake-money alerts must not reach
   that chat. A separate chat can be added later as opt-in.
4. **Cadence: daily**, the same strategy and timeframe as the trial; `demo_smoke.py` provides immediate order
   coverage.
