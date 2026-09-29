// Static dashboard renderer. Reads ./data.json (produced by
// scripts/export_dashboard_data.py) and renders the office view, the live
// market chart (a live TradingView widget by default, switching to an
// annotated lightweight-charts view -- with the selected signal's plan
// levels drawn on it -- when that symbol has an active signal, since
// TradingView's free widget can't draw custom price lines), and the data
// tables. No build step, no framework — this is meant to be dropped
// straight onto Cloudflare Pages.

const fmtPrice = (n) => (n == null ? "—" : Number(n).toLocaleString(undefined, { maximumFractionDigits: 6 }));
const escapeHtml = (s) =>
  String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
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

const STATUS_LABEL = { working: "working", disabled: "disabled" };
const statusLabel = (status) => STATUS_LABEL[status] || "idle";

function renderRoles(roles) {
  const container = document.getElementById("roles");
  container.innerHTML = "";

  const workingCount = roles.filter((r) => r.status === "working").length;
  document.getElementById("office-status").textContent = `${workingCount}/${roles.length} working`;

  // An elliptical stage around the center console, boss anchored front-and-
  // center at the bottom (i=0 lands at angle = +90°, i.e. straight down) and
  // the rest fanning out clockwise around the rest of the ellipse.
  const radiusXPct = 45;
  const radiusYPct = 41;
  const positions = roles.map((role, i) => {
    const angle = (2 * Math.PI * i) / roles.length + Math.PI / 2;
    const x = 50 + radiusXPct * Math.cos(angle);
    const y = 50 + radiusYPct * Math.sin(angle);
    // Desks further "forward" (toward the bottom, nearer the viewer) render
    // a little larger and above desks further "back" -- a cheap pseudo-depth
    // cue for what's otherwise a flat layout.
    const depth = (y - (50 - radiusYPct)) / (2 * radiusYPct); // 0 = back, 1 = front
    const scale = 0.82 + 0.32 * depth;
    return { role, x, y, scale, z: Math.round(depth * 100) };
  });

  positions.forEach((pos) => {
    container.appendChild(
      buildDesk(pos.role, { x: `${pos.x}%`, y: `${pos.y}%`, scale: pos.scale, z: pos.z }, pos.role.id === "boss")
    );
  });

  renderOfficeLinks(positions);

  updateOfficeScreen(roles);
}

function buildDesk(role, pos, isBoss) {
  const tierClass = `tier-${role.model_tier}`;
  const tierLetter = (role.model_tier || "?")[0].toUpperCase();
  const isWorking = role.status === "working";
  const desk = document.createElement("div");
  desk.className =
    "desk" +
    (isBoss ? " desk-boss" : "") +
    (role.status === "disabled" ? " desk-disabled" : "") +
    (isWorking ? " is-working" : "");
  desk.style.setProperty("--x", pos.x);
  desk.style.setProperty("--y", pos.y);
  desk.style.setProperty("--scale", pos.scale);
  desk.style.zIndex = pos.z; // boss is always frontmost (depth 1), so this alone puts it on top
  desk.dataset.roleId = role.id;
  desk.innerHTML = `
    <div class="desk-figure">
      <span class="desk-antenna"><span class="desk-light"></span></span>
      <span class="desk-head ${tierClass}"></span>
      <span class="desk-body ${tierClass}"></span>
    </div>
    <div class="desk-plate">${isBoss ? '<span class="desk-crown">&#128081;</span>' : ""}${escapeHtml(role.label)}</div>
    <div class="desk-pillbar">
      <span class="pill pill-tier ${tierClass}">${tierLetter}</span>
      <span class="pill pill-status">
        <span class="status-dot ${isWorking ? "working" : ""}"></span>
        ${statusLabel(role.status)}
      </span>
    </div>
  `;
  desk.addEventListener("click", () => showRoleDrawer(role, isBoss));
  return desk;
}

// Console position, in the same 0-100 coordinate space as desk positions --
// must match .office-screen's `left`/`top` in style.css.
const CONSOLE_X = 50;
const CONSOLE_Y = 46;

