// Site region "enhanced": the Enhanced story's closed / working / open split, each MVP item with its state and last line,
// and the latest port build with its play link (card RUNNER-ENHANCED). It is the home page's top region.
// mount(el, ctx): ctx = {api(path), on(kind, fn)}. The record is refetched when a run event lands on the feed stream.
export const LIVE_KINDS = ["run.progress:tas", "run.progress:tas-target", "run.progress:unit", "run.progress:set", "run.queued", "run.ended"];
export const STATES = ["closed", "working", "open"];

const node = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) k === "class" ? (n.className = v) : n.setAttribute(k, v);
  n.append(...kids.filter(k => k != null));
  return n;
};

/** Share of the story's children in each state, in STATES order, for the bar widths. */
export function shares(story) {
  const total = STATES.reduce((sum, s) => sum + story[s], 0);
  return STATES.map(s => ({ state: s, count: story[s], percent: total ? (100 * story[s]) / total : 0 }));
}

const LABEL = { closed: "Closed", working: "Working", open: "Open" };

export function render(rec) {
  const bar = node("div", { class: "enh-bar" }, ...shares(rec.story).map(p => node("span", { class: `enh-${p.state}`, style: `width:${p.percent}%`, title: `${p.count} ${LABEL[p.state].toLowerCase()}` })));
  const split = node("p", {}, ...STATES.map(s => node("span", { class: `enh-tag enh-${s}` }, node("b", {}, String(rec.story[s])), LABEL[s])));
  const items = node("ul", { class: "enh-items" }, ...rec.items.map(i => node("li", {}, node("span", { class: "name" }, i.name), node("span", { class: `enh-pill enh-${i.state}` }, LABEL[i.state]), node("span", { class: "dim" }, i.line))));
  const b = rec.build, built = b.built ? new Date(b.built * 1000).toLocaleString("en-GB", { hour12: false }) : "never";
  const build = node("p", {}, "Port build ", node("code", {}, b.pin ? b.pin.slice(0, 12) : "unpinned"), ` built ${built} `, node("a", { class: "btn", href: b.play }, "Play it"));
  return [node("section", { class: "card" }, node("h3", {}, "Story progress"), split, bar), node("section", { class: "card" }, node("h3", {}, "MVP items"), items), node("section", { class: "card" }, node("h3", {}, "Latest build"), build)];
}

export function mount(el, ctx) {
  let alive = true;
  async function load() {
    const got = await fetch("/api/enhanced");
    const rec = await got.json();
    if (!alive || !el.isConnected) return;
    el.replaceChildren(...(got.ok ? render(rec) : [node("p", { class: "stub" }, rec.error)]));
  }
  for (const kind of LIVE_KINDS) ctx.on(kind, () => { if (alive) load(); });
  addEventListener("hashchange", () => { alive = false; }, { once: true });
  return load();
}
