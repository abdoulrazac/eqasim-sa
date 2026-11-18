#!/usr/bin/env python3
"""Render and validate one Nantes scientific protocol declaration."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from eqasim_sa.adapters.eqasim.config import load_yaml_document
from eqasim_sa.adapters.eqasim.source_fingerprint import (
    eqasim_source_exclusions,
    model_source_sha256,
)
from eqasim_sa.core.sensitivity import engine_descriptor, morris_unit_trajectories
from eqasim_sa.core.software_identity import read_software_identity
from eqasim_sa.utils import platform
from eqasim_sa.utils.filesystem import resolve_root
from eqasim_sa.utils.text_identity import canonical_json_sha256, lf_sha256


DIGEST = re.compile(r"[0-9a-f]{64}")
SEED_POOL = list(range(45001, 45021))
PRIMARY_INDICATOR = "tv_distance(age, ref=INSEE)"
SCREENING_FACTORS = {
    "matching_minimum_observations",
    "matching_minimum_age",
}
FACTOR_SUPPORTS = [
    {"name": "matching_minimum_age", "support": [0, 5, 12]},
    {"name": "matching_minimum_observations", "support": [5, 20, 50]},
]
CANONICAL_DESIGN_KEYS = {
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
ALLOCATION_KEYS = {
    "version",
    "status",
    "primaryIndicator",
    "varianceQuantile",
    "varianceQuantileValue",
    "targetMcse",
    "rawRequiredReplications",
    "selectedReplications",
    "currentReplications",
    "maximumReplications",
    "estimatedProductionPositions",
    "availableRuns",
    "withinTarget",
    "seedPolicy",
}
FIXED_MODEL_VALUES = {
    "sampling_rate": 0.001,
    "matching_age_scheme": "matching_6",
    "analysis_age_scheme": "analysis_7",
    "income_assignation_method": "uniform",
    "hts": "edgt_44",
    "departments": [44],
}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--model-config", type=Path, required=True)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--data-manifest", type=Path, required=True)
    parser.add_argument("--lockfile", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--data-path", type=Path, required=True)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--pilot-campaign-root", type=Path)
    parser.add_argument("--screening-campaign-root", type=Path)
    return parser.parse_args()


def _absolute(path: Path, name: str) -> str:
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return str(path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _evidence_root(root: Path | None, argument: str) -> Path:
    if root is None:
        raise ValueError(f"{argument} is required for this protocol")
    _absolute(root, argument.removeprefix("--").replace("-", " "))
    # Resolve once: linked ancestors are accepted; links below are refused by
    # the per-file checks.
    resolved = resolve_root(root, "evidence root")
    if not resolved.is_dir():
        raise ValueError(f"evidence root must be a directory: {root}")
    return resolved


def _context_descriptor(arguments: argparse.Namespace) -> dict[str, str]:
    model_document = load_yaml_document(arguments.model_config)
    software = read_software_identity()
    return {
        "eqasimSourceSha256": model_source_sha256(
            arguments.model_root,
            eqasim_source_exclusions(arguments.model_root, model_document),
        ),
        "dataManifestSha256": canonical_json_sha256(
            arguments.data_manifest.read_bytes()
        ),
        "lockfileSha256": lf_sha256(arguments.lockfile),
        "eqasimSaVersion": software.version,
    }


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number {value}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"malformed JSON evidence {path}: {error}") from error
    if not isinstance(document, dict):
        raise ValueError(f"JSON evidence must contain an object: {path}")
    _finite_json(document, str(path))
    return document


def _json_file(root: Path | None, filename: str, argument: str) -> tuple[dict[str, Any], Path]:
    root = _evidence_root(root, argument)
    path = root / filename
    if platform.is_link(path) or not path.is_file():
        raise ValueError(f"evidence must be a regular non-symlink file: {path}")
    document = _read_json_file(path)
    return document, path


def _safe_evidence_file(root: Path | None, relative: str, argument: str) -> Path:
    root = _evidence_root(root, argument)
    if (
        type(relative) is not str
        or not relative
        or "\\" in relative
        or "\0" in relative
    ):
        raise ValueError("manifest evidence path must be a safe relative POSIX path")
    pure = PurePosixPath(relative)
    if (
        pure.is_absolute()
        or pure.as_posix() != relative
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ValueError("manifest evidence path must be a safe relative POSIX path")
    current = root
    for index, part in enumerate(pure.parts):
        current /= part
        try:
            info = current.lstat()
        except OSError as error:
            raise ValueError(f"manifest evidence path is unavailable: {relative}") from error
        if stat.S_ISLNK(info.st_mode) or platform.is_link(current):
            raise ValueError(f"manifest evidence path must not traverse symlinks: {relative}")
        if index + 1 < len(pure.parts):
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"manifest evidence parent must be a directory: {relative}")
        elif not stat.S_ISREG(info.st_mode):
            raise ValueError(f"evidence must be a regular non-symlink file: {current}")
    return current


def _sealed_morris_plan(
    root: Path | None,
    argument: str,
    phase: str,
    expected_descriptor: dict[str, str],
) -> None:
    root = _evidence_root(root, argument)
    path = root / "plan.parquet"
    if platform.is_link(path) or not path.is_file():
        raise ValueError(f"plan.parquet must be a regular non-symlink file: {path}")
    try:
        table = pq.ParquetFile(path).read()
    except (OSError, pa.ArrowException) as error:
        raise ValueError(f"malformed plan.parquet: {path}: {error}") from error
    metadata = table.schema.metadata or {}
    expected_metadata = {
        b"planSchemaVersion",
        b"canonicalizationVersion",
        b"contextDescriptor",
        b"canonicalMorrisDesignVersion",
        b"canonicalMorrisDesign",
    }
    if (
        set(metadata) != expected_metadata
        or metadata.get(b"planSchemaVersion") != b"2"
        or metadata.get(b"canonicalizationVersion") != b"1"
        or metadata.get(b"canonicalMorrisDesignVersion") != b"3"
    ):
        raise ValueError("canonical Morris plan metadata is missing or unsupported")
    try:
        descriptor = json.loads(
            metadata[b"contextDescriptor"], object_pairs_hook=_object
        )
        design = json.loads(
            metadata[b"canonicalMorrisDesign"], object_pairs_hook=_object
        )
        descriptor_bytes = json.dumps(
            descriptor, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        design_bytes = json.dumps(
            design,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("canonical Morris plan metadata is malformed") from error
    if (
        metadata[b"contextDescriptor"] != descriptor_bytes
        or metadata[b"canonicalMorrisDesign"] != design_bytes
    ):
        raise ValueError("canonical Morris plan metadata is not canonical")
    if descriptor != expected_descriptor:
        raise ValueError("plan contextDescriptor does not match current renderer inputs")
    if not isinstance(design, dict) or set(design) != CANONICAL_DESIGN_KEYS:
        raise ValueError("canonical Morris design metadata keys are invalid")
    checkpoints = [8, 16, 24] if phase == "pilot" else [6, 12, 18, 24]
    if (
        design.get("factors") != FACTOR_SUPPORTS
        or design.get("trajectories") != 24
        or design.get("p") != 4
        or design.get("delta") != {"numerator": 2, "denominator": 3}
        or design.get("convergenceCheckpoints") != checkpoints
        or design.get("bootstrapResamples") != (999 if phase == "pilot" else 1999)
        or design.get("confidenceLevel") != 0.95
        or design.get("screeningTopK") != 2
        or design.get("inverseCdfMap") != "discrete-uniform-generalized-inverse-v1"
        or not isinstance(design.get("selectedTrajectories"), list)
        or len(design["selectedTrajectories"]) != 24
    ):
        raise ValueError("plan does not match the canonical Morris protocol geometry")
    candidate = design.get("candidateDefinition")
    if (
        not isinstance(candidate, dict)
        or set(candidate) != {"designSeed", "factors", "levels", "candidateTrajectories"}
        or candidate.get("levels") != 4
        or candidate.get("candidateTrajectories") != 32
        or not isinstance(candidate.get("factors"), list)
        or len(candidate["factors"]) != 2
        or any(not isinstance(item, dict) for item in candidate["factors"])
        or [
            {"name": item.get("name"), "support": item.get("support")}
            for item in candidate["factors"]
        ]
        != FACTOR_SUPPORTS
    ):
        raise ValueError("plan candidate design is incompatible with the protocol")
    if design.get("numericalEngine") != engine_descriptor(
        "canonical_morris",
        sample_size=candidate["candidateTrajectories"],
        sample_seeds=(candidate["designSeed"],),
        levels=candidate["levels"],
        selected_trajectories=design["trajectories"],
    ):
        raise ValueError("plan SALib numerical engine is incompatible with the protocol")
    matrices = morris_unit_trajectories(
        tuple(item["name"] for item in candidate["factors"]),
        candidate_trajectories=candidate["candidateTrajectories"],
        selected_trajectories=design["trajectories"],
        levels=candidate["levels"],
        seed=candidate["designSeed"],
    )
    expected_selected = [
        {
            "matrix": matrix,
            "trajectoryId": hashlib.sha256(
                b"eqasim-sa:design:canonical-morris:v3\0"
                + json.dumps(
                    {"matrix": matrix, "selectedIndex": index},
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest(),
        }
        for index, matrix in enumerate(matrices)
    ]
    if design["selectedTrajectories"] != expected_selected:
        raise ValueError("plan SALib-selected Morris trajectories are invalid")
    contract = design.get("scientificContract")
    software = read_software_identity()
    charter = contract.get("charter") if isinstance(contract, dict) else None
    decision = charter.get("decision") if isinstance(charter, dict) else None
    stochastic = charter.get("stochastic") if isinstance(charter, dict) else None
    allocation = stochastic.get("allocation") if isinstance(stochastic, dict) else None
    contract_parameters = contract.get("parameters") if isinstance(contract, dict) else None
    if (
        not isinstance(contract, dict)
        or set(contract) != {"schemaVersion", "software", "charter", "parameters"}
        or contract.get("schemaVersion") != 2
        or contract.get("software")
        != {
            "eqasimSaVersion": expected_descriptor["eqasimSaVersion"],
            "eqasimSaSha256": software.sha256,
        }
        or not isinstance(contract_parameters, list)
        or len(contract_parameters) != 2
        or any(not isinstance(item, dict) for item in contract_parameters)
        or [
            {"name": item.get("name"), "support": item.get("support")}
            for item in contract_parameters
        ]
        != FACTOR_SUPPORTS
        or not isinstance(decision, dict)
        or decision.get("objective") != "screening"
        or decision.get("primary_indicator") != PRIMARY_INDICATOR
        or not isinstance(stochastic, dict)
        or set(stochastic)
        != {
            "target",
            "random_source",
            "intrinsic_variance",
            "common_random_numbers",
            "allocation",
        }
        or stochastic.get("target") != "conditional_mean"
        or not isinstance(allocation, dict)
        or set(allocation)
        != {
            "phase",
            "variance_quantile",
            "target_mcse",
            "minimum_replications",
            "maximum_replications",
            "selected_replications",
            "provenance",
        }
        or allocation.get("phase") != phase
    ):
        raise ValueError("plan scientific contract is incompatible with the protocol")
    base_seeds = design.get("replicationBaseSeeds")
    if phase == "pilot":
        valid_seeds = base_seeds == SEED_POOL[:2]
        valid_allocation = (
            allocation.get("minimum_replications") == 2
            and allocation.get("maximum_replications") == 20
            and allocation.get("selected_replications") is None
            and allocation.get("provenance") is None
        )
    else:
        selected = allocation.get("selected_replications")
        valid_seeds = (
            type(selected) is int
            and 2 <= selected <= len(SEED_POOL)
            and base_seeds == SEED_POOL[:selected]
        )
        valid_allocation = (
            allocation.get("minimum_replications") == 2
            and allocation.get("maximum_replications") == 20
            and type(allocation.get("provenance")) is str
            and bool(allocation["provenance"])
        )
    if not valid_seeds or not valid_allocation:
        raise ValueError(f"plan {phase} replication allocation or base seeds are invalid")
    replications = len(base_seeds)
    expected_columns = [
        "designId",
        "method",
        "role",
        "runId",
        "seed",
        "contextHash",
        "matching_minimum_age",
        "matching_minimum_observations",
    ]
    if (
        table.column_names != expected_columns
        or len(table) != 24 * 3 * replications
        or any(column.null_count for column in table.columns)
        or set(table["method"].to_pylist()) != {"canonical_morris"}
        or set(table["matching_minimum_age"].to_pylist()) != {0, 5, 12}
        or set(table["matching_minimum_observations"].to_pylist()) != {5, 20, 50}
    ):
        raise ValueError("plan table does not match the sealed canonical Morris design")


def _finite_json(value: Any, location: str) -> None:
    if type(value) is float and not math.isfinite(value):
        raise ValueError(f"non-finite JSON number at {location}")
    if isinstance(value, dict):
        for key, item in value.items():
            _finite_json(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _finite_json(item, f"{location}[{index}]")


def _mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{location} must be a JSON object")
    return value


def _exact(value: Any, expected: Any, location: str) -> None:
    if type(value) is not type(expected) or value != expected:
        raise ValueError(f"{location} must equal {expected!r}")


def _integer(value: Any, location: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{location} must be an integer")
    return value


def _number(value: Any, location: str) -> float | int:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{location} must be a finite number")
    return value


def _manifest_evidence(
    root: Path | None,
    *,
    evidence_name: str,
    path_field: str,
    digest_field: str,
    argument: str,
) -> tuple[dict[str, Any], dict[str, Any], Path, str]:
    manifest, _ = _json_file(root, "analysis-generation.json", argument)
    selected_path = manifest.get(path_field)
    if manifest.get("version") != 7:
        raise ValueError("analysis generation manifest version must be 7")
    _exact(manifest.get("layout"), "immutable-generation-v1", "manifest.layout")
    generation_id = manifest.get("generationId")
    if type(generation_id) is not str or DIGEST.fullmatch(generation_id) is None:
        raise ValueError("manifest.generationId must be a lowercase SHA-256 digest")
    if type(selected_path) is not str:
        raise ValueError(f"manifest.{path_field} must be a relative path")
    expected_parent = PurePosixPath("analysis", "generations", generation_id)
    if manifest.get("generationPath") != expected_parent.as_posix():
        raise ValueError(
            "manifest.generationPath must select an explicitly supported "
            "immutable generation"
        )
    selected = PurePosixPath(selected_path)
    if selected.parent != expected_parent or selected.name != evidence_name:
        raise ValueError(
            f"manifest.{path_field} must select {evidence_name} in its immutable generation"
        )
    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict):
        raise ValueError("manifest.outputs must be an object")
    matches = [
        record
        for record in outputs.values()
        if isinstance(record, dict) and record.get("path") == selected_path
    ]
    if len(matches) != 1 or set(matches[0]) != {"path", "size", "sha256"}:
        raise ValueError(f"manifest.outputs must contain {selected_path!r} exactly once")
    record = matches[0]
    if type(record["size"]) is not int or record["size"] < 0:
        raise ValueError("manifest output size must be a non-negative integer")
    expected_size = record["size"]
    path = _safe_evidence_file(root, selected_path, argument)
    recorded_digest = manifest.get(digest_field)
    if type(recorded_digest) is not str or DIGEST.fullmatch(recorded_digest) is None:
        raise ValueError(f"manifest.{digest_field} must be a lowercase SHA-256 digest")
    if matches[0]["sha256"] != recorded_digest:
        raise ValueError(f"manifest.outputs digest does not match manifest.{digest_field}")
    if path.stat().st_size != expected_size:
        raise ValueError(f"manifest output size does not match {evidence_name}")
    evidence = _read_json_file(path)
    actual_digest = _sha256(path)
    if recorded_digest != actual_digest:
        raise ValueError(f"manifest.{digest_field} does not match {evidence_name}")
    return manifest, evidence, path, actual_digest


def _production_evidence(
    root: Path | None, expected_descriptor: dict[str, str]
) -> tuple[int, str]:
    _sealed_morris_plan(
        root, "--pilot-campaign-root", "pilot", expected_descriptor
    )
    manifest, allocation, _, digest = _manifest_evidence(
        root,
        evidence_name="replication-allocation.json",
        path_field="replicationAllocationPath",
        digest_field="replicationAllocationSha256",
        argument="--pilot-campaign-root",
    )
    _exact(manifest.get("inferenceStatus"), "pilot_only", "manifest.inferenceStatus")
    _exact(manifest.get("sensitivityMethod"), "canonical_morris", "manifest.sensitivityMethod")
    if set(allocation) != ALLOCATION_KEYS:
        raise ValueError("replication allocation must have the exact version 1 schema")
    exact_values = {
        "version": 1,
        "status": "recommended",
        "primaryIndicator": PRIMARY_INDICATOR,
        "varianceQuantile": 0.9,
        "targetMcse": 0.01,
        "currentReplications": 2,
        "maximumReplications": 20,
        "availableRuns": 1440,
        "seedPolicy": "canonical-morris-derived-crn-v1",
    }
    for field, expected in exact_values.items():
        _exact(allocation.get(field), expected, f"allocation.{field}")
    _number(allocation.get("varianceQuantileValue"), "allocation.varianceQuantileValue")
    if allocation["varianceQuantileValue"] < 0:
        raise ValueError("allocation.varianceQuantileValue must be non-negative")
    selected = _integer(allocation.get("selectedReplications"), "allocation.selectedReplications")
    raw = _integer(allocation.get("rawRequiredReplications"), "allocation.rawRequiredReplications")
    if not 2 <= selected <= len(SEED_POOL):
        raise ValueError("allocation.selectedReplications must be within [2, 20]")
    if raw != selected:
        raise ValueError("recommended allocation must select rawRequiredReplications")
    positions = _integer(
        allocation.get("estimatedProductionPositions"),
        "allocation.estimatedProductionPositions",
    )
    if positions != 24 * 3 * selected:
        raise ValueError("allocation.estimatedProductionPositions contradicts the protocol geometry")
    if positions > allocation["availableRuns"]:
        raise ValueError("allocation estimated positions exceed available runs")
    within_target = allocation.get("withinTarget")
    if type(within_target) is not bool or within_target != (2 >= raw):
        raise ValueError("allocation.withinTarget contradicts the replication counts")
    return selected, digest


def _screening_evidence(root: Path | None, expected_descriptor: dict[str, str]) -> str:
    _sealed_morris_plan(
        root, "--screening-campaign-root", "production", expected_descriptor
    )
    manifest, report, _, digest = _manifest_evidence(
        root,
        evidence_name="governed-report.json",
        path_field="governedReportPath",
        digest_field="governedReportSha256",
        argument="--screening-campaign-root",
    )
    _exact(manifest.get("inferenceStatus"), "conditional", "manifest.inferenceStatus")
    _exact(report.get("version"), 1, "report.version")
    _exact(report.get("method"), "canonical_morris", "report.method")
    _exact(report.get("inferenceStatus"), "conditional", "report.inferenceStatus")
    _exact(report.get("claimAuthorized"), False, "report.claimAuthorized")
    decision = _mapping(report.get("decision"), "report.decision")
    _exact(decision.get("objective"), "screening", "report.decision.objective")
    _exact(
        decision.get("primary_indicator"),
        PRIMARY_INDICATOR,
        "report.decision.primary_indicator",
    )
    completion = _mapping(report.get("completion"), "report.completion")
    _exact(completion.get("complete"), True, "report.completion.complete")
    convergence = _mapping(report.get("convergence"), "report.convergence")
    _exact(convergence.get("converged"), True, "report.convergence.converged")
    _exact(convergence.get("status"), "met", "report.convergence.status")
    robustness = _mapping(report.get("robustness"), "report.robustness")
    _exact(robustness.get("status"), "pending", "report.robustness.status")
    checks = robustness.get("checks")
    if (
        not isinstance(checks, list)
        or not checks
        or any(not isinstance(check, dict) or not check for check in checks)
    ):
        raise ValueError("report.robustness.checks must be a nonempty array of objects")
    for index, check in enumerate(checks):
        _exact(check.get("status"), "pending", f"report.robustness.checks[{index}].status")
    reason_codes = report.get("reasonCodes")
    if (
        not isinstance(reason_codes, list)
        or any(type(code) is not str or not code for code in reason_codes)
        or "robustness_pending" not in reason_codes
    ):
        raise ValueError("report.reasonCodes must include 'robustness_pending'")
    non_actionable = report.get("nonActionableFactors")
    if not isinstance(non_actionable, list) or any(type(item) is not str for item in non_actionable):
        raise ValueError("report.nonActionableFactors must be an array of strings")
    evidence = report.get("factorEvidence")
    if not isinstance(evidence, list) or any(not isinstance(item, dict) for item in evidence):
        raise ValueError("report.factorEvidence must be an array of objects")
    retained = {}
    for item in evidence:
        factor = item.get("factor")
        if factor in SCREENING_FACTORS:
            if factor in retained:
                raise ValueError(f"report.factorEvidence duplicates {factor!r}")
            _exact(item.get("classification"), "screening", f"factorEvidence[{factor}].classification")
            _exact(item.get("evidenceStatus"), "available", f"factorEvidence[{factor}].evidenceStatus")
            _number(item.get("estimate"), f"factorEvidence[{factor}].estimate")
            _number(item.get("decisionRank"), f"factorEvidence[{factor}].decisionRank")
            retained[factor] = item
    missing = SCREENING_FACTORS - retained.keys()
    if missing:
        raise ValueError(f"report is missing actionable screening evidence for {sorted(missing)}")
    if SCREENING_FACTORS.intersection(non_actionable):
        raise ValueError("retained screening factors must be actionable")
    return digest


def _replace(value: Any, replacements: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: _replace(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace(item, replacements) for item in value]
    if isinstance(value, str):
        for marker, replacement in replacements.items():
            if value == marker:
                return replacement
            value = value.replace(marker, str(replacement))
    return value


def _validate_model_config(path: Path) -> None:
    with path.open(encoding="utf-8") as stream:
        document = yaml.safe_load(stream)
    config = document.get("config") if isinstance(document, dict) else None
    if not isinstance(config, dict):
        raise ValueError("model config must contain a mapping at config")
    contradictions = {
        key: (config.get(key), expected)
        for key, expected in FIXED_MODEL_VALUES.items()
        if config.get(key) != expected
    }
    if contradictions:
        details = ", ".join(
            f"{key}={actual!r} (expected {expected!r})"
            for key, (actual, expected) in contradictions.items()
        )
        raise ValueError(f"model config contradicts fixed protocol values: {details}")


def render(arguments: argparse.Namespace) -> None:
    if arguments.output.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {arguments.output}")
    with arguments.template.open(encoding="utf-8") as stream:
        document = yaml.safe_load(stream)
    if not isinstance(document, dict):
        raise ValueError("protocol template must contain a YAML mapping")
    _absolute(arguments.campaign_root, "campaign root")
    allocation = document.get("charter", {}).get("stochastic", {}).get("allocation")
    objective = document.get("charter", {}).get("decision", {}).get("objective")
    evidence_root = (
        arguments.pilot_campaign_root
        if isinstance(allocation, dict) and allocation.get("phase") == "production"
        else arguments.screening_campaign_root
        if objective in {"fp", "ff"}
        else None
    )
    if evidence_root is not None and arguments.campaign_root.resolve() == evidence_root.resolve():
        raise ValueError("campaign root must be distinct from its evidence root")
    _validate_model_config(arguments.model_config)

    replacements = {
        "__MACHINE_MODEL_CONFIG__": _absolute(arguments.model_config, "model config"),
        "__MACHINE_MODEL_ROOT__": _absolute(arguments.model_root, "model root"),
        "__MACHINE_DATA_MANIFEST__": _absolute(arguments.data_manifest, "data manifest"),
        "__MACHINE_LOCKFILE__": _absolute(arguments.lockfile, "lockfile"),
        "__MACHINE_SCRATCH__": _absolute(arguments.scratch, "scratch"),
        "__MACHINE_DATA_PATH__": _absolute(arguments.data_path, "data path"),
        "__MACHINE_CANONICAL_MORRIS_PILOT_ROOT__": _absolute(arguments.campaign_root, "campaign root"),
        "__MACHINE_CANONICAL_MORRIS_PRODUCTION_ROOT__": _absolute(arguments.campaign_root, "campaign root"),
        "__MACHINE_MULTISCRAMBLE_FP_ROOT__": _absolute(arguments.campaign_root, "campaign root"),
        "__MACHINE_MULTISCRAMBLE_FF_ROOT__": _absolute(arguments.campaign_root, "campaign root"),
    }
    if isinstance(allocation, dict) and allocation.get("phase") == "production":
        production_r, pilot_digest = _production_evidence(
            arguments.pilot_campaign_root, _context_descriptor(arguments)
        )
        replacements.update(
            {
                "__BLOCKED_SELECTED_REPLICATION_COUNT__": production_r,
                "__BLOCKED_PRODUCTION_SEED_PREFIX__": SEED_POOL[:production_r],
                "__BLOCKED_PILOT_ALLOCATION_SHA256__": pilot_digest,
            }
        )
    elif objective in {"fp", "ff"}:
        replacements["__BLOCKED_SCREENING_PROVENANCE_SHA256__"] = (
            _screening_evidence(
                arguments.screening_campaign_root, _context_descriptor(arguments)
            )
        )

    rendered = _replace(document, replacements)
    text = yaml.safe_dump(rendered, sort_keys=False, allow_unicode=False)
    if "__MACHINE_" in text or "__BLOCKED_" in text:
        raise ValueError("rendered protocol contains unresolved markers")
    if text == arguments.template.read_text(encoding="utf-8"):
        raise ValueError("rendered output must differ from its template")
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    with arguments.output.open("x", encoding="utf-8") as stream:
        stream.write(text)


if __name__ == "__main__":
    try:
        render(_arguments())
    except (FileExistsError, OSError, TypeError, ValueError, yaml.YAMLError) as error:
        raise SystemExit(f"render_protocol.py: {error}") from error
