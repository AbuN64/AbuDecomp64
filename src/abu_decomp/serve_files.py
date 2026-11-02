"""The file routes of `decomp tas serve`: `/data/*` (the baked site, `<tas.runs>/site/`), `/static/*` (the page's icon
sprite and region modules) and `/coverage/*` (the gcovr HTML report, wearing the site stylesheet)."""

from __future__ import annotations

import re
import urllib.parse
from http import HTTPStatus
from pathlib import Path
from typing import Any

from abu_decomp import site_frame, tas_coverage
from abu_decomp.serve_http import GZIP_MIN, TEXT_KINDS, accepts_gzip, tag_matches

STATIC = Path(__file__).with_name("static")  # the page's icon sprite and region modules (site/), at /static/NAME
HASHED_NAME = re.compile(r"\.[0-9a-f]{6,}\.[a-z]+$")  # fns.1a2b3c4d.json: the content hash is in the name
NO_STORE_DATA = frozenset({"live.json", "manifest.json"})
DATA_KINDS = {
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".bin": "application/octet-stream",
    ".jpg": "image/jpeg",
    ".html": "text/html; charset=utf-8",
    ".css": "text/css",
    ".js": "text/javascript",
}
FILE_KINDS = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css",
    ".js": "text/javascript",
    ".svg": "image/svg+xml",
}


def data(req: Any, server: Any, name: str, query: str = "") -> None:
    """A file of the baked site (`<tas.runs>/site/`). A client that accepts gzip gets the `.gz` sibling the bake
    wrote (else a body over GZIP_MIN is compressed once per tag); hashed names are immutable, live.json and
    manifest.json are never stored, the rest revalidate by ETag."""
    rel = urllib.parse.unquote(name)
    root = server.site.resolve()
    target = (root / rel).resolve()
    if root not in target.parents or target.suffix == ".gz" or not target.is_file():
        return req.missing()
    kind = DATA_KINDS.get(target.suffix, "application/octet-stream")
    zipped = target.with_name(target.name + ".gz")
    packed = accepts_gzip(req.headers) and zipped.is_file()
    try:
        info = (zipped if packed else target).stat()
        body = (zipped if packed else target).read_bytes()
    except OSError:
        return req.missing()
    tag = f'"{info.st_mtime_ns:x}-{info.st_size:x}{"z" if packed else ""}"'
    headers = {"ETag": tag, "Vary": "Accept-Encoding"}
    if target.name in NO_STORE_DATA:
        headers["Cache-Control"] = "no-store"
    elif HASHED_NAME.search(target.name):
        headers["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        headers["Cache-Control"] = "no-cache"
    if tag_matches(req.headers.get("If-None-Match"), tag):
        req.send_response(HTTPStatus.NOT_MODIFIED)
        for key, value in headers.items():
            req.send_header(key, value)
        req.end_headers()
        return
    if packed:
        headers["Content-Encoding"] = "gzip"
    elif kind.split(";")[0] in TEXT_KINDS and len(body) >= GZIP_MIN and accepts_gzip(req.headers):
        body = server.gzipped(f"data{tag}{target.name}", body)
        headers["Content-Encoding"] = "gzip"
    req.send_response(HTTPStatus.OK)
    req.send_header("Content-Type", kind)
    req.send_header("Content-Length", str(len(body)))
    for key, value in headers.items():
        req.send_header(key, value)
    req.end_headers()
    req.wfile.write(body)


def send_file(req: Any, root: Path, name: str, wrap: Any = None) -> None:
    """A file below `root`; a name that leaves it, or is no file, is a 404. `wrap` rewrites an HTML body first."""
    path = (root / name).resolve()
    if root.resolve() not in path.parents or not path.is_file():
        return req.missing()
    body = path.read_bytes()
    if wrap and path.suffix == ".html":
        body = wrap(body)
    req.reply(body, FILE_KINDS.get(path.suffix, "application/octet-stream"))


def static(req: Any, server: Any, name: str, query: str = "") -> None:
    """The page's static files below STATIC."""
    send_file(req, STATIC, name)


def coverage(req: Any, server: Any, name: str, query: str = "") -> None:
    """The gcovr HTML report; the generated report links the site stylesheet too."""
    send_file(req, server.cfg["runs"] / tas_coverage.HTML, name or "index.html", site_frame.wrap_report)
