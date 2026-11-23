"""Analyze complete campaign snapshots and prepare governed results."""

import json
import math
import os
import re
import stat
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import pyarrow as pa

from eqasim_sa.core.analysis_publication import (
    AnalysisArtifact,
    AnalysisPublication,
    validate_descriptive_execution_evidence,
    validate_scientific_execution_evidence,
)
from eqasim_sa.core.campaign_snapshot import CampaignSnapshot, DerivedOutputs
from eqasim_sa.core.context_identity import ExecutionContextIdentity
from eqasim_sa.core.run import validate_model_document
from eqasim_sa.core.indicators import (
    Indicator,
    analysis_identity,
    indicator_definition_sha256,
    validate_outputs_declaration,
)
from eqasim_sa.core.methods.canonical_morris import (
    _canonical_morris_tables,
    _replication_allocation,
)
from eqasim_sa.core.methods.common import validate_bootstrap_resamples
from eqasim_sa.core.methods.discrete_oat import _discrete_oat_effects
from eqasim_sa.core.methods.multiscramble_sobol import (
    _multiscramble_sobol_tables,
)
from eqasim_sa.core.methods.sobol import _sobol_ranking
from eqasim_sa.core.methods.structured import (
    validate_structured_plan_metadata,
    validate_structured_plan_rows,
)
from eqasim_sa.core.reporting import (
    _convergence_evidence_table,
    _governed_report,
    _output_summary_table,
)
from eqasim_sa.core.software_identity import read_software_identity
from eqasim_sa.utils.arrow import read_parquet, rows_to_table
from eqasim_sa.utils import platform
from eqasim_sa.utils.filesystem import open_regular

@dataclass(frozen=True)
class AnalyzeResult:
    """Paths, counts, and inference status produced by campaign analysis."""

    path: Path
    dispersion_path: Path | None
    sensitivity_method: str | None
    sensitivity_path: Path | None
    runs: int
    indicators: int
    values: int
    completion_rate: float
    expected: int
    completed: int
    missing: int
    failed: int
    convergence_path: Path | None = None
    rank_reversals_path: Path | None = None
    by_scramble_path: Path | None = None
    diagnostics_path: Path | None = None
    noise_floor_path: Path | None = None
    replication_allocation_path: Path | None = None
    inference_status: str | None = None
    output_summary_path: Path | None = None
    convergence_evidence_path: Path | None = None
    governed_report_path: Path | None = None


def analyze(
    campaign_root: Path,
    indicators: Sequence[Indicator],
    *,
    campaign: str,
    completeness_threshold: float,
    seed_role: str = "replication",
    method: str = "grid",
    bootstrap_resamples: int | None = None,
    confidence_level: float | None = None,
    convergence_checkpoints: tuple[int, ...] | None = None,
    screening_top_k: int | None = None,
    meta_config_sha256: str | None = None,
    descriptive_only: bool = False,
    derived_outputs: DerivedOutputs | None = None,
    derived_workers: int = 1,
) -> AnalyzeResult:
    """Analyze a campaign and publish outputs under its governed contract.

    ``derived_outputs`` computes the analysis outputs indicators read, for each
    captured run (``derived_workers`` runs at a time), under the analysis
    lease; any error fails the analysis whatever the completeness threshold.
    """

    campaign_root = campaign_root.resolve(strict=True)
    return _analyze_campaign(
        campaign_root,
        indicators,
        campaign,
        completeness_threshold,
        seed_role,
        method,
        bootstrap_resamples,
        confidence_level,
        convergence_checkpoints,
        screening_top_k,
        meta_config_sha256,
        descriptive_only,
        derived_outputs,
        derived_workers,
    )


