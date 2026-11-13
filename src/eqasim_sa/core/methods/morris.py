"""Validation and ranking for adjacent one-at-a-time designs."""

import hashlib
import json
import math
import re
import statistics

import pyarrow as pa

from eqasim_sa.core.context_identity import ExecutionContextIdentity
from eqasim_sa.core.methods.common import _json, trajectory_role
from eqasim_sa.core.methods.morris_plan import (
    DISCRETE_OAT_DOMAIN,
    validate_morris_values,
)
from eqasim_sa.utils.arrow import rows_to_table

MORRIS_ROLE = re.compile(
    r"^trajectory:(0|[1-9][0-9]*):rank:(0|[1-9][0-9]*):(start|factor:([A-Za-z_][A-Za-z0-9_]*))$"
)


def _morris_ranking(plan: pa.Table, rows: list[tuple[str, str, float]]) -> pa.Table:
    return _adjacent_oat_ranking(
        plan,
        rows,
        design_version_key=b"morrisDesignVersion",
        design_key=b"morrisDesign",
        domain=b"eqasim-sa:design:morris:v1\0",
        method="morris",
        label="Morris",
    )


def _discrete_oat_effects(
    plan: pa.Table, rows: list[tuple[str, str, float]]
) -> pa.Table:
    return _adjacent_oat_ranking(
        plan,
        rows,
        design_version_key=b"discreteOatDesignVersion",
        design_key=b"discreteOatDesign",
        domain=DISCRETE_OAT_DOMAIN,
        method="discrete_oat",
        label="Discrete OAT",
    )


