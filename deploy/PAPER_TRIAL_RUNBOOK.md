# Paper daily-bar trial: operator runbook (always-on VM)

> **DO NOT MERGE the trial change (or its rollback) into `main` until the 1h bot on the VM is stopped and
> disabled (section 3, step 1).** The VM pulls `main` by itself: `scripts/run_live.py` pushes `web/data.json`
> after every cycle and, when the push is rejected because `main` moved, runs `git pull --no-rebase --no-edit`.
> Within ~5 minutes of a merge the new `config/config.yaml` is on the VM's disk. The running process keeps its
> old config in memory, but the next restart of any kind (crash, OOM kill, reboot, `Restart=always`) would start
> a 1d / bar-parity bot on top of the 1h state: 1h OPEN plans sized on 1h ATR then monitored close-only, 1h cash
> and positions, the first bar skipped as stale, a pruned audit log, and a parity check that refuses to run.
> Merge only inside the deploy window below, after step 1.

**What this is.** Step-by-step instructions for switching the always-on paper bot (`scripts/run_live.py` under
systemd, see [README.md](README.md)) from 1h candles to the **daily-bar paper trial**: Option A of
[docs/multiday_hold_design.md](../docs/multiday_hold_design.md) (§3.1), run under its pre-registered protocol (§5).

**What this is not.** It is **paper only**. `mode: paper` stays, the two-switch live gate is untouched, no order is
ever sent to an exchange, and no real money is involved. The trial is an *implementation-parity check plus a
catastrophe guard*, **not an edge test** (design §0 item 4, §5): even a clean 50-trade trial cannot statistically
confirm the 2-year edge. Real-money use of these signals is out of scope for the whole trial (design §3.4, §8
decision 7). The live-readiness blockers in issue #99 are **not** part of this and stay open.

Pre-flight risks and what the first week can and cannot show: [docs/daily_trial_preflight.md](../docs/daily_trial_preflight.md).

---

## 0. What the shipped config now says

Seven things changed in `config/config.yaml`; every strategy and risk parameter is unchanged (the daily evidence
was produced with exactly these, see `docs/daily_trial_preflight.md` §2). `tests/test_paper_trial_config.py` pins
all of them, so an accidental edit fails the test suite (when the suite is run: CI is dead until 2026-11-01).

| key | 1h era | trial | why |
|---|---|---|---|
| `market.symbols` | 16 x `*/USDT` | the same 16 coins as `*/USD` | the evidence is `*/USD` bars; Kraken's USDT books are thinner and give different closes and a different trade list (preflight §2) |
| `market.timeframe` | `"1h"` | `"1d"` | Option A: the unchanged pipeline on daily bars (design §0, §3.1) |
| `loop.bar_parity` | `false` | `true` | P1-P5: one decision per symbol per closed bar, as the backtest decides (design §3.1, `docs/bot_bar_parity.md`) |
| `loop.max_entry_delay_minutes` | `null` | `60` | P4 staleness guard (untuned; can only remove entries). Also the Telegram "Valid until" |
| `logging.closed_bars_dir` | `null` | `"logs/daily_bars"` | the exact bars the bot decided on, for the weekly parity check |
| `logging.retention_days` | `30` | `0` | keep the whole audit log: the parity check replays from trial day 0 (pruning cannot be repaired) |
| `notifier.label` | `null` | `"PAPER TRIAL"` | every Telegram BUY / SELL / fill message is prefixed `PAPER TRIAL ·` |

Unchanged and fine at 1d: `loop.poll_interval_seconds: 300` (one real decision per symbol per day; the other ~287
cycles a day only echo "already decided" and re-check held positions), `market.history_bars: 200` (>= 150 is
checked at startup), `htf_filter.enabled: true` (runs on the scan frame at 1d, no extra fetch).

---

## 1. Before deploy night (decisions and pre-checks, any time)

These need **you** (the user), not the VM:

