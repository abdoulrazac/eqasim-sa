"""Validation and analysis for the single-scramble Sobol method."""

import hashlib
import json
import math
import re

import numpy as np
import pyarrow as pa
from scipy.stats import qmc

from eqasim_sa.core.context_identity import ExecutionContextIdentity
from eqasim_sa.core.indicators import validate_outputs_declaration
from eqasim_sa.core.methods.common import validate_bootstrap_resamples
from eqasim_sa.core.methods.sobol_plan import (
    validate_sobol_bootstrap_work,
    validate_sobol_effective_hybrid_contrasts,
)
from eqasim_sa.core.methods.structured import (
    validate_structured_factor_supports,
    validate_structured_plan_metadata,
)
from eqasim_sa.utils.arrow import rows_to_table

BOOTSTRAP_DOMAIN = b"eqasim-sa:bootstrap:sobol:v1\0"
SOBOL_DOMAIN = b"eqasim-sa:design:sobol:v1\0"


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
    structured: dict[str, dict[str, list[str]]] | None = None,
) -> pa.Table:
    metadata = plan.schema.metadata or {}
    required = {
        b"planSchemaVersion",
        b"canonicalizationVersion",
        b"contextDescriptor",
        b"outputsDeclaration",
        b"sobolDesignVersion",
        b"sobolDesign",
    }
    if structured is None:
        structured = validate_structured_plan_metadata(dict(metadata))
    if structured:
        required |= {b"structuredFactorsVersion", b"structuredFactors"}
    if (
        set(metadata) != required
        or metadata.get(b"planSchemaVersion") != (b"3" if structured else b"1")
        or metadata.get(b"canonicalizationVersion") != b"1"
        or metadata.get(b"sobolDesignVersion") != b"1"
    ):
        raise ValueError("Sobol plan metadata is missing or unsupported")
    validate_outputs_declaration(metadata[b"outputsDeclaration"])
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
    except (TypeError, ValueError) as error:
        raise ValueError("Sobol metadata contains non-canonical values") from error
    try:
        context_identity = ExecutionContextIdentity.from_descriptor(descriptor)
    except ValueError as error:
        raise ValueError(
            "Sobol contextDescriptor must contain exactly four string keys"
        ) from error
    if (
        metadata[b"sobolDesign"] != canonical
        or metadata[b"contextDescriptor"] != context_identity.canonical_bytes
    ):
        raise ValueError("Sobol metadata is not canonical")
    design_keys = {
        "baseSampleSize",
        "designSeed",
        "scramble",
        "bits",
        "mapping",
        "factors",
    }
    if structured:
        design_keys.add("structuredFactors")
    if set(design) != design_keys:
        raise ValueError("Sobol design metadata keys are invalid")
    if structured and design["structuredFactors"] != structured:
        raise ValueError(
            "Sobol method structuredFactors do not match structured plan metadata"
        )
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
    validate_structured_factor_supports(structured, domains)
    if n * (2 * len(names) + 2) > 2**63 - 1:
        raise ValueError("Sobol N(2D+2) must fit in signed int64")
    validate_bootstrap_resamples(
        bootstrap_resamples,
        method="Sobol",
        minimum=1,
    )
    validate_sobol_bootstrap_work(bootstrap_resamples, n, len(names))
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
    projected = [
        tuple(
            domains[name][math.floor(float(value) * len(domains[name]))]
            for name, value in zip(names * 2, point)
        )
        for point in sample
    ]
    validate_sobol_effective_hybrid_contrasts(projected, names)
    expected = {}
    for row_index, values in enumerate(projected):
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
    context_hash = context_identity.sha256
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
    return rows_to_table(result, schema)
