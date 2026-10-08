"""Build deterministic campaign plans and validate their sealed metadata."""

import copy
import hashlib
import itertools
import json
import math
import os
import re
import stat
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from eqasim_sa.core.charter import (
    validate_scientific_contract,
    validate_scientific_stopping,
)
from eqasim_sa.core.context_identity import ExecutionContextIdentity
from eqasim_sa.core.methods.canonical_morris_plan import (
    CANONICAL_MORRIS_DOMAIN,
    CANONICAL_MORRIS_SEED_DOMAIN,
    MAX_CANONICAL_MORRIS_CANDIDATES,
    MAX_CANONICAL_MORRIS_DISTANCE_TERMS,
    _canonical_definition,
    _canonical_morris_positions,
    _canonical_replication_seed,
    _discrete_uniform_generalized_inverse_index,
    canonical_morris_distance_terms,
    changed_factor,
    validate_canonical_morris_bootstrap_work,
)
from eqasim_sa.core.methods.common import (
    MAX_BOOTSTRAP_RESAMPLES,
    MAX_BOOTSTRAP_WORK_CELLS,
    MAX_PLAN_CELLS,
    MAX_PLAN_POSITIONS,
    _json,
    validate_bootstrap_resamples,
)
from eqasim_sa.core.methods.morris_plan import (
    DISCRETE_OAT_DOMAIN,
    MORRIS_DOMAIN,
    _discrete_oat_positions,
    _morris_positions,
    validate_morris_values,
)
from eqasim_sa.core.methods.multiscramble_sobol_plan import (
    MAX_MULTISCRAMBLE_DESIGN_POSITIONS,
    MAX_MULTISCRAMBLE_TABLE_CELLS,
    MULTISCRAMBLE_SOBOL_DOMAIN,
    _multiscramble_definition,
    _multiscramble_sobol_positions,
    validate_multiscramble_scientific_contract,
)
from eqasim_sa.core.methods.sobol_plan import (
    SOBOL_DOMAIN,
    _sobol_positions,
    validate_sobol_bootstrap_work,
    validate_sobol_effective_hybrid_contrasts,
)
from eqasim_sa.core.methods.structured import (
    FIXED,
    NAME,
    _structured_json,
    validate_structured_factor_supports,
    validate_structured_plan_metadata,
    validate_structured_plan_rows,
)
from eqasim_sa.core.ledger import RESERVED_COLUMNS
from eqasim_sa.core.run import RunSpec, compute_run_id
from eqasim_sa.utils.filesystem import (
    PublicationOutcomeUnknown,
    file_state,
    secure_directory,
    write_parquet_exclusive,
)

HEX64 = re.compile(r"^[0-9a-f]{64}$")
DOMAIN = b"eqasim-sa:design:grid:v1\0"


@dataclass(frozen=True)
class GridDesign:
    """Cartesian demonstration design over factor supports and seeds."""

    base_config: dict
    factors: dict[str, list]
    seeds: tuple[int, ...]
    requirements: dict
    context_hash: str
    context_descriptor: dict
    campaign_root: Path
    method: str = "grid"
    structured_factors: dict[str, dict[str, list[str]]] = field(default_factory=dict)


@dataclass(frozen=True)
class MorrisDesign:
    """Legacy non-inferential Morris design retained for compatibility."""

    base_config: dict
    factors: dict[str, list]
    seeds: tuple[int, ...]
    requirements: dict
    context_hash: str
    context_descriptor: dict
    campaign_root: Path
    trajectories: int
    design_seed: int
    method: str = "morris"
    structured_factors: dict[str, dict[str, list[str]]] = field(default_factory=dict)


@dataclass(frozen=True)
class DiscreteOATDesign:
    """Adjacent-level one-at-a-time demonstration design."""

    base_config: dict
    factors: dict[str, list]
    seeds: tuple[int, ...]
    requirements: dict
    context_hash: str
    context_descriptor: dict
    campaign_root: Path
    trajectories: int
    design_seed: int
    method: str = "discrete_oat"
    structured_factors: dict[str, dict[str, list[str]]] = field(default_factory=dict)


@dataclass(frozen=True)
class CanonicalMorrisDesign:
    """Scientific Morris screening design with replication metadata."""

    base_config: dict
    factors: dict[str, list]
    seeds: tuple[int, ...]
    requirements: dict
    context_hash: str
    context_descriptor: dict
    campaign_root: Path
    trajectories: int
    levels: int
    candidate_trajectories: int
    design_seed: int
    distributions: dict[str, dict]
    scientific_contract: dict
    bootstrap_resamples: int
    confidence_level: float
    convergence_checkpoints: tuple[int, ...]
    screening_top_k: int
    method: str = "canonical_morris"
    structured_factors: dict[str, dict[str, list[str]]] = field(default_factory=dict)


@dataclass(frozen=True)
class SobolDesign:
    """Legacy single-scramble Sobol demonstration design."""

    base_config: dict
    factors: dict[str, list]
    seeds: tuple[int, ...]
    requirements: dict
    context_hash: str
    context_descriptor: dict
    campaign_root: Path
    base_sample_size: int
    design_seed: int
    method: str = "sobol"
    structured_factors: dict[str, dict[str, list[str]]] = field(default_factory=dict)


