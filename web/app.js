// Static dashboard renderer. Reads ./data.json (produced by
// scripts/export_dashboard_data.py) and renders the office view, the
// TradingView lightweight-charts panel, and the data tables. No build step,
// no framework — this is meant to be dropped straight onto Cloudflare Pages.

const fmtMoney = (n) => (n == null ? "—" : `$${Number(n).toLocaleString(undefined, { maximumFractionDigits: 2 })}`);
const fmtPct = (n) => (n == null ? "—" : `${(n * 100).toFixed(2)}%`);
const fmtPrice = (n) => (n == null ? "—" : Number(n).toLocaleString(undefined, { maximumFractionDigits: 6 }));
const fmtTime = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString();
};
const stars = (n) => {
  if (n == null) return "—";
  const full = Math.round(n);
  return `${"★".repeat(full)}${"☆".repeat(5 - full)} (${Number(n).toFixed(1)})`;
};

// What each desk owns — static, mirrors the roster in CLAUDE.md / README.
const ROLE_OWNS = {
  boss: "Whole repo. Reads each request, routes it to the owning role below, sizes complexity, and picks the model tier — Opus always for anything touching risk_engine.py or broker_live.py.",
  market_analyst: "src/scanner.py, src/signal_engine.py, src/indicators.py — the 5 pattern detectors, indicator math, and confluence filtering.",
  trade_strategist: "src/trade_planner.py — entry zone, stop loss, take-profit, invalidation level, and the rule-based 1-5 star confidence score.",
  risk_officer: "src/risk_engine.py — position sizing, exposure cap, max loss per trade, daily loss limit, volatility filter, cooldown, kill switch. Always reviewed at Opus.",
  compliance_reviewer: "src/claude_advisor.py — the optional Claude second opinion. APPROVE/VETO only, never originates or resizes a trade, fails closed on error.",
  execution_trader: "src/broker_paper.py, src/broker_live.py, src/monitor.py, src/trade_plan_store.py — order execution and the PENDING/OPEN/CLOSED lifecycle. broker_live.py is always Opus-reviewed.",
};

function renderRoles(roles) {
  const container = document.getElementById("roles");
  container.innerHTML = "";

  const boss = roles.find((r) => r.id === "boss");
  const others = roles.filter((r) => r.id !== "boss");
  const workingCount = roles.filter((r) => r.status === "working").length;
  document.getElementById("office-status").textContent = `${workingCount}/${roles.length} working`;

  const radiusPct = 40;
  others.forEach((role, i) => {
    const angle = (2 * Math.PI * i) / others.length - Math.PI / 2; // start at top, clockwise
    const x = 50 + radiusPct * Math.cos(angle);
    const y = 50 + radiusPct * Math.sin(angle);
    container.appendChild(buildDesk(role, { x: `${x}%`, y: `${y}%` }));
  });

  if (boss) {
    container.appendChild(buildDesk(boss, { x: "50%", y: "50%" }, true));
  }
}

function buildDesk(role, pos, isBoss) {
  const initials = role.label.split(" ").map((w) => w[0]).slice(0, 2).join("");
  const tierClass = `tier-${role.model_tier}`;
  const desk = document.createElement("div");
  desk.className = "desk" + (isBoss ? " desk-boss" : "");
  desk.style.setProperty("--x", pos.x);
  desk.style.setProperty("--y", pos.y);
  desk.dataset.roleId = role.id;
  desk.innerHTML = `
    <div class="desk-avatar ${tierClass}">${initials}</div>
    <div class="desk-label">${isBoss ? '<span class="desk-crown">&#128081;</span>' : ""}${role.label}</div>
    <div class="desk-status">
      <span class="status-dot ${role.status === "working" ? "working" : ""}"></span>
      ${role.status === "working" ? "working" : "idle"}
    </div>
  `;
  desk.addEventListener("click", () => showRoleDrawer(role, isBoss));
  return desk;
}

