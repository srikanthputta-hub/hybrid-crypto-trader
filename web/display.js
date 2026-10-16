// Pure display helpers for web/app.js, split out so they can be tested without a DOM (tests/test_dashboard_display.py
// runs this file under node). Everything keys off data.display, the optional block scripts/export_dashboard_data.py
// writes ONLY when the bot runs a daily-or-longer timeframe or sets notifier.label. With no `display` block (the
// default hourly bot) every helper returns the hourly defaults / null, so the 1h dashboard is unchanged.
(function (root) {
  const WORD = { "1d": "daily", "1w": "weekly", "1M": "monthly" };
  const PERIODS = ["hourly", "daily", "weekly"];

  const displayOf = (data) => (data && data.display) || null;

  // "2026-10-17 00:00 UTC": the bar close is a UTC instant; showing it in the viewer's local zone would move the
  // "daily close" to a different-looking hour.
  function fmtUtc(iso) {
    const d = new Date(iso);
    if (!iso || isNaN(d)) return iso || "—";
    return d.toISOString().slice(0, 16).replace("T", " ") + " UTC";
  }

  // The label the user configured (notifier.label, e.g. "PAPER TRIAL"), or null: no label, no banner.
  function trialBannerText(data) {
    const d = displayOf(data);
    if (!d || !d.label) return null;
    const bars = d.timeframe ? ` · ${d.timeframe} bars` : "";
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

  const api = { fmtUtc, trialBannerText, asOfText, defaultPnlPeriod };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.DashboardDisplay = api;
})(typeof window !== "undefined" ? window : globalThis);
