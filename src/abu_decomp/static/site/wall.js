// The home wall: every playthrough as a tile, three columns (one at phone width), rows growing as pages load from
// /api/wall when the sentinel below the last row comes into view. The pictures are the server's one /wall stream (a
// thumb of every running run, each frame naming its run by index): a visible tile shows the newest thumb it was sent,
// a tile off screen keeps the last one. The stream is reopened by a run event when it has ended (the feed's signal,
// never a clock). The wall's data-streams and data-visible attributes are the measured counts.
export const PAGE = 12;

/** Merge a wall page into the tile order: ids already shown keep their place, new ids append. */
export function merge(order, tiles) {
  const seen = new Set(order);
  return order.concat(tiles.map(t => t.id).filter(id => !seen.has(id)));
}

/** /wall framing: u32 BE length (top bit set: a run table or a reach record), u64 BE stamp, u32 BE run index, then the
 * body. A flagged body starting "{" names the index ({"id": run}); other flagged bodies are reach records, not read here. */
export function wallFramer(onTable, onThumb) {
  let buf = new Uint8Array(0);
  return chunk => {
    const joined = new Uint8Array(buf.length + chunk.length); joined.set(buf); joined.set(chunk, buf.length); buf = joined;
    let o = 0;
    while (buf.length - o >= 16) {
      const view = new DataView(buf.buffer, buf.byteOffset + o, 16), raw = view.getUint32(0), n = raw & 0x7fffffff;
      if (buf.length - o < 16 + n) break;
      const body = buf.slice(o + 16, o + 16 + n), index = view.getUint32(12);
      if (!(raw & 0x80000000)) onThumb(index, body);
      else if (body[0] === 0x7b) onTable(index, JSON.parse(new TextDecoder().decode(body)).id);
      o += 16 + n;
    }
    buf = buf.slice(o);
  };
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
  function paint(id) {
    const tile = tiles.get(id);
    if (tile.url && visible.has(id) && tile.img.src !== tile.url) tile.img.src = tile.url;
    tile.node.dataset.still = tile.url ? "" : "empty";
  }
  // The one /wall stream: a thumb of every running run. Reads until the server ends it; `open` starts it again.
  const names = new Map(), latest = new Map(); // stream index -> run; run -> the newest thumb's object URL
  let reading = false;
  async function open() {
    if (reading || !grid.isConnected) return;
    reading = true; names.clear();
    const take = wallFramer((index, id) => names.set(index, id), (index, jpeg) => {
      const id = names.get(index), blobs = ctx.URL || URL;
      if (latest.has(id)) blobs.revokeObjectURL(latest.get(id));
      latest.set(id, blobs.createObjectURL(new Blob([jpeg], { type: "image/jpeg" })));
      if (tiles.has(id)) { tiles.get(id).url = latest.get(id); paint(id); }
    });
    try {
      const res = await (ctx.fetch || fetch)("/wall", { cache: "no-store" });
      const reader = res.body.getReader();
      for (;;) { const { done, value } = await reader.read(); if (done) break; take(value); }
    } catch { /* the stream ended: a run event opens it again */ }
    reading = false;
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
    tiles.set(t.id, { node, img, cap, streaming: false, url: latest.get(t.id) || null });
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
    open();
    if (!tiles.has(id)) { next = 0; return void load(); }
    label({ id, lane: ev.lane, movie: ev.fields.movie, status: ev.kind === "run.ended" ? "done" : "running", poll: ev.fields.poll || 0 });
  };
  for (const kind of ["run.started:tas", "run.started:tas-target", "run.started:unit", "run.started:set", "run.ended"]) ctx.on(kind, onRun);
  open();
  return load();
}
