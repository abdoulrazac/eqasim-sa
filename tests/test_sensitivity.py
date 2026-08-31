import numpy as np
import pytest

from eqasim_sa.core.sensitivity import (
    SALIB_VERSION,
    engine_descriptor,
    morris_statistics,
    morris_unit_trajectories,
    sobol_symmetric_indices,
    sobol_unit_positions,
)


def test_engine_descriptors_seal_public_salib_apis() -> None:
    morris = engine_descriptor(
        "canonical_morris",
        sample_size=12,
        sample_seeds=(-7,),
        levels=4,
        selected_trajectories=3,
    )
    sobol = engine_descriptor(
        "multiscramble_sobol",
        sample_size=16,
        sample_seeds=(13, 17),
    )

    assert morris["version"] == SALIB_VERSION
    assert morris["sampleApi"] == "SALib.sample.morris.sample"
    assert morris["estimator"] == "salib-morris-mu-mu-star-sigma-v1"
    assert morris["sampleCalls"] == [
        {
            "N": 12,
            "local_optimization": True,
            "num_levels": 4,
            "optimal_trajectories": 3,
            "seed": (-7) % 2**64,
        }
    ]
    assert morris["analyzeOptions"] == {
        "conf_level": 0.95,
        "num_levels": 4,
        "num_resamples": 2,
        "print_to_console": False,
        "scaled": False,
        "seed": 0,
    }
    assert sobol["analyzeApi"] == "SALib.analyze.sobol.analyze"
    assert sobol["sampleCalls"] == [
        {
            "N": 16,
            "calc_second_order": True,
            "scramble": True,
            "seed": seed,
            "skip_values": 0,
        }
        for seed in (13, 17)
    ]
    assert sobol["analyzeOptions"] == {
        "calc_second_order": False,
        "conf_level": 0.95,
        "keep_resamples": False,
        "n_processors": None,
        "num_resamples": 2,
        "parallel": False,
        "print_to_console": False,
        "seed": 0,
    }
    assert sobol["uncertainty"] == "independent-scrambles-student-t-v1"


def test_morris_sample_is_deterministic_and_on_the_declared_grid() -> None:
    arguments = dict(
        names=("a", "b", "c"),
        candidate_trajectories=12,
        selected_trajectories=4,
        levels=4,
        seed=-7,
    )
    first = morris_unit_trajectories(**arguments)
    second = morris_unit_trajectories(**arguments)

    assert first == second
    assert len(first) == 4
    assert all(len(matrix) == 4 for matrix in first)
    assert all(value in range(4) for matrix in first for row in matrix for value in row)


def test_morris_candidate_pool_must_strictly_exceed_selected_trajectories() -> None:
    with pytest.raises(ValueError, match="strictly greater"):
        morris_unit_trajectories(
            ("a", "b"),
            candidate_trajectories=3,
            selected_trajectories=3,
            levels=4,
            seed=1,
        )


def test_sobol_sample_exposes_complete_saltelli_roles_deterministically() -> None:
    first = sobol_unit_positions(("a", "b"), 8, 17)
    second = sobol_unit_positions(("a", "b"), 8, 17)

    assert first == second
    assert len(first) == 8 * (2 * 2 + 2)
    assert [role for role, _ in first[:6]] == [
        "A:row:0",
        "AB:factor:a:row:0",
        "AB:factor:b:row:0",
        "BA:factor:a:row:0",
        "BA:factor:b:row:0",
        "B:row:0",
    ]


def test_sobol_sample_rejects_allocation_above_operational_cap() -> None:
    with pytest.raises(ValueError, match="operational position cap"):
        sobol_unit_positions(("a", "b"), 2**20, 17)


def test_sobol_indices_rank_an_additive_linear_function() -> None:
    names = ("a", "b")
    positions = dict(sobol_unit_positions(names, 1024, 21))

    def values(prefix: str, factor: str | None = None) -> np.ndarray:
        result = []
        for row in range(1024):
            role = (
                f"{prefix}:row:{row}"
                if factor is None
                else f"{prefix}:factor:{factor}:row:{row}"
            )
            point = positions[role]
            result.append(point[0] + 2 * point[1])
        return np.asarray(result)

    first, total = sobol_symmetric_indices(
        names,
        values("A"),
        values("B"),
        np.asarray([values("AB", factor) for factor in names]),
        np.asarray([values("BA", factor) for factor in names]),
        indicator="linear",
    )

    assert first == pytest.approx([0.2, 0.8], abs=0.02)
    assert total == pytest.approx([0.2, 0.8], abs=0.02)


def test_sobol_indices_are_invariant_for_low_amplitude_outputs() -> None:
    names = ("a", "b")
    positions = dict(sobol_unit_positions(names, 1024, 21))

    def values(prefix: str, factor: str | None = None) -> np.ndarray:
        roles = (
            f"{prefix}:row:{row}"
            if factor is None
            else f"{prefix}:factor:{factor}:row:{row}"
            for row in range(1024)
        )
        return np.asarray(
            [positions[role][0] + 2 * positions[role][1] for role in roles]
        )

    arrays = (
        values("A"),
        values("B"),
        np.asarray([values("AB", factor) for factor in names]),
        np.asarray([values("BA", factor) for factor in names]),
    )
    baseline = sobol_symmetric_indices(names, *arrays, indicator="linear")
    scaled = sobol_symmetric_indices(
        names, *(array * 1e-12 for array in arrays), indicator="low-amplitude"
    )

    assert scaled[0] == pytest.approx(baseline[0], abs=1e-12)
    assert scaled[1] == pytest.approx(baseline[1], abs=1e-12)


