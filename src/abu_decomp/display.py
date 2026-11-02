"""The site's own display adapter: one interface (Create, Describe, Capture, Input, Delete), with a Linux and a
Windows backend.

Linux holds a headless Wayland compositor (labwc) and a VNC server on a Unix socket (wayvnc) in a private run directory,
captures a frame and sends keys and pointer events by speaking RFB to the Linux socket. Windows reads the guest
desktop over
VNC at an endpoint the site's config names (the guest's own VNC server is not ours to start or stop). Both are reached
through the same address (`unix path` or `tcp host:port`), so capture and the browser viewer (viewer.py) do not care
which backend made the display.
Called as `decomp display ...`, so rig jobs and the runner use the same code. Nothing here calls another toolkit.
"""

from __future__ import annotations

import json
import os
import secrets
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from PIL import Image

from abu_decomp import DecompError, inotify

# Host packages the Linux backend runs; the rig's install names them (docs/display.md), nothing here installs them.
PACKAGES = ("labwc", "wayvnc", "wlr-randr", "pipewire", "wireplumber", "pipewire-pulse", "pactl", "parec")
# The display's sound: a private PipeWire (with its session manager and Pulse server) in the run directory, so a game
# drawing on the display finds it through XDG_RUNTIME_DIR alone and plays into the null sink SINK; Audio reads that
# sink's monitor
# as raw s16le stereo at AUDIO_RATE.
SINK = "display"
AUDIO_RATE = 48000
AUDIO_LATENCY_MS = 20
# Seconds a stopped tool gets to leave on SIGTERM before SIGKILL: a bound the adapter declares, not a wait for effect.
GRACE_SECONDS = 5.0
# Seconds a tool gets to become ready (labwc's socket, wlr-randr's exit, wayvnc's socket) and the RFB server to answer
# one read; past them the adapter fails naming the tool that never came up (a hang is a defect, never a wait).
START_SECONDS = 30.0
RFB_SECONDS = 30.0
# The longest one display command may run: create starts three tools, then every command ends in an RFB read. A caller
# (ci/display_input_proof.sh) bounds its calls by it.
COMMAND_SECONDS = 3 * START_SECONDS + RFB_SECONDS
OUTPUT = "HEADLESS-1"
HOLD = "hold.sock"  # the input holder's control socket in the display's directory
RAW, CURSOR, POINTER_POS = 0, -239, -232
# Dials of a Windows guest's ServerInit that may answer EOF right after the display starts (QUIRKS 7).
ATTEMPTS = 3


@dataclass(frozen=True)
class Record:
    """What Describe answers for one display."""

    identity: str
    width: int
    height: int
    socket: str
    pids: dict[str, int]
    directory: str


class Backend(Protocol):
    def create(self, size: str, endpoint: str | None = None) -> Record: ...
    def describe(self, subject: str) -> Record: ...
    def capture(self, subject: str, path: Path) -> dict[str, object]: ...
    def input(self, subject: str, events: object) -> dict[str, object]: ...
    def environment(self, subject: str) -> dict[str, str]: ...
    def audio(self, subject: str) -> subprocess.Popen: ...
    def delete(self, subject: str) -> Record: ...


def parse_size(size: str) -> tuple[int, int]:
    """'1280x720' -> (1280, 720); anything else is a refusal."""
    width, sep, height = size.partition("x")
    if not sep or not width.isdecimal() or not height.isdecimal() or int(width) < 1 or int(height) < 1:
        raise DecompError("display.size", f"size {size!r} is not WIDTHxHEIGHT", "pass --size 1280x720")
    return int(width), int(height)


# ---------------------------------------------------------------- Input

