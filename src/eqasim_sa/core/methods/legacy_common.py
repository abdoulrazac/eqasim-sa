"""Shared validation for scientific plans created by legacy eqasim-sa releases."""

import json
import math
import re

import pyarrow as pa

from eqasim_sa.core.charter import (
    parse_stochastic,
    validate_scientific_contract,
    validate_scientific_stopping,
)
from eqasim_sa.core.context_identity import (
    LEGACY_CONTEXT_DOMAIN_V1,
    ExecutionContextIdentity,
)
from eqasim_sa.core.methods.common import _json
from eqasim_sa.core.methods.multiscramble_sobol_plan import (
    validate_multiscramble_scientific_contract,
)

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
    except Exception as error:
        raise ValueError(f"legacy {label} v{version} metadata is invalid") from error
    try:
        context_identity = ExecutionContextIdentity.from_descriptor(
            descriptor, LEGACY_CONTEXT_DOMAIN_V1
        )
    except ValueError as error:
        raise ValueError(
            f"legacy {label} v{version} contextDescriptor must contain exactly four string keys"
        ) from error
    if (
        metadata[design_key] != design_bytes
        or metadata[b"contextDescriptor"] != context_identity.canonical_bytes
    ):
        raise ValueError(f"legacy {label} v{version} metadata is not canonical")
    return design, context_identity.as_dict(), context_identity.sha256


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
