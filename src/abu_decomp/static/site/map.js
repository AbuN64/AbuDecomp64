// Site region "map": the left sidebar on every page. /api/map's region groups (about a dozen, ROM order) are drawn as
// blocks of still squares, one per function; a square's colour is its step on the chosen view's three-step scale
// (Decomp: assembly -> C, not matching -> matching C; Behaviour: not exercised -> exercised, differs -> exact). The
// legend with each step's count sits beside the grid at all times. Hover shows the function; click opens it in a
// full-page modal over the current page: the modal is the `fn` query of the location hash (`#/stream/7?fn=NAME`), so
// the page underneath never remounts and a link or the back button opens and closes it.
// mount(el, ctx): ctx = {contract, api(path), on(kind, fn), route}. Live changes arrive on ctx.on (the feed's push
// channel); the sidebar refetches when one lands and redraws only when the answer's version moved. boot(el) mounts it
// on a page without the runner shell (site_frame pages): it fetches the contract itself and has no push channel.
export const LIVE_KINDS = ["function.matched", "case.diffed", "feedback.filed", "run.ended"]; // events that move the rows
export const VIEW_KEY = "map.view"; // localStorage key of the chosen view
export const LOCKED = { play: "behaviour" }; // routes that show one view only: playing builds up the behaviour grid

// The location hash split into its route and query (`#/stream/7?fn=x` -> {path: "stream/7", query: {fn: "x"}}).
export function parseHash(hash) {
  const [path, query = ""] = String(hash || "").replace(/^#\/?/, "").split("?");
  return { path, query: Object.fromEntries(new URLSearchParams(query)) };
}
// The hash with the function modal opened on `name` (or closed when name is null), the route and other params kept.
export function withFn(hash, name) {
  const { path, query } = parseHash(hash);
  if (name == null) delete query.fn; else query.fn = name;
  const q = new URLSearchParams(query).toString();
  return `#/${path}${q ? `?${q}` : ""}`;
}
// The route key a page mounts on: the hash without its query, so opening or closing the modal never remounts it.
export const routeKey = hash => parseHash(hash).path;
// The view a route shows: a locked route's own view, else the chosen one when it names a view, else the first.
export function viewFor(route, chosen, views) {
  const keys = views.map(v => v.key);
  if (LOCKED[route] && keys.includes(LOCKED[route])) return LOCKED[route];
  return keys.includes(chosen) ? chosen : keys[0];
}
// Row indices bucketed by group, each bucket in ROM order (rows are [name, size, decomp, behaviour, group]).
export function bucket(rows, groups) {
  const out = groups.map(() => []);
  rows.forEach((row, i) => out[row[4]].push(i));
  return out;
}
// The step column of a view in a row, and how many rows sit on each of the view's three steps.
export const column = view => (view === "behaviour" ? 3 : 2);
export function tally(rows, view) {
  const at = column(view), out = [0, 0, 0];
  for (const row of rows) out[row[at]]++;
  return out;
}

const h = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) k === "class" ? (n.className = v) : n.setAttribute(k, v);
  n.append(...kids.filter(k => k != null));
  return n;
};
const fmt = n => n.toLocaleString("en-US");

