// Drives web/ in real Chromium (Playwright, the pre-installed /opt/pw-browsers/chromium) against fixture JSON and
// prints what the page did as one JSON object; tests/test_dashboard_page.py asserts on it.
//   node tests/dashboard_page_check.cjs <web dir> <fixtures dir>
// The fixtures dir holds busy.json, stale.json, evil.json, real.json, intraday.json (see tests/dashboard_fixture.py).
// The page is served by a local server that applies web/_headers, so the Content-Security-Policy is really enforced
// and any inline script/style or external request shows up as a console error or a foreign request.
const { chromium } = require(require.resolve("playwright", { paths: [process.env.NODE_PATH || "/opt/node22/lib/node_modules", "."] }));
const http = require("http");
const fs = require("fs");
const os = require("os");
const path = require("path");

const [WEB, FX] = process.argv.slice(2).map((p) => path.resolve(p));
const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json" };
const NOW = "2026-11-21T21:10:00Z";

function headerRules() {
  const rules = [];
  let cur = null;
  for (const line of fs.readFileSync(path.join(WEB, "_headers"), "utf8").split("\n")) {
    if (!line.trim() || line.trim().startsWith("#")) continue;
    if (!/^\s/.test(line)) rules.push((cur = { pattern: line.trim(), headers: {} }));
    else cur.headers[line.slice(0, line.indexOf(":")).trim()] = line.slice(line.indexOf(":") + 1).trim();
  }
  return rules;
}

function stage(files, manifest) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "dashcheck-"));
  fs.cpSync(WEB, dir, { recursive: true });
  for (const [name, src] of Object.entries(files)) fs.copyFileSync(path.join(FX, src), path.join(dir, name));
  if (manifest) fs.writeFileSync(path.join(dir, "instances.json"), typeof manifest === "string" ? manifest : JSON.stringify(manifest));
  return dir;
}

function serve(root, { dateHeader = NOW, missing = [] } = {}) {
  const rules = headerRules();
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, "http://x");
    const rel = url.pathname === "/" ? "/index.html" : decodeURIComponent(url.pathname);
    const file = path.join(root, rel);
    if (missing.includes(rel.slice(1)) || !file.startsWith(root) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
      res.writeHead(404);
      return res.end("nope");
    }
    const headers = { "Content-Type": MIME[path.extname(file)] || "application/octet-stream", Date: new Date(dateHeader).toUTCString() };
    for (const r of rules) if (r.pattern === "/*" || r.pattern === url.pathname) Object.assign(headers, r.headers);
    res.writeHead(200, headers);
    fs.createReadStream(file).pipe(res);
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server)));
}

async function visit(browser, root, opts = {}) {
  const server = await serve(root, opts.server);
  const base = `http://127.0.0.1:${server.address().port}`;
  const context = await browser.newContext({ viewport: { width: opts.width || 390, height: 844 }, colorScheme: opts.scheme || "light" });
  await context.clock.setFixedTime(new Date(opts.clock || NOW));
  const page = await context.newPage();
  const problems = [];
  const requests = [];
  page.on("console", (m) => {
    if (m.type() === "error" || m.type() === "warning") problems.push(m.text());
  });
  page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
  page.on("request", (r) => requests.push(r.url()));
  await page.goto(`${base}/${opts.hash ? "#" + opts.hash : ""}`);
  await page.waitForFunction(() => document.querySelector("#kpis .kpi") || !document.querySelector("#alert").hidden, null, { timeout: 8000 }).catch(() => {});
  await page.waitForTimeout(250);
  const done = async () => {
    await context.close();
    server.close();
  };
  return { page, problems, requests, base, done };
}

const text = (page, sel) => page.evaluate((s) => (document.querySelector(s) ? document.querySelector(s).textContent.trim() : null), sel);
const hidden = (page, sel) => page.evaluate((s) => document.querySelector(s).hidden, sel);

