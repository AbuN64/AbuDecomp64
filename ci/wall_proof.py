"""Wall proof: a private `tas serve` of REPO with four parallel plays, then headless chromium shots of the home page at
desktop and phone width and the wall's measured counts (#wall data-visible / data-streams, its column count).

    python ci/wall_proof.py REPO WORK    (on the rig; REPO is a checkout, WORK an empty scratch folder)

The profile is the tree's ci/tas-profile.json rendered by deploy_rig (the one renderer, this script's own ci/). Serve
runs on its own runs folder under WORK (the baked site copied in, so the map sidebar draws) and its own port;
readiness is serve's sd_notify READY=1, the plays are watched on /api/feed/stream, the page's state by a
MutationObserver. It exits non-zero at once when a play is refused or a run ends with no stream frame (its own play
length passed with nothing to show). Prints one JSON line per width.
"""

import base64
import functools
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # this script's own ci/: deploy_rig, whatever REPO is
import deploy_rig

SERVE_UP_SECONDS = 180  # serve's own startup (it gates the corpus): its READY=1 must arrive within this
MOVIES = ("arena-bottleneck", "arena-courtyard", "arena-fallout", "arena-the-pit")  # four named plays, one ROM
PLAYS = len(MOVIES)
PLAYS_SECONDS = 120  # bound on one HTTP answer and on the feed's silence between two events
PAGE_MS = 20000  # the page must settle (streams == visible > 0) within this; past it the state is read as it is
LISTEN = 8197
CDP = 9337
WIDTHS = {"desktop": (1440, 900, False), "phone": (390, 844, True)}

repo, work = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
work.mkdir(parents=True, exist_ok=True)
home = Path.home()
profile = json.loads(deploy_rig.rendered_profile(repo / deploy_rig.TEMPLATE, home))
missing = [m for m in MOVIES if not (Path(profile["tas"]["movies"]) / f"{m}.tas").is_file()]
if missing:
    sys.exit(f"wall proof: no movie {', '.join(missing)} in {profile['tas']['movies']}")
live_runs = Path(profile["tas"]["runs"])
runs = work / "runs"
if (live_runs / "site").is_dir():
    shutil.copytree(live_runs / "site", runs / "site", ignore=shutil.ignore_patterns("live.json"))
profile["tas"]["runs"] = str(runs)
profile["tas"]["serve"] |= {"listen": f"127.0.0.1:{LISTEN}", "jobs": PLAYS}
(work / "profile.json").write_text(json.dumps(profile))

notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
notify.bind(str(work / "notify"))
notify.settimeout(SERVE_UP_SECONDS)
env = os.environ | {"NOTIFY_SOCKET": str(work / "notify"), "PYTHONPATH": str(repo / "src")}
cmd = [sys.executable, "-m", "abu_decomp", "--profile", str(work / "profile.json"), "tas", "serve"]
with open(work / "serve.log", "w") as log:  # the child keeps its own copy of the descriptor
    serve = subprocess.Popen(cmd, cwd=repo / "src", env=env, stdout=log, stderr=subprocess.STDOUT)
browser = None
base = f"http://127.0.0.1:{LISTEN}"


def api(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=PLAYS_SECONDS))


def cdp(sock, reader, counter, method, **params):
    """One Chrome DevTools call over the raw websocket: send a masked text frame, read frames to its answer."""
    n = next(counter)
    payload = json.dumps({"id": n, "method": method, "params": params}).encode()
    mask = os.urandom(4)
    size = len(payload)
    head = (
        bytes([0x81, 0x80 | 126]) + struct.pack(">H", size)
        if size < 65536
        else (bytes([0x81, 0x80 | 127]) + struct.pack(">Q", size))
    )
    sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
    while True:
        second = reader.read(2)[1] & 127
        if second == 126:
            second = struct.unpack(">H", reader.read(2))[0]
        elif second == 127:
            second = struct.unpack(">Q", reader.read(8))[0]
        message = json.loads(reader.read(second))
        if message.get("id") == n:
            return message.get("result", message)


