# Hybrid Crypto Trading Bot

A crypto trading bot for Claude Code built as a five-stage pipeline — scanner,
signal engine, trade planner, risk engine, and a 24/7 monitor — plus a
daily-trend confirmation gate (stage 2b) between the signal engine and the
trade planner, and an optional Claude second opinion that can veto (but never
originate) a trade.
Supports any exchange [ccxt](https://github.com/ccxt/ccxt) supports, starts in
**paper trading** mode by default, and only ever places real orders once you
deliberately flip two independent safety switches.

## Architecture

```
universe               src/universe.py   (infrastructure, not a stage)
   Which symbols the bot may open NEW plans on. `universe.mode: static`
   (the only implemented mode; `discover` raises NotImplementedError) is
   exactly `market.symbols`. Every cycle the tradeable members become the risk
   engine's symbol whitelist. Symbols with an active plan or position are
   always walked too, even if they've left the universe (tagged
   `source="held"`), so they keep being monitored and priced (but never get a
   new plan, and are not scanned for new setups).
   |
   v
market data (ccxt)     src/market_data.py   (infrastructure, not a stage)
   `MarketDataCache` fetches closed-bar OHLCV per symbol and only refetches a
   symbol once a new bar of `market.timeframe` has closed; held symbols are
   fetched first. A symbol that is due but fails to fetch is skipped for the
   cycle (logged as "market data unavailable"), never served from stale data.
   After each refresh it writes `logs/market_snapshot.json` (last close +
   recent candles), which the dashboard export reads instead of calling the
   exchange itself.
   |
   v
   bar gate (only with `loop.bar_parity: true`)   src/bot.py
   Decides each symbol once per closed `market.timeframe` bar (persisted in
   `logs/bar_decisions.json`), runs every monitor check before any new entry,
   then new entries in sorted(symbol) order with room/cash as held before this
   bar's exits, optionally skipping entries from a bar older than
   `loop.max_entry_delay_minutes`. Orchestration, not a stage. Off by default.
   |
   v
1. SCANNER            src/scanner.py
   Scans OHLCV 24/7 for five pattern types: Breakout, Pullback, Momentum,
   Trend Continuation, Reversal. Reports every candidate it sees with a raw
   0-1 strength score. No news/social/on-chain feeds (see "Extending it").
   |
   v
2. SIGNAL ENGINE       src/signal_engine.py
   Drops weak candidates, drops conflicting evidence (bullish + bearish at
   once = noise), and turns agreeing candidates into one prioritized Signal
   with a confluence-boosted engine score.
   |
   v
2b. HTF FILTER         src/htf_filter.py
   Daily-trend confirmation gate. Checks the signal's direction against the
   daily chart's own trend (fast EMA vs. slow EMA on the most recent daily
   candle) — standard top-down multi-timeframe practice. A signal that
   disagrees with (or can't confirm) the daily uptrend is blocked here; like
   the risk engine and Claude advisor, this stage can only veto, never
   originate or resize a trade. Fails closed on insufficient daily history or
   a fetch error. Cached per symbol per UTC day, since daily candles only
   change once a day. With `loop.bar_parity: true` at `market.timeframe: "1d"`
   it is evaluated on the scan frame's last 150 bars instead (no fetch), the
   same window the backtest harness uses.
   |
   v
3. TRADE PLANNER       src/trade_planner.py
   Builds the full plan: entry zone, stop loss, take-profit target, and a
   separate invalidation level (where the whole thesis is wrong, not just
   where you cut losses) — plus a rule-based 1-5 star confidence score.
   |
   v
4. RISK ENGINE         src/risk_engine.py
   Position size (risk-based: sized off max_loss_per_trade_pct and the
   plan's own stop distance, tapered down as volatility rises), total
   exposure across all open positions, minimum risk/reward, daily loss
   limit, market volatility (ATR) filter, symbol whitelist (the universe's
   tradeable members), per-symbol cooldown, a global new-entry rate cap
   (`max_new_entries_per_window` per `new_entry_window_minutes`, across all
   symbols; restored from the plan store after a restart), an optional
   minimum order size (`min_order_quote_usd`, ships as 0 = off; rejects,
   never sizes up), kill switch file. Only ever rejects or shrinks a plan.
   |
   v
   [optional] CLAUDE ADVISOR   src/claude_advisor.py
   Reviews the specific plan the pipeline already built. Can only APPROVE or
   VETO — never originates a trade or changes its size. Gives its own 1-5
   star confidence rating, shown side by side with the rule-based one. Fails
   *closed* (vetoes) by default if the API call errors or times out.
   |
   v
   trade plan store (PENDING)     src/trade_plan_store.py
   |
   v
5. 24/7 MONITOR        src/monitor.py
   Every cycle, for every active plan (in or out of the current universe;
   the symbol whitelist is deliberately not consulted here): fills a PENDING
   plan once price re-enters the entry zone, discards it if price breaches
   invalidation first, and closes an OPEN position at its stop loss or
   take-profit target. Triggers use the last CLOSED bar's close (at 1d: one
   check per daily close; an intrabar move that recovers by the close does not
   trigger). This bot executes automatically — there's no separate
   human alert step (an optional Telegram message is sent for new signals,
   fills and closes if configured).
   |
   v
   paper broker (simulated) or live broker (real order, double-gated)
```

Each cycle walks the symbols once, in universe order: a symbol with an
active plan goes straight to the monitor; a symbol whose *base asset* already
has an active plan on another symbol (e.g. `BTC/USD` vs `BTC/USDT`) is
skipped (one active plan per base asset); everything else runs through stages
1-4. With `loop.bar_parity: true` (default `false`) the cycle is two passes
instead, matching how `strategy_pack_run/backtest_live_bot.py` decides a bar:
a monitor pass over every held symbol, then an entry pass over FLAT symbols
whose latest closed bar is not decided yet, in sorted(symbol) order (see
`docs/bot_bar_parity.md`). Every cycle, for every symbol, one line is written to
`logs/audit_log.jsonl` regardless of what happened — so "why didn't it trade
at 14:32" is always answerable from that file alone.

`signal_engine.rank_signals()` (a deterministic engine-score / liquidity /
symbol ordering for when exposure or rate caps leave only some slots) exists
and is unit-tested but is **not wired into `src/bot.py`** — the cycle still
processes symbols in universe (= `market.symbols`) order, or sorted(symbol)
order with `loop.bar_parity: true`. See
`docs/p1c_rank_signals_validation.md`.

This bot is **long-only spot**: a bearish signal is logged for visibility but
never acted on directly. Exits are governed entirely by an open position's own
stop loss / take-profit / invalidation levels (stage 5), which is more robust
than reacting to every bearish blip in isolation.

## Quick start

```bash
git clone <this-repo-url>
cd hybrid-crypto-trader
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: at minimum nothing is required for paper mode with the advisor
# disabled. To use the Claude advisor, set ANTHROPIC_API_KEY.

pytest                       # run the test suite
PYTHONPATH=. python scripts/validate_invariants.py   # safety-switch invariants
python scripts/run_backtest.py --symbol BTC/USDT --bars 1000
python scripts/run_paper.py  # runs continuously in paper mode (Ctrl+C to stop)
```

Or run a single cycle without the loop, useful for testing/cron:

```bash
python -m src.bot --once
```

## Configuration

All settings live in `config/config.yaml`, organized by pipeline stage.
Secrets (API keys) live only in `.env`, which is gitignored — **never commit
it**.

| Section | What it controls |
|---|---|
| `mode` | `paper` (default) or `live` |
| `exchange.id` / `exchange.testnet` | any ccxt exchange id; use its sandbox where supported |
| `market.symbols` | whitelist — the bot only ever opens new plans on these (it keeps monitoring an existing plan until it closes, even if you remove its symbol here). At most one active plan per base asset (e.g. `BTC/USD` and `BTC/USDT` can't both be held) |
| `market.timeframe` / `market.history_bars` | candle size the scanner runs on (default `1h`) and how many closed bars are fetched per symbol |
| `universe.*` | where the tradeable symbol list comes from; only `mode: static` (= `market.symbols`) is implemented and it is the only key read — the rest are reserved for a not-yet-built `discover` mode |
| `indicators` | EMA fast/slow periods, RSI period (shared by scanner + planner) |
| `scanner.*` | per-pattern thresholds — lookback windows, volume multiplier, momentum threshold, consolidation range, RSI extremes |
| `signal_engine.*` | minimum candidate strength, confluence bonus, pattern tie-break priority |
| `htf_filter.enabled` | stage 2b daily-trend confirmation gate on/off (default on) |
| `trade_planner.*` | entry zone width, stop/invalidation distance (in ATRs), minimum risk/reward, ADX trend-regime stop widening |
| `risk.*` | position sizing (`max_position_pct`, `max_loss_per_trade_pct`, `volatility_scale_floor`), total exposure cap, daily loss limit, volatility filter, per-symbol cooldown, global new-entry rate cap (`max_new_entries_per_window`, `new_entry_window_minutes`), optional minimum order size (`min_order_quote_usd`, 0 = off), kill switch file. `volatility_scale_floor` is not yet passed through by `src/bot.py`; the engine's built-in 0.5 applies |
| `advisor.*` | turn the Claude second opinion on/off (shipped **off**), model, fail-open/closed, timeout |
| `loop.poll_interval_seconds` | seconds between full cycles in continuous mode |
| `loop.bar_parity` / `loop.max_entry_delay_minutes` | backtest-parity cycle rules P1-P5 (one decision per symbol per closed bar, monitor before entries, sorted entry order, pre-exit room, 1d htf on the scan frame) and the entry-staleness guard; both off by default (`false` / `null` = today's behaviour). See `docs/bot_bar_parity.md` |
| `logging.*` | audit log / trade log / plan store / market snapshot paths, the bar-parity decision ledger (`bar_decisions_path`) and optional closed-bar archive (`closed_bars_dir`), and `retention_days` pruning (used by `scripts/run_live.py` only) |

## Going live — read this before you do it

Live trading requires **both**:

1. `mode: live` in `config/config.yaml`
2. the environment variable `LIVE_TRADING_CONFIRM=YES-I-UNDERSTAND-THE-RISK` in `.env`

Missing either one raises an error rather than silently trading or silently
not trading. This is deliberate friction, not a bug.

Before you do this:

- **Backtest and paper-trade the strategy first**, for longer than feels
  necessary, and expect to tune `scanner`/`trade_planner` thresholds — the
  shipped defaults are a reasonable starting point, not a tuned edge. Past
  performance in a backtest is not a guarantee of anything.
- **Scope your exchange API key to trading only** — disable withdrawal
  permissions on the key you give this bot.
- **Start with a small amount of capital** you are fully prepared to lose.
  Crypto markets are volatile and this bot, like any trading system, can lose
  money — including all of it.
- **Read the code**, especially `src/broker_live.py` and `src/risk_engine.py`,
  before trusting it with real funds. This is a starting point you should
  understand and adapt, not a black box.
- This is not financial advice, and nothing in this repo (including the
  Claude advisor's output or the confidence star ratings) constitutes a
  recommendation to buy or sell any asset.
- If you're trading from Ireland, crypto gains are generally subject to
  Capital Gains Tax and there may be reporting obligations — check current
  Revenue guidance or talk to a tax advisor; this project does not handle
  tax record-keeping for you (the CSV trade log in `logs/trades.csv` is a
  starting point for your own records, not a substitute for advice).

## Project layout

```
config/config.yaml           settings for every pipeline stage
src/universe.py               which symbols are tradeable (static mode = market.symbols) + held-symbol tagging
src/market_data.py            bar-close-aligned OHLCV cache + logs/market_snapshot.json writer
src/exchange.py               ccxt wrapper (market data + live order placement)
src/indicators.py             EMA / RSI / ATR
src/scanner.py                stage 1: Breakout/Pullback/Momentum/Trend/Reversal detectors
src/signal_engine.py          stage 2: filters + prioritizes candidates into one signal (+ rank_signals, not yet wired in)
src/htf_filter.py             stage 2b: daily-trend confirmation gate (veto only, like risk_engine/claude_advisor)
src/trade_planner.py          stage 3: entry zone/stop/target/invalidation/confidence
src/risk_engine.py            stage 4: position sizing, exposure, daily loss, volatility, cooldown, new-entry rate cap, min order size
src/claude_advisor.py         stage 4b: optional Claude veto + independent confidence score
src/trade_plan_store.py       PENDING/OPEN/CLOSED lifecycle for every plan (current + history)
src/monitor.py                stage 5: fills/discards/exits, pure decision logic
src/portfolio.py              cash/position/PnL bookkeeping shared by both brokers
src/broker_paper.py           simulated fills, no exchange credentials needed
src/broker_live.py            real order placement, gated by two independent switches
src/audit.py                  append-only JSONL log of every decision, every cycle
src/backtest.py               replays the pattern/planning pipeline over historical OHLCV (simplified: no advisor, no risk-engine sizing)
src/pnl.py, src/risk_analytics.py, src/risk_counterfactual.py
                              read-only reporting on realized trades / rejected plans; never feed back into the risk engine
src/heartbeat.py, src/notifier.py, src/log_retention.py
                              optional dead-man's-switch ping, Telegram messages, log pruning
src/bot.py                    wires every stage together; the main loop (+ the optional bar-parity cycle: bar gate, decision ledger, closed-bar archive)
scripts/run_paper.py          convenience entry point, refuses to run if mode: live
scripts/run_live.py           always-on host loop: cycle + dashboard export/push + log pruning + heartbeat (paper mode only)
scripts/run_backtest.py       CLI backtest runner
scripts/export_dashboard_data.py  writes web/data.json from the bot's logs + market snapshot
scripts/validate_invariants.py    mechanical check of the safety non-negotiables (also run by CI)
scripts/reset_portfolio.py, diagnose_recent_closes.py, replay_risk_rejections.py
                              paper-portfolio reset (archives logs) and read-only diagnostics
strategy_pack_run/            standalone real-pipeline backtest / strategy-evaluation harness and its reports (own README)
research/v2_signals.py        scaffolding for a prospective v2 strategy; not wired into src/
docs/                         playbook, design docs and validation write-ups; start at docs/STATUS.md
deploy/                       systemd unit + notes for running scripts/run_live.py on an always-on host
web/                          static dashboard (see below)
tests/                        pytest suite covering every module above
```

## Dashboard

`web/` is a static, framework-free dashboard: an "office" view of the pipeline
stages (styled after a trading-desk swarm-office layout, one desk per role,
colored by model tier), a live trade-plan chart using TradingView's
open-source [lightweight-charts](https://github.com/tradingview/lightweight-charts)
library (vendored in `web/vendor/`, no external CDN or API key needed), and
data tables for open positions, pending plans, recent scanner activity, and
recent risk decisions. Prices and chart candles come from
`logs/market_snapshot.json` (written by the bot's `MarketDataCache` each cycle),
so the export makes no exchange calls of its own.

It's a **snapshot** dashboard, not a live-streaming one: it reads
`web/data.json`, which you regenerate from the bot's own log files —

```bash
python scripts/export_dashboard_data.py   # writes web/data.json
```

— and it only updates for visitors once that new `data.json` is deployed.
Re-run the export script (a cron job, or right before you push) and redeploy
to refresh it. A sample `web/data.json` ships in the repo so the dashboard
isn't blank before you've run the bot yourself.

For near-zero-gap coverage — the bot cycling continuously and the dashboard
updating after every cycle, rather than on whatever a scheduler's next tick
happens to be — see `deploy/README.md` for running `scripts/run_live.py` as
a systemd service on an always-on host.

To preview it locally (a plain `file://` open won't work because the page
`fetch()`es `data.json`):

```bash
cd web && python3 -m http.server 8080
# open http://localhost:8080
```

### Deploying to Cloudflare Pages (GitHub auto-deploy)

1. Push this repo to GitHub (see the note in your original setup, or
   `gh repo create ... --push`).
2. In the Cloudflare dashboard: **Workers & Pages → Create → Pages → Connect
   to Git**, pick this repository.
3. Build settings — this is a static site, so:
   - Framework preset: `None`
   - Build command: *(leave empty)*
   - Build output directory: `web`
4. Deploy. Every push to your default branch rebuilds and redeploys
   automatically, including whenever you commit a freshly regenerated
   `web/data.json`.

`web/_headers` tells Cloudflare not to cache `data.json`, so a redeploy is
visible immediately rather than waiting out a cache TTL.

If you'd rather deploy from the command line instead of GitHub integration,
`wrangler pages deploy web --project-name=hybrid-crypto-trader` works too
(requires `wrangler` authenticated with your Cloudflare account).

## Running this repo like a trading brokerage (CLAUDE.md)

`CLAUDE.md` sets up Claude Code to work on this repo the way a small trading
brokerage would run its engineering org: every module has an owning role
(Market Analyst, Trade Strategist, Risk Officer, Compliance Reviewer,
Execution Trader, Platform Engineer, QA/Tester, Architect), each with a
matching subagent definition in `.claude/agents/`, a default model tier
(Haiku/Sonnet/Opus), and a complexity rubric for when to escalate. Changes to
`src/risk_engine.py` or `src/broker_live.py` always get an Opus-level pass
regardless of how small they look — see `CLAUDE.md` for the full rationale
and the non-negotiables list.

## Extending it

- Add a new pattern by writing a `_detect_*` function in `src/scanner.py` that
  returns a `Candidate` — the signal engine and trade planner don't need to
  know anything pattern-specific beyond the `details` dict key each pattern's
  entry level is stored under (see `_LEVEL_KEYS` in `src/trade_planner.py`).
- Give the Claude advisor more context (news headlines, on-chain data,
  funding rates) by extending the `rationale`/`portfolio_context` dicts built
  in `src/bot.py` before calling `claude_advisor.review_trade_plan`. Actual
  news/social/on-chain **data sources** aren't wired up yet — the scanner
  today only sees OHLCV+volume from your exchange — so this is a real gap to
  fill, not a switch to flip.
- Add exchanges by changing `exchange.id` — anything `ccxt` supports works
  as long as it implements `fetch_ohlcv`, `fetch_ticker`, and (for live mode)
  `create_order`.

## License

MIT — see `LICENSE`.
