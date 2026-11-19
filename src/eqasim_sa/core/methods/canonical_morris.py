"""Validation, inference, and recommendations for canonical Morris designs."""

import hashlib
import json
import math
import re
import statistics
from decimal import Decimal

import numpy as np
import pyarrow as pa

from eqasim_sa.core.charter import (
    validate_scientific_contract,
    validate_scientific_stopping,
)
from eqasim_sa.core.context_identity import ExecutionContextIdentity
from eqasim_sa.core.indicators import validate_outputs_declaration
from eqasim_sa.core.run import validate_model_document
from eqasim_sa.core.methods.canonical_morris_plan import (
    CANONICAL_MORRIS_DOMAIN,
    MAX_CANONICAL_MORRIS_CANDIDATES,
    MAX_CANONICAL_MORRIS_DISTANCE_TERMS,
    _canonical_replication_seed,
    _discrete_uniform_generalized_inverse_index,
    canonical_morris_distance_terms,
    validate_canonical_morris_bootstrap_work,
)
from eqasim_sa.core.methods.common import (
    _json,
    trajectory_role,
    validate_bootstrap_resamples,
)
from eqasim_sa.core.sensitivity import (
    engine_descriptor,
    morris_statistics,
    morris_unit_trajectories,
)
from eqasim_sa.utils.arrow import rows_to_table

CANONICAL_MORRIS_BOOTSTRAP_DOMAIN = b"eqasim-sa:bootstrap:canonical-morris:v1\0"


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
        b"outputsDeclaration",
        b"modelDocument",
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
    validate_outputs_declaration(metadata[b"outputsDeclaration"])
    validate_model_document(metadata[b"modelDocument"])
    try:
        design = json.loads(metadata[b"canonicalMorrisDesign"])
        descriptor = json.loads(metadata[b"contextDescriptor"])
        design_bytes = _json(design)
    except Exception as error:
        raise ValueError("canonical Morris metadata is invalid") from error
    try:
        context_identity = ExecutionContextIdentity.from_descriptor(descriptor)
    except ValueError as error:
        raise ValueError(
            "canonical Morris contextDescriptor must contain exactly four string keys"
        ) from error
    if (
        metadata[b"canonicalMorrisDesign"] != design_bytes
        or metadata[b"contextDescriptor"] != context_identity.canonical_bytes
    ):
        raise ValueError("canonical Morris metadata is not canonical")
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
            *(
                pa.field(name, pa.string(), False)
                for name in ("designId", "method", "role", "runId")
            ),
            pa.field("seed", pa.int64(), False),
            pa.field("contextHash", pa.string(), False),
            *(pa.field(name, support_types[name], False) for name in columns),
        ]
    )
    expected_columns = [
        "designId",
        "method",
        "role",
        "runId",
        "seed",
        "contextHash",
        *columns,
    ]
    if plan.column_names != expected_columns or not plan.schema.remove_metadata().equals(
        expected_schema
    ):
        raise ValueError("canonical Morris plan schema does not match sealed supports")
    context_hash = context_identity.sha256
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
                role = trajectory_role(
                    trajectory,
                    rank,
                    names[changed_factors[rank - 1]] if rank else None,
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
        locations[design_id] = {
            **location,
            "trajectory": trajectory,
            "replication": replication,
            "rank": rank,
            "matrix": matrix,
        }
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
    noise_table = rows_to_table(noise_rows, noise_schema)
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
            direction = (
                matrix[after["rank"]][factor_index]
                - matrix[before["rank"]][factor_index]
            ) // half
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
        rows_to_table(ranking_rows, ranking_schema),
        rows_to_table(convergence_rows, convergence_schema),
        rows_to_table(reversal_rows, reversal_schema),
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