try:
    while notify.recv(4096).find(b"READY=1") < 0:
        pass
    print("serve: ready", flush=True)
    ids = []
    for movie in MOVIES:
        try:
            started = api("/api/play", {"movie": movie, "label": "wall-proof"})
        except urllib.error.HTTPError as error:
            sys.exit(f"wall proof: play {movie} refused {error.code} {error.read()[:300].decode(errors='replace')}")
        if started.get("refused"):  # serve keeps a play it cannot start as a refused row and answers its reason
            sys.exit(f"wall proof: play {movie} refused: {started['refused']}")
        ids.append(started["id"])
        print("play:", movie, ids[-1], flush=True)
    stream = urllib.request.urlopen(base + "/api/feed/stream", timeout=PLAYS_SECONDS)
    framed = set()
    for _ in stream:  # every feed change and keepalive is a moment to read the four tiles
        tiles = {t["id"]: t for t in api(f"/api/wall?offset=0&limit={4 * PLAYS + 8}")["tiles"] if t["id"] in ids}
        framed |= {i for i, t in tiles.items() if t["poll"] > 0}
        # queued is serve's placeholder until a run writes its record; a run is dead when its own end (the signal, no
        # clock here) comes with no frame
        over = {i for i, t in tiles.items() if t["status"] not in ("queued", "running")}
        dead = sorted(over - framed)
        if dead:
            for i in dead:  # the run's own files say why it ended: its record and every log tail
                for f in sorted((runs / "plays" / i).glob("*")):
                    if f.is_file() and f.suffix in (".json", ".log", ".txt"):
                        print(f"--- {i}/{f.name}:", f.read_text(errors="replace")[-1500:], flush=True)
            sys.exit(f"wall proof: run(s) {dead} ended with no stream frame: {[tiles[i] for i in dead]}")
        if framed >= set(ids):
            break
    stream.close()
    print("plays framed:", {i: (t["status"], t["poll"], t["polls"]) for i, t in tiles.items()}, flush=True)

    browser = subprocess.Popen(
        [
            "/usr/bin/chromium",
            "--headless",
            "--no-sandbox",
            "--disable-gpu",
            f"--user-data-dir={work}/chromium",
            f"--remote-debugging-port={CDP}",
            "--remote-allow-origins=*",
            "about:blank",
        ],
        stderr=subprocess.PIPE,
        text=True,
    )
    while "DevTools listening" not in browser.stderr.readline():
        pass
    failed = []
    for name, (w, h, mobile) in WIDTHS.items():
        req = urllib.request.Request(f"http://127.0.0.1:{CDP}/json/new?about:blank", method="PUT")
        target = json.load(urllib.request.urlopen(req, timeout=PLAYS_SECONDS))
        sock = socket.create_connection(("127.0.0.1", CDP), timeout=PAGE_MS / 1000 + 30)
        route = target["webSocketDebuggerUrl"].split(str(CDP), 1)[1]
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(
            f"GET {route} HTTP/1.1\r\nHost: 127.0.0.1:{CDP}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        reader = sock.makefile("rb")
        while reader.readline() not in (b"\r\n", b""):
            pass
        call = functools.partial(cdp, sock, reader, iter(range(1, 1000)))

        call("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=1, mobile=mobile)
        call("Page.enable")
        call("Page.navigate", url=base + "/")
        settle = (
            "new Promise(done => { const read = () => { const g = document.getElementById('wall');"
            " if (!g) return null; const v = Number(g.dataset.visible || 0), s = Number(g.dataset.streams || 0);"
            " return {visible: v, streams: s, tiles: g.children.length,"
            " columns: getComputedStyle(g).gridTemplateColumns.split(' ').length}; };"
            " const check = () => { const r = read(); if (r && r.visible > 0 && r.streams === r.visible) done(r); };"
            " new MutationObserver(check).observe(document, {attributes: true, childList: true, subtree: true});"
            f" setTimeout(() => done(read() || {{wall: 'absent'}}), {PAGE_MS}); check(); }})"
        )
        state = call("Runtime.evaluate", expression=settle, awaitPromise=True, returnByValue=True)["result"]["value"]
        state["feed"] = call(
            "Runtime.evaluate",
            returnByValue=True,
            expression=(
                "(() => { const f = document.getElementById('feed'), t = document.getElementById('feedtoggle');"
                " if (!f || !t) return 'absent';"
                " const pane = () => getComputedStyle(f.closest('aside') || f).display;"
                " const items = [...f.querySelectorAll('li')].slice(0, 6)"
                ".map(li => li.textContent.trim().slice(0, 90));"
                " const shown = pane(); t.click(); const hidden = pane();"
                " const kept = localStorage.getItem('site.feed');"
                " t.click(); return {count: f.querySelectorAll('li').length, items, shown, hidden, kept}; })()"
            ),
        )["result"]["value"]
        shot = work / f"wall-{name}.jpg"
        shot.write_bytes(base64.b64decode(call("Page.captureScreenshot", format="jpeg", quality=70)["data"]))
        print(f"SHOT {shot.name} {base64.b64encode(shot.read_bytes()).decode()}", flush=True)
        print(json.dumps({"width": name, "px": w, "shot": str(shot), **state}), flush=True)
        sock.close()
        if not state.get("visible") or state.get("streams") != state["visible"]:
            failed.append(name)  # every width is still shot; the proof fails after them
    if failed:
        sys.exit(f"wall proof: streams != visible tiles (or none visible) at {', '.join(failed)}")
finally:
    if browser:
        browser.kill()
    serve.terminate()
    serve.wait()
    print("serve log tail:", (work / "serve.log").read_text()[-600:], flush=True)
