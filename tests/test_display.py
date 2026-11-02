"""The display adapter: processes and the RFB socket are mocked; Delete runs against a real child process."""

import json
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from abu_decomp import DecompError, cli, display

W, H = 4, 2


def rfb_server(conn: socket.socket, pixels: list[bytes]) -> None:
    """Speaks RFB 3.8 no-auth over `conn`; the Nth framebuffer request is answered with pixels[N] (BGRX bytes)."""

    def need(count: int) -> bytes:
        data = b""
        while len(data) < count:
            data += conn.recv(count - len(data))
        return data

    conn.sendall(b"RFB 003.008\n")
    assert need(12) == b"RFB 003.008\n"
    conn.sendall(b"\x01\x01")
    assert need(1) == b"\x01"
    conn.sendall(struct.pack(">I", 0))
    need(1)
    conn.sendall(struct.pack(">HH", W, H) + bytes(16) + struct.pack(">I", 4) + b"test")
    need(20)  # SetPixelFormat
    need(4 + 12)  # SetEncodings: raw, cursor, pointer position
    for frame in pixels:
        assert need(10)[0] == 3
        cursor = struct.pack(">HHHHi", 0, 0, 1, 1, display.CURSOR) + bytes(4) + b"\x80"
        conn.sendall(struct.pack(">BxH", 0, 2) + cursor + struct.pack(">HHHHi", 0, 0, W, H, display.RAW) + frame)
    conn.close()


def served(pixels: list[bytes]):
    near, far = socket.socketpair()
    thread = threading.Thread(target=rfb_server, args=(far, pixels), daemon=True)
    thread.start()
    return near, thread


def test_frame_drops_the_stale_first_update(tmp_path: Path) -> None:
    stale, fresh = bytes(W * H * 4), bytes([0, 0, 255, 0]) * (W * H)  # BGRX: red
    near, thread = served([stale, fresh])
    image = display.Rfb(near).frame(drop_first=True)
    thread.join()
    assert image.size == (W, H)
    assert image.getpixel((0, 0)) == (255, 0, 0)


def test_capture_writes_a_png_and_reports_black(tmp_path: Path) -> None:
    (tmp_path / "d-aa").mkdir()
    record = display.Record("d-aa", W, H, "sock", {}, str(tmp_path / "d-aa"))
    (tmp_path / "d-aa" / "display.json").write_text(json.dumps(record.__dict__))
    black = bytes(W * H * 4)
    near, thread = served([black, black])
    linux = display.Linux(tmp_path, connect=lambda _path: near)
    out = tmp_path / "frame.png"
    answer = linux.capture("d-aa", out)
    thread.join()
    assert answer == {"path": str(out), "width": W, "height": H, "non_black": False}
    assert Image.open(out).size == (W, H)


def test_rfb_refuses_a_server_without_none_security() -> None:
    near, far = socket.socketpair()
    far.sendall(b"RFB 003.008\n\x01\x02")
    with pytest.raises(DecompError, match="only None"):
        display.Rfb(near)


