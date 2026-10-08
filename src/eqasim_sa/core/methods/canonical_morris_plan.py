"""Plan generation and limits for canonical Morris designs."""

from __future__ import annotations

import hashlib

from eqasim_sa.core.methods.common import (
    MAX_BOOTSTRAP_WORK_CELLS,
    _json,
    trajectory_role,
)
from eqasim_sa.core.sensitivity import engine_descriptor, morris_unit_trajectories

CANONICAL_MORRIS_DOMAIN = b"eqasim-sa:design:canonical-morris:v3\0"
CANONICAL_MORRIS_SEED_DOMAIN = b"eqasim-sa:canonical-morris-seed:v1\0"
MAX_CANONICAL_MORRIS_CANDIDATES = 500
MAX_CANONICAL_MORRIS_DISTANCE_TERMS = 50_000_000


def validate_canonical_morris_bootstrap_work(
    bootstrap_resamples: int,
    factors: int,
    trajectories: int,
    replications: int,
    checkpoints: tuple[int, ...] | list[int],
) -> None:
    pairwise_rankings = factors * (factors - 1) // 2
    work = bootstrap_resamples * len(checkpoints) * (
        factors * trajectories * replications
        + factors * factors
        + pairwise_rankings
    )
    if work > MAX_BOOTSTRAP_WORK_CELLS:
        raise ValueError(
            "canonical Morris bootstrap work exceeds the operational safe cap of "
            f"{MAX_BOOTSTRAP_WORK_CELLS} work cells"
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
    matrices = morris_unit_trajectories(
        names,
        candidate_trajectories=design.candidate_trajectories,
        selected_trajectories=design.trajectories,
        levels=design.levels,
        seed=design.design_seed,
    )
    selected = [
        (
            hashlib.sha256(
                CANONICAL_MORRIS_DOMAIN
                + _json({"matrix": matrix, "selectedIndex": index})
            ).hexdigest(),
            matrix,
        )
        for index, matrix in enumerate(matrices)
    ]
    definition = {
        "bootstrapResamples": design.bootstrap_resamples,
        "candidateDefinition": candidate_definition,
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
        "numericalEngine": engine_descriptor(
            design.method,
            sample_size=design.candidate_trajectories,
            sample_seeds=(design.design_seed,),
            levels=design.levels,
            selected_trajectories=design.trajectories,
        ),
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
            {"matrix": matrix, "trajectoryId": identity}
            for identity, matrix in selected
        ],
        "trajectories": design.trajectories,
    }
    return definition, selected


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
                role = trajectory_role(
                    trajectory,
                    rank,
                    names[changed_factor(matrix, rank)] if rank else None,
                )
                yield values, replication, seed, role, definition


def changed_factor(matrix: list[list[int]], rank: int) -> int:
    return next(
        index
        for index, (before, after) in enumerate(zip(matrix[rank - 1], matrix[rank]))
        if before != after
    )
