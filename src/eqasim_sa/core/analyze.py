import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import stat
import statistics
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from scipy.stats import qmc

from eqasim_sa.core.indicators import ArtifactSet, Indicator
from eqasim_sa.core.ledger import _write_atomic, plan_run_ids, write_parquet_atomic
from eqasim_sa.core.plan import validate_morris_values

BOOTSTRAP_DOMAIN = b"eqasim-sa:bootstrap:sobol:v1\0"
SOBOL_DOMAIN = b"eqasim-sa:design:sobol:v1\0"


@dataclass(frozen=True)
class AnalyzeResult:
    path: Path
    dispersion_path: Path | None
    sensitivity_method: str | None
    sensitivity_path: Path | None
    runs: int
    indicators: int
    values: int
    completion_rate: float
    expected: int
    completed: int
    missing: int
    failed: int


def analyze(
    campaign_root: Path,
    indicators: Sequence[Indicator],
    *,
    campaign: str,
    completeness_threshold: float,
    seed_role: str = "replication",
    method: str = "grid",
    bootstrap_resamples: int | None = None,
    confidence_level: float | None = None,
) -> AnalyzeResult:
    campaign_root = campaign_root.resolve(strict=True)
    lock_name = hashlib.sha256(os.fsencode(campaign_root)).hexdigest()[:16]
    lock = campaign_root.parent / f".eqasim-sa-campaign-{lock_name}.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return _analyze_locked(
            campaign_root,
            indicators,
            campaign,
            completeness_threshold,
            seed_role,
            method,
            bootstrap_resamples,
            confidence_level,
        )
    finally:
        os.close(descriptor)