class FakeTool:
    pid = 0
    returncode = None

    def poll(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0


def fake_spawn(argv, env, **_kw):
    run = Path(env["XDG_RUNTIME_DIR"])
    if argv[0] == "labwc":
        (run / "wayland-0").touch()
    if argv[0] == "wayvnc" or argv[-1].endswith(display.HOLD):
        Path(argv[-1]).touch()
    fake_spawn.argv.append(argv)
    return FakeTool()


fake_spawn.argv = []


@pytest.fixture
def tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in display.PACKAGES:
        (bin_dir / name).touch()
    monkeypatch.setenv("PATH", str(bin_dir))
    fake_spawn.argv = []


def test_create_starts_labwc_randr_wayvnc_and_checks_the_size(tmp_path: Path, tools: None) -> None:
    near, thread = served([])
    linux = display.Linux(tmp_path / "root", spawn=fake_spawn, connect=lambda _path: near)
    (tmp_path / "root").mkdir()
    # the server announces W x H, so a request for another size is refused after the tools started
    with pytest.raises(DecompError, match=f"is {W}x{H}, not 1280x720"):
        linux.create("1280x720")
    thread.join()
    assert [a[0] for a in fake_spawn.argv] == ["labwc", "wlr-randr", "wayvnc"]
    assert fake_spawn.argv[1][-2:] == ["--custom-mode", "1280x720"]
    near, thread = served([])
    ok = display.Linux(tmp_path / "root", spawn=fake_spawn, connect=lambda _path: near).create(f"{W}x{H}")
    thread.join()
    assert ok.identity.startswith("d-") and set(ok.pids) == {"labwc", "wayvnc", "holder"}
    assert linux.describe(ok.identity) == ok


def test_create_names_a_missing_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(DecompError, match="labwc is not installed"):
        display.Linux(tmp_path).create("1280x720")


def test_delete_stops_a_real_process_and_removes_the_folder(tmp_path: Path) -> None:
    labwc = tmp_path / "labwc"  # comm is the file name: a stand-in the adapter accepts as labwc
    shutil.copy(shutil.which("sleep") or "/bin/sleep", labwc)
    child = subprocess.Popen([str(labwc), "60"])
    directory = tmp_path / "d-bb"
    (directory / "run").mkdir(parents=True)
    (directory / "run" / "wayland-0").touch()
    record = display.Record("d-bb", W, H, "sock", {"labwc": child.pid}, str(directory))
    (directory / "display.json").write_text(json.dumps(record.__dict__))
    assert display.Linux(tmp_path).delete("d-bb") == record
    assert child.wait() == -15
    assert not directory.exists()


def test_describe_refuses_an_unknown_or_escaping_identity(tmp_path: Path) -> None:
    for subject in ("d-none", "../x"):
        with pytest.raises(DecompError, match="no display"):
            display.Linux(tmp_path).describe(subject)


def test_windows_create_records_the_guest_size_and_delete_stops_nothing(tmp_path: Path) -> None:
    near, thread = served([])
    windows = display.Windows(tmp_path, connect=lambda _address: near)
    record = windows.create("ignored", "guest.example.lan:5900")
    thread.join()
    assert (record.width, record.height, record.socket, record.pids) == (W, H, "tcp:guest.example.lan:5900", {})
    assert windows.describe(record.identity) == record
    assert windows.delete(record.identity) == record
    assert not (tmp_path / record.identity).exists()


def test_windows_capture_takes_the_first_update(tmp_path: Path) -> None:
    (tmp_path / "d-aa").mkdir()
    record = display.Record("d-aa", W, H, "tcp:g:1", {}, str(tmp_path / "d-aa"))
    (tmp_path / "d-aa" / "display.json").write_text(json.dumps(record.__dict__))
    near, _ = served([bytes([0, 0, 255, 0]) * (W * H)])
    # the stub server answers the SetPixelFormat of a fresh handshake, so the connect factory hands out one socket
    done = display.Windows(tmp_path, connect=lambda _a: near).capture("d-aa", tmp_path / "f.png")
    assert done["non_black"] is True


def test_windows_redials_after_an_eof_at_server_init(tmp_path: Path) -> None:
    dead, far = socket.socketpair()
    far.close()  # the guest hangs up right after accepting: EOF at ServerInit
    live, thread = served([])
    dials = iter([dead, live])
    record = display.Windows(tmp_path, connect=lambda _a: next(dials)).create("x", "g:5900")
    thread.join()
    assert record.width == W


def test_windows_create_needs_an_endpoint_and_linux_refuses_one(tmp_path: Path) -> None:
    with pytest.raises(DecompError, match="--endpoint HOST:PORT"):
        display.Windows(tmp_path).create("1x1")
    with pytest.raises(DecompError, match="for Windows"):
        display.Linux(tmp_path).create("1x1", "g:1")


def test_backend_picks_by_name(tmp_path: Path) -> None:
    assert isinstance(display.backend(tmp_path, "windows"), display.Windows)
    with pytest.raises(DecompError, match="no display backend"):
        display.backend(tmp_path, "plan9")


def test_size_is_width_by_height() -> None:
    assert display.parse_size("1280x720") == (1280, 720)
    for bad in ("1280", "0x5", "ax1", ""):
        with pytest.raises(DecompError):
            display.parse_size(bad)


def test_decomp_display_describe_prints_the_record(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directory = tmp_path / "d-cc"
    directory.mkdir()
    record = display.Record("d-cc", W, H, "sock", {}, str(directory))
    (directory / "display.json").write_text(json.dumps(record.__dict__))
    assert cli.main(["display", "--dir", str(tmp_path), "describe", "d-cc"]) == 0
    assert '"d-cc"' in capsys.readouterr().out


def listening(raw: bytes) -> tuple[socket.socket, threading.Thread, bytearray]:
    """An RFB server that records every client byte after SetEncodings and answers each framebuffer request."""
    near, far = socket.socketpair()
    seen = bytearray()

    def serve() -> None:
        def need(count: int) -> bytes:
            data = b""
            while len(data) < count:
                data += far.recv(count - len(data))
            return data

        far.sendall(b"RFB 003.008\n")
        need(12)
        far.sendall(b"\x01\x01")
        need(1)
        far.sendall(struct.pack(">I", 0))
        need(1)
        far.sendall(struct.pack(">HH", W, H) + bytes(16) + struct.pack(">I", 4) + b"test")
        need(20 + 4 + 12)
        while head := far.recv(1):  # until the client hangs up
            if head[0] == 3:  # a framebuffer request: recorded as one byte, answered with a frame
                need(9)
                seen.append(3)
                far.sendall(struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", 0, 0, W, H, display.RAW) + raw)
            else:
                seen.extend(head + need({4: 7, 5: 5}[head[0]]))
        far.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return near, thread, seen


def make_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A display record whose directory is relative to the cwd: a Unix socket path in tmp_path is past 108 bytes."""
    monkeypatch.chdir(tmp_path)
    directory = Path("d-aa")
    directory.mkdir()
    record = display.Record("d-aa", W, H, "sock", {}, str(directory))
    (directory / "display.json").write_text(json.dumps(record.__dict__))
    return directory


def point(mask: int, x: int, y: int) -> bytes:
    return struct.pack(">BBHH", 5, mask, x, y)


def test_strokes_go_out_in_order_on_the_one_connection_and_wait_for_the_server() -> None:
    # focus click, key, click: the click that focuses a window only helps the key if both ride one connection
    near, thread, seen = listening(bytes(W * H * 4))
    strokes = display.plan(
        [{"click": {"x": 3, "y": 1}}, {"key": "Control_L+c"}, {"pointer": {"x": 2, "y": 0, "buttons": 4}}], W, H
    )
    display.Rfb(near).strokes(strokes)
    near.close()
    thread.join()
    key = struct.pack(">BBxxI", 4, 1, 0xFFE3) + struct.pack(">BBxxI", 4, 1, 0x63)
    key += struct.pack(">BBxxI", 4, 0, 0x63) + struct.pack(">BBxxI", 4, 0, 0xFFE3)
    # a request (3) follows every change of the button mask, and the last one ends the call
    assert bytes(seen) == (
        point(0, 3, 1) + point(1, 3, 1) + b"\x03" + point(0, 3, 1) + b"\x03" + key + point(4, 2, 0) + b"\x03\x03"
    )


def serve_holder(path: Path, answer: bytes) -> tuple[threading.Thread, list[bytes]]:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen()
    got: list[bytes] = []

    def run() -> None:
        conn, _ = server.accept()
        with conn:
            got.append(conn.makefile("rb").readline())
            conn.sendall(answer)
        server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, got


def test_input_hands_the_strokes_to_the_displays_holder_in_one_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = make_record(tmp_path, monkeypatch)
    thread, got = serve_holder(directory / display.HOLD, b"ok\n")
    events = [{"click": {"x": 3, "y": 1}}, {"key": "k"}, {"click": {"x": 3, "y": 1}}]
    answer = display.Linux(Path.cwd()).input("d-aa", events)
    thread.join()
    assert answer == {"events": 3, "strokes": 3 + 2 + 3}
    assert len(got) == 1 and json.loads(got[0]) == [list(s) for s in display.plan(events, W, H)]


def test_input_names_a_holder_that_is_gone_or_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = make_record(tmp_path, monkeypatch)
    with pytest.raises(DecompError, match="holder did not answer"):
        display.Linux(Path.cwd()).input("d-aa", [{"key": "k"}])
    thread, _ = serve_holder(directory / "hold.sock.2", b"error: RFB closed\n")
    (directory / display.HOLD).write_text("")  # not a socket: the connect fails
    with pytest.raises(DecompError, match="holder did not answer"):
        display.Linux(Path.cwd()).input("d-aa", [{"key": "k"}])
    (directory / display.HOLD).unlink()
    (directory / "hold.sock.2").rename(directory / display.HOLD)
    with pytest.raises(DecompError, match="holder answered 'error: RFB closed'"):
        display.Linux(Path.cwd()).input("d-aa", [{"key": "k"}])
    thread.join()


def test_hold_keeps_one_rfb_connection_for_every_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    near, _, seen = listening(bytes(W * H * 4))
    dials: list[str] = []
    monkeypatch.setattr(display, "connect_unix", lambda path: dials.append(path) or near)
    monkeypatch.chdir(tmp_path)
    ctl = Path("hold.sock")
    holder = threading.Thread(target=display.hold, args=("rfb", str(ctl)), daemon=True)
    holder.start()
    deadline = time.monotonic() + 5
    while not ctl.exists() and time.monotonic() < deadline:  # the test's own bound on its thread, not the product's
        holder.join(0.01)

    def send(strokes: list) -> bytes:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.connect(str(ctl))
        conn.sendall(json.dumps(strokes).encode() + b"\n")
        return conn.makefile("rb").readline()

    assert send(display.plan([{"click": {"x": 3, "y": 1}}], W, H)) == b"ok\n"
    assert send(display.plan([{"key": "k"}], W, H)) == b"ok\n"
    assert dials == ["rfb"]  # two requests, one connection
    assert bytes(seen) == point(0, 3, 1) + point(1, 3, 1) + b"\x03" + point(0, 3, 1) + b"\x03\x03" + (
        struct.pack(">BBxxI", 4, 1, 0x6B) + struct.pack(">BBxxI", 4, 0, 0x6B) + b"\x03"
    )
    assert send([]).startswith(b"ok")


def test_input_refuses_bad_events_before_connecting(tmp_path: Path) -> None:
    (tmp_path / "d-aa").mkdir()
    record = display.Record("d-aa", W, H, "sock", {}, str(tmp_path / "d-aa"))
    (tmp_path / "d-aa" / "display.json").write_text(json.dumps(record.__dict__))

    def never(_path: str) -> socket.socket:
        raise AssertionError("connected")

    linux = display.Linux(tmp_path, connect=never)
    bad = [
        ([], "non-empty"),
        ([{"key": "Hyper"}], "not a known key"),
        ([{"key": "a", "text": "b"}], "exactly one"),
        ([{"text": "\x07"}], "no keysym"),
        ([{"click": {"x": W, "y": 0}}], "inside 4x2"),
        ([{"click": {"x": 0, "y": 0, "button": "side"}}], "button must"),
        ([{"pointer": {"x": 0, "y": 0, "buttons": 999}}], "mask"),
    ]
    for events, why in bad:
        with pytest.raises(DecompError, match=why):
            linux.input("d-aa", events)


def test_keysyms() -> None:
    assert display.chord("F10") == [0xFFC7]
    assert display.chord("+") == [0x2B] and display.chord("Control_L+plus") == [0xFFE3, 0x2B]
    assert display.chord("Control_L+") is None
    assert display.keysym_of("\n") == 0xFF0D and display.keysym_of("€") == 0x010020AC


def test_windows_input_answers_unsupported() -> None:
    with pytest.raises(DecompError, match="Unsupported") as raised:
        display.Windows(Path("w")).input("d", [{"key": "a"}])
    assert raised.value.key == "display.unsupported"


def test_decomp_display_input_refuses_non_json_events(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["display", "--dir", str(tmp_path), "input", "d-cc", "--events", "{"]) != 0
    assert "not JSON" in capsys.readouterr().out


def test_rfb_names_a_server_that_never_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    near, far = socket.socketpair()
    near.settimeout(0.05)  # the declared RFB_SECONDS, shortened: the far end stays silent
    with pytest.raises(DecompError, match="RFB dial: wayvnc sent nothing"):
        display.Rfb(near)
    far.close()


def test_create_names_the_tool_that_never_became_ready(
    tmp_path: Path, tools: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(display, "START_SECONDS", 0.05)
    (tmp_path / "root").mkdir()

    def labwc_only(argv, env, **kw):
        return (
            FakeTool()
            if argv[0] != "labwc"
            else subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **kw)
        )

    linux = display.Linux(tmp_path / "root", spawn=labwc_only, connect=lambda _path: None)
    with pytest.raises(DecompError, match="labwc never became ready: wayland-0 did not appear"):
        linux.create("1280x720")
    assert list((tmp_path / "root").iterdir()) == []


def test_held_keys_are_planned_with_their_own_release() -> None:
    held = display.plan([{"keydown": "Left"}, {"keyup": "Left"}], W, H)
    assert held == [("key", 0xFF51, 1, 0), ("key", 0xFF51, 0, 0)]
    with pytest.raises(DecompError, match="not a known key"):
        display.plan([{"keydown": "Hyper"}], W, H)


class Shown:
    """A display backend that records what invoke asked of it; the display folder is a real tmp folder."""

    def __init__(self, root: Path) -> None:
        self.root, self.deleted = root, []

    def create(self, size: str, endpoint: str | None = None) -> display.Record:
        (self.root / "d-live").mkdir()
        return display.Record("d-live", W, H, "sock", {}, str(self.root / "d-live"))

    def environment(self, subject: str) -> dict[str, str]:
        return display.Linux.environment(self, subject)

    def describe(self, subject: str) -> display.Record:
        return display.Record(subject, W, H, "sock", {}, str(self.root / subject))

    def delete(self, subject: str) -> display.Record:
        self.deleted.append(subject)
        return self.describe(subject)


def live_profile(tmp_path: Path) -> Path:
    """The rig's tas section shape: the port's args name the ROM, --no-dialog, a --cpak and the {user_dir} token."""
    (tmp_path / "rom.z64").write_bytes(b"rom")
    (tmp_path / "ragewars").write_text("")
    profile = tmp_path / "profile.json"
    tas_section = {
        "movies": "movies",
        "runs": "runs",
        "port": "ragewars",
        "rom": "rom.z64",
        "args": ["--rom", str(tmp_path / "rom.z64"), "--no-dialog", "--cpak", "off", "--user-dir", "{user_dir}"],
        "env": {"SDL_VIDEODRIVER": "offscreen", "SDL_AUDIODRIVER": "dummy"},
    }
    profile.write_text(json.dumps({"schema_version": 1, "name": "t", "tas": tas_section}))
    return profile


def test_invoke_without_a_command_launches_the_profiles_game(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shown, ran = Shown(tmp_path), []
    monkeypatch.setattr(display, "backend", lambda root, kind: shown)
    monkeypatch.setattr(
        subprocess, "run", lambda argv, env, check: ran.append((argv, env)) or subprocess.CompletedProcess(argv, 0)
    )
    assert cli.main(["--profile", str(live_profile(tmp_path)), "display", "invoke"]) == 0
    (argv, env), user = ran[0], tmp_path / "d-live" / "game" / "user"
    assert argv[0] == str(tmp_path / "ragewars")
    assert argv[argv.index("--rom") + 1] == str(tmp_path / "rom.z64")
    assert argv[argv.index("--user-dir") + 1] == str(user) and user.is_dir()
    # the Controller Pak screen: a card in the slot, never the replays' empty slot that stops on NO CONTROLLER PAK FOUND
    assert argv.count("--cpak") == 1 and argv[argv.index("--cpak") + 1] == str(user / "controller-pak.mpk")
    assert "--no-dialog" in argv
    assert env["SDL_AUDIODRIVER"] == "dummy" and env["SDL_VIDEODRIVER"] == "wayland"  # the display's driver wins
    assert env["WAYLAND_DISPLAY"] == "wayland-0" and shown.deleted == ["d-live"]


def test_invoke_with_a_command_runs_it_and_needs_no_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shown, ran = Shown(tmp_path), []
    monkeypatch.setattr(display, "backend", lambda root, kind: shown)
    monkeypatch.setattr(
        subprocess, "run", lambda argv, env, check: ran.append(argv) or subprocess.CompletedProcess(argv, 3)
    )
    monkeypatch.setattr(cli, "_profile", lambda path: pytest.fail("a command needs no profile"))
    assert cli.main(["display", "invoke", "--", "foot", "-e", "true"]) == 0
    assert ran == [["foot", "-e", "true"]] and shown.deleted == ["d-live"]
