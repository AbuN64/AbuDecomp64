"""The display adapter: processes and the RFB socket are mocked; Delete runs against a real child process."""

import json
import shutil
import socket
import struct
import subprocess
import threading
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
    image = display.Rfb(near).frame()
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

    def wait(self) -> int:
        return 0


def fake_spawn(argv, env, **_kw):
    run = Path(env["XDG_RUNTIME_DIR"])
    if argv[0] == "labwc":
        (run / "wayland-0").touch()
    if argv[0] == "wayvnc":
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
    assert ok.identity.startswith("d-") and set(ok.pids) == {"labwc", "wayvnc"}
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


def test_windows_backend_refuses_every_verb() -> None:
    for verb in (lambda w: w.create("1x1"), lambda w: w.describe("d"), lambda w: w.delete("d")):
        with pytest.raises(DecompError, match="not built yet"):
            verb(display.Windows())


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
