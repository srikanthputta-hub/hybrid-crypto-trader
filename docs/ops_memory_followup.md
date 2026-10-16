# Ops memory follow-up: bounded-memory export and parity check, dashboard at a daily bar

- **Role:** Platform Engineer (Sonnet). Branch `claude/ops-memory-and-1d-dashboard`, based on `origin/main`
  (`b7e8941`). Local only: not pushed, no PR, not merged. Paper only; no change to `src/risk_engine.py`,
  `src/broker_live.py`, the two-switch gate, any config value, or any decision the bot makes.
- **Why this file exists:** the pre-flight this work answers (`docs/daily_trial_preflight.md`, §3 / §6 item 4 / §8
  item 7) and the runbook (`deploy/PAPER_TRIAL_RUNBOOK.md`, §10 / §11) live on the unmerged PR #106
  (`claude/daily-bar-paper-trial`). **Neither file exists on `main`**, so their edits are written out here
  (section 5) to be applied when #106 lands, instead of creating conflicting copies.

## 1. What was wrong

With `logging.retention_days: 0` the audit log grows ~1.15 MB/day (16 symbols x 288 lines, mostly "already decided"
echoes) for up to 18 months. Two scripts held all of it in memory:

| script | held | consequence |
|---|---|---|
| `scripts/export_dashboard_data.py` (run by `scripts/run_live.py` after every ~5 minute cycle, as a subprocess) | the whole file as text, a list of its lines, up to 200,000 parsed events | OOM-kill risk on a 1 GB VM from ~4-6 months; an OOM over midnight costs parity budget |
| `scripts/paper_parity_check.py` | one parsed dict per line (~1.1 KB each) | ~3.8 GB at 18 months |

## 2. What changed

### 2.1 `scripts/export_dashboard_data.py`: tail-read, bounded memory

