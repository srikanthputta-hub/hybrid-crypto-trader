# System redesign 2026-10: full universe, multi-timeframe confluence, sentiment inputs

- **Status:** design draft for user review. No code in this change.
- **Author role:** Architect (CLAUDE.md roster). Contract changes are rated **High** on the complexity rubric (Opus).
- **Date:** 2026-10-05. Branched from `origin/main` at `ad30b4a`.
- **Scope:** three phases the user has already decided on, in this order: (1) the full Kraken USD/USDT universe,
  (2) multi-timeframe (MTF) confluence, (3) Fear & Greed + CryptoPanic sentiment as plain-code pipeline inputs.
  This doc covers *how* to build them. It does not reopen *whether* to build them.

Non-negotiables this design keeps in every phase: the two-switch live gate in `src/broker_live.py` is untouched,
and no phase changes `broker_live.py` at all. The Claude advisor stays APPROVE/VETO only and fails closed. Every
new decision input is deterministic Python. No new stage calls an LLM. Every new stage can only **remove** a
trade (veto). None of them can originate or resize one. The 5-gate backtest stays the promotion bar. Phase 3's
CryptoPanic part is the one place it cannot be applied directly, and §6.5 handles that openly.

---

## 1. Facts that constrain the design (measured, not assumed)

Unless noted, these were measured from this session on 2026-10-05 against Kraken's public API with ccxt 4.5.84.
This time Kraken *was* reachable from the sandbox, unlike the 2026-09-27 note in `config/config.yaml`.

