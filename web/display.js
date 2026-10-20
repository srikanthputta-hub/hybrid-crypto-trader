// Pure display/derivation layer for web/app.js. No DOM, no network, no clock of its own (every function that needs
// "now" takes it as an argument), so tests/test_dashboard_display.py can run this file under node and pin every
// number the page shows. Nothing here invents a value: a missing field comes back as null and the page says "no data".
//
// Inputs are the dashboard JSON that scripts/export_dashboard_data.py writes (web/data.json for the daily trial; one
// file per bot instance, listed in web/instances.json). The original keys are read as before; the additive `ops`
// block (instance identity, per-symbol decisions, per-trade R, equity samples, health) is read when present.
(function (root) {
  "use strict";

  // ----------------------------------------------------------------------------------------------------------
  // Daily-bar display hints (unchanged contract: data.display, written only for daily-or-longer bars / a label)
  // ----------------------------------------------------------------------------------------------------------
  const WORD = { "1d": "daily", "1w": "weekly", "1M": "monthly" };
  const PERIODS = ["hourly", "daily", "weekly"];

  const displayOf = (data) => (data && data.display) || null;
  const opsOf = (data) => (data && data.ops && typeof data.ops === "object" ? data.ops : null);

  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  // "2026-10-17 00:00 UTC": the bar close is a UTC instant; showing it in the viewer's local zone would move the
  // "daily close" to a different-looking hour.
  function fmtUtc(iso) {
    const d = new Date(iso);
    if (!iso || isNaN(d)) return iso || "\u2014";
    return d.toISOString().slice(0, 16).replace("T", " ") + " UTC";
  }

  // "Oct 17 00:05": short UTC stamp for tables (the "UTC" is stated once in the table caption).
  function fmtUtcShort(iso) {
    const d = new Date(iso);
    if (!iso || isNaN(d)) return "\u2014";
    const pad = (n) => String(n).padStart(2, "0");
    return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
  }

  // The label the user configured (notifier.label, e.g. "PAPER TRIAL"), or null: no label, no banner.
  function trialBannerText(data) {
    const d = displayOf(data);
    if (!d || !d.label) return null;
    const bars = d.timeframe ? ` \u00b7 ${d.timeframe} bars` : "";
    return `${d.label}${bars}`;
  }

  // Prices, equity and the annotated chart are as of the last closed bar; at a daily bar that is up to 24 h old.
  function asOfText(data) {
    const d = displayOf(data);
    if (!d || !d.price_as_of) return null;
    const word = WORD[d.timeframe] || d.timeframe || "bar";
    return `prices as of ${fmtUtc(d.price_as_of)} (last ${word} close)`;
  }

  // Which P/L tab the modal opens on: Daily at a daily bar (the Hourly tab is empty), Hourly otherwise.
  function defaultPnlPeriod(data) {
    const d = displayOf(data);
    return d && PERIODS.includes(d.pnl_default_period) ? d.pnl_default_period : "hourly";
  }

  // ----------------------------------------------------------------------------------------------------------
  // Escaping: every dynamic string goes through `html` (escaped by default); `raw` marks markup built here.
  // ----------------------------------------------------------------------------------------------------------
  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const escapeHtml = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ESC[c]);
  const raw = (s) => ({ __raw: String(s) });
  function part(v) {
    if (v && typeof v === "object" && "__raw" in v) return v.__raw;
    if (Array.isArray(v)) return v.map(part).join("");
    return escapeHtml(v);
  }
  // html`<b>${userText}</b>`: interpolations are escaped unless wrapped in raw() (or an array of raw()/strings).
  function html(strings, ...vals) {
    let out = strings[0];
    for (let i = 0; i < vals.length; i++) out += part(vals[i]) + strings[i + 1];
    return raw(out);
  }

  // ----------------------------------------------------------------------------------------------------------
  // Number formatting (en-US on purpose: deterministic, and a single-owner dashboard)
  // ----------------------------------------------------------------------------------------------------------
  const isNum = (n) => typeof n === "number" && isFinite(n);
  // (n === 0 ? 0 : n) turns -0 into 0: toLocaleString would print "-0".
  const group = (n, min, max) =>
    (n === 0 ? 0 : n).toLocaleString("en-US", { minimumFractionDigits: min, maximumFractionDigits: max });

  function fmtMoney(n, signed) {
    if (!isNum(n)) return "\u2014";
    const sign = n < 0 ? "-" : signed && n > 0 ? "+" : "";
    return `${sign}$${group(Math.abs(n), 2, 2)}`;
  }

  // frac 0.0123 -> "1.23%". signed adds "+" on positives.
  function fmtPct(frac, digits, signed) {
    if (!isNum(frac)) return "\u2014";
    const d = digits == null ? 2 : digits;
    const v = frac * 100;
    const sign = v < 0 ? "-" : signed && v > 0 ? "+" : "";
    return `${sign}${Math.abs(v).toFixed(d)}%`;
  }

  // R multiples: "+0.12R" / "-0.57R"
  function fmtR(n, digits) {
    if (!isNum(n)) return "\u2014";
    const d = digits == null ? 2 : digits;
    const v = Number(n.toFixed(d));
    return `${v > 0 ? "+" : v < 0 ? "-" : ""}${Math.abs(v).toFixed(d)}R`;
  }

  // Prices: 6 decimals max at $1+, 4 significant digits below (SHIB-level prices keep their distinction).
  function fmtPrice(n) {
    if (!isNum(n)) return "\u2014";
    if (n === 0) return "0";
    if (Math.abs(n) >= 1) return n.toLocaleString("en-US", { maximumFractionDigits: 6 });
    return n.toLocaleString("en-US", { minimumSignificantDigits: 4, maximumSignificantDigits: 4 });
  }

  const signClass = (n) => (!isNum(n) || n === 0 ? "" : n > 0 ? "pos" : "neg");

  // ----------------------------------------------------------------------------------------------------------
  // Freshness: "updated N min ago", amber after 15 min, red (stale warning) after 60
  // ----------------------------------------------------------------------------------------------------------
  const FRESH_WARN_MIN = 15;
  const FRESH_STALE_MIN = 60;

  function ageMinutes(iso, nowMs) {
    const t = Date.parse(iso);
    if (!iso || isNaN(t) || !isNum(nowMs)) return null;
    return Math.max(0, (nowMs - t) / 60000);
  }

  // ok <= 15 min < warn <= 60 min < stale. Unknown (no/invalid timestamp) is its own level, never "ok".
  function freshness(minutes) {
    if (!isNum(minutes)) return { level: "unknown", minutes: null };
    if (minutes > FRESH_STALE_MIN) return { level: "stale", minutes };
    if (minutes > FRESH_WARN_MIN) return { level: "warn", minutes };
    return { level: "ok", minutes };
  }

  function ageText(minutes) {
    if (!isNum(minutes)) return "unknown";
    const m = Math.floor(minutes);
    if (m < 1) return "just now";
    if (m < 60) return `${m} min ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return m % 60 ? `${h} h ${m % 60} min ago` : `${h} h ago`;
    const d = Math.floor(h / 24);
    return h % 24 ? `${d} d ${h % 24} h ago` : `${d} d ago`;
  }

  // The server's Date header minus the viewer's clock, so a phone with a wrong clock does not paint fresh data stale.
  function clockSkewMs(dateHeader, localNowMs) {
    const t = Date.parse(dateHeader);
    return isNaN(t) || !isNum(localNowMs) ? 0 : t - localNowMs;
  }

  const TF_UNIT_MIN = { m: 1, h: 60, d: 1440 };
  function timeframeMinutes(tf) {
    const m = /^(\d+)([mhd])$/.exec(tf || "");
    return m && Number(m[1]) > 0 ? Number(m[1]) * TF_UNIT_MIN[m[2]] : null;
  }

  // Has the bot decided the most recent CLOSED bar? `current` = yes; otherwise how long the close is overdue:
  // waiting (within the grace, normal just after a close), late (amber), missed (red: beyond the entry-delay guard,
  // normally 60 min). Only m/h/d bars are epoch-aligned; anything else is "unknown".
  function barStatus(lastBarClose, timeframe, nowMs) {
    const tfMin = timeframeMinutes(timeframe);
    const last = Date.parse(lastBarClose);
    if (!tfMin || !isNum(nowMs)) return { state: "unknown", expectedClose: null };
    const tfMs = tfMin * 60000;
    const expected = Math.floor(nowMs / tfMs) * tfMs;
    const expectedIso = new Date(expected).toISOString();
    if (!isNaN(last) && last >= expected) return { state: "current", expectedClose: expectedIso, overdueMinutes: 0 };
    const overdue = (nowMs - expected) / 60000;
    const grace = Math.min(15, tfMin / 4);
    const late = Math.min(60, tfMin);
    // a whole bar older than the one that just closed is a skipped decision whatever the clock says
    const skipped = !isNaN(last) && last < expected - tfMs;
    const state = skipped ? "missed" : overdue <= grace ? "waiting" : overdue <= late ? "late" : "missed";
    return { state, expectedClose: expectedIso, overdueMinutes: overdue };
  }

  // ----------------------------------------------------------------------------------------------------------
  // Portfolio derivations
  // ----------------------------------------------------------------------------------------------------------
  // Unrealised P/L of the open positions from the exported fills and the last prices. `known` < `count` when some
  // position has no price (then the sum is partial and the page says so).
  function openPnl(positions) {
    const list = Array.isArray(positions) ? positions : [];
    let value = 0;
    let cost = 0;
    let known = 0;
    for (const p of list) {
      if (isNum(p.current_price) && isNum(p.fill_price) && isNum(p.fill_amount)) {
        value += (p.current_price - p.fill_price) * p.fill_amount;
        cost += p.fill_price * p.fill_amount;
        known += 1;
      }
    }
    return { value, pct: cost > 0 ? value / cost : null, cost, count: list.length, known };
  }

  // Mark-to-market drawdown of the exported equity samples [[iso, equity, cash], ...]. The peak starts at the
  // starting cash when known (the portfolio began there). Null when there is nothing to measure.
  function drawdownSeries(points, startingCash) {
    const pts = [];
    for (const p of Array.isArray(points) ? points : []) {
      const t = Date.parse(p && p[0]);
      if (!isNaN(t) && isNum(p[1])) pts.push({ t, equity: p[1] });
    }
    if (!pts.length) return null;
    let peak = isNum(startingCash) && startingCash > 0 ? startingCash : pts[0].equity;
    let maxDd = 0;
    let maxAt = null;
    for (const p of pts) {
      peak = Math.max(peak, p.equity);
      p.peak = peak;
      p.dd = peak > 0 ? (peak - p.equity) / peak : 0;
      if (p.dd > maxDd) {
        maxDd = p.dd;
        maxAt = p.t;
      }
    }
    return { points: pts, maxDd, maxAt, currentDd: pts[pts.length - 1].dd, peak, since: pts[0].t };
  }

  // ----------------------------------------------------------------------------------------------------------
  // Kill rule: mean re-costed R vs the frozen thresholds at 20/30/40/50 trades, and the drawdown limit
  // ----------------------------------------------------------------------------------------------------------
  const EPS = 1e-9; // a mean of rounded R values sitting exactly on a threshold is not below it (float noise)
  const mean = (xs) => xs.reduce((a, b) => a + b, 0) / xs.length;

  // rule: {avg_r: [{n, max}, ...], max_drawdown}. series: per-trade re-costed R in close order (null where unknown),
  // or null when the export has none. dd: drawdownSeries() or null. A checkpoint is judged on the FIRST n trades only
  // (the rule says "at 20 trades"); one with an unknown R among them is "partial", never guessed.
  function killRuleStatus(rule, series, dd) {
    const cps = rule && Array.isArray(rule.avg_r) ? [...rule.avg_r].sort((a, b) => a.n - b.n) : [];
    const have = Array.isArray(series);
    const n = have ? series.length : 0;
    const out = {
      hasRule: cps.length > 0,
      trades: have ? n : null,
      checkpoints: [],
      curve: [],
      current: null,
      next: null,
      avgR: { state: "nodata" },
      drawdown: { state: "nodata", limit: rule && isNum(rule.max_drawdown) ? rule.max_drawdown : null },
      state: "nodata",
    };
    if (have) {
      const vals = [];
      series.forEach((r, i) => {
        if (isNum(r)) vals.push(r);
        out.curve.push({ n: i + 1, mean: vals.length ? mean(vals) : null });
      });
      out.current = { known: vals.length, mean: vals.length ? mean(vals) : null };
    }
    for (const cp of cps) {
      const reached = have && n >= cp.n;
      const entry = { n: cp.n, max: cp.max, reached, mean: null, known: 0, status: "pending" };
      if (reached) {
        const vals = series.slice(0, cp.n).filter(isNum);
        entry.known = vals.length;
        entry.mean = vals.length ? mean(vals) : null;
        entry.status = !vals.length ? "nodata" : vals.length < cp.n ? "partial" : entry.mean < cp.max - EPS ? "breach" : "ok";
      }
      out.checkpoints.push(entry);
    }
    out.next = out.checkpoints.find((c) => !c.reached) || null;
    const reachedCps = out.checkpoints.filter((c) => c.reached);
    out.latest = reachedCps.length ? reachedCps[reachedCps.length - 1] : null;
    if (out.hasRule && have) {
      if (out.checkpoints.some((c) => c.status === "breach")) out.avgR = { state: "breach" };
      else if (!out.latest) out.avgR = { state: "insufficient", need: cps[0].n, have: n };
      else if (out.latest.status === "partial" || out.latest.status === "nodata") out.avgR = { state: "partial" };
      else out.avgR = { state: "ok" };
    }
    if (dd && out.drawdown.limit != null) {
      out.drawdown = {
        state: dd.maxDd > out.drawdown.limit + EPS ? "breach" : "ok",
        limit: out.drawdown.limit,
        max: dd.maxDd,
        current: dd.currentDd,
        currentBreach: dd.currentDd > out.drawdown.limit + EPS,
      };
    }
    // Overall: a breach of either condition is a breach; otherwise "ok" means no condition met on the data we have.
    if (out.avgR.state === "breach" || out.drawdown.state === "breach") out.state = "breach";
    else if (out.avgR.state === "nodata" && out.drawdown.state === "nodata") out.state = "nodata";
    else out.state = "ok";
    return out;
  }

  // ----------------------------------------------------------------------------------------------------------
  // Decisions
  // ----------------------------------------------------------------------------------------------------------
  const DECISION_STATES = ["scan", "skip", "holding", "blocked", "entered", "exited", "error", "nodata"];
  const DECISION_LABEL = {
    scan: "scan", skip: "skip", holding: "holding", blocked: "blocked", entered: "entered", exited: "exited",
    error: "error", nodata: "no data",
  };
  function decisionCounts(rows) {
    const counts = {};
    for (const r of Array.isArray(rows) ? rows : []) {
      const s = DECISION_STATES.includes(r && r.state) ? r.state : "nodata";
      counts[s] = (counts[s] || 0) + 1;
    }
    return counts;
  }

  // ----------------------------------------------------------------------------------------------------------
  // KPI row
  // ----------------------------------------------------------------------------------------------------------
  function buildKpis(data) {
    const d = data || {};
    const ops = opsOf(d);
    const portfolio = d.portfolio || {};
    const pnl = (d.pnl && d.pnl.summary) || {};
    const stats = ops && ops.trade_stats ? ops.trade_stats : null;
    const open = openPnl(d.positions);
    const kpis = [];

    const equity = portfolio.equity;
    kpis.push({
      id: "equity", label: "Equity", value: fmtMoney(equity),
      caption: isNum(portfolio.pnl_pct) ? `${fmtPct(portfolio.pnl_pct, 2, true)} since start \u00b7 cash + open positions at the last close` : "cash + open positions at the last close",
      cls: "", note: isNum(equity) ? "" : "no data",
    });
    kpis.push({
      id: "cash", label: "Cash", value: fmtMoney(portfolio.cash),
      caption: isNum(portfolio.cash) && isNum(equity) && equity > 0 ? `${fmtPct(portfolio.cash / equity, 1)} of equity, not in a position` : "not in a position",
      cls: "", note: isNum(portfolio.cash) ? "" : "no data",
    });
    kpis.push({
      id: "open_pnl", label: "Open P/L",
      value: open.count ? (open.known ? fmtMoney(open.value, true) : "\u2014") : fmtMoney(0),
      caption: !open.count ? "no open positions"
        : open.known < open.count ? `unrealised, ${open.known} of ${open.count} positions priced`
        : `${fmtPct(open.pct, 2, true)} unrealised across ${open.count} position${open.count === 1 ? "" : "s"}`,
      cls: open.known ? signClass(open.value) : "", note: "",
    });

    const trades = stats ? stats.n : isNum(pnl.total_trades) ? pnl.total_trades : null;
    kpis.push({
      id: "closed", label: "Closed trades", value: isNum(trades) ? String(trades) : "\u2014",
      caption: isNum(pnl.total_realized_pnl) && trades ? `round trips \u00b7 realised ${fmtMoney(pnl.total_realized_pnl, true)}` : "round trips closed so far",
      cls: "", note: isNum(trades) ? "" : "no data",
    });

    const winRate = stats && isNum(stats.win_rate) ? stats.win_rate : isNum(pnl.win_rate) ? pnl.win_rate : null;
    kpis.push({
      id: "win_rate", label: "Win rate", value: winRate == null ? "\u2014" : fmtPct(winRate, 0),
      caption: winRate == null ? (trades === 0 ? "no closed trades yet" : "no data")
        : `${stats ? stats.wins : Math.round(winRate * trades)} of ${trades} closed trades made money`,
      cls: "", note: "",
    });

    let avgR = { value: "\u2014", caption: "no data", cls: "" };
    if (stats) {
      const recost = ops.recost && ops.recost.label ? ops.recost.label : "re-costed";
      if (isNum(stats.avg_r_recosted)) {
        const booked = isNum(stats.avg_r_booked) ? ` \u00b7 as booked ${fmtR(stats.avg_r_booked)}` : "";
        const partial = stats.r_missing ? ` \u00b7 ${stats.r_missing} trade${stats.r_missing === 1 ? "" : "s"} without R` : "";
        avgR = { value: fmtR(stats.avg_r_recosted), caption: `re-costed, ${recost}${booked}${partial}`, cls: signClass(stats.avg_r_recosted) };
      } else {
        avgR = { value: "\u2014", caption: stats.n ? "no R available for these trades" : "no closed trades yet", cls: "" };
      }
    }
    kpis.push({ id: "avg_r", label: "Average R", value: avgR.value, caption: avgR.caption, cls: avgR.cls, note: "" });

    const dd = ops && ops.equity_curve ? drawdownSeries(ops.equity_curve.points, ops.starting_cash) : null;
    const risk = (d.risk && d.risk.drawdown) || {};
    let ddKpi;
    if (dd) {
      ddKpi = { value: fmtPct(dd.maxDd, 2), caption: `peak-to-trough on equity samples since ${fmtUtcShort(new Date(dd.since).toISOString())} UTC \u00b7 now ${fmtPct(dd.currentDd, 2)}`, cls: dd.maxDd > 0 ? "neg" : "" };
    } else if (isNum(risk.max_drawdown_pct)) {
      ddKpi = { value: fmtPct(risk.max_drawdown_pct, 2), caption: "realised trades only (no equity samples exported)", cls: risk.max_drawdown_pct > 0 ? "neg" : "" };
    } else {
      ddKpi = { value: "\u2014", caption: "no data", cls: "" };
    }
    kpis.push({ id: "max_dd", label: "Max drawdown", value: ddKpi.value, caption: ddKpi.caption, cls: ddKpi.cls, note: "" });
    return kpis;
  }

  // ----------------------------------------------------------------------------------------------------------
  // Status strip and system health
  // ----------------------------------------------------------------------------------------------------------
  function statusModel(data, entry, nowMs) {
    const ops = opsOf(data);
    const inst = (ops && ops.instance) || {};
    const display = displayOf(data) || {};
    const fresh = freshness(ageMinutes(data && data.generated_at, nowMs));
    const timeframe = inst.timeframe || display.timeframe || null;
    const lastBar = ops && ops.health ? ops.health.last_bar_close : null;
    return {
      name: (entry && entry.name) || inst.name || "Bot",
      label: inst.label || display.label || null,
      mode: (data && data.mode) || inst.mode || null,
      timeframe,
      fresh: { ...fresh, text: fresh.level === "unknown" ? "update time unknown" : `updated ${ageText(fresh.minutes)}` },
      lastBar: lastBar || null,
      bar: lastBar || timeframe ? barStatus(lastBar, timeframe, nowMs) : { state: "unknown" },
      kill: ops && ops.kill_switch && typeof ops.kill_switch.present === "boolean" ? ops.kill_switch.present : null,
    };
  }

  // Rows for the system-health block: {key, label, value, level (ok|warn|bad|info|none), note}
  function systemHealth(data, nowMs) {
    const ops = opsOf(data);
    const rows = [];
    const lvl = (f) => (f.level === "ok" ? "ok" : f.level === "warn" ? "warn" : f.level === "stale" ? "bad" : "none");
    const gen = freshness(ageMinutes(data && data.generated_at, nowMs));
    rows.push({ key: "export", label: "Dashboard data", value: gen.level === "unknown" ? "unknown" : ageText(gen.minutes), level: lvl(gen),
      note: "when the VM last regenerated this file (every ~5 min)" });
    if (!ops) {
      rows.push({ key: "ops", label: "Bot health", value: "no data", level: "none", note: "this data.json has no `ops` block (older exporter)" });
      return rows;
    }
    const h = ops.health || {};
    const beat = freshness(ageMinutes(h.last_event_at, nowMs));
    rows.push({ key: "heartbeat", label: "Last bot cycle (heartbeat)", value: h.last_event_at ? ageText(beat.minutes) : "no data", level: h.last_event_at ? lvl(beat) : "none",
      note: h.last_event_at ? `newest audit line ${fmtUtc(h.last_event_at)}` : "no audit line yet" });
    const timeframe = ops.instance && ops.instance.timeframe;
    const bar = barStatus(h.last_bar_close, timeframe, nowMs);
    const barText = { current: "decided", waiting: "waiting for the close decision", late: "decision late", missed: "decision MISSED", unknown: "unknown" }[bar.state];
    rows.push({ key: "bar_close", label: "Last decided bar", value: h.last_bar_close ? fmtUtc(h.last_bar_close) : "no data",
      level: { current: "ok", waiting: "info", late: "warn", missed: "bad", unknown: "none" }[bar.state],
      note: bar.expectedClose ? `newest closed bar ${fmtUtc(bar.expectedClose)}: ${barText}` : barText });
    const lb = h.latest_bar || {};
    const expected = h.expected_symbols;
    if (isNum(lb.symbols_decided) && isNum(expected)) {
      rows.push({ key: "decided", label: "Symbols decided on that bar", value: `${lb.symbols_decided} / ${expected}`,
        level: lb.symbols_decided >= expected ? "ok" : "warn", note: lb.symbols_decided >= expected ? "every symbol has an audit line for the bar" : "some symbols have no audit line for the bar" });
    }
    const w = h.window || {};
    if (isNum(lb.stale_skips)) {
      rows.push({ key: "stale_skips", label: "Stale-bar skips", value: `${lb.stale_skips} on the last bar`,
        level: lb.stale_skips === 0 ? "ok" : "bad",
        note: isNum(w.stale_skips) ? `${w.stale_skips} in the last ${w.days} days (entries skipped because the decision came more than ${h.max_entry_delay_minutes == null ? "N" : h.max_entry_delay_minutes} min after the close)` : "" });
    }
    if (isNum(w.errors) || isNum(w.unavailable)) {
      const bad = (w.errors || 0) + (lb.errors || 0) > 0;
      rows.push({ key: "errors", label: "Errors / data outages", value: `${w.errors || 0} errors \u00b7 ${w.unavailable || 0} unavailable`,
        level: bad ? "bad" : (w.unavailable || 0) > 0 ? "warn" : "ok", note: `audit lines in the last ${w.days} days` });
    }
    const p = ops.parity;
    if (p && p.status) {
      rows.push({ key: "parity", label: "Parity check", value: p.status,
        level: p.status === "PASS" ? "ok" : p.status === "WARN" ? "warn" : p.status === "FAIL" ? "bad" : "info",
        note: `${p.file_modified ? "run " + fmtUtcShort(p.file_modified) + " UTC \u00b7 " : ""}${p.message || ""}` });
    } else {
      rows.push({ key: "parity", label: "Parity check", value: "no report exported", level: "none",
        note: "the weekly paper_parity_check.py --json-out report (logs/parity_*.json) is not on the exporting machine" });
    }
    if (ops.kill_switch && typeof ops.kill_switch.present === "boolean") {
      rows.push({ key: "kill_switch", label: "Kill-switch file", value: ops.kill_switch.present ? "PRESENT" : "absent",
        level: ops.kill_switch.present ? "bad" : "ok", note: ops.kill_switch.present ? "new entries are blocked; exits still run" : "new entries allowed" });
    }
    return rows;
  }

  // The one banner above the figures: the most serious thing the viewer should know before trusting a number.
  // level "bad" (red) or "warn" (amber); null when nothing is wrong. `fetchError` is the last failed load, if any.
  function alertModel(data, nowMs, fetchError) {
    if (!data) return fetchError ? { level: "bad", text: `Could not load the dashboard data (${fetchError}).` } : null;
    const gen = freshness(ageMinutes(data.generated_at, nowMs));
    if (gen.level === "stale") {
      return { level: "bad", text: `STALE DATA: this file was last regenerated ${ageText(gen.minutes)}. The VM, the bot or its uploader may be down. Treat every figure below as out of date.` };
    }
    const ops = opsOf(data);
    const beat = freshness(ageMinutes(ops && ops.health ? ops.health.last_event_at : null, nowMs));
    if (beat.level === "stale") {
      return { level: "bad", text: `The bot has not written an audit line for ${ageText(beat.minutes).replace(" ago", "")}, although the dashboard file is being regenerated. The bot process may be down.` };
    }
    if (ops && ops.kill_switch && ops.kill_switch.present === true) {
      return { level: "warn", text: "The kill-switch file is present: the bot is blocked from opening new positions (exits still run)." };
    }
    if (fetchError) return { level: "warn", text: `Showing the last data loaded; refreshing failed (${fetchError}).` };
    if (gen.level === "warn") return { level: "warn", text: `The data is ${Math.floor(gen.minutes)} min old; it normally refreshes about every 5 minutes.` };
    return null;
  }

  // ----------------------------------------------------------------------------------------------------------
  // Instances (web/instances.json): one tab per bot instance, each with its own data file and kill rule
  // ----------------------------------------------------------------------------------------------------------
  // A data file must be a plain file name next to the page: no path, no scheme, no host, so a bad manifest can never
  // make the page request anything outside its own directory.
  const SAFE_DATA = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.json$/;
  const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/;
  const FALLBACK_INSTANCE = { id: "default", name: "Dashboard", data: "data.json", kill_rule: null };

  function parseManifest(json) {
    const list = json && Array.isArray(json.instances) ? json.instances : [];
    const seen = new Set();
    const instances = [];
    for (const e of list) {
      if (!e || !SAFE_ID.test(e.id || "") || !SAFE_DATA.test(e.data || "") || seen.has(e.id)) continue;
      seen.add(e.id);
      instances.push({ id: e.id, name: typeof e.name === "string" && e.name ? e.name : e.id, data: e.data, kill_rule: validRule(e.kill_rule) });
    }
    if (!instances.length) return { instances: [{ ...FALLBACK_INSTANCE }], default: FALLBACK_INSTANCE.id };
    const def = instances.some((i) => i.id === json.default) ? json.default : instances[0].id;
    return { instances, default: def };
  }

  function validRule(rule) {
    if (!rule || !Array.isArray(rule.avg_r)) return null;
    const cps = rule.avg_r.filter((c) => c && isNum(c.n) && c.n > 0 && isNum(c.max));
    if (!cps.length) return null;
    return {
      label: typeof rule.label === "string" ? rule.label : null,
      source: typeof rule.source === "string" ? rule.source : null,
      avg_r: cps.map((c) => ({ n: c.n, max: c.max })),
      max_drawdown: isNum(rule.max_drawdown) && rule.max_drawdown > 0 ? rule.max_drawdown : null,
    };
  }

  // Which instance to open: the URL hash wins, then the remembered one, then the manifest default.
  function pickInstance(manifest, hash, stored) {
    const ids = manifest.instances.map((i) => i.id);
    const fromHash = String(hash || "").replace(/^#/, "");
    for (const candidate of [fromHash, stored, manifest.default]) if (ids.includes(candidate)) return candidate;
    return ids[0];
  }

  // ----------------------------------------------------------------------------------------------------------
  // Inline SVG charts (no library, themed through CSS classes; sized to the container so text never scales down)
  // ----------------------------------------------------------------------------------------------------------
  const r1 = (n) => Math.round(n * 10) / 10;

  function niceTicks(lo, hi, count) {
    const span = hi - lo || 1;
    const rough = span / Math.max(1, count);
    const pow = Math.pow(10, Math.floor(Math.log10(rough)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * pow).find((s) => s >= rough) || pow * 10;
    if (!isFinite(step) || step <= 0 || !isFinite(lo) || !isFinite(hi)) return [];
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Number(v.toPrecision(12)));
    return out;
  }

  // spec: {width, height, points:[{x:ms|number, y}], xKind:"time"|"number", yFmt(fn), area:"none"|"below"|"above" (fill
  // toward `baseline`), baseline:number, refLines:[{y,label,cls}], yMin,yMax (optional hard range), label (aria),
  // endLabel:fn(point)->string, cls}
  function lineChartSvg(spec) {
    const W = Math.max(240, Math.round(spec.width || 320));
    const H = Math.max(120, Math.round(spec.height || 180));
    const m = { l: 52, r: 12, t: 10, b: 22 };
    const pts = (spec.points || []).filter((p) => isNum(p.x) && isNum(p.y));
    const aria = escapeHtml(spec.label || "chart");
    if (!pts.length) {
      return `<svg class="chart ${escapeHtml(spec.cls || "")}" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${aria}: no data"><text class="ch-empty" x="${W / 2}" y="${H / 2}" text-anchor="middle">no data</text></svg>`;
    }
    let xMin = Math.min(...pts.map((p) => p.x));
    let xMax = Math.max(...pts.map((p) => p.x));
    if (xMax - xMin < 1e-9) {
      const pad = spec.xKind === "time" ? 3600e3 : 1;
      xMin -= pad;
      xMax += pad;
    }
    const refYs = (spec.refLines || []).map((r) => r.y).filter(isNum);
    const ys = pts.map((p) => p.y).concat(refYs, isNum(spec.baseline) ? [spec.baseline] : []);
    let yMin = isNum(spec.yMin) ? spec.yMin : Math.min(...ys);
    let yMax = isNum(spec.yMax) ? spec.yMax : Math.max(...ys);
    if (yMax - yMin < 1e-9) {
      const pad = Math.abs(yMax) * 0.01 || 1;
      yMin -= pad;
      yMax += pad;
    } else if (!isNum(spec.yMin) && !isNum(spec.yMax)) {
      const pad = (yMax - yMin) * 0.08;
      yMin -= pad;
      yMax += pad;
    }
    const X = (x) => m.l + ((x - xMin) / (xMax - xMin)) * (W - m.l - m.r);
    const Y = (y) => m.t + (1 - (y - yMin) / (yMax - yMin)) * (H - m.t - m.b);
    const yFmt = spec.yFmt || ((v) => String(v));
    const parts = [];
    for (const t of niceTicks(yMin, yMax, Math.max(2, Math.floor((H - m.t - m.b) / 38)))) {
      parts.push(`<line class="ch-grid" x1="${m.l}" x2="${W - m.r}" y1="${r1(Y(t))}" y2="${r1(Y(t))}"/>`);
      parts.push(`<text class="ch-axis" x="${m.l - 6}" y="${r1(Y(t)) + 4}" text-anchor="end">${escapeHtml(yFmt(t))}</text>`);
    }
    const xTicks = [];
    const nx = W < 420 ? 3 : 5;
    for (let i = 0; i < nx; i++) xTicks.push(xMin + ((xMax - xMin) * i) / (nx - 1));
    xTicks.forEach((t, i) => {
      const label = spec.xKind === "time" ? fmtUtcShort(new Date(t).toISOString()).replace(/ \d\d:\d\d$/, "") : String(Math.round(t));
      const anchor = i === 0 ? "start" : i === nx - 1 ? "end" : "middle";
      parts.push(`<text class="ch-axis" x="${r1(X(t))}" y="${H - 6}" text-anchor="${anchor}">${escapeHtml(label)}</text>`);
    });
    for (const r of spec.refLines || []) {
      if (!isNum(r.y)) continue;
      parts.push(`<line class="ch-ref ${escapeHtml(r.cls || "")}" x1="${m.l}" x2="${W - m.r}" y1="${r1(Y(r.y))}" y2="${r1(Y(r.y))}"/>`);
      if (r.label) parts.push(`<text class="ch-ref-label ${escapeHtml(r.cls || "")}" x="${W - m.r - 2}" y="${r1(Y(r.y)) - 4}" text-anchor="end">${escapeHtml(r.label)}</text>`);
    }
    const path = pts.map((p, i) => `${i ? "L" : "M"}${r1(X(p.x))} ${r1(Y(p.y))}`).join("");
    if (spec.area && spec.area !== "none" && isNum(spec.baseline)) {
      const by = r1(Y(spec.baseline));
      parts.push(`<path class="ch-area ${escapeHtml(spec.area)}" d="${path}L${r1(X(pts[pts.length - 1].x))} ${by}L${r1(X(pts[0].x))} ${by}Z"/>`);
    }
    parts.push(`<path class="ch-line" d="${path}"/>`);
    const last = pts[pts.length - 1];
    parts.push(`<circle class="ch-dot" cx="${r1(X(last.x))}" cy="${r1(Y(last.y))}" r="3.5"/>`);
    if (spec.endLabel) {
      const left = X(last.x) > W * 0.6;
      parts.push(`<text class="ch-end" x="${r1(X(last.x)) + (left ? -7 : 7)}" y="${r1(Y(last.y)) - 7}" text-anchor="${left ? "end" : "start"}">${escapeHtml(spec.endLabel(last))}</text>`);
    }
    return `<svg class="chart ${escapeHtml(spec.cls || "")}" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${aria}">${parts.join("")}</svg>`;
  }

  // Equity curve and drawdown for the same samples (already measured by drawdownSeries).
  function equityChartSvg(dd, startingCash, width, height) {
    const points = dd ? dd.points.map((p) => ({ x: p.t, y: p.equity })) : [];
    return lineChartSvg({
      width, height, points, xKind: "time", label: "Equity curve", cls: "equity",
      baseline: isNum(startingCash) ? startingCash : undefined, area: isNum(startingCash) ? "below" : "none",
      refLines: isNum(startingCash) ? [{ y: startingCash, label: "start", cls: "start" }] : [],
      yFmt: (v) => `$${group(v, 0, 0)}`, endLabel: (p) => fmtMoney(p.y),
    });
  }

  function drawdownChartSvg(dd, limit, width, height) {
    const points = dd ? dd.points.map((p) => ({ x: p.t, y: -p.dd * 100 })) : [];
    const lim = isNum(limit) ? -limit * 100 : null;
    return lineChartSvg({
      width, height, points, xKind: "time", label: "Drawdown", cls: "drawdown",
      baseline: 0, area: "above", yMax: 1, yMin: Math.min(lim == null ? -5 : lim * 1.15, ...points.map((p) => p.y * 1.15), -5),
      refLines: lim == null ? [] : [{ y: lim, label: `kill line -${group(limit * 100, 0, 1)}%`, cls: "limit" }],
      yFmt: (v) => `${group(v, 0, 1)}%`, endLabel: (p) => `${group(p.y, 1, 1)}%`,
    });
  }

  // Mean re-costed R as the trades accumulate, against the frozen thresholds at their checkpoints. The running mean is
  // drawn dashed until the first checkpoint (the rule is not evaluated there yet) and solid after it.
  function killRuleChartSvg(status, width, height) {
    const W = Math.max(240, Math.round(width || 320));
    const H = Math.max(140, Math.round(height || 200));
    const m = { l: 46, r: 14, t: 12, b: 24 };
    const cps = status.checkpoints;
    const nMax = Math.max(cps.length ? cps[cps.length - 1].n : 50, status.trades || 0);
    const xMax = nMax + Math.max(1, Math.round(nMax * 0.04));
    const curve = status.curve.filter((c) => isNum(c.mean));
    const all = cps.map((c) => c.max).concat(curve.map((c) => c.mean), [0]);
    const yMin = Math.min(...all) - 0.15;
    const yMax = Math.max(...all) + 0.15;
    const X = (n) => m.l + (n / xMax) * (W - m.l - m.r);
    const Y = (v) => m.t + (1 - (v - yMin) / (yMax - yMin)) * (H - m.t - m.b);
    const parts = [];
    for (const t of niceTicks(yMin, yMax, 4)) {
      parts.push(`<line class="ch-grid" x1="${m.l}" x2="${W - m.r}" y1="${r1(Y(t))}" y2="${r1(Y(t))}"/><text class="ch-axis" x="${m.l - 6}" y="${r1(Y(t)) + 4}" text-anchor="end">${escapeHtml(group(t, 1, 2))}</text>`);
    }
    parts.push(`<line class="ch-zero" x1="${m.l}" x2="${W - m.r}" y1="${r1(Y(0))}" y2="${r1(Y(0))}"/>`);
    const first = cps.length ? cps[0].n : 20;
    parts.push(`<rect class="ch-prezone" x="${m.l}" y="${m.t}" width="${r1(X(first) - m.l)}" height="${H - m.t - m.b}"/>`);
    // threshold staircase: flat from each checkpoint until the next one
    if (cps.length) {
      let d = "";
      cps.forEach((c, i) => {
        const x1 = r1(X(c.n));
        const x2 = r1(X(i + 1 < cps.length ? cps[i + 1].n : xMax));
        d += `${i ? "L" : "M"}${x1} ${r1(Y(c.max))}L${x2} ${r1(Y(c.max))}`;
      });
      parts.push(`<path class="ch-threshold" d="${d}"/>`);
      cps.forEach((c) => {
        parts.push(`<circle class="ch-cp ${escapeHtml(c.status)}" cx="${r1(X(c.n))}" cy="${r1(Y(c.max))}" r="4"/>`);
        const next = cps[cps.indexOf(c) + 1];
        const mid = next ? (X(c.n) + X(next.n)) / 2 : X(xMax); // centred over its own flat segment (the last one: right-aligned), clear of the dots
        parts.push(`<text class="ch-cp-label" x="${r1(mid)}" y="${r1(Y(c.max)) - 7}" text-anchor="${next ? "middle" : "end"}">${escapeHtml(group(c.max, 2, 2))}</text>`);
      });
    }
    const pre = curve.filter((c) => c.n <= first);
    const post = curve.filter((c) => c.n >= first);
    const seg = (list) => list.map((c, i) => `${i ? "L" : "M"}${r1(X(c.n))} ${r1(Y(c.mean))}`).join("");
    if (pre.length > 1) parts.push(`<path class="ch-line pre" d="${seg(pre)}"/>`);
    if (post.length > 1) parts.push(`<path class="ch-line" d="${seg(post)}"/>`);
    if (curve.length) {
      const last = curve[curve.length - 1];
      parts.push(`<circle class="ch-dot" cx="${r1(X(last.n))}" cy="${r1(Y(last.mean))}" r="4"/>`);
      const left = X(last.n) > W * 0.6;
      parts.push(`<text class="ch-end" x="${r1(X(last.n)) + (left ? -7 : 7)}" y="${r1(Y(last.mean)) - 8}" text-anchor="${left ? "end" : "start"}">${escapeHtml(fmtR(last.mean))}</text>`);
    }
    for (let n = 0; n <= xMax; n += 10) {
      parts.push(`<text class="ch-axis" x="${r1(X(n))}" y="${H - 6}" text-anchor="middle">${n}</text>`);
    }
    parts.push(`<text class="ch-axis-title" x="${W - m.r}" y="${m.t + 10}" text-anchor="end">trades closed \u2192</text>`);
    return `<svg class="chart killrule" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Mean re-costed R against the kill thresholds">${parts.join("")}</svg>`;
  }

  // Drawdown against the kill line as a horizontal meter: the track runs 0 .. a bit past the limit; the wide bar is the
  // worst drawdown so far, the thin bar the current one, the tick the limit. Null limit/values draw only what is known.
  function drawdownMeterSvg(width, current, max, limit) {
    const W = Math.max(200, Math.round(width || 300));
    const H = 46;
    const l = 4;
    const r = 4;
    const top = Math.max(isNum(limit) ? limit * 1.25 : 0, isNum(max) ? max * 1.1 : 0, isNum(current) ? current * 1.1 : 0, 0.05);
    const X = (v) => r1(l + (Math.min(Math.max(v, 0), top) / top) * (W - l - r));
    const parts = [`<rect class="dd-track" x="${l}" y="14" width="${W - l - r}" height="14" rx="7"/>`];
    if (isNum(max)) parts.push(`<rect class="dd-max ${isNum(limit) && max > limit ? "breach" : ""}" x="${l}" y="14" width="${Math.max(0, X(max) - l)}" height="14" rx="7"/>`);
    if (isNum(current)) parts.push(`<rect class="dd-cur" x="${l}" y="19" width="${Math.max(0, X(current) - l)}" height="4" rx="2"/>`);
    if (isNum(limit)) {
      parts.push(`<line class="dd-limit" x1="${X(limit)}" x2="${X(limit)}" y1="8" y2="34"/>`);
      parts.push(`<text class="ch-ref-label limit" x="${X(limit)}" y="${H - 2}" text-anchor="${X(limit) > W * 0.7 ? "end" : "middle"}">kill line ${escapeHtml(group(limit * 100, 0, 1))}%</text>`);
    }
    parts.push(`<text class="ch-axis" x="${l}" y="${H - 2}" text-anchor="start">0%</text>`);
    return `<svg class="chart meter" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Drawdown against the kill line">${parts.join("")}</svg>`;
  }

  const api = {
    // daily-bar hints
    fmtUtc, fmtUtcShort, trialBannerText, asOfText, defaultPnlPeriod,
    // escaping
    escapeHtml, raw, html,
    // formatting
    fmtMoney, fmtPct, fmtR, fmtPrice, signClass, isNum,
    // freshness
    FRESH_WARN_MIN, FRESH_STALE_MIN, ageMinutes, freshness, ageText, clockSkewMs, timeframeMinutes, barStatus,
    // derivations
    openPnl, drawdownSeries, killRuleStatus, decisionCounts, DECISION_STATES, DECISION_LABEL,
    buildKpis, statusModel, systemHealth, alertModel,
    // instances
    parseManifest, pickInstance, SAFE_DATA,
    // charts
    niceTicks, lineChartSvg, equityChartSvg, drawdownChartSvg, killRuleChartSvg, drawdownMeterSvg,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.DashboardDisplay = api;
})(typeof window !== "undefined" ? window : globalThis);
