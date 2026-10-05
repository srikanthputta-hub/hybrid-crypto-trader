# P1c validation: does `rank_signals` processing order change existing backtest results?

**Status:** investigation complete, numbers below are real backtest output. **Not yet reviewed by
the Architect role** (required per `docs/system-redesign-2026-10.md` §4.1's
`strategy_pack_run/backtest_live_bot.py` row: "QA/Tester + Architect review") — this is a
harness-semantics change and per this repo's `CLAUDE.md` roster needs that review before anything
gets wired toward `src/bot.py`. **No decision is made here about whether to proceed to wiring
`rank_signals` into the live bot** — that's the user's call once this is reviewed.

**Branch:** `claude/redesign-p1c-harness-validation`, off `origin/main`, with
`origin/claude/redesign-p1c-rank-signals` merged in for `src/signal_engine.py`'s `rank_signals()`
(imported, not modified).

## The question

`strategy_pack_run/backtest_live_bot.py`'s `simulate_portfolio` has always processed each bar's
candidate signals in `sorted(symbols)` (alphabetical) order when the real `src/risk_engine.py`
had to decide who gets a scarce new-entry slot. Per §4.1/§4.4 rule 2 of the system redesign doc,
this harness now has a `use_rank_order` flag that, when set, processes a bar's FLAT-with-signal
symbols in `src.signal_engine.rank_signals` order instead (imported, not reimplemented) — highest
`engine_score` first, tie-broken by trailing dollar volume, then symbol. Everything else
(OPEN/PENDING monitor checks, FLAT symbols with no signal that bar) keeps the original
`sorted(symbols)` order, since those never compete for anything (see "Why only one loop needed to
change" below). **Default is `False` everywhere** — `run_live_bot_report.py`, `hyperopt_param.py`,
and every existing test call `simulate_portfolio`/`placebo_run` without the flag, so none of them
changed behavior.

Question: does flipping that flag change the 5-gate backtest result on the **existing**
`kraken_720` (`strategy_pack_run/data/`) and `long` (`strategy_pack_run/data_long/`) datasets — the
same datasets, same config, same `kraken_T3_taker` costs as every other backtest in this repo's
history — and if so, is it better, worse, or a wash?

## Why only one loop needed to change

`simulate_portfolio` has two places that iterate over `symbols` per bar: step 1 (execute whatever
was queued from the *previous* bar's close, at *this* bar's open — fills/discards/exits) and step 3
(decide the *next* trigger for every symbol, from *this* bar's close — this is where a FLAT symbol
with a fresh signal gets a brand-new plan approved or rejected). Only step 3's FLAT branch can
change behavior based on order, for a specific, checkable reason:

- `risk_engine.check_new_trade`'s **`max_total_exposure_pct` term** (`risk.max_total_exposure_pct:
  0.40`) is computed from `open_positions_value`, which only counts symbols already **OPEN** —
  never symbols that got a new PENDING plan earlier in the *same* bar's step-3 loop (a plan doesn't
  spend cash or count as exposure until it *fills*, at the *next* bar's open). So within one bar,
  every FLAT candidate sees the *identical* `open_positions_value`, regardless of processing order.
  Confirmed by instrumentation below — order doesn't need to matter here, and empirically never did.
- `risk_engine`'s **GLOBAL `max_new_entries_per_window` cap** (`max_new_entries_per_window: 5`,
  `new_entry_window_minutes: 120`) is the only place order can actually matter. On daily bars
  (1440 minutes apart), an entry from a *previous* bar is always outside the 120-minute window by
  the time the next bar is evaluated, so the cap can only ever bind between symbols competing
  **within the same bar**. Whoever is checked first when more than 5 symbols signal on one day
  gets the slot; whoever is checked later that day doesn't.

So the fix only needed to reorder step 3's FLAT-with-signal subset per bar; OPEN/PENDING monitor
checks and FLAT-with-no-signal symbols are left in their original order (their relative order is
provably inert). `placebo_run` also got a `use_rank_order` passthrough (default `False`) so a
placebo comparison can use the same ordering as whatever real run it's being compared against.

Liquidity tie-break input: no `src/universe.py`/liquidity module exists yet (that's a later phase).
Per the task's instruction not to invent an unreviewed new formula, the harness computes exactly
the formula already named by `rank_signals`' own docstring and the design doc's
`universe.min_median_quote_volume_usd` — a point-in-time, no-look-ahead 30-bar trailing **median**
of `close * volume` (`backtest_live_bot.trailing_quote_volume`, `min_periods=1`).

## Does `max_total_exposure_pct` / `max_new_entries_per_window` ever actually bind?

**Yes, both bind, and heavily** — this is not a case where the test can't distinguish the two
orderings for lack of contention:

| Dataset | Order | total buy-checks | rejected: exposure cap ("no room left") | rejected: rate cap (`max_new_entries_per_window`) |
|---|---|---|---|---|
| kraken_720 | sorted(symbols) | 669 | 380 (57%) | 38 |
| kraken_720 | rank_signals | 759 | 444 (58%) | 40 |
| long | sorted(symbols) | 2405 | 876 (36%) | 78 |
| long | rank_signals | 2357 | 839 (36%) | 62 |

Every single "no room left" rejection, on both datasets and both orderings, was the exposure term
specifically saturated (`open_positions_value >= portfolio_equity * max_total_exposure_pct`, to
within 0.1%) — `max_total_exposure_pct` is the dominant, constantly-binding constraint in this
backtest, not an edge case. The rate cap binds far less often in absolute count (38-78 instances)
but, as shown below, those are exactly the events that matter for this question, because the
exposure-cap numbers above (380 vs 444, 876 vs 839) are mostly a **downstream consequence** of the
rate-cap-driven divergence, not a sign that ordering changes the exposure check directly (it
can't — see above). Once the rate cap hands bar *t*'s scarce slot(s) to a different symbol, the
portfolio's OPEN-position set differs from that point on, which changes `open_positions_value` (and
therefore exposure-cap outcomes) on every later bar, regardless of order at those later bars. That
cascading effect, not a direct per-bar interaction, is why the trade-level diffs below are much
larger than the raw count of rate-cap contests.

**Concrete bar-level winner differences** (bars where >=2 FLAT symbols signaled the same day *and*
the rate cap rejected at least one of them, `sorted(symbols)` run used to define "contested"):

| Dataset | Contested bars | Winner-set differs under `rank_signals` | Winner-set identical |
|---|---|---|---|
| kraken_720 | 9 | **9** | 0 |
| long | 23 | **23** | 0 |

Every contested bar changed who got the slot. A few examples (kraken_720):

```
2024-11-13: sorted candidates=[ADA,ALGO,ATOM,AVAX,BTC,DOGE,DOT,ETH,SHIB,SOL,XRP,XTZ]
            -> won=[ADA,ALGO,ATOM,AVAX,BTC]
            rank   candidates=[BTC,SOL,ETH,DOGE,XRP,AVAX,SHIB,ADA,DOT,ATOM,ALGO,XTZ]
            -> won=[AVAX,BTC,ETH,SOL,XRP]        <- a completely different top-5

2026-08-23: sorted candidates=[ADA,ALGO,ATOM,BCH,BNB,BTC,DOGE,DOT,LTC,SOL,XRP,XTZ]
            -> won=[ADA,ALGO,ATOM,BCH,BNB]
            rank   candidates=[BTC,ADA,DOGE,LINK,LTC,BNB,ALGO,BCH,DOT,ATOM,XTZ]
            -> won=[]                            <- rank order's own top-5 candidates ended up
                                                     rejected on OTHER, order-independent grounds
                                                     (by this point in the run the two portfolios'
                                                     exposure/cash state had already diverged from
                                                     earlier contested bars)
```

(Full list, both datasets: produced by `strategy_pack_run/run_rank_order_validation.py`, not
committed separately since it's reproducible from the script + the existing CSVs.)

## The 5-gate comparison

Costs: `kraken_T3_taker` (same as every other live-strategy report in this repo). Equity $10,000.
200-sim time-shift placebo, same seed=0 for both legs. `>=100 trades`, `avg R CI low > 0`,
`later 30% avg R > 0`, `beats placebo >=90th pct` (both return and avg R percentile >= 90),
`net return > 0 and Sharpe > buy&hold` — same 5 gates, same thresholds, as
`run_live_bot_report.py`/the Daily Backtester Routine.

### kraken_720 (16 symbols, 719 bars, 2024-10-13 to 2026-10-01)

| order | trades | win% | avg R | avg R 95% CI | PF | return | maxDD | Sharpe | exposure |
|---|---|---|---|---|---|---|---|---|---|
| sorted(symbols) | 154 | 40.3% | 0.18 | [-0.08, 0.46] | 1.33 | 22.5% | -24.1% | 0.68 | 22.1% |
| rank_signals | 137 | 38.0% | 0.11 | [-0.18, 0.41] | 1.25 | 16.2% | -25.5% | 0.52 | 22.8% |

| order | >=100 trades | avg R CI low > 0 | later 30% avg R > 0 | beats placebo >=90th pct | return>0 & Sharpe>bh | placebo pctile (return/avgR) |
|---|---|---|---|---|---|---|
| sorted(symbols) | PASS | FAIL | PASS | PASS | PASS | 98th / 91th |
| rank_signals | PASS | FAIL | PASS | **FAIL** | PASS | 96th / 86th |

**sorted(symbols): 4/5 — rank_signals: 3/5.** `rank_signals` is strictly worse here: fewer trades
(137 vs 154), lower win rate, lower avg R, lower return, deeper drawdown, lower Sharpe, and it
newly fails the placebo gate (avg R drops from the 91st to the 86th percentile of 200 time-shifted
placebos, below the 90th-percentile bar).

### long (16 symbols, 4091 bars, 2015-07-21 to 2026-10-01)

| order | trades | win% | avg R | avg R 95% CI | PF | return | maxDD | Sharpe | exposure |
|---|---|---|---|---|---|---|---|---|---|
| sorted(symbols) | 562 | 48.0% | 0.52 | [0.36, 0.68] | 1.49 | 387.5% | -29.2% | 1.00 | 15.8% |
| rank_signals | 572 | 48.3% | 0.53 | [0.38, 0.69] | 1.42 | 327.0% | -30.3% | 0.94 | 15.4% |

| order | >=100 trades | avg R CI low > 0 | later 30% avg R > 0 | beats placebo >=90th pct | return>0 & Sharpe>bh | placebo pctile (return/avgR) |
|---|---|---|---|---|---|---|
| sorted(symbols) | PASS | PASS | PASS | PASS | PASS | 100th / 98th |
| rank_signals | PASS | PASS | PASS | PASS | PASS | 100th / 98th |

**5/5 both ways — a wash on the gate verdict.** Individual metrics move a little (10 more trades,
avg R +0.01, but return -60pp and Sharpe -0.06 under `rank_signals`) but nothing crosses a gate
threshold either way.

## Harness sanity check

`run_live_bot_report.py` was NOT modified. Running it unmodified today (`--data-dir data`) gives
**154 trades, 40.3% win, avg R 0.18 [-0.08, 0.46], return 22.5%, Sharpe 0.68, maxDD -24.1%** —
exactly the `sorted(symbols)` row above, confirming the new `use_rank_order` flag's default path is
byte-identical to the pre-existing code.

This does **not** match the numbers committed in `strategy_pack_run/report_live_strategy_kraken_720.md`
(189 trades, 43.9% win) / `report_live_strategy_long.md` — those files are **stale**, generated at
commit `9294f6b`, before PR #55 ("Add a global new-entry rate cap to the risk engine",
`max_new_entries_per_window`) and PR #57 ("Widen `trade_planner.stop_atr_mult` from 1.0 to 1.5")
landed on `main`. The correct, current baseline is the one recorded in
`scripts/backtest_summary.json` (`generated_at: 2026-10-01T13:37:00Z`, i.e. after both PRs), whose
`parameter_variant` candidate notes repeatedly cite "Baseline 0.1: 154 trades, avg R 0.18
[-0.08,0.46], return 22.5%, Sharpe 0.68, maxDD -24.1%" (kraken_720) and "Baseline 0.1: 562 trades,
avg R 0.52 [0.36,0.68], return 387%, Sharpe 1.00, maxDD -29.2%" (long) — both match this
investigation's `sorted(symbols)` numbers exactly. **Sanity check passes against the current
on-record baseline; the two `report_live_strategy_*.md` files should be regenerated separately
(not part of this task) since they predate two real strategy changes.**

## Verdict

**(c) worse on `kraken_720`, (b) a wash on `long`.** Not a pure win anywhere, and clearly a net
negative on the smaller, harder, already-marginal dataset:

- The cap genuinely binds on both datasets (not a case of "N=16 too small to tell" — 38-78
  rate-cap rejections and 9-23 genuinely contested bars per dataset, every contested bar producing
  a different winner), so this test **can** distinguish the two orderings, and it did.
- On `long` (4091 bars, far more trades, far more opportunities for the two orderings to average
  out), the result is a wash: same 5/5 gate verdict, small moves in both directions on the
  underlying metrics.
- On `kraken_720` (719 bars, already the harder dataset — the live strategy's own baseline already
  fails 1 of 5 gates here), `rank_signals` makes it worse by every single metric and flips a
  second gate to FAIL. The mechanism isn't subtle: fewer trades survive at all (137 vs 154) because
  early contested-bar winner swaps cascade into a different exposure/cash trajectory for the rest
  of the run, and the trades that do happen are worse on average (avg R 0.11 vs 0.18).
- Engine-score-first ranking is not obviously the wrong idea in principle, but nothing here
  supports a claim that it's an improvement. The honest reading is: this specific ranking formula
  (`-engine_score, -median_quote_volume_30d, symbol`), on this specific 16-symbol universe, at this
  specific `max_new_entries_per_window: 5` cap, performs worse-or-even, not better.

This result should NOT be confounded with the planned universe expansion (P1d/P1e) — per the design
doc's own framing, that's exactly why this ranking-only step was gated separately, on the existing
data, first. A different, larger universe (where the rate cap would plausibly bind far more often
and far more clearly) is a separate question with its own gate run.

**This does not decide whether `rank_signals` gets wired toward `src/bot.py`.** That's a separate
decision for the user, informed by: (a) this result (worse-or-wash, not a win, on the only data
available to test it), (b) the Architect review this harness-semantics change still needs per
`CLAUDE.md`'s roster note, and (c) whatever weight to give a result that's a wash on 4091 bars but
a regression on 719.

## A real bug found (and fixed) while writing the unit tests

Writing a small, controlled test for "the rate cap decides the winner" (two hand-built symbols,
one signal each on the same bar, `max_new_entries_per_window=1` so exactly one must win) failed in
a way that revealed `simulate_portfolio`'s `RiskEngine(...)` construction never forwarded
`risk_cfg["max_new_entries_per_window"]` / `risk_cfg["new_entry_window_minutes"]` at all — it always
used `RiskEngine`'s class defaults (5 / 120) regardless of what was in `risk_cfg`. `src/bot.py` (the
live bot) **does** forward these two keys correctly, with an explicit "only passed when present"
comment — this harness was supposed to mirror that construction exactly and silently didn't, for
these two keys specifically (every other `risk_cfg` key was already forwarded correctly).

**Fixed** in the same commit (mirrors `src/bot.py`'s own `**{key: risk_cfg[key] for key in (...) if
key in risk_cfg}` pattern exactly). **Did not change any number in this report or any previously
recorded result**: `config/config.yaml`'s configured values (`max_new_entries_per_window: 5`,
`new_entry_window_minutes: 120`) are numerically identical to `RiskEngine`'s own class defaults, and
`scripts/backtest_summary.json`'s 18 recorded `parameter_variant` candidates never swept either key
(`hyperopt_param.py` only searches `trade_planner.*`/`risk.*` scalars one at a time, and no
candidate's `config_key` is either of these two) — confirmed by re-running
`run_live_bot_report.py --data-dir data` after the fix and getting the identical 154-trade result.
This is disclosed here rather than fixed silently because it's a real correctness gap beyond this
task's original ordering-only scope, even though it happened not to matter for anything on record
so far — it would matter the moment anyone tries to backtest a `risk.max_new_entries_per_window` or
`risk.new_entry_window_minutes` parameter variant, which nothing has done yet.

## Files touched

- `strategy_pack_run/backtest_live_bot.py` — `trailing_quote_volume()` (new, pure), `simulate_portfolio(..., use_rank_order: bool = False)`, `placebo_run(..., use_rank_order: bool = False)`. Every existing caller's behavior is unchanged (parameter defaults to `False`; verified against `scripts/backtest_summary.json`'s on-record baseline above).
- `strategy_pack_run/run_rank_order_validation.py` — new one-off investigation script (not part of the Daily Backtester Routine's regular tooling) that produced every number in this report; imports `engine.py`/`run_report.py`/`run_live_bot_report.py`/`backtest_live_bot.py` unchanged.
- `strategy_pack_run/test_backtest_live_bot.py` — new tests for `use_rank_order` (see below).
- This file.

## Validation run

- `PYTHONPATH=. python3 -m pytest -q` — 282 passed (unchanged test count; `tests/` doesn't import this harness).
- `PYTHONPATH=..:. python3 -m pytest -q strategy_pack_run/` (run from `strategy_pack_run/`, since that's where its own `sys.path` bootstrap expects to run from) — 22 passed (19 pre-existing + 3 new `use_rank_order` tests), including `test_pack.py`'s truncation/no-look-ahead invariant tests.
- `PYTHONPATH=. python3 scripts/validate_invariants.py` — 5/5 passed.
