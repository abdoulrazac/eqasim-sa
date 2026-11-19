"""Publish immutable analysis generations from verified campaign snapshots."""

import hashlib
import importlib.metadata
import json
import os
import platform as python_platform
import re
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

import pyarrow as pa

from eqasim_sa.core.campaign_snapshot import (
    CampaignSnapshot,
    DerivedOutputs,
    context_descriptor,
    expected_records,
)
from eqasim_sa.utils import platform
from eqasim_sa.utils.filesystem import (
    file_lease,
    owned_by_current_user,
    fsync_directory,
    secure_directory,
    stable_file,
    write_bytes_exclusive,
    write_bytes_atomic,
    write_parquet_exclusive,
)
from eqasim_sa.core.indicators import ArtifactSet
from eqasim_sa.core.software_identity import (
    SoftwareIdentity,
    verify_software_source_states,
)
from eqasim_sa.ports.simulator import ExecutionEvidence


ANALYSIS_LAYOUT = "immutable-generation-v1"
ANALYSIS_STATE_DIRECTORY = "analysis"
GENERATION_DOMAIN = b"eqasim-sa:analysis-generation:immutable-v1\0"
PROVENANCE_DOMAIN = b"eqasim-sa:analysis-provenance:v1\0"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
OUTPUT_ROLE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
OWNED_OUTPUTS = {
    "governedReport": "governed-report.json",
    "capturedRunSet": "captured-run-set.json",
}