1. **Pause the Daily Backtester Routine and do not merge its PRs: a MUST.** Its promotion PRs **#78, #82, #83,
   #85, #88** (and any future "Daily backtest sweep" PR) each change the strategy under test, which restarts the
   trial clock (design §5 "Strategy under test", §8 decision 6). `tests/test_paper_trial_config.py` fails on such
   a change, but **nothing runs it automatically while CI is dead (until 2026-11-01)**, so the test cannot block a
   merge: pausing the Routine and leaving its PRs unmerged is the only real guard. Its stored prompt (outside this
   repo) may also cite stale baselines: `data_long/` **562 / 0.52** is now **565 trades, avg R 0.53 [0.37, 0.69],
   406.2%, Sharpe 1.03, maxDD -27.7%** (original harness, $10 floor); `data/` 154 / 0.18 is unchanged; the P6-fixed
   numbers are 132 / 0.22 and 546 / 0.50 (`docs/min_order_floor_10_rebaseline.md`). PR **#29** (Cloudflare Cron
   Trigger) must also stay unmerged: it would run a second bot process.
2. **Confirm the kill thresholds (USER MUST CONFIRM BEFORE DAY 0).** Recommended: the re-simulated set
   **-0.57 / -0.43 / -0.34 / -0.30** at 20 / 30 / 40 / 50 trades (section 8). Write the choice down before day 0.
3. **Decide the venue and fee tier the trial is judged at** (design §8 decision 8). You follow the alerts by hand
   on Kraken: Kraken T3 taker (0.38% fee + 0.05% slippage per side) is the default; use your actual tier.
4. **Telegram token.** If you have not already done the post-#103 steps in [README.md](README.md) ("After deploying
   the notifier/heartbeat log-redaction fix"), do them: check for leaked tokens (section 3 step 2), rotate the bot
   token in BotFather (`/revoke`), put the new one in `.env`. A rotated token makes any old leaked copy useless.
5. **VM shape: check `free -m` before day 0** (over SSH: `nproc; free -m; df -h ~`). The repo does not record which
   shape the VM is. On the 6 GB Ampere shape memory is fine for the whole trial; on a 1 GB `E2.1.Micro` it is not
   (section 11: run the parity check off the VM from day 1, and expect the dashboard export to run out of memory
   after ~4-6 months).
6. **GitHub Actions**: `refresh-dashboard.yml`'s `schedule:` must stay disabled (it is, since 2026-09-29), and do not
   press its "Run workflow" button during the trial: that runs a second, independent bot cycle on CI.

---

## 2. Timing: stop first, merge, then START between 00:01 and 00:45 UTC

Kraken daily bars close at **00:00 UTC**. The bot decides each symbol on the first cycle after that close (every
300 s, so 00:00-00:05 UTC plus the cycle's own runtime). The staleness guard skips entries from a bar that closed
more than 60 minutes earlier.

So the **first** cycle after the switch must happen between 00:00 and 01:00 UTC:

- **Inside the window** (recommended: start at ~00:05 UTC): the first cycle decides the bar that just closed,
  normally. That bar is trial day 0, and it is also the parity check's trial start (the earliest `bar_close` in the
  fresh audit log).
- **Outside the window** (e.g. 14:00 UTC): the first cycle logs `stale bar: entries skipped` for every symbol and
  marks that bar decided. The parity check then starts its replay on that stale bar; whenever the backtest had a
  signal there, the result is an explained `staleness_guard` mismatch that counts toward the 10% budget from the
  first entry on (the strict, pre-registered default), and a harness-only entry on that bar keeps both sides apart
  until the harness exits. See section 4, "If the first cycle says stale".

The order is fixed: **step 1 (stop and disable the 1h bot) -> steps 2-3 -> step 4 (merge) -> steps 5-7 -> step 8
(start at ~00:05 UTC)**. Steps 1-7 can be done any time on the day before (the paper bot simply stays stopped);
only step 8 is tied to the window.

---

## 3. The switch, step by step (on the VM, from the repo root)

```bash
ssh -i /path/to/key.pem ubuntu@<PUBLIC_IP>
cd ~/hybrid-crypto-trader
```

**Step 1. Kill switch, stop, disable: BEFORE anything is merged.**

```bash
touch KILL_SWITCH                                # any restart before step 7 opens no new position
sudo systemctl stop hybrid-crypto-trader
sudo systemctl disable hybrid-crypto-trader     # a reboot must not start it before step 8
systemctl is-active hybrid-crypto-trader        # expect: inactive
pgrep -af "run_live.py|src.bot|run_paper.py"     # expect: no output (exactly ONE bot process may ever run)
```

**Step 2. Check for leaked secrets** (deploy/README.md; before archiving, so you know which files matter).