// A glowing line from every currently-working desk to the console, so it's
// visually obvious who's actively reporting in right now. Both desks and the
// console are opaque boxes, so a line drawn between their exact centers
// mostly disappears underneath them -- only the short gap between their
// edges is ever actually visible. edgePoint() finds where a line toward
// (cx, cy) crosses a given box's own edge, so the line starts/ends right at
// that box's border (a couple units past it, so it visibly overlaps the
// border) instead of wasting length hidden under either box.
function edgePoint(fromX, fromY, towardX, towardY, halfW, halfH) {
  const vx = towardX - fromX;
  const vy = towardY - fromY;
  if (vx === 0 && vy === 0) return { x: fromX, y: fromY };
  const t = Math.min(vx !== 0 ? halfW / Math.abs(vx) : Infinity, vy !== 0 ? halfH / Math.abs(vy) : Infinity);
  return { x: fromX + vx * Math.min(1, t * 1.15), y: fromY + vy * Math.min(1, t * 1.15) };
}

// Half-width/height of a rendered box, converted into the same 0-100 space
// as desk/console positions. Measured live off the actual DOM rather than
// hardcoded, since desk boxes are a fixed pixel size while the ring itself
// is fluid -- their size relative to the ring shifts across breakpoints
// (and the boss desk / depth-based scale() both change a desk's size too),
// so a hardcoded percentage can't stay right at every viewport width.
function halfSizePct(el, wrapRect) {
  const r = el.getBoundingClientRect();
  return { w: (50 * r.width) / wrapRect.width, h: (50 * r.height) / wrapRect.height };
}

function renderOfficeLinks(positions) {
  const svg = document.getElementById("office-links");
  svg.innerHTML = "";
  const wrapRect = document.querySelector(".office-ring-wrap").getBoundingClientRect();
  const consoleHalf = halfSizePct(document.getElementById("office-screen"), wrapRect);

  positions
    .filter((pos) => pos.role.status === "working")
    .forEach((pos) => {
      const deskEl = document.querySelector(`.desk[data-role-id="${pos.role.id}"]`);
      const deskHalf = deskEl ? halfSizePct(deskEl, wrapRect) : { w: 8, h: 8 };
      const start = edgePoint(pos.x, pos.y, CONSOLE_X, CONSOLE_Y, deskHalf.w, deskHalf.h);
      const end = edgePoint(CONSOLE_X, CONSOLE_Y, pos.x, pos.y, consoleHalf.w, consoleHalf.h);
      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("x1", start.x);
      line.setAttribute("y1", start.y);
      line.setAttribute("x2", end.x);
      line.setAttribute("y2", end.y);
      line.setAttribute("class", "office-link" + (pos.role.id === "boss" ? " office-link-boss" : ""));
      svg.appendChild(line);
    });
}

function showRoleDrawer(role, isBoss) {
  document.querySelectorAll(".desk").forEach((d) => d.classList.toggle("selected", d.dataset.roleId === role.id));

  document.getElementById("role-drawer-crown").hidden = !isBoss;
  document.getElementById("role-drawer-label").textContent = role.label;
  document.getElementById("role-drawer-status").innerHTML = `
    <span class="status-dot ${role.status === "working" ? "working" : ""}"></span>
    ${statusLabel(role.status)}
    · ${role.last_active ? "last active " + fmtTime(role.last_active) : "no activity yet"}
    · ${role.model_tier.toUpperCase()} tier
  `;
  document.getElementById("role-drawer-detail").textContent = role.detail || "";
  document.getElementById("role-drawer-owns").textContent = ROLE_OWNS[role.id] || "";

  const drawer = document.getElementById("role-drawer");
  drawer.hidden = false;
}

// --- center screen: a short live feed of every role working this cycle
// (label + one-line detail), capped so it never overflows the console; falls
// back to the boss's own status (or a quiet default) when nothing's active. ---
const SCREEN_FEED_MAX = 3;

function updateOfficeScreen(roles) {
  const screenEl = document.getElementById("office-screen");
  const content = document.getElementById("office-screen-content");

  const working = roles.filter((r) => r.status === "working" && r.detail);

  if (working.length) {
    screenEl.classList.add("active");
    const shown = working.slice(0, SCREEN_FEED_MAX);
    const lines = shown
      .map(
        (r) =>
          `<div class="office-screen-line${r.id === "boss" ? " feed-boss" : ""}"><span class="feed-role">${escapeHtml(r.label)}</span>${escapeHtml(r.detail)}</div>`
      )
      .join("");
    const more = working.length > shown.length ? `<div class="office-screen-line feed-more">+${working.length - shown.length} more working</div>` : "";
    content.innerHTML = `<div class="office-screen-feed">${lines}${more}</div>`;
  } else {
    const boss = roles.find((r) => r.id === "boss");
    screenEl.classList.remove("active");
    content.innerHTML = `<div class="office-screen-text">${escapeHtml((boss && boss.detail) || "All quiet — waiting for the next cycle.")}</div>`;
  }
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
}