# X keysyms of the key names an event's "key" accepts beyond one character.
KEYSYMS = {
    "BackSpace": 0xFF08, "Tab": 0xFF09, "Return": 0xFF0D, "Escape": 0xFF1B, "Delete": 0xFFFF,
    "Home": 0xFF50, "Left": 0xFF51, "Up": 0xFF52, "Right": 0xFF53, "Down": 0xFF54,
    "Page_Up": 0xFF55, "Page_Down": 0xFF56, "End": 0xFF57, "Print": 0xFF61, "Insert": 0xFF63, "Menu": 0xFF67,
    "KP_Enter": 0xFF8D, "Shift_L": 0xFFE1, "Shift_R": 0xFFE2, "Control_L": 0xFFE3, "Control_R": 0xFFE4,
    "Caps_Lock": 0xFFE5, "Alt_L": 0xFFE9, "Alt_R": 0xFFEA, "Super_L": 0xFFEB, "Super_R": 0xFFEC,
    "space": 0x20, "plus": 0x2B,
    **{f"F{n}": 0xFFBD + n for n in range(1, 13)},
}  # fmt: skip
LEFT, MIDDLE, RIGHT = 1, 2, 4  # RFB pointer button mask bits
BUTTONS = {"left": LEFT, "middle": MIDDLE, "right": RIGHT}
# One input stroke: ("key", keysym, down, 0) or ("pointer", mask, x, y).
Stroke = tuple[str, int, int, int]


def keysym_of(char: str) -> int | None:
    """The X keysym typing one character: Latin-1 is its own, newline Return, tab Tab, the rest 0x01000000|code."""
    code = ord(char)
    if char == "\n":
        return KEYSYMS["Return"]
    if char == "\t":
        return KEYSYMS["Tab"]
    if code < 0x20 or 0x7F <= code < 0xA0 or 0xD800 <= code < 0xE000:
        return None
    return code if code <= 0xFF else 0x01000000 | code


def chord(name: str) -> list[int] | None:
    """'Return' -> [0xff0d]; 'Control_L+c' -> [0xffe3, 0x63]; a lone '+' is the plus key; unknown -> None."""
    keysyms = []
    for part in [name] if name == "+" else name.split("+"):
        keysym = KEYSYMS.get(part) or (keysym_of(part) if len(part) == 1 else None)
        if keysym is None:
            return None
        keysyms.append(keysym)
    return keysyms


def press(keysyms: list[int]) -> list[Stroke]:
    """Press in order, release in reverse."""
    return [("key", k, 1, 0) for k in keysyms] + [("key", k, 0, 0) for k in reversed(keysyms)]


def plan(events: object, width: int, height: int) -> list[Stroke]:
    """Every event of the list as strokes, or a refusal naming the first bad event; nothing is sent before all pass.

    An event holds exactly one member: {"key": "Control_L+c"}, {"text": "hi"},
    {"pointer": {"x": 1, "y": 2, "buttons": 0}}
    (an absolute move holding the buttons mask), {"click": {"x": 1, "y": 2, "button": "left"}} (move, press, release)
    or {"keydown": "a"} / {"keyup": "a"} (a held key: the release is its own event).
    """
    if not isinstance(events, list) or not events:
        raise DecompError(
            "display.input", "events must be a non-empty JSON list", 'pass --events \'[{"key": "Return"}]\''
        )
    strokes: list[Stroke] = []
    for at, event in enumerate(events):
        if not isinstance(event, dict) or len(event) != 1:
            raise DecompError(
                "display.input", f"event {at} must hold exactly one of key, keydown, keyup, text, pointer, click"
            )
        ((kind, value),) = event.items()
        if kind == "key" and isinstance(value, str) and (keysyms := chord(value)):
            strokes += press(keysyms)
        elif kind == "key":
            raise DecompError(
                "display.input", f"event {at}: key {value!r} is not a known key name", "e.g. Return, F10, a"
            )
        elif kind in ("keydown", "keyup") and isinstance(value, str) and (keysyms := chord(value)):
            strokes += [("key", k, int(kind == "keydown"), 0) for k in keysyms]
        elif kind in ("keydown", "keyup"):
            raise DecompError("display.input", f"event {at}: key {value!r} is not a known key name", "e.g. Return, a")
        elif kind == "text" and isinstance(value, str):
            for char in value:
                if (keysym := keysym_of(char)) is None:
                    raise DecompError("display.input", f"event {at}: text character {char!r} has no keysym")
                strokes += press([keysym])
        elif kind in ("pointer", "click") and isinstance(value, dict):
            x, y = value.get("x"), value.get("y")
            if not (
                isinstance(x, int) and isinstance(y, int) and not isinstance(x, bool) and not isinstance(y, bool)
                and 0 <= x < width and 0 <= y < height
            ):  # fmt: skip
                raise DecompError("display.input", f"event {at}: x,y must be integers inside {width}x{height}")
            if kind == "pointer":
                mask = value.get("buttons", 0)
                if not isinstance(mask, int) or isinstance(mask, bool) or not 0 <= mask <= 255:
                    raise DecompError("display.input", f"event {at}: buttons must be an integer mask 0-255")
                strokes.append(("pointer", mask, x, y))
            else:
                button = BUTTONS.get(value.get("button", "left"))
                if button is None:
                    raise DecompError("display.input", f"event {at}: button must be one of {', '.join(BUTTONS)}")
                strokes += [("pointer", 0, x, y), ("pointer", button, x, y), ("pointer", 0, x, y)]
        else:
            raise DecompError("display.input", f"event {at}: {kind!r} with that value is not an input event")
    return strokes


