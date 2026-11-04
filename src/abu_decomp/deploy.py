"""`decomp tools deploy`: install ONE named package at one commit into a toolkit venv.

No repository pins another of our tools by commit, so the deploy of a package (`--package`, `--sha`) installs that
package at that commit, moves every other tool of ours the live venv holds (`sources.PACKAGES`) to its source commit
(`sources.landed_commit`, installed with its dependencies: each tool's land deploys its own toolkit, never this
one, so a tool carried as installed would never move), and carries every other package as it is installed (the
exact commit of a git install, the exact version of the rest). The package is installed from a git commit only
(`name @ git+URL@SHA`, a full 40-hex SHA; a wheel or a branch is refused), so a venv's provenance is
always a commit that `decomp dependencies` can check.

The carried set is read from the venv being replaced, or from `--carry-from PATH` when the venv named by `--venv` is
scratch the caller recreates (the rig's `<toolkit>-stage` link): that is the live toolkit link, read before the new venv
is built, and the scratch venv is never read. A live install that is not a git commit (a path, an archive, an editable
tree) refuses the deploy naming its origin.

A game project pins none of our tools (it builds with its own make), so a deploy never asks a project to move. It
declares the Python packages its make runs (splat, spimdisasm, ...) in `requirements.txt` at its root, each pinned
`name==version`; the deploy installs them into every venv beside the decomp commit and its `dev` extra, so a plain
deploy gives a working venv with no hand reinstall. The file is read from the project repository at one revision,
never from a work tree (a stale or detached tree cannot drop packages): `--project-sha`, else the project's main (the
profile's `land.remote` / `land.branch`, default origin/main) fetched at deploy time. The smoke test imports every
declared package from the new venv, so a venv that cannot import one is a failed deploy.

When the package is a tool, the checkout the seat names for it in `tools.checkouts` (a tool's code run from a git
checkout) is brought to the deployed commit (fetch, detached checkout) and refused, untouched, when it has local
changes.

A toolkit build holds several packages, so its name identifies all of its content, never one package's commit: `<name>`
is the first 12 hex digits of the sha256 of every requirement the build holds (the carried set, the package at its
commit, the project's pinned packages), sorted. Two builds of one content share a name (a redeploy is the already-active
no-op); a build that differs in any package gets another. `decomp tools name` answers that name without building, for
a deployer that must name the build before the activation (`content_name` is the one rule, used by both).

The package's own commit is the one Land has just pushed to Gitea, which fans it out to the GitHub mirror some time
later, so pip must not fetch it from there: the requirement and the recorded provenance stay `name @ git+URL@SHA`
(the source URL), but the pip run that installs it rewrites that URL to this deploy's tree
(`tree_fetch`; the activation job's checkout at the landed revision). No wait, no retry.

A deploy never changes a file under a running process. Each `--venv PATH` is a live path that is a symlink: the
carried packages and the package are installed into a fresh venv beside it (`PATH.<name>`, pip run by that venv's own
python so console script shebangs point into it), the smoke test (`decomp dependencies` for provenance and `unbake
symbols --version V` on the project, `unbake` by name) runs against the fresh venv, the fresh tree is made
read-only (`chmod -R a-w`, so
a job's pip install into the live toolkit fails loudly instead of rewriting it in place), and only then is the symlink
swapped with rename(2). The venv keeps the unbake library (decomp imports it) but none of its commands: the install
ends by removing the console scripts the unbake distribution declares (`retire`), so `unbake` and `unbake-run` resolve
by name to CakeUnbake64's published commands on the job PATH, never to a carried copy.
Processes started through PATH's console scripts keep their versioned tree; superseded
versions stay until purge removes them (policy `venv_versions`: not the live target, not the newest rollback, no
process using it). A live path that is still a real directory is refused (a deploy only swaps symlinks), and the link
is read back.

The new tool's Makefile and local.mk are one contract with the trees that run them (a retired recipe variable or
cc_cache.py mode in a tree's files fails every build there, 10-27), so a deploy ends by regenerating both in every
work tree of the project (`git worktree list`) with the new venv's `unbake sync --makefile`. A tree some live process
runs in is skipped and named; a tree whose own content refuses the sync is named with the refusal. Neither fails the
deploy: `resync` in the result lists every tree with its outcome.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from abu_decomp import DecompError, layout, sources, tools
from abu_decomp.config import ConfigError, Profile

SHA = re.compile(r"[0-9a-f]{40}")
_EXACT = re.compile(r"^[A-Za-z0-9._-]+(\[[A-Za-z0-9._,-]+\])?==[A-Za-z0-9.!+_*-]+$")


Requirements = Callable[[Path, str, str, str | None], "tuple[str, str]"]
Run = Callable[..., "tuple[int, str]"]  # (argv, cwd, env=None): `env` is added to the process environment


class DeployError(DecompError):
    pass


def requirements_at(project: Path, remote: str, branch: str, sha: str | None) -> tuple[str, str]:
    """(commit, text of `requirements.txt` there) from the project repository: `sha` when given, else the tip of
    `remote`/`branch` fetched now. The text is empty when that commit has no such file."""

    def git(*argv: str) -> str:
        done = subprocess.run(["git", "-C", str(project), *argv], capture_output=True, text=True, check=False)
        if done.returncode:
            raise DeployError(
                "deploy.requirements", f"{project}: git {' '.join(argv)} failed: {done.stderr.strip()[-300:]}"
            )
        return done.stdout

    if sha is None:
        git("fetch", "-q", remote, branch)
    commit = git("rev-parse", "--verify", f"{sha or 'FETCH_HEAD'}^{{commit}}").strip()
    listed = git("ls-tree", "--name-only", commit, "requirements.txt")
    return commit, git("show", f"{commit}:requirements.txt") if listed.strip() else ""


def run_command(argv: Sequence[str], cwd: Path | None = None, env: Mapping[str, str] | None = None) -> tuple[int, str]:
    done = subprocess.run(
        list(argv), cwd=cwd, env={**os.environ, **env} if env else None, capture_output=True, text=True, check=False
    )
    return done.returncode, (done.stdout + done.stderr)[-200_000:]


def landed_tools(run: Run = run_command) -> dict[str, str]:
    """Landed commits for every sibling tool, including AbuCache64."""
    return {
        name: sources.landed_commit(url, run) for name, url in sources.PACKAGES.items() if name != tools.DISTRIBUTION
    }


def check_sha(label: str, value: str) -> str:
    if not SHA.fullmatch(value):
        raise DeployError(
            "deploy.sha", f"{label} must be a full 40-hex commit SHA (git+URL@SHA installs only), got {value!r}"
        )
    return value


# Run by the live venv's python: one JSON list of the requirements it holds, apart from the package being deployed
# and pip itself: `name @ git+URL@COMMIT` for a git install, `name @ URL` for an archive or wheel fetched from a
# URL (a wheel URL names an exact version), `name==VERSION` for the rest. A file:// install and an editable tree
# cannot be reproduced elsewhere and fail the deploy.
CARRY = """\
import importlib.metadata as metadata
import json
import re
import sys

