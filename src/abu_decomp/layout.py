"""The project's folder names, read verbatim from its [layout] through unbake's reader.

Every abu_decomp module takes a folder name from here: none spells a folder literal or folds a folder's case,
so a project whose [layout] says Source/Engine/Unicol gets exactly that spelling in every path the tools write.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from unbake.config import layout_at


def read(root: Path) -> Mapping[str, Any]:
    """The project's [layout] (its config.toml over unbake's repo.toml), keys spelled as unbake names them."""
    return layout_at(Path(root))


def source_roots(root: Path) -> tuple[str, ...]:
    """The source roots, relative to the project root, as [layout] spells them."""
    return tuple(read(root)["source_roots"])


def header_roots(root: Path) -> tuple[str, ...]:
    """The header roots, relative to the project root, as [layout] spells them."""
    return tuple(read(root)["header_roots"])


def folder(root: Path, key: str) -> str:
    """One folder root by its [layout] key (asm_root, build_root, roms_root, versions_root, assets_root, ...)."""
    value = read(root)[key]
    if not isinstance(value, str):
        raise TypeError(f"[layout] {key} is not one folder: {value!r}")
    return value


def path(root: Path, key: str, *parts: str) -> Path:
    """root / the folder [layout] names under key / parts, no segment re-cased."""
    return Path(root) / folder(root, key) / Path(*parts) if parts else Path(root) / folder(root, key)


def _under(roots: tuple[str, ...], relative: str) -> bool:
    parts = Path(relative).parts
    return any(parts[: len(Path(r).parts)] == Path(r).parts for r in roots)


def under_source(root: Path, relative: str) -> bool:
    """Whether a project-relative path sits under one of the source roots, compared byte for byte."""
    return _under(source_roots(root), relative)


def under_header(root: Path, relative: str) -> bool:
    """Whether a project-relative path sits under one of the header roots, compared byte for byte."""
    return _under(header_roots(root), relative)


def lookup_key(path: str) -> str:
    """The case-insensitive key of a tracked path, for finding a file the way the 1999 tree's case-insensitive
    filesystem resolved `#include` names; never used to spell a path the tools write."""
    return path.lower()


def guard(include: str) -> str:
    """The include guard macro of a header's include path (`game/Foo` -> `GAME_FOO_H`): a C identifier, not a path."""
    return re.sub(r"\W", "_", include).upper() + "_H"


def relative(roots: tuple[str, ...], value: str) -> str:
    """Remove the longest matching configured root, preserving all remaining bytes."""
    for root in sorted(roots, key=len, reverse=True):
        if value.startswith(root + "/"):
            return value[len(root) + 1 :]
    return value


def header_relative(root: Path, value: str) -> str:
    return relative(header_roots(root), value)


def source_relative(root: Path, value: str) -> str:
    return relative(source_roots(root), value)


def header_path(root: Path, value: str) -> Path:
    candidates = [Path(root) / h / value for h in header_roots(root)]
    return next((p for p in candidates if p.is_file()), candidates[0])


def code_globs(root: Path) -> tuple[str, ...]:
    return tuple(f"{h}/**/*{s}" for h in header_roots(root) for s in (".h", ".H")) + tuple(
        f"{r}/**/*{s}" for r in source_roots(root) for s in (".c", ".C", ".cpp", ".CPP")
    )
