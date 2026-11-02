"""`GET /audio/RUN`: the run's sound as a WAV of unknown length, paced to the wall clock."""

from __future__ import annotations

import struct
import time
from http import HTTPStatus
from pathlib import Path
from typing import Any

from abu_decomp import tas, tas_oracle
from abu_decomp.serve_http import query_args
from abu_decomp.serve_pace import Allowance, realtime, wait_for_files

AUDIO_LEAD_SECONDS = 0.25  # a listener joins this far behind the live end of the sound


def wav_header(rate: int) -> bytes:
    """A RIFF/WAVE header for s16 stereo of unknown length (sizes at their maximum), for a live stream."""
    size = 0xFFFFFFFF
    fmt = struct.pack("<HHIIHH", 1, 2, rate, rate * 4, 4, 16)
    return (
        b"RIFF"
        + struct.pack("<I", size)
        + b"WAVEfmt "
        + struct.pack("<I", len(fmt))
        + fmt
        + b"data"
        + struct.pack("<I", size - 36)
    )


def audio(req: Any, server: Any, run: Path, query: str = "") -> None:
    row = tas.read_record(run)
    if not row:
        return req.missing()
    if not realtime(run, row):
        return req.reply(b"", "audio/wav", HTTPStatus.NO_CONTENT, extra={"X-Audio-Muted": "not-realtime"})
    name = tas_oracle.AUDIO if query_args(query).get("side") == "oracle" else tas.AUDIO
    pcm, rate_file = run / name, run / (name + ".rate")
    # The run's own writes of its sound files start the stream; a run that ends or a client that leaves ends the wait.
    if not wait_for_files(run, [rate_file, pcm], lambda: tas.running(run), req.connection):
        return req.missing()
    rate = int(rate_file.read_text().strip() or 32000)
    if not 8000 <= rate <= 192000:
        return req.missing()
    req.stream_head("audio/wav", {"Connection": "close"})
    req.connection.settimeout(5)
    req.wfile.write(wav_header(rate))
    oracle = name == tas_oracle.AUDIO

    def due() -> int:
        return int(max(0.0, time.time() - float(row.get("started") or 0)) * rate) * 4

    end = min(due(), pcm.stat().st_size) if oracle else pcm.stat().st_size
    offset = max(0, end - int(rate * AUDIO_LEAD_SECONDS) * 4) // 4 * 4
    # Even a producer that writes in bursts gets one steady wall-paced stream. Initial lead covers jitter.
    allowance_clock, sent = Allowance(rate, AUDIO_LEAD_SECONDS), 0
    with pcm.open("rb") as handle:
        while True:
            allowance = allowance_clock.frames() * 4 - sent
            count = min(max(0, allowance), int(rate * 0.05) * 4)
            if oracle:
                count = min(count, max(0, due() - offset))
            handle.seek(offset)
            chunk = handle.read(count)
            chunk = chunk[: len(chunk) // 4 * 4]
            if chunk:
                req.wfile.write(chunk)
                req.wfile.flush()
                offset += len(chunk)
                sent += len(chunk)
            elif not tas.running(run) and (offset >= pcm.stat().st_size or (oracle and offset >= due())):
                return
            time.sleep(0.02)