@dataclass(frozen=True)
class MultiScrambleSobolDesign:
    """Scientific Sobol design across independent declared QMC scrambles."""

    base_config: dict
    factors: dict[str, list]
    seeds: tuple[int, ...]
    requirements: dict
    context_hash: str
    context_descriptor: dict
    campaign_root: Path
    base_sample_sizes: tuple[int, ...]
    scramble_seeds: tuple[int, ...]
    distributions: dict[str, dict]
    scientific_contract: dict
    confidence_level: float
    method: str = "multiscramble_sobol"
    structured_factors: dict[str, dict[str, list[str]]] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanResult:
    """Publication status and cardinalities of a deterministic plan."""

    status: str
    path: Path
    design_positions: int
    unique_runs: int
    context_hash: str


def estimate_plan(
    design_positions: int,
    run_ids: tuple[str, ...],
    plan_bytes: int,
    observations: tuple[tuple[float, int, int], ...],
    workers: int,
    method: str = "grid",
    formula_inputs: dict[str, int] | None = None,
) -> dict:
    """Estimate campaign resources from design size and calibration data."""

    if any(
        type(value) is not int or value < 0 for value in (design_positions, plan_bytes)
    ):
        raise ValueError(
            "design positions and plan bytes must be non-negative integers"
        )
    if not observations:
        raise ValueError("provide a calibration meta-config with a complete run")
    if any(
        type(duration) not in (int, float)
        or not math.isfinite(duration)
        or duration < 0
        or type(size) is not int
        or size < 0
        or type(scratch) is not int
        or scratch < 0
        for duration, size, scratch in observations
    ):
        raise ValueError(
            "observation duration, durable bytes and scratch bytes must be finite and non-negative"
        )
    if isinstance(workers, bool) or not isinstance(workers, int) or workers <= 0:
        raise ValueError("workers must be a strictly positive integer")
    formulas = {
        "sobol": "N(2D+2)",
        "morris": "T(D+1)R",
        "discrete_oat": "T(D+1)R",
        "canonical_morris": "T(D+1)R",
        "multiscramble_sobol": "S*N(2D+2)",
    }
    formula, inputs = (
        (formulas.get(method), formula_inputs) if method in formulas else (None, None)
    )
    if method == "sobol":
        if (
            not isinstance(inputs, dict)
            or set(inputs) != {"D", "N"}
            or any(type(value) is not int or value <= 0 for value in inputs.values())
        ):
            raise ValueError("Sobol formula inputs D and N must be positive integers")
        if design_positions != inputs["N"] * (2 * inputs["D"] + 2):
            raise ValueError("Sobol designPositions must equal N*(2*D+2)")
    elif method == "multiscramble_sobol":
        if (
            not isinstance(inputs, dict)
            or set(inputs) != {"D", "N", "S"}
            or any(type(value) is not int or value <= 0 for value in inputs.values())
        ):
            raise ValueError("multi-scramble Sobol formula inputs D, N and S must be positive integers")
        if design_positions != inputs["S"] * inputs["N"] * (2 * inputs["D"] + 2):
            raise ValueError("multi-scramble Sobol designPositions must equal S*N*(2*D+2)")
    elif method in {"morris", "discrete_oat", "canonical_morris"}:
        if (
            not isinstance(inputs, dict)
            or set(inputs) != {"D", "T", "R"}
            or any(type(value) is not int or value <= 0 for value in inputs.values())
        ):
            raise ValueError(
                "Morris formula inputs D, T and R must be positive integers"
            )
        if design_positions != inputs["T"] * (inputs["D"] + 1) * inputs["R"]:
            raise ValueError("Morris designPositions must equal T*(D+1)*R")
    elif method != "grid":
        raise ValueError(f"unsupported estimate method: {method}")
    count, runs = len(observations), len(set(run_ids))
    durations, durable_bytes, scratch_bytes = zip(*observations)
    mean_duration = sum(durations) / count
    mean_bytes = sum(durable_bytes) / count
    observed_scratch = max(scratch_bytes)
    hours, disk = runs * mean_duration / 3600, math.ceil(plan_bytes + runs * mean_bytes)
    scratch = workers * observed_scratch
    if not all(
        math.isfinite(value) and value >= 0
        for value in (mean_duration, mean_bytes, hours, disk, scratch)
    ):
        raise OverflowError("estimate exceeds finite JSON numeric range")
    return {
        "status": "estimated",
        "estimateVersion": 2,
        "method": method,
        "formula": formula,
        "formulaInputs": inputs,
        "designPositions": design_positions,
        "runs": runs,
        "calibrationRuns": count,
        "meanDurationSeconds": mean_duration,
        "meanDurableBytesPerRun": mean_bytes,
        "estimatedComputeHours": hours,
        "estimatedDiskBytes": disk,
        "workers": workers,
        "observedScratchBytesPerWorker": observed_scratch,
        "estimatedScratchBytes": scratch,
        "error": None,
    }




