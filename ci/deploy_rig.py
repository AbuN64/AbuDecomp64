#!/usr/bin/env python3
"""The rig deploy steps of AbuDecomp64: one step per `.deploy` line, each an entry point of this file. The close runs
them in file order on the rig at the landed revision, from the root of the tree, with LANDED_Projects_AbuN64_AbuDecomp64
set and `deploy-env TOOLKIT=<live link>` in the environment; it appends its own landing arguments, which no step reads.
A step prints its facts and exits 0, or prints one line on stderr and exits 1; it calls no other tool of the close.

  name       the engine's `decomp tools name` answers the build's NAME, a digest of every package commit the new toolkit
             holds (the live toolkit's, with abu-decomp-64 at the landed SHA), never the SHA alone: a toolkit
             build holds several packages (unbake, recipemaker and cake-taste-64 are carried as installed: CakeUnbake64
             deploys its own toolkit, never this one), so the name digests all of them. It also makes the sealed old
             builds `<toolkit>.<x>` that are neither live nor `.previous` writable (u+w) so the activation can delete
             them; the deploy itself deletes nothing;
  build      the activation (`activate`, BUILD is <toolkit>.<SHA>): `decomp tools deploy --package abu-decomp-64 --sha
             SHA` installs the landed commit into a fresh venv `<BUILD>.<NAME>` beside every other package of the live
             toolkit carried over as installed (`--carry-from` the live link; BUILD, the link the engine swaps, starts
             as scratch and is never read), BUILD ends linked to that venv, and the full test suite (pytest over tests/)
             runs in the tree with BUILD's python and the tree's src first on the import path. After the gate passes the
             built tree is made read-only (`chmod -R a-w`): the live toolkit is never written in place. The imported
             abu_decomp path must lie inside the tree. A failing suite exits non-zero, the live and previous links stay;
             on exit 0 the close switches the live link to BUILD and keeps the replaced venv as <toolkit>.previous;
  readback   the live link resolves to BUILD, a write into the live tree is refused (it is read-only), and the installed
             commit of AbuDecomp64 and of every direct git pin its pyproject has equals the pinned one. Any mismatch
             refuses (fix forward, no rollback). The deploy removes nothing: pruning old builds is the activation's;
  profile    the live tas profile (~/rig-state/tas/profile.json) is written from the landed tree's ci/tas-profile.json,
             never edited anywhere else: its `${home}` is the deploy user's home and every other `${name}` comes from
             the host's site file (~/.config/abu-decomp/site.json, a flat object of strings, one key per name the
             template uses). The previous live profile is not read, so a key the tree does not know cannot survive a
             deploy; a name without a value, a site key the template does not use or a template that is not a JSON
             object refuses before the live file is touched, and the file is replaced whole (never half written);
  serve      the site unit ci/abu-decomp-tas.service (ExecStart in the live toolkit link, Type=notify) is installed,
             enabled and restarted; it returns when the page listens, so the unit's own result is the signal. The page
             on :8099 is read back: its `deployed-sha` meta tag must equal the landed SHA.

  deploy-env TOOLKIT=/home/abu/venv-decomp
  rig run ci/deploy_rig.py name
  rig activate ci/deploy_rig.py /home/abu/venv-decomp build
  rig run ci/deploy_rig.py readback
  rig run ci/deploy_rig.py profile
  rig run ci/deploy_rig.py serve"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ENV = "LANDED_Projects_AbuN64_AbuDecomp64"
SHA = re.compile(r"[0-9a-f]{40}")
NAME_RE = re.compile(r"[0-9a-f]{12}")
TEMPLATE = Path("ci/tas-profile.json")  # in the tree: the one source of the live profile
SITE_FILE = Path(".config/abu-decomp/site.json")  # under the deploy user's home: the host's values for the template
LIVE_PROFILE = Path("rig-state/tas/profile.json")  # under the deploy user's home: what abu-decomp-tas.service reads
HOLE = re.compile(r"\$\{([a-z_]+)\}")

# bash(script, args) runs one of the scripts below and returns its exit code and, when captured, its stdout.
Bash = Callable[..., "tuple[int, str]"]

# NAME runs in the tree before the activation and answers the build's name: the engine's JSON envelope on stdout,
# whose result.name is the digest of the whole content. Arguments: SHA TOOLKIT.
NAME = r"""set -u
sha=$1 toolkit=$2
work=$(pwd -P)
(cd "$HOME" && PYTHONPATH="$work/src" "$toolkit/bin/python" -m abu_decomp tools name --package abu-decomp-64 \
  --sha "$sha" --carry-from "$toolkit") || { echo "name-error: decomp tools name exit $?" >&2; exit 1; }
