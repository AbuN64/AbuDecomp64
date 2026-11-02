// The home wall: every playthrough as a tile, three columns (one at phone width), rows growing as pages load from
// /api/wall when the sentinel below the last row comes into view. Only tiles on screen stream: a visible tile shows its
// run's newest frame and moves on each feed event of that run (the feed's signal, never a clock); a tile off screen
// keeps the last frame it showed. The wall's data-streams and data-visible attributes are the measured counts.
export const PAGE = 12;

/** Merge a wall page into the tile order: ids already shown keep their place, new ids append. */
export function merge(order, tiles) {
  const seen = new Set(order);
  return order.concat(tiles.map(t => t.id).filter(id => !seen.has(id)));
}

/** The frame a tile shows: the newest recorded poll of its stream index, else null (no picture yet). */
export function frameUrl(run, index) {
  const polls = (index && index.polls) || [];
  return polls.length ? `/still/${encodeURIComponent(run)}?side=port&poll=${polls[polls.length - 1]}` : null;
}

export function mount(el, ctx) {
  const host = el, doc = ctx.doc || document, Observer = ctx.Observer || globalThis.IntersectionObserver; // tests pass both
  const grid = doc.createElement("div"), more = doc.createElement("div");
  grid.className = "wall"; grid.id = "wall"; more.className = "wall-more";
  host.replaceChildren(grid, more);
  const tiles = new Map(), visible = new Set();
  let order = [], next = 0, loading = false;
  const count = () => { grid.dataset.visible = visible.size; grid.dataset.streams = [...visible].filter(id => tiles.get(id).streaming).length; };

  // A tile streams only once its frame has loaded on screen: the count measures pictures shown, not requests made.
  async function paint(id) {
    const tile = tiles.get(id);
    const index = await ctx.api(`/api/stream/${encodeURIComponent(id)}`).catch(() => null);
    const url = frameUrl(id, index);
    if (url && visible.has(id) && tile.img.src !== url) tile.img.src = url;
    tile.node.dataset.still = url ? "" : "empty";
  }
  const seen = new Observer(entries => {
    for (const e of entries) {
      const id = e.target.dataset.run;
      if (e.isIntersecting) { visible.add(id); tiles.get(id).streaming = Boolean(tiles.get(id).img.naturalWidth); paint(id); }
      else { visible.delete(id); tiles.get(id).streaming = false; }
    }
    count();
  });
  function tile(t) {
    const node = doc.createElement("a"), img = doc.createElement("img"), cap = doc.createElement("span");
    node.className = "tile"; node.href = `#/stream/${encodeURIComponent(t.id)}`; node.dataset.run = t.id;
    img.alt = `${t.movie} by ${t.lane}`; img.decoding = "async";
    const shown = ok => { const it = tiles.get(t.id); it.streaming = ok && visible.has(t.id); if (!ok) node.dataset.still = "empty"; count(); };
    img.onload = () => shown(true); img.onerror = () => shown(false);
    node.append(img, cap);
    tiles.set(t.id, { node, img, cap, streaming: false });
    seen.observe(node);
    return node;
  }
  function label(t) {
    const it = tiles.get(t.id);
    it.cap.textContent = `${t.lane} · ${t.movie} · ${t.status === "running" ? `poll ${t.poll.toLocaleString("en-US")}` : t.status}`;
    it.node.dataset.status = t.status;
  }
  async function load() {
    if (loading || next === null) return;
    loading = true;
    const page = await ctx.api(`/api/wall?offset=${next}&limit=${PAGE}`).catch(() => null);
    loading = false;
    if (!page || !Array.isArray(page.tiles)) return void (more.textContent = "The wall could not load.");
    const fresh = page.tiles.filter(t => !tiles.has(t.id));
    if (page.offset === 0 && order.length) grid.prepend(...fresh.map(tile)); // runs started since: on top
    else grid.append(...fresh.map(tile));
    page.tiles.forEach(label);
    order = merge(order, page.tiles);
    if (page.offset !== 0 || next === 0) next = page.next;
    more.textContent = page.total ? "" : "No playthroughs yet.";
    grid.dataset.tiles = order.length;
  }
  new Observer(entries => { if (entries.some(e => e.isIntersecting)) load(); }).observe(more);
  // A run event moves its tile when on screen; a run the wall has not shown yet starts the wall over from the top.
  const onRun = ev => {
    const id = ev.fields && ev.fields.run;
    if (!id || !grid.isConnected) return;
    if (!tiles.has(id)) { next = 0; return void load(); }
    label({ id, lane: ev.lane, movie: ev.fields.movie, status: ev.kind === "run.ended" ? "done" : "running", poll: ev.fields.poll || 0 });
    if (visible.has(id)) paint(id);
  };
  for (const kind of ["run.started:tas", "run.started:tas-target", "run.started:unit", "run.started:set", "run.ended"]) ctx.on(kind, onRun);
  return load();
}
