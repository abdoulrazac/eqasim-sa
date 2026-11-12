"""Operating-system differences isolated behind one small interface.

Callers must read ``eqasim_sa.utils.platform.IS_WINDOWS`` as a module attribute
at call time so tests can monkeypatch it; the functions below do the same.
"""

from __future__ import annotations

import os
import platform as _stdlib_platform
import sys
import time
from pathlib import Path

__all__ = (
    "IS_WINDOWS",
    "LOCK_OFFSET",
    "RETRY_DELAYS",
    "current_uid",
    "fsync_directory",
    "hostname",
    "is_link",
    "lock_exclusive_nonblocking",
    "rename_with_retry",
    "replace_with_retry",
    "unlock",
)

IS_WINDOWS: bool = sys.platform == "win32"

if IS_WINDOWS:
    import msvcrt
else:
    import fcntl

# Windows byte-range locks are mandatory; lock a range far beyond any content
# so the file stays readable by other processes.
LOCK_OFFSET = 2**31 - 2
RETRY_DELAYS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0)


def lock_exclusive_nonblocking(descriptor: int) -> bool:
    """Take an exclusive advisory lock without waiting; True when acquired."""
    if IS_WINDOWS:
        position = os.lseek(descriptor, 0, os.SEEK_CUR)
        try:
            os.lseek(descriptor, LOCK_OFFSET, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except PermissionError:
                return False
            return True
        finally:
            os.lseek(descriptor, position, os.SEEK_SET)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def unlock(descriptor: int) -> None:
    """Release a lock taken by :func:`lock_exclusive_nonblocking`."""
    if IS_WINDOWS:
        position = os.lseek(descriptor, 0, os.SEEK_CUR)
        try:
            os.lseek(descriptor, LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        finally:
            os.lseek(descriptor, position, os.SEEK_SET)
        return
    fcntl.flock(descriptor, fcntl.LOCK_UN)


def fsync_directory(path: Path) -> None:
    """Flush directory entries to disk (POSIX); no-op on Windows."""
    if IS_WINDOWS:
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def is_link(path: Path) -> bool:
    """True for symbolic links and, on Windows, directory junctions."""
    return path.is_symlink() or path.is_junction()


def hostname() -> str:
    return _stdlib_platform.node()


def replace_with_retry(source: Path, destination: Path) -> None:
    _with_retry(os.replace, source, destination, passthrough=())


def rename_with_retry(source: Path, destination: Path) -> None:
    _with_retry(os.rename, source, destination, passthrough=(FileExistsError,))


def _with_retry(operation, source, destination, *, passthrough) -> None:
    if not IS_WINDOWS:
        operation(source, destination)
        return
    delays = iter(RETRY_DELAYS)
    while True:
        try:
            operation(source, destination)
            return
        except passthrough:
            raise
        except PermissionError:
            delay = next(delays, None)
            if delay is None:
                raise
            time.sleep(delay)


def current_uid() -> int | None:
    return None if IS_WINDOWS else os.getuid()