def _analyze_campaign(
    campaign_root: Path,
    indicators: Sequence[Indicator],
    campaign: str,
    completeness_threshold: float,
    seed_role: str,
    method: str,
    bootstrap_resamples: int | None,
    confidence_level: float | None,
    convergence_checkpoints: tuple[int, ...] | None,
    screening_top_k: int | None,
    meta_config_sha256: str | None,
    descriptive_only: bool,
    derived_outputs: DerivedOutputs | None,
    derived_workers: int,
) -> AnalyzeResult:
    if type(descriptive_only) is not bool:
        raise ValueError("descriptive_only must be boolean")
    oat_methods = {"discrete_oat", "canonical_morris"}
    if method in oat_methods and seed_role != "replication":
        raise ValueError(f"{method} requires seed_role replication")
    if method in oat_methods and not indicators:
        raise ValueError(f"{method} requires at least one indicator")
    if method in {"sobol", "multiscramble_sobol"} and seed_role != "factor":
        raise ValueError("Sobol requires seed_role factor")
    if method in {"sobol", "multiscramble_sobol"} and not indicators:
        raise ValueError("Sobol requires at least one indicator")
    if method == "sobol":
        validate_bootstrap_resamples(
            bootstrap_resamples,
            method="Sobol",
            minimum=1,
        )
    if method == "sobol" and (
        type(confidence_level) is not float
        or not math.isfinite(confidence_level)
        or not 0 < confidence_level < 1
    ):
        raise ValueError("Sobol confidence_level must be a finite float in (0,1)")
    if method == "canonical_morris":
        validate_bootstrap_resamples(
            bootstrap_resamples,
            method="canonical_morris",
            minimum=100,
        )
        if (
            type(confidence_level) is not float
            or not math.isfinite(confidence_level)
            or not 0 < confidence_level < 1
        ):
            raise ValueError(
                "canonical_morris confidence_level must be a finite float in (0,1)"
            )
        if (
            not isinstance(convergence_checkpoints, tuple)
            or not convergence_checkpoints
            or any(
                type(value) is not int or value < 2
                for value in convergence_checkpoints
            )
            or tuple(sorted(set(convergence_checkpoints)))
            != convergence_checkpoints
        ):
            raise ValueError(
                "canonical_morris convergence_checkpoints must be sorted unique integers at least 2"
            )
        if type(screening_top_k) is not int or screening_top_k <= 0:
            raise ValueError("canonical_morris screening_top_k must be positive")
    if method == "multiscramble_sobol" and (
        type(confidence_level) is not float
        or not math.isfinite(confidence_level)
        or not 0 < confidence_level < 1
    ):
        raise ValueError("multiscramble_sobol confidence_level must be a finite float in (0,1)")
    if seed_role not in {"replication", "factor"}:
        raise ValueError("seed_role must be replication or factor")
    if (
        type(completeness_threshold) is not float
        or not math.isfinite(completeness_threshold)
        or not 0 <= completeness_threshold <= 1
    ):
        raise ValueError("completeness_threshold must be a finite float in [0,1]")
    _validate_indicators(indicators)
    plan = campaign_root / "plan.parquet"
    if platform.is_link(plan) or not plan.is_file():
        raise ValueError("plan.parquet must be a regular non-symlink file")
    plan_snapshot = CampaignSnapshot.inspect_plan(plan)
    plan_table = plan_snapshot.table
    structured_factors = validate_structured_plan_metadata(
        dict(plan_table.schema.metadata or {})
    )
    validate_structured_plan_rows(plan_table, structured_factors)
    if not len(plan_table):
        raise ValueError("plan contains no expected runs")
    methods = (
        set(plan_table["method"].to_pylist())
        if "method" in plan_table.column_names
        else set()
    )
    if (
        len(methods) != 1
        or method
        not in {
            "grid",
            "discrete_oat",
            "canonical_morris",
            "sobol",
            "multiscramble_sobol",
        }
        or methods != {method}
    ):
        actual = next(iter(methods), "missing") if len(methods) <= 1 else "mixed"
        raise ValueError(
            f"plan method {actual} is incompatible with requested method {method}"
        )
    if method in {
        "discrete_oat",
        "canonical_morris",
        "sobol",
        "multiscramble_sobol",
    } and completeness_threshold != 1.0:
        raise ValueError(f"{method.title()} completeness_threshold must equal 1.0")
    sealed_scientific_contract = _validate_frozen_plan(
        plan_table,
        method,
        bootstrap_resamples,
        confidence_level,
        convergence_checkpoints,
        screening_top_k,
        structured_factors,
        descriptive_only,
    )
    numerical_engine = _current_numerical_engine(plan_table, method)
    indicator_identities = tuple(_current_indicator_identities(indicators))
    analysis_software = read_software_identity()
    if sealed_scientific_contract is not None:
        if sealed_scientific_contract["software"] != {
            "eqasimSaVersion": analysis_software.version,
            "eqasimSaSha256": analysis_software.sha256,
        }:
            raise ValueError(
                "active eqasim-sa software does not match the sealed scientific plan"
            )
        _validate_scientific_indicator_definitions(
            indicator_identities, sealed_scientific_contract
        )
    snapshot = CampaignSnapshot.capture(
        campaign_root,
        plan,
        campaign,
        inspected_plan=plan_snapshot,
    )
    inspected_expected = set(snapshot.expected_ids)
    inspected_completed = set(snapshot.completed_ids)
    inspected_failed = _failure_ids(
        campaign_root, inspected_expected - inspected_completed
    )
    inspected_missing = inspected_expected - inspected_completed - inspected_failed
    inspected_rate = len(inspected_completed) / len(inspected_expected)
    if not descriptive_only and inspected_rate < completeness_threshold:
        raise ValueError(
            "campaign completeness "
            f"{inspected_rate:.17g} is below threshold {completeness_threshold:.17g}: "
            f"missing={len(inspected_missing)}, failed={len(inspected_failed)}"
        )
    if sealed_scientific_contract is not None:
        validate_scientific_execution_evidence(
            plan_table,
            campaign,
            snapshot.execution_evidence,
            sealed_scientific_contract,
            allow_empty=descriptive_only,
        )
    elif descriptive_only:
        validate_descriptive_execution_evidence(snapshot.execution_evidence)
    with AnalysisPublication(campaign_root) as publication:
        plan_capture, run_capture = publication.capture(
            snapshot,
            sealed_scientific_contract,
            allow_empty_scientific_evidence=descriptive_only,
            derived_outputs=derived_outputs,
            derived_workers=derived_workers,
        )
        expected_ids = set(run_capture.expected_ids)
        completed_ids = set(run_capture.completed_ids)
        if not expected_ids:
            raise ValueError("plan contains no expected runs")
        failed_ids = _failure_ids(campaign_root, expected_ids - completed_ids)
        missing_ids = expected_ids - completed_ids - failed_ids
        completion_rate = len(completed_ids) / len(expected_ids)
        if not descriptive_only and completion_rate < completeness_threshold:
            raise ValueError(
                "campaign completeness "
                f"{completion_rate:.17g} is below threshold {completeness_threshold:.17g}: "
                f"missing={len(missing_ids)}, failed={len(failed_ids)}"
            )
        try:
            rows = []
            for run_id in sorted(completed_ids):
                artifacts = run_capture.artifacts[run_id]
                for indicator in indicators:
                    try:
                        value = indicator.compute(artifacts)
                    except Exception as error:
                        raise ValueError(
                            f"run {run_id}, indicator {indicator.name!r}: {error}"
                        ) from error
                    if type(value) is not float or not math.isfinite(value):
                        raise ValueError(
                            f"run {run_id}, indicator {indicator.name!r}: must return a finite float"
                        )
                    rows.append((run_id, indicator.name, value))
            schema = pa.schema(
                [
                    ("runId", pa.string()),
                    ("indicator", pa.string()),
                    ("value", pa.float64()),
                ]
            )
            table = pa.Table.from_arrays(
                [
                    pa.array((row[index] for row in rows), type=field.type)
                    for index, field in enumerate(schema)
                ],
                schema=schema,
            )
            outputs = {
                "indicators": AnalysisArtifact("indicators.parquet", table),
                "outputSummary": AnalysisArtifact(
                    "output-summary.parquet",
                    _output_summary_table(
                        plan_table,
                        rows,
                        tuple(indicator.name for indicator in indicators),
                    ),
                ),
            }
            if seed_role == "replication":
                outputs["dispersion"] = AnalysisArtifact(
                    "replication-dispersion.parquet",
                    _dispersion_table(plan_capture.path, rows),
                )
            inference_status = None
            scientific_contract = sealed_scientific_contract
            sensitivity_table = None
            convergence_table = None
            if not descriptive_only:
                if method == "discrete_oat":
                    outputs["sensitivity"] = AnalysisArtifact(
                        "discrete-oat-effects.parquet",
                        _discrete_oat_effects(plan_table, rows),
                    )
                elif method == "canonical_morris":
                    (
                        ranking_table,
                        convergence_table,
                        reversal_table,
                        noise_table,
                    ) = _canonical_morris_tables(
                        plan_table,
                        rows,
                        bootstrap_resamples,
                        confidence_level,
                        convergence_checkpoints,
                        screening_top_k,
                        include_noise_floor=True,
                    )
                    sensitivity_table = ranking_table
                    outputs["sensitivity"] = AnalysisArtifact(
                        "canonical-morris-ranking.parquet", ranking_table
                    )
                    outputs["convergence"] = AnalysisArtifact(
                        "canonical-morris-convergence.parquet", convergence_table
                    )
                    outputs["rankReversals"] = AnalysisArtifact(
                        "canonical-morris-rank-reversals.parquet", reversal_table
                    )
                    outputs["noiseFloor"] = AnalysisArtifact(
                        "noise-floor.parquet", noise_table
                    )
                    sealed = json.loads(
                        (plan_table.schema.metadata or {})[b"canonicalMorrisDesign"]
                    )
                    allocation = sealed["scientificContract"]["charter"][
                        "stochastic"
                    ]["allocation"]
                    inference_status = (
                        "pilot_only" if allocation["phase"] == "pilot" else None
                    )
                    if allocation["phase"] == "pilot":
                        recommendation = _replication_allocation(sealed, noise_table)
                        encoded_recommendation = json.dumps(
                            recommendation,
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        ).encode()
                        outputs["replicationAllocation"] = AnalysisArtifact(
                            "replication-allocation.json", encoded_recommendation
                        )
                elif method == "sobol":
                    outputs["sensitivity"] = AnalysisArtifact(
                        "sobol-indices.parquet",
                        _sobol_ranking(
                            plan_table,
                            rows,
                            bootstrap_resamples,
                            confidence_level,
                            structured_factors,
                        ),
                    )
                elif method == "multiscramble_sobol":
                    (
                        indices_table,
                        by_scramble_table,
                        convergence_table,
                        diagnostics_table,
                    ) = _multiscramble_sobol_tables(
                        plan_table, rows, confidence_level, structured_factors
                    )
                    sensitivity_table = indices_table
                    outputs["sensitivity"] = AnalysisArtifact(
                        "multiscramble-sobol-indices.parquet", indices_table
                    )
                    outputs["byScramble"] = AnalysisArtifact(
                        "multiscramble-sobol-by-scramble.parquet", by_scramble_table
                    )
                    outputs["convergence"] = AnalysisArtifact(
                        "multiscramble-sobol-convergence.parquet", convergence_table
                    )
                    outputs["diagnostics"] = AnalysisArtifact(
                        "multiscramble-sobol-diagnostics.parquet", diagnostics_table
                    )
            complete = (
                len(completed_ids) == len(expected_ids)
                and not missing_ids
                and not failed_ids
            )
            if not complete:
                inference_status = "incomplete_noninferential"
            elif descriptive_only:
                inference_status = "descriptive_noninferential"
            elif method not in {"canonical_morris", "multiscramble_sobol"}:
                inference_status = "demonstration_noninferential"
            criterion_reports = []
            converged = False
            if scientific_contract is not None and not descriptive_only:
                evidence_table, converged, criterion_reports = _convergence_evidence_table(
                    method,
                    scientific_contract["charter"],
                    convergence_table,
                    {
                        parameter["name"]
                        for parameter in scientific_contract["parameters"]
                        if parameter["name"] != "seed"
                        or scientific_contract["charter"]["stochastic"]["target"]
                        != "finite_seed_factor"
                    },
                )
                outputs["convergenceEvidence"] = AnalysisArtifact(
                    "convergence-evidence.parquet", evidence_table
                )
                if inference_status != "pilot_only":
                    inference_status = "conditional" if converged else "unstable"
            def build_governed_report(
                records: Mapping[str, Mapping[str, object]],
            ) -> Mapping[str, object]:
                return _governed_report(
                    method=method,
                    inference_status=inference_status,
                    completion_rate=completion_rate,
                    expected=len(expected_ids),
                    completed=len(completed_ids),
                    missing=len(missing_ids),
                    failed=len(failed_ids),
                    output_records=records,
                    scientific_contract=scientific_contract,
                    sensitivity_table=sensitivity_table,
                    converged=converged,
                    criterion_reports=criterion_reports,
                )

            def guard_indicators() -> None:
                if tuple(_current_indicator_identities(indicators)) != indicator_identities:
                    raise RuntimeError("indicator implementation changed during analysis")

            guard_indicators()
            published = publication.publish(
                outputs,
                {
                "sensitivityMethod": method if "sensitivity" in outputs else None,
                "completionRate": completion_rate,
                "expected": len(expected_ids),
                "completed": len(completed_ids),
                "missing": len(missing_ids),
                "failed": len(failed_ids),
                "inferenceStatus": inference_status,
                },
                governed_report=build_governed_report,
                meta_config_sha256=meta_config_sha256,
                analysis_options={
                    "campaign": campaign,
                    "completenessThreshold": completeness_threshold,
                    "seedRole": seed_role,
                    "method": method,
                    "bootstrapResamples": bootstrap_resamples,
                    "confidenceLevel": confidence_level,
                    "convergenceCheckpoints": (
                        list(convergence_checkpoints)
                        if convergence_checkpoints is not None
                        else None
                    ),
                    "screeningTopK": screening_top_k,
                    "descriptiveOnly": descriptive_only,
                },
                numerical_engine=numerical_engine,
                plan=plan_capture,
                runs=run_capture,
                indicator_identities=indicator_identities,
                software_identity=analysis_software,
                indicator_guard=guard_indicators,
            )
            committed = published.paths
            return AnalyzeResult(
                path=committed["indicators"],
                dispersion_path=committed.get("dispersion"),
                sensitivity_method=method if "sensitivity" in outputs else None,
                sensitivity_path=committed.get("sensitivity"),
                runs=len(completed_ids),
                indicators=len(indicators),
                values=len(rows),
                completion_rate=completion_rate,
                expected=len(expected_ids),
                completed=len(completed_ids),
                missing=len(missing_ids),
                failed=len(failed_ids),
                convergence_path=committed.get("convergence"),
                rank_reversals_path=committed.get("rankReversals"),
                by_scramble_path=committed.get("byScramble"),
                diagnostics_path=committed.get("diagnostics"),
                noise_floor_path=committed.get("noiseFloor"),
                replication_allocation_path=committed.get("replicationAllocation"),
                inference_status=inference_status,
                output_summary_path=committed["outputSummary"],
                convergence_evidence_path=committed.get("convergenceEvidence"),
                governed_report_path=committed["governedReport"],
            )
        except BaseException:
            raise


