"""The POST routes of `decomp tas serve`: start, stop and keep a run, take it over, and send live input.
The page's keyboard and gamepad reach a Linux or Windows display through `/api/display/ID/input`, the same display
Input the CLI and the live proof use."""

from __future__ import annotations

from typing import Any

from abu_decomp import display
from abu_decomp.serve_http import need_id


def play(req: Any, server: Any, body: dict[str, Any]) -> None:
    req.json(
        server.play(
            str(body.get("movie") or ""),
            int(body.get("speed", 0)),
            str(body.get("port") or "") or None,
            str(body.get("label") or "") or None,
            str(body.get("pak") or "") or None,
            bool(body.get("oracle", False)),
            str(body.get("cpak") or "") or None,
            str(body.get("from") or "") or None,
            int(body["at"]) if body.get("at") is not None else None,
            bool(body.get("fps_unlock", False)),
            str(body.get("start") or "") or None,
            bool(body.get("take", False)),
            int(body["hold_at"]) if body.get("hold_at") is not None else None,
        )
    )


def stop(req: Any, server: Any, body: dict[str, Any]) -> None:
    req.json(server.stop(need_id(body)))


def pin(req: Any, server: Any, body: dict[str, Any]) -> None:
    req.json(server.keep(need_id(body), bool(body.get("pinned", True))))


def take(req: Any, server: Any, body: dict[str, Any]) -> None:
    req.json(server.take(need_id(body)))


def run_input(req: Any, server: Any, body: dict[str, Any]) -> None:
    """Live input to a taken run: a gate (`gate`), the hand-back (`release`), or one controller sample."""
    if body.get("gate"):
        count = int(body["count"]) if body.get("count") is not None else None
        req.json(server.gate(need_id(body), str(body["gate"]), count))
    elif body.get("release"):
        req.json(server.release(need_id(body)))
    else:
        req.json(
            server.input(
                need_id(body),
                int(body.get("seq", 0)),
                int(body.get("buttons", 0)),
                int(body.get("x", 0)),
                int(body.get("y", 0)),
                int(body.get("pad", 0)),
            )
        )


def display_input(req: Any, server: Any, body: dict[str, Any], shown: str) -> None:
    req.json(display.backend(server.displays).input(shown, body.get("events")))


ACTIONS = {"/api/play": play, "/api/stop": stop, "/api/pin": pin, "/api/take": take, "/api/input": run_input}
DISPLAY_INPUT = ("/api/display/", "/input")
