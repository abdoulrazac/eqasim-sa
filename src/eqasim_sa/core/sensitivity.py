from __future__ import annotations

import importlib.metadata
import math
from typing import Sequence

import numpy as np
from SALib.analyze import morris as salib_morris_analyze
from SALib.analyze import sobol as salib_sobol_analyze
from SALib.sample import morris as salib_morris_sample
from SALib.sample import sobol as salib_sobol_sample


SALIB_VERSION = "1.5.2"
MAX_MORRIS_CANDIDATES = 500
MAX_SOBOL_SAMPLE_POSITIONS = 1_000_000


def _installed_version() -> str:
    version = importlib.metadata.version("SALib")
    if version != SALIB_VERSION:
        raise RuntimeError(
            f"SALib {SALIB_VERSION} is required by the numerical contract, found {version}"
        )
    return version


def engine_descriptor(
    method: str,
    *,
    sample_size: int,
    sample_seeds: Sequence[int],
    levels: int | None = None,
    selected_trajectories: int | None = None,
) -> dict[str, object]:
    version = _installed_version()
    if type(sample_size) is not int or sample_size <= 0:
        raise ValueError("SALib descriptor sample_size must be positive")
    if not sample_seeds or any(type(seed) is not int for seed in sample_seeds):
        raise ValueError("SALib descriptor sample_seeds must contain integers")
    if method == "canonical_morris":
        if (
            len(sample_seeds) != 1
            or type(levels) is not int
            or type(selected_trajectories) is not int
        ):
            raise ValueError("canonical Morris descriptor options are incomplete")
        return {
            "analyzeApi": "SALib.analyze.morris.analyze",
            "analyzeOptions": {
                "conf_level": 0.95,
                "num_levels": levels,
                "num_resamples": 2,
                "print_to_console": False,
                "scaled": False,
                "seed": 0,
            },
            "estimator": "salib-morris-mu-mu-star-sigma-v1",
            "library": "SALib",
            "sampleApi": "SALib.sample.morris.sample",
            "sampleCalls": [
                {
                    "N": sample_size,
                    "local_optimization": True,
                    "num_levels": levels,
                    "optimal_trajectories": selected_trajectories,
                    "seed": sample_seeds[0] % 2**64,
                }
            ],
            "seedMapping": "signed-int64-mod-2^64-v1",
            "version": version,
        }
    if method == "multiscramble_sobol":
        if levels is not None or selected_trajectories is not None:
            raise ValueError("multi-scramble Sobol descriptor options are invalid")
        return {
            "analyzeApi": "SALib.analyze.sobol.analyze",
            "analyzeOptions": {
                "calc_second_order": False,
                "conf_level": 0.95,
                "keep_resamples": False,
                "n_processors": None,
                "num_resamples": 2,
                "parallel": False,
                "print_to_console": False,
                "seed": 0,
            },
            "estimator": "symmetric-forward-reverse-salib-v1",
            "library": "SALib",
            "sampleApi": "SALib.sample.sobol.sample",
            "sampleCalls": [
                {
                    "N": sample_size,
                    "calc_second_order": True,
                    "scramble": True,
                    "seed": seed,
                    "skip_values": 0,
                }
                for seed in sample_seeds
            ],
            "uncertainty": "independent-scrambles-student-t-v1",
            "version": version,
        }
    raise ValueError(f"unsupported SALib method {method!r}")


def _problem(names: Sequence[str]) -> dict[str, object]:
    if not names or any(not isinstance(name, str) or not name for name in names):
        raise ValueError("SALib factor names must be non-empty strings")
    if len(set(names)) != len(names):
        raise ValueError("SALib factor names must be unique")
    return {
        "num_vars": len(names),
        "names": list(names),
        "bounds": [[0.0, 1.0] for _ in names],
    }


