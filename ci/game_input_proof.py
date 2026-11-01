"""Rig proof that the runner page's keyboard and gamepad reach a real SDL game window, press and release.

The game (the ragewars binary, SDL on Wayland) runs inside `decomp display invoke` with WAYLAND_DEBUG=1, so its own
stderr names every wl_keyboard.key event the compositor delivered to it (evdev code, 1 press / 0 release). Headless
chromium opens the site's viewer page with a fake browser gamepad, sends real key events and moves the fake pad; the
page forwards both over RFB. Run from the repo root as
`python3 -u ci/game_input_proof.py WORKDIR RAGEWARS ROM`; prints `input keyboard rc=N`, `input gamepad rc=N` and
`input cli rc=N` lines. Every wait is a signal (a game stderr line, the page's report) bounded by a declared value.
"""

import base64
import faulthandler
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOTAL_SECONDS = (
    170  # STALL: the whole proof; past it the step that was running is named and every thread's stack dumped
)
UP_SECONDS = 45  # GAME_UP: invoke prints the display line, then the game opens its window and takes keyboard focus
EVENT_SECONDS = 10  # EVENT: one delivered key event shows in the game's WAYLAND_DEBUG stream
CHROMIUM_SECONDS = 30  # CHROMIUM: the DevTools port and the page's first frames
DELETE_SECONDS = 15  # DELETE: invoke ends and removes the display after the game is stopped

EVDEV = {"ArrowRight": 106, "ArrowLeft": 105, "x": 45}  # the keys the proof sends, as the game's wl_keyboard names them


PAD_SCRIPT = (
    "window.__pad={b:[],a:[0,0]};window.__polls=0;window.__rfb=[];"
    "navigator.getGamepads=()=>{window.__polls++;return [{buttons:Array.from({length:17},"
    "(_,i)=>({pressed:window.__pad.b.includes(i)})),axes:window.__pad.a}]};"
    "const send=WebSocket.prototype.send;"
    "WebSocket.prototype.send=function(d){if(d[0]===4)window.__rfb.push(Array.from(d).join());"
    "return send.call(this,d)};'installed'"
)

current = ["start"]  # the step now running, named by the watchdog and by every refusal
children = []  # the display runner and the browser, stopped on any refusal
pages = []  # the DevTools session, read by page_state when a key never reaches the game


def step(text):
    current[0] = text
    print("step:", text, flush=True)


def stop_children():
    """Stop the browser and end the display runner (SIGTERM: it removes the display and its game) so the job exits."""
    for child in children:
        if child.poll() is None:
            child.terminate()
    for child in children:
        try:
            child.wait(timeout=DELETE_SECONDS)
        except subprocess.TimeoutExpired:
            child.kill()


def fail(name, detail):
    print(f"FAIL {name} in step [{current[0]}]: {detail}", flush=True)
    stop_children()
    os._exit(1)


def page_state():
    """What the runner page itself saw: gamepad polls, the pad's buttons, the RFB key messages it sent."""
    if not pages:
        return "no page"
    try:
        return (
            pages[0]
            .call(
                "Runtime.evaluate",
                expression="JSON.stringify({polls:window.__polls,pad:window.__pad,keymsgs:window.__rfb.slice(-8),"
                "error:document.querySelector('#screen').dataset.error})",
            )["result"]
            .get("value", "no value")
        )
    except Exception as err:  # the page is the thing being diagnosed: report whatever it still answers
        return f"page unreadable: {err}"


def watchdog():
    print(
        f"FAIL STALL in step [{current[0]}]: no end within {TOTAL_SECONDS}s; keys {keys}; game said {said[-8:]}",
        flush=True,
    )
    faulthandler.dump_traceback(file=sys.stdout)
    stop_children()
    os._exit(1)


stalled = threading.Timer(TOTAL_SECONDS, watchdog)
stalled.daemon = True
stalled.start()


repo = Path.cwd()
sys.path.insert(0, str(repo / "src"))
from abu_decomp import display, viewer  # noqa: E402

sys.path.insert(0, str(repo / "ci"))
import game_input_lines  # noqa: E402

work, game, rom = Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3]
displays = work / "displays"
stderr_fifo, stop = work / "game.stderr", work / "stop.fifo"
for fifo in (stderr_fifo, stop):
    fifo.unlink(missing_ok=True)
    os.mkfifo(fifo)

keys = []  # (evdev code, state) as the game received them, in order
said = []  # the game's own non-protocol stderr lines, shown when it never came up
seat = []  # the first seat, keyboard and toplevel protocol lines, shown when the game never took focus
entered = threading.Event()  # the game's window got keyboard focus
arrived = threading.Condition()


