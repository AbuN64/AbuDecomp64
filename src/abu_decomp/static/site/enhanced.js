// Site region "enhanced": the Enhanced MVP plan (cards RUNNER-ENHANCED, RUNNER-ENHANCED-PLAN). It takes over the page: the
// plan's closed / working / open split, each MVP item with its plan rows (card link, state, last line), the critical path
// (the plan's longest chain) highlighted, and the finish line (the platform proofs and the launch gate). It is the home page.
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

const pill = state => node("span", { class: `enh-pill enh-${state}` }, LABEL[state]);
const cardLink = (rec, row, text) => node("a", { href: `${rec.kan}/cards/${row.card}`, target: "_blank", rel: "noopener" }, text);

/** The rows of one item or of the finish line: card link, state, minutes and the card's last line; critical rows marked. */
export function rowList(rec, rows) {
  return node("ul", { class: "enh-rows" }, ...rows.map(r => node("li", { class: r.critical ? "crit" : null },
    node("span", { class: "name" }, cardLink(rec, r, r.key), r.critical ? node("b", { class: "enh-crit", title: "on the critical path" }, "critical") : null),
    pill(r.state), node("span", { class: "dim" }, `${r.minutes} min`), node("span", { class: "dim" }, r.line))));
}

const itemCard = (rec, item) => node("section", { class: "card" },
  node("h3", {}, item.name, pill(item.state)), node("p", { class: "dim" }, item.acceptance), rowList(rec, item.rows));

export function render(rec) {
  const bar = node("div", { class: "enh-bar" }, ...shares(rec.plan).map(p => node("span", { class: `enh-${p.state}`, style: `width:${p.percent}%`, title: `${p.count} ${LABEL[p.state].toLowerCase()}` })));
  const split = node("p", {}, ...STATES.map(s => node("span", { class: `enh-tag enh-${s}` }, node("b", {}, String(rec.plan[s])), LABEL[s])));
  const b = rec.build, built = b.built ? new Date(b.built * 1000).toLocaleString("en-GB", { hour12: false }) : "never";
  const nav = node("p", { class: "enh-nav" }, node("a", { class: "btn", href: "#/map" }, "Map"), node("a", { class: "btn", href: b.play }, "Play"),
    node("span", { class: "dim" }, `port build ${b.pin ? b.pin.slice(0, 12) : "unpinned"}, built ${built}`));
  const path = node("ol", { class: "enh-path" }, ...rec.critical.rows.map(r => node("li", { class: `enh-${r.state}` }, cardLink(rec, r, r.key), node("span", { class: "dim" }, ` ${r.minutes} min`))));
  const finish = node("section", { class: "card enh-finish" }, node("h3", {}, "Finish line"),
    ...rec.finish.proofs.map(item => node("div", {}, node("h4", {}, item.name, pill(item.state)), rowList(rec, item.rows))),
    node("h4", {}, "Launch gate"), rowList(rec, rec.finish.gate));
  return [nav, node("section", { class: "card" }, node("h3", {}, "Plan progress"), split, bar),
    node("section", { class: "card" }, node("h3", {}, `Critical path, ${rec.critical.minutes} min`), path),
    node("h2", {}, "MVP items"), ...rec.items.map(item => itemCard(rec, item)), finish];
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
