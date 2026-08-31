"""Crash-conscious publication primitives for durable campaign files.

The public interface deliberately hides descriptor handling, stable-file capture,
temporary names, backup names, fsync ordering, and rollback bookkeeping.  Callers
choose only the destination and how to serialize its content.
"""

from __future__ import annotations

import hashlib
import fcntl
import os
import shutil
import stat
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pyarrow as pa
import pyarrow.parquet as pq


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class PublicationOutcomeUnknown(RuntimeError):
    """A durable replacement could neither be committed nor rolled back."""


@dataclass(frozen=True)
class StableFile:
    """Identity and content captured during one stable read of a regular file."""

    path: Path
    state: tuple[int, int, int, int, int, int]
    size: int
    sha256: str


def file_state(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def file_identity(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


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
    try:
        visible = open_directory(path)
    except OSError:
        return False
    try:
        return file_identity(os.fstat(visible)) == file_identity(os.fstat(descriptor))
    finally:
        os.close(visible)


def secure_directory(path: Path, *, create: bool = False) -> Path:
    try:
        descriptor = open_directory(path, create=create)
    except OSError as error:
        raise ValueError(
            f"directory must be a regular non-symlink directory: {path}"
        ) from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError(f"directory must be owned by the current user: {path}")
        return path
    finally:
        os.close(descriptor)


def validate_directory(path: Path) -> None:
    descriptor = open_directory(path)
    os.close(descriptor)


def open_regular(path: Path) -> tuple[int, int, str]:
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


def validate_regular_file(path: Path) -> None:
    descriptor, parent, _ = open_regular(path)
    os.close(descriptor)
    os.close(parent)


def stable_file(path: Path) -> StableFile:
    descriptor, parent, name = open_regular(path)
    try:
        initial = os.fstat(descriptor)
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        final = os.fstat(descriptor)
        visible = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if file_state(final) != file_state(initial) or file_state(visible) != file_state(initial):
            raise RuntimeError(f"source changed while being hashed: {path}")
        return StableFile(path, file_state(initial), initial.st_size, digest.hexdigest())
    finally:
        os.close(descriptor)
        os.close(parent)


def capture_stable_file(
    source: Path,
    destination: Path,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> StableFile:
    """Copy one stable regular file to a new exclusive destination."""
    source_descriptor, source_parent, source_name = open_regular(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination_descriptor = -1
    try:
        initial = os.fstat(source_descriptor)
        destination_descriptor = os.open(
            destination,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o400,
        )
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            write_all(destination_descriptor, chunk)
        os.fchmod(destination_descriptor, 0o400)
        os.fsync(destination_descriptor)
        final = os.fstat(source_descriptor)
        visible = os.stat(source_name, dir_fd=source_parent, follow_symlinks=False)
        if file_state(final) != file_state(initial) or file_state(visible) != file_state(initial):
            raise RuntimeError(f"source changed while being captured: {source}")
        actual_sha256 = digest.hexdigest()
        if expected_size is not None and size != expected_size:
            raise ValueError(f"source size does not match its marker: {source}")
        if expected_sha256 is not None and actual_sha256 != expected_sha256:
            raise ValueError(f"source hash does not match its marker: {source}")
        return StableFile(source, file_state(initial), size, actual_sha256)
    except BaseException:
        if destination_descriptor >= 0:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        os.close(source_descriptor)
        os.close(source_parent)


def verify_stable_file(snapshot: StableFile) -> None:
    current = stable_file(snapshot.path)
    if current != snapshot:
        raise RuntimeError(f"selected analysis source changed: {snapshot.path}")


def write_all(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short write while staging durable content")
        remaining = remaining[written:]


def fsync_directory(path: Path) -> None:
    descriptor = open_directory(path)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def file_lease(
    path: Path,
    *,
    blocking: bool = True,
    interrupted: Callable[[], bool] | None = None,
) -> Iterator[None]:
    """Hold one private module lease without traversing links."""
    parent = open_directory(path.parent, create=True)
    descriptor = -1
    try:
        descriptor = os.open(
            path.name,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
        ):
            raise ValueError("lease must be a private owned regular file")
        os.fchmod(descriptor, 0o600)
        while True:
            if interrupted is not None and interrupted():
                raise KeyboardInterrupt
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not blocking:
                    raise
                time.sleep(0.05)
        visible = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if (
            file_identity(visible) != file_identity(opened)
            or not directory_is_current(path.parent, parent)
        ):
            raise RuntimeError("lease changed while being acquired")
        yield
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def write_exclusive(
    destination: Path,
    writer: Callable[[Path], None],
    *,
    staging_directory: Path | None = None,
) -> Path:
    """Publish a new durable file without ever replacing an existing name."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = staging_directory or destination.parent
    staging.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=staging
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    preserve_temporary = False
    try:
        writer(temporary)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        staged = temporary.lstat()
        if not stat.S_ISREG(staged.st_mode) or temporary.is_symlink():
            raise ValueError("exclusive publication writer produced an invalid file")
        os.link(temporary, destination, follow_symlinks=False)
        try:
            published = destination.lstat()
            if file_identity(published) != file_identity(staged):
                raise RuntimeError("exclusive destination differs from staged content")
            temporary.unlink()
            fsync_directory(staging)
            fsync_directory(destination.parent)
        except BaseException as commit_error:
            try:
                visible = destination.lstat()
                if file_identity(visible) != file_identity(staged):
                    raise RuntimeError("exclusive destination changed before rollback")
                destination.unlink()
                fsync_directory(destination.parent)
                if temporary.exists():
                    temporary.unlink()
                fsync_directory(staging)
            except BaseException as rollback_error:
                preserve_temporary = temporary.exists()
                raise PublicationOutcomeUnknown(
                    "exclusive commit failed and rollback could not be established"
                ) from rollback_error
            raise commit_error
        return destination
    finally:
        if temporary.exists() and not preserve_temporary:
            temporary.unlink()


def write_atomic(
    destination: Path,
    writer: Callable[[Path], None],
    *,
    staging_directory: Path | None = None,
) -> Path:
    """Replace a durable file and restore its predecessor on fsync failure."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = staging_directory or (
        destination.parent.parent / ".eqasim-sa" / "transactions"
        if destination.parent.name == "runs"
        else destination.parent
    )
    staging.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=staging
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    backup = staging / f".{destination.name}.{uuid.uuid4().hex}.backup"
    preserve_backup = False
    try:
        writer(temporary)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        if destination.exists() or destination.is_symlink():
            existing = destination.lstat()
            if not stat.S_ISREG(existing.st_mode) or destination.is_symlink():
                raise ValueError(f"destination must be a regular file: {destination}")
            os.link(destination, backup, follow_symlinks=False)
        os.replace(temporary, destination)
        try:
            fsync_directory(destination.parent)
        except OSError:
            if backup.exists():
                try:
                    os.replace(backup, destination)
                    fsync_directory(destination.parent)
                except OSError:
                    preserve_backup = True
                    raise
            else:
                destination.unlink(missing_ok=True)
                fsync_directory(destination.parent)
            raise
        backup.unlink(missing_ok=True)
        return destination
    except BaseException:
        temporary.unlink(missing_ok=True)
        if not preserve_backup:
            backup.unlink(missing_ok=True)
        raise


def write_parquet_atomic(
    table: pa.Table,
    destination: Path,
    *,
    staging_directory: Path | None = None,
) -> Path:
    return write_atomic(
        destination,
        lambda path: pq.write_table(table, path, compression="zstd"),
        staging_directory=staging_directory,
    )


def write_parquet_exclusive(
    table: pa.Table,
    destination: Path,
    *,
    staging_directory: Path | None = None,
) -> Path:
    return write_exclusive(
        destination,
        lambda path: pq.write_table(table, path, compression="zstd"),
        staging_directory=staging_directory,
    )


def write_bytes_atomic(
    content: bytes,
    destination: Path,
    *,
    staging_directory: Path | None = None,
) -> Path:
    return write_atomic(
        destination,
        lambda path: path.write_bytes(content),
        staging_directory=staging_directory,
    )


def copy_file_atomic(
    source: Path,
    destination: Path,
    *,
    staging_directory: Path | None = None,
) -> Path:
    def copy(temporary: Path) -> None:
        with source.open("rb") as input_stream, temporary.open("wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream)

    return write_atomic(destination, copy, staging_directory=staging_directory)


def replace_bytes_restorable(
    content: bytes,
    destination: Path,
    *,
    staging_directory: Path,
) -> Path:
    """Durably replace bytes and restore the exact predecessor on failure."""
    staging_directory.mkdir(parents=True, exist_ok=True)
    temporary = staging_directory / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    backup = staging_directory / f".{destination.name}.{uuid.uuid4().hex}.backup"
    descriptor = os.open(
        temporary,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
        0o600,
    )
    try:
        write_all(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    previous: os.stat_result | None = None
    replacement: os.stat_result | None = None
    backup_created = False
    committed = False
    preserve_backup = False
    try:
        if destination.exists() or destination.is_symlink():
            previous = destination.lstat()
            if not stat.S_ISREG(previous.st_mode) or destination.is_symlink():
                raise ValueError(
                    f"destination must be a regular non-symlink file: {destination}"
                )
            os.link(destination, backup, follow_symlinks=False)
            backup_created = True
            held = backup.lstat()
            if (held.st_dev, held.st_ino) != (previous.st_dev, previous.st_ino):
                raise RuntimeError("durable publication backup changed while creating it")
        os.replace(temporary, destination)
        replacement = destination.lstat()
        if not stat.S_ISREG(replacement.st_mode) or destination.is_symlink():
            raise RuntimeError("durable replacement is not a regular file")
        try:
            fsync_directory(staging_directory)
            fsync_directory(destination.parent)
        except BaseException as commit_error:
            try:
                current = destination.lstat()
                if (current.st_dev, current.st_ino) != (
                    replacement.st_dev,
                    replacement.st_ino,
                ):
                    raise RuntimeError("durable destination changed before rollback")
                if previous is None:
                    destination.unlink()
                    if destination.exists() or destination.is_symlink():
                        raise RuntimeError("new durable destination survived rollback")
                else:
                    os.replace(backup, destination)
                    backup_created = False
                    restored = destination.lstat()
                    if (restored.st_dev, restored.st_ino) != (
                        previous.st_dev,
                        previous.st_ino,
                    ):
                        raise RuntimeError("previous durable destination was not restored")
                fsync_directory(destination.parent)
                fsync_directory(staging_directory)
            except BaseException as rollback_error:
                preserve_backup = backup_created
                raise PublicationOutcomeUnknown(
                    "durable commit failed and rollback could not be established"
                ) from rollback_error
            raise commit_error
        committed = True
        return destination
    finally:
        temporary.unlink(missing_ok=True)
        if backup_created and not preserve_backup:
            try:
                backup.unlink()
                if committed:
                    fsync_directory(staging_directory)
            except OSError:
                if not committed:
                    raise
