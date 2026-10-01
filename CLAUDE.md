# Working on this repo: run it like a trading brokerage

This file tells Claude Code how to approach work in this repository. Treat the
codebase the way a small, serious crypto trading brokerage would run its
engineering org: every module has an owning role, every role has a model tier
and a way of working that matches how much damage a mistake in that area can
do, and the person orchestrating the work (you, in the top-level session —
the "CEO" seat below) is responsible for routing each task to the right role
and picking a model/effort level that matches its actual complexity, not its
apparent size.

This mirrors the same idea as the SPEHR project's swarm-office setup (see
`/areas/spehr.md`-style tooling if you've seen it) — a roster of specialist
roles instead of one generalist doing everything at one fixed setting.

## Trading bot rules (read `docs/crypto_bot_upgrade_playbook.md` before touching strategy code)

- Never place live orders or change live config without the user's explicit approval. Default to paper/testnet.
- No look-ahead: act only on closed candles, execute at the next bar's open. Run the tests in `research/v2_signals.py` before committing anything that touches signal/entry logic.
- Every backtest must include fees, slippage and funding, and report out-of-sample results.
- Do not add indicators or change parameters without a written reason and an out-of-sample test.
- Risk limits live in plain code (a risk module), never in an LLM prompt.
- Never read, print or commit API keys. Work on a git branch and commit before big changes.

`research/v2_signals.py` is starter scaffolding for a prospective v2 trend-following
redesign (Donchian ensemble + BTC regime filter + ATR sizing/trailing stop) per the
playbook — not wired into the live bot (`src/`) yet, not a finished strategy, not
proof of any edge. Its self-tests (`python research/v2_signals.py`) use this repo's
own `src/indicators.py` (sma/atr) rather than TA-Lib, matching this codebase's
existing "no extra TA dependency needed" convention — swap was verified numerically
equivalent (same Wilder-smoothing ATR, same plain rolling-mean SMA) before trusting it.

## The roster

| Role | Owns | Responsibilities | Default model |
|---|---|---|---|
| **CEO / Orchestrator** | Whole repo | Reads the request, identifies the owning role(s) below, sizes the complexity, picks the model/effort, delegates via the Agent tool to the matching subagent in `.claude/agents/` (or works directly if already at the right tier), and is the one who signs off on anything cross-cutting (new pipeline stage, going live, changing safety switches). | Whatever the session is already running as; escalate to Opus for anything touching `risk_engine.py` or `broker_live.py` regardless of who authored the change. |
| **Market Analyst** | `src/scanner.py`, `src/signal_engine.py`, `src/indicators.py` | Pattern detectors (Breakout/Pullback/Momentum/Trend Continuation/Reversal), indicator math, signal filtering/confluence logic. | Sonnet |
| **Trade Strategist** | `src/trade_planner.py` | Entry zone/stop-loss/take-profit/invalidation formulas, the rule-based confidence score. | Sonnet |
| **Risk Officer** | `src/risk_engine.py` | Position sizing, total exposure cap, per-trade max loss, daily loss limit, volatility filter, cooldown, kill switch. This is where a bug directly loses money. | **Opus, always** — see below |
| **Compliance Reviewer** | `src/claude_advisor.py` | The Claude second-opinion step: prompt design, APPROVE/VETO parsing, fail-open/closed behavior. Must never let the advisor originate or resize a trade. | Sonnet; Opus when touching the fail-safe (`_fallback`) path itself |
| **Execution Trader** | `src/broker_paper.py`, `src/broker_live.py`, `src/monitor.py`, `src/trade_plan_store.py` | Order execution, fill logic, the PENDING/OPEN/CLOSED lifecycle, the monitor's fill/discard/exit triggers. | Sonnet for paper broker / monitor / store; **Opus, always** for `broker_live.py` — real orders, real funds |
| **Platform Engineer** | `src/exchange.py`, `src/config.py`, `config/config.yaml`, `.env.example`, `requirements.txt`, `.github/workflows/ci.yml` | ccxt integration, config loading, dependencies, CI. | Haiku for routine bumps/CI tweaks; Sonnet for new exchange integrations |
| **QA / Tester** | `tests/` | Test coverage for every module above — a change without a passing, relevant test isn't done. | Sonnet; Haiku is fine for straightforward regression tests |
| **Architect** | Cross-module contracts (`Candidate`, `Signal`, `TradePlan` dataclasses), `README.md`, overall pipeline shape | Whether a change fits the existing five-stage pipeline or needs a new stage; keeps the modules' interfaces small and stable. | Opus for anything that changes a shared contract; Sonnet for doc-only updates |

## Complexity rubric (this decides model + effort, not the size of the diff)

- **Low** — typo/log-message fixes, a config value tweak with existing test
  coverage, a dependency bump, adding one obvious regression test. → Haiku,
  low effort. Work directly, don't delegate.
- **Medium** — tuning a scanner/planner threshold, adding a well-scoped test
  file, a routine bug fix with a clear repro, a paper-broker or monitor change
  covered by tests. → Sonnet, medium effort.
- **High** — anything touching `risk_engine.py` position sizing or
  `broker_live.py` (regardless of how small the diff looks), a new pattern
  detector or pipeline stage, a change to a shared dataclass contract, an
  ambiguous or underspecified request, anything security- or
  correctness-sensitive. → Opus, high effort. Get a second look before
  merging even if you wrote it yourself.

When in doubt, round up a tier — the cost of over-thinking a config tweak is
small; the cost of under-thinking a risk-sizing change is real money.

## How to delegate

Use the Agent tool with the matching subagent name from `.claude/agents/`
(`market-analyst`, `trade-strategist`, `risk-officer`, `compliance-reviewer`,
`execution-trader`, `platform-engineer`, `qa-tester`, `architect`). Each
subagent file pins its own default model. Override the model only when the
complexity rubric above says the task is a different tier than that role's
default.

Always run the full test suite (`pytest`) before considering any role's work
done — see each subagent's own instructions for what else it must check.

## Non-negotiables (no role, no model tier gets to skip these)

- Never remove or weaken the two-switch live-trading gate (`mode: live` +
  `LIVE_TRADING_CONFIRM=YES-I-UNDERSTAND-THE-RISK`) in `src/broker_live.py`.
- Never make the Claude advisor (`src/claude_advisor.py`) able to originate a
  trade or resize one — APPROVE/VETO only, and it must fail *closed* by
  default on any error.
- Never commit `.env`, real API keys, or anything under `logs/`.
- A change to `src/risk_engine.py` or `src/broker_live.py` always gets an
  Opus-level read-through before merge, even for a one-line diff.