function renderTable(tableId, rows, columns, emptyMessage, onRowClick) {
  const tbody = document.querySelector(`#${tableId} tbody`);
  tbody.innerHTML = "";
  if (!rows.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="${columns.length}">${emptyMessage}</td></tr>`;
    return;
  }
  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = columns.map((col) => `<td>${col(row)}</td>`).join("");
    if (row.symbol) tr.dataset.symbol = row.symbol;
    if (onRowClick) {
      tr.classList.add("row-clickable");
      tr.addEventListener("click", () => onRowClick(row));
    }
    tbody.appendChild(tr);
  }
}

function renderActiveSignals(positions, pending, onSelect) {
  const rows = [
    ...positions.map((r) => ({ ...r, statusLabel: "OPEN" })),
    ...pending.map((r) => ({ ...r, statusLabel: "PENDING" })),
  ].sort((a, b) => (b.timestamp || "").localeCompare(a.timestamp || "")); // newest first

  renderTable(
    "table-signals-active",
    rows,
    [
      (r) => fmtTime(r.timestamp),
      (r) => r.symbol,
      (r) => `<span class="status-badge status-${r.statusLabel.toLowerCase()}">${r.statusLabel}</span>`,
      (r) => r.pattern ?? "—",
      (r) =>
        r.statusLabel === "OPEN"
          ? fmtPrice(r.fill_price)
          : r.entry_zone
          ? `${fmtPrice(r.entry_zone[0])} – ${fmtPrice(r.entry_zone[1])}`
          : "—",
      (r) => fmtPrice(r.stop_loss),
      (r) => fmtPrice(r.take_profit),
      (r) => stars(r.confidence_rule_based),
    ],
    "No active signals right now — the bot is still watching.",
    onSelect ? (row) => onSelect(row.symbol) : null
  );
  markActiveSignalRow(currentSymbol);
}

function markActiveSignalRow(symbol) {
  const rows = document.querySelectorAll("#table-signals-active tbody tr");
  rows.forEach((tr) => {
    tr.classList.toggle("row-selected", !!symbol && tr.dataset.symbol === symbol);
  });
}

const HISTORY_KIND_LABEL = {
  signal: "Signal",
  filled: "Bought",
  stop_loss: "Stop-loss",
  take_profit: "Target hit",
  rejected: "Skipped",
  vetoed: "Skipped",
};

function renderSignalHistory(history) {
  renderTable(
    "table-signals-history",
    history,
    [
      (r) => fmtTime(r.timestamp),
      (r) => r.symbol,
      (r) => `<span class="kind-badge kind-${r.kind}">${HISTORY_KIND_LABEL[r.kind] || r.kind}</span>`,
      (r) => r.summary,
    ],
    "No signal history recorded yet."
  );
}

// --- P/L modal: realized P/L, broker-statement style. Opened from the
// Signals panel's P/L button. Reads data.pnl (built by src.pnl.build_pnl_report
// / scripts/export_dashboard_data.py) -- already bucketed hourly/daily/weekly
// server-side, this just renders whichever bucket set is currently selected. ---
let pnlData = null;
let pnlChartInstance = null;
let pnlCurrentPeriod = "hourly";

function fmtMoney(n) {
  if (n == null) return "—";
  const sign = n < 0 ? "-" : "";
  return `${sign}$${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function pnlClass(n) {
  if (n == null) return "";
  return n > 0 ? "positive" : n < 0 ? "negative" : "";
}

function renderPnlSummary(summary) {
  const el = document.getElementById("pnl-summary");
  const stat = (label, value, cls) => `
    <div class="pnl-stat">
      <div class="pnl-stat-label">${label}</div>
      <div class="pnl-stat-value ${cls || ""}">${value}</div>
    </div>`;
  el.innerHTML = [
    stat("Total P/L", fmtMoney(summary.total_realized_pnl), pnlClass(summary.total_realized_pnl)),
    stat("Trades", summary.total_trades),
    stat("Win Rate", summary.win_rate != null ? `${(summary.win_rate * 100).toFixed(0)}%` : "—"),
    stat("Avg Win", fmtMoney(summary.avg_win), "positive"),
    stat("Avg Loss", fmtMoney(summary.avg_loss), "negative"),
    stat("Best Trade", fmtMoney(summary.best_trade), "positive"),
  ].join("");
}

function renderPnlChart(buckets) {
  const container = document.getElementById("pnl-chart-container");
  const emptyEl = document.getElementById("pnl-empty");

  if (pnlChartInstance) {
    pnlChartInstance.remove();
    pnlChartInstance = null;
  }
  container.innerHTML = "";

  const hasAnyTrades = buckets.some((b) => b.trades > 0);
  if (!hasAnyTrades) {
    container.style.display = "none";
    emptyEl.hidden = false;
    emptyEl.textContent = "No closed trades yet — realized P/L will show up here once a position closes (stop-loss or take-profit).";
    return;
  }
  if (typeof LightweightCharts === "undefined") {
    container.style.display = "none";
    emptyEl.hidden = false;
    emptyEl.textContent = "Chart library failed to load — try again once it's available.";
    return;
  }
  container.style.display = "block";
  emptyEl.hidden = true;

  const chart = LightweightCharts.createChart(container, {
    layout: { background: { color: "transparent" }, textColor: "#e6edf7" },
    grid: { vertLines: { color: "#22314e" }, horzLines: { color: "#22314e" } },
    timeScale: {
      borderColor: "#22314e",
      // Bucket index doubles as a categorical "time" -- these aren't real
      // trading timestamps, so the axis is relabeled with each bucket's own
      // hourly/daily/weekly label instead of whatever date lightweight-charts
      // would otherwise derive from the raw index.
      tickMarkFormatter: (time) => (buckets[time] ? buckets[time].label : ""),
    },
    rightPriceScale: { borderColor: "#22314e" },
    width: container.clientWidth,
    height: 220,
  });
  const series = chart.addHistogramSeries({ priceFormat: { type: "price", precision: 2, minMove: 0.01 } });
  series.setData(
    buckets.map((b, i) => ({
      time: i,
      value: b.pnl,
      color: b.pnl >= 0 ? "#22c55e" : "#ef4444",
    }))
  );
  chart.timeScale().fitContent();
  pnlChartInstance = chart;
}

function renderPnlPeriod(period) {
  pnlCurrentPeriod = period;
  if (!pnlData) return;
  renderPnlChart(pnlData[period] || []);
}

function initPnl(pnl) {
  pnlData = pnl || { summary: {}, hourly: [], daily: [], weekly: [] };
  renderPnlSummary(pnlData.summary || {});

  const buttons = document.querySelectorAll("#pnl-period-toggle .toggle-btn");
  buttons.forEach((btn) => {
    btn.addEventListener("click", () => {
      buttons.forEach((b) => b.classList.toggle("active", b === btn));
      renderPnlPeriod(btn.dataset.period);
    });
  });
}

function showPnlModal() {
  document.getElementById("pnl-modal-backdrop").hidden = false;
  // (Re)render on open, not just on load, so the chart sizes correctly --
  // a chart built while its container was display:none would measure 0 width.
  renderPnlPeriod(pnlCurrentPeriod);
}

function hidePnlModal() {
  document.getElementById("pnl-modal-backdrop").hidden = true;
}

document.getElementById("pnl-btn").addEventListener("click", showPnlModal);
document.getElementById("pnl-modal-close").addEventListener("click", hidePnlModal);
document.getElementById("pnl-modal-backdrop").addEventListener("click", (e) => {
  if (e.target.id === "pnl-modal-backdrop") hidePnlModal();
});

window.addEventListener("resize", () => {
  if (!pnlChartInstance) return;
  const container = document.getElementById("pnl-chart-container");
  pnlChartInstance.applyOptions({ width: container.clientWidth });
});

function initSignalsToggle() {
  const buttons = document.querySelectorAll("#signals-toggle .toggle-btn");
  buttons.forEach((btn) => {
    btn.addEventListener("click", () => {
      buttons.forEach((b) => b.classList.toggle("active", b === btn));
      const view = btn.dataset.view;
      document.getElementById("table-signals-active").hidden = view !== "active";
      document.getElementById("table-signals-history").hidden = view !== "history";
    });
  });
}

// --- Live Market: one TradingView chart that follows whichever symbol is
// picked from the dropdown or clicked in the Signals panel's Active tab. If
// that symbol has an active plan, its entry/stop/target/invalidation levels
// are drawn as order lines directly on the TradingView chart (via its widget
// API) instead of a separate candle chart. ---

// Exchange id (ccxt) -> TradingView exchange prefix. Falls back to an
// uppercased exchange id for anything not listed, which is correct for most
// TradingView-covered exchanges.
const TV_EXCHANGE_PREFIX = {
  binance: "BINANCE",
  binanceus: "BINANCEUS",
  coinbase: "COINBASE",
  coinbasepro: "COINBASE",
  kraken: "KRAKEN",
  bybit: "BYBIT",
  okx: "OKX",
  kucoin: "KUCOIN",
};

function toTradingViewSymbol(exchangeId, symbol) {
  const prefix = TV_EXCHANGE_PREFIX[exchangeId] || exchangeId.toUpperCase();
  return `${prefix}:${symbol.replace("/", "")}`;
}

let tvWidget = null;
let exchangeId = "kraken";
let plansBySymbol = {};
let candlesBySymbol = {};
let currentSymbol = null;

// Plan info (pattern/R:R/confidence) comes straight from data.json and is
// shown regardless of which chart engine below ends up rendering.
function renderPlanInfo(plan) {
  const confEl = document.getElementById("chart-confidence");
  if (!plan) {
    confEl.innerHTML = "";
    return;
  }
  confEl.innerHTML = `
    <span><strong>${plan.pattern ?? "—"}</strong></span>
    <span>Risk/Reward: <strong>1 : ${plan.risk_reward ?? "—"}</strong></span>
    <span>Rule-based confidence: <span class="stars">${stars(plan.confidence_rule_based)}</span></span>
    <span>Claude confidence: <span class="stars">${stars(plan.confidence_claude)}</span></span>
  `;
}

function renderTradingViewWidget(symbol) {
  const container = document.getElementById("tv-widget-container");
  if (typeof TradingView === "undefined" || !TradingView.widget) {
    container.innerHTML =
      '<div class="muted" style="padding:24px;">TradingView\'s widget script didn\'t load (offline, network policy, or an ad blocker) — live chart unavailable right now.</div>';
    return;
  }
  container.innerHTML = "";
  tvWidget = new TradingView.widget({
    autosize: true,
    symbol: toTradingViewSymbol(exchangeId, symbol),
    interval: "15",
    timezone: "Etc/UTC",
    theme: "dark",
    style: "1",
    locale: "en",
    toolbar_bg: "#101a2e",
    enable_publishing: false,
    hide_top_toolbar: false,
    hide_legend: false,
    save_image: false,
    container_id: "tv-widget-container",
  });
}

// Single lightweight-charts instance, torn down and rebuilt on every call --
// this fires each time the user selects a different signal, so the old chart
// must be disposed first or the library stacks a new canvas on the old one.
let lwcInstance = null;

function renderAnnotatedChart(symbol, candles, plan) {
  const containerEl = document.getElementById("chart-container");
  const emptyEl = document.getElementById("chart-empty");

  if (lwcInstance) {
    lwcInstance.remove();
    lwcInstance = null;
  }
  containerEl.innerHTML = "";

  if (!candles || !candles.length) {
    containerEl.style.display = "none";
    emptyEl.style.display = "block";
    return;
  }
  if (typeof LightweightCharts === "undefined") {
    containerEl.style.display = "none";
    emptyEl.style.display = "block";
    emptyEl.textContent = "Chart library failed to load from the CDN (network issue or blocked script) — the tables below still reflect the latest data.";
    return;
  }
  containerEl.style.display = "block";
  emptyEl.style.display = "none";

  const lwc = LightweightCharts.createChart(containerEl, {
    layout: { background: { color: "transparent" }, textColor: "#e6edf7" },
    grid: { vertLines: { color: "#22314e" }, horzLines: { color: "#22314e" } },
    timeScale: { timeVisible: true, secondsVisible: false },
    width: containerEl.clientWidth,
    height: 420,
  });
  const series = lwc.addCandlestickSeries({
    upColor: "#22c55e", downColor: "#ef4444", borderVisible: false,
    wickUpColor: "#22c55e", wickDownColor: "#ef4444",
  });
  series.setData(candles);

  if (plan) {
    const lines = [
      { price: plan.take_profit, color: "#22c55e", title: "TAKE PROFIT" },
      { price: plan.entry_zone ? plan.entry_zone[1] : null, color: "#38bdf8", title: "ENTRY HIGH" },
      { price: plan.entry_zone ? plan.entry_zone[0] : null, color: "#38bdf8", title: "ENTRY LOW" },
      { price: plan.stop_loss, color: "#ef4444", title: "STOP LOSS" },
      { price: plan.invalidation, color: "#7b8bab", title: "INVALIDATION" },
    ];
    for (const line of lines) {
      if (line.price == null) continue;
      series.createPriceLine({
        price: line.price, color: line.color, lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true, title: line.title,
      });
    }
  }

  lwc.timeScale().fitContent();
  lwcInstance = lwc;
}

// One persistent resize listener (not re-added per render) that always
// resizes whatever the current annotated-chart instance is, if any.
window.addEventListener("resize", () => {
  if (!lwcInstance) return;
  const containerEl = document.getElementById("chart-container");
  lwcInstance.applyOptions({ width: containerEl.clientWidth });
});

function symbolSignalStatus(symbol) {
  if (plansBySymbol[symbol]?.statusLabel === "OPEN") return "open";
  if (plansBySymbol[symbol]?.statusLabel === "PENDING") return "pending";
  return "none";
}

function selectSymbol(symbol) {
  currentSymbol = symbol;
  const status = symbolSignalStatus(symbol);
  document.getElementById("symbol-dropdown-label").textContent = symbol;
  document.getElementById("symbol-dropdown-dot").className = `signal-dot ${status === "none" ? "" : status}`;
  document.querySelectorAll(".symbol-dropdown-item").forEach((el) => el.classList.toggle("selected", el.dataset.symbol === symbol));
  document.getElementById("symbol-dropdown-list").hidden = true;
  markActiveSignalRow(symbol);

  const plan = plansBySymbol[symbol] || null;
  const candles = candlesBySymbol[symbol]?.candles;
  renderPlanInfo(plan);

  const titleEl = document.getElementById("chart-title");
  const tvContainer = document.getElementById("tv-widget-container");
  const emptyEl = document.getElementById("chart-empty");

  if (plan && candles) {
    // Active signal with candle data -- TradingView's free widget can't draw
    // custom price lines, so this uses our own chart library instead, which
    // can actually put the entry/stop/target/invalidation levels on it.
    titleEl.innerHTML = `${symbol} — ${plan.pattern ?? "signal"} <span class="muted">— annotated chart</span>`;
    tvContainer.style.display = "none";
    renderAnnotatedChart(symbol, candles, plan);
  } else {
    titleEl.innerHTML = 'Live Market <span class="muted">— real-time, via TradingView</span>';
    document.getElementById("chart-container").style.display = "none";
    emptyEl.style.display = "none";
    tvContainer.style.display = "block";
    renderTradingViewWidget(symbol);
  }
}

function initLiveMarket(data) {
  exchangeId = data.exchange_id || "kraken";
  const symbols = data.symbols && data.symbols.length ? data.symbols : ["BTC/USDT"];
  const positions = data.positions || [];
  const pending = data.pending || [];
  plansBySymbol = {};
  for (const p of pending) plansBySymbol[p.symbol] = { ...p, statusLabel: "PENDING" };
  for (const p of positions) plansBySymbol[p.symbol] = { ...p, statusLabel: "OPEN" }; // an OPEN record wins if a symbol is somehow in both
  candlesBySymbol = data.charts || {};

  const btn = document.getElementById("symbol-dropdown-btn");
  const list = document.getElementById("symbol-dropdown-list");

  list.innerHTML = symbols
    .map((s) => {
      const status = symbolSignalStatus(s);
      return `<div class="symbol-dropdown-item" data-symbol="${s}">
        <span class="signal-dot ${status === "none" ? "" : status}"></span>
        <span>${s}</span>
        ${status !== "none" ? `<span class="signal-tag">${status}</span>` : ""}
      </div>`;
    })
    .join("");

  list.querySelectorAll(".symbol-dropdown-item").forEach((item) => {
    item.addEventListener("click", () => selectSymbol(item.dataset.symbol));
  });

  btn.addEventListener("click", () => {
    list.hidden = !list.hidden;
  });
  document.addEventListener("click", (e) => {
    if (!document.getElementById("symbol-dropdown").contains(e.target)) list.hidden = true;
  });

  // Default to the most recent active signal, same as before this chart merge;
  // falls back to the first watchlist symbol if nothing's active right now.
  const activeSymbols = [...positions, ...pending].sort((a, b) => (b.timestamp || "").localeCompare(a.timestamp || ""));
  selectSymbol(activeSymbols.length ? activeSymbols[0].symbol : symbols[0]);
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
  initLiveMarket(data);
  renderActiveSignals(data.positions, data.pending, selectSymbol);
  renderSignalHistory(data.signal_history || []);
  initPnl(data.pnl);
}

initSignalsToggle();

main();