def _analyze_locked(
    campaign_root: Path,
    indicators: Sequence[Indicator],
    campaign: str,
    completeness_threshold: float,
    seed_role: str,
    method: str,
    bootstrap_resamples: int | None,
    confidence_level: float | None,
) -> AnalyzeResult:
    if method == "morris" and seed_role != "replication":
        raise ValueError("Morris requires seed_role replication")
    if method == "morris" and not indicators:
        raise ValueError("Morris requires at least one indicator")
    if method == "sobol" and seed_role != "factor":
        raise ValueError("Sobol requires seed_role factor")
    if method == "sobol" and not indicators:
        raise ValueError("Sobol requires at least one indicator")
    if method == "sobol" and (
        type(bootstrap_resamples) is not int
        or not 1 <= bootstrap_resamples <= 2**63 - 1
    ):
        raise ValueError(
            "Sobol bootstrap_resamples must be a positive signed int64 integer"
        )
    if method == "sobol" and (
        type(confidence_level) is not float
        or not math.isfinite(confidence_level)
        or not 0 < confidence_level < 1
    ):
        raise ValueError("Sobol confidence_level must be a finite float in (0,1)")
    if seed_role not in {"replication", "factor"}:
        raise ValueError("seed_role must be replication or factor")
    if (
        type(completeness_threshold) is not float
        or not math.isfinite(completeness_threshold)
        or not 0 <= completeness_threshold <= 1
    ):
        raise ValueError("completeness_threshold must be a finite float in [0,1]")
    _validate_indicators(indicators)
    plan = campaign_root / "plan.parquet"
    if plan.is_symlink() or not plan.is_file():
        raise ValueError("plan.parquet must be a regular non-symlink file")
    plan_table = pq.read_table(plan)
    if not len(plan_table):
        raise ValueError("plan contains no expected runs")
    methods = (
        set(plan_table["method"].to_pylist())
        if "method" in plan_table.column_names
        else set()
    )
    if (
        len(methods) != 1
        or method not in {"grid", "morris", "sobol"}
        or methods != {method}
    ):
        actual = next(iter(methods), "missing") if len(methods) <= 1 else "mixed"
        raise ValueError(
            f"plan method {actual} is incompatible with requested method {method}"
        )
    if method in {"morris", "sobol"} and completeness_threshold != 1.0:
        raise ValueError(f"{method.title()} completeness_threshold must equal 1.0")
    protected = (plan, campaign_root / "runs", campaign_root / "archives")
    output = campaign_root / "indicators.parquet"
    dispersion = campaign_root / "replication-dispersion.parquet"
    morris_sensitivity = campaign_root / "morris-ranking.parquet"
    sobol_sensitivity = campaign_root / "sobol-indices.parquet"
    generation = campaign_root / "analysis-generation.json"
    derived = (output, dispersion, morris_sensitivity, sobol_sensitivity, generation)
    expected_ids, completed_ids = plan_run_ids(campaign_root, plan, campaign)
    if not expected_ids:
        raise ValueError("plan contains no expected runs")
    failed_ids = _failure_ids(campaign_root, expected_ids - completed_ids)
    missing_ids = expected_ids - completed_ids - failed_ids
    completion_rate = len(completed_ids) / len(expected_ids)
    if completion_rate < completeness_threshold:
        raise ValueError(
            "campaign completeness "
            f"{completion_rate:.17g} is below threshold {completeness_threshold:.17g}: "
            f"missing={len(missing_ids)}, failed={len(failed_ids)}"
        )
    with tempfile.TemporaryDirectory(prefix="eqasim-sa-analyze-") as temporary_name:
        backup = Path(temporary_name) / "campaign"
        _backup(campaign_root, backup, (*protected, *derived))
        before = _snapshot(campaign_root, (*protected, *derived))
        try:
            rows = []
            for run_id in sorted(completed_ids):
                artifacts = _artifact_copy(campaign_root, backup.parent, run_id)
                for indicator in indicators:
                    try:
                        value = indicator.compute(artifacts)
                    except Exception as error:
                        raise ValueError(
                            f"run {run_id}, indicator {indicator.name!r}: {error}"
                        ) from error
                    if type(value) is not float or not math.isfinite(value):
                        raise ValueError(
                            f"run {run_id}, indicator {indicator.name!r}: must return a finite float"
                        )
                    rows.append((run_id, indicator.name, value))
            if _snapshot(campaign_root, (*protected, *derived)) != before:
                raise RuntimeError("campaign sources were mutated by an indicator")
            schema = pa.schema(
                [
                    ("runId", pa.string()),
                    ("indicator", pa.string()),
                    ("value", pa.float64()),
                ]
            )
            table = pa.Table.from_arrays(
                [
                    pa.array((row[index] for row in rows), type=field.type)
                    for index, field in enumerate(schema)
                ],
                schema=schema,
            )
            destination = write_parquet_atomic(table, output)
            dispersion_path = None
            if seed_role == "replication":
                dispersion_table = _dispersion_table(plan, rows)
                dispersion_path = write_parquet_atomic(dispersion_table, dispersion)
            elif dispersion.exists() or dispersion.is_symlink():
                dispersion.unlink()
            sensitivity_path = None
            if method == "morris":
                sensitivity_path = write_parquet_atomic(
                    _morris_ranking(plan_table, rows), morris_sensitivity
                )
                if sobol_sensitivity.exists() or sobol_sensitivity.is_symlink():
                    sobol_sensitivity.unlink()
            elif method == "sobol":
                sensitivity_path = write_parquet_atomic(
                    _sobol_ranking(
                        plan_table, rows, bootstrap_resamples, confidence_level
                    ),
                    sobol_sensitivity,
                )
                if morris_sensitivity.exists() or morris_sensitivity.is_symlink():
                    morris_sensitivity.unlink()
            else:
                for stale in (morris_sensitivity, sobol_sensitivity):
                    if stale.exists() or stale.is_symlink():
                        stale.unlink()
            manifest = {
                "version": 2,
                "indicatorsSha256": _digest(output),
                "dispersionSha256": _digest(dispersion) if dispersion_path else None,
                "sensitivitySha256": _digest(sensitivity_path)
                if sensitivity_path
                else None,
                "sensitivityMethod": method if sensitivity_path else None,
                "completionRate": completion_rate,
                "expected": len(expected_ids),
                "completed": len(completed_ids),
                "missing": len(missing_ids),
                "failed": len(failed_ids),
            }
            encoded = json.dumps(
                manifest, sort_keys=True, separators=(",", ":")
            ).encode()
            _write_atomic(generation, lambda path: path.write_bytes(encoded))
            return AnalyzeResult(
                destination,
                dispersion_path,
                method if sensitivity_path else None,
                sensitivity_path,
                len(completed_ids),
                len(indicators),
                len(rows),
                completion_rate,
                len(expected_ids),
                len(completed_ids),
                len(missing_ids),
                len(failed_ids),
            )
        except BaseException:
            if _snapshot(campaign_root, (*protected, *derived)) != before:
                _restore(campaign_root, backup, (*protected, *derived))
            raise


