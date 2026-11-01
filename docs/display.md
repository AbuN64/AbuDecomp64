# Display

`decomp display` is the site's own headless display adapter: one interface, a Linux and a Windows backend. Nothing in it calls
another toolkit; a rig job and the runner run the same code.

```sh
decomp display create --size 1280x720        # -> {"identity": "d-1a2b3c4d", "width": 1280, "height": 720, ...}
decomp display describe d-1a2b3c4d
decomp display capture d-1a2b3c4d --path /abs/frame.png   # -> {"path", "width", "height", "non_black"}
decomp display input d-1a2b3c4d --events '[{"key": "F10"}, {"click": {"x": 640, "y": 360}}]'   # -> {"events": 2, "strokes": 5}
decomp display delete d-1a2b3c4d             # stops the compositor and VNC server, removes the folder
```

`--dir` (before the verb) sets where displays are kept; the default is `~/.local/state/abu-decomp/displays`.

## Linux backend

`create` starts `labwc` (headless, pixman), sets the output to the size with `wlr-randr`, then starts `wayvnc` on a Unix
socket (`rfb.sock`) in `<dir>/<id>/`, and checks the socket reports that size. `capture` speaks RFB 3.8 (no auth, raw
true-colour) to that socket and writes one PNG; wayvnc's first update is the buffer kept since its last client left, so it
is dropped and the second is written. `input` sends KeyEvent and PointerEvent messages over RFB through the display's holder: `create` also starts one small
process (`holder`, `display.py` run as a script) that keeps a single RFB connection open for the display's life and listens on
`hold.sock` in the display's folder. wayvnc makes a virtual keyboard and pointer per RFB connection and drops them on
disconnect, so a client such as foot only gets pointer focus and a click only lands while one connection lives; the holder is
that connection. One `input` call is one request: the holder sends the whole events list in order (a framebuffer round trip
follows every button change and ends the list, so the server has taken them all) and answers `ok`. `--events` is a JSON list; each event holds exactly one member and the whole list is
validated (known key, text with a keysym, x,y inside the display) before anything is sent:

| Event | Sends |
|---|---|
| `{"key": "Control_L+c"}` | the keys down in order, then up in reverse; a name (`Return`, `Escape`, `F1`-`F12`, arrows, `plus`, ...) or one character; a lone `+` is the plus key |
| `{"text": "hi"}` | each character as a key press and release |
| `{"pointer": {"x": 10, "y": 20, "buttons": 0}}` | one absolute move holding the button mask (1 left, 2 middle, 4 right) |
| `{"click": {"x": 10, "y": 20, "button": "left"}}` | move, press, release (`left` default, `middle`, `right`) |
| `{"keydown": "Left"}` / `{"keyup": "Left"}` | one key (or chord) held down, or released: a game polling its keyboard state sees the hold between the two events |

Every wait is bounded by a declared value and fails by name: `create` gives labwc's socket, `wlr-randr` and wayvnc's
socket `START_SECONDS` each (a tool that exits wakes it through its pidfd), and each RFB read gets `RFB_SECONDS`
(`display.rfb`: "RFB dial: wayvnc sent nothing").

`delete` sends SIGTERM to each tool and the holder, waits on its pidfd (SIGKILL after
`GRACE_SECONDS`) and removes the folder. No TCP port is opened.

Host packages the rig's install names: `labwc`, `wayvnc`, `wlr-randr`. `create` refuses by name when one is missing; the
adapter never installs packages.

## Runner page

`/viewer/<id>` sends the canvas's keys down and up over RFB by physical key (a release always matches its press, and a
blur releases what is held). Its first browser gamepad is polled each frame and mapped onto the same keys, so the game
sees a keyboard: d-pad and left stick the arrows, A x, B c, X s, Y d, LB q, RB e, LT z, RT r, Back Tab, Start Return.
Events also go to `POST /api/display/<id>/input` (`{"events": [...]}`): the same `Input` action the CLI uses.

## Windows

`--backend windows` (the default where `sys.platform` is `win32`) reaches a guest desktop over VNC. The guest's own VNC
server (tvnserver) is not ours to start or stop, so the site's config names its endpoint and `create` only proves it
answers.

```sh
decomp display --backend windows create --endpoint HOST:PORT   # redials a bounded number of times on an EOF right after ServerInit
decomp display --backend windows capture d-1a2b3c4d --path /abs/frame.png
decomp display --backend windows delete d-1a2b3c4d             # forgets the record, stops nothing on the guest
```

`create` refuses `display.endpoint` without `--endpoint HOST:PORT` (a Linux `create` refuses it with one: it starts its
own VNC server). `describe` and `capture` speak RFB to the endpoint like the Linux backend; the record carries the size
the guest reports, and `--size` is the guest's own. `input` and `invoke` refuse `display.unsupported` ("Unsupported: ...")
because a Windows guest takes input from its own viewer session and runs its own programs. A display id the directory
does not hold refuses `display.notfound` naming it (`no display 'd-none'`); the site answers `GET /viewer/<id>` for it
with a 404 of the same text.

## Proof

`ci/display_proof.sh` covers create, describe, capture and delete. `ci/display_input_proof.sh PYTHON LANE_DIR` runs `foot`
on a display, sends it a key and a click through `decomp display input`, and writes the RESULT.md lines: the client
(`ci/display_input_client.py`) reports the key and the pointer press it received, and the frames captured before and after
are compared. Run it as a rig job.
