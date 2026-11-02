"""What a fake port leaves in a run: the poll lines of RUN/lockstep/port/lockstep.jsonl (CONTRACTS.md "Run record")."""

import json
from pathlib import Path
from typing import Any

from abu_decomp import tas


def word(buttons: int, x: int, y: int) -> str:
    return f"{buttons << 16 | (x & 255) << 8 | (y & 255):08x}"


def write_polls(env: dict[str, Any], raw: str) -> None:
    """Write the polls of movie text `raw` (BUTTONS X Y per pad per line) as the port's lockstep poll lines."""
    lines = [json.dumps({"k": "meta", "side": "port"})]
    for number, poll in enumerate(tas.parse_raw(raw), 1):
        pads = [word(*poll[i : i + 3]) for i in range(0, len(poll), 3)]
        lines.append(
            json.dumps({"k": "poll", "poll": number, "vi": number, "pad": pads, "pages": []}, separators=(",", ":"))
        )
    Path(env["N64C_LOCKSTEP_DIR"], "lockstep.jsonl").write_text("\n".join(lines) + "\n")
