# Match And Peel

Generic method for a port that must first equal its oracle (the original cartridge) and is then simplified. Game facts come from the project profile and the game's own Match-And-Peel playbook; this one names none.

## Phase 1: match

Build the port exact, rickety allowed, 1:1 with the oracle, until the whole-run census is zero in every class. Efficiency is not a goal here; equality is.

## The match loop

1. Record one whole-run lockstep record of the oracle.
2. Dump at every sweep poll, in one serial admitted pass.
3. Find the roots of the divergences.
4. Write a class table in the sweep notes before any code.
5. Fix one batch: one regression slice of the real record per class.
6. Run every gate keep-going and record the whole refusal list.
7. Prove with one whole-run lockstep against the previous one.

Never one guess, one targeted capture or one instance fixed without its class swept.

```sh
bin/test
```

## Masks

A value is masked only when nothing reads it (a host execution artefact), and a contract test proves no reader. A value anything reads is modeled, not masked.

## The marker

Every piece of code that exists only to match the cartridge (modeled latency, deliberate slow path, emulated hardware quirk) carries one comment:

```
TODO remove when <condition>: <class> <what the cartridge does>; undo = <efficient host behaviour>
```

The game's own Match-And-Peel playbook lists each class as `class | sites | condition | undo`. Masks are not markers.

## Unit-test philosophy

Unit tests pin observable behaviour, never the match-only mechanism, so phase 2 can change the mechanism under them. Each regression slice is real recorded data and runs in seconds.

## Phase 2: peel

Once the census is zero and unit tests pin behaviour, remove one class at a time behind an identical facade: same observable behaviour, the build still presenting the same interface. After each class, lockstep and unit tests are green; delete its comments and its table row. The peel is done when the table is empty.