def _current_indicator_identities(
    indicators: Sequence[Indicator],
) -> tuple[dict[str, object], ...]:
    return tuple(analysis_identity(indicator) for indicator in indicators)


def _validate_scientific_indicator_definitions(
    identities: Sequence[dict[str, object]], scientific_contract: dict
) -> None:
    for identity in identities:
        try:
            indicator_definition_sha256(identity)
        except ValueError as error:
            raise ValueError(
                f"current indicator {identity.get('name')!r} has incomplete scientific provenance"
            ) from error
    try:
        decision = scientific_contract["charter"]["decision"]
        primary_name = decision["primary_indicator"]
        sealed = decision["primary_indicator_definition"]
        sealed_sha256 = decision["primary_indicator_definition_sha256"]
    except (KeyError, TypeError) as error:
        raise ValueError("scientific plan has no sealed primary indicator definition") from error
    matches = [identity for identity in identities if identity.get("name") == primary_name]
    if len(matches) != 1:
        raise ValueError(
            "currently supplied indicators must contain the sealed primary indicator exactly once"
        )
    current = matches[0]
    try:
        current_sha256 = indicator_definition_sha256(current)
    except ValueError as error:
        raise ValueError("current primary indicator has incomplete scientific provenance") from error
    if current != sealed or current_sha256 != sealed_sha256:
        raise ValueError(
            "current primary indicator definition does not match the sealed scientific plan"
        )


