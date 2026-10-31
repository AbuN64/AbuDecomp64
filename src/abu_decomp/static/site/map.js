// Site region "map": a canvas of one box per function row of /api/map, coloured by progress (asm < exact < named; a
// judged call is green when it behaves, red when it differs) and lit by the filter chips (all unchecked = all dark).
// mount(el, ctx): ctx = {contract, api(path), go(route), on(kind, fn)}. Live changes arrive on ctx.on (the feed's
// push channel); the map refetches when one lands and redraws only when the answer's version moved.
export const CELL = 8; // box pitch in px
export const GAP = 2;
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
export const KEYS = [["asm", "Still assembly"], ["fuzzy", "Close C, not exact"], ["exact", "C matches the ROM"], ["named", "C with a clean name"], ["behaves", "Calls behave equally"], ["differs", "A call differs"]];
export const STATE_WORDS = Object.fromEntries(KEYS);
export const COLOUR_VAR = { asm: "--s-asm", fuzzy: "--warn", exact: "--s-exact", named: "--s-named", behaves: "--b-ok", differs: "--b-diff" };
export const boxAt = (x, y, cols) => Math.floor(y / CELL) * cols + Math.floor(x / CELL);

const h = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) k === "class" ? (n.className = v) : n.setAttribute(k, v);
  n.append(...kids);
  return n;
};

export function mount(el, ctx) {
  const filters = ctx.contract.filters;
  const checked = litFrom(location.hash, filters);
  const state = { map: null, bit: {}, mask: maskOf(filters, checked) };
  const canvas = h("canvas", { class: "map-canvas" });
  const name = h("div", { class: "map-name mono dim" }, " ");
  const chips = filters.map(f => {
    const chip = h("button", { class: "map-chip", type: "button", "aria-pressed": String(checked.has(f.key)), "data-filter": f.key }, f.label[0].toUpperCase() + f.label.slice(1));
    chip.addEventListener("click", () => {
      checked.has(f.key) ? checked.delete(f.key) : checked.add(f.key);
      chip.setAttribute("aria-pressed", String(checked.has(f.key)));
      state.mask = maskOf(filters, checked);
      const query = new URLSearchParams(location.hash.split("?")[1] || "");
      if (checked.size) query.set("lit", litParam(filters, checked)); else query.delete("lit");
      history.replaceState(null, "", `${location.pathname}${location.search}${location.hash.split("?")[0] || "#/"}${query.size ? `?${query}` : ""}`);
      draw();
    });
    return chip;
  });
  const legend = h("ul", { class: "map-legend" }, ...KEYS.map(([key, words]) => h("li", {}, h("i", { style: "background:var(" + COLOUR_VAR[key] + ")" }), words)));
  const tip = h("div", { class: "map-tip", role: "tooltip", hidden: "" });
  document.body.append(tip);
  el.replaceChildren(
    h("style", {}, `
      .map-chips { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
      .map-chip { min-height: 32px; padding: 4px 14px; border-radius: 16px; background: var(--raised); color: var(--dim); font: 13px var(--sans); border: 1px solid var(--line); }
      .map-chip:hover { border-color: var(--accent); color: var(--text); }
      .map-chip[aria-pressed="true"] { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); font-weight: 600; }
      .map-canvas { display: block; width: 100%; border-radius: 8px; background: var(--raised); cursor: pointer; }
      .map-note { margin: 0 0 12px; font-size: 12px; color: var(--dim); }
      .map-legend { list-style: none; margin: 0 0 16px; padding: 0; display: grid; grid-template-columns: 1fr 1fr; gap: 8px 12px; font-size: 13px; }
      .map-legend li { display: flex; align-items: center; gap: 8px; }
      .map-legend i { width: 14px; height: 14px; border-radius: 3px; flex: none; }
      .map-tip { position: fixed; z-index: 50; pointer-events: none; max-width: 320px; padding: 10px 12px; border-radius: 8px; background: var(--raised); border: 1px solid var(--line);
        box-shadow: 0 6px 20px rgba(0,0,0,.35); font-size: 13px; }
      .map-tip[hidden] { display: none; }
      .map-tip b { display: block; font: 600 13px var(--mono); margin-bottom: 4px; word-break: break-all; }
      .map-tip span { display: block; color: var(--dim); }`),
    h("h2", {}, "Function map"), h("p", { class: "map-note" }, "Pick what to light up. Hover a box for its details, click it to open the function."),
    h("div", { class: "map-chips" }, ...chips), legend, canvas);

  function draw() {
    const rows = state.map ? state.map.rows : [];
    const width = Math.floor(canvas.parentElement.clientWidth || canvas.clientWidth || 300);
    const { cols, height } = layout(rows.length, width);
    canvas.width = width;
    canvas.height = height;
    const style = getComputedStyle(document.documentElement);
    const paint = key => style.getPropertyValue(state.map.colours[key]).trim();
    const dark = style.getPropertyValue("--line").trim();
    const g = canvas.getContext("2d");
    g.clearRect(0, 0, width, height);
    rows.forEach(([, , st, flags], i) => {
      g.fillStyle = isLit(flags, state.mask) ? paint(colourKey(st, flags, state.bit)) : dark; // dark unless a checked filter selects it
      g.fillRect((i % cols) * CELL, Math.floor(i / cols) * CELL, CELL - GAP, CELL - GAP);
    });
    canvas.dataset.version = state.map.version; // ready marker: the canvas shows this answer under this mask
    canvas.dataset.mask = String(state.mask);
  }

  async function load() {
    const map = await ctx.api("/api/map");
    if (!map.rows) {
      el.dataset.error = String(map.error || "unavailable");
      return;
    }
    delete el.dataset.error;
    if (state.map && state.map.version === map.version) return;
    state.map = map;
    state.bit = Object.fromEntries(map.filters.map(f => [f.key, f.bit]));
    draw();
  }
  canvas.addEventListener("mousemove", e => {
    const rect = canvas.getBoundingClientRect(), { cols } = layout(state.map ? state.map.rows.length : 0, canvas.width);
    const scale = canvas.width / rect.width;
    const row = state.map && state.map.rows[boxAt((e.clientX - rect.left) * scale, (e.clientY - rect.top) * scale, cols)];
    canvas.dataset.hover = row ? row[0] : "";
    tip.hidden = !row;
    if (!row) return;
    const on = filters.filter(f => row[3] & f.bit).map(f => f.label);
    tip.replaceChildren(h("b", {}, row[0]), h("span", {}, STATE_WORDS[colourKey(row[2], row[3], state.bit)]), h("span", {}, row[1].toLocaleString("en-US") + " bytes"), ...on.map(l => h("span", {}, l)));
    tip.style.left = Math.min(e.clientX + 16, innerWidth - 340) + "px";
    tip.style.top = Math.min(e.clientY + 16, innerHeight - tip.offsetHeight - 8) + "px";
  });
  canvas.addEventListener("mouseleave", () => { tip.hidden = true; canvas.dataset.hover = ""; });
  canvas.addEventListener("click", e => {
    if (canvas.dataset.hover) ctx.go(`fn/${canvas.dataset.hover}`);
  });
  for (const kind of LIVE_KINDS) ctx.on(kind, load);
  load();
}
