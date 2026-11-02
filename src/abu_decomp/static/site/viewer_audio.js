// The viewer's sound: GET /viewer/<id>/audio is one growing WAV (44-byte header, then s16le stereo) of the display's sound
// from the moment it is asked. Browsers start audio only after a user gesture, so the stream opens on the first click or key
// on the page; each chunk is scheduled right behind the last one, so the sound keeps pace with the picture. The bytes and the
// loudest sample so far are written to the target's dataset (audioBytes, audioPeak) for the page's own proof.
export const LEAD_SECONDS = 0.05;  // a chunk that arrives late plays this far ahead of now instead of in the past

export function decode(bytes, carry) {
  // s16le stereo frames out of the byte stream; a partial frame is carried to the next chunk
  const all = new Uint8Array(carry.length + bytes.length); all.set(carry); all.set(bytes, carry.length);
  const frames = Math.floor(all.length / 4), view = new DataView(all.buffer, 0, frames * 4);
  const left = new Float32Array(frames), right = new Float32Array(frames);
  let peak = 0;
  for (let i = 0; i < frames; i++) {
    const l = view.getInt16(i * 4, true), r = view.getInt16(i * 4 + 2, true);
    left[i] = l / 32768; right[i] = r / 32768; peak = Math.max(peak, Math.abs(l), Math.abs(r));
  }
  return { left, right, peak, carry: all.slice(frames * 4) };
}

export async function play(url, target, { fetch = globalThis.fetch, AudioContext = globalThis.AudioContext } = {}) {
  const ctx = new AudioContext(), reply = await fetch(url, { cache: "no-store" });
  if (!reply.ok || !reply.body) throw new Error(`the display's sound answered ${reply.status}`);
  target.dataset.audioBytes = "0";  // the stream is open
  const reader = reply.body.getReader();
  let header = new Uint8Array(0), carry = new Uint8Array(0), rate = 0, next = 0, bytes = 0, peak = 0, lead = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) { target.dataset.audioError = "the display's sound ended"; return { bytes, peak }; }
    let chunk = value;
    if (!rate) {  // the WAV header: the sample rate is at byte 24
      const joined = new Uint8Array(header.length + chunk.length); joined.set(header); joined.set(chunk, header.length);
      if (joined.length < 44) { header = joined; continue; }
      rate = new DataView(joined.buffer).getUint32(24, true); chunk = joined.slice(44);
    }
    const out = decode(chunk, carry); carry = out.carry;
    bytes += chunk.length; peak = Math.max(peak, out.peak);
    target.dataset.audioBytes = String(bytes); target.dataset.audioPeak = String(peak);
    if (!out.left.length) continue;
    const buffer = ctx.createBuffer(2, out.left.length, rate);
    buffer.copyToChannel(out.left, 0); buffer.copyToChannel(out.right, 1);
    const source = ctx.createBufferSource(); source.buffer = buffer; source.connect(ctx.destination);
    next = Math.max(next, ctx.currentTime + LEAD_SECONDS); source.start(next); next += buffer.duration;
    // the sync with the picture, which is drawn as it arrives: the most this sound has played behind its arrival
    lead = Math.max(lead, Math.round((next - ctx.currentTime) * 1000)); target.dataset.audioLeadMs = String(lead);
  }
}

export function onFirstGesture(element, url, target, seams) {
  // the browser's autoplay rule: the sound starts on the first pointer press or key, once
  let started = false;
  const start = () => {
    if (started) return; started = true;
    play(url, target, seams).catch(err => { target.dataset.audioError = String(err); });
  };
  for (const kind of ["pointerdown", "keydown"]) element.addEventListener(kind, start);
  return start;
}
