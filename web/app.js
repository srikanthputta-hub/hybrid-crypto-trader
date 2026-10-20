// Static dashboard renderer. Reads web/instances.json (the list of bot instances and each one's kill rule), then the
// selected instance's JSON (data.json for the daily trial; written by scripts/export_dashboard_data.py) and renders it.
// All derivation and formatting lives in display.js (pure, unit-tested); this file only places results in the DOM.
//
// Safety: every dynamic string goes through D.html (escaped by default; markup built here is marked D.raw). There are
// no inline event handlers (listeners are attached here), no eval, and the only requests are this origin's own JSON
// files (a data file name from the manifest must pass D.SAFE_DATA). No library is loaded from a CDN.
(function () {
  "use strict";

  const D = window.DashboardDisplay;
  const { html, raw, escapeHtml: esc } = D;
  const $ = (id) => document.getElementById(id);
  const set = (id, markup) => {
    $(id).innerHTML = markup.__raw;
  };

  const REFRESH_MS = 60 * 1000; // data.json changes about every 5 minutes; a minute keeps "updated N min ago" honest
  const TICK_MS = 15 * 1000; // re-evaluate ages (no refetch)
  const FETCH_TIMEOUT_MS = 15 * 1000;

  const CLOSED_PREVIEW = 8; // rows shown before "Show all" (a phone shows one card per row)
  const state = { showAllClosed: false, manifest: null, id: null, entry: null, data: null, skewMs: 0, filter: null, error: null, symbol: null };
  const nowMs = () => Date.now() + state.skewMs;

  // ------------------------------------------------------------------------------------------------------------
  // loading
  // ------------------------------------------------------------------------------------------------------------
  async function getJson(path) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), FETCH_TIMEOUT_MS);
    try {
      const res = await fetch(path, { cache: "no-store", signal: ctl.signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return { json: await res.json(), date: res.headers.get("date") };
    } finally {
      clearTimeout(timer);
    }
  }

  async function loadManifest() {
    try {
      return D.parseManifest((await getJson("instances.json")).json);
    } catch (err) {
      console.warn("instances.json not available, using a single data.json instance:", err);
      return D.parseManifest(null);
    }
  }

  async function loadData() {
    const entry = state.entry;
    try {
      const { json, date } = await getJson(entry.data);
      if (state.entry !== entry) return; // the viewer switched instance while this was in flight
      if (!json || typeof json !== "object" || Array.isArray(json)) throw new Error("not a dashboard JSON object");
      state.data = json;
      state.skewMs = date ? D.clockSkewMs(date, Date.now()) : state.skewMs;
      state.error = null;
    } catch (err) {
      if (state.entry !== entry) return;
      state.error = err && err.name === "AbortError" ? "timed out" : String((err && err.message) || err);
      console.error("dashboard load failed:", err);
    }
    renderAll();
  }

  // ------------------------------------------------------------------------------------------------------------
  // small builders
  // ------------------------------------------------------------------------------------------------------------
  const levelClass = { ok: "ok", warn: "warn", stale: "bad", bad: "bad", none: "none", unknown: "none", info: "none" };
  const safeWord = (s) => String(s == null ? "" : s).replace(/[^a-z0-9_-]/gi, "");
  const chartWidth = (el) => Math.max(260, Math.floor(el.clientWidth || el.parentElement.clientWidth || 320));

  function table(cols, rows, caption) {
    return html`${caption ? html`<div class="cap">${caption}</div>` : ""}<table class="rt"><thead><tr>${cols.map(
      (c) => html`<th scope="col" class="${c.num ? "num" : ""}">${c.label}</th>`
    )}</tr></thead><tbody>${rows.map(
      (r) => html`<tr>${cols.map(
        (c) => html`<td data-label="${c.label}" class="${c.primary ? "primary" : ""} ${c.num ? "num" : ""} ${c.cls ? c.cls(r) : ""}">${c.cell(r)}</td>`
      )}</tr>`
    )}</tbody></table>`;
  }

  const stars = (n) => (D.isNum(n) ? `${"\u2605".repeat(Math.round(n))}${"\u2606".repeat(Math.max(0, 5 - Math.round(n)))}` : "\u2014");

  // ------------------------------------------------------------------------------------------------------------
  // the live part: anything that depends on "now" (ages, alert, chips, health)
  // ------------------------------------------------------------------------------------------------------------
  function renderStrip() {
    const data = state.data;
    $("inst-name").textContent = (state.entry && state.entry.name) || "Dashboard";
    document.title = `${(state.entry && state.entry.name) || "Dashboard"} \u00b7 Trading desk`;
    const alert = D.alertModel(data, nowMs(), state.error);
    $("main").classList.toggle("stale", !!alert && alert.level === "bad"); // dim the figures when they cannot be trusted
    const alertEl = $("alert");
    alertEl.hidden = !alert;
    alertEl.className = `alert ${alert ? alert.level : ""}`;
    alertEl.textContent = alert ? alert.text : "";
    if (!data) {
      $("chip-fresh").className = "chip none";
      $("chip-fresh").textContent = state.error ? "no data" : "loading\u2026";
      return;
    }
    const m = D.statusModel(data, state.entry, nowMs());
    const label = $("inst-label");
    const bannerText = D.trialBannerText(data) || m.label;
    label.hidden = !bannerText;
    label.textContent = bannerText || "";
    const mode = $("inst-mode");
    mode.hidden = !m.mode;
    mode.textContent = m.mode ? `mode=${m.mode}` : "";
    mode.className = `badge mode-${safeWord(m.mode)}`;

    const fresh = $("chip-fresh");
    fresh.className = `chip ${levelClass[m.fresh.level]}`;
    fresh.textContent = m.fresh.level === "stale" ? `STALE \u00b7 ${m.fresh.text}` : m.fresh.text;
    fresh.title = data.generated_at ? `dashboard file generated ${D.fmtUtc(data.generated_at)}` : "";

    const bar = $("chip-bar");
    bar.hidden = !m.lastBar;
    if (m.lastBar) {
      const suffix = { waiting: " \u00b7 next decision pending", late: " \u00b7 decision late", missed: " \u00b7 decision MISSED" }[m.bar.state] || "";
      bar.textContent = `last bar ${D.fmtUtc(m.lastBar)}${suffix}`;
      bar.className = `chip ${{ current: "ok", waiting: "none", late: "warn", missed: "bad", unknown: "none" }[m.bar.state]}`;
    }
    const kill = $("chip-kill");
    kill.hidden = m.kill == null;
    if (m.kill != null) {
      kill.textContent = m.kill ? "kill switch ON" : "kill switch off";
      kill.className = `chip ${m.kill ? "bad" : "ok"}`;
    }
    const ks = killStatus();
    const rule = $("chip-rule");
    rule.hidden = !ks.hasRule;
    if (ks.hasRule) {
      rule.textContent = ks.state === "breach" ? "kill rule BREACHED" : ks.avgR.state === "insufficient" ? `kill rule: ${ks.avgR.have}/${ks.avgR.need} trades` : ks.state === "nodata" ? "kill rule: no data" : "kill rule: within limits";
      rule.className = `chip ${ks.state === "breach" ? "bad" : ks.avgR.state === "insufficient" || ks.state === "nodata" ? "none" : "ok"}`;
    }
    const asOf = D.asOfText(data);
    $("asof").hidden = !asOf;
    $("asof").textContent = asOf || "";
    renderHealth();
  }

  function renderHealth() {
    const data = state.data;
    if (!data) return;
    const rows = D.systemHealth(data, nowMs());
    set("health", html`${rows.map(
      (r) => html`<div data-key="${r.key}"><dt>${r.label}</dt><dd class="${r.level}">${r.value}</dd>${r.note ? html`<dd class="why">${r.note}</dd>` : ""}</div>`
    )}`);
    const roles = Array.isArray(data.roles) ? data.roles : [];
    set("roles", html`${roles.map(
      (r) => html`<span class="role ${safeWord(r.status)}" title="${r.detail || ""}">${r.label} \u00b7 ${r.status || "idle"}</span>`
    )}`);
  }

  // ------------------------------------------------------------------------------------------------------------
  // data panels
  // ------------------------------------------------------------------------------------------------------------
  const opsOf = () => (state.data && state.data.ops) || null;

  function drawdownOf() {
    const ops = opsOf();
    return ops && ops.equity_curve ? D.drawdownSeries(ops.equity_curve.points, ops.starting_cash) : null;
  }

  function killStatus() {
    const ops = opsOf();
    let series = ops && Array.isArray(ops.r_recosted_series) ? ops.r_recosted_series : null;
    // An older export has no per-trade R, but its P/L summary still proves "no trades yet": that case is known, not missing.
    const summary = state.data && state.data.pnl && state.data.pnl.summary;
    if (!series && summary && summary.total_trades === 0) series = [];
    return D.killRuleStatus(state.entry && state.entry.kill_rule, series, drawdownOf());
  }

  function renderKpis() {
    const kpis = D.buildKpis(state.data);
    set("kpis", html`${kpis.map(
      (k) => html`<div class="kpi" data-kpi="${k.id}"><div class="kpi-label">${k.label}</div><div class="kpi-value ${k.cls}">${k.value}</div><div class="kpi-cap">${k.caption}</div></div>`
    )}`);
  }

  function renderKillRule() {
    const ks = killStatus();
    const ops = opsOf();
    const rule = state.entry && state.entry.kill_rule;
    const pill = $("kill-pill");
    document.querySelector("#kill-card .kill-grid").hidden = !ks.hasRule; // nothing to plot without a rule
    const recost = ops && ops.recost && ops.recost.label ? ops.recost.label : "re-costed";

    if (!ks.hasRule) {
      pill.textContent = "no rule";
      pill.className = "pill";
      $("kill-lede").textContent = "No kill rule is defined for this instance (add \"kill_rule\" to its entry in web/instances.json).";
    } else if (ks.state === "breach") {
      pill.textContent = "BREACHED";
      pill.className = "pill bad";
      const why = [];
      if (ks.avgR.state === "breach") {
        const b = ks.checkpoints.filter((c) => c.status === "breach").map((c) => `${c.n} trades (${D.fmtR(c.mean)} < ${D.fmtR(c.max)})`);
        why.push(`mean R below the threshold at ${b.join(", ")}`);
      }
      if (ks.drawdown.state === "breach") why.push(`equity drawdown ${D.fmtPct(ks.drawdown.max, 1)} exceeded ${D.fmtPct(ks.drawdown.limit, 0)}`);
      $("kill-lede").textContent = `Stop condition met: ${why.join("; ")}. Per the runbook the trial stops; what happens next is the owner's decision.`;
    } else if (ks.state === "nodata") {
      pill.textContent = "no data";
      pill.className = "pill";
      $("kill-lede").textContent = "No data: this export carries no per-trade R and no equity samples, so the rule cannot be evaluated.";
    } else {
      const insufficient = ks.avgR.state === "insufficient";
      pill.textContent = insufficient ? `${ks.avgR.have}/${ks.avgR.need} trades` : "within limits";
      pill.className = `pill ${insufficient ? "" : "ok"}`;
      $("kill-lede").textContent = insufficient
        ? `Mean R: not enough trades yet (${ks.avgR.have}/${ks.avgR.need}). The thresholds start to apply at ${ks.avgR.need} closed trades; until then only the drawdown line can fire.`
        : ks.avgR.state === "partial"
        ? "Mean R: some trades have no R in this export, so the latest checkpoint cannot be fully judged."
        : "No stop condition is met on the data available.";
    }

    // mean R chart + checkpoints
    const chart = $("kill-r-chart");
    if (ks.hasRule && ks.trades != null) {
      chart.innerHTML = D.killRuleChartSvg(ks, chartWidth(chart), 210);
    } else {
      chart.innerHTML = D.lineChartSvg({ width: chartWidth(chart), height: 120, points: [], label: "Mean R" });
    }
    set("kill-cps", html`${ks.checkpoints.map((c) => {
      const text = !c.reached
        ? `${c.n}: pending (${ks.trades || 0}/${c.n}) \u00b7 stop below ${D.fmtR(c.max)}`
        : c.status === "nodata"
        ? `${c.n}: no R known`
        : `${c.n}: ${D.fmtR(c.mean)} vs ${D.fmtR(c.max)} \u00b7 ${c.status === "ok" ? "ok" : c.status === "breach" ? "BREACH" : `partial (${c.known}/${c.n} known)`}`;
      return html`<li class="${c.status}">${text}</li>`;
    })}`);
    let rNote = "";
    if (ks.trades == null) rNote = "No per-trade R in this export.";
    else if (!ks.trades) rNote = "No closed trades yet, so there is no mean to plot. The line shows the stop thresholds.";
    else if (ks.current && ks.current.mean != null) {
      const next = ks.next ? ` Next checkpoint at ${ks.next.n} trades (${ks.next.n - ks.trades} to go).` : "";
      rNote = `Running mean ${D.fmtR(ks.current.mean)} over ${ks.current.known} trade${ks.current.known === 1 ? "" : "s"} with a known R (${recost}).${next}`;
      if (ks.latest && ks.latest.mean != null) rNote += ` Latest checkpoint (${ks.latest.n}): ${D.fmtR(ks.latest.mean)} against ${D.fmtR(ks.latest.max)}, margin ${D.fmtR(ks.latest.mean - ks.latest.max)}.`;
    } else rNote = "Closed trades have no R in this export (their plan records are missing).";
    $("kill-r-note").textContent = rNote;

    // drawdown meter
    const meter = $("kill-dd-chart");
    const dd = ks.drawdown;
    if (dd.state === "nodata") {
      meter.innerHTML = "";
      const realised = state.data.risk && state.data.risk.drawdown ? state.data.risk.drawdown : {};
      $("kill-dd-note").textContent = D.isNum(realised.max_drawdown_pct)
        ? `No equity samples exported; realised-only drawdown is ${D.fmtPct(realised.max_drawdown_pct, 2)}${dd.limit != null ? ` (limit ${D.fmtPct(dd.limit, 0)})` : ""}.`
        : "No data: no equity samples and no realised drawdown in this export.";
    } else {
      meter.innerHTML = D.drawdownMeterSvg(chartWidth(meter), dd.current, dd.max, dd.limit);
      $("kill-dd-note").textContent = `Worst ${D.fmtPct(dd.max, 2)}, now ${D.fmtPct(dd.current, 2)}; the rule stops the trial above ${D.fmtPct(dd.limit, 0)}. Mark-to-market on the equity samples, not realised trades only.`;
    }
    $("kill-source").textContent = rule ? [rule.label, rule.source].filter(Boolean).join(" \u00b7 ") : "";
  }

  function renderEquity() {
    const ops = opsOf();
    const curve = ops && ops.equity_curve;
    const dd = drawdownOf();
    const eq = $("equity-chart");
    const ddEl = $("dd-chart");
    const limit = state.entry && state.entry.kill_rule ? state.entry.kill_rule.max_drawdown : null;
    eq.innerHTML = D.equityChartSvg(dd, ops ? ops.starting_cash : null, chartWidth(eq), 190);
    ddEl.innerHTML = D.drawdownChartSvg(dd, limit, chartWidth(ddEl), 150);
    $("equity-since").textContent = dd ? `samples since ${D.fmtUtc(new Date(dd.since).toISOString())}` : "";
    $("equity-note").textContent = !curve
      ? "No data: this export has no equity samples (older exporter)."
      : dd && dd.points.length < 3
      ? `Only ${dd.points.length} equity point${dd.points.length === 1 ? "" : "s"} so far. The exporter adds a sample whenever equity or cash changes (and at least every 6 hours), so the curve fills in as the trial runs.`
      : "Mark-to-market equity as sampled by the dashboard export (cash plus open positions at the last close), starting from the starting cash at the first audit line.";
  }

  function renderDecisions() {
    const ops = opsOf();
    const rows = ops && Array.isArray(ops.decisions) ? ops.decisions : null;
    const filters = $("dec-filters");
    const grid = $("dec-grid");
    const h = ops && ops.health;
    $("dec-bar").textContent = h && h.last_bar_close
      ? `bar ${D.fmtUtc(h.last_bar_close)}${h.latest_bar ? ` \u00b7 ${h.latest_bar.symbols_decided}/${h.expected_symbols} decided` : ""}`
      : "";
    if (!rows) {
      filters.innerHTML = "";
      set("dec-grid", html`<li class="empty">No data: this export has no per-symbol decisions (older exporter).</li>`);
      return;
    }
    const counts = D.decisionCounts(rows);
    const active = state.filter;
    set("dec-filters", html`<button type="button" data-state="" aria-pressed="${active == null}">all ${rows.length}</button>${D.DECISION_STATES.filter((s) => counts[s]).map(
      (s) => html`<button type="button" data-state="${s}" aria-pressed="${active === s}">${D.DECISION_LABEL[s]} ${counts[s]}</button>`
    )}`);
    const shown = rows.filter((r) => !active || r.state === active);
    const latest = h && h.last_bar_close;
    set("dec-grid", shown.length ? html`${shown.map((r) => {
      const state_ = D.DECISION_STATES.includes(r.state) ? r.state : "nodata";
      const meta = [];
      if (r.pattern) meta.push(r.pattern);
      if (latest && r.bar_close && r.bar_close !== latest) meta.push(`older bar ${D.fmtUtc(r.bar_close)}`);
      if (r.data_unavailable) meta.push("data unavailable this cycle");
      return html`<li class="dec" data-symbol="${r.symbol}"><span class="dec-sym">${r.symbol}</span><span class="dec-state"><span class="st st-${state_}">${D.DECISION_LABEL[state_]}</span></span><span class="dec-reason">${r.reason}</span>${meta.length ? html`<span class="dec-meta">${meta.join(" \u00b7 ")}</span>` : ""}</li>`;
    })}` : html`<li class="empty">No symbols in this state.</li>`);
  }

  function renderPositions() {
    const positions = Array.isArray(state.data.positions) ? state.data.positions : [];
    $("pos-count").textContent = positions.length ? `${positions.length} open` : "";
    if (!positions.length) {
      set("pos-table", html`<div class="empty">No open positions.</div>`);
      return;
    }
    const cols = [
      { label: "Symbol", primary: true, cell: (p) => p.symbol },
      { label: "Pattern", cell: (p) => (p.pattern ? p.pattern.replace(/_/g, " ") : "\u2014") },
      { label: "Entry", num: true, cell: (p) => D.fmtPrice(p.fill_price) },
      { label: "Stop", num: true, cell: (p) => D.fmtPrice(p.stop_loss) },
      { label: "Target", num: true, cell: (p) => D.fmtPrice(p.take_profit) },
      { label: "Last", num: true, cell: (p) => D.fmtPrice(p.current_price) },
      {
        label: "Unrealised", num: true,
        cls: (p) => D.signClass(p.unrealized_pnl_pct),
        cell: (p) => {
          const usd = D.isNum(p.current_price) && D.isNum(p.fill_price) && D.isNum(p.fill_amount) ? (p.current_price - p.fill_price) * p.fill_amount : null;
          return usd == null ? "\u2014" : `${D.fmtMoney(usd, true)} (${D.fmtPct(p.unrealized_pnl_pct, 2, true)})`;
        },
      },
      { label: "Opened", cell: (p) => D.fmtUtcShort(p.timestamp) },
    ];
    set("pos-table", table(cols, positions, "Entry = paper fill price. Last = the last closed bar's close. Times UTC."));
  }

  function renderPending() {
    const pending = Array.isArray(state.data.pending) ? state.data.pending : [];
    $("pend-count").textContent = pending.length ? `${pending.length} waiting` : "";
    if (!pending.length) {
      set("pend-table", html`<div class="empty">No pending plans.</div>`);
      return;
    }
    const cols = [
      { label: "Symbol", primary: true, cell: (p) => p.symbol },
      { label: "Pattern", cell: (p) => (p.pattern ? p.pattern.replace(/_/g, " ") : "\u2014") },
      { label: "Entry zone", num: true, cell: (p) => (Array.isArray(p.entry_zone) ? `${D.fmtPrice(p.entry_zone[0])} \u2013 ${D.fmtPrice(p.entry_zone[1])}` : "\u2014") },
      { label: "Stop", num: true, cell: (p) => D.fmtPrice(p.stop_loss) },
      { label: "Target", num: true, cell: (p) => D.fmtPrice(p.take_profit) },
      { label: "R:R", num: true, cell: (p) => (D.isNum(p.risk_reward) ? `1 : ${p.risk_reward}` : "\u2014") },
      { label: "Confidence", cell: (p) => stars(p.confidence_rule_based) },
      { label: "Planned", cell: (p) => D.fmtUtcShort(p.timestamp) },
    ];
    set("pend-table", table(cols, pending, "A plan waits for price to reach its entry zone. Times UTC."));
  }

  const REASON = { take_profit: "target hit", stop_loss: "stop-loss", invalidated: "invalidated", manual: "manual" };
  function renderClosed() {
    const ops = opsOf();
    const rows = ops && Array.isArray(ops.closed_trades) ? ops.closed_trades : null;
    if (!rows) {
      $("closed-count").textContent = "";
      set("closed-table", html`<div class="empty">No data: this export has no per-trade list (older exporter).</div>`);
      return;
    }
    const total = ops.trade_stats ? ops.trade_stats.n : rows.length;
    const shownRows = state.showAllClosed ? rows : rows.slice(0, CLOSED_PREVIEW);
    $("closed-count").textContent = total ? `latest ${shownRows.length} of ${total}` : "";
    if (!rows.length) {
      set("closed-table", html`<div class="empty">No closed trades yet.</div>`);
      return;
    }
    const cols = [
      { label: "Symbol", primary: true, cell: (t) => t.symbol },
      { label: "Closed", cell: (t) => D.fmtUtcShort(t.closed_at) },
      { label: "Result", cell: (t) => REASON[t.reason] || t.reason || "\u2014" },
      { label: "Entry \u2192 exit", num: true, cell: (t) => `${D.fmtPrice(t.entry)} \u2192 ${D.fmtPrice(t.exit)}` },
      { label: "P/L", num: true, cls: (t) => D.signClass(t.pnl), cell: (t) => `${D.fmtMoney(t.pnl, true)} (${D.fmtPct(t.pnl_pct, 2, true)})` },
      { label: "R re-costed", num: true, cls: (t) => D.signClass(t.r_recosted), cell: (t) => D.fmtR(t.r_recosted) },
      { label: "R booked", num: true, cell: (t) => D.fmtR(t.r_booked) },
    ];
    const label = ops.recost && ops.recost.label ? ops.recost.label : "re-costed";
    const more = rows.length > CLOSED_PREVIEW ? html`<button type="button" class="more" id="closed-more" aria-expanded="${state.showAllClosed}">${state.showAllClosed ? "Show fewer" : `Show all ${rows.length} exported trades`}</button>` : "";
    set("closed-table", html`${table(cols, shownRows, `R re-costed at ${label}; R booked is the paper broker's own P/L over the planned risk. Times UTC.`)}${more}`);
  }

  // ---- folds ----------------------------------------------------------------------------------------------
  const HISTORY_LABEL = { signal: "Signal", filled: "Bought", stop_loss: "Stop-loss", take_profit: "Target hit", rejected: "Skipped", vetoed: "Skipped" };
  function renderHistory() {
    const list = Array.isArray(state.data.signal_history) ? state.data.signal_history : [];
    if (!list.length) {
      set("hist-body", html`<div class="empty">No notable events in the last 14 days.</div>`);
      return;
    }
    const shown = list.slice(0, 200);
    let day = null;
    const out = [];
    for (const r of shown) {
      const d = (r.timestamp || "").slice(0, 10);
      if (d !== day) {
        day = d;
        out.push(html`<div class="hist-day">${D.fmtUtcShort(`${d}T00:00:00Z`).replace(/ \d\d:\d\d$/, "")} UTC</div>`);
      }
      out.push(html`<div class="hist-row"><span class="st">${HISTORY_LABEL[r.kind] || r.kind}</span><span><b>${r.symbol}</b> ${r.summary}</span><span class="when">${D.fmtUtcShort(r.timestamp)} UTC</span></div>`);
    }
    if (list.length > shown.length) out.push(html`<div class="note">Showing the latest ${shown.length} of ${list.length}.</div>`);
    set("hist-body", html`${out}`);
  }

  function renderRisk() {
    const risk = state.data.risk;
    const min = (risk && risk.min_trades_for_stats) || 5;
    const n = (risk && risk.sample_size) || 0;
    if (!risk || n < min) {
      set("risk-body", html`<div class="empty">Not enough closed trades for meaningful statistics (${n}/${min} minimum).</div>`);
      return;
    }
    const kv = [];
    const vc = risk.var_cvar || {};
    for (const level of ["95", "99"]) {
      if (vc[level]) {
        kv.push(["VaR " + level + "%", D.fmtPct(vc[level].var_pct, 2)], ["CVaR " + level + "%", D.fmtPct(vc[level].cvar_pct, 2)]);
      }
    }
    const dd = risk.drawdown || {};
    kv.push(["Max drawdown (realised)", D.fmtPct(dd.max_drawdown_pct, 2)], ["Current drawdown (realised)", D.fmtPct(dd.current_drawdown_pct, 2)]);
    const sig = risk.significance || {};
    set("risk-body", html`<div class="kv">${kv.map(([k, v]) => html`<div><div class="k">${k}</div><div class="v">${v}</div></div>`)}</div>${sig.verdict ? html`<p class="note">Win-rate test against the strategy's own breakeven rate: ${sig.verdict} (${sig.observed_wins}/${sig.sample_size} wins, p = ${sig.p_value}).</p>` : ""}`);
  }

  function renderBacktests() {
    const bt = state.data.backtest;
    if (!bt || !bt.strategies) {
      set("bt-body", html`<div class="empty">No backtest results in this export.</div>`);
      return;
    }
    const datasets = {};
    (bt.datasets || []).forEach((d) => (datasets[d.id] = d));
    const rows = [];
    for (const [key, strat] of Object.entries(bt.strategies)) {
      for (const r of strat.results || []) rows.push({ strategy: strat.label || key, ...r, gate: ((strat.gates || {})[r.dataset] || {}).overall });
    }
    const cols = [
      { label: "Strategy", primary: true, cell: (r) => r.strategy },
      { label: "Dataset", cell: (r) => (datasets[r.dataset] ? datasets[r.dataset].label : r.dataset) },
      { label: "Costs", cell: (r) => r.cost_preset },
      { label: "Trades", num: true, cell: (r) => (r.trades == null ? "\u2014" : r.trades) },
      { label: "Avg R", num: true, cell: (r) => D.fmtR(r.avg_r) },
      { label: "Return", num: true, cell: (r) => (D.isNum(r.return_pct) ? `${r.return_pct.toFixed(1)}%` : "\u2014") },
      { label: "Max DD", num: true, cell: (r) => (D.isNum(r.max_dd_pct) ? `${r.max_dd_pct.toFixed(1)}%` : "\u2014") },
      { label: "Gates", cell: (r) => r.gate || "\u2014" },
    ];
    const cands = Array.isArray(bt.candidates) ? bt.candidates : [];
    const passed = cands.filter((c) => (c.gates || {}).overall === "PASS").length;
    set("bt-body", html`<p class="note">${bt.verdict || ""}</p>${table(cols, rows, `${cands.length} candidate variants tested by the (paused) daily routine, ${passed} passed every gate. Offline, fee-and-slippage-costed backtests; none of this is live trading.`)}`);
  }

  // ---- symbol chart (vendored lightweight-charts, themed from the CSS variables) -----------------------------
  let lwc = null;
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  function renderSymbolChart() {
    const fold = $("sym-fold");
    if (!fold.open || !state.data) return;
    const select = $("sym-select");
    const symbols = Array.isArray(state.data.symbols) ? state.data.symbols : [];
    if (select.options.length !== symbols.length || symbols.some((s, i) => select.options[i].value !== s)) {
      set("sym-select", html`${symbols.map((s) => html`<option value="${s}">${s}</option>`)}`);
    }
    const plans = {};
    for (const p of state.data.pending || []) plans[p.symbol] = p;
    for (const p of state.data.positions || []) plans[p.symbol] = p;
    if (!state.symbol || !symbols.includes(state.symbol)) state.symbol = Object.keys(plans)[0] || symbols[0] || null;
    select.value = state.symbol || "";
    const box = $("sym-chart");
    if (lwc) {
      lwc.remove();
      lwc = null;
    }
    box.innerHTML = "";
    const candles = state.symbol && state.data.charts && state.data.charts[state.symbol] ? state.data.charts[state.symbol].candles : null;
    const plan = state.symbol ? plans[state.symbol] : null;
    $("sym-note").textContent = !state.symbol ? "No symbol." : !candles || !candles.length ? "No candles for this symbol in this export." : plan ? `${state.symbol}: ${plan.pattern || "plan"} (dashed lines are the plan's levels).` : `${state.symbol}: no active plan.`;
    if (!candles || !candles.length || typeof LightweightCharts === "undefined") return;
    lwc = LightweightCharts.createChart(box, {
      width: box.clientWidth || 320,
      height: 320,
      layout: { background: { color: "transparent" }, textColor: cssVar("--text") },
      grid: { vertLines: { color: cssVar("--border") }, horzLines: { color: cssVar("--border") } },
      rightPriceScale: { borderColor: cssVar("--border") },
      timeScale: { borderColor: cssVar("--border") },
    });
    const series = lwc.addCandlestickSeries({
      upColor: cssVar("--pos"), downColor: cssVar("--neg"), borderVisible: false, wickUpColor: cssVar("--pos"), wickDownColor: cssVar("--neg"),
    });
    series.setData(candles);
    if (plan) {
      const levels = [
        [plan.take_profit, cssVar("--pos"), "target"],
        [Array.isArray(plan.entry_zone) ? plan.entry_zone[1] : null, cssVar("--accent"), "entry high"],
        [Array.isArray(plan.entry_zone) ? plan.entry_zone[0] : null, cssVar("--accent"), "entry low"],
        [plan.stop_loss, cssVar("--neg"), "stop"],
      ];
      for (const [price, color, title] of levels) {
        if (D.isNum(price)) series.createPriceLine({ price, color, lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title });
      }
    }
    lwc.timeScale().fitContent();
  }

  // ------------------------------------------------------------------------------------------------------------
  // orchestration
  // ------------------------------------------------------------------------------------------------------------
  function renderCharts() {
    if (!state.data) return;
    renderKillRule();
    renderEquity();
  }

  function renderAll() {
    renderStrip();
    if (!state.data) return;
    renderKpis();
    renderCharts();
    renderDecisions();
    renderPositions();
    renderPending();
    renderClosed();
    renderHistory();
    renderRisk();
    renderBacktests();
    renderSymbolChart();
    $("foot-file").textContent = `${state.entry.data} \u00b7 generated ${D.fmtUtc(state.data.generated_at)}${state.data.ops ? ` \u00b7 ops schema ${state.data.ops.schema}` : " \u00b7 no ops block"}`;
  }

  function renderTabs() {
    const nav = $("inst-tabs");
    const list = state.manifest.instances;
    nav.hidden = list.length < 2;
    set("inst-tabs", html`${list.map(
      (i) => html`<button type="button" role="tab" data-id="${i.id}" aria-selected="${i.id === state.id}">${i.name}</button>`
    )}`);
  }

  function selectInstance(id, remember) {
    const entry = state.manifest.instances.find((i) => i.id === id) || state.manifest.instances[0];
    if (state.entry === entry && state.data) return;
    state.entry = entry;
    state.id = entry.id;
    state.data = null;
    state.error = null;
    state.filter = null;
    state.symbol = null;
    if (remember) {
      try {
        localStorage.setItem("dash-instance", entry.id);
      } catch (e) {
        /* blocked storage: the choice just is not remembered */
      }
    }
    renderTabs();
    for (const id_ of ["kpis", "dec-grid", "pos-table", "pend-table", "closed-table", "health", "roles"]) $(id_).innerHTML = "";
    renderStrip();
    loadData();
  }

  function currentTheme() {
    const attr = document.documentElement.getAttribute("data-theme");
    if (attr === "light" || attr === "dark") return attr;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  function wire() {
    $("theme-toggle").addEventListener("click", () => {
      const next = currentTheme() === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try {
        localStorage.setItem("dash-theme", next);
      } catch (e) {
        /* blocked storage */
      }
      renderSymbolChart(); // the only canvas chart; the SVG charts follow the CSS variables by themselves
    });
    if (window.matchMedia) {
      const mq = window.matchMedia("(prefers-color-scheme: dark)");
      const onChange = () => renderSymbolChart();
      if (mq.addEventListener) mq.addEventListener("change", onChange);
    }
    $("inst-tabs").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-id]");
      if (!btn) return;
      if (location.hash !== `#${btn.dataset.id}`) location.hash = btn.dataset.id; // hashchange does the switch
      else selectInstance(btn.dataset.id, true);
    });
    window.addEventListener("hashchange", () => selectInstance(D.pickInstance(state.manifest, location.hash, null), true));
    $("dec-filters").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-state]");
      if (!btn) return;
      state.filter = btn.dataset.state || null;
      renderDecisions();
    });
    $("closed-table").addEventListener("click", (e) => {
      if (!e.target.closest("#closed-more")) return;
      state.showAllClosed = !state.showAllClosed;
      renderClosed();
    });
    $("sym-fold").addEventListener("toggle", renderSymbolChart);
    $("sym-select").addEventListener("change", (e) => {
      state.symbol = e.target.value;
      renderSymbolChart();
    });
    let resizeTimer = null;
    let lastWidth = window.innerWidth;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        if (window.innerWidth === lastWidth) return; // mobile browsers fire resize when the URL bar hides
        lastWidth = window.innerWidth;
        renderCharts();
        renderSymbolChart();
      }, 150);
    });
  }

  async function main() {
    wire();
    state.manifest = await loadManifest();
    let stored = null;
    try {
      stored = localStorage.getItem("dash-instance");
    } catch (e) {
      /* blocked storage */
    }
    selectInstance(D.pickInstance(state.manifest, location.hash, stored), false);
    setInterval(loadData, REFRESH_MS);
    setInterval(renderStrip, TICK_MS);
  }

  main();
})();
