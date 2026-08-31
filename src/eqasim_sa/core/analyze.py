import hashlib
import itertools
import json
import math
import os
import re
import stat
import statistics
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Sequence

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from scipy.stats import qmc, t as student_t

from eqasim_sa.core.analysis_publication import (
    AnalysisPublication,
    inspect_plan,
    inspect_runs,
    validate_descriptive_execution_evidence,
    validate_scientific_execution_evidence,
)
from eqasim_sa.core.durable_publication import write_atomic, write_parquet_atomic
from eqasim_sa.core.charter import (
    parse_stochastic,
    validate_scientific_contract,
    validate_scientific_stopping,
)
from eqasim_sa.core.indicators import (
    Indicator,
    analysis_identity,
    indicator_definition_sha256,
    verify_indicator_runtime_identity,
)
from eqasim_sa.core.ledger import campaign_lock
from eqasim_sa.core.plan import (
    CANONICAL_MORRIS_DOMAIN,
    DISCRETE_OAT_DOMAIN,
    MAX_CANONICAL_MORRIS_CANDIDATES,
    MAX_CANONICAL_MORRIS_DISTANCE_TERMS,
    MAX_MULTISCRAMBLE_DESIGN_POSITIONS,
    MAX_MULTISCRAMBLE_TABLE_CELLS,
    MULTISCRAMBLE_SOBOL_DOMAIN,
    _canonical_replication_seed,
    _discrete_uniform_generalized_inverse_index,
    _json,
    canonical_morris_distance_terms,
    validate_bootstrap_resamples,
    validate_canonical_morris_bootstrap_work,
    validate_morris_values,
    validate_multiscramble_scientific_contract,
    validate_sobol_bootstrap_work,
    validate_sobol_effective_hybrid_contrasts,
    validate_structured_factor_supports,
    validate_structured_plan_metadata,
    validate_structured_plan_rows,
)
from eqasim_sa.core.sensitivity import (
    engine_descriptor,
    morris_statistics,
    morris_unit_trajectories,
    sobol_symmetric_indices,
    sobol_unit_positions,
)

LEGACY_CANONICAL_MORRIS_DOMAIN = b"eqasim-sa:design:canonical-morris:v1\0"
LEGACY_MULTISCRAMBLE_SOBOL_DOMAIN = b"eqasim-sa:design:multiscramble-sobol:v1\0"
from eqasim_sa.core.software_identity import read_software_identity

BOOTSTRAP_DOMAIN = b"eqasim-sa:bootstrap:sobol:v1\0"
CANONICAL_MORRIS_BOOTSTRAP_DOMAIN = b"eqasim-sa:bootstrap:canonical-morris:v1\0"
SOBOL_DOMAIN = b"eqasim-sa:design:sobol:v1\0"


@dataclass(frozen=True)
class AnalyzeResult:
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
) -> AnalyzeResult:
    campaign_root = campaign_root.resolve(strict=True)
    with campaign_lock(campaign_root, "campaign"):
        return _analyze_locked(
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
        )


