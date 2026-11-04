"""Folder names come only from the project's [layout], verbatim: no module spells a folder literal or folds its case."""

import ast
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from abu_decomp import cli, gates, layout
from abu_decomp.data import check
from abu_decomp.data.config import DataConfig
from abu_decomp.land import top_dirs

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "abu_decomp"
# The folder names unbake's packaged repo.toml [layout] defaults to: spelling one in code bypasses the project's own.
FOLDERS = frozenset({"src", "include", "sdk", "asm", "versions", "roms", "build", "assets"})
FOLDS = frozenset({"upper", "lower", "casefold"})


PATH_CALLS = frozenset({"Path", "PurePath", "PurePosixPath", "joinpath", "join", "startswith", "glob", "rglob"})


def _literal(node: ast.Constant, parents: dict[int, ast.AST]) -> bool:
    """A folder spelled in code: `src/...`, or a bare folder name used as a path part (`root / "src"`,
    `Path("src")`, `p.startswith("src")`, `parts[0] == "src"`). A bare name as a JSON key or scope label is not."""
    value = node.value
    if any(value.startswith(name + "/") for name in FOLDERS):
        return True
    if value not in FOLDERS:
        return False
    parent = parents.get(id(node))
    if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Div):
        return True
    if isinstance(parent, ast.Compare):
        return any(isinstance(n, ast.Name) and n.id in PATH_WORDS for n in ast.walk(parent))
    if isinstance(parent, (ast.Tuple, ast.List, ast.Set)):
        owner = parents.get(id(parent))
        if isinstance(owner, (ast.Assign, ast.AnnAssign)):
            targets = owner.targets if isinstance(owner, ast.Assign) else [owner.target]
            return any(isinstance(t, ast.Name) and any(w in t.id.lower() for w in ("root", "dir")) for t in targets)
        if isinstance(owner, ast.Dict):
            return any(
                v is parent and isinstance(k, ast.Constant) and k.value in {"dirs", "source_dirs", "roots"}
                for k, v in zip(owner.keys, owner.values, strict=True)
            )
    if isinstance(parent, ast.Call):
        func = parent.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        return name in PATH_CALLS
    return False


# What a path is named or built from: a fold of one of these folds a folder name or a path's directory part.
PATH_WORDS = frozenset(
    {
        "path",
        "paths",
        "p",
        "rel",
        "relative",
        "file",
        "files",
        "dir",
        "dirs",
        "dirname",
        "folder",
        "root",
        "roots",
        "parts",
        "parent",
        "parents",
        "include",
        "includer",
        "include_path",
        "cand",
        "target",
        "h",
        "header",
        "as_posix",
        "relative_to",
        "Path",
        "PurePath",
        "PurePosixPath",
        "basename",
        "join",
        "normpath",
    }
)


def _suffix_fold(node: ast.Call, parents: dict[int, ast.AST]) -> bool:
    """A fold of a file suffix for classification (`p.suffix.lower()`, `name.lower().endswith((".c", ".h"))`,
    `s.lower() for s in cfg["suffixes"]`)."""
    receiver = node.func.value  # type: ignore[attr-defined]
    if isinstance(receiver, ast.Attribute) and receiver.attr in {"suffix", "suffixes"}:
        return True
    parent = parents.get(id(node))
    if isinstance(parent, (ast.comprehension, ast.GeneratorExp, ast.ListComp, ast.SetComp)):
        loops = parent.generators if not isinstance(parent, ast.comprehension) else [parent]
        return any("suffixes" in ast.unparse(loop.iter) for loop in loops)
    return isinstance(parent, ast.Attribute) and parent.attr == "endswith"  # a lowered name tested for its suffix


def _path_fold(node: ast.Call) -> bool:
    """A fold whose receiver is a path or a folder name, by the names it is built from; a fold of a symbol,
    a version, a package or a message names no folder."""
    receiver = node.func.value  # type: ignore[attr-defined]
    words = {n.id for n in ast.walk(receiver) if isinstance(n, ast.Name)}
    words |= {n.attr for n in ast.walk(receiver) if isinstance(n, ast.Attribute)}
    return bool(words & PATH_WORDS)


