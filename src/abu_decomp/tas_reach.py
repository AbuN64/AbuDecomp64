"""The run's reach stream (GAME-RUNNER 10a): the port's reach datagrams, the cumulative bitset, RUN/reach.bin.
Owner of the stream is the play (tas.play), never the site server: the play binds the run's reach socket
(`tas.reach_address`, abstract namespace; one reader per socket) before the port starts, folds every datagram into the
cumulative bitset over fns.json rows, and at the port's end writes RUN/reach.bin (headerless bitset, atomic, only if
the port sent a record) whether or not `tas serve` runs. The one writer of reach.bin is `Collector.finish`.
`tas serve` is a reader: the collector forwards what it receives to the live socket (`tas.reach_live_address`, the reach
name + `.live`) with a non-blocking send that is dropped while nobody listens, and the server's RunReach binds that
socket for the /video relay. A server that starts listening mid-run gets one full cumulative record (type 1) first, in
place of the delta it would have missed (the collector sees the earlier send refused), so the relay is never short.
This module also holds the wire decoding both sides share (`parse`, `merge`, `full`); the wire format is the
port's (record type 1 = poll + bitset, type 2 = poll + varint-delta lists of fresh and hot rows)."""

from __future__ import annotations

import contextlib
import socket
import struct
import threading
from pathlib import Path

FILE = "reach.bin"  # RUN/reach.bin: the run's final reach bitset over fns.json rows
MAX_RECORD = 65536
MAX_ROW = 1 << 17
RCVBUF = 1 << 20


def varint(data: bytes, pos: int) -> tuple[int, int]:
    """One LEB128 integer of a reach record at `pos`: (value, next position); ValueError when it is cut off."""
    value = shift = 0
    while True:
        if pos >= len(data) or shift > 28:
            raise ValueError("bad varint")
        byte = data[pos]
        pos += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, pos
        shift += 7


def rows(data: bytes, pos: int) -> tuple[list[int], int]:
    count, pos = varint(data, pos)
    found, row = [], 0
    for _ in range(count):
        step, pos = varint(data, pos)
        row += step
        if row >= MAX_ROW:
            raise ValueError("row out of range")
        found.append(row)
    return found, pos


def full(poll: int, bits: bytes) -> bytes:
    """A full reach record (type 1): the poll and the bitset over fns.json rows."""
    return struct.pack("<BI", 1, poll) + bytes(bits)


def parse(data: bytes) -> tuple[int, list[int] | None]:
    """(poll, rows entered) of one datagram, rows None for a full record; ValueError when it is malformed."""
    if len(data) < 5 or data[0] not in (1, 2):
        raise ValueError("not a reach record")
    poll = struct.unpack_from("<I", data, 1)[0]
    if data[0] == 1:
        return poll, None
    fresh, pos = rows(data, 5)
    hot, pos = rows(data, pos)
    if pos != len(data):
        raise ValueError("trailing bytes")
    return poll, fresh + hot


def merge(bits: bytearray, data: bytes, entered: list[int] | None) -> None:
    """Fold a parsed datagram into the cumulative bitset: a full record replaces it, a delta sets its rows."""
    if entered is None:
        bits[:] = data[5:]
        return
    for row in entered:
        if row >> 3 >= len(bits):
            bits.extend(bytes((row >> 3) + 1 - len(bits)))
        bits[row >> 3] |= 1 << (row & 7)


def abstract(address: str) -> str:
    """The socket name of an `@name` address (Linux abstract namespace)."""
    return "\0" + address[1:]


class Collector:
    """The play's reach reader for one run: binds the reach socket, folds datagrams, forwards them to `live`."""

    def __init__(self, run: Path, address: str, live: str, rows: int) -> None:
        self.run = run
        self.rows = rows  # the run.json census rows: reach.bin is ceil(rows/8) bytes
        self.address = abstract(address)
        self.live = abstract(live)
        self.bits = bytearray()
        self.poll = 0
        self.count = 0  # records accepted
        self.rejected = 0
        self.listened = True  # the server was reachable at the last forward: no full record owed
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.out = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.out.setblocking(False)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF)
            self.sock.bind(self.address)
        except OSError:
            self.close()
            raise
        self.thread = threading.Thread(target=self.read, daemon=True, name=f"reach-{run.name}")
        self.thread.start()

    def close(self) -> None:
        self.sock.close()
        self.out.close()

    def feed(self, data: bytes) -> None:
        """Apply one datagram and forward it. A malformed one is dropped, never half applied."""
        try:
            poll, entered = parse(data)
        except ValueError:
            self.rejected += 1
            return
        merge(self.bits, data, entered)
        self.poll, self.count = poll, self.count + 1
        # A server that was not listening at the last send missed records: its first one is the cumulative set.
        sent = data if self.listened else full(poll, bytes(self.bits))
        try:
            self.out.sendto(sent, self.live)
            self.listened = True
        except OSError:  # nobody listens (or its buffer is full): dropped, a full record follows next time
            self.listened = False

    def read(self) -> None:
        """Until the empty datagram `finish` sends (the port's datagrams are all ahead of it in the queue)."""
        while True:
            try:
                data = self.sock.recv(MAX_RECORD + 1)
            except OSError:
                return
            if not data:
                return
            self.feed(data)

    def finish(self) -> None:
        """Stop the reader after the datagrams queued so far and write RUN/reach.bin if the port sent a record.
        Atomic, like run.json; the run folder archived under the play is not an error. The port extends the bitset
        only up to the highest row it entered, so it is zero-filled to the census's ceil(rows/8) bytes, never cut: a
        longer one stays as sent and `lockstep.py check` refuses it (CONTRACTS.md Run record)."""
        with contextlib.suppress(OSError):
            self.out.sendto(b"", self.address)
        self.thread.join()
        self.close()
        if self.count:
            with contextlib.suppress(OSError):
                tmp = self.run / (FILE + ".tmp")
                tmp.write_bytes(bytes(self.bits) + bytes(max(0, (self.rows + 7) // 8 - len(self.bits))))
                tmp.replace(self.run / FILE)
