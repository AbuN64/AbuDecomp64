"""`decomp tas serve`: the Runner site's server (orch plans/GAME-RUNNER.md, sections 7 and 10a; lanes/RUNNER-REDO).
The page (runner.html) is the site shell: header, function map sidebar, and the routes feed / function / stream, each
region a module under static/site. The record contract, the live feed and the region endpoints are module site_core
(`/api/contract`, `/api/feed`, `/api/feed/stream` Server-Sent Events, `/api/map|fn|stream|feedback/...`); the feed
is fed from the same run rows live.json holds, at the moment Live writes them.
Section `tas.serve`: {"listen": "HOST:PORT", "jobs": N, "inbox": path, "states": path, "pin": path, "keep_days": days,
"phase_frame_minutes": m, "port_source": path, "stream_every": polls, "wall_finished": N (required: the finished
runs the /wall stream opens with)}; `lanes`, `invocations` and `claims` are still accepted (the bake reads the last
two). Every run (started here, by `tas play` in a shell, or by `play --all`) is a
folder under `runs/plays` whose `run.json` the port's run writes (module tas); the page sees all of them: the port's own
latest picture (`view.bmp`), its log and its sound (`audio.pcm`, served as a growing WAV).
The page is static files: `/data/*` serves `<tas.runs>/site/` (module tas_bake writes it; a `.gz` sibling goes out to
a client that accepts gzip, hashed names are immutable). The one file serve writes there is `live.json`: the running,
queued and newest finished runs, rewritten from an inotify watch on the run folders (class Live), never per request.
(Each route is a module `serve_*`: pages, files, api, phase, run, video, audio, feed, actions; this module holds
the Server, Live and Reach they read, the route tables and `serve`.)
`/video/RUN` is the run's sampled JPEG stream and its one picture path (no still route of the latest picture);
`/wall` one multiplexed stream of 192 px thumbs of every running run;
the live reach stream (class Reach, GAME-RUNNER 10a) is the play's (module tas_reach): `tas play` reads the port's
datagrams on `<input socket>.reach` and writes RUN/reach.bin, serve never does. Serve is a reader of the play's forward
on `<input socket>.reach.live` (one reader per socket): it keeps a cumulative bitset and rides it in-band on `/video` (a
record whose length field has REACH_FLAG set; a new viewer gets one full record first); a finished run's /video sends
the reach.bin the play left.
Any running run can be taken over by live input (`/api/take`, `/api/input`: datagrams to the run's input socket,
tas.input_address), `/api/play` without a movie starts a human run, and `/api/play` with `from` + `at` retries a run
from a poll (tas.play `source`/`take_at`); one run is driven by live input at a time. `/api/pin` keeps a run; every
hour runs older than `keep_days` (default 3) that are neither pinned nor their movie's newest are deleted
(tas.archive). `inbox` is the integration train's inbox, `states` the decomp progress states, `pin` the git folder of
the decomp source the port was built from. Serve gates the regression corpus (module tas_corpus) whenever the port
binary is a build it has not been gated on and nothing runs. Function state (exact / fuzzy / none) is read from the
decomp source, as the progress states define it. A run played with the oracle (module tas_oracle) also shows the
original game: its latest picture (/oracle/<id>), both sides' pictures at each compared poll (/shot/<id>) and the
scores (/diff/<id>).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib
from collections import OrderedDict, deque
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from PIL import Image

from abu_decomp import (
    DecompError,
    evidence,
    inotify,
    serve_actions,
    serve_api,
    serve_feed,
    serve_files,
    serve_http,
    serve_pages,
    serve_phase,
    serve_run,
    serve_video,
    serve_wake,
    site_core,
    site_enhanced,
    site_map,
    tas,
    tas_board,
    tas_corpus,
    tas_coverage,
    tas_health,
    tas_oracle,
    tas_phasemap,
    tas_points,
    tas_reach,
    tas_seat,
    tas_triage,
)
from abu_decomp.config import Profile
from abu_decomp.serve_audio import AUDIO_CHUNK_BYTES, AUDIO_LEAD_SECONDS, wav_header
from abu_decomp.serve_pace import RELEASED, driven, realtime
from abu_decomp.serve_video import (
    REACH_FLAG,
    VIDEO_FPS,
    VIDEO_HEADER,
    WALL_FPS,
    WALL_TABLE,
)

_SERVE_KEYS = frozenset(
    {
        "listen",
        "jobs",
        "inbox",
        "states",
        "pin",
        "lanes",
        "keep_days",
        "invocations",
        "claims",
        "phase_frame_minutes",
        "port_source",
        "enhanced",
        "stream_every",
        "wall_finished",
    }
)
RECENT_SHOWN = 24  # newest finished runs in live.json (all running and queued ones are always there)
HISTORY_PAGE = 60  # runs per /api/runs page
GZIP_KEPT = 16  # compressed bodies kept by tag, so a repeat fetch of an unchanged body costs no compression
STOP_GRACE = 3.0  # seconds a stopped run gets before its process group is killed
UNDEPLOYED = "undeployed"  # the mark of a server started from a source tree, not from a deployed toolkit
WALL_WIDTH = 192
LIVE_GAP_SECONDS = 0.2  # live.json is rewritten at most this often: a change shows well inside 0.5 s
LIVE_IDLE_SECONDS = 60.0  # idle live.json refresh (its clock only: a dead run owner is noticed by its pidfd)
REACH_KEPT_RECORDS = 2048  # records a slow /video client may lag behind before it is given a fresh full record
REACH_KEPT_BYTES = 1 << 20
REACH_RUNS_KEPT = 64  # finished runs whose relay stays in memory


def picture(data: bytes, kind: str = "PNG") -> tuple[bytes, int]:
    """One capture decoder for live BMP, archived BMP and PPM shots; PNG stills, JPEG video, THUMB wall tiles.

    Pillow owns offset, signed height, padded stride and channel conversion. Validate the capture contract
    before decoding, preserving the shim's reserved input acknowledgement independently of the pixel bytes.
    """
    seq = 0
    if data.startswith(b"BM") and len(data) >= 54:
        offset, dib = struct.unpack_from("<II", data, 10)
        width, signed_height, planes, bits, compression = struct.unpack_from("<iiHHI", data, 18)
        height = abs(signed_height)
        stride = ((width * bits + 31) // 32) * 4
        if dib < 40 or offset < 14 + dib or planes != 1 or bits not in (24, 32) or compression:
            raise ValueError("unsupported BMP layout")
        if not 0 < width <= 4096 or not 0 < height <= 4096 or len(data) < offset + stride * height:
            raise ValueError("incomplete BMP")
        seq = struct.unpack_from("<I", data, 6)[0]
    elif not (data.startswith(tas_oracle.PPM_HEAD) and len(data) == tas_oracle.PPM_SIZE):
        raise ValueError("unsupported capture")
    with Image.open(io.BytesIO(data)) as image:
        rgb = image.convert("RGB")
        out = io.BytesIO()
        if kind == "THUMB":  # the wall's tile: WALL_WIDTH px wide
            size = (WALL_WIDTH, max(1, round(rgb.height * WALL_WIDTH / rgb.width)))
            rgb.resize(size, Image.Resampling.BILINEAR, reducing_gap=2.0).save(out, "JPEG", quality=70)
        elif kind == "JPEG":
            rgb.save(out, "JPEG", quality=82, subsampling=0)
        else:
            rgb.save(out, "PNG", compress_level=1)
        return out.getvalue(), seq


def deployed_sha() -> str:
    """The commit the toolkit this interpreter runs from was deployed at: the `suite-sha` line the deploy's suite gate
    wrote to suite.txt in the venv (the link is followed, so a restart after the link flips reads the new toolkit)."""
    suite = Path(sys.prefix).resolve() / "suite.txt"
    try:
        text = suite.read_text()
    except OSError as error:
        raise DecompError("tas.serve", f"cannot read the deployed sha from {suite}: {error}") from error
    for line in text.splitlines():
        key, _, value = line.partition(": ")
        if key == "suite-sha" and re.fullmatch(r"[0-9a-f]{40}", value):
            return value
    raise DecompError("tas.serve", f"{suite} has no suite-sha line with a 40-hex commit")


def notify_ready(environ: dict[str, str] | None = None) -> bool:
    """Tells systemd (Type=notify) the page is listening, so `systemctl restart` returns only once it answers; False
    when the process was not started by systemd with a notify socket."""
    address = (os.environ if environ is None else environ).get("NOTIFY_SOCKET")
    if not address:
        return False
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.connect("\0" + address[1:] if address.startswith("@") else address)
        sock.sendall(b"READY=1")
    return True


class Server:
    def __init__(self, profile: Profile, deployed: str = UNDEPLOYED) -> None:
        self.profile = profile
        self.deployed = deployed
        self.pictures: OrderedDict[tuple[Path, str], tuple[int, int, bytes, int]] = OrderedDict()
        self.picture_lock = threading.Lock()
        self.cfg = tas.settings(profile)
        serve = dict(profile.section("tas").get("serve", {}))
        extra = serve.keys() - _SERVE_KEYS
        if extra:
            raise DecompError("tas.serve", f"unknown tas.serve keys: {', '.join(sorted(extra))}")
        self.listen = str(serve.get("listen", "127.0.0.1:8099"))
        self.jobs = int(serve.get("jobs", 4))
        # A play started here without the oracle pictures the port every `stream_every` polls (shots/port), so the
        # wall's tile and the stream show it; 0 or absent: no port pictures (an oracle play pictures both sides).
        self.stream_every = int(serve.get("stream_every", 0))
        if self.stream_every < 0:
            raise DecompError("tas.serve", f"stream_every is a poll count >= 0, not {self.stream_every}")
        # The /wall stream opens with the last thumb of this many newest finished runs (declared, no default).
        if "wall_finished" not in serve:
            raise DecompError("tas.serve", "wall_finished is not declared (how many finished runs the wall opens with)")
        self.wall_finished = int(serve["wall_finished"])
        if self.wall_finished < 0:
            raise DecompError("tas.serve", f"wall_finished is a run count >= 0, not {self.wall_finished}")
        self.inbox = profile.path("tas.serve.inbox", serve["inbox"]) if "inbox" in serve else None
        self.states = profile.path("tas.serve.states", serve["states"]) if "states" in serve else None
        self.pin_dir = profile.path("tas.serve.pin", serve["pin"]) if "pin" in serve else None
        self.keep_days = float(serve.get("keep_days", 3))
        self.port_source = (
            profile.path("tas.serve.port_source", serve["port_source"]) if "port_source" in serve else None
        )
        self.enhanced = site_enhanced.settings(serve.get("enhanced"))  # the Enhanced page's ButlerMaster reading
        self.phase = tas_phasemap.Phase(
            profile, self.cfg, self.pin_dir, self.table, float(serve.get("phase_frame_minutes", 10))
        )
        self.oracles: dict[str, tuple[int, Any]] = {}  # run id -> (record mtime, summary) of finished runs
        self.slots = threading.Semaphore(max(1, self.jobs))  # replays that run at once; the others wait queued
        self.quiet = threading.Condition()  # signalled when the runs or the build changed and none runs
        self.quiet_count = 0
        self.lock = threading.Lock()
        self.queued: dict[str, dict[str, Any]] = {}  # id -> placeholder row until the run writes its record
        self.refused: dict[str, dict[str, Any]] = {}  # id -> the row of a play refused at once (newest RECENT_SHOWN)
        self.cache: dict[Any, Any] = {}
        self.gating = False
        self.gz: dict[str, bytes] = {}
        self.site = self.cfg["runs"] / "site"  # the baked site (module tas_bake) and live.json
        self.live = Live(self)
        self.reach = Reach()
        self.feed = site_core.Feed()  # the site's typed events, fed by Live from the run rows
        self.snapshots = site_core.Snapshots(self.feed)  # the map and Enhanced answers, rebuilt on feed changes
        self.snapshots.register("map", lambda: site_map.answer(self, "GET", "", "", b""))
        self.snapshots.register("enhanced", lambda: site_enhanced.answer(self, "GET", "", "", b""))

    # --- cached facts ---

    def _cached(self, key: Any, make: Any) -> Any:
        """`make()` once per key: a key names the facts it was made from (a file's mtime, a run's status)."""
        if key not in self.cache:
            self.cache[key] = make()
        return self.cache[key]

    @staticmethod
    def stamp(path: Path | None) -> int:
        """The mtime of `path` (0 when it is not there): the part of a cache key that changes with the file."""
        try:
            return path.stat().st_mtime_ns if path else 0
        except OSError:
            return 0

    def settle(self) -> None:
        """The runs or the port build changed with no run going: wake the gate and the archive."""
        with self.quiet:
            self.quiet_count += 1
            self.quiet.notify_all()

    def settled(self, seen: int) -> int:
        """Block until `settle` was called after the count `seen`; the new count."""
        with self.quiet:
            self.quiet.wait_for(lambda: self.quiet_count != seen)
            return self.quiet_count

    def gzipped(self, tag: str, body: bytes) -> bytes:
        """`body` compressed, kept under its tag (the newest few) so an unchanged body is compressed once."""
        with self.lock:
            hit = self.gz.get(tag)
        if hit is None:
            packer = zlib.compressobj(5, zlib.DEFLATED, 31)
            hit = packer.compress(body) + packer.flush()
            with self.lock:
                self.gz[tag] = hit
                while len(self.gz) > GZIP_KEPT:
                    del self.gz[next(iter(self.gz))]
        return hit

    def pin_sha(self) -> str | None:
        def read() -> str | None:
            if not self.pin_dir:
                return None
            out = subprocess.run(
                ["git", "-C", str(self.pin_dir), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
            )
            return out.stdout.strip() or None

        return self._cached(("pin", self.stamp(self.pin_dir / ".git" if self.pin_dir else None)), read)

    def fn_state(self, name: str) -> str:
        """exact / fuzzy / none for one function, as the progress states define them."""

        def read() -> str:
            try:
                none = json.loads(self.states.read_text()) if self.states else {}
            except (OSError, ValueError):
                none = {}
            for version in (none.get("versions") or {}).values():
                if name in tas_board.none_names(version):
                    return "none"
            project = tas_triage._project(self.profile)
            site = tas_triage.definition_site(project, name) if project else None
            if site and project:
                fn = evidence.parse((project / site.rpartition(":")[0]).read_text(errors="replace")).get(name)
                return "fuzzy" if fn is not None and fn.kind == evidence.NM else "exact"
            return "none"

        return self._cached(("fn", name, self.stamp(self.states)), read)

    def movie_meta(self, path: Path) -> dict[str, Any]:
        """A movie's poll count and branch: `root` (the movie its chain of takes started from), `from` / `at` (the
        movie and poll a take left, from its header)."""

        def read() -> dict[str, Any]:
            try:
                header, polls = tas.decode(path.read_text())
            except (OSError, DecompError, ValueError):
                header, polls = {}, []
            at = header.get("at", "")
            return {
                "polls": len(polls),
                "root": tas.take_root(path.stem),
                "from": header.get("from") or None,
                "at": int(at) if at.isdigit() else None,
            }

        return self._cached(("movie", str(path), path.stat().st_mtime_ns), read)

    def slot_pool(self) -> dict[str, Any]:
        """The host slot pool (module slots): who holds which slots, who waits, and the last landed->live seconds
        (page-port.py's landings.jsonl beside the runs folder)."""
        from abu_decomp import slots

        out = slots.status()
        rows = []
        try:
            lines = (Path(self.cfg["runs"]).parent / "landings.jsonl").read_text().splitlines()[-20:]
            rows = [json.loads(line) for line in lines if line.strip()]
        except (OSError, ValueError, KeyError):
            pass
        out["landings"] = rows[-5:]
        return out

    def coverage(self) -> dict[str, Any]:
        """The game-code coverage table (runs/coverage.json, written by `tas play --all` / `tas coverage`): the summary
        and the two lists that drive work. {} when no table exists yet."""
        path = self.cfg["runs"] / tas_coverage.TABLE

        def read() -> dict[str, Any]:
            report = tas_coverage.read_table(self.cfg)
            return tas_coverage.lists(report) if report else {}

        try:
            return self._cached(("coverage", path.stat().st_mtime_ns), read)
        except OSError:
            return {}

    def table(self) -> dict[str, Any]:
        """The raw coverage table ({} before the first one), cached by its mtime."""
        path = self.cfg["runs"] / tas_coverage.TABLE
        try:
            return self._cached(("table", path.stat().st_mtime_ns), lambda: tas_coverage.read_table(self.cfg) or {})
        except OSError:
            return {}

    def gatekeeper(self) -> None:
        """Gate the corpus whenever the port binary is a build it was not gated on (page-port.py rebuilds it after each
        train round, a repin, a hand build; Live sees the file change and settles the server): only with no run live
        and the build not older than its pins. A profile naming no port has nothing to gate: that is one explicit
        state, printed once (the profile install restarts the server when it names one), not a failure every check."""
        if not self.cfg.get("port"):
            print("tas page: corpus gate not configured: the profile names no tas.port (no shared seat)", flush=True)
            return
        seen = 0
        while True:
            seen = self.settled(seen)
            try:
                if not tas_corpus.gate_due(self.cfg):
                    continue
                if any(row.get("status") == "running" for _, row in tas.run_rows(self.cfg)) or self.queued:
                    continue
                if tas_health.read(self.cfg, self.profile).get("pins_stale"):
                    continue
                self.gating = True
                result, _ = tas_corpus.gate(self.profile, jobs=self.jobs)
                print(
                    f"tas page: corpus gate {result['verdict']} {result['counts']} "
                    f"{result['wall_seconds']}/{result['cpu_seconds']} s (wall/cpu)",
                    flush=True,
                )
            except Exception as error:  # the next check tries again; the page shows the last gate file
                print(f"tas page: corpus gate failed to run: {error}", flush=True)
            finally:
                self.gating = False

    # --- the contract ---

    def capture(self, path: Path, kind: str = "PNG") -> tuple[int, bytes, int]:
        """Cache only the newest capture per path, bounded across viewers and archived runs."""
        with self.picture_lock:
            info = path.stat()
            key = (path, kind)
            hit = self.pictures.get(key)
            if hit and hit[:2] == (info.st_mtime_ns, info.st_size):
                self.pictures.move_to_end(key)
                return hit[0] // 1000, hit[2], hit[3]
            # Capture writers rename atomically. Stat the open file, not the name that may already be newer.
            with path.open("rb") as handle:
                info = os.fstat(handle.fileno())
                body, seq = picture(handle.read(), kind)
            self.pictures[key] = (info.st_mtime_ns, info.st_size, body, seq)
            self.pictures.move_to_end(key)
            while len(self.pictures) > 64:
                self.pictures.popitem(last=False)
            return info.st_mtime_ns // 1000, body, seq

    def row(self, run: Path, record: dict[str, Any]) -> dict[str, Any]:
        """One runs[] row (contract) of a run folder and its record."""
        running = record.get("status") == "running"
        taken = driven(run)
        return {
            "id": run.name,
            "movie": record.get("movie", ""),
            "status": "running" if running else "done",
            "speed": int(record.get("speed") or 0),
            "realtime": realtime(run, record),
            "poll": tas.last_poll(run / tas.HASH) if running else int(record.get("polls_reached") or 0),
            "held": tas.held_at(run) if running else None,  # the poll the gate holds it at, None while it runs
            "polls": int(record.get("polls") or 0),
            "outcome": None if running else record.get("outcome"),
            "subject": None if running else record.get("subject") or None,
            "started": record.get("started", 0),
            "ended": None if running else record.get("ended"),
            "label": record.get("label") or None,
            "port": record.get("port") or str(self.cfg.get("port") or ""),
            "input": "human" if taken else "movie",
            "took_at": self.took_at(run, record)
            if taken and (record.get("from") or record.get("input") != "human")
            else None,
            "stopped": bool(record.get("stopped")),
            "error": record.get("error") or None,
            "from": record.get("from") or None,
            "at": record.get("at"),
            "saved": record.get("saved") or None,
            "pinned": (run / tas.PINNED).exists(),
            "pak": record.get("pak") or "on",
            "cpak": record.get("cpak") or "none",
            "fps_unlock": bool(record.get("fps_unlock")),
            "pads": int(record.get("pads") or 1),
            "shown": record.get("shown") or None,
            "oracle": self.oracle(run, record, running),
        }

    def took_at(self, run: Path, record: dict[str, Any]) -> int | None:
        """The poll live input took a movie run over at (a retry: its `at`), None while the movie still drives it."""
        if record.get("at") is not None:
            return int(record["at"])
        return self._cached(("took", run.name, record.get("status")), lambda: tas.took_over_at(run) or None)

    def oracle(self, run: Path, record: dict[str, Any], running: bool) -> dict[str, Any] | None:
        if not isinstance(record.get("oracle"), dict):
            return None
        if running:
            return tas_oracle.summary(run, record)
        try:
            stamp = (run / tas.RECORD).stat().st_mtime_ns
        except OSError:
            stamp = 0
        hit = self.oracles.get(run.name)
        if not hit or hit[0] != stamp:
            hit = (stamp, tas_oracle.summary(run, record))
            self.oracles[run.name] = hit
        return hit[1]

    def port_format(self) -> int | None:
        """`format` of port.json in the page port's source (tas.serve.port_source); None when absent or unreadable."""
        if not self.port_source:
            return None
        try:
            value = json.loads((self.port_source / "port.json").read_text()).get("format")
        except (OSError, ValueError, AttributeError):
            return None
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def state(self) -> dict[str, Any]:
        """The body of live.json: running and queued runs and the newest finished ones (the contract run row minus
        its port path), the build and the clock."""
        rows = tas.run_rows(self.cfg)
        on_disk = {row["id"] for _, row in rows if "id" in row}
        with self.lock:
            for run_id in [k for k in self.queued if k in on_disk]:
                del self.queued[run_id]
            queued = list(self.queued.values()) + list(self.refused.values())
        live = [(run, row) for run, row in rows if row.get("status") == "running"]
        done = [(run, row) for run, row in rows if row.get("status") != "running"][-RECENT_SHOWN:]
        runs = [
            {k: v for k, v in r.items() if k != "port"}
            for r in [self.row(run, row) for run, row in live + done] + queued
        ]
        runs.sort(key=lambda r: r["started"])
        port = self.cfg.get("port")
        try:
            built = port.stat().st_mtime
        except (OSError, AttributeError):
            built = 0
        return {
            "now": time.time(),
            "build": {"port": str(port), "pin": self.pin_sha(), "built": built, "format": self.port_format()},
            "runs": runs,
        }

    def running_runs(self) -> list[Path]:
        """The folders of the runs going now."""
        return [run for run, row in tas.run_rows(self.cfg) if row.get("status") == "running"]

    def unfinished(self) -> set[Path]:
        """The run folders whose record is missing or running: the ones that still change."""
        plays = self.cfg["runs"] / "plays"
        try:
            runs = list(plays.iterdir())
        except OSError:  # no plays folder (yet, or any more)
            return set()
        return {
            run
            for run in runs
            if run.is_dir() and (tas.read_record(run) or {"status": "running"}).get("status") == "running"
        }

    def history(self, before: float | None, limit: int | None = None) -> dict[str, Any]:
        """Finished runs newest first, older than `before` (a `started` time), `limit` at most; `more` when older
        ones remain."""
        rows = [
            (run, row)
            for run, row in reversed(tas.run_rows(self.cfg))
            if row.get("status") != "running" and (before is None or float(row.get("started") or 0) < before)
        ]
        limit = max(1, min(limit or HISTORY_PAGE, HISTORY_PAGE))
        return {"runs": [self.row(run, row) for run, row in rows[:limit]], "more": len(rows) > limit}

    def movies(
        self, rows: list[tuple[Path, dict[str, Any]]] | None = None, inbox: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """Every kept movie's row (what the baked movies.json is made from)."""
        rows = tas.run_rows(self.cfg) if rows is None else rows
        inbox = tas_board.inbox_lines(self.inbox) if inbox is None else inbox
        try:
            standing = json.loads((self.cfg["runs"] / "standing.json").read_text())
        except (OSError, ValueError):
            standing = {}
        latest: dict[str, dict[str, Any]] = {}
        oracle_max: dict[str, float | None] = {}
        for _, row in rows:
            if row.get("status") == "done" and row.get("outcome") and not row.get("stopped") and not row.get("taken"):
                latest[row.get("movie", "")] = row
                if isinstance(row.get("oracle"), dict):
                    oracle_max[row.get("movie", "")] = row["oracle"].get("max_diff")
        folder = self.cfg["movies"]
        out = []
        for path in sorted(folder.glob("*.tas")) if folder.is_dir() else []:
            name = path.stem
            last = latest.get(name)
            known = last or standing.get(name) or {}
            fn = last.get("fn") if last else tas.fn_of(known)
            train = None
            if fn:
                hits = [row for row in inbox if fn in row["line"]]
                train = {"line": hits[-1]["line"], "mark": hits[-1]["mark"]} if hits else None
            meta = self.movie_meta(path)
            out.append(
                {
                    "name": name,
                    "polls": meta["polls"],
                    "root": meta["root"],
                    "from": meta["from"],
                    "at": meta["at"],
                    "outcome": known.get("outcome") or "",
                    "subject": known.get("subject") or None,
                    "reached": int(known.get("polls_reached") or 0),
                    "same_twice": last.get("same_twice") if last else None,
                    "fn": {"name": fn, "state": self.fn_state(fn)} if fn else None,
                    "train": train,
                    "last_run": last.get("ended") if last else None,
                    "label": last.get("label") if last else None,
                    "kind": known.get("kind") or None,
                    "oracle_max": oracle_max.get(name),
                }
            )
        return out

    def _placeholder(
        self,
        run_id: str,
        movie: str,
        speed: int,
        port: Path,
        label: str | None,
        pak: str | None = None,
        cpak: str | None = None,
        fps_unlock: bool = False,
    ) -> None:
        polls = 0
        with contextlib.suppress(OSError):
            polls = self.movie_meta(tas._movie(self.cfg, movie))["polls"]
        if pak is None:  # a replay keeps what its movie ran with
            with contextlib.suppress(DecompError, OSError):
                pak = tas.movie_pak(self.cfg, movie)
        if cpak is None:
            with contextlib.suppress(DecompError, OSError):
                cpak = tas.movie_cpak(self.cfg, movie)
        elif cpak == "off":
            cpak = "none"
        elif cpak == "on":
            cpak = "fresh"
        row = self._row(run_id, movie, speed, port, label, pak, cpak, fps_unlock, polls)
        with self.lock:
            self.queued[run_id] = row
        self.reach.expect(self.cfg["runs"] / "plays" / run_id, lambda: run_id in self.queued)
        self.live.touch()

    def _row(
        self,
        run_id: str,
        movie: str,
        speed: int,
        port: Path | None,
        label: str | None,
        pak: str | None,
        cpak: str | None,
        fps_unlock: bool,
        polls: int,
    ) -> dict[str, Any]:
        row = {"id": run_id, "movie": movie, "status": "queued", "speed": speed, "poll": 0, "polls": polls}
        row.update(cpak=cpak or "none", outcome=None, subject=None, started=time.time(), ended=None)
        row.update(
            label=label or None, port=str(port), input="movie", took_at=None, stopped=False, saved=None, pinned=False
        )
        row.update(pak=pak or "on", oracle=None, fps_unlock=fps_unlock, shown=None, **{"from": None, "at": None})
        return row

    def follow_seat(self) -> None:
        """The profile's shared seat is a live link the port deploy flips to each landed build: resolve it again at
        every play, not once at start (live: a site started before the flip kept the old build and refused every
        play as stale). The dict is updated in place, the holders of self.cfg see the new build."""
        port = tas.settings(self.profile)["port"]
        with self.lock:
            self.cfg["port"] = port

    def seat_refusal(self, port: str | None, movie: str, label: str | None) -> dict[str, Any] | None:
        """A play on the shared seat that cannot start ends at once: it is never queued, its refused row (error = the
        reason, shown by the feed as run.refused) is kept instead, and the answer carries the id and the reason. None
        when the play can start."""
        self.follow_seat()
        if port:
            return None
        try:
            tas_seat.check(self.cfg)
        except DecompError as error:
            reason = error.detail
        else:
            return None
        with self.lock:
            run_id = tas.new_run_id(self.cfg, movie)
            while run_id in self.queued or run_id in self.refused:
                run_id += "-q"
            now = time.time()
            row = self._row(run_id, movie, 0, None, label, None, None, False, 0)
            row.update(started=now)
            self._keep_refused(row, reason, now)
        self.live.touch()
        return {"id": run_id, "refused": reason}

    def _keep_refused(self, row: dict[str, Any], reason: str, now: float) -> None:
        """Keep `row` as a refused play (the feed shows run.refused with `reason`); the caller holds the lock."""
        row.update(status="done", outcome="refused", error=reason, ended=now)
        self.refused[row["id"]] = row
        for old in list(self.refused)[:-RECENT_SHOWN]:
            del self.refused[old]

    def _ended(self, run_id: str, error: Exception | None = None) -> None:
        """The start thread of queued run `run_id` is over. A thread that failed before the run wrote its record left
        nothing for the page to show: the queued row becomes a refused row carrying the reason (the journal states it
        too), so a play that cannot start is never left queued and never forgotten silently."""
        with self.lock:
            row = self.queued.pop(run_id, None)
            if error is not None and row is not None and not tas.read_record(self.run_dir(run_id) or Path()):
                reason = error.detail if isinstance(error, DecompError) else f"{type(error).__name__}: {error}"
                self._keep_refused(row, reason, time.time())
                print(f"tas page: play {run_id} refused: {reason}", flush=True)
        self.live.touch()

    def human_running(self) -> str | None:
        """The run live input drives now, if any (one at a time)."""
        for run, row in tas.run_rows(self.cfg):
            if row.get("status") == "running" and driven(run):
                return run.name
        with self.lock:
            return next((k for k, r in self.queued.items() if r.get("input") == "human"), None)

    def new_human(
        self,
        port: str | None,
        label: str | None,
        pak: str | None = None,
        cpak: str | None = None,
        fps_unlock: bool = False,
    ) -> dict[str, Any]:
        busy = self.human_running()
        if busy:
            raise DecompError("tas.live", f"live input already drives {busy}; stop it first")
        tas.check_label(label)
        tas.check_pak(pak)
        tas.check_cpak(cpak)
        movie = tas.human_movie_name()
        if refused := self.seat_refusal(port, movie, label):
            return refused
        exe = tas.with_port(self.cfg, port)["port"]
        with self.lock:
            run_id = tas.new_run_id(self.cfg, movie)
        self._placeholder(run_id, movie, 1, exe, label or tas.HUMAN_LABEL, pak or "on", cpak or "on", fps_unlock)
        with self.lock:
            self.queued[run_id]["input"] = "human"
        self.live.touch()

        def go() -> None:
            failure: Exception | None = None
            try:
                tas.play(
                    self.profile,
                    None,
                    speed=1,
                    run_id=run_id,
                    port=port or None,
                    label=label or None,
                    pak=pak,
                    cpak=cpak,
                    fps_unlock=fps_unlock,
                )
            except Exception as error:
                failure = error
            finally:
                self._ended(run_id, failure)

        threading.Thread(target=go, daemon=True).start()  # never takes a replay slot
        return {"id": run_id, "movie": movie}

    def take(self, run_id: str) -> dict[str, Any]:
        run = self.run_dir(run_id)
        if not run or not tas.running(run):
            raise DecompError("tas.take", f"no running run {run_id}")
        busy = self.human_running()
        if busy and busy != run_id:
            raise DecompError("tas.take", f"live input already drives {busy}; stop it first")
        (run / tas.TAKEN).write_text("page\n")
        (run / RELEASED).unlink(missing_ok=True)
        self.send(run, tas.TAKE_DATAGRAM)
        self.live.touch()
        return {"id": run_id, "taken": True}

    def release(self, run_id: str) -> dict[str, Any]:
        """Give a taken run back: it returns to turbo with neutral input, and another run may be taken. `taken` stays
        (the run is still saved as a take); `released` says live input no longer drives it."""
        run = self.run_dir(run_id)
        if not run or not driven(run):
            raise DecompError("tas.release", f"run {run_id} is not driven by live input")
        self.send(run, tas.RELEASE_DATAGRAM)
        (run / RELEASED).write_text("page\n")
        self.live.touch()
        return {"id": run_id, "released": True}

    def input(self, run_id: str, seq: int, buttons: int, x: int, y: int, pad: int = 0) -> dict[str, Any]:
        run = self.run_dir(run_id)
        if not run or not driven(run):
            raise DecompError("tas.input", f"run {run_id} is not driven by live input")
        if not (0 <= buttons <= 0xFFFF and -128 <= x <= 127 and -128 <= y <= 127 and seq >= 0):
            raise DecompError("tas.input", "buttons 0..0xffff, x and y -128..127, seq >= 0")
        pads = int((tas.read_record(run) or {}).get("pads") or 1)
        if not 0 <= pad < pads:
            raise DecompError("tas.input", f"pad {pad} of {pads}: this run has {pads} controller(s), numbered from 0")
        self.send(run, f"{seq} {buttons:x} {x} {y}{f' {pad}' if pad else ''}".encode())
        return {"ok": True}

    def gate(self, run_id: str, verb: str, count: int | None = None) -> dict[str, Any]:
        """Hold, step, seek or let go a running replay on its own poll count (the gate, shim/n64c_tas.c). While the
        gate may hold the run the supervisor applies no timeout (tas.GATED); `run` gives the timeout back."""
        run = self.run_dir(run_id)
        if not run or not tas.running(run):
            raise DecompError("tas.gate", f"no running run {run_id}")
        data = tas.gate_datagram(verb, count)
        if verb == "run":
            (run / tas.GATED).unlink(missing_ok=True)
        else:
            (run / tas.GATED).write_text(f"{verb}\n")
        self.send(run, data)
        self.live.touch()
        return {"id": run_id, "gate": verb, "count": count}

    def send(self, run: Path, data: bytes) -> None:
        try:
            tas.send_input(run, data)
        except OSError as error:
            raise DecompError("tas.input", f"the run's input socket is not there yet: {error}") from error

    def play(
        self,
        movie: str,
        speed: int,
        port: str | None = None,
        label: str | None = None,
        pak: str | None = None,
        oracle: bool = False,
        cpak: str | None = None,
        source: str | None = None,
        take_at: int | None = None,
        fps_unlock: bool = False,
        start: str | None = None,
        take: bool = False,
        hold_at: int | None = None,
    ) -> dict[str, Any]:
        if hold_at is not None and not movie:
            raise DecompError("tas.play", "hold_at holds a replay of a movie")
        if source:
            return self.retry(source, take_at, port, label, fps_unlock)
        if start and take and not movie:
            return self.from_point(start, port, label, fps_unlock)
        if start and not movie:
            movie, start = start, None  # the point alone, replayed like a movie
        if not movie:
            return self.new_human(port, label, pak, cpak, fps_unlock)
        tas.movie_polls(self.cfg, movie, start)  # refuse a missing movie or another ROM here, not in the thread
        if oracle:
            tas_oracle.required(self.profile)
        if not 0 <= speed <= 1000:
            raise DecompError("tas.play", f"speed is 0 (turbo) .. 1000, not {speed}")
        tas.check_label(label)
        tas.check_pak(pak)
        tas.check_cpak(cpak)
        if refused := self.seat_refusal(port, movie, label):
            return refused
        exe = tas.with_port(self.cfg, port)["port"]
        with self.lock:
            run_id = tas.new_run_id(self.cfg, movie)
            while run_id in self.queued:
                run_id += "-q"
        self._placeholder(run_id, movie, speed, exe, label, pak, cpak, fps_unlock)

        def go() -> None:
            failure: Exception | None = None
            self.slots.acquire()
            try:
                options = {
                    "port": port or None,
                    "label": label or None,
                    "pak": pak,
                    "cpak": cpak,
                    "oracle": oracle,
                    "oracle_pictures": oracle,  # a person asked from the page: the picture comparison is the point
                    "shots_every": 0 if oracle else self.stream_every,  # the oracle's pictures cover the port
                    "hold_at": hold_at,
                    "fps_unlock": fps_unlock,
                }
                tas.play(self.profile, movie, speed=speed, run_id=run_id, start=start, **options)
            except Exception as error:  # with a record the page shows it; without one the play is refused
                failure = error
            finally:
                self.slots.release()
                self._ended(run_id, failure)

        threading.Thread(target=go, daemon=True).start()
        return {"id": run_id}

    def from_point(self, start: str, port: str | None, label: str | None, fps_unlock: bool = False) -> dict[str, Any]:
        """A new live run at start point `start`: the point's polls at turbo, then live input (tas.play take)."""
        busy = self.human_running()
        if busy:
            raise DecompError("tas.live", f"live input already drives {busy}; stop it first")
        tas.check_label(label)
        point = tas_points.load(self.cfg, start)  # refuse a missing point here, not in the thread
        movie = tas.human_movie_name()
        if refused := self.seat_refusal(port, movie, label):
            return refused
        exe = tas.with_port(self.cfg, port)["port"]
        with self.lock:
            run_id = tas.new_run_id(self.cfg, movie)
        cpak, pak = point["header"].get("cpak", "none"), point["header"].get("pak", "on")
        self._placeholder(run_id, movie, 0, exe, label or tas.HUMAN_LABEL, pak, cpak, fps_unlock)
        with self.lock:
            self.queued[run_id].update(
                input="human",
                took_at=len(point["polls"]),
                polls=len(point["polls"]),
                pads=tas.pad_count(point["polls"]),
            )
        self.live.touch()

        def go() -> None:
            failure: Exception | None = None
            try:
                tas.play(
                    self.profile,
                    None,
                    run_id=run_id,
                    port=port or None,
                    label=label or None,
                    start=start,
                    take=True,
                    fps_unlock=fps_unlock,
                )
            except Exception as error:
                failure = error
            finally:
                self._ended(run_id, failure)

        threading.Thread(target=go, daemon=True).start()  # never takes a replay slot
        return {"id": run_id, "movie": movie, "start": start}

    def retry(
        self, source: str, take_at: int | None, port: str | None, label: str | None, fps_unlock: bool = False
    ) -> dict[str, Any]:
        """Retry run `source` from poll `take_at`: its recording to that poll at turbo, then live input (tas.play)."""
        busy = self.human_running()
        if busy:
            raise DecompError("tas.live", f"live input already drives {busy}; stop it first")
        tas.check_label(label)
        found = tas.retry_source(self.cfg, source, take_at)  # refuse a bad run or poll here, not in the thread
        if refused := self.seat_refusal(port, found["movie"], label):
            return refused
        exe = tas.with_port(self.cfg, port)["port"]
        with self.lock:
            run_id = tas.new_run_id(self.cfg, found["movie"])
        self._placeholder(
            run_id, found["movie"], 0, exe, label or tas.HUMAN_LABEL, found["pak"], found["cpak"], fps_unlock
        )
        with self.lock:
            self.queued[run_id].update(input="human", took_at=take_at, polls=len(found["polls"]), pads=found["pads"])
            self.queued[run_id].update({"from": source, "at": take_at})
        self.live.touch()

        def go() -> None:
            failure: Exception | None = None
            try:
                tas.play(
                    self.profile, None, run_id=run_id, port=port or None, label=label or None, source=source,
                    take_at=take_at, fps_unlock=fps_unlock,
                )  # fmt: skip
            except Exception as error:
                failure = error
            finally:
                self._ended(run_id, failure)

        threading.Thread(target=go, daemon=True).start()  # never takes a replay slot
        return {"id": run_id, "movie": found["movie"]}

    def keep(self, run_id: str, keep: bool) -> dict[str, Any]:
        out = tas.pin(self.cfg, run_id, keep)
        self.live.touch()
        return out

    def archive(self) -> None:
        """Whenever the runs changed with none going: delete runs past keep_days (tas.archive); a failure waits for
        the next change."""
        seen = 0
        while True:
            seen = self.settled(seen)
            with contextlib.suppress(OSError, DecompError):
                gone = tas.archive(self.cfg, self.keep_days)
                if gone:
                    print(f"tas page: archived {len(gone)} runs older than {self.keep_days:g} days", flush=True)

    def run_dir(self, run_id: str) -> Path | None:
        return tas.run_folder(self.cfg, run_id)

    def stop(self, run_id: str) -> dict[str, Any]:
        run = self.run_dir(run_id)
        row = tas.read_record(run) if run else None
        if not run or not row:
            with self.lock:
                if run_id in self.refused:  # refused at the start: no process was ever there to stop
                    return {"id": run_id, "stopped": False, "refused": self.refused[run_id]["error"]}
            raise DecompError("tas.stop", f"no run {run_id}")
        if row.get("status") != "running" or not row.get("pgid"):
            return {"id": run_id, "stopped": False}
        (run / "stopped").write_text("page\n")
        group = int(row["pgid"])
        try:
            os.killpg(group, signal.SIGTERM)
        except (OSError, ValueError):
            return {"id": run_id, "stopped": False}
        # The gdb bridge can outlive a TERM while it writes stacks; the run's whole group goes after STOP_GRACE.
        threading.Timer(STOP_GRACE, self._kill, (run, group)).start()
        return {"id": run_id, "stopped": True}

    @staticmethod
    def _kill(run: Path, group: int) -> None:
        if tas.running(run):
            with contextlib.suppress(OSError):
                os.killpg(group, signal.SIGKILL)


class Live:
    """live.json (`<site>/live.json`): the body of `Server.state()` written whenever the runs change, never per request.
    An inotify watch on `runs/plays`, on every folder of a run that is not done and on the port's folder reports a new
    run, a rewritten `run.json`, the `taken` / `stopped` / `pinned` marks, each poll the run's `hash.txt` gains and a
    rebuilt port. Writes are at most one per LIVE_GAP_SECONDS (a kernel timer) and carry no change of their own when
    only the clock moved; with nothing happening it is rewritten every LIVE_IDLE_SECONDS (its clock). The owner of each
    running run (the pid in its record) is watched through a pidfd in the same select: its exit rewrites live.json at
    once, and an owner already gone when it is first watched is noticed by the write that follows. A change with no
    run going also wakes the corpus gate and the archive (`Server.settle`)."""

    def __init__(self, server: Server) -> None:
        self.server = server
        self.path = server.site / "live.json"
        self.wake_r, self.wake_w = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self.last = b""
        self.running: list[Path] = []
        self.watched: set[Path] = set()
        self.watcher: inotify.Watcher | None = None
        self.owners: dict[Path, int] = {}  # run -> pidfd of its owner; -1 once gone

    def touch(self) -> None:
        """Something the watch cannot see changed (a queued run, a pin): rewrite soon."""
        with contextlib.suppress(OSError):
            os.write(self.wake_w, b"x")

    def write(self) -> bool:
        """Write live.json if its content changed (not just `now`); True when it did. Atomic: temp file + rename."""
        doc = self.server.state()
        self.server.feed.publish_runs(doc["runs"])  # the feed sees every run change Live sees
        self.running = [self.server.cfg["runs"] / "plays" / r["id"] for r in doc["runs"] if r["status"] == "running"]
        body = json.dumps({**doc, "now": 0}, separators=(",", ":")).encode()
        if body == self.last and self.path.is_file():
            return False
        self.last = body
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_bytes(json.dumps(doc, separators=(",", ":")).encode())
        tmp.replace(self.path)
        return True

    def sync(self) -> None:
        """Watch the folders of runs that are not done, drop the others, and give the reach relay its running set."""
        watcher = self.watcher
        if watcher is None:
            return
        unfinished = self.server.unfinished()
        for run in unfinished - self.watched:
            with contextlib.suppress(OSError):
                watcher.add(
                    run,
                    inotify.CREATE | inotify.MOVED_TO | inotify.CLOSE_WRITE | inotify.DELETE | inotify.MODIFY,
                )
                self.watched.add(run)
        for run in self.watched - unfinished:
            watcher.remove(run)
            self.watched.discard(run)
        self.watch_owners()
        self.server.reach.sync([run for run in self.watched if tas.running(run)])

    def watch_owners(self) -> None:
        """Hold a pidfd on the owner of every watched running run and release those of runs no longer watched. An owner
        that is already gone at open gets no fd: the write that follows this sync reports it."""
        for run in set(self.owners) - self.watched:
            self.release(run)
        for run in self.watched - set(self.owners):
            row = tas.read_record(run) or {}
            if row.get("status") != "running":
                continue
            try:
                self.owners[run] = os.pidfd_open(int(row["pid"]))
            except (KeyError, TypeError, ValueError):  # an old or queued record with no owner pid
                continue
            except OSError:  # the owner is gone already
                self.owners[run] = -1

    def release(self, run: Path) -> None:
        fd = self.owners.pop(run, -1)
        if fd >= 0:
            os.close(fd)

    def owner_exited(self, ready: list[int]) -> bool:
        """True when a watched owner's pidfd is readable (it exited); that fd is closed and not watched again."""
        gone = False
        for run, fd in self.owners.items():
            if fd >= 0 and fd in ready:
                os.close(fd)
                self.owners[run] = -1
                gone = True
        return gone

    def classify(self, events: list[tuple[Path, int]]) -> tuple[bool, bool]:
        """(a run appeared, went or was marked, anything the page shows moved): what the events were."""
        plays = self.server.cfg["runs"] / "plays"
        port = self.server.cfg.get("port")
        marks = (tas.RECORD, tas.TAKEN, RELEASED, "stopped", tas.PINNED)
        structural = any(path.parent == plays or path.name in marks for path, _mask in events)
        ticks = (tas.HASH, "output.txt")  # poll counter; the port's output (a gate hold, a live takeover)
        moved = any(path.name in ticks or (port is not None and path == Path(port)) for path, _mask in events)
        return structural, structural or moved

    def loop(self) -> None:
        plays = self.server.cfg["runs"] / "plays"
        plays.mkdir(parents=True, exist_ok=True)
        self.watcher = watcher = inotify.Watcher()
        watcher.add(plays, inotify.CREATE | inotify.MOVED_TO | inotify.DELETE)
        port = self.server.cfg.get("port")
        if port and Path(port).parent.is_dir():
            watcher.add(Path(port).parent, inotify.CREATE | inotify.MOVED_TO | inotify.CLOSE_WRITE)
        gap, idle = inotify.Timer(), inotify.Timer()
        self.sync()
        dirty = event = True  # event: something happened, not just the idle clock
        held = False  # the gap timer runs: no write before it goes off
        while True:
            if dirty and not held:
                dirty, held = False, True
                gap.arm(LIVE_GAP_SECONDS)
                idle.arm(LIVE_IDLE_SECONDS)
                try:
                    self.sync()  # also finds a run that died without a record, and drops watches of finished ones
                    self.write()
                except Exception as error:  # a bad record or a full disk: the next change writes again
                    print(f"tas page: live.json writer: {error}", flush=True)
                else:
                    if event and not self.running:
                        self.server.settle()
                event = False
            watching = [fd for fd in self.owners.values() if fd >= 0]
            ready = select.select([watcher.fd, self.wake_r, gap.fd, idle.fd, *watching], [], [])[0]
            if self.owner_exited(ready):
                dirty = event = True
            if gap.fd in ready and gap.expired():
                held = False
            if idle.fd in ready and idle.expired():
                dirty = True
            if self.wake_r in ready:
                inotify.drain(self.wake_r)
                dirty = event = True
            if watcher.fd in ready:
                structural, moved = self.classify(watcher.read())
                if structural:
                    self.sync()  # a new run's reader must listen before its port sends its first record
                dirty = dirty or moved
                event = event or moved

    def start(self) -> None:
        threading.Thread(target=self.loop, daemon=True, name="live").start()


class RunReach:
    """One run's reach stream: the cumulative bitset, the poll of the last record and the records not yet forgotten."""

    def __init__(self, run: Path, pending: Callable[[], bool] = lambda: False) -> None:
        self.run = run
        self.pending = pending  # true while the run is queued: its folder and port are not there yet, so keep listening
        self.lock = threading.Lock()
        # notified on every record taken or refused and when the reader ends
        self.changed = threading.Condition(self.lock)
        self.bits = bytearray()
        self.poll = 0
        self.count = 0  # records accepted
        self.rejected = 0
        self.seq = 0  # sequence number of the newest kept record
        self.records: deque[tuple[int, bytes]] = deque()
        self.size = 0
        self.done = False
        self.recheck_r, self.recheck_w = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)  # `recheck` -> the reader
        self.listening = threading.Event()  # set once the socket is bound: the port's datagrams are no longer dropped
        self.ends: dict[int, int] = {}  # read end -> write end of each wake pipe
        self.pipes: set[int] = set()  # write ends of the viewers' wake pipes (`listen`)

    def announce(self) -> None:
        """Called with the lock held after anything changed: wake the waiters and every viewer's stream."""
        self.changed.notify_all()
        for fd in self.pipes:
            with contextlib.suppress(OSError):
                os.write(fd, b"x")

    def recheck(self) -> None:
        """Tell the reader to ask whether the run is still going (Live saw the run's folder change)."""
        with self.lock:
            if not self.done:  # the pipe is closed once the reader is over
                os.write(self.recheck_w, b"x")

    def listen(self) -> int:
        """A pipe's read end that turns readable whenever a record arrives or the reader ends (one per viewer)."""
        read, write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        with self.lock:
            self.pipes.add(write)
        self.ends[read] = write
        return read

    def unlisten(self, read: int) -> None:
        with self.lock:
            self.pipes.discard(self.ends[read])
        os.close(self.ends.pop(read))
        os.close(read)

    def feed(self, data: bytes) -> bool:
        """Apply one datagram. A malformed one is dropped (False), never half applied."""
        try:
            poll, entered = tas_reach.parse(data)
        except ValueError:
            with self.changed:
                self.rejected += 1
                self.announce()
            return False
        with self.changed:
            tas_reach.merge(self.bits, data, entered)
            self.poll, self.count, self.seq = poll, self.count + 1, self.seq + 1
            self.records.append((self.seq, bytes(data)))
            self.size += len(data)
            while len(self.records) > REACH_KEPT_RECORDS or self.size > REACH_KEPT_BYTES:
                self.size -= len(self.records.popleft()[1])
            self.announce()
        return True

    def snapshot(self) -> tuple[bytes, int]:
        """(a full record of everything so far, the sequence number it is current to), taken together."""
        with self.lock:
            return tas_reach.full(self.poll, self.bits), self.seq

    def since(self, seq: int) -> tuple[list[bytes], int]:
        """(records after `seq` in order, the newest sequence number). A reader that fell behind what is kept gets
        one fresh full record instead of the gap."""
        with self.lock:
            if self.seq == seq:
                return [], seq
            if not self.records or self.records[0][0] > seq + 1:
                return [tas_reach.full(self.poll, self.bits)], self.seq
            return [r for n, r in self.records if n > seq], self.seq

    def finish(self) -> None:
        """The reader ended: wake the waiters. Serve writes no file (tas.play owns RUN/reach.bin)."""
        with self.changed:
            self.done = True
            self.announce()

    def serve(self) -> None:
        """The reader: the play's forwarded reach records on the run's live socket (tas.reach_live_address, abstract
        namespace; the play sends non-blocking and drops when nobody listens) until the run ends."""
        address = tas_reach.abstract(tas.reach_live_address(self.run))
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
                sock.bind(address)
            except OSError as error:  # another serve already listens for this run
                print(f"tas page: no reach stream for {self.run.name}: {error}", flush=True)
                with self.changed:
                    self.done = True
                    self.announce()
                os.close(self.recheck_r)
                os.close(self.recheck_w)
                return
            self.listening.set()
            while True:
                ready = select.select([sock, self.recheck_r], [], [])[0]
                if self.recheck_r in ready:
                    inotify.drain(self.recheck_r)
                    if not (tas.running(self.run) or self.pending()):
                        break
                if sock in ready:
                    try:
                        self.feed(sock.recv(tas_reach.MAX_RECORD + 1, socket.MSG_DONTWAIT))
                    except BlockingIOError:
                        pass
                    except OSError:
                        break
            while True:  # whatever the port sent before it exited
                try:
                    self.feed(sock.recv(tas_reach.MAX_RECORD + 1, socket.MSG_DONTWAIT))
                except OSError:
                    break
        self.finish()
        os.close(self.recheck_r)
        os.close(self.recheck_w)


class ReachFeed:
    """One /video viewer's cursor on a run's reach records: a full record first, then each later one once, in order."""

    def __init__(self, relay: RunReach | None, final: bytes | None = None) -> None:
        self.relay, self.final = relay, final
        self.seq = 0

    def first(self) -> bytes | None:
        if self.relay is None:
            return self.final
        record, self.seq = self.relay.snapshot()
        return record

    def pending(self) -> list[bytes]:
        if self.relay is None:
            return []
        records, self.seq = self.relay.since(self.seq)
        return records

    def listen(self) -> int | None:
        """A readable fd that signals new records (None for a finished run: it has no more)."""
        return self.relay.listen() if self.relay else None

    def close(self, fd: int | None) -> None:
        if fd is not None and self.relay:
            self.relay.unlisten(fd)


class Reach:
    """The reach relay of all runs (GAME-RUNNER 10a): a RunReach per running run, kept a while after the run ends."""

    def __init__(self) -> None:
        self.runs: OrderedDict[str, RunReach] = OrderedDict()
        self.lock = threading.Lock()

    def sync(self, running: list[Path]) -> None:
        """Listen for every running run that has no reader yet."""
        with self.lock:
            for run in running:
                if run.name not in self.runs:
                    relay = self.runs[run.name] = RunReach(run)
                    threading.Thread(target=relay.serve, daemon=True, name=f"reach-{run.name}").start()
            going = {run.name for run in running}
            for name, relay in self.runs.items():
                if name not in going and not relay.done:
                    relay.recheck()  # a run that left the running set, or left the queue, ends its reader
            while len(self.runs) > REACH_RUNS_KEPT:
                oldest = next(iter(self.runs))
                if not self.runs[oldest].done:
                    break
                del self.runs[oldest]

    def expect(self, run: Path, pending: Callable[[], bool]) -> None:
        """Listen for a run that has not started: the port's first full record is dropped when nobody listens yet, so
        the socket is bound when the run is queued, and stays bound while `pending` says the run is still queued."""
        with self.lock:
            if run.name not in self.runs:
                relay = self.runs[run.name] = RunReach(run, pending)
                threading.Thread(target=relay.serve, daemon=True, name=f"reach-{run.name}").start()

    def feed_for(self, run: Path) -> ReachFeed | None:
        """The records of `run` for one viewer: its live relay, else the final set a finished run left in reach.bin
        (sent once as a full record), else None."""
        with self.lock:
            relay = self.runs.get(run.name)
        if relay is not None:
            return ReachFeed(relay)
        try:
            bits = (run / tas_reach.FILE).read_bytes()
        except OSError:
            return None
        record = tas.read_record(run) or {}
        return ReachFeed(None, tas_reach.full(int(record.get("polls_reached") or 0), bits))


# Routes: an exact path, a first path segment (+ the rest), and a run's folder (video, frame ...). A route module
# answers through the request's reply helpers; none imports this module.


def audio(req: Any, server: Any, run: Path, query: str = "") -> None:
    """The run's sound as written: a WAV header, then the PCM bytes as the port appends them, each sent when the
    file grows. A listener joins AUDIO_LEAD_SECONDS behind the live end; the stream ends with the run or the peer."""
    row = tas.read_record(run)
    if not row:
        return req.missing()
    if not realtime(run, row):
        return req.reply(b"", "audio/wav", HTTPStatus.NO_CONTENT, extra={"X-Audio-Muted": "not-realtime"})
    name = tas.AUDIO
    pcm, rate_file = run / name, run / (name + ".rate")
    with serve_wake.Wake(req.connection) as wake:
        wake.watch(run, {name, name + ".rate", tas.RECORD}, serve_wake.CHANGED | inotify.CREATE | inotify.MODIFY)
        while not rate_file.is_file() or not pcm.is_file():
            if not tas.running(run):
                return req.missing()
            if not wake.wait():
                return
        rate = int(rate_file.read_text().strip() or 32000)
        if not 8000 <= rate <= 192000:
            return req.missing()
        req.stream_head("audio/wav", {"Connection": "close"})
        if not wake.send(wav_header(rate)):
            return
        end = pcm.stat().st_size
        with pcm.open("rb") as handle:
            handle.seek(max(0, end - int(rate * AUDIO_LEAD_SECONDS) * 4) // 4 * 4)
            while True:
                alive = tas.running(run)  # asked before the read: what the port wrote before it left is sent
                while chunk := handle.read(AUDIO_CHUNK_BYTES):
                    handle.seek(-(len(chunk) % 4), os.SEEK_CUR)  # whole 16-bit stereo samples only
                    chunk = chunk[: len(chunk) // 4 * 4]
                    if not chunk:
                        break
                    if not wake.send(chunk) or not wake.flush():
                        return
                if not alive or not wake.wait():
                    return


def video(req: Any, server: Any, run: Path, query: str = "") -> None:
    """One bounded latest-frame stream: a frame goes out when the run's picture file is replaced and reach records
    when they arrive, at most VIDEO_FPS times a second, independent of RTT and turbo's game clock."""
    req.stream_head("application/x-tas-frames", {"X-Video-Fps": str(VIDEO_FPS), "Connection": "close"})
    # A stalled phone cannot accumulate seconds of pictures in the server's socket buffer.
    req.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
    stamp, sent_seq = 0, 0
    feed = server.reach.feed_for(run)  # the run's reach records (section 10a), None when it has none
    first = feed.first() if feed else None
    records_fd = feed.listen() if feed else None  # a reach record wakes the stream like a new picture
    try:
        with serve_wake.Wake(req.connection, 1 / VIDEO_FPS) as wake:
            wake.watch(run, {tas.VIEW, tas.RECORD})
            if records_fd is not None:
                wake.signal(records_fd)
            while True:
                try:
                    current, body, seq = server.capture(run / tas.VIEW, "JPEG")
                except (OSError, ValueError):
                    current, body, seq = stamp, b"", 0
                changed = current != stamp
                # Reach records go out ahead of the picture they belong to; a new viewer's first bytes are a full one.
                records = ([first] if first else []) + (feed.pending() if feed else [])
                first = None
                out = b"".join(VIDEO_HEADER.pack(len(r) | REACH_FLAG, current, sent_seq) + r for r in records)
                if changed:
                    out += VIDEO_HEADER.pack(len(body), current, seq) + body
                if out and not wake.send(out):
                    return
                if changed:
                    stamp, sent_seq = current, seq
                if not tas.running(run) or not wake.wait():
                    return
    finally:
        if feed:
            feed.close(records_fd)


def wall(req: Any, server: Any, query: str = "") -> None:
    """One multiplexed stream of WALL_WIDTH px JPEG thumbs of every running run, at most WALL_FPS a second, each
    round woken by a run's picture file or a run starting or ending. It opens with the last thumb of the newest
    `tas.serve.wall_finished` finished runs, oldest first (a finished run's picture does not change). A frame is
    VIDEO_HEADER (length, stamp, run index in the place of the input sequence) + JPEG. The first time a run
    shows, a record with WALL_TABLE set in its length (body: JSON {"id": run id}) names its index; indices are
    per connection and never reused. Each run's reach records (section 10a) ride flagged under its index too, a
    full one first: the body of a flagged record is the table when it starts with "{", else a reach record.
    Browsers allow six connections per host, so twelve per-tile streams would stall."""
    req.stream_head("application/x-tas-wall", {"X-Wall-Fps": str(WALL_FPS), "Connection": "close"})
    req.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)
    index: dict[str, int] = {}
    stamps: dict[str, int] = {}
    feeds: dict[str, Any] = {}
    plays = server.cfg["runs"] / "plays"
    listening: list[tuple[Any, int]] = []
    try:
        with serve_wake.Wake(req.connection, 1 / WALL_FPS) as wake:
            wake.watch(plays, None, inotify.CREATE | inotify.MOVED_TO | inotify.DELETE)
            if (opening := finished_thumbs(server, index, stamps)) and not wake.send(opening):
                return
            wall_loop(req, server, wake, plays, index, stamps, feeds, listening)
    finally:
        for feed, fd in listening:
            feed.close(fd)


def finished_thumbs(server: Any, index: dict[str, int], stamps: dict[str, int]) -> bytes:
    """The wall's opening frames: the table record and last thumb of each of the newest `wall_finished` finished
    runs (a run without a picture is named and shows none), oldest first."""
    finished = [run for run, row in tas.run_rows(server.cfg) if row.get("status") != "running"]
    out = b""
    for run in finished[len(finished) - server.wall_finished :] if server.wall_finished else []:
        index[run.name] = len(index)
        name = json.dumps({"id": run.name}).encode()
        out += VIDEO_HEADER.pack(len(name) | WALL_TABLE, 0, index[run.name]) + name
        try:
            stamp, body, _ = server.capture(run / tas.VIEW, "THUMB")
        except (OSError, ValueError):
            continue
        stamps[run.name] = stamp
        out += VIDEO_HEADER.pack(len(body), stamp, index[run.name]) + body
    return out


def wall_loop(
    req: Any,
    server: Any,
    wake: Any,
    plays: Path,
    index: dict[str, int],
    stamps: dict[str, int],
    feeds: dict[str, Any],
    listening: list[tuple[Any, int]],
) -> None:
    if True:
        while True:
            unfinished = server.unfinished()  # a run's folder is watched from its creation: no record is missed
            for run in unfinished - wake.watching():
                wake.watch(run, {tas.VIEW, tas.RECORD})
            for run in wake.watching() - unfinished - {plays}:
                wake.drop(run)
            out = b""
            for run in server.running_runs():
                try:
                    stamp, body, _ = server.capture(run / tas.VIEW, "THUMB")
                except (OSError, ValueError):
                    continue
                if run.name not in index:
                    index[run.name] = len(index)
                    name = json.dumps({"id": run.name}).encode()
                    out += VIDEO_HEADER.pack(len(name) | WALL_TABLE, 0, index[run.name]) + name
                    feeds[run.name] = feed = server.reach.feed_for(run)
                    if feed and (fd := feed.listen()) is not None:  # its reach records wake the wall too
                        wake.signal(fd)
                        listening.append((feed, fd))
                    first = feed.first() if feed else None
                    if first and any(first[5:]):  # a run that lit nothing yet costs the wall no record
                        out += VIDEO_HEADER.pack(len(first) | REACH_FLAG, stamp, index[run.name]) + first
                feed = feeds.get(run.name)
                for record in feed.pending() if feed else []:
                    out += VIDEO_HEADER.pack(len(record) | REACH_FLAG, stamp, index[run.name]) + record
                if stamps.get(run.name) != stamp:
                    stamps[run.name] = stamp
                    out += VIDEO_HEADER.pack(len(body), stamp, index[run.name]) + body
            if out and not wake.send(out):
                return
            if not wake.wait():
                return


EXACT = {
    "/": serve_pages.index,
    "/watch": serve_pages.watch,
    "/wall": wall,
    "/api/feed/stream": serve_feed.stream,
    "/api/coverage": serve_api.coverage,
    "/api/pool": serve_api.pool,
    "/api/runs": serve_api.runs,
    "/api/phase": serve_phase.phase,
    "/phase/shot": serve_phase.shot,
    "/phase/frame.png": serve_phase.frame,
}
PREFIX = {
    "data": serve_files.data,
    "static": serve_files.static,
}
RUN_ROUTES = {
    "video": video,
    "oracle": serve_video.oracle,
    "shot": serve_run.shot,
    "still": serve_run.still,
    "diff": serve_run.diff,
    "log": serve_run.log,
    "audio": audio,
}


def handler(server: Server) -> type[BaseHTTPRequestHandler]:
    class Handler(serve_http.Replies):
        site = server

        def do_GET(self) -> None:
            path, _, query = self.path.partition("?")
            head, _, rest = path[1:].partition("/")
            try:
                if path in EXACT:
                    EXACT[path](self, server, query)
                elif (site := site_core.answer(server, "GET", path, query, b"")) is not None:
                    self.json(site[1], site[0])
                elif head == "coverage":
                    serve_files.coverage(self, server, rest, query)
                elif head in PREFIX and rest:
                    PREFIX[head](self, server, rest, query)
                elif head in RUN_ROUTES and rest:
                    name = rest.partition("/")[0]
                    if (run := server.run_dir(name)) is None:
                        self.missing(f"no run {name}")
                    else:
                        RUN_ROUTES[head](self, server, run, query)
                else:
                    self.missing()
            except (BrokenPipeError, ConnectionResetError):
                return
            except DecompError as error:  # a refusal answers (a missing run is a 404 naming it), never a hang
                status = HTTPStatus.NOT_FOUND if error.key.endswith("notfound") else HTTPStatus.BAD_REQUEST
                self.json({"error": str(error)}, status)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                raw = self.rfile.read(length)
                path, _, query = self.path.partition("?")
                if (site := site_core.answer(server, "POST", path, query, raw)) is not None:
                    return self.json(site[1], site[0])
                body = json.loads(raw or b"{}")
                if path in serve_actions.ACTIONS:
                    serve_actions.ACTIONS[path](self, server, body)
                else:
                    self.missing()
            except (DecompError, ValueError, TypeError) as error:
                self.json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    return Handler


def serve(profile: Profile) -> dict[str, Any]:
    server = Server(profile, deployed_sha())
    host, _, port = server.listen.rpartition(":")
    http = ThreadingHTTPServer((host, int(port)), handler(server))
    http.daemon_threads = True
    server.live.start()
    server.snapshots.start()
    threading.Thread(target=server.archive, daemon=True).start()
    threading.Thread(target=server.gatekeeper, daemon=True).start()
    threading.Thread(target=server.phase.run_frames, daemon=True).start()
    print(f"tas page: http://{server.listen}/ at {server.deployed}", flush=True)
    notify_ready()
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        http.server_close()
    return {"listen": server.listen}