def _scalar(name: str, value: object) -> None:
    if type(value) not in (int, float, str):
        raise TypeError(f"factor {name!r} must be scalar")
    if (
        type(value) is int
        and not -(2**63) <= value < 2**63
        or type(value) is float
        and not math.isfinite(value)
        or type(value) is str
        and (not value or "\0" in value)
    ):
        raise ValueError(
            f"factor {name!r} values must be finite int64 or non-empty strings without NUL"
        )


def _validate_scientific_software_binding(
    contract: dict, context_descriptor: dict
) -> None:
    try:
        software = contract["software"]
        version = context_descriptor["eqasimSaVersion"]
    except (KeyError, TypeError) as error:
        raise ValueError(
            "scientific design context must bind eqasimSaVersion"
        ) from error
    if software.get("eqasimSaVersion") != version:
        raise ValueError(
            "scientific contract software version does not match the plan context descriptor"
        )


Design = GridDesign | MorrisDesign | DiscreteOATDesign | CanonicalMorrisDesign | SobolDesign | MultiScrambleSobolDesign


def _plan_positions(design: Design, names: tuple[str, ...]) -> int:
    if design.method == "grid":
        return math.prod(len(design.factors[name]) for name in names) * len(
            design.seeds
        )
    if design.method in {"morris", "discrete_oat", "canonical_morris"}:
        return design.trajectories * (len(names) + 1) * len(design.seeds)
    if design.method == "sobol":
        return design.base_sample_size * (2 * len(names) + 2)
    return (
        len(design.scramble_seeds)
        * design.base_sample_sizes[-1]
        * (2 * len(names) + 2)
    )


def _validate_plan_allocation(design: Design, names: tuple[str, ...]) -> None:
    positions = _plan_positions(design, names)
    if positions > MAX_PLAN_POSITIONS:
        raise ValueError(
            f"{design.method} allocation exceeds the safe cap of "
            f"{MAX_PLAN_POSITIONS} design positions"
        )
    columns = len(names) - ("seed" in names) + len(FIXED)
    if positions * columns > MAX_PLAN_CELLS:
        raise ValueError(
            f"{design.method} allocation exceeds the safe cap of "
            f"{MAX_PLAN_CELLS} table cells"
        )


