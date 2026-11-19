"""Validation and analysis for replicated-scramble Sobol designs."""

import hashlib
import json
import math
import re
import statistics
from collections import Counter

import numpy as np
import pyarrow as pa
from scipy.stats import t as student_t

from eqasim_sa.core.charter import validate_scientific_stopping
from eqasim_sa.core.context_identity import ExecutionContextIdentity
from eqasim_sa.core.indicators import validate_outputs_declaration
from eqasim_sa.core.run import validate_model_document
from eqasim_sa.core.methods.common import _json
from eqasim_sa.core.methods.multiscramble_sobol_plan import (
    MAX_MULTISCRAMBLE_DESIGN_POSITIONS,
    MAX_MULTISCRAMBLE_TABLE_CELLS,
    MULTISCRAMBLE_SOBOL_DOMAIN,
    validate_multiscramble_scientific_contract,
)
from eqasim_sa.core.methods.sobol_plan import (
    validate_sobol_effective_hybrid_contrasts,
)
from eqasim_sa.core.methods.structured import (
    validate_structured_factor_supports,
    validate_structured_plan_metadata,
)
from eqasim_sa.core.sensitivity import (
    engine_descriptor,
    sobol_symmetric_indices,
    sobol_unit_positions,
)


def _objective_average_ranks(
    values: dict[str, float], *, descending: bool
) -> dict[str, float]:
    ordered = sorted(
        values,
        key=lambda name: (
            (-values[name]) if descending else values[name],
            name,
        ),
    )
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
        b"outputsDeclaration",
        b"modelDocument",
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
    validate_outputs_declaration(metadata[b"outputsDeclaration"])
    validate_model_document(metadata[b"modelDocument"])
    try:
        design = json.loads(metadata[b"multiScrambleSobolDesign"])
        descriptor = json.loads(metadata[b"contextDescriptor"])
        design_bytes = _json(design)
    except Exception as error:
        raise ValueError("multi-scramble Sobol metadata is invalid") from error
    try:
        context_identity = ExecutionContextIdentity.from_descriptor(descriptor)
    except ValueError as error:
        raise ValueError("multi-scramble Sobol contextDescriptor is invalid") from error
    if (
        metadata[b"multiScrambleSobolDesign"] != design_bytes
        or metadata[b"contextDescriptor"] != context_identity.canonical_bytes
    ):
        raise ValueError("multi-scramble Sobol metadata is not canonical")
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
            *(
                pa.field(name, pa.string(), False)
                for name in ("designId", "method", "role", "runId")
            ),
            pa.field("seed", pa.int64(), False),
            pa.field("contextHash", pa.string(), False),
            *(pa.field(name, support_types[name], False) for name in columns),
        ]
    )
    if plan.column_names != expected_schema.names or not plan.schema.remove_metadata().equals(
        expected_schema
    ):
        raise ValueError("multi-scramble Sobol plan schema does not match sealed factors")
    context_hash = context_identity.sha256
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
            raise ValueError(
                f"multi-scramble Sobol role is duplicate or invalid: {role!r}"
            )
        position, design_id = expected[role]
        actual = tuple(
            location["seed"] if name == "seed" else location[name]
            for name in names
        )
        run_id = location["runId"]
        if (
            actual != position
            or location["designId"] != design_id
            or location["method"] != "multiscramble_sobol"
            or location["contextHash"] != context_hash
        ):
            raise ValueError(
                "multi-scramble Sobol role, designId, context, or mapped value is invalid"
            )
        if (
            config_runs.setdefault(actual, run_id) != run_id
            or run_configs.setdefault(run_id, actual) != actual
        ):
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
                            [
                                observations[locations[role]["runId"], indicator]
                                for role in roles
                            ],
                            dtype=np.float64,
                        )
                    except KeyError as error:
                        raise ValueError(
                            f"multi-scramble Sobol indicator {indicator!r} is incomplete"
                        ) from error

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
    critical = float(
        student_t.ppf((1 + confidence_level) / 2, len(scramble_seeds) - 1)
    )
    for indicator in indicators:
        for checkpoint in sizes:
            aggregates = {}
            is_primary_indicator = indicator == primary_indicator
            for factor in names:
                first_values = [
                    estimates[indicator, checkpoint, scramble, factor]["first_order"]
                    for scramble in range(len(scramble_seeds))
                ]
                total_values = [
                    estimates[indicator, checkpoint, scramble, factor]["total_order"]
                    for scramble in range(len(scramble_seeds))
                ]
                try:
                    first_mean = statistics.fmean(first_values)
                    total_mean = statistics.fmean(total_values)
                    first_sd = statistics.stdev(first_values)
                    total_sd = statistics.stdev(total_values)
                except OverflowError as error:
                    raise ValueError(
                        "multi-scramble Sobol aggregate is not finite"
                    ) from error
                root_scrambles = math.sqrt(len(scramble_seeds))
                first_se = first_sd / root_scrambles
                total_se = total_sd / root_scrambles
                first_margin, total_margin = critical * first_se, critical * total_se
                previous_value = previous.get((indicator, factor))
                first_change = (
                    first_mean - previous_value[0]
                    if previous_value is not None
                    else None
                )
                total_change = (
                    total_mean - previous_value[1]
                    if previous_value is not None
                    else None
                )
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
                    )
                    if objective == "ff"
                    and is_primary_indicator
                    and factor != "seed"
                    else None,
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
            ranks = _objective_average_ranks(
                rank_values, descending=objective == "fp"
            )
            for factor in names:
                aggregates[factor]["rank"] = ranks.get(factor)
                convergence_rows.append(aggregates[factor])
                previous[indicator, factor] = (
                    aggregates[factor]["first_order_mean"],
                    aggregates[factor]["total_order_mean"],
                )
    final_size = sizes[-1]
    index_rows = [row for row in convergence_rows if row["checkpoint"] == final_size]
    by_schema = pa.schema(
        [
            pa.field("indicator", pa.string(), False),
            pa.field("checkpoint", pa.int64(), False),
            pa.field("scramble", pa.int64(), False),
            pa.field("factor", pa.string(), False),
            pa.field("is_actionable", pa.bool_(), False),
            pa.field("first_order", pa.float64(), False),
            pa.field("total_order", pa.float64(), False),
            pa.field("output_variance", pa.float64(), False),
            pa.field("effective_contrasts", pa.int64(), False),
            pa.field("effective_contrast_rate", pa.float64(), False),
            pa.field("ST_minus_S", pa.float64(), False),
            pa.field("first_order_out_of_range", pa.bool_(), False),
            pa.field("total_order_out_of_range", pa.bool_(), False),
        ]
    )
    convergence_schema = pa.schema(
        [
            pa.field("indicator", pa.string(), False),
            pa.field("factor", pa.string(), False),
            pa.field("checkpoint", pa.int64(), False),
            pa.field("scrambles", pa.int64(), False),
            pa.field("confidence_level", pa.float64(), False),
            *[
                pa.field(f"{order}_{statistic}", pa.float64(), False)
                for order in ("first_order", "total_order")
                for statistic in ("mean", "sd", "se", "ci_low", "ci_high", "ci_width")
            ],
            pa.field("first_order_change", pa.float64()),
            pa.field("total_order_change", pa.float64()),
            pa.field("is_primary_indicator", pa.bool_(), False),
            pa.field("is_actionable", pa.bool_(), False),
            pa.field("objective", pa.string(), False),
            pa.field("within_practical_tolerance", pa.bool_()),
            pa.field("rank", pa.float64()),
        ]
    )
    diagnostics_schema = pa.schema(
        [
            pa.field("indicator", pa.string(), False),
            pa.field("checkpoint", pa.int64(), False),
            pa.field("scramble", pa.int64(), False),
            pa.field("sum_first_order", pa.float64(), False),
            pa.field("first_order_out_of_range_count", pa.int64(), False),
            pa.field("total_order_out_of_range_count", pa.int64(), False),
            pa.field("negative_gap_count", pa.int64(), False),
            pa.field("design_positions", pa.int64(), False),
            pa.field("unique_run_ids", pa.int64(), False),
            pa.field("duplicate_fraction", pa.float64(), False),
            pa.field("maximum_multiplicity", pa.int64(), False),
        ]
    )
    index_schema = convergence_schema
    return (
        pa.Table.from_pylist(index_rows, schema=index_schema),
        pa.Table.from_pylist(by_scramble_rows, schema=by_schema),
        pa.Table.from_pylist(convergence_rows, schema=convergence_schema),
        pa.Table.from_pylist(diagnostic_rows, schema=diagnostics_schema),
    )