def normal(name):
    return re.sub(r"[-_.]+", "-", name).lower()

skip = {normal(name) for name in sys.argv[1:]} | {"pip"}
rows = []
for dist in metadata.distributions():
    name = dist.metadata["Name"]
    if normal(name) in skip:
        continue
    raw = dist.read_text("direct_url.json")
    if raw is None:
        rows.append(f"{name}=={dist.version}")
        continue
    origin = json.loads(raw)
    url = origin.get("url", "")
    vcs = origin.get("vcs_info", {})
    if origin.get("dir_info", {}).get("editable") or url.startswith("file:"):
        sys.exit(f"{name} is installed from {url}, an editable or file:// install that cannot be carried")
    if not vcs:
        rows.append(f"{name} @ {url}")
    elif vcs.get("vcs") == "git":
        rows.append(f"{name} @ git+{url}@{vcs['commit_id']}")
    else:
        sys.exit(f"{name} is installed from {url} with {vcs.get('vcs')}, not git")
print(json.dumps(sorted(rows)))
"""


# CakeUnbake64's commands (`unbake`, `unbake-run`, ...) belong to its own toolkit, published into the one command
# directory the job PATH holds. This venv keeps the unbake library (decomp imports it) and nothing runnable of it.
RETIRE = """\
import importlib.metadata as metadata
import json
import pathlib
import sys

