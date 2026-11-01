"""The site's own browser viewer for a display: a WebSocket bridge to the display's RFB socket and the page that
draws it.

The browser speaks RFB itself (static/site/viewer.js, raw encoding on a canvas); this module only carries bytes between
the WebSocket and the display's address (Unix socket or guest TCP). stdlib only, no noVNC, no other toolkit.
"""

from __future__ import annotations

import base64
import hashlib
import select
import socket
import struct
from collections.abc import Callable
from html import escape

from abu_decomp.display import Record

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CHUNK = 65536


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()


def handshake_reply(key: str) -> bytes:
    lines = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Accept: {accept_key(key)}",
    ]
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def frame(payload: bytes) -> bytes:
    """One unmasked binary frame (server to client)."""
    size = len(payload)
    if size < 126:
        head = struct.pack(">BB", 0x82, size)
    elif size < 65536:
        head = struct.pack(">BBH", 0x82, 126, size)
    else:
        head = struct.pack(">BBQ", 0x82, 127, size)
    return head + payload


def unframe(buffer: bytearray) -> tuple[list[bytes], bool]:
    """Pull every whole client frame out of `buffer` (masked); the flag is True once a close frame arrived."""
    out: list[bytes] = []
    while len(buffer) >= 2:
        opcode, length = buffer[0] & 0x0F, buffer[1] & 0x7F
        at = 2
        if length == 126:
            if len(buffer) < 4:
                break
            (length,) = struct.unpack(">H", buffer[2:4])
            at = 4
        elif length == 127:
            if len(buffer) < 10:
                break
            (length,) = struct.unpack(">Q", buffer[2:10])
            at = 10
        masked = bool(buffer[1] & 0x80)
        if len(buffer) < at + (4 if masked else 0) + length:
            break
        mask = bytes(buffer[at : at + 4]) if masked else b"\0\0\0\0"
        at += 4 if masked else 0
        data = bytes(buffer[at : at + length])
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        del buffer[: at + length]
        if opcode == 8:
            return out, True
        if opcode in (1, 2):
            out.append(data)
    return out, False


def bridge(client: socket.socket, upstream: socket.socket) -> None:
    """Carry bytes both ways until either side closes. Blocks on select only; the close of a socket is the signal."""
    pending = bytearray()
    sockets = [client, upstream]
    while True:
        ready, _, _ = select.select(sockets, [], [])
        if upstream in ready:
            data = upstream.recv(CHUNK)
            if not data:
                return
            client.sendall(frame(data))
        if client in ready:
            data = client.recv(CHUNK)
            if not data:
                return
            pending += data
            messages, closed = unframe(pending)
            for message in messages:
                upstream.sendall(message)
            if closed:
                return


def serve_socket(client: socket.socket, key: str, record: Record, connect: Callable[[str], socket.socket]) -> None:
    upstream = connect(record.socket)
    try:
        client.sendall(handshake_reply(key))
        bridge(client, upstream)
    finally:
        upstream.close()


def viewer_page(record: Record) -> str:
    """The full-window page of one display: the canvas and the client script, which dials /viewer/<id>/ws."""
    ident = escape(record.identity)
    return (
        "<!doctype html><meta charset=utf-8><title>display " + ident + "</title>"
        "<style>html,body{margin:0;background:#111;height:100%}"
        "canvas{display:block;max-width:100%;max-height:100vh;margin:auto;"
        "outline:none}</style>"
        f'<canvas id=screen tabindex=0 data-display="{ident}" data-ws="/viewer/{ident}/ws"></canvas>'
        '<script type=module src="/static/site/viewer.js"></script>'
    )


def live_page(records: list[Record]) -> str:
    """The Live view: one embedded viewer per display kept on this site."""
    if not records:
        return (
            "<!doctype html><meta charset=utf-8><title>live</title>"
            "<p>No live display. Start one with <code>decomp display invoke</code>."
        )
    cells = "".join(
        f'<figure style="margin:0"><figcaption>{escape(r.identity)} {r.width}x{r.height}</figcaption>'
        f'<iframe src="/viewer/{escape(r.identity)}"'
        f' style="width:100%;aspect-ratio:{r.width}/{r.height};border:0"></iframe></figure>'
        for r in records
    )
    return (
        "<!doctype html><meta charset=utf-8><title>live</title>"
        f"<body style='background:#111;color:#ddd;font:14px sans-serif'>{cells}"
    )
