# Hybrid Crypto Trading Bot

A crypto trading bot for Claude Code built as a five-stage pipeline — scanner,
signal engine, trade planner, risk engine, and a 24/7 monitor — with an
optional Claude second opinion that can veto (but never originate) a trade.
Supports any exchange [ccxt](https://github.com/ccxt/ccxt) supports, starts in
**paper trading** mode by default, and only ever places real orders once you
deliberately flip two independent safety switches.

## Architecture

```
market data (ccxt)
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
3. TRADE PLANNER       src/trade_planner.py
   Builds the full plan: entry zone, stop loss, take-profit target, and a
   separate invalidation level (where the whole thesis is wrong, not just
   where you cut losses) — plus a rule-based 1-5 star confidence score.
   |
   v
4. RISK ENGINE         src/risk_engine.py
   Position size (risk-based: sized off max_loss_per_trade_pct and the
   plan's own stop distance), total exposure across all open positions,
   minimum risk/reward, daily loss limit, market volatility (ATR) filter,
   symbol whitelist, per-symbol cooldown, kill switch file.
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
   Every cycle, for every active plan: fills a PENDING plan once price
   re-enters the entry zone, discards it if price breaches invalidation
   first, and closes an OPEN position at its stop loss or take-profit
   target. This bot executes automatically — there's no separate human
   alert step.
   |
   v
   paper broker (simulated) or live broker (real order, double-gated)
```

Every cycle, for every symbol, one line is written to `logs/audit_log.jsonl`
regardless of what happened — so "why didn't it trade at 14:32" is always
answerable from that file alone.

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
| `market.symbols` | whitelist — the bot only ever looks at / trades these |
| `indicators` | EMA fast/slow periods, RSI period (shared by scanner + planner) |
| `scanner.*` | per-pattern thresholds — lookback windows, volume multiplier, momentum threshold, consolidation range, RSI extremes |
| `signal_engine.*` | minimum candidate strength, confluence bonus, pattern tie-break priority |
| `trade_planner.*` | entry zone width, stop/invalidation distance (in ATRs), minimum risk/reward |
| `risk.*` | position sizing, total exposure cap, max loss per trade, daily loss limit, volatility filter, cooldown, kill switch file |
| `advisor.*` | turn the Claude second opinion on/off, model, fail-open/closed, timeout |

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
src/exchange.py               ccxt wrapper (market data + live order placement)
src/indicators.py             EMA / RSI / ATR
src/scanner.py                stage 1: Breakout/Pullback/Momentum/Trend/Reversal detectors
src/signal_engine.py          stage 2: filters + prioritizes candidates into one signal
src/trade_planner.py          stage 3: entry zone/stop/target/invalidation/confidence
src/risk_engine.py            stage 4: position sizing, exposure, daily loss, volatility, cooldown
src/claude_advisor.py         stage 4b: optional Claude veto + independent confidence score
src/trade_plan_store.py       PENDING/OPEN/CLOSED lifecycle for every plan (current + history)
src/monitor.py                stage 5: fills/discards/exits, pure decision logic
src/portfolio.py              cash/position/PnL bookkeeping shared by both brokers
src/broker_paper.py           simulated fills, no exchange credentials needed
src/broker_live.py            real order placement, gated by two independent switches
src/audit.py                  append-only JSONL log of every decision, every cycle
src/backtest.py               replays the same pipeline over historical OHLCV
src/bot.py                    wires every stage together; the main loop
scripts/run_paper.py          convenience entry point, refuses to run if mode: live
scripts/run_backtest.py       CLI backtest runner
tests/                        pytest suite (59 tests) covering every module above
```

## Dashboard

`web/` is a static, framework-free dashboard: an "office" view of the pipeline
stages (styled after a trading-desk swarm-office layout, one desk per role,
colored by model tier), a live trade-plan chart using TradingView's
open-source [lightweight-charts](https://github.com/tradingview/lightweight-charts)
library (vendored in `web/vendor/`, no external CDN or API key needed), and
data tables for open positions, pending plans, recent scanner activity, and
recent risk decisions.

It's a **snapshot** dashboard, not a live-streaming one: it reads
`web/data.json`, which you regenerate from the bot's own log files —

```bash
python scripts/export_dashboard_data.py   # writes web/data.json
```

— and it only updates for visitors once that new `data.json` is deployed.
Re-run the export script (a cron job, or right before you push) and redeploy
to refresh it. A sample `web/data.json` ships in the repo so the dashboard
isn't blank before you've run the bot yourself.

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

### Keeping the bot cycling reliably (Cloudflare Cron Trigger)

`.github/workflows/refresh-dashboard.yml` runs one bot cycle and regenerates
`web/data.json`. Its own `schedule:` trigger is GitHub's native cron, which
is **not reliable** — in practice, gaps of an hour or more between runs are
common even at a `*/5 * * * *` setting, since GitHub doesn't guarantee
scheduled-workflow timing.

`worker/index.js` (the same Worker that serves the dashboard) fixes this: on
a Cloudflare Cron Trigger (`triggers.crons` in `wrangler.jsonc`, currently
every minute), it calls GitHub's API to fire that workflow's
`workflow_dispatch` event directly. Cloudflare Cron Triggers don't have
GitHub's reliability problem, so this is what actually keeps cycles running
close to continuously. The workflow's own `concurrency` block queues
back-to-back dispatches safely rather than clashing.

This needs one secret the repo doesn't ship with: **`GITHUB_DISPATCH_TOKEN`**
— a GitHub fine-grained personal access token scoped to *only this repo*,
with **Actions: Read and write** permission (that's the minimum needed to
call the dispatch endpoint). Create one under GitHub → Settings → Developer
settings → Personal access tokens → Fine-grained tokens, then set it as a
Cloudflare Worker secret:

```bash
npx wrangler secret put GITHUB_DISPATCH_TOKEN
```

— or via the Cloudflare dashboard: **Workers & Pages → hybrid-crypto-trader →
Settings → Variables and Secrets → Add → type "Secret"**. Never commit this
token.

Note that triggering a private repo's Actions workflow this often uses real
GitHub Actions minutes (2,000/month free, then billed) — this project's repo
is public specifically so those minutes are unlimited/free. If you fork this
privately, either widen the cron interval in `wrangler.jsonc` or budget for
the extra Actions usage.

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
