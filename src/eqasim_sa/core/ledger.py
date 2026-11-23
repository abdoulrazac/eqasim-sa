"""Maintain durable run leases, archives, and completion records."""

import errno
import json
import math
import os
import re
import stat
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pyarrow as pa
import pyarrow.parquet as pq

from eqasim_sa.utils import platform
from eqasim_sa.utils.arrow import read_parquet
from eqasim_sa.utils.filesystem import (
    DirectoryAnchor,
    FileState,
    StableFile,
    absolute,
    anchor_directory,
    descriptor_sha256,
    file_lease,
    file_identity,
    file_state,
    fsync_directory,
    open_regular,
    secure_directory,
    short_name,
    write_parquet_exclusive,
)
from eqasim_sa.ports.simulator import Artifact, ExecutionEvidence

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
ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
ARCHIVE_FILE_NAME = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?$")
COMPLETION_MARKER_VERSION = b"completionMarkerVersion"
ARTIFACT_MANIFEST = b"artifactManifest"
EXECUTION_EVIDENCE = b"executionEvidence"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)
_READ_FLAGS = os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | _BINARY
_CREATE_FLAGS = os.O_CREAT | os.O_EXCL | os.O_RDWR | _NOFOLLOW | _BINARY


@dataclass(frozen=True)
class RunRecord:
    """Canonical completion metadata for one successfully published run."""

    run_id: str
    campaign: str
    seed: int
    context_hash: str
    node: str
    duration: float
    parameters: dict[str, Scalar]
    scratch_bytes: int = 0