def _validate_frozen_plan(
    plan: pa.Table,
    method: str,
    bootstrap_resamples: int | None,
    confidence_level: float | None,
    convergence_checkpoints: tuple[int, ...] | None,
    screening_top_k: int | None,
    structured_factors: dict[str, dict[str, list[str]]],
    descriptive_only: bool,
) -> dict | None:
    """Validate every sealed row before run-state or indicator access."""
    if method == "grid":
        _validate_grid_plan(plan, structured_factors)
        return None
    if method == "discrete_oat":
        _discrete_oat_effects(plan, [])
        return None
    if method == "canonical_morris":
        _canonical_morris_tables(
            plan,
            [],
            bootstrap_resamples,
            confidence_level,
            convergence_checkpoints,
            screening_top_k,
            include_noise_floor=False,
        )
        return json.loads(
            (plan.schema.metadata or {})[b"canonicalMorrisDesign"]
        )["scientificContract"]
    if method == "sobol":
        _sobol_ranking(
            plan,
            [],
            bootstrap_resamples,
            confidence_level,
            structured_factors,
        )
        return None
    _multiscramble_sobol_tables(
        plan,
        [],
        confidence_level,
        structured_factors,
    )
    return json.loads(
        (plan.schema.metadata or {})[b"multiScrambleSobolDesign"]
    )["scientificContract"]