"""

# BUILD runs inside the activation job with BUILD (<toolkit>.<SHA>) set by the close. It starts as a link to the live
# toolkit and `decomp tools deploy` builds `$BUILD.<name>` (or -N when that name is taken) and swaps only that link, so
# BUILD ends linked to the new venv, a failed gate leaves the live and previous links unchanged and the close's switch
# is the only way a venv goes live. The job runs with the caller's own environment as Dos admits it.
# Arguments: SHA TOOLKIT NAME.
BUILD = r"""set -u
sha=$1 toolkit=$2 name=$3
: "${BUILD:?the close sets BUILD for an activation}"
work=$(pwd -P)
# the link the deploy swaps is scratch it never reads: the packages carried over are the live toolkit's
ln -sfn "$(readlink -f "$toolkit")" "$BUILD" || exit 1
(cd "$HOME" && PYTHONPATH="$work/src" "$toolkit/bin/python" -m abu_decomp tools deploy --package abu-decomp-64 \
  --sha "$sha" --venv "$BUILD" --carry-from "$toolkit" --name "$name") \
  || { echo "deploy-error: decomp tools deploy exit $?" >&2; exit 1; }
built=$(readlink -f "$BUILD")
case $built in
  "$BUILD.$name" | "$BUILD.$name"-[0-9]*) ;;
  *) echo "deploy-error: $BUILD reads $built, not the build $name of $sha" >&2; exit 1 ;;
esac
echo "deploy-build: $built"
"""

# SUITE runs in the tree after BUILD (Arguments: SHA), with built and BUILD set: one `suite-KEY: value` line per
# fact, then the suite's own output.
SUITE = r"""set -u
sha=$1
work=$(mktemp -d) || exit 9
trap 'rm -rf "$work"' EXIT
echo "suite-checkout: $(pwd -P)"
echo "suite-sha: $(git rev-parse HEAD)"
head=$(git rev-parse HEAD)
[ "$head" = "$sha" ] || { echo "suite: the tree is at $head, not the landed $sha" >&2; exit 1; }
cat > "$work/runner.py" <<'PY'
__RUNNER__
PY
mkdir "$work/tmp" || exit 9
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=init.templateDir GIT_CONFIG_VALUE_0=
export TMPDIR=$work/tmp ABU_SLOTS_DIR=$(mktemp -d "$work/slots.XXXXXX")
export PYTHONPATH=$PWD/src PYTHONDONTWRITEBYTECODE=1
rc=0
"$BUILD/bin/python" "$work/runner.py" > "$work/suite.log" 2>&1 || rc=$?
cat "$work/suite.log"
if [ "$rc" = 0 ]; then
  # the deploy sealed the tree (chmod -R a-w) after its smoke test: open only the root for the one record, then seal
  # again, so the gate has passed before the tree goes read-only and the close flips the link to a tree nobody writes
  chmod u+w "$built" || exit 9
  { echo "suite-sha: $(git rev-parse HEAD)"; grep '^suite-\(summary\|import\)' "$work/suite.log"; } \
    > "$built/suite.txt" || exit 9
  chmod -R a-w "$built" || exit 9
