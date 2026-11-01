// Site region "map": the whole page is the function map. /api/map's rows are drawn region by region (one labelled
// section per ROM segment, a canvas of one box per function), coloured by progress (asm < exact < named; a judged call
// is green when it behaves, red when it differs) and lit by the highlight chips (all unchecked = all dark). Hovering a
// box shows a short tooltip; clicking pins its detail panel (name, region, state, coverage, links).
// mount(el, ctx): ctx = {contract, api(path), go(route), on(kind, fn)}. Live changes arrive on ctx.on (the feed's
// push channel); the map refetches when one lands and redraws only when the answer's version moved.
export const CELL = 16; // box pitch in px: a 12 px box with a 4 px gap
export const GAP = 4;
export const LIVE_KINDS = ["function.matched", "case.diffed", "feedback.filed", "run.ended"]; // events that move the rows

// The bitmask of the checked filters (`filters` is the contract's [{key, bit}]).
export const maskOf = (filters, checked) => filters.reduce((m, f) => (checked.has(f.key) ? m | f.bit : m), 0);
// A box is lit when its flags share a bit with the checked filters; a zero mask lights nothing.
export const isLit = (flags, mask) => (flags & mask) !== 0;
// The progress colour key of a box: a judged call over the decomp state (the same rule as site_map.colour_key).
export function colourKey(state, flags, bit) {
  if (flags & bit.differs) return "differs";
  if (flags & bit.behaves) return "behaves";
  return state in { asm: 1, fuzzy: 1, exact: 1, named: 1 } ? state : "asm";
}
// Grid of `n` boxes in `width` px: columns, rows and the canvas height.
export function layout(n, width) {
  const cols = Math.max(1, Math.floor(width / CELL));
  const rows = Math.ceil(n / cols);
  return { cols, rows, height: rows * CELL };
}
// The checked filters are the page's `lit` query (`#/?lit=named,exact`), in contract order, so a view is a URL.
export const litParam = (filters, checked) => filters.filter(f => checked.has(f.key)).map(f => f.key).join(",");
export function litFrom(hash, filters) {
  const wanted = new Set((new URLSearchParams(hash.split("?")[1] || "").get("lit") || "").split(","));
  return new Set(filters.filter(f => wanted.has(f.key)).map(f => f.key));
}
// The legend: each progress colour key with the words the page shows for it, and its CSS variable (site_map.COLOURS).
export const KEYS = [["asm", "Still Assembly"], ["fuzzy", "Close C"], ["exact", "Exact C"], ["named", "Named C"], ["behaves", "Calls Behave"], ["differs", "A Call Differs"]];
// The highlight chips: one proper-case name and one icon (static/icons.svg) per contract filter key.
export const FILTER_NAMES = { named: ["Named C", "file-code-2"], exact: ["Exact C", "code-xml"], asm: ["Still Assembly", "list"], exercised: ["Port Ran It", "play"], "original-only": ["Original Only", "history"], behaves: ["Calls Equal", "circle-check"], differs: ["Call Differs", "circle-alert"], feedback: ["Human Flag", "pin"], streamed: ["Stream Hit", "radio"] };
export const filterName = key => (FILTER_NAMES[key] || [key])[0];
export const hex = n => "0x" + n.toString(16).toUpperCase().padStart(8, "0");
export const kb = n => (n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`);
// Rows of one region, as [global row index, row] pairs in ROM order.
export const inRegion = (rows, region) => rows.flatMap((row, i) => (row[4] === region ? [[i, row]] : []));
export const STATE_WORDS = Object.fromEntries(KEYS);
export const COLOUR_VAR = { asm: "--s-asm", fuzzy: "--warn", exact: "--s-exact", named: "--s-named", behaves: "--b-ok", differs: "--b-diff" };
export const boxAt = (x, y, cols) => Math.floor(y / CELL) * cols + Math.floor(x / CELL);

const h = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) k === "class" ? (n.className = v) : n.setAttribute(k, v);
  n.append(...kids.filter(k => k != null));
  return n;
};
const icon = name => {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg"), u = document.createElementNS("http://www.w3.org/2000/svg", "use");
  s.setAttribute("class", "i");
  u.setAttribute("href", `/static/icons.svg#i-${name}`);
  s.append(u);
  return s;
};

