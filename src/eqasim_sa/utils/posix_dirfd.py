"""POSIX-only directory-descriptor helpers.

These helpers pin directories through ``O_DIRECTORY | O_NOFOLLOW`` descriptors
and ``dir_fd`` relative calls.  They are unavailable on Windows and are used
only by the Linux-only SLURM backend (and transitionally by the ledger).  The
portable, path-based layer lives in :mod:`eqasim_sa.utils.filesystem`.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from eqasim_sa.utils.filesystem import absolute, file_identity, file_state

__all__ = (
    "directory_is_current",
    "open_child_directory",
    "open_directory",
    "open_regular_at",
)

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def open_directory(path: Path, *, create: bool = False) -> int:
    """Open every directory component without traversing symbolic links."""
    resolved = absolute(path)
    descriptor = os.open(resolved.anchor, _DIRECTORY_FLAGS)
    try:
        for part in resolved.parts[1:]:
            if create:
                try:
                    os.mkdir(part, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_child_directory(parent: int, name: str, *, create: bool = False) -> int:
    """Open one safe child directory relative to an existing descriptor."""

    if not name or "/" in name or name in {".", ".."}:
        raise ValueError(f"invalid directory name: {name!r}")
    if create:
        try:
            os.mkdir(name, dir_fd=parent)
        except FileExistsError:
            pass
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError(f"{name} must be a directory")
    return descriptor


def directory_is_current(path: Path, descriptor: int) -> bool:
    """Check whether a path still resolves to an open directory descriptor."""

    try:
        visible = open_directory(path)
    except OSError:
        return False
    try:
        return file_identity(os.fstat(visible)) == file_identity(os.fstat(descriptor))
    finally:
        os.close(visible)


def open_regular_at(path: Path) -> tuple[int, int, str]:
    """Open a stable regular file and its parent without following symlinks."""

    resolved = absolute(path)
    if not resolved.name:
        raise ValueError("regular file path must have a name")
    parent = open_directory(resolved.parent)
    descriptor = -1
    try:
        visible = os.stat(resolved.name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(visible.st_mode):
            raise ValueError(f"not a stable regular non-symlink file: {path}")
        descriptor = os.open(resolved.name, _READ_FLAGS, dir_fd=parent)
        opened = os.fstat(descriptor)
        visible = os.stat(resolved.name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(visible.st_mode)
            or file_state(opened) != file_state(visible)
        ):
            raise ValueError(f"not a stable regular non-symlink file: {path}")
        return descriptor, parent, resolved.name
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)
        raise
