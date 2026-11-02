"""Rig proof that the viewer plays the game's sound: the game (the ragewars binary, SDL) runs inside
`decomp display invoke`,
whose display carries its own sound server; headless chromium opens the site's viewer page, gets one real click (the
autoplay rule's gesture) and the page's audio client (viewer_audio.js) fetches /viewer/<id>/audio. The page reports the
bytes it received and the loudest sample once it heard sound or a bound of sound passed. Run from the repo root as
`python3 -u ci/display_audio_proof.py WORKDIR RAGEWARS ROM`; prints `audio: ok bytes=N peak=N` or a FAIL line naming the
step. Every wait is a signal (the display line, the page's dataset) bounded by a declared value."""

import base64
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UP_SECONDS = 60  # DISPLAY_UP: invoke prints the display line once the compositor, sound server and VNC server answer
CHROMIUM_SECONDS = 30  # CHROMIUM: the DevTools port answers
HEAR_SECONDS = 150  # HEAR: the page reports sound (a non-zero sample) or this much sound arrived silent
AUDIBLE_PEAK = 1000  # HEAR: a sample this loud is the game's music, not the first edge of a fade-in (title RMS 1530+)
SILENT_BYTES = 48000 * 4 * 90  # the page gives up after 90 s of silent sound: the game is silent until its title
DELETE_SECONDS = 30  # DELETE: invoke ends and removes the display after the game is stopped


def step(text):
    print("step:", text, flush=True)


def fail(name, detail):
    print(f"FAIL {name}: {detail}", flush=True)
    os._exit(1)


repo = Path.cwd()
sys.path.insert(0, str(repo / "src"))
from abu_decomp import display, viewer  # noqa: E402
from abu_decomp.tas_serve import wav_header  # noqa: E402

CURL_SECONDS = 5  # --curl: seconds of sound read straight from the route after its first non-zero sample, no browser
CURL_SILENT_BYTES = 48000 * 4 * 90  # --curl gives up after 90 s of silent sound
curl = "--curl" in sys.argv[4:]
# --tone: a 440 Hz tone played into the display's sink beside the game (pacat, the same Pulse server the game plays to),
# so the capture -> route -> page chain is proven with known sound when the game itself is silent
tone = "--tone" in sys.argv[4:]  # name the broken link: the capture or the route, with no page in between
work, game, rom = Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3]
(work / "user").mkdir(parents=True, exist_ok=True)  # the port formats the Controller Pak file there
displays = work / "displays"
game_log = work / "game.log"  # the game's own output, shown when the page heard nothing
command = (
    f"exec env SDL_VIDEODRIVER=wayland {game} --rom {rom} --user-dir {work}/user --no-dialog "
    f"--cpak {work}/user/controller-pak.mpk "
    f"--window 640x480 > {game_log} 2>&1"
)
step("invoke: starting the display and the game")
run = subprocess.Popen(
    [sys.executable, "-m", "abu_decomp", "display", "--dir", str(displays), "invoke", "--", "sh", "-c", command],
    cwd=repo / "src",
    stderr=subprocess.PIPE,
    text=True,
)
timer = threading.Timer(UP_SECONDS, run.kill)
timer.start()
first = run.stderr.readline()
timer.cancel()
if not first:
    fail("DISPLAY_UP", f"invoke printed no display line within {UP_SECONDS}s (exit {run.poll()})")
ident = json.loads(first)["display"]
print("invoke: display", ident, flush=True)
backend = display.backend(displays)
record = backend.describe(ident)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def do_GET(self):
        if self.path.endswith("/ws"):
            self.close_connection = True
            viewer.serve_socket(self.connection, self.headers["Sec-WebSocket-Key"], record, display.connect_address)
        elif self.path.endswith("/audio"):  # the site's route: the adapter's capture sent by viewer.stream_audio
            print("audio: the page asked for the sound", flush=True)
            capture = backend.audio(ident)
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            viewer.stream_audio(self.wfile, capture, wav_header(display.AUDIO_RATE))
        elif self.path.startswith("/static/site/"):
            body = (repo / "src/abu_decomp/static/site" / self.path.rsplit("/", 1)[1]).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:  # /live embeds the viewer page as the site does; any other path is the viewer page itself
            page = viewer.live_page([record]) if self.path == "/live" else viewer.viewer_page(record)
            body = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


SCREEN = "document.querySelector('iframe')||document.querySelector('#screen')"  # what the click lands on
# the canvas whose dataset the audio client writes, inside /live's embedded viewer or on the viewer page itself
CANVAS = "(document.querySelector('iframe')?.contentDocument||document).querySelector('#screen')"


