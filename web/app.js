// Static dashboard renderer. Reads ./data.json (produced by
// scripts/export_dashboard_data.py) and renders the agent cards, the
// portfolio overview (win-rate gauge, stat tiles, exposure bars), the live
// market chart (a live TradingView widget by default, switching to an
// annotated lightweight-charts view -- with the selected signal's plan
// levels drawn on it -- when that symbol has an active signal, since
// TradingView's free widget can't draw custom price lines), and the signals
// feed. No build step, no framework — this is meant to be dropped straight
// onto Cloudflare Pages.

const fmtPrice = (n) => {
  if (n == null) return "—";
  if (n === 0) return "0";
  const abs = Math.abs(n);
  // A fixed decimal-place cap rounds sub-cent assets (SHIB-level prices,
  // ~0.000006) into indistinguishable values -- fill/stop/target can differ
  // only in the 7th-9th decimal place. Significant-digit formatting keeps
  // that distinction regardless of magnitude; $1+ assets keep the previous
  // fixed-decimal look, which already reads fine at that scale.
  if (abs >= 1) return Number(n).toLocaleString(undefined, { maximumFractionDigits: 6 });
  return Number(n).toLocaleString(undefined, { minimumSignificantDigits: 4, maximumSignificantDigits: 4 });
};
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
const fmtMoney = (n) => {
  if (n == null) return "—";
  const sign = n < 0 ? "-" : "";
  return `${sign}$${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
};
const pnlClass = (n) => (n == null ? "" : n > 0 ? "positive" : n < 0 ? "negative" : "");

// What each role owns — static, mirrors the roster in CLAUDE.md / README.
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

// --- AI Trading Agents: one compact card per pipeline role -----------------

function renderAgentCards(roles) {
  const container = document.getElementById("agent-cards");
  container.innerHTML = "";

  const workingCount = roles.filter((r) => r.status === "working").length;
  document.getElementById("agents-count").textContent = `${workingCount}/${roles.length} working`;

  roles.forEach((role) => {
    const isBoss = role.id === "boss";
    const isWorking = role.status === "working";
    const tierClass = `tier-${role.model_tier}`;
    const tierLetter = (role.model_tier || "?")[0].toUpperCase();

    const card = document.createElement("div");
    card.className =
      "agent-card" +
      (isBoss ? " is-boss" : "") +
      (isWorking ? " is-working" : "") +
      (role.status === "disabled" ? " is-disabled" : "");
    card.dataset.roleId = role.id;
    card.innerHTML = `
      <div class="agent-card-top">
        <span class="agent-card-name">${isBoss ? '<span class="agent-card-crown">&#128081;</span>' : ""}${escapeHtml(role.label)}</span>
        <span class="status-dot ${isWorking ? "working" : ""}"></span>
      </div>
      <div class="agent-card-detail">${escapeHtml(role.detail || "Idle — waiting for the next cycle")}</div>
      <div class="agent-card-meta">
        <span class="pill pill-tier ${tierClass}">${tierLetter}</span>
        <span class="pill pill-status">${statusLabel(role.status)}</span>
      </div>
    `;
    card.addEventListener("click", () => showRoleModal(role, isBoss));
    container.appendChild(card);
  });
}

function showRoleModal(role, isBoss) {
  document.querySelectorAll(".agent-card").forEach((c) => c.classList.toggle("selected", c.dataset.roleId === role.id));

  document.getElementById("role-modal-crown").hidden = !isBoss;
  document.getElementById("role-modal-label").textContent = role.label;
  document.getElementById("role-modal-status").innerHTML = `
    <span class="status-dot ${role.status === "working" ? "working" : ""}"></span>
    ${statusLabel(role.status)}
    · ${role.last_active ? "last active " + fmtTime(role.last_active) : "no activity yet"}
    · ${role.model_tier.toUpperCase()} tier
  `;
  document.getElementById("role-modal-detail").textContent = role.detail || "";
  document.getElementById("role-modal-owns").textContent = ROLE_OWNS[role.id] || "";
  document.getElementById("role-modal-backdrop").hidden = false;
}

function hideRoleModal() {
  document.getElementById("role-modal-backdrop").hidden = true;
  document.querySelectorAll(".agent-card").forEach((c) => c.classList.remove("selected"));
}

document.getElementById("role-modal-close").addEventListener("click", hideRoleModal);
document.getElementById("role-modal-backdrop").addEventListener("click", (e) => {
  if (e.target.id === "role-modal-backdrop") hideRoleModal();
});

function renderSummary(data) {
  const modeBadge = document.getElementById("mode-badge");
  modeBadge.textContent = data.mode;
  modeBadge.className = `badge mode-${data.mode}`;

  document.getElementById("generated-at").textContent = `snapshot: ${fmtTime(data.generated_at)}`;
}

// --- Portfolio Overview: win-rate gauge + stat tiles + exposure bars -------

function renderOverview(data) {
  const portfolio = data.portfolio || {};
  const pnlSummary = (data.pnl && data.pnl.summary) || {};
  const roles = data.roles || [];

  const workingCount = roles.filter((r) => r.status === "working").length;
  const statusEl = document.getElementById("overview-status");
  statusEl.textContent = workingCount ? `${workingCount} active` : "idle";
  statusEl.classList.toggle("idle", workingCount === 0);

  // Win rate is a natural 0-100% gauge subject -- unlike P/L itself (unbounded,
  // would need an arbitrary scale to draw as an arc), it maps directly onto one.
  const winRate = pnlSummary.win_rate;
  const pct = winRate != null ? Math.round(winRate * 100) : 0;
  document.getElementById("win-gauge").style.setProperty("--pct", pct);
  document.getElementById("win-gauge-value").textContent = winRate != null ? `${pct}%` : "—";

  const stat = (label, value, cls) => `
    <div class="stat-tile">
      <div class="stat-label">${label}</div>
      <div class="stat-value ${cls || ""}">${value}</div>
    </div>`;
  const pnlPct = portfolio.pnl_pct;
  document.getElementById("overview-stats").innerHTML = [
    stat("Equity", portfolio.equity != null ? fmtMoney(portfolio.equity) : "—"),
    stat("Cash", portfolio.cash != null ? fmtMoney(portfolio.cash) : "—"),
    stat("P/L", pnlPct != null ? `${(pnlPct * 100).toFixed(2)}%` : "—", pnlClass(pnlPct)),
    stat("Realized P/L", fmtMoney(pnlSummary.total_realized_pnl), pnlClass(pnlSummary.total_realized_pnl)),
  ].join("");

  renderExposureBars(data.positions || [], portfolio.equity);
}

function renderExposureBars(positions, equity) {
  const container = document.getElementById("exposure-bars");
  if (!positions.length || !equity) {
    container.innerHTML = '<div class="exposure-empty">No open positions right now.</div>';
    return;
  }
  const rows = positions
    .map((p) => {
      const value = (p.current_price || p.fill_price || 0) * (p.fill_amount || 0);
      const pct = equity ? (value / equity) * 100 : 0;
      return { symbol: p.symbol, pct, cls: pnlClass(p.unrealized_pnl_pct) };
    })
    .sort((a, b) => b.pct - a.pct);
  container.innerHTML = rows
    .map(
      (r) => `
      <div class="exposure-row">
        <span class="exposure-symbol">${escapeHtml(r.symbol)}</span>
        <span class="exposure-bar-track"><span class="exposure-bar-fill ${r.cls}" style="width:${Math.min(100, r.pct).toFixed(1)}%"></span></span>
        <span class="exposure-pct">${r.pct.toFixed(1)}%</span>
      </div>`
    )
    .join("");
}

// --- Live Signals Feed: compact card rows instead of a wide table (this
// panel lives in a narrow sidebar column) -----------------------------------

function renderFeedList(containerId, rows, emptyMessage, rowRenderer, onRowClick) {
  const container = document.getElementById(containerId);
  container.innerHTML = "";
  if (!rows.length) {
    container.innerHTML = `<div class="feed-empty">${emptyMessage}</div>`;
    return;
  }
  for (const row of rows) {
    const div = document.createElement("div");
    div.className = "feed-row" + (onRowClick ? " row-clickable" : "");
    if (row.symbol) div.dataset.symbol = row.symbol;
    div.innerHTML = rowRenderer(row);
    if (onRowClick) div.addEventListener("click", () => onRowClick(row));
    container.appendChild(div);
  }
}

function renderActiveFeed(positions, pending, onSelect) {
  const rows = [
    ...positions.map((r) => ({ ...r, statusLabel: "OPEN" })),
    ...pending.map((r) => ({ ...r, statusLabel: "PENDING" })),
  ].sort((a, b) => (b.timestamp || "").localeCompare(a.timestamp || "")); // newest first

  renderFeedList(
    "feed-active",
    rows,
    "No active signals right now — the bot is still watching.",
    (r) => `
      <div class="feed-row-top">
        <span class="feed-row-symbol">${escapeHtml(r.symbol)}</span>
        <span class="status-badge status-${r.statusLabel.toLowerCase()}">${r.statusLabel}</span>
      </div>
      <div class="feed-row-time">${fmtTime(r.timestamp)} · ${escapeHtml(r.pattern ?? "—")}</div>
      <div class="feed-row-detail">
        ${
          r.statusLabel === "OPEN"
            ? `Fill ${fmtPrice(r.fill_price)}`
            : r.entry_zone
            ? `Entry ${fmtPrice(r.entry_zone[0])}–${fmtPrice(r.entry_zone[1])}`
            : "—"
        }
        · Stop ${fmtPrice(r.stop_loss)} · Target ${fmtPrice(r.take_profit)}<br>${stars(r.confidence_rule_based)}
      </div>
    `,
    onSelect ? (row) => onSelect(row.symbol) : null
  );
  markActiveFeedRow(currentSymbol);
}

function markActiveFeedRow(symbol) {
  document.querySelectorAll("#feed-active .feed-row").forEach((el) => {
    el.classList.toggle("row-selected", !!symbol && el.dataset.symbol === symbol);
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

function renderHistoryFeed(history) {
  renderFeedList(
    "feed-history",
    history,
    "No signal history recorded yet.",
    (r) => `
      <div class="feed-row-top">
        <span class="feed-row-symbol">${escapeHtml(r.symbol)}</span>
        <span class="kind-badge kind-${r.kind}">${HISTORY_KIND_LABEL[r.kind] || r.kind}</span>
      </div>
      <div class="feed-row-time">${fmtTime(r.timestamp)}</div>
      <div class="feed-row-detail">${escapeHtml(r.summary)}</div>
    `,
    null
  );
}

// --- Sidebar collapse: either the agents (left) or feed (right) rail can be
// tucked away to a thin strip so the center chart/overview column can claim
// the space. State is remembered per-browser (not shared, not synced to
// data.json) via localStorage, purely a per-viewer UI convenience. ---

function initCollapsibleSidebars() {
  const layout = document.getElementById("main-layout");
  const sides = [
    { key: "agents-col-collapsed", col: "agents-col", btn: "agents-collapse-toggle", layoutClass: "agents-collapsed", collapsedIcon: "▶", expandedIcon: "◀" },
    { key: "feed-col-collapsed", col: "feed-col", btn: "feed-collapse-toggle", layoutClass: "feed-collapsed", collapsedIcon: "◀", expandedIcon: "▶" },
  ];

  for (const side of sides) {
    const colEl = document.getElementById(side.col);
    const btnEl = document.getElementById(side.btn);
    let collapsed = false;
    try {
      collapsed = localStorage.getItem(side.key) === "1";
    } catch {
      // Private-window/blocked-storage: fall back to the default (expanded).
    }

    const apply = () => {
      colEl.classList.toggle("collapsed", collapsed);
      layout.classList.toggle(side.layoutClass, collapsed);
      btnEl.textContent = collapsed ? side.collapsedIcon : side.expandedIcon;
      btnEl.title = collapsed ? "Expand" : "Collapse";
      btnEl.setAttribute("aria-expanded", String(!collapsed));
    };
    apply();

    btnEl.addEventListener("click", () => {
      collapsed = !collapsed;
      apply();
      try {
        localStorage.setItem(side.key, collapsed ? "1" : "0");
      } catch {
        // Ignore -- collapse still works for this page view, just won't persist.
      }
      // The center chart resizes with its column; give the CSS transition a
      // moment to finish before recomputing chart widths off clientWidth.
      setTimeout(() => {
        if (lwcInstance) lwcInstance.applyOptions({ width: document.getElementById("chart-container").clientWidth });
        if (pnlChartInstance) pnlChartInstance.applyOptions({ width: document.getElementById("pnl-chart-container").clientWidth });
      }, 220);
    });
  }
}

function initSignalsToggle() {
  const buttons = document.querySelectorAll("#signals-toggle .toggle-btn");
  buttons.forEach((btn) => {
    btn.addEventListener("click", () => {
      buttons.forEach((b) => b.classList.toggle("active", b === btn));
      const view = btn.dataset.view;
      document.getElementById("feed-active").hidden = view !== "active";
      document.getElementById("feed-history").hidden = view !== "history";
    });
  });
}

// --- P/L modal: realized P/L, broker-statement style. Opened from the feed
// panel's P/L button. Reads data.pnl (built by src.pnl.build_pnl_report /
// scripts/export_dashboard_data.py) -- already bucketed hourly/daily/weekly
// server-side, this just renders whichever bucket set is currently selected. --

let pnlData = null;
let pnlChartInstance = null;
let pnlCurrentPeriod = "hourly";

function renderPnlSummary(summary) {
  const el = document.getElementById("pnl-summary");
  const stat = (label, value, cls) => `
    <div class="stat-tile">
      <div class="stat-label">${label}</div>
      <div class="stat-value ${cls || ""}">${value}</div>
    </div>`;
  el.innerHTML = [
    stat("Total P/L", fmtMoney(summary.total_realized_pnl), pnlClass(summary.total_realized_pnl)),
    stat("Trades", summary.total_trades ?? 0),
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

// --- Live Market: one TradingView chart that follows whichever symbol is
// picked from the dropdown or clicked in the feed's Active view. If that
// symbol has an active plan, its entry/stop/target/invalidation levels are
// drawn on an annotated chart instead, since TradingView's free widget can't
// draw custom price lines. ---

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
  markActiveFeedRow(symbol);

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
  renderAgentCards(data.roles || []);
  renderOverview(data);
  initLiveMarket(data);
  renderActiveFeed(data.positions || [], data.pending || [], selectSymbol);
  renderHistoryFeed(data.signal_history || []);
  initPnl(data.pnl);
}

initSignalsToggle();
initCollapsibleSidebars();

main();
