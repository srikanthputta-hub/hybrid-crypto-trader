// Screenshots + sanity checks for web/ with Playwright's Chromium. Serves a copy of web/ from a local server that
// applies web/_headers (so the Content-Security-Policy is really enforced), pins the browser clock, and reports
// console errors, CSP violations, requests to other origins and horizontal overflow for every shot.
//
//   python tests/dashboard_fixture.py /tmp/fx            # busy/stale/breach/intraday fixtures
//   NODE_PATH=$(npm root -g) node docs/dashboard/shoot.cjs docs/dashboard /tmp/fx
//
// Chromium is the pre-installed /opt/pw-browsers/chromium (no `playwright install`).
const { chromium } = require(require.resolve("playwright", { paths: [process.env.NODE_PATH || "/opt/node22/lib/node_modules", "."] }));
const http = require("http");
const fs = require("fs");
const os = require("os");
const path = require("path");

const REPO = path.resolve(__dirname, "..", "..");
const WEB = path.join(REPO, "web");
const OUT = path.resolve(process.argv[2] || __dirname);
const FX = path.resolve(process.argv[3] || "/tmp/fx");
const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml" };

function parseHeaders(file) {
  const rules = [];
  let cur = null;
  for (const line of fs.readFileSync(file, "utf8").split("\n")) {
    if (!line.trim() || line.trim().startsWith("#")) continue;
    if (!/^\s/.test(line)) rules.push((cur = { pattern: line.trim(), headers: {} }));
    else {
      const i = line.indexOf(":");
      cur.headers[line.slice(0, i).trim()] = line.slice(i + 1).trim();
    }
  }
  return rules;
}

function serve(root, fixedDate, headerRules) {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, "http://x");
    let rel = decodeURIComponent(url.pathname);
    if (rel === "/") rel = "/index.html";
    const file = path.join(root, rel);
    if (!file.startsWith(root) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
      res.writeHead(404);
      return res.end("not found");
    }
    const headers = { "Content-Type": MIME[path.extname(file)] || "application/octet-stream", Date: new Date(fixedDate).toUTCString() };
    for (const r of headerRules) if (r.pattern === "/*" || r.pattern === url.pathname || (url.pathname === "/index.html" && r.pattern === "/")) Object.assign(headers, r.headers);
    res.writeHead(200, headers);
    fs.createReadStream(file).pipe(res);
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server)));
}

function stage(dataFiles, manifest) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "dash-"));
  fs.cpSync(WEB, dir, { recursive: true });
  for (const [name, src] of Object.entries(dataFiles)) fs.copyFileSync(src, path.join(dir, name));
  if (manifest) fs.writeFileSync(path.join(dir, "instances.json"), JSON.stringify(manifest));
  return dir;
}