The dashboard needs only: the newest event (the Boss card's `last_active`), the newest event matching each role's
marker keys, and the signal-history items (newest first, stopping at 14 days / 3000 items). The export now streams
the file **backwards in 1 MiB chunks** (`_iter_audit_lines_reversed`), parses one line at a time, and reduces as it
goes (`_scan_audit`: one pass, the same events inspected in the same order the old per-role and history scans used).
Nothing is accumulated beyond those few events and the history items. It stops as soon as the history cut-off has
passed and every role has been found; a role that never appears (the advisor, when disabled) keeps the scan going to
the existing 200,000-line cap, which is unchanged and is the only bound on how far back it can read (a 128 MiB byte
ceiling, `AUDIT_TAIL_MAX_BYTES`, additionally bounds run time on a pathological log of enormous lines; at the real
~260 bytes/line the line cap binds first, at ~50 MB).

Line semantics are exactly the old `read_text().strip().splitlines()[-max_lines:]`: split on `str.splitlines()`
separators, trailing whitespace-only lines and trailing/leading whitespace of the last/first content line dropped,
blank lines counted toward the cap. Splitting is on the 0x0A byte before decoding (0x0A never occurs inside a
multi-byte UTF-8 sequence), so a character straddling a chunk edge is decoded whole.

### 2.2 `scripts/paper_parity_check.py`: one streaming pass, compact evidence

`build_report` no longer calls `read_audit`. `scan_audit` makes one pass over the file; every line is still
JSON-parsed and validated (a corrupt line is reported with the same message and line number), and feeds:

- `_BotCollector`: the per-(bar, symbol) groups, as `collect_bot` always built them. The audit line numbers each
  group keeps (they are printed in `duplicate_decision` / `missing_decision_event` messages) are an `array('I')`
  (4 bytes) instead of a list of ints (~36 bytes);
- `AuditEvidence`: the event count, the earliest event time, the "market data unavailable" days, the executed fills,
  and one (wall-clock time, bar) stamp per bar-carrying line as two flat arrays (12 bytes). `stamps()` sorts them
  (skipped when already in time order) and collapses each run of equal bars to its first stamp; the only two
  questions the trade-log cross-check asks of the full list (`_bar_at`, and the first in-range stamp) have the same
  answer on the collapsed list (proved in a randomised test).

The "already decided" echoes (99% of lines) are **not** skipped before parsing, deliberately: they carry the
timestamps and bar closes the cross-checks need, they are the only evidence for `missing_decision_event` (a lost
deciding line whose echoes survive), and a corrupt echo line must still fail with exit code 2. What is gone is
*holding* them: each is parsed, counted and stamped, then dropped.

Error precedence is preserved: the old code read the whole file first, so a corrupt line anywhere beat a
`collect_bot` problem in an earlier line; the scan holds the first such problem back until the file has been read.

### 2.3 Dashboard at 1d (`web/`, plus the export's `display` block)

At a daily bar every price and candle is as of the last bar close, up to 24 hours old. Fixed, display only:

- **"as of" label.** The export surfaces `display.price_as_of` (the close of the newest closed bar behind the
  snapshot: last candle open + one bar). The page shows "prices as of 2026-10-16 00:00 UTC (last daily close)" in the
  top bar, under the Portfolio Overview, and after the annotated chart's title.
- **Label banner.** `display.label` is `notifier.label` (e.g. `PAPER TRIAL`); the page shows a banner
  "PAPER TRIAL · 1d bars" when it is set (also at 1h: the label is the operator's own opt-in).
- **P/L default.** `display.pnl_default_period` is `daily` at a daily-or-longer bar; the P/L modal opens on Daily
  (the Hourly tab is empty at one decision a day). Chosen once at load; the 30 s refresh never resets a tab the
  viewer picked.
- **"working" window for the role cards.** `RECENCY_WINDOW` (15 minutes) assumed a cycle every few minutes doing
  work. At a daily bar the decision pass for a closed bar takes the first hour after the close (the bot's own
  `max_entry_delay_minutes` guard is 60), so the cards now read "working" for `max(15 min, timeframe / 24)`: 15
  minutes for every timeframe up to 6h (**1h is unchanged**), 1 hour at 1d.

The `display` key is written **only** when the timeframe is daily-or-longer or `notifier.label` is set. At the hourly
default with no label, `web/data.json` is byte-for-byte what the previous script wrote, and the page (`index.html` +
`app.js`) leaves every pre-existing element in the same state as the frozen pre-change page.

### 2.4 Comment / doc drift

- `src/bot.py` stage 2b comment: "this 1h signal" now says the signal is 1h by default and the configured
  `market.timeframe` (1d in the trial). **Comment lines only**; `git diff` shows only `#` lines and
  `ast.dump` of the module is identical before and after.
- `strategy_pack_run/run_live_bot_report.py` has a fixed "Important limits" paragraph saying `config/config.yaml`
  runs `market.timeframe: "1h"`. **Not changed**: the committed reports are reproduced byte-for-byte from that text
  (the pre-flight diffed four of them), so editing it would invalidate the byte-reproducibility check. Once the
  daily-trial config (#106) is on `main` that sentence is stale for new runs; treat it as describing the config *at the
  time the report was written* (the committed reports were all written with `1h` in `config.yaml`). If a future
  report must carry current wording, regenerate and re-commit all of them together.

## 3. Measured memory (synthetic log shaped like the trial's)

Generated by a script reproducing the real shape: 16 symbols x 288 ticks/day, the first tick of each day a decision
line (some with plan / fill / exit / rejection), the rest ~270-byte "already decided" echoes. 540 days = 2,488,320
lines, 641 MB (1.19 MB/day, the measured rate; the 180-day file is the ~6-month point). Peak RSS from
`resource.getrusage`, one fresh process per run, on a 16 GB / 4 core dev VM. "Delta" is peak minus the RSS right after
the imports (28 MB for the export, 98 MB for the parity check, which imports pandas).

**Dashboard export** (the audit-log part: read + role cards + history; the rest of the export is a few MB):

| log | before: peak RSS (delta) | after: peak RSS (delta) | time before / after |
|---|---|---|---|
| 60 days, 72 MB | 346 MB (+318) | 34 MB (+5) | 4.3 s / 1.1 s |
| 180 days, 214 MB | 520 MB (+491) | 34 MB (+5) | 3.8 s / 1.1 s |
| 540 days, 641 MB | 1450 MB (+1422) | 33 MB (+5) | 14.4 s / 1.1 s |

The before numbers match the pre-flight's estimate (~300 MB / ~480 MB / ~1 GB). After: constant, independent of
the log's size (the cap is 200,000 lines, ~50 MB of file read).

**Parity check** (the audit-log part: read + group + trade-log evidence; the harness replay and the bar archive are
unchanged and small next to this):

| log | before: peak RSS (delta) | after: peak RSS (delta) | time before / after |
|---|---|---|---|
| 60 days, 276k lines | 516 MB (+418) | 105 MB (+6) | 11 s / 3 s |
| 180 days, 829k lines | 1357 MB (+1259) | 115 MB (+17) | 41 s / 11 s |
| 540 days, 2.49M lines | 3855 MB (+3757) | 148 MB (+50) | 127 s / 33 s |

What still grows: ~20 bytes per audit line (12 for the stamp, 4 for the line number, array over-allocation) plus a
few hundred bytes per (bar, symbol); about 50 MB at 18 months, against ~3.8 GB before. Time is still linear in the
file (every line is parsed); at 18 months the weekly check takes about half a minute for the audit part.

## 4. Equivalence evidence and the deliberate differences

Frozen copies of the previous scripts (`tests/_legacy_export_dashboard_data.py`, `tests/_legacy_paper_parity_check.py`,
and the previous page in `tests/_legacy_web/`) are the "before" in these tests; all run locally (CI is out of Actions
minutes until 2026-11-01):

- `tests/test_export_audit_tail.py`: the line reader vs the old whole-file read on an edge corpus (empty file, only
  newlines, no trailing newline, partial last line, corrupt lines, CRLF / lone CR / `\x0b \x0c \x85  ` separators,
  leading blank lines and non-breaking spaces, BOM, non-dict JSON, a 3 MB line, multi-byte characters), at nine chunk
  sizes from 1 byte to 1 MiB and six line caps, plus 400 randomised files in the suite (and 30,000 in a one-off run,
  0 differences); the reducers vs the old `_role_statuses` / `_signal_history` on generated logs below, at and above the
  line cap and with every history cut-off; the **whole `main()` run old vs new, `web/data.json` compared byte for
  byte**, on a multi-month log (advisor off and on), a 210,000-line log over the real 200,000 cap (the advisor events
  straddle the cap boundary), the bot loop's own trial log, and missing / empty logs.
- `tests/test_paper_parity_scan.py`: the groups, evidence and `check_trade_log` vs the old functions on the trial
  fixture; the stamp collapse against the full sorted list on 200 random stamp sets; whole reports (cleaned JSON and
  text) identical on the trial, on 12 mutated copies of it (lost deciding line, duplicated decision, only echoes
  left, a fetch failure, the END of the log dropped, a missing symbol, a bad `bar_close`, a malformed risk decision,
  naive / odd / missing timestamps, an unseen fill), on trade-log and option variants, on odd audit files (empty,
  corrupt, non-dict, bad UTF-8), and on a synthetic long log (the trial's decisions followed by 120 echo ticks per
  bar and symbol, with monotonic and with interleaved wall-clock stamps).
  `tests/test_paper_parity_check.py` is untouched and passes.
- `tests/test_dashboard_display.py`: the export at 1h (byte-identical; with a label, identical but for the `display`
  key), at 1d (identical but for `display` and exactly the role statuses the wider window flips), and the page run in
  node against a DOM stub built from `index.html`'s own ids: at 1h every pre-existing element is in the same state as
  with the frozen old page; at 1d the new elements and the P/L tab are asserted value by value. (Needs `node`; skipped
  without it.)

**Not byte-identical, on purpose, only on corrupt input:**

1. *Invalid UTF-8 in the audit log.* The old export raised `UnicodeDecodeError` and the dashboard stopped refreshing;
   the new reader replaces the bad bytes (the damaged line fails to parse and is skipped like any corrupt line). The
   parity check is unchanged here (it still exits 2 with "cannot read audit log").
2. *The 128 MiB byte ceiling* (export only): only reachable if 200,000 lines exceed 128 MiB (>670 bytes/line on
   average; real lines are ~260). Beyond it the export drops the oldest part of its window instead of reading it.

Unchanged (kept identical on purpose): a non-object JSON line (`123`, `[1]`) in the audit log still makes the
export fail where the old one did; the parity check still rejects it with exit 2.

## 5. Edits to apply to the #106 documents when it merges

**`docs/daily_trial_preflight.md` §3 table, dashboard row:** replace "display follow-up (design §6, Platform
Engineer); runbook §10 tells the operator" with "fixed (docs/ops_memory_followup.md §2.3): 'as of <bar close>' label,
`notifier.label` banner, P/L opens on Daily". The `RECENCY_WINDOW` row: "display: roles show working for the first hour
after the 00:00 UTC decisions (window = timeframe / 24, 15 min at 1h)".

**§6 item 4 ("Memory, not disk"):** replace the memory paragraph with:

> The audit log grows ~1.15 MB/day (16 x 288 lines at ~260 bytes). Both readers are now bounded
> (docs/ops_memory_followup.md): the dashboard export reads the log newest-first in 1 MiB chunks and keeps only what
> the dashboard shows (measured peak RSS 33-34 MB at 2, 6 and 18 months, was 346 MB / 520 MB / 1450 MB); the parity
> check streams the log and keeps ~20 bytes per line (50 MB over the import baseline at 18 months, was 3.8 GB). Memory
> is no longer a constraint on the 1 GB `E2.1.Micro` shape from the audit log; the bot's own ~150-250 MB and the
> harness replay are what remain. The follow-ups this item listed (tail-read in the export; skip echoes in the
> parity check) are done, the second in the form "parse, count and drop" rather than "skip before parsing" (the echoes
> are the integrity evidence for `missing_decision_event`).

**§8 item 5 and item 7:** item 5 (memory growth vs the VM's shape) becomes "fixed, see §6 item 4"; item 7's
follow-ups: the export tail-read and the dashboard "as of" label / daily P/L default are done; the `bot.py` comment is
fixed (comment only); the `run_live_bot_report.py` sentence is deliberately left (section 2.4).

**`deploy/PAPER_TRIAL_RUNBOOK.md` §10:** drop "There is no 'as of last daily close' label yet" (the dashboard now says
"prices as of ... (last daily close)" and shows a `PAPER TRIAL` banner); drop "P/L defaults to the Hourly tab ...
use Daily / Weekly" (it opens on Daily); the office bullet becomes "roles show working for about an hour after the
00:00 UTC decisions".

**§11 ("Disk and memory"):** replace the two memory bullets and the 1 GB `E2.1.Micro` paragraph with:

> Memory is bounded by design now. `scripts/export_dashboard_data.py` reads the audit log from the end in 1 MiB chunks
> (measured 33-34 MB peak RSS at 2, 6 and 18 months of log). `scripts/paper_parity_check.py` streams the log and keeps
> ~20 bytes per line (about 50 MB over its 98 MB import baseline at 18 months, ~33 s for the audit part). Both fit the
> 1 GB `E2.1.Micro` shape, so the weekly check can run on the VM; running it elsewhere is no longer needed for
> memory. Still check `free -m` once before day 0: the bot itself (~150-250 MB) is the largest resident process.

The "do not turn pruning back on mid-trial" paragraph stays.

## 6. For an independent reviewer

1. `_iter_raw_lines_reversed` / `_iter_audit_lines_reversed`: the absolute-offset bookkeeping for the first content
   line, the `+ "\n"` in `lines_of`, the `pieces` join across chunks, and the byte-cap early return (never yields a
   partial line).
2. `_scan_audit` vs the old per-role and history scans: same events inspected in the same order; `_HistoryCollector`
   is the old loop body verbatim (the `break` points are `done`).
3. `AuditEvidence.stamps()` collapse: the claim that `_bar_at` and "first in-range stamp" are unchanged (the randomised
   test is the evidence; check the argument in the docstring).
4. `scan_audit`'s deferred-error behaviour, and that `_BotCollector.feed` is `collect_bot`'s loop body verbatim except
   the bar-string memo and the explicit `line` argument.
5. The `display` block: omitted at 1h with no label (byte-identical), and the page's behaviour with and without it.
6. That nothing in `src/` changed except the comment lines in `src/bot.py` (`ast.dump` identical).
