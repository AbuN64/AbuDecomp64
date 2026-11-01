# Display

`decomp display` is the site's own headless display adapter: one interface, a Linux backend now. Nothing in it calls
another toolkit; a rig job and the runner run the same code.

```sh
decomp display create --size 1280x720        # -> {"identity": "d-1a2b3c4d", "width": 1280, "height": 720, ...}
decomp display describe d-1a2b3c4d
decomp display capture d-1a2b3c4d --path /abs/frame.png   # -> {"path", "width", "height", "non_black"}
decomp display delete d-1a2b3c4d             # stops the compositor and VNC server, removes the folder
```

`--dir` (before the verb) sets where displays are kept; the default is `~/.local/state/abu-decomp/displays`.

## Linux backend

`create` starts `labwc` (headless, pixman), sets the output to the size with `wlr-randr`, then starts `wayvnc` on a Unix
socket (`rfb.sock`) in `<dir>/<id>/`, and checks the socket reports that size. `capture` speaks RFB 3.8 (no auth, raw
true-colour) to that socket and writes one PNG; wayvnc's first update is the buffer kept since its last client left, so it
is dropped and the second is written. `delete` sends SIGTERM to each tool, waits on its pidfd (SIGKILL after
`GRACE_SECONDS`) and removes the folder. No TCP port is opened.

Host packages the rig's install names: `labwc`, `wayvnc`, `wlr-randr`. `create` refuses by name when one is missing; the
adapter never installs packages.

## Windows

Every verb refuses with `display.unsupported` until RUNNER-LIVE-DISPLAY fills the backend.
