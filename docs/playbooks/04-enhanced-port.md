# 04 Enhanced port

Goal: a player-facing port that improves on the faithful one (settings, display, input, packages), stands on its own,
and then grows from console-style code into clean generic code, without ever playing the game differently.

Use when the faithful port has reached its release stage ([4-staged-releases](4-staged-releases.md)) and the
enhancements those rules defer are now the work. The code-replacement half of it is
[5-ship-of-theseus](5-ship-of-theseus.md), applied to the fork described here.

## The enhanced port stands on its own

1. **A fork, not a dependency.** The enhanced port carries its own copy of the decomp C and of the shim as source in
   its tree. The faithful port and the decomp repository go on without it.
2. **Fork points are recorded.** The port's provenance file names, for the decomp C and for the shim, the commit each
   copy was taken from and the date. It is the only link back; it is a record, not an input to the build.
3. **No build-time tie to the decomp repository.** No submodule, no fetch step, no commit pin. A fresh clone of the
   enhanced port builds and runs with nothing else checked out.
4. **No toolchain product is required.** The port builds with the platform's ordinary compiler, CMake and the
   package tools of each target, so anyone can build it.
5. **Fixes flow back as backport cards.** A fix worth having in the decomp (a wrong function, a shim defect) is
   carded against the decomp repository, where the shim is cleaned and standardised, and lands there as a normal
   change. The fork never waits for it and never pulls it back in; the provenance file gets a new line only if a copy
   is deliberately refreshed.
6. **The fork may diverge.** Shim code in the fork is the port's own: it is reshaped for the port's needs, not kept
   identical to the standardised shim.

## MVP first

The MVP is the shortest list a player would call a finished port. Write it as a table before any code:

| Item | Player-visible acceptance | Rows that make it real |
|---|---|---|

The items, each with an acceptance a player can see:

- **Clean quit and saved settings.** Quit from the menu, closing the window and the platform's back action all exit
  with code 0, and the next launch shows the same settings.
- **One settings file.** Options and controls come from one file; no second store appears.
- **Display options.** Window size, mode and scale apply at once; a bad value is refused and the old one stays; the
  choice survives a restart.
- **Framerate unlock.** A menu toggle unlocks it and persists. A cap or vsync that is not built shows as
  unsupported, never as fake success.
- **Rebinding.** Keyboard and gamepad controls are rebound or unbound in the menu, the new input works and the old
  stops, it persists, and an invalid choice is refused with the old binding intact.
- **Installable packages per platform.** Each package installs, asks for the cartridge dump on first start (none is
  ever packaged), and keeps saves and settings across an update.
- **No gameplay regression.** With default settings the game plays every recorded TAS run identically to the
  baseline taken before the enhancements.
- **A public-runtime proof per platform.** The installed package launches under the platform's real display or
  device, passes the settings and binding checks above, and quits clean. A structural claim or a unit test is not
  runtime acceptance.

How an item becomes real:

- **Vertical-slice rows.** Each item is split into rows of one agent run, every row a thin end-to-end slice through
  contract, code, adapter and test, with unbuilt options as explicit stubs. The MVP table lists the rows per item;
  a row belongs to the item whose acceptance it moves.
- **Proofs come last.** The platform proofs wait on every integration they exercise; a final gate row reads the table
  back against the proofs and the replay comparison, and lists what is still open.
- **Launch blockers are listed and owned.** Every known bug that stops a platform from launching goes in a short
  list with the rows that fix it and the proof that clears it. A blocker with no row is the first thing to fix.
- **Unproved platform gaps are listed too**, so a missing driver or an unavailable device is visible instead of
  assumed.
- **Out-of-MVP items are named** (for example mouse look, touch layouts, extra controllers, independent bindings for
  other players, a framerate cap, vsync) with no row, so they are not mistaken for forgotten work.

Determinism gates the replay comparison: if the replay of the baseline does not repeat itself
([02-tas-behaviour-loop](02-tas-behaviour-loop.md)), the regression item cannot be judged and its chain is the first
blocker.

## After the MVP: from console style to clean generic code

The enhanced fork's long-term goal is to leave the console style behind, subsystem by subsystem, until the code
reads like ordinary modern code of its kind (a graphics layer that looks like a graphics layer, not like a display
list interpreter).

- **One subsystem at a time, graphics first**, then the next that the readiness census ranks highest. The order is
  the plank order of [5-ship-of-theseus](5-ship-of-theseus.md), taken on the fork.
- **The frozen reference is the oracle.** The reference build of the faithful port is the comparison for every step:
  the recorded runs, by tick state, on every platform, plus the performance gate. A step that changes behaviour
  fails its replay and does not land.
- **One step, proven, then the next.** No toggle forks the fork again: the old path is deleted once the new one is
  proven.
- **Keep the ledger.** The fork records, per unit, whether it is still console-style or already clean generic code,
  with the commit that converted it. Its share is the progress number.
- **Backport what is decomp-worthy.** A wrong decompiled function found on the way is fixed in the decomp, by card;
  a conversion itself stays in the fork.