def read_game_stream():
    with open(stderr_fifo) as stream:
        for line in stream:
            if not line.startswith("["):
                said.append(line.strip())
            if len(seat) < 12 and re.search(r"wl_seat|wl_keyboard|xdg_toplevel.*(activated|configure)", line):
                seat.append(line.strip()[-120:])
            if game_input_lines.entered(line):
                entered.set()
            if event := game_input_lines.key_event(line):
                with arrived:
                    keys.append(event)
                    arrived.notify_all()


threading.Thread(target=read_game_stream, daemon=True).start()


def await_key(code, state):
    """Wait for the game to receive this key event; returns once it did, fails by name when it never does."""
    with arrived:
        if not arrived.wait_for(lambda: (code, state) in keys, EVENT_SECONDS):
            fail("EVENT", f"the game never received key {code} state {state}; saw {keys}; page {page_state()}")


command = (
    f"exec env WAYLAND_DEBUG=1 SDL_VIDEODRIVER=wayland {game} --rom {rom} --user-dir {work}/user --no-dialog "
    f"--window 1280x720 2> {stderr_fifo}"
)
step("invoke: starting the display and the game")
run = subprocess.Popen(
    [sys.executable, "-m", "abu_decomp", "display", "--dir", str(displays), "invoke", "--", "sh", "-c", command],
    cwd=repo / "src",
    stderr=subprocess.PIPE,
    text=True,
)
children.append(run)
timer = threading.Timer(UP_SECONDS, run.kill)
timer.start()
first = run.stderr.readline()
timer.cancel()
if not first:
    fail("DISPLAY_UP", f"invoke printed no display line within {UP_SECONDS}s (exit {run.poll()})")
ident = json.loads(first)["display"]
print("invoke: display", ident, flush=True)
if not entered.wait(UP_SECONDS):
    fail(
        "GAME_UP",
        f"the game window never took keyboard focus within {UP_SECONDS}s; it said {said[-5:]}; protocol {seat}",
    )
print("game: window has keyboard focus", flush=True)

reported = threading.Event()  # the page drew its second frame: the RFB link is up


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        reported.set()
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        if self.path.endswith("/ws"):
            self.close_connection = True
            record = display.load(displays, ident)
            viewer.serve_socket(self.connection, self.headers["Sec-WebSocket-Key"], record, display.connect_address)
            return
        if self.path.startswith("/static/site/"):
            body = (repo / "src/abu_decomp/static/site" / self.path.rsplit("/", 1)[1]).read_bytes()
            kind = "text/javascript"
        else:
            probe = (
                "<script>const c=document.querySelector('#screen');"
                "new MutationObserver(()=>Number(c.dataset.live)>=2&&fetch('/report',{method:'POST',body:'{}'}))"
                ".observe(c,{attributes:true})</script>"
            )
            body = (viewer.viewer_page(display.load(displays, ident)) + probe).encode()
            kind = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
http.daemon_threads = True
threading.Thread(target=http.serve_forever, daemon=True).start()


