# Deploying to an always-on host (e.g. Oracle Cloud Always Free)

This makes the bot actually run 24/7 with no gaps, instead of depending on a
scheduler to wake it up periodically. `scripts/run_live.py` runs bot cycles
forever on `config.yaml`'s `loop.poll_interval_seconds`, regenerating and
regenerating `web/data.json` after every cycle and pushing it when it matters (see "Dashboard push throttle" below),
and Telegram alerts (if `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are set)
fire the moment a signal is found, not on the next scheduled tick.

**Running the paper daily-bar trial?** The switch from 1h to daily bars (log archive, portfolio reset, the
00:01-00:45 UTC start window, weekly parity check, kill rule, rollback) is in
[PAPER_TRIAL_RUNBOOK.md](PAPER_TRIAL_RUNBOOK.md). **The VM auto-pulls `main`** (`scripts/run_live.py` runs
`git pull --no-rebase --no-edit` whenever its `web/data.json` push is rejected), so a config change merged to
`main` lands on the VM's disk within about an hour (at its next dashboard push) and takes effect at the next restart of any kind. Stop and
disable the service *before* merging the trial change (or its rollback): runbook section 3, step 1.

## Provisioning the VM (Oracle Cloud "Always Free" tier)

1. Sign up at oracle.com/cloud/free.
2. Compute -> Instances -> Create Instance:
   - Image: Canonical Ubuntu 22.04
   - Shape: "Ampere (Arm)" -> `VM.Standard.A1.Flex` -> 1 OCPU / 6 GB memory
     (comfortably inside the Always Free 4 OCPU / 24 GB aggregate limit).
     Ampere capacity is often exhausted in a given availability domain --
     if creation fails with an out-of-capacity error, try the other two
     ADs before falling back to `VM.Standard.E2.1.Micro` (always available,
     but only 1 GB RAM -- fine for this bot, just tighter).
   - Networking: default VCN, "Assign a public IPv4 address" checked. **On
     a brand-new tenancy with no existing VCN**, the instance wizard's
     inline "Create new virtual cloud network" option has a known bug where
     it creates a subnet the wizard itself then refuses to recognize as
     public, permanently greying out the public-IP toggle. If that happens,
     create the network first instead: Networking -> Virtual cloud networks
     -> Start VCN Wizard -> "Create VCN with Internet Connectivity" (this
     builds a real public subnet + internet gateway in one step), then go
     back to Create Instance and select that VCN's public subnet directly.
   - SSH keys: let it generate a pair for you and click "Download private
     key" -- **verify the file actually landed in your Downloads folder
     before moving on**; if it didn't, don't try to recover it, just
     terminate and recreate the instance using a key pair you generate
     yourself (`ssh-keygen`) and paste the public half in instead, which
     removes any ambiguity about whether the download succeeded.
3. Note the instance's public IP once it's running.

## First-time setup (run these yourself, over SSH -- nothing here should be
## pasted into a chat with anyone, including an AI assistant)

```bash
ssh -i /path/to/your-private-key.pem ubuntu@<PUBLIC_IP>

sudo apt update && sudo apt install -y python3 python3-venv python3-pip git
# Ubuntu 22.04 ships Python 3.10 by default, which this codebase runs fine
# on -- no need for deadsnakes/python3.11 specifically.

git clone https://github.com/srikanthputta-hub/hybrid-crypto-trader.git
cd hybrid-crypto-trader

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env   # fill in EXCHANGE_API_KEY / EXCHANGE_API_SECRET, TELEGRAM_BOT_TOKEN,
            # TELEGRAM_CHAT_ID, etc. -- see .env.example's own comments
```

### Git push access

`run_live.py` commits and pushes `web/data.json` (throttled, below), so this
VM needs its own push credentials. Create a fine-grained GitHub personal
access token scoped to *only this repo*, with **Contents: Read and write**
permission (GitHub -> Settings -> Developer settings -> Personal access
tokens -> Fine-grained tokens), then on the VM:

```bash
git config user.name "hybrid-crypto-trader-bot"
git config user.email "hybrid-crypto-trader-bot@users.noreply.github.com"
git remote set-url origin https://<YOUR_TOKEN>@github.com/srikanthputta-hub/hybrid-crypto-trader.git
```

## Install as a systemd service (survives reboots, restarts on crash)

```bash
sudo cp deploy/hybrid-crypto-trader.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hybrid-crypto-trader
sudo systemctl status hybrid-crypto-trader   # should show "active (running)"
journalctl -u hybrid-crypto-trader -f        # tail live logs
```

## Dashboard push throttle (why the VM no longer pushes every 5 minutes)

Every push to `main` triggers a Cloudflare Workers Build. The loop used to push `web/data.json` every 5-minute cycle
(about 288 builds a day), which used up the monthly build-minute allowance and froze the live dashboard on an old
deployment. Now the export still runs every cycle (it only writes `web/data.json` locally), but the loop commits and
pushes only when

- the dashboard-relevant content changed (portfolio, positions, pending plans, trade stats, closed trades, the
  per-symbol decisions, kill switch, latest-bar health, parity report, the newest equity values), or
- the **heartbeat** is due: at least `HCT_DASHBOARD_PUSH_MINUTES` minutes since the last successful push.

Timestamps and ages (`generated_at`, hourly P/L labels, last audit line, the equity curve's end time) are ignored when
deciding "changed". Expected load: about 24 heartbeat pushes a day plus a few content changes (the daily bar close,
entries and exits), roughly 30 builds a day instead of 288.

- **Env var:** `HCT_DASHBOARD_PUSH_MINUTES`, default `60`. A non-number is ignored (60 is used); anything below `5` is
  raised to `5`. Set it in the systemd unit (`Environment=HCT_DASHBOARD_PUSH_MINUTES=60`) or the VM's `.env`.
- **Apply a change / this code:** the running process loads `run_live.py` once, so restart it:
  `sudo systemctl restart hybrid-crypto-trader`.
- **State:** `logs/dashboard_push_state.json` (last pushed fingerprint and time; gitignored, survives restarts). A
  missing or corrupt file just means "push now". It advances only after a successful push, so a failed push is retried
  on the next cycle.
- **What the dashboard's freshness means now:** "updated N min ago" is the time since the last *publish*, not the last
  bot cycle, so it normally counts up to about the heartbeat (60 min) and resets. The exporter writes the interval into
  `data.json` (`ops.health.push_interval_minutes`) and the page turns amber at 1.5x it (90 min) and red / STALE at
  3x it (180 min); without that key it keeps the old 15 / 60 minute thresholds. A skipped cycle leaves `web/data.json`
  modified in the VM's working tree; that is expected and harmless (the next export rewrites it).

## Direct dashboard deploy (publish without a Cloudflare build)

Even throttled, every git push of `web/data.json` is a Workers Build, and build minutes ran out. With this section's
setup the VM uploads the static site itself: after each export, `scripts/dashboard_deploy.py` runs `wrangler deploy`
from the repo root, which uploads `wrangler.jsonc`'s assets directory (`./web`, including the fresh `web/data.json`)
straight to the `hybrid-crypto-trader` Worker. No build runs, so no build minutes are used.

**It is off until you set both `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`.** Without them nothing changes:
the git push throttle above keeps working as before.

How it behaves:

- **When:** after the cycle's export, when the dashboard content changed (same fingerprint as the push throttle) or the
  heartbeat is due. Heartbeat: `HCT_DASHBOARD_DEPLOY_MINUTES`, default `15`, minimum `5` (garbage is ignored).
- **State:** `logs/dashboard_deploy_state.json` (last successful deploy; gitignored). Missing or corrupt means "deploy
  now". It advances only after a success.
- **Failure:** logged (`dashboard deploy FAILED ...`) and retried no sooner than 5 minutes later, doubling to a maximum of
  60 minutes between attempts while it keeps failing, so a Cloudflare outage cannot cost every cycle time or flood the log.
  The first success resets this.
- **Safety:** `wrangler` runs with a hard 120 s timeout (the whole process group is killed), never two at once, and
  nothing in this step can raise into the trading loop. The loop waits for a deploy at most that long; a normal deploy
  takes a few seconds. wrangler gets only the two Cloudflare variables plus `PATH`/`HOME`/proxy settings in its
  environment (not your exchange, Telegram or Claude secrets), the credentials are never put on the command line, and
  anything wrangler prints is scrubbed of them before the short tail is logged.
- **Freshness chip:** while enabled, the exporter also writes `ops.health.deploy_interval_minutes` and the page's
  "updated N min ago" turns amber at 1.5x and red at 3x that heartbeat (floors 15 / 60 min); without it the page falls
  back to `push_interval_minutes`, then to 15 / 60.
- **Git pushes stay:** the hourly/content-change commit of `web/data.json` is kept as optional history, because the
  orchestrator reads `web/data.json` from git for monitoring. Once you complete step 5 below, those data-only commits no
  longer trigger a build.

### One-time VM setup

1. **Node.js LTS and wrangler** (Ubuntu 22.04). The Ubuntu `nodejs` package is too old for current wrangler, so use NodeSource:
   ```bash
   curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
   sudo apt-get install -y nodejs
   node --version   # current wrangler needs a recent LTS (v20+)
   ```
   Then install wrangler, pinned to a major you have tried (check `npm view wrangler version` and pick the current one):
   ```bash
   # option A, system-wide (found on PATH):
   sudo npm install -g wrangler@4
   # option B, project-local (found as node_modules/.bin/wrangler; node_modules/ is gitignored):
   cd ~/hybrid-crypto-trader && npm install --no-save wrangler@4
   ```
   `scripts/dashboard_deploy.py` looks for `HCT_WRANGLER_BIN`, then `./node_modules/.bin/wrangler`, then `wrangler` on
   `PATH`, then `npx --no-install wrangler`. (It never lets `npx` download anything on its own.)
2. **Create a narrowly scoped Cloudflare API token.** Cloudflare dashboard -> My Profile -> API Tokens -> Create Token
   -> start from the **Edit Cloudflare Workers** template, then trim it to exactly:
   - Permissions: **Account -> Workers Scripts -> Edit** (this is the one that matters for `wrangler deploy` of a
     static-assets Worker). Remove everything else the template adds (Workers Routes, KV, R2, D1, Zone, User ->
     User Details ...) unless `wrangler deploy` later says it needs one -- and then add only that one.
   - Account Resources: **Include -> your single account** (not "All accounts").
   - Zone Resources: none needed; do not grant any.
   - Optionally add a client IP filter with the VM's public IP, and a TTL if you want forced rotation.
   Copy the token once; Cloudflare will not show it again. Your **Account ID** is on the Workers & Pages overview page
   (right-hand sidebar) or in the dashboard URL.
3. **Put both values in the VM's `.env`** (gitignored; type them yourself over SSH, never paste them into a chat):
   ```bash
   cd ~/hybrid-crypto-trader
   chmod 600 .env
   nano .env
   # CLOUDFLARE_API_TOKEN=...
   # CLOUDFLARE_ACCOUNT_ID=...
   # HCT_DASHBOARD_DEPLOY_MINUTES=15     (optional)
   ```
   `.env.example` carries commented placeholders only. Never commit `.env`.
4. **Verify before enabling the service:**
   ```bash
   set -a; . ./.env; set +a      # exports the two variables into this shell only
   wrangler whoami               # (or: npx --no-install wrangler whoami) -- should list your account
   wrangler deploy               # a manual deploy from the repo root; prints the Worker URL on success
   ```
   If `whoami` shows the account and the deploy succeeds, the token has what it needs.
5. **Cloudflare dashboard -> Workers & Pages -> `hybrid-crypto-trader` -> Settings -> Builds -> Build watch paths:**
   add **Exclude paths: `web/data.json`**. Pushes that only change that file then no longer start a build, so the git
   history of data commits costs no build minutes. (Code changes you merge on purpose still build as before.)
6. **Restart the service** so the loop picks up the new code and `.env`:
   `sudo systemctl restart hybrid-crypto-trader`, then `journalctl -u hybrid-crypto-trader -f`. The start-up line says
   `dashboard direct deploy (wrangler): enabled, heartbeat 15 min`, and each deploy logs
   `dashboard deploy ok in N.Ns (...)`. The values themselves are never logged.

### Disable it

Remove (or comment out) `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` in `.env` and restart the service. With
either one missing the deploy step is a silent no-op and the exporter stops writing `deploy_interval_minutes`.

### Rotate the token

1. Cloudflare dashboard -> My Profile -> API Tokens -> create a new token exactly as in step 2 (or use **Roll** on the
   existing token, which keeps the permissions and shows a new secret once).
2. Put the new value in `.env` (`nano .env`), keep `chmod 600 .env`, `sudo systemctl restart hybrid-crypto-trader`.
3. Confirm the next deploy line says `ok`, then **delete** the old token in the dashboard (skip this if you used Roll;
   the old secret is already dead).
4. Rotate immediately if the token may have been exposed: check `journalctl -u hybrid-crypto-trader` and `logs/` for it
   (the code scrubs it from its own logs, but a manual `wrangler` run in a recorded shell, or a pasted `.env`, is yours
   to check).

## Heartbeat monitor (dead-man's-switch for the VM/process itself)

The bot's own Telegram alerts (`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`) can only
fire from inside a running process -- they can't tell you if the VM crashes,
loses power, or the `hybrid-crypto-trader` process itself dies. An external
heartbeat monitor closes that gap: `scripts/run_live.py` pings a URL once per
loop iteration (regardless of whether that cycle traded anything), and the
monitoring service alerts you the moment those pings stop arriving.

1. Sign up free at [healthchecks.io](https://healthchecks.io) (no card
   required).
2. Create a new check, e.g. named `hybrid-crypto-trader-heartbeat`.
3. Set its schedule to a **Simple** period matching roughly 2x your
   `poll_interval_seconds` (e.g. 10 minutes for the default 300s/5min loop) --
   this tolerates one slow/failed cycle without a false alarm, but still
   catches a genuinely dead process within two missed cycles.
4. Copy the check's **ping URL** (`https://hc-ping.com/<uuid>`).
5. On the VM, add it to `.env`:
   ```bash
   nano .env
   # HEALTHCHECK_PING_URL=https://hc-ping.com/<uuid>
   ```
   then `sudo systemctl restart hybrid-crypto-trader`.
6. In healthchecks.io's check settings, add an **Integration** for however you
   want to be alerted -- email is on by default; Telegram, Slack, etc. are
   also available under Integrations if you'd rather be paged there instead
   (or in addition).

Leave `HEALTHCHECK_PING_URL` blank to disable -- `send_heartbeat_ping()` is a
no-op with no URL set, same fail-safe pattern as the Telegram notifier.

## Before you flip this on: disable GitHub Actions' own schedule

`.github/workflows/refresh-dashboard.yml`'s `schedule:` trigger (and, if
you've set it up, the Cloudflare Cron Trigger that dispatches it) would
otherwise run a **second, independent** bot process against the same paper
portfolio at the same time as this VM -- two processes each thinking they
own the portfolio state, racing to push `web/data.json`. Once this VM is
running, either:

- Delete/comment out the `schedule:` block in `refresh-dashboard.yml` (keep
  `workflow_dispatch:` for the occasional manual on-demand run), or
- Disable the workflow entirely from GitHub's Actions tab ("..." -> Disable
  workflow).

