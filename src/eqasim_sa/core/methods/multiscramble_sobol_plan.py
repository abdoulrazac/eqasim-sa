"""Plan generation and contract validation for replicated-scramble Sobol."""

from __future__ import annotations

import math

from eqasim_sa.core.charter import validate_scientific_contract
from eqasim_sa.core.methods.common import MAX_PLAN_CELLS, MAX_PLAN_POSITIONS
from eqasim_sa.core.methods.sobol_plan import validate_sobol_effective_hybrid_contrasts
from eqasim_sa.core.sensitivity import engine_descriptor, sobol_unit_positions

MULTISCRAMBLE_SOBOL_DOMAIN = b"eqasim-sa:design:multiscramble-sobol:v3\0"
MAX_MULTISCRAMBLE_DESIGN_POSITIONS = MAX_PLAN_POSITIONS
MAX_MULTISCRAMBLE_TABLE_CELLS = MAX_PLAN_CELLS


def validate_multiscramble_scientific_contract(
    contract: dict,
    factors: dict[str, list],
    distributions: dict,
) -> None:
    validate_scientific_contract(
        contract,
        factors,
        distributions,
        allowed_objectives=frozenset({"fp", "ff"}),
    )
    if contract["charter"]["stochastic"]["target"] != "finite_seed_factor":
        raise ValueError(
            "multi-scramble Sobol scientific_contract requires finite_seed_factor"
        )
    decision = contract["charter"]["decision"]
    if decision["objective"] == "ff" and decision["practical_tolerance"] > 1:
        raise ValueError(
            "multi-scramble Sobol practical_tolerance must be a dimensionless "
            "total-order variance-share tolerance in [0,1]"
        )


def _multiscramble_definition(
    design: MultiScrambleSobolDesign, names: tuple[str, ...]
) -> dict:
    domains = {**design.factors, "seed": list(design.seeds)}
    definition = {
        "baseSampleSizes": list(design.base_sample_sizes),
        "confidenceLevel": design.confidence_level,
        "factors": [
            {
                "name": name,
                "support": domains[name],
                "distribution": design.distributions[name],
            }
            for name in names
        ],
        "mapping": "equal-width-v1",
        "maximumBaseSampleSize": design.base_sample_sizes[-1],
        "numericalEngine": engine_descriptor(
            design.method,
            sample_size=design.base_sample_sizes[-1],
            sample_seeds=design.scramble_seeds,
        ),
        "saltelliEstimator": "symmetric-forward-reverse-salib-v1",
        "scientificContract": design.scientific_contract,
        "scrambleSeeds": list(design.scramble_seeds),
    }
    if design.structured_factors:
        definition["structuredFactors"] = design.structured_factors
    return definition


def _multiscramble_sobol_positions(
    design: MultiScrambleSobolDesign,
    names: tuple[str, ...],
    definition: dict,
):  # type: ignore[no-untyped-def]
    domains = {**design.factors, "seed": list(design.seeds)}
    maximum = design.base_sample_sizes[-1]
    for scramble, scramble_seed in enumerate(design.scramble_seeds):
        positions = [
            (
                role,
                tuple(
                    math.floor(value * len(domains[name]))
                    for name, value in zip(names, unit_position)
                ),
            )
            for role, unit_position in sobol_unit_positions(
                names, maximum, scramble_seed
            )
        ]
        by_role = dict(positions)
        if len(by_role) != len(positions):
            raise RuntimeError("SALib returned duplicate multi-scramble Sobol roles")
        for checkpoint in design.base_sample_sizes:
            pairs = [
                by_role[f"A:row:{row}"] + by_role[f"B:row:{row}"]
                for row in range(checkpoint)
            ]
            try:
                validate_sobol_effective_hybrid_contrasts(pairs, names)
            except ValueError as error:
                raise ValueError(
                    f"multi-scramble Sobol scramble {scramble}, checkpoint {checkpoint}: {error}"
                ) from error
        for role, selected in positions:
            values = tuple(
                domains[name][index] for name, index in zip(names, selected)
            )
            yield values, f"scramble:{scramble}:{role}", definition
