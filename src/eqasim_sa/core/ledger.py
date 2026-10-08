import errno
import json
import math
import os
import re
import stat
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from eqasim_sa.utils.filesystem import (
    FileIdentity,
    FileState,
    StableFile,
    absolute,
    descriptor_sha256,
    directory_is_current,
    file_lease,
    file_identity,
    file_state,
    fsync_directory,
    open_child_directory,
    open_directory,
    open_regular,
    secure_directory,
    write_parquet_exclusive,
)
from eqasim_sa.ports.simulator import ExecutionEvidence

_PARQUET_FILE = pq.ParquetFile

RESERVED_COLUMNS = (
    "runId",
    "campaign",
    "seed",
    "status",
    "attempts",
    "contextHash",
    "node",
    "duration",
    "scratchBytes",
)
Scalar = bool | int | float | str
RUN_ID = re.compile(r"^[0-9a-f]{32}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.parquet$")
COMPLETION_MARKER_VERSION = b"completionMarkerVersion"
ARTIFACT_MANIFEST = b"artifactManifest"
EXECUTION_EVIDENCE = b"executionEvidence"
LEGACY_UNBOUND = "legacy_unbound"
VERIFIED = "verified"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    campaign: str
    seed: int
    context_hash: str
    node: str
    duration: float
    parameters: dict[str, Scalar]
    scratch_bytes: int = 0


@dataclass(frozen=True)
class ArtifactManifestEntry:
    name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class CompletionState:
    duration: float
    scratch_bytes: int
    archive_integrity: str
    artifacts: tuple[ArtifactManifestEntry, ...]
    execution_evidence: ExecutionEvidence | None
    marker: StableFile
    archive: Path | None
    archive_state: FileState | None
    captured_artifacts: tuple[StableFile, ...]


def run_path(campaign_root: Path, run_id: str) -> Path:
    if type(run_id) is not str or not RUN_ID.fullmatch(run_id):
        raise ValueError(
            "run_id must contain exactly 32 lowercase hexadecimal characters"
        )
    return campaign_root / "runs" / f"{run_id}.parquet"


@contextmanager
def run_lease(
    campaign_root: Path,
    run_id: str,
    interrupted: Callable[[], bool] | None = None,
) -> Iterator[None]:
    if type(run_id) is not str or not RUN_ID.fullmatch(run_id):
        raise ValueError(
            "run_id must contain exactly 32 lowercase hexadecimal characters"
        )
    technical = secure_directory(campaign_root / ".eqasim-sa", create=True)
    locks = secure_directory(technical / "locks", create=True)
    runs = secure_directory(locks / "runs", create=True)
    fsync_directory(campaign_root)
    fsync_directory(technical)
    fsync_directory(locks)
    with file_lease(runs / f"{run_id}.lock", interrupted=interrupted):
        yield


def _regular_path_is_current(path: Path, expected: FileState) -> bool:
    try:
        descriptor, parent, _ = open_regular(path)
    except (OSError, ValueError):
        return False
    try:
        return file_state(os.fstat(descriptor)) == expected
    finally:
        os.close(descriptor)
        os.close(parent)


