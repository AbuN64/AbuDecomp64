"""The client `ci/display_input_proof.sh` runs inside foot on the display: it reports what input reached it.

Raw tty with SGR mouse reporting on. Opening the ready FIFO blocks until the proof reads it (the proof's
signal to send);
the client then reads until it holds a key 'k' and a left press, writes one line to the result FIFO, and stays open.
"""

import re
import sys
import termios
import tty

ready, result, debug = sys.argv[1:4]
stdin = sys.stdin.buffer.raw  # type: ignore[attr-defined]
tty.setraw(sys.stdin.fileno())
sys.stdout.write("\x1b[?1000h\x1b[?1006hwaiting for input\r\n")
sys.stdout.flush()
with open(ready, "w") as fifo:
    fifo.write("ready\n")
seen = b""
press = None
while b"k" not in seen or not press:
    chunk = stdin.read(64)
    with open(debug, "a") as log:
        log.write(f"{chunk!r}\n")
    seen += chunk
    if b"m" in chunk:  # diagnostic: mouse reporting off, so foot shows its own text cursor when the pointer is over it
        sys.stdout.write("\x1b[?1000l\x1b[?1006l")
        sys.stdout.flush()
    press = re.search(rb"\x1b\[<0;(\d+);(\d+)M", seen)
cell = f"{press[1].decode()},{press[2].decode()}"
sys.stdout.write(f"key k, pointer press at cell {cell}\r\n")
sys.stdout.flush()
with open(result, "w") as fifo:
    fifo.write(f"key ok pointer ok cell {cell}\n")
termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
stdin.read(1)