| Fact | Value | Consequence |
|---|---|---|
| Kraken spot markets quoted in USD | 676 (632 `online`, 42 `cancel_only`, 2 `post_only`) | The "full universe" is about 630 pairs, not 150-250. A liquidity floor is required, not optional (see §4.3). |
| Kraken spot markets quoted in USDT | 45. **Every USDT base also has a USD pair.** | USD/USDT should be resolved per base, preferring USD. |
| The config's "confirmed dead" symbols (NEAR, SUI, ...) | Dead as **/USDT** only. NEAR/USD and SUI/USD are both in Kraken's top 10 USD pairs by 24h volume. | The dead list in `market.symbols` comments is a USDT-quote artifact. A USD-first universe brings these coins back. |
| USD pairs by 24h quote volume (one `fetch_tickers` snapshot, includes stablecoin/fiat pairs) | >=$100k: 176 · >=$500k: 93 · >=$1M: 65 · >=$5M: 18 · >=$10M: 12 | The user's "150-250+" maps to roughly a $100k/day floor. That is deep into the illiquid tail. |
| Stablecoin/fiat/wrapped bases among USD pairs | USDT, USDC, DAI, PYUSD, RLUSD, USDG, USDE, USDS, USD1, USDQ, EURC, EURQ, EUR, GBP, AUD, PAXG, XAUT, TBTC | These must be excluded by rule. USDT/USD and USDC/USD are the #2 and #3 USD pairs by volume. |
| `fetch_tickers` across all 632 USD pairs | **1 call, 1.2 s** | Current prices for the whole universe cost one call, not N calls. |
| ccxt `kraken.rateLimit` | 1000 ms (about 1 public call/s) | Kraken's public REST limit is per IP. Concurrency does not raise it. |
| Sequential `fetch_ohlcv` | 5 calls = 5.7 s (about **1.1 s/call**) | A per-symbol OHLCV pass over N symbols takes about 1.1·N seconds. |
| Kraken OHLC depth | Max 720 bars per call. **`since` cannot reach further back**: a 15m request with `since`=60 days ago returned bars from 2026-09-28 only (7.5 days). | Kraken cannot supply intraday *history* for backtesting. Phase 2's dataset must come from elsewhere (§5.4). |
| ccxt async / ccxt.pro | Not importable: `aiohttp` is not installed | Async or WebSocket would add a new runtime dependency. |
| Live price source today | `run_cycle` sets `current_prices[symbol]` to the last **closed** 1h close (`ExchangeClient.fetch_ohlcv_df` drops the forming bar, since #46) | With a 300 s poll on 1h bars, 11 of 12 cycles re-download 200 bars and decide on **identical** inputs. Today's loop spends most of its exchange budget on no-op cycles. |
| Exchange calls per hour today | 16 OHLCV × 12 cycles = 192, plus `scripts/export_dashboard_data.py` (run after every cycle by `scripts/run_live.py`) making 16 `fetch_ticker` calls plus one OHLCV call per active plan per cycle | Roughly 400-500 calls/h for 16 symbols. The exporter runs in a **subprocess with its own ccxt throttler**, so its calls are not rate-limited together with the bot's. |
| Backtest harness | `strategy_pack_run/backtest_live_bot.py` replays the real `src/` pipeline on **daily bars only** (`TF_SECONDS = {"1d": 86400}`, `load_dir` globs `*_1d.csv`) | Today `htf_filter` is tested daily-against-daily, so the multi-timeframe property of the live bot has **never been backtested**. The live 1h strategy itself has only been gated on daily bars. |
| Live strategy's own gate status | `long`: 5/5 PASS. `kraken_720`: FAIL on CI-low and placebo (`report_live_strategy_kraken_720.md`). All 18 Routine candidates pass `long` and fail `kraken_720`. | A "promotion bar" needs a precise definition per phase (§7). |

Not verified from this session because the egress proxy returned 403: `api.alternative.me` (Fear & Greed) and
`cryptopanic.com`. Every statement below about those two services is marked as either documented or assumed,
and the open questions list what the user has to confirm.

---

## 2. Target pipeline shape

```
 [infra] UNIVERSE              src/universe.py          (Phase 1)  which symbols exist today. Refreshed daily, not per cycle.
 [infra] MARKET DATA CACHE     src/market_data.py       (Phase 1)  bar-close-aligned OHLCV cache per (symbol, timeframe),
                                                                    one shared rate budget, monitor fetches first
    |
 1. SCANNER                    src/scanner.py           unchanged logic; runs on market.timeframe (1h) only
 2. SIGNAL ENGINE              src/signal_engine.py     unchanged per-symbol logic + NEW rank_signals() (Phase 1)
    |   -- cycle pass boundary: all symbols scanned, then bullish signals ranked once, then processed in rank order --
 2b. CONTEXT GATES (veto-only) src/context_gates.py     formalizes today's htf_filter slot into one interface
       i.   fear_greed gate    src/sentiment/           (Phase 3a)  market-wide, 1 value/day, no per-symbol cost
       ii.  MTF confluence     src/mtf_confluence.py    (Phase 2)   replaces src/htf_filter.py
       iii. cryptopanic gate   src/sentiment/           (Phase 3b)  per-coin, rate-limited, SHADOW first
 3. TRADE PLANNER              src/trade_planner.py     copies gate verdicts into rationale (no TradePlan field change)
 4. RISK ENGINE                src/risk_engine.py       + set_symbol_whitelist() (Phase 1). Sizing unchanged.
 4b. CLAUDE ADVISOR            src/claude_advisor.py    unchanged
 5. MONITOR                    src/monitor.py           unchanged. Always runs for every held symbol, in or out of the universe.
```

### 2.1 Stage decisions and why

- **Universe and market-data cache are infrastructure, not stages.** They don't look at setups or make trade
  decisions. They decide which symbols and bars exist, the same role "market data (ccxt)" plays in the README
  diagram today. The scanner -> monitor pipeline keeps five stages.
- **Context gates stay in the existing stage-2b slot. No new numbered stage.** `htf_filter` already sits between
  the signal engine and the planner and can only veto. MTF confluence is a generalization of it. Both sentiment
  inputs have the same contract: take a bullish `Signal` and return pass/veto plus a reason. Putting them behind one
  interface (`GateVerdict`, §3) means each new input is one more module in the same slot, not a new shape.
- **Why sentiment goes here and not in scanner, signal-engine scoring, or risk:**
  1. *After the signal engine:* per-coin news lookups happen only for symbols that already have a live bullish
     signal, which is a handful per bar close. The other option is ~150 symbols every cycle. That keeps
     CryptoPanic within a free-tier quota no matter how big the universe is.
  2. *Before the trade planner and risk engine:* a vetoed signal never takes a cooldown slot, a
     `max_new_entries_per_window` slot, or exposure headroom.
  3. *A veto, not a score adjustment:* changing `engine_score` would move confidence stars and Phase 1's
     cross-symbol ranking. That is harder to validate than a binary gate. A gate splits trades cleanly into
     "blocked" and "allowed", which is exactly what §6.5's shadow evaluation needs.
  4. *Not in the risk engine:* sentiment shouldn't size positions. Sizing is Risk Officer / Opus territory, and a
     soft, unvalidated input has no place in it.
  5. *Not an LLM:* CLAUDE.md non-negotiable. Every gate is plain arithmetic over numbers fetched from an API.
- **Gate order: i -> ii -> iii, cheapest and most certain first.** F&G is one cached number per day. MTF needs
  extra OHLCV fetches. CryptoPanic is the scarcest budget (quota). Each gate only sees what the previous one
  passed, so each costs as little as possible.
- **Cycle restructure (Phase 1):** `run_cycle` today runs scan -> plan -> risk symbol by symbol, in
  `market.symbols` order. That means **config order decides which symbols win the scarce exposure and new-entry
  slots**. With 16 symbols that's minor. With 150 it is an unexamined selection rule. The cycle becomes:
  pass A (every symbol: monitor, then scan + signal), pass B (rank the bullish signals), pass C (in rank order:
  gates -> planner -> risk -> advisor -> store). The "exactly one audit line per symbol per scan" property
  holds: pass A starts each symbol's event dict, pass C finishes it.

---

## 3. Shared contract changes (all phases)

The rule: additive optional fields only. Nothing is removed or redefined.

| Contract | Change | Phase | Default | Constructed at | Consumed at |
|---|---|---|---|---|---|
| `Candidate` (`src/scanner.py`) | `+ timeframe: str \| None = None` (provenance; scanner sets `market.timeframe`) | 2 | `None` | `scanner._detect_*` (10 sites) | `signal_engine.evaluate`, `bot.py` audit `candidates`, `backtest_live_bot` |
| `Signal` (`src/signal_engine.py`) | `+ timeframe: str \| None = None` | 2 | `None` | `signal_engine.evaluate`, `strategy_pack_run/backtest_live_bot._placebo_signal_at`, `tests/test_signal_engine.py`, `tests/test_trade_planner.py`, `tests/test_bot.py` | `trade_planner.build_plan`, `bot.py`, harness |
| `Signal` | `+ context_gates: dict[str, dict[str, Any]] = field(default_factory=dict)`, keyed by gate name (`"mtf"`, `"fear_greed"`, `"cryptopanic"`), value = `GateVerdict.to_dict()` | 2 (reused in 3) | `{}` | gates write into it in pass C | `trade_planner` (copies it into `rationale["context_gates"]`), `bot.py` audit |
| `GateVerdict` (**new**, `src/context_gates.py`) | `name: str; passed: bool; mode: "enforce"\|"shadow"; reason: str; score: float \| None; details: dict; data_as_of: str \| None` | 2 | n/a | each gate | stored only as `to_dict()` |
| `TradePlan` | **no field change**. Gate data travels in the existing free-form `rationale` dict. | n/a | n/a | n/a | n/a |
| `PlanRecord`, `RiskDecision`, `AdvisorVerdict` | **unchanged** in all three phases | n/a | n/a | n/a | n/a |
| `RiskEngine` API (not a dataclass, but a contract) | `+ set_symbol_whitelist(symbols: Iterable[str]) -> None`. The whitelist check in `check_new_trade` keeps the same meaning: "may open **new** trades on". | 1 | n/a | built in `bot.build_components`; re-set on every universe refresh | `check_new_trade` |
| `UniverseMember` / `Universe` (**new**, `src/universe.py`) | `symbol, base, quote, median_quote_volume_30d, cost_min, amount_min, source ("discovered"\|"pinned"\|"held")`; `Universe(members, generated_at, excluded: dict[symbol, reason])` | 1 | n/a | `universe.refresh` | `bot.py`, `market_data`, `RiskEngine.set_symbol_whitelist` |

**Why `context_gates` holds plain dicts and not nested dataclasses:** `TradePlanStore` and `AuditLog` serialize with
`json.dumps(..., default=str)`. A nested dataclass wouldn't fail. It would quietly turn into its `repr` string in
`logs/trade_plans_current.json` and the audit log, and the plan would come back from disk as a different shape
than it went in. The test for this change has to assert that `plan.rationale` survives a JSON round trip
unchanged.

**Audit-event keys (not a dataclass, but read by `scripts/export_dashboard_data.py:429`):** `daily_trend_confirmed`
stays as long as `htf_filter` exists. The PR that retires `htf_filter` (Phase 2c) also updates the exporter to read
`context_gates`, in the same change.

---

## 4. Phase 1: full Kraken USD/USDT universe

### 4.1 What changes

| File | Change | Owner (rubric tier) |
|---|---|---|
| `src/market_data.py` (new) | In-memory OHLCV cache keyed by `(symbol, timeframe)`. Each key is refetched **only once a new bar of that timeframe has closed** (closed-bar logic reused from `ExchangeClient.fetch_ohlcv_df`). Also handles one shared call budget per cycle, priority ordering (held symbols, then pinned, then by liquidity rank), a stale-data check, and a per-cycle snapshot written to `logs/market_snapshot.json`. | Platform Engineer (Sonnet) |
| `src/universe.py` (new) | `refresh(exchange, cfg, held_symbols) -> Universe`: `load_markets` -> spot -> quote in `universe.quotes` -> `info.status == "online"` -> drop `exclude_bases` / `exclude_symbols` / quarantined -> one pair per base (`prefer_quote`) -> point-in-time liquidity (30-day **median** daily quote volume from 1d OHLCV, with hysteresis) -> `min_listing_age_days` -> rank, cap at `max_symbols` -> add `include_symbols` -> add held symbols as `source="held"` (monitor-only). | Platform Engineer (Sonnet) |
| `src/exchange.py` | `+ fetch_tickers_prices(symbols) -> dict[str, float]` (one call). `create_market_order` untouched. | Platform Engineer (Haiku/Sonnet) |
| `src/signal_engine.py` | `+ rank_signals(signals: dict[str, Signal], liquidity: dict[str, float]) -> list[str]`. Pure function. Key: `(-engine_score, -median_quote_volume_30d, symbol)`. Deterministic and pre-registered here, not tuned. | Market Analyst (Sonnet) |
| `src/bot.py` | Three-pass `run_cycle` (§2.1). Scan only on bar close; monitor every tick. One active plan **per base asset**, not per symbol (stops BTC/USD and BTC/USDT both being held during the quote migration). Universe refresh daily at `universe.refresh_utc`. Held symbols are always monitored even after they leave the universe. | Architect + Execution Trader (**Opus**, since it calls into the risk engine) |
| `src/risk_engine.py` | `+ set_symbol_whitelist()` only. No sizing change. | Risk Officer (**Opus, always**) |
| `src/config.py` | `+ universe`, `+ market_data` properties. `symbols` keeps its meaning in `static` mode. | Platform Engineer |
| `scripts/run_live.py` | Sleep `interval - elapsed` instead of a flat `interval` (a 165 s bar-close pass should not push the next tick out by 300 s). | Platform Engineer |
| `scripts/export_dashboard_data.py` | Read prices and chart candles from `logs/market_snapshot.json`. **Zero exchange calls.** `symbols` in `web/data.json` becomes the resolved universe. | Platform Engineer |
| `web/app.js` | The symbol sidebar (`app.js:1091`) assumes ~16 entries. It needs filter/search or should show only symbols with activity. | out of roster; flag for whoever owns `web/` |
| `strategy_pack_run/fetch_kraken.py`, `fetch_coinbase_long.py` | `--universe discover`: fetch 1d history for every Kraken USD base. **Include `cancel_only` pairs in the dataset** (they're being wound down, which is exactly the population survivorship bias drops), even though they're excluded live. | QA/Tester (Sonnet) |
| `strategy_pack_run/backtest_live_bot.py` | Point-in-time universe per bar (§4.4); `symbol_whitelist` updated per bar; process symbols in `rank_signals` order instead of `sorted(symbols)`; liquidity-tiered slippage. | QA/Tester + Architect review |
| `scripts/validate_invariants.py` | `+` invariant: none of `src/universe.py`, `src/market_data.py` imports `anthropic` or `src.claude_advisor`. | QA/Tester |

### 4.2 Config shape

```yaml
market:
  symbols: [...]            # unchanged; still the source of truth when universe.mode == "static"
  timeframe: "1h"
  history_bars: 200

universe:
  mode: static              # "static" = exactly today's behavior (market.symbols) | "discover"
  quotes: ["USD", "USDT"]
  prefer_quote: "USD"       # one pair per base asset; USD is deeper on Kraken, and every USDT base has a USD pair
  min_median_quote_volume_usd: 1000000   # 30-day MEDIAN daily quote volume, point-in-time (not today's 24h ticker)
  exit_volume_ratio: 0.5    # hysteresis: a member leaves only below 0.5 x the floor, so membership doesn't churn
  min_listing_age_days: 60  # also guarantees >= 26 daily bars for the 1d slow EMA (htf_filter / Phase 2)
  max_symbols: 150
  include_symbols: []       # pins (must still be online)
  exclude_bases: [USDT, USDC, DAI, PYUSD, RLUSD, USDG, USDE, USDS, USD1, USDQ, EURC, EURQ, EUR, GBP, AUD, PAXG, XAUT, TBTC]
  exclude_symbols: {}       # "SYMBOL": "reason + date" -- human-maintained structural exclusions (replaces the comment list)
  quarantine_after_failures: 3   # consecutive fetch failures -> excluded until next daily refresh (auto, temporary)
  refresh_utc: "00:15"

market_data:
  bar_close_settle_seconds: 20   # wait after a bar's close before fetching it
  max_scan_pass_seconds: 240     # symbols not reached in time skip THIS bar's scan (logged), never the monitor
```

Rationale for the defaults. These are starting values to test, not tuned values:
- **$1M median floor**: about 65 USD pairs today before exclusions. That's roughly 4x the current universe while
  staying out of the tail the playbook warns about (`docs/crypto_bot_upgrade_playbook.md` Layer 1: "small caps
  revert instead of trend"; §1.2: "spreading across more coins lowered Sharpe"). The user asked for the full
  universe, and the floor is a config value. §4.4 tests at most **two a-priori floors** ($1M and $100k) as
  separate candidates, not a sweep.
- **Quarantine is automatic and temporary. Permanent exclusion stays human.** Auto-excluding permanently is how a
  good symbol disappears without anyone noticing. A symbol quarantined 3 days in a row shows up on the dashboard
  for a human to add to `exclude_symbols` with a reason, which is the same discipline the current config
  comments follow.

### 4.3 Scan-cycle and rate-limit design

**The main lever is not doing no-op work. Concurrency doesn't help.** Kraken limits per IP. Async ccxt
(`ccxt.async_support`, which needs `aiohttp`) would only overlap network latency, about a 10% gain at
1.1 s → ~1.0 s per call. **Recommendation: stay synchronous.** No new dependency. Revisit WebSocket OHLC (Kraken
WS v2 `ohlc` channel, many symbols on one socket) only if §4.6's operational acceptance fails.

The schedule:

| What | When | Calls |
|---|---|---|
| Base-timeframe (1h) OHLCV, every universe member | once per 1h bar close (+20 s settle) | N per hour |
| 1d OHLCV, every universe member (liquidity ranking; shared with Phase 2's 1d confirmation) | once per day, trickled at low priority after 00:00 UTC | N per day |
| `load_markets` | once per day (universe refresh) | 1 per day |
| Monitor prices for held symbols | every tick, from the cached last **closed** 1h close (same semantics as today and as the backtest) | 0 extra |
| Dashboard prices | every tick, one `fetch_tickers` over held + universe symbols, written into `market_snapshot.json` | 1 per tick |
| Chart candles for active plans | from the cache | 0 extra |

Bar-close pass length at about 1.1 s/call:

| N (universe) | 1h pass at bar close | fits in `max_scan_pass_seconds: 240`? | calls/hour, steady state |
|---|---|---|---|
| 16 (today) | ~18 s | yes | ~30 (vs ~400-500 today) |
| 65 ($1M floor) | ~72 s | yes | ~80 |
| 150 (`max_symbols`) | ~165 s | yes | ~170 |
| 176 ($100k floor) | ~194 s | yes, little headroom | ~195 |
| 632 (no floor) | ~695 s | **no**. More than half the universe would skip every bar. | ~650 |

**The safety property that matters most: the monitor never waits behind the scan.** Pass A's fetch order is
(1) symbols with an active plan, (2) pins, (3) the rest by liquidity rank. If Kraken starts rate-limiting
(`EGeneral:Too many requests`, temporary IP lockout), the symbols cut off are low-liquidity scan candidates.
Stops on open positions keep being checked. This is a change from today, where one symbol's fetch failure is
isolated but nothing is prioritized.

**Current-price semantics do not change.** The monitor keeps using the last closed 1h close. That matches what the
backtest harness models (close-only triggers, next-open fills). Switching to the live ticker price for stops would
make stops trigger intrabar, which is a strategy change and would need its own backtest. It's flagged, not done
silently.

Other operational effects:
- **Audit log volume:** one line per symbol per **scan** (bar close) instead of per 300 s tick. Between bar closes
  nothing the scanner sees can change, so "why didn't it trade at 14:32" still has an exact answer: the 14:00
  bar-close line. Plus one summary line per tick (`kind: "cycle"`, with monitor actions and fetch-budget stats).
  At N=150 that's about 3,600 lines/day, vs 4,608/day at N=16 today. *This changes a property documented in the
  README and needs the user's OK. See Open Questions.*
- **Telegram volume** grows with the universe. Existing caps (`max_new_entries_per_window: 5` per 120 min) bound
  the number of BUY messages.
- **VM memory:** 150 × (200 × 1h + 60 × 1d) bars with indicator columns is tens of MB. That fits even on the
  1 GB `E2.1.Micro` fallback named in `deploy/README.md`.

### 4.4 Validation (5 gates, same discipline)

New datasets. **Do not overwrite** `data/` or `data_long/`. The baseline must remain reproducible.
- `kraken_720_universe`: Kraken 1d for every USD base, including `cancel_only`. About 680 calls, about 13 min,
  one-off.
- `long_universe`: Coinbase Exchange 1d for every Kraken-universe base that Coinbase also lists. Same fetcher and
  pagination as `fetch_coinbase_long.py`.

Harness rules. Each one exists because it removes a known bias:
1. **Point-in-time universe.** At bar *t*, a symbol is eligible only if it has at least `min_listing_age_days` of
   history before *t* and its trailing-30-day median `close × volume` clears the floor, with the same hysteresis.
   Using today's ticker volume to choose the backtest universe is look-ahead. On `long_universe`, Coinbase
   volumes aren't Kraken volumes, so eligibility is **rank-based top-N by trailing dollar volume**, with N set to
   the live universe size. That's an approximation, documented as such in the report.
2. **Ranking parity.** `simulate_portfolio` processes each bar's signals in `src.signal_engine.rank_signals`
   order. It imports that function and does not reimplement it.
3. **Tiered slippage.** The flat 5 bp in `COST_PRESETS["kraken_T3_taker"]` was set for 16 majors. Tiers, set a
   priori: median quote volume >= $10M: 5 bp; $1M-$10M: 20 bp; < $1M: 50 bp. Calibrate once against real spreads
   (one `fetch_order_book` per universe symbol, about 150 calls) **before** any gate run, then freeze the tiers.
4. **Survivorship caveat stays in the report.** Including `cancel_only` pairs helps, but coins delisted more than
   ~2 years ago are gone from both sources.

**Promotion criteria for `universe.mode: discover`:**
- (a) the same 5 gates, at `kraken_T3_taker` + tiered slippage, on both universe datasets, under the same
  pass/fail rule the Routine applies to parameter variants today (§7);
- (b) **no regression** against the static-16 baseline re-run on the **same harness version** (ranking + tiered
  slippage). A comparison across harness versions is invalid;
- (c) a **tier decomposition** in the report: the original 16 vs the new symbols, each with trade count, avg R,
  and bootstrap CI. If the expansion only passes because the majors carry it and the added symbols have
  avg R <= 0, the expansion adds risk without edge. That's a FAIL for the expansion even if the aggregate passes;
- (d) at most two floor candidates ($1M, $100k), each recorded as its own `new_strategy`-kind candidate in
  `scripts/backtest_summary.json`.

Note: more symbols means more trades, which makes the `>=100 trades` gate and the CI gate on `kraken_720` easier
to pass. A larger sample is legitimate evidence **only** under rule 1. Without point-in-time selection it's
inflation. That's why rule 1 is mandatory.

**Ranking-only interim step (Phase 1c).** Introducing `rank_signals` changes behavior even at N=16 (today's
order is config order). Gate it alone on the existing `data/` and `data_long/` before the universe grows, so the
two changes aren't confounded.

### 4.5 Risks and uncertainty

- **Evidence leans against the expansion.** The playbook's sources found small caps revert and more coins lowered
  Sharpe. A FAIL here is a plausible, legitimate outcome, not a harness bug.
- **Correlated bursts get more frequent.** 2026-09-29 (11 symbols entered on one market-wide pullback) happened at
  N=16. `max_total_exposure_pct` and `max_new_entries_per_window` still bind, and ranking now decides who gets
  the slots. A correlation-cluster cap (playbook Layer 4) is a possible Risk Officer follow-up, not part of this
  design.
- **Dust fills.** The config comment already notes there's no minimum order/notional in the risk engine. More
  small caps make dust fills more likely. Kraken's `limits.cost.min` / `limits.amount.min` are available per
  pair (e.g. SHIB/USD `amount.min` = 770,000). Recommend a separate Risk Officer ticket (Opus) for
  `risk.min_order_quote_usd`. It's **required before any live mode**, since `broker_live.py` would reject
  below-minimum orders.
- **Manual-trading venue mismatch.** XMR, XAUT and FARTCOIN were removed on 2026-09-27 because the user couldn't
  trade them on Revolut X. A Kraken-wide universe will produce many signals for coins Revolut X doesn't list. See
  Open Questions.
- **Quote migration.** Today's 16 symbols are `/USDT`. With `prefer_quote: USD`, new plans use `/USD`. Existing
  `/USDT` plans are monitored to completion as `source="held"`. The per-base one-plan rule stops a double
  position. Per-symbol cooldowns don't carry across quotes, which is acceptable.
- **Ticker collisions and duplicate assets** (wrapped BTC, tokenized gold) are only caught as well as
  `exclude_bases` covers them. Review the list at every universe refresh that adds a new base. The refresh log
  lists newly added bases explicitly.
- **Harness runtime grows about linearly with symbols** (about 10x). The Daily Backtester Routine's time budget may
  not fit. `hyperopt_param.py` already caches signals across iterations. Extend that to a disk cache keyed by
  (dataset, scanner/signal config hash).

### 4.6 Operational acceptance after promotion (paper)

This is not a strategy gate. It's a check that the plumbing holds before Phase 2 adds more fetches. 14 days of
paper with: zero "held symbol not priced" events; p95 bar-close pass under 240 s; rate-limit errors under 1% of
calls; zero orphaned plans (a PENDING/OPEN record whose symbol wasn't priced in a cycle).

---

## 5. Phase 2: multi-timeframe confluence

### 5.1 Stage design

`src/mtf_confluence.py` replaces `src/htf_filter.py` in context-gate slot ii. Pure core plus a fetch-backed wrapper,
the same split as today's `htf_filter`.

- **Per-timeframe bias** for each configured TF: `+1` if fast EMA > slow EMA **and** close > slow EMA on the last
  *closed* bar; `-1` if both are reversed; otherwise `0`. Uses `indicators.fast_ema`/`slow_ema` and the existing
  `src.indicators.ema`. No new indicator. The playbook warns that stacked or fancy filters cost more than they
  help.
- **Agreement score** = Σ wₜ·biasₜ / Σ wₜ, in [-1, 1].
- **Decision:** pass iff every `required` TF has bias +1, **and** the score is at least `min_agreement`, **and**
  no `veto_if_bearish` TF has bias -1.
- **Lower-timeframe role:** 15m is a *timing* veto only. It can block an entry into an active 15m down-move. It is
  never required to agree, and **the scanner never runs on 15m**. So Phase 2 cannot add any trades, including
  fee-heavy 15m ones. It can only remove trades. That answers the playbook's "faster timeframes lose more"
  concern by construction.
- **The scanner still runs on `market.timeframe` only.** Running pattern detectors per timeframe would multiply
  candidates and introduce a new contract (cross-TF candidate merging). That's a different, larger design. It is
  deliberately out of scope.

```yaml
mtf_confluence:            # replaces htf_filter; htf_filter.enabled stays until Phase 2c retires it
  enabled: false
  base_timeframe: "1h"     # must equal market.timeframe (validated at load)
  timeframes:
    "1d":  {weight: 0.4, required: true}                         # = today's htf_filter semantics
    "4h":  {weight: 0.3, required: false}
    "1h":  {weight: 0.2, required: false}
    "15m": {weight: 0.1, required: false, veto_if_bearish: true}
  min_agreement: 0.5
  history_bars: 60
  max_confirm_fetches_per_cycle: 60   # beyond this, lower-ranked signals fail CLOSED ("unconfirmed: fetch budget")
```

Pre-registered values, chosen before seeing any result. With these numbers: 1d up + 4h up = 0.7, pass.
1d up + 4h flat + 1h up = 0.6, pass. 1d up + 4h down + 1h up + 15m up = 0.4, fail. 1d not up = fail
(identical to today). The config has five free numbers. That's a curve-fitting surface, so this exact config is
tested once. Later variants go through the Routine as parameter variants on **both** datasets, never chosen by
eye on one.

**Equivalence test before retirement:** configured as `{"1d": {weight: 1, required: true}}` with
`min_agreement: 1.0`, `mtf_confluence` must make **identical decisions** to `htf_filter` on the existing
`tests/test_htf_filter.py` fixtures and on a replay of `data_long`. Only then is `htf_filter.py` deleted (2c).

### 5.2 Contracts

`Candidate.timeframe`, `Signal.timeframe`, and `Signal.context_gates["mtf"]`. Per-TF bias, weights, score and each
TF's `data_as_of` all go in `details`, so the audit log shows exactly which timeframe blocked a signal. See §3.

### 5.3 Data fetch, consistent with Phase 1

- 1d: **already in the cache** from Phase 1's daily liquidity fetch. Zero extra calls.
- 4h and 15m (and 1h, already the base): fetched **on demand, only for symbols whose bullish signal reached
  slot ii**, in Phase 1 rank order, then cached until that TF's next bar close. This generalizes `htf_filter`'s
  per-day `_cache`.
- Expected cost: about 2 extra calls per surviving signal. At a typical handful of signals per bar that's well
  under 30 calls/h. **Worst case** is a market-wide event with 50 simultaneous signals: about 100 calls, about
  110 s, capped by `max_confirm_fetches_per_cycle`. Signals past the cap fail closed, and ranking decides who
  gets the budget, so the monitor is never starved.
- **Don't fetch 15m/4h universe-wide.** That would be about 600 calls per 15 minutes at N=150 and would break
  §4.3's budget.

### 5.4 Validation: needs a new intraday dataset and a re-baselined live strategy first

**Phase 2.0 is a hard prerequisite that may surface bad news:**
1. **Intraday dataset.** Kraken can't supply it (§1: 720-bar cap, `since` doesn't reach back). Source: Coinbase
   Exchange via the existing `fetch_coinbase_long.py` pagination. Fetch **15m only** and derive 1h/4h/1d by
   resampling, so all four timeframes are consistent by construction. Coinbase has no native 4h; resample aligned
   to 00:00 UTC, and confirm that matches Kraken's 4h bar boundaries. Size: 16 majors × up to 11 years ≈ 6M
   rows, about 20k paginated requests. Store it **gitignored as parquet** (backtest-only dependency in
   `strategy_pack_run/requirements-backtest.txt`), not as CSV in git. If Phase 1 is promoted, a universe-wide
   intraday set multiplies this about 10x. See Open Questions.
2. **Harness generalization.** `TF_SECONDS`, `PPY_1D`, `load_dir`'s `*_1d.csv` glob, and `SCAN_TAIL_BARS`
   become timeframe-aware. Per-bar Python replay at 1h is about 24x more bars than daily, so the Phase 1 signal
   cache becomes mandatory.
3. **Re-baseline the current live strategy at 1h, with no MTF change.** Every gate result so far was on daily
   bars. At 1h, `risk.cooldown_minutes` and `max_daily_loss_pct` become binding for the first time, as
   `run_live_bot_report.py`'s LIMITATIONS section predicts. **If the live strategy fails the gates at its own
   live resolution, stop and bring it to the user before building MTF on top of it.** That finding outranks
   Phase 2.

**Look-ahead hazard, the classic MTF backtest bug:** at a 1h close *t*, the harness may use only 4h/1d bars whose
**close time is at or before *t***. The forming 4h/1d bar is excluded, the same rule `fetch_ohlcv_df` applies
live. This needs its own truncation-invariance test, modeled on
`strategy_pack_run/test_pack.py::test_truncation_no_lookahead`: decisions at *t* must be identical whether or not
the data after *t* exists.

**Promotion:** 5 gates against the **1h re-baseline** on the same harness version (§7). Plus: the trade-count
reduction is reported, because MTF can only remove trades. On a `kraken_720`-sized window, removing trades can
push the `>=100 trades` gate to FAIL, and that has to be read as "not enough evidence", not as "MTF is bad".

### 5.5 Risks

- The 1h re-baseline could fail (above). That's the biggest schedule risk in the whole redesign.
- **Overlap with what exists.** 1d bias = today's `htf_filter`. 1h bias overlaps the scanner's own EMA-based
  detectors. The net new information is mostly 4h plus the 15m timing veto, and the backtest may show it adds
  nothing. That's an acceptable result.
- Coinbase vs Kraken price and bar-boundary differences make the intraday backtest a model of the logic, not a
  replay of Kraken fills. Same caveat class as `long` today.
- `export_dashboard_data.py:429` reads `daily_trend_confirmed`. The retirement PR updates it (§3).

---

## 6. Phase 3: sentiment (Fear & Greed + CryptoPanic), plain code, free tier

### 6.1 Modules

| File | What | Owner |
|---|---|---|
| `src/sentiment/fear_greed.py` | Client for `https://api.alternative.me/fng/` (no key). Fetch once a day, cache, `data_as_of` on every reading. | Platform Engineer |
| `src/sentiment/cryptopanic.py` | Client (token from env). **One batched call per cycle at most** (`currencies=` comma list of the coins that reached slot iii), cached `cache_minutes`. Mapping table from Kraken base to CryptoPanic currency code. Unmapped coins return "no data". | Platform Engineer |
| `src/sentiment/gates.py` | Two pure gate functions returning `GateVerdict`. All scoring arithmetic lives here. | Market Analyst |
| `src/sentiment/archive.py` | Appends every raw CryptoPanic post seen (id, published_at, currencies, votes) and every F&G reading to `logs/sentiment_archive/YYYY-MM.jsonl`. **Excluded from `src/log_retention.py` pruning.** This archive *is* the future backtest dataset. | Platform Engineer |
| `scripts/validate_invariants.py` | `+` no module under `src/sentiment/`, `src/mtf_confluence.py`, or `src/context_gates.py` imports `anthropic` or `src.claude_advisor`. `+` `sentiment.*.mode` is in {`shadow`, `enforce`}. This turns "not an LLM call" from a convention into a mechanical CI check. | QA/Tester |

```yaml
sentiment:
  fear_greed:
    enabled: false
    mode: shadow            # shadow (log would-veto, never block) | enforce
    block_at_or_above: 80   # PRE-REGISTERED hypothesis H1, see 6.2. Not tuned.
    lag_days: 1             # decide on the value published >= 1 full day before the bar close (look-ahead safety)
    stale_after_hours: 36
    on_unavailable: pass    # see 6.6 and Open Questions
  cryptopanic:
    enabled: false
    mode: shadow            # stays shadow until 6.5's criteria are met AND the user signs off. Never Routine-promoted.
    lookback_hours: 24
    min_posts: 3            # fewer posts than this = "no signal", gate passes
    veto_at_or_below: -0.3
    cache_minutes: 30
    max_calls_per_hour: 4   # placeholder until the user confirms the free tier's real quota
    on_unavailable: pass
```

### 6.2 Fear & Greed (slot i): can go through the full 5 gates

- **Signal:** one market-wide value per UTC day. **Gate (H1):** veto new long entries when F&G (lagged 1 day) is
  80 or higher ("Extreme Greed").
- **Why H1 and not "block Extreme Fear":** extreme-fear periods mostly coincide with daily downtrends that the
  1d bias (`htf_filter` / MTF `required: true`) already blocks, so a fear rule would mostly duplicate an existing
  filter. Extreme greed is the regime the current filters **allow**: late-trend euphoria and market-wide
  correlated entries, the 2026-09-29 burst pattern. That's an a-priori argument, not evidence. The opposite rule
  is not tested alongside it unless the user asks. Every extra hypothesis tested at the 90th-percentile placebo
  threshold raises the chance of a false pass.
- **History:** alternative.me documents a full daily history via `?limit=0`, going back to 2018-02-01. This wasn't
  fetched from this session (proxy 403), so it has to be confirmed on the first fetch. Commit a frozen snapshot
  to `strategy_pack_run/data_sentiment/fng_daily.csv` (about 3k rows) so the Routine doesn't need network access
  to alternative.me.
- **Backtest windows:** `kraken_720` (2025-04 → 2026-10) is fully covered. `long` must be **truncated to
  2018-02-01 onward for both the candidate and the baseline** so the comparison is like for like. That drops
  2015-2018 for BTC/ETH/LTC/BCH. Most other coins start in 2019-2021 anyway.
- **Caveat that the gates don't fully capture:** F&G extremes cluster into maybe 10-20 multi-week episodes since
  2018. The effective sample is episodes, not trades. The placebo gate partly covers this. The report should also
  state the number of distinct extreme-greed episodes the result depends on.
- **Promotion:** the 5 gates (§7) against the then-live baseline. A pass can go straight to `mode: enforce` with
  the user's sign-off, the same as any `new_strategy`. A shadow period beforehand is optional, not required.

### 6.3 CryptoPanic (slot iii): what can and can't be known

What the design assumes about the free tier. **The user has to confirm all of it on signup** (Open Questions):
every request needs an `auth_token`; the free plan has a small request quota and returns **recent** posts only;
deep historical archives are a paid feature; the sentiment it exposes is **community votes / bullish-bearish
tags**, not an NLP score. If any of that is wrong in the direction of "more available", the design gets easier
(§6.5 step 4). If the free tier exposes even less, for example no votes, use the fallback score below.

- **Score** for coin *c* at bar close *t*, over posts tagged *c* published in (*t* − `lookback_hours`, *t*]:
  `score = (Σpositive − Σnegative) / (Σpositive + Σnegative + 5)`. The +5 prior shrinks thinly-voted coins
  toward 0. **Fallback** if votes aren't on the free tier: use CryptoPanic's own bullish/bearish tag counts,
  `(n_bull − n_bear) / (n_bull + n_bear + 5)`.
- **Gate:** veto iff `n_posts ≥ min_posts` **and** `score ≤ veto_at_or_below`. **No news means pass.** Most of a
  150-coin universe has zero headlines on any given day. Treating no news as a veto would quietly become
  "only trade coins in the news", which would be a huge, unvalidated universe filter.

### 6.4 Can the 5-gate methodology validate CryptoPanic? **No, not on the free tier, and not right away.**

The 5 gates need years of point-in-time history across several regimes. If the free tier really serves only
recent posts, that history doesn't exist for this project. Backtesting against posts fetched *today* about the
past would be look-ahead: vote counts keep changing after publication, and deleted or edited posts disappear.
This doc doesn't lower the bar to work around that. It proposes a different, explicitly weaker validation path
(§6.5), keeps CryptoPanic in `shadow` until that path passes **and** the user signs off, and requires a real
5-gate re-validation once our own archive is deep enough.

### 6.5 Alternative validation path: pre-registered shadow observation window

Freeze these criteria in this doc before observation starts. Changing them after seeing data restarts the window.

1. **Shadow mode (Phase 3b).** For every bullish signal reaching slot iii, compute the verdict and record it in
   `Signal.context_gates["cryptopanic"]` with `mode: "shadow"`, `passed: true`, and
   `details.would_veto: bool`. It flows into the plan rationale and the audit log, but **nothing is blocked**.
   Because nothing is blocked, the would-veto trades actually get taken in paper and produce **real paper
   outcomes**. This is better than `src/risk_counterfactual.py`'s gross, no-cost replay.
2. **Window:** at least 90 calendar days **and** at least 30 closed would-veto trades **and** at least 100 closed
   scored trades, whichever comes last. At recent paper rates (about 24 fills in the 5 days to 2026-10-05) and an
   unknown veto rate, this could take 3-9 months. That's the honest cost.
3. **Promotion criteria. All must hold**, computed on realized net R from `logs/trades.csv` joined to plan
   rationale:
   - (a) 95% bootstrap CI on **mean R(would-pass) − mean R(would-veto)** lies entirely above 0;
   - (b) **random-veto placebo:** veto the same number of trades at random from the scored set, 1,000 times. The
     real gate's improvement in total R must rank at or above the 90th percentile. This mirrors the existing
     placebo gate;
   - (c) applying the veto does not reduce **total** realized R (removing trades must not cost more than it
     saves);
   - (d) robustness: (a)'s sign holds with each single coin removed and with each single calendar week removed,
     so one headline event can't carry the result;
   - (e) the user signs off in writing (PR approval). **Never auto-promoted by the Routine.**
4. **Re-validation with full gates later.** Once `logs/sentiment_archive/` covers at least 18 months, and ideally
   includes a regime change, run the full 5 gates on the archived window, using only vote counts as recorded at
   archive time. If an enforced gate fails then, it goes back to shadow.
5. **What this path does not give:** multi-regime evidence, a long out-of-sample split, or a 200-sim time-shift
   placebo over years. A shadow pass is weaker evidence than a 5-gate pass and should be read that way. The most
   likely result of the window is **no detectable effect**, in which case the gate stays in shadow or off. That's
   a fine outcome.

**Timing recommendation (user's call):** the archive collector (`archive.py`, read-only, no trade impact) could
start **now**, ahead of Phases 1-2, once the user has a CryptoPanic token. Every month it runs earlier is a month
less waiting in §6.5 and a month more history for step 4. This doesn't change the phase order. It only starts
the clock.

### 6.6 Failure behavior: an explicit exception to "fail closed", needs the user's decision

Every other gate in this pipeline fails closed. For sentiment, this doc recommends **`on_unavailable: pass`**
(gate skipped, loudly logged, surfaced on the dashboard) for a specific, documented reason. On 2026-09-27 the
advisor failed closed on a **missing API key** and silently vetoed every trade for days (`config/config.yaml`,
`advisor.enabled` comment). A free-tier sentiment source with quota exhaustion, outages or an expired token would
recreate exactly that failure. Sentiment is an *extra* filter on a strategy that is validated without it, so
skipping it degrades to the validated baseline, not to something unvalidated. **MTF keeps failing closed**,
because it replaces `htf_filter`, which is part of the validated baseline. This is listed as an open question
rather than decided here.

---

## 7. Validation discipline across phases

- **Promotion rule:** each phase uses the same 5 gates and the same pass/fail rule the Daily Backtester Routine
  applies to `parameter_variant` candidates today. That rule lives in the Routine's stored prompt, which is not
  in this repo. The record so far (18 candidates, all pass `long`, all fail `kraken_720`, none promoted) suggests
  it requires both datasets. **Confirm before Phase 1's gate run** (Open Questions). Each phase is tested against
  the **then-live** baseline. If Phase 1 fails, Phase 2 is gated on the static-16 universe. The phases are
  independently backtestable by construction.
- **Harness versioning:** add `harness_version` (additive) to `candidates_schema` in
  `scripts/backtest_summary.json`. Add the new dataset ids (`kraken_720_universe`, `long_universe`,
  `long_15m`, ...) to `datasets`. Results from different harness versions are never compared.
  `export_dashboard_data.py` must tolerate unknown dataset ids.
- **Auto-promotion guardrail:** the Routine writes `config_key`/`config_value` verbatim into
  `config/config.yaml` for passing parameter variants. Enabling a stage (`universe.mode`, `*.enabled`,
  `sentiment.*.mode`) must **never** be a mechanical promotion. It's always a human-reviewed `new_strategy`-kind
  change. Any `sentiment.cryptopanic.*` key is excluded from auto-promotion entirely, because it can't be
  backtested. This needs a denylist in the Routine's prompt and/or in `hyperopt_param.py` (which today only
  allows `trade_planner.*` and `risk.*`, and should keep that restriction).
- **Shadow is not a backdoor.** `mode: shadow` exists **only** for inputs that can't be backtested (CryptoPanic),
  and optionally as a live corroboration step after a 5-gate pass (F&G). It is never a substitute for a gate run
  that *could* be done.

---

## 8. Sequencing

```
P0  (anytime; no strategy impact)
    README stage-2b/htf_filter drift fix · CLAUDE.md roster drift (§9) · F&G history snapshot committed
    [optional, needs CRYPTOPANIC token] sentiment archive collector, logging only

P1a market_data cache + bar-close scan + export reads snapshot ........ parity test: identical decisions vs today on replayed fixtures
P1b universe.py in `static` mode + per-base one-plan rule + RiskEngine.set_symbol_whitelist + held-symbol monitoring
P1c three-pass cycle + rank_signals ...................................... 5 gates on EXISTING data/, data_long/ (ranking alone)
P1d universe datasets + point-in-time harness + tiered slippage (calibrated, then frozen)
P1e 5-gate run: discover @ $1M, discover @ $100k ......................... USER DECISION
P1f flip universe.mode: discover -> 14-day ops acceptance (§4.6)

P2.0 15m Coinbase dataset + timeframe-aware harness + 1h RE-BASELINE of live strategy ... USER CHECKPOINT if it fails
     (data/harness work can start in parallel with P1d; the re-baseline must use whatever P1 outcome is live)
P2a context_gates.py interface + Candidate/Signal fields + mtf_confluence.py (enabled: false) + htf-equivalence test
P2b 5-gate run vs the 1h re-baseline ........................................ USER DECISION
P2c enable MTF; delete htf_filter.py; exporter + README updated in the same PR

P3a F&G gate (slot i) -> 5 gates on F&G-covered windows ........................ USER DECISION -> enforce
P3b CryptoPanic gate in shadow (needs token; archive already running if P0 was done)
P3c >= 90-day window -> §6.5 criteria -> USER SIGN-OFF -> enforce; full 5-gate re-validation at >= 18 months of archive
```

Hard dependencies:
- P1a comes before anything that adds exchange calls (all of P2 and P3). It's the budget everything else spends.
- P1c's ranking comes before P2's fetch cap and P3's quota. Both caps decide who gets budget in rank order.
- P2a's `GateVerdict` interface comes before P3.
- P2.0's dataset comes before P2b.
- Contract changes (§3) land in P2a as one additive PR, with every constructor, consumer and test touched in the
  same change and a full `pytest` run.

---

## 9. Ownership: roster drift to fix (CLAUDE.md)

Already drifted on `main`, before any of this:
- `src/htf_filter.py` and `src/bot.py` aren't in any CLAUDE.md role's "Owns" column.
- The README architecture diagram doesn't show stage 2b at all.
- `strategy_pack_run/` and `scripts/` (the backtest harness, `run_live.py`, the exporter) have no owner.

Proposed new rows/assignments: `src/universe.py`, `src/market_data.py`, `src/sentiment/{fear_greed,cryptopanic,archive}.py`
go to Platform Engineer. `src/mtf_confluence.py`, `src/sentiment/gates.py` and `rank_signals` go to Market
Analyst. `src/context_gates.py` (the interface) and `src/bot.py` orchestration go to Architect, with an Opus pass
because `bot.py` drives the risk engine. `strategy_pack_run/` goes to QA/Tester, with Architect review of any
harness-semantics change. This doc doesn't edit CLAUDE.md or README. Each phase's PR updates both in the same
change, per the Architect role definition.

---

## 10. Keys and secrets the user has to set up

| Phase | Secret / access | Where | Notes |
|---|---|---|---|
| 1, 2 | **None.** Kraken and Coinbase public market data need no key. | n/a | Coinbase pagination for the 15m dataset is about 20k requests. Run it from an environment that can reach `api.exchange.coinbase.com`. |
| 3a | **None** for Fear & Greed. | n/a | Needs egress to `api.alternative.me` for live use. This sandbox got a proxy 403. The committed snapshot covers backtests. alternative.me asks for attribution if the value is shown on the dashboard. |
| 3b | **`CRYPTOPANIC_API_TOKEN`** (user registers at cryptopanic.com; free tier) | (1) the Oracle VM's `.env`. Since 2026-09-29 the bot runs there under systemd via `scripts/run_live.py`, so this is where it matters. (2) A GitHub Actions repo secret **only** if `refresh-dashboard.yml`'s manual `workflow_dispatch` runs should use it, and then it **must also be added to that workflow's `env:` block**. The `ANTHROPIC_API_KEY` precedent shows a secret existing isn't enough. (3) `.env.example` + a `Config.cryptopanic_api_token` property. | Not needed by the Daily Backtester Routine, since there's no history to backtest. With `on_unavailable: pass`, a missing token shows up as a dashboard warning, not a silent all-veto. |

No phase needs a new exchange key. No phase changes `EXCHANGE_API_*` usage or `LIVE_TRADING_CONFIRM`.

---

## 11. Open questions for the user

1. **Universe floor vs "full universe".** About 630 USD pairs exist, but only about 65 clear a $1M median daily
   volume and about 176 clear $100k. With no floor the scan can't fit the cycle (§4.3). OK to test $1M and $100k
   as the two candidates, with `max_symbols: 150`?
2. **Revolut X.** You trade these signals manually on Revolut X, and three coins were dropped on 2026-09-27
   because Revolut X lacked them. Should the universe be intersected with a Revolut X availability list you
   maintain (for example `universe.notify_only_bases`), so Telegram BUY alerts only fire for coins you can
   actually trade? Or should the paper bot trade the whole Kraken universe regardless?
3. **Audit-log granularity.** OK to change "one audit line per symbol per cycle" to "per symbol per bar-close
   scan, plus one summary line per cycle" (§4.3)? It's documented in the README as a property.
4. **Promotion rule.** The Routine's exact pass/fail rule isn't in the repo. Is it "all 5 gates on both
   `kraken_720` and `long`"? Should new stages use exactly that rule (§7)?
5. **If the live strategy fails the gates at 1h (P2.0)**, is that a stop-everything finding (fix the base
   strategy first), or do you want Phase 2 to proceed on the daily-bar evidence?
6. **Phase 2 dataset scope if Phase 1 is promoted.** A universe-wide 15m history is about 10x the 16-coin set.
   OK to validate MTF on (a) the 16 majors over 11 years plus (b) the universe over a ~2-year window, rather than
   the full universe over 11 years?
7. **F&G hypothesis.** H1 (block at Extreme Greed >= 80) is the pre-registered default. Do you want H2 (Extreme
   Fear) tested as well, knowing a second test raises the false-pass chance?
8. **Sentiment failure mode.** Accept `on_unavailable: pass` for both sentiment gates (§6.6), as a deliberate
   exception to fail-closed? Or keep fail-closed and accept that a CryptoPanic outage or quota exhaustion halts
   new entries?
9. **CryptoPanic free-tier facts to confirm on signup:** request quota (per day/month), how far back `posts`
   paginates, whether vote counts are included, and whether the ToS allows automated use in a trading tool. The
   §6.3 score and §6.5 timeline depend on all four.
10. **Start the sentiment archive collector early (P0)?** It has no trade impact, and every month it runs before
    Phase 3 shortens the shadow wait.
11. **Minimum order notional** (§4.5): OK to open a separate Risk Officer (Opus) ticket for
    `risk.min_order_quote_usd` as a Phase 1 prerequisite? The universe expansion makes dust fills much more
    common.