def morris_unit_trajectories(
    names: Sequence[str],
    *,
    candidate_trajectories: int,
    selected_trajectories: int,
    levels: int,
    seed: int,
) -> list[list[list[int]]]:
    _installed_version()
    if (
        type(candidate_trajectories) is not int
        or type(selected_trajectories) is not int
        or not 2 <= selected_trajectories < candidate_trajectories
        or candidate_trajectories > MAX_MORRIS_CANDIDATES
    ):
        raise ValueError(
            "SALib Morris candidate_trajectories must be strictly greater than "
            "selected_trajectories, which must be at least 2, and at most 500"
        )
    if type(levels) is not int or levels < 4 or levels % 2:
        raise ValueError("SALib Morris levels must be an even integer of at least 4")
    values = np.asarray(
        salib_morris_sample.sample(
            _problem(names),
            candidate_trajectories,
            num_levels=levels,
            optimal_trajectories=selected_trajectories,
            local_optimization=True,
            seed=seed % 2**64,
        ),
        dtype=np.float64,
    )
    dimensions = len(names)
    expected = (selected_trajectories * (dimensions + 1), dimensions)
    if values.shape != expected or not np.all(np.isfinite(values)):
        raise RuntimeError("SALib Morris returned an invalid sample shape or value")
    scaled = values * (levels - 1)
    indices = np.rint(scaled).astype(np.int64)
    if not np.allclose(scaled, indices, rtol=0.0, atol=1e-12):
        raise RuntimeError("SALib Morris sample is not on the declared level grid")
    matrices = indices.reshape(selected_trajectories, dimensions + 1, dimensions)
    half = levels // 2
    for matrix in matrices:
        changes = np.diff(matrix, axis=0)
        if any(
            np.count_nonzero(row) != 1
            or abs(int(row[np.flatnonzero(row)[0]])) != half
            for row in changes
        ):
            raise RuntimeError("SALib Morris trajectory has an invalid elementary step")
        if sorted(int(np.flatnonzero(row)[0]) for row in changes) != list(
            range(dimensions)
        ):
            raise RuntimeError("SALib Morris trajectory does not change every factor once")
    return matrices.tolist()


def sobol_unit_positions(
    names: Sequence[str], base_sample_size: int, seed: int
) -> list[tuple[str, tuple[float, ...]]]:
    _installed_version()
    if (
        type(base_sample_size) is not int
        or base_sample_size < 2
        or base_sample_size & (base_sample_size - 1)
    ):
        raise ValueError("SALib Sobol base sample size must be a power of two")
    dimensions = len(names)
    if base_sample_size * (2 * dimensions + 2) > MAX_SOBOL_SAMPLE_POSITIONS:
        raise ValueError("SALib Sobol sample exceeds the operational position cap")
    values = np.asarray(
        salib_sobol_sample.sample(
            _problem(names),
            base_sample_size,
            calc_second_order=True,
            scramble=True,
            skip_values=0,
            seed=seed,
        ),
        dtype=np.float64,
    )
    step = 2 * dimensions + 2
    if (
        values.shape != (base_sample_size * step, dimensions)
        or not np.all(np.isfinite(values))
        or np.any(values < 0.0)
        or np.any(values >= 1.0)
    ):
        raise RuntimeError("SALib Sobol returned an invalid sample shape or value")
    positions: list[tuple[str, tuple[float, ...]]] = []
    for row in range(base_sample_size):
        block = values[row * step : (row + 1) * step]
        a, b = block[0], block[-1]
        positions.append((f"A:row:{row}", tuple(float(value) for value in a)))
        for index, name in enumerate(names):
            ab = block[index + 1]
            expected = a.copy()
            expected[index] = b[index]
            if not np.array_equal(ab, expected):
                raise RuntimeError("SALib Sobol AB hybrid orientation changed")
            positions.append(
                (f"AB:factor:{name}:row:{row}", tuple(float(value) for value in ab))
            )
        for index, name in enumerate(names):
            ba = block[dimensions + index + 1]
            expected = b.copy()
            expected[index] = a[index]
            if not np.array_equal(ba, expected):
                raise RuntimeError("SALib Sobol BA hybrid orientation changed")
            positions.append(
                (f"BA:factor:{name}:row:{row}", tuple(float(value) for value in ba))
            )
        positions.append((f"B:row:{row}", tuple(float(value) for value in b)))
    return positions


