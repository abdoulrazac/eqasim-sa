import copy
import hashlib
import itertools
import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from scipy.stats import qmc

from eqasim_sa.core.charter import validate_scientific_contract
from eqasim_sa.core.ledger import RESERVED_COLUMNS, write_parquet_atomic
from eqasim_sa.core.run import RunSpec, compute_run_id

FIXED = ("designId", "method", "role", "runId", "seed", "contextHash")
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
DOMAIN = b"eqasim-sa:design:grid:v1\0"
MORRIS_DOMAIN = b"eqasim-sa:design:morris:v1\0"
DISCRETE_OAT_DOMAIN = b"eqasim-sa:design:discrete-oat:v1\0"
CANONICAL_MORRIS_DOMAIN = b"eqasim-sa:design:canonical-morris:v1\0"
CANONICAL_MORRIS_SEED_DOMAIN = b"eqasim-sa:canonical-morris-seed:v1\0"
SOBOL_DOMAIN = b"eqasim-sa:design:sobol:v1\0"
MAX_CANONICAL_MORRIS_CANDIDATES = 500
MAX_CANONICAL_MORRIS_DISTANCE_TERMS = 50_000_000


@dataclass(frozen=True)
class GridDesign:
    base_config: dict
    factors: dict[str, list]
    seeds: tuple[int, ...]
    requirements: dict
    context_hash: str
    context_descriptor: dict
    campaign_root: Path
    method: str = "grid"


@dataclass(frozen=True)
class MorrisDesign:
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


@dataclass(frozen=True)
class DiscreteOATDesign:
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


@dataclass(frozen=True)
class CanonicalMorrisDesign:
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


@dataclass(frozen=True)
class SobolDesign:
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


@dataclass(frozen=True)
class PlanResult:
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


def _json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


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


def validate_morris_values(name: str, values: list) -> None:
    if (
        len(values) < 2
        or type(values[0]) not in (int, float)
        or any(type(value) is not type(values[0]) for value in values)
    ):
        raise TypeError(
            f"Morris factor {name!r} needs two or more numeric levels of the same type"
        )
    if any(not math.isfinite(value) for value in values):
        raise ValueError(f"Morris factor {name!r} levels must be finite")
    if any(left >= right for left, right in zip(values, values[1:])):
        raise ValueError(
            f"Morris factor {name!r} levels must be distinct and strictly increasing"
        )
    extent = values[-1] - values[0]
    deltas = tuple((right - left) / extent for left, right in zip(values, values[1:]))
    if not math.isfinite(extent) or any(
        not math.isfinite(delta) or delta == 0 for delta in deltas
    ):
        raise ValueError(
            f"Morris factor {name!r} range and normalized deltas must be finite and representable"
        )


def canonical_morris_distance_terms(
    dimensions: int, candidates: int, trajectories: int
) -> int:
    pair_distances = candidates * (candidates - 1) // 2
    greedy_distances = sum(
        (candidates - selected) * selected
        for selected in range(2, trajectories)
    )
    return (pair_distances + greedy_distances) * dimensions * (dimensions + 1) ** 2


Design = GridDesign | MorrisDesign | DiscreteOATDesign | CanonicalMorrisDesign | SobolDesign


