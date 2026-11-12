"""Capture and guard the installed package's immutable source identity."""

import hashlib
import importlib.metadata
import json
import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path

from eqasim_sa.utils.filesystem import (
    FileState,
    StableFile,
    file_state,
    stable_file_bytes,
)
from eqasim_sa.utils import platform
from eqasim_sa.utils.text_identity import lf_bytes


SOFTWARE_IDENTITY_DOMAIN = b"eqasim-sa:software-identity:v2\0"
SOURCE_SUFFIXES = frozenset({".py"})
TEMPLATE_SUFFIXES = frozenset({".md", ".yaml", ".yml"})


@dataclass(frozen=True)
class SoftwareSourceState:
    """Stable path, metadata, size, and digest for one behavior source."""

    relative_path: str
    path: Path
    state: FileState
    size: int
    sha256: str


@dataclass(frozen=True)
class SoftwareIdentity:
    """Versioned digest and complete source inventory of the installed package."""

    version: str
    sha256: str
    package_root: Path
    source_states: tuple[SoftwareSourceState, ...]


def _included(relative: Path) -> bool:
    return relative.suffix in SOURCE_SUFFIXES or (
        relative.parts[:1] == ("templates",)
        and relative.suffix in TEMPLATE_SUFFIXES
    )


def _inventory(root: Path) -> tuple[tuple[str, Path], ...]:
    try:
        root_info = root.lstat()
    except OSError as error:
        raise ValueError("eqasim_sa package root is unavailable") from error
    if not stat.S_ISDIR(root_info.st_mode) or platform.is_link(root):
        raise ValueError("eqasim_sa package root must be a non-symlink directory")

    result: list[tuple[str, Path]] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: os.fsencode(item.name))
        except OSError as error:
            raise ValueError(f"cannot inventory eqasim_sa package directory: {directory}") from error
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root)
            if "__pycache__" in relative.parts:
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise ValueError(f"cannot inspect eqasim_sa package entry: {relative}") from error
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"eqasim_sa package entry must not be a symlink: {relative}")
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
                continue
            if _included(relative):
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError(
                        f"eqasim_sa behavior source must be a regular file: {relative}"
                    )
                result.append((f"eqasim_sa/{relative.as_posix()}", path))
    result.sort(key=lambda item: item[0].encode("utf-8"))
    if not result:
        raise ValueError("eqasim_sa package has no behavior sources")
    return tuple(result)


def _stable_source_bytes(path: Path) -> tuple[bytes, StableFile]:
    try:
        return stable_file_bytes(path)
    except (OSError, ValueError) as error:
        raise ValueError(f"software source is not a stable regular file: {path}") from error
    except RuntimeError as error:
        raise RuntimeError(f"software source changed while hashing: {path}") from error


def stable_published_identity(
    path: Path, *, text: bool
) -> tuple[StableFile, int, str]:
    """Read a stable file once; return its raw snapshot and published digest.

    The raw snapshot guards stability on this machine. The published size and
    digest are computed on LF-normalized content for text files, so CRLF
    checkouts publish the same identity; binary files are hashed raw.
    """

    content, snapshot = _stable_source_bytes(path)
    published = lf_bytes(content) if text else content
    return snapshot, len(published), hashlib.sha256(published).hexdigest()


def read_software_identity(
    package_root: Path | None = None, version: str | None = None
) -> SoftwareIdentity:
    """Inventory package behavior sources and derive their canonical digest."""

    root = (
        Path(__file__).resolve(strict=True).parents[1]
        if package_root is None
        else package_root
    )
    if platform.is_link(root):
        raise ValueError("eqasim_sa package root must not be a symlink")
    root = root.resolve(strict=True)
    resolved_version = importlib.metadata.version("eqasim-sa") if version is None else version
    if not isinstance(resolved_version, str) or not resolved_version or "\0" in resolved_version:
        raise ValueError("eqasim_sa version must be a non-empty string without NUL")

    sources = []
    descriptor = []
    for relative_path, path in _inventory(root):
        # Stability is guarded on raw bytes; the published identity hashes the
        # LF-normalized content so CRLF checkouts yield the same identity.
        snapshot, published_size, published_sha256 = stable_published_identity(
            path, text=True
        )
        sources.append(
            SoftwareSourceState(
                relative_path, path, snapshot.state, snapshot.size, snapshot.sha256
            )
        )
        descriptor.append(
            {"path": relative_path, "sha256": published_sha256, "size": published_size}
        )
    encoded = json.dumps(
        {"files": descriptor},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    identity = SoftwareIdentity(
        resolved_version,
        hashlib.sha256(SOFTWARE_IDENTITY_DOMAIN + encoded).hexdigest(),
        root,
        tuple(sources),
    )
    verify_software_source_states(identity)
    return identity


def verify_software_source_states(identity: SoftwareIdentity) -> None:
    """Fail if the package inventory or any source metadata changed."""

    if not isinstance(identity, SoftwareIdentity):
        raise TypeError("software identity is required")
    current_inventory = _inventory(identity.package_root)
    expected_paths = tuple(item.relative_path for item in identity.source_states)
    if tuple(relative for relative, _ in current_inventory) != expected_paths:
        raise RuntimeError("eqasim_sa source inventory changed after sealing")
    for source, (_, path) in zip(identity.source_states, current_inventory, strict=True):
        try:
            visible = path.lstat()
        except OSError as error:
            raise RuntimeError(
                f"eqasim_sa source changed after sealing: {source.relative_path}"
            ) from error
        if not stat.S_ISREG(visible.st_mode) or file_state(visible) != source.state:
            raise RuntimeError(
                f"eqasim_sa source changed after sealing: {source.relative_path}"
            )


class SoftwareSourceGuard:
    """Thread-safe fail-closed guard for a previously sealed package identity."""

    def __init__(self, identity: SoftwareIdentity) -> None:
        self.identity = identity
        self._failure: str | None = None
        self._lock = threading.Lock()

    def guard(self) -> None:
        with self._lock:
            if self._failure is not None:
                raise RuntimeError(self._failure)
            try:
                verify_software_source_states(self.identity)
            except Exception as error:
                self._failure = f"software provenance guard failed: {error}"
                raise RuntimeError(self._failure) from error
