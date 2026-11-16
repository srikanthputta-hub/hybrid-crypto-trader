#!/usr/bin/env bash
# Read-only health check for the always-on VM (deploy/README.md). Changes nothing: no restart, no git write,
# no config edit, and it never reads .env or prints a secret. Run it from anywhere:
#
#   cd ~/hybrid-crypto-trader && git fetch -q origin main && git show origin/main:scripts/vm_health_check.sh | bash
#
# (that form needs no checkout change, so it works while the bot is running), or `bash scripts/vm_health_check.sh`.
# Each line starts with OK, WARN or INFO; WARN lines are the ones to look at.

REPO="${HCT_REPO:-$HOME/hybrid-crypto-trader}"
SERVICE="${HCT_SERVICE:-hybrid-crypto-trader}"
PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
cd "$REPO" 2>/dev/null || { echo "WARN  repo not found at $REPO (set HCT_REPO)"; exit 1; }

ok()   { echo "OK    $*"; }
warn() { echo "WARN  $*"; WARNS=$((WARNS + 1)); }
info() { echo "INFO  $*"; }
WARNS=0
NOW_UTC=$(date -u '+%Y-%m-%d %H:%M:%S UTC')
echo "=== hybrid-crypto-trader VM health check, $NOW_UTC ==="

echo "--- service"
state=$(systemctl is-active "$SERVICE" 2>/dev/null)
enabled=$(systemctl is-enabled "$SERVICE" 2>/dev/null)
since=$(systemctl show "$SERVICE" -p ActiveEnterTimestamp --value 2>/dev/null)
[ "$state" = "active" ] && ok "service $state since $since" || warn "service is '$state' (expected active)"
[ "$enabled" = "enabled" ] && ok "service enabled (starts on boot)" || warn "service is '$enabled' (expected enabled)"
n=$(pgrep -fc "scripts/run_live.py" 2>/dev/null); n=${n:-0}
[ "$n" = "1" ] && ok "exactly one bot process" || warn "$n bot processes running (expected 1)"

echo "--- config"
mode=$(grep -E '^mode:' config/config.yaml | awk '{print $2}' | tr -d '"')
[ "$mode" = "paper" ] && ok "mode: paper" || warn "mode is '$mode' (expected paper)"
tf=$(grep -E '^\s+timeframe:' config/config.yaml | head -1 | awk '{print $2}' | tr -d '"')
info "timeframe: $tf"
[ -e KILL_SWITCH ] && warn "KILL_SWITCH file present: no new entries will be opened" || ok "no KILL_SWITCH file"
[ -e logs/live_broker_halt.json ] && warn "logs/live_broker_halt.json present (live broker halt marker)" || ok "no live halt marker"
[ -e .env.save ] && warn ".env.save exists (editor backup that may hold old secrets): rm ~/hybrid-crypto-trader/.env.save" || ok "no stray .env.save"
perm=$(stat -c '%a' .env 2>/dev/null)
[ -z "$perm" ] && warn ".env missing" || { [ "$perm" = "600" ] && ok ".env permissions 600" || warn ".env permissions $perm (run: chmod 600 .env)"; }

echo "--- bot activity (logs/)"
"$PY" - <<'PYEOF'
import json, os, time, glob, datetime as dt
def out(tag, msg): print(f"{tag:<5} {msg}")
now = dt.datetime.now(dt.timezone.utc)
try:
    d = json.load(open("logs/bar_decisions.json"))
    bars = d.get("bars", {})
    latest = max(bars.values()) if bars else None
    out("INFO", f"bar decisions: timeframe {d.get('timeframe')}, {len(bars)} symbols, latest bar {latest}")
    if latest:
        age_h = (now - dt.datetime.fromisoformat(latest)).total_seconds() / 3600
        tf = d.get("timeframe")
        limit = 26 if tf == "1d" else 2
        (out("OK", f"latest decided bar is {age_h:.1f} h old") if age_h <= limit
         else out("WARN", f"latest decided bar is {age_h:.1f} h old (expected <= {limit} h for {tf})"))
except Exception as e:
    out("WARN", f"cannot read logs/bar_decisions.json ({type(e).__name__})")
try:
    p = "logs/audit_log.jsonl"
    age_min = (time.time() - os.path.getmtime(p)) / 60
    out("OK" if age_min <= 15 else "WARN", f"audit log last written {age_min:.0f} min ago, size {os.path.getsize(p)/1e6:.1f} MB")
    errors = stale = 0; lines = 0
    with open(p, "rb") as f:
        f.seek(max(0, os.path.getsize(p) - 3_000_000))
        for raw in f.read().splitlines()[1:]:
            try: e = json.loads(raw)
            except Exception: continue
            ts = e.get("timestamp")
            if not ts or (now - dt.datetime.fromisoformat(ts)).total_seconds() > 86400: continue
            lines += 1
            if e.get("error"): errors += 1
            if "stale bar" in str(e.get("note")): stale += 1
    out("OK" if errors == 0 else "WARN", f"last 24 h: {lines} audit events, {errors} with errors, {stale} stale-bar skips")
except Exception as e:
    out("WARN", f"cannot read logs/audit_log.jsonl ({type(e).__name__})")
try:
    rows = [l for l in open("logs/trades.csv") if l.strip()][1:]
    out("INFO", f"trades.csv: {len(rows)} fills recorded")