async function shoot(browser, scenario, { root, clock, shots, hash }) {
  const rules = parseHeaders(path.join(WEB, "_headers"));
  const server = await serve(root, clock, rules);
  const base = `http://127.0.0.1:${server.address().port}`;
  const report = [];
  for (const shot of shots) {
    const context = await browser.newContext({
      viewport: { width: shot.width, height: shot.height || 900 },
      colorScheme: shot.scheme,
      deviceScaleFactor: 1,
    });
    await context.clock.setFixedTime(new Date(clock));
    const page = await context.newPage();
    const problems = [];
    const foreign = [];
    page.on("console", (m) => {
      if (m.type() === "error" || m.type() === "warning") problems.push(`${m.type()}: ${m.text()}`);
    });
    page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
    page.on("request", (r) => {
      if (!r.url().startsWith(base) && !r.url().startsWith("data:")) foreign.push(r.url());
    });
    await page.goto(`${base}/${hash ? "#" + hash : ""}`);
    await page.waitForSelector("#kpis .kpi, #alert:not([hidden])", { timeout: 10000 });
    if (shot.click) for (const sel of shot.click) await page.click(sel);
    if (shot.open) for (const sel of shot.open) await page.evaluate((s) => (document.querySelector(s).open = true), sel);
    await page.waitForTimeout(400);
    const checks = await page.evaluate(() => {
      const vw = document.documentElement.clientWidth;
      const wide = [];
      for (const el of document.querySelectorAll("body *")) {
        const r = el.getBoundingClientRect();
        if (r.width && r.right > vw + 1 && !el.closest(".sr-only") && !el.closest(".skip-link") && !el.closest(".tabs")) wide.push(`${el.tagName.toLowerCase()}.${el.className && el.className.baseVal === undefined ? el.className : ""}`);
      }
      return { vw, scrollW: document.documentElement.scrollWidth, wide: wide.slice(0, 6), theme: document.documentElement.getAttribute("data-theme"), title: document.title };
    });
    const file = path.join(OUT, `${shot.name}.png`);
    if (shot.maxHeight) {
      // long fixture pages repeat the same cards: keep the first screens (the PNGs stay small)
      const h = await page.evaluate(() => document.documentElement.scrollHeight);
      await page.screenshot({ path: file, fullPage: true, clip: { x: 0, y: 0, width: shot.width, height: Math.min(h, shot.maxHeight) } });
    } else await page.screenshot({ path: file, fullPage: shot.full !== false });
    if (process.argv[4] && shot.name.startsWith(process.argv[5] || "")) {
      // review aid: readable slices of the full page (not part of the committed screenshots)
      const h = await page.evaluate(() => document.documentElement.scrollHeight);
      const step = shot.width < 600 ? 1000 : 1100;
      for (let y = 0, i = 0; y < h; y += step, i++) {
        await page.screenshot({ path: path.join(process.argv[4], `${shot.name}-${i}.png`), fullPage: true, clip: { x: 0, y, width: shot.width, height: Math.min(step, h - y) } });
      }
    }
    report.push({ shot: shot.name, kb: Math.round(fs.statSync(file).size / 1024), overflowX: checks.scrollW > checks.vw, wide: checks.wide, problems, foreignRequests: foreign, theme: checks.theme });
    await context.close();
  }
  server.close();
  console.log(`== ${scenario}`);
  for (const r of report) console.log(JSON.stringify(r));
  return report;
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ executablePath: "/opt/pw-browsers/chromium", args: ["--no-sandbox"] });
  const phone = { width: 390, height: 844 };
  const desk = { width: 1280, height: 900 };
  const real = stage({}, null);
  await shoot(browser, "real current web/data.json (generated 2026-10-20T20:30Z, viewed 4 min later)", {
    root: real, clock: "2026-10-20T20:34:30Z",
    shots: [
      { name: "real-phone-light", ...phone, scheme: "light" }, { name: "real-phone-dark", ...phone, scheme: "dark" },
      { name: "real-desktop-light", ...desk, scheme: "light" }, { name: "real-desktop-dark", ...desk, scheme: "dark" },
    ],
  });
  await shoot(browser, "real current web/data.json viewed 40 min after generation (amber)", {
    root: real, clock: "2026-10-20T21:10:00Z", shots: [{ name: "real-phone-amber-light", ...phone, scheme: "light", full: false }],
  });
  const busy = stage({ "data.json": path.join(FX, "busy.json") }, null);
  await shoot(browser, "fixture: several open/closed trades, fresh", {
    root: busy, clock: "2026-11-21T21:10:00Z",
    shots: [
      { name: "busy-phone-light", ...phone, scheme: "light", maxHeight: 3000 },
      { name: "busy-phone-dark", ...phone, scheme: "dark", maxHeight: 3000 },
      { name: "busy-desktop-light", ...desk, scheme: "light", maxHeight: 2300 },
      { name: "busy-desktop-dark", ...desk, scheme: "dark", maxHeight: 2300 },
      { name: "busy-phone-toggled-to-dark", ...phone, scheme: "light", click: ["#theme-toggle"], full: false },
    ],
  });
  const stale = stage({ "data.json": path.join(FX, "stale.json") }, null);
  await shoot(browser, "fixture: stale timestamp (95 min)", {
    root: stale, clock: "2026-11-21T21:10:00Z",
    shots: [{ name: "stale-phone-light", ...phone, scheme: "light", maxHeight: 1800 }, { name: "stale-desktop-dark", ...desk, scheme: "dark", maxHeight: 1300 }],
  });
  const breach = stage({ "data.json": path.join(FX, "breach.json") }, null);
  await shoot(browser, "fixture: kill rule breached", {
    root: breach, clock: "2026-11-21T21:10:00Z", shots: [{ name: "breach-phone-dark", ...phone, scheme: "dark", maxHeight: 2400 }, { name: "breach-desktop-light", ...desk, scheme: "light", maxHeight: 1300 }],
  });
  const manifest = JSON.parse(fs.readFileSync(path.join(WEB, "instances.json"), "utf8"));
  manifest.instances.push({ id: "intraday", name: "Intraday", data: "data_intraday.json" });
  const multi = stage({ "data.json": path.join(FX, "busy.json"), "data_intraday.json": path.join(FX, "intraday.json") }, manifest);
  await shoot(browser, "fixture: two instances (tabs), intraday selected via #intraday", {
    root: multi, clock: "2026-11-21T21:10:00Z", hash: "intraday",
    shots: [{ name: "multi-intraday-phone-light", ...phone, scheme: "light", maxHeight: 1800 }],
  });
  await browser.close();
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
