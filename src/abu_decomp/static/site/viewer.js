// The site's own RFB viewer: a WebSocket to /viewer/<id>/ws carrying RFB 3.8 (security None), raw rectangles on a canvas,
// pointer and key input back. Cursor (-239) and PointerPos (-232) are asked for so a server never draws the pointer
// into the frames; both are read and dropped. The display's sound (viewer_audio.js) plays from /viewer/<id>/audio once the
// page gets its first click or key (the browser's autoplay rule).
import { onFirstGesture } from "./viewer_audio.js";
const canvas = document.querySelector("#screen"), ctx = canvas.getContext("2d");
onFirstGesture(document, canvas.dataset.audio, canvas);
const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${canvas.dataset.ws}`);
socket.binaryType = "arraybuffer";
let buffer = new Uint8Array(0), waiter = null;
socket.onmessage = e => { const add = new Uint8Array(e.data), next = new Uint8Array(buffer.length + add.length);
  next.set(buffer); next.set(add, buffer.length); buffer = next; if (waiter) waiter(); };
const opened = new Promise(ok => { socket.onopen = ok; });
async function read(count) {
  while (buffer.length < count) await new Promise(ok => { waiter = ok; });
  const out = buffer.slice(0, count); buffer = buffer.slice(count); return out;
}
const view = b => new DataView(b.buffer, b.byteOffset, b.byteLength);
const send = (...parts) => socket.send(Uint8Array.from(parts.flat()));
const u16 = n => [n >> 8 & 255, n & 255], u32 = n => [n >> 24 & 255, n >> 16 & 255, n >> 8 & 255, n & 255];

let updates = 0;
async function run() {
  await opened;
  await read(12); send([...new TextEncoder().encode("RFB 003.008\n")]);
  const kinds = await read((await read(1))[0]);
  if (!kinds.includes(1)) throw new Error("the display asks for a password; the viewer speaks security None only");
  send([1]); await read(4); send([1]);
  const init = await read(4), width = view(init).getUint16(0), height = view(init).getUint16(2);
  await read(16); await read(view(await read(4)).getUint32(0));
  canvas.width = width; canvas.height = height;
  // 32 bpp, depth 24, little endian, true colour, 255/255/255, shifts 16/8/0 -> bytes B G R X
  send([0, 0, 0, 0, 32, 24, 0, 1, 0, 255, 0, 255, 0, 255, 16, 8, 0, 0, 0, 0]);
  send([2, 0, ...u16(3), ...[0, -239 >>> 0, -232 >>> 0].flatMap(u32)]);
  const request = incremental => send([3, incremental, 0, 0, 0, 0, ...u16(width), ...u16(height)]);
  request(0);
  for (;;) {
    const kind = (await read(1))[0];
    if (kind === 3) { await read(3); await read(view(await read(4)).getUint32(0)); continue; }
    if (kind !== 0) continue;  // Bell and SetColourMap carry nothing this client uses
    const rects = view(await read(3)).getUint16(1);
    for (let i = 0; i < rects; i++) {
      const h = view(await read(12)), x = h.getUint16(0), y = h.getUint16(2), w = h.getUint16(4), ht = h.getUint16(6), enc = h.getInt32(8);
      if (enc === 0) {
        const px = await read(w * ht * 4), rgba = new Uint8ClampedArray(w * ht * 4);
        for (let p = 0; p < w * ht; p++) { rgba[p * 4] = px[p * 4 + 2]; rgba[p * 4 + 1] = px[p * 4 + 1]; rgba[p * 4 + 2] = px[p * 4]; rgba[p * 4 + 3] = 255; }
        ctx.putImageData(new ImageData(rgba, w, ht), x, y);
      } else if (enc === -239) await read(w * ht * 4 + Math.ceil(w / 8) * ht);
      else if (enc !== -232) throw new Error(`rectangle encoding ${enc} was not requested`);
    }
    updates++;
    canvas.dataset.live = String(updates);
    request(updates === 1 ? 0 : 1);  // wayvnc's first update is the buffer kept since its last client left: ask again for the whole screen
  }
}
let buttons = 0;
const held = new Set();
const point = e => { const r = canvas.getBoundingClientRect();
  return [Math.round((e.clientX - r.left) * canvas.width / r.width), Math.round((e.clientY - r.top) * canvas.height / r.height)]; };
const pointer = e => { if (socket.readyState !== 1) return; const [x, y] = point(e); send([5, buttons, ...u16(x), ...u16(y)]); };
canvas.addEventListener("mousemove", pointer);
canvas.addEventListener("mousedown", e => { buttons |= 1 << e.button; pointer(e); });
canvas.addEventListener("mouseup", e => { buttons &= ~(1 << e.button); pointer(e); });
// Keys are sent by physical position (e.code) so a release always matches its press, even when Shift changed the character.
const NAMED = { Enter: 0xff0d, Backspace: 0xff08, Tab: 0xff09, Escape: 0xff1b, Space: 0x20, ArrowLeft: 0xff51, ArrowUp: 0xff52, ArrowRight: 0xff53,
  ArrowDown: 0xff54, ShiftLeft: 0xffe1, ShiftRight: 0xffe2, ControlLeft: 0xffe3, ControlRight: 0xffe4, AltLeft: 0xffe9, AltRight: 0xffea,
  Delete: 0xffff, Home: 0xff50, End: 0xff57, PageUp: 0xff55, PageDown: 0xff56 };
const symbol = e => NAMED[e.code] ?? (/^Key[A-Z]$/.test(e.code) ? e.code.charCodeAt(3) + 32 : /^Digit\d$/.test(e.code) ? e.code.charCodeAt(5)
  : /^F([1-9]|1[0-2])$/.test(e.code) ? 0xffbd + Number(e.code.slice(1)) : e.key.length === 1 ? e.key.codePointAt(0) : 0);
const sendKey = (sym, down) => { if (socket.readyState !== 1) return;
  down ? held.add(sym) : held.delete(sym); send([4, down, 0, 0, ...u32(sym)]); };
const key = (e, down) => { e.preventDefault();
  const sym = symbol(e);
  if (sym && !e.repeat) sendKey(sym, down); };
canvas.addEventListener("keydown", e => key(e, 1));
canvas.addEventListener("keyup", e => key(e, 0));
canvas.addEventListener("blur", () => { for (const sym of [...held]) sendKey(sym, 0); padHeld.clear(); });
canvas.focus();

// The first connected gamepad becomes keys on the same RFB path, so the game sees a keyboard and a release is sent like a press.
// Standard-mapping buttons: A x, B c, X s, Y d, LB q, RB e, LT z, RT r, Back Tab, Start Return, d-pad and left stick the arrows.
const PAD_KEYS = { 0: 0x78, 1: 0x63, 2: 0x73, 3: 0x64, 4: 0x71, 5: 0x65, 6: 0x7a, 7: 0x72, 8: 0xff09, 9: 0xff0d, 12: 0xff52, 13: 0xff54, 14: 0xff51, 15: 0xff53 };
const padHeld = new Set();
function padLoop() {
  const g = [...(navigator.getGamepads?.() ?? [])].find(Boolean), want = new Set();
  if (g) {
    g.buttons.forEach((b, n) => { if (b.pressed && PAD_KEYS[n]) want.add(PAD_KEYS[n]); });
    const [x, y] = [g.axes[0] ?? 0, g.axes[1] ?? 0];
    if (x < -0.5) want.add(0xff51); if (x > 0.5) want.add(0xff53); if (y < -0.5) want.add(0xff52); if (y > 0.5) want.add(0xff54);
  }
  for (const sym of padHeld) if (!want.has(sym)) { padHeld.delete(sym); sendKey(sym, 0); }
  for (const sym of want) if (!padHeld.has(sym)) { padHeld.add(sym); sendKey(sym, 1); }
  requestAnimationFrame(padLoop);
}
padLoop();
run().catch(err => { canvas.dataset.error = String(err); document.body.append(Object.assign(document.createElement("pre"), { textContent: String(err), style: "color:#f88" })); });
