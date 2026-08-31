import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from eqasim_sa.core import ledger
from eqasim_sa.core.indicators import ArtifactSet
from eqasim_sa.core.ledger import RunRecord, fsync_directory
from eqasim_sa.core.plan import MAX_PLAN_CELLS, MAX_PLAN_POSITIONS
from eqasim_sa.core.software_identity import (
    SoftwareIdentity,
    verify_software_source_states,
)
from eqasim_sa.ports.simulator import ExecutionEvidence


ANALYSIS_LAYOUT = "immutable-generation-v1"
ANALYSIS_STATE_DIRECTORY = ".eqasim-sa-analysis"
GENERATION_DOMAIN = b"eqasim-sa:analysis-generation:immutable-v1\0"
PROVENANCE_DOMAIN = b"eqasim-sa:analysis-provenance:v1\0"
RUN_SET_DOMAIN = b"eqasim-sa:analysis-run-set:v1\0"
SHA256 = re.compile(r"^[0-9a-f]{64}$")

OUTPUT_FILENAMES = frozenset(
    {
        "indicators.parquet",
        "replication-dispersion.parquet",
        "morris-ranking.parquet",
        "discrete-oat-effects.parquet",
        "canonical-morris-ranking.parquet",
        "canonical-morris-convergence.parquet",
        "canonical-morris-rank-reversals.parquet",
        "noise-floor.parquet",
        "replication-allocation.json",
        "sobol-indices.parquet",
        "multiscramble-sobol-indices.parquet",
        "multiscramble-sobol-by-scramble.parquet",
        "multiscramble-sobol-convergence.parquet",
        "multiscramble-sobol-diagnostics.parquet",
        "output-summary.parquet",
        "convergence-evidence.parquet",
        "governed-report.json",
        "captured-run-set.json",
    }
)
LEGACY_FLAT_OUTPUTS = tuple(sorted(OUTPUT_FILENAMES))


class AnalysisManifestOutcomeUnknown(RuntimeError):
    """The current analysis pointer could not be durably committed or rolled back."""


@dataclass(frozen=True)
class PlanCapture:
    path: Path
    table: pa.Table
    sha256: str
    context_descriptor: dict | None


@dataclass(frozen=True)
class RunCapture:
    expected_ids: frozenset[str]
    completed_ids: frozenset[str]
    artifacts: Mapping[str, ArtifactSet]
    run_set_sha256: str
    run_set_document: Mapping[str, object]
    scientific_execution_evidence_sha256: str | None


@dataclass(frozen=True)
class PublishedGeneration:
    generation_id: str
    path: Path
    paths: Mapping[str, Path]
    manifest: Mapping[str, object]


@dataclass(frozen=True)
class _FileSnapshot:
    path: Path
    state: tuple[int, int, int, int, int, int]
    size: int
    sha256: str


@dataclass(frozen=True)
class PlanInspection:
    path: Path
    table: pa.Table
    sha256: str
    context_descriptor: dict | None
    _source: _FileSnapshot


@dataclass(frozen=True)
class RunInspection:
    expected_ids: frozenset[str]
    completed_ids: frozenset[str]
    execution_evidence: Mapping[str, ExecutionEvidence | None]


@dataclass(frozen=True)
class _SelectedRun:
    expected: RunRecord
    marker: _FileSnapshot
    archive: Path
    archive_state: tuple[int, int, int, int, int, int]
    artifacts: tuple[_FileSnapshot, ...]
    descriptor: dict
    execution_evidence: ExecutionEvidence | None