def test_sobol_indices_are_invariant_for_extreme_finite_hybrid_outputs() -> None:
    names = ("a",)
    arrays = (
        np.asarray([0.9, 1.0, 0.85, 0.95]),
        np.asarray([0.8, 0.9, 0.82, 0.88]),
        np.asarray([[-1.0, -0.9, -0.8, -0.7]]),
        np.asarray([[-0.8, -0.7, -0.6, -0.5]]),
    )

    baseline = sobol_symmetric_indices(names, *arrays, indicator="extreme")
    scaled = sobol_symmetric_indices(
        names, *(array * 1e308 for array in arrays), indicator="extreme"
    )

    assert scaled[0] == pytest.approx(baseline[0], rel=1e-14)
    assert scaled[1] == pytest.approx(baseline[1], rel=1e-14)


def test_sobol_indices_reject_when_scaling_erases_base_variance() -> None:
    adjacent = np.nextafter(1.0, 2.0)
    with pytest.raises(ValueError, match="scaling erased base variance"):
        sobol_symmetric_indices(
            ("a",),
            np.asarray([1.0, adjacent]),
            np.asarray([1.0, adjacent]),
            np.asarray([[-1e308, 1e308]]),
            np.asarray([[1e308, -1e308]]),
            indicator="dynamic-range",
        )


def test_sobol_symmetric_average_does_not_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "eqasim_sa.core.sensitivity.salib_sobol_analyze.analyze",
        lambda *args, **kwargs: {"S1": np.asarray([1e308]), "ST": np.asarray([1e308])},
    )

    first, total = sobol_symmetric_indices(
        ("a",),
        np.asarray([0.0, 1.0]),
        np.asarray([1.0, 0.0]),
        np.asarray([[1.0, 0.0]]),
        np.asarray([[0.0, 1.0]]),
        indicator="large-index",
    )

    assert first.tolist() == [1e308]
    assert total.tolist() == [1e308]


def test_sobol_symmetric_average_preserves_subnormal_indices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smallest = np.nextafter(0.0, 1.0)
    monkeypatch.setattr(
        "eqasim_sa.core.sensitivity.salib_sobol_analyze.analyze",
        lambda *args, **kwargs: {
            "S1": np.asarray([smallest]),
            "ST": np.asarray([smallest]),
        },
    )

    first, total = sobol_symmetric_indices(
        ("a",),
        np.asarray([0.0, 1.0]),
        np.asarray([1.0, 0.0]),
        np.asarray([[1.0, 0.0]]),
        np.asarray([[0.0, 1.0]]),
        indicator="subnormal-index",
    )

    assert first.tolist() == [smallest]
    assert total.tolist() == [smallest]


def test_sobol_indices_match_ishigami_references() -> None:
    names = ("x1", "x2", "x3")
    size = 8192
    positions = dict(sobol_unit_positions(names, size, 20260824))

    def values(prefix: str, factor: str | None = None) -> np.ndarray:
        result = []
        for row in range(size):
            role = (
                f"{prefix}:row:{row}"
                if factor is None
                else f"{prefix}:factor:{factor}:row:{row}"
            )
            x1, x2, x3 = (2 * np.pi * value - np.pi for value in positions[role])
            result.append(np.sin(x1) + 7 * np.sin(x2) ** 2 + 0.1 * x3**4 * np.sin(x1))
        return np.asarray(result)

    first, total = sobol_symmetric_indices(
        names,
        values("A"),
        values("B"),
        np.asarray([values("AB", factor) for factor in names]),
        np.asarray([values("BA", factor) for factor in names]),
        indicator="Ishigami",
    )

    assert first == pytest.approx([0.3139, 0.4424, 0.0], abs=0.02)
    assert total == pytest.approx([0.5576, 0.4424, 0.2437], abs=0.02)


def test_morris_statistics_rank_a_monotone_linear_function() -> None:
    names = ("a", "b")
    matrices = morris_unit_trajectories(
        names,
        candidate_trajectories=16,
        selected_trajectories=8,
        levels=4,
        seed=11,
    )
    inputs = np.asarray(matrices, dtype=np.float64).reshape(-1, 2) / 3
    outputs = inputs[:, 0] + 2 * inputs[:, 1]

    result = morris_statistics(names, inputs, outputs, levels=4)

    assert result["a"][:2] == pytest.approx((1.0, 1.0))
    assert result["b"][:2] == pytest.approx((2.0, 2.0))
    assert result["a"][2] == pytest.approx(0.0, abs=1e-12)
    assert result["b"][2] == pytest.approx(0.0, abs=1e-12)
