"""Validation of legacy multiscramble Sobol plans."""

import hashlib
import math
import re
from decimal import Decimal

import pyarrow as pa
from scipy.stats import qmc

from eqasim_sa.core.methods.common import _json
from eqasim_sa.core.methods.legacy_common import (
    _legacy_support,
    _validate_legacy_plan_schema,
    _validate_legacy_scientific_contract,
)
from eqasim_sa.core.methods.multiscramble_sobol_plan import (
    MAX_MULTISCRAMBLE_DESIGN_POSITIONS,
    MAX_MULTISCRAMBLE_TABLE_CELLS,
)
from eqasim_sa.core.methods.sobol_plan import (
    validate_sobol_effective_hybrid_contrasts,
)
from eqasim_sa.core.methods.structured import validate_structured_factor_supports

LEGACY_MULTISCRAMBLE_SOBOL_DOMAIN = b"eqasim-sa:design:multiscramble-sobol:v1\0"

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
