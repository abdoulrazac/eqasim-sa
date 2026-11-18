"""Content fingerprint of the eqasim source tree, independent of Git."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterable
from pathlib import Path

from eqasim_sa.utils import platform
from eqasim_sa.utils.text_identity import lf_sha256

MODEL_SOURCE_DOMAIN = b"eqasim-sa:model-source:v1\0"

_CODE_SUFFIXES = frozenset({".py", ".java"})
_BUILD_FILES = frozenset({"pom.xml", "pyproject.toml", "uv.lock"})
_ENVIRONMENT_MARKERS = ("pyvenv.cfg", "conda-meta")


def _check(deadline: float | None) -> None:
    if deadline is not None and deadline - time.monotonic() <= 0:
        raise TimeoutError("model source fingerprint time budget exhausted")


def _is_code(name: str) -> bool:
    return name in _BUILD_FILES or Path(name).suffix in _CODE_SUFFIXES


def _skip_directory(
    directory: Path, excluded: frozenset[Path], deadline: float | None
) -> bool:
    _check(deadline)
    name = directory.name
    if name.startswith(".") or name == "__pycache__":
        return True
    if platform.is_link(directory):
        return True
    if any((directory / marker).exists() for marker in _ENVIRONMENT_MARKERS):
        return True
    return directory.resolve() in excluded


def model_source_sha256(
    root: Path,
    excluded: Iterable[Path] = (),
    deadline: float | None = None,
) -> str:
    """Digest the code files under ``root`` (sorted POSIX paths, LF-normalized)."""

    root = Path(root)
    if not root.is_dir():
        raise ValueError(f"model root is not a directory: {root}")
    skipped = frozenset(Path(path).resolve() for path in excluded)
    entries: list[tuple[str, str]] = []
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name
            for name in directories
            if not _skip_directory(current_path / name, skipped, deadline)
        ]
        for name in files:
            _check(deadline)
            path = current_path / name
            if not _is_code(name) or platform.is_link(path):
                continue
            entries.append((path.relative_to(root).as_posix(), lf_sha256(path)))
    if not entries:
        raise ValueError(f"no eqasim source files found under {root}")
    entries.sort()
    payload = json.dumps(entries, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(MODEL_SOURCE_DOMAIN + payload.encode("utf-8")).hexdigest()
