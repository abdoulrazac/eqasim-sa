import copy
import hashlib
import itertools
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from scipy.stats import qmc

from eqasim_sa.core.ledger import RESERVED_COLUMNS, write_parquet_atomic
from eqasim_sa.core.run import RunSpec, compute_run_id

FIXED = ("designId", "method", "role", "runId", "seed", "contextHash")
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
DOMAIN = b"eqasim-sa:design:grid:v1\0"
MORRIS_DOMAIN = b"eqasim-sa:design:morris:v1\0"
SOBOL_DOMAIN = b"eqasim-sa:design:sobol:v1\0"


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
    observations: tuple[tuple[float, int], ...],
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
        for duration, size in observations
    ):
        raise ValueError(
            "observation duration and durable bytes must be finite and non-negative"
        )
    formulas = {"sobol": "N(2D+2)", "morris": "T(D+1)R"}
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
    elif method == "morris":
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
    mean_duration, mean_bytes = (sum(values) / count for values in zip(*observations))
    hours, disk = runs * mean_duration / 3600, math.ceil(plan_bytes + runs * mean_bytes)
    if not all(
        math.isfinite(value) and value >= 0
        for value in (mean_duration, mean_bytes, hours, disk)
    ):
        raise OverflowError("estimate exceeds finite JSON numeric range")
    return {
        "status": "estimated",
        "estimateVersion": 1,
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


def _validate(design: GridDesign | MorrisDesign | SobolDesign) -> tuple[str, ...]:
    if design.method not in {"grid", "morris", "sobol"}:
        raise ValueError("method must be grid, morris or sobol")
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
        if design.method == "morris":
            validate_morris_values(name, values)
        for value in values:
            _scalar(name, value)
        if len({type(value) for value in values}) != 1:
            raise TypeError(f"factor {name!r} levels must have the same type")
        if design.method == "sobol" and len(values) < 2:
            raise ValueError(f"Sobol factor {name!r} needs two or more values")
        if design.method == "sobol" and len(set(values)) != len(values):
            raise ValueError(f"Sobol factor {name!r} values must be distinct")
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
    if design.method == "morris":
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


def _table(
    design: GridDesign | MorrisDesign | SobolDesign, names: tuple[str, ...]
) -> pa.Table:
    metadata = {
        b"planSchemaVersion": b"1",
        b"canonicalizationVersion": b"1",
        b"contextDescriptor": _json(design.context_descriptor),
    }
    if design.method == "morris":
        morris = {
            "designSeed": design.design_seed,
            "trajectories": design.trajectories,
            "factors": [
                {"name": name, "values": design.factors[name]} for name in names
            ],
        }
        metadata.update({b"morrisDesignVersion": b"1", b"morrisDesign": _json(morris)})
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
        else _sobol_positions(design, names)
    )
    for position in selected:
        if design.method == "sobol":
            all_values, role, definition = position
            logical = dict(zip(names, all_values))
            seed, replication, extra = logical["seed"], 0, [role, definition]
            indices = ()
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
    design: GridDesign | MorrisDesign | SobolDesign,
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


def unique_config_seed_pairs(design: GridDesign | MorrisDesign | SobolDesign) -> bool:
    names = _validate(design)
    ids = _table(design, names)["runId"].to_pylist()
    return len(ids) == len(set(ids))


def load_run_specs(
    path: Path, design: GridDesign | MorrisDesign | SobolDesign, campaign: str
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


def build_plan(design: GridDesign | MorrisDesign | SobolDesign) -> PlanResult:
    names = _validate(design)
    candidate = _table(design, names)
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