except FileNotFoundError:
    out("INFO", "trades.csv: none yet (no fills)")
try:
    cur = json.load(open("logs/trade_plans_current.json"))
    plans = cur if isinstance(cur, list) else list(cur.values()) if isinstance(cur, dict) else []
    states = {}
    for p_ in plans:
        s = (p_.get("status") if isinstance(p_, dict) else None) or "?"
        states[s] = states.get(s, 0) + 1
    out("INFO", f"trade plans: {states or 'none'}")
except FileNotFoundError:
    out("INFO", "trade plans: none")
except Exception as e:
    out("WARN", f"cannot read logs/trade_plans_current.json ({type(e).__name__})")
reps = sorted(glob.glob("logs/parity_*.json"))
if reps:
    try:
        r = json.load(open(reps[-1])); st = r.get("status") or r.get("verdict")
        out("OK" if st in ("PASS", "INSUFFICIENT DATA") else "WARN", f"latest parity report {os.path.basename(reps[-1])}: {st}")
        age_d = (time.time() - os.path.getmtime(reps[-1])) / 86400
        if age_d > 8: out("WARN", f"latest parity report is {age_d:.0f} days old: run the weekly check")
    except Exception as e:
        out("WARN", f"cannot read {reps[-1]} ({type(e).__name__})")
else:
    out("WARN", "no logs/parity_*.json report yet: run the weekly parity check")
PYEOF

echo "--- journal (last 24 h)"
J=$(journalctl -u "$SERVICE" --since "24 hours ago" --no-pager 2>/dev/null || sudo -n journalctl -u "$SERVICE" --since "24 hours ago" --no-pager 2>/dev/null)
if [ -z "$J" ]; then
  warn "cannot read the journal (try: sudo bash scripts/vm_health_check.sh)"
else
  e=$(printf '%s\n' "$J" | grep -cE " ERROR |Traceback")
  [ "$e" = "0" ] && ok "no ERROR/Traceback lines" || warn "$e ERROR/Traceback lines (see: journalctl -u $SERVICE --since '24 hours ago' | grep -E 'ERROR|Traceback')"
  t=$(printf '%s\n' "$J" | grep -c "api.telegram.org/bot")
  [ "$t" = "0" ] && ok "no Telegram bot URL in the journal" || warn "$t journal lines contain a Telegram bot URL: rotate the token (deploy/README.md)"
  p=$(printf '%s\n' "$J" | grep -c "dashboard push:")
  info "$p dashboard pushes logged (throttled: ~24/day expected; 0 means the service runs pre-#117 code: restart it)"
  printf '%s\n' "$J" | grep -q "dashboard direct deploy (wrangler): enabled" && info "direct dashboard deploy: enabled" || info "direct dashboard deploy: not enabled (optional)"
  r=$(printf '%s\n' "$J" | grep -c "starting live loop")
  [ "$r" -le 2 ] && ok "$r service (re)starts in 24 h" || warn "$r service (re)starts in 24 h (crash loop?)"
fi

echo "--- git"
br=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
[ "$br" = "main" ] && ok "on branch main" || warn "on branch '$br' (expected main)"
dirty=$(git status --porcelain --untracked-files=no 2>/dev/null | grep -v ' web/data.json$' | wc -l)
[ "$dirty" = "0" ] && ok "no local edits to tracked files (besides web/data.json)" || warn "$dirty tracked files edited locally: git status"
last=$(git log -1 --format='%cr' --author=hybrid-crypto-trader-bot 2>/dev/null)
info "last bot dashboard commit: ${last:-none}"
head=$(git rev-parse --short HEAD); info "checked-out commit $head"
if git fetch -q origin main 2>/dev/null; then
  behind=$(git rev-list --count HEAD..origin/main)
  code_behind=$(git diff --name-only HEAD origin/main -- src scripts config | wc -l)
  ok "GitHub reachable (credentials work)"
  [ "$code_behind" = "0" ] && ok "code is current with origin/main ($behind data-only commits behind)" \
    || info "$code_behind code/config files differ from origin/main: the loop pulls when its next push is rejected; restart the service afterwards to load them"
else
  warn "cannot fetch from GitHub (token expired? see deploy/README.md 'Git push access')"
fi

echo "--- host"
df -h "$REPO" | tail -1 | awk '{print "INFO  disk: "$4" free of "$2" ("$5" used)"}'
use=$(df --output=pcent "$REPO" | tail -1 | tr -dc '0-9'); [ "${use:-0}" -lt 85 ] || warn "disk ${use}% full"
free -m | awk '/Mem:/ {print "INFO  memory: "$7" MB available of "$2" MB"}'
du -sh logs 2>/dev/null | awk '{print "INFO  logs/ size: "$1}'
timedatectl 2>/dev/null | grep -q "synchronized: yes" && ok "clock synchronized" || warn "clock not synchronized (timedatectl)"
[ -f /var/run/reboot-required ] && info "Ubuntu reboot pending (do it outside 23:50-00:15 UTC; the service restarts itself)" || ok "no reboot pending"
command -v node >/dev/null && info "node $(node --version)" || info "node not installed (only needed for the optional direct deploy)"

echo "=== $WARNS warning(s) ==="
[ "$WARNS" = "0" ] && echo "All checks passed." || echo "Look at the WARN lines above."
exit 0