class Cdp:
    """A DevTools WebSocket client: `call` sends one command and returns its result."""

    def __init__(self, port, url):
        request = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?{url}", method="PUT")
        target = json.load(urllib.request.urlopen(request, timeout=CHROMIUM_SECONDS))
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=CHROMIUM_SECONDS)
        route = target["webSocketDebuggerUrl"].split(str(port), 1)[1]
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (
                f"GET {route} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        self.reader = self.sock.makefile("rb")
        while self.reader.readline() not in (b"\r\n", b""):
            pass
        self.next_id = 0

    def call(self, method, **params):
        self.next_id += 1
        payload = json.dumps({"id": self.next_id, "method": method, "params": params}).encode()
        mask = os.urandom(4)
        head = bytes([0x81, 0x80 | 126]) + struct.pack(">H", len(payload))
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        while True:
            size = self.reader.read(2)[1] & 127
            if size == 126:
                size = struct.unpack(">H", self.reader.read(2))[0]
            elif size == 127:
                size = struct.unpack(">Q", self.reader.read(8))[0]
            message = json.loads(self.reader.read(size))
            if message.get("id") == self.next_id:
                if "error" in message:
                    fail("CDP", f"{method}: {message['error']}")
                if "exceptionDetails" in message["result"]:
                    fail("CDP", f"{method}: the page threw {message['result']['exceptionDetails']}")
                return message["result"]


browser = subprocess.Popen(
    [
        "chromium",
        "--headless",
        "--no-sandbox",
        "--disable-gpu",
        f"--user-data-dir={work}/chromium",
        "--window-size=1280,720",
        "--remote-debugging-port=9334",
        "about:blank",
    ],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.PIPE,
    text=True,
)
children.append(browser)
step("chromium: starting the headless browser")
timer = threading.Timer(CHROMIUM_SECONDS, browser.kill)
timer.start()
for line in browser.stderr:
    if "DevTools listening" in line:
        break
else:
    fail("CHROMIUM", f"no DevTools port within {CHROMIUM_SECONDS}s")
timer.cancel()

results = {}
try:
    step("page: opening the viewer and waiting for the RFB link")
    page = Cdp(9334, "about:blank")
    pages.append(page)
    page.call("Page.navigate", url=f"http://127.0.0.1:{http.server_port}/viewer/{ident}")
    if not reported.wait(CHROMIUM_SECONDS):
        fail("CHROMIUM", f"the page drew no second frame within {CHROMIUM_SECONDS}s")
    # a fake browser gamepad the proof moves, installed on the loaded page (the viewer polls navigator.getGamepads each
    # frame): buttons by index of the standard mapping, stick axes; plus a record of the key messages the page sends
    page.call("Runtime.evaluate", expression=PAD_SCRIPT)
    shown = display.backend(displays).capture(ident, work / "game.png")
    print("capture: game window non_black =", shown.get("non_black"), flush=True)
    box = page.call(
        "Runtime.evaluate",
        expression="(r=>JSON.stringify([r.left+r.width/2,r.top+r.height/2]))"
        "(document.querySelector('#screen').getBoundingClientRect())",
    )["result"]["value"]
    x, y = json.loads(box)
    for kind in ("mousePressed", "mouseReleased"):  # focus the canvas, and click into the game window
        page.call("Input.dispatchMouseEvent", type=kind, x=x, y=y, button="left", clickCount=1)

    def keyboard(name, code):
        event = {"key": name, "code": name if len(name) > 1 else "Key" + name.upper(), "windowsVirtualKeyCode": code}
        page.call("Input.dispatchKeyEvent", type="rawKeyDown", **event)
        await_key(EVDEV[name], 1)
        page.call("Input.dispatchKeyEvent", type="keyUp", **event)
        await_key(EVDEV[name], 0)

    step("keyboard: ArrowRight press and release from the runner page")
    keyboard("ArrowRight", 39)
    results["keyboard"] = f"ArrowRight evdev 106 press+release received by the game; keys seen {keys}"
    step("gamepad: the page's frame loop, then d-pad left and A press and release from the runner page")
    page.call("Page.bringToFront")
    alive = page.call(
        "Runtime.evaluate",
        expression="Promise.race([new Promise(ok=>requestAnimationFrame(()=>ok('ticks'))),"
        "new Promise(ok=>setTimeout(()=>ok('stalled'),3000))])",
        awaitPromise=True,
    )["result"]["value"]
    if alive != "ticks":
        fail("PAD_LOOP", "the page's requestAnimationFrame never ticked, so its gamepad poll cannot run")
    for index, name in ((14, "ArrowLeft"), (0, "x")):
        page.call("Runtime.evaluate", expression=f"window.__pad.b=[{index}]")
        await_key(EVDEV[name], 1)
        page.call("Runtime.evaluate", expression="window.__pad.b=[]")
        await_key(EVDEV[name], 0)
    results["gamepad"] = (
        f"d-pad left->ArrowLeft 105 and pad A->x evdev 45 press+release received by the game; keys {keys}"
    )
finally:
    browser.kill()

step("cli: the same Input action as keydown and keyup in one call")
before = len(keys)
display.backend(displays).input(ident, [{"keydown": "Right"}, {"keyup": "Right"}])
with arrived:
    if not arrived.wait_for(lambda: keys[before:] == [(106, 1), (106, 0)], EVENT_SECONDS):
        fail("EVENT", f"the CLI keydown/keyup never reached the game as 106 press then release; saw {keys[before:]}")
results["cli"] = "keydown+keyup Right received by the game in order"

for name, text in results.items():
    print(f"input {name} rc=0 {text}", flush=True)
step("delete: stopping the game")
run.terminate()
try:
    code = run.wait(timeout=DELETE_SECONDS)
except subprocess.TimeoutExpired:
    run.kill()
    fail("DELETE", f"invoke still running {DELETE_SECONDS}s after the game was stopped")
print("delete:", "left folder" if (displays / ident).exists() else "clean", "exit", code, flush=True)
