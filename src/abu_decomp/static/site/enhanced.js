// Site region "enhanced": the Enhanced story's closed / working / open split, each MVP item with its state and last line,
// and the latest port build with its play link (card RUNNER-ENHANCED). Route #/enhanced.
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

export function render(rec) {
  const bar = node("div", { class: "enh-bar" }, ...shares(rec.story).map(p => node("span", { class: `enh-${p.state}`, style: `width:${p.percent}%`, title: `${p.count} ${p.state}` })));
  const split = node("p", {}, ...STATES.map(s => node("span", { class: `enh-tag enh-${s}` }, `${rec.story[s]} ${s}`)));
  const items = node("table", { class: "enh-items" }, ...rec.items.map(i => node("tr", {}, node("th", {}, i.name), node("td", { class: `enh-${i.state}` }, i.state), node("td", {}, i.line))));
  const b = rec.build, built = b.built ? new Date(b.built * 1000).toLocaleString("en-GB", { hour12: false }) : "never";
  const build = node("p", {}, "Latest port build ", node("code", {}, b.pin ? b.pin.slice(0, 12) : "unpinned"), ` built ${built} `, node("a", { class: "btn", href: b.play }, "Play it"));
  return [node("h2", {}, "Enhanced"), split, bar, node("h3", {}, "MVP items"), items, node("h3", {}, "Latest build"), build];
}

export function mount(view, ctx) {
  let alive = true;
  async function load() {
    const got = await fetch("/api/enhanced");
    const rec = await got.json();
    if (!alive || ctx.route.name !== "enhanced") return;
    view.replaceChildren(...(got.ok ? render(rec) : [node("h2", {}, "Enhanced"), node("p", { class: "stub" }, rec.error)]));
  }
  for (const kind of LIVE_KINDS) ctx.on(kind, () => { if (alive) load(); });
  addEventListener("hashchange", () => { alive = false; }, { once: true });
  return load();
}