def census(package: Path = PACKAGE) -> list[str]:
    """Every folder literal and every fold outside the layout module, as `path:line what`."""
    found = []
    for file in sorted(package.rglob("*.py")):
        if file == package / "layout.py":
            continue
        tree = ast.parse(file.read_text())
        docs = {
            id(n.body[0].value)
            for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and n.body
            and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)
        }
        parents = {id(c): n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        where = file.relative_to(package)
        for node in {id(n): n for n in ast.walk(tree)}.values():
            text = isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs
            if text and _literal(node, parents):  # type: ignore[arg-type]
                found.append(f"{where}:{node.lineno} literal {node.value!r}")  # type: ignore[attr-defined]
            fold = isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in FOLDS
            if fold and _path_fold(node) and not _suffix_fold(node, parents):  # type: ignore[arg-type]
                found.append(f"{where}:{node.lineno} fold {ast.unparse(node)[:60]}")
    return sorted(found, key=lambda row: (row.split(":")[0], int(row.split(":")[1].split()[0])))


class CensusTest(unittest.TestCase):
    def test_no_folder_literal_or_fold_outside_layout(self):
        found = census()
        self.assertEqual(found, [], f"{len(found)} folder literals or folds outside abu_decomp/layout.py")

    def test_census_sees_a_literal_and_a_fold_but_not_a_suffix_fold(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            (package / "m.py").write_text(
                '"""src/ in a docstring is prose."""\n'
                'A = "src/"\n'
                'E = {"build": 1}["build"]\n'
                'F = root / "asm"\n'
                "B = parts[1].casefold()\n"
                "G = symbol.upper()\n"
                'H = tuple(s.lower() for s in cfg["suffixes"])\n'
                "C = p.suffix.lower()\n"
                'D = name.lower().endswith((".c", ".cpp"))\n'
            )
            self.assertEqual(
                census(package), ["m.py:2 literal 'src/'", "m.py:4 literal 'asm'", "m.py:5 fold parts[1].casefold()"]
            )

    def test_census_sees_folder_lists_but_not_command_kinds(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            (package / "m.py").write_text(
                'ROOTS = ("src", "include")\ncfg = {"dirs": ["sdk"]}\nkind = cfg["kind"] == "roms"\n'
            )
            self.assertEqual(len(census(package)), 3)


class MixedCaseTest(unittest.TestCase):
    """A project whose [layout] spells the 1999 mixed-case roots gets those spellings back byte for byte."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "config.toml").write_text(
            "[layout]\n"
            'source_roots = ["Source"]\n'
            'header_roots = ["Include"]\n'
            'build_root = "Build"\n'
            'subsystem_source = "Source/Engine/{subsystem}"\n'
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_source_header_and_build_paths(self):
        self.assertEqual(layout.source_roots(self.root), ("Source",))
        self.assertEqual(layout.header_roots(self.root), ("Include",))
        self.assertEqual(layout.path(self.root, "build_root", "us", "rw.z64"), self.root / "Build" / "us" / "rw.z64")
        self.assertEqual(layout.read(self.root)["subsystem_source"].format(subsystem="Unicol"), "Source/Engine/Unicol")

    def test_source_membership_is_byte_exact(self):
        self.assertTrue(layout.under_source(self.root, "Source/Engine/Unicol/collide.cpp"))
        self.assertFalse(layout.under_source(self.root, "source/Engine/Unicol/collide.cpp"))
        self.assertFalse(layout.under_source(self.root, "src/Engine/Unicol/collide.cpp"))

    def test_land_top_dirs_keep_the_spelling(self):
        files = ["Source/Engine/Unicol/collide.cpp", "Source/engine/x.c", "Include/Engine/Unicol.h"]
        self.assertEqual(top_dirs(files, layout.source_roots(self.root)), ["Engine", "engine"])

    def test_gate_units_come_from_the_source_roots(self):
        paths = ["Source/Engine/Unicol/collide.cpp", "src/old.c", "Include/Engine/Unicol.h"]
        rev = type("Rev", (), {"root": self.root, "rev": "HEAD", "paths": paths})()
        units, modules = gates._units(rev, {"map": str(PurePosixPath("Unicol.map"))})
        self.assertEqual(units, ["Source/Engine/Unicol/collide.cpp"])
        self.assertEqual(modules, {"Source/Engine/Unicol/collide.cpp": []})

    def test_cleanup_command_reads_mixed_case_source_and_header_paths(self):
        for file in ("Source/Engine/Unicol/collide.cpp", "Include/Engine/Unicol.h"):
            target = self.root / file
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("int D_80000000;\n")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        profile = self.root / "profile.json"
        profile.write_text(json.dumps({"schema_version": 1, "name": "layout-test", "tools": {}}))
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["--profile", str(profile), "cleanup", "--project", str(self.root), "count"])
        self.assertEqual(code, 0, output.getvalue())
        document = json.loads(output.getvalue())
        self.assertIn("Engine/Unicol", str(document))
        self.assertIn("Include/Engine", str(document))

    def test_dependency_command_preserves_header_source_and_build_spellings(self):
        target = self.root / "Source/Engine/Unicol/collide.cpp"
        target.parent.mkdir(parents=True)
        target.write_text("int collide;\n")
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return SimpleNamespace(stdout="Build/us/Source/Engine/Unicol/collide.cpp.d\n")

        self.assertEqual(
            check.header_dependents(self.root, "us", ["Include/Engine/Unicol.h"], run),
            ["Source/Engine/Unicol/collide.cpp"],
        )
        argv, kwargs = calls[0]
        self.assertIn("Include/Engine/Unicol.h", argv)
        self.assertEqual(argv[-1], "Build/us")
        self.assertEqual(kwargs["cwd"], self.root)

    def test_data_defaults_use_this_project_layout(self):
        cfg = DataConfig(tree=self.root)
        self.assertEqual(cfg.source_dirs, ("Source", "Include"))
        self.assertEqual(cfg.object, "Build/{v}/{unit}.o")
        self.assertIn("-IInclude", cfg.cpp)
        self.assertNotIn("-Iinclude", cfg.cpp)

    def test_pool_marker_uses_configured_build_root(self):
        from unbake.hdrnarrow import EXTRACTED_MARKER

        self.assertIsNotNone(check.marker_refusal(self.root))
        marker = self.root / "Build" / EXTRACTED_MARKER
        marker.parent.mkdir()
        marker.write_text("commit\n")
        self.assertIsNone(check.marker_refusal(self.root))

    def test_nested_and_multiple_roots_preserve_case(self):
        with (self.root / "config.toml").open("a") as stream:
            stream.write('versions_root = "Facts"\n')
        self.assertEqual(layout.path(self.root, "versions_root", "us"), self.root / "Facts/us")
        self.assertEqual(
            layout.source_relative(self.root, "Source/Engine/Unicol/collide.cpp"), "Engine/Unicol/collide.cpp"
        )


if __name__ == "__main__":
    unittest.main()
