"""The site's own display adapter: one interface (Create, Describe, Capture, Delete), a Linux backend now.

Linux holds a headless Wayland compositor (labwc) and a VNC server on a Unix socket (wayvnc) in a private run directory,
and captures one frame by speaking RFB to that socket. Windows is an explicit refusal until its backend lands.
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
PACKAGES = ("labwc", "wayvnc", "wlr-randr")
# Seconds a stopped tool gets to leave on SIGTERM before SIGKILL: a bound the adapter declares, not a wait for effect.
GRACE_SECONDS = 5.0
OUTPUT = "HEADLESS-1"
RAW, CURSOR, POINTER_POS = 0, -239, -232


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
    def create(self, size: str) -> Record: ...
    def describe(self, subject: str) -> Record: ...
    def capture(self, subject: str, path: Path) -> dict[str, object]: ...
    def delete(self, subject: str) -> Record: ...


def parse_size(size: str) -> tuple[int, int]:
    """'1280x720' -> (1280, 720); anything else is a refusal."""
    width, sep, height = size.partition("x")
    if not sep or not width.isdecimal() or not height.isdecimal() or int(width) < 1 or int(height) < 1:
        raise DecompError("display.size", f"size {size!r} is not WIDTHxHEIGHT", "pass --size 1280x720")
    return int(width), int(height)


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
            chunk = self.conn.recv(count - len(data))
            if not chunk:
                raise DecompError("display.rfb", f"RFB closed after {len(data)} of {count} bytes")
            data += chunk
        return bytes(data)

    def update(self) -> Image.Image:
        """Ask for the whole screen and answer the next FramebufferUpdate as an image of the screen."""
        self.conn.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, self.width, self.height))
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

    def frame(self) -> Image.Image:
        """wayvnc's first update is the buffer kept since its last client left (QUIRKS 4): drop it, use the second."""
        self.update()
        return self.update()


def connect_unix(path: str) -> socket.socket:
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.connect(path)
    except OSError as exc:
        conn.close()
        raise DecompError("display.connect", f"cannot reach {path}: {exc}") from exc
    return conn


# ---------------------------------------------------------------- Linux


def is_tool(pid: int, name: str) -> bool:
    """Whether the pid still is that tool (a record outlives its processes; a pid number is reused)."""
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip() == name
    except OSError:
        return False


class Linux:
    """labwc (headless, pixman) + wlr-randr to the size + wayvnc on rfb.sock, all in <root>/<id>/."""

    def __init__(
        self,
        root: Path,
        spawn: Callable[..., subprocess.Popen] = subprocess.Popen,
        connect: Callable[[str], socket.socket] = connect_unix,
    ) -> None:
        self.root = root
        self.spawn = spawn
        self.connect = connect

    def create(self, size: str) -> Record:
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
            if self._start(directory, mode, client).wait() != 0:
                raise DecompError("display.start", "wlr-randr failed: " + self._log(directory, "wlr-randr"))
            serve = ["wayvnc", "--config", str(config / "wayvnc"), "--unix-socket", "--max-fps=60", str(rfb)]
            wayvnc = self._start(directory, serve, client)
            pids["wayvnc"] = wayvnc.pid
            self._await(rfb, wayvnc, directory, "wayvnc")
            record = Record(identity, width, height, str(rfb), pids, str(directory))
            self._check_size(record)
            (directory / "display.json").write_text(json.dumps(asdict(record)))
        except BaseException:
            self._stop(pids)
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return record

    def _start(self, directory: Path, argv: list[str], env: dict[str, str]) -> subprocess.Popen:
        with (directory / f"{argv[0]}.log").open("wb") as log:
            return self.spawn(argv, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)

    @staticmethod
    def _log(directory: Path, name: str) -> str:
        return (directory / f"{name}.log").read_text(errors="replace")[-400:].strip()

    def _await(self, path: Path, process: subprocess.Popen, directory: Path, name: str) -> None:
        """Block on inotify for `path` and on the tool's exit; no sleep, no timeout (the job's own cap bounds it)."""
        watcher = inotify.Watcher()
        try:
            watcher.add(path.parent, inotify.CREATE | inotify.MOVED_TO)
            while not path.exists():
                if process.poll() is not None:
                    failure = f"{name} exited {process.returncode}: {self._log(directory, name)}"
                    raise DecompError("display.start", failure)
                select.select([watcher.fd], [], [])
                watcher.read()
        finally:
            watcher.close()

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
        path = self.root / subject / "display.json"
        if not subject.startswith("d-") or "/" in subject or not path.is_file():
            raise DecompError("display.notfound", f"no display {subject!r}", "create one with `decomp display create`")
        return Record(**json.loads(path.read_text()))

    def capture(self, subject: str, path: Path) -> dict[str, object]:
        record = self.describe(subject)
        if not path.is_absolute():
            raise DecompError("display.path", "capture needs an absolute --path")
        conn = self.connect(record.socket)
        try:
            image = Rfb(conn).frame()
        finally:
            conn.close()
        partial = path.with_name("." + path.name + ".part")
        image.save(partial, "PNG")
        partial.replace(path)
        black = image.getbbox() is None
        return {"path": str(path), "width": image.width, "height": image.height, "non_black": not black}

    def delete(self, subject: str) -> Record:
        record = self.describe(subject)
        self._stop(record.pids)
        shutil.rmtree(record.directory)
        return record

    def _stop(self, pids: dict[str, int]) -> None:
        """SIGTERM every tool, newest first, and wait on its pidfd; SIGKILL what outlives GRACE_SECONDS."""
        for name in reversed(list(pids)):
            pid = pids[name]
            if not is_tool(pid, name):
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
    """The Windows backend is RUNNER-LIVE-DISPLAY's; until it lands every verb refuses."""

    def _refuse(self, *_args: object) -> object:
        raise DecompError("display.unsupported", "the Windows display backend is not built yet")

    create = describe = capture = delete = _refuse  # type: ignore[assignment]


def backend(root: Path | None = None) -> Backend:
    base = root or Path.home() / ".local" / "state" / "abu-decomp" / "displays"
    if sys.platform == "linux":
        base.mkdir(parents=True, exist_ok=True)
        return Linux(base)
    if sys.platform == "win32":
        return Windows()  # type: ignore[return-value]
    raise DecompError("display.unsupported", f"no display backend for {sys.platform}")
