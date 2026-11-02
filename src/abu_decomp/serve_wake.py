"""What a stream handler blocks on: a producer's file changes (inotify), the peer closing the connection, and at most
one wake per `gap` seconds (a kernel timer, so a fast producer is sampled, never followed frame by frame)."""

from __future__ import annotations

import select
import socket
from pathlib import Path
from typing import Any

from abu_decomp import inotify

CHANGED = inotify.MOVED_TO | inotify.CLOSE_WRITE  # a file written whole, or one renamed into place


def closed(peer: socket.socket) -> bool:
    """Whether the peer hung up (a client has nothing more to say after its request; any bytes are dropped)."""
    try:
        return peer.recv(4096, socket.MSG_DONTWAIT) == b""
    except BlockingIOError:
        return False
    except OSError:
        return True


class Wake:
    def __init__(self, peer: socket.socket, gap: float = 0.0) -> None:
        self.peer, self.gap = peer, gap
        self.watcher = inotify.Watcher()
        self.timer = inotify.Timer() if gap else None
        self.names: dict[Path, frozenset[str] | None] = {}
        self.seen, self.held = True, False  # the first wait returns at once: look before waiting

    def __enter__(self) -> Wake:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def watch(self, directory: Path, names: set[str] | None = None, mask: int = CHANGED) -> None:
        """Wake on events of these file names in `directory` (None: any name)."""
        if directory not in self.names:
            self.watcher.add(directory, mask)
            self.names[directory] = None if names is None else frozenset(names)

    def drop(self, directory: Path) -> None:
        self.watcher.remove(directory)
        self.names.pop(directory, None)

    def watching(self) -> set[Path]:
        return set(self.names)

    def wait(self) -> bool:
        """True when a watched file changed and `gap` passed since the last return; False when the peer closed."""
        while True:
            if self.seen and not self.held:
                self.seen = False
                if self.timer:
                    self.timer.arm(self.gap)
                    self.held = True
                return True
            fds: list[Any] = [self.watcher.fd, self.peer]
            if self.timer and self.held:
                fds.append(self.timer.fd)
            ready = select.select(fds, [], [])[0]
            if self.peer in ready and closed(self.peer):
                return False
            if self.timer and self.timer.fd in ready and self.timer.expired():
                self.held = False
            if self.watcher.fd in ready:
                for path, _mask in self.watcher.read():
                    names = self.names.get(path.parent, frozenset())
                    if names is None or path.name in names:
                        self.seen = True

    def close(self) -> None:
        self.watcher.close()
        if self.timer:
            self.timer.close()