```bash
grep -rl 'api.telegram.org/bot' logs/ ; echo "grep exit: $?"                    # exit 1 = none found
journalctl -u hybrid-crypto-trader | grep -c 'api.telegram.org/bot'               # expect 0
journalctl -u hybrid-crypto-trader | grep -c 'hc-ping.com'                        # expect 0
```

Anything found means that token was exposed: rotate it (section 1 item 4) before step 8. Do not paste log contents
anywhere.

**Step 3. Archive the 1h-era logs and reset the paper portfolio.** `scripts/reset_portfolio.py` **moves** (never
deletes) `logs/trades.csv`, `logs/trade_plans_current.json`, `logs/trade_plans_history.jsonl` and
`logs/audit_log.jsonl` into a new `logs/archive/<UTC timestamp>/`. With those gone, the next start reconstructs a
fresh `risk.starting_cash_usd` (10,000) portfolio with no positions or plans. The reset is **required**, not
optional: design §5 starts the trial from a fresh portfolio, the parity check replays the harness from a fresh
portfolio at the trial start (an inherited 1h position would be a mismatch from day 0), and it refuses to run
(exit 2) when `trades.csv` is older than the audit log.

```bash
.venv/bin/python scripts/reset_portfolio.py
```

- It must print **`Archived to logs/archive/<stamp>:`** followed by the files it moved. If it says "No portfolio
  state files found" or "nothing to reset", stop and look: the 1h bot should have left files.
- It ends with **`Run: sudo systemctl restart hybrid-crypto-trader`. IGNORE that line.** A restart would start the
  (disabled) unit now, outside the 00:01-00:45 UTC window and before the merge.