const STYLE = `
  .map-page { display: grid; grid-template-columns: minmax(0, 1fr) 340px; gap: 32px; align-items: start; max-width: 1480px; }
  .map-page h1 { margin: 0 0 6px; font: 700 24px var(--sans); }
  .map-note { margin: 0 0 24px; color: var(--dim); }
  .map-group { margin: 0 0 8px; font: 600 12px var(--sans); letter-spacing: .06em; text-transform: uppercase; color: var(--dim); }
  .map-chips, .map-legend { display: flex; flex-wrap: wrap; gap: 12px; margin: 0 0 24px; padding: 0; list-style: none; }
  .map-chip { display: inline-flex; align-items: center; gap: 10px; min-height: 40px; padding: 0 16px 0 12px; border-radius: 20px; background: var(--raised); color: var(--dim);
    font: 14px var(--sans); border: 1px solid var(--line); }
  .map-chip svg.i, .map-panel svg.i { width: 20px; height: 20px; }
  .map-chip:hover { border-color: var(--accent); color: var(--text); }
  .map-chip[aria-pressed="true"] { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); font-weight: 600; }
  .map-legend li { display: inline-flex; align-items: center; gap: 10px; font-size: 14px; }
  .map-legend i, .map-state i { width: 16px; height: 16px; border-radius: 4px; flex: none; }
  .map-region { margin: 0 0 28px; padding: 16px 18px 18px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface); }
  .map-head { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 16px; margin: 0 0 14px; }
  .map-region h3 { margin: 0; font: 600 16px var(--sans); }
  .map-region .dim { font: 13px var(--mono); }
  .map-canvas { display: block; max-width: 100%; cursor: pointer; }
  .map-panel { position: sticky; top: 16px; padding: 20px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface); }
  .map-panel h2 { margin: 0 0 4px; padding: 0; border: 0; font: 600 16px var(--mono); text-transform: none; letter-spacing: 0; color: var(--text); word-break: break-all; }
  .map-panel dl { display: grid; grid-template-columns: 96px 1fr; gap: 12px 12px; margin: 16px 0; }
  .map-panel dt { color: var(--dim); }
  .map-panel dd { margin: 0; }
  .map-state, .map-cover li, .map-links a, .map-links button { display: flex; align-items: center; gap: 10px; }
  .map-cover, .map-links { margin: 0; padding: 0; list-style: none; display: grid; gap: 8px; }
  .map-links { margin-top: 20px; }
  .map-links a, .map-links button { min-height: 40px; padding: 0 14px; border-radius: 8px; border: 1px solid var(--line); background: var(--raised); text-decoration: none; }
  .map-links a:hover, .map-links button:hover { border-color: var(--accent); }
  .map-tip { position: fixed; z-index: 50; pointer-events: none; max-width: 320px; padding: 8px 12px; border-radius: 8px; background: var(--raised); border: 1px solid var(--line);
    box-shadow: 0 6px 20px rgba(0,0,0,.35); font-size: 13px; }
  .map-tip[hidden] { display: none; }
  .map-tip b { display: block; font: 600 13px var(--mono); word-break: break-all; }
  .map-tip span { color: var(--dim); }
  @media (max-width: 1100px) { .map-page { grid-template-columns: 1fr; } .map-panel { position: static; } }`;