def _validate(design: Design) -> tuple[str, ...]:
    if design.method not in {
        "grid", "morris", "discrete_oat", "canonical_morris", "sobol", "multiscramble_sobol"
    }:
        raise ValueError(
            "method must be grid, morris, discrete_oat, canonical_morris, sobol or multiscramble_sobol"
        )
    if not isinstance(design.base_config, dict) or not isinstance(
        design.base_config.get("config"), dict
    ):
        raise TypeError("base configuration and its config block must be mappings")
    _json(design.base_config)
    names = tuple(sorted(design.factors))
    if not isinstance(design.structured_factors, dict):
        raise TypeError("structured_factors must be a mapping")
    if set(design.structured_factors) - set(names):
        raise ValueError("structured_factors must name declared factors")
    if design.structured_factors and design.method in {
        "morris", "discrete_oat", "canonical_morris"
    }:
        raise ValueError(f"{design.method} does not support structured factors")
    if design.method == "sobol" and "seed" in design.factors:
        raise ValueError("SobolDesign factors must not contain the special factor seed")
    if design.method == "multiscramble_sobol" and "seed" in design.factors:
        raise ValueError("MultiScrambleSobolDesign factors must not contain the special factor seed")
    if not names and design.method != "sobol":
        raise ValueError("at least one factor is required")
    for name in names:
        if (
            type(name) is not str
            or not NAME.fullmatch(name)
            or name in FIXED + RESERVED_COLUMNS
        ):
            raise ValueError(f"invalid or reserved factor name: {name!r}")
        values = design.factors[name]
        if not isinstance(values, list) or not values:
            raise ValueError(f"factor {name!r} values must be a non-empty list")
        if design.method in {"morris", "discrete_oat"}:
            validate_morris_values(name, values)
        for value in values:
            _scalar(name, value)
        if len({type(value) for value in values}) != 1:
            raise TypeError(f"factor {name!r} levels must have the same type")
        if design.method in {"sobol", "canonical_morris", "multiscramble_sobol"} and len(values) < 2:
            raise ValueError(f"factor {name!r} needs two or more values")
        if (
            design.method in {"sobol", "canonical_morris", "multiscramble_sobol"}
            and len(set(values)) != len(values)
        ):
            raise ValueError(f"factor {name!r} values must be distinct")
        if name in design.structured_factors:
            configurations = design.structured_factors[name]
            if list(configurations) != values:
                raise ValueError(
                    f"structured factor {name!r} labels must exactly match factor support"
                )
            if name not in design.base_config["config"]:
                raise ValueError(f"structured factor {name!r} is absent from base config")
            base_value = design.base_config["config"][name]
            if not isinstance(base_value, list) or any(
                not isinstance(item, str) for item in base_value
            ):
                raise TypeError(
                    f"structured factor {name!r} base config value must be a list of strings"
                )
            payloads = []
            for label, payload in configurations.items():
                if not isinstance(label, str) or not label or "\0" in label:
                    raise ValueError("structured factor labels must be safe strings")
                if (
                    not isinstance(payload, list)
                    or not payload
                    or any(
                        not isinstance(item, str) or not item or "\0" in item
                        for item in payload
                    )
                    or len(set(payload)) != len(payload)
                ):
                    raise ValueError(
                        f"structured factor {name!r} payloads must be non-empty unique string lists"
                    )
                payloads.append(tuple(payload))
            if len(set(payloads)) != len(payloads):
                raise ValueError(f"structured factor {name!r} payloads must be distinct")
    if not design.seeds or any(
        type(seed) is not int or not -(2**63) <= seed < 2**63 for seed in design.seeds
    ):
        raise ValueError("seeds must be non-empty signed int64 values")
    if len(set(design.seeds)) != len(design.seeds):
        raise ValueError("seeds must be unique")
    if design.method == "sobol" and len(design.seeds) < 2:
        raise ValueError("Sobol seed requires at least two distinct values")
    if not HEX64.fullmatch(design.context_hash):
        raise ValueError(
            "context_hash must contain 64 lowercase hexadecimal characters"
        )
    try:
        context_identity = ExecutionContextIdentity.from_descriptor(
            design.context_descriptor
        )
    except ValueError as error:
        raise ValueError("context_descriptor must contain exactly four string keys")
    if context_identity.sha256 != design.context_hash:
        raise ValueError("context_hash does not match context_descriptor")
    config = design.requirements.get("config", {})
    if set(design.requirements) - {"run", "config"} or not isinstance(config, dict):
        raise ValueError("requirements may contain only run and config")
    if set(config) & (set(names) | {"random_seed"}):
        raise ValueError("requirements config collides with a factor or random_seed")
    _json(design.requirements)
    if design.method in {"morris", "discrete_oat"}:
        if type(design.trajectories) is not int or design.trajectories <= 0:
            raise ValueError("trajectories must be a strictly positive integer")
        if (
            type(design.design_seed) is not int
            or not -(2**63) <= design.design_seed < 2**63
        ):
            raise ValueError("design_seed must be a signed int64")
        _validate_plan_allocation(design, names)
        candidates = math.factorial(len(names)) * math.prod(
            2 * (len(design.factors[name]) - 1) for name in names
        )
        if design.trajectories > candidates:
            raise ValueError(
                f"trajectories exceeds Morris candidate count {candidates}"
            )
    if design.method == "canonical_morris":
        dimensions = len(names)
        if type(design.levels) is not int or design.levels < 4 or design.levels % 2:
            raise ValueError("levels must be an even integer greater than or equal to 4")
        if type(design.trajectories) is not int or design.trajectories < 2:
            raise ValueError("canonical Morris trajectories must be at least 2")
        _validate_plan_allocation(design, names)
        universe = math.factorial(dimensions) * design.levels**dimensions
        if (
            type(design.candidate_trajectories) is not int
            or design.candidate_trajectories <= design.trajectories
            or design.candidate_trajectories > MAX_CANONICAL_MORRIS_CANDIDATES
            or design.candidate_trajectories > universe
        ):
            raise ValueError(
                "candidate_trajectories must be strictly greater than trajectories "
                "and at most "
                f"{min(universe, MAX_CANONICAL_MORRIS_CANDIDATES)}"
            )
        if canonical_morris_distance_terms(
            dimensions, design.candidate_trajectories, design.trajectories
        ) > MAX_CANONICAL_MORRIS_DISTANCE_TERMS:
            raise ValueError(
                "canonical Morris candidate selection exceeds the deterministic "
                "distance-computation budget"
            )
        if (
            type(design.design_seed) is not int
            or not -(2**63) <= design.design_seed < 2**63
        ):
            raise ValueError("design_seed must be a signed int64")
        if set(design.distributions) != set(names):
            raise ValueError("distributions must exactly match canonical Morris factors")
        for name in names:
            distribution = design.distributions[name]
            if not isinstance(distribution, dict) or set(distribution) != {
                "family", "transform", "provenance"
            }:
                raise ValueError(f"distribution for factor {name!r} has invalid keys")
            if (
                distribution["family"] != "discrete_uniform"
                or distribution["transform"] != "identity"
                or not isinstance(distribution["provenance"], str)
                or not distribution["provenance"]
            ):
                raise ValueError(
                    f"distribution for factor {name!r} must be discrete_uniform/identity with provenance"
                )
        if not isinstance(design.scientific_contract, dict):
            raise TypeError("scientific_contract must be a mapping")
        _json(design.scientific_contract)
        validate_scientific_contract(
            design.scientific_contract, design.factors, design.distributions
        )
        _validate_scientific_software_binding(
            design.scientific_contract, design.context_descriptor
        )
        stochastic = design.scientific_contract["charter"]["stochastic"]
        if stochastic["target"] != "conditional_mean":
            raise ValueError(
                "canonical Morris scientific_contract requires conditional_mean"
            )
        allocation = stochastic["allocation"]
        if allocation["phase"] == "pilot":
            if len(design.seeds) < allocation["minimum_replications"]:
                raise ValueError(
                    "canonical Morris pilot seed count must be at least minimum_replications"
                )
        elif len(design.seeds) != allocation["selected_replications"]:
            raise ValueError(
                "canonical Morris production seed count must equal selected_replications"
            )
        validate_bootstrap_resamples(
            design.bootstrap_resamples,
            method="canonical Morris",
            minimum=100,
        )
        if (
            type(design.confidence_level) is not float
            or not math.isfinite(design.confidence_level)
            or not 0 < design.confidence_level < 1
        ):
            raise ValueError("confidence_level must be a finite float in (0,1)")
        checkpoints = design.convergence_checkpoints
        if (
            not isinstance(checkpoints, tuple)
            or not checkpoints
            or any(type(value) is not int or value < 2 for value in checkpoints)
            or tuple(sorted(set(checkpoints))) != checkpoints
            or checkpoints[-1] != design.trajectories
        ):
            raise ValueError(
                "convergence_checkpoints must be sorted unique integers at least 2 ending at trajectories"
            )
        if (
            type(design.screening_top_k) is not int
            or not 1 <= design.screening_top_k <= dimensions
        ):
            raise ValueError(
                "screening_top_k must be a positive integer no greater than factor count"
            )
        validate_canonical_morris_bootstrap_work(
            design.bootstrap_resamples,
            dimensions,
            design.trajectories,
            len(design.seeds),
            checkpoints,
        )
        validate_scientific_stopping(
            "canonical_morris",
            design.scientific_contract["charter"]["stopping"],
            len(checkpoints),
        )
    if design.method == "sobol":
        if len(names) + 1 < 2:
            raise ValueError("Sobol requires at least two factors including seed")
        if len(names) + 1 > 10_600:
            raise ValueError("Sobol supports at most 10600 factors")
        if type(design.base_sample_size) is not int or design.base_sample_size < 2:
            raise ValueError(
                "base_sample_size must be an integer greater than or equal to 2"
            )
        if design.base_sample_size & (design.base_sample_size - 1):
            raise ValueError("base_sample_size must be a power of two")
        if design.base_sample_size * (2 * (len(names) + 1) + 2) > 2**63 - 1:
            raise ValueError("Sobol N(2D+2) must fit in signed int64")
        if type(design.design_seed) is not int or not 0 <= design.design_seed < 2**64:
            raise ValueError("design_seed must be an unsigned 64-bit integer")
        _validate_plan_allocation(design, tuple(sorted((*names, "seed"))))
        return tuple(sorted((*names, "seed")))
    if design.method == "multiscramble_sobol":
        names = tuple(sorted((*names, "seed")))
        dimensions = len(names)
        if not 2 <= dimensions <= 10_600:
            raise ValueError("multi-scramble Sobol requires 2 to 10600 factors including seed")
        sizes = design.base_sample_sizes
        if (
            not isinstance(sizes, tuple)
            or not sizes
            or any(type(value) is not int or value < 2 or value & (value - 1) for value in sizes)
            or tuple(sorted(set(sizes))) != sizes
            or any(right != 2 * left for left, right in zip(sizes, sizes[1:]))
        ):
            raise ValueError("base_sample_sizes must be sorted powers of two at least 2, each doubling")
        if (
            not isinstance(design.scramble_seeds, tuple)
            or len(design.scramble_seeds) < 4
            or any(type(seed) is not int or not 0 <= seed < 2**64 for seed in design.scramble_seeds)
            or len(set(design.scramble_seeds)) != len(design.scramble_seeds)
        ):
            raise ValueError("scramble_seeds must contain at least four unique uint64 integers")
        positions = len(design.scramble_seeds) * sizes[-1] * (2 * dimensions + 2)
        if positions > 2**63 - 1:
            raise ValueError("multi-scramble Sobol S*N*(2D+2) must fit in signed int64")
        _validate_plan_allocation(design, names)
        if set(design.distributions) != set(names):
            raise ValueError("distributions must exactly match multi-scramble Sobol factors")
        for name in names:
            distribution = design.distributions[name]
            if (
                not isinstance(distribution, dict)
                or set(distribution) != {"family", "transform", "provenance"}
                or distribution["family"] != "discrete_uniform"
                or distribution["transform"] != "identity"
                or not isinstance(distribution["provenance"], str)
                or not distribution["provenance"]
            ):
                raise ValueError(f"distribution for factor {name!r} is invalid")
        if type(design.confidence_level) is not float or not math.isfinite(design.confidence_level) or not 0 < design.confidence_level < 1:
            raise ValueError("confidence_level must be a finite float in (0,1)")
        validate_multiscramble_scientific_contract(
            design.scientific_contract,
            {**design.factors, "seed": list(design.seeds)},
            design.distributions,
        )
        _validate_scientific_software_binding(
            design.scientific_contract, design.context_descriptor
        )
        validate_scientific_stopping(
            "multiscramble_sobol",
            design.scientific_contract["charter"]["stopping"],
            len(sizes),
        )
        return names
    if design.method == "grid":
        _validate_plan_allocation(design, names)
    return names


