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

    def fail_rollback(source: Path, target: Path) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("injected rollback failure")
        real_replace(source, target)

    monkeypatch.setattr(durable_publication.os, "fsync", fail_commit)
    monkeypatch.setattr(durable_publication.os, "replace", fail_rollback)

    with pytest.raises(OSError, match="rollback failure"):
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
