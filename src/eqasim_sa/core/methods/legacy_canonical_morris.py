"""Validation of legacy canonical Morris plans."""

import hashlib
import itertools
import math
import re
from decimal import Decimal

import pyarrow as pa

from eqasim_sa.core.methods.canonical_morris_plan import (
    MAX_CANONICAL_MORRIS_CANDIDATES,
    MAX_CANONICAL_MORRIS_DISTANCE_TERMS,
    _canonical_replication_seed,
    _discrete_uniform_generalized_inverse_index,
    canonical_morris_distance_terms,
)
from eqasim_sa.core.methods.common import (
    _json,
    trajectory_role,
    validate_bootstrap_resamples,
)
from eqasim_sa.core.methods.legacy_common import (
    _legacy_support,
    _validate_legacy_plan_schema,
    _validate_legacy_scientific_contract,
)
from eqasim_sa.core.methods.multiscramble_sobol_plan import (
    MAX_MULTISCRAMBLE_DESIGN_POSITIONS,
    MAX_MULTISCRAMBLE_TABLE_CELLS,
)

LEGACY_CANONICAL_MORRIS_DOMAIN = b"eqasim-sa:design:canonical-morris:v1\0"

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
                role = trajectory_role(
                    trajectory,
                    rank,
                    names[changed_factors[rank - 1]] if rank else None,
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