def _table(design: Design, names: tuple[str, ...]) -> pa.Table:
    metadata = {
        b"planSchemaVersion": b"1",
        b"canonicalizationVersion": b"1",
        b"contextDescriptor": ExecutionContextIdentity.from_descriptor(
            design.context_descriptor
        ).canonical_bytes,
    }
    if design.structured_factors:
        metadata.update(
            {
                b"planSchemaVersion": b"3",
                b"structuredFactorsVersion": b"1",
                b"structuredFactors": _structured_json(design.structured_factors),
            }
        )
    canonical_plan = None
    if design.method == "morris":
        morris = {
            "designSeed": design.design_seed,
            "trajectories": design.trajectories,
            "factors": [
                {"name": name, "values": design.factors[name]} for name in names
            ],
        }
        metadata.update({b"morrisDesignVersion": b"1", b"morrisDesign": _json(morris)})
    if design.method == "discrete_oat":
        discrete_oat = {
            "designSeed": design.design_seed,
            "trajectories": design.trajectories,
            "factors": [
                {"name": name, "values": design.factors[name]} for name in names
            ],
        }
        metadata.update(
            {
                b"discreteOatDesignVersion": b"1",
                b"discreteOatDesign": _json(discrete_oat),
            }
        )
    if design.method == "canonical_morris":
        canonical_plan = _canonical_definition(design, names)
        canonical, _ = canonical_plan
        metadata[b"planSchemaVersion"] = b"3" if design.structured_factors else b"2"
        metadata.update(
            {
                b"canonicalMorrisDesignVersion": b"3",
                b"canonicalMorrisDesign": _json(canonical),
            }
        )
    if design.method == "sobol":
        domains = {**design.factors, "seed": list(design.seeds)}
        sobol = {
            "baseSampleSize": design.base_sample_size,
            "designSeed": design.design_seed,
            "scramble": True,
            "bits": 64,
            "mapping": "equal-width-v1",
            "factors": [{"name": name, "values": domains[name]} for name in names],
        }
        if design.structured_factors:
            sobol["structuredFactors"] = design.structured_factors
        metadata.update({b"sobolDesignVersion": b"1", b"sobolDesign": _json(sobol)})
    if design.method == "multiscramble_sobol":
        multiscramble = _multiscramble_definition(design, names)
        metadata[b"planSchemaVersion"] = b"3" if design.structured_factors else b"2"
        metadata.update(
            {
                b"multiScrambleSobolDesignVersion": b"3",
                b"multiScrambleSobolDesign": _json(multiscramble),
            }
        )
    columns = tuple(name for name in names if name != "seed")
    scalar_types = {int: pa.int64(), float: pa.float64(), str: pa.string()}
    types = (
        [pa.string()] * 4
        + [pa.int64(), pa.string()]
        + [scalar_types[type(design.factors[n][0])] for n in columns]
    )
    rows, identifiers, runs, resolved_runs, levels = (
        [],
        set(),
        {},
        {},
        [range(len(design.factors[name])) for name in columns],
    )
    positions = (
        (indices, replication, seed)
        for indices in itertools.product(*levels)
        for replication, seed in enumerate(design.seeds)
    )
    selected = (
        positions
        if design.method == "grid"
        else _morris_positions(design, names)
        if design.method == "morris"
        else _discrete_oat_positions(design, names)
        if design.method == "discrete_oat"
        else _canonical_morris_positions(design, names, *canonical_plan)
        if design.method == "canonical_morris"
        else _sobol_positions(design, names)
        if design.method == "sobol"
        else _multiscramble_sobol_positions(design, names, multiscramble)
    )
    for position in selected:
        if design.method in {"sobol", "multiscramble_sobol"}:
            all_values, role, definition = position
            logical = dict(zip(names, all_values))
            seed, replication, extra = logical["seed"], 0, [role, definition]
            indices = ()
        elif design.method == "canonical_morris":
            all_values, replication, seed, role, definition = position
            logical = dict(zip(names, all_values))
            indices, extra = (), [role, definition]
        else:
            indices, replication, seed, *extra = position
            logical = {
                name: design.factors[name][index] for name, index in zip(names, indices)
            }
        role = "point" if design.method == "grid" else extra[0]
        preimage = (
            {
                "factors": [
                    {"levelIndex": index, "name": name}
                    for name, index in zip(names, indices)
                ],
                "replicationIndex": replication,
            }
            if design.method == "grid"
            else {
                "design": extra[1]
                if design.method in {"sobol", "multiscramble_sobol"}
                else extra[1]
                if design.method == "canonical_morris"
                else {**extra[1], "trajectories": design.trajectories},
                **(
                    {}
                    if design.method in {"sobol", "multiscramble_sobol"}
                    else {"replicationIndex": replication}
                ),
                "role": role,
            }
        )
        domain = (
            DOMAIN
            if design.method == "grid"
            else MORRIS_DOMAIN
            if design.method == "morris"
            else DISCRETE_OAT_DOMAIN
            if design.method == "discrete_oat"
            else CANONICAL_MORRIS_DOMAIN
            if design.method == "canonical_morris"
            else MULTISCRAMBLE_SOBOL_DOMAIN
            if design.method == "multiscramble_sobol"
            else SOBOL_DOMAIN
        )
        design_id = hashlib.sha256(domain + _json(preimage)).hexdigest()[:32]
        if design_id in identifiers:
            raise RuntimeError("designId collision between distinct design positions")
        identifiers.add(design_id)
        levels = tuple(logical[name] for name in columns)
        projected = (
            tuple(
                (
                    type(value),
                    value.hex() if type(value) is float else value,
                )
                for value in levels
            ),
            (type(seed), seed),
        )
        cached = resolved_runs.get(projected)
        if cached is None:
            resolved = resolve_config(design, columns, levels, seed)
            cached = (_json(resolved), compute_run_id(resolved, seed))
            resolved_runs[projected] = cached
        canonical, run_id = cached
        if runs.setdefault(run_id, canonical) != canonical:
            raise RuntimeError(
                "runId collision between distinct resolved configurations"
            )
        rows.append(
            [
                design_id,
                design.method,
                role,
                run_id,
                seed,
                design.context_hash,
                *(logical[name] for name in columns),
            ]
        )
    schema = pa.schema(
        [
            pa.field(name, kind, nullable=False)
            for name, kind in zip((*FIXED, *columns), types)
        ],
        metadata=metadata,
    )
    arrays = [
        pa.array([row[i] for row in rows], type=kind) for i, kind in enumerate(types)
    ]
    return pa.Table.from_arrays(arrays, schema=schema)


