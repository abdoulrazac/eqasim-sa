"""Plan generation and validation for single-scramble Sobol designs."""

from __future__ import annotations

import math

from scipy.stats import qmc

from eqasim_sa.core.methods.common import MAX_BOOTSTRAP_WORK_CELLS

SOBOL_DOMAIN = b"eqasim-sa:design:sobol:v1\0"


def validate_sobol_bootstrap_work(
    bootstrap_resamples: int, base_sample_size: int, dimensions: int
) -> None:
    work = bootstrap_resamples * base_sample_size * (2 * dimensions + 2)
    if work > MAX_BOOTSTRAP_WORK_CELLS:
        raise ValueError(
            "Sobol bootstrap B*N*(2D+2) exceeds the operational safe cap of "
            f"{MAX_BOOTSTRAP_WORK_CELLS} work cells"
        )


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
    validate_sobol_effective_hybrid_contrasts(indices, names)
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


def validate_sobol_effective_hybrid_contrasts(points, names):  # type: ignore[no-untyped-def]
    dimensions = len(names)
    for factor, name in enumerate(names):
        if not any(point[factor] != point[dimensions + factor] for point in points):
            raise ValueError(
                f"Sobol factor {name!r} has no effective hybrid contrast after "
                "discrete projection; increase base_sample_size or change its domain"
            )