State (portfolio cash, open positions, cooldowns) lives in `logs/` on this
VM's local disk and persists naturally across restarts -- unlike
`refresh-dashboard.yml`'s ephemeral CI runners, there's no `actions/cache`
dance needed here.

## Updating the code later

```bash
cd ~/hybrid-crypto-trader
git checkout -- web/data.json   # if the VM's dashboard file is modified
git pull --no-rebase --no-edit   # Ubuntu 22.04's git 2.34 errors on divergent branches with a plain pull
sudo systemctl restart hybrid-crypto-trader
```

Note the loop already pulls `main` on its own (above), so "updating" is often just the restart. A change to
`config/config.yaml` that changes the trading cycle (like the paper-trial switch) needs the runbook's ordering instead.

### After deploying the notifier/heartbeat log-redaction fix

Earlier versions logged the full exception text on a failed Telegram send (and
a failed heartbeat ping). `requests` puts the request URL in that text, so the
Telegram bot token (`.../bot<TOKEN>/sendMessage`) and the healthchecks.io ping
URL (its UUID works as a credential) could have been written to `logs/` and
`journalctl`. Once this fix is deployed:

1. Check for leaks: `grep -rl 'api.telegram.org/bot' logs/` and
   `journalctl -u hybrid-crypto-trader | grep -c 'api.telegram.org/bot'` (also
   grep for your healthchecks.io ping UUID). Anything found means the secret
   was exposed.
2. Rotate the Telegram bot token: in BotFather send `/revoke`, pick the bot,
   then put the new token in `.env` (`TELEGRAM_BOT_TOKEN`) and restart the service.
   If your healthchecks.io URL showed up, regenerate the check's ping URL too
   and update `HEALTHCHECK_PING_URL`.
3. Delete the old logs that contain the secret (and vacuum the journal:
   `sudo journalctl --vacuum-time=1s`, or `--rotate` first). Don't paste log
   contents anywhere while doing this.