# ---------------------------------------------------------------- RFB


class Rfb:
    """The part of RFB 3.8 a capture needs: no-auth handshake, true-colour raw frames."""

    def __init__(self, conn: socket.socket) -> None:
        self.conn = conn
        version = self.read(12)
        if not version.startswith(b"RFB "):
            raise DecompError("display.rfb", f"not an RFB server: {version!r}")
        self.conn.sendall(b"RFB 003.008\n")
        kinds = self.read(self.read(1)[0])
        if not kinds:
            (length,) = struct.unpack(">I", self.read(4))
            raise DecompError("display.rfb", "RFB refused the handshake: " + self.read(length).decode())
        if 1 not in kinds:
            raise DecompError("display.rfb", f"RFB offers security types {list(kinds)}, only None (1) is spoken")
        self.conn.sendall(b"\x01")
        if struct.unpack(">I", self.read(4))[0] != 0:
            raise DecompError("display.rfb", "RFB security result is a failure")
        self.conn.sendall(b"\x01")  # ClientInit: shared
        self.width, self.height = struct.unpack(">HH", self.read(4))
        self.read(16)  # the server's pixel format: replaced below
        self.read(struct.unpack(">I", self.read(4))[0])  # name
        # 32 bpp, depth 24, little endian, true colour, 255/255/255, shifts 16/8/0 -> bytes B G R X
        self.conn.sendall(b"\x00\x00\x00\x00" + struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
        encodings = (RAW, CURSOR, POINTER_POS)
        self.conn.sendall(struct.pack(">BxH", 2, len(encodings)) + b"".join(struct.pack(">i", e) for e in encodings))

    def read(self, count: int) -> bytes:
        data = bytearray()
        while len(data) < count:
            try:
                chunk = self.conn.recv(count - len(data))
            except TimeoutError as exc:
                raise DecompError(
                    "display.rfb",
                    f"RFB dial: wayvnc sent nothing for {RFB_SECONDS}s after {len(data)} of {count} bytes",
                ) from exc
            if not chunk:
                raise DecompError("display.rfb", f"RFB closed after {len(data)} of {count} bytes")
            data += chunk
        return bytes(data)

    def update(self, width: int = 0, height: int = 0) -> Image.Image:
        """Ask for the screen (or its top-left width x height) and answer the next FramebufferUpdate as an image."""
        self.conn.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, width or self.width, height or self.height))
        screen = Image.new("RGB", (self.width, self.height))
        while (kind := self.read(1)[0]) != 0:
            if kind == 3:  # ServerCutText
                self.read(struct.unpack(">3xI", self.read(7))[0])
            elif kind != 2:  # Bell has no body
                raise DecompError("display.rfb", f"RFB server message {kind} is not spoken")
        (rects,) = struct.unpack(">xH", self.read(3))
        for _ in range(rects):
            x, y, w, h, encoding = struct.unpack(">HHHHi", self.read(12))
            if encoding == RAW:
                screen.paste(Image.frombytes("RGB", (w, h), self.read(w * h * 4), "raw", "BGRX"), (x, y))
            elif encoding == CURSOR:
                self.read(w * h * 4 + ((w + 7) // 8) * h)
            elif encoding != POINTER_POS:
                raise DecompError("display.rfb", f"RFB rectangle encoding {encoding} was not requested")
        return screen

    def send(self, stroke: Stroke) -> None:
        """One KeyEvent (message 4) or PointerEvent (message 5)."""
        kind, first, second, third = stroke
        if kind == "key":
            self.conn.sendall(struct.pack(">BBxxI", 4, second, first))
        else:
            self.conn.sendall(struct.pack(">BBHH", 5, first, second, third))

    def strokes(self, strokes: list[Stroke]) -> None:
        """Send the strokes in order and return once the server has taken them (a framebuffer round trip)."""
        buttons = 0
        for stroke in strokes:
            self.send(stroke)
            if stroke[0] == "pointer" and stroke[1] != buttons:
                buttons = stroke[1]
                self.update(1, 1)  # wayvnc takes a button change per pass of its loop: a round trip separates them
        self.update()

    def frame(self, drop_first: bool = False) -> Image.Image:
        """wayvnc's first update is the buffer kept since its last client left (QUIRKS 4): drop it, use the second."""
        if drop_first:
            self.update()
        return self.update()


def connect_unix(path: str) -> socket.socket:
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.settimeout(RFB_SECONDS)
        conn.connect(path)
    except OSError as exc:
        conn.close()
        raise DecompError("display.connect", f"cannot reach {path}: {exc}") from exc
    return conn


def connect_address(address: str) -> socket.socket:
    """Dial a display's address: `tcp:host:port` for a guest VNC server, else the path of a Unix socket."""
    if not address.startswith("tcp:"):
        return connect_unix(address)
    host, _, port = address[4:].rpartition(":")
    try:
        return socket.create_connection((host, int(port)))
    except (OSError, ValueError) as exc:
        raise DecompError("display.connect", f"cannot reach {address}: {exc}") from exc


def load(root: Path, subject: str) -> Record:
    path = root / subject / "display.json"
    if not subject.startswith("d-") or "/" in subject or not path.is_file():
        raise DecompError("display.notfound", f"no display {subject!r}", "create one with `decomp display create`")
    return Record(**json.loads(path.read_text()))


def records(root: Path) -> list[Record]:
    """Every display kept under root, oldest name first."""
    return [load(root, d.name) for d in sorted(root.glob("d-*")) if (d / "display.json").is_file()]


def read_frame(record: Record, connect: Callable[[str], socket.socket], drop_first: bool) -> Image.Image:
    conn = connect(record.socket)
    try:
        return Rfb(conn).frame(drop_first)
    finally:
        conn.close()


def write_png(image: Image.Image, path: Path) -> dict[str, object]:
    if not path.is_absolute():
        raise DecompError("display.path", "capture needs an absolute --path")
    partial = path.with_name("." + path.name + ".part")
    image.save(partial, "PNG")
    partial.replace(path)
    return {"path": str(path), "width": image.width, "height": image.height, "non_black": image.getbbox() is not None}


# ---------------------------------------------------------------- Linux


def is_tool(pid: int, name: str) -> bool:
    """Whether the pid still is that tool (a record outlives its processes; a pid number is reused)."""
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip().startswith(name)
    except OSError:
        return False


def hold(rfb: str, ctl: str) -> None:
    """The process a display keeps for its life: one RFB connection to wayvnc and the control socket Input writes to.

    A request is one JSON line of strokes, answered `ok` or `error: ...` once the server took them.
    """
    client = Rfb(connect_unix(rfb))
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(ctl + ".tmp")
    server.listen()
    os.rename(ctl + ".tmp", ctl)  # a socket file exists from bind, before listen: the display waits on this rename
    while True:
        conn, _ = server.accept()
        with conn:
            conn.settimeout(RFB_SECONDS)
            try:
                client.strokes(json.loads(conn.makefile().readline()))
                conn.sendall(b"ok\n")
            except (DecompError, OSError, ValueError) as exc:  # the held connection is gone or the request is bad
                conn.sendall(f"error: {exc}\n".encode())
                if not isinstance(exc, ValueError):
                    raise


class Linux:
    """labwc (headless, pixman) + wlr-randr to the size + wayvnc on rfb.sock, all in <root>/<id>/."""

    def __init__(
        self,
        root: Path,
        spawn: Callable[..., subprocess.Popen] = subprocess.Popen,
        connect: Callable[[str], socket.socket] = connect_address,
    ) -> None:
        self.root = root
        self.spawn = spawn
        self.connect = connect

    def create(self, size: str, endpoint: str | None = None) -> Record:
        if endpoint:
            raise DecompError(
                "display.endpoint", "a Linux display starts its own VNC server; --endpoint is for Windows"
            )
        width, height = parse_size(size)
        for tool in PACKAGES:
            if not any((Path(d) / tool).exists() for d in os.get_exec_path()):
                raise DecompError("display.package", f"{tool} is not installed", f"install {', '.join(PACKAGES)}")
        identity = "d-" + secrets.token_hex(4)
        directory = self.root / identity
        run, config, rfb = directory / "run", directory / "config", directory / "rfb.sock"
        run.mkdir(parents=True, mode=0o700)
        config.mkdir(mode=0o700)
        (config / "wayvnc").write_text("")
        pids: dict[str, int] = {}
        try:
            base = {"HOME": str(Path.home()), "PATH": os.environ.get("PATH", ""), "XDG_RUNTIME_DIR": str(run)}
            client = base | {"WAYLAND_DISPLAY": "wayland-0"}
            headless = {"WLR_BACKENDS": "headless", "WLR_RENDERER": "pixman", "WLR_LIBINPUT_NO_DEVICES": "1"}
            compositor = base | headless | {"WLR_HEADLESS_OUTPUTS": "1"}
            labwc = self._start(directory, ["labwc", "-C", str(config)], compositor)
            pids["labwc"] = labwc.pid
            self._await(run / "wayland-0", labwc, directory, "labwc")
            mode = ["wlr-randr", "--output", OUTPUT, "--custom-mode", f"{width}x{height}"]
            randr = self._start(directory, mode, client)
            try:
                failed = randr.wait(timeout=START_SECONDS) != 0
            except subprocess.TimeoutExpired as exc:
                randr.kill()
                raise DecompError(
                    "display.start", f"wlr-randr never finished in {START_SECONDS}s (labwc up, no output mode set)"
                ) from exc
            if failed:
                raise DecompError("display.start", "wlr-randr failed: " + self._log(directory, "wlr-randr"))
            self._sound(directory, run, base, pids)
            serve = ["wayvnc", "--config", str(config / "wayvnc"), "--unix-socket", "--max-fps=60", str(rfb)]
            wayvnc = self._start(directory, serve, client)
            pids["wayvnc"] = wayvnc.pid
            self._await(rfb, wayvnc, directory, "wayvnc")
            record = Record(identity, width, height, str(rfb), pids, str(directory))
            self._check_size(record)
            package = str(Path(__file__).resolve().parents[1])
            holder = self._start(
                directory, [sys.executable, str(Path(__file__).resolve()), str(rfb), str(directory / HOLD)],
                client | {"PYTHONPATH": package}, "holder",
            )  # fmt: skip
            pids["holder"] = holder.pid
            self._await(directory / HOLD, holder, directory, "holder")
            (directory / "display.json").write_text(json.dumps(asdict(record)))
        except BaseException:
            self._stop(pids)
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return record

    def _sound(self, directory: Path, run: Path, base: dict[str, str], pids: dict[str, int]) -> None:
        """Start the display's own PipeWire, WirePlumber and Pulse server, then load the null sink as the default."""
        state = directory / "state"
        state.mkdir(mode=0o700)
        env = base | {"XDG_CONFIG_HOME": str(directory / "config"), "XDG_STATE_HOME": str(state)}
        pipewire = self._start(directory, ["pipewire"], env)
        pids["pipewire"] = pipewire.pid
        self._await(run / "pipewire-0", pipewire, directory, "pipewire")
        pids["wireplumber"] = self._start(directory, ["wireplumber"], env).pid
        (run / "pulse").mkdir(mode=0o700)  # the Pulse server's socket folder, watched for its socket
        pulse = self._start(directory, ["pipewire-pulse"], env)
        pids["pipewire-pulse"] = pulse.pid
        self._await(run / "pulse" / "native", pulse, directory, "pipewire-pulse")
        server = "--server=unix:" + str(run / "pulse" / "native")
        for argv in (
            [
                "pactl",
                server,
                "load-module",
                "module-null-sink",
                f"sink_name={SINK}",
                f"rate={AUDIO_RATE}",
                "channels=2",
            ],
            ["pactl", server, "set-default-sink", SINK],
        ):
            step = self._start(directory, argv, env, "pactl")
            try:
                failed = step.wait(timeout=START_SECONDS) != 0
            except subprocess.TimeoutExpired as exc:
                step.kill()
                raise DecompError("display.audio", f"{' '.join(argv[2:4])} never finished in {START_SECONDS}s") from exc
            if failed:
                raise DecompError("display.audio", f"pactl {argv[2]} failed: " + self._log(directory, "pactl"))

    def audio(self, subject: str) -> subprocess.Popen:
        """The display's sound from now on: parec on the null sink's monitor, raw s16le stereo AUDIO_RATE on stdout.

        The caller owns the process (reads it, kills it when its viewer leaves); each viewer gets its own, so the sound
        starts at the moment it connects and keeps pace with the picture.
        """
        record = self.describe(subject)
        native = Path(record.directory) / "run" / "pulse" / "native"
        if not native.exists():
            raise DecompError("display.audio", f"display {subject} has no sound server (no {native})")
        argv = [
            "parec", "--server=unix:" + str(native), "--device=" + SINK + ".monitor", "--raw", "--format=s16le",
            "--channels=2", f"--rate={AUDIO_RATE}", f"--latency-msec={AUDIO_LATENCY_MS}",
        ]  # fmt: skip
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(Path.home()), "XDG_RUNTIME_DIR": str(native.parents[1])}
        with (Path(record.directory) / "parec.log").open(
            "ab"
        ) as log:  # parec's own words, kept beside the other tools'
            return self.spawn(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log)

    def _start(self, directory: Path, argv: list[str], env: dict[str, str], name: str = "") -> subprocess.Popen:
        with (directory / f"{name or argv[0]}.log").open("wb") as log:
            return self.spawn(argv, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)

    @staticmethod
    def _log(directory: Path, name: str) -> str:
        return (directory / f"{name}.log").read_text(errors="replace")[-400:].strip()

    def _await(self, path: Path, process: subprocess.Popen, directory: Path, name: str) -> None:
        """Block on inotify for `path` and the tool's exit (a pidfd), bounded by START_SECONDS, naming the tool."""
        watcher = inotify.Watcher()
        exit_fd = None
        try:
            watcher.add(path.parent, inotify.CREATE | inotify.MOVED_TO)
            while not path.exists():
                if process.poll() is not None:
                    failure = f"{name} exited {process.returncode}: {self._log(directory, name)}"
                    raise DecompError("display.start", failure)
                if exit_fd is None:
                    exit_fd = os.pidfd_open(process.pid)
                ready, _, _ = select.select([watcher.fd, exit_fd], [], [], START_SECONDS)
                if not ready:
                    failure = f"{name} never became ready: {path.name} did not appear in {START_SECONDS}s: "
                    raise DecompError("display.start", failure + self._log(directory, name))
                if watcher.fd in ready:
                    watcher.read()
        finally:
            watcher.close()
            if exit_fd is not None:
                os.close(exit_fd)

    def _check_size(self, record: Record) -> None:
        conn = self.connect(record.socket)
        try:
            got = Rfb(conn)
        finally:
            conn.close()
        if (got.width, got.height) != (record.width, record.height):
            raise DecompError(
                "display.start", f"the display is {got.width}x{got.height}, not {record.width}x{record.height}"
            )

    def describe(self, subject: str) -> Record:
        return load(self.root, subject)

    def capture(self, subject: str, path: Path) -> dict[str, object]:
        return write_png(read_frame(self.describe(subject), self.connect, drop_first=True), path)

    def environment(self, subject: str) -> dict[str, str]:
        """What a client needs to draw on this display: its Wayland socket in the private run directory, and SDL told
        to use it (over a headless profile's `SDL_VIDEODRIVER=offscreen`, which would leave the display black)."""
        run = Path(self.describe(subject).directory) / "run"
        return {"XDG_RUNTIME_DIR": str(run), "WAYLAND_DISPLAY": "wayland-0", "SDL_VIDEODRIVER": "wayland"}

    def input(self, subject: str, events: object) -> dict[str, object]:
        """Send the events over RFB; the framebuffer request after them is answered once the server has taken them."""
        record = self.describe(subject)
        strokes = plan(events, record.width, record.height)
        # The strokes go through the display's holder: wayvnc makes a virtual keyboard and pointer per RFB connection
        # and drops them on disconnect, so a client (foot) only gets pointer focus and the click only lands while one
        # connection lives. The holder keeps that connection for the display's life.
        ctl = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            ctl.settimeout(RFB_SECONDS)
            try:
                ctl.connect(str(Path(record.directory) / HOLD))
                ctl.sendall(json.dumps(strokes).encode() + b"\n")
                answer = ctl.makefile().readline().strip()
            except OSError as exc:
                raise DecompError("display.input", f"the display's input holder did not answer: {exc}") from exc
        finally:
            ctl.close()
        if answer != "ok":
            raise DecompError("display.input", f"the display's input holder answered {answer or 'nothing'!r}")
        return {"events": len(events), "strokes": len(strokes)}  # type: ignore[arg-type]

    def delete(self, subject: str) -> Record:
        record = self.describe(subject)
        self._stop(record.pids)
        shutil.rmtree(record.directory)
        return record

    def _stop(self, pids: dict[str, int]) -> None:
        """SIGTERM every tool, newest first, and wait on its pidfd; SIGKILL what outlives GRACE_SECONDS."""
        for name in reversed(list(pids)):
            pid = pids[name]
            if not is_tool(pid, "python" if name == "holder" else name):
                continue
            try:
                fd = os.pidfd_open(pid)
            except OSError:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                if not select.select([fd], [], [], GRACE_SECONDS)[0]:
                    os.kill(pid, signal.SIGKILL)
                    select.select([fd], [], [])
            except ProcessLookupError:
                pass
            finally:
                os.close(fd)


# ---------------------------------------------------------------- Windows


class Windows:
    """The guest desktop over VNC at `host:port` (the site's config names it). The guest's VNC server (tvnserver) is the
    guest's own: Create only proves it answers and records it, Delete forgets the record and stops nothing there."""

    def __init__(
        self,
        root: Path,
        connect: Callable[[str], socket.socket] = connect_address,
        attempts: int = ATTEMPTS,
    ) -> None:
        self.root = root
        self.connect = connect
        self.attempts = attempts

    def create(self, size: str, endpoint: str | None = None) -> Record:
        """`size` is the guest's own and is not set here; the record carries the size the guest reports."""
        host, _, port = (endpoint or "").rpartition(":")
        if not host or not port.isdecimal():
            raise DecompError(
                "display.endpoint", "a Windows display needs --endpoint HOST:PORT", "name it in the config"
            )
        address = f"tcp:{host}:{port}"
        rfb = self._handshake(address)
        identity = "d-" + secrets.token_hex(4)
        directory = self.root / identity
        directory.mkdir(parents=True, mode=0o700)
        record = Record(identity, rfb.width, rfb.height, address, {}, str(directory))
        (directory / "display.json").write_text(json.dumps(asdict(record)))
        return record

    def _handshake(self, address: str) -> Rfb:
        """ServerInit's first answer can be EOF right after the guest display starts (QUIRKS 7): redial, bounded."""
        for attempt in range(self.attempts):
            conn = self.connect(address)
            try:
                return Rfb(conn)
            except DecompError as exc:
                if attempt + 1 == self.attempts or "closed" not in exc.detail:
                    raise
            finally:
                conn.close()
        raise AssertionError("unreachable")

    def describe(self, subject: str) -> Record:
        return load(self.root, subject)

    def capture(self, subject: str, path: Path) -> dict[str, object]:
        return write_png(read_frame(self.describe(subject), self.connect, drop_first=False), path)

    def input(self, subject: str, events: object) -> dict[str, object]:
        raise DecompError(
            "display.unsupported",
            "Unsupported: a Windows guest takes input from its own viewer session; input is for Linux displays",
        )

    def environment(self, subject: str) -> dict[str, str]:
        raise DecompError("display.unsupported", "a Windows guest runs its own programs; invoke is for Linux displays")

    def audio(self, subject: str) -> subprocess.Popen:
        raise DecompError(
            "display.unsupported",
            "Unsupported: a Windows guest's sound has no capture yet; audio is for Linux displays",
        )

    def delete(self, subject: str) -> Record:
        record = self.describe(subject)
        shutil.rmtree(record.directory)
        return record


def backend(root: Path | None = None, kind: str | None = None) -> Backend:
    kind = kind or ("windows" if sys.platform == "win32" else "linux")
    base = root or Path.home() / ".local" / "state" / "abu-decomp" / "displays"
    if kind not in ("linux", "windows"):
        raise DecompError("display.unsupported", f"no display backend {kind!r}", "use linux or windows")
    base.mkdir(parents=True, exist_ok=True)
    return Linux(base) if kind == "linux" else Windows(base)


if __name__ == "__main__":  # the display's holder: `python display.py RFB_SOCKET CONTROL_SOCKET`
    hold(sys.argv[1], sys.argv[2])
