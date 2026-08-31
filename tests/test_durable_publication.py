import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from eqasim_sa.core import durable_publication
from eqasim_sa.core.durable_publication import (
    PublicationOutcomeUnknown,
    capture_stable_file,
    stable_file,
    verify_stable_file,
    write_parquet_atomic,
    write_parquet_exclusive,
    replace_bytes_restorable,
    write_atomic,
)


def test_stable_capture_is_exclusive_and_verifiable(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    destination = tmp_path / "captured.bin"
    source.write_bytes(b"stable content")

    snapshot = capture_stable_file(source, destination)

    assert destination.read_bytes() == b"stable content"
    assert snapshot == stable_file(source)
    verify_stable_file(snapshot)
    with pytest.raises(FileExistsError):
        capture_stable_file(source, destination)


def test_stable_capture_rejects_symlink_source(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"content")
    linked = tmp_path / "linked.bin"
    linked.symlink_to(source)

    with pytest.raises(ValueError, match="non-symlink"):
        capture_stable_file(linked, tmp_path / "captured.bin")


def test_atomic_replacement_restores_previous_file_after_directory_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "value.parquet"
    pq.write_table(pa.table({"value": [1]}), destination, compression="zstd")
    real_fsync = durable_publication.os.fsync
    calls = 0

    def fail_commit(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(durable_publication.os, "fsync", fail_commit)
    with pytest.raises(OSError, match="directory fsync"):
        write_parquet_atomic(pa.table({"value": [2]}), destination)

    assert pq.read_table(destination).to_pylist() == [{"value": 1}]
    assert not tuple(tmp_path.glob(".*.tmp"))
    assert not tuple(tmp_path.glob(".*.backup"))


def test_failed_atomic_rollback_preserves_recoverable_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "value.parquet"
    pq.write_table(pa.table({"value": [1]}), destination, compression="zstd")
    real_fsync = durable_publication.os.fsync
    real_replace = durable_publication.os.replace
    fsync_calls = 0
    replace_calls = 0

    def fail_commit(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("injected directory fsync failure")
        real_fsync(descriptor)

    def fail_rollback(source, target, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("injected rollback failure")
        real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(durable_publication.os, "fsync", fail_commit)
    monkeypatch.setattr(durable_publication.os, "replace", fail_rollback)

    with pytest.raises(PublicationOutcomeUnknown, match="rollback"):
        write_parquet_atomic(pa.table({"value": [2]}), destination)

    backups = tuple(tmp_path.glob(".*.backup"))
    assert len(backups) == 1
    assert pq.read_table(backups[0]).to_pylist() == [{"value": 1}]


def test_exclusive_publication_never_clobbers_existing_destination(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "value.parquet"
    write_parquet_exclusive(pa.table({"value": [1]}), destination)

    with pytest.raises(FileExistsError):
        write_parquet_exclusive(pa.table({"value": [2]}), destination)

    assert pq.read_table(destination).to_pylist() == [{"value": 1}]


def test_restorable_bytes_remove_temporary_after_staging_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "pointer.json"

    def fail_write(descriptor: int, content: bytes) -> None:
        os.write(descriptor, content[:1])
        raise OSError("injected staging failure")

    monkeypatch.setattr(durable_publication, "write_all", fail_write)
    with pytest.raises(OSError, match="staging failure"):
        replace_bytes_restorable(
            b"new", destination, staging_directory=tmp_path
        )

    assert not destination.exists()
    assert not tuple(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("damage", ["symlink", "hardlink", "fifo"])
def test_atomic_writer_must_leave_one_private_regular_file(
    tmp_path: Path, damage: str
) -> None:
    destination = tmp_path / "value.bin"
    foreign = tmp_path / "foreign.bin"
    foreign.write_bytes(b"foreign")

    def damage_staging(path: Path) -> None:
        path.unlink()
        if damage == "symlink":
            path.symlink_to(foreign)
        elif damage == "hardlink":
            os.link(foreign, path)
        else:
            os.mkfifo(path)

    with pytest.raises(ValueError, match="private regular"):
        write_atomic(destination, damage_staging)

    assert not destination.exists()
    assert not tuple(tmp_path.glob(".*.tmp"))


def test_atomic_rechecks_exact_predecessor_before_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "value.bin"
    destination.write_bytes(b"original")
    real_current = durable_publication.directory_is_current
    calls = 0

    def mutate_during_parent_check(path: Path, descriptor: int) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            destination.unlink()
            destination.write_bytes(b"concurrent")
        return real_current(path, descriptor)

    monkeypatch.setattr(
        durable_publication, "directory_is_current", mutate_during_parent_check
    )
    with pytest.raises(RuntimeError, match="predecessor changed"):
        write_atomic(destination, lambda path: path.write_bytes(b"replacement"))

    assert destination.read_bytes() == b"concurrent"
    assert not tuple(tmp_path.glob(".*.backup"))


@pytest.mark.parametrize("interruption", [False, True])
def test_atomic_rolls_back_every_failure_after_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: bool,
) -> None:
    destination = tmp_path / "value.bin"
    destination.write_bytes(b"original")
    real_fsync = durable_publication.os.fsync
    calls = 0

    def fail_after_replace(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            if interruption:
                raise KeyboardInterrupt
            raise OSError("injected post-replace failure")
        real_fsync(descriptor)

    monkeypatch.setattr(durable_publication.os, "fsync", fail_after_replace)
    expected = KeyboardInterrupt if interruption else OSError
    with pytest.raises(expected):
        write_atomic(destination, lambda path: path.write_bytes(b"replacement"))

    assert destination.read_bytes() == b"original"
    assert not tuple(tmp_path.glob(".*.tmp"))
    assert not tuple(tmp_path.glob(".*.backup"))


def test_atomic_rolls_back_new_destination_to_proven_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "value.bin"
    real_fsync = durable_publication.os.fsync
    calls = 0

    def fail_after_replace(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected post-replace failure")
        real_fsync(descriptor)

    monkeypatch.setattr(durable_publication.os, "fsync", fail_after_replace)
    with pytest.raises(OSError, match="post-replace"):
        write_atomic(destination, lambda path: path.write_bytes(b"replacement"))

    assert not destination.exists()


def test_atomic_cleanup_failure_after_commit_does_not_change_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "value.bin"
    destination.write_bytes(b"original")
    real_unlink = durable_publication.os.unlink

    def fail_backup_cleanup(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(path).endswith(".backup"):
            raise OSError("injected cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(durable_publication.os, "unlink", fail_backup_cleanup)

    assert write_atomic(
        destination, lambda path: path.write_bytes(b"replacement")
    ) == destination.resolve()
    assert destination.read_bytes() == b"replacement"
    assert len(tuple(tmp_path.glob(".*.backup"))) == 1


def test_exclusive_publication_reports_unknown_when_absence_cannot_be_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "value.parquet"
    real_fsync = durable_publication.os.fsync
    real_unlink = Path.unlink
    fsync_calls = 0

    def fail_commit(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("injected directory fsync failure")
        real_fsync(descriptor)

    def fail_rollback(path: Path, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        if path == destination:
            raise OSError("injected rollback failure")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(durable_publication.os, "fsync", fail_commit)
    monkeypatch.setattr(Path, "unlink", fail_rollback)

    with pytest.raises(PublicationOutcomeUnknown, match="rollback"):
        write_parquet_exclusive(pa.table({"value": [1]}), destination)

    assert pq.read_table(destination).to_pylist() == [{"value": 1}]
