"""Stable campaign cuts shared by execution, estimation, and analysis."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from eqasim_sa.utils import platform
from eqasim_sa.utils.filesystem import (
    FileState,
    StableFile,
    capture_stable_file,
    file_state,
    fsync_directory,
    stable_file,
    verify_stable_file,
)
from eqasim_sa.core.indicators import ArtifactSet, validate_output_name
from eqasim_sa.core.ledger import (
    RUN_ID,
    CompletionState,
    RunRecord,
    completion_marker_names,
    completion_state,
)
from eqasim_sa.ports.simulator import ExecutionEvidence


RUN_SET_DOMAIN = b"eqasim-sa:analysis-run-set:v1\0"
RUN_SET_SCHEMA_VERSION = 2
DERIVED_DIRECTORY = "derived"

DerivedOutputs = Callable[[str, ArtifactSet, dict], Mapping[str, tuple[Path, dict]]]
"""Compute the analysis outputs of one captured run.

Called with the runId, the run's captured archive files, and
``{"parameters": {"runId", "seed", "factors"}, "artifactsSha256": {name: sha256}}``;
returns ``{analysis: (csv, provenance)}`` where ``provenance`` is a JSON
mapping recorded in the captured run set; its ``sha256`` is the expected
SHA-256 of the CSV, verified while copying it.
"""


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
    root: Path
    contexts: Mapping[str, Mapping[str, object]]

    def with_derived_outputs(
        self, derived_outputs: DerivedOutputs, workers: int = 1
    ) -> MaterializedCampaign:
        """Add analysis outputs to every captured run, ``workers`` runs at a time.

        Each produced CSV is copied below ``derived/<runId>/`` next to the
        captured archive files, then recorded with its provenance in the run
        set.  Any failure fails the whole capture.
        """

        if type(workers) is not int or workers < 1:
            raise ValueError("derived output workers must be a positive integer")
        run_ids = list(self.artifacts)
        produced: dict[str, Mapping[str, tuple[Path, dict]]] = {}
        if run_ids:
            pool = ThreadPoolExecutor(max_workers=min(workers, len(run_ids)))
            try:
                futures = {
                    pool.submit(
                        derived_outputs,
                        run_id,
                        self.artifacts[run_id],
                        json.loads(json.dumps(dict(self.contexts[run_id]))),
                    ): run_id
                    for run_id in run_ids
                }
                pending = set(futures)
                while pending:
                    done, pending = wait(pending, return_when="FIRST_EXCEPTION")
                    for future in done:
                        error = future.exception()
                        if error is not None:
                            raise error
                        produced[futures[future]] = future.result()
            finally:
                # Started runs finish (their scripts exit) before the lease ends.
                pool.shutdown(wait=True, cancel_futures=True)
        names: set[str] | None = None
        artifacts_by_run = {}
        descriptors = []
        for descriptor in self.run_set_document["runs"]:  # type: ignore[union-attr]
            run_id = descriptor["runId"]
            outputs = produced[run_id]
            if not isinstance(outputs, Mapping):
                raise TypeError("derived outputs must be a mapping")
            if names is None:
                names = set(outputs)
            elif set(outputs) != names:
                raise ValueError("derived outputs must name the same analyses for every run")
            archived = self.artifacts[run_id]
            paths = dict(archived.paths)
            records = {}
            for name in sorted(outputs):
                validate_output_name(name)
                if name in paths:
                    raise ValueError(f"derived output {name!r} is an archived output")
                source, provenance = outputs[name]
                if (
                    not isinstance(provenance, Mapping)
                    or "size" in provenance
                    or not isinstance(provenance.get("sha256"), str)
                ):
                    raise ValueError(
                        f"derived output {name!r} provenance must be a mapping "
                        "with the CSV sha256 and without size"
                    )
                target = self.root / DERIVED_DIRECTORY / run_id / f"{name}.csv"
                captured = capture_stable_file(
                    Path(source), target, expected_sha256=provenance["sha256"]
                )
                paths[name] = target
                records[name] = {
                    **json.loads(json.dumps(dict(provenance), allow_nan=False)),
                    "size": captured.size,
                    "sha256": captured.sha256,
                }
            if records:
                fsync_directory(self.root / DERIVED_DIRECTORY / run_id)
            artifacts_by_run[run_id] = ArtifactSet(paths)
            descriptors.append({**descriptor, "analyses": records})
        run_set_descriptor = {"schemaVersion": RUN_SET_SCHEMA_VERSION, "runs": descriptors}
        run_set_sha256 = _domain_digest(RUN_SET_DOMAIN, run_set_descriptor)
        return replace(
            self,
            artifacts=MappingProxyType(artifacts_by_run),
            run_set_sha256=run_set_sha256,
            run_set_document=MappingProxyType(
                {**run_set_descriptor, "capturedRunSetSha256": run_set_sha256}
            ),
        )


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
            marker = completion.marker
            archive = completion.archive
            archive_state = completion.archive_state
            artifacts = completion.captured_artifacts
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
    def completed_evidence(
        cls, campaign_root: Path, specs: Sequence[object]
    ) -> dict[str, ExecutionEvidence | None]:
        """Map each completed run of ``specs`` to its recorded execution evidence."""

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
        evidence: dict[str, ExecutionEvidence | None] = {}
        for name in entries:
            run_id = name.removesuffix(".parquet")
            state = completion_state(campaign_root, expected[run_id])
            if state is not None:
                evidence[run_id] = state.execution_evidence
        return evidence

    @classmethod
    def completed_specs(cls, campaign_root: Path, specs: Sequence[object]) -> frozenset[str]:
        return frozenset(cls.completed_evidence(campaign_root, specs))

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
        contexts = {}
        for run_id, selected in self.runs.items():
            run_destination = destination / "artifacts" / run_id
            run_destination.mkdir(parents=True, mode=0o700)
            copied = {}
            captured_artifacts = []
            # Captured files follow the manifest order: the manifest names them.
            for entry, artifact in zip(
                selected.completion.artifacts, selected.artifacts, strict=True
            ):
                if artifact.path.name != entry.file_name:
                    raise RuntimeError(
                        f"selected run {run_id} archive does not match its manifest"
                    )
                target = run_destination / entry.file_name
                captured = capture_stable_file(
                    artifact.path,
                    target,
                    expected_size=artifact.size,
                    expected_sha256=artifact.sha256,
                )
                copied[entry.name] = target
                captured_artifacts.append(
                    {
                        "name": entry.name,
                        "sourcePath": entry.source_path,
                        "size": captured.size,
                        "sha256": captured.sha256,
                    }
                )
            fsync_directory(run_destination)
            artifacts_by_run[run_id] = ArtifactSet(copied)
            contexts[run_id] = MappingProxyType(
                {
                    "parameters": {
                        "runId": run_id,
                        "seed": selected.expected.seed,
                        "factors": dict(selected.expected.parameters),
                    },
                    "artifactsSha256": {
                        item["name"]: item["sha256"] for item in captured_artifacts
                    },
                }
            )
            descriptor = selected.descriptor
            descriptors.append(
                {
                    "runId": run_id,
                    "campaign": selected.expected.campaign,
                    "seed": selected.expected.seed,
                    "contextHash": selected.expected.context_hash,
                    "parameters": selected.expected.parameters,
                    "markerSha256": selected.marker.sha256,
                    "artifacts": captured_artifacts,
                    "executionEvidenceSha256": descriptor[
                        "executionEvidenceSha256"
                    ],
                    "executionEvidence": descriptor["executionEvidence"],
                    "analyses": {},
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
        run_set_descriptor = {
            "schemaVersion": RUN_SET_SCHEMA_VERSION,
            "runs": descriptors,
        }
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
            destination,
            MappingProxyType(contexts),
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
        "artifacts": [
            {
                "name": item.name,
                "sourcePath": item.source_path,
                "size": item.size,
                "sha256": item.sha256,
            }
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
    if not stat.S_ISDIR(info.st_mode) or platform.is_link(path):
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


def _domain_digest(domain: bytes, value: object) -> str:
    content = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(domain + content).hexdigest()
