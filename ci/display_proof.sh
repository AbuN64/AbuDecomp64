#!/bin/bash
# Proof of `decomp display` on a host with labwc and wayvnc: create 1280x720, describe, capture, delete; prints one
# result line per verb. Run from the repo root (a rig job's worktree) with the Python that has Pillow as the first
# argument; the display lives under the job's own TMPDIR.
set -u
repo=$PWD
py=${1:-python3}
work=$(mktemp -d "${TMPDIR:-/tmp}/display-proof.XXXXXX")
dd() { (cd "$repo/src" && "$py" -m abu_decomp display --dir "$work/displays" "$@"); }
field() { "$py" -c 'import json,sys; print(json.load(sys.stdin)["result"]["'"$1"'"])'; }
create=$(dd create --size 1280x720) || { echo "create: failed"; exit 1; }
id=$(echo "$create" | field identity) || exit 1
echo "create: ok $id $(echo "$create" | field width)x$(echo "$create" | field height)"
describe=$(dd describe "$id")
echo "describe: $describe"
pids=$(echo "$describe" | "$py" -c 'import json,sys; print(*json.load(sys.stdin)["result"]["pids"].values())')
cap=$(dd capture "$id" --path "$work/frame.png") && echo "capture: $cap" || echo "capture: failed"
dd delete "$id" > /dev/null || echo "delete: failed"
left=""
for p in $pids; do kill -0 "$p" 2>/dev/null && left="$left pid$p"; done
[ -e "$work/displays/$id" ] && left="$left folder"
if [ -z "$left" ]; then echo "delete: clean"; else echo "delete: left$left"; fi
rm -rf "$work"