MORRIS_ROLE = re.compile(
    r"^trajectory:(0|[1-9][0-9]*):rank:(0|[1-9][0-9]*):(start|factor:([A-Za-z_][A-Za-z0-9_]*))$"
)


def _sobol_estimates(f_a, f_b, f_ab, f_ba, indicator: str):  # type: ignore[no-untyped-def]
    arrays = tuple(
        np.asarray(value, dtype=np.float64) for value in (f_a, f_b, f_ab, f_ba)
    )
    if any(not np.all(np.isfinite(value)) for value in arrays):
        raise ValueError(f"Sobol indicator {indicator!r} contains a non-finite value")
    f_a, f_b, f_ab, f_ba = arrays
    mean = np.mean(np.concatenate((f_a, f_b)))
    a, b, ab, ba = f_a - mean, f_b - mean, f_ab - mean, f_ba - mean
    variance = np.mean(np.concatenate((a, b)) ** 2)
    if not np.isfinite(variance) or variance <= 0:
        raise ValueError(
            f"Sobol indicator {indicator!r} has constant or invalid variance"
        )
    first = (np.mean(b * (ab - a), axis=1) + np.mean(a * (ba - b), axis=1)) / (
        2 * variance
    )
    total = (np.mean((a - ab) ** 2, axis=1) + np.mean((b - ba) ** 2, axis=1)) / (
        4 * variance
    )
    if not np.all(np.isfinite(first)) or not np.all(np.isfinite(total)):
        raise ValueError(f"Sobol indicator {indicator!r} produced non-finite indices")
    return first, total


def _sobol_bootstrap(
    f_a, f_b, f_ab, f_ba, count: int, confidence: float, design: bytes, indicator: str
):  # type: ignore[no-untyped-def]
    seed = int.from_bytes(
        hashlib.sha256(
            BOOTSTRAP_DOMAIN + design + b"\0" + indicator.encode("utf-8")
        ).digest()[:16],
        "big",
    )
    rng, first, total, attempts = np.random.default_rng(seed), [], [], 0
    while len(first) < count and attempts < 10 * count:
        attempts += 1
        indices = rng.integers(0, len(f_a), size=len(f_a))
        try:
            values = _sobol_estimates(
                f_a[indices],
                f_b[indices],
                f_ab[:, indices],
                f_ba[:, indices],
                indicator,
            )
        except ValueError as error:
            if "variance" in str(error):
                continue
            raise
        first.append(values[0])
        total.append(values[1])
    if len(first) != count:
        raise ValueError(
            f"Sobol indicator {indicator!r} has degenerate bootstrap resamples"
        )
    alpha = 1 - confidence
    return (
        np.quantile(first, (alpha / 2, 1 - alpha / 2), axis=0),
        np.quantile(total, (alpha / 2, 1 - alpha / 2), axis=0),
        attempts,
    )