def _current_numerical_engine(plan: pa.Table, method: str) -> dict | None:
    metadata = plan.schema.metadata or {}
    if (
        method == "canonical_morris"
        and metadata.get(b"canonicalMorrisDesignVersion") == b"3"
    ):
        return json.loads(metadata[b"canonicalMorrisDesign"])["numericalEngine"]
    if (
        method == "multiscramble_sobol"
        and metadata.get(b"multiScrambleSobolDesignVersion") == b"3"
    ):
        return json.loads(metadata[b"multiScrambleSobolDesign"])["numericalEngine"]
    return None


def _validate_grid_plan(
    plan: pa.Table, structured: dict[str, dict[str, list[str]]]
) -> None:
    fixed = ["designId", "method", "role", "runId", "seed", "contextHash"]
    if plan.column_names[: len(fixed)] != fixed or any(
        column.null_count for column in plan.columns
    ):
        raise ValueError("Grid plan schema, column order, or null values are invalid")
    expected_types = [pa.string()] * 4 + [pa.int64(), pa.string()]
    if any(
        plan.schema.field(name).type != kind
        for name, kind in zip(fixed, expected_types)
    ):
        raise ValueError("Grid fixed plan schema types are invalid")
    metadata = dict(plan.schema.metadata or {})
    required = {
        b"planSchemaVersion",
        b"canonicalizationVersion",
        b"contextDescriptor",
        b"outputsDeclaration",
        b"modelDocument",
    }
    if structured:
        required |= {b"structuredFactorsVersion", b"structuredFactors"}
    if (
        set(metadata) != required
        or metadata.get(b"canonicalizationVersion") != b"1"
        or metadata.get(b"planSchemaVersion")
        != (b"3" if structured else b"1")
    ):
        raise ValueError("Grid plan metadata is missing or unsupported")
    validate_outputs_declaration(metadata[b"outputsDeclaration"])
    validate_model_document(metadata[b"modelDocument"])
    try:
        descriptor = json.loads(metadata[b"contextDescriptor"])
    except Exception as error:
        raise ValueError("Grid contextDescriptor metadata is invalid") from error
    try:
        context_identity = ExecutionContextIdentity.from_descriptor(descriptor)
    except ValueError as error:
        raise ValueError(
            "Grid contextDescriptor must contain exactly four string keys"
        ) from error
    if metadata[b"contextDescriptor"] != context_identity.canonical_bytes:
        raise ValueError("Grid contextDescriptor metadata is not canonical")
    expected_context = context_identity.sha256
    design_ids = set()
    contexts = set()
    for row in plan.to_pylist():
        design_id = row["designId"]
        run_id = row["runId"]
        context_hash = row["contextHash"]
        if (
            re.fullmatch(r"[0-9a-f]{32}", design_id or "") is None
            or design_id in design_ids
        ):
            raise ValueError("Grid designId must be unique lowercase hexadecimal")
        design_ids.add(design_id)
        if (
            row["method"] != "grid"
            or row["role"] != "point"
            or re.fullmatch(r"[0-9a-f]{32}", run_id or "") is None
            or type(row["seed"]) is not int
            or not -(2**63) <= row["seed"] < 2**63
            or type(context_hash) is not str
            or not context_hash
        ):
            raise ValueError("Grid frozen row identity is invalid")
        if context_hash != expected_context:
            raise ValueError("Grid contextHash does not match sealed contextDescriptor")
        contexts.add(context_hash)
        for name in plan.column_names[len(fixed) :]:
            value = row[name]
            if type(value) not in (int, float, str) or (
                type(value) is float and not math.isfinite(value)
            ):
                raise ValueError(f"Grid factor {name!r} contains an invalid scalar")
    if len(contexts) != 1:
        raise ValueError("Grid plan contextHash values are inconsistent")