def sobol_symmetric_indices(
    names: Sequence[str],
    f_a: np.ndarray,
    f_b: np.ndarray,
    f_ab: np.ndarray,
    f_ba: np.ndarray,
    *,
    indicator: str,
) -> tuple[np.ndarray, np.ndarray]:
    _installed_version()
    dimensions = len(names)
    a = np.asarray(f_a, dtype=np.float64)
    b = np.asarray(f_b, dtype=np.float64)
    ab = np.asarray(f_ab, dtype=np.float64)
    ba = np.asarray(f_ba, dtype=np.float64)
    if (
        a.ndim != 1
        or b.shape != a.shape
        or ab.shape != (dimensions, len(a))
        or ba.shape != ab.shape
        or any(not np.all(np.isfinite(value)) for value in (a, b, ab, ba))
    ):
        raise ValueError(f"Sobol indicator {indicator!r} has invalid arrays")
    if len(a) < 2:
        raise ValueError("SALib Sobol analysis requires at least two base rows")
    base = np.concatenate((a, b))
    base_minimum, base_maximum = float(np.min(base)), float(np.max(base))
    if base_minimum == base_maximum:
        raise ValueError(f"Sobol indicator {indicator!r} has constant or invalid variance")
    complete = np.concatenate((base, ab.reshape(-1), ba.reshape(-1)))
    minimum, maximum = float(np.min(complete)), float(np.max(complete))
    center = minimum / 2 + maximum / 2
    scale = max(abs(minimum - center), abs(maximum - center))
    if not math.isfinite(scale) or scale == 0:
        raise ValueError(f"Sobol indicator {indicator!r} has constant or invalid variance")
    scaled_center = minimum / scale / 2 + maximum / scale / 2
    a, b, ab, ba = (
        (value / scale) - scaled_center for value in (a, b, ab, ba)
    )
    if any(not np.all(np.isfinite(value)) for value in (a, b, ab, ba)):
        raise ValueError(f"Sobol indicator {indicator!r} has invalid scaled arrays")
    scaled_base = np.concatenate((a, b))
    if (
        float(np.min(scaled_base)) == float(np.max(scaled_base))
        or float(np.var(scaled_base)) == 0
    ):
        raise ValueError(
            f"Sobol indicator {indicator!r} scaling erased base variance"
        )

    def analyze(left: np.ndarray, hybrids: np.ndarray, right: np.ndarray):  # type: ignore[no-untyped-def]
        output = np.empty(len(left) * (dimensions + 2), dtype=np.float64)
        blocks = output.reshape(len(left), dimensions + 2)
        blocks[:, 0] = left
        blocks[:, 1:-1] = hybrids.T
        blocks[:, -1] = right
        result = salib_sobol_analyze.analyze(
            _problem(names),
            output,
            calc_second_order=False,
            num_resamples=2,
            conf_level=0.95,
            print_to_console=False,
            parallel=False,
            keep_resamples=False,
            seed=0,
        )
        return np.asarray(result["S1"]), np.asarray(result["ST"])

    forward_first, forward_total = analyze(a, ab, b)
    reverse_first, reverse_total = analyze(b, ba, a)
    def finite_average(left: np.ndarray, right: np.ndarray) -> np.ndarray:
        with np.errstate(over="ignore"):
            combined = left + right
        return np.where(np.isfinite(combined), combined / 2, left / 2 + right / 2)

    first = finite_average(forward_first, reverse_first)
    total = finite_average(forward_total, reverse_total)
    if not np.all(np.isfinite(first)) or not np.all(np.isfinite(total)):
        raise ValueError(f"Sobol indicator {indicator!r} produced non-finite indices")
    return first, total


def morris_statistics(
    names: Sequence[str],
    unit_inputs: np.ndarray,
    outputs: np.ndarray,
    *,
    levels: int,
) -> dict[str, tuple[float, float, float]]:
    _installed_version()
    inputs = np.asarray(unit_inputs, dtype=np.float64)
    values = np.asarray(outputs, dtype=np.float64)
    dimensions = len(names)
    if (
        inputs.ndim != 2
        or inputs.shape[1] != dimensions
        or values.shape != (len(inputs),)
        or len(inputs) % (dimensions + 1)
        or not np.all(np.isfinite(inputs))
        or not np.all(np.isfinite(values))
    ):
        raise ValueError("SALib Morris analysis inputs are invalid")
    result = salib_morris_analyze.analyze(
        _problem(names),
        inputs,
        values,
        num_resamples=2,
        conf_level=0.95,
        scaled=False,
        print_to_console=False,
        num_levels=levels,
        seed=0,
    )
    statistics_by_name = {}
    for index, name in enumerate(names):
        item = (
            float(result["mu"][index]),
            float(result["mu_star"][index]),
            float(result["sigma"][index]),
        )
        if not all(math.isfinite(value) for value in item):
            raise ValueError("SALib Morris produced non-finite statistics")
        statistics_by_name[name] = item
    return statistics_by_name