Then move the other 1h-era state files into **that same** directory (copy the path it printed; don't pick "the
newest archive" blindly, which could be an older one if the reset moved nothing):

```bash
ARCH=logs/archive/<stamp-printed-above>
for f in logs/market_snapshot.json logs/bar_decisions.json logs/daily_bars; do [ -e "$f" ] && mv "$f" "$ARCH"/; done
ls -la "$ARCH" logs/
```

Expected: `$ARCH` holds the four files from the reset plus `market_snapshot.json` (and `bar_decisions.json` /
`daily_bars` only if a test run ever created them; the 1h bot never does). `logs/` is left with `.gitkeep` and
`archive/` only. `logs/archive/` is gitignored; never `git add` it.

**Step 4. Merge the trial PR into `main`** (you, on GitHub), now that the 1h bot is stopped. Nothing on the VM
pulls while the bot is stopped.

**Step 5. Pull main on the VM.**

```bash
git status --short                       # typically only " M web/data.json"
git checkout -- web/data.json            # drop the VM's uncommitted dashboard file, if modified
git pull --no-rebase --no-edit           # plain `git pull` errors on divergent branches with Ubuntu 22.04's git 2.34
git log -1 --oneline                     # the merge of the trial PR (or a later dashboard commit on top of it)
```

**Step 6. Verify the config and the code.**

```bash
grep -nE '^\s*(mode|timeframe|history_bars|poll_interval_seconds|bar_parity|max_entry_delay_minutes|label|closed_bars_dir|retention_days):' config/config.yaml
grep -nE '^\s+- "[A-Z]+/USDT?"' config/config.yaml                    # 16 lines, all /USD
PYTHONPATH=. .venv/bin/python scripts/validate_invariants.py         # expect: 7/7 invariants passed
.venv/bin/python -m pytest -q tests/test_paper_trial_config.py        # expect: all passed (1-2 minutes)
```

The first grep must show `mode: paper`, `timeframe: "1d"`, `history_bars: 200`, `poll_interval_seconds: 300`,
`bar_parity: true`, `max_entry_delay_minutes: 60`, `label: "PAPER TRIAL"`, `closed_bars_dir: "logs/daily_bars"`,
`retention_days: 0`; it also matches `universe:`'s `mode: static` line, which is expected. The second shows 16
`*/USD` symbols (BTC, ETH, XRP, SOL, ADA, DOGE, AVAX, LINK, BNB, LTC, DOT, ATOM, ALGO, BCH, XTZ, SHIB).
`validate_invariants.py` prints `bar_parity=True, max_entry_delay_minutes=60.0` and `label='PAPER TRIAL'`. Do
**not** edit `config/config.yaml` on the VM: a local edit diverges from main and breaks the loop's own pulls.

**Step 7. Clock and kill switch.**

```bash
timedatectl | grep -E 'Time zone|synchronized'    # "System clock synchronized: yes"
date -u
rm KILL_SWITCH                                    # the trial needs entries; a kill switch makes every check a rejection
```

The staleness guard and the bar-close detection use the host clock (via ccxt).

**Step 8. Start at ~00:05 UTC** (between 00:01 and 00:45 UTC):

```bash
sudo systemctl enable --now hybrid-crypto-trader
systemctl is-active hybrid-crypto-trader            # active
```

Optional, if you would rather not be awake: schedule the start (date in UTC; check the VM's `date -u` first):

```bash
sudo systemd-run --on-calendar='YYYY-MM-DD 00:05:00 UTC' --unit=paper-trial-start \
  /bin/systemctl enable --now hybrid-crypto-trader
systemctl list-timers paper-trial-start.timer      # shows the next elapse in UTC
```

---

## 4. What the first cycles must show (check within 15 minutes of the start)

```bash
journalctl -u hybrid-crypto-trader --since "15 min ago" | grep -E "starting live loop|ERROR|Traceback|ledger"
wc -l logs/audit_log.jsonl          # 16 after the first cycle, +16 per cycle (one line per symbol per cycle)
cat logs/bar_decisions.json         # {"timeframe": "1d", "bars": {16 */USD symbols: "<today>T00:00:00+00:00"}}
ls logs/daily_bars                  # 16 files: ADA_USD_1d.csv ... XTZ_USD_1d.csv
wc -l logs/daily_bars/BTC_USD_1d.csv    # 201 (header + the 200 bars the first decision used); +1 per day
```

Summary of the cycles so far:

```bash
.venv/bin/python - <<'EOF'
import json, collections
ev = [json.loads(l) for l in open("logs/audit_log.jsonl")]
print("symbols:", sorted({e["symbol"] for e in ev}))
print("bar_close values:", collections.Counter(e.get("bar_close") for e in ev))
print("stale skips:", sum("stale bar" in str(e.get("note")) for e in ev), "(must be 0)")
print("scans:", sum("candidates" in e for e in ev), "(<= 16, all in the first cycle)")
print("already decided echoes:", sum("already decided" in str(e.get("note")) for e in ev))
print("planned:", [(e["symbol"], e["risk_decision"]["max_quote_amount"]) for e in ev if e.get("action") == "planned"])
print("htf source:", collections.Counter(e.get("daily_trend_source") for e in ev if "daily_trend_confirmed" in e))
print("errors:", [(e["symbol"], e.get("error")) for e in ev if e.get("error")])
EOF
```

Expected:

- `starting live loop: mode=paper ... symbols=[16 */USD pairs]` in the journal, no traceback. No ledger warning
  (there is no ledger file yet; a "written for timeframe '1h'" warning would mean step 3 missed it, which is
  harmless: that ledger is ignored).
- Every audit line carries `bar_close` = **today 00:00 UTC**. **No** `stale bar: entries skipped`.
- First cycle: one decision per symbol: `candidates` (a scan), possibly a `signal`, `daily_trend_source:
  "scan_frame"` for every htf check (no separate daily fetch), a `risk_decision`, and 0-5 `planned` entries
  (each with `max_quote_amount` >= 10 and an immediate paper fill at the last daily close).
- Second cycle (5 min later) and every later cycle that day: FLAT symbols log `bar closing ... already decided`,
  held symbols `OPEN plan active ..., no trigger this cycle`. No new scans, no new plans until the next 00:00 UTC.
- Telegram: any BUY message starts with `PAPER TRIAL ·`, and ends with
  `Bar: <yesterday's date> (daily, closed <today> 00:00 UTC)` and `Valid until: <today> 01:00 UTC`. A "Bought" fill
  message follows each BUY. Zero messages on a quiet day is normal (about 6 BUYs a month are expected).
- `logs/trades.csv`: one `buy` row per planned entry, `quote_amount` >= 10.
- The dashboard (section 10) shows cash 10,000 minus any entries.

### If the first cycle says "stale bar"

The start was outside the window. Entries were skipped, so the portfolio is still flat. Two ways on:

- **Clean restart (preferred):** the next night, `touch KILL_SWITCH`, stop and disable the service, run section 3
  step 3 again (this archives the contaminated audit log, ledger and bar archive into a new dated directory), then
  steps 7 and 8. Nothing needs merging again.
- **Cheaper alternative:** leave the bot running and pass `--trial-start <the next 00:00 UTC close>` (e.g.
  `--trial-start 2026-10-17T00:00:00+00:00`) to **every** weekly parity check from now on. This is correct only
  because the portfolio is still flat at that close (the stale bar opened nothing); `docs/paper_parity_check.md`
  calls `--trial-start` diagnostic, so record this choice in the trial notes as a protocol decision.

---

## 5. Every day, automatically (nothing for you to do)

- **~00:00-00:06 UTC**: the bot decides every symbol on the bar that just closed. Exits (stop-loss / take-profit,
  close-only: the daily close must be beyond the level; an intraday spike that recovers does not trigger) and new
  entries both happen in that cycle, in that order. BUY, Bought and SELL messages arrive then.
- **The rest of the day**: a cycle every 5 minutes re-checks held positions against the same last close (no change
  is possible until the next close), writes the audit line, refreshes `web/data.json` and pings the heartbeat.

**What you do by hand: nothing.** The paper broker fills its own entries at the last daily close. The Telegram BUY
is a **paper alert**, not a recommendation and not a live order. You follow these alerts by hand on Kraken (an
older config note also mentions Revolut X, where you traded them earlier); during the trial, by the design's
recommendation, you don't trade them with real money (design §3.4, §8 decision 7). If you nevertheless mirror a
signal (your own risk decision, against that recommendation): `Valid until` (bar close + 60 min) is the latest
time an entry is inside what the evidence covers; your fill price is whatever Kraken trades at when you act, not
the close the plan used; and the paper stop is close-only, so a hand-placed stop order would behave differently.

---

## 6. Weekly routine (any day, but NOT between 23:50 and 00:15 UTC)

```bash
cd ~/hybrid-crypto-trader
PYTHONPATH=. .venv/bin/python scripts/paper_parity_check.py --days 7 --json-out "logs/parity_$(date -u +%F).json"
echo "exit code: $?"
du -h logs/audit_log.jsonl ; free -m | head -2 ; df -h ~ | tail -1
```

Read-only, no network, about 15 s plus a few seconds per trial week (on a 1 GB VM, run it off the VM: section 11).
Avoid the minutes around midnight: the bot writes the decision ledger just before each decision's audit line, and
a check that catches it in between stops with exit 2 ("the END of the audit log is missing"). Just re-run it later.

| output (first line) | exit | meaning | what you do |
|---|---|---|---|
| `PAPER TRIAL PARITY CHECK -- INSUFFICIENT DATA` | 0 | fewer than 3 decided bars in the window (normal in the first 2-3 days) | nothing |
| `-- PASS` | 0 | the bot's entries, exits, plan levels and sizes match the P6 harness replayed on the same archived bars, trial to date. "agreement is vacuous" = no entry or exit on either side this week (normal on quiet weeks) | nothing |
| `-- WARN` | 0 | mismatches this week, all explained (`staleness_guard`, `bot_gap`, `fetch_failure`, ...) and the trial-to-date budget (<= 10%) holds | read the listed causes (an outage over midnight, a failed fetch); fix the cause if it is operational |
| `-- FAIL` | 1 | an **unexplained** mismatch anywhere in the trial to date, or the explained mismatches exceed the 10% budget | **the trial is paused** (below) |
| `error: ...` | 2 | corrupt or missing input (audit log, ledger, bar archive, config), or the run hit the midnight window | re-run outside 23:50-00:15 UTC; if it persists, the logs need repair: bring it to the Architect |

A typical PASS looks like:

```
PAPER TRIAL PARITY CHECK -- PASS
the bot's decisions match the harness's
window: 2026-10-14 .. 2026-10-20  (7 decided bars, 16 symbols)
trial start: 2026-10-14 (earliest bar_close in the audit log)
entries : harness 2, bot 2, matched 2, mismatched 0; agreement 100.0%, mismatch 0.0% (budget 10%)
exits   : harness 1, bot 1, matched 1, mismatched 0; agreement 100.0%, mismatch 0.0% (budget 10%)
...
```

**FAIL = the trial is paused** (design §5 "Parity"): an unexplained mismatch is an *implementation* finding, not a
strategy verdict. Leave the bot running (more evidence for the diagnosis), do not edit the config, do not count
trades after the mismatch toward any trial verdict, and bring the JSON report to the Architect. Once the cause is
understood and fixed, the mismatch is acknowledged explicitly with `--ack BAR:SYMBOL:KIND`
(`docs/paper_parity_check.md`); whether a fix restarts the trial is your decision. `--relaxed-early-budget` is a
loosening of the pre-registered rule and is **not** accepted; don't use it unless you decide to accept it.

**Restarts and outages.** Do restarts outside 23:50-00:15 UTC. A restart mid-day is harmless (the ledger keeps
every decision). If the bot is down at 00:00 UTC for more than an hour (including an OOM kill, section 11), that
day's entries are skipped as stale on the next cycle (exits still execute): the check reports `staleness_guard`
(or `bot_gap` if no symbol was decided at all), explained but counted against the 10% budget. The heartbeat
(deploy/README.md) is what tells you the bot is down; keep it configured.

---

## 7. Interim and final reports

The Architect writes an interim report every 10 closed trades and a final report from `logs/trades.csv` plus the
audit log (design §5 "Who decides"). Count closed trades with
`grep -c ',sell,' logs/trades.csv`. Trades are re-costed at the venue and fee tier you chose (section 1 item 3),
not at the paper broker's 0.10%. Duration: until **50 closed trades or 18 months**, whichever comes first,
minimum 6 months (expected 8-12 months at 4-6 trades a month).

---

## 8. Kill rule (catastrophe guard): USER MUST CONFIRM BEFORE DAY 0

**Recommended (freeze this unless you decide otherwise):** stop if mean re-costed R falls below
**-0.57 at 20 trades, -0.43 at 30, -0.34 at 40, or -0.30 at 50**, or if paper equity drawdown exceeds **30%**.

Why these numbers. Design §5 pre-registered -0.70 / -0.55 / -0.45 / -0.40 (the 5th percentile, at each check, of a
true +0.18R strategy under whole-entry-day resampling; they replaced the first draft's `0.18 - 1.645 x sigma /
sqrt(n)` formula) and also said: "The kill thresholds and power below must be re-simulated on the re-gated trade
lists before the protocol is frozen." Re-run with the design's own method on the P6-fixed, $10-floor `data/` trade
list (132 trades), the 5th-percentile thresholds are the recommended set: false-stop rate 11% (the design's
intended level), power against a true -0.24R strategy 52-54% (independent review: 11% / ~52%; this branch: 11% /
54%). The design's set applied to the same corrected pool would false-stop only ~5% and catch -0.24R only ~40-42%
of the time, i.e. looser than the design intended. The same method on the original trade list reproduces the
design's numbers. Details: `docs/daily_trial_preflight.md` §5.

Either way it is a guard against gross failure, not a test: no 50-trade rule separates +0.18R from -0.24R well.
Equity drawdown is on the dashboard's Risk view ("Current Drawdown"). If a kill condition fires, the trial stops;
what happens next (pause with `KILL_SWITCH`, roll back, keep paper running without trial status) is your decision.

---

## 9. Rolling back to 1h paper

**Why you might:** a parity FAIL that can't be explained or fixed quickly, a bug in the bar-parity path, a kill
condition, or simply ending the trial. Remember what 1h is: its re-baselined net expectancy is **-0.24R, 95% CI
[-0.35, -0.13]** (design E1). Rolling back means paper-trading a strategy already shown to be negative; its BUY
signals must not be traded (design §3.3).

**Immediately (no code change):** `touch KILL_SWITCH` in the repo root stops every new entry on the next cycle; open
positions keep their stop-loss / take-profit checks. This is reversible (`rm KILL_SWITCH`).

**The rollback itself is a PR to main, never a local edit on the VM, and the same ordering applies as for the
switch: stop and disable first, merge second** (otherwise the VM auto-pulls the 1h config under the daily state).
The values to restore, all together (`closed_bars_dir` and `max_entry_delay_minutes` are startup errors without
`bar_parity: true`):

```yaml
market:
  timeframe: "1h"
  # symbols: keep */USD (works at 1h too) or restore the 1h-era */USDT list: your choice, in the same PR
loop:
  bar_parity: false
  max_entry_delay_minutes: null
notifier:
  label: null
logging:
  closed_bars_dir: null
  retention_days: 30
```

(the simplest form is `git revert` of this change's config commits), plus updating `tests/test_paper_trial_config.py`
in the same PR, which pins the trial values on purpose. Order, outside 23:50-00:15 UTC:

```bash
touch KILL_SWITCH
sudo systemctl stop hybrid-crypto-trader && sudo systemctl disable hybrid-crypto-trader
.venv/bin/python scripts/reset_portfolio.py      # check "Archived to logs/archive/<stamp>:"; IGNORE its restart hint
ARCH=logs/archive/<stamp-printed-above>
for f in logs/market_snapshot.json logs/bar_decisions.json logs/daily_bars; do [ -e "$f" ] && mv "$f" "$ARCH"/; done
# now merge the rollback PR on GitHub, then:
git checkout -- web/data.json; git pull --no-rebase --no-edit
PYTHONPATH=. .venv/bin/python scripts/validate_invariants.py   # 7/7, bar_parity=False, label=None
rm KILL_SWITCH                                                   # only if you want 1h paper entries again
sudo systemctl enable --now hybrid-crypto-trader
```

Archive (don't delete) the trial's logs: they are the trial's record. The reset is needed because the trial's
daily-ATR positions would otherwise be monitored with 1h-era expectations and the 1h paper record would start from
the trial's equity.

---

## 10. Dashboard expectations

- `web/data.json` is regenerated and pushed by the VM after **every** cycle (~5 minutes), as before: the
  `generated_at` time keeps moving even though the bot's decisions only change once a day.
- **Prices are the last daily close**, up to 24 hours old, and the annotated chart for an active plan shows **daily
  candles** (the last 150). There is no "as of last daily close" label yet (a display follow-up in design §6).
  The TradingView widget is TradingView's own live 15-minute chart, not the bot's bars.
- Portfolio starts at 10,000 cash after the reset. P/L defaults to the **Hourly** tab, which is mostly empty at 1d:
  use **Daily** / **Weekly**.
- The pipeline "office" shows roles as working only for ~15 minutes after the 00:00 UTC decisions; idle the rest of
  the day is normal. The Compliance Reviewer shows disabled (the advisor is off).
- Signal history (14 days) shows planned / filled / closed / rejected events from the midnight cycles.

---

## 11. Disk and memory

Measured with the trial config (16 symbols, 300 s poll): the audit log grows **~1.15 MB/day** (~35 MB/month,
~0.63 GB at the 18-month maximum), mostly "already decided" lines; the closed-bar archive and plan history are
negligible. Disk is not the problem. **Memory is**:

- `scripts/export_dashboard_data.py` (run after every cycle) holds the whole audit log as text, then a list of its
  line strings (~1.2x the file), then up to the last 200,000 parsed events (a fixed ~220 MB floor once the log is
  older than ~43 days). Peak: **~300 MB at 2 months, ~480 MB at 6 months, ~1 GB at 18 months**, on top of the bot
  process itself (~150-250 MB).
- `scripts/paper_parity_check.py` parses every audit line: about **1.1 KB of RAM per line**, i.e. ~5 MB per trial day
  (~0.9 GB after 6 months, ~2.7 GB after 18).

**6 GB Ampere shape:** fine for the whole trial; still watch `free -m` weekly.

**1 GB `E2.1.Micro`:** run the weekly parity check **off the VM from day 1**: copy `logs/` and `config/` to another
machine with the repo and point the check at them (`--config`, `--audit-log`, `--trade-log`, `--bar-decisions`,
`--bars-dir`). Expect the dashboard export to be OOM-killed after **~4-6 months** unless the export is changed to
read only the tail of the audit log (a Platform Engineer follow-up, not made here). An export OOM by itself only
stops the dashboard, but an OOM kill of the bot process over midnight skips that day's entries and costs parity
budget (section 6).

Do **not** turn pruning back on mid-trial (`retention_days: 0` is a trial prerequisite) and do not delete or rotate
`logs/audit_log.jsonl`.

---

## 12. When the trial ends

The final report (design §5 "Confirmed / Not falsified") is written from the logs; nothing is promoted
automatically. Then: set `retention_days` back to 30 (or archive the audit log first), decide what runs next, and
re-open the Routine's promotion PRs for review if you want them evaluated.