def _sobol_ranking(
    plan: pa.Table,
    rows: list[tuple[str, str, float]],
    bootstrap_resamples: int,
    confidence_level: float,
) -> pa.Table:
    metadata = plan.schema.metadata or {}
    required = {
        b"planSchemaVersion",
        b"canonicalizationVersion",
        b"contextDescriptor",
        b"sobolDesignVersion",
        b"sobolDesign",
    }
    if set(metadata) != required or any(
        metadata.get(key) != b"1"
        for key in (
            b"planSchemaVersion",
            b"canonicalizationVersion",
            b"sobolDesignVersion",
        )
    ):
        raise ValueError("Sobol plan metadata is missing or unsupported")
    try:
        design, descriptor = (
            json.loads(metadata[b"sobolDesign"]),
            json.loads(metadata[b"contextDescriptor"]),
        )
    except Exception as error:
        raise ValueError("Sobol metadata is invalid") from error
    try:
        canonical = json.dumps(
            design,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        descriptor_bytes = json.dumps(
            descriptor, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (TypeError, ValueError) as error:
        raise ValueError("Sobol metadata contains non-canonical values") from error
    if (
        metadata[b"sobolDesign"] != canonical
        or metadata[b"contextDescriptor"] != descriptor_bytes
    ):
        raise ValueError("Sobol metadata is not canonical")
    context_keys = {
        "eqasimCommit",
        "dataManifestSha256",
        "lockfileSha256",
        "eqasimSaVersion",
    }
    if (
        not isinstance(descriptor, dict)
        or set(descriptor) != context_keys
        or any(type(value) is not str for value in descriptor.values())
    ):
        raise ValueError(
            "Sobol contextDescriptor must contain exactly four string keys"
        )
    if set(design) != {
        "baseSampleSize",
        "designSeed",
        "scramble",
        "bits",
        "mapping",
        "factors",
    }:
        raise ValueError("Sobol design metadata keys are invalid")
    n, design_seed, factors = (
        design["baseSampleSize"],
        design["designSeed"],
        design["factors"],
    )
    if (
        type(n) is not int
        or n < 2
        or n & (n - 1)
        or type(design_seed) is not int
        or not 0 <= design_seed < 2**64
        or design["scramble"] is not True
        or design["bits"] != 64
        or design["mapping"] != "equal-width-v1"
        or not isinstance(factors, list)
    ):
        raise ValueError("Sobol sealed design settings are invalid")
    domains, domain_types = {}, {}
    for factor in factors:
        if (
            not isinstance(factor, dict)
            or set(factor) != {"name", "values"}
            or type(factor["name"]) is not str
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", factor["name"]) is None
            or factor["name"] in domains
            or factor["name"] in {"designId", "method", "role", "runId", "contextHash"}
            or not isinstance(factor["values"], list)
            or len(factor["values"]) < 2
        ):
            raise ValueError("Sobol sealed factor domain is invalid")
        name, values = factor["name"], factor["values"]
        kind = type(values[0])
        if (
            kind not in (int, float, str)
            or any(type(value) is not kind for value in values)
            or len(set(values)) != len(values)
            or kind is int
            and any(not -(2**63) <= value < 2**63 for value in values)
            or kind is float
            and any(not math.isfinite(value) for value in values)
            or kind is str
            and any(not value or "\0" in value for value in values)
            or name == "seed"
            and kind is not int
        ):
            raise ValueError(
                "Sobol sealed factor values must be homogeneous finite scalars"
            )
        domains[name] = values
        domain_types[name] = {int: pa.int64(), float: pa.float64(), str: pa.string()}[
            kind
        ]
    names = tuple(domains)
    if (
        names != tuple(sorted(names))
        or "seed" not in domains
        or not 2 <= len(names) <= 10_600
    ):
        raise ValueError("Sobol sealed factors are invalid")
    if n * (2 * len(names) + 2) > 2**63 - 1:
        raise ValueError("Sobol N(2D+2) must fit in signed int64")
    columns = tuple(name for name in names if name != "seed")
    if plan.column_names != [
        "designId",
        "method",
        "role",
        "runId",
        "seed",
        "contextHash",
        *columns,
    ]:
        raise ValueError("Sobol plan columns do not match sealed domains")
    expected_schema = pa.schema(
        [
            *(
                pa.field(name, pa.string(), False)
                for name in ("designId", "method", "role", "runId")
            ),
            pa.field("seed", pa.int64(), False),
            pa.field("contextHash", pa.string(), False),
            *(pa.field(name, domain_types[name], False) for name in columns),
        ]
    )
    if not plan.schema.remove_metadata().equals(expected_schema):
        raise ValueError("Sobol plan Arrow schema types or nullability are invalid")
    sample = qmc.Sobol(
        d=2 * len(names), scramble=True, bits=64, rng=design_seed, optimization=None
    ).random_base2(m=n.bit_length() - 1)
    expected = {}
    for row_index, point in enumerate(sample):
        values = tuple(
            domains[name][math.floor(float(value) * len(domains[name]))]
            for name, value in zip(names * 2, point)
        )
        a, b = values[: len(names)], values[len(names) :]
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
        for role, position in positions:
            design_id = hashlib.sha256(
                SOBOL_DOMAIN
                + json.dumps(
                    {"design": design, "role": role},
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode()
            ).hexdigest()[:32]
            expected[role] = position, design_id
    if len(plan) != n * (2 * len(names) + 2):
        raise ValueError("Sobol plan cardinality is inconsistent")
    observations = {(run_id, indicator): value for run_id, indicator, value in rows}
    if len(observations) != len(rows):
        raise ValueError("multiple indicator values for one Sobol runId")
    indicators, locations, config_runs, run_configs = (
        sorted({row[1] for row in rows}),
        {},
        {},
        {},
    )
    context_hash = hashlib.sha256(
        b"eqasim-sa:context:v1\0" + descriptor_bytes
    ).hexdigest()
    for location in plan.to_pylist():
        role = location["role"]
        if role in locations or role not in expected:
            raise ValueError(f"Sobol role is missing, duplicate or invalid: {role!r}")
        position, design_id = expected[role]
        actual = tuple(
            location[name] if name != "seed" else location["seed"] for name in names
        )
        if (
            actual != position
            or location["designId"] != design_id
            or location["method"] != "sobol"
            or location["contextHash"] != context_hash
        ):
            raise ValueError(
                "Sobol sealed role, designId, context or projected value is invalid"
            )
        config = tuple(actual)
        run_id = location["runId"]
        if (
            config_runs.setdefault(config, run_id) != run_id
            or run_configs.setdefault(run_id, config) != config
        ):
            raise ValueError("Sobol runId relation is inconsistent")
        locations[role] = location
    if set(locations) != set(expected):
        raise ValueError("Sobol roles are incomplete")
    result = []
    for indicator in indicators:

        def values(prefix: str, factor: str | None = None):
            roles = (
                (
                    f"{prefix}:row:{index}"
                    if factor is None
                    else f"{prefix}:factor:{factor}:row:{index}"
                )
                for index in range(n)
            )
            try:
                return np.array(
                    [
                        observations[(locations[role]["runId"], indicator)]
                        for role in roles
                    ]
                )
            except KeyError as error:
                raise ValueError(
                    f"Sobol indicator {indicator!r} is incomplete"
                ) from error

        f_a, f_b = values("A"), values("B")
        f_ab = np.array([values("AB", factor) for factor in names])
        f_ba = np.array([values("BA", factor) for factor in names])
        first, total = _sobol_estimates(f_a, f_b, f_ab, f_ba, indicator)
        first_ci, total_ci, attempts = _sobol_bootstrap(
            f_a,
            f_b,
            f_ab,
            f_ba,
            bootstrap_resamples,
            confidence_level,
            canonical,
            indicator,
        )
        ranked = sorted(
            range(len(names)), key=lambda index: (-total[index], names[index])
        )
        for rank, index in enumerate(ranked, 1):
            result.append(
                (
                    indicator,
                    names[index],
                    rank,
                    n,
                    bootstrap_resamples,
                    attempts,
                    first[index],
                    first_ci[0, index],
                    first_ci[1, index],
                    total[index],
                    total_ci[0, index],
                    total_ci[1, index],
                )
            )
    schema = pa.schema(
        [
            pa.field("indicator", pa.string(), False),
            pa.field("factor", pa.string(), False),
            pa.field("rank", pa.int64(), False),
            pa.field("base_sample_size", pa.int64(), False),
            pa.field("bootstrap_resamples", pa.int64(), False),
            pa.field("bootstrap_attempts", pa.int64(), False),
            pa.field("first_order", pa.float64(), False),
            pa.field("first_order_ci_low", pa.float64(), False),
            pa.field("first_order_ci_high", pa.float64(), False),
            pa.field("total_order", pa.float64(), False),
            pa.field("total_order_ci_low", pa.float64(), False),
            pa.field("total_order_ci_high", pa.float64(), False),
        ]
    )
    return pa.Table.from_arrays(
        [
            pa.array((row[index] for row in result), type=field.type)
            for index, field in enumerate(schema)
        ],
        schema=schema,
    )


def _morris_ranking(plan: pa.Table, rows: list[tuple[str, str, float]]) -> pa.Table:
    metadata = plan.schema.metadata or {}
    expected_metadata = {
        b"planSchemaVersion",
        b"canonicalizationVersion",
        b"contextDescriptor",
        b"morrisDesignVersion",
        b"morrisDesign",
    }
    if set(metadata) != expected_metadata or any(
        metadata.get(name) != b"1"
        for name in (
            b"planSchemaVersion",
            b"canonicalizationVersion",
            b"morrisDesignVersion",
        )
    ):
        raise ValueError("Morris plan metadata is missing or unsupported")
    try:
        design = json.loads(metadata[b"morrisDesign"])
        descriptor = json.loads(metadata[b"contextDescriptor"])
    except Exception as error:
        raise ValueError("Morris metadata is invalid") from error
    descriptor_bytes = json.dumps(
        descriptor, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    if (
        not isinstance(descriptor, dict)
        or set(descriptor)
        != {"eqasimCommit", "dataManifestSha256", "lockfileSha256", "eqasimSaVersion"}
        or metadata[b"contextDescriptor"] != descriptor_bytes
    ):
        raise ValueError("Morris contextDescriptor metadata is invalid")
    context_hash = hashlib.sha256(
        b"eqasim-sa:context:v1\0" + descriptor_bytes
    ).hexdigest()
    if set(design) != {"designSeed", "trajectories", "factors"}:
        raise ValueError("Morris design metadata keys are invalid")
    canonical = json.dumps(
        design,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    if metadata[b"morrisDesign"] != canonical:
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
    observations = {}
    for run_id, indicator, value in rows:
        key = run_id, indicator
        if key in observations:
            raise ValueError(
                f"multiple indicator values for runId {run_id}, indicator {indicator}"
            )
        observations[key] = value
    indicators = tuple(sorted({indicator for _, indicator, _ in rows}))
    groups, identifiers, replications = {}, set(), {}
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
        if any(location[name] not in domains[name] for name in names):
            raise ValueError("Morris plan value is outside its sealed domain")
        matching = [
            index
            for index in range(len(seeds))
            if hashlib.sha256(
                b"eqasim-sa:design:morris:v1\0"
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
            raise ValueError("Morris designId does not match role and replication")
        for indicator in indicators:
            key = location["runId"], indicator
            if key not in observations:
                raise ValueError(
                    f"runId {location['runId']} has no indicator {indicator}"
                )
            groups.setdefault((indicator, trajectory, location["seed"]), []).append(
                (rank, factor, location, observations[key])
            )
    effects, signatures = {}, {}
    dimension = len(names)
    for (indicator, trajectory, seed), points in groups.items():
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
    if (
        set(signatures) != set(range(trajectories))
        or len(set(signatures.values())) != trajectories
    ):
        raise ValueError("Morris trajectory candidate signatures must be unique")
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
    return pa.Table.from_arrays(
        [
            pa.array((row[index] for row in result), type=field.type)
            for index, field in enumerate(schema)
        ],
        schema=schema,
    )


def _failure_ids(campaign_root: Path, incomplete: set[str]) -> set[str]:
    logs = campaign_root / "logs"
    if not logs.exists() and not logs.is_symlink():
        return set()
    if logs.is_symlink() or not logs.is_dir():
        raise ValueError("logs must be a regular non-symlink directory")
    failed = set()
    for run_id in incomplete:
        path = logs / f"{run_id}.log"
        if not path.exists() and not path.is_symlink():
            continue
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
                raise ValueError("must be a non-empty regular file")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
                    raise ValueError("must be a non-empty regular file")
                if not os.read(descriptor, 1):
                    raise ValueError("must be readable and non-empty")
            finally:
                os.close(descriptor)
        except Exception as error:
            raise ValueError(f"invalid failure log {path}: {error}") from error
        failed.add(run_id)
    return failed


def _dispersion_table(plan: Path, rows: list[tuple[str, str, float]]) -> pa.Table:
    table = pq.read_table(plan)
    fixed = {"designId", "method", "role", "runId", "seed", "contextHash"}
    parameters = tuple(sorted(set(table.column_names) - fixed))
    reserved = {"indicator", "n", "mean", "std", "cv", "ci95_low", "ci95_high"}
    collision = set(parameters) & reserved
    if collision:
        raise ValueError(
            f"plan parameters collide with dispersion columns: {sorted(collision)}"
        )
    configurations: dict[str, tuple] = {}
    for row in table.select(("runId", *parameters)).to_pylist():
        key = tuple(row[name] for name in parameters)
        previous = configurations.setdefault(row["runId"], key)
        if previous != key:
            raise ValueError(
                f"plan has inconsistent configuration for runId {row['runId']}"
            )
    groups: dict[tuple, list[float]] = {}
    for run_id, indicator, value in rows:
        groups.setdefault((*configurations[run_id], indicator), []).append(value)
    result = []
    for key in sorted(groups):
        values = groups[key]
        n = len(values)
        mean = statistics.fmean(values)
        std = statistics.stdev(values) if n > 1 else None
        margin = 1.96 * std / n**0.5 if std is not None else None
        if margin is not None and not math.isfinite(margin):
            margin = None
        cv = None if std is None or mean == 0 else std / abs(mean)
        if cv is not None and not math.isfinite(cv):
            cv = None
        low = None if margin is None else mean - margin
        high = None if margin is None else mean + margin
        if low is not None and not math.isfinite(low):
            low = None
        if high is not None and not math.isfinite(high):
            high = None
        result.append((*key, n, mean, std, cv, low, high))
    fields = [table.schema.field(name) for name in parameters] + [
        pa.field("indicator", pa.string(), nullable=False),
        pa.field("n", pa.int64(), nullable=False),
        pa.field("mean", pa.float64(), nullable=False),
        pa.field("std", pa.float64()),
        pa.field("cv", pa.float64()),
        pa.field("ci95_low", pa.float64()),
        pa.field("ci95_high", pa.float64()),
    ]
    schema = pa.schema(fields)
    return pa.Table.from_arrays(
        [
            pa.array((row[index] for row in result), type=field.type)
            for index, field in enumerate(schema)
        ],
        schema=schema,
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_indicators(indicators: Sequence[Indicator]) -> None:
    names: set[str] = set()
    identities: set[object] = set()
    for indicator in indicators:
        if indicator.name in names:
            raise ValueError(f"duplicate indicator name: {indicator.name}")
        names.add(indicator.name)
        identity = getattr(indicator, "identity", None)
        if identity is not None:
            if identity in identities:
                raise ValueError(f"duplicate indicator identity: {identity!r}")
            identities.add(identity)


def _artifacts(campaign_root: Path, run_id: str) -> dict[str, Path]:
    archive = campaign_root / "archives" / run_id
    if archive.is_symlink() or not archive.is_dir():
        raise ValueError(f"archive for run {run_id} must be a regular directory")
    paths = {}
    for entry in archive.iterdir():
        if entry.is_symlink() or not entry.is_file() or entry.suffix != ".parquet":
            raise ValueError(
                f"archive for run {run_id} contains invalid artifact {entry.name!r}"
            )
        paths[entry.stem] = entry
    return paths


def _artifact_copy(campaign_root: Path, temporary: Path, run_id: str) -> ArtifactSet:
    destination = temporary / "artifacts" / run_id
    destination.mkdir(parents=True)
    paths = {}
    for name, source in _artifacts(campaign_root, run_id).items():
        target = destination / source.name
        shutil.copy2(source, target)
        target.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        paths[name] = target
    destination.chmod(stat.S_IRUSR | stat.S_IXUSR)
    return ArtifactSet(paths)


def _relative(root: Path, path: Path) -> Path:
    return path.relative_to(root)


def _backup(root: Path, backup: Path, paths: tuple[Path, ...]) -> None:
    backup.mkdir()
    for source in paths:
        if not source.exists() and not source.is_symlink():
            continue
        target = backup / _relative(root, source)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir() and not source.is_symlink():
            shutil.copytree(source, target, copy_function=shutil.copy2)
        else:
            shutil.copy2(source, target, follow_symlinks=False)


def _snapshot(root: Path, paths: tuple[Path, ...]) -> tuple[tuple, ...]:
    states = []
    for path in paths:
        if not path.exists() and not path.is_symlink():
            states.append((os.fspath(_relative(root, path)), None))
            continue
        entries = (path, *sorted(path.rglob("*"))) if path.is_dir() else (path,)
        for entry in entries:
            info = entry.lstat()
            content = entry.read_bytes() if stat.S_ISREG(info.st_mode) else None
            states.append(
                (
                    os.fspath(_relative(root, entry)),
                    stat.S_IFMT(info.st_mode),
                    stat.S_IMODE(info.st_mode),
                    info.st_size,
                    info.st_mtime_ns,
                    content,
                )
            )
    return tuple(states)


def _restore(root: Path, backup: Path, paths: tuple[Path, ...]) -> None:
    for destination in paths:
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        else:
            destination.unlink(missing_ok=True)
        source = backup / _relative(root, destination)
        if not source.exists() and not source.is_symlink():
            continue
        if source.is_dir() and not source.is_symlink():
            shutil.copytree(source, destination, copy_function=shutil.copy2)
            shutil.copystat(source, destination)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination, follow_symlinks=False)
