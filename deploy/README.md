# Deploying to an always-on host (e.g. Oracle Cloud Always Free)

This makes the bot actually run 24/7 with no gaps, instead of depending on a
scheduler to wake it up periodically. `scripts/run_live.py` runs bot cycles
forever on `config.yaml`'s `loop.poll_interval_seconds`, regenerating and
pushing `web/data.json` after every cycle so the dashboard stays live too,
and Telegram alerts (if `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are set)
fire the moment a signal is found, not on the next scheduled tick.

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

`run_live.py` commits and pushes `web/data.json` after every cycle, so this
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
git pull
sudo systemctl restart hybrid-crypto-trader
```

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