export function mount(el, ctx) {
  let data = null, version = null, view = null, cells = [];
  el.classList.add("sitemap");
  const toggle = h("div", { class: "seg", role: "tablist", "aria-label": "map view" });
  const legend = h("ol", { class: "legend" });
  const grid = h("div", { class: "groups" }, h("p", { class: "dim" }, "Loading the map"));
  const tip = h("div", { class: "tip", role: "tooltip", hidden: "" });
  el.replaceChildren(h("div", { class: "key" }, toggle, legend), grid, tip);

  const route = () => parseHash(location.hash).path.split("/")[0];
  function setView() {
    const next = viewFor(route(), localStorage.getItem(VIEW_KEY), data.views);
    const locked = Boolean(LOCKED[route()]);
    toggle.hidden = locked;
    if (next === view && toggle.dataset.locked === String(locked)) return;
    view = next;
    toggle.dataset.locked = String(locked);
    el.dataset.view = view;
    for (const b of toggle.children) b.setAttribute("aria-selected", String(b.dataset.view === view));
    const spec = data.views.find(v => v.key === view), counts = tally(data.rows, view);
    legend.replaceChildren(...spec.steps.map((label, s) =>
      h("li", {}, h("i", { class: `sq s${s}` }), h("span", {}, label), h("b", {}, fmt(counts[s])))));
    const at = column(view);
    cells.forEach((cell, i) => { cell.className = `sq s${data.rows[i][at]}`; });
  }
  function draw() {
    toggle.replaceChildren(...data.views.map(v => h("button", { role: "tab", "data-view": v.key }, v.label)));
    const buckets = bucket(data.rows, data.groups);
    cells = new Array(data.rows.length);
    grid.replaceChildren(...data.groups.map((g, gi) => {
      const sqs = h("div", { class: "sqs" });
      for (const i of buckets[gi]) sqs.append(cells[i] = h("i", { "data-i": i }));
      return h("section", { class: "grp", "data-g": gi }, h("h3", {}, h("span", {}, g.label), h("small", {}, fmt(g.count))), sqs);
    }));
    view = null;
    setView();
  }
  async function load() {
    const got = await ctx.api("/api/map").catch(error => ({ error: error.message }));
    if (!got || got.error) return void grid.replaceChildren(h("p", { class: "dim" }, `No map: ${(got && got.error) || "no answer"}`));
    if (got.version === version) return;
    data = got; version = got.version;
    draw();
  }
  toggle.addEventListener("click", e => {
    const b = e.target.closest("button[data-view]");
    if (!b) return;
    localStorage.setItem(VIEW_KEY, b.dataset.view);
    setView();
  });
  grid.addEventListener("pointerover", e => {
    const i = e.target.dataset && e.target.dataset.i;
    if (i == null) return void (tip.hidden = true);
    const row = data.rows[i], g = data.groups[row[4]], spec = data.views.find(v => v.key === view);
    tip.replaceChildren(h("b", {}, row[0]), h("span", {}, `${spec.steps[row[column(view)]]} · ${fmt(row[1])} bytes`),
      h("span", { class: "dim" }, g.members.length > 1 ? `${g.label} group: ${g.members.join(", ")}` : g.label));
    const r = e.target.getBoundingClientRect();
    tip.style.left = `${Math.min(r.right + 8, innerWidth - 260)}px`;
    tip.style.top = `${r.top - 4}px`;
    tip.hidden = false;
  });
  grid.addEventListener("pointerleave", () => { tip.hidden = true; });
  grid.addEventListener("click", e => {
    const i = e.target.dataset && e.target.dataset.i;
    if (i != null) location.hash = withFn(location.hash, data.rows[i][0]);
  });
  addEventListener("hashchange", () => { if (data) setView(); syncModal(ctx); });
  for (const kind of LIVE_KINDS) ctx.on(kind, () => { if (el.isConnected) load(); });
  load();
  syncModal(ctx);
}

// The function modal: open over the current page while the hash names `fn`, closed (and emptied) otherwise.
let modal = null, shown = null;
function syncModal(ctx) {
  const name = parseHash(location.hash).query.fn || null;
  if (name === shown) return;
  shown = name;
  if (!modal) {
    const body = h("div", { class: "body" });
    const close = h("button", { class: "btn", "aria-label": "close" }, "Close");
    const title = h("span", { class: "mono" });
    modal = h("dialog", { class: "fnmodal" }, h("div", { class: "bar" }, title, close), body);
    close.addEventListener("click", () => modal.close());
    modal.addEventListener("close", () => { if (parseHash(location.hash).query.fn) location.hash = withFn(location.hash, null); });
    document.body.append(modal);
  }
  const [title, body] = [modal.querySelector(".bar .mono"), modal.querySelector(".body")];
  if (!name) { body.replaceChildren(); if (modal.open) modal.close(); return; }
  title.textContent = name;
  const host = h("div"); // a fresh host per function: the last one's live listeners see it disconnected
  body.replaceChildren(host);
  if (!modal.open) modal.showModal();
  import("/static/site/fn.js").then(mod => mod.mount(host, { ...ctx, route: { name: "fn", arg: name, query: {} } }));
}

// The sidebar on a page without the runner shell: its own contract read, no push channel.
export async function boot(root) {
  const api = async path => { const r = await fetch(path); return r.json(); };
  const contract = await api("/api/contract").catch(() => null);
  mount(root, { contract, api, on: () => {}, route: null });
}
