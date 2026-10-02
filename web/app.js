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
  backtester: "strategy_pack_run/ + scripts/backtest_summary.json — runs once a day, finds candidate strategy variants, backtests them, and records results on the Testing dashboard's candidate leaderboard. Never touches src/ or config/config.yaml, never places an order, never promotes a strategy into live use — a human reviews and merges its PR.",
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
  // Max Drawdown is a teaser for the fuller VaR/CVaR/significance breakdown
  // in the Risk modal (see renderRiskModal) -- same relationship the
  // Realized P/L tile here has to the full P/L modal.
  const maxDrawdownPct = ((data.risk || {}).drawdown || {}).max_drawdown_pct;
  document.getElementById("overview-stats").innerHTML = [
    stat("Equity", portfolio.equity != null ? fmtMoney(portfolio.equity) : "—"),
    stat("Cash", portfolio.cash != null ? fmtMoney(portfolio.cash) : "—"),
    stat("P/L", pnlPct != null ? `${(pnlPct * 100).toFixed(2)}%` : "—", pnlClass(pnlPct)),
    stat("Realized P/L", fmtMoney(pnlSummary.total_realized_pnl), pnlClass(pnlSummary.total_realized_pnl)),
    stat("Max Drawdown", maxDrawdownPct != null ? `${(maxDrawdownPct * 100).toFixed(2)}%` : "—", maxDrawdownPct > 0 ? "negative" : ""),
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

// History covers up to 2 weeks (see scripts/export_dashboard_data.py's
// HISTORY_MAX_AGE) -- grouped into Hourly/Daily/Weekly buckets so that span
// is actually navigable instead of one long undifferentiated scroll. The
// list itself is already newest-first from the server, so a single pass
// inserting a header whenever the bucket changes is enough; no re-sorting.
function _historyBucketKey(date, period) {
  if (period === "hourly") {
    return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}-${date.getHours()}`;
  }
  if (period === "weekly") {
    // Sunday-start week.
    const weekStart = new Date(date.getFullYear(), date.getMonth(), date.getDate() - date.getDay());
    return `${weekStart.getFullYear()}-${weekStart.getMonth()}-${weekStart.getDate()}`;
  }
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`; // daily
}

function _historyBucketLabel(date, period) {
  if (period === "hourly") {
    return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric" });
  }
  if (period === "weekly") {
    const weekStart = new Date(date.getFullYear(), date.getMonth(), date.getDate() - date.getDay());
    return `Week of ${weekStart.toLocaleDateString(undefined, { month: "short", day: "numeric" })}`;
  }
  const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const diffDays = Math.round((startOfDay(new Date()) - startOfDay(date)) / 86400000);
  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  return date.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

function renderHistoryFeed(history, period) {
  const container = document.getElementById("feed-history");
  container.innerHTML = "";
  if (!history.length) {
    container.innerHTML = '<div class="feed-empty">No signal history recorded yet.</div>';
    return;
  }

  let lastBucketKey = null;
  for (const r of history) {
    const date = new Date(r.timestamp);
    const bucketKey = _historyBucketKey(date, period);
    if (bucketKey !== lastBucketKey) {
      const header = document.createElement("div");
      header.className = "feed-section-header";
      header.textContent = _historyBucketLabel(date, period);
      container.appendChild(header);
      lastBucketKey = bucketKey;
    }
    const row = document.createElement("div");
    row.className = "feed-row";
    row.innerHTML = `
      <div class="feed-row-top">
        <span class="feed-row-symbol">${escapeHtml(r.symbol)}</span>
        <span class="kind-badge kind-${r.kind}">${HISTORY_KIND_LABEL[r.kind] || r.kind}</span>
      </div>
      <div class="feed-row-time">${fmtTime(r.timestamp)}</div>
      <div class="feed-row-detail">${escapeHtml(r.summary)}</div>
    `;
    container.appendChild(row);
  }
}

let lastHistoryData = [];
let historyPeriod = "daily";

function initHistoryPeriodToggle() {
  const buttons = document.querySelectorAll("#history-period-toggle .toggle-btn");
  buttons.forEach((btn) => {
    btn.addEventListener("click", () => {
      buttons.forEach((b) => b.classList.toggle("active", b === btn));
      historyPeriod = btn.dataset.period;
      renderHistoryFeed(lastHistoryData, historyPeriod);
    });
  });
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
      document.getElementById("history-period-toggle").hidden = view !== "history";
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

// Split from initPnl() so the live-refresh poll can update the P/L numbers
// on every cycle (updatePnlData) without re-attaching the period-toggle click
// listeners each time (initPnl, called once) -- doing that in one function
// would stack a duplicate listener on every poll, firing the click handler
// multiple times after enough refreshes.
function updatePnlData(pnl) {
  pnlData = pnl || { summary: {}, hourly: [], daily: [], weekly: [] };
  renderPnlSummary(pnlData.summary || {});
  // Keep the chart current too if the modal happens to be open during a refresh.
  const backdrop = document.getElementById("pnl-modal-backdrop");
  if (backdrop && !backdrop.hidden) renderPnlPeriod(pnlCurrentPeriod);
}

function initPnl(pnl) {
  updatePnlData(pnl);

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

// --- Risk modal: statistical validation on the bot's own realized trade
// history (src/risk_analytics.py via data.risk) -- historical VaR/CVaR,
// realized-equity drawdown, and a Monte Carlo win-rate significance test
// against the strategy's own configured breakeven win rate. Opened from the
// feed panel's Risk button. Deliberately a near-duplicate of the P/L modal's
// structure (separate CSS/HTML/JS) rather than a shared component, same as
// role-modal vs. pnl-modal -- keeps this addition from risking either of
// the already-tested modals. --

let lastRiskData = null;

const RISK_VERDICT_LABEL = {
  surprising: "Surprising",
  plausible: "Plausible",
  unremarkable: "Unremarkable",
};

function renderRiskModal(risk) {
  const emptyEl = document.getElementById("risk-empty");
  const contentEl = document.getElementById("risk-content");

  const sampleSize = risk && risk.sample_size;
  const minTrades = (risk && risk.min_trades_for_stats) || 5;
  if (!risk || sampleSize < minTrades) {
    contentEl.hidden = true;
    emptyEl.hidden = false;
    emptyEl.textContent = `Not enough closed trades yet for meaningful statistics (${sampleSize || 0}/${minTrades} minimum) — check back once a few more positions close.`;
    return;
  }
  contentEl.hidden = false;
  emptyEl.hidden = true;

  const sig = risk.significance || {};
  const sigEl = document.getElementById("risk-significance");
  const verdict = sig.verdict || "unremarkable";
  sigEl.className = `risk-significance-card verdict-${verdict}`;
  const observedPct = sig.observed_win_rate != null ? `${(sig.observed_win_rate * 100).toFixed(0)}%` : "—";
  const expectedPct = sig.expected_win_rate != null ? `${(sig.expected_win_rate * 100).toFixed(0)}%` : "—";
  sigEl.innerHTML = `
    <div class="risk-significance-headline verdict-${verdict}">${RISK_VERDICT_LABEL[verdict] || verdict} — ${sig.observed_wins ?? "—"}/${sig.sample_size ?? "—"} wins (${observedPct})</div>
    <div class="risk-significance-detail">
      The strategy's own breakeven win rate (from its configured risk:reward floor) is ${expectedPct}.
      A Monte Carlo test against that bar gives this exact a win count a one-sided p-value of ${sig.p_value ?? "—"} —
      ${
        verdict === "surprising"
          ? "rare enough under breakeven assumptions to be worth investigating, not just a run of bad luck."
          : verdict === "plausible"
          ? "on the edge of what breakeven assumptions would typically produce."
          : "well within the range a breakeven strategy produces by chance on a sample this size."
      }
    </div>
  `;

  const stat = (label, value, cls) => `
    <div class="stat-tile">
      <div class="stat-label">${label}</div>
      <div class="stat-value ${cls || ""}">${value}</div>
    </div>`;
  const pct = (v) => (v != null ? `${(v * 100).toFixed(2)}%` : "—");

  const varCvar = risk.var_cvar || {};
  document.getElementById("risk-var-cvar").innerHTML = ["95", "99"]
    .filter((level) => varCvar[level])
    .map((level) => {
      const v = varCvar[level];
      return [
        stat(`VaR ${level}%`, pct(v.var_pct), pnlClass(v.var_pct)),
        stat(`CVaR ${level}%`, pct(v.cvar_pct), pnlClass(v.cvar_pct)),
      ].join("");
    })
    .join("") || '<div class="muted">Not enough trades for a tail estimate yet.</div>';

  const dd = risk.drawdown || {};
  document.getElementById("risk-drawdown").innerHTML = [
    stat("Max Drawdown", dd.max_drawdown_pct != null ? pct(dd.max_drawdown_pct) : "—", dd.max_drawdown_pct > 0 ? "negative" : ""),
    stat("Current Drawdown", dd.current_drawdown_pct != null ? pct(dd.current_drawdown_pct) : "—", dd.current_drawdown_pct > 0 ? "negative" : ""),
    stat("Peak Equity", dd.peak_equity != null ? fmtMoney(dd.peak_equity) : "—"),
    stat("Trough Equity", dd.trough_equity != null ? fmtMoney(dd.trough_equity) : "—"),
  ].join("");
}

// Mirrors updatePnlData/initPnl's split -- the live-refresh poll calls this
// every cycle, only re-rendering the modal body if it's actually open, so a
// 30s poll never attaches duplicate listeners to the (listener-free) risk
// modal content.
function updateRiskData(risk) {
  lastRiskData = risk || null;
  const backdrop = document.getElementById("risk-modal-backdrop");
  if (backdrop && !backdrop.hidden) renderRiskModal(lastRiskData);
}

function showRiskModal() {
  document.getElementById("risk-modal-backdrop").hidden = false;
  renderRiskModal(lastRiskData);
}

function hideRiskModal() {
  document.getElementById("risk-modal-backdrop").hidden = true;
}

document.getElementById("risk-btn").addEventListener("click", showRiskModal);
document.getElementById("risk-modal-close").addEventListener("click", hideRiskModal);
document.getElementById("risk-modal-backdrop").addEventListener("click", (e) => {
  if (e.target.id === "risk-modal-backdrop") hideRiskModal();
});

// --- Testing (backtest) modal -------------------------------------------
// Shows the offline strategy-backtest report (src.backtest_summary.json via
// data.backtest, see scripts/export_dashboard_data.py) -- a static snapshot
// from the last time the strategy_pack_run backtest pack was actually run,
// not a live/continuously-updated figure like Risk or P/L. Reuses the
// .risk-modal-backdrop/.risk-modal chrome (see .backtest-modal in
// style.css) rather than inventing new overlay plumbing.

let lastBacktestData = null;

function fmtPct(v, digits = 1) {
  return v != null ? `${v > 0 ? "+" : ""}${v.toFixed(digits)}%` : "—";
}
function fmtNum(v, digits = 2) {
  return v != null ? v.toFixed(digits) : "—";
}
function pctClass(v) {
  return v > 0 ? "positive" : v < 0 ? "negative" : "";
}
function gateBadge(verdict) {
  const v = (verdict || "").toUpperCase();
  const cls = v === "PASS" ? "pass" : "fail";
  return `<span class="backtest-gate-badge ${cls}">${v || "—"}</span>`;
}

const GATE_LABELS = {
  min_100_trades: "≥100 trades",
  avg_r_ci_low_gt_0: "Avg R CI low > 0",
  later_30pct_avg_r_gt_0: "Later 30% avg R > 0",
  beats_placebo_90th_pct: "Beats placebo ≥90th pct",
  net_return_and_sharpe_gt_buy_hold: "Return > 0 & Sharpe > buy&hold",
};

function renderBacktestStrategy(key, strat, datasetsById) {
  const rows = (strat.results || [])
    .map((r, i) => {
      const prevDataset = i > 0 ? strat.results[i - 1].dataset : null;
      const dividerCls = prevDataset && prevDataset !== r.dataset ? "backtest-row-divider" : "";
      const ds = datasetsById[r.dataset];
      const ciLabel = r.avg_r_ci ? `[${fmtNum(r.avg_r_ci[0])}, ${fmtNum(r.avg_r_ci[1])}]` : "—";
      return `
        <tr class="${dividerCls}">
          <td>${(ds && ds.label) || r.dataset}</td>
          <td>${r.cost_preset}</td>
          <td>${r.trades ?? "—"}</td>
          <td>${fmtPct(r.win_rate_pct)}</td>
          <td>${fmtNum(r.avg_r)}</td>
          <td>${ciLabel}</td>
          <td>${fmtNum(r.profit_factor)}</td>
          <td class="${pctClass(r.return_pct)}">${fmtPct(r.return_pct)}</td>
          <td class="negative">${fmtPct(r.max_dd_pct)}</td>
          <td>${fmtNum(r.sharpe)}</td>
          <td>${fmtPct(r.exposure_pct)}</td>
        </tr>`;
    })
    .join("");

  const gateRows = Object.entries(strat.gates || {})
    .map(([datasetId, gates]) => {
      const ds = datasetsById[datasetId];
      const cells = Object.keys(GATE_LABELS)
        .map((g) => `<td>${gateBadge(gates[g])}</td>`)
        .join("");
      return `<tr><td>${(ds && ds.label) || datasetId}</td>${cells}<td>${gateBadge(gates.overall)}</td></tr>`;
    })
    .join("");
  const gateHeaderCells = Object.values(GATE_LABELS).map((l) => `<th>${l}</th>`).join("");

  return `
    <div class="backtest-strategy">
      <div class="backtest-strategy-header">${strat.label || key}</div>
      <div class="backtest-strategy-desc">${strat.description || ""}</div>
      <div class="backtest-table-wrap">
        <table class="backtest-table">
          <thead><tr>
            <th>Dataset</th><th>Costs</th><th>Trades</th><th>Win %</th><th>Avg R</th>
            <th>Avg R 95% CI</th><th>PF</th><th>Return</th><th>Max DD</th><th>Sharpe</th><th>Exposure</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <div class="backtest-gates-wrap">
        <table class="backtest-gates-table">
          <thead><tr><th>Dataset</th>${gateHeaderCells}<th>Overall</th></tr></thead>
          <tbody>${gateRows}</tbody>
        </table>
      </div>
    </div>`;
}

function renderBacktestModal(backtest) {
  const emptyEl = document.getElementById("backtest-empty");
  const contentEl = document.getElementById("backtest-content");

  if (!backtest || !backtest.strategies) {
    contentEl.hidden = true;
    emptyEl.hidden = false;
    emptyEl.textContent = "No backtest results available yet.";
    return;
  }
  contentEl.hidden = false;
  emptyEl.hidden = true;

  document.getElementById("backtest-methodology").textContent = backtest.methodology || "";

  const overallFail = Object.values(backtest.strategies).some((s) =>
    Object.values(s.gates || {}).some((g) => g.overall === "FAIL")
  );
  const verdictEl = document.getElementById("backtest-verdict");
  verdictEl.className = `risk-significance-card verdict-${overallFail ? "surprising" : "unremarkable"}`;
  verdictEl.innerHTML = `<div class="risk-significance-headline verdict-${overallFail ? "surprising" : "unremarkable"}">${
    overallFail ? "Not cleared for paper trading" : "Cleared every gate"
  }</div><div class="risk-significance-detail">${backtest.verdict || ""}</div>`;

  const datasetsById = {};
  (backtest.datasets || []).forEach((d) => (datasetsById[d.id] = d));
  document.getElementById("backtest-datasets").innerHTML = (backtest.datasets || [])
    .map(
      (d) => `
        <div class="backtest-dataset-card">
          <div class="backtest-dataset-label">${d.label}</div>
          <div class="muted">${d.source}</div>
          <div class="muted">${d.symbols} symbols · ${d.bars} ${d.timeframe} bars · ${d.window}</div>
        </div>`
    )
    .join("");

  document.getElementById("backtest-strategies").innerHTML = Object.entries(backtest.strategies)
    .map(([key, strat]) => renderBacktestStrategy(key, strat, datasetsById))
    .join("");

  renderBacktestCandidates(backtest.candidates || [], datasetsById);
}

function renderBacktestCandidates(candidates, datasetsById) {
  const emptyEl = document.getElementById("backtest-candidates-empty");
  const listEl = document.getElementById("backtest-candidates");
  if (!candidates.length) {
    emptyEl.hidden = false;
    listEl.innerHTML = "";
    return;
  }
  emptyEl.hidden = true;

  // Newest first.
  const sorted = [...candidates].sort((a, b) => (b.tested_at || "").localeCompare(a.tested_at || ""));

  listEl.innerHTML = sorted
    .map((c) => {
      const gates = c.gates || {};
      const allPass = gates.overall === "PASS";
      const ds = datasetsById[c.dataset];
      const gateCells = Object.keys(GATE_LABELS)
        .map((g) => `<span title="${GATE_LABELS[g]}">${gateBadge(gates[g])}</span>`)
        .join("");
      const ci = c.avg_r_ci ? `[${fmtNum(c.avg_r_ci[0])}, ${fmtNum(c.avg_r_ci[1])}]` : "—";
      const testedDate = c.tested_at ? new Date(c.tested_at).toLocaleDateString() : "—";
      const promoted = Boolean(c.promoted_at);
      const promotedBadge = promoted
        ? `<span class="pill pill-promoted" title="Auto-promoted to live config.yaml on ${new Date(c.promoted_at).toLocaleString()}">LIVE ✓</span>`
        : "";
      const promotedLine = promoted
        ? `<div class="backtest-candidate-promoted">Promoted to live config.yaml ${new Date(c.promoted_at).toLocaleDateString()}${c.promoted_pr_url ? ` · <a href="${escapeHtml(c.promoted_pr_url)}" target="_blank" rel="noopener">PR</a>` : ""}</div>`
        : "";
      return `
        <div class="backtest-candidate-card ${allPass ? "all-pass" : ""} ${promoted ? "is-promoted" : ""}">
          <div class="backtest-candidate-head">
            <div class="backtest-candidate-name">${escapeHtml(c.name || c.id || "untitled")} ${gateBadge(gates.overall)} ${promotedBadge}</div>
            <div class="backtest-candidate-meta">${testedDate} · ${escapeHtml((ds && ds.label) || c.dataset || "—")} · ${c.kind === "new_strategy" ? "new strategy" : "parameter variant"}</div>
          </div>
          <div class="backtest-candidate-desc">${escapeHtml(c.description || "")}</div>
          <div class="backtest-candidate-stats">
            <div><span class="label">Trades</span>${c.trades ?? "—"}</div>
            <div><span class="label">Win%</span>${fmtPct(c.win_rate_pct)}</div>
            <div><span class="label">Avg R</span>${fmtNum(c.avg_r)}</div>
            <div><span class="label">CI</span>${ci}</div>
            <div><span class="label">Return</span>${fmtPct(c.return_pct)}</div>
            <div><span class="label">Max DD</span>${fmtPct(c.max_dd_pct)}</div>
            <div><span class="label">Sharpe</span>${fmtNum(c.sharpe)}</div>
          </div>
          <div class="backtest-candidate-gates">${gateCells}</div>
          ${promotedLine}
          ${c.notes ? `<div class="backtest-candidate-notes">${escapeHtml(c.notes)}</div>` : ""}
        </div>`;
    })
    .join("");
}

function updateBacktestData(backtest) {
  lastBacktestData = backtest || null;
  const backdrop = document.getElementById("backtest-modal-backdrop");
  if (backdrop && !backdrop.hidden) renderBacktestModal(lastBacktestData);
}

function showBacktestModal() {
  document.getElementById("backtest-modal-backdrop").hidden = false;
  renderBacktestModal(lastBacktestData);
}

function hideBacktestModal() {
  document.getElementById("backtest-modal-backdrop").hidden = true;
}

document.getElementById("backtest-btn").addEventListener("click", showBacktestModal);
document.getElementById("backtest-modal-close").addEventListener("click", hideBacktestModal);
document.getElementById("backtest-modal-backdrop").addEventListener("click", (e) => {
  if (e.target.id === "backtest-modal-backdrop") hideBacktestModal();
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

// Live-refresh counterpart to initLiveMarket -- updates plan/candle data and
// the currently-viewed chart's annotations without rebuilding the symbol
// dropdown (which would also close it if a viewer had it open) or reloading
// the TradingView widget for a symbol that's already showing it live (that
// widget streams on its own; reconstructing the iframe every poll would just
// be a jarring flicker for no benefit). Only actually swaps chart type when
// the underlying data calls for it -- a new signal appearing, or an existing
// one closing.
function refreshLiveMarketData(data) {
  const positions = data.positions || [];
  const pending = data.pending || [];
  plansBySymbol = {};
  for (const p of pending) plansBySymbol[p.symbol] = { ...p, statusLabel: "PENDING" };
  for (const p of positions) plansBySymbol[p.symbol] = { ...p, statusLabel: "OPEN" };
  candlesBySymbol = data.charts || {};

  document.querySelectorAll(".symbol-dropdown-item").forEach((item) => {
    const symbol = item.dataset.symbol;
    const status = symbolSignalStatus(symbol);
    const dot = item.querySelector(".signal-dot");
    if (dot) dot.className = `signal-dot ${status === "none" ? "" : status}`;
    const existingTag = item.querySelector(".signal-tag");
    if (status !== "none") {
      if (existingTag) existingTag.textContent = status;
      else item.insertAdjacentHTML("beforeend", `<span class="signal-tag">${status}</span>`);
    } else if (existingTag) {
      existingTag.remove();
    }
  });

  if (!currentSymbol) return;
  const status = symbolSignalStatus(currentSymbol);
  document.getElementById("symbol-dropdown-dot").className = `signal-dot ${status === "none" ? "" : status}`;

  const plan = plansBySymbol[currentSymbol] || null;
  const candles = candlesBySymbol[currentSymbol]?.candles;
  renderPlanInfo(plan);

  const titleEl = document.getElementById("chart-title");
  const tvContainer = document.getElementById("tv-widget-container");
  const showingAnnotated = document.getElementById("chart-container").style.display !== "none";

  if (plan && candles) {
    // Redraw in place -- a lightweight-charts instance, not an iframe, so
    // this is cheap and keeps the entry/stop/target lines current.
    titleEl.innerHTML = `${currentSymbol} — ${plan.pattern ?? "signal"} <span class="muted">— annotated chart</span>`;
    tvContainer.style.display = "none";
    renderAnnotatedChart(currentSymbol, candles, plan);
  } else if (showingAnnotated) {
    // The plan driving the annotated view just closed -- fall back to the
    // live TradingView widget for this symbol.
    titleEl.innerHTML = 'Live Market <span class="muted">— real-time, via TradingView</span>';
    document.getElementById("chart-container").style.display = "none";
    document.getElementById("chart-empty").style.display = "none";
    tvContainer.style.display = "block";
    renderTradingViewWidget(currentSymbol);
  }
  // else: already showing the live TradingView widget for this symbol -- leave it alone.

  markActiveFeedRow(currentSymbol);
}

async function fetchDashboardData() {
  const res = await fetch("data.json", { cache: "no-store" });
  return res.json();
}

async function main() {
  let data;
  try {
    data = await fetchDashboardData();
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
  lastHistoryData = data.signal_history || [];
  renderHistoryFeed(lastHistoryData, historyPeriod);
  initPnl(data.pnl);
  updateRiskData(data.risk);
  updateBacktestData(data.backtest);

  setInterval(refreshDashboard, LIVE_REFRESH_INTERVAL_MS);
}

// Auto-refresh: re-fetches data.json and re-renders the data-driven panels
// (Portfolio Overview, agent cards, signals feed, P/L, the current chart's
// annotations) on an interval, so the dashboard stays current without a
// manual page reload. This is bounded by how often web/data.json itself
// actually changes -- once per bot cycle (poll_interval_seconds, 5 minutes
// by default) -- polling faster than that just re-renders the same numbers,
// which is harmless but pointless; 30s is frequent enough to feel live
// without hammering the endpoint. The TradingView widget for a symbol that's
// already showing it live is deliberately left untouched (see
// refreshLiveMarketData) since it streams on its own.
const LIVE_REFRESH_INTERVAL_MS = 30_000;
let refreshInFlight = false;

async function refreshDashboard() {
  if (refreshInFlight) return; // a slow fetch shouldn't overlap with the next interval's tick
  refreshInFlight = true;
  try {
    const data = await fetchDashboardData();
    renderSummary(data);
    renderAgentCards(data.roles || []);
    renderOverview(data);
    refreshLiveMarketData(data);
    renderActiveFeed(data.positions || [], data.pending || [], selectSymbol);
    lastHistoryData = data.signal_history || [];
    renderHistoryFeed(lastHistoryData, historyPeriod);
    updatePnlData(data.pnl);
    updateRiskData(data.risk);
    updateBacktestData(data.backtest);
  } catch (err) {
    console.error("live refresh failed, will retry next interval:", err);
  } finally {
    refreshInFlight = false;
  }
}

initSignalsToggle();
initHistoryPeriodToggle();
initCollapsibleSidebars();

main();