def cdp_click(port, url):
    """Open url in the running chromium and click the canvas once: a real user gesture for the autoplay rule."""
    # a blank tab first: a tab opened on url answers Runtime calls in its initial about:blank, so a click sent then
    # lands before the page exists; navigating it ourselves gives the page's own load event as the signal
    request = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT")
    target = json.load(urllib.request.urlopen(request, timeout=CHROMIUM_SECONDS))
    sock = socket.create_connection(("127.0.0.1", port), timeout=HEAR_SECONDS)  # each DevTools answer, the hearing too
    route = target["webSocketDebuggerUrl"].split(str(port), 1)[1]
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        f"GET {route} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
    )
    reader = sock.makefile("rb")
    while reader.readline() not in (b"\r\n", b""):
        pass

    def wait(match):  # the next message match accepts (the events and answers in between are read and dropped)
        while True:
            size = reader.read(2)[1] & 127
            if size == 126:
                size = struct.unpack(">H", reader.read(2))[0]
            elif size == 127:
                size = struct.unpack(">Q", reader.read(8))[0]
            message = json.loads(reader.read(size))
            if match(message):
                return message

    def call(ident, method, params):
        payload = json.dumps({"id": ident, "method": method, "params": params}).encode()
        mask = os.urandom(4)
        head = bytes([0x81, 0x80 | 126]) + struct.pack(">H", len(payload))
        sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        return wait(lambda m: m.get("id") == ident)

    call(1, "Page.enable", {})
    call(2, "Page.navigate", {"url": url})
    wait(lambda m: m.get("method") == "Page.loadEventFired")  # the viewer page itself loaded, its scripts ran
    expression = f"JSON.stringify(({SCREEN}).getBoundingClientRect())"
    answer = call(3, "Runtime.evaluate", {"expression": expression, "returnByValue": True})
    box = json.loads(answer["result"]["result"]["value"])  # the embedded viewer's frame on /live, the canvas otherwise
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    for n, kind in enumerate(("mousePressed", "mouseReleased")):
        call(4 + n, "Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1})
    return call


http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
http.daemon_threads = True
threading.Thread(target=http.serve_forever, daemon=True).start()

if tone:
    import math

    pacat = subprocess.Popen(
        ["pacat", "--server=unix:" + str(Path(record.directory) / "run/pulse/native"), "--device=" + display.SINK,
         "--raw", "--format=s16le", "--channels=2", f"--rate={display.AUDIO_RATE}"],
        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    second = b"".join(
        struct.pack("<hh", v, v) for v in (int(8000 * math.sin(2 * math.pi * 440 * i / 48000)) for i in range(48000))
    )

    def feed():  # pacat paces the writes at the sink's rate; it ends when the display goes
        try:
            while True:
                pacat.stdin.write(second)
        except OSError:
            pass

    threading.Thread(target=feed, daemon=True).start()
    print("tone: 440 Hz peak 8000 into the display's sink beside the game", flush=True)


def evidence():
    sinks = subprocess.run(
        ["pactl", "--server=unix:" + str(Path(record.directory) / "run/pulse/native"), "list", "short", "sink-inputs"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    print("sink-inputs:", sinks.stdout.strip().replace("\n", " | ") or "(none)", sinks.stderr.strip(), flush=True)
    for kind in ("sinks", "sink-inputs"):  # the state that silences a stream: corked, muted, volume, which sink
        long = subprocess.run(
            ["pactl", "--server=unix:" + str(Path(record.directory) / "run/pulse/native"), "list", kind],
            capture_output=True, text=True, check=False,
        ).stdout  # fmt: skip
        keep = (
            "Sink #",
            "Sink Input #",
            "Name:",
            "Sink:",
            "Corked",
            "Mute",
            "Volume:",
            "State",
            "Sample",
            "application.name",
        )
        print(
            f"{kind}:", " | ".join(x.strip() for x in long.splitlines() if x.strip().startswith(keep))[:900], flush=True
        )
    parec_log = Path(record.directory) / "parec.log"
    print(
        "parec:", parec_log.read_text(errors="replace")[-800:] if parec_log.exists() else "(no parec log)", flush=True
    )


def stop():
    step("delete: stop the game and the display")
    subprocess.run(["pkill", "-f", "--", f"--user-dir {work}/user"], check=False)
    try:
        run.wait(DELETE_SECONDS)
    except subprocess.TimeoutExpired:
        fail("DELETE", f"invoke still running {DELETE_SECONDS}s after the game was stopped")


if curl:
    step(f"curl: GET /viewer/{ident}/audio for {CURL_SECONDS}s of sound")
    got, first = b"", -1
    try:
        reply = urllib.request.urlopen(
            f"http://127.0.0.1:{http.server_port}/viewer/{ident}/audio", timeout=HEAR_SECONDS
        )
        while chunk := reply.read1(65536):
            if first < 0 and any(chunk[max(0, 44 - len(got)) :]):  # past the header, a sound that is not silence
                first = len(got) + max(0, 44 - len(got))
            got += chunk
            if (first < 0 and len(got) >= 44 + CURL_SILENT_BYTES) or (
                first >= 0 and len(got) >= first + 48000 * 4 * CURL_SECONDS
            ):
                break
    except OSError as exc:
        print("curl: error", exc, flush=True)
    print(f"curl: first non-zero byte at {first} ({max(first - 44, 0) / 192000:.2f}s of sound)", flush=True)
    got = got[:44] + got[max(first, 44) :]
    samples = (
        struct.unpack(f"<{(len(got) - 44) // 2}h", got[44 : 44 + (len(got) - 44) // 2 * 2]) if len(got) > 44 else ()
    )
    rms = (sum(v * v for v in samples) / len(samples)) ** 0.5 if samples else 0
    peak = max((abs(v) for v in samples), default=0)
    print(f"curl: bytes={len(got)} header={got[:44].hex()} rms={rms:.1f} peak={peak}", flush=True)
    evidence()
    stop()
    if peak > 0:
        print(f"audio: ok bytes={len(got) - 44} peak={peak}", flush=True)
        sys.exit(0)
    fail(
        "CURL",
        "the route gave no sound; game said: "
        + (game_log.read_text(errors="replace")[-600:] if game_log.exists() else "(no game log)"),
    )
step("viewer: headless chromium opens the page and clicks it")
browser = subprocess.Popen(
    ["chromium", "--headless", "--no-sandbox", "--disable-gpu", f"--user-data-dir={work}/chromium",
     "--window-size=1280,720", "--remote-debugging-port=9334", "about:blank"],
    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
)  # fmt: skip
for line in browser.stderr:  # chromium announces the DevTools port once it listens
    if "DevTools listening" in line:
        break
else:
    fail("CHROMIUM", f"chromium exited {browser.poll()} before its DevTools port listened")
held = cdp_click(9334, f"http://127.0.0.1:{http.server_port}/live")  # the owner's page: one viewer per display
step("hear: the page's audio client receives the display's sound")
# the page's audio client writes its counters to the canvas's dataset; this promise resolves on the first sound it
# hears, on SILENT_BYTES of silence or on its error: the page's own signal, bounded by HEAR_SECONDS on the socket
hear = (
    f"new Promise(ok=>{{const c={CANVAS},d=c.dataset,done=()=>Number(d.audioPeak||0)>={AUDIBLE_PEAK}||"
    f"Number(d.audioBytes||0)>={SILENT_BYTES}||d.audioError;if(done())ok(1);"
    "else new MutationObserver((_,o)=>{if(done()){o.disconnect();ok(1)}}).observe(c,{attributes:true})})"
)
try:
    heard = bool(held(8, "Runtime.evaluate", {"expression": hear, "awaitPromise": True}))
except OSError:  # the socket's HEAR_SECONDS bound passed with no signal from the page
    heard = False
seen = (
    held(9, "Runtime.evaluate", {"expression": f"JSON.stringify(({CANVAS}).dataset)",
                                 "returnByValue": True}).get("result", {}).get("result", {}).get("value")
    if heard else None
)  # fmt: skip
data = json.loads(seen or "{}")
report = {
    "bytes": int(data.get("audioBytes", 0)),
    "peak": int(data.get("audioPeak", 0)),
    "error": data.get("audioError", ""),
    "lead_ms": int(data.get("audioLeadMs", 0)),  # the sound's delay behind the picture, drawn on arrival
}
print("dataset:", seen, flush=True)
print("page:", json.dumps(report), flush=True)
evidence()
browser.kill()
stop()
if not heard:
    said = game_log.read_text(errors="replace")[-1500:] if game_log.exists() else "(no game log)"
    fail("HEAR", f"the page reported nothing within {HEAR_SECONDS}s; invoke exit {run.returncode}; game said: {said}")
if report.get("bytes", 0) > 0 and report.get("peak", 0) >= AUDIBLE_PEAK:
    print(f"audio: ok bytes={report['bytes']} peak={report['peak']} lead_ms={report['lead_ms']}", flush=True)
else:
    fail("SILENT", f"the page received {report.get('bytes', 0)} bytes, peak {report.get('peak', 0)}: {report}")