OUTPUT_FILENAMES = frozenset(
    {
        "indicators.parquet",
        "replication-dispersion.parquet",
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


@dataclass(frozen=True)
class PlanCapture:
    """Stable plan snapshot copied into an analysis transaction."""

    path: Path
    table: pa.Table
    sha256: str
    context_descriptor: dict | None


@dataclass(frozen=True)
class RunCapture:
    """Selected run artifacts and the digest of their captured inventory."""

    expected_ids: frozenset[str]
    completed_ids: frozenset[str]
    artifacts: Mapping[str, ArtifactSet]
    run_set_sha256: str
    run_set_document: Mapping[str, object]
    scientific_execution_evidence_sha256: str | None

    @property
    def analyses(self) -> dict[str, dict[str, dict]]:
        """Provenance of each analysis output, by analysis then runId."""

        analyses: dict[str, dict[str, dict]] = {}
        for run in self.run_set_document["runs"]:  # type: ignore[union-attr]
            for name, record in run["analyses"].items():
                analyses.setdefault(name, {})[run["runId"]] = dict(record)
        return analyses


@dataclass(frozen=True)
class PublishedGeneration:
    """Committed immutable analysis generation and its resolved outputs."""

    generation_id: str
    path: Path
    paths: Mapping[str, Path]
    manifest: Mapping[str, object]


@dataclass(frozen=True)
class AnalysisArtifact:
    """Named table or byte payload prepared for analysis publication."""

    filename: str
    content: pa.Table | bytes


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


def _validate_generation_inventory(generations: Path) -> None:
    for generation in generations.iterdir():
        info = generation.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or platform.is_link(generation)
            or SHA256.fullmatch(generation.name) is None
        ):
            raise ValueError(
                f"analysis generations contain unexpected content: {generation}"
            )
        outputs = tuple(generation.iterdir())
        if not outputs or any(output.name not in OUTPUT_FILENAMES for output in outputs):
            raise ValueError(
                f"analysis generation has an invalid inventory: {generation}"
            )
        for output in outputs:
            output_info = output.lstat()
            if (
                not stat.S_ISREG(output_info.st_mode)
                or platform.is_link(output)
                or not owned_by_current_user(output_info)
                or output_info.st_nlink != 1
            ):
                raise ValueError(
                    f"analysis generation contains an invalid output: {output}"
                )


def _validate_analysis_state(
    state: Path, *, allow_active_staging: bool = False
) -> None:
    if not state.exists() and not platform.is_link(state):
        return
    secure_directory(state, create=False)
    children = {entry.name: entry for entry in state.iterdir()}
    foreign = set(children) - {"staging", "generations"}
    if foreign:
        raise ValueError(
            "analysis state contains unexpected root content: "
            f"{sorted(foreign)}"
        )
    existing_staging = children.get("staging")
    if existing_staging is not None:
        secure_directory(existing_staging, create=False)
        for entry in existing_staging.iterdir():
            if (
                not allow_active_staging
                or platform.is_link(entry)
                or not entry.is_dir()
                or re.fullmatch(r"[0-9a-f]{32}", entry.name) is None
            ):
                raise ValueError("analysis staging must be empty before publication")
    existing_generations = children.get("generations")
    if existing_generations is not None:
        secure_directory(existing_generations, create=False)
        _validate_generation_inventory(existing_generations)


def validate_scientific_execution_evidence(
    table: pa.Table,
    campaign: str,
    evidence_by_run: Mapping[str, ExecutionEvidence | None],
    scientific_contract: dict,
    *,
    allow_empty: bool = False,
) -> str | None:
    """Require equivalent execution evidence for scientific runs."""

    expected = expected_records(table, campaign)
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
        descriptor = context_descriptor(table)
        model_source = descriptor["eqasimSourceSha256"]
    except (KeyError, TypeError) as error:
        raise ValueError("scientific contract or plan context is incomplete") from error
    common: bytes | None = None
    for run_id in sorted(evidence_by_run, key=os.fsencode):
        evidence = evidence_by_run[run_id]
        if evidence is None:
            raise ValueError(
                f"scientific run {run_id} requires execution evidence"
            )
        if evidence.context_hash != expected[run_id].context_hash:
            raise ValueError(
                f"scientific run {run_id} execution evidence contextHash does not match plan"
            )
        if evidence.eqasim_sa_sha256 != software_sha256:
            raise ValueError(
                f"scientific run {run_id} execution evidence software digest does not match plan"
            )
        if evidence.model_source_sha256 != model_source:
            raise ValueError(
                f"scientific run {run_id} execution evidence eqasim source does not match plan"
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
    """Validate optional but uniform execution evidence for descriptive runs."""

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
    """Capture sorted runtime versions and the sealed package source digest."""

    versions = {"python": python_platform.python_version()}
    for distribution in ("eqasim-sa", "numpy", "pyarrow", "SALib", "scipy"):
        versions[distribution] = importlib.metadata.version(distribution)
    versions["eqasim-sa-source"] = software_identity.sha256
    return {name: versions[name] for name in sorted(versions)}


class AnalysisPublication:
    """Capture sources and atomically publish one immutable analysis generation."""

    def __init__(self, campaign_root: Path) -> None:
        self.campaign_root = campaign_root
        _validate_analysis_state(
            campaign_root / ANALYSIS_STATE_DIRECTORY,
            allow_active_staging=True,
        )
        technical = secure_directory(campaign_root / ".eqasim-sa", create=True)
        locks = secure_directory(technical / "locks", create=True)
        fsync_directory(campaign_root)
        fsync_directory(technical)
        self._lease_context = file_lease(locks / "analysis.lock")
        self._entered = False
        self._published = False
        self._snapshot: CampaignSnapshot | None = None

    def __enter__(self) -> "AnalysisPublication":
        if self._entered:
            raise RuntimeError("analysis publication context is not reentrant")
        self._lease_context.__enter__()
        self._entered = True
        try:
            self._initialize()
        except BaseException:
            self._lease_context.__exit__(*sys.exc_info())
            self._entered = False
            raise
        return self

    def _initialize(self) -> None:
        self.state = secure_directory(
            self.campaign_root / ANALYSIS_STATE_DIRECTORY, create=True
        )
        _validate_analysis_state(self.state)
        fsync_directory(self.campaign_root)
        self.staging = secure_directory(self.state / "staging", create=True)
        self.generations = secure_directory(self.state / "generations", create=True)
        fsync_directory(self.state)
        self.work = self.staging / uuid.uuid4().hex
        self.work.mkdir(mode=0o700)
        self.capture_directory = self.work / "capture"
        self.generation_staging = self.work / "generation"
        self.capture_directory.mkdir(mode=0o700)
        self.generation_staging.mkdir(mode=0o700)
        fsync_directory(self.staging)
    def __exit__(self, *exc_info) -> None:  # type: ignore[no-untyped-def]
        try:
            if self.work.exists() and not platform.is_link(self.work):
                platform.remove_tree(self.work)
                fsync_directory(self.staging)
        finally:
            self._lease_context.__exit__(*exc_info)
            self._entered = False

    def capture(
        self,
        snapshot: CampaignSnapshot,
        scientific_contract: dict | None = None,
        *,
        allow_empty_scientific_evidence: bool = False,
        derived_outputs: DerivedOutputs | None = None,
        derived_workers: int = 1,
    ) -> tuple[PlanCapture, RunCapture]:
        """Copy the selected sources, then add their analysis outputs.

        ``derived_outputs`` runs under the ``analysis.lock`` lease on the
        captured (already verified) copies, ``derived_workers`` runs at a time.
        """

        if snapshot.campaign_root != self.campaign_root:
            raise ValueError("campaign snapshot belongs to another campaign root")
        materialized = snapshot.materialize(self.capture_directory / "campaign")
        self._snapshot = snapshot
        evidence = snapshot.execution_evidence
        if scientific_contract is not None:
            scientific_evidence_sha256 = validate_scientific_execution_evidence(
                snapshot.plan_table,
                snapshot.campaign,
                evidence,
                scientific_contract,
                allow_empty=allow_empty_scientific_evidence,
            )
        else:
            scientific_evidence_sha256 = None
            if allow_empty_scientific_evidence:
                validate_descriptive_execution_evidence(evidence)
        if derived_outputs is not None:
            materialized = materialized.with_derived_outputs(
                derived_outputs, derived_workers
            )
        return (
            PlanCapture(
                materialized.plan_path,
                snapshot.plan_table,
                snapshot.plan_sha256,
                snapshot.context_descriptor,
            ),
            RunCapture(
                snapshot.expected_ids,
                snapshot.completed_ids,
                materialized.artifacts,
                materialized.run_set_sha256,
                materialized.run_set_document,
                scientific_evidence_sha256,
            ),
        )

    def verify_sources(self) -> None:
        if self._snapshot is None:
            raise RuntimeError("analysis campaign was not captured")
        self._snapshot.verify()

    def publish(
        self,
        outputs: Mapping[str, AnalysisArtifact],
        summary: Mapping[str, object],
        *,
        governed_report: Callable[
            [Mapping[str, Mapping[str, object]]], Mapping[str, object]
        ],
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
        if set(outputs) & set(OWNED_OUTPUTS):
            raise ValueError("analysis publication owns governed and capture documents")
        if any(
            type(role) is not str or OUTPUT_ROLE.fullmatch(role) is None
            for role in outputs
        ):
            raise ValueError("analysis output roles must be safe identifiers")
        if any(
            not isinstance(artifact, AnalysisArtifact)
            for artifact in outputs.values()
        ):
            raise TypeError("analysis outputs must contain AnalysisArtifact values")
        if outputs["indicators"].filename != "indicators.parquet":
            raise ValueError("indicators output must use indicators.parquet")
        filenames = [artifact.filename for artifact in outputs.values()]
        if any(type(filename) is not str for filename in filenames):
            raise TypeError("analysis output filenames must be strings")
        if set(filenames) & set(OWNED_OUTPUTS.values()):
            raise ValueError("analysis publication owns governed and capture filenames")
        if len(set(filenames)) != len(filenames):
            raise ValueError("analysis output filenames must be unique")
        staged_records: dict[str, dict[str, object]] = {}

        def serialize(name: str, artifact: AnalysisArtifact) -> None:
            path = self.generation_staging / artifact.filename
            if artifact.filename not in OUTPUT_FILENAMES:
                raise ValueError(
                    f"unknown analysis output filename: {artifact.filename}"
                )
            if isinstance(artifact.content, pa.Table):
                write_parquet_exclusive(
                    artifact.content,
                    path,
                    staging_directory=self.generation_staging,
                )
            elif type(artifact.content) is bytes:
                write_bytes_exclusive(
                    artifact.content,
                    path,
                    staging_directory=self.generation_staging,
                )
            else:
                raise TypeError("analysis artifact content must be a table or bytes")
            snapshot = stable_file(path)
            if path.lstat().st_nlink != 1:
                raise ValueError(f"analysis output must not be hard-linked: {path}")
            _fsync_regular(path)
            staged_records[name] = {
                "name": path.name,
                "size": snapshot.size,
                "sha256": snapshot.sha256,
            }

        for name, artifact in outputs.items():
            serialize(name, artifact)
        report_document = governed_report(MappingProxyType(staged_records.copy()))
        if not isinstance(report_document, Mapping):
            raise TypeError("governed report builder must return a mapping")
        serialize(
            "governedReport",
            AnalysisArtifact(
                OWNED_OUTPUTS["governedReport"],
                _canonical(dict(report_document)),
            ),
        )
        serialize(
            "capturedRunSet",
            AnalysisArtifact(
                OWNED_OUTPUTS["capturedRunSet"],
                _canonical(dict(runs.run_set_document)),
            ),
        )
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
            "analyses": runs.analyses,
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
            **_flat_output_fields(output_records),
        }
        encoded_manifest = _canonical(manifest)
        fsync_directory(self.generation_staging)
        if indicator_guard is not None:
            indicator_guard()
        verify_software_source_states(software_identity)
        self.verify_sources()
        if final.exists() or platform.is_link(final):
            _validate_existing_generation(final, output_records)
        else:
            platform.rename_with_retry(self.generation_staging, final)
            fsync_directory(self.generations)
        self.verify_sources()
        if indicator_guard is not None:
            indicator_guard()
        verify_software_source_states(software_identity)
        _validate_existing_generation(final, output_records)
        if indicator_guard is not None:
            indicator_guard()
        write_bytes_atomic(
            encoded_manifest,
            self.campaign_root / "analysis-generation.json",
            staging_directory=self.staging,
        )
        self._published = True
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


def _fsync_regular(path: Path) -> None:
    # Windows can only flush handles opened for writing.
    access = os.O_RDWR if platform.IS_WINDOWS else os.O_RDONLY
    flags = access | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _flat_output_fields(outputs: Mapping[str, Mapping[str, object]]) -> dict:
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
    if not stat.S_ISDIR(info.st_mode) or platform.is_link(directory):
        raise ValueError("content-addressed analysis generation is not a directory")
    expected = {Path(str(record["path"])).name for record in outputs.values()}
    entries = tuple(directory.iterdir())
    if {entry.name for entry in entries} != expected:
        raise ValueError("content-addressed analysis generation inventory is invalid")
    by_name = {Path(str(record["path"])).name: record for record in outputs.values()}
    for entry in entries:
        snapshot = stable_file(entry)
        record = by_name[entry.name]
        if (
            entry.lstat().st_nlink != 1
            or snapshot.size != record["size"]
            or snapshot.sha256 != record["sha256"]
        ):
            raise ValueError("content-addressed analysis generation is corrupted")