def _validate(design: Design) -> tuple[str, ...]:
    if design.method not in {
        "grid", "morris", "discrete_oat", "canonical_morris", "sobol"
    }:
        raise ValueError(
            "method must be grid, morris, discrete_oat, canonical_morris or sobol"
        )
    if not isinstance(design.base_config, dict) or not isinstance(
        design.base_config.get("config"), dict
    ):
        raise TypeError("base configuration and its config block must be mappings")
    _json(design.base_config)
    names = tuple(sorted(design.factors))
    if design.method == "sobol" and "seed" in design.factors:
        raise ValueError("SobolDesign factors must not contain the special factor seed")
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
        if design.method in {"sobol", "canonical_morris"} and len(values) < 2:
            raise ValueError(f"factor {name!r} needs two or more values")
        if (
            design.method in {"sobol", "canonical_morris"}
            and len(set(values)) != len(values)
        ):
            raise ValueError(f"factor {name!r} values must be distinct")
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
        universe = math.factorial(dimensions) * design.levels**dimensions
        if (
            type(design.candidate_trajectories) is not int
            or design.candidate_trajectories < design.trajectories
            or design.candidate_trajectories > MAX_CANONICAL_MORRIS_CANDIDATES
            or design.candidate_trajectories > universe
        ):
            raise ValueError(
                "candidate_trajectories must be at least trajectories and at most "
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
        if (
            type(design.bootstrap_resamples) is not int
            or not 100 <= design.bootstrap_resamples < 2**63
        ):
            raise ValueError(
                "canonical Morris bootstrap_resamples must be a signed int64 at least 100"
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
        return tuple(sorted((*names, "seed")))
    return names


def _morris_positions(design: MorrisDesign, names: tuple[str, ...]):  # type: ignore[no-untyped-def]
    definition = {
        "designSeed": design.design_seed,
        "factors": [{"name": name, "values": design.factors[name]} for name in names],
    }
    encoded = _json(definition)
    count = math.factorial(len(names)) * math.prod(
        2 * (len(design.factors[name]) - 1) for name in names
    )
    offset = (
        int.from_bytes(
            hashlib.sha256(MORRIS_DOMAIN + encoded + b"\0offset").digest(), "big"
        )
        % count
    )
    stride = (
        int.from_bytes(
            hashlib.sha256(MORRIS_DOMAIN + encoded + b"\0stride").digest(), "big"
        )
        % count
        or 1
    )
    while math.gcd(stride, count) != 1:
        stride = (stride + 1) % count or 1
    for trajectory in range(design.trajectories):
        code = (offset + trajectory * stride) % count
        choices = []
        for name in names:
            code, digit = divmod(code, 2 * (len(design.factors[name]) - 1))
            choices.append((digit // 2, 1 if digit % 2 == 0 else -1))
        available, permutation = list(range(len(names))), []
        for remaining in range(len(names), 0, -1):
            block = math.factorial(remaining - 1)
            index, code = divmod(code, block)
            permutation.append(available.pop(index))
        point = [edge if direction == 1 else edge + 1 for edge, direction in choices]
        points = [tuple(point)]
        for factor in permutation:
            point = list(point)
            point[factor] += choices[factor][1]
            points.append(tuple(point))
        for replication, seed in enumerate(design.seeds):
            for rank, indices in enumerate(points):
                role = (
                    f"trajectory:{trajectory}:rank:0:start"
                    if rank == 0
                    else f"trajectory:{trajectory}:rank:{rank}:factor:{names[permutation[rank - 1]]}"
                )
                yield indices, replication, seed, role, definition


def _discrete_oat_positions(design: DiscreteOATDesign, names: tuple[str, ...]):  # type: ignore[no-untyped-def]
    definition = {
        "designSeed": design.design_seed,
        "factors": [{"name": name, "values": design.factors[name]} for name in names],
    }
    encoded = _json(definition)
    count = math.factorial(len(names)) * math.prod(
        2 * (len(design.factors[name]) - 1) for name in names
    )
    offset = int.from_bytes(
        hashlib.sha256(DISCRETE_OAT_DOMAIN + encoded + b"\0offset").digest(), "big"
    ) % count
    stride = int.from_bytes(
        hashlib.sha256(DISCRETE_OAT_DOMAIN + encoded + b"\0stride").digest(), "big"
    ) % count or 1
    while math.gcd(stride, count) != 1:
        stride = (stride + 1) % count or 1
    for trajectory in range(design.trajectories):
        code = (offset + trajectory * stride) % count
        choices = []
        for name in names:
            code, digit = divmod(code, 2 * (len(design.factors[name]) - 1))
            choices.append((digit // 2, 1 if digit % 2 == 0 else -1))
        available, permutation = list(range(len(names))), []
        for remaining in range(len(names), 0, -1):
            block = math.factorial(remaining - 1)
            index, code = divmod(code, block)
            permutation.append(available.pop(index))
        point = [edge if direction == 1 else edge + 1 for edge, direction in choices]
        points = [tuple(point)]
        for factor in permutation:
            point = list(point)
            point[factor] += choices[factor][1]
            points.append(tuple(point))
        for replication, seed in enumerate(design.seeds):
            for rank, indices in enumerate(points):
                role = (
                    f"trajectory:{trajectory}:rank:0:start"
                    if rank == 0
                    else f"trajectory:{trajectory}:rank:{rank}:factor:{names[permutation[rank - 1]]}"
                )
                yield indices, replication, seed, role, definition


def _canonical_candidate(code: int, dimensions: int, levels: int) -> list[list[int]]:
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


def _trajectory_distance(left: list[list[int]], right: list[list[int]]) -> int:
    return sum(
        (left_value - right_value) ** 2
        for left_point in left
        for right_point in right
        for left_value, right_value in zip(left_point, right_point)
    )


def _discrete_uniform_generalized_inverse_index(
    support_length: int, levels: int, grid_index: int
) -> int:
    if grid_index == 0:
        return 0
    return min(
        (support_length * grid_index - 1) // (levels - 1),
        support_length - 1,
    )


def _canonical_replication_seed(
    design_seed: int,
    base_seed: int,
    trajectory_id: str,
    replication_index: int,
) -> int:
    preimage = {
        "baseSeed": base_seed,
        "designSeed": design_seed,
        "replicationIndex": replication_index,
        "trajectoryId": trajectory_id,
        "version": 1,
    }
    unsigned = int.from_bytes(
        hashlib.sha256(CANONICAL_MORRIS_SEED_DOMAIN + _json(preimage)).digest()[:8],
        "big",
    )
    return unsigned if unsigned < 2**63 else unsigned - 2**64


def _canonical_definition(
    design: CanonicalMorrisDesign, names: tuple[str, ...]
) -> tuple[dict, list[tuple[str, list[list[int]]]]]:
    candidate_definition = {
        "designSeed": design.design_seed,
        "factors": [
            {
                "name": name,
                "support": design.factors[name],
                "distribution": design.distributions[name],
            }
            for name in names
        ],
        "levels": design.levels,
        "candidateTrajectories": design.candidate_trajectories,
    }
    pool, selected = _canonical_selection(
        candidate_definition, design.trajectories
    )
    definition = {
        "bootstrapResamples": design.bootstrap_resamples,
        "candidateDefinition": candidate_definition,
        "candidatePoolIdentities": [identity for identity, _ in pool],
        "confidenceLevel": design.confidence_level,
        "convergenceCheckpoints": list(design.convergence_checkpoints),
        "delta": {"numerator": design.levels // 2, "denominator": design.levels - 1},
        "distributions": [
            {"name": name, **design.distributions[name]} for name in names
        ],
        "factors": [
            {"name": name, "support": design.factors[name]} for name in names
        ],
        "inverseCdfMap": "discrete-uniform-generalized-inverse-v1",
        "p": design.levels,
        "replicationBaseSeeds": list(design.seeds),
        "scientificContract": design.scientific_contract,
        "screeningTopK": design.screening_top_k,
        "seedDerivation": {
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
        },
        "selectedTrajectories": [
            {"candidateId": identity, "matrix": matrix, "selectionOrder": index}
            for index, (identity, matrix) in enumerate(selected)
        ],
        "selectionAlgorithm": "greedy-sum-pairwise-squared-distance",
        "selectionVersion": 1,
        "trajectories": design.trajectories,
    }
    return definition, selected


def _canonical_selection(
    candidate_definition: dict, trajectories: int
) -> tuple[list[tuple[str, list[list[int]]]], list[tuple[str, list[list[int]]]]]:
    dimensions = len(candidate_definition["factors"])
    levels = candidate_definition["levels"]
    encoded = _json(candidate_definition)
    universe = math.factorial(dimensions) * levels**dimensions
    offset = int.from_bytes(
        hashlib.sha256(CANONICAL_MORRIS_DOMAIN + encoded + b"\0offset").digest(),
        "big",
    ) % universe
    stride = int.from_bytes(
        hashlib.sha256(CANONICAL_MORRIS_DOMAIN + encoded + b"\0stride").digest(),
        "big",
    ) % universe or 1
    while math.gcd(stride, universe) != 1:
        stride = (stride + 1) % universe or 1
    pool = []
    for index in range(candidate_definition["candidateTrajectories"]):
        matrix = _canonical_candidate(
            (offset + index * stride) % universe, dimensions, levels
        )
        identity = hashlib.sha256(_json(matrix)).hexdigest()
        pool.append((identity, matrix))
    by_identity = dict(pool)
    if len(by_identity) != len(pool):
        raise RuntimeError("canonical Morris candidate identity collision")
    if trajectories == 1:
        selected_ids = [min(by_identity)]
    else:
        best_pair = None
        best_distance = -1
        for pair in itertools.combinations(sorted(by_identity), 2):
            distance = _trajectory_distance(by_identity[pair[0]], by_identity[pair[1]])
            if distance > best_distance:
                best_pair, best_distance = pair, distance
        if best_pair is None:
            raise RuntimeError("canonical Morris selection requires two candidates")
        selected_ids = list(best_pair)
        while len(selected_ids) < trajectories:
            best_candidate = None
            best_score = -1
            for candidate in sorted(set(by_identity) - set(selected_ids)):
                score = sum(
                    _trajectory_distance(by_identity[candidate], by_identity[selected])
                    for selected in selected_ids
                )
                if score > best_score:
                    best_candidate, best_score = candidate, score
            if best_candidate is None:
                raise RuntimeError("canonical Morris candidate pool was exhausted")
            selected_ids.append(best_candidate)
    selected = [(identity, by_identity[identity]) for identity in selected_ids]
    return pool, selected


def _canonical_morris_positions(
    design: CanonicalMorrisDesign,
    names: tuple[str, ...],
    definition: dict,
    selected: list[tuple[str, list[list[int]]]],
):  # type: ignore[no-untyped-def]
    actual_seeds = set()
    for trajectory, (trajectory_id, matrix) in enumerate(selected):
        mapped = [
            tuple(
                design.factors[name][
                    _discrete_uniform_generalized_inverse_index(
                        len(design.factors[name]), design.levels, grid_index
                    )
                ]
                for name, grid_index in zip(names, point)
            )
            for point in matrix
        ]
        for rank in range(1, len(mapped)):
            changed = [
                index
                for index, (before, after) in enumerate(zip(mapped[rank - 1], mapped[rank]))
                if before != after
            ]
            if len(changed) != 1:
                raise ValueError(
                    "canonical Morris mapped step endpoints must differ in exactly one factor"
                )
        for replication, base_seed in enumerate(design.seeds):
            seed = _canonical_replication_seed(
                design.design_seed, base_seed, trajectory_id, replication
            )
            if seed in actual_seeds:
                raise RuntimeError("canonical Morris derived seed collision between blocks")
            actual_seeds.add(seed)
            for rank, values in enumerate(mapped):
                role = (
                    f"trajectory:{trajectory}:rank:0:start"
                    if rank == 0
                    else f"trajectory:{trajectory}:rank:{rank}:factor:{names[changed_factor(matrix, rank)]}"
                )
                yield values, replication, seed, role, definition


def changed_factor(matrix: list[list[int]], rank: int) -> int:
    return next(
        index
        for index, (before, after) in enumerate(zip(matrix[rank - 1], matrix[rank]))
        if before != after
    )


def _sobol_positions(design: SobolDesign, names: tuple[str, ...]):  # type: ignore[no-untyped-def]
    domains = {**design.factors, "seed": list(design.seeds)}
    definition = {
        "baseSampleSize": design.base_sample_size,
        "designSeed": design.design_seed,
        "scramble": True,
        "bits": 64,
        "mapping": "equal-width-v1",
        "factors": [{"name": name, "values": domains[name]} for name in names],
    }
    sample = qmc.Sobol(
        d=2 * len(names),
        scramble=True,
        bits=64,
        rng=design.design_seed,
        optimization=None,
    ).random_base2(m=design.base_sample_size.bit_length() - 1)
    indices = [
        tuple(
            math.floor(float(value) * len(domains[name]))
            for name, value in zip(names * 2, row)
        )
        for row in sample
    ]
    validate_sobol_effective_hybrid_contrasts(indices, names)
    for row_index, point in enumerate(indices):
        a, b = point[: len(names)], point[len(names) :]
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
        for role, selected in positions:
            values = tuple(domains[name][index] for name, index in zip(names, selected))
            yield values, role, definition


def validate_sobol_effective_hybrid_contrasts(points, names):  # type: ignore[no-untyped-def]
    dimensions = len(names)
    for factor, name in enumerate(names):
        if not any(point[factor] != point[dimensions + factor] for point in points):
            raise ValueError(
                f"Sobol factor {name!r} has no effective hybrid contrast after "
                "discrete projection; increase base_sample_size or change its domain"
            )


def _table(design: Design, names: tuple[str, ...]) -> pa.Table:
    metadata = {
        b"planSchemaVersion": b"1",
        b"canonicalizationVersion": b"1",
        b"contextDescriptor": _json(design.context_descriptor),
    }
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
        metadata[b"planSchemaVersion"] = b"2"
        metadata.update(
            {
                b"canonicalMorrisDesignVersion": b"1",
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
        metadata.update({b"sobolDesignVersion": b"1", b"sobolDesign": _json(sobol)})
    columns = tuple(name for name in names if name != "seed")
    scalar_types = {int: pa.int64(), float: pa.float64(), str: pa.string()}
    types = (
        [pa.string()] * 4
        + [pa.int64(), pa.string()]
        + [scalar_types[type(design.factors[n][0])] for n in columns]
    )
    rows, identifiers, runs, levels = (
        [],
        set(),
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
    )
    for position in selected:
        if design.method == "sobol":
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
                if design.method == "sobol"
                else extra[1]
                if design.method == "canonical_morris"
                else {**extra[1], "trajectories": design.trajectories},
                **(
                    {}
                    if design.method == "sobol"
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
            else SOBOL_DOMAIN
        )
        design_id = hashlib.sha256(domain + _json(preimage)).hexdigest()[:32]
        if design_id in identifiers:
            raise RuntimeError("designId collision between distinct design positions")
        identifiers.add(design_id)
        levels = tuple(logical[name] for name in columns)
        resolved = resolve_config(design, columns, levels, seed)
        nested = resolved["config"]
        canonical, run_id = _json(resolved), compute_run_id(resolved, seed)
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
                *(nested[name] for name in columns),
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
    resolved = copy.deepcopy(design.base_config)
    nested = resolved["config"]
    nested.update(zip(names, values))
    nested["random_seed"] = seed
    if "run" in design.requirements:
        resolved["run"] = copy.deepcopy(design.requirements["run"])
    nested.update(copy.deepcopy(design.requirements.get("config", {})))
    return resolved


def unique_config_seed_pairs(design: Design) -> bool:
    names = _validate(design)
    ids = _table(design, names)["runId"].to_pylist()
    return len(ids) == len(set(ids))


def load_run_specs(
    path: Path, design: Design, campaign: str
) -> tuple[RunSpec, ...]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("plan.parquet must be a regular non-symlink file")
    names = _validate(design)
    expected, actual = _table(design, names), pq.ParquetFile(path).read()
    same = expected.equals(actual)
    if design.method == "sobol" and expected.schema.equals(
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


def build_plan(design: Design) -> PlanResult:
    names = _validate(design)
    candidate = _table(design, names)
    if design.method == "canonical_morris":
        try:
            budget = design.scientific_contract["charter"]["budget"]
            allowed = _available_scientific_runs(
                budget["maximum_runs"], budget["reserve_fraction"]
            )
        except (KeyError, TypeError) as error:
            raise ValueError("scientific_contract must contain charter budget") from error
        unique_runs = len(set(candidate["runId"].to_pylist()))
        if allowed == 0 or unique_runs > allowed:
            raise ValueError(
                f"canonical Morris unique runs {unique_runs} exceed scientific execution budget {allowed}"
            )
    root, destination = design.campaign_root, design.campaign_root / "plan.parquet"
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise ValueError("plan.parquet must be a regular non-symlink file")
    if destination.exists():
        existing = pq.ParquetFile(destination).read()
        if candidate.schema.equals(
            existing.schema, check_metadata=True
        ) and candidate.equals(existing):
            return PlanResult(
                "unchanged",
                Path("plan.parquet"),
                len(candidate),
                len(set(candidate["runId"].to_pylist())),
                design.context_hash,
            )
        raise FileExistsError("plan differs; choose a new campaign_root")
    if root.exists() and next(root.iterdir(), None) is not None:
        raise FileExistsError("campaign_root contains durable state without a plan")
    write_parquet_atomic(candidate, destination)
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