def resolve_config(
    design: Design,
    names: tuple[str, ...],
    values: tuple,
    seed: int,
) -> dict:
    """Resolve one design point into an executable model configuration."""

    resolved = copy.deepcopy(design.base_config)
    nested = resolved["config"]
    for name, value in zip(names, values):
        nested[name] = copy.deepcopy(
            design.structured_factors[name][value]
            if name in design.structured_factors
            else value
        )
    nested["random_seed"] = seed
    if "run" in design.requirements:
        resolved["run"] = copy.deepcopy(design.requirements["run"])
    nested.update(copy.deepcopy(design.requirements.get("config", {})))
    return resolved


def unique_config_seed_pairs(design: Design) -> bool:
    """Report whether every design row maps to a distinct run identity."""

    names = _validate(design)
    ids = _table(design, names)["runId"].to_pylist()
    return len(ids) == len(set(ids))


def load_run_specs(
    path: Path, design: Design, campaign: str
) -> tuple[RunSpec, ...]:
    """Verify a sealed plan against its design and reconstruct unique runs."""

    if path.is_symlink() or not path.is_file():
        raise ValueError("plan.parquet must be a regular non-symlink file")
    names = _validate(design)
    transactions = path.parent / ".eqasim-sa" / "transactions"
    existing_transactions = None
    if transactions.exists() or transactions.is_symlink():
        existing_transactions = secure_directory(transactions)
    actual = _read_committed_plan(path, existing_transactions)
    if actual is None:
        raise ValueError("plan.parquet disappeared before its commit was proven")
    expected = _table(design, names)
    same = expected.equals(actual)
    if design.method in {"sobol", "multiscramble_sobol"} and expected.schema.equals(
        actual.schema, check_metadata=True
    ):
        expected_rows = {row["designId"]: row for row in expected.to_pylist()}
        actual_rows = {row["designId"]: row for row in actual.to_pylist()}
        same = len(actual_rows) == len(actual) and actual_rows == expected_rows
    if not expected.schema.equals(actual.schema, check_metadata=True) or not same:
        raise ValueError(
            "plan schema, metadata, context, designId, factors, seed, or runId mismatch"
        )
    specs = {}
    for row in expected.to_pylist():
        if row["runId"] not in specs:
            columns = tuple(name for name in names if name != "seed")
            values = tuple(row[name] for name in columns)
            specs[row["runId"]] = RunSpec(
                campaign,
                resolve_config(design, columns, values, row["seed"]),
                row["seed"],
                design.context_hash,
                dict(zip(columns, values)),
            )
    return tuple(specs.values())