async function snapshot(v) {
  const { page } = v;
  return page.evaluate(() => {
    const kpis = {};
    document.querySelectorAll("#kpis .kpi").forEach((k) => (kpis[k.dataset.kpi] = { value: k.querySelector(".kpi-value").textContent, caption: k.querySelector(".kpi-cap").textContent }));
    const vw = document.documentElement.clientWidth;
    return {
      kpis,
      name: document.getElementById("inst-name").textContent,
      label: document.getElementById("inst-label").textContent,
      mode: document.getElementById("inst-mode").textContent,
      fresh: { text: document.getElementById("chip-fresh").textContent, cls: document.getElementById("chip-fresh").className },
      bar: { text: document.getElementById("chip-bar").textContent, hidden: document.getElementById("chip-bar").hidden },
      kill: { text: document.getElementById("chip-kill").textContent, hidden: document.getElementById("chip-kill").hidden, cls: document.getElementById("chip-kill").className },
      rule: { text: document.getElementById("chip-rule").textContent, hidden: document.getElementById("chip-rule").hidden, cls: document.getElementById("chip-rule").className },
      alert: { hidden: document.getElementById("alert").hidden, text: document.getElementById("alert").textContent, cls: document.getElementById("alert").className },
      killPill: document.getElementById("kill-pill").textContent,
      killLede: document.getElementById("kill-lede").textContent,
      killCps: [...document.querySelectorAll("#kill-cps li")].map((l) => l.textContent),
      killSvg: !!document.querySelector("#kill-r-chart svg"),
      equitySvgPath: !!document.querySelector("#equity-chart svg path.ch-line"),
      decisions: [...document.querySelectorAll("#dec-grid .dec")].map((d) => ({ symbol: d.dataset.symbol, state: d.querySelector(".st").textContent })),
      filters: [...document.querySelectorAll("#dec-filters button")].map((b) => b.textContent),
      posRows: document.querySelectorAll("#pos-table tbody tr").length,
      pendRows: document.querySelectorAll("#pend-table tbody tr").length,
      closedRows: document.querySelectorAll("#closed-table tbody tr").length,
      health: [...document.querySelectorAll("#health > div")].map((r) => ({ key: r.dataset.key, value: r.querySelector("dd").textContent, cls: r.querySelector("dd").className })),
      tabs: [...document.querySelectorAll("#inst-tabs button")].map((b) => ({ id: b.dataset.id, name: b.textContent, selected: b.getAttribute("aria-selected") })),
      tabsHidden: document.getElementById("inst-tabs").hidden,
      theme: document.documentElement.getAttribute("data-theme"),
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: vw,
      bodyBg: getComputedStyle(document.body).backgroundColor,
      foot: document.getElementById("foot-file").textContent,
      title: document.title,
      injected: { pwned: window.__pwned === undefined ? null : window.__pwned, imgs: document.querySelectorAll("img").length, scripts: document.querySelectorAll("script:not([src])").length },
      inlineHandlers: [...document.querySelectorAll("*")].filter((e) => [...e.attributes].some((a) => /^on/i.test(a.name))).length,
      inlineStyles: document.querySelectorAll("[style]").length,
    };
  });
}

