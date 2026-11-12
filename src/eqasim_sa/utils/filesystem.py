"""Crash-conscious filesystem operations for durable campaign files.

The public interface deliberately hides stable-file capture, temporary names,
backup names, fsync ordering, and rollback bookkeeping.  Callers choose only
the destination and how to serialize its content.

Every operation is path-based so that it behaves identically on Linux, macOS
and Windows: no directory is ever opened, and every file handle is closed
before the file is linked, renamed, replaced or removed.  Directory identity
(``st_dev``/``st_ino``) is captured by :class:`DirectoryAnchor` and compared
before and after each publication, so a concurrent directory replacement is
detected and refused rather than made impossible.
"""

from __future__ import annotations

import errno
import hashlib
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

from eqasim_sa.utils import platform

__all__ = (
    "DirectoryAnchor",
    "PublicationOutcomeUnknown",
    "FileIdentity",
    "FileState",
    "StableFile",
    "absolute",
    "anchor_directory",
    "capture_stable_file",
    "copy_file_atomic",
    "descriptor_sha256",
    "file_identity",
    "file_lease",
    "file_state",
    "fsync_directory",
    "open_regular",
    "path_state",
    "publish_bytes",
    "reject_links_below",
    "resolve_root",
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


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_BINARY = getattr(os, "O_BINARY", 0)
_READ_FLAGS = os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _BINARY
_CREATE_FLAGS = os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BINARY

fsync_directory = platform.fsync_directory


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


@dataclass(frozen=True)
class DirectoryAnchor:
    """A directory path together with the identity it had when anchored."""

    path: Path
    identity: FileIdentity

    def is_current(self) -> bool:
        """True while the path still names the same non-link directory."""

        try:
            info = os.lstat(self.path)
        except OSError:
            return False
        return (
            stat.S_ISDIR(info.st_mode)
            and not platform.is_link(self.path)
            and file_identity(info) == self.identity
        )

    def child(self, name: str, *, create: bool = False) -> DirectoryAnchor:
        """Anchor one direct, non-link child directory."""

        if (
            not name
            or name in {".", ".."}
            or "/" in name
            or "\\" in name
            or Path(name).name != name
            or Path(name).anchor
        ):
            raise ValueError(f"invalid directory name: {name!r}")
        child = self.path / name
        if create:
            try:
                os.mkdir(child)
            except FileExistsError:
                pass
        return _anchor(child)


def _anchor(path: Path) -> DirectoryAnchor:
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or platform.is_link(path):
        raise NotADirectoryError(
            errno.ENOTDIR, "path must be a non-symlink directory", str(path)
        )
    return DirectoryAnchor(path, file_identity(info))


def anchor_directory(path: Path, *, create: bool = False) -> DirectoryAnchor:
    """Capture the identity of a non-link directory, creating it if asked."""

    resolved = absolute(path)
    if create:
        resolved.mkdir(parents=True, exist_ok=True)
    return _anchor(resolved)


def resolve_root(path: Path, name: str) -> Path:
    """Resolve a user-supplied root once; links among its ancestors are fine."""

    try:
        return Path(path).resolve(strict=True)
    except FileNotFoundError as error:
        raise FileNotFoundError(
            errno.ENOENT, f"{name} does not exist", str(path)
        ) from error


def reject_links_below(root: Path, path: Path, name: str) -> None:
    """Reject a path whose components strictly below ``root`` include a link."""

    lexical_root = absolute(root)
    lexical_path = absolute(path)
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError as error:
        raise ValueError(f"{name} must be below {lexical_root}") from error
    current = lexical_root
    for part in relative.parts:
        current = current / part
        if platform.is_link(current):
            raise ValueError(f"{name} must not traverse a symlink")


def secure_directory(path: Path, *, create: bool = False) -> Path:
    """Validate an owned non-link directory, creating it if asked."""

    try:
        anchor = anchor_directory(path, create=create)
        info = os.lstat(anchor.path)
    except OSError as error:
        raise ValueError(
            f"directory must be a regular non-symlink directory: {path}"
        ) from error
    uid = platform.current_uid()
    if uid is not None and info.st_uid != uid:
        raise ValueError(f"directory must be owned by the current user: {path}")
    return path


def validate_directory(path: Path) -> None:
    """Require a path to name an existing non-link directory."""

    anchor_directory(path)


def open_regular(path: Path) -> int:
    """Open a stable regular non-link file for reading; the caller closes it."""

    resolved = absolute(path)
    if not resolved.name:
        raise ValueError("regular file path must have a name")
    visible = os.lstat(resolved)
    if not stat.S_ISREG(visible.st_mode):
        raise ValueError(f"not a stable regular non-symlink file: {path}")
    descriptor = os.open(resolved, _READ_FLAGS)
    try:
        opened = os.fstat(descriptor)
        current = os.lstat(resolved)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or file_identity(opened) != file_identity(visible)
            or file_state(opened) != file_state(current)
        ):
            raise ValueError(f"not a stable regular non-symlink file: {path}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def validate_regular_file(path: Path) -> None:
    """Require a path to identify a stable regular non-symlink file."""

    os.close(open_regular(path))


def _read_stable_file(path: Path, *, retain_content: bool) -> tuple[bytes | None, StableFile]:
    descriptor = open_regular(path)
    try:
        initial = os.fstat(descriptor)
        digest = hashlib.sha256()
        content = bytearray() if retain_content else None
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            if content is not None:
                content.extend(chunk)
        final = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    visible = os.lstat(absolute(path))
    if file_state(final) != file_state(initial) or file_state(
        visible
    ) != file_state(initial):
        raise RuntimeError(f"source changed while being hashed: {path}")
    snapshot = StableFile(path, file_state(initial), initial.st_size, digest.hexdigest())
    return bytes(content) if content is not None else None, snapshot


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
    source_descriptor = open_regular(source)
    destination_descriptor = -1
    created = False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        initial = os.fstat(source_descriptor)
        destination_descriptor = os.open(
            destination, _CREATE_FLAGS | os.O_WRONLY, 0o400
        )
        created = True
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
        visible = os.lstat(absolute(source))
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
            os.close(destination_descriptor)
            destination_descriptor = -1
        if created:
            try:
                platform.remove_file(destination)
            except OSError:
                pass
        raise
    finally:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        os.close(source_descriptor)


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


def _sync_flags() -> int:
    # Windows can only flush handles opened for writing.
    access = os.O_RDWR if platform.IS_WINDOWS else os.O_RDONLY
    return access | _NOFOLLOW | _NONBLOCK | _BINARY


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, _sync_flags())
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _link(source: Path, destination: Path) -> None:
    if platform.IS_WINDOWS:
        os.link(source, destination)
    else:
        os.link(source, destination, follow_symlinks=False)