def _analyze_locked(
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
) -> AnalyzeResult:
    if type(descriptive_only) is not bool:
        raise ValueError("descriptive_only must be boolean")
    oat_methods = {"morris", "discrete_oat", "canonical_morris"}
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
    if plan.is_symlink() or not plan.is_file():
        raise ValueError("plan.parquet must be a regular non-symlink file")
    plan_inspection = inspect_plan(plan)
    plan_table = plan_inspection.table
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
            "morris",
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
        "morris",
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
    run_inspection = inspect_runs(
        campaign_root,
        plan_inspection,
        campaign,
        allow_legacy_unbound=descriptive_only,
    )
    inspected_expected = set(run_inspection.expected_ids)
    inspected_completed = set(run_inspection.completed_ids)
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
            run_inspection.execution_evidence,
            sealed_scientific_contract,
            allow_empty=descriptive_only,
        )
    elif descriptive_only:
        validate_descriptive_execution_evidence(run_inspection.execution_evidence)
    with AnalysisPublication(campaign_root) as publication:
        plan_capture = publication.capture_plan(plan, plan_inspection)
        run_capture = publication.capture_runs(
            plan_table,
            campaign,
            sealed_scientific_contract,
            allow_empty_scientific_evidence=descriptive_only,
            allow_legacy_unbound=descriptive_only,
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
        output = publication.stage_path("indicators.parquet")
        dispersion = publication.stage_path("replication-dispersion.parquet")
        morris_sensitivity = publication.stage_path("morris-ranking.parquet")
        discrete_oat_sensitivity = publication.stage_path("discrete-oat-effects.parquet")
        canonical_morris_sensitivity = publication.stage_path(
            "canonical-morris-ranking.parquet"
        )
        canonical_morris_convergence = publication.stage_path(
            "canonical-morris-convergence.parquet"
        )
        canonical_morris_reversals = publication.stage_path(
            "canonical-morris-rank-reversals.parquet"
        )
        noise_floor = publication.stage_path("noise-floor.parquet")
        replication_allocation = publication.stage_path("replication-allocation.json")
        sobol_sensitivity = publication.stage_path("sobol-indices.parquet")
        multiscramble_sensitivity = publication.stage_path(
            "multiscramble-sobol-indices.parquet"
        )
        multiscramble_by_scramble = publication.stage_path(
            "multiscramble-sobol-by-scramble.parquet"
        )
        multiscramble_convergence = publication.stage_path(
            "multiscramble-sobol-convergence.parquet"
        )
        multiscramble_diagnostics = publication.stage_path(
            "multiscramble-sobol-diagnostics.parquet"
        )
        output_summary = publication.stage_path("output-summary.parquet")
        convergence_evidence = publication.stage_path("convergence-evidence.parquet")
        governed_report = publication.stage_path("governed-report.json")
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
            destination = write_parquet_atomic(table, output)
            output_summary_path = write_parquet_atomic(
                _output_summary_table(
                    plan_table, rows, tuple(indicator.name for indicator in indicators)
                ),
                output_summary,
            )
            dispersion_path = None
            if seed_role == "replication":
                dispersion_table = _dispersion_table(plan_capture.path, rows)
                dispersion_path = write_parquet_atomic(dispersion_table, dispersion)
            sensitivity_path = None
            convergence_path = None
            rank_reversals_path = None
            by_scramble_path = None
            diagnostics_path = None
            noise_floor_path = None
            replication_allocation_path = None
            inference_status = None
            scientific_contract = sealed_scientific_contract
            sensitivity_table = None
            convergence_table = None
            if not descriptive_only:
                if method == "morris":
                    sensitivity_path = write_parquet_atomic(
                        _morris_ranking(plan_table, rows), morris_sensitivity
                    )
                elif method == "discrete_oat":
                    sensitivity_path = write_parquet_atomic(
                        _discrete_oat_effects(plan_table, rows),
                        discrete_oat_sensitivity,
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
                    sensitivity_path = write_parquet_atomic(
                        ranking_table, canonical_morris_sensitivity
                    )
                    convergence_path = write_parquet_atomic(
                        convergence_table, canonical_morris_convergence
                    )
                    rank_reversals_path = write_parquet_atomic(
                        reversal_table, canonical_morris_reversals
                    )
                    noise_floor_path = write_parquet_atomic(noise_table, noise_floor)
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
                        write_atomic(
                            replication_allocation,
                            lambda path: path.write_bytes(encoded_recommendation),
                        )
                        replication_allocation_path = replication_allocation
                elif method == "sobol":
                    sensitivity_path = write_parquet_atomic(
                        _sobol_ranking(
                            plan_table,
                            rows,
                            bootstrap_resamples,
                            confidence_level,
                            structured_factors,
                        ),
                        sobol_sensitivity,
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
                    sensitivity_path = write_parquet_atomic(
                        indices_table, multiscramble_sensitivity
                    )
                    by_scramble_path = write_parquet_atomic(
                        by_scramble_table, multiscramble_by_scramble
                    )
                    convergence_path = write_parquet_atomic(
                        convergence_table, multiscramble_convergence
                    )
                    diagnostics_path = write_parquet_atomic(
                        diagnostics_table, multiscramble_diagnostics
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
            elif method == "sobol":
                inference_status = "legacy_noninferential"
            elif method not in {"canonical_morris", "multiscramble_sobol"}:
                inference_status = "demonstration_noninferential"
            convergence_evidence_path = None
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
                convergence_evidence_path = write_parquet_atomic(
                    evidence_table, convergence_evidence
                )
                if inference_status != "pilot_only":
                    inference_status = "conditional" if converged else "unstable"
            report = _governed_report(
                method=method,
                inference_status=inference_status,
                completion_rate=completion_rate,
                expected=len(expected_ids),
                completed=len(completed_ids),
                missing=len(missing_ids),
                failed=len(failed_ids),
                output_summary_path=output_summary_path,
                convergence_evidence_path=convergence_evidence_path,
                sensitivity_path=sensitivity_path,
                scientific_contract=scientific_contract,
                sensitivity_table=sensitivity_table,
                converged=converged,
                criterion_reports=criterion_reports,
            )
            encoded_report = json.dumps(
                report, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
            write_atomic(governed_report, lambda path: path.write_bytes(encoded_report))
            governed_report_path = governed_report
            outputs = {
                "indicators": destination,
                "outputSummary": output_summary_path,
                "governedReport": governed_report_path,
            }
            optional_outputs = {
                "dispersion": dispersion_path,
                "sensitivity": sensitivity_path,
                "convergence": convergence_path,
                "rankReversals": rank_reversals_path,
                "byScramble": by_scramble_path,
                "diagnostics": diagnostics_path,
                "noiseFloor": noise_floor_path,
                "replicationAllocation": replication_allocation_path,
                "convergenceEvidence": convergence_evidence_path,
            }
            outputs.update(
                {
                    name: path
                    for name, path in optional_outputs.items()
                    if path is not None
                }
            )
            def guard_indicators() -> None:
                if tuple(_current_indicator_identities(indicators)) != indicator_identities:
                    raise RuntimeError("indicator implementation changed during analysis")

            guard_indicators()
            published = publication.publish(
                outputs,
                {
                "sensitivityMethod": method if sensitivity_path else None,
                "completionRate": completion_rate,
                "expected": len(expected_ids),
                "completed": len(completed_ids),
                "missing": len(missing_ids),
                "failed": len(failed_ids),
                "inferenceStatus": inference_status,
                },
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
                sensitivity_method=method if sensitivity_path else None,
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
    identities = []
    for indicator in indicators:
        verify_indicator_runtime_identity(indicator)
        identities.append(analysis_identity(indicator))
    return tuple(identities)


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
    metadata = plan.schema.metadata or {}
    legacy = [
        (legacy_method, metadata[key])
        for legacy_method, key in (
            ("canonical_morris", b"canonicalMorrisDesignVersion"),
            ("multiscramble_sobol", b"multiScrambleSobolDesignVersion"),
        )
        if metadata.get(key) in {b"1", b"2"}
    ]
    if len(legacy) > 1:
        raise ValueError("plan declares multiple legacy scientific methods")
    if legacy:
        legacy_method, legacy_version_bytes = legacy[0]
        legacy_version = int(legacy_version_bytes)
        if method != legacy_method:
            raise ValueError(
                f"legacy {legacy_method} plan does not match requested method {method}"
            )
        if not descriptive_only:
            raise ValueError(
                f"{method} design v{legacy_version} is legacy and cannot support inference; "
                "replan the campaign with the current SALib design v3"
            )
        _validate_legacy_scientific_plan(
            plan,
            method,
            bootstrap_resamples,
            confidence_level,
            convergence_checkpoints,
            screening_top_k,
            structured_factors,
            legacy_version,
        )
        return None
    if method == "grid":
        _validate_grid_plan(plan, structured_factors)
        return None
    if method == "morris":
        _morris_ranking(plan, [])
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


def _legacy_scientific_metadata(
    plan: pa.Table,
    method: str,
    structured: dict[str, dict[str, list[str]]],
    version: int,
) -> tuple[dict, dict, str]:
    canonical = method == "canonical_morris"
    label = "canonical Morris" if canonical else "multi-scramble Sobol"
    version_key = (
        b"canonicalMorrisDesignVersion"
        if canonical
        else b"multiScrambleSobolDesignVersion"
    )
    design_key = (
        b"canonicalMorrisDesign" if canonical else b"multiScrambleSobolDesign"
    )
    required = {
        b"planSchemaVersion",
        b"canonicalizationVersion",
        b"contextDescriptor",
        version_key,
        design_key,
    }
    if structured:
        required |= {b"structuredFactorsVersion", b"structuredFactors"}
    metadata = plan.schema.metadata or {}
    expected_schema_version = (
        b"2" if version == 1 or canonical or not structured else b"3"
    )
    if (
        version not in {1, 2}
        or version == 1 and structured
        or canonical and structured
        or set(metadata) != required
        or metadata.get(b"planSchemaVersion") != expected_schema_version
        or metadata.get(b"canonicalizationVersion") != b"1"
        or metadata.get(version_key) != str(version).encode()
    ):
        raise ValueError(
            f"legacy {label} v{version} plan metadata is missing or unsupported"
        )
    try:
        design = json.loads(metadata[design_key])
        descriptor = json.loads(metadata[b"contextDescriptor"])
        design_bytes = _json(design)
        descriptor_bytes = _json(descriptor)
    except Exception as error:
        raise ValueError(f"legacy {label} v{version} metadata is invalid") from error
    if metadata[design_key] != design_bytes or metadata[b"contextDescriptor"] != descriptor_bytes:
        raise ValueError(f"legacy {label} v{version} metadata is not canonical")
    if (
        not isinstance(descriptor, dict)
        or set(descriptor)
        != {
            "eqasimCommit",
            "dataManifestSha256",
            "lockfileSha256",
            "eqasimSaVersion",
        }
        or any(type(value) is not str for value in descriptor.values())
    ):
        raise ValueError(
            f"legacy {label} v{version} contextDescriptor must contain exactly four string keys"
        )
    context_hash = hashlib.sha256(
        b"eqasim-sa:context:v1\0" + descriptor_bytes
    ).hexdigest()
    return design, descriptor, context_hash


def _legacy_support(
    name: object,
    support: object,
    supports: dict[str, list],
    *,
    label: str,
) -> pa.DataType:
    if (
        type(name) is not str
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
        or name in supports
        or name in {"designId", "method", "role", "runId", "contextHash"}
        or not isinstance(support, list)
        or len(support) < 2
    ):
        raise ValueError(f"legacy {label} v2 factor support is invalid")
    kind = type(support[0])
    if (
        kind not in (int, float, str)
        or any(type(value) is not kind for value in support)
        or len(set(support)) != len(support)
        or kind is int
        and any(not -(2**63) <= value < 2**63 for value in support)
        or kind is float
        and any(not math.isfinite(value) for value in support)
        or kind is str
        and any(not value or "\0" in value for value in support)
    ):
        raise ValueError(f"legacy {label} v2 supports must contain distinct finite scalars")
    supports[name] = support
    return {int: pa.int64(), float: pa.float64(), str: pa.string()}[kind]


def _validate_legacy_plan_schema(
    plan: pa.Table,
    supports: dict[str, list],
    support_types: dict[str, pa.DataType],
    *,
    seed_is_factor: bool,
    label: str,
) -> tuple[str, ...]:
    names = tuple(supports)
    columns = tuple(name for name in names if not seed_is_factor or name != "seed")
    expected = pa.schema(
        [
            *(
                pa.field(name, pa.string(), False)
                for name in ("designId", "method", "role", "runId")
            ),
            pa.field("seed", pa.int64(), False),
            pa.field("contextHash", pa.string(), False),
            *(pa.field(name, support_types[name], False) for name in columns),
        ]
    )
    if (
        plan.column_names != expected.names
        or not plan.schema.remove_metadata().equals(expected)
        or any(column.null_count for column in plan.columns)
    ):
        raise ValueError(f"legacy {label} v2 plan schema does not match sealed supports")
    return names


def _legacy_v1_text(value: object, label: str) -> str:
    if type(value) is not str or not value or "\0" in value:
        raise ValueError(f"legacy scientific v1 {label} must be non-empty text without NUL")
    return value


def _validate_legacy_v1_scientific_contract(
    contract: object,
    supports: dict[str, list],
    distributions: dict[str, dict],
    *,
    method: str,
) -> None:
    try:
        _json(contract)
    except Exception as error:
        raise ValueError("legacy scientific v1 contract must be JSON-safe") from error
    if not isinstance(contract, dict) or set(contract) != {"charter", "parameters"}:
        raise ValueError("legacy scientific v1 contract keys are invalid")
    charter = contract["charter"]
    if not isinstance(charter, dict) or set(charter) != {
        "status",
        "stochastic",
        "decision",
        "uncertainty",
        "budget",
        "stopping",
        "robustness",
    } or charter["status"] != "scientific":
        raise ValueError("legacy scientific v1 charter is invalid")

    try:
        parse_stochastic(charter["stochastic"], "legacy scientific v1 stochastic")
    except (TypeError, ValueError) as error:
        raise ValueError("legacy scientific v1 stochastic contract is invalid") from error
    expected_target = (
        "conditional_mean" if method == "canonical_morris" else "finite_seed_factor"
    )
    if charter["stochastic"]["target"] != expected_target:
        raise ValueError("legacy scientific v1 stochastic target is invalid")

    decision = charter["decision"]
    decision_keys = {
        "claim",
        "use",
        "setting",
        "objective",
        "estimand",
        "primary_indicator",
        "practical_tolerance",
    }
    if not isinstance(decision, dict) or set(decision) != decision_keys:
        raise ValueError("legacy scientific v1 decision keys are invalid")
    for key in ("claim", "setting", "estimand", "primary_indicator"):
        _legacy_v1_text(decision[key], f"decision {key}")
    allowed_objectives = {"screening"} if method == "canonical_morris" else {"fp", "ff"}
    if decision["use"] not in {"diagnostic", "prognostic"}:
        raise ValueError("legacy scientific v1 decision use is invalid")
    if decision["objective"] not in allowed_objectives:
        raise ValueError("legacy scientific v1 decision objective is invalid")
    tolerance = decision["practical_tolerance"]
    if decision["objective"] == "ff":
        if (
            type(tolerance) is not float
            or not math.isfinite(tolerance)
            or not 0 <= tolerance <= 1
        ):
            raise ValueError("legacy scientific v1 factor-fixing tolerance is invalid")
    elif tolerance is not None:
        raise ValueError("legacy scientific v1 tolerance must be null outside factor fixing")

    uncertainty = charter["uncertainty"]
    if not isinstance(uncertainty, dict) or set(uncertainty) != {
        "dependence",
        "inventory",
    }:
        raise ValueError("legacy scientific v1 uncertainty is invalid")
    dependence = uncertainty["dependence"]
    if (
        not isinstance(dependence, dict)
        or set(dependence) != {"kind", "rationale", "groups"}
        or dependence["kind"] != "independent"
        or dependence["groups"] != []
    ):
        raise ValueError("legacy scientific v1 dependence is invalid")
    _legacy_v1_text(dependence["rationale"], "dependence rationale")

    categories = {"parameters", "data", "structures", "scenarios", "numerics"}
    inventory = uncertainty["inventory"]
    if not isinstance(inventory, dict) or set(inventory) != categories:
        raise ValueError("legacy scientific v1 uncertainty inventory is invalid")
    varied_parameters = set()
    for category in categories:
        items = inventory[category]
        if not isinstance(items, list):
            raise ValueError("legacy scientific v1 inventory categories must be lists")
        names = set()
        for item in items:
            if not isinstance(item, dict) or set(item) != {
                "name",
                "status",
                "provenance",
                "rationale",
            }:
                raise ValueError("legacy scientific v1 inventory item is invalid")
            name = _legacy_v1_text(item["name"], "inventory name")
            if name in names or item["status"] not in {"varied", "fixed", "excluded"}:
                raise ValueError("legacy scientific v1 inventory identity or status is invalid")
            names.add(name)
            _legacy_v1_text(item["provenance"], "inventory provenance")
            _legacy_v1_text(item["rationale"], "inventory rationale")
            if category == "parameters" and item["status"] == "varied":
                varied_parameters.add(name)
    if varied_parameters != set(supports):
        raise ValueError("legacy scientific v1 varied parameters do not match factors")

    budget = charter["budget"]
    if (
        not isinstance(budget, dict)
        or set(budget) != {"maximum_runs", "reserve_fraction"}
        or type(budget["maximum_runs"]) is not int
        or not 0 < budget["maximum_runs"] < 2**63
        or type(budget["reserve_fraction"]) is not float
        or not math.isfinite(budget["reserve_fraction"])
        or not 0 <= budget["reserve_fraction"] < 1
    ):
        raise ValueError("legacy scientific v1 budget is invalid")
    stopping = charter["stopping"]
    if not isinstance(stopping, list) or not stopping:
        raise ValueError("legacy scientific v1 stopping criteria are invalid")
    metrics = set()
    for criterion in stopping:
        if (
            not isinstance(criterion, dict)
            or set(criterion) != {"metric", "threshold", "consecutive_checks"}
            or type(criterion["threshold"]) not in (int, float)
            or not math.isfinite(criterion["threshold"])
            or criterion["threshold"] <= 0
            or type(criterion["consecutive_checks"]) is not int
            or criterion["consecutive_checks"] <= 0
        ):
            raise ValueError("legacy scientific v1 stopping criterion is invalid")
        metric = _legacy_v1_text(criterion["metric"], "stopping metric")
        if metric in metrics:
            raise ValueError("legacy scientific v1 stopping metrics must be unique")
        metrics.add(metric)
    robustness = charter["robustness"]
    if not isinstance(robustness, list) or not robustness:
        raise ValueError("legacy scientific v1 robustness checks are invalid")
    checks = set()
    for check in robustness:
        if not isinstance(check, dict) or set(check) != {"name", "rationale"}:
            raise ValueError("legacy scientific v1 robustness check is invalid")
        name = _legacy_v1_text(check["name"], "robustness name")
        if name in checks:
            raise ValueError("legacy scientific v1 robustness names must be unique")
        checks.add(name)
        _legacy_v1_text(check["rationale"], "robustness rationale")

    expected_parameters = [
        {
            "name": name,
            "support": supports[name],
            "distribution": distributions[name],
        }
        for name in sorted(supports)
    ]
    if contract["parameters"] != expected_parameters:
        raise ValueError("legacy scientific v1 parameters do not match sealed factors")


def _validate_legacy_scientific_contract(
    design: dict,
    descriptor: dict,
    supports: dict[str, list],
    distributions: dict[str, dict],
    *,
    method: str,
    checkpoints: int,
    version: int,
) -> None:
    if version == 1:
        _validate_legacy_v1_scientific_contract(
            design["scientificContract"],
            supports,
            distributions,
            method=method,
        )
        return
    if method == "canonical_morris":
        validate_scientific_contract(
            design["scientificContract"], supports, distributions
        )
    else:
        validate_multiscramble_scientific_contract(
            design["scientificContract"], supports, distributions
        )
    contract = design["scientificContract"]
    if contract["software"]["eqasimSaVersion"] != descriptor["eqasimSaVersion"]:
        raise ValueError(
            "legacy scientific v2 software version does not match contextDescriptor"
        )
    validate_scientific_stopping(
        method, contract["charter"]["stopping"], checkpoints
    )


def _legacy_canonical_candidate(
    code: int, dimensions: int, levels: int
) -> list[list[int]]:
    choices = []
    for _ in range(dimensions):
        code, digit = divmod(code, levels)
        edge = digit // 2
        choices.append((edge, 1 if digit % 2 == 0 else -1))
    available, permutation = list(range(dimensions)), []
    for remaining in range(dimensions, 0, -1):
        block = math.factorial(remaining - 1)
        index, code = divmod(code, block)
        permutation.append(available.pop(index))
    half = levels // 2
    point = [edge if direction == 1 else edge + half for edge, direction in choices]
    matrix = [list(point)]
    for factor in permutation:
        point = list(point)
        point[factor] += choices[factor][1] * half
        matrix.append(point)
    return matrix


def _legacy_trajectory_distance(
    left: list[list[int]], right: list[list[int]]
) -> int:
    return sum(
        (left_value - right_value) ** 2
        for left_point in left
        for right_point in right
        for left_value, right_value in zip(left_point, right_point)
    )


def _legacy_canonical_selection(
    candidate_definition: dict, trajectories: int
) -> tuple[list[tuple[str, list[list[int]]]], list[tuple[str, list[list[int]]]]]:
    dimensions = len(candidate_definition["factors"])
    levels = candidate_definition["levels"]
    encoded = _json(candidate_definition)
    universe = math.factorial(dimensions) * levels**dimensions
    offset = int.from_bytes(
        hashlib.sha256(
            LEGACY_CANONICAL_MORRIS_DOMAIN + encoded + b"\0offset"
        ).digest(),
        "big",
    ) % universe
    stride = int.from_bytes(
        hashlib.sha256(
            LEGACY_CANONICAL_MORRIS_DOMAIN + encoded + b"\0stride"
        ).digest(),
        "big",
    ) % universe or 1
    while math.gcd(stride, universe) != 1:
        stride = (stride + 1) % universe or 1
    pool = []
    for index in range(candidate_definition["candidateTrajectories"]):
        matrix = _legacy_canonical_candidate(
            (offset + index * stride) % universe, dimensions, levels
        )
        pool.append((hashlib.sha256(_json(matrix)).hexdigest(), matrix))
    by_identity = dict(pool)
    if len(by_identity) != len(pool):
        raise ValueError("legacy canonical Morris candidate identities collide")
    best_pair = None
    best_distance = -1
    for pair in itertools.combinations(sorted(by_identity), 2):
        distance = _legacy_trajectory_distance(
            by_identity[pair[0]], by_identity[pair[1]]
        )
        if distance > best_distance:
            best_pair, best_distance = pair, distance
    if best_pair is None:
        raise ValueError("legacy canonical Morris selection requires two candidates")
    selected_ids = list(best_pair)
    while len(selected_ids) < trajectories:
        best_candidate = None
        best_score = -1
        for candidate in sorted(set(by_identity) - set(selected_ids)):
            score = sum(
                _legacy_trajectory_distance(
                    by_identity[candidate], by_identity[selected]
                )
                for selected in selected_ids
            )
            if score > best_score:
                best_candidate, best_score = candidate, score
        if best_candidate is None:
            raise ValueError("legacy canonical Morris candidate pool was exhausted")
        selected_ids.append(best_candidate)
    return pool, [(identity, by_identity[identity]) for identity in selected_ids]


def _validate_legacy_canonical_morris(
    plan: pa.Table,
    design: dict,
    descriptor: dict,
    context_hash: str,
    bootstrap_resamples: int | None,
    confidence_level: float | None,
    convergence_checkpoints: tuple[int, ...] | None,
    screening_top_k: int | None,
    version: int,
) -> None:
    expected_keys = {
        "bootstrapResamples",
        "candidateDefinition",
        "candidatePoolIdentities",
        "confidenceLevel",
        "convergenceCheckpoints",
        "delta",
        "distributions",
        "factors",
        "inverseCdfMap",
        "p",
        "replicationBaseSeeds",
        "scientificContract",
        "screeningTopK",
        "seedDerivation",
        "selectedTrajectories",
        "selectionAlgorithm",
        "selectionVersion",
        "trajectories",
    }
    if not isinstance(design, dict) or set(design) != expected_keys:
        raise ValueError("legacy canonical Morris v2 design metadata keys are invalid")
    factors, sealed_distributions = design["factors"], design["distributions"]
    if (
        not isinstance(factors, list)
        or not factors
        or not isinstance(sealed_distributions, list)
    ):
        raise ValueError("legacy canonical Morris v2 factors or distributions are invalid")
    supports: dict[str, list] = {}
    support_types: dict[str, pa.DataType] = {}
    for factor in factors:
        if not isinstance(factor, dict) or set(factor) != {"name", "support"}:
            raise ValueError("legacy canonical Morris v2 factor definition is invalid")
        name = factor["name"]
        support_types[name] = _legacy_support(
            name, factor["support"], supports, label="canonical Morris"
        )
    names = tuple(supports)
    if names != tuple(sorted(names)) or "seed" in supports:
        raise ValueError("legacy canonical Morris v2 factors must be sorted")
    distributions = {}
    for distribution in sealed_distributions:
        if (
            not isinstance(distribution, dict)
            or set(distribution) != {"name", "family", "transform", "provenance"}
            or distribution.get("name") in distributions
            or distribution.get("family") != "discrete_uniform"
            or distribution.get("transform") != "identity"
            or type(distribution.get("provenance")) is not str
            or not distribution["provenance"]
        ):
            raise ValueError("legacy canonical Morris v2 distribution is invalid")
        distributions[distribution["name"]] = {
            key: distribution[key] for key in ("family", "transform", "provenance")
        }
    if tuple(distributions) != names:
        raise ValueError(
            "legacy canonical Morris v2 distributions must match sorted factors"
        )

    p, trajectories = design["p"], design["trajectories"]
    candidate = design["candidateDefinition"]
    candidate_count = (
        candidate.get("candidateTrajectories") if isinstance(candidate, dict) else None
    )
    candidate_seed = candidate.get("designSeed") if isinstance(candidate, dict) else None
    if (
        not isinstance(candidate, dict)
        or set(candidate)
        != {"designSeed", "factors", "levels", "candidateTrajectories"}
        or candidate
        != {
            "designSeed": candidate_seed,
            "factors": [
                {
                    "name": name,
                    "support": supports[name],
                    "distribution": distributions[name],
                }
                for name in names
            ],
            "levels": p,
            "candidateTrajectories": candidate_count,
        }
        or type(candidate_seed) is not int
        or not -(2**63) <= candidate_seed < 2**63
        or type(p) is not int
        or p < 4
        or p % 2
        or type(trajectories) is not int
        or trajectories < 2
        or type(candidate_count) is not int
        or not trajectories <= candidate_count
        <= (
            MAX_CANONICAL_MORRIS_CANDIDATES
            if len(names) >= 4
            else min(
                math.factorial(len(names)) * p ** len(names),
                MAX_CANONICAL_MORRIS_CANDIDATES,
            )
        )
    ):
        raise ValueError("legacy canonical Morris v2 candidate definition is invalid")
    if canonical_morris_distance_terms(len(names), candidate_count, trajectories) > (
        MAX_CANONICAL_MORRIS_DISTANCE_TERMS
    ):
        raise ValueError("legacy canonical Morris v2 candidate definition is too large")

    base_seeds, checkpoints = (
        design["replicationBaseSeeds"],
        design["convergenceCheckpoints"],
    )
    validate_bootstrap_resamples(
        design["bootstrapResamples"], method="legacy canonical Morris v2", minimum=100
    )
    if (
        not isinstance(base_seeds, list)
        or not base_seeds
        or any(type(seed) is not int or not -(2**63) <= seed < 2**63 for seed in base_seeds)
        or len(set(base_seeds)) != len(base_seeds)
        or type(design["confidenceLevel"]) is not float
        or not math.isfinite(design["confidenceLevel"])
        or not 0 < design["confidenceLevel"] < 1
        or not isinstance(checkpoints, list)
        or not checkpoints
        or any(type(value) is not int or value < 2 for value in checkpoints)
        or sorted(set(checkpoints)) != checkpoints
        or checkpoints[-1] != trajectories
        or type(design["screeningTopK"]) is not int
        or not 1 <= design["screeningTopK"] <= len(names)
        or bootstrap_resamples != design["bootstrapResamples"]
        or confidence_level != design["confidenceLevel"]
        or convergence_checkpoints is None
        or list(convergence_checkpoints) != checkpoints
        or screening_top_k != design["screeningTopK"]
    ):
        raise ValueError("legacy canonical Morris v2 sealed analysis settings are invalid")
    if (
        design["delta"] != {"numerator": p // 2, "denominator": p - 1}
        or design["inverseCdfMap"] != "discrete-uniform-generalized-inverse-v1"
        or design["selectionAlgorithm"]
        != "greedy-sum-pairwise-squared-distance"
        or design["selectionVersion"] != 1
        or design["seedDerivation"]
        != {
            "algorithm": "sha256-first-64-bits-signed",
            "domain": "eqasim-sa:canonical-morris-seed:v1",
            "fields": [
                "version",
                "designSeed",
                "baseSeed",
                "trajectoryId",
                "replicationIndex",
            ],
            "version": 1,
        }
    ):
        raise ValueError("legacy canonical Morris v2 algorithm metadata is invalid")
    _validate_legacy_scientific_contract(
        design,
        descriptor,
        supports,
        distributions,
        method="canonical_morris",
        checkpoints=len(checkpoints),
        version=version,
    )
    budget = design["scientificContract"]["charter"]["budget"]
    positions = trajectories * (len(names) + 1) * len(base_seeds)
    available = int(
        Decimal(budget["maximum_runs"])
        * (Decimal(1) - Decimal(str(budget["reserve_fraction"])))
    )
    if (
        positions > available
        or positions > MAX_MULTISCRAMBLE_DESIGN_POSITIONS
        or positions * (len(names) + 6) > MAX_MULTISCRAMBLE_TABLE_CELLS
    ):
        raise ValueError("legacy canonical Morris plan exceeds its budget or safe cap")
    stochastic = design["scientificContract"]["charter"]["stochastic"]
    if stochastic["target"] != "conditional_mean":
        raise ValueError("legacy canonical Morris requires conditional_mean")
    allocation = stochastic["allocation"]
    if (
        allocation["phase"] == "pilot"
        and len(base_seeds) < allocation["minimum_replications"]
        or allocation["phase"] == "production"
        and len(base_seeds) != allocation["selected_replications"]
    ):
        raise ValueError("legacy canonical Morris replication allocation is invalid")

    pool = design["candidatePoolIdentities"]
    selected = design["selectedTrajectories"]
    if (
        not isinstance(pool, list)
        or len(pool) != candidate_count
        or any(type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in pool)
        or len(set(pool)) != len(pool)
        or not isinstance(selected, list)
        or len(selected) != trajectories
    ):
        raise ValueError("legacy canonical Morris v2 candidate identities are invalid")
    expected_pool, expected_selected = _legacy_canonical_selection(
        candidate, trajectories
    )
    if pool != [identity for identity, _ in expected_pool] or selected != [
        {"candidateId": identity, "matrix": matrix, "selectionOrder": index}
        for index, (identity, matrix) in enumerate(expected_selected)
    ]:
        raise ValueError("legacy canonical Morris v2 selection is not reproducible")
    expected_locations = {}
    derived_seeds = set()
    half = p // 2
    for trajectory, item in enumerate(selected):
        if (
            not isinstance(item, dict)
            or set(item) != {"candidateId", "matrix", "selectionOrder"}
            or item["selectionOrder"] != trajectory
            or item["candidateId"] not in pool
            or hashlib.sha256(_json(item["matrix"])).hexdigest() != item["candidateId"]
        ):
            raise ValueError("legacy canonical Morris v2 selected trajectory is invalid")
        matrix = item["matrix"]
        if (
            not isinstance(matrix, list)
            or len(matrix) != len(names) + 1
            or any(
                not isinstance(point, list)
                or len(point) != len(names)
                or any(type(value) is not int or not 0 <= value < p for value in point)
                for point in matrix
            )
        ):
            raise ValueError("legacy canonical Morris v2 trajectory matrix is invalid")
        changed_factors = []
        for before, after in zip(matrix, matrix[1:]):
            changed = [
                index
                for index, (left, right) in enumerate(zip(before, after))
                if left != right
            ]
            if len(changed) != 1 or after[changed[0]] - before[changed[0]] not in {-half, half}:
                raise ValueError("legacy canonical Morris v2 unit step is invalid")
            changed_factors.append(changed[0])
        if sorted(changed_factors) != list(range(len(names))):
            raise ValueError("legacy canonical Morris v2 trajectory must change every factor")
        mapped = [
            tuple(
                supports[name][
                    _discrete_uniform_generalized_inverse_index(
                        len(supports[name]), p, grid_index
                    )
                ]
                for name, grid_index in zip(names, point)
            )
            for point in matrix
        ]
        for rank, (before, after) in enumerate(zip(mapped, mapped[1:]), 1):
            changed = [index for index, values in enumerate(zip(before, after)) if values[0] != values[1]]
            if changed != [changed_factors[rank - 1]]:
                raise ValueError("legacy canonical Morris v2 mapped step is invalid")
        for replication, base_seed in enumerate(base_seeds):
            # v2 derives from the declared base seed, not from the active design engine.
            seed = _canonical_replication_seed(
                candidate_seed, base_seed, item["candidateId"], replication
            )
            if seed in derived_seeds:
                raise ValueError("legacy canonical Morris v2 derived seeds collide")
            derived_seeds.add(seed)
            for rank, values in enumerate(mapped):
                role = (
                    f"trajectory:{trajectory}:rank:0:start"
                    if rank == 0
                    else f"trajectory:{trajectory}:rank:{rank}:factor:{names[changed_factors[rank - 1]]}"
                )
                design_id = hashlib.sha256(
                    LEGACY_CANONICAL_MORRIS_DOMAIN
                    + _json(
                        {
                            "design": design,
                            "replicationIndex": replication,
                            "role": role,
                        }
                    )
                ).hexdigest()[:32]
                expected_locations[role, seed] = values, design_id

    _validate_legacy_plan_schema(
        plan,
        supports,
        support_types,
        seed_is_factor=False,
        label="canonical Morris",
    )
    if len(plan) != len(expected_locations):
        raise ValueError("legacy canonical Morris v2 plan cardinality is inconsistent")
    locations, design_ids, config_runs, run_configs = set(), set(), {}, {}
    for row in plan.to_pylist():
        key = row["role"], row["seed"]
        actual = tuple(row[name] for name in names)
        design_id, run_id = row["designId"], row["runId"]
        configuration = (*actual, row["seed"])
        expected_values, expected_design_id = expected_locations.get(
            key, (None, None)
        )
        if (
            key in locations
            or key not in expected_locations
            or row["method"] != "canonical_morris"
            or row["contextHash"] != context_hash
            or actual != expected_values
            or design_id != expected_design_id
            or design_id in design_ids
            or re.fullmatch(r"[0-9a-f]{32}", run_id or "") is None
            or config_runs.setdefault(configuration, run_id) != run_id
            or run_configs.setdefault(run_id, configuration) != configuration
        ):
            raise ValueError("legacy canonical Morris v2 sealed row identity is invalid")
        locations.add(key)
        design_ids.add(design_id)
    if locations != set(expected_locations):
        raise ValueError("legacy canonical Morris v2 plan rows are incomplete")


def _validate_legacy_multiscramble_sobol(
    plan: pa.Table,
    design: dict,
    descriptor: dict,
    context_hash: str,
    confidence_level: float | None,
    structured: dict[str, dict[str, list[str]]],
    version: int,
) -> None:
    keys = {
        "baseSampleSizes",
        "confidenceLevel",
        "factors",
        "mapping",
        "maximumBaseSampleSize",
        "qmc",
        "saltelliEstimator",
        "scientificContract",
        "scrambleSeeds",
    }
    if structured:
        keys.add("structuredFactors")
    if not isinstance(design, dict) or set(design) != keys:
        raise ValueError("legacy multi-scramble Sobol v2 design metadata keys are invalid")
    if structured and design["structuredFactors"] != structured:
        raise ValueError("legacy multi-scramble Sobol v2 structured factors are inconsistent")
    sizes, scramble_seeds = design["baseSampleSizes"], design["scrambleSeeds"]
    if (
        not isinstance(sizes, list)
        or not sizes
        or any(type(value) is not int or value < 2 or value & (value - 1) for value in sizes)
        or sorted(set(sizes)) != sizes
        or any(right != 2 * left for left, right in zip(sizes, sizes[1:]))
        or design["maximumBaseSampleSize"] != sizes[-1]
        or not isinstance(scramble_seeds, list)
        or len(scramble_seeds) < 4
        or any(type(seed) is not int or not 0 <= seed < 2**64 for seed in scramble_seeds)
        or len(set(scramble_seeds)) != len(scramble_seeds)
        or design["mapping"] != "equal-width-v1"
        or design["qmc"]
        != {
            "bits": 64,
            "engine": "scipy.stats.qmc.Sobol",
            "optimization": None,
            "scramble": True,
        }
        or design["saltelliEstimator"] != "symmetric-ab-ba-v1"
        or type(design["confidenceLevel"]) is not float
        or not math.isfinite(design["confidenceLevel"])
        or not 0 < design["confidenceLevel"] < 1
        or confidence_level != design["confidenceLevel"]
    ):
        raise ValueError("legacy multi-scramble Sobol v2 sealed settings are invalid")
    factors = design["factors"]
    if not isinstance(factors, list):
        raise ValueError("legacy multi-scramble Sobol v2 factors are invalid")
    supports: dict[str, list] = {}
    support_types: dict[str, pa.DataType] = {}
    distributions = {}
    for factor in factors:
        if not isinstance(factor, dict) or set(factor) != {"name", "support", "distribution"}:
            raise ValueError("legacy multi-scramble Sobol v2 factor definition is invalid")
        name = factor["name"]
        support_types[name] = _legacy_support(
            name, factor["support"], supports, label="multi-scramble Sobol"
        )
        distribution = factor["distribution"]
        if (
            not isinstance(distribution, dict)
            or set(distribution) != {"family", "transform", "provenance"}
            or distribution.get("family") != "discrete_uniform"
            or distribution.get("transform") != "identity"
            or type(distribution.get("provenance")) is not str
            or not distribution["provenance"]
        ):
            raise ValueError("legacy multi-scramble Sobol v2 distribution is invalid")
        distributions[name] = distribution
    names = tuple(supports)
    if names != tuple(sorted(names)) or "seed" not in supports or not 2 <= len(names) <= 10_600:
        raise ValueError("legacy multi-scramble Sobol v2 factors must be sorted and include seed")
    validate_structured_factor_supports(structured, supports)
    positions = len(scramble_seeds) * sizes[-1] * (2 * len(names) + 2)
    if (
        positions > MAX_MULTISCRAMBLE_DESIGN_POSITIONS
        or positions * (len(names) + 5) > MAX_MULTISCRAMBLE_TABLE_CELLS
        or len(plan) != positions
    ):
        raise ValueError("legacy multi-scramble Sobol v2 plan cardinality is invalid")
    _validate_legacy_scientific_contract(
        design,
        descriptor,
        supports,
        distributions,
        method="multiscramble_sobol",
        checkpoints=len(sizes),
        version=version,
    )
    budget = design["scientificContract"]["charter"]["budget"]
    available = int(
        Decimal(budget["maximum_runs"])
        * (Decimal(1) - Decimal(str(budget["reserve_fraction"])))
    )
    if positions > available:
        raise ValueError("legacy multi-scramble Sobol plan exceeds its budget")
    contract_supports = {
        parameter["name"]: parameter["support"]
        for parameter in design["scientificContract"]["parameters"]
    }
    validate_structured_factor_supports(structured, contract_supports)
    _validate_legacy_plan_schema(
        plan,
        supports,
        support_types,
        seed_is_factor=True,
        label="multi-scramble Sobol",
    )

    maximum = sizes[-1]
    expected_by_role = {}
    domains = supports
    for scramble, scramble_seed in enumerate(scramble_seeds):
        sample = qmc.Sobol(
            d=2 * len(names),
            scramble=True,
            bits=64,
            rng=scramble_seed,
            optimization=None,
        ).random_base2(m=maximum.bit_length() - 1)
        indices = [
            tuple(
                math.floor(float(value) * len(domains[name]))
                for name, value in zip(names * 2, row)
            )
            for row in sample
        ]
        for row_index, point in enumerate(indices):
            a, b = point[: len(names)], point[len(names) :]
            positions_for_row = [
                (f"scramble:{scramble}:A:row:{row_index}", a),
                (f"scramble:{scramble}:B:row:{row_index}", b),
            ]
            for index, name in enumerate(names):
                ab, ba = list(a), list(b)
                ab[index], ba[index] = b[index], a[index]
                positions_for_row.extend(
                    (
                        (
                            f"scramble:{scramble}:AB:factor:{name}:row:{row_index}",
                            tuple(ab),
                        ),
                        (
                            f"scramble:{scramble}:BA:factor:{name}:row:{row_index}",
                            tuple(ba),
                        ),
                    )
                )
            for role, selected_indices in positions_for_row:
                values = tuple(
                    domains[name][index]
                    for name, index in zip(names, selected_indices)
                )
                design_id = hashlib.sha256(
                    LEGACY_MULTISCRAMBLE_SOBOL_DOMAIN
                    + _json({"design": design, "role": role})
                ).hexdigest()[:32]
                expected_by_role[role] = values, design_id

    by_role, design_ids, config_runs, run_configs = {}, set(), {}, {}
    for row in plan.to_pylist():
        role, design_id, run_id = row["role"], row["designId"], row["runId"]
        values = tuple(row["seed"] if name == "seed" else row[name] for name in names)
        configuration = values
        expected_values, expected_design_id = expected_by_role.get(
            role, (None, None)
        )
        if (
            role in by_role
            or role not in expected_by_role
            or row["method"] != "multiscramble_sobol"
            or row["contextHash"] != context_hash
            or values != expected_values
            or design_id != expected_design_id
            or design_id in design_ids
            or re.fullmatch(r"[0-9a-f]{32}", run_id or "") is None
            or config_runs.setdefault(configuration, run_id) != run_id
            or run_configs.setdefault(run_id, configuration) != configuration
        ):
            raise ValueError("legacy multi-scramble Sobol v2 sealed row identity is invalid")
        by_role[role] = values
        design_ids.add(design_id)
    try:
        for scramble in range(len(scramble_seeds)):
            pairs = []
            for row in range(maximum):
                prefix = f"scramble:{scramble}:"
                a = by_role[f"{prefix}A:row:{row}"]
                b = by_role[f"{prefix}B:row:{row}"]
                pairs.append(a + b)
                for index, name in enumerate(names):
                    ab, ba = list(a), list(b)
                    ab[index], ba[index] = b[index], a[index]
                    if (
                        by_role[f"{prefix}AB:factor:{name}:row:{row}"] != tuple(ab)
                        or by_role[f"{prefix}BA:factor:{name}:row:{row}"] != tuple(ba)
                    ):
                        raise ValueError(
                            "legacy multi-scramble Sobol v2 hybrid rows are inconsistent"
                        )
            for checkpoint in sizes:
                validate_sobol_effective_hybrid_contrasts(
                    pairs[:checkpoint], names
                )
    except KeyError as error:
        raise ValueError(
            "legacy multi-scramble Sobol v2 base roles are missing or invalid"
        ) from error
    if len(by_role) != positions:
        raise ValueError("legacy multi-scramble Sobol v2 plan has unexpected roles")


def _validate_legacy_scientific_plan(
    plan: pa.Table,
    method: str,
    bootstrap_resamples: int | None,
    confidence_level: float | None,
    convergence_checkpoints: tuple[int, ...] | None,
    screening_top_k: int | None,
    structured: dict[str, dict[str, list[str]]],
    version: int,
) -> None:
    design, descriptor, context_hash = _legacy_scientific_metadata(
        plan, method, structured, version
    )
    if method == "canonical_morris":
        _validate_legacy_canonical_morris(
            plan,
            design,
            descriptor,
            context_hash,
            bootstrap_resamples,
            confidence_level,
            convergence_checkpoints,
            screening_top_k,
            version,
        )
        return
    _validate_legacy_multiscramble_sobol(
        plan,
        design,
        descriptor,
        context_hash,
        confidence_level,
        structured,
        version,
    )


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
    if metadata:
        required = {
            b"planSchemaVersion",
            b"canonicalizationVersion",
            b"contextDescriptor",
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
        try:
            descriptor = json.loads(metadata[b"contextDescriptor"])
            descriptor_bytes = _json(descriptor)
        except Exception as error:
            raise ValueError("Grid contextDescriptor metadata is invalid") from error
        if (
            metadata[b"contextDescriptor"] != descriptor_bytes
            or not isinstance(descriptor, dict)
            or set(descriptor)
            != {
                "eqasimCommit",
                "dataManifestSha256",
                "lockfileSha256",
                "eqasimSaVersion",
            }
            or any(type(value) is not str for value in descriptor.values())
        ):
            raise ValueError(
                "Grid contextDescriptor must contain exactly four string keys"
            )
        expected_context = hashlib.sha256(
            b"eqasim-sa:context:v1\0" + descriptor_bytes
        ).hexdigest()
    else:
        expected_context = None
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
        if expected_context is not None and context_hash != expected_context:
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


MORRIS_ROLE = re.compile(
    r"^trajectory:(0|[1-9][0-9]*):rank:(0|[1-9][0-9]*):(start|factor:([A-Za-z_][A-Za-z0-9_]*))$"
)


def _output_summary_table(
    plan: pa.Table,
    rows: list[tuple[str, str, float]],
    indicator_names: tuple[str, ...] | None = None,
) -> pa.Table:
    observations = {}
    for run_id, indicator, value in rows:
        key = run_id, indicator
        if key in observations:
            raise ValueError("multiple indicator values for one output-summary runId")
        observations[key] = value
    indicators = tuple(
        sorted(indicator_names or {indicator for _, indicator, _ in rows})
    )
    positions = plan["runId"].to_pylist()
    result = []
    for indicator in indicators:
        values = [
            observations[run_id, indicator]
            for run_id in positions
            if (run_id, indicator) in observations
        ]
        if not values:
            result.append((indicator, 0, None, None, None, None, None, None, None))
            continue
        ordered = sorted(values)

        def quantile(probability: float) -> float:
            position = probability * (len(ordered) - 1)
            lower = math.floor(position)
            fraction = position - lower
            upper = min(lower + 1, len(ordered) - 1)
            return math.fsum(
                ((1.0 - fraction) * ordered[lower], fraction * ordered[upper])
            )

        try:
            summary = (
                statistics.fmean(values),
                statistics.stdev(values) if len(values) > 1 else None,
                min(values),
                quantile(0.05),
                quantile(0.5),
                quantile(0.95),
                max(values),
            )
        except OverflowError as error:
            raise ValueError(
                f"indicator {indicator!r} produced an overflowing output summary"
            ) from error
        if any(value is not None and not math.isfinite(value) for value in summary):
            raise ValueError(f"indicator {indicator!r} produced a non-finite output summary")
        result.append(
            (
                indicator,
                len(values),
                *summary,
            )
        )
    schema = pa.schema(
        [
            pa.field("indicator", pa.string(), False),
            pa.field("n", pa.int64(), False),
            pa.field("mean", pa.float64()),
            pa.field("standardDeviation", pa.float64()),
            pa.field("minimum", pa.float64()),
            pa.field("q05", pa.float64()),
            pa.field("median", pa.float64()),
            pa.field("q95", pa.float64()),
            pa.field("maximum", pa.float64()),
        ]
    )
    return _rows_table(result, schema)


def _convergence_evidence_table(
    method: str,
    charter: dict,
    convergence: pa.Table,
    actionable_factors: set[str] | None = None,
) -> tuple[pa.Table, bool, list[dict]]:
    schema = pa.schema(
        [
            pa.field("metric", pa.string(), False),
            pa.field("checkpoint", pa.int64()),
            pa.field("value", pa.float64()),
            pa.field("threshold", pa.float64(), False),
            pa.field("passed", pa.bool_(), False),
            pa.field("consecutivePasses", pa.int64(), False),
            pa.field("criterionMet", pa.bool_(), False),
            pa.field("evaluationStatus", pa.string(), False),
        ]
    )
    decision = charter["decision"]
    primary = decision["primary_indicator"]
    table_rows = convergence.to_pylist()
    evidence_rows, criterion_reports = [], []
    for criterion in charter["stopping"]:
        metric = criterion["metric"]
        threshold = criterion["threshold"]
        required = criterion["consecutive_checks"]
        supported = (method, metric) in {
            ("canonical_morris", "rank_stability"),
            ("multiscramble_sobol", "ci_width"),
        }
        values = []
        evaluation_failed = False
        if supported and method == "canonical_morris":
            grouped = {}
            all_grouped = {}
            for row in table_rows:
                all_grouped.setdefault(row["checkpoint_trajectories"], set()).add(
                    row["factor"]
                )
                if row.get("indicator") == primary:
                    grouped.setdefault(row["checkpoint_trajectories"], {})[
                        row["factor"]
                    ] = row["rank"]
            checkpoints = sorted(grouped)
            expected_factors = actionable_factors or (
                set(next(iter(grouped.values()))) if grouped else set()
            )
            evaluation_failed = (
                set(grouped) != set(all_grouped)
                or any(
                    set(grouped[checkpoint]) != all_grouped[checkpoint]
                    or set(grouped[checkpoint]) != expected_factors
                    for checkpoint in grouped
                )
            )
            for previous, current in zip(checkpoints, checkpoints[1:]):
                if set(grouped[previous]) != set(grouped[current]) or not grouped[current]:
                    evaluation_failed = True
                    break
                dimension = len(grouped[current])
                value = max(
                    abs(grouped[current][factor] - grouped[previous][factor])
                    for factor in grouped[current]
                ) / max(1, dimension - 1)
                values.append((current, value))
        elif supported:
            width = (
                "first_order_ci_width"
                if decision["objective"] == "fp"
                else "total_order_ci_width"
            )
            grouped = {}
            all_checkpoints = {row["checkpoint"] for row in table_rows}
            factors_by_checkpoint = {}
            for row in table_rows:
                if row.get("indicator") == primary and row.get("is_actionable") is True:
                    grouped.setdefault(row["checkpoint"], []).append(row.get(width))
                    factors_by_checkpoint.setdefault(row["checkpoint"], set()).add(
                        row["factor"]
                    )
            expected_factors = actionable_factors or (
                set(next(iter(factors_by_checkpoint.values())))
                if factors_by_checkpoint
                else set()
            )
            evaluation_failed = set(grouped) != all_checkpoints or any(
                factors != expected_factors for factors in factors_by_checkpoint.values()
            )
            for checkpoint in sorted(grouped):
                widths = grouped[checkpoint]
                if widths and all(
                    type(value) is float and math.isfinite(value) and value >= 0
                    for value in widths
                ):
                    values.append((checkpoint, max(widths)))
                else:
                    evaluation_failed = True
        if not supported:
            evidence_rows.append(
                (metric, None, None, threshold, False, 0, False, "unsupported")
            )
            status, consecutive = "unsupported", 0
        elif evaluation_failed or not values or any(
            not math.isfinite(value) for _, value in values
        ):
            evidence_rows.append(
                (metric, None, None, threshold, False, 0, False, "unevaluable")
            )
            status, consecutive = "unevaluable", 0
        else:
            consecutive = 0
            for checkpoint, value in values:
                passed = value <= threshold
                consecutive = consecutive + 1 if passed else 0
                evidence_rows.append(
                    (
                        metric,
                        checkpoint,
                        value,
                        threshold,
                        passed,
                        consecutive,
                        consecutive >= required,
                        "evaluated",
                    )
                )
            status = "met" if consecutive >= required else "not_met"
        criterion_reports.append(
            {
                "metric": metric,
                "threshold": threshold,
                "consecutiveChecks": required,
                "status": status,
                "finalConsecutivePasses": consecutive,
            }
        )
    return (
        _rows_table(evidence_rows, schema),
        bool(criterion_reports)
        and all(criterion["status"] == "met" for criterion in criterion_reports),
        criterion_reports,
    )


def _governed_report(
    *,
    method: str,
    inference_status: str | None,
    completion_rate: float,
    expected: int,
    completed: int,
    missing: int,
    failed: int,
    output_summary_path: Path,
    convergence_evidence_path: Path | None,
    sensitivity_path: Path | None,
    scientific_contract: dict | None,
    sensitivity_table: pa.Table | None,
    converged: bool,
    criterion_reports: list[dict],
) -> dict:
    charter = scientific_contract["charter"] if scientific_contract else None
    decision = charter["decision"] if charter else None
    exclusions = []
    non_actionable = []
    factor_evidence = []
    robustness_checks = []
    if charter:
        for category, items in charter["uncertainty"]["inventory"].items():
            for item in items:
                if item["status"] == "excluded":
                    exclusions.append({"category": category, **item})
        primary = decision["primary_indicator"]
        objective = decision["objective"]
        if sensitivity_table is not None:
            for row in sensitivity_table.to_pylist():
                if row["indicator"] != primary:
                    continue
                actionable = row.get("is_actionable", True)
                if not actionable:
                    non_actionable.append(row["factor"])
                    continue
                evidence = {
                    "factor": row["factor"],
                    "classification": objective,
                    "decisionRank": (
                        row["rank"]
                        if type(row["rank"]) in (int, float)
                        and math.isfinite(row["rank"])
                        else None
                    ),
                }
                if objective == "fp":
                    evidence["estimate"] = row["first_order_mean"]
                elif objective == "ff":
                    evidence["estimate"] = row["total_order_ci_high"]
                    evidence["withinPracticalTolerance"] = row[
                        "within_practical_tolerance"
                    ]
                else:
                    evidence["estimate"] = row["mu_star"]
                if type(evidence["estimate"]) is not float or not math.isfinite(
                    evidence["estimate"]
                ):
                    evidence["estimate"] = None
                    evidence["evidenceStatus"] = "invalid"
                else:
                    evidence["evidenceStatus"] = "available"
                factor_evidence.append(evidence)
        robustness_checks = [
            {**check, "status": "pending"} for check in charter["robustness"]
        ]
    robustness_status = "pending" if robustness_checks else "not_applicable"
    reasons = []
    if inference_status in {
        "demonstration_noninferential",
        "legacy_noninferential",
        "pilot_only",
        "incomplete_noninferential",
        "descriptive_noninferential",
    }:
        reasons.append(inference_status)
    if charter and not converged:
        reasons.append("stopping_not_met")
    if robustness_checks:
        reasons.append("robustness_pending")
    return {
        "version": 1,
        "method": method,
        "decision": decision,
        "completion": {
            "complete": completed == expected and missing == 0 and failed == 0,
            "completionRate": completion_rate,
            "expected": expected,
            "completed": completed,
            "missing": missing,
            "failed": failed,
        },
        "evidence": {
            "outputSummary": {
                "path": output_summary_path.name,
                "sha256": _digest(output_summary_path),
            },
            "convergence": (
                {
                    "path": convergence_evidence_path.name,
                    "sha256": _digest(convergence_evidence_path),
                }
                if convergence_evidence_path
                else None
            ),
            "sensitivity": (
                {"path": sensitivity_path.name, "sha256": _digest(sensitivity_path)}
                if sensitivity_path
                else None
            ),
        },
        "exclusions": exclusions,
        "nonActionableFactors": sorted(set(non_actionable)),
        "factorEvidence": factor_evidence,
        "convergence": {
            "status": "met" if converged else "not_met" if charter else "not_applicable",
            "converged": converged,
            "criteria": criterion_reports,
        },
        "robustness": {"status": robustness_status, "checks": robustness_checks},
        "inferenceStatus": inference_status,
        "claimAuthorized": False,
        "reasonCodes": reasons,
    }


def _sobol_estimates(f_a, f_b, f_ab, f_ba, indicator: str):  # type: ignore[no-untyped-def]
    arrays = tuple(
        np.asarray(value, dtype=np.float64) for value in (f_a, f_b, f_ab, f_ba)
    )
    if any(not np.all(np.isfinite(value)) for value in arrays):
        raise ValueError(f"Sobol indicator {indicator!r} contains a non-finite value")
    f_a, f_b, f_ab, f_ba = arrays
    mean = np.mean(np.concatenate((f_a, f_b)))
    a, b, ab, ba = f_a - mean, f_b - mean, f_ab - mean, f_ba - mean
    variance = np.mean(np.concatenate((a, b)) ** 2)
    if not np.isfinite(variance) or variance <= 0:
        raise ValueError(
            f"Sobol indicator {indicator!r} has constant or invalid variance"
        )
    first = (np.mean(b * (ab - a), axis=1) + np.mean(a * (ba - b), axis=1)) / (
        2 * variance
    )
    total = (np.mean((a - ab) ** 2, axis=1) + np.mean((b - ba) ** 2, axis=1)) / (
        4 * variance
    )
    if not np.all(np.isfinite(first)) or not np.all(np.isfinite(total)):
        raise ValueError(f"Sobol indicator {indicator!r} produced non-finite indices")
    return first, total


def _sobol_bootstrap(
    f_a, f_b, f_ab, f_ba, count: int, confidence: float, design: bytes, indicator: str
):  # type: ignore[no-untyped-def]
    seed = int.from_bytes(
        hashlib.sha256(
            BOOTSTRAP_DOMAIN + design + b"\0" + indicator.encode("utf-8")
        ).digest()[:16],
        "big",
    )
    rng, first, total, attempts = np.random.default_rng(seed), [], [], 0
    while len(first) < count and attempts < 10 * count:
        attempts += 1
        indices = rng.integers(0, len(f_a), size=len(f_a))
        try:
            values = _sobol_estimates(
                f_a[indices],
                f_b[indices],
                f_ab[:, indices],
                f_ba[:, indices],
                indicator,
            )
        except ValueError as error:
            if "variance" in str(error):
                continue
            raise
        first.append(values[0])
        total.append(values[1])
    if len(first) != count:
        raise ValueError(
            f"Sobol indicator {indicator!r} has degenerate bootstrap resamples"
        )
    alpha = 1 - confidence
    return (
        np.quantile(first, (alpha / 2, 1 - alpha / 2), axis=0),
        np.quantile(total, (alpha / 2, 1 - alpha / 2), axis=0),
        attempts,
    )


def _sobol_ranking(
    plan: pa.Table,
    rows: list[tuple[str, str, float]],
    bootstrap_resamples: int,
    confidence_level: float,
    structured: dict[str, dict[str, list[str]]] | None = None,
) -> pa.Table:
    metadata = plan.schema.metadata or {}
    required = {
        b"planSchemaVersion",
        b"canonicalizationVersion",
        b"contextDescriptor",
        b"sobolDesignVersion",
        b"sobolDesign",
    }
    if structured is None:
        structured = validate_structured_plan_metadata(dict(metadata))
    if structured:
        required |= {b"structuredFactorsVersion", b"structuredFactors"}
    if (
        set(metadata) != required
        or metadata.get(b"planSchemaVersion") != (b"3" if structured else b"1")
        or metadata.get(b"canonicalizationVersion") != b"1"
        or metadata.get(b"sobolDesignVersion") != b"1"
    ):
        raise ValueError("Sobol plan metadata is missing or unsupported")
    try:
        design, descriptor = (
            json.loads(metadata[b"sobolDesign"]),
            json.loads(metadata[b"contextDescriptor"]),
        )
    except Exception as error:
        raise ValueError("Sobol metadata is invalid") from error
    try:
        canonical = json.dumps(
            design,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        descriptor_bytes = json.dumps(
            descriptor, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (TypeError, ValueError) as error:
        raise ValueError("Sobol metadata contains non-canonical values") from error
    if (
        metadata[b"sobolDesign"] != canonical
        or metadata[b"contextDescriptor"] != descriptor_bytes
    ):
        raise ValueError("Sobol metadata is not canonical")
    context_keys = {
        "eqasimCommit",
        "dataManifestSha256",
        "lockfileSha256",
        "eqasimSaVersion",
    }
    if (
        not isinstance(descriptor, dict)
        or set(descriptor) != context_keys
        or any(type(value) is not str for value in descriptor.values())
    ):
        raise ValueError(
            "Sobol contextDescriptor must contain exactly four string keys"
        )
    design_keys = {
        "baseSampleSize",
        "designSeed",
        "scramble",
        "bits",
        "mapping",
        "factors",
    }
    if structured:
        design_keys.add("structuredFactors")
    if set(design) != design_keys:
        raise ValueError("Sobol design metadata keys are invalid")
    if structured and design["structuredFactors"] != structured:
        raise ValueError(
            "Sobol method structuredFactors do not match structured plan metadata"
        )
    n, design_seed, factors = (
        design["baseSampleSize"],
        design["designSeed"],
        design["factors"],
    )
    if (
        type(n) is not int
        or n < 2
        or n & (n - 1)
        or type(design_seed) is not int
        or not 0 <= design_seed < 2**64
        or design["scramble"] is not True
        or design["bits"] != 64
        or design["mapping"] != "equal-width-v1"
        or not isinstance(factors, list)
    ):
        raise ValueError("Sobol sealed design settings are invalid")
    domains, domain_types = {}, {}
    for factor in factors:
        if (
            not isinstance(factor, dict)
            or set(factor) != {"name", "values"}
            or type(factor["name"]) is not str
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", factor["name"]) is None
            or factor["name"] in domains
            or factor["name"] in {"designId", "method", "role", "runId", "contextHash"}
            or not isinstance(factor["values"], list)
            or len(factor["values"]) < 2
        ):
            raise ValueError("Sobol sealed factor domain is invalid")
        name, values = factor["name"], factor["values"]
        kind = type(values[0])
        if (
            kind not in (int, float, str)
            or any(type(value) is not kind for value in values)
            or len(set(values)) != len(values)
            or kind is int
            and any(not -(2**63) <= value < 2**63 for value in values)
            or kind is float
            and any(not math.isfinite(value) for value in values)
            or kind is str
            and any(not value or "\0" in value for value in values)
            or name == "seed"
            and kind is not int
        ):
            raise ValueError(
                "Sobol sealed factor values must be homogeneous finite scalars"
            )
        domains[name] = values
        domain_types[name] = {int: pa.int64(), float: pa.float64(), str: pa.string()}[
            kind
        ]
    names = tuple(domains)
    if (
        names != tuple(sorted(names))
        or "seed" not in domains
        or not 2 <= len(names) <= 10_600
    ):
        raise ValueError("Sobol sealed factors are invalid")
    validate_structured_factor_supports(structured, domains)
    if n * (2 * len(names) + 2) > 2**63 - 1:
        raise ValueError("Sobol N(2D+2) must fit in signed int64")
    validate_bootstrap_resamples(
        bootstrap_resamples,
        method="Sobol",
        minimum=1,
    )
    validate_sobol_bootstrap_work(bootstrap_resamples, n, len(names))
    columns = tuple(name for name in names if name != "seed")
    if plan.column_names != [
        "designId",
        "method",
        "role",
        "runId",
        "seed",
        "contextHash",
        *columns,
    ]:
        raise ValueError("Sobol plan columns do not match sealed domains")
    expected_schema = pa.schema(
        [
            *(
                pa.field(name, pa.string(), False)
                for name in ("designId", "method", "role", "runId")
            ),
            pa.field("seed", pa.int64(), False),
            pa.field("contextHash", pa.string(), False),
            *(pa.field(name, domain_types[name], False) for name in columns),
        ]
    )
    if not plan.schema.remove_metadata().equals(expected_schema):
        raise ValueError("Sobol plan Arrow schema types or nullability are invalid")
    sample = qmc.Sobol(
        d=2 * len(names), scramble=True, bits=64, rng=design_seed, optimization=None
    ).random_base2(m=n.bit_length() - 1)
    projected = [
        tuple(
            domains[name][math.floor(float(value) * len(domains[name]))]
            for name, value in zip(names * 2, point)
        )
        for point in sample
    ]
    validate_sobol_effective_hybrid_contrasts(projected, names)
    expected = {}
    for row_index, values in enumerate(projected):
        a, b = values[: len(names)], values[len(names) :]
        positions = [(f"A:row:{row_index}", a), (f"B:row:{row_index}", b)]
        for index, name in enumerate(names):
            ab, ba = list(a), list(b)
            ab[index], ba[index] = b[index], a[index]
            positions.extend(
                (
                    (f"AB:factor:{name}:row:{row_index}", tuple(ab)),
                    (f"BA:factor:{name}:row:{row_index}", tuple(ba)),
                )
            )
        for role, position in positions:
            design_id = hashlib.sha256(
                SOBOL_DOMAIN
                + json.dumps(
                    {"design": design, "role": role},
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode()
            ).hexdigest()[:32]
            expected[role] = position, design_id
    if len(plan) != n * (2 * len(names) + 2):
        raise ValueError("Sobol plan cardinality is inconsistent")
    observations = {(run_id, indicator): value for run_id, indicator, value in rows}
    if len(observations) != len(rows):
        raise ValueError("multiple indicator values for one Sobol runId")
    indicators, locations, config_runs, run_configs = (
        sorted({row[1] for row in rows}),
        {},
        {},
        {},
    )
    context_hash = hashlib.sha256(
        b"eqasim-sa:context:v1\0" + descriptor_bytes
    ).hexdigest()
    for location in plan.to_pylist():
        role = location["role"]
        if role in locations or role not in expected:
            raise ValueError(f"Sobol role is missing, duplicate or invalid: {role!r}")
        position, design_id = expected[role]
        actual = tuple(
            location[name] if name != "seed" else location["seed"] for name in names
        )
        if (
            actual != position
            or location["designId"] != design_id
            or location["method"] != "sobol"
            or location["contextHash"] != context_hash
        ):
            raise ValueError(
                "Sobol sealed role, designId, context or projected value is invalid"
            )
        config = tuple(actual)
        run_id = location["runId"]
        if (
            config_runs.setdefault(config, run_id) != run_id
            or run_configs.setdefault(run_id, config) != config
        ):
            raise ValueError("Sobol runId relation is inconsistent")
        locations[role] = location
    if set(locations) != set(expected):
        raise ValueError("Sobol roles are incomplete")
    result = []
    for indicator in indicators:

        def values(prefix: str, factor: str | None = None):
            roles = (
                (
                    f"{prefix}:row:{index}"
                    if factor is None
                    else f"{prefix}:factor:{factor}:row:{index}"
                )
                for index in range(n)
            )
            try:
                return np.array(
                    [
                        observations[(locations[role]["runId"], indicator)]
                        for role in roles
                    ]
                )
            except KeyError as error:
                raise ValueError(
                    f"Sobol indicator {indicator!r} is incomplete"
                ) from error

        f_a, f_b = values("A"), values("B")
        f_ab = np.array([values("AB", factor) for factor in names])
        f_ba = np.array([values("BA", factor) for factor in names])
        first, total = _sobol_estimates(f_a, f_b, f_ab, f_ba, indicator)
        first_ci, total_ci, attempts = _sobol_bootstrap(
            f_a,
            f_b,
            f_ab,
            f_ba,
            bootstrap_resamples,
            confidence_level,
            canonical,
            indicator,
        )
        ranked = sorted(
            range(len(names)), key=lambda index: (-total[index], names[index])
        )
        for rank, index in enumerate(ranked, 1):
            result.append(
                (
                    indicator,
                    names[index],
                    rank,
                    n,
                    bootstrap_resamples,
                    attempts,
                    first[index],
                    first_ci[0, index],
                    first_ci[1, index],
                    total[index],
                    total_ci[0, index],
                    total_ci[1, index],
                )
            )
    schema = pa.schema(
        [
            pa.field("indicator", pa.string(), False),
            pa.field("factor", pa.string(), False),
            pa.field("rank", pa.int64(), False),
            pa.field("base_sample_size", pa.int64(), False),
            pa.field("bootstrap_resamples", pa.int64(), False),
            pa.field("bootstrap_attempts", pa.int64(), False),
            pa.field("first_order", pa.float64(), False),
            pa.field("first_order_ci_low", pa.float64(), False),
            pa.field("first_order_ci_high", pa.float64(), False),
            pa.field("total_order", pa.float64(), False),
            pa.field("total_order_ci_low", pa.float64(), False),
            pa.field("total_order_ci_high", pa.float64(), False),
        ]
    )
    return pa.Table.from_arrays(
        [
            pa.array((row[index] for row in result), type=field.type)
            for index, field in enumerate(schema)
        ],
        schema=schema,
    )


def _objective_average_ranks(values: dict[str, float], *, descending: bool) -> dict[str, float]:
    ordered = sorted(values, key=lambda name: ((-values[name]) if descending else values[name], name))
    result, index = {}, 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and values[ordered[end]] == values[ordered[index]]:
            end += 1
        rank = ((index + 1) + end) / 2
        result.update((name, rank) for name in ordered[index:end])
        index = end
    return result


def _finite_population_variance(values: np.ndarray, indicator: str) -> float:
    minimum, maximum = float(np.min(values)), float(np.max(values))
    if minimum == maximum:
        return 0.0
    center = minimum / 2 + maximum / 2
    scale = max(abs(minimum - center), abs(maximum - center))
    normalized = (values - center) / scale
    normalized_mean = float(np.mean(normalized))
    variance = float(np.mean((normalized - normalized_mean) ** 2))
    log_result = math.log(variance) + 2 * math.log(scale)
    if log_result > math.log(np.finfo(np.float64).max):
        raise ValueError(
            f"Sobol indicator {indicator!r} output variance exceeds float64 range"
        )
    if log_result < math.log(np.nextafter(0.0, 1.0)):
        raise ValueError(
            f"Sobol indicator {indicator!r} output variance is below float64 range"
        )
    result = variance * scale * scale
    if not math.isfinite(result):
        raise ValueError(
            f"Sobol indicator {indicator!r} output variance exceeds float64 range"
        )
    return result


def _multiscramble_sobol_tables(
    plan: pa.Table,
    rows: list[tuple[str, str, float]],
    confidence_level: float,
    structured: dict[str, dict[str, list[str]]] | None = None,
) -> tuple[pa.Table, pa.Table, pa.Table, pa.Table]:
    metadata = plan.schema.metadata or {}
    required = {
        b"planSchemaVersion",
        b"canonicalizationVersion",
        b"contextDescriptor",
        b"multiScrambleSobolDesignVersion",
        b"multiScrambleSobolDesign",
    }
    if structured is None:
        structured = validate_structured_plan_metadata(dict(metadata))
    if structured:
        required |= {b"structuredFactorsVersion", b"structuredFactors"}
    if (
        set(metadata) != required
        or metadata.get(b"planSchemaVersion") != (b"3" if structured else b"2")
        or metadata.get(b"canonicalizationVersion") != b"1"
        or metadata.get(b"multiScrambleSobolDesignVersion") != b"3"
    ):
        raise ValueError("multi-scramble Sobol plan metadata is missing or unsupported")
    try:
        design = json.loads(metadata[b"multiScrambleSobolDesign"])
        descriptor = json.loads(metadata[b"contextDescriptor"])
        design_bytes, descriptor_bytes = _json(design), _json(descriptor)
    except Exception as error:
        raise ValueError("multi-scramble Sobol metadata is invalid") from error
    if metadata[b"multiScrambleSobolDesign"] != design_bytes or metadata[b"contextDescriptor"] != descriptor_bytes:
        raise ValueError("multi-scramble Sobol metadata is not canonical")
    if (
        not isinstance(descriptor, dict)
        or set(descriptor)
        != {
            "eqasimCommit",
            "dataManifestSha256",
            "lockfileSha256",
            "eqasimSaVersion",
        }
        or any(type(value) is not str for value in descriptor.values())
    ):
        raise ValueError("multi-scramble Sobol contextDescriptor is invalid")
    keys = {
        "baseSampleSizes",
        "confidenceLevel",
        "factors",
        "mapping",
        "maximumBaseSampleSize",
        "numericalEngine",
        "saltelliEstimator",
        "scientificContract",
        "scrambleSeeds",
    }
    if structured:
        keys.add("structuredFactors")
    if not isinstance(design, dict) or set(design) != keys:
        raise ValueError("multi-scramble Sobol design metadata keys are invalid")
    if structured and design["structuredFactors"] != structured:
        raise ValueError(
            "multi-scramble Sobol method structuredFactors do not match structured plan metadata"
        )
    sizes, scramble_seeds = design["baseSampleSizes"], design["scrambleSeeds"]
    if (
        not isinstance(sizes, list)
        or not sizes
        or any(type(value) is not int or value < 2 or value & (value - 1) for value in sizes)
        or sorted(set(sizes)) != sizes
        or any(right != 2 * left for left, right in zip(sizes, sizes[1:]))
        or design["maximumBaseSampleSize"] != sizes[-1]
        or not isinstance(scramble_seeds, list)
        or len(scramble_seeds) < 4
        or any(type(seed) is not int or not 0 <= seed < 2**64 for seed in scramble_seeds)
        or len(set(scramble_seeds)) != len(scramble_seeds)
        or design["mapping"] != "equal-width-v1"
        or design["numericalEngine"]
        != engine_descriptor(
            "multiscramble_sobol",
            sample_size=sizes[-1],
            sample_seeds=scramble_seeds,
        )
        or design["saltelliEstimator"]
        != "symmetric-forward-reverse-salib-v1"
        or type(design["confidenceLevel"]) is not float
        or not math.isfinite(design["confidenceLevel"])
        or not 0 < design["confidenceLevel"] < 1
        or confidence_level != design["confidenceLevel"]
    ):
        raise ValueError("multi-scramble Sobol sealed settings are invalid")
    factors = design["factors"]
    supports, distributions, support_types = {}, {}, {}
    if not isinstance(factors, list):
        raise ValueError("multi-scramble Sobol factors are invalid")
    for factor in factors:
        if not isinstance(factor, dict) or set(factor) != {"name", "support", "distribution"}:
            raise ValueError("multi-scramble Sobol factor definition is invalid")
        name, support, distribution = factor["name"], factor["support"], factor["distribution"]
        if (
            type(name) is not str
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
            or name in supports
            or not isinstance(support, list)
            or len(support) < 2
            or len(set(support)) != len(support)
        ):
            raise ValueError("multi-scramble Sobol factor support is invalid")
        kind = type(support[0])
        if (
            kind not in (int, float, str)
            or any(type(value) is not kind for value in support)
            or kind is int and any(not -(2**63) <= value < 2**63 for value in support)
            or kind is float and any(not math.isfinite(value) for value in support)
            or kind is str and any(not value or "\0" in value for value in support)
            or not isinstance(distribution, dict)
            or set(distribution) != {"family", "transform", "provenance"}
            or distribution.get("family") != "discrete_uniform"
            or distribution.get("transform") != "identity"
            or type(distribution.get("provenance")) is not str
            or not distribution["provenance"]
        ):
            raise ValueError("multi-scramble Sobol support or distribution is invalid")
        supports[name], distributions[name] = support, distribution
        support_types[name] = {int: pa.int64(), float: pa.float64(), str: pa.string()}[kind]
    names = tuple(supports)
    if names != tuple(sorted(names)) or "seed" not in supports or not 2 <= len(names) <= 10_600:
        raise ValueError("multi-scramble Sobol factors must be sorted and include seed")
    positions = len(scramble_seeds) * sizes[-1] * (2 * len(names) + 2)
    if positions > 2**63 - 1 or positions > MAX_MULTISCRAMBLE_DESIGN_POSITIONS:
        raise ValueError("multi-scramble Sobol design cardinality exceeds the allocation cap")
    if positions * (len(names) + 5) > MAX_MULTISCRAMBLE_TABLE_CELLS:
        raise ValueError("multi-scramble Sobol table cells exceed the allocation cap")
    if len(plan) != positions:
        raise ValueError("multi-scramble Sobol plan cardinality is inconsistent")
    validate_multiscramble_scientific_contract(
        design["scientificContract"],
        supports,
        distributions,
    )
    if (
        design["scientificContract"]["software"]["eqasimSaVersion"]
        != descriptor["eqasimSaVersion"]
    ):
        raise ValueError(
            "multi-scramble Sobol scientific software version does not match contextDescriptor"
        )
    validate_scientific_stopping(
        "multiscramble_sobol",
        design["scientificContract"]["charter"]["stopping"],
        len(sizes),
    )
    validate_structured_factor_supports(structured, supports)
    contract_supports = {
        parameter["name"]: parameter["support"]
        for parameter in design["scientificContract"]["parameters"]
    }
    validate_structured_factor_supports(structured, contract_supports)
    decision = design["scientificContract"]["charter"]["decision"]
    objective = decision["objective"]
    primary_indicator = decision["primary_indicator"]
    tolerance = decision["practical_tolerance"]
    columns = tuple(name for name in names if name != "seed")
    expected_schema = pa.schema(
        [
            *(pa.field(name, pa.string(), False) for name in ("designId", "method", "role", "runId")),
            pa.field("seed", pa.int64(), False),
            pa.field("contextHash", pa.string(), False),
            *(pa.field(name, support_types[name], False) for name in columns),
        ]
    )
    if plan.column_names != expected_schema.names or not plan.schema.remove_metadata().equals(expected_schema):
        raise ValueError("multi-scramble Sobol plan schema does not match sealed factors")
    context_hash = hashlib.sha256(b"eqasim-sa:context:v1\0" + descriptor_bytes).hexdigest()
    expected, projected_by_scramble = {}, {}
    maximum = sizes[-1]
    for scramble, seed in enumerate(scramble_seeds):
        projected = [
            (
                role,
                tuple(
                    supports[name][math.floor(value * len(supports[name]))]
                    for name, value in zip(names, point)
                ),
            )
            for role, point in sobol_unit_positions(names, maximum, seed)
        ]
        by_role = dict(projected)
        if len(by_role) != len(projected):
            raise ValueError("multi-scramble Sobol SALib roles are duplicate")
        pairs = [
            by_role[f"A:row:{row}"] + by_role[f"B:row:{row}"]
            for row in range(maximum)
        ]
        projected_by_scramble[scramble] = pairs
        for checkpoint in sizes:
            try:
                validate_sobol_effective_hybrid_contrasts(
                    pairs[:checkpoint], names
                )
            except ValueError as error:
                raise ValueError(
                    f"multi-scramble Sobol scramble {scramble}, checkpoint {checkpoint}: {error}"
                ) from error
        for role, position in projected:
            sealed_role = f"scramble:{scramble}:{role}"
            design_id = hashlib.sha256(
                MULTISCRAMBLE_SOBOL_DOMAIN
                + _json({"design": design, "role": sealed_role})
            ).hexdigest()[:32]
            expected[sealed_role] = position, design_id
    locations, config_runs, run_configs = {}, {}, {}
    for location in plan.to_pylist():
        role = location["role"]
        if role in locations or role not in expected:
            raise ValueError(f"multi-scramble Sobol role is duplicate or invalid: {role!r}")
        position, design_id = expected[role]
        actual = tuple(location["seed"] if name == "seed" else location[name] for name in names)
        run_id = location["runId"]
        if (
            actual != position
            or location["designId"] != design_id
            or location["method"] != "multiscramble_sobol"
            or location["contextHash"] != context_hash
        ):
            raise ValueError("multi-scramble Sobol role, designId, context, or mapped value is invalid")
        if config_runs.setdefault(actual, run_id) != run_id or run_configs.setdefault(run_id, actual) != actual:
            raise ValueError("multi-scramble Sobol runId relation is inconsistent")
        locations[role] = location
    if set(locations) != set(expected):
        raise ValueError("multi-scramble Sobol roles are incomplete")
    observations = {}
    for run_id, indicator, value in rows:
        if (run_id, indicator) in observations:
            raise ValueError("multiple indicator values for one multi-scramble Sobol runId")
        observations[run_id, indicator] = value
    indicators = tuple(sorted({indicator for _, indicator, _ in rows}))
    estimates, by_scramble_rows, diagnostic_rows = {}, [], []
    for indicator in indicators:
        for checkpoint in sizes:
            for scramble in range(len(scramble_seeds)):
                def values(prefix: str, factor: str | None = None) -> np.ndarray:
                    roles = (
                        f"scramble:{scramble}:{prefix}:row:{row}"
                        if factor is None
                        else f"scramble:{scramble}:{prefix}:factor:{factor}:row:{row}"
                        for row in range(checkpoint)
                    )
                    try:
                        return np.asarray(
                            [observations[locations[role]["runId"], indicator] for role in roles],
                            dtype=np.float64,
                        )
                    except KeyError as error:
                        raise ValueError(f"multi-scramble Sobol indicator {indicator!r} is incomplete") from error

                f_a, f_b = values("A"), values("B")
                f_ab = np.asarray([values("AB", factor) for factor in names])
                f_ba = np.asarray([values("BA", factor) for factor in names])
                first, total = sobol_symmetric_indices(
                    names,
                    f_a,
                    f_b,
                    f_ab,
                    f_ba,
                    indicator=indicator,
                )
                output_variance = _finite_population_variance(
                    np.concatenate((f_a, f_b)), indicator
                )
                projected = projected_by_scramble[scramble][:checkpoint]
                effective = [
                    sum(point[index] != point[len(names) + index] for point in projected)
                    for index in range(len(names))
                ]
                if any(count == 0 for count in effective):
                    raise ValueError("multi-scramble Sobol ineffective projected contrast")
                for index, factor in enumerate(names):
                    first_order = float(first[index])
                    total_order = float(total[index])
                    gap = total_order - first_order
                    if not math.isfinite(gap):
                        raise ValueError(
                            "multi-scramble Sobol derived statistic is not finite"
                        )
                    item = {
                        "indicator": indicator,
                        "checkpoint": checkpoint,
                        "scramble": scramble,
                        "factor": factor,
                        "is_actionable": factor != "seed",
                        "first_order": first_order,
                        "total_order": total_order,
                        "output_variance": output_variance,
                        "effective_contrasts": effective[index],
                        "effective_contrast_rate": effective[index] / checkpoint,
                        "ST_minus_S": gap,
                        "first_order_out_of_range": bool(first_order < 0 or first_order > 1),
                        "total_order_out_of_range": bool(total_order < 0 or total_order > 1),
                    }
                    by_scramble_rows.append(item)
                    estimates[indicator, checkpoint, scramble, factor] = item
                prefix_roles = [
                    role
                    for role in expected
                    if role.startswith(f"scramble:{scramble}:")
                    and int(role.rsplit(":", 1)[1]) < checkpoint
                ]
                prefix_run_ids = [locations[role]["runId"] for role in prefix_roles]
                multiplicities = Counter(prefix_run_ids)
                try:
                    sum_first_order = math.fsum(float(value) for value in first)
                except OverflowError as error:
                    raise ValueError(
                        "multi-scramble Sobol diagnostic is not finite"
                    ) from error
                if not math.isfinite(sum_first_order):
                    raise ValueError("multi-scramble Sobol diagnostic is not finite")
                diagnostic_rows.append(
                    {
                        "indicator": indicator,
                        "checkpoint": checkpoint,
                        "scramble": scramble,
                        "sum_first_order": sum_first_order,
                        "first_order_out_of_range_count": int(np.sum((first < 0) | (first > 1))),
                        "total_order_out_of_range_count": int(np.sum((total < 0) | (total > 1))),
                        "negative_gap_count": int(np.sum(total < first)),
                        "design_positions": len(prefix_run_ids),
                        "unique_run_ids": len(multiplicities),
                        "duplicate_fraction": 1 - len(multiplicities) / len(prefix_run_ids),
                        "maximum_multiplicity": max(multiplicities.values()),
                    }
                )
    convergence_rows = []
    previous = {}
    critical = float(student_t.ppf((1 + confidence_level) / 2, len(scramble_seeds) - 1))
    for indicator in indicators:
        for checkpoint in sizes:
            aggregates = {}
            is_primary_indicator = indicator == primary_indicator
            for factor in names:
                first_values = [estimates[indicator, checkpoint, scramble, factor]["first_order"] for scramble in range(len(scramble_seeds))]
                total_values = [estimates[indicator, checkpoint, scramble, factor]["total_order"] for scramble in range(len(scramble_seeds))]
                try:
                    first_mean, total_mean = statistics.fmean(first_values), statistics.fmean(total_values)
                    first_sd, total_sd = statistics.stdev(first_values), statistics.stdev(total_values)
                except OverflowError as error:
                    raise ValueError(
                        "multi-scramble Sobol aggregate is not finite"
                    ) from error
                first_se, total_se = first_sd / math.sqrt(len(scramble_seeds)), total_sd / math.sqrt(len(scramble_seeds))
                first_margin, total_margin = critical * first_se, critical * total_se
                first_change = first_mean - previous[indicator, factor][0] if (indicator, factor) in previous else None
                total_change = total_mean - previous[indicator, factor][1] if (indicator, factor) in previous else None
                derived = (
                    first_mean - first_margin,
                    first_mean + first_margin,
                    2 * first_margin,
                    total_mean - total_margin,
                    total_mean + total_margin,
                    2 * total_margin,
                    first_change,
                    total_change,
                )
                if not all(
                    math.isfinite(value)
                    for value in (
                        first_mean,
                        total_mean,
                        first_sd,
                        total_sd,
                        first_se,
                        total_se,
                        first_margin,
                        total_margin,
                        *(value for value in derived if value is not None),
                    )
                ):
                    raise ValueError("multi-scramble Sobol aggregate is not finite")
                aggregates[factor] = {
                    "indicator": indicator,
                    "factor": factor,
                    "checkpoint": checkpoint,
                    "scrambles": len(scramble_seeds),
                    "confidence_level": confidence_level,
                    "first_order_mean": first_mean,
                    "first_order_sd": first_sd,
                    "first_order_se": first_se,
                    "first_order_ci_low": derived[0],
                    "first_order_ci_high": derived[1],
                    "first_order_ci_width": derived[2],
                    "total_order_mean": total_mean,
                    "total_order_sd": total_sd,
                    "total_order_se": total_se,
                    "total_order_ci_low": derived[3],
                    "total_order_ci_high": derived[4],
                    "total_order_ci_width": derived[5],
                    "first_order_change": first_change,
                    "total_order_change": total_change,
                    "is_primary_indicator": is_primary_indicator,
                    "is_actionable": factor != "seed",
                    "objective": objective,
                    "within_practical_tolerance": (
                        derived[4] <= tolerance
                    ) if objective == "ff" and is_primary_indicator and factor != "seed" else None,
                }
            rank_values = (
                {
                    factor: item["first_order_mean"]
                    for factor, item in aggregates.items()
                    if factor != "seed"
                }
                if objective == "fp"
                else {
                    factor: item["total_order_ci_high"]
                    for factor, item in aggregates.items()
                    if factor != "seed"
                }
            )
            ranks = _objective_average_ranks(rank_values, descending=objective == "fp")
            for factor in names:
                aggregates[factor]["rank"] = ranks.get(factor)
                convergence_rows.append(aggregates[factor])
                previous[indicator, factor] = (
                    aggregates[factor]["first_order_mean"],
                    aggregates[factor]["total_order_mean"],
                )
    final_size = sizes[-1]
    index_rows = [row for row in convergence_rows if row["checkpoint"] == final_size]
    by_schema = pa.schema([
        pa.field("indicator", pa.string(), False), pa.field("checkpoint", pa.int64(), False),
        pa.field("scramble", pa.int64(), False), pa.field("factor", pa.string(), False),
        pa.field("is_actionable", pa.bool_(), False),
        pa.field("first_order", pa.float64(), False), pa.field("total_order", pa.float64(), False),
        pa.field("output_variance", pa.float64(), False), pa.field("effective_contrasts", pa.int64(), False),
        pa.field("effective_contrast_rate", pa.float64(), False), pa.field("ST_minus_S", pa.float64(), False),
        pa.field("first_order_out_of_range", pa.bool_(), False), pa.field("total_order_out_of_range", pa.bool_(), False),
    ])
    convergence_schema = pa.schema([
        pa.field("indicator", pa.string(), False), pa.field("factor", pa.string(), False),
        pa.field("checkpoint", pa.int64(), False), pa.field("scrambles", pa.int64(), False),
        pa.field("confidence_level", pa.float64(), False),
        *[pa.field(f"{order}_{stat}", pa.float64(), False) for order in ("first_order", "total_order") for stat in ("mean", "sd", "se", "ci_low", "ci_high", "ci_width")],
        pa.field("first_order_change", pa.float64()), pa.field("total_order_change", pa.float64()),
        pa.field("is_primary_indicator", pa.bool_(), False),
        pa.field("is_actionable", pa.bool_(), False),
        pa.field("objective", pa.string(), False), pa.field("within_practical_tolerance", pa.bool_()),
        pa.field("rank", pa.float64()),
    ])
    diagnostics_schema = pa.schema([
        pa.field("indicator", pa.string(), False), pa.field("checkpoint", pa.int64(), False),
        pa.field("scramble", pa.int64(), False), pa.field("sum_first_order", pa.float64(), False),
        pa.field("first_order_out_of_range_count", pa.int64(), False),
        pa.field("total_order_out_of_range_count", pa.int64(), False),
        pa.field("negative_gap_count", pa.int64(), False), pa.field("design_positions", pa.int64(), False),
        pa.field("unique_run_ids", pa.int64(), False), pa.field("duplicate_fraction", pa.float64(), False),
        pa.field("maximum_multiplicity", pa.int64(), False),
    ])
    index_schema = convergence_schema
    return (
        pa.Table.from_pylist(index_rows, schema=index_schema),
        pa.Table.from_pylist(by_scramble_rows, schema=by_schema),
        pa.Table.from_pylist(convergence_rows, schema=convergence_schema),
        pa.Table.from_pylist(diagnostic_rows, schema=diagnostics_schema),
    )


def _morris_ranking(plan: pa.Table, rows: list[tuple[str, str, float]]) -> pa.Table:
    return _adjacent_oat_ranking(
        plan,
        rows,
        design_version_key=b"morrisDesignVersion",
        design_key=b"morrisDesign",
        domain=b"eqasim-sa:design:morris:v1\0",
        method="morris",
        label="Morris",
    )


def _discrete_oat_effects(
    plan: pa.Table, rows: list[tuple[str, str, float]]
) -> pa.Table:
    return _adjacent_oat_ranking(
        plan,
        rows,
        design_version_key=b"discreteOatDesignVersion",
        design_key=b"discreteOatDesign",
        domain=DISCRETE_OAT_DOMAIN,
        method="discrete_oat",
        label="Discrete OAT",
    )


def _adjacent_oat_ranking(
    plan: pa.Table,
    rows: list[tuple[str, str, float]],
    *,
    design_version_key: bytes,
    design_key: bytes,
    domain: bytes,
    method: str,
    label: str,
) -> pa.Table:
    metadata = plan.schema.metadata or {}
    expected_metadata = {
        b"planSchemaVersion",
        b"canonicalizationVersion",
        b"contextDescriptor",
        design_version_key,
        design_key,
    }
    if set(metadata) != expected_metadata or any(
        metadata.get(name) != b"1"
        for name in (
            b"planSchemaVersion",
            b"canonicalizationVersion",
            design_version_key,
        )
    ):
        raise ValueError(f"{label} plan metadata is missing or unsupported")
    try:
        design = json.loads(metadata[design_key])
        descriptor = json.loads(metadata[b"contextDescriptor"])
    except Exception as error:
        raise ValueError("Morris metadata is invalid") from error
    descriptor_bytes = json.dumps(
        descriptor, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    if (
        not isinstance(descriptor, dict)
        or set(descriptor)
        != {"eqasimCommit", "dataManifestSha256", "lockfileSha256", "eqasimSaVersion"}
        or metadata[b"contextDescriptor"] != descriptor_bytes
    ):
        raise ValueError("Morris contextDescriptor metadata is invalid")
    context_hash = hashlib.sha256(
        b"eqasim-sa:context:v1\0" + descriptor_bytes
    ).hexdigest()
    if set(design) != {"designSeed", "trajectories", "factors"}:
        raise ValueError("Morris design metadata keys are invalid")
    canonical = json.dumps(
        design,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    if metadata[design_key] != canonical:
        raise ValueError("Morris design metadata is not canonical")
    if (
        type(design["designSeed"]) is not int
        or not -(2**63) <= design["designSeed"] < 2**63
    ):
        raise ValueError("Morris designSeed metadata is invalid")
    factors = design["factors"]
    if not isinstance(factors, list) or not factors:
        raise ValueError("Morris factor domains are invalid")
    declared_names = tuple(
        factor.get("name") for factor in factors if isinstance(factor, dict)
    )
    if any(type(name) is not str for name in declared_names) or declared_names != tuple(
        sorted(declared_names)
    ):
        raise ValueError("Morris factors must be in lexicographic order")
    domains = {}
    for factor in factors:
        if not isinstance(factor, dict) or set(factor) != {"name", "values"}:
            raise ValueError("Morris factor domain is invalid")
        name, values = factor["name"], factor["values"]
        if (
            name in domains
            or name not in plan.column_names
            or not isinstance(values, list)
        ):
            raise ValueError("Morris factor domain is inconsistent with plan")
        try:
            validate_morris_values(name, values)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Morris factor domain is invalid: {error}") from error
        expected_type = pa.int64() if type(values[0]) is int else pa.float64()
        field = plan.schema.field(name)
        if field.nullable or field.type != expected_type:
            raise ValueError("Morris plan factor schema does not match sealed domain")
        domains[name] = values
    names = tuple(sorted(domains))
    fixed = {"designId", "method", "role", "runId", "seed", "contextHash"}
    if set(plan.column_names) - fixed != set(names):
        raise ValueError("Morris plan columns do not match sealed domains")
    fixed_types = (
        pa.string(),
        pa.string(),
        pa.string(),
        pa.string(),
        pa.int64(),
        pa.string(),
    )
    if any(
        plan.schema.field(name).nullable or plan.schema.field(name).type != kind
        for name, kind in zip(
            ("designId", "method", "role", "runId", "seed", "contextHash"), fixed_types
        )
    ):
        raise ValueError("Morris fixed plan schema is invalid")
    trajectories = design["trajectories"]
    if type(trajectories) is not int or trajectories <= 0:
        raise ValueError("Morris trajectories metadata is invalid")
    candidates = math.factorial(len(names)) * math.prod(
        2 * (len(domains[name]) - 1) for name in names
    )
    if trajectories > candidates:
        raise ValueError("Morris trajectories exceed candidate count")
    selection_definition = {
        "designSeed": design["designSeed"],
        "factors": factors,
    }
    selection_bytes = _json(selection_definition)
    offset = int.from_bytes(
        hashlib.sha256(domain + selection_bytes + b"\0offset").digest(), "big"
    ) % candidates
    stride = int.from_bytes(
        hashlib.sha256(domain + selection_bytes + b"\0stride").digest(), "big"
    ) % candidates or 1
    while math.gcd(stride, candidates) != 1:
        stride = (stride + 1) % candidates or 1
    expected_positions = {}
    for trajectory in range(trajectories):
        code = (offset + trajectory * stride) % candidates
        choices = []
        for name in names:
            code, digit = divmod(code, 2 * (len(domains[name]) - 1))
            choices.append((digit // 2, 1 if digit % 2 == 0 else -1))
        available, permutation = list(range(len(names))), []
        for remaining in range(len(names), 0, -1):
            block = math.factorial(remaining - 1)
            index, code = divmod(code, block)
            permutation.append(available.pop(index))
        point = [edge if direction == 1 else edge + 1 for edge, direction in choices]
        points = [tuple(point)]
        for factor_index in permutation:
            point = list(point)
            point[factor_index] += choices[factor_index][1]
            points.append(tuple(point))
        for rank, indices in enumerate(points):
            role = (
                f"trajectory:{trajectory}:rank:0:start"
                if rank == 0
                else f"trajectory:{trajectory}:rank:{rank}:factor:"
                f"{names[permutation[rank - 1]]}"
            )
            expected_positions[role] = tuple(
                domains[name][index] for name, index in zip(names, indices)
            )
    observations = {}
    for run_id, indicator, value in rows:
        key = run_id, indicator
        if key in observations:
            raise ValueError(
                f"multiple indicator values for runId {run_id}, indicator {indicator}"
            )
        observations[key] = value
    indicators = tuple(sorted({indicator for _, indicator, _ in rows}))
    groups, plan_groups, identifiers, replications = {}, {}, set(), {}
    config_runs, run_configs = {}, {}
    seeds = {row["seed"] for row in plan.to_pylist()}
    if len(plan) != trajectories * (len(names) + 1) * len(seeds):
        raise ValueError("Morris plan cardinality is inconsistent")
    for location in plan.to_pylist():
        if location["contextHash"] != context_hash:
            raise ValueError("Morris contextHash is invalid or inconsistent")
        design_id = location["designId"]
        if (
            re.fullmatch(r"[0-9a-f]{32}", design_id or "") is None
            or design_id in identifiers
        ):
            raise ValueError("Morris designId must be unique lowercase hexadecimal")
        identifiers.add(design_id)
        match = MORRIS_ROLE.fullmatch(location["role"])
        if match is None:
            raise ValueError(f"invalid Morris role: {location['role']!r}")
        trajectory, rank = int(match.group(1)), int(match.group(2))
        factor = match.group(4)
        if trajectory >= trajectories or (rank == 0) != (factor is None):
            raise ValueError(f"invalid Morris rank or role: {location['role']!r}")
        actual = tuple(location[name] for name in names)
        if (
            location["role"] not in expected_positions
            or actual != expected_positions[location["role"]]
        ):
            raise ValueError("Morris plan value does not match its sealed trajectory")
        matching = [
            index
            for index in range(len(seeds))
            if hashlib.sha256(
                domain
                + json.dumps(
                    {
                        "design": design,
                        "replicationIndex": index,
                        "role": location["role"],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
            ).hexdigest()[:32]
            == design_id
        ]
        if (
            len(matching) != 1
            or replications.setdefault(location["seed"], matching[0]) != matching[0]
        ):
            raise ValueError(f"{label} designId does not match role and replication")
        if location["method"] != method:
            raise ValueError(f"{label} method identity is invalid")
        run_id = location["runId"]
        configuration = (*actual, location["seed"])
        if (
            re.fullmatch(r"[0-9a-f]{32}", run_id or "") is None
            or config_runs.setdefault(configuration, run_id) != run_id
            or run_configs.setdefault(run_id, configuration) != configuration
        ):
            raise ValueError(f"{label} runId relation is inconsistent")
        plan_groups.setdefault((trajectory, location["seed"]), []).append(
            (rank, factor, location)
        )
        for indicator in indicators:
            key = location["runId"], indicator
            if key not in observations:
                raise ValueError(
                    f"runId {location['runId']} has no indicator {indicator}"
                )
            groups.setdefault((indicator, trajectory, location["seed"]), []).append(
                (rank, factor, location, observations[key])
            )
    dimension = len(names)
    signatures = {}
    for (trajectory, _seed), points in plan_groups.items():
        points.sort(key=lambda item: item[0])
        if [point[0] for point in points] != list(range(dimension + 1)):
            raise ValueError("Morris trajectory has missing or duplicate ranks")
        factor_order = tuple(point[1] for point in points[1:])
        if len(set(factor_order)) != dimension or set(factor_order) != set(names):
            raise ValueError("Morris trajectory must change each factor exactly once")
        signature = (
            factor_order,
            tuple(tuple(point[2][name] for name in names) for point in points),
        )
        if signatures.setdefault(trajectory, signature) != signature:
            raise ValueError("Morris trajectory differs between replications")
        for before, after in zip(points, points[1:]):
            changed = [name for name in names if before[2][name] != after[2][name]]
            if changed != [after[1]]:
                raise ValueError(
                    "Morris transition does not change exactly its named factor"
                )
    if (
        len(plan_groups) != trajectories * len(seeds)
        or set(signatures) != set(range(trajectories))
        or len(set(signatures.values())) != trajectories
    ):
        raise ValueError("Morris trajectory candidate signatures must be unique")
    effects = {}
    for (indicator, trajectory, seed), points in groups.items():
        points.sort(key=lambda item: item[0])
        if [point[0] for point in points] != list(range(dimension + 1)):
            raise ValueError("Morris trajectory has missing or duplicate ranks")
        factor_order = tuple(point[1] for point in points[1:])
        if len(set(factor_order)) != dimension or set(factor_order) != set(names):
            raise ValueError("Morris trajectory must change each factor exactly once")
        for before, after in zip(points, points[1:]):
            factor = after[1]
            changed = [name for name in names if before[2][name] != after[2][name]]
            if changed != [factor]:
                raise ValueError(
                    "Morris transition does not change exactly its named factor"
                )
            domain = domains[factor]
            left, right = before[2][factor], after[2][factor]
            if abs(domain.index(left) - domain.index(right)) != 1:
                raise ValueError(
                    "Morris transition is not between adjacent domain levels"
                )
            delta = (right - left) / (domain[-1] - domain[0])
            effect = (after[3] - before[3]) / delta
            if not math.isfinite(effect):
                raise ValueError("Morris elementary effect is not finite")
            effects.setdefault((indicator, factor), []).append(
                (trajectory, seed, effect)
            )
    result = []
    for indicator in indicators:
        statistics_by_factor = []
        for factor in names:
            entries = effects.get((indicator, factor), [])
            if len(entries) != trajectories * len(seeds):
                raise ValueError("Morris elementary effects are incomplete")
            values = [entry[2] for entry in entries]
            mu, mu_star = statistics.fmean(values), statistics.fmean(map(abs, values))
            sigma = statistics.stdev(values) if len(values) > 1 else None
            seed_std = None
            if len(seeds) > 1:
                variances = [
                    statistics.variance(
                        entry[2] for entry in entries if entry[0] == trajectory
                    )
                    for trajectory in range(trajectories)
                ]
                seed_std = math.sqrt(statistics.fmean(variances))
            if not all(
                math.isfinite(value)
                for value in (mu, mu_star, sigma, seed_std)
                if value is not None
            ):
                raise ValueError("Morris aggregate is not finite")
            statistics_by_factor.append((factor, mu, mu_star, sigma, seed_std))
        statistics_by_factor.sort(key=lambda item: (-item[2], item[0]))
        for rank, values in enumerate(statistics_by_factor, 1):
            factor, mu, mu_star, sigma, seed_std = values
            result.append(
                (
                    indicator,
                    factor,
                    rank,
                    trajectories,
                    len(seeds),
                    trajectories * len(seeds),
                    mu,
                    mu_star,
                    sigma,
                    seed_std,
                )
            )
    schema = pa.schema(
        [
            pa.field("indicator", pa.string(), False),
            pa.field("factor", pa.string(), False),
            pa.field("rank", pa.int64(), False),
            pa.field("trajectories", pa.int64(), False),
            pa.field("replications", pa.int64(), False),
            pa.field("n", pa.int64(), False),
            pa.field("mu", pa.float64(), False),
            pa.field("mu_star", pa.float64(), False),
            pa.field("sigma", pa.float64()),
            pa.field("seed_std", pa.float64()),
        ]
    )
    return pa.Table.from_arrays(
        [
            pa.array((row[index] for row in result), type=field.type)
            for index, field in enumerate(schema)
        ],
        schema=schema,
    )


def _canonical_morris_tables(
    plan: pa.Table,
    rows: list[tuple[str, str, float]],
    bootstrap_resamples: int,
    confidence_level: float,
    convergence_checkpoints: tuple[int, ...],
    screening_top_k: int,
    *,
    include_noise_floor: bool = False,
) -> tuple[pa.Table, pa.Table, pa.Table] | tuple[pa.Table, pa.Table, pa.Table, pa.Table]:
    metadata = plan.schema.metadata or {}
    required_metadata = {
        b"planSchemaVersion",
        b"canonicalizationVersion",
        b"contextDescriptor",
        b"canonicalMorrisDesignVersion",
        b"canonicalMorrisDesign",
    }
    if (
        set(metadata) != required_metadata
        or metadata.get(b"planSchemaVersion") != b"2"
        or metadata.get(b"canonicalizationVersion") != b"1"
        or metadata.get(b"canonicalMorrisDesignVersion") != b"3"
    ):
        raise ValueError("canonical Morris plan metadata is missing or unsupported")
    try:
        design = json.loads(metadata[b"canonicalMorrisDesign"])
        descriptor = json.loads(metadata[b"contextDescriptor"])
        design_bytes = _json(design)
        descriptor_bytes = _json(descriptor)
    except Exception as error:
        raise ValueError("canonical Morris metadata is invalid") from error
    if (
        metadata[b"canonicalMorrisDesign"] != design_bytes
        or metadata[b"contextDescriptor"] != descriptor_bytes
    ):
        raise ValueError("canonical Morris metadata is not canonical")
    context_keys = {
        "eqasimCommit",
        "dataManifestSha256",
        "lockfileSha256",
        "eqasimSaVersion",
    }
    if (
        not isinstance(descriptor, dict)
        or set(descriptor) != context_keys
        or any(type(value) is not str for value in descriptor.values())
    ):
        raise ValueError(
            "canonical Morris contextDescriptor must contain exactly four string keys"
        )
    definition_keys = {
        "bootstrapResamples",
        "candidateDefinition",
        "confidenceLevel",
        "convergenceCheckpoints",
        "delta",
        "distributions",
        "factors",
        "inverseCdfMap",
        "numericalEngine",
        "p",
        "replicationBaseSeeds",
        "scientificContract",
        "screeningTopK",
        "seedDerivation",
        "selectedTrajectories",
        "trajectories",
    }
    if not isinstance(design, dict) or set(design) != definition_keys:
        raise ValueError("canonical Morris design metadata keys are invalid")

    factors = design["factors"]
    distributions = design["distributions"]
    if not isinstance(factors, list) or not factors or not isinstance(distributions, list):
        raise ValueError("canonical Morris factors or distributions are invalid")
    supports, distribution_by_name, support_types = {}, {}, {}
    for factor in factors:
        if not isinstance(factor, dict) or set(factor) != {"name", "support"}:
            raise ValueError("canonical Morris factor definition is invalid")
        name, support = factor["name"], factor["support"]
        if (
            type(name) is not str
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
            or name in supports
            or name in {"designId", "method", "role", "runId", "seed", "contextHash"}
            or not isinstance(support, list)
            or len(support) < 2
        ):
            raise ValueError("canonical Morris factor support is invalid")
        kind = type(support[0])
        if (
            kind not in (int, float, str)
            or any(type(value) is not kind for value in support)
            or len(set(support)) != len(support)
            or kind is int
            and any(not -(2**63) <= value < 2**63 for value in support)
            or kind is float
            and any(not math.isfinite(value) for value in support)
            or kind is str
            and any(not value or "\0" in value for value in support)
        ):
            raise ValueError("canonical Morris supports must contain distinct finite scalars")
        supports[name] = support
        support_types[name] = {int: pa.int64(), float: pa.float64(), str: pa.string()}[
            kind
        ]
    names = tuple(supports)
    if names != tuple(sorted(names)):
        raise ValueError("canonical Morris factors must be in lexicographic order")
    for distribution in distributions:
        if (
            not isinstance(distribution, dict)
            or set(distribution) != {"name", "family", "transform", "provenance"}
            or distribution.get("name") in distribution_by_name
            or distribution.get("family") != "discrete_uniform"
            or distribution.get("transform") != "identity"
            or type(distribution.get("provenance")) is not str
            or not distribution["provenance"]
        ):
            raise ValueError("canonical Morris distribution definition is invalid")
        distribution_by_name[distribution["name"]] = {
            key: distribution[key] for key in ("family", "transform", "provenance")
        }
    if tuple(distribution_by_name) != names:
        raise ValueError("canonical Morris distributions must match sorted factors")

    p = design["p"]
    trajectories = design["trajectories"]
    candidate_definition = design["candidateDefinition"]
    candidate_count = (
        candidate_definition.get("candidateTrajectories")
        if isinstance(candidate_definition, dict)
        else None
    )
    candidate_seed = (
        candidate_definition.get("designSeed")
        if isinstance(candidate_definition, dict)
        else None
    )
    candidate_expected = {
        "designSeed": candidate_seed,
        "factors": [
            {
                "name": name,
                "support": supports[name],
                "distribution": distribution_by_name[name],
            }
            for name in names
        ],
        "levels": p,
        "candidateTrajectories": candidate_count,
    }
    universe = math.factorial(len(names)) * p ** len(names) if type(p) is int else 0
    if (
        not isinstance(candidate_definition, dict)
        or set(candidate_definition)
        != {"designSeed", "factors", "levels", "candidateTrajectories"}
        or candidate_definition != candidate_expected
        or type(candidate_definition["designSeed"]) is not int
        or not -(2**63) <= candidate_definition["designSeed"] < 2**63
        or type(p) is not int
        or p < 4
        or p % 2
        or type(trajectories) is not int
        or trajectories < 2
        or type(candidate_count) is not int
        or not trajectories < candidate_count <= min(
            universe, MAX_CANONICAL_MORRIS_CANDIDATES
        )
    ):
        raise ValueError("canonical Morris candidate definition is invalid")
    if canonical_morris_distance_terms(
        len(names), candidate_count, trajectories
    ) > MAX_CANONICAL_MORRIS_DISTANCE_TERMS:
        raise ValueError(
            "canonical Morris candidate selection exceeds the deterministic "
            "distance-computation budget"
        )
    base_seeds = design["replicationBaseSeeds"]
    checkpoints = design["convergenceCheckpoints"]
    validate_bootstrap_resamples(
        design["bootstrapResamples"],
        method="canonical Morris",
        minimum=100,
    )
    if (
        not isinstance(base_seeds, list)
        or not base_seeds
        or any(type(seed) is not int or not -(2**63) <= seed < 2**63 for seed in base_seeds)
        or len(set(base_seeds)) != len(base_seeds)
        or type(design["confidenceLevel"]) is not float
        or not math.isfinite(design["confidenceLevel"])
        or not 0 < design["confidenceLevel"] < 1
        or not isinstance(checkpoints, list)
        or not checkpoints
        or any(type(value) is not int or value < 2 for value in checkpoints)
        or sorted(set(checkpoints)) != checkpoints
        or checkpoints[-1] != trajectories
        or type(design["screeningTopK"]) is not int
        or not 1 <= design["screeningTopK"] <= len(names)
    ):
        raise ValueError("canonical Morris sealed analysis settings are invalid")
    if (
        bootstrap_resamples != design["bootstrapResamples"]
        or confidence_level != design["confidenceLevel"]
        or list(convergence_checkpoints) != checkpoints
        or screening_top_k != design["screeningTopK"]
    ):
        raise ValueError("canonical Morris requested analysis settings do not match the plan")
    validate_canonical_morris_bootstrap_work(
        design["bootstrapResamples"],
        len(names),
        trajectories,
        len(base_seeds),
        checkpoints,
    )
    if (
        design["delta"] != {"numerator": p // 2, "denominator": p - 1}
        or design["inverseCdfMap"]
        != "discrete-uniform-generalized-inverse-v1"
        or design["numericalEngine"]
        != engine_descriptor(
            "canonical_morris",
            sample_size=candidate_count,
            sample_seeds=(candidate_seed,),
            levels=p,
            selected_trajectories=trajectories,
        )
        or design["seedDerivation"]
        != {
            "algorithm": "sha256-first-64-bits-signed",
            "domain": "eqasim-sa:canonical-morris-seed:v1",
            "fields": [
                "version",
                "designSeed",
                "baseSeed",
                "trajectoryId",
                "replicationIndex",
            ],
            "version": 1,
        }
    ):
        raise ValueError("canonical Morris algorithm versions or delta are invalid")
    validate_scientific_contract(
        design["scientificContract"], supports, distribution_by_name
    )
    if (
        design["scientificContract"]["software"]["eqasimSaVersion"]
        != descriptor["eqasimSaVersion"]
    ):
        raise ValueError(
            "canonical Morris scientific software version does not match contextDescriptor"
        )
    validate_scientific_stopping(
        "canonical_morris",
        design["scientificContract"]["charter"]["stopping"],
        len(checkpoints),
    )
    stochastic = design["scientificContract"]["charter"]["stochastic"]
    if stochastic["target"] != "conditional_mean":
        raise ValueError("canonical Morris requires sealed conditional_mean stochastic target")
    allocation = stochastic["allocation"]
    if allocation["phase"] == "pilot":
        if len(base_seeds) < allocation["minimum_replications"]:
            raise ValueError(
                "canonical Morris pilot replications are below sealed minimum_replications"
            )
    elif len(base_seeds) != allocation["selected_replications"]:
        raise ValueError(
            "canonical Morris production replications do not equal selected_replications"
        )
    matrices = morris_unit_trajectories(
        names,
        candidate_trajectories=candidate_count,
        selected_trajectories=trajectories,
        levels=p,
        seed=candidate_definition["designSeed"],
    )
    expected_selected = [
        {
            "matrix": matrix,
            "trajectoryId": hashlib.sha256(
                CANONICAL_MORRIS_DOMAIN
                + _json({"matrix": matrix, "selectedIndex": index})
            ).hexdigest(),
        }
        for index, matrix in enumerate(matrices)
    ]
    if design["selectedTrajectories"] != expected_selected:
        raise ValueError("canonical Morris SALib-selected trajectories are invalid")
    selected = [
        (item["trajectoryId"], item["matrix"])
        for item in expected_selected
    ]

    columns = [name for name in names if name != "seed"]
    expected_schema = pa.schema(
        [
            *(pa.field(name, pa.string(), False) for name in ("designId", "method", "role", "runId")),
            pa.field("seed", pa.int64(), False),
            pa.field("contextHash", pa.string(), False),
            *(pa.field(name, support_types[name], False) for name in columns),
        ]
    )
    if plan.column_names != ["designId", "method", "role", "runId", "seed", "contextHash", *columns] or not plan.schema.remove_metadata().equals(expected_schema):
        raise ValueError("canonical Morris plan schema does not match sealed supports")
    context_hash = hashlib.sha256(
        b"eqasim-sa:context:v1\0" + descriptor_bytes
    ).hexdigest()
    expected_locations, derived_seeds = {}, set()
    half = p // 2
    for trajectory, (trajectory_id, matrix) in enumerate(selected):
        if any(
            type(value) is not int or not 0 <= value < p
            for point in matrix
            for value in point
        ):
            raise ValueError("canonical Morris unit matrix is outside its level grid")
        changed_factors = []
        for before, after in zip(matrix, matrix[1:]):
            changed = [
                index
                for index, (left, right) in enumerate(zip(before, after))
                if left != right
            ]
            if len(changed) != 1 or after[changed[0]] - before[changed[0]] not in {-half, half}:
                raise ValueError("canonical Morris unit step must change one coordinate by p/2")
            changed_factors.append(changed[0])
        if sorted(changed_factors) != list(range(len(names))):
            raise ValueError("canonical Morris trajectory must change every factor once")
        mapped = [
            tuple(
                supports[name][
                    _discrete_uniform_generalized_inverse_index(
                        len(supports[name]), p, grid_index
                    )
                ]
                for name, grid_index in zip(names, point)
            )
            for point in matrix
        ]
        for rank, (before, after) in enumerate(zip(mapped, mapped[1:]), 1):
            changed = [
                index
                for index, (left, right) in enumerate(zip(before, after))
                if left != right
            ]
            if changed != [changed_factors[rank - 1]]:
                raise ValueError(
                    "canonical Morris mapped step must change exactly its named factor"
                )
        for replication, base_seed in enumerate(base_seeds):
            seed = _canonical_replication_seed(
                candidate_definition["designSeed"], base_seed, trajectory_id, replication
            )
            if seed in derived_seeds:
                raise ValueError("canonical Morris derived seed collision between blocks")
            derived_seeds.add(seed)
            for rank, values in enumerate(mapped):
                role = (
                    f"trajectory:{trajectory}:rank:0:start"
                    if rank == 0
                    else f"trajectory:{trajectory}:rank:{rank}:factor:{names[changed_factors[rank - 1]]}"
                )
                preimage = {
                    "design": design,
                    "replicationIndex": replication,
                    "role": role,
                }
                design_id = hashlib.sha256(
                    CANONICAL_MORRIS_DOMAIN + _json(preimage)
                ).hexdigest()[:32]
                expected_locations[design_id] = (
                    trajectory,
                    replication,
                    rank,
                    role,
                    seed,
                    values,
                    matrix,
                )
    if len(plan) != trajectories * (len(names) + 1) * len(base_seeds):
        raise ValueError("canonical Morris plan cardinality is inconsistent")
    locations, config_runs, run_configs = {}, {}, {}
    for location in plan.to_pylist():
        design_id = location["designId"]
        if design_id in locations or design_id not in expected_locations:
            raise ValueError("canonical Morris designId is missing, duplicate or invalid")
        trajectory, replication, rank, role, seed, values, matrix = expected_locations[
            design_id
        ]
        actual = tuple(location[name] for name in names)
        if (
            location["method"] != "canonical_morris"
            or location["role"] != role
            or location["seed"] != seed
            or location["contextHash"] != context_hash
            or actual != values
            or re.fullmatch(r"[0-9a-f]{32}", location["runId"] or "") is None
        ):
            raise ValueError("canonical Morris sealed row identity or mapped value is invalid")
        config = (*actual, seed)
        run_id = location["runId"]
        if (
            config_runs.setdefault(config, run_id) != run_id
            or run_configs.setdefault(run_id, config) != config
        ):
            raise ValueError("canonical Morris runId relation is inconsistent")
        locations[design_id] = {**location, "trajectory": trajectory, "replication": replication, "rank": rank, "matrix": matrix}
    if set(locations) != set(expected_locations):
        raise ValueError("canonical Morris plan rows are incomplete")

    observations = {}
    for run_id, indicator, value in rows:
        key = run_id, indicator
        if key in observations:
            raise ValueError("multiple indicator values for one canonical Morris runId")
        observations[key] = value
    indicators = tuple(sorted({indicator for _, indicator, _ in rows}))
    primary_indicator = design["scientificContract"]["charter"]["decision"][
        "primary_indicator"
    ]
    if include_noise_floor and primary_indicator not in indicators:
        raise ValueError("canonical Morris primary indicator is absent from analysis rows")
    noise_groups = {}
    noise_roles = {}
    for location in locations.values():
        point = location["trajectory"], location["rank"]
        noise_roles.setdefault(point, location["role"])
        if noise_roles[point] != location["role"]:
            raise ValueError("canonical Morris point role differs between replications")
        for indicator in indicators:
            try:
                value = observations[location["runId"], indicator]
            except KeyError as error:
                raise ValueError(
                    f"canonical Morris indicator {indicator!r} is incomplete"
                ) from error
            noise_groups.setdefault((indicator, *point), []).append(value)
    noise_rows = []
    for (indicator, trajectory, rank), values in sorted(noise_groups.items()):
        if len(values) != len(base_seeds):
            raise ValueError("canonical Morris noise-floor replications are incomplete")
        variance = statistics.variance(values) if len(values) > 1 else None
        intrinsic_sd = math.sqrt(variance) if variance is not None else None
        mcse = intrinsic_sd / math.sqrt(len(values)) if intrinsic_sd is not None else None
        noise_rows.append(
            (
                indicator,
                trajectory,
                rank,
                noise_roles[trajectory, rank],
                len(values),
                statistics.fmean(values),
                variance,
                intrinsic_sd,
                mcse,
                indicator == primary_indicator,
            )
        )
    noise_schema = pa.schema(
        [
            pa.field("indicator", pa.string(), False),
            pa.field("trajectory", pa.int64(), False),
            pa.field("rank", pa.int64(), False),
            pa.field("role", pa.string(), False),
            pa.field("replications", pa.int64(), False),
            pa.field("mean", pa.float64(), False),
            pa.field("intrinsic_variance", pa.float64()),
            pa.field("intrinsic_sd", pa.float64()),
            pa.field("mcse", pa.float64()),
            pa.field("is_primary_indicator", pa.bool_(), False),
        ]
    )
    noise_table = _rows_table(noise_rows, noise_schema)
    mean_outputs = {
        indicator: [
            [
                statistics.fmean(noise_groups[indicator, trajectory, rank])
                for rank in range(len(names) + 1)
            ]
            for trajectory in range(trajectories)
        ]
        for indicator in indicators
    }
    effects = {
        indicator: {
            factor: [[None for _ in base_seeds] for _ in range(trajectories)]
            for factor in names
        }
        for indicator in indicators
    }
    by_block = {}
    for location in locations.values():
        by_block.setdefault(
            (location["trajectory"], location["replication"]), []
        ).append(location)
    delta = half / (p - 1)
    for (trajectory, replication), points in by_block.items():
        points.sort(key=lambda item: item["rank"])
        if [point["rank"] for point in points] != list(range(len(names) + 1)):
            raise ValueError("canonical Morris trajectory ranks are incomplete")
        matrix = points[0]["matrix"]
        for before, after in zip(points, points[1:]):
            factor = after["role"].split(":")[-1]
            factor_index = names.index(factor)
            direction = (matrix[after["rank"]][factor_index] - matrix[before["rank"]][factor_index]) // half
            for indicator in indicators:
                try:
                    left = observations[(before["runId"], indicator)]
                    right = observations[(after["runId"], indicator)]
                except KeyError as error:
                    raise ValueError(
                        f"canonical Morris indicator {indicator!r} is incomplete"
                    ) from error
                effect = (right - left) / (direction * delta)
                if not math.isfinite(effect):
                    raise ValueError("canonical Morris elementary effect is not finite")
                effects[indicator][factor][trajectory][replication] = effect

    ranking_rows, convergence_rows, reversal_rows = [], [], []
    for indicator in indicators:
        for checkpoint in checkpoints:
            estimates, boot_mu_star, boot_ranks, boot_top_k = _canonical_checkpoint_statistics(
                effects[indicator],
                names,
                checkpoint,
                bootstrap_resamples,
                confidence_level,
                screening_top_k,
                design_bytes,
                indicator,
                matrices,
                mean_outputs[indicator],
                p,
            )
            ordered = sorted(names, key=lambda name: (-estimates[name][1], name))
            point_ranks = _average_ranks(
                {factor: estimates[factor][1] for factor in names}
            )
            for factor in ordered:
                mu, mu_star, sigma, seed_std = estimates[factor]
                samples = boot_mu_star[factor]
                alpha = 1 - confidence_level
                ci_low, ci_high = np.quantile(
                    samples, (alpha / 2, 1 - alpha / 2)
                )  # NumPy's default linear percentile convention.
                rank_low = float(
                    np.quantile(boot_ranks[factor], alpha / 2, method="lower")
                )
                rank_high = float(
                    np.quantile(
                        boot_ranks[factor], 1 - alpha / 2, method="higher"
                    )
                )
                top_probability = statistics.fmean(boot_top_k[factor])
                values = (
                    indicator,
                    factor,
                    point_ranks[factor],
                    rank_low,
                    rank_high,
                    top_probability,
                    checkpoint,
                    len(base_seeds),
                    checkpoint * len(base_seeds),
                    delta,
                    mu,
                    mu_star,
                    float(ci_low),
                    float(ci_high),
                    sigma,
                    seed_std,
                    bootstrap_resamples,
                    confidence_level,
                )
                convergence_rows.append(values)
                if checkpoint == trajectories:
                    ranking_rows.append(values)
            for left_index, factor_a in enumerate(ordered):
                for factor_b in ordered[left_index + 1 :]:
                    point_tie = estimates[factor_a][1] == estimates[factor_b][1]
                    point_order = 0 if point_tie else 1
                    reversal_probability = (
                        None
                        if point_tie
                        else statistics.fmean(
                            left < right
                            for left, right in zip(
                                boot_mu_star[factor_a], boot_mu_star[factor_b]
                            )
                        )
                    )
                    tie_probability = statistics.fmean(
                        left == right
                        for left, right in zip(
                            boot_mu_star[factor_a], boot_mu_star[factor_b]
                        )
                    )
                    reversal_rows.append(
                        (
                            indicator,
                            checkpoint,
                            factor_a,
                            factor_b,
                            point_order,
                            reversal_probability,
                            tie_probability,
                            bootstrap_resamples,
                        )
                    )
    convergence_schema = _canonical_effect_schema("checkpoint_trajectories")
    ranking_schema = _canonical_effect_schema("trajectories")
    reversal_schema = pa.schema(
        [
            pa.field("indicator", pa.string(), False),
            pa.field("checkpoint_trajectories", pa.int64(), False),
            pa.field("factor_a", pa.string(), False),
            pa.field("factor_b", pa.string(), False),
            pa.field("point_order", pa.int64(), False),
            pa.field("reversal_probability", pa.float64()),
            pa.field("tie_probability", pa.float64(), False),
            pa.field("bootstrap_resamples", pa.int64(), False),
        ]
    )
    result = (
        _rows_table(ranking_rows, ranking_schema),
        _rows_table(convergence_rows, convergence_schema),
        _rows_table(reversal_rows, reversal_schema),
    )
    return (*result, noise_table) if include_noise_floor else result


def _replication_allocation(design: dict, noise_floor: pa.Table) -> dict:
    stochastic = design["scientificContract"]["charter"]["stochastic"]
    allocation = stochastic["allocation"]
    if stochastic["target"] != "conditional_mean" or allocation["phase"] != "pilot":
        raise ValueError("replication allocation requires a canonical Morris pilot")
    primary_indicator = design["scientificContract"]["charter"]["decision"][
        "primary_indicator"
    ]
    variances = np.asarray(
        [
            row["intrinsic_variance"]
            for row in noise_floor.to_pylist()
            if row["indicator"] == primary_indicator
        ],
        dtype=np.float64,
    )
    if not len(variances) or not np.all(np.isfinite(variances)):
        raise ValueError("primary-indicator noise-floor variances are incomplete")
    quantile_value = float(np.quantile(variances, allocation["variance_quantile"]))
    ratio = Decimal(str(quantile_value)) / Decimal(str(allocation["target_mcse"])) ** 2
    raw_required = max(allocation["minimum_replications"], math.ceil(ratio))
    selected = min(raw_required, allocation["maximum_replications"])
    positions = design["trajectories"] * (len(design["factors"]) + 1) * selected
    budget = design["scientificContract"]["charter"]["budget"]
    available = int(
        Decimal(budget["maximum_runs"])
        * (Decimal(1) - Decimal(str(budget["reserve_fraction"])))
    )
    status = (
        "infeasible"
        if positions > available
        else "capped"
        if raw_required > allocation["maximum_replications"]
        else "recommended"
    )
    current = len(design["replicationBaseSeeds"])
    return {
        "version": 1,
        "status": status,
        "primaryIndicator": primary_indicator,
        "varianceQuantile": allocation["variance_quantile"],
        "varianceQuantileValue": quantile_value,
        "targetMcse": allocation["target_mcse"],
        "rawRequiredReplications": raw_required,
        "selectedReplications": selected,
        "currentReplications": current,
        "maximumReplications": allocation["maximum_replications"],
        "estimatedProductionPositions": positions,
        "availableRuns": available,
        "withinTarget": current >= raw_required,
        "seedPolicy": "canonical-morris-derived-crn-v1",
    }


def _canonical_checkpoint_statistics(
    effects: dict[str, list[list[float]]],
    names: tuple[str, ...],
    checkpoint: int,
    bootstrap_resamples: int,
    confidence_level: float,
    screening_top_k: int,
    design_bytes: bytes,
    indicator: str,
    unit_matrices: list[list[list[int]]],
    point_outputs: list[list[float]],
    levels: int,
) -> tuple[dict, dict, dict, dict]:
    del confidence_level
    unit_inputs = (
        np.asarray(unit_matrices[:checkpoint], dtype=np.float64).reshape(
            -1, len(names)
        )
        / (levels - 1)
    )
    outputs = np.asarray(point_outputs[:checkpoint], dtype=np.float64).reshape(-1)
    point_statistics = morris_statistics(
        names,
        unit_inputs,
        outputs,
        levels=levels,
    )
    trajectory_means, estimates = {}, {}
    for factor in names:
        values = [list(row) for row in effects[factor][:checkpoint]]
        if any(any(value is None for value in row) for row in values):
            raise ValueError("canonical Morris elementary effects are incomplete")
        means = [statistics.fmean(row) for row in values]
        trajectory_means[factor] = means
        mu, mu_star, sigma = point_statistics[factor]
        seed_std = None
        if len(values[0]) > 1:
            seed_std = math.sqrt(
                statistics.fmean(statistics.variance(row) for row in values)
            )
        estimates[factor] = (mu, mu_star, sigma, seed_std)
    seed = int.from_bytes(
        hashlib.sha256(
            CANONICAL_MORRIS_BOOTSTRAP_DOMAIN
            + design_bytes
            + b"\0"
            + indicator.encode("utf-8")
            + b"\0"
            + str(checkpoint).encode("ascii")
        ).digest()[:16],
        "big",
    )
    rng = np.random.default_rng(seed)
    boot_mu_star = {factor: [] for factor in names}
    boot_ranks = {factor: [] for factor in names}
    boot_top_k = {factor: [] for factor in names}
    for _ in range(bootstrap_resamples):
        selected = rng.integers(0, checkpoint, size=checkpoint)
        current = {
            factor: statistics.fmean(
                abs(trajectory_means[factor][int(index)]) for index in selected
            )
            for factor in names
        }
        ranks = _average_ranks(current)
        top_k = _top_k_weights(current, screening_top_k)
        for factor in names:
            boot_mu_star[factor].append(current[factor])
            boot_ranks[factor].append(ranks[factor])
            boot_top_k[factor].append(top_k[factor])
    return estimates, boot_mu_star, boot_ranks, boot_top_k


def _average_ranks(values: dict[str, float]) -> dict[str, float]:
    return {
        factor: 1.0
        + sum(other > value for other in values.values())
        + (sum(other == value for other in values.values()) - 1) / 2
        for factor, value in values.items()
    }


def _top_k_weights(values: dict[str, float], top_k: int) -> dict[str, float]:
    weights = {factor: 0.0 for factor in values}
    remaining = top_k
    for value in sorted(set(values.values()), reverse=True):
        group = [factor for factor, current in values.items() if current == value]
        if remaining >= len(group):
            weight = 1.0
        elif remaining > 0:
            weight = remaining / len(group)
        else:
            weight = 0.0
        for factor in group:
            weights[factor] = weight
        remaining = max(0, remaining - len(group))
    return weights


def _canonical_effect_schema(trajectory_name: str) -> pa.Schema:
    return pa.schema(
        [
            pa.field("indicator", pa.string(), False),
            pa.field("factor", pa.string(), False),
            pa.field("rank", pa.float64(), False),
            pa.field("rank_ci_low", pa.float64(), False),
            pa.field("rank_ci_high", pa.float64(), False),
            pa.field("top_k_probability", pa.float64(), False),
            pa.field(trajectory_name, pa.int64(), False),
            pa.field("replications", pa.int64(), False),
            pa.field("elementary_effects", pa.int64(), False),
            pa.field("delta", pa.float64(), False),
            pa.field("mu", pa.float64(), False),
            pa.field("mu_star", pa.float64(), False),
            pa.field("mu_star_ci_low", pa.float64(), False),
            pa.field("mu_star_ci_high", pa.float64(), False),
            pa.field("sigma", pa.float64()),
            pa.field("seed_std", pa.float64()),
            pa.field("bootstrap_resamples", pa.int64(), False),
            pa.field("confidence_level", pa.float64(), False),
        ]
    )


def _rows_table(rows: list[tuple], schema: pa.Schema) -> pa.Table:
    return pa.Table.from_arrays(
        [
            pa.array((row[index] for row in rows), type=field.type)
            for index, field in enumerate(schema)
        ],
        schema=schema,
    )


def _failure_ids(campaign_root: Path, incomplete: set[str]) -> set[str]:
    logs = campaign_root / "logs"
    if not logs.exists() and not logs.is_symlink():
        return set()
    if logs.is_symlink() or not logs.is_dir():
        raise ValueError("logs must be a regular non-symlink directory")
    failed = set()
    for run_id in incomplete:
        path = logs / f"{run_id}.log"
        if not path.exists() and not path.is_symlink():
            continue
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
                raise ValueError("must be a non-empty regular file")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
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
    table = pq.read_table(plan)
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
    return pa.Table.from_arrays(
        [
            pa.array((row[index] for row in result), type=field.type)
            for index, field in enumerate(schema)
        ],
        schema=schema,
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
