"""Rig proof of the live view: a replay stand-in (foot) runs inside `decomp display invoke`, and headless chromium
opens the
site's own viewer page, served by the viewer module's handler, and screenshots what the page drew. Run from the repo
root
as `python3 -u ci/live_proof.py WORKDIR`; prints each step as it starts, then `invoke:`, `capture:`, `viewer:` and
`delete:` result lines. Every wait is bounded by a declared value below and fails by its name."""

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

from PIL import Image

UP_SECONDS = 60  # DISPLAY_UP: invoke prints the display line once labwc and wayvnc answer
CHROMIUM_SECONDS = 90  # CHROMIUM: headless chromium screenshots the viewer page
DELETE_SECONDS = 60  # DELETE: invoke ends and removes the display after the replay stand-in exits


def step(text):
    print("step:", text, flush=True)


def fail(name, detail):
    print(f"FAIL {name}: {detail}", flush=True)
    sys.exit(1)


repo = Path.cwd()
sys.path.insert(0, str(repo / "src"))
from abu_decomp import display, viewer  # noqa: E402

work = Path(sys.argv[1]).resolve()
displays = work / "displays"
stop = work / "stop.fifo"  # the stand-in blocks reading it; closing the write end is the signal that ends the replay
stop.unlink(missing_ok=True)
os.mkfifo(stop)
cmd = [
    sys.executable,
    "-m",
    "abu_decomp",
    "display",
    "--dir",
    str(displays),
    "invoke",
    "--",
    "foot",
    "-e",
    "sh",
    "-c",
    f"echo replay; cat {stop}",
]
step("invoke: starting the display and the replay stand-in")
run = subprocess.Popen(cmd, cwd=repo / "src", stderr=subprocess.PIPE, text=True)
timer = threading.Timer(UP_SECONDS, run.kill)  # a silent invoke closes its stderr, so the readline below returns empty
timer.start()
first = run.stderr.readline()
timer.cancel()
if not first:
    fail("DISPLAY_UP", f"invoke printed no display line within {UP_SECONDS}s (exit {run.poll()})")
ident = json.loads(first)["display"]
print("invoke: display", ident, flush=True)


reported = threading.Event()  # the page's script says it drew a frame (or failed): the signal to take the screenshot


def cdp_screenshot(port, url, path):
    """Open url in the running chromium and, once the page reported, save a PNG of it over the DevTools WebSocket."""
    request = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?{url}", method="PUT")
    target = json.load(urllib.request.urlopen(request, timeout=CHROMIUM_SECONDS))
    sock = socket.create_connection(("127.0.0.1", port), timeout=CHROMIUM_SECONDS)
    route = target["webSocketDebuggerUrl"].split(str(port), 1)[1]
    key = base64.b64encode(os.urandom(16)).decode()
    handshake = (
        f"GET {route} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(handshake.encode())
    reader = sock.makefile("rb")
    while reader.readline() not in (b"\r\n", b""):
        pass
    if not reported.wait(CHROMIUM_SECONDS):
        fail("CHROMIUM", f"the page reported nothing within {CHROMIUM_SECONDS}s")
    payload = json.dumps({"id": 1, "method": "Page.captureScreenshot"}).encode()
    mask = os.urandom(4)
    head = bytes([0x81, 0x80 | 126]) + struct.pack(">H", len(payload))
    sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
    while True:
        second = reader.read(2)[1]
        size = second & 127
        if size == 126:
            size = struct.unpack(">H", reader.read(2))[0]
        elif size == 127:
            size = struct.unpack(">Q", reader.read(8))[0]
        message = json.loads(reader.read(size))
        if message.get("id") == 1:
            path.write_bytes(base64.b64decode(message["result"]["data"]))
            return


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def do_POST(self):  # the page's own report of what its script did, printed for the proof log
        print("viewer: page report", self.rfile.read(int(self.headers["Content-Length"])).decode(), flush=True)
        reported.set()
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        if self.path.endswith("/ws"):
            self.close_connection = True
            record = display.load(displays, ident)
            viewer.serve_socket(self.connection, self.headers["Sec-WebSocket-Key"], record, display.connect_address)
        elif self.path.startswith("/static/site/"):
            body = (repo / "src/abu_decomp/static/site" / self.path.rsplit("/", 1)[1]).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            probe = (
                "<script>const c=document.querySelector('#screen');"
                "new MutationObserver(()=>(Number(c.dataset.live)>=2||c.dataset.error)"
                "&&fetch('/report',{method:'POST',body:JSON.stringify("
                "{live:c.dataset.live,error:c.dataset.error,width:c.width})}))"
                ".observe(c,{attributes:true})</script>"
            )
            body = (viewer.viewer_page(display.load(displays, ident)) + probe).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
http.daemon_threads = True
threading.Thread(target=http.serve_forever, daemon=True).start()
frame = work / "frame.png"
step("capture: one RFB frame")
print("capture:", display.backend(displays).capture(ident, frame), flush=True)
shot = work / "page.png"
step("viewer: headless chromium opens the page")
browser = subprocess.Popen(
    [
        "chromium",
        "--headless",
        "--no-sandbox",
        "--disable-gpu",
        f"--user-data-dir={work}/chromium",
        "--window-size=1280,720",
        "--remote-debugging-port=9333",
        "about:blank",
    ],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.PIPE,
    text=True,
)
timer = threading.Timer(CHROMIUM_SECONDS, browser.kill)
timer.start()
for line in browser.stderr:  # chromium announces the DevTools port once it listens
    if "DevTools listening" in line:
        break
else:
    fail("CHROMIUM", f"no DevTools port within {CHROMIUM_SECONDS}s")
timer.cancel()
try:
    cdp_screenshot(9333, f"http://127.0.0.1:{http.server_port}/viewer/{ident}", shot)
except OSError as exc:
    fail("CHROMIUM", f"DevTools unreachable: {exc}")
finally:
    browser.kill()
drawn = Image.open(shot).convert("RGB").getbbox() is not None if shot.exists() else False
colours = len(Image.open(shot).convert("RGB").getcolors(1 << 24) or []) if shot.exists() else 0
print(f"viewer: page_screenshot={shot.name} drawn={drawn} colours={colours}", flush=True)
step("delete: closing the replay stand-in")
os.close(os.open(stop, os.O_WRONLY))  # `cat` ends, the replay stand-in exits and invoke deletes the display
try:
    code = run.wait(timeout=DELETE_SECONDS)
except subprocess.TimeoutExpired:
    run.kill()
    fail("DELETE", f"invoke still running {DELETE_SECONDS}s after the replay stand-in ended")
left = (displays / ident).exists()
print("delete:", "left folder" if left else "clean", "exit", code, flush=True)
sys.exit(1 if left or code or not drawn or colours < 2 else 0)
