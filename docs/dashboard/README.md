# Dashboard (web/)

Static, no build step, no CDN at runtime. `web/index.html` + `style.css` + `theme.js` + `display.js` + `app.js`, the
vendored `vendor/lightweight-charts` (only for the per-symbol candle chart), and JSON written by the VM.

## Data flow

- `scripts/export_dashboard_data.py` (run by `scripts/run_live.py` after every cycle) writes `web/data.json`; the
  VM commits+pushes it only on a content change or an hourly heartbeat (`scripts/dashboard_push.py`, `deploy/README.md`
  "Dashboard push throttle"), and `ops.health.push_interval_minutes` tells the page how stale is stale. When the VM
  also deploys `web/` straight to Cloudflare (`scripts/dashboard_deploy.py`, off unless `CLOUDFLARE_API_TOKEN` and
  `CLOUDFLARE_ACCOUNT_ID` are set; `deploy/README.md` "Direct dashboard deploy") the exporter additionally writes
  `ops.health.deploy_interval_minutes` and the page prefers it for its freshness thresholds. Every
  pre-existing key is unchanged. The additive `ops` key carries what the page needs beyond that: instance identity,
  the per-symbol decision for the latest bar, per-trade R (as booked and re-costed at Kraken T3 taker, the trial's
  judging tier), equity samples, health counters, the kill-switch file and the newest `logs/parity_*.json`.
  Building `ops` is wrapped: if it fails the key is simply missing and the page says "no data".
- Equity history: the bot keeps no mark-to-market history, so the exporter appends a sample to
  `logs/dashboard_equity.jsonl` whenever equity/cash changed (and at least every 6 h), seeded at the audit log's first
  line with the starting cash. The curve therefore starts from the first export after this change, not from day 0.
- `web/instances.json` lists the bot instances (one tab each, `#id` in the URL selects one) and each one's kill rule.
  The frozen daily-trial thresholds (-0.57 / -0.43 / -0.34 / -0.30 at 20 / 30 / 40 / 50 trades, drawdown 30%) live
  there and nowhere else; `tests/test_dashboard_page.py` / `test_dashboard_display_js.py` pin how they are applied.
- A data file name must match `[A-Za-z0-9._-]+.json` (no path, no scheme), so the manifest cannot point the page
  anywhere outside its own directory.

## Adding the intraday instance later

1. On its machine run `python scripts/export_dashboard_data.py --config <its config.yaml> --out web/data_intraday.json
   --instance-id intraday --instance-name Intraday` (and `git add` that file in its own push loop).
2. Add `{ "id": "intraday", "name": "Intraday", "data": "data_intraday.json", "kill_rule": { ... } }` to
   `web/instances.json` (omit `kill_rule` to show "no rule"). Nothing else changes; no code edit.

## Checks

- `tests/test_export_ops.py`: the `ops` block (classification, R maths against the harness formula, equity samples,
  parity summary, CLI, failure isolation). `tests/test_dashboard_display_js.py`: every derivation in `display.js`
  under node. `tests/test_dashboard_page.py`: the page in real Chromium with `_headers` (CSP) applied.
- `python tests/dashboard_fixture.py /tmp/fx && node docs/dashboard/shoot.cjs docs/dashboard /tmp/fx` regenerates the
  screenshots here (also reports console errors, CSP violations, off-origin requests, horizontal overflow).
