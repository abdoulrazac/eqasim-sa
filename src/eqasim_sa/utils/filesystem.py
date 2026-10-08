"""Crash-conscious filesystem operations for durable campaign files.

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
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pyarrow as pa
import pyarrow.parquet as pq

__all__ = (
    "PublicationOutcomeUnknown",
    "FileIdentity",
    "FileState",
    "StableFile",
    "absolute",
    "capture_stable_file",
    "copy_file_atomic",
    "descriptor_sha256",
    "directory_is_current",
    "file_identity",
    "file_lease",
    "file_state",
    "fsync_directory",
    "open_child_directory",
    "open_directory",
    "open_regular",
    "path_state",
    "publish_bytes",
    "reject_symlink_traversal",
    "secure_directory",
    "stable_file",
    "stable_file_bytes",
    "validate_directory",
    "validate_regular_file",
    "verify_stable_file",
    "write_all",
    "write_atomic",
    "write_bytes_atomic",
    "write_bytes_exclusive",
    "write_exclusive",
    "write_parquet_atomic",
    "write_parquet_exclusive",
)


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class PublicationOutcomeUnknown(RuntimeError):
    """A durable replacement could neither be committed nor rolled back."""


@dataclass(frozen=True)
class FileIdentity:
    """Stable identity of one filesystem object."""

    device: int
    inode: int
    kind: int


@dataclass(frozen=True)
class FileState:
    """Mutation-sensitive state of one filesystem object."""

    identity: FileIdentity
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class StableFile:
    """Identity and content captured during one stable read of a regular file."""

    path: Path
    state: FileState
    size: int
    sha256: str


def file_state(info: os.stat_result) -> FileState:
    """Reduce stat metadata to the fields used for stability checks."""

    return FileState(
        file_identity(info),
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def path_state(path: Path, *, follow_symlinks: bool = True) -> FileState:
    """Capture the stable metadata state visible at a path."""

    return file_state(path.stat(follow_symlinks=follow_symlinks))


def file_identity(info: os.stat_result) -> FileIdentity:
    """Return device, inode, and file type identity from stat metadata."""

    return FileIdentity(info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


def absolute(path: Path) -> Path:
    """Return a lexical absolute path without resolving symbolic links."""

    return Path(os.path.abspath(os.fspath(path)))


def reject_symlink_traversal(path: Path, name: str) -> None:
    """Reject a lexical path whose existing prefix contains a symbolic link."""
    resolved = absolute(path)
    current = Path(resolved.anchor)
    for part in resolved.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"{name} must not traverse a symlink")


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


def secure_directory(path: Path, *, create: bool = False) -> Path:
    """Validate an owned directory without traversing symbolic links."""

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
    """Require every component of a directory path to be non-symlinked."""

    descriptor = open_directory(path)
    os.close(descriptor)


def open_regular(path: Path) -> tuple[int, int, str]:
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


def validate_regular_file(path: Path) -> None:
    """Require a path to identify a stable regular non-symlink file."""

    descriptor, parent, _ = open_regular(path)
    os.close(descriptor)
    os.close(parent)


def _read_stable_file(path: Path, *, retain_content: bool) -> tuple[bytes | None, StableFile]:
    descriptor, parent, name = open_regular(path)
    try:
        initial = os.fstat(descriptor)
        digest = hashlib.sha256()
        content = bytearray() if retain_content else None
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            if content is not None:
                content.extend(chunk)
        final = os.fstat(descriptor)
        visible = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if file_state(final) != file_state(initial) or file_state(
            visible
        ) != file_state(initial):
            raise RuntimeError(f"source changed while being hashed: {path}")
        snapshot = StableFile(
            path, file_state(initial), initial.st_size, digest.hexdigest()
        )
        return bytes(content) if content is not None else None, snapshot
    finally:
        os.close(descriptor)
        os.close(parent)


def stable_file(path: Path) -> StableFile:
    """Hash one stable regular file without following symbolic links."""
    _, snapshot = _read_stable_file(path, retain_content=False)
    return snapshot


def stable_file_bytes(path: Path) -> tuple[bytes, StableFile]:
    """Read and hash one stable regular file in a single verified pass."""
    content, snapshot = _read_stable_file(path, retain_content=True)
    assert content is not None
    return content, snapshot


def descriptor_sha256(
    descriptor: int,
    interrupted: Callable[[], bool] | None = None,
) -> str:
    """Hash a descriptor from its start, with optional interruption."""
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        if interrupted is not None and interrupted():
            raise KeyboardInterrupt
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


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
    """Fail when a regular file differs from a captured stable snapshot."""

    current = stable_file(snapshot.path)
    if current != snapshot:
        raise RuntimeError(f"selected analysis source changed: {snapshot.path}")


def write_all(descriptor: int, content: bytes) -> None:
    """Write an entire byte payload to a descriptor, handling short writes."""

    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short write while staging durable content")
        remaining = remaining[written:]


def fsync_directory(path: Path) -> None:
    """Synchronize directory metadata through a safely opened descriptor."""

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
    guard: Callable[[], bool] | None = None,
) -> Path:
    """Publish a new durable file without replacing an existing name.

    Both directories stay pinned for the whole transaction.  ``guard`` lets a
    caller bind publication to a wider directory graph without reimplementing
    link, fsync, rollback, or outcome-unknown handling.
    """
    requested_destination = destination
    destination = absolute(destination)
    staging = absolute(staging_directory or destination.parent)
    destination_parent = open_directory(destination.parent, create=True)
    staging_parent = -1
    descriptor = -1
    temporary_name = f".{destination.name}.{uuid.uuid4().hex}.tmp"
    temporary = staging / temporary_name
    linked = False
    temporary_exists = False
    preserve_temporary = False
    staged_identity: FileIdentity | None = None

    def guarded() -> bool:
        return guard is None or guard()

    try:
        staging_parent = open_directory(staging, create=True)
        if os.fstat(destination_parent).st_dev != os.fstat(staging_parent).st_dev:
            raise ValueError(
                "exclusive publication staging must share the destination filesystem"
            )
        descriptor = os.open(
            temporary_name,
            os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
            dir_fd=staging_parent,
        )
        temporary_exists = True
        writer(temporary)
        staged = os.fstat(descriptor)
        visible = os.stat(
            temporary_name, dir_fd=staging_parent, follow_symlinks=False
        )
        if (
            not stat.S_ISREG(staged.st_mode)
            or staged.st_uid != os.getuid()
            or staged.st_nlink != 1
            or file_state(staged) != file_state(visible)
        ):
            raise ValueError(
                "exclusive publication writer must leave one private owned regular file"
            )
        os.fsync(descriptor)
        staged = os.fstat(descriptor)
        visible = os.stat(
            temporary_name, dir_fd=staging_parent, follow_symlinks=False
        )
        if file_state(staged) != file_state(visible):
            raise RuntimeError("exclusive staged file changed while being fsynced")
        staged_identity = file_identity(staged)
        if (
            not guarded()
            or not directory_is_current(destination.parent, destination_parent)
            or not directory_is_current(staging, staging_parent)
        ):
            raise RuntimeError("exclusive publication directories changed")
        os.link(
            temporary_name,
            destination.name,
            src_dir_fd=staging_parent,
            dst_dir_fd=destination_parent,
            follow_symlinks=False,
        )
        linked = True
        published = os.stat(
            destination.name,
            dir_fd=destination_parent,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(published.st_mode)
            or file_identity(published) != staged_identity
        ):
            raise RuntimeError("exclusive destination differs from staged content")
        os.fsync(destination_parent)
        if (
            not guarded()
            or not directory_is_current(destination.parent, destination_parent)
            or not directory_is_current(staging, staging_parent)
        ):
            raise RuntimeError("exclusive publication directories changed")
        os.unlink(temporary_name, dir_fd=staging_parent)
        temporary_exists = False
        os.fsync(descriptor)
        os.fsync(staging_parent)
        published = os.stat(
            destination.name,
            dir_fd=destination_parent,
            follow_symlinks=False,
        )
        if (
            published.st_nlink != 1
            or file_identity(published) != staged_identity
            or not guarded()
            or not directory_is_current(destination.parent, destination_parent)
            or not directory_is_current(staging, staging_parent)
        ):
            raise RuntimeError("exclusive publication did not reach a stable commit")
        linked = False
        return requested_destination
    except BaseException as commit_error:
        if linked:
            try:
                if staged_identity is None:
                    raise RuntimeError("exclusive staged identity was not captured")
                current = os.stat(
                    destination.name,
                    dir_fd=destination_parent,
                    follow_symlinks=False,
                )
                if file_identity(current) != staged_identity:
                    raise RuntimeError(
                        "exclusive destination changed before rollback"
                    )
                os.unlink(destination.name, dir_fd=destination_parent)
                os.fsync(destination_parent)
                try:
                    os.stat(
                        destination.name,
                        dir_fd=destination_parent,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise RuntimeError(
                        "exclusive destination survived rollback"
                    )
                if staging_parent >= 0:
                    os.fsync(staging_parent)
                linked = False
            except BaseException as rollback_error:
                preserve_temporary = temporary_exists
                raise PublicationOutcomeUnknown(
                    "exclusive commit failed and rollback could not be established"
                ) from rollback_error
        raise commit_error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if staging_parent >= 0:
            if temporary_exists and not preserve_temporary:
                try:
                    os.unlink(temporary_name, dir_fd=staging_parent)
                except OSError:
                    pass
            os.close(staging_parent)
        os.close(destination_parent)


def write_atomic(
    destination: Path,
    writer: Callable[[Path], None],
    *,
    staging_directory: Path | None = None,
) -> Path:
    """Replace a durable file and prove either commit or exact rollback."""
    requested_destination = destination
    destination = absolute(destination)
    staging = absolute(
        staging_directory
        or (
            destination.parent.parent / ".eqasim-sa" / "transactions"
            if destination.parent.name == "runs"
            else destination.parent
        )
    )
    destination_parent = open_directory(destination.parent, create=True)
    staging_parent = -1
    staged_descriptor = -1
    previous_descriptor = -1
    temporary_name = f".{destination.name}.{uuid.uuid4().hex}.tmp"
    backup_name = f".{destination.name}.{uuid.uuid4().hex}.backup"
    temporary = staging / temporary_name
    backup_created = False
    preserve_backup = False
    replaced = False
    committed = False
    previous_identity: FileIdentity | None = None
    replacement_identity: FileIdentity | None = None
    try:
        staging_parent = open_directory(staging, create=True)
        descriptor = os.open(
            temporary_name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
            dir_fd=staging_parent,
        )
        os.close(descriptor)
        writer(temporary)
        visible = os.stat(
            temporary_name, dir_fd=staging_parent, follow_symlinks=False
        )
        if not stat.S_ISREG(visible.st_mode) or visible.st_nlink != 1:
            raise ValueError("durable writer must leave one private regular file")
        staged_descriptor = os.open(
            temporary_name, _READ_FLAGS, dir_fd=staging_parent
        )
        staged = os.fstat(staged_descriptor)
        visible = os.stat(
            temporary_name, dir_fd=staging_parent, follow_symlinks=False
        )
        if file_state(staged) != file_state(visible):
            raise RuntimeError("staged durable file changed before fsync")
        os.fsync(staged_descriptor)
        staged = os.fstat(staged_descriptor)
        visible = os.stat(
            temporary_name, dir_fd=staging_parent, follow_symlinks=False
        )
        if file_state(staged) != file_state(visible):
            raise RuntimeError("staged durable file changed while being fsynced")
        replacement_identity = file_identity(staged)

        try:
            predecessor = os.stat(
                destination.name,
                dir_fd=destination_parent,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            predecessor = None
        if predecessor is not None:
            if not stat.S_ISREG(predecessor.st_mode):
                raise ValueError(
                    f"destination must be a regular non-symlink file: {destination}"
                )
            previous_descriptor = os.open(
                destination.name, _READ_FLAGS, dir_fd=destination_parent
            )
            opened_previous = os.fstat(previous_descriptor)
            visible_previous = os.stat(
                destination.name,
                dir_fd=destination_parent,
                follow_symlinks=False,
            )
            if file_state(opened_previous) != file_state(visible_previous):
                raise RuntimeError("durable predecessor changed while being pinned")
            os.link(
                destination.name,
                backup_name,
                src_dir_fd=destination_parent,
                dst_dir_fd=staging_parent,
                follow_symlinks=False,
            )
            backup_created = True
            pinned = os.fstat(previous_descriptor)
            visible_previous = os.stat(
                destination.name,
                dir_fd=destination_parent,
                follow_symlinks=False,
            )
            held_backup = os.stat(
                backup_name, dir_fd=staging_parent, follow_symlinks=False
            )
            previous_identity = file_identity(pinned)
            if (
                file_identity(visible_previous) != previous_identity
                or file_identity(held_backup) != previous_identity
                or visible_previous.st_size != pinned.st_size
                or visible_previous.st_mtime_ns != pinned.st_mtime_ns
            ):
                raise RuntimeError("durable predecessor changed before replacement")

        if not directory_is_current(destination.parent, destination_parent) or not (
            directory_is_current(staging, staging_parent)
        ):
            raise RuntimeError("durable publication directories changed")
        try:
            current_predecessor = os.stat(
                destination.name,
                dir_fd=destination_parent,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            current_predecessor = None
        if previous_identity is None:
            if current_predecessor is not None:
                raise RuntimeError("durable destination appeared before replacement")
        elif (
            current_predecessor is None
            or file_identity(current_predecessor) != previous_identity
            or previous_descriptor < 0
            or file_identity(os.fstat(previous_descriptor)) != previous_identity
        ):
            raise RuntimeError("durable predecessor changed before replacement")
        os.replace(
            temporary_name,
            destination.name,
            src_dir_fd=staging_parent,
            dst_dir_fd=destination_parent,
        )
        replaced = True
        published = os.stat(
            destination.name,
            dir_fd=destination_parent,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(published.st_mode)
            or file_identity(published) != replacement_identity
        ):
            raise RuntimeError("durable replacement changed during publication")
        if not directory_is_current(destination.parent, destination_parent) or not (
            directory_is_current(staging, staging_parent)
        ):
            raise RuntimeError("durable publication directories changed after replace")
        os.fsync(staging_parent)
        os.fsync(destination_parent)
        if not directory_is_current(destination.parent, destination_parent) or not (
            directory_is_current(staging, staging_parent)
        ):
            raise RuntimeError("durable publication directories changed during fsync")
        committed = True
        return requested_destination
    except BaseException as commit_error:
        if replaced:
            try:
                if replacement_identity is None:
                    raise RuntimeError("durable replacement identity was not captured")
                if not directory_is_current(
                    destination.parent, destination_parent
                ) or not directory_is_current(staging, staging_parent):
                    raise RuntimeError("durable publication directories changed")
                current = os.stat(
                    destination.name,
                    dir_fd=destination_parent,
                    follow_symlinks=False,
                )
                if file_identity(current) != replacement_identity:
                    raise RuntimeError(
                        "durable destination changed before rollback"
                    )
                if previous_identity is None:
                    os.unlink(destination.name, dir_fd=destination_parent)
                    try:
                        os.stat(
                            destination.name,
                            dir_fd=destination_parent,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        pass
                    else:
                        raise RuntimeError(
                            "new durable destination survived rollback"
                        )
                else:
                    os.replace(
                        backup_name,
                        destination.name,
                        src_dir_fd=staging_parent,
                        dst_dir_fd=destination_parent,
                    )
                    backup_created = False
                    restored = os.stat(
                        destination.name,
                        dir_fd=destination_parent,
                        follow_symlinks=False,
                    )
                    if (
                        file_identity(restored) != previous_identity
                        or previous_descriptor < 0
                        or file_identity(os.fstat(previous_descriptor))
                        != previous_identity
                    ):
                        raise RuntimeError(
                            "previous durable destination was not restored"
                        )
                os.fsync(destination_parent)
                os.fsync(staging_parent)
                if not directory_is_current(
                    destination.parent, destination_parent
                ) or not directory_is_current(staging, staging_parent):
                    raise RuntimeError(
                        "durable publication directories changed during rollback"
                    )
            except BaseException as rollback_error:
                preserve_backup = backup_created
                raise PublicationOutcomeUnknown(
                    "durable commit failed and rollback could not be established"
                ) from rollback_error
        raise commit_error
    finally:
        for descriptor in (staged_descriptor, previous_descriptor):
            if descriptor >= 0:
                os.close(descriptor)
        if staging_parent >= 0:
            try:
                os.unlink(temporary_name, dir_fd=staging_parent)
            except OSError:
                pass
            if backup_created and not preserve_backup:
                try:
                    os.unlink(backup_name, dir_fd=staging_parent)
                    if committed:
                        try:
                            os.fsync(staging_parent)
                        except OSError:
                            pass
                except OSError:
                    if not committed:
                        pass
            os.close(staging_parent)
        os.close(destination_parent)


def write_parquet_atomic(
    table: pa.Table,
    destination: Path,
    *,
    staging_directory: Path | None = None,
) -> Path:
    """Atomically replace a destination with a compressed Parquet table."""

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
    guard: Callable[[], bool] | None = None,
) -> Path:
    """Publish a Parquet table without replacing an existing destination."""

    return write_exclusive(
        destination,
        lambda path: pq.write_table(table, path, compression="zstd"),
        staging_directory=staging_directory,
        guard=guard,
    )


def write_bytes_atomic(
    content: bytes,
    destination: Path,
    *,
    staging_directory: Path | None = None,
) -> Path:
    """Atomically replace a destination with a byte payload."""

    return write_atomic(
        destination,
        lambda path: path.write_bytes(content),
        staging_directory=staging_directory,
    )


def write_bytes_exclusive(
    content: bytes,
    destination: Path,
    *,
    staging_directory: Path | None = None,
    guard: Callable[[], bool] | None = None,
) -> Path:
    """Publish bytes without replacing an existing destination."""

    return write_exclusive(
        destination,
        lambda path: path.write_bytes(content),
        staging_directory=staging_directory,
        guard=guard,
    )


def publish_bytes(
    content: bytes,
    destination: Path,
    *,
    replace: bool,
    default_mode: int = 0o600,
) -> Path:
    """Durably publish bytes, preserving an existing file's mode on replacement."""
    if type(default_mode) is not int or not 0 <= default_mode <= 0o777:
        raise ValueError("default_mode must be a Unix permission mode")
    requested_destination = destination
    destination = absolute(destination)
    parent = open_directory(destination.parent)
    temporary_name = f".{destination.name}.{uuid.uuid4().hex}.tmp"
    temporary_descriptor = -1
    try:
        mode = default_mode
        try:
            existing = os.stat(
                destination.name, dir_fd=parent, follow_symlinks=False
            )
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISREG(existing.st_mode):
                raise ValueError(
                    "existing destination must be a regular non-symlink file"
                )
            if not replace:
                raise FileExistsError(f"destination already exists: {destination}")
            mode = stat.S_IMODE(existing.st_mode)
        temporary_descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=parent,
        )
        os.fchmod(temporary_descriptor, mode)
        write_all(temporary_descriptor, content)
        os.fsync(temporary_descriptor)
        if replace:
            os.replace(
                temporary_name,
                destination.name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
            )
        else:
            os.link(
                temporary_name,
                destination.name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
                follow_symlinks=False,
            )
            os.unlink(temporary_name, dir_fd=parent)
        os.fsync(parent)
        return requested_destination
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def copy_file_atomic(
    source: Path,
    destination: Path,
    *,
    staging_directory: Path | None = None,
) -> Path:
    """Atomically copy a file through the guarded staging machinery."""

    def copy(temporary: Path) -> None:
        with source.open("rb") as input_stream, temporary.open("wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream)

    return write_atomic(destination, copy, staging_directory=staging_directory)