def _state(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _domain_digest(domain: bytes, value: object) -> str:
    return hashlib.sha256(domain + _canonical(value)).hexdigest()


def _write_all(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short write while staging analysis content")
        remaining = remaining[written:]


def _secure_directory(path: Path, *, create: bool) -> Path:
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise ValueError(f"analysis state must use a regular non-symlink directory: {path}")
    return path


def _copy_stable_file(
    source: Path,
    destination: Path,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> _FileSnapshot:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    source_descriptor = os.open(source, flags)
    destination_descriptor = -1
    try:
        initial = os.fstat(source_descriptor)
        visible = source.lstat()
        if (
            not stat.S_ISREG(initial.st_mode)
            or not stat.S_ISREG(visible.st_mode)
            or _state(initial) != _state(visible)
        ):
            raise ValueError(f"analysis source is not a stable regular file: {source}")
        destination_descriptor = os.open(
            destination,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
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
            _write_all(destination_descriptor, chunk)
        os.fchmod(destination_descriptor, 0o400)
        os.fsync(destination_descriptor)
        final = os.fstat(source_descriptor)
        visible = source.lstat()
        if _state(final) != _state(initial) or _state(visible) != _state(initial):
            raise RuntimeError(f"analysis source changed while being captured: {source}")
        actual_digest = digest.hexdigest()
        if expected_size is not None and size != expected_size:
            raise ValueError(f"analysis source size does not match its v2 marker: {source}")
        if expected_sha256 is not None and actual_digest != expected_sha256:
            raise ValueError(f"analysis source hash does not match its v2 marker: {source}")
        return _FileSnapshot(source, _state(initial), size, actual_digest)
    finally:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        os.close(source_descriptor)


def _hash_stable_file(path: Path) -> _FileSnapshot:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        initial = os.fstat(descriptor)
        visible = path.lstat()
        if (
            not stat.S_ISREG(initial.st_mode)
            or not stat.S_ISREG(visible.st_mode)
            or _state(initial) != _state(visible)
        ):
            raise ValueError(f"analysis source is not a stable regular file: {path}")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        final = os.fstat(descriptor)
        visible = path.lstat()
        if _state(final) != _state(initial) or _state(visible) != _state(initial):
            raise RuntimeError(f"analysis source changed while being hashed: {path}")
        return _FileSnapshot(path, _state(initial), initial.st_size, digest.hexdigest())
    finally:
        os.close(descriptor)


def _verify_file(snapshot: _FileSnapshot) -> None:
    current = _hash_stable_file(snapshot.path)
    if (
        current.state != snapshot.state
        or current.size != snapshot.size
        or current.sha256 != snapshot.sha256
    ):
        raise RuntimeError(f"selected analysis source changed: {snapshot.path}")


def _directory_state(path: Path) -> tuple[int, int, int, int, int, int]:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise ValueError(f"analysis archive is not a regular directory: {path}")
    return _state(info)


def _context_descriptor(table: pa.Table) -> dict | None:
    payload = (table.schema.metadata or {}).get(b"contextDescriptor")
    if payload is None:
        return None
    try:
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError
        _canonical(value)
        return value
    except (TypeError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("plan contextDescriptor is invalid") from error


def _expected_records(table: pa.Table, campaign: str) -> dict[str, RunRecord]:
    if type(campaign) is not str or not campaign:
        raise ValueError("campaign must be a non-empty string")
    required = {"designId", "method", "role", "runId", "seed", "contextHash"}
    if not required <= set(table.column_names) or any(
        column.null_count for column in table.columns
    ):
        raise ValueError("plan schema or null values are invalid")
    parameters = tuple(name for name in table.column_names if name not in required)
    expected: dict[str, RunRecord] = {}
    for row in table.to_pylist():
        run_id = row["runId"]
        if type(run_id) is not str or ledger.RUN_ID.fullmatch(run_id) is None:
            raise ValueError("plan contains an invalid runId")
        record = RunRecord(
            run_id,
            campaign,
            row["seed"],
            row["contextHash"],
            "",
            0.0,
            {name: row[name] for name in parameters},
        )
        previous = expected.setdefault(run_id, record)
        if previous != record:
            raise ValueError(f"plan has inconsistent duplicate runId {run_id}")
    return expected


def _completion_descriptor(run_id: str, completion) -> dict:  # type: ignore[no-untyped-def]
    evidence = completion.execution_evidence
    evidence_bytes = evidence.canonical_bytes() if evidence is not None else None
    return {
        "runId": run_id,
        "archiveIntegrity": completion.archive_integrity,
        "artifacts": [
            {"name": item.name, "size": item.size, "sha256": item.sha256}
            for item in completion.artifacts
        ],
        "executionEvidenceSha256": (
            hashlib.sha256(evidence_bytes).hexdigest() if evidence_bytes is not None else None
        ),
        "executionEvidence": (
            json.loads(evidence_bytes) if evidence_bytes is not None else None
        ),
    }


def validate_scientific_execution_evidence(
    table: pa.Table,
    campaign: str,
    evidence_by_run: Mapping[str, ExecutionEvidence | None],
    scientific_contract: dict,
    *,
    allow_empty: bool = False,
) -> str | None:
    expected = _expected_records(table, campaign)
    if set(evidence_by_run) - set(expected):
        raise ValueError("scientific execution evidence names an unexpected run")
    if not evidence_by_run or all(
        evidence is None for evidence in evidence_by_run.values()
    ):
        if allow_empty:
            return None
        raise ValueError("scientific analysis requires selected runs with execution evidence")
    try:
        software_sha256 = scientific_contract["software"]["eqasimSaSha256"]
        descriptor = _context_descriptor(table)
        model_commit = descriptor["eqasimCommit"]
    except (KeyError, TypeError) as error:
        raise ValueError("scientific contract or plan context is incomplete") from error
    common: bytes | None = None
    for run_id in sorted(evidence_by_run, key=os.fsencode):
        evidence = evidence_by_run[run_id]
        if evidence is None or evidence.schema_version != 2:
            raise ValueError(
                f"scientific run {run_id} requires non-null schema-v2 execution evidence"
            )
        if evidence.context_hash != expected[run_id].context_hash:
            raise ValueError(
                f"scientific run {run_id} execution evidence contextHash does not match plan"
            )
        if evidence.eqasim_sa_sha256 != software_sha256:
            raise ValueError(
                f"scientific run {run_id} execution evidence software digest does not match plan"
            )
        if evidence.model_commit != model_commit:
            raise ValueError(
                f"scientific run {run_id} execution evidence model commit does not match plan"
            )
        payload = evidence.canonical_bytes()
        if common is None:
            common = payload
        elif payload != common:
            raise ValueError(
                "scientific selected runs do not have exactly equivalent execution evidence"
            )
    if common is None:
        raise ValueError("scientific analysis requires execution evidence")
    return hashlib.sha256(common).hexdigest()


def validate_descriptive_execution_evidence(
    evidence_by_run: Mapping[str, ExecutionEvidence | None],
) -> str | None:
    if not evidence_by_run or all(
        evidence is None for evidence in evidence_by_run.values()
    ):
        return None
    if any(evidence is None for evidence in evidence_by_run.values()):
        raise ValueError(
            "descriptive selected runs must be uniformly unattested or uniformly attested"
        )
    payloads = {
        evidence.canonical_bytes()
        for evidence in evidence_by_run.values()
        if evidence is not None
    }
    if len(payloads) != 1:
        raise ValueError(
            "descriptive selected runs do not have exactly equivalent execution evidence"
        )
    return hashlib.sha256(next(iter(payloads))).hexdigest()


def dependency_versions(software_identity: SoftwareIdentity) -> dict[str, str]:
    versions = {"python": platform.python_version()}
    for distribution in ("eqasim-sa", "numpy", "pyarrow", "SALib", "scipy"):
        versions[distribution] = importlib.metadata.version(distribution)
    versions["eqasim-sa-source"] = software_identity.sha256
    return {name: versions[name] for name in sorted(versions)}


def _capture_marker_names(campaign_root: Path) -> tuple[str, ...]:
    for _ in range(32):
        try:
            return ledger._run_marker_names(campaign_root)
        except ValueError as error:
            cause = error.__cause__
            if not (
                isinstance(cause, RuntimeError)
                and "changed while listing markers" in str(cause)
            ):
                raise
    raise RuntimeError("could not capture a stable completion marker set")


def inspect_plan(plan: Path) -> PlanInspection:
    """Read and verify a plan without creating analysis publication state."""
    source = _hash_stable_file(plan)
    parquet = pq.ParquetFile(plan)
    rows = parquet.metadata.num_rows
    columns = len(parquet.schema_arrow)
    if rows > MAX_PLAN_POSITIONS or rows * columns > MAX_PLAN_CELLS:
        raise ValueError("plan exceeds the operational allocation cap")
    table = parquet.read()
    _verify_file(source)
    return PlanInspection(
        plan,
        table,
        source.sha256,
        _context_descriptor(table),
        source,
    )


def inspect_runs(
    campaign_root: Path,
    plan: PlanInspection,
    campaign: str,
    *,
    allow_legacy_unbound: bool = False,
) -> RunInspection:
    """Inspect one stable completion-marker cut without copying its archives."""
    expected = _expected_records(plan.table, campaign)
    for _ in range(32):
        entries = _capture_marker_names(campaign_root)
        foreign = [
            name
            for name in entries
            if not name.endswith(".parquet")
            or name.removesuffix(".parquet") not in expected
        ]
        if foreign:
            raise ValueError(f"invalid completion marker names: {sorted(foreign)}")
        completed = []
        evidence_by_run = {}
        unstable = False
        for marker_name in sorted(entries, key=os.fsencode):
            run_id = marker_name.removesuffix(".parquet")
            completion = ledger._completion_state(campaign_root, expected[run_id])
            if completion is None:
                unstable = True
                break
            if completion.archive_integrity != ledger.VERIFIED and not (
                allow_legacy_unbound
                and completion.archive_integrity == ledger.LEGACY_UNBOUND
            ):
                raise ValueError(
                    f"run {run_id} requires a verified v2 completion marker for analysis"
                )
            completed.append(run_id)
            evidence_by_run[run_id] = completion.execution_evidence
        if unstable or _capture_marker_names(campaign_root) != entries:
            continue
        _verify_file(plan._source)
        return RunInspection(
            frozenset(expected),
            frozenset(completed),
            MappingProxyType(evidence_by_run),
        )
    raise RuntimeError("could not inspect a stable completion marker set")


class AnalysisPublication:
    def __init__(self, campaign_root: Path) -> None:
        self.campaign_root = campaign_root
        self.state = _secure_directory(
            campaign_root / ANALYSIS_STATE_DIRECTORY, create=True
        )
        fsync_directory(campaign_root)
        self.staging = _secure_directory(self.state / "staging", create=True)
        self.generations = _secure_directory(self.state / "generations", create=True)
        fsync_directory(self.state)
        self.work = self.staging / uuid.uuid4().hex
        self.work.mkdir(mode=0o700)
        self.capture_directory = self.work / "capture"
        self.generation_staging = self.work / "generation"
        self.capture_directory.mkdir(mode=0o700)
        self.generation_staging.mkdir(mode=0o700)
        fsync_directory(self.staging)
        self._plan_source: _FileSnapshot | None = None
        self._selected: tuple[_SelectedRun, ...] = ()
        self._published = False

    def __enter__(self) -> "AnalysisPublication":
        return self

    def __exit__(self, *exc_info) -> None:  # type: ignore[no-untyped-def]
        if self.work.exists() and not self.work.is_symlink():
            shutil.rmtree(self.work)
            fsync_directory(self.staging)

    def capture_plan(
        self, plan: Path, expected: PlanInspection | None = None
    ) -> PlanCapture:
        destination = self.capture_directory / "plan.parquet"
        if expected is not None:
            _verify_file(expected._source)
        self._plan_source = _copy_stable_file(
            plan,
            destination,
            expected_size=expected._source.size if expected is not None else None,
            expected_sha256=expected.sha256 if expected is not None else None,
        )
        table = pq.read_table(destination)
        return PlanCapture(
            destination,
            table,
            self._plan_source.sha256,
            _context_descriptor(table),
        )

    def capture_runs(
        self,
        table: pa.Table,
        campaign: str,
        scientific_contract: dict | None = None,
        *,
        allow_empty_scientific_evidence: bool = False,
        allow_legacy_unbound: bool = False,
    ) -> RunCapture:
        expected = _expected_records(table, campaign)
        entries = _capture_marker_names(self.campaign_root)
        foreign = [
            name
            for name in entries
            if not name.endswith(".parquet")
            or name.removesuffix(".parquet") not in expected
        ]
        if foreign:
            raise ValueError(f"invalid completion marker names: {sorted(foreign)}")
        artifacts_by_run: dict[str, ArtifactSet] = {}
        selected = []
        descriptors = []
        for marker_name in sorted(entries, key=os.fsencode):
            run_id = marker_name.removesuffix(".parquet")
            record = expected[run_id]
            completion = ledger._completion_state(self.campaign_root, record)
            if completion is None:
                raise RuntimeError("captured completion marker disappeared")
            if completion.archive_integrity != ledger.VERIFIED and not (
                allow_legacy_unbound
                and completion.archive_integrity == ledger.LEGACY_UNBOUND
            ):
                raise ValueError(
                    f"run {run_id} requires a verified v2 completion marker for analysis"
                )
            descriptor = _completion_descriptor(run_id, completion)
            marker = _hash_stable_file(
                self.campaign_root / "runs" / f"{run_id}.parquet"
            )
            archive = self.campaign_root / "archives" / run_id
            archive_state = _directory_state(archive)
            destination = self.capture_directory / "artifacts" / run_id
            destination.mkdir(parents=True, mode=0o700)
            copied_paths = {}
            source_snapshots = []
            if completion.archive_integrity == ledger.VERIFIED:
                artifact_sources = tuple(
                    (archive / item.name, item.size, item.sha256)
                    for item in completion.artifacts
                )
            else:
                entries = tuple(
                    sorted(archive.iterdir(), key=lambda path: os.fsencode(path.name))
                )
                if not entries or any(
                    entry.suffix != ".parquet"
                    or entry.is_symlink()
                    or not entry.is_file()
                    for entry in entries
                ):
                    raise ValueError(
                        f"legacy archive for run {run_id} must contain only parquet artifacts"
                    )
                if len({entry.stem for entry in entries}) != len(entries):
                    raise ValueError(
                        f"legacy archive for run {run_id} has duplicate artifact names"
                    )
                artifact_sources = tuple((entry, None, None) for entry in entries)
            captured_artifacts = []
            for source, expected_size, expected_sha256 in artifact_sources:
                target = destination / source.name
                snapshot = _copy_stable_file(
                    source,
                    target,
                    expected_size=expected_size,
                    expected_sha256=expected_sha256,
                )
                source_snapshots.append(snapshot)
                copied_paths[source.stem] = target
                captured_artifacts.append(
                    {
                        "name": source.name,
                        "size": snapshot.size,
                        "sha256": snapshot.sha256,
                    }
                )
            fsync_directory(destination)
            current = ledger._completion_state(self.campaign_root, record)
            if (
                current is None
                or _completion_descriptor(run_id, current) != descriptor
                or _directory_state(archive) != archive_state
            ):
                raise RuntimeError(f"selected run {run_id} changed while being captured")
            _verify_file(marker)
            artifacts_by_run[run_id] = ArtifactSet(copied_paths)
            captured_descriptor = {**descriptor, "artifacts": captured_artifacts}
            selected.append(
                _SelectedRun(
                    record,
                    marker,
                    archive,
                    archive_state,
                    tuple(source_snapshots),
                    descriptor,
                    completion.execution_evidence,
                )
            )
            descriptors.append(
                {
                    "runId": run_id,
                    "campaign": record.campaign,
                    "seed": record.seed,
                    "contextHash": record.context_hash,
                    "parameters": record.parameters,
                    "markerSha256": marker.sha256,
                    "archiveIntegrity": captured_descriptor["archiveIntegrity"],
                    "artifacts": captured_descriptor["artifacts"],
                    "executionEvidenceSha256": captured_descriptor[
                        "executionEvidenceSha256"
                    ],
                    "executionEvidence": captured_descriptor["executionEvidence"],
                }
            )
        self._selected = tuple(selected)
        run_set_descriptor = {"schemaVersion": 1, "runs": descriptors}
        run_set_sha256 = _domain_digest(RUN_SET_DOMAIN, run_set_descriptor)
        run_set_document = {
            **run_set_descriptor,
            "capturedRunSetSha256": run_set_sha256,
        }
        if scientific_contract is not None:
            scientific_evidence_sha256 = validate_scientific_execution_evidence(
                table,
                campaign,
                {
                    item.expected.run_id: item.execution_evidence
                    for item in selected
                },
                scientific_contract,
                allow_empty=allow_empty_scientific_evidence,
            )
        else:
            scientific_evidence_sha256 = None
            if allow_empty_scientific_evidence:
                validate_descriptive_execution_evidence(
                    {
                        item.expected.run_id: item.execution_evidence
                        for item in selected
                    }
                )
        return RunCapture(
            frozenset(expected),
            frozenset(item.expected.run_id for item in selected),
            MappingProxyType(artifacts_by_run),
            run_set_sha256,
            MappingProxyType(run_set_document),
            scientific_evidence_sha256,
        )

    def stage_path(self, filename: str) -> Path:
        if filename not in OUTPUT_FILENAMES:
            raise ValueError(f"unknown analysis output filename: {filename}")
        return self.generation_staging / filename

    def verify_sources(self) -> None:
        try:
            if self._plan_source is None:
                raise RuntimeError("analysis plan was not captured")
            _verify_file(self._plan_source)
            for selected in self._selected:
                _verify_file(selected.marker)
                if _directory_state(selected.archive) != selected.archive_state:
                    raise RuntimeError(
                        f"selected analysis archive changed: {selected.archive}"
                    )
                for artifact in selected.artifacts:
                    _verify_file(artifact)
                completion = ledger._completion_state(
                    self.campaign_root, selected.expected
                )
                if (
                    completion is None
                    or completion.archive_integrity
                    != selected.descriptor["archiveIntegrity"]
                    or _completion_descriptor(selected.expected.run_id, completion)
                    != selected.descriptor
                ):
                    raise RuntimeError(
                        f"selected run {selected.expected.run_id} changed"
                    )
        except RuntimeError:
            raise
        except Exception as error:
            raise RuntimeError("selected analysis sources changed during analysis") from error

    def publish(
        self,
        outputs: Mapping[str, Path],
        summary: Mapping[str, object],
        *,
        meta_config_sha256: str | None,
        analysis_options: Mapping[str, object],
        numerical_engine: Mapping[str, object] | None,
        plan: PlanCapture,
        runs: RunCapture,
        indicator_identities: Sequence[Mapping[str, object]],
        software_identity: SoftwareIdentity,
        indicator_guard: Callable[[], None] | None = None,
    ) -> PublishedGeneration:
        if self._published:
            raise RuntimeError("analysis publication may only be committed once")
        if meta_config_sha256 is not None and (
            type(meta_config_sha256) is not str
            or SHA256.fullmatch(meta_config_sha256) is None
        ):
            raise ValueError("meta_config_sha256 must be a lowercase SHA-256 digest")
        if not outputs or "indicators" not in outputs:
            raise ValueError("analysis publication requires indicators output")
        verify_software_source_states(software_identity)
        captured_run_set = self.generation_staging / "captured-run-set.json"
        descriptor = os.open(
            captured_run_set,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o400,
        )
        try:
            _write_all(descriptor, _canonical(dict(runs.run_set_document)))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        publication_outputs = {**outputs, "capturedRunSet": captured_run_set}
        if len(set(publication_outputs.values())) != len(publication_outputs):
            raise ValueError("analysis output paths must be unique")
        staged_records = {}
        for name, path in publication_outputs.items():
            if path.parent != self.generation_staging or path.name not in OUTPUT_FILENAMES:
                raise ValueError(f"analysis output escaped generation staging: {path}")
            snapshot = _hash_stable_file(path)
            if path.lstat().st_nlink != 1:
                raise ValueError(f"analysis output must not be hard-linked: {path}")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            staged_records[name] = {
                "name": path.name,
                "size": snapshot.size,
                "sha256": snapshot.sha256,
            }
        inventory = {entry.name for entry in self.generation_staging.iterdir()}
        expected_inventory = {record["name"] for record in staged_records.values()}
        if inventory != expected_inventory:
            raise ValueError("analysis generation staging contains unexpected content")
        provenance = {
            "metaConfigSha256": meta_config_sha256,
            "analysisOptions": dict(analysis_options),
            "numericalEngine": (
                dict(numerical_engine) if numerical_engine is not None else None
            ),
            "plan": {
                "sha256": plan.sha256,
                "contextDescriptor": plan.context_descriptor,
            },
            "capturedRunSetSha256": runs.run_set_sha256,
            "capturedRunSet": {
                "output": "capturedRunSet",
                "sha256": staged_records["capturedRunSet"]["sha256"],
            },
            "scientificExecutionEvidenceSha256": (
                runs.scientific_execution_evidence_sha256
            ),
            "dependencies": dependency_versions(software_identity),
            "indicators": [dict(identity) for identity in indicator_identities],
        }
        provenance_sha256 = _domain_digest(PROVENANCE_DOMAIN, provenance)
        generation_id = _domain_digest(
            GENERATION_DOMAIN,
            {
                "layout": ANALYSIS_LAYOUT,
                "outputs": staged_records,
                "provenanceSha256": provenance_sha256,
            },
        )
        generation_relative = (
            Path(ANALYSIS_STATE_DIRECTORY) / "generations" / generation_id
        )
        final = self.campaign_root / generation_relative
        output_records = {
            name: {
                "path": (generation_relative / record["name"]).as_posix(),
                "size": record["size"],
                "sha256": record["sha256"],
            }
            for name, record in staged_records.items()
        }
        manifest = {
            **dict(summary),
            "version": 7,
            "layout": ANALYSIS_LAYOUT,
            "generationId": generation_id,
            "generationPath": generation_relative.as_posix(),
            "outputs": output_records,
            "provenance": provenance,
            "provenanceSha256": provenance_sha256,
            **_legacy_output_fields(output_records),
        }
        encoded_manifest = _canonical(manifest)
        fsync_directory(self.generation_staging)
        if indicator_guard is not None:
            indicator_guard()
        verify_software_source_states(software_identity)
        self.verify_sources()
        if final.exists() or final.is_symlink():
            _validate_existing_generation(final, output_records)
        else:
            self.generation_staging.rename(final)
            fsync_directory(self.generations)
        self.verify_sources()
        if indicator_guard is not None:
            indicator_guard()
        verify_software_source_states(software_identity)
        _validate_existing_generation(final, output_records)
        if indicator_guard is not None:
            indicator_guard()
        _commit_manifest(self.campaign_root, self.staging, encoded_manifest)
        self._published = True
        try:
            _clean_legacy_outputs(self.campaign_root)
        except Exception:
            pass
        final_paths = MappingProxyType(
            {
                name: self.campaign_root / record["path"]
                for name, record in output_records.items()
            }
        )
        return PublishedGeneration(
            generation_id,
            final,
            final_paths,
            MappingProxyType(manifest),
        )


def _legacy_output_fields(outputs: Mapping[str, Mapping[str, object]]) -> dict:
    def path(name: str) -> str | None:
        record = outputs.get(name)
        return None if record is None else str(record["path"])

    def digest(name: str) -> str | None:
        record = outputs.get(name)
        return None if record is None else str(record["sha256"])

    fields = {
        "indicatorsPath": path("indicators"),
        "indicatorsSha256": digest("indicators"),
        "dispersionPath": path("dispersion"),
        "dispersionSha256": digest("dispersion"),
        "sensitivityPath": path("sensitivity"),
        "sensitivitySha256": digest("sensitivity"),
        "outputSummaryPath": path("outputSummary"),
        "outputSummarySha256": digest("outputSummary"),
        "convergenceEvidencePath": path("convergenceEvidence"),
        "convergenceEvidenceSha256": digest("convergenceEvidence"),
        "governedReportPath": path("governedReport"),
        "governedReportSha256": digest("governedReport"),
    }
    aliases = {
        "ranking": "sensitivity",
        "convergence": "convergence",
        "rankReversals": "rankReversals",
        "noiseFloor": "noiseFloor",
        "replicationAllocation": "replicationAllocation",
        "byScramble": "byScramble",
        "diagnostics": "diagnostics",
    }
    for prefix, name in aliases.items():
        fields[f"{prefix}Path"] = path(name)
        fields[f"{prefix}Sha256"] = digest(name)
    return fields


def _validate_existing_generation(
    directory: Path, outputs: Mapping[str, Mapping[str, object]]
) -> None:
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or directory.is_symlink():
        raise ValueError("content-addressed analysis generation is not a directory")
    expected = {Path(str(record["path"])).name for record in outputs.values()}
    entries = tuple(directory.iterdir())
    if {entry.name for entry in entries} != expected:
        raise ValueError("content-addressed analysis generation inventory is invalid")
    by_name = {Path(str(record["path"])).name: record for record in outputs.values()}
    for entry in entries:
        snapshot = _hash_stable_file(entry)
        record = by_name[entry.name]
        if (
            entry.lstat().st_nlink != 1
            or snapshot.size != record["size"]
            or snapshot.sha256 != record["sha256"]
        ):
            raise ValueError("content-addressed analysis generation is corrupted")


def _commit_manifest(campaign_root: Path, staging: Path, content: bytes) -> None:
    temporary = staging / f".analysis-generation-{uuid.uuid4().hex}.tmp"
    backup = staging / f".analysis-generation-{uuid.uuid4().hex}.backup"
    descriptor = os.open(
        temporary,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        _write_all(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    destination = campaign_root / "analysis-generation.json"
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
                    "analysis-generation.json must be a regular non-symlink file"
                )
            os.link(destination, backup, follow_symlinks=False)
            backup_created = True
            held = backup.lstat()
            if (held.st_dev, held.st_ino) != (previous.st_dev, previous.st_ino):
                raise RuntimeError("analysis manifest backup changed while creating it")
        os.replace(temporary, destination)
        replacement = destination.lstat()
        if not stat.S_ISREG(replacement.st_mode) or destination.is_symlink():
            raise RuntimeError("analysis manifest replacement is not a regular file")
        try:
            fsync_directory(staging)
            fsync_directory(campaign_root)
        except BaseException as commit_error:
            try:
                current = destination.lstat()
                if (current.st_dev, current.st_ino) != (
                    replacement.st_dev,
                    replacement.st_ino,
                ):
                    raise RuntimeError(
                        "analysis manifest changed before commit rollback"
                    )
                if previous is None:
                    destination.unlink()
                    if destination.exists() or destination.is_symlink():
                        raise RuntimeError("new analysis manifest survived rollback")
                else:
                    os.replace(backup, destination)
                    backup_created = False
                    restored = destination.lstat()
                    if (restored.st_dev, restored.st_ino) != (
                        previous.st_dev,
                        previous.st_ino,
                    ):
                        raise RuntimeError(
                            "previous analysis manifest was not restored"
                        )
                fsync_directory(campaign_root)
                fsync_directory(staging)
            except BaseException as rollback_error:
                preserve_backup = backup_created
                raise AnalysisManifestOutcomeUnknown(
                    "analysis manifest commit failed and rollback could not be established"
                ) from rollback_error
            raise commit_error
        committed = True
    finally:
        temporary.unlink(missing_ok=True)
        if backup_created and not preserve_backup:
            try:
                backup.unlink()
                if committed:
                    fsync_directory(staging)
            except OSError:
                if not committed:
                    raise


def _clean_legacy_outputs(campaign_root: Path) -> None:
    changed = False
    for filename in LEGACY_FLAT_OUTPUTS:
        path = campaign_root / filename
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            path.unlink()
            changed = True
    if changed:
        fsync_directory(campaign_root)