def _failure_ids(campaign_root: Path, incomplete: set[str]) -> set[str]:
    logs = campaign_root / "logs"
    if not logs.exists() and not platform.is_link(logs):
        return set()
    if platform.is_link(logs) or not logs.is_dir():
        raise ValueError("logs must be a regular non-symlink directory")
    failed = set()
    for run_id in incomplete:
        path = logs / f"{run_id}.log"
        if not path.exists() and not platform.is_link(path):
            continue
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
                raise ValueError("must be a non-empty regular file")
            descriptor = open_regular(path)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
                    raise ValueError("must be a non-empty regular file")
                if not os.read(descriptor, 1):
                    raise ValueError("must be readable and non-empty")
            finally:
                os.close(descriptor)
        except Exception as error:
            raise ValueError(f"invalid failure log {path}: {error}") from error
        failed.add(run_id)
    return failed


def _dispersion_table(plan: Path, rows: list[tuple[str, str, float]]) -> pa.Table:
    table = read_parquet(plan)
    fixed = {"designId", "method", "role", "runId", "seed", "contextHash"}
    parameters = tuple(sorted(set(table.column_names) - fixed))
    reserved = {"indicator", "n", "mean", "std", "cv", "ci95_low", "ci95_high"}
    collision = set(parameters) & reserved
    if collision:
        raise ValueError(
            f"plan parameters collide with dispersion columns: {sorted(collision)}"
        )
    configurations: dict[str, tuple] = {}
    for row in table.select(("runId", *parameters)).to_pylist():
        key = tuple(row[name] for name in parameters)
        previous = configurations.setdefault(row["runId"], key)
        if previous != key:
            raise ValueError(
                f"plan has inconsistent configuration for runId {row['runId']}"
            )
    groups: dict[tuple, list[float]] = {}
    for run_id, indicator, value in rows:
        groups.setdefault((*configurations[run_id], indicator), []).append(value)
    result = []
    for key in sorted(groups):
        values = groups[key]
        n = len(values)
        mean = statistics.fmean(values)
        std = statistics.stdev(values) if n > 1 else None
        margin = 1.96 * std / n**0.5 if std is not None else None
        if margin is not None and not math.isfinite(margin):
            margin = None
        cv = None if std is None or mean == 0 else std / abs(mean)
        if cv is not None and not math.isfinite(cv):
            cv = None
        low = None if margin is None else mean - margin
        high = None if margin is None else mean + margin
        if low is not None and not math.isfinite(low):
            low = None
        if high is not None and not math.isfinite(high):
            high = None
        result.append((*key, n, mean, std, cv, low, high))
    fields = [table.schema.field(name) for name in parameters] + [
        pa.field("indicator", pa.string(), nullable=False),
        pa.field("n", pa.int64(), nullable=False),
        pa.field("mean", pa.float64(), nullable=False),
        pa.field("std", pa.float64()),
        pa.field("cv", pa.float64()),
        pa.field("ci95_low", pa.float64()),
        pa.field("ci95_high", pa.float64()),
    ]
    schema = pa.schema(fields)
    return rows_to_table(result, schema)


def _validate_indicators(indicators: Sequence[Indicator]) -> None:
    names: set[str] = set()
    identities: set[object] = set()
    for indicator in indicators:
        if indicator.name in names:
            raise ValueError(f"duplicate indicator name: {indicator.name}")
        names.add(indicator.name)
        identity = getattr(indicator, "identity", None)
        if identity is not None:
            if identity in identities:
                raise ValueError(f"duplicate indicator identity: {identity!r}")
            identities.add(identity)