def _manifest_bytes(entries: Sequence[ArtifactManifestEntry]) -> bytes:
    return json.dumps(
        [
            {"name": entry.name, "size": entry.size, "sha256": entry.sha256}
            for entry in entries
        ],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _validated_manifest_entries(
    entries: Sequence[ArtifactManifestEntry],
) -> tuple[ArtifactManifestEntry, ...]:
    validated = tuple(entries)
    if not validated:
        raise ValueError("artifact manifest must not be empty")
    for entry in validated:
        if (
            not isinstance(entry, ArtifactManifestEntry)
            or type(entry.name) is not str
            or not ARTIFACT_NAME.fullmatch(entry.name)
            or type(entry.size) is not int
            or entry.size < 0
            or type(entry.sha256) is not str
            or not SHA256.fullmatch(entry.sha256)
        ):
            raise ValueError("artifact manifest contains an invalid entry")
    names = [entry.name for entry in validated]
    if len(names) != len(set(names)):
        raise ValueError("artifact manifest contains duplicate names")
    return tuple(sorted(validated, key=lambda entry: entry.name.encode("utf-8")))


def _parse_manifest(payload: bytes) -> tuple[ArtifactManifestEntry, ...]:
    try:
        document = json.loads(payload)
        if not isinstance(document, list):
            raise ValueError
        entries = []
        for item in document:
            if not isinstance(item, dict) or set(item) != {"name", "size", "sha256"}:
                raise ValueError
            entries.append(
                ArtifactManifestEntry(item["name"], item["size"], item["sha256"])
            )
        validated = _validated_manifest_entries(entries)
        if [entry.name for entry in entries] != [entry.name for entry in validated]:
            raise ValueError
        if payload != _manifest_bytes(validated):
            raise ValueError
        return validated
    except (TypeError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("artifact manifest is invalid or noncanonical") from error


def _entry_state(parent: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _verify_archive(
    campaign_root: Path,
    campaign: int,
    run_id: str,
    artifacts: tuple[ArtifactManifestEntry, ...],
) -> tuple[Path, FileState, tuple[StableFile, ...]]:
    archives = open_child_directory(campaign, "archives")
    try:
        archives_identity = file_identity(os.fstat(archives))
        if file_identity(
            os.stat("archives", dir_fd=campaign, follow_symlinks=False)
        ) != archives_identity:
            raise RuntimeError("archives directory changed while opening")
        archive = open_child_directory(archives, run_id)
        try:
            initial_directory = os.fstat(archive)
            visible_directory = os.stat(run_id, dir_fd=archives, follow_symlinks=False)
            if file_state(initial_directory) != file_state(visible_directory):
                raise RuntimeError("archive changed while opening")
            expected_names = [entry.name for entry in artifacts]
            names = os.listdir(archive)
            if sorted(names, key=lambda name: os.fsencode(name)) != expected_names:
                raise ValueError("archive inventory does not match artifact manifest")
            captured = []
            for entry in artifacts:
                visible = os.stat(
                    entry.name, dir_fd=archive, follow_symlinks=False
                )
                if not stat.S_ISREG(visible.st_mode):
                    raise ValueError(
                        f"archive artifact {entry.name!r} is not a regular file"
                    )
                descriptor = os.open(entry.name, _READ_FLAGS, dir_fd=archive)
                try:
                    initial = os.fstat(descriptor)
                    visible = os.stat(
                        entry.name, dir_fd=archive, follow_symlinks=False
                    )
                    if (
                        not stat.S_ISREG(initial.st_mode)
                        or not stat.S_ISREG(visible.st_mode)
                        or file_state(initial) != file_state(visible)
                    ):
                        raise ValueError(
                            f"archive artifact {entry.name!r} is not a stable regular file"
                        )
                    if initial.st_size != entry.size:
                        raise ValueError(
                            f"archive artifact {entry.name!r} size does not match manifest"
                        )
                    with os.fdopen(os.dup(descriptor), "rb") as stream:
                        pq.ParquetFile(stream).metadata
                    digest = descriptor_sha256(descriptor)
                    final = os.fstat(descriptor)
                    visible = os.stat(
                        entry.name, dir_fd=archive, follow_symlinks=False
                    )
                    if file_state(final) != file_state(initial) or file_state(visible) != file_state(
                        initial
                    ):
                        raise RuntimeError(
                            f"archive artifact {entry.name!r} changed while hashing"
                        )
                    if digest != entry.sha256:
                        raise ValueError(
                            f"archive artifact {entry.name!r} hash does not match manifest"
                        )
                    captured.append(
                        StableFile(
                            campaign_root / "archives" / run_id / entry.name,
                            file_state(initial),
                            initial.st_size,
                            digest,
                        )
                    )
                finally:
                    os.close(descriptor)
            if (
                sorted(os.listdir(archive), key=lambda name: os.fsencode(name))
                != sorted(names, key=lambda name: os.fsencode(name))
                or file_state(os.fstat(archive)) != file_state(initial_directory)
                or file_state(os.stat(run_id, dir_fd=archives, follow_symlinks=False))
                != file_state(initial_directory)
            ):
                raise RuntimeError("archive changed while verifying")
            archive_state = file_state(initial_directory)
        finally:
            os.close(archive)
        if file_identity(
            os.stat("archives", dir_fd=campaign, follow_symlinks=False)
        ) != archives_identity:
            raise RuntimeError("archives directory changed while verifying")
        return (
            campaign_root / "archives" / run_id,
            archive_state,
            tuple(captured),
        )
    finally:
        os.close(archives)


def completion_state(
    campaign_root: Path, expected: RunRecord
) -> CompletionState | None:
    path = run_path(campaign_root, expected.run_id)
    try:
        campaign = open_directory(campaign_root)
    except FileNotFoundError:
        return None
    except Exception as error:
        raise ValueError(f"invalid completion marker {path}: {error}") from error
    try:
        try:
            runs = open_child_directory(campaign, "runs")
        except FileNotFoundError:
            return None
        try:
            runs_identity = file_identity(os.fstat(runs))
            if file_identity(
                os.stat("runs", dir_fd=campaign, follow_symlinks=False)
            ) != runs_identity:
                raise RuntimeError("runs directory changed while opening marker")
            try:
                visible = os.stat(path.name, dir_fd=runs, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if not stat.S_ISREG(visible.st_mode):
                raise ValueError("not a regular non-symlink file")
            try:
                descriptor = os.open(path.name, _READ_FLAGS, dir_fd=runs)
            except FileNotFoundError:
                return None
            try:
                initial = os.fstat(descriptor)
                visible = os.stat(path.name, dir_fd=runs, follow_symlinks=False)
                if (
                    not stat.S_ISREG(initial.st_mode)
                    or not stat.S_ISREG(visible.st_mode)
                    or file_state(initial) != file_state(visible)
                ):
                    raise ValueError("not a stable regular non-symlink file")
                with os.fdopen(os.dup(descriptor), "rb") as stream:
                    parquet = _PARQUET_FILE(stream)
                    if parquet.metadata.num_rows != 1:
                        raise ValueError("row count mismatch")
                    metadata = dict(parquet.schema_arrow.metadata or {})
                    table = parquet.read()
                    codecs = {
                        parquet.metadata.row_group(group)
                        .column(column)
                        .compression
                        for group in range(parquet.metadata.num_row_groups)
                        for column in range(
                            parquet.metadata.row_group(group).num_columns
                        )
                    }
                version = metadata.get(COMPLETION_MARKER_VERSION)
                manifest_payload = metadata.get(ARTIFACT_MANIFEST)
                evidence_payload = metadata.get(EXECUTION_EVIDENCE)
                if version is None:
                    if manifest_payload is not None or evidence_payload is not None:
                        raise ValueError("completion metadata has no marker version")
                    integrity = LEGACY_UNBOUND
                    artifacts: tuple[ArtifactManifestEntry, ...] = ()
                    evidence = None
                elif version == b"1":
                    if manifest_payload is not None or evidence_payload is not None:
                        raise ValueError("legacy marker must not bind v2 metadata")
                    integrity = LEGACY_UNBOUND
                    artifacts = ()
                    evidence = None
                elif version == b"2":
                    if manifest_payload is None:
                        raise ValueError("v2 marker is missing its artifact manifest")
                    base_metadata = {
                        COMPLETION_MARKER_VERSION,
                        ARTIFACT_MANIFEST,
                    }
                    expected_metadata = base_metadata | (
                        {EXECUTION_EVIDENCE} if evidence_payload is not None else set()
                    )
                    if set(metadata) != expected_metadata:
                        raise ValueError("v2 marker metadata is not canonical")
                    artifacts = _parse_manifest(manifest_payload)
                    evidence = (
                        ExecutionEvidence.from_bytes(evidence_payload)
                        if evidence_payload is not None
                        else None
                    )
                    if evidence is not None and evidence.context_hash != expected.context_hash:
                        raise ValueError("execution evidence contextHash does not match marker")
                    integrity = VERIFIED
                else:
                    raise ValueError(f"unsupported completion marker version: {version!r}")
                schema_record = RunRecord(
                    expected.run_id,
                    expected.campaign,
                    expected.seed,
                    expected.context_hash,
                    "",
                    0.0,
                    expected.parameters,
                )
                if not _run_table(schema_record).schema.equals(
                    table.schema, check_metadata=False
                ) or any(column.null_count for column in table.columns):
                    raise ValueError("schema, row count, or null value mismatch")
                row = table.to_pylist()[0]
                fixed = {
                    "runId": expected.run_id,
                    "campaign": expected.campaign,
                    "seed": expected.seed,
                    "status": "completed",
                    "attempts": 1,
                    "contextHash": expected.context_hash,
                    **expected.parameters,
                }
                if any(
                    row[name] != value or type(row[name]) is not type(value)
                    for name, value in fixed.items()
                ):
                    raise ValueError("identity, context, or parameter mismatch")
                duration = row["duration"]
                if (
                    type(duration) is not float
                    or not math.isfinite(duration)
                    or duration < 0
                ):
                    raise ValueError("duration must be a finite non-negative float")
                scratch_bytes = row["scratchBytes"]
                if type(scratch_bytes) is not int or scratch_bytes < 0:
                    raise ValueError("scratchBytes must be a non-negative integer")
                if codecs != {"ZSTD"}:
                    raise ValueError("compression must be zstd")
                if integrity == VERIFIED:
                    archive, archive_state, captured_artifacts = _verify_archive(
                        campaign_root, campaign, expected.run_id, artifacts
                    )
                else:
                    archive = None
                    archive_state = None
                    captured_artifacts = ()
                marker_sha256 = descriptor_sha256(descriptor)
                final = os.fstat(descriptor)
                visible = os.stat(path.name, dir_fd=runs, follow_symlinks=False)
                if file_state(final) != file_state(initial) or file_state(visible) != file_state(
                    initial
                ):
                    raise RuntimeError("completion marker changed while verifying")
                if file_identity(
                    os.stat("runs", dir_fd=campaign, follow_symlinks=False)
                ) != runs_identity:
                    raise RuntimeError("runs directory changed while verifying marker")
                if not directory_is_current(campaign_root, campaign):
                    raise RuntimeError("campaign root changed while verifying marker")
                return CompletionState(
                    duration,
                    scratch_bytes,
                    integrity,
                    artifacts,
                    evidence,
                    StableFile(path, file_state(initial), initial.st_size, marker_sha256),
                    archive,
                    archive_state,
                    captured_artifacts,
                )
            finally:
                os.close(descriptor)
        finally:
            os.close(runs)
    except Exception as error:
        raise ValueError(f"invalid completion marker {path}: {error}") from error
    finally:
        os.close(campaign)


def completion_marker_names(campaign_root: Path) -> tuple[str, ...]:
    try:
        campaign = open_directory(campaign_root)
    except FileNotFoundError:
        return ()
    except Exception as error:
        raise ValueError(f"invalid completion marker directory: {error}") from error
    try:
        try:
            runs = open_child_directory(campaign, "runs")
        except FileNotFoundError:
            return ()
        try:
            initial = file_state(os.fstat(runs))
            identity = file_identity(os.fstat(runs))
            names = tuple(os.listdir(runs))
            if (
                file_state(os.fstat(runs)) != initial
                or file_identity(
                    os.stat("runs", dir_fd=campaign, follow_symlinks=False)
                )
                != identity
                or not directory_is_current(campaign_root, campaign)
            ):
                raise RuntimeError("runs directory changed while listing markers")
            return names
        finally:
            os.close(runs)
    except Exception as error:
        raise ValueError(f"invalid completion marker directory: {error}") from error
    finally:
        os.close(campaign)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written == 0:
            raise OSError("short write while staging artifact")
        view = view[written:]


def _parquet_codecs(parquet: pq.ParquetFile) -> set[str]:
    return {
        parquet.metadata.row_group(group).column(column).compression
        for group in range(parquet.metadata.num_row_groups)
        for column in range(parquet.metadata.row_group(group).num_columns)
    }


def _scan_parquet_descriptor(
    descriptor: int, interrupted: Callable[[], bool] | None
) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    with os.fdopen(os.dup(descriptor), "rb") as stream:
        parquet = pq.ParquetFile(stream)
        if _parquet_codecs(parquet) - {"ZSTD"}:
            raise ValueError("published artifacts must use zstd compression")
        for _ in parquet.iter_batches(batch_size=65536):
            if interrupted is not None and interrupted():
                raise KeyboardInterrupt


def _copy_parquet_artifact(
    source: Path,
    destination_directory: int,
    name: str,
    interrupted: Callable[[], bool] | None,
) -> ArtifactManifestEntry:
    source_descriptor, source_parent, source_name = open_regular(source)
    destination_descriptor = -1
    try:
        source_initial = os.fstat(source_descriptor)
        destination_descriptor = os.open(
            name,
            os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
            dir_fd=destination_directory,
        )
        with os.fdopen(os.dup(source_descriptor), "rb") as source_stream:
            parquet = pq.ParquetFile(source_stream)
            already_zstd = not (_parquet_codecs(parquet) - {"ZSTD"})
            if already_zstd:
                os.lseek(source_descriptor, 0, os.SEEK_SET)
                while True:
                    if interrupted is not None and interrupted():
                        raise KeyboardInterrupt
                    chunk = os.read(source_descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    _write_all(destination_descriptor, chunk)
            else:
                with os.fdopen(os.dup(destination_descriptor), "wb") as output:
                    with pq.ParquetWriter(
                        output, parquet.schema_arrow, compression="zstd"
                    ) as writer:
                        for batch in parquet.iter_batches(batch_size=65536):
                            if interrupted is not None and interrupted():
                                raise KeyboardInterrupt
                            writer.write_batch(batch)
                    output.flush()
        source_final = os.fstat(source_descriptor)
        source_visible = os.stat(
            source_name, dir_fd=source_parent, follow_symlinks=False
        )
        if (
            file_state(source_final) != file_state(source_initial)
            or file_state(source_visible) != file_state(source_initial)
            or not _regular_path_is_current(source, file_state(source_initial))
        ):
            raise RuntimeError(f"artifact changed while publishing: {source}")
        os.fsync(destination_descriptor)
        _scan_parquet_descriptor(destination_descriptor, interrupted)
        digest = descriptor_sha256(destination_descriptor, interrupted)
        final = os.fstat(destination_descriptor)
        visible = os.stat(name, dir_fd=destination_directory, follow_symlinks=False)
        if (
            not stat.S_ISREG(final.st_mode)
            or file_state(final) != file_state(visible)
        ):
            raise RuntimeError(f"staged artifact changed while publishing: {name}")
        return ArtifactManifestEntry(name, final.st_size, digest)
    except BaseException:
        try:
            os.unlink(name, dir_fd=destination_directory)
        except FileNotFoundError:
            pass
        raise
    finally:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        os.close(source_descriptor)
        os.close(source_parent)


def _remove_entry_at(parent: int, name: str) -> None:
    info = _entry_state(parent, name)
    if info is None:
        return
    if stat.S_ISDIR(info.st_mode):
        child = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
        try:
            opened = os.fstat(child)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise RuntimeError(f"directory changed while removing: {name}")
            for entry in os.listdir(child):
                _remove_entry_at(child, entry)
            visible = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (visible.st_dev, visible.st_ino) != (opened.st_dev, opened.st_ino):
                raise RuntimeError(f"directory changed while removing: {name}")
        finally:
            os.close(child)
        os.rmdir(name, dir_fd=parent)
    else:
        os.unlink(name, dir_fd=parent)


class ArchiveTransaction:
    def __init__(
        self,
        campaign_root: Path,
        run_id: str,
        artifacts: Sequence[tuple[str, Path]],
        interrupted: Callable[[], bool] | None,
    ) -> None:
        run_path(campaign_root, run_id)
        sources = tuple(artifacts)
        if not sources:
            raise ValueError("manifest must contain at least one artifact")
        if any(
            type(name) is not str
            or not ARTIFACT_NAME.fullmatch(name)
            or not isinstance(path, Path)
            for name, path in sources
        ):
            raise ValueError("archive transaction contains an invalid artifact")
        names = [name for name, _ in sources]
        if len(names) != len(set(names)):
            raise ValueError("archive transaction contains duplicate artifacts")
        self.campaign_root = campaign_root
        self.run_id = run_id
        self.sources = tuple(
            sorted(sources, key=lambda item: item[0].encode("utf-8"))
        )
        self.interrupted = interrupted
        self.manifest: tuple[ArtifactManifestEntry, ...] = ()
        self._campaign_parent = -1
        self._campaign_name = ""
        self._campaign = -1
        self._technical = -1
        self._archives = -1
        self._runs = -1
        self._marker_staging = -1
        self._staging = -1
        self._staging_name: str | None = None
        self._committed = False
        self._archives_identity: FileIdentity | None = None
        self._runs_identity: FileIdentity | None = None
        self._technical_identity: FileIdentity | None = None
        self._marker_staging_identity: FileIdentity | None = None

    def prepare(self) -> None:
        absolute_path = absolute(self.campaign_root)
        if not absolute_path.name:
            raise ValueError("campaign root must have a final path component")
        self._campaign_parent = open_directory(absolute_path.parent, create=True)
        self._campaign_name = absolute_path.name
        self._campaign = open_child_directory(
            self._campaign_parent, self._campaign_name, create=True
        )
        self._archives = open_child_directory(self._campaign, "archives", create=True)
        self._runs = open_child_directory(self._campaign, "runs", create=True)
        self._technical = open_child_directory(
            self._campaign, ".eqasim-sa", create=True
        )
        self._marker_staging = open_child_directory(
            self._technical, "transactions", create=True
        )
        self._archives_identity = file_identity(os.fstat(self._archives))
        self._runs_identity = file_identity(os.fstat(self._runs))
        self._technical_identity = file_identity(os.fstat(self._technical))
        self._marker_staging_identity = file_identity(os.fstat(self._marker_staging))
        os.fsync(self._technical)
        os.fsync(self._campaign)
        self._staging_name = f".{self.run_id}.{uuid.uuid4().hex}.tmp"
        os.mkdir(self._staging_name, mode=0o700, dir_fd=self._archives)
        self._staging = open_child_directory(self._archives, self._staging_name)
        self.manifest = tuple(
            _copy_parquet_artifact(source, self._staging, name, self.interrupted)
            for name, source in self.sources
        )
        os.fsync(self._staging)

    def _campaign_is_current(self) -> bool:
        if self._campaign_parent < 0 or self._campaign < 0:
            return False
        try:
            visible = os.stat(
                self._campaign_name,
                dir_fd=self._campaign_parent,
                follow_symlinks=False,
            )
        except OSError:
            return False
        return file_identity(visible) == file_identity(os.fstat(self._campaign))

    def _publication_is_current(self) -> bool:
        try:
            return (
                self._campaign_is_current()
                and file_identity(
                    os.stat("archives", dir_fd=self._campaign, follow_symlinks=False)
                )
                == self._archives_identity
                and file_identity(
                    os.stat("runs", dir_fd=self._campaign, follow_symlinks=False)
                )
                == self._runs_identity
                and file_identity(
                    os.stat(
                        ".eqasim-sa",
                        dir_fd=self._campaign,
                        follow_symlinks=False,
                    )
                )
                == self._technical_identity
                and file_identity(
                    os.stat(
                        "transactions",
                        dir_fd=self._technical,
                        follow_symlinks=False,
                    )
                )
                == self._marker_staging_identity
            )
        except OSError:
            return False

    def commit(
        self,
        record: RunRecord,
        marker_writer: Callable[..., Path] | None = None,
        execution_evidence: bytes | ExecutionEvidence | None = None,
    ) -> Path:
        if self._committed or self._staging_name is None or not self.manifest:
            raise RuntimeError("archive transaction is not ready to commit")
        validate_run_record(record)
        validate_parameters(record.parameters)
        if record.run_id != self.run_id:
            raise ValueError("archive transaction run_id does not match RunRecord")
        evidence = _validated_execution_evidence(execution_evidence, record.context_hash)
        if not self._publication_is_current():
            raise RuntimeError("campaign publication directories changed")
        marker_name = f"{self.run_id}.parquet"
        if _entry_state(self._runs, marker_name) is not None:
            raise FileExistsError(f"completion marker already exists: {marker_name}")
        if self.interrupted is not None and self.interrupted():
            raise KeyboardInterrupt
        archive_backups = []
        while True:
            if _entry_state(self._archives, self.run_id) is not None:
                backup = f".{self.run_id}.{uuid.uuid4().hex}.backup"
                try:
                    os.rename(
                        self.run_id,
                        backup,
                        src_dir_fd=self._archives,
                        dst_dir_fd=self._archives,
                    )
                except FileNotFoundError:
                    continue
                archive_backups.append(backup)
            try:
                os.rename(
                    self._staging_name,
                    self.run_id,
                    src_dir_fd=self._archives,
                    dst_dir_fd=self._archives,
                )
                break
            except OSError as error:
                if error.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
        self._staging_name = None
        os.fsync(self._archives)
        try:
            if (
                not self._campaign_is_current()
                or file_identity(
                    os.stat("archives", dir_fd=self._campaign, follow_symlinks=False)
                )
                != self._archives_identity
            ):
                raise RuntimeError("archives directory changed while publishing")
            if marker_writer is None:
                destination = run_path(self.campaign_root, self.run_id)
                write_parquet_exclusive(
                    _run_table_with_metadata(record, self.manifest, evidence),
                    destination,
                    staging_directory=(
                        self.campaign_root / ".eqasim-sa" / "transactions"
                    ),
                    guard=self._publication_is_current,
                )
            else:
                destination = (
                    marker_writer(
                        self.campaign_root, record, self.manifest, evidence
                    )
                    if evidence is not None
                    else marker_writer(self.campaign_root, record, self.manifest)
                )
        except BaseException:
            current = _entry_state(self._archives, self.run_id)
            staged = os.fstat(self._staging)
            if current is not None and (current.st_dev, current.st_ino) == (
                staged.st_dev,
                staged.st_ino,
            ):
                _remove_entry_at(self._archives, self.run_id)
                if archive_backups:
                    os.rename(
                        archive_backups.pop(),
                        self.run_id,
                        src_dir_fd=self._archives,
                        dst_dir_fd=self._archives,
                    )
                for backup in archive_backups:
                    _remove_entry_at(self._archives, backup)
                archive_backups.clear()
                os.fsync(self._archives)
            raise
        for backup in archive_backups:
            _remove_entry_at(self._archives, backup)
        if archive_backups:
            os.fsync(self._archives)
        self._committed = True
        return destination

    def close(self) -> None:
        try:
            if self._staging >= 0:
                os.close(self._staging)
                self._staging = -1
            if self._staging_name is not None and self._archives >= 0:
                _remove_entry_at(self._archives, self._staging_name)
                self._staging_name = None
        finally:
            for attribute in (
                "_marker_staging",
                "_technical",
                "_runs",
                "_archives",
                "_campaign",
                "_campaign_parent",
            ):
                descriptor = getattr(self, attribute)
                if descriptor >= 0:
                    os.close(descriptor)
                    setattr(self, attribute, -1)


@contextmanager
def archive_transaction(
    campaign_root: Path,
    run_id: str,
    artifacts: Sequence[tuple[str, Path]],
    interrupted: Callable[[], bool] | None = None,
) -> Iterator[ArchiveTransaction]:
    transaction = ArchiveTransaction(campaign_root, run_id, artifacts, interrupted)
    try:
        transaction.prepare()
        yield transaction
    finally:
        transaction.close()


def validate_parameters(parameters: dict[str, Scalar]) -> None:
    if not isinstance(parameters, dict):
        raise TypeError("parameters must be a dictionary")
    if collision := RESERVED_COLUMNS & parameters.keys():
        raise ValueError(f"reserved parameter columns: {sorted(collision)}")
    invalid_types = [
        name
        for name, value in parameters.items()
        if type(name) is not str or type(value) not in (bool, int, float, str)
    ]
    invalid_numbers = [
        name
        for name, value in parameters.items()
        if type(value) is int
        and not -(2**63) <= value < 2**63
        or type(value) is float
        and not math.isfinite(value)
    ]
    if invalid_types:
        raise TypeError(
            f"parameters must have string names and scalar values: {invalid_types}"
        )
    if invalid_numbers:
        raise ValueError(
            f"parameters must contain finite int64/float values: {invalid_numbers}"
        )


def validate_run_record(record: RunRecord) -> None:
    if not isinstance(record, RunRecord):
        raise TypeError("record must be a RunRecord")
    if type(record.run_id) is not str or not RUN_ID.fullmatch(record.run_id):
        raise ValueError(
            "run_id must contain exactly 32 lowercase hexadecimal characters"
        )
    if type(record.campaign) is not str or not record.campaign:
        raise ValueError("campaign must be a non-empty string")
    if type(record.seed) is not int or not -(2**63) <= record.seed < 2**63:
        raise ValueError("seed must be a signed int64 integer")
    if type(record.context_hash) is not str or not record.context_hash:
        raise ValueError("context_hash must be a non-empty string")
    if type(record.node) is not str or not record.node:
        raise ValueError("node must be a non-empty string")
    if (
        type(record.duration) is not float
        or not math.isfinite(record.duration)
        or record.duration < 0
    ):
        raise ValueError("duration must be a finite non-negative float")
    if (
        type(record.scratch_bytes) is not int
        or not 0 <= record.scratch_bytes < 2**63
    ):
        raise ValueError("scratch_bytes must be a non-negative signed int64 integer")


def write_run(
    campaign_root: Path,
    record: RunRecord,
    artifact_manifest: Sequence[ArtifactManifestEntry] | None = None,
    execution_evidence: bytes | ExecutionEvidence | None = None,
) -> Path:
    validate_run_record(record)
    validate_parameters(record.parameters)
    evidence = _validated_execution_evidence(execution_evidence, record.context_hash)
    if evidence is not None and artifact_manifest is None:
        raise ValueError("execution evidence requires a v2 artifact manifest")
    table = (
        _run_table(record)
        if artifact_manifest is None
        else _run_table_with_metadata(record, artifact_manifest, evidence)
    )
    campaign = secure_directory(campaign_root, create=True)
    runs = secure_directory(campaign / "runs", create=True)
    technical = secure_directory(campaign / ".eqasim-sa", create=True)
    transactions = secure_directory(technical / "transactions", create=True)
    if runs.stat().st_dev != transactions.stat().st_dev:
        raise ValueError(
            "completion marker transaction must share the runs filesystem"
        )
    fsync_directory(campaign)
    fsync_directory(technical)
    return write_parquet_exclusive(
        table,
        run_path(campaign, record.run_id),
        staging_directory=transactions,
    )


def _validated_execution_evidence(
    value: bytes | ExecutionEvidence | None, context_hash: str
) -> ExecutionEvidence | None:
    if value is None:
        return None
    evidence = (
        value
        if isinstance(value, ExecutionEvidence)
        else ExecutionEvidence.from_bytes(value)
        if isinstance(value, bytes)
        else None
    )
    if evidence is None:
        raise TypeError("execution evidence must be canonical bytes or ExecutionEvidence")
    if evidence.context_hash != context_hash:
        raise ValueError("execution evidence contextHash does not match RunRecord")
    return evidence


def _run_table_with_metadata(
    record: RunRecord,
    artifact_manifest: Sequence[ArtifactManifestEntry],
    execution_evidence: ExecutionEvidence | None,
) -> pa.Table:
    entries = _validated_manifest_entries(artifact_manifest)
    metadata = {
        COMPLETION_MARKER_VERSION: b"2",
        ARTIFACT_MANIFEST: _manifest_bytes(entries),
    }
    if execution_evidence is not None:
        metadata[EXECUTION_EVIDENCE] = execution_evidence.canonical_bytes()
    return _run_table(record).replace_schema_metadata(metadata)


def _run_table(record: RunRecord) -> pa.Table:
    fixed: dict[str, Scalar] = {
        "runId": record.run_id,
        "campaign": record.campaign,
        "seed": record.seed,
        "status": "completed",
        "attempts": 1,
        "contextHash": record.context_hash,
        "node": record.node,
        "duration": record.duration,
        "scratchBytes": record.scratch_bytes,
    }
    return pa.Table.from_pylist([{**fixed, **record.parameters}])