def validate_source_path(value: object) -> str:
    """Validate a POSIX path relative to the simulator's output folder."""

    if (
        type(value) is not str
        or not value
        or "\\" in value
        or "\0" in value
        or ":" in value
        or value.startswith("/")
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError(f"invalid artifact source path: {value!r}")
    return value


def archive_file_name(name: str, source_path: str) -> str:
    """Name of a declared output in its run archive: ``<name><suffix>``."""

    if type(name) is not str or not ARTIFACT_NAME.fullmatch(name):
        raise ValueError(f"invalid artifact name: {name!r}")
    file_name = name + PurePosixPath(validate_source_path(source_path)).suffix
    if not ARCHIVE_FILE_NAME.fullmatch(file_name):
        raise ValueError(
            f"output {name!r} file suffix cannot name an archive file: {source_path!r}"
        )
    return file_name


@dataclass(frozen=True)
class ArtifactManifestEntry:
    """Output name, original relative path, size, and digest of one archived file."""

    name: str
    source_path: str
    size: int
    sha256: str

    @property
    def file_name(self) -> str:
        return archive_file_name(self.name, self.source_path)


@dataclass(frozen=True)
class CompletionState:
    """Validated completion record, artifact inventory, and execution evidence."""

    duration: float
    scratch_bytes: int
    artifacts: tuple[ArtifactManifestEntry, ...]
    execution_evidence: ExecutionEvidence | None
    marker: StableFile
    archive: Path
    archive_state: FileState
    captured_artifacts: tuple[StableFile, ...]


def run_path(campaign_root: Path, run_id: str) -> Path:
    """Return the completion-marker path for a validated run identity."""

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
    """Hold the exclusive per-run lease used for execution and publication."""

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
        descriptor = open_regular(path)
    except (OSError, ValueError):
        return False
    try:
        return file_state(os.fstat(descriptor)) == expected
    finally:
        os.close(descriptor)


def _manifest_bytes(entries: Sequence[ArtifactManifestEntry]) -> bytes:
    return json.dumps(
        [
            {
                "name": entry.name,
                "sourcePath": entry.source_path,
                "size": entry.size,
                "sha256": entry.sha256,
            }
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
        try:
            entry.file_name
        except ValueError as error:
            raise ValueError("artifact manifest contains an invalid entry") from error
    names = [entry.name for entry in validated]
    if len(names) != len(set(names)):
        raise ValueError("artifact manifest contains duplicate names")
    if len({entry.file_name.casefold() for entry in validated}) != len(validated):
        raise ValueError("artifact manifest contains colliding archive file names")
    return tuple(sorted(validated, key=lambda entry: entry.name.encode("utf-8")))


def _parse_manifest(payload: bytes) -> tuple[ArtifactManifestEntry, ...]:
    try:
        document = json.loads(payload)
        if not isinstance(document, list):
            raise ValueError
        entries = []
        for item in document:
            if not isinstance(item, dict) or set(item) != {
                "name",
                "sourcePath",
                "size",
                "sha256",
            }:
                raise ValueError
            entries.append(
                ArtifactManifestEntry(
                    item["name"], item["sourcePath"], item["size"], item["sha256"]
                )
            )
        validated = _validated_manifest_entries(entries)
        if [entry.name for entry in entries] != [entry.name for entry in validated]:
            raise ValueError
        if payload != _manifest_bytes(validated):
            raise ValueError
        return validated
    except (TypeError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("artifact manifest is invalid or noncanonical") from error


def _entry_state(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _verify_archive(
    campaign_root: Path,
    campaign: DirectoryAnchor,
    run_id: str,
    artifacts: tuple[ArtifactManifestEntry, ...],
) -> tuple[Path, FileState, tuple[StableFile, ...]]:
    archives = campaign.child("archives")
    archive = archives.child(run_id)

    def require_current(stage: str) -> None:
        if not campaign.is_current():
            raise RuntimeError("campaign root changed while verifying marker")
        if not archives.is_current():
            raise RuntimeError(f"archives directory changed while {stage}")
        if not archive.is_current():
            raise RuntimeError(f"archive changed while {stage}")

    initial_directory = os.lstat(archive.path)
    if file_identity(initial_directory) != archive.identity:
        raise RuntimeError("archive changed while opening")
    expected_names = sorted(
        (entry.file_name for entry in artifacts), key=lambda name: os.fsencode(name)
    )
    names = os.listdir(archive.path)
    if sorted(names, key=lambda name: os.fsencode(name)) != expected_names:
        raise ValueError("archive inventory does not match artifact manifest")
    captured = []
    for entry in artifacts:
        path = archive.path / entry.file_name
        visible = os.lstat(path)
        if not stat.S_ISREG(visible.st_mode) or platform.is_link(path):
            raise ValueError(f"archive artifact {entry.name!r} is not a regular file")
        descriptor = os.open(path, _READ_FLAGS)
        try:
            initial = os.fstat(descriptor)
            visible = os.lstat(path)
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
            digest = descriptor_sha256(descriptor)
            final = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        require_current("verifying")
        visible = os.lstat(path)
        if file_state(final) != file_state(initial) or file_state(visible) != file_state(
            initial
        ):
            raise RuntimeError(f"archive artifact {entry.name!r} changed while hashing")
        if digest != entry.sha256:
            raise ValueError(
                f"archive artifact {entry.name!r} hash does not match manifest"
            )
        captured.append(
            StableFile(
                campaign_root / "archives" / run_id / entry.file_name,
                file_state(initial),
                initial.st_size,
                digest,
            )
        )
    if (
        sorted(os.listdir(archive.path), key=lambda name: os.fsencode(name))
        != sorted(names, key=lambda name: os.fsencode(name))
        or file_state(os.lstat(archive.path)) != file_state(initial_directory)
    ):
        raise RuntimeError("archive changed while verifying")
    require_current("verifying")
    return (
        campaign_root / "archives" / run_id,
        file_state(initial_directory),
        tuple(captured),
    )


def completion_state(
    campaign_root: Path, expected: RunRecord
) -> CompletionState | None:
    """Validate and return a run completion marker, or ``None`` if absent."""

    path = run_path(campaign_root, expected.run_id)
    try:
        campaign = anchor_directory(campaign_root)
    except FileNotFoundError:
        return None
    except Exception as error:
        raise ValueError(f"invalid completion marker {path}: {error}") from error
    try:
        try:
            runs = campaign.child("runs")
        except FileNotFoundError:
            return None
        marker = runs.path / path.name
        try:
            visible = os.lstat(marker)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(visible.st_mode) or platform.is_link(marker):
            raise ValueError("not a regular non-symlink file")
        try:
            descriptor = os.open(marker, _READ_FLAGS)
        except FileNotFoundError:
            return None
        try:
            initial = os.fstat(descriptor)
            visible = os.lstat(marker)
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
                table = read_parquet(parquet)
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
            if version != b"2":
                raise ValueError(f"unsupported completion marker version: {version!r}")
            if manifest_payload is None:
                raise ValueError("v2 marker is missing its artifact manifest")
            expected_metadata = {COMPLETION_MARKER_VERSION, ARTIFACT_MANIFEST} | (
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
            archive, archive_state, captured_artifacts = _verify_archive(
                campaign_root, campaign, expected.run_id, artifacts
            )
            marker_sha256 = descriptor_sha256(descriptor)
            final = os.fstat(descriptor)
            if not campaign.is_current():
                raise RuntimeError("campaign root changed while verifying marker")
            if not runs.is_current():
                raise RuntimeError("runs directory changed while verifying marker")
            visible = os.lstat(marker)
            if file_state(final) != file_state(initial) or file_state(visible) != file_state(
                initial
            ):
                raise RuntimeError("completion marker changed while verifying")
            return CompletionState(
                duration,
                scratch_bytes,
                artifacts,
                evidence,
                StableFile(path, file_state(initial), initial.st_size, marker_sha256),
                archive,
                archive_state,
                captured_artifacts,
            )
        finally:
            os.close(descriptor)
    except Exception as error:
        raise ValueError(f"invalid completion marker {path}: {error}") from error


def completion_marker_names(campaign_root: Path) -> tuple[str, ...]:
    """List stable completion marker names while rejecting foreign content."""

    try:
        campaign = anchor_directory(campaign_root)
    except FileNotFoundError:
        return ()
    except Exception as error:
        raise ValueError(f"invalid completion marker directory: {error}") from error
    try:
        try:
            runs = campaign.child("runs")
        except FileNotFoundError:
            return ()
        initial = file_state(os.lstat(runs.path))
        if initial.identity != runs.identity:
            raise RuntimeError("runs directory changed while listing markers")
        names = tuple(os.listdir(runs.path))
        if (
            file_state(os.lstat(runs.path)) != initial
            or not runs.is_current()
            or not campaign.is_current()
        ):
            raise RuntimeError("runs directory changed while listing markers")
        return names
    except Exception as error:
        raise ValueError(f"invalid completion marker directory: {error}") from error


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written == 0:
            raise OSError("short write while staging artifact")
        view = view[written:]


def _copy_artifact(
    artifact: Artifact,
    destination_directory: DirectoryAnchor,
    interrupted: Callable[[], bool] | None,
) -> ArtifactManifestEntry:
    """Copy one artifact byte for byte into staging; handles closed on return."""

    name = archive_file_name(artifact.name, artifact.source_path)
    source = artifact.path
    destination = destination_directory.path / name
    source_descriptor = open_regular(source)
    destination_descriptor = -1
    created = False
    try:
        source_initial = os.fstat(source_descriptor)
        destination_descriptor = os.open(destination, _CREATE_FLAGS, 0o600)
        created = True
        while True:
            if interrupted is not None and interrupted():
                raise KeyboardInterrupt
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            _write_all(destination_descriptor, chunk)
        source_final = os.fstat(source_descriptor)
        source_visible = os.lstat(absolute(source))
        if (
            file_state(source_final) != file_state(source_initial)
            or file_state(source_visible) != file_state(source_initial)
            or not _regular_path_is_current(source, file_state(source_initial))
        ):
            raise RuntimeError(f"artifact changed while publishing: {source}")
        os.fsync(destination_descriptor)
        digest = descriptor_sha256(destination_descriptor, interrupted)
        final = os.fstat(destination_descriptor)
        os.close(destination_descriptor)
        destination_descriptor = -1
        visible = os.lstat(destination)
        if (
            not stat.S_ISREG(final.st_mode)
            or file_state(final) != file_state(visible)
            or not destination_directory.is_current()
        ):
            raise RuntimeError(f"staged artifact changed while publishing: {name}")
        if final.st_size != source_initial.st_size:
            raise RuntimeError(f"staged artifact is incomplete: {name}")
        return ArtifactManifestEntry(
            artifact.name, artifact.source_path, final.st_size, digest
        )
    except BaseException:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
            destination_descriptor = -1
        if created:
            try:
                platform.remove_file(destination)
            except FileNotFoundError:
                pass
        raise
    finally:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        os.close(source_descriptor)


def _remove_entry(path: Path) -> None:
    """Remove a staging, backup or archive entry without following links."""

    info = _entry_state(path)
    if info is None:
        return
    if stat.S_ISDIR(info.st_mode) and not platform.is_link(path):
        identity = file_identity(info)
        for entry in os.listdir(path):
            _remove_entry(path / entry)
        visible = os.lstat(path)
        if file_identity(visible) != identity or platform.is_link(path):
            raise RuntimeError(f"directory changed while removing: {path.name}")
        os.rmdir(path)
    else:
        platform.remove_file(path)


def _rename_conflicts(error: OSError, destination: Path) -> bool:
    """True when a rename failed because ``destination`` is occupied.

    POSIX reports EEXIST/ENOTEMPTY; Windows reports FileExistsError, or a
    PermissionError (after bounded retries) when a file inside the occupied
    destination is open.  A PermissionError with no destination is genuine.
    """

    if isinstance(error, FileExistsError) or error.errno in {
        errno.EEXIST,
        errno.ENOTEMPTY,
    }:
        return True
    return isinstance(error, PermissionError) and _entry_state(destination) is not None


class ArchiveTransaction:
    """Stage artifacts and commit archive plus marker as one guarded operation.

    No directory is held open: every managed directory is anchored by path and
    identity during :meth:`prepare` and rechecked before each publication step.
    Artifacts are staged in ``archives/.s<8 hex>``; an existing archive is moved
    aside to ``archives/.b<8 hex>`` before the staging directory takes its name,
    and is restored if the completion marker cannot be published.
    """

    def __init__(
        self,
        campaign_root: Path,
        run_id: str,
        artifacts: Sequence[Artifact],
        interrupted: Callable[[], bool] | None,
    ) -> None:
        run_path(campaign_root, run_id)
        sources = tuple(artifacts)
        if not sources:
            raise ValueError("manifest must contain at least one artifact")
        try:
            if any(
                not isinstance(artifact, Artifact)
                or not isinstance(artifact.path, Path)
                for artifact in sources
            ):
                raise ValueError
            file_names = [
                archive_file_name(artifact.name, artifact.source_path)
                for artifact in sources
            ]
        except ValueError as error:
            raise ValueError("archive transaction contains an invalid artifact") from error
        names = [artifact.name for artifact in sources]
        if len(names) != len(set(names)) or len(
            {name.casefold() for name in file_names}
        ) != len(file_names):
            raise ValueError("archive transaction contains duplicate artifacts")
        self.campaign_root = campaign_root
        self.run_id = run_id
        self.sources = tuple(
            sorted(sources, key=lambda item: item.name.encode("utf-8"))
        )
        self.interrupted = interrupted
        self.manifest: tuple[ArtifactManifestEntry, ...] = ()
        self._campaign: DirectoryAnchor | None = None
        self._archives: DirectoryAnchor | None = None
        self._runs: DirectoryAnchor | None = None
        self._technical: DirectoryAnchor | None = None
        self._marker_staging: DirectoryAnchor | None = None
        self._staging: DirectoryAnchor | None = None
        self._staging_path: Path | None = None
        self._staging_pending = False
        self._committed = False

    def prepare(self) -> None:
        absolute_path = absolute(self.campaign_root)
        if not absolute_path.name:
            raise ValueError("campaign root must have a final path component")
        # Links among the campaign's ancestors are accepted (spec 8.3): the
        # parent is resolved once; links are refused from the campaign down.
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        parent = anchor_directory(absolute_path.parent.resolve(strict=True))
        self._campaign = parent.child(absolute_path.name, create=True)
        self._archives = self._campaign.child("archives", create=True)
        self._runs = self._campaign.child("runs", create=True)
        self._technical = self._campaign.child(".eqasim-sa", create=True)
        self._marker_staging = self._technical.child("transactions", create=True)
        fsync_directory(self._technical.path)
        fsync_directory(self._campaign.path)
        while True:
            staging = self._archives.path / short_name("s")
            try:
                os.mkdir(staging, 0o700)
            except FileExistsError:
                continue
            break
        self._staging_pending = True
        self._staging_path = staging
        self._staging = self._archives.child(staging.name)
        if not self._publication_is_current():
            raise RuntimeError("campaign publication directories changed")
        self.manifest = tuple(
            _copy_artifact(artifact, self._staging, self.interrupted)
            for artifact in self.sources
        )
        fsync_directory(self._staging.path)

    def _publication_is_current(self) -> bool:
        anchors = (
            self._campaign,
            self._archives,
            self._runs,
            self._technical,
            self._marker_staging,
        )
        return all(anchor is not None and anchor.is_current() for anchor in anchors)

    def commit(
        self,
        record: RunRecord,
        marker_writer: Callable[..., Path] | None = None,
        execution_evidence: bytes | ExecutionEvidence | None = None,
    ) -> Path:
        if (
            self._committed
            or not self._staging_pending
            or self._staging is None
            or self._archives is None
            or self._runs is None
            or self._campaign is None
            or not self.manifest
        ):
            raise RuntimeError("archive transaction is not ready to commit")
        validate_run_record(record)
        validate_parameters(record.parameters)
        if record.run_id != self.run_id:
            raise ValueError("archive transaction run_id does not match RunRecord")
        evidence = _validated_execution_evidence(execution_evidence, record.context_hash)
        if not self._publication_is_current() or not self._staging.is_current():
            raise RuntimeError("campaign publication directories changed")
        marker_name = f"{self.run_id}.parquet"
        if _entry_state(self._runs.path / marker_name) is not None:
            raise FileExistsError(f"completion marker already exists: {marker_name}")
        if self.interrupted is not None and self.interrupted():
            raise KeyboardInterrupt
        archives = self._archives.path
        archive = archives / self.run_id
        staging = self._staging
        archive_backups: list[Path] = []
        try:
            while True:
                if _entry_state(archive) is not None:
                    backup = archives / short_name("b")
                    try:
                        platform.rename_with_retry(archive, backup)
                    except FileNotFoundError:
                        continue
                    archive_backups.append(backup)
                try:
                    platform.rename_with_retry(staging.path, archive)
                    break
                except OSError as error:
                    if not _rename_conflicts(error, archive):
                        raise
        except BaseException:
            if archive_backups and _entry_state(archive) is None:
                platform.rename_with_retry(archive_backups.pop(), archive)
                for backup in archive_backups:
                    _remove_entry(backup)
                fsync_directory(archives)
            raise
        self._staging_pending = False
        fsync_directory(archives)
        try:
            if not self._campaign.is_current() or not self._archives.is_current():
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
            current = _entry_state(archive)
            if current is not None and file_identity(current) == staging.identity:
                _remove_entry(archive)
                if archive_backups:
                    platform.rename_with_retry(archive_backups.pop(), archive)
                for backup in archive_backups:
                    _remove_entry(backup)
                archive_backups.clear()
                fsync_directory(archives)
            raise
        for backup in archive_backups:
            _remove_entry(backup)
        if archive_backups:
            fsync_directory(archives)
        self._committed = True
        return destination

    def close(self) -> None:
        staging = self._staging
        if self._staging_pending:
            if staging is not None:
                if staging.is_current():
                    _remove_entry(staging.path)
            elif self._staging_path is not None:
                # The private staging name was created but never anchored.
                _remove_entry(self._staging_path)
        self._staging_pending = False


@contextmanager
def archive_transaction(
    campaign_root: Path,
    run_id: str,
    artifacts: Sequence[Artifact],
    interrupted: Callable[[], bool] | None = None,
) -> Iterator[ArchiveTransaction]:
    """Prepare an archive transaction and guarantee staging cleanup."""

    transaction = ArchiveTransaction(campaign_root, run_id, artifacts, interrupted)
    try:
        transaction.prepare()
        yield transaction
    finally:
        transaction.close()


def validate_parameters(parameters: dict[str, Scalar]) -> None:
    """Validate scalar plan parameters accepted by completion records."""

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
    """Validate run identity, timing, context, parameters, and resource use."""

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
    artifact_manifest: Sequence[ArtifactManifestEntry],
    execution_evidence: bytes | ExecutionEvidence | None = None,
) -> Path:
    """Publish one immutable v2 completion marker bound to its archive."""

    validate_run_record(record)
    validate_parameters(record.parameters)
    evidence = _validated_execution_evidence(execution_evidence, record.context_hash)
    table = _run_table_with_metadata(record, artifact_manifest, evidence)
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