fi
exit "$rc"
"""

# READBACK runs in the tree after the switch. Arguments: SHA TOOLKIT BUILD.
READBACK = r"""set -u
sha=$1 toolkit=$2 build=$3
live=$(readlink -f "$toolkit") || exit 9
want=$(readlink -f "$build") || exit 9
[ "$live" = "$want" ] || { echo "readback: $toolkit reads $live, not $build ($want)" >&2; exit 1; }
if touch "$toolkit/.readonly-probe" 2>/dev/null; then
  rm -f "$toolkit/.readonly-probe"
  echo "readback: a write into the live toolkit $toolkit succeeded, it must be read-only" >&2; exit 1
fi
echo "readback-readonly: a write into the live toolkit was refused"
"$toolkit/bin/python" - "$sha" <<'PY'
import importlib.metadata
import json
import re
import sys
import tomllib

sha = sys.argv[1]
PIN = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)\s*@\s*git\+(?P<url>https://[^@\s]+)@(?P<commit>[0-9a-f]{40})$")
with open("pyproject.toml", "rb") as handle:
    project = tomllib.load(handle)["project"]
pins = {project["name"]: sha}
for row in project.get("dependencies", []):
    match = PIN.match(row.strip())
    if match:
        pins[match["name"]] = match["commit"]
bad = []
for name, commit in sorted(pins.items()):
    try:
        direct = json.loads(importlib.metadata.distribution(name).read_text("direct_url.json") or "{}")
        installed = direct.get("vcs_info", {}).get("commit_id", "none")
    except importlib.metadata.PackageNotFoundError:
        installed = "not installed"
    print(f"readback-pin: {name} {installed}", flush=True)
    if installed != commit:
        bad.append(f"{name} is {installed}, the pin is {commit}")
if bad:
    sys.exit("readback: " + "; ".join(bad))
PY
"""

# SERVE runs in the tree after the readback. Argument: SHA. It refuses when the restarted page carries another sha.
SERVE = r"""set -u
sha=$1
unit=abu-decomp-tas.service
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
mkdir -p "$HOME/.config/systemd/user" || exit 9
install -m 644 "ci/$unit" "$HOME/.config/systemd/user/$unit" || exit 9
systemctl --user daemon-reload && systemctl --user enable "$unit" || exit 9
if ! systemctl --user restart "$unit"; then
  systemctl --user status --no-pager "$unit" | tail -n 5 >&2
  echo "serve: restart of $unit failed" >&2
  exit 1
fi
page=$(curl -fsS http://localhost:8099/) || { echo "serve: :8099 does not answer after the restart" >&2; exit 1; }
served=$(printf %s "$page" | sed -n 's/.*<meta name="deployed-sha" content="\([^"]*\)">.*/\1/p' | head -n 1)
[ "$served" = "$sha" ] || { echo "serve: :8099 serves ${served:-no deployed-sha}, not $sha" >&2; exit 1; }
echo "serve-sha: $served"
"""

# The runner: puts the checkout's src first, proves the package comes from there, runs pytest over tests/ and reports.
RUNNER = r"""import os
import sys
import time

root = os.path.realpath(os.getcwd())
sys.path.insert(0, os.path.join(root, "src"))
PACKAGE = "abu_decomp"


def emit(line):
    # a test may leave its own output without a newline: every fact starts on a fresh line
    sys.stdout.write("\n" + line + "\n")
    sys.stdout.flush()


def imported():
    return sorted({os.path.realpath(m.__file__) for n, m in sys.modules.items()
                   if (n == PACKAGE or n.startswith(PACKAGE + ".")) and getattr(m, "__file__", None)})


def inside(path):
    return path.startswith(root + os.sep)


__import__(PACKAGE)
emit("suite-import: " + os.path.realpath(sys.modules[PACKAGE].__file__))
import pytest  # noqa: E402


class Tally:
    def __init__(self):
        self.passed = self.failed = self.skipped = 0
        self.first = None

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.first = self.first or report.nodeid
        if hasattr(report, "context"):  # a subtest report: its test has its own report, pytest counts them apart
            return
        if report.failed:
            self.failed += 1
        elif report.skipped and report.when != "teardown":
            self.skipped += 1
        elif report.passed and report.when == "call":
            self.passed += 1

    def pytest_collectreport(self, report):
        if report.failed:
            self.failed += 1
            self.first = self.first or report.nodeid


tally = Tally()
start = time.monotonic()
# rootdir and config pinned to the checkout: node ids stay checkout-relative wherever the checkout sits
config = os.path.join(root, "pyproject.toml")
args = ["-q", "-p", "no:cacheprovider", "-rfE", "--tb=short", "--rootdir", root, "-c", config]
code = int(pytest.main(args + ["tests"], plugins=[tally]))
seconds = time.monotonic() - start
outside = [path for path in imported() if not inside(path)]
if outside:
    emit("suite-import: " + outside[0])
    sys.exit(3)
emit(f"suite-summary: {tally.passed} passed, {tally.failed} failed, {tally.skipped} skipped in {seconds:.0f} s")
if code or tally.first or not tally.passed:
    emit("suite-first-failure: " + (tally.first or f"pytest exit {code}, {tally.passed} passed"))
    sys.exit(1)
"""


class Refused(Exception):
    """One line cause of a refusal or failed step."""


def bash(script: str, *args: str, capture: bool = False) -> tuple[int, str]:
    done = subprocess.run(
        ["bash", "-c", script, "step", *args], stdout=subprocess.PIPE if capture else None, text=True, check=False
    )
    return done.returncode, done.stdout or ""


def unseal_old_builds(toolkit: str) -> list[str]:
    """Make the sealed old builds `<toolkit>.<x>` that are neither the live toolkit nor `.previous` writable by their
    owner (u+w on every folder), so the activation can delete them. Returns the folders opened. The deploy removes
    nothing itself: the activation owns pruning."""
    home, base = os.path.split(toolkit)
    keep = {os.path.realpath(toolkit), os.path.realpath(f"{toolkit}.previous")}
    opened = []
    for entry in sorted(os.listdir(home)):
        path = os.path.join(home, entry)
        folder = os.path.realpath(path)
        if not entry.startswith(f"{base}.") or entry == f"{base}.previous" or folder in keep or not os.path.isdir(folder):
            continue
        for here, _dirs, _ in os.walk(folder):
            os.chmod(here, os.stat(here).st_mode | stat.S_IWUSR)
        opened.append(folder)
    return sorted(set(opened))


def last_line(text: str, default: object) -> str:
    return text.strip().splitlines()[-1] if text.strip() else str(default)


def build_name(text: str) -> str:
    """The name the engine answered: result.name of the JSON envelope on the name step's stdout."""
    for line in reversed(text.splitlines()):
        try:
            name = json.loads(line)["result"]["name"]
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(name, str) and NAME_RE.fullmatch(name):
            return name
    raise Refused(f"the name step answered no build name: {last_line(text, 'no output')}")


def landed(environ: dict[str, str]) -> tuple[str, str]:
    """The landed SHA and the live toolkit link, each from its own explicit variable."""
    sha = environ.get(ENV, "")
    if not SHA.fullmatch(sha):
        raise Refused(f"{ENV} is not a 40-hex SHA: {sha!r}")
    toolkit = environ.get("TOOLKIT", "")
    if not toolkit.startswith("/"):
        raise Refused(f"TOOLKIT is not an absolute path (a `deploy-env TOOLKIT=<live link>` line): {toolkit!r}")
    return sha, toolkit


def name(sha: str, toolkit: str, run: Bash) -> str:
    code, out = run(NAME, sha, toolkit, capture=True)
    sys.stdout.write(out)
    if code:
        raise Refused(f"name exit {code}: {last_line(out, 'no output')}")
    return build_name(out)


def render_profile(template: str, home: str, site: dict[str, object]) -> str:
    """The template with every `${name}` filled: home from the deploy user, the rest from the host's site file."""
    if "home" in site:
        raise Refused(f"{SITE_FILE} sets home, which is the deploy user's home")
    holes = set(HOLE.findall(template))
    values = {"home": home, **{key: value for key, value in site.items() if isinstance(value, str)}}
    unset = sorted(holes - values.keys())
    if unset:
        raise Refused(f"{SITE_FILE} has no value for: {', '.join(unset)}")
    unused = sorted(site.keys() - holes)
    if unused:
        raise Refused(f"{SITE_FILE} has keys {TEMPLATE} does not use: {', '.join(unused)}")
    text = HOLE.sub(lambda hole: values[hole[1]], template)
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise Refused(f"{TEMPLATE} renders to invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise Refused(f"{TEMPLATE} renders to {type(parsed).__name__}, not an object")
    return text


def install_profile(template: Path, home: Path) -> Path:
    """Write the live profile whole from the tree's template; the previous live file is replaced, never read."""
    try:
        site = json.loads((home / SITE_FILE).read_text())
        text = template.read_text()
    except (OSError, ValueError) as exc:
        raise Refused(f"profile input: {exc}") from exc
    if not isinstance(site, dict) or not all(isinstance(value, str) for value in site.values()):
        raise Refused(f"{SITE_FILE} is not an object of strings")
    rendered = render_profile(text, str(home), site)
    live = home / LIVE_PROFILE
    staged = live.with_name(f"{live.name}.{os.getpid()}.new")
    try:
        staged.write_text(rendered)
        os.replace(staged, live)
    except OSError as exc:
        staged.unlink(missing_ok=True)
        raise Refused(f"profile write: {exc}") from exc
    return live


def step_name(sha: str, toolkit: str, run: Bash, environ: dict[str, str]) -> None:
    revision = name(sha, toolkit, run)
    opened = unseal_old_builds(toolkit)  # a stuck activation cannot delete a sealed read-only build
    sys.stdout.write(f"unseal: {len(opened)} old builds writable for the activation\n")
    sys.stdout.write(f"name: {revision}\n")
    sys.stdout.flush()


def step_build(sha: str, toolkit: str, run: Bash, environ: dict[str, str]) -> None:
    revision = name(sha, toolkit, run)
    code, _ = run(BUILD + SUITE.replace("__RUNNER__", RUNNER), sha, toolkit, revision)
    if code:
        raise Refused(f"build exit {code}, links unchanged")


def step_readback(sha: str, toolkit: str, run: Bash, environ: dict[str, str]) -> None:
    code, _ = run(READBACK, sha, toolkit, f"{toolkit}.{sha}")
    if code:
        raise Refused(f"readback exit {code}")


def step_profile(sha: str, toolkit: str, run: Bash, environ: dict[str, str]) -> None:
    home = environ.get("HOME", "")
    if not home.startswith("/"):
        raise Refused(f"HOME is not an absolute path: {home!r}")
    sys.stdout.write(f"profile: {install_profile(TEMPLATE, Path(home))} from {TEMPLATE} at {sha}\n")
    sys.stdout.flush()


def step_serve(sha: str, toolkit: str, run: Bash, environ: dict[str, str]) -> None:
    code, _ = run(SERVE, sha)
    if code:
        raise Refused(f"serve exit {code}")


STEPS = {
    "name": step_name,
    "build": step_build,
    "readback": step_readback,
    "profile": step_profile,
    "serve": step_serve,
}


def main(argv: list[str] | None = None, environ: dict[str, str] | None = None, run: Bash = bash) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        step = STEPS.get(argv[0] if argv else "")
        if step is None:
            raise Refused(f"the step is one of {', '.join(STEPS)}, not {argv[0] if argv else 'nothing'!r}")
        environ = dict(os.environ) if environ is None else environ
        step(*landed(environ), run, environ)
    except Refused as refusal:
        sys.stderr.write(f"deploy refused: {refusal}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