(async () => {
  const browser = await chromium.launch({ executablePath: "/opt/pw-browsers/chromium", args: ["--no-sandbox"] });
  const out = {};

  // 1. a busy, fresh daily trial on a phone, then interactions
  {
    const v = await visit(browser, stage({ "data.json": "busy.json" }));
    out.busy = await snapshot(v);
    out.busy.problems = v.problems;
    out.busy.foreign = v.requests.filter((u) => !u.startsWith(v.base) && !u.startsWith("data:"));
    await v.page.click('#dec-filters button[data-state="blocked"]');
    out.busy.afterFilter = await v.page.evaluate(() => [...document.querySelectorAll("#dec-grid .dec")].map((d) => d.dataset.symbol + ":" + d.querySelector(".st").textContent));
    await v.page.click("#theme-toggle");
    out.busy.afterToggle = { theme: await v.page.evaluate(() => document.documentElement.getAttribute("data-theme")), stored: await v.page.evaluate(() => localStorage.getItem("dash-theme")), bodyBg: await v.page.evaluate(() => getComputedStyle(document.body).backgroundColor) };
    await v.page.click("#theme-toggle");
    out.busy.afterToggleBack = await v.page.evaluate(() => document.documentElement.getAttribute("data-theme"));
    await v.page.click("#closed-more");
    out.busy.closedRowsAll = await v.page.evaluate(() => document.querySelectorAll("#closed-table tbody tr").length);
    await v.page.evaluate(() => (document.getElementById("sym-fold").open = true));
    await v.page.waitForTimeout(300);
    out.busy.symChart = await v.page.evaluate(() => !!document.querySelector("#sym-chart canvas"));
    await v.done();
  }
  // 2. dark by system preference, no stored choice, then a stored light choice wins over the system
  {
    const root = stage({ "data.json": "busy.json" });
    const v = await visit(browser, root, { scheme: "dark" });
    out.darkSystem = { bodyBg: await v.page.evaluate(() => getComputedStyle(document.body).backgroundColor), theme: await v.page.evaluate(() => document.documentElement.getAttribute("data-theme")) };
    await v.page.evaluate(() => localStorage.setItem("dash-theme", "light"));
    await v.page.reload();
    await v.page.waitForTimeout(300);
    out.darkSystem.storedLight = { bodyBg: await v.page.evaluate(() => getComputedStyle(document.body).backgroundColor), theme: await v.page.evaluate(() => document.documentElement.getAttribute("data-theme")) };
    await v.done();
  }
  // 3. desktop width
  {
    const v = await visit(browser, stage({ "data.json": "busy.json" }), { width: 1280 });
    out.desktop = await snapshot(v);
    out.desktop.headerCells = await v.page.evaluate(() => document.querySelectorAll("#pos-table thead th").length);
    out.desktop.problems = v.problems;
    await v.done();
  }
  // 4. stale file
  {
    const v = await visit(browser, stage({ "data.json": "stale.json" }));
    out.stale = await snapshot(v);
    out.stale.mainStale = await v.page.evaluate(() => document.getElementById("main").classList.contains("stale"));
    await v.done();
  }
  // 5. amber: a 40-minute-old copy of the busy fixture (browser clock 37 min later)
  {
    const v = await visit(browser, stage({ "data.json": "busy.json" }), { clock: "2026-11-21T21:47:00Z", server: { dateHeader: "2026-11-21T21:47:00Z" } });
    out.amber = await snapshot(v);
    await v.done();
  }
  // 5b. the VM publishes hourly (ops.health.push_interval_minutes = 60): amber from 90 min, red from 180 min
  for (const [key, clock] of [["pushedOk", "2026-11-21T21:47:00Z"], ["pushedAmber", "2026-11-21T22:45:00Z"], ["pushedStale", "2026-11-22T00:17:00Z"]]) {
    const v = await visit(browser, stage({ "data.json": "pushed.json" }), { clock, server: { dateHeader: clock } });
    out[key] = await snapshot(v);
    await v.done();
  }
  // 6. a viewer whose clock is 3 hours wrong still sees correct freshness (the server's Date header wins)
  {
    const v = await visit(browser, stage({ "data.json": "busy.json" }), { clock: "2026-11-22T00:10:00Z", server: { dateHeader: NOW } });
    out.skew = await snapshot(v);
    await v.done();
  }
  // 7. the real current data.json (older exporter, no `ops` block)
  {
    const v = await visit(browser, stage({ "data.json": "real.json" }), { clock: "2026-10-20T20:34:30Z", server: { dateHeader: "2026-10-20T20:34:30Z" } });
    out.real = await snapshot(v);
    out.real.problems = v.problems;
    await v.done();
  }
  // 8. hostile strings everywhere
  {
    const v = await visit(browser, stage({ "data.json": "evil.json" }));
    out.evil = await snapshot(v);
    out.evil.problems = v.problems;
    out.evil.html = await v.page.evaluate(() => document.body.innerHTML.includes("<img src=x") || document.body.innerHTML.includes("<script>alert"));
    out.evil.text = await v.page.evaluate(() => document.body.textContent.includes("<img src=x onerror=window.__pwned=1>"));
    await v.done();
  }
  // 9. two instances: tabs, hash selection, switching, remembered choice
  {
    const manifest = JSON.parse(fs.readFileSync(path.join(WEB, "instances.json"), "utf8"));
    manifest.instances.push({ id: "intraday", name: "Intraday", data: "data_intraday.json" });
    const root = stage({ "data.json": "busy.json", "data_intraday.json": "intraday.json" }, manifest);
    const v = await visit(browser, root);
    out.multi = { first: await snapshot(v) };
    await v.page.click('#inst-tabs button[data-id="intraday"]');
    await v.page.waitForFunction(() => document.getElementById("inst-name").textContent === "Intraday" && document.querySelector("#kpis .kpi"));
    await v.page.waitForTimeout(200);
    out.multi.second = await snapshot(v);
    out.multi.hash = await v.page.evaluate(() => location.hash);
    out.multi.stored = await v.page.evaluate(() => localStorage.getItem("dash-instance"));
    out.multi.secondKillGridHidden = await v.page.evaluate(() => document.querySelector("#kill-card .kill-grid").hidden);
    await v.page.goto(`${v.base}/`);
    await v.page.waitForFunction(() => document.querySelector("#kpis .kpi"));
    out.multi.reload = await v.page.evaluate(() => document.getElementById("inst-name").textContent);
    await v.done();
  }
  // 10. a hostile manifest: data paths that leave the directory or the origin are ignored, no request is made to them
  {
    const manifest = { default: "x", instances: [{ id: "x", name: "Evil", data: "http://evil.example/data.json" }, { id: "y", name: "Up", data: "../data.json" }, { id: "z", name: "Slash", data: "/etc/passwd.json" }] };
    const v = await visit(browser, stage({ "data.json": "busy.json" }, manifest));
    out.badManifest = await snapshot(v);
    out.badManifest.requests = v.requests.map((u) => u.replace(v.base, ""));
    await v.done();
  }
  // 11. a data file that 404s, and a manifest that is not JSON
  {
    const v = await visit(browser, stage({}, "{not json"), { server: { missing: ["data.json"] } });
    out.missing = await snapshot(v);
    await v.done();
  }
  await browser.close();
  console.log(JSON.stringify(out));
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