def _adjacent_oat_ranking(
    plan: pa.Table,
    rows: list[tuple[str, str, float]],
    *,
    design_version_key: bytes,
    design_key: bytes,
    domain: bytes,
    method: str,
    label: str,
) -> pa.Table:
    metadata = plan.schema.metadata or {}
    expected_metadata = {
        b"planSchemaVersion",
        b"canonicalizationVersion",
        b"contextDescriptor",
        design_version_key,
        design_key,
    }
    if set(metadata) != expected_metadata or any(
        metadata.get(name) != b"1"
        for name in (
            b"planSchemaVersion",
            b"canonicalizationVersion",
            design_version_key,
        )
    ):
        raise ValueError(f"{label} plan metadata is missing or unsupported")
    try:
        design = json.loads(metadata[design_key])
        context_identity = ExecutionContextIdentity.from_canonical_bytes(
            metadata[b"contextDescriptor"]
        )
    except Exception as error:
        raise ValueError("Morris metadata is invalid") from error
    context_identity.as_dict()
    context_hash = context_identity.sha256
    if set(design) != {"designSeed", "trajectories", "factors"}:
        raise ValueError("Morris design metadata keys are invalid")
    canonical = json.dumps(
        design,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    if metadata[design_key] != canonical:
        raise ValueError("Morris design metadata is not canonical")
    if (
        type(design["designSeed"]) is not int
        or not -(2**63) <= design["designSeed"] < 2**63
    ):
        raise ValueError("Morris designSeed metadata is invalid")
    factors = design["factors"]
    if not isinstance(factors, list) or not factors:
        raise ValueError("Morris factor domains are invalid")
    declared_names = tuple(
        factor.get("name") for factor in factors if isinstance(factor, dict)
    )
    if any(type(name) is not str for name in declared_names) or declared_names != tuple(
        sorted(declared_names)
    ):
        raise ValueError("Morris factors must be in lexicographic order")
    domains = {}
    for factor in factors:
        if not isinstance(factor, dict) or set(factor) != {"name", "values"}:
            raise ValueError("Morris factor domain is invalid")
        name, values = factor["name"], factor["values"]
        if (
            name in domains
            or name not in plan.column_names
            or not isinstance(values, list)
        ):
            raise ValueError("Morris factor domain is inconsistent with plan")
        try:
            validate_morris_values(name, values)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Morris factor domain is invalid: {error}") from error
        expected_type = pa.int64() if type(values[0]) is int else pa.float64()
        field = plan.schema.field(name)
        if field.nullable or field.type != expected_type:
            raise ValueError("Morris plan factor schema does not match sealed domain")
        domains[name] = values
    names = tuple(sorted(domains))
    fixed = {"designId", "method", "role", "runId", "seed", "contextHash"}
    if set(plan.column_names) - fixed != set(names):
        raise ValueError("Morris plan columns do not match sealed domains")
    fixed_types = (
        pa.string(),
        pa.string(),
        pa.string(),
        pa.string(),
        pa.int64(),
        pa.string(),
    )
    if any(
        plan.schema.field(name).nullable or plan.schema.field(name).type != kind
        for name, kind in zip(
            ("designId", "method", "role", "runId", "seed", "contextHash"), fixed_types
        )
    ):
        raise ValueError("Morris fixed plan schema is invalid")
    trajectories = design["trajectories"]
    if type(trajectories) is not int or trajectories <= 0:
        raise ValueError("Morris trajectories metadata is invalid")
    candidates = math.factorial(len(names)) * math.prod(
        2 * (len(domains[name]) - 1) for name in names
    )
    if trajectories > candidates:
        raise ValueError("Morris trajectories exceed candidate count")
    selection_definition = {
        "designSeed": design["designSeed"],
        "factors": factors,
    }
    selection_bytes = _json(selection_definition)
    offset = int.from_bytes(
        hashlib.sha256(domain + selection_bytes + b"\0offset").digest(), "big"
    ) % candidates
    stride = int.from_bytes(
        hashlib.sha256(domain + selection_bytes + b"\0stride").digest(), "big"
    ) % candidates or 1
    while math.gcd(stride, candidates) != 1:
        stride = (stride + 1) % candidates or 1
    expected_positions = {}
    for trajectory in range(trajectories):
        code = (offset + trajectory * stride) % candidates
        choices = []
        for name in names:
            code, digit = divmod(code, 2 * (len(domains[name]) - 1))
            choices.append((digit // 2, 1 if digit % 2 == 0 else -1))
        available, permutation = list(range(len(names))), []
        for remaining in range(len(names), 0, -1):
            block = math.factorial(remaining - 1)
            index, code = divmod(code, block)
            permutation.append(available.pop(index))
        point = [edge if direction == 1 else edge + 1 for edge, direction in choices]
        points = [tuple(point)]
        for factor_index in permutation:
            point = list(point)
            point[factor_index] += choices[factor_index][1]
            points.append(tuple(point))
        for rank, indices in enumerate(points):
            role = trajectory_role(
                trajectory,
                rank,
                names[permutation[rank - 1]] if rank else None,
            )
            expected_positions[role] = tuple(
                domains[name][index] for name, index in zip(names, indices)
            )
    observations = {}
    for run_id, indicator, value in rows:
        key = run_id, indicator
        if key in observations:
            raise ValueError(
                f"multiple indicator values for runId {run_id}, indicator {indicator}"
            )
        observations[key] = value
    indicators = tuple(sorted({indicator for _, indicator, _ in rows}))
    groups, plan_groups, identifiers, replications = {}, {}, set(), {}
    config_runs, run_configs = {}, {}
    seeds = {row["seed"] for row in plan.to_pylist()}
    if len(plan) != trajectories * (len(names) + 1) * len(seeds):
        raise ValueError("Morris plan cardinality is inconsistent")
    for location in plan.to_pylist():
        if location["contextHash"] != context_hash:
            raise ValueError("Morris contextHash is invalid or inconsistent")
        design_id = location["designId"]
        if (
            re.fullmatch(r"[0-9a-f]{32}", design_id or "") is None
            or design_id in identifiers
        ):
            raise ValueError("Morris designId must be unique lowercase hexadecimal")
        identifiers.add(design_id)
        match = MORRIS_ROLE.fullmatch(location["role"])
        if match is None:
            raise ValueError(f"invalid Morris role: {location['role']!r}")
        trajectory, rank = int(match.group(1)), int(match.group(2))
        factor = match.group(4)
        if trajectory >= trajectories or (rank == 0) != (factor is None):
            raise ValueError(f"invalid Morris rank or role: {location['role']!r}")
        actual = tuple(location[name] for name in names)
        if (
            location["role"] not in expected_positions
            or actual != expected_positions[location["role"]]
        ):
            raise ValueError("Morris plan value does not match its sealed trajectory")
        matching = [
            index
            for index in range(len(seeds))
            if hashlib.sha256(
                domain
                + json.dumps(
                    {
                        "design": design,
                        "replicationIndex": index,
                        "role": location["role"],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
            ).hexdigest()[:32]
            == design_id
        ]
        if (
            len(matching) != 1
            or replications.setdefault(location["seed"], matching[0]) != matching[0]
        ):
            raise ValueError(f"{label} designId does not match role and replication")
        if location["method"] != method:
            raise ValueError(f"{label} method identity is invalid")
        run_id = location["runId"]
        configuration = (*actual, location["seed"])
        if (
            re.fullmatch(r"[0-9a-f]{32}", run_id or "") is None
            or config_runs.setdefault(configuration, run_id) != run_id
            or run_configs.setdefault(run_id, configuration) != configuration
        ):
            raise ValueError(f"{label} runId relation is inconsistent")
        plan_groups.setdefault((trajectory, location["seed"]), []).append(
            (rank, factor, location)
        )
        for indicator in indicators:
            key = location["runId"], indicator
            if key not in observations:
                raise ValueError(
                    f"runId {location['runId']} has no indicator {indicator}"
                )
            groups.setdefault((indicator, trajectory, location["seed"]), []).append(
                (rank, factor, location, observations[key])
            )
    dimension = len(names)
    signatures = {}
    for (trajectory, _seed), points in plan_groups.items():
        points.sort(key=lambda item: item[0])
        if [point[0] for point in points] != list(range(dimension + 1)):
            raise ValueError("Morris trajectory has missing or duplicate ranks")
        factor_order = tuple(point[1] for point in points[1:])
        if len(set(factor_order)) != dimension or set(factor_order) != set(names):
            raise ValueError("Morris trajectory must change each factor exactly once")
        signature = (
            factor_order,
            tuple(tuple(point[2][name] for name in names) for point in points),
        )
        if signatures.setdefault(trajectory, signature) != signature:
            raise ValueError("Morris trajectory differs between replications")
        for before, after in zip(points, points[1:]):
            changed = [name for name in names if before[2][name] != after[2][name]]
            if changed != [after[1]]:
                raise ValueError(
                    "Morris transition does not change exactly its named factor"
                )
    if (
        len(plan_groups) != trajectories * len(seeds)
        or set(signatures) != set(range(trajectories))
        or len(set(signatures.values())) != trajectories
    ):
        raise ValueError("Morris trajectory candidate signatures must be unique")
    effects = {}
    for (indicator, trajectory, seed), points in groups.items():
        points.sort(key=lambda item: item[0])
        if [point[0] for point in points] != list(range(dimension + 1)):
            raise ValueError("Morris trajectory has missing or duplicate ranks")
        factor_order = tuple(point[1] for point in points[1:])
        if len(set(factor_order)) != dimension or set(factor_order) != set(names):
            raise ValueError("Morris trajectory must change each factor exactly once")
        for before, after in zip(points, points[1:]):
            factor = after[1]
            changed = [name for name in names if before[2][name] != after[2][name]]
            if changed != [factor]:
                raise ValueError(
                    "Morris transition does not change exactly its named factor"
                )
            domain = domains[factor]
            left, right = before[2][factor], after[2][factor]
            if abs(domain.index(left) - domain.index(right)) != 1:
                raise ValueError(
                    "Morris transition is not between adjacent domain levels"
                )
            delta = (right - left) / (domain[-1] - domain[0])
            effect = (after[3] - before[3]) / delta
            if not math.isfinite(effect):
                raise ValueError("Morris elementary effect is not finite")
            effects.setdefault((indicator, factor), []).append(
                (trajectory, seed, effect)
            )
    result = []
    for indicator in indicators:
        statistics_by_factor = []
        for factor in names:
            entries = effects.get((indicator, factor), [])
            if len(entries) != trajectories * len(seeds):
                raise ValueError("Morris elementary effects are incomplete")
            values = [entry[2] for entry in entries]
            mu, mu_star = statistics.fmean(values), statistics.fmean(map(abs, values))
            sigma = statistics.stdev(values) if len(values) > 1 else None
            seed_std = None
            if len(seeds) > 1:
                variances = [
                    statistics.variance(
                        entry[2] for entry in entries if entry[0] == trajectory
                    )
                    for trajectory in range(trajectories)
                ]
                seed_std = math.sqrt(statistics.fmean(variances))
            if not all(
                math.isfinite(value)
                for value in (mu, mu_star, sigma, seed_std)
                if value is not None
            ):
                raise ValueError("Morris aggregate is not finite")
            statistics_by_factor.append((factor, mu, mu_star, sigma, seed_std))
        statistics_by_factor.sort(key=lambda item: (-item[2], item[0]))
        for rank, values in enumerate(statistics_by_factor, 1):
            factor, mu, mu_star, sigma, seed_std = values
            result.append(
                (
                    indicator,
                    factor,
                    rank,
                    trajectories,
                    len(seeds),
                    trajectories * len(seeds),
                    mu,
                    mu_star,
                    sigma,
                    seed_std,
                )
            )
    schema = pa.schema(
        [
            pa.field("indicator", pa.string(), False),
            pa.field("factor", pa.string(), False),
            pa.field("rank", pa.int64(), False),
            pa.field("trajectories", pa.int64(), False),
            pa.field("replications", pa.int64(), False),
            pa.field("n", pa.int64(), False),
            pa.field("mu", pa.float64(), False),
            pa.field("mu_star", pa.float64(), False),
            pa.field("sigma", pa.float64()),
            pa.field("seed_std", pa.float64()),
        ]
    )
    return rows_to_table(result, schema)