def _lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _short_name(prefix: str) -> str:
    return f".{prefix}{uuid.uuid4().hex[:8]}"


def _create_empty(path: Path, mode: int = 0o600) -> os.stat_result:
    descriptor = os.open(path, _CREATE_FLAGS | os.O_WRONLY, mode)
    try:
        return os.fstat(descriptor)
    finally:
        os.close(descriptor)


def _owned(info: os.stat_result) -> bool:
    uid = platform.current_uid()
    return uid is None or info.st_uid == uid


@contextmanager
def file_lease(
    path: Path,
    *,
    blocking: bool = True,
    interrupted: Callable[[], bool] | None = None,
) -> Iterator[None]:
    """Hold one private module lease without traversing links."""
    path = absolute(path)
    parent = anchor_directory(path.parent, create=True)
    if platform.is_link(path):
        raise ValueError("lease must be a private owned regular file")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | _NOFOLLOW | _BINARY, 0o600)
    locked = False
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not _owned(opened)
            or opened.st_nlink != 1
        ):
            raise ValueError("lease must be a private owned regular file")
        os.fchmod(descriptor, 0o600)
        while True:
            if interrupted is not None and interrupted():
                raise KeyboardInterrupt
            if platform.lock_exclusive_nonblocking(descriptor):
                locked = True
                break
            if not blocking:
                raise BlockingIOError(errno.EWOULDBLOCK, "lease is held", str(path))
            time.sleep(0.05)
        visible = os.lstat(path)
        if file_identity(visible) != file_identity(opened) or not parent.is_current():
            raise RuntimeError("lease changed while being acquired")
        yield
    finally:
        try:
            if locked:
                platform.unlock(descriptor)
        finally:
            os.close(descriptor)