names = sorted(e.name for e in metadata.distribution("unbake").entry_points if e.group == "console_scripts")
for name in names:
    (pathlib.Path(sys.prefix) / "bin" / name).unlink(missing_ok=True)
print(json.dumps(names))
"""


def carried(source: Path, package: str, project_packages: Sequence[str], run: Run) -> list[str]:
    """The requirements the `source` venv holds (the live toolkit), other than `package` and the project's own pinned
    packages (those are installed at the versions asked for)."""
    skip = [package, *(re.split(r"[\[=]", spec)[0] for spec in project_packages)]
    code, output = run([str(source / "bin" / "python"), "-c", CARRY, *skip], None)
    if code:
        raise DeployError("deploy.carry", f"cannot read the packages installed in {source}: {output[-300:]}")
    return json.loads(output.strip().splitlines()[-1])


def landed_carry(carry: Sequence[str], package: str, run: Run) -> tuple[list[str], list[str]]:
    """(tools, rest) of the `carry` rows: each tool of ours but `package` becomes `name @ git+URL@landed commit` (found
    11-02: venv-decomp kept unbake and cake-taste-64 at the commits a past deploy installed, 48 and more behind their
    main, because the carry reinstalled them as installed); every other row stays as installed."""
    ours = {re.sub(r"[-_.]+", "-", name).lower(): name for name in sources.PACKAGES if name != package}
    selected: set[str] = set()
    rest: list[str] = []
    for row in carry:
        tool = ours.get(re.sub(r"[-_.]+", "-", re.split(r"\s*@|==|\[", row, maxsplit=1)[0].strip()).lower())
        if tool:
            selected.add(tool)
        else:
            rest.append(row)
    # A new decomp checkout may declare tools the live toolkit has never held. Resolve the same declared sibling
    # set as its clean environment, in sources.PACKAGES dependency order (abucache before unbake).
    if package == tools.DISTRIBUTION:
        selected.update(ours.values())
    moved = sources.clean_requirements([name for name in sources.PACKAGES if name in selected], run)
    return moved, rest


def on_main(url: str, commit: str, run: Run = run_command) -> bool:
    """Whether `commit` is an ancestor of (or is) the commit the source repository `url` has landed at (its HEAD), read
    from a fetch of commits only (no trees or blobs) into a scratch bare repository. A commit the fetch does not hold
    (a handgate branch's, an unpushed one) is not on main; a fetch that fails refuses."""
    with tempfile.TemporaryDirectory(prefix="on-main.") as bare:
        fetch = ["git", "-C", bare, "fetch", "-q", "--filter=tree:0", url, "HEAD"]
        for argv in (["git", "init", "-q", "--bare", bare], fetch):
            code, output = run(argv, None)
            if code:
                raise DeployError("deploy.landed", f"{url}: {' '.join(argv[3:])} failed: {output.strip()[-300:]}")
        code, _ = run(["git", "-C", bare, "merge-base", "--is-ancestor", commit, "FETCH_HEAD"], None)
    return code == 0


def source_tree(package: str, commit: str, run: Run = run_command) -> Path:
    """The git tree this deploy runs from (the one holding this module: the activation job's checkout at the landed
    revision), which must hold `commit`: pip fetches the package's own commit from it, never from the mirror of
    `sources.PACKAGES[package]`, which Gitea fans out to some time after Land pushes."""
    here = str(Path(__file__).resolve().parent)
    code, output = run(["git", "-C", here, "rev-parse", "--show-toplevel"], None)
    if code or not output.strip():
        raise DeployError("deploy.tree", f"{here} is not inside a git tree, so {commit} cannot be fetched from it")
    tree = Path(output.strip())
    code, output = run(["git", "-C", str(tree), "cat-file", "-e", f"{commit}^{{commit}}"], None)
    if code:
        raise DeployError("deploy.tree", f"{tree} does not hold {package} commit {commit}: {output.strip()[-300:]}")
    return tree


def tree_fetch(package: str, tree: Path) -> dict[str, str]:
    """The environment of the one pip subprocess that installs `package`: git rewrites the package source URL to `tree`
    (`url.<tree>.insteadOf`), so the requirement and the provenance pip records stay `git+URL@SHA` while the fetch of
    that commit is served by `tree`. Nothing else is rewritten and nothing is written to any git config file."""
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.{tree}.insteadOf",
        "GIT_CONFIG_VALUE_0": sources.PACKAGES[package],
    }


