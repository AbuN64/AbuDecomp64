# ruff: noqa: E501  (HARNESS is one JavaScript module kept readable as written)
"""The viewer's audio client (static/site/viewer_audio.js) under node, with fetch, AudioContext and the page mocked."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "src" / "abu_decomp" / "static" / "site" / "viewer_audio.js"

HARNESS = """
import * as audio from "%s";
const header = new Uint8Array(44); new DataView(header.buffer).setUint32(24, 48000, true);
const pcm = new Uint8Array(new Int16Array([0, 0, 1200, -900, 300]).buffer);  // two frames and half of a third
const chunks = [header.slice(0, 20), header.slice(20), pcm, new Uint8Array([7, 0, 9])];  // the third frame completes
const fetched = [], started = [];
const fetch = async (url) => { fetched.push(url); let i = 0;
  return { ok: true, body: { getReader: () => ({ read: async () => i < chunks.length ? { value: chunks[i++] } : { done: true } }) } }; };
class AudioContext { constructor() { this.currentTime = 1; this.destination = {}; }
  createBuffer(ch, n, rate) { const data = [new Float32Array(n), new Float32Array(n)];
    return { duration: n / rate, length: n, data, copyToChannel(d, c) { data[c].set(d); } }; }
  createBufferSource() { return { connect() {}, start(at) { started.push([at, this.buffer.length, Array.from(this.buffer.data[0])]); } }; } }
const listeners = {}, element = { addEventListener: (k, f) => { listeners[k] = f; } }, target = { dataset: {} };
audio.onFirstGesture(element, "/viewer/d-aa/audio", target, { fetch, AudioContext });
const before = fetched.length;
listeners.pointerdown(); listeners.keydown();
await new Promise(ok => setTimeout(ok, 50));
process.stdout.write(JSON.stringify({ before, fetched, started, dataset: target.dataset, gestures: Object.keys(listeners).sort() }));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node runs the page module")
def test_audio_starts_on_the_first_gesture_and_schedules_the_stream_back_to_back() -> None:
    done = subprocess.run(
        ["node", "--input-type=module", "-e", HARNESS % MODULE.as_uri()], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert got["before"] == 0 and got["fetched"] == ["/viewer/d-aa/audio"]  # nothing before a gesture, once after two
    assert got["gestures"] == ["keydown", "pointerdown"]
    assert got["dataset"] == {
        "audioBytes": "13",
        "audioPeak": "1200",
        "audioLeadMs": "50",  # LEAD_SECONDS plus the chunks queued behind it
        "audioError": "the display's sound ended",
    }  # header bytes are not sound
    (at1, n1, left1), (at2, n2, left2) = got["started"]
    assert (n1, n2) == (2, 1) and left1[1] == pytest.approx(1200 / 32768) and left2[0] == pytest.approx(300 / 32768)
    assert at1 == pytest.approx(1.05) and at2 == pytest.approx(
        at1 + 2 / 48000
    )  # the second chunk right behind the first