def _plan_transactions(root: Path) -> Path:
    technical = root / ".eqasim-sa"
    transactions = technical / "transactions"
    secure_directory(technical, create=True)
    if {entry.name for entry in technical.iterdir()} - {"transactions"}:
        raise FileExistsError("campaign technical directory contains foreign state")
    secure_directory(transactions, create=True)
    allowed = f".plan.parquet."
    for entry in transactions.iterdir():
        info = entry.lstat()
        if (
            not entry.name.startswith(allowed)
            or not entry.name.endswith(".tmp")
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink not in {1, 2}
        ):
            raise FileExistsError(
                "campaign transaction directory contains foreign state"
            )
    return transactions


def _pending_plan_link(destination: Path, transactions: Path) -> bool:
    try:
        visible = destination.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(visible.st_mode) or visible.st_nlink < 2:
        return False
    for entry in transactions.iterdir():
        try:
            candidate = entry.lstat()
        except FileNotFoundError:
            continue
        if (
            entry.name.startswith(".plan.parquet.")
            and entry.name.endswith(".tmp")
            and stat.S_ISREG(candidate.st_mode)
            and candidate.st_dev == visible.st_dev
            and candidate.st_ino == visible.st_ino
        ):
            return True
    return False


def _read_committed_plan(
    destination: Path, transactions: Path | None = None
) -> pa.Table | None:
    """Read only a plan whose exclusive publication has crossed its fsync point."""
    deadline = time.monotonic() + 5.0
    while True:
        try:
            descriptor = os.open(
                destination, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            )
        except FileNotFoundError:
            return None
        try:
            initial = os.fstat(descriptor)
            if not stat.S_ISREG(initial.st_mode):
                raise ValueError("plan.parquet must be a regular non-symlink file")
            if initial.st_nlink != 1:
                if transactions is None or not _pending_plan_link(
                    destination, transactions
                ):
                    try:
                        visible = destination.lstat()
                    except FileNotFoundError:
                        continue
                    publication_finished = (
                        visible.st_nlink == 1
                        and visible.st_dev == initial.st_dev
                        and visible.st_ino == initial.st_ino
                        and file_state(visible) == file_state(os.fstat(descriptor))
                    )
                    if publication_finished:
                        continue
                    raise ValueError(
                        "plan.parquet must not be a foreign hard-linked file"
                    )
                pending = True
            else:
                pending = False
                with os.fdopen(os.dup(descriptor), "rb") as stream:
                    table = pq.ParquetFile(stream).read()
                final = os.fstat(descriptor)
                try:
                    visible = destination.lstat()
                except FileNotFoundError:
                    continue
                if file_state(initial) != file_state(final) or file_state(
                    initial
                ) != file_state(visible):
                    raise RuntimeError("plan.parquet changed while being read")
                return table
        finally:
            os.close(descriptor)
        if pending:
            if time.monotonic() >= deadline:
                raise PublicationOutcomeUnknown(
                    "plan publication remains visible without a proven commit"
                )
            time.sleep(0.001)


