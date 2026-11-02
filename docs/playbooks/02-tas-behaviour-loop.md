# 02 TAS behaviour loop

Goal: close the last functions and behaviours once the port draws the game.
After first light the remaining failures appear only while playing: a menu
choice that crashes, a screen that draws wrong, a path the boot never took.
This loop turns each one into named, behaving C in the decomp.

## The loop

1. **Play it once, record it.** Drive the port from the stream or a pad with
   recording on. Every controller poll is logged (buttons and stick), so a run
   is a replay from reset, not a video. Save the run under a short name that
   says what it does (`pak-continue-no-save`, `options-sound`).
2. **Validate, replay and measure.** Before any reader trusts a run record,
   run `shim/boot/lockstep.py check RUN`, where `RUN` is its completed run folder;
   stop the cycle if validation fails. `decomp tas play NAME` boots the port
   with the movie as its only input and reports one outcome: `ok`, `crash`
   (with the fault subject and the guest backtrace), `hang` (no poll for the
   watchdog window), or `stop` (the port's own boot stop). Replays are
   deterministic because input is keyed to the poll index, not wall time. The
   oracle is not involved here; the port runs at full speed. Validate each replay's
   record before reading it, then run `shim/boot/lockstep.py match --run RUN` to
   list divergences in `RUN/lockstep/match.json`, followed by `decomp tas coverage`
   to measure covered/total functions in `runs/coverage-table.json`. If the run
   lacks lockstep evidence, capture it as described below; never report missing
   evidence as zero divergences left.
3. **Crash or weird: take the functions.** From the outcome take the guest
   frames of the faulting thread, plus the function that wrote the bad value
   when a watch names it. Those, and their direct callees on the path, are the
   suspects. A crash in a library or layer function means its caller passed a
   bad value; walk up until the frame is game code.
4. **Permute behaviour against the clean reference.** For each suspect run
   `decomp behave --function SYM` (no candidate first: it describes what the
   original reads, writes and calls), then with the decomp's current C as the
   candidate. `behave` runs the original recompiled from the ROM and the
   candidate side by side over seeded and captured inputs through Cake Taste's
   clean probe interface and names the first difference with its inputs.
   `differs` is the bug; `untestable` says what to capture next.
5. **Iterate the C until it behaves.** Fix the body, rerun `behave` until
   `same`. Keep it ostensibly what the original authors wrote: no host hacks,
   no `#ifdef` for the port.
6. **Give it a clean name, then land it.** Name the function, its locals,
   fields and file through Recipe (`recipe name` / `structure`). A function
   leaves this loop named and documented, never as `func_XXXXXXXX`. Land it
   through `decomp land` / `decomp ship` (a commit on the card branch; Land pushes it), matched if the bytes match, else as
   the file's non-matching body, so the port and the decomp share one body.
7. **Replay again.** The same movie must now pass or fail later. A movie that
   passes becomes a regression test: it stays in the TAS folder and runs after
   every port build. Repeat step 2's check, match and coverage sequence on the
   final replays. Report `covered/total functions` and `divergences left` as
   measured integer counts from the coverage table and match reports.

## Determinism comes first

A movie is only evidence if replaying it plays the same game. Prove it before trusting any replay:
replay one movie twice and compare the per-poll shots (`N64C_TAS_SHOT_EVERY`); they must match byte for byte.
When they do not, find the sources before anything else:
- the game's random-number seed: find its generator (a linear congruential step on a global word) and the
  function that writes the seed; a constant seed is fine, a seed from a clock or counter is not;
- every clock the game reads (Count, osGetTime, VI count): in the port these must be the virtual clock,
  never wall time;
- thread order: the port's scheduler must run one guest thread at a time in a fixed order.
- host behaviour: nothing the host does (options screen, speed, frame-rate unlock or cap, live take/release) may
  feed the guest a pad, a poll or a retrace the recomp would not see; the options screen holds the guest (the frame
  loop presents only: no poll, no VI, no clock) instead of feeding it a neutral pad. Prove each with the
  `N64C_TAS_HASH` trace byte-identical with and without the behaviour on one movie.
- host code addresses: guest data holds native function pointers (callback tables compiled for the host), which
  move whenever any host code changes size. The full-RDRAM hash (`N64C_TAS_HASH`) and dump (`N64C_TAS_DUMP_AT`)
  read every word that points into host code as `N64C_TAS_CODE_WORD` ("CODE", 0x434F4445), on Linux (the ELF
  range `__executable_start` to `etext`) and on Windows (the executable sections the loaded image's PE headers
  name from `__ImageBase`), so two builds that differ only in host code give the same trace.

## Finding the cause, in order of cost

1. The stop names its subject and the run folder holds stacks; every stop path (host fault, code pointer,
   TLB miss, MMIO) must leave them, or that is the first defect to fix.
2. `decomp behave` on the top suspects. A verdict counts only with its counts: `same` over mostly
   untestable cases is no evidence.
3. A wrong value: replay under gdb, record the address at the point it was right, set a hardware
   watchpoint, and let it name the writer. Then compare the writer's stores with the original asm.
4. Lockstep (port vs oracle on the same movie) when the oracle runs: `shim/boot/lockstep.py match --oracle DIR --port DIR` keys the whole movie by poll and lists, per class (input, vi, calls, memory), the persistent divergences in poll order, the matched-through poll and the first divergence; the port run for it sets both `N64C_LOCKSTEP=1 N64C_LOCKSTEP_DIR=DIR` (the poll lines, lockstep.jsonl: page hashes only) and `N64C_CALLS=DIR` (the call lines the calls class keys on, calls.jsonl beside them) in one run; both sides write the same `poll` line, so a matching poll is the same line byte for byte. On a run record (CONTRACTS.md "Run record") play it with `decomp tas play MOVIE --oracle --lockstep` (both sides record into RUN/lockstep/{port,oracle} and the play runs `lockstep.py match --run RUN` into RUN/lockstep/match.json at its end; no hand-set N64C_LOCKSTEP/N64R_LOCKSTEP_DIR), then `lockstep.py check RUN` before any reader trusts it. Both sides must reach the movie's last poll: compare poll counts in match.json before interpreting divergences. Process exit completes each side independently; at turbo, new poll file events renew the port's configured timeout_seconds inactivity bound. The port half exists only in the TAS flavour (`-DRAGE_DECOMP_TRACE=ON -DRAGE_DECOMP_COVERAGE=ON`; the plain release build writes no lockstep files) and the play runs both sides at once, so the whole pp-1p-match movie is one rig job (no per-side split: two sequential halves cost twice the wall time). `tas play` refuses `slots.disk_floor` when the rig disk under rig-state/slots has under 40 GiB free; a killed whole-movie play leaves its RUN/lockstep behind, so remove your own card's stale runs before replaying. Use the explicit 5,400 s rig job bound and wait for its completion answer. Measure completed replay cost separately from build cost; the retained hash-only incomplete play took 196.7 s, which does not measure a completed movie. The record is page hashes only: a page whose hashes differ is listed as a `page` row until its bytes are dumped. `decomp tas lockstep-dump RUN` replays both sides to the poll match.json's `dump` names, writes just those pages to RUN/lockstep/<side>/dump/POLL/, and reruns match, which compares them word by word; inspect the first divergent poll once, then repair its owning cause before replaying; never loop one dump replay per divergent poll.

## Port fixes are backported at once

When a cycle fixes the port for something that lives in the game (its code, its data, a raw address the
data holds, a layout), the same cycle fixes the decomp too: the decomp source must say what the original
said, so a fresh port built from it needs no workaround. The cycle closes only when both land. A port fix
that models hardware (a device, a clock, memory behaviour) needs no backport; say so in the commit.

## Every fix is a class

A defect found once exists elsewhere. Before closing a loop, name its class, write the detector that finds
every site across the whole ROM, and make it a gate that refuses new sites (a baseline that only goes down).
Cycle 1 produced three: struct fields whose names state an offset the layout does not give
(`unbake audit` `source.offset-name`), declarations that disagree with definitions (`gates.py --lto` over
every unit), and code words held as raw numbers in data (`addrscan`/`image`, including blobs and entries
inside a function's prologue).

## Rules

- A movie is input only. It names the port build and ROM digest it was
  recorded on; replay refuses a different ROM.
- Never fix the port's layer to hide a game-code difference that `behave`
  shows. Fix the layer only when the original behaves the same and the
  difference is in the hardware model.
- One movie per behaviour. Short movies that reach one screen beat one long
  playthrough; a failing long movie is cut down to the shortest prefix that
  still fails before triage.
- Journal each loop: the movie, the outcome, the suspects, the `behave`
  verdicts, the fix and the names given. The journal is evidence, written as
  it happens.

## Commands

| Step | Command |
|---|---|
| Record, save, list, delete | stream TAS controls, or `decomp tas save|list|show|delete` |
| Replay | `decomp tas play NAME [--build DIR]` |
| Describe / compare | `decomp behave --project TREE --host FILE --function SYM [--candidate FILE.c]` |
| Name | `recipe name`, `recipe structure` |
| Land | `decomp land`, `decomp ship` |