export function mount(el, ctx) {
  const filters = ctx.contract.filters;
  const checked = litFrom(location.hash, filters);
  const state = { map: null, bit: {}, mask: maskOf(filters, checked), pinned: -1, sections: [] };
  const tip = h("div", { class: "map-tip", role: "tooltip", hidden: "" });
  document.querySelectorAll(".map-tip").forEach(n => n.remove());
  document.body.append(tip);
  const chips = filters.map(f => {
    const chip = h("button", { class: "map-chip", type: "button", "aria-pressed": String(checked.has(f.key)), "data-filter": f.key }, icon((FILTER_NAMES[f.key] || [])[1] || "list"), filterName(f.key));
    chip.addEventListener("click", () => {
      checked.has(f.key) ? checked.delete(f.key) : checked.add(f.key);
      chip.setAttribute("aria-pressed", String(checked.has(f.key)));
      state.mask = maskOf(filters, checked);
      const query = new URLSearchParams(location.hash.split("?")[1] || "");
      if (checked.size) query.set("lit", litParam(filters, checked)); else query.delete("lit");
      history.replaceState(null, "", `${location.pathname}${location.search}${location.hash.split("?")[0] || "#/map"}${query.size ? `?${query}` : ""}`);
      draw();
    });
    return chip;
  });
  const legend = h("ul", { class: "map-legend" }, ...KEYS.map(([key, words]) => h("li", {}, h("i", { style: "background:var(" + COLOUR_VAR[key] + ")" }), words)));
  const regions = h("div", { class: "map-regions" });
  const panel = h("div", { class: "map-panel", "aria-live": "polite" });
  el.replaceChildren(
    h("style", {}, STYLE),
    h("div", { class: "map-page" },
      h("div", {}, h("h1", {}, "Function Map"), h("p", { class: "map-note" }, "Every function of the ROM, grouped by region. Pick what to light up, hover a box for its name, click it to pin its details."),
        h("div", { class: "map-group" }, "Highlight"), h("div", { class: "map-chips" }, ...chips),
        h("div", { class: "map-group" }, "Progress"), legend, regions),
      panel));
  showPanel();

  const rowAt = (canvas, e) => {
    const rect = canvas.getBoundingClientRect(), { cols } = layout(canvas.rows.length, canvas.width / canvas.dpr);
    const x = e.clientX - rect.left, y = e.clientY - rect.top;
    if (x >= cols * CELL) return null;
    const at = canvas.rows[boxAt(x, y, cols)];
    return at ? at : null;
  };
  function showPanel() {
    const hit = state.map && state.map.rows[state.pinned];
    if (!hit) return panel.replaceChildren(h("h2", {}, "Details"), h("p", { class: "dim" }, "Click a box on the map to pin its details here."));
    const [name, size, st, flags, region] = hit, key = colourKey(st, flags, state.bit), r = state.map.regions[region];
    const on = filters.filter(f => flags & f.bit);
    panel.replaceChildren(
      h("h2", {}, name),
      h("dl", {},
        h("dt", {}, "Region"), h("dd", {}, r.label),
        h("dt", {}, "State"), h("dd", { class: "map-state" }, h("i", { style: "background:var(" + COLOUR_VAR[key] + ")" }), STATE_WORDS[key]),
        h("dt", {}, "Size"), h("dd", {}, `${size.toLocaleString("en-US")} bytes`),
        h("dt", {}, "Coverage"), h("dd", {}, on.length ? h("ul", { class: "map-cover" }, ...on.map(f => h("li", {}, icon(FILTER_NAMES[f.key][1]), filterName(f.key)))) : "Not reached")),
      h("ul", { class: "map-links" },
        h("li", {}, h("a", { href: `#/fn/${encodeURIComponent(name)}` }, icon("code-xml"), "Open Function")),
        h("li", {}, h("button", { type: "button", "data-unpin": "" }, icon("x"), "Unpin"))));
    panel.querySelector("[data-unpin]").addEventListener("click", () => pin(-1));
  }
  function pin(index) {
    state.pinned = index;
    el.dataset.pinned = index >= 0 ? state.map.rows[index][0] : "";
    showPanel();
    draw();
  }

  function build() {
    const rows = state.map.rows;
    state.sections = state.map.regions.map((r, i) => {
      const canvas = h("canvas", { class: "map-canvas", "aria-label": `${r.label} functions` });
      canvas.rows = inRegion(rows, i);
      canvas.dpr = devicePixelRatio || 1;
      canvas.addEventListener("mousemove", e => {
        const hit = rowAt(canvas, e);
        canvas.dataset.hover = hit ? hit[1][0] : "";
        tip.hidden = !hit;
        if (!hit) return;
        const [name, , st, flags] = hit[1];
        tip.replaceChildren(h("b", {}, name), h("span", {}, STATE_WORDS[colourKey(st, flags, state.bit)]));
        tip.style.left = Math.min(e.clientX + 16, innerWidth - 340) + "px";
        tip.style.top = Math.min(e.clientY + 16, innerHeight - tip.offsetHeight - 8) + "px";
      });
      canvas.addEventListener("mouseleave", () => { tip.hidden = true; canvas.dataset.hover = ""; });
      canvas.addEventListener("click", e => { const hit = rowAt(canvas, e); if (hit) pin(hit[0]); });
      const meta = h("span", { class: "dim" }, `${r.count.toLocaleString("en-US")} functions · ${kb(r.bytes)} · ROM ${hex(r.rom)}`);
      regions.append(h("section", { class: "map-region", "data-region": r.key }, h("div", { class: "map-head" }, h("h3", {}, r.label), meta), canvas));
      return canvas;
    });
  }

  function draw() {
    const style = getComputedStyle(document.documentElement);
    const paint = key => style.getPropertyValue(state.map.colours[key]).trim();
    const dark = style.getPropertyValue("--line").trim(), ring = style.getPropertyValue("--text").trim();
    for (const canvas of state.sections) {
      const width = Math.floor(canvas.parentElement.clientWidth - 36) || 300, dpr = canvas.dpr;
      const { cols, height } = layout(canvas.rows.length, width);
      canvas.width = width * dpr;
      canvas.height = height * dpr;
      canvas.style.width = width + "px";
      canvas.style.height = height + "px";
      const g = canvas.getContext("2d");
      g.scale(dpr, dpr);
      canvas.rows.forEach(([index, [, , st, flags]], i) => {
        const x = (i % cols) * CELL, y = Math.floor(i / cols) * CELL;
        g.fillStyle = isLit(flags, state.mask) ? paint(colourKey(st, flags, state.bit)) : dark; // dark unless a checked filter selects it
        g.fillRect(x, y, CELL - GAP, CELL - GAP);
        if (index === state.pinned) { g.strokeStyle = ring; g.lineWidth = 2; g.strokeRect(x - 1, y - 1, CELL - GAP + 2, CELL - GAP + 2); }
      });
      canvas.dataset.version = state.map.version; // ready marker: the canvas shows this answer under this mask
      canvas.dataset.mask = String(state.mask);
    }
  }

  async function load() {
    const map = await ctx.api("/api/map");
    if (!map.rows) {
      el.dataset.error = String(map.error || "unavailable");
      return;
    }
    delete el.dataset.error;
    if (state.map && state.map.version === map.version) return;
    const first = !state.map;
    state.map = map;
    state.bit = Object.fromEntries(map.filters.map(f => [f.key, f.bit]));
    if (first || state.sections.length !== map.regions.length) { regions.replaceChildren(); build(); }
    else state.sections.forEach((c, i) => { c.rows = inRegion(map.rows, i); });
    if (state.pinned >= map.rows.length) state.pinned = -1;
    showPanel();
    draw();
  }
  addEventListener("keydown", e => { if (e.key === "Escape" && state.pinned >= 0 && el.isConnected) pin(-1); });
  addEventListener("resize", () => { if (state.map && el.isConnected) draw(); });
  addEventListener("hashchange", () => tip.remove(), { once: true });
  for (const kind of LIVE_KINDS) ctx.on(kind, () => { if (el.isConnected) load(); });
  load();
}