def requirement(package: str, commit: str) -> str:
    """The pip requirement of `package` at `commit`, with its `dev` extra (the suite a deploy gates on runs from it; pip
    only warns when a package has no such extra)."""
    return f"{package}[dev] @ git+{sources.PACKAGES[package]}@{commit}"


def content_name(specs: Sequence[str], carry: Sequence[str]) -> str:
    """The name of a build: 12 hex digits identifying every requirement it holds, whatever package is deployed."""
    return hashlib.sha256("\n".join(sorted({*specs, *carry})).encode()).hexdigest()[:12]


def versioned_path(live: Path, name: str) -> Path:
    """`<live>.<name>`, or `<live>.<name>-N` when that name is already taken (a redeploy of the same content)."""
    base = live.with_name(f"{live.name}.{name}")
    candidate, number = base, 1
    while candidate.exists() or candidate.is_symlink():
        number += 1
        candidate = base.with_name(f"{base.name}-{number}")
    return candidate


def seal(venv: Path, run: Run) -> dict[str, Any]:
    """Make the whole `venv` tree read-only (`chmod -R a-w`): a deployed toolkit is never written in place. The next
    deploy builds a fresh tree, so nothing needs write access to this one."""
    code, output = run(["chmod", "-R", "a-w", str(venv)], None)
    return {"check": f"chmod -R a-w {venv}", "ok": code == 0, "tail": output[-300:] if code else ""}


def unseal(venv: Path) -> None:
    """Give the owner write access again on our own never-linked tree, so it can be removed."""
    for root, dirs, _files in os.walk(venv):
        for name in [".", *dirs]:
            if not (Path(root) / name).is_symlink():
                (Path(root) / name).chmod(0o700)
    shutil.rmtree(venv, ignore_errors=True)


def point(live: Path, target: Path) -> dict[str, Any]:
    """Make `live` a symlink to `target`, atomically: a fresh link is renamed over it (rename(2))."""
    fresh = live.with_name(f".{live.name}.{os.getpid()}.new")
    fresh.unlink(missing_ok=True)
    fresh.symlink_to(target)
    row: dict[str, Any] = {"live": str(live), "target": str(target)}
    try:
        row["previous"] = os.path.realpath(live) if live.exists() else ""
        os.rename(fresh, live)
    finally:
        fresh.unlink(missing_ok=True)
    row["readback"] = os.path.realpath(live) == os.path.realpath(target) and live.is_symlink()
    return row


def project_requirements(text: str, source: str) -> list[str]:
    """The exact pins of the project's `requirements.txt` (`text`, read from `source`); a line that is not
    `name==version` is refused, so a venv never holds a package the project did not pin."""
    rows = [row.split("#")[0].strip() for row in text.splitlines()]
    rows = [row for row in rows if row]
    for row in rows:
        if not _EXACT.match(row):
            raise DeployError("deploy.requirements", f"{source}: {row!r} is not an exact name==version pin")
    return rows


# Run by the new venv's python: import the module each declared distribution is named for (splat64 -> splat; its
# distribution may also ship stray top-level modules such as `dist`, which are not the package), or every top-level
# module of a distribution whose name matches none.
IMPORT_CHECK = """\
import importlib
import importlib.metadata as metadata
import re
import sys

def normal(name):
    return re.sub(r"[-_.]+", "-", name).lower()

owners = metadata.packages_distributions()
for spec in sys.argv[1:]:
    dist = normal(re.split(r"[\\[=]", spec)[0])
    modules = sorted(mod for mod, names in owners.items() if dist in {normal(n) for n in names})
    if not modules:
        sys.exit(f"{spec}: installs no importable module")
    named = [mod for mod in modules if normal(mod) in (dist, dist.rstrip("0123456789"))]
    for module in named or modules:
        importlib.import_module(module)
    print(spec, "imports", *(named or modules))
"""