def _same_plan(candidate: pa.Table, existing: pa.Table) -> bool:
    return candidate.schema.equals(
        existing.schema, check_metadata=True
    ) and candidate.equals(existing)


def _validate_existing_plan_technical(root: Path) -> None:
    technical = root / ".eqasim-sa"
    if not technical.exists() and not technical.is_symlink():
        return
    secure_directory(technical)
    children = {entry.name: entry for entry in technical.iterdir()}
    if set(children) - {"locks", "transactions", "slurm"}:
        raise FileExistsError("campaign technical directory contains foreign state")
    for child in children.values():
        secure_directory(child)


def build_plan(design: Design) -> PlanResult:
    """Create or verify an immutable deterministic plan for a campaign."""

    names = _validate(design)
    design_positions = _plan_positions(design, names)
    if design.method in {"canonical_morris", "multiscramble_sobol"}:
        try:
            budget = design.scientific_contract["charter"]["budget"]
            allowed = _available_scientific_runs(
                budget["maximum_runs"], budget["reserve_fraction"]
            )
        except (KeyError, TypeError) as error:
            raise ValueError("scientific_contract must contain charter budget") from error
        if allowed == 0 or design_positions > allowed:
            raise ValueError(
                f"{'canonical Morris' if design.method == 'canonical_morris' else design.method} "
                f"design positions {design_positions} exceed scientific execution budget {allowed}"
            )
    candidate = _table(design, names)
    root, destination = design.campaign_root, design.campaign_root / "plan.parquet"
    _validate_existing_plan_technical(root)
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise ValueError("plan.parquet must be a regular non-symlink file")
    if destination.exists():
        visible = destination.lstat()
        if visible.st_nlink == 1:
            existing = _read_committed_plan(destination)
            if existing is not None and _same_plan(candidate, existing):
                return PlanResult(
                    "unchanged",
                    Path("plan.parquet"),
                    len(candidate),
                    len(set(candidate["runId"].to_pylist())),
                    design.context_hash,
                )
            if existing is not None:
                raise FileExistsError("plan differs; choose a new campaign_root")
    if root.exists():
        entries = {entry.name for entry in root.iterdir()}
        if entries - {".eqasim-sa", "plan.parquet"}:
            raise FileExistsError("campaign_root contains durable state without a plan")
    transactions = _plan_transactions(root)
    while True:
        try:
            write_parquet_exclusive(
                candidate,
                destination,
                staging_directory=transactions,
            )
        except FileExistsError:
            existing = _read_committed_plan(destination, transactions)
            if existing is None:
                continue
            if _same_plan(candidate, existing):
                return PlanResult(
                    "unchanged",
                    Path("plan.parquet"),
                    len(candidate),
                    len(set(candidate["runId"].to_pylist())),
                    design.context_hash,
                )
            raise FileExistsError("plan differs; choose a new campaign_root") from None
        break
    return PlanResult(
        "created",
        Path("plan.parquet"),
        len(candidate),
        len(set(candidate["runId"].to_pylist())),
        design.context_hash,
    )


def _available_scientific_runs(maximum_runs: int, reserve_fraction: float) -> int:
    return int(
        Decimal(maximum_runs)
        * (Decimal(1) - Decimal(str(reserve_fraction)))
    )
