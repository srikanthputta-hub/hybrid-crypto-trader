// Runs web/app.js (or a frozen copy) against a minimal DOM stub built from index.html's own element ids, and prints
// the resulting state of every element as JSON. Used by tests/test_dashboard_display.py:
//   node tests/dashboard_stub.js <index.html> <app.js> <display.js|-> <data.json>
// An id app.js asks for that index.html does not define comes back null, as in a browser, so the script throws --
// that is how the test notices a mismatch between app.js and index.html.
const fs = require("fs");
const vm = require("vm");

const [indexPath, appPath, displayPath, dataPath] = process.argv.slice(2);
const html = fs.readFileSync(indexPath, "utf8");
const data = JSON.parse(fs.readFileSync(dataPath, "utf8"));

function makeEl(id, attrs) {
  const store = {
    id,
    hidden: /\shidden(\s|>|=)/.test(attrs),
    textContent: "",
    innerHTML: "",
    className: (attrs.match(/class="([^"]*)"/) || [, ""])[1],
    style: { setProperty(k, v) { this[k] = v; } },
    dataset: {},
    children: [],
    clientWidth: 800,
    value: "",
  };
  const dm = attrs.match(/data-period="([^"]*)"/);
  if (dm) store.dataset.period = dm[1];
  const classes = new Set(store.className.split(/\s+/).filter(Boolean));
  const listeners = {};
  const classList = {
    toggle(c, force) {
      const on = force === undefined ? !classes.has(c) : !!force;
      on ? classes.add(c) : classes.delete(c);
      return on;
    },
    add: (c) => classes.add(c),
    remove: (c) => classes.delete(c),
    contains: (c) => classes.has(c),
  };
  const el = new Proxy(store, {
    get(t, p) {
      if (p in t) return t[p];
      if (p === "classList") return classList;
      if (p === "addEventListener") return (ev, cb) => ((listeners[ev] = listeners[ev] || []).push(cb));
      if (p === "appendChild") return (child) => (t.children.push(child), child);
      if (p === "insertAdjacentHTML") return (_pos, h) => (t.innerHTML += h);
      if (p === "querySelectorAll" || p === "querySelector") return () => (p === "querySelector" ? null : []);
      if (p === "contains") return () => false;
      if (p === "remove" || p === "setAttribute") return () => {};
      if (p === "__classes") return classes;
      return undefined;
    },
    set(t, p, v) {
      t[p] = v;
      return true;
    },
  });
  return el;
}

const elements = {};
const tagRe = /<([a-z0-9]+)\b([^>]*?\sid="([^"]+)"[^>]*)>/g;
let m;
while ((m = tagRe.exec(html))) elements[m[3]] = makeEl(m[3], m[2]);

// the P/L modal's period buttons: real stubs, in document order
const pnlBlock = (html.match(/id="pnl-period-toggle">([\s\S]*?)<\/div>/) || [, ""])[1];
const pnlButtons = [...pnlBlock.matchAll(/<button([^>]*)>/g)].map((b, i) => makeEl(`pnl-btn-${i}`, b[1]));

const document = {
  getElementById: (id) => elements[id] || null,
  querySelectorAll: (sel) => (sel === "#pnl-period-toggle .toggle-btn" ? pnlButtons : []),
  querySelector: () => null,
  createElement: () => makeEl("created", ""),
  addEventListener: () => {},
};

const ctx = {
  document,
  fetch: async () => ({ json: async () => data }),
  setInterval: () => 0,
  setTimeout,
  console,
  Number, String, Date, Math, JSON, Set, Array, Object, isNaN, Promise,
};
ctx.window = ctx;
ctx.window.addEventListener = () => {};
vm.createContext(ctx);
if (displayPath !== "-") vm.runInContext(fs.readFileSync(displayPath, "utf8"), ctx, { filename: "display.js" });
vm.runInContext(fs.readFileSync(appPath, "utf8"), ctx, { filename: "app.js" });

setTimeout(() => {
  const dump = {};
  for (const [id, el] of Object.entries(elements)) {
    dump[id] = {
      hidden: el.hidden,
      textContent: el.textContent,
      innerHTML: el.innerHTML,
      className: el.className,
      classes: [...el.__classes].sort(),
      children: el.children.map((c) => ({ className: c.className, innerHTML: c.innerHTML, dataset: c.dataset })),
    };
  }
  dump.__pnlButtons = pnlButtons.map((b) => ({ period: b.dataset.period, classes: [...b.__classes].sort() }));
  console.log(JSON.stringify(dump));
}, 50);