def install(
    venv: Path,
    base: Path,
    specs: Sequence[str],
    carry: Sequence[str],
    run: Run,
    fetch: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Create `venv` with the python of `base` (the venv it replaces), then pip install with the new venv's python:
    the carried requirements exactly as they are (`--no-deps`: they are already a resolved set), then `specs` (the
    package at its commit, the tools of ours at their landed commits and the project's pinned packages) with their
    dependencies. `fetch` is the environment of that last pip run (`tree_fetch`)."""
    python = venv / "bin" / "python"
    steps = [
        ("create", [str(base / "bin" / "python"), "-m", "venv", str(venv)]),
        *([("carry", [str(python), "-m", "pip", "install", "--no-deps", *carry])] if carry else []),
        ("install", [str(python), "-m", "pip", "install", *specs], fetch),
        ("retire", [str(python), "-c", RETIRE]),  # the venv carries no unbake command: land and the rig find it by name
    ]
    done: list[dict[str, Any]] = []
    for name, argv, *env in steps:
        code, output = run(argv, None, *env) if env and env[0] else run(argv, None)
        done.append({"step": name, "ok": code == 0, "tail": output[-400:] if code else ""})
        if code:
            break
    return done


def smoke(venv: Path, project: Path | None, host: Path, run: Run, packages: Sequence[str] = ()) -> list[dict[str, Any]]:
    """`decomp dependencies` (every installed tool from a git install), an import of every declared project package,
    and `unbake symbols --version V` on the project (the `unbake` command found by name on PATH, CakeUnbake64's own),
    V being the first version directory of the project."""
    results = []
    code, output = run([str(venv / "bin" / "decomp"), "dependencies"], None)
    results.append({"check": "decomp dependencies", "ok": code == 0, "tail": output[-300:] if code else ""})
    if packages:
        code, output = run([str(venv / "bin" / "python"), "-c", IMPORT_CHECK, *packages], None)
        results.append(
            {"check": "import " + " ".join(packages), "ok": code == 0, "tail": output[-300:] if code else ""}
        )
    root = layout.path(project, "versions_root") if project else None
    versions = sorted(p.name for p in root.iterdir() if p.is_dir()) if root and root.is_dir() else []
    if not project or not versions:
        results.append(
            {"check": "unbake symbols", "ok": False, "tail": "the project has no versions/ directory to ask"}
        )
        return results
    argv = [
        "unbake",
        "--host",
        str(host),
        "--project",
        str(project),
        "symbols",
        "--version",
        versions[0],
    ]
    code, output = run(argv, project)
    results.append(
        {
            "check": f"unbake symbols --version {versions[0]}",
            "ok": code == 0 and bool(output.strip()),
            "tail": output[-300:] if code else "",
        }
    )
    return results


def checkout_steps(
    profile: Profile, package: str, commit: str
) -> tuple[dict[str, tuple[Path, str]], list[dict[str, Any]]]:
    """The checkout the seat runs `package` from (`tools.checkouts`, a tool only) with the commit it must sit at, and a
    blocking finding for it when it has local changes (aligning would discard or carry them; never done)."""
    wanted = {
        command: (path, commit)
        for command, path in tools.checkouts_of(profile).items()
        if tools._normal(tools.TOOLS[command].distribution) == tools._normal(package)
    }
    findings = []
    for command, (path, _) in wanted.items():
        if not path.is_dir():
            findings.append(
                {"key": "deploy.checkout", "blocking": True, "message": f"{command} checkout {path} is not a directory"}
            )
        elif dirty := tools.checkout_dirty(path):
            findings.append(
                {
                    "key": "deploy.checkout_dirty",
                    "blocking": True,
                    "message": f"{command} checkout {path} has local changes and is left as it is: "
                    f"{'; '.join(dirty[:5])}",
                }
            )
    return wanted, findings


def align_checkouts(wanted: dict[str, tuple[Path, str]], run: Run) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch each checkout, then a detached checkout of its commit. (rows done, findings)"""
    done: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for command, (path, sha) in wanted.items():
        code, output = run(["git", "-C", str(path), "fetch", "-q", "origin"], None)
        if code == 0:
            code, output = run(["git", "-C", str(path), "checkout", "-q", "--detach", sha], None)
        done.append({"checkout": command, "path": str(path), "commit": sha, "ok": code == 0})
        if code:
            findings.append(
                {
                    "key": "deploy.checkout",
                    "blocking": True,
                    "message": f"{command} checkout {path} cannot reach {sha}: {output[-300:]}",
                }
            )
    return done, findings


def worktrees(project: Path, run: Run) -> list[Path]:
    """The project's work trees (its own checkout among them) that carry a Makefile."""
    code, output = run(["git", "worktree", "list", "--porcelain"], project)
    trees = (
        [Path(line.split(" ", 1)[1]) for line in output.splitlines() if line.startswith("worktree ")]
        if not code
        else []
    )
    return [tree for tree in trees if (tree / "Makefile").is_file()]


def holders(tree: Path) -> list[int]:
    """The live processes whose working directory is inside `tree`."""
    root, mine, found = os.path.realpath(tree), os.getpid(), []
    for entry in os.scandir("/proc"):
        if entry.name.isdigit() and int(entry.name) != mine:
            try:
                cwd = os.readlink(f"{entry.path}/cwd")
            except OSError:
                continue
            if cwd == root or cwd.startswith(root + os.sep):
                found.append(int(entry.name))
    return found


def resync(venv: Path, host: Path, project: Path, run: Run) -> list[dict[str, Any]]:
    """`unbake sync --makefile` (Makefile and local.mk) of the new venv in every work tree of the project."""
    rows = []
    for tree in worktrees(project, run):
        if pids := holders(tree):
            rows.append({"tree": str(tree), "outcome": "skipped", "held_by": pids[:5]})
            continue
        argv = ["unbake", "--host", str(host), "--project", str(tree), "sync", "--makefile"]
        code, output = run(argv, tree)
        rows.append(
            {"tree": str(tree), "outcome": "refused" if code else "synced", "tail": output[-300:] if code else ""}
        )
    return rows


def _land_table(profile: Profile) -> dict[str, Any]:
    """The profile's `land` section (where the project's main lives), empty when the seat has none."""
    try:
        return profile.section("land")
    except ConfigError:
        return {}


def name(
    profile: Profile,
    package: str,
    sha: str,
    source: Path,
    *,
    project_sha: str | None = None,
    run: Run = run_command,
    requirements_of: Requirements = requirements_at,
) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    """`decomp tools name`: the build name a deploy of `package` at `sha` over the packages `source` holds would give;
    builds nothing."""
    if package not in sources.PACKAGES:
        raise DeployError("deploy.package", f"--package must be one of {sorted(sources.PACKAGES)}, got {package!r}")
    check_sha("--sha", sha)
    if project_sha:
        check_sha("--project-sha", project_sha)
    if not (source / "bin" / "python").is_file():
        raise DeployError("deploy.venv", f"--carry-from {source} is not a venv (no bin/python)")
    land = _land_table(profile)
    revision, text = requirements_of(
        tools.project_dir(profile), str(land.get("remote", "origin")), str(land.get("branch", "main")), project_sha
    )
    packages = project_requirements(text, f"requirements.txt at {revision[:12]}")
    moved, carry = landed_carry(carried(source, package, packages, run), package, run)
    specs = [requirement(package, sha), *moved, *packages]
    return {"name": content_name(specs, carry), "requirements": specs, "carried": carry}, 0, []


def deploy(
    profile: Profile,
    package: str,
    sha: str,
    venvs: Sequence[Path],
    *,
    project_sha: str | None = None,
    carry_from: Path | None = None,
    expect_name: str | None = None,
    run: Run = run_command,
    requirements_of: Requirements = requirements_at,
) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    """`decomp tools deploy`; `expect_name` is the build name the caller read from `decomp tools name`: a deploy whose
    content gives another name refuses before it builds anything."""
    if package not in sources.PACKAGES:
        raise DeployError("deploy.package", f"--package must be one of {sorted(sources.PACKAGES)}, got {package!r}")
    check_sha("--sha", sha)
    if not venvs:
        raise DeployError("deploy.venv", "pass at least one --venv")
    for venv in venvs:
        if not (venv / "bin" / "python").is_file():
            raise DeployError("deploy.venv", f"{venv} is not a venv (no bin/python)")
        if not venv.is_symlink():
            raise DeployError("deploy.venv", f"{venv} is a real directory, not a symlink")
    project = tools.project_dir(profile)
    if carry_from and not (carry_from / "bin" / "python").is_file():
        raise DeployError("deploy.venv", f"--carry-from {carry_from} is not a venv (no bin/python)")
    if project_sha:
        check_sha("--project-sha", project_sha)
    land = _land_table(profile)
    remote, branch = str(land.get("remote", "origin")), str(land.get("branch", "main"))
    revision, text = requirements_of(project, remote, branch, project_sha)
    packages = project_requirements(text, f"{project} requirements.txt at {revision[:12]}")
    specs = [requirement(package, sha), *packages]
    # only this repository's own package is served by the tree; another tool's commit lives in its own repository
    fetch = tree_fetch(package, source_tree(package, sha, run)) if package == tools.DISTRIBUTION else None
    result: dict[str, Any] = {
        "package": package,
        "sha": sha,
        "requirements": specs,
        "project_revision": revision,
        "packages": packages,
        "venvs": [],
    }
    findings: list[dict[str, Any]] = []
    checkouts, refused = checkout_steps(profile, package, sha)
    if refused:
        return result, 1, refused
    result["checkouts"], findings = align_checkouts(checkouts, run)
    if findings:
        return result, 1, findings
    for live in venvs:
        source = carry_from or live
        moved, carry = landed_carry(carried(source, package, packages, run), package, run)
        wanted = [requirement(package, sha), *moved, *packages]
        build = content_name(wanted, carry)
        if expect_name and build != expect_name:
            raise DeployError("deploy.name", f"{live} would build {build}, not the expected {expect_name}")
        venv = versioned_path(live, build)
        steps = install(venv, source, wanted, carry, run, fetch)
        row: dict[str, Any] = {
            "venv": str(live),
            "name": build,
            "versioned": str(venv),
            "moved": moved,
            "carried": carry,
            "steps": steps,
        }
        result["venvs"].append(row)
        if all(step["ok"] for step in steps):
            row["smoke"] = smoke(venv, project, tools.host_file(profile), run, packages)
            bad = [check for check in row["smoke"] if not check["ok"]]
            findings += [
                {"key": "deploy.smoke", "blocking": True, "message": f"{venv}: {c['check']} failed: {c['tail']}"}
                for c in bad
            ]
        else:
            failed = next(step for step in steps if not step["ok"])
            bad = [failed]
            findings.append(
                {
                    "key": "deploy.install",
                    "blocking": True,
                    "message": f"{venv}: {failed['step']} failed: {failed['tail']}",
                }
            )
        if bad:
            shutil.rmtree(venv, ignore_errors=True)  # our own half-built venv, never linked, so nothing uses it
            continue
        row["seal"] = seal(venv, run)  # after the gate (smoke), before the flip
        if not row["seal"]["ok"]:
            findings.append(
                {
                    "key": "deploy.seal",
                    "blocking": True,
                    "message": f"{venv}: {row['seal']['check']} failed: {row['seal']['tail']}",
                }
            )
            unseal(venv)  # built but never linked, so nothing uses it
            continue
        try:
            row["swap"] = point(live, venv)
        except DeployError as error:
            findings.append({"key": error.key, "blocking": True, "message": str(error)})
            unseal(venv)  # built but never linked, so nothing uses it
            continue
        row["swap"]["readback_smoke"] = smoke(live, project, tools.host_file(profile), run, packages)
        if not row["swap"]["readback"] or not all(c["ok"] for c in row["swap"]["readback_smoke"]):
            findings.append(
                {"key": "deploy.readback", "blocking": True, "message": f"{live} does not read back to {venv}"}
            )
            continue
        row["resync"] = resync(live, tools.host_file(profile), project, run)
    return result, int(bool(findings)), findings
