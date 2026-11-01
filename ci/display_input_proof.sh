#!/bin/bash
# Proof of `decomp display input` on a host with labwc, wayvnc and foot: create 1280x720, run a client in foot on it
# (ci/display_input_client.py), capture, send a key and a pointer click through the CLI, capture again, delete, and
# answer the Windows backend's input. Writes RESULT.md into the directory given as the second argument.
# Run from the repo root (a rig job's worktree); the Python with Pillow is the first argument.
set -u
# Every wait is bounded by the adapter's declared COMMAND_SECONDS and fails by name; every step echoes before it runs.
step() { echo "[$(date +%T)] $*" >&2; }
repo=$PWD
py=${1:-python3}
PROOF_SECONDS=$(cd "$repo/src" && "$py" -c 'from abu_decomp import display; print(int(display.COMMAND_SECONDS))')
lane=${2:?"usage: display_input_proof.sh PYTHON LANE_DIR"}
work=$(mktemp -d "${TMPDIR:-/tmp}/display-input-proof.XXXXXX")
decomp() { (cd "$repo/src" && timeout "$PROOF_SECONDS" "$py" -u -m abu_decomp display --dir "$work/displays" "$@"); }
# Reads one line from a FIFO for at most PROOF_SECONDS; the name says what never answered.
await_line() {
  local line
  line=$(timeout "$PROOF_SECONDS" head -n 1 "$1") && [ -n "$line" ] && { echo "$line"; return 0; }
  step "never became ready: $2 sent nothing in ${PROOF_SECONDS}s"
  return 1
}
mkdir -p "$lane"
out=$lane/RESULT.md
: > "$out"
step "create 1280x720 (labwc, wlr-randr, wayvnc each bounded by the adapter's START_SECONDS)"
id=$(WAYLAND_DEBUG=1 decomp create --size 1280x720 | "$py" -c 'import json,sys; print(json.load(sys.stdin)["result"]["identity"])') || {
  echo "create: failed (stderr names the tool that never became ready)" >> "$out"
  exit 1
}
echo "create: ok $id 1280x720" >> "$out"
mkfifo "$work/ready" "$work/result"
env WAYLAND_DEBUG=1 XDG_RUNTIME_DIR="$work/displays/$id/run" WAYLAND_DISPLAY=wayland-0 HOME="$HOME" PATH="$PATH" \
  foot --fullscreen "$py" "$repo/ci/display_input_client.py" "$work/ready" "$work/result" "$work/client.log" > "$work/foot.log" 2>&1 &
foot=$!
step "foot client started; waiting for it to open the ready fifo"
await_line "$work/ready" "the foot client on the display" > /dev/null || {
  echo "foot: never became ready ($(tail -c 300 "$work/foot.log"))" >> "$out"
  kill "$foot" 2> /dev/null
  decomp delete "$id" > /dev/null
  exit 1
}
step "capture before"
decomp capture "$id" --path "$work/before.png" > /dev/null || echo "capture before: failed" >> "$out"
# One call, one RFB connection: wayvnc makes fresh virtual devices per connection and drops them on disconnect, so
# the click that focuses the foot window and the key that follows it must share the connection.
events='[{"click": {"x": 640, "y": 360}}, {"key": "k"}, {"click": {"x": 640, "y": 360}}]'
step "input click (focus), key, click in one call"
if decomp input "$id" --events "$events" > "$work/input.json" && got=$(await_line "$work/result" "the foot client's input report"); then
  case $got in
    "key ok pointer ok"*)
      echo "input key: ok ($got)" >> "$out"
      echo "input pointer: ok" >> "$out"
      ;;
    *) echo "input key: failed ($got)" >> "$out" ;;
  esac
else
  echo "input key: failed (see $work)" >> "$out"
  step "client.log: $(tail -c 400 "$work/client.log" 2> /dev/null)"
  for probe in '{"pointer": {"x": 200, "y": 200, "buttons": 0}}' '{"pointer": {"x": 200, "y": 200, "buttons": 1}}' \
    '{"pointer": {"x": 200, "y": 200, "buttons": 0}}'; do
    decomp input "$id" --events "[$probe]" > /dev/null
    step "probe $probe -> client.log: $(tail -c 200 "$work/client.log" 2> /dev/null)"
  done
  decomp input "$id" --events '[{"key": "m"}]' > /dev/null
  decomp input "$id" --events '[{"pointer": {"x": 300, "y": 300, "buttons": 0}}]' > /dev/null
  decomp capture "$id" --path "$work/cursor.png" > /dev/null
  step "cursor.png (mouse reporting off, pointer at 300,300):"
  base64 -w 0 "$work/cursor.png" >&2
  echo >&2
  failed=1
  step "foot pointer buttons: $(grep -a 'wl_pointer.*button' "$work/foot.log" | head -n 6)"
  step "foot keyboard enter: $(grep -a 'wl_keyboard.*enter' "$work/foot.log" | head -n 3)"
  step "foot saw: $(grep -a 'wl_pointer.*\(enter\|leave\|button\)\|wl_keyboard.*\(keymap\|enter\|leave\|key\)\|wl_seat.*capabilities' "$work/foot.log" | tail -n 12)"
  step "wayvnc asked: $(grep -a 'virtual_pointer\|wl_seat' "$work/displays/$id/wayvnc.log" | tail -n 30)"
  step "labwc did: $(grep -a 'virtual_pointer.*\(button\|motion\)\|wl_pointer.*button' "$work/displays/$id/labwc.log" | tail -n 30)"
  step "client.log at the end: $(tail -c 300 "$work/client.log" 2> /dev/null)"
  step "wayvnc: $(wayvnc --version 2>&1 | head -2) / labwc: $(labwc --version 2>&1 | head -1)"
  step "wayvnc options: $(wayvnc --help 2>&1 | tr '\n' '|' | cut -c1-900)"
  for f in labwc wayvnc; do step "$f.log: $(tail -c 600 "$work/displays/$id/$f.log" 2> /dev/null)"; done
fi
step "capture after"
decomp capture "$id" --path "$work/after.png" > /dev/null || echo "capture after: failed" >> "$out"
"$py" -c '
import sys
from PIL import Image, ImageChops
a, b = (Image.open(p).convert("RGB") for p in sys.argv[1:3])
print("capture changed: " + ("yes" if ImageChops.difference(a, b).getbbox() else "no"))
' "$work/before.png" "$work/after.png" >> "$out"
[ -n "${failed:-}" ] && { step "after.png:"; base64 -w 0 "$work/after.png" >&2; echo >&2; }
kill "$foot" 2> /dev/null
step "delete"
decomp delete "$id" > /dev/null && echo "delete: ok" >> "$out" || echo "delete: failed" >> "$out"
step "windows backend input"
win=$(cd "$repo/src" && "$py" -c '
import sys
from abu_decomp import cli
cli.parser()  # builds every command parser (their lazy imports) while the platform is still Linux
sys.platform = "win32"  # after the imports: the platform branches of other modules run on Linux
sys.exit(cli.main(["display", "input", "d-none", "--events", "[{\"key\": \"a\"}]"]))
' 2>&1)
case $win in
  *display.unsupported*Unsupported*) echo "windows input: Unsupported" >> "$out" ;;
  *) echo "windows input: $win" >> "$out" ;;
esac
cat "$out"
rm -rf "$work"