function showRoleDrawer(role, isBoss) {
  document.querySelectorAll(".desk").forEach((d) => d.classList.toggle("selected", d.dataset.roleId === role.id));

  document.getElementById("role-drawer-crown").hidden = !isBoss;
  document.getElementById("role-drawer-label").textContent = role.label;
  document.getElementById("role-drawer-status").innerHTML = `
    <span class="status-dot ${role.status === "working" ? "working" : ""}"></span>
    ${role.status === "working" ? "working" : "idle"}
    · ${role.last_active ? "last active " + fmtTime(role.last_active) : "no activity yet"}
    · ${role.model_tier.toUpperCase()} tier
  `;
  document.getElementById("role-drawer-owns").textContent = ROLE_OWNS[role.id] || "";

  const drawer = document.getElementById("role-drawer");
  drawer.hidden = false;
}

function hideRoleDrawer() {
  document.getElementById("role-drawer").hidden = true;
  document.querySelectorAll(".desk").forEach((d) => d.classList.remove("selected"));
}

document.getElementById("role-drawer-close").addEventListener("click", hideRoleDrawer);

function renderSummary(data) {
  const modeBadge = document.getElementById("mode-badge");
  modeBadge.textContent = data.mode;
  modeBadge.className = `badge mode-${data.mode}`;

  document.getElementById("generated-at").textContent = `snapshot: ${fmtTime(data.generated_at)}`;

  document.getElementById("stat-equity").textContent = fmtMoney(data.portfolio.equity);
  document.getElementById("stat-cash").textContent = fmtMoney(data.portfolio.cash);

  const pnlEl = document.getElementById("stat-pnl");
  pnlEl.textContent = fmtPct(data.portfolio.pnl_pct);
  pnlEl.className = "stat-value " + (data.portfolio.pnl_pct > 0 ? "positive" : data.portfolio.pnl_pct < 0 ? "negative" : "");

  document.getElementById("stat-positions").textContent = `${data.positions.length} / ${data.pending.length}`;
}

function renderTable(tableId, rows, columns, emptyMessage) {
  const tbody = document.querySelector(`#${tableId} tbody`);
  tbody.innerHTML = "";
  if (!rows.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="${columns.length}">${emptyMessage}</td></tr>`;
    return;
  }
  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = columns.map((col) => `<td>${col(row)}</td>`).join("");
    tbody.appendChild(tr);
  }
}

function renderPositions(positions) {
  renderTable(
    "table-positions",
    positions,
    [
      (r) => r.symbol,
      (r) => r.pattern ?? "—",
      (r) => fmtPrice(r.fill_price),
      (r) => fmtPrice(r.stop_loss),
      (r) => fmtPrice(r.take_profit),
      (r) => fmtPrice(r.invalidation),
      (r) => (r.unrealized_pnl_pct == null ? "—" : `<span style="color:${r.unrealized_pnl_pct >= 0 ? "#22c55e" : "#ef4444"}">${fmtPct(r.unrealized_pnl_pct)}</span>`),
      (r) => `${stars(r.confidence_rule_based)} / ${stars(r.confidence_claude)}`,
    ],
    "No open positions right now."
  );
}

function renderPending(pending) {
  renderTable(
    "table-pending",
    pending,
    [
      (r) => r.symbol,
      (r) => r.pattern ?? "—",
      (r) => (r.entry_zone ? `${fmtPrice(r.entry_zone[0])} – ${fmtPrice(r.entry_zone[1])}` : "—"),
      (r) => fmtPrice(r.stop_loss),
      (r) => fmtPrice(r.take_profit),
      (r) => fmtPrice(r.invalidation),
      (r) => `${stars(r.confidence_rule_based)} / ${stars(r.confidence_claude)}`,
    ],
    "No pending plans waiting to fill."
  );
}

function renderCandidates(recentCandidates) {
  renderTable(
    "table-candidates",
    recentCandidates,
    [
      (r) => fmtTime(r.timestamp),
      (r) => r.symbol,
      (r) => r.candidates.map((c) => `${c.pattern} (${c.direction}, ${c.strength.toFixed(2)})`).join(", ") || "none",
    ],
    "No scanner activity recorded yet."
  );
}

