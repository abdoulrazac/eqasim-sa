"""Stable campaign cuts shared by execution, estimation, and analysis."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from eqasim_sa.utils.filesystem import (
    reject_links_below,
    FileState,
    StableFile,
    capture_stable_file,
    file_state,
    fsync_directory,
    stable_file,
    verify_stable_file,
)
from eqasim_sa.core.indicators import ArtifactSet
from eqasim_sa.core.ledger import (
    LEGACY_UNBOUND,
    RUN_ID,
    VERIFIED,
    CompletionState,
    RunRecord,
    completion_marker_names,
    completion_state,
)
from eqasim_sa.ports.simulator import ExecutionEvidence


RUN_SET_DOMAIN = b"eqasim-sa:analysis-run-set:v1\0"


@dataclass(frozen=True)
class RunSnapshot:
    """Stable completion marker, archive, and artifacts for one selected run."""

    expected: RunRecord
    completion: CompletionState
    marker: StableFile
    archive: Path
    archive_state: FileState
    artifacts: tuple[StableFile, ...]
    descriptor: Mapping[str, object]

    @property
    def durable_bytes(self) -> int:
        return self.marker.size + sum(artifact.size for artifact in self.artifacts)


@dataclass(frozen=True)
class MaterializedCampaign:
    """Read-only copy of selected campaign inputs used during analysis."""

    plan_path: Path
    artifacts: Mapping[str, ArtifactSet]
    run_set_sha256: str
    run_set_document: Mapping[str, object]


@dataclass(frozen=True)
class PlanSnapshot:
    """Stable plan file, decoded table, and optional context descriptor."""

    source: StableFile
    table: pa.Table
    context_descriptor: dict | None


@dataclass(frozen=True)
class CampaignSnapshot:
    """Coherent immutable view of a plan and its completed run archives."""

    campaign_root: Path
    campaign: str
    plan_source: StableFile
    plan_table: pa.Table
    plan_sha256: str
    context_descriptor: dict | None
    expected: Mapping[str, RunRecord]
    runs: Mapping[str, RunSnapshot]

    @classmethod
    def inspect_plan(cls, plan: Path) -> PlanSnapshot:
        from eqasim_sa.core.plan import MAX_PLAN_CELLS, MAX_PLAN_POSITIONS

        source = stable_file(plan)
        parquet = pq.ParquetFile(plan)
        rows = parquet.metadata.num_rows
        columns = len(parquet.schema_arrow)
        if rows > MAX_PLAN_POSITIONS or rows * columns > MAX_PLAN_CELLS:
            raise ValueError("plan exceeds the operational allocation cap")
        table = parquet.read()
        _verify_stable_state(source)
        return PlanSnapshot(source, table, context_descriptor(table))

    @classmethod
    def capture(
        cls,
        campaign_root: Path,
        plan: Path,
        campaign: str,
        *,
        allow_legacy_unbound: bool = False,
        inspected_plan: PlanSnapshot | None = None,
    ) -> "CampaignSnapshot":
        inspected = inspected_plan or cls.inspect_plan(plan)
        if inspected.source.path != plan:
            raise ValueError("inspected plan belongs to another path")
        _verify_stable_state(inspected.source)
        source = inspected.source
        table = inspected.table
        expected = expected_records(table, campaign)
        entries = _stable_marker_names(campaign_root)
        foreign = [
            name
            for name in entries
            if not name.endswith(".parquet")
            or name.removesuffix(".parquet") not in expected
        ]
        if foreign:
            raise ValueError(f"invalid completion marker names: {sorted(foreign)}")
        selected = {}
        for marker_name in sorted(entries, key=os.fsencode):
            run_id = marker_name.removesuffix(".parquet")
            record = expected[run_id]
            completion = completion_state(campaign_root, record)
            if completion is None:
                raise RuntimeError("captured completion marker disappeared")
            if completion.archive_integrity != VERIFIED and not (
                allow_legacy_unbound
                and completion.archive_integrity == LEGACY_UNBOUND
            ):
                raise ValueError(
                    f"run {run_id} requires a verified v2 completion marker for analysis"
                )
            marker = completion.marker
            if completion.archive_integrity == VERIFIED:
                if completion.archive is None or completion.archive_state is None:
                    raise RuntimeError("verified completion did not capture its archive")
                archive = completion.archive
                archive_state = completion.archive_state
                artifacts = completion.captured_artifacts
            else:
                archive = campaign_root / "archives" / run_id
                archive_state = directory_state(archive)
                artifacts_list = []
                for path, _, _ in _artifact_sources(archive, completion):
                    try:
                        reject_links_below(campaign_root, path, "archive artifact")
                        captured = stable_file(path)
                        pq.ParquetFile(path).metadata
                        _verify_stable_state(captured)
                    except Exception as error:
                        raise ValueError(
                            f"invalid archive for {run_id}: {error}"
                        ) from error
                    artifacts_list.append(captured)
                artifacts = tuple(artifacts_list)
            descriptor = completion_descriptor(run_id, completion)
            if (
                directory_state(archive) != archive_state
                or not _stable_state_is_current(marker)
                or any(
                    not _stable_state_is_current(artifact) for artifact in artifacts
                )
            ):
                raise RuntimeError(f"selected run {run_id} changed while being captured")
            selected[run_id] = RunSnapshot(
                record,
                completion,
                marker,
                archive,
                archive_state,
                artifacts,
                MappingProxyType(descriptor),
            )
        _verify_stable_state(source)
        return cls(
            campaign_root,
            campaign,
            source,
            table,
            source.sha256,
            inspected.context_descriptor,
            MappingProxyType(expected),
            MappingProxyType(selected),
        )

    @classmethod
    def completed_specs(cls, campaign_root: Path, specs: Sequence[object]) -> frozenset[str]:
        from eqasim_sa.core.run import compute_run_id

        expected = {}
        for spec in specs:
            run_id = compute_run_id(spec.resolved_config, spec.seed)  # type: ignore[attr-defined]
            expected[run_id] = RunRecord(
                run_id,
                spec.campaign,  # type: ignore[attr-defined]
                spec.seed,  # type: ignore[attr-defined]
                spec.expected_context_hash,  # type: ignore[attr-defined]
                "",
                0.0,
                spec.parameters,  # type: ignore[attr-defined]
            )
        entries = _stable_marker_names(campaign_root)
        foreign = [
            name
            for name in entries
            if not name.endswith(".parquet")
            or name.removesuffix(".parquet") not in expected
        ]
        if foreign:
            raise ValueError(f"invalid completion marker names: {sorted(foreign)}")
        completed = []
        for name in entries:
            run_id = name.removesuffix(".parquet")
            if completion_state(campaign_root, expected[run_id]) is not None:
                completed.append(run_id)
        return frozenset(completed)

    @classmethod
    def run_completed(cls, campaign_root: Path, spec: object) -> bool:
        from eqasim_sa.core.run import compute_run_id

        run_id = compute_run_id(spec.resolved_config, spec.seed)  # type: ignore[attr-defined]
        record = RunRecord(
            run_id,
            spec.campaign,  # type: ignore[attr-defined]
            spec.seed,  # type: ignore[attr-defined]
            spec.expected_context_hash,  # type: ignore[attr-defined]
            "",
            0.0,
            spec.parameters,  # type: ignore[attr-defined]
        )
        return completion_state(campaign_root, record) is not None

    @property
    def expected_ids(self) -> frozenset[str]:
        return frozenset(self.expected)

    @property
    def completed_ids(self) -> frozenset[str]:
        return frozenset(self.runs)

    @property
    def execution_evidence(self) -> Mapping[str, ExecutionEvidence | None]:
        return MappingProxyType(
            {
                run_id: selected.completion.execution_evidence
                for run_id, selected in self.runs.items()
            }
        )

    @property
    def measurements(self) -> tuple[tuple[float, int, int], ...]:
        return tuple(
            (
                selected.completion.duration,
                selected.durable_bytes,
                selected.completion.scratch_bytes,
            )
            for selected in self.runs.values()
        )

    @property
    def durable_bytes(self) -> int:
        return sum(selected.durable_bytes for selected in self.runs.values())

    @property
    def tracked_states(self) -> tuple[tuple[Path, FileState], ...]:
        states = [(self.plan_source.path, self.plan_source.state)]
        for selected in self.runs.values():
            states.extend(
                (
                    (selected.archive, selected.archive_state),
                    (selected.marker.path, selected.marker.state),
                    *(
                        (artifact.path, artifact.state)
                        for artifact in selected.artifacts
                    ),
                )
            )
        return tuple(states)

    def materialize(self, destination: Path) -> MaterializedCampaign:
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        plan_path = destination / "plan.parquet"
        capture_stable_file(
            self.plan_source.path,
            plan_path,
            expected_size=self.plan_source.size,
            expected_sha256=self.plan_source.sha256,
        )
        artifacts_by_run = {}
        descriptors = []
        for run_id, selected in self.runs.items():
            run_destination = destination / "artifacts" / run_id
            run_destination.mkdir(parents=True, mode=0o700)
            copied = {}
            captured_artifacts = []
            for artifact in selected.artifacts:
                target = run_destination / artifact.path.name
                captured = capture_stable_file(
                    artifact.path,
                    target,
                    expected_size=artifact.size,
                    expected_sha256=artifact.sha256,
                )
                copied[artifact.path.stem] = target
                captured_artifacts.append(
                    {
                        "name": artifact.path.name,
                        "size": captured.size,
                        "sha256": captured.sha256,
                    }
                )
            fsync_directory(run_destination)
            artifacts_by_run[run_id] = ArtifactSet(copied)
            descriptor = selected.descriptor
            descriptors.append(
                {
                    "runId": run_id,
                    "campaign": selected.expected.campaign,
                    "seed": selected.expected.seed,
                    "contextHash": selected.expected.context_hash,
                    "parameters": selected.expected.parameters,
                    "markerSha256": selected.marker.sha256,
                    "archiveIntegrity": descriptor["archiveIntegrity"],
                    "artifacts": captured_artifacts,
                    "executionEvidenceSha256": descriptor[
                        "executionEvidenceSha256"
                    ],
                    "executionEvidence": descriptor["executionEvidence"],
                }
            )
        for selected in self.runs.values():
            verify_stable_file(selected.marker)
            if directory_state(selected.archive) != selected.archive_state:
                raise RuntimeError(
                    f"selected analysis archive changed: {selected.archive}"
                )
            for artifact in selected.artifacts:
                _verify_stable_state(artifact)
        run_set_descriptor = {"schemaVersion": 1, "runs": descriptors}
        run_set_sha256 = _domain_digest(RUN_SET_DOMAIN, run_set_descriptor)
        run_set_document = {
            **run_set_descriptor,
            "capturedRunSetSha256": run_set_sha256,
        }
        return MaterializedCampaign(
            plan_path,
            MappingProxyType(artifacts_by_run),
            run_set_sha256,
            MappingProxyType(run_set_document),
        )

    def verify(self) -> None:
        try:
            verify_stable_file(self.plan_source)
            for selected in self.runs.values():
                verify_stable_file(selected.marker)
                if directory_state(selected.archive) != selected.archive_state:
                    raise RuntimeError(
                        f"selected analysis archive changed: {selected.archive}"
                    )
                for artifact in selected.artifacts:
                    verify_stable_file(artifact)
        except RuntimeError:
            raise
        except Exception as error:
            raise RuntimeError("selected analysis sources changed during analysis") from error


def expected_records(table: pa.Table, campaign: str) -> dict[str, RunRecord]:
    """Reconstruct unique expected run records from a validated plan table."""

    if type(campaign) is not str or not campaign:
        raise ValueError("campaign must be a non-empty string")
    required = {"designId", "method", "role", "runId", "seed", "contextHash"}
    if not required <= set(table.column_names) or any(
        column.null_count for column in table.columns
    ):
        raise ValueError("plan schema or null values are invalid")
    parameters = tuple(name for name in table.column_names if name not in required)
    expected = {}
    for row in table.to_pylist():
        run_id = row["runId"]
        if type(run_id) is not str or RUN_ID.fullmatch(run_id) is None:
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


def context_descriptor(table: pa.Table) -> dict | None:
    """Decode and validate the optional context descriptor sealed in a plan."""

    payload = (table.schema.metadata or {}).get(b"contextDescriptor")
    if payload is None:
        return None
    try:
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return value
    except (TypeError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("plan contextDescriptor is invalid") from error


def completion_descriptor(run_id: str, completion: CompletionState) -> dict:
    """Return the canonical analysis-facing description of a completed run."""

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
            hashlib.sha256(evidence_bytes).hexdigest()
            if evidence_bytes is not None
            else None
        ),
        "executionEvidence": (
            json.loads(evidence_bytes) if evidence_bytes is not None else None
        ),
    }


def directory_state(path: Path) -> FileState:
    """Capture metadata for a regular, non-symlink analysis archive directory."""

    try:
        info = path.lstat()
    except OSError as error:
        raise ValueError(f"analysis archive is not a regular directory: {path}") from error
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise ValueError(f"analysis archive is not a regular directory: {path}")
    return file_state(info)


def _stable_state_is_current(snapshot: StableFile) -> bool:
    try:
        current = snapshot.path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(current.st_mode) and file_state(current) == snapshot.state


def _verify_stable_state(snapshot: StableFile) -> None:
    if not _stable_state_is_current(snapshot):
        raise RuntimeError(f"selected analysis source changed: {snapshot.path}")


def _stable_marker_names(campaign_root: Path) -> tuple[str, ...]:
    for _ in range(32):
        try:
            return completion_marker_names(campaign_root)
        except ValueError as error:
            cause = error.__cause__
            if not (
                isinstance(cause, RuntimeError)
                and "changed while listing markers" in str(cause)
            ):
                raise
    raise RuntimeError("could not capture a stable completion marker set")


def _artifact_sources(
    archive: Path, completion: CompletionState
) -> tuple[tuple[Path, int | None, str | None], ...]:
    if completion.archive_integrity == VERIFIED:
        return tuple(
            (archive / item.name, item.size, item.sha256)
            for item in completion.artifacts
        )
    entries = tuple(sorted(archive.iterdir(), key=lambda path: os.fsencode(path.name)))
    if not entries or any(
        entry.suffix != ".parquet" or entry.is_symlink() or not entry.is_file()
        for entry in entries
    ):
        raise ValueError(
            f"legacy archive for run {archive.name} must contain only parquet artifacts"
        )
    if len({entry.stem for entry in entries}) != len(entries):
        raise ValueError(
            f"legacy archive for run {archive.name} has duplicate artifact names"
        )
    return tuple((entry, None, None) for entry in entries)


def _domain_digest(domain: bytes, value: object) -> str:
    content = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(domain + content).hexdigest()