def write_exclusive(
    destination: Path,
    writer: Callable[[Path], None],
    *,
    staging_directory: Path | None = None,
    guard: Callable[[], bool] | None = None,
) -> Path:
    """Publish a new durable file without replacing an existing name.

    Both directories are anchored for the whole transaction and rechecked
    around the link.  ``guard`` lets a caller bind publication to a wider
    directory graph without reimplementing link, fsync, rollback, or
    outcome-unknown handling.
    """
    requested_destination = destination
    destination = absolute(destination)
    staging = absolute(staging_directory or destination.parent)
    destination_anchor = anchor_directory(destination.parent, create=True)
    temporary = staging / _short_name("t")
    linked = False
    temporary_exists = False
    preserve_temporary = False
    staged_identity: FileIdentity | None = None

    def stable() -> bool:
        return (
            (guard is None or guard())
            and destination_anchor.is_current()
            and staging_anchor.is_current()
        )

    try:
        staging_anchor = anchor_directory(staging, create=True)
        if destination_anchor.identity.device != staging_anchor.identity.device:
            raise ValueError(
                "exclusive publication staging must share the destination filesystem"
            )
        created = _create_empty(temporary)
        temporary_exists = True
        writer(temporary)
        if not (destination_anchor.is_current() and staging_anchor.is_current()):
            raise RuntimeError("exclusive publication directories changed")
        visible = os.lstat(temporary)
        if (
            not stat.S_ISREG(visible.st_mode)
            or not _owned(visible)
            or visible.st_nlink != 1
            or file_identity(visible) != file_identity(created)
        ):
            raise ValueError(
                "exclusive publication writer must leave one private owned regular file"
            )
        descriptor = os.open(temporary, _sync_flags())
        try:
            staged = os.fstat(descriptor)
            if file_identity(staged) != file_identity(created) or file_state(
                staged
            ) != file_state(os.lstat(temporary)):
                raise RuntimeError("exclusive staged file changed before fsync")
            os.fsync(descriptor)
            staged = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if file_state(staged) != file_state(os.lstat(temporary)):
            raise RuntimeError("exclusive staged file changed while being fsynced")
        staged_identity = file_identity(staged)
        if not stable():
            raise RuntimeError("exclusive publication directories changed")
        _link(temporary, destination)
        linked = True
        published = os.lstat(destination)
        if (
            not stat.S_ISREG(published.st_mode)
            or file_identity(published) != staged_identity
        ):
            raise RuntimeError("exclusive destination differs from staged content")
        fsync_directory(destination.parent)
        if not stable():
            raise RuntimeError("exclusive publication directories changed")
        os.unlink(temporary)
        temporary_exists = False
        _fsync_file(destination)
        fsync_directory(staging)
        published = os.lstat(destination)
        if (
            published.st_nlink != 1
            or file_identity(published) != staged_identity
            or not stable()
        ):
            raise RuntimeError("exclusive publication did not reach a stable commit")
        linked = False
        return requested_destination
    except BaseException as commit_error:
        if linked:
            try:
                if staged_identity is None:
                    raise RuntimeError("exclusive staged identity was not captured")
                current = os.lstat(destination)
                if file_identity(current) != staged_identity:
                    raise RuntimeError(
                        "exclusive destination changed before rollback"
                    )
                os.unlink(destination)
                fsync_directory(destination.parent)
                if _lstat_or_none(destination) is not None:
                    raise RuntimeError(
                        "exclusive destination survived rollback"
                    )
                fsync_directory(staging)
                linked = False
            except BaseException as rollback_error:
                preserve_temporary = temporary_exists
                raise PublicationOutcomeUnknown(
                    "exclusive commit failed and rollback could not be established"
                ) from rollback_error
        raise commit_error
    finally:
        if temporary_exists and not preserve_temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def write_atomic(
    destination: Path,
    writer: Callable[[Path], None],
    *,
    staging_directory: Path | None = None,
) -> Path:
    """Replace a durable file and prove either commit or exact rollback.

    The predecessor is pinned by a hard link (``.b<hex>``) in the staging
    directory, which both keeps its identity alive and serves as the rollback
    source.
    """
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
    destination_anchor = anchor_directory(destination.parent, create=True)
    temporary = staging / _short_name("t")
    backup = staging / _short_name("b")
    temporary_created = False
    backup_created = False
    preserve_backup = False
    replaced = False
    committed = False
    previous_identity: FileIdentity | None = None
    replacement_identity: FileIdentity | None = None

    def directories_current() -> bool:
        return destination_anchor.is_current() and staging_anchor.is_current()

    try:
        staging_anchor = anchor_directory(staging, create=True)
        _create_empty(temporary)
        temporary_created = True
        writer(temporary)
        if not directories_current():
            raise RuntimeError("durable publication directories changed")
        visible = os.lstat(temporary)
        if not stat.S_ISREG(visible.st_mode) or visible.st_nlink != 1:
            raise ValueError("durable writer must leave one private regular file")
        descriptor = os.open(temporary, _sync_flags())
        try:
            staged = os.fstat(descriptor)
            if file_identity(staged) != file_identity(visible) or file_state(
                staged
            ) != file_state(os.lstat(temporary)):
                raise RuntimeError("staged durable file changed before fsync")
            os.fsync(descriptor)
            staged = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if file_state(staged) != file_state(os.lstat(temporary)):
            raise RuntimeError("staged durable file changed while being fsynced")
        replacement_identity = file_identity(staged)

        predecessor = _lstat_or_none(destination)
        if predecessor is not None:
            if not stat.S_ISREG(predecessor.st_mode):
                raise ValueError(
                    f"destination must be a regular non-symlink file: {destination}"
                )
            previous_identity = file_identity(predecessor)
            _link(destination, backup)
            backup_created = True
            visible_previous = os.lstat(destination)
            held_backup = os.lstat(backup)
            if (
                file_identity(visible_previous) != previous_identity
                or file_identity(held_backup) != previous_identity
                or visible_previous.st_size != predecessor.st_size
                or visible_previous.st_mtime_ns != predecessor.st_mtime_ns
            ):
                raise RuntimeError("durable predecessor changed before replacement")

        if not directories_current():
            raise RuntimeError("durable publication directories changed")
        current_predecessor = _lstat_or_none(destination)
        if previous_identity is None:
            if current_predecessor is not None:
                raise RuntimeError("durable destination appeared before replacement")
        elif (
            current_predecessor is None
            or file_identity(current_predecessor) != previous_identity
            or file_identity(os.lstat(backup)) != previous_identity
        ):
            raise RuntimeError("durable predecessor changed before replacement")
        platform.replace_with_retry(temporary, destination)
        replaced = True
        temporary_created = False
        published = os.lstat(destination)
        if (
            not stat.S_ISREG(published.st_mode)
            or file_identity(published) != replacement_identity
        ):
            raise RuntimeError("durable replacement changed during publication")
        if not directories_current():
            raise RuntimeError("durable publication directories changed after replace")
        fsync_directory(staging)
        fsync_directory(destination.parent)
        if not directories_current():
            raise RuntimeError("durable publication directories changed during fsync")
        committed = True
        return requested_destination
    except BaseException as commit_error:
        if replaced:
            try:
                if replacement_identity is None:
                    raise RuntimeError("durable replacement identity was not captured")
                if not directories_current():
                    raise RuntimeError("durable publication directories changed")
                current = os.lstat(destination)
                if file_identity(current) != replacement_identity:
                    raise RuntimeError(
                        "durable destination changed before rollback"
                    )
                if previous_identity is None:
                    os.unlink(destination)
                    if _lstat_or_none(destination) is not None:
                        raise RuntimeError(
                            "new durable destination survived rollback"
                        )
                else:
                    platform.replace_with_retry(backup, destination)
                    backup_created = False
                    restored = os.lstat(destination)
                    if file_identity(restored) != previous_identity:
                        raise RuntimeError(
                            "previous durable destination was not restored"
                        )
                fsync_directory(destination.parent)
                fsync_directory(staging)
                if not directories_current():
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
        if temporary_created:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        if backup_created and not preserve_backup:
            try:
                os.unlink(backup)
                if committed:
                    try:
                        fsync_directory(staging)
                    except OSError:
                        pass
            except OSError:
                pass


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
    parent = anchor_directory(destination.parent)
    temporary = parent.path / _short_name("t")
    temporary_descriptor = -1
    try:
        mode = default_mode
        existing = _lstat_or_none(destination)
        if existing is not None:
            if not stat.S_ISREG(existing.st_mode):
                raise ValueError(
                    "existing destination must be a regular non-symlink file"
                )
            if not replace:
                raise FileExistsError(f"destination already exists: {destination}")
            mode = stat.S_IMODE(existing.st_mode)
        temporary_descriptor = os.open(temporary, _CREATE_FLAGS | os.O_WRONLY, mode)
        os.fchmod(temporary_descriptor, mode)
        write_all(temporary_descriptor, content)
        os.fsync(temporary_descriptor)
        os.close(temporary_descriptor)
        temporary_descriptor = -1
        if not parent.is_current():
            raise RuntimeError("publication directory changed")
        if replace:
            platform.replace_with_retry(temporary, destination)
        else:
            _link(temporary, destination)
            os.unlink(temporary)
        fsync_directory(parent.path)
        if not parent.is_current():
            raise RuntimeError("publication directory changed")
        return requested_destination
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


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
