# abu-decomp-64

Runs the N64 decompilation dev cycle over unbake, Recipe Maker and Cake Taste, driven by AI lanes,; the TAS page (`decomp tas serve`) shows the movies it plays.

> This is a personal tool. It is not officially maintained and may change or break without notice.

## Install

Python 3.13 or newer, in its own virtual environment.

```sh
python3.13 -m venv ~/.venvs/decomp && . ~/.venvs/decomp/bin/activate
pip install git+https://github.com/AbuN64/abu-decomp-64 && decomp dependencies
```

`decomp dependencies` checks that unbake, Recipe Maker and Cake Taste are installed from git, each at the commit it landed at.

## Usage

`decomp --profile seat.json tas serve` starts the TAS web page. Omitting `tas.serve.listen` binds all IPv4
interfaces at `0.0.0.0:8099`, making the page available from the LAN. The existing `listen` field can be set
explicitly in the seat profile (merge this fragment into its `tas` configuration):

```json
{"tas": {"serve": {"listen": "0.0.0.0:8099"}}}
```

```sh
decomp --profile seat.json unbake report --next
decomp port new --spec port.json ../my-port   # a port on the n64 shim (RageWarsEnhanced shim/)
```

```sh
decomp --profile seat.json behave --project TREE --host HOST --function func_80299B74_de --candidate cand.c
decomp --profile seat.json behave --project TREE --host HOST --function func_80299B74_de   # describe the original
```

`decomp behave` runs the original function and a candidate C function on the same generated inputs and compares their
behaviour: `same`, `differs` (first difference and the inputs that show it) or `untestable` (with the reason). A near miss
that is `same` is wrong only in code shape; one that `differs` has wrong logic. It never replaces the byte check. Without
`--candidate` it describes the original: the offsets it reads and writes through each pointer argument (a struct-typing
helper), its globals and callees. See [CONTRACTS.md](CONTRACTS.md), module `behave`.

Every command prints one JSON result. `decomp mcp` serves the same commands as MCP tools ([docs/mcp.md](docs/mcp.md)). `decomp --help` lists the rest.

`decomp cleanup owners --dir HEADERS` lists the owning module of every declaration of a header folder (definer for externs,
majority user for types and macros). Without `--dir` it plans the unit-local `extern` declarations of every source:
the owning header of an id is its definer's (for a symbol no C unit defines: the header of the unit whose `start`/`end`
address range in the module map holds it, else its majority user), the prototype is the definition's or the one every
declaration agrees on, and an id whose declarations conflict, or whose name an includer of the destination header spells
otherwise, is refused and listed with the reason. A unit sees a header only through its unconditional `#include`s (one under an
`#if`/`#ifdef` is not read by every build): a unit that has the destination header only that way gets a plain `#include`.
`--packs DIR` writes `externs-batch-NN.diff` (each over the tree the
earlier ones leave, applied like any tool batch: one stamp per batch), `externs.json` (unit-extern counts before and
after, per batch the estimated stamp CPU, the refusals) and `externs.tsv`; `--batch-cpu` and `--unit-cpu` size a batch
under the stamp CPU cap. Project facts come from `--map` (default `docs/MODULES.tsv`).

Phase playbooks (what each of the four phases and the naming/real-C track is for, its measurement and order of attack): [docs/playbooks](docs/playbooks/README.md).

## Development

```sh
cd <repo> && bin/test
```

`bin/test` is the one entry point and brings its own environment: it makes (or reuses) the repo-local `.venv` (git-ignored)
from `pyproject.toml`'s dependencies and dev extra (Cake Taste, unbake, Recipe Maker, gcovr, ruff, pytest and the rest), keyed
on a hash of `pyproject.toml` and of the commit each of our tools has landed at. `pyproject.toml` names our tools bare
(`unbake`, `recipemaker`, `cake-taste-64`) and pins no commit, so nothing drifts when a tool lands: the entry resolves each
name to its source repository's landed commit (`abu_decomp.sources.PACKAGES`, `landed_tools`), the same read `ensure_venv.py` makes. An unchanged tree whose tools have not moved reuses the environment at once; a change in either installs
again. It needs only a
`python3` of the version `pyproject.toml` asks for and reads no environment variable to find an interpreter.

- `bin/test` runs the full check (lint, format, every test).
- `bin/test tests/test_x.py tests/test_y.py` runs just those test files (or modules, `test_x`).
- The n64 shim is owned by RageWarsEnhanced (`shim/`): this repository holds no copy and reads the one found beside it (`abu_decomp.enhanced`, the parent of the git common directory plus `RageWarsEnhanced`). A missing sibling is refused naming the path to clone.
- `bin/test --changed` lints the changed files and runs only the tests that import the changed modules (under a second to
  seconds); run the full `bin/test` before a push that touches `ci/`.

A Proof for a card on this repo is `cd <repo> && bin/test <tests>`.

## License

Released under [GNU GPL version 3 or later](LICENSE).

## One owner per cache

AbuCache64 owns every cache, content store, worker slot, pool, executor and timing path (abucache.compile, store,
slots, pool, stages). `ci/abucache-audit`, run by the check entry point, refuses a new one here naming that owner; the
files it still lists move to abucache and leave the list, nothing joins it.

Slot admission belongs solely to `abucache.slots.Admission`. `abu_decomp.admission` declares
`Policy` values: 2 reserve cores, 0 GiB memory floor, 0.75 GiB per slot, 40 GiB disk
per slot, and no stamp-specific memory or width. The seat profile's `land.slots`
updates these values. TAS, replay, proof and stamp builds share AbuCache64's host
pool; refusal precedes the workload. Owner, interactive, batch and background
queue in that order, FIFO within each class. No local slot implementation remains.

For a two-repository development proof, prepare the private test environment with
`py=$(python3 ci/ensure_venv.py)` and
`"$py" -m pip install --no-deps --editable /path/to/AbuCache64`, then run `bin/test`
and AbuCache64's `ci/check`. This tests the shared worktree without a global install.

Coverage build identity reads use `abucache.memo`, keyed by operation and resolved port path. Each public coverage build clears that operation after taking its union lock; repeated reads within the build reuse the value.

Stage duration results use `abucache.stages.timed()` and its `Timing.seconds` observation (seconds, including running work).
This requires AbuCache64 with the duration observation API introduced at `43bb0be`; first-party dependencies resolve
their landed main revision rather than retaining a consumer commit pin. Deadline and heartbeat clocks retain their scheduling role.

The clean-environment bootstrap uses the standard-library-only `abu_decomp.sources` resolver, also used by
the deploy boundary. It never imports runtime tools before installing them. Its environment key includes
every resolved source revision, including AbuCache64, as well as `pyproject.toml`.