function renderRiskDecisions(recentRiskDecisions) {
  renderTable(
    "table-risk",
    recentRiskDecisions,
    [
      (r) => fmtTime(r.timestamp),
      (r) => r.symbol,
      (r) => (r.allowed ? '<span style="color:#22c55e">yes</span>' : '<span style="color:#ef4444">no</span>'),
      (r) => r.reason,
    ],
    "No risk engine decisions recorded yet."
  );
}

function renderChart(chart) {
  const emptyEl = document.getElementById("chart-empty");
  const containerEl = document.getElementById("chart-container");
  const confEl = document.getElementById("chart-confidence");
  const titleEl = document.getElementById("chart-symbol");

  if (!chart || !chart.candles || !chart.candles.length) {
    containerEl.style.display = "none";
    emptyEl.style.display = "block";
    titleEl.textContent = "Latest Trade Plan";
    confEl.innerHTML = "";
    return;
  }
  if (typeof LightweightCharts === "undefined") {
    containerEl.style.display = "none";
    emptyEl.style.display = "block";
    emptyEl.textContent = "Chart library failed to load from the CDN (network issue or blocked script) — the tables below still reflect the latest data.";
    titleEl.textContent = `${chart.symbol} — ${chart.plan ? chart.plan.pattern : "no active plan"}`;
    confEl.innerHTML = "";
    return;
  }
  containerEl.style.display = "block";
  emptyEl.style.display = "none";
  titleEl.textContent = `${chart.symbol} — ${chart.plan ? chart.plan.pattern : "no active plan"}`;

  const lwc = LightweightCharts.createChart(containerEl, {
    layout: { background: { color: "transparent" }, textColor: "#e6edf7" },
    grid: { vertLines: { color: "#22314e" }, horzLines: { color: "#22314e" } },
    timeScale: { timeVisible: true, secondsVisible: false },
    width: containerEl.clientWidth,
    height: 360,
  });
  const series = lwc.addCandlestickSeries({
    upColor: "#22c55e", downColor: "#ef4444", borderVisible: false,
    wickUpColor: "#22c55e", wickDownColor: "#ef4444",
  });
  series.setData(chart.candles);

  if (chart.plan) {
    const p = chart.plan;
    const lines = [
      { price: p.take_profit, color: "#22c55e", title: "TAKE PROFIT" },
      { price: p.entry_zone ? p.entry_zone[1] : null, color: "#38bdf8", title: "ENTRY HIGH" },
      { price: p.entry_zone ? p.entry_zone[0] : null, color: "#38bdf8", title: "ENTRY LOW" },
      { price: p.stop_loss, color: "#ef4444", title: "STOP LOSS" },
      { price: p.invalidation, color: "#7b8bab", title: "INVALIDATION" },
    ];
    for (const line of lines) {
      if (line.price == null) continue;
      series.createPriceLine({
        price: line.price, color: line.color, lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true, title: line.title,
      });
    }
    confEl.innerHTML = `
      <span>Risk/Reward: <strong>1 : ${p.risk_reward ?? "—"}</strong></span>
      <span>Rule-based confidence: <span class="stars">${stars(p.confidence_rule_based)}</span></span>
      <span>Claude confidence: <span class="stars">${stars(p.confidence_claude)}</span></span>
    `;
  } else {
    confEl.innerHTML = "";
  }

  lwc.timeScale().fitContent();
  window.addEventListener("resize", () => lwc.applyOptions({ width: containerEl.clientWidth }), { once: true });
}

async function main() {
  let data;
  try {
    const res = await fetch("data.json", { cache: "no-store" });
    data = await res.json();
  } catch (err) {
    document.getElementById("generated-at").textContent = "couldn't load data.json";
    console.error(err);
    return;
  }
  renderSummary(data);
  renderRoles(data.roles);
  renderPositions(data.positions);
  renderPending(data.pending);
  renderCandidates(data.recent_candidates);
  renderRiskDecisions(data.recent_risk_decisions);
  renderChart(data.chart);
}

main();
