// Site region "map": the whole page is the ROM block map. /api/map's blocks (about 160 runs of equal ROM bytes, read top
// to bottom, left to right) are drawn as spaced blocks that fit one screen; a block fills with the progress colour of
// the share of its bytes the highlight selects (asm < exact < named; a judged call is green when it behaves, red when
// it differs). "Light up" lights every block in reading order until the map is fully lit; the chips narrow it to one
// kind. Hovering a block shows a short tooltip; clicking pins its details.
// mount(el, ctx): ctx = {contract, api(path), go(route), on(kind, fn)}. Live changes arrive on ctx.on (the feed's
// push channel); the map refetches when one lands and redraws only when the answer's version moved.
export const LIVE_KINDS = ["function.matched", "case.diffed", "feedback.filed", "run.ended"]; // events that move the rows
export const ALL = "all"; // the lit query's word for "every function lit"

// The bitmask of the checked filters (`filters` is the contract's [{key, bit}]).
export const maskOf = (filters, checked) => filters.reduce((m, f) => (checked.has(f.key) ? m | f.bit : m), 0);
// A function is lit when its flags share a bit with the checked filters, or when everything is lit; a zero mask lights nothing.
export const isLit = (flags, mask, all = false) => all || (flags & mask) !== 0;
// The progress colour key of a function: a judged call over the decomp state (the same rule as site_map.colour_key).
export function colourKey(state, flags, bit) {
  if (flags & bit.differs) return "differs";
  if (flags & bit.behaves) return "behaves";
  return state in { asm: 1, fuzzy: 1, exact: 1, named: 1 } ? state : "asm";
}
// Columns of the block grid for `n` blocks: a wide grid (16 by 10 for 160) so the map fits one screen.
export const gridCols = n => Math.max(1, Math.ceil(Math.sqrt(n * 1.6)));
// What lights one block: the share (0..1) of its bytes a lit function holds, and the colour key most lit bytes have.
export function blockLight(rows, block, bit, mask, all = false) {
  const bytes = {};
  let lit = 0, total = 0;
  for (let i = block.start; i < block.end; i++) {
    const [, size, st, flags] = rows[i];
    total += size;
    if (!isLit(flags, mask, all)) continue;
    lit += size;
    const key = colourKey(st, flags, bit);
    bytes[key] = (bytes[key] || 0) + size;
  }
  const key = Object.keys(bytes).sort((a, b) => bytes[b] - bytes[a] || (a < b ? -1 : 1))[0] || "asm";
  return { share: total ? lit / total : 0, key };
}
// The map is fully lit when every block's share is 1.
export const fullyLit = (rows, blocks, bit, mask, all = false) => blocks.every(b => blockLight(rows, b, bit, mask, all).share === 1);
// The lit state is the page's `lit` query (`#/?lit=named,exact`, or `lit=all`), in contract order, so a view is a URL.
export const litParam = (filters, checked) => filters.filter(f => checked.has(f.key)).map(f => f.key).join(",");
export function litFrom(hash, filters) {
  const wanted = new Set((new URLSearchParams(hash.split("?")[1] || "").get("lit") || "").split(","));
  return new Set(filters.filter(f => wanted.has(f.key)).map(f => f.key));
}
export const allFrom = hash => new URLSearchParams(hash.split("?")[1] || "").get("lit") === ALL;
// The legend: each progress colour key with the words the page shows for it, and its CSS variable (site_map.COLOURS).
export const KEYS = [["asm", "Assembly"], ["fuzzy", "Close C"], ["exact", "Exact C"], ["named", "Named C"], ["behaves", "Behaves"], ["differs", "Differs"]];
// The highlight chips: one short name and one icon (static/icons.svg) per contract filter key.
export const FILTER_NAMES = { named: ["Named", "file-code-2"], exact: ["Exact", "code-xml"], asm: ["Assembly", "list"], exercised: ["Ran", "play"], "original-only": ["Original", "history"], behaves: ["Equal", "circle-check"], differs: ["Differs", "circle-alert"], feedback: ["Flagged", "pin"], streamed: ["Streamed", "radio"] };
export const filterName = key => (FILTER_NAMES[key] || [key])[0];
export const hex = n => "0x" + n.toString(16).toUpperCase().padStart(8, "0");
export const kb = n => (n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`);
export const STATE_WORDS = Object.fromEntries(KEYS);
export const COLOUR_VAR = { asm: "--s-asm", fuzzy: "--warn", exact: "--s-exact", named: "--s-named", behaves: "--b-ok", differs: "--b-diff" };

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
  .map-page { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 24px; align-items: start; max-width: 1480px; }
  .map-page h1 { margin: 0 0 12px; font: 700 22px var(--sans); }
  .map-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 0 0 12px; }
  .map-chip { display: inline-flex; align-items: center; gap: 6px; min-height: 32px; padding: 0 12px 0 8px; border-radius: 16px; background: var(--raised); color: var(--dim);
    font: 13px var(--sans); border: 1px solid var(--line); }
  .map-chip svg.i, .map-panel svg.i { width: 16px; height: 16px; }
  .map-chip:hover { border-color: var(--accent); color: var(--text); }
  .map-chip[aria-pressed="true"] { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); font-weight: 600; }
  .map-chip.map-all { padding: 0 14px 0 10px; font-weight: 600; color: var(--text); }
  .map-chip.map-all[aria-pressed="true"] { color: var(--accent-ink); }
  .map-sep { width: 1px; align-self: stretch; background: var(--line); margin: 0 4px; }
  .map-legend { display: flex; flex-wrap: wrap; gap: 4px 14px; margin: 12px 0 0; padding: 0; list-style: none; font-size: 13px; color: var(--dim); }
  .map-legend li { display: inline-flex; align-items: center; gap: 6px; }
  .map-legend i, .map-state i { width: 12px; height: 12px; border-radius: 3px; flex: none; }
  .map-grid { display: grid; grid-template-columns: repeat(var(--cols, 16), minmax(0, 1fr)); gap: 8px; }
  .map-block { position: relative; overflow: hidden; aspect-ratio: 1.5; padding: 0; border-radius: 6px; border: 1px solid var(--line); background: var(--surface); }
  .map-block i { position: absolute; inset: auto 0 0 0; height: 0; background: var(--fill, var(--s-asm)); transition: height .3s ease, background .3s ease; transition-delay: calc(var(--i) * 9ms); }
  .map-block:hover, .map-block[aria-pressed="true"] { border-color: var(--text); }
  .map-panel { position: sticky; top: 16px; padding: 16px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface); }
  .map-panel h2 { margin: 0 0 4px; padding: 0; border: 0; font: 600 15px var(--mono); text-transform: none; letter-spacing: 0; color: var(--text); }
  .map-panel dl { display: grid; grid-template-columns: 80px 1fr; gap: 8px; margin: 12px 0; font-size: 13px; }
  .map-panel dt { color: var(--dim); }
  .map-panel dd { margin: 0; }
  .map-state, .map-links a { display: flex; align-items: center; gap: 8px; }
  .map-links a { min-height: 32px; padding: 0 12px; border-radius: 8px; border: 1px solid var(--line); background: var(--raised); text-decoration: none; font-size: 13px; }
  .map-links a:hover { border-color: var(--accent); }
  .map-tip { position: fixed; z-index: 50; pointer-events: none; max-width: 320px; padding: 8px 12px; border-radius: 8px; background: var(--raised); border: 1px solid var(--line);
    box-shadow: 0 6px 20px rgba(0,0,0,.35); font-size: 13px; }
  .map-tip[hidden] { display: none; }
  .map-tip b { display: block; font: 600 13px var(--mono); }
  .map-tip span { color: var(--dim); }
  @media (max-width: 1000px) { .map-page { grid-template-columns: 1fr; } .map-panel { position: static; } }`;

export function mount(el, ctx) {
  const filters = ctx.contract.filters;
  const checked = litFrom(location.hash, filters);
  const state = { map: null, bit: {}, mask: maskOf(filters, checked), all: allFrom(location.hash), pinned: -1, cells: [] };
  const tip = h("div", { class: "map-tip", role: "tooltip", hidden: "" });
  document.querySelectorAll(".map-tip").forEach(n => n.remove());
  document.body.append(tip);
  const chips = [];
  const allChip = h("button", { class: "map-chip map-all", type: "button", "aria-pressed": String(state.all), "data-all": "" }, icon("play"), "Light up ↓ →");
  function changed() {
    state.mask = maskOf(filters, checked);
    allChip.setAttribute("aria-pressed", String(state.all));
    chips.forEach(c => c.setAttribute("aria-pressed", String(checked.has(c.dataset.filter))));
    const query = new URLSearchParams(location.hash.split("?")[1] || "");
    const value = state.all ? ALL : litParam(filters, checked);
    if (value) query.set("lit", value); else query.delete("lit");
    history.replaceState(null, "", `${location.pathname}${location.search}${location.hash.split("?")[0] || "#/map"}${query.size ? `?${query}` : ""}`);
    draw();
  }
  allChip.addEventListener("click", () => { state.all = !state.all; checked.clear(); changed(); });
  for (const f of filters) {
    const chip = h("button", { class: "map-chip", type: "button", "aria-pressed": String(checked.has(f.key)), "data-filter": f.key }, icon((FILTER_NAMES[f.key] || [])[1] || "list"), filterName(f.key));
    chip.addEventListener("click", () => { state.all = false; checked.has(f.key) ? checked.delete(f.key) : checked.add(f.key); changed(); });
    chips.push(chip);
  }
  const legend = h("ul", { class: "map-legend" }, ...KEYS.map(([key, words]) => h("li", {}, h("i", { style: "background:var(" + COLOUR_VAR[key] + ")" }), words)));
  const grid = h("div", { class: "map-grid" });
  const panel = h("div", { class: "map-panel", "aria-live": "polite" });
  el.replaceChildren(
    h("style", {}, STYLE),
    h("div", { class: "map-page" },
      h("div", {}, h("h1", {}, "ROM Map"), h("div", { class: "map-bar" }, allChip, h("span", { class: "map-sep" }), ...chips), grid, legend),
      panel));
  showPanel();

  const light = block => blockLight(state.map.rows, block, state.bit, state.mask, state.all);
  function showPanel() {
    const block = state.map && state.map.blocks[state.pinned];
    if (!block) return panel.replaceChildren(h("h2", {}, "Block"), h("p", { class: "dim" }, "Click a block to pin its details here."));
    const rows = state.map.rows.slice(block.start, block.end), count = {};
    for (const [, , st, flags] of rows) { const k = colourKey(st, flags, state.bit); count[k] = (count[k] || 0) + 1; }
    panel.replaceChildren(
      h("h2", {}, `ROM ${hex(block.rom)}`),
      h("dl", {},
        h("dt", {}, "Size"), h("dd", {}, `${rows.length.toLocaleString("en-US")} functions · ${kb(block.bytes)}`),
        h("dt", {}, "Regions"), h("dd", {}, block.regions.map(r => state.map.regions[r].label).join(", ")),
        h("dt", {}, "States"), h("dd", {}, ...KEYS.filter(([k]) => count[k]).map(([k, words]) => h("div", { class: "map-state" }, h("i", { style: "background:var(" + COLOUR_VAR[k] + ")" }), `${count[k]} ${words}`)))),
      h("div", { class: "map-links" }, h("a", { href: `#/fn/${encodeURIComponent(rows[0][0])}` }, icon("code-xml"), "Open First Function")));
  }
  function pin(index) {
    state.pinned = index;
    el.dataset.pinned = index >= 0 ? String(index) : "";
    showPanel();
    draw();
  }

  function build() {
    const { blocks } = state.map;
    grid.style.setProperty("--cols", gridCols(blocks.length));
    grid.replaceChildren();
    state.cells = blocks.map((block, i) => {
      const cell = h("button", { class: "map-block", type: "button", "data-block": String(i), "aria-label": `ROM ${hex(block.rom)}` }, h("i", { style: `--i:${i}` }));
      cell.addEventListener("mousemove", e => {
        const { share, key } = light(block);
        tip.hidden = false;
        tip.replaceChildren(h("b", {}, `ROM ${hex(block.rom)}`), h("span", {}, `${block.end - block.start} functions · ${kb(block.bytes)} · ${Math.round(share * 100)}% ${share ? STATE_WORDS[key] : "dark"}`));
        tip.style.left = Math.min(e.clientX + 16, innerWidth - 340) + "px";
        tip.style.top = Math.min(e.clientY + 16, innerHeight - tip.offsetHeight - 8) + "px";
      });
      cell.addEventListener("mouseleave", () => { tip.hidden = true; });
      cell.addEventListener("click", () => pin(state.pinned === i ? -1 : i));
      grid.append(cell);
      return cell;
    });
  }

  function draw() {
    const { blocks } = state.map;
    state.cells.forEach((cell, i) => {
      const { share, key } = light(blocks[i]), fill = cell.firstChild;
      fill.style.height = `${Math.round(share * 100)}%`;
      fill.style.setProperty("--fill", `var(${state.map.colours[key]})`);
      cell.setAttribute("aria-pressed", String(i === state.pinned));
    });
    grid.dataset.version = state.map.version; // ready marker: the grid shows this answer under this lit state
    grid.dataset.lit = state.all ? ALL : String(state.mask);
    grid.dataset.full = String(fullyLit(state.map.rows, blocks, state.bit, state.mask, state.all));
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
    if (first || state.cells.length !== map.blocks.length) build();
    if (state.pinned >= map.blocks.length) state.pinned = -1;
    showPanel();
    draw();
  }
  addEventListener("keydown", e => { if (e.key === "Escape" && state.pinned >= 0 && el.isConnected) pin(-1); });
  addEventListener("hashchange", () => tip.remove(), { once: true });
  for (const kind of LIVE_KINDS) ctx.on(kind, () => { if (el.isConnected) load(); });
  load();
}
