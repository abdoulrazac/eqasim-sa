import hashlib
import json
import math
import multiprocessing
import os
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from scipy.stats import sobol_indices

from eqasim_sa.adapters.application import analyze_from_meta_config
from eqasim_sa.adapters.fake.simulator import FakeSimulator
from eqasim_sa.cli import dispatch
from eqasim_sa.core.analyze import (
    _morris_ranking,
    _sobol_bootstrap,
    _sobol_estimates,
    _sobol_ranking,
    analyze,
)
from eqasim_sa.core.indicators import ArtifactSet, RegisteredIndicator
from eqasim_sa.core.ledger import RunRecord, write_parquet_atomic, write_run
from eqasim_sa.core.plan import MorrisDesign, SobolDesign, build_plan, load_run_specs
from eqasim_sa.core.run import execute_run


def _artifact_value(artifacts: ArtifactSet) -> float:
    return float(pq.read_table(artifacts.path("age"))["synthetic"][0].as_py())


def _custom_indicator() -> RegisteredIndicator:
    return RegisteredIndicator("custom", "age", _artifact_value)


def _constant_indicator(artifacts: ArtifactSet) -> float:
    return 1.0


def _analyze(root: Path, indicators=(), **changes):  # type: ignore[no-untyped-def]
    options = {"campaign": "test", "completeness_threshold": 0.0, **changes}
    return analyze(root, indicators, **options)


def _mutating_indicator(campaign: Path):  # type: ignore[no-untyped-def]
    def mutate(artifacts: ArtifactSet) -> float:
        assert artifacts.path("age").stat().st_mode & 0o222 == 0
        assert campaign not in artifacts.path("age").parents
        (campaign / "plan.parquet").write_bytes(b"damaged plan")
        (campaign / "runs" / ("f" * 32 + ".parquet")).write_bytes(b"foreign")
        (campaign / "indicators.parquet").write_bytes(b"damaged previous")
        return 1.0

    return RegisteredIndicator("mutator", "age", mutate)


class ForbiddenSimulator:
    def simulate(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("analyze must not invoke a simulator")


def _campaign(tmp_path: Path) -> tuple[Path, tuple[str, str]]:
    root = tmp_path / "campaign"
    run_ids = ("1" * 32, "2" * 32)
    write_parquet_atomic(
        pa.table(
            {
                "designId": ["d" * 32, "e" * 32, "f" * 32],
                "method": ["grid"] * 3,
                "role": ["point"] * 3,
                "runId": [run_ids[1], run_ids[0], "3" * 32],
                "seed": [1] * 3,
                "contextHash": ["hash"] * 3,
            }
        ),
        root / "plan.parquet",
    )
    for run_id, synthetic in zip(run_ids, (2.0, 4.0), strict=True):
        write_parquet_atomic(
            pa.table(
                {
                    "class": ["x", "y"],
                    "synthetic": [synthetic, 1.0],
                    "reference": [1.0, 1.0],
                }
            ),
            root / "archives" / run_id / "age.parquet",
        )
        write_run(root, RunRecord(run_id, "test", 1, "hash", "node", 1.0, {}))
    write_parquet_atomic(
        pa.table({"class": ["x"], "synthetic": [99.0], "reference": [1.0]}),
        root / "archives" / ("3" * 32) / "age.parquet",
    )
    return root, run_ids


def _morris_campaign(tmp_path: Path, seeds: tuple[int, ...] = (11, 12)) -> Path:
    root = tmp_path / "morris"
    descriptor = {
        "eqasimCommit": "a",
        "dataManifestSha256": "b",
        "lockfileSha256": "c",
        "eqasimSaVersion": "0.1.0",
    }
    context_hash = hashlib.sha256(
        b"eqasim-sa:context:v1\0"
        + json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    design = MorrisDesign(
        {"config": {"a": 0, "b": -1.0}},
        {"b": [-1.0, 1.0], "a": [0, 2, 5]},
        seeds,
        {},
        context_hash,
        descriptor,
        root,
        trajectories=2,
        design_seed=20260814,
    )
    build_plan(design)
    unique = {}
    for row in pq.read_table(root / "plan.parquet").to_pylist():
        unique.setdefault(row["runId"], row)
    for run_id, row in unique.items():
        coefficient = 2.0 if row["seed"] == seeds[0] else 4.0
        value = coefficient * row["a"] - 3.0 * row["b"]
        write_parquet_atomic(
            pa.table({"synthetic": [value]}), root / "archives" / run_id / "age.parquet"
        )
        write_run(
            root,
            RunRecord(
                run_id,
                "test",
                row["seed"],
                context_hash,
                "node",
                1.0,
                {"a": row["a"], "b": row["b"]},
            ),
        )
    return root


def _sobol_campaign(tmp_path: Path, n: int = 8) -> Path:
    root = tmp_path / "sobol"
    descriptor = {
        "eqasimCommit": "a",
        "dataManifestSha256": "b",
        "lockfileSha256": "c",
        "eqasimSaVersion": "0.1.0",
    }
    context_hash = hashlib.sha256(
        b"eqasim-sa:context:v1\0"
        + json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    design = SobolDesign(
        {"config": {"a": 0}},
        {"a": [0, 1, 2]},
        (11, 12, 13),
        {},
        context_hash,
        descriptor,
        root,
        base_sample_size=n,
        design_seed=20260814,
    )
    build_plan(design)
    unique = {}
    for row in pq.read_table(root / "plan.parquet").to_pylist():
        unique.setdefault(row["runId"], row)
    for run_id, row in unique.items():
        value = float(row["a"] + row["seed"] / 10 + row["a"] * row["seed"] / 20)
        write_parquet_atomic(
            pa.table({"synthetic": [value]}), root / "archives" / run_id / "age.parquet"
        )
        write_run(
            root,
            RunRecord(
                run_id, "test", row["seed"], context_hash, "node", 1.0, {"a": row["a"]}
            ),
        )
    return root


def test_sobol_symmetric_estimators_equal_formulas_and_two_scipy_orientations() -> None:
    f_a = np.array([0.2, 1.1, -0.4, 2.0, 0.7, -1.2, 1.8, 0.1])
    f_b = np.array([1.3, -0.2, 0.5, 1.7, -0.8, 0.9, 2.1, -0.5])
    f_ab = np.array(
        [
            [0.8, 0.4, -0.1, 1.8, 0.2, -0.4, 2.0, -0.2],
            [1.0, 0.0, 0.3, 1.9, -0.3, 0.5, 1.9, -0.1],
        ]
    )
    f_ba = np.array(
        [
            [0.7, 0.6, 0.1, 1.6, -0.1, 0.2, 1.7, -0.3],
            [0.4, 0.9, -0.2, 1.5, 0.4, -0.1, 2.2, 0.0],
        ]
    )
    first, total = _sobol_estimates(f_a, f_b, f_ab, f_ba, "value")
    forward = sobol_indices(
        func={"f_A": f_a[None, :], "f_B": f_b[None, :], "f_AB": f_ab[:, None, :]}, n=8
    )
    reverse = sobol_indices(
        func={"f_A": f_b[None, :], "f_B": f_a[None, :], "f_AB": f_ba[:, None, :]}, n=8
    )
    assert first == pytest.approx((forward.first_order + reverse.first_order) / 2)
    assert total == pytest.approx((forward.total_order + reverse.total_order) / 2)


@pytest.mark.parametrize(
    "function,expected_first,expected_total",
    [
        (lambda values: values[:, 0] + values[:, 1], (0.5, 0.5), (0.5, 0.5)),
        (lambda values: values[:, 0] * values[:, 1], (3 / 7, 3 / 7), (4 / 7, 4 / 7)),
    ],
)
def test_sobol_estimators_match_independent_additive_and_interaction_analytics(
    function,
    expected_first: tuple[float, float],
    expected_total: tuple[float, float],  # type: ignore[no-untyped-def]
) -> None:
    sample = (
        __import__("scipy")
        .stats.qmc.Sobol(d=4, scramble=True, bits=64, rng=12345, optimization=None)
        .random_base2(m=14)
    )
    a, b = sample[:, :2], sample[:, 2:]
    ab, ba = [], []
    for index in range(2):
        forward, reverse = a.copy(), b.copy()
        forward[:, index], reverse[:, index] = b[:, index], a[:, index]
        ab.append(function(forward))
        ba.append(function(reverse))
    first, total = _sobol_estimates(
        function(a), function(b), np.array(ab), np.array(ba), "analytic"
    )
    assert first == pytest.approx(expected_first, abs=2e-4)
    assert total == pytest.approx(expected_total, abs=2e-4)


def test_sobol_bootstrap_matches_paired_seeded_percentile_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    f_a = np.array([0.0, 1.0, 2.0, 4.0])
    f_b = np.array([1.0, 3.0, 2.0, 5.0])
    f_ab = np.array([[0.5, 2.0, 2.5, 4.5], [1.0, 1.5, 3.0, 4.0]])
    f_ba = np.array([[0.2, 2.5, 2.2, 4.8], [0.8, 2.8, 2.7, 4.2]])
    design, indicator, count, confidence = b'{"sealed":true}', "indicator", 7, 0.8
    expected_seed = int.from_bytes(
        hashlib.sha256(
            b"eqasim-sa:bootstrap:sobol:v1\0" + design + b"\0" + indicator.encode()
        ).digest()[:16],
        "big",
    )
    real_rng, draws = np.random.default_rng(expected_seed), []

    class TrackingRng:
        def integers(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            value = real_rng.integers(*args, **kwargs)
            draws.append(value.copy())
            return value

    monkeypatch.setattr(
        "eqasim_sa.core.analyze.np.random.default_rng",
        lambda seed: (
            TrackingRng()
            if seed == expected_seed
            else (_ for _ in ()).throw(AssertionError(f"unexpected seed {seed}"))
        ),
    )
    first_ci, total_ci, attempts = _sobol_bootstrap(
        f_a, f_b, f_ab, f_ba, count, confidence, design, indicator
    )

    def reference(indices):  # type: ignore[no-untyped-def]
        values = [array[indices] for array in (f_a, f_b)]
        mean = np.mean(np.concatenate(values))
        a, b = values[0] - mean, values[1] - mean
        ab, ba = f_ab[:, indices] - mean, f_ba[:, indices] - mean
        variance = np.mean(np.concatenate((a, b)) ** 2)
        first = (np.mean(b * (ab - a), axis=1) + np.mean(a * (ba - b), axis=1)) / (
            2 * variance
        )
        total = (np.mean((a - ab) ** 2, axis=1) + np.mean((b - ba) ** 2, axis=1)) / (
            4 * variance
        )
        return first, total

    reference_values = [reference(indices) for indices in draws]
    alpha = 1 - confidence
    assert attempts == count == len(draws)
    assert first_ci == pytest.approx(
        np.quantile(
            [value[0] for value in reference_values], (alpha / 2, 1 - alpha / 2), axis=0
        )
    )
    assert total_ci == pytest.approx(
        np.quantile(
            [value[1] for value in reference_values], (alpha / 2, 1 - alpha / 2), axis=0
        )
    )


def test_sobol_analysis_is_deterministic_permutation_invariant_and_has_exact_schema(
    tmp_path: Path,
) -> None:
    root = _sobol_campaign(tmp_path)
    first = _analyze(
        root,
        (_custom_indicator(),),
        completeness_threshold=1.0,
        seed_role="factor",
        method="sobol",
        bootstrap_resamples=31,
        confidence_level=0.9,
    )
    ranking = root / "sobol-indices.parquet"
    expected_schema = pa.schema(
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
    assert first.sensitivity_method == "sobol" and first.sensitivity_path == ranking
    assert pq.ParquetFile(ranking).schema_arrow == expected_schema
    rows = pq.read_table(ranking).to_pylist()
    assert {row["factor"] for row in rows} == {"a", "seed"}
    assert all(
        row["bootstrap_resamples"] == 31 and row["bootstrap_attempts"] >= 31
        for row in rows
    )
    expected = ranking.read_bytes()
    plan = pq.read_table(root / "plan.parquet")
    write_parquet_atomic(
        plan.take(pa.array(list(reversed(range(len(plan)))))), root / "plan.parquet"
    )
    replay = _analyze(
        root,
        (_custom_indicator(),),
        completeness_threshold=1.0,
        seed_role="factor",
        method="sobol",
        bootstrap_resamples=31,
        confidence_level=0.9,
    )
    assert replay.sensitivity_path.read_bytes() == expected


@pytest.mark.parametrize("damage", ["role", "design", "value", "run", "metadata"])
def test_sobol_sealed_validation_rejects_tampering_and_preserves_outputs(
    tmp_path: Path,
    damage: str,
) -> None:
    root = _sobol_campaign(tmp_path)
    outputs = {
        root / "indicators.parquet": b"old indicators",
        root / "morris-ranking.parquet": b"old morris",
        root / "sobol-indices.parquet": b"old sobol",
        root / "analysis-generation.json": b"old manifest",
    }
    for path, value in outputs.items():
        path.write_bytes(value)
    table = pq.read_table(root / "plan.parquet")
    rows, metadata = table.to_pylist(), dict(table.schema.metadata)
    if damage == "role":
        rows[0]["role"] = "A:row:99"
    elif damage == "design":
        rows[0]["designId"] = "f" * 32
    elif damage == "value":
        rows[0]["a"] = 99
    elif damage == "run":
        rows[1]["runId"] = rows[0]["runId"]
    else:
        metadata[b"sobolDesignVersion"] = b"2"
    write_parquet_atomic(
        pa.Table.from_pylist(rows, schema=table.schema.with_metadata(metadata)),
        root / "plan.parquet",
    )
    with pytest.raises(ValueError, match="Sobol|role|design|metadata|runId|sealed"):
        _analyze(
            root,
            (_custom_indicator(),),
            completeness_threshold=1.0,
            seed_role="factor",
            method="sobol",
            bootstrap_resamples=7,
            confidence_level=0.9,
        )
    assert {path: path.read_bytes() for path in outputs} == outputs


def test_sobol_sealed_cardinality_is_rejected_before_qmc_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import eqasim_sa.core.analyze as analyze_module

    root = _sobol_campaign(tmp_path)
    table = pq.read_table(root / "plan.parquet")
    metadata = dict(table.schema.metadata)
    design = json.loads(metadata[b"sobolDesign"])
    design["baseSampleSize"] = 2**61
    metadata[b"sobolDesign"] = json.dumps(
        design, sort_keys=True, separators=(",", ":")
    ).encode()
    table = table.replace_schema_metadata(metadata)
    monkeypatch.setattr(
        analyze_module.qmc,
        "Sobol",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("QMC allocated")),
    )
    with pytest.raises(ValueError, match=r"N\(2D\+2\).*int64"):
        _sobol_ranking(table, [], 3, 0.95)


@pytest.mark.parametrize(
    "damage",
    [
        "fixed-type",
        "fixed-nullable",
        "factor-type",
        "unsafe-name",
        "duplicate-name",
        "bool-domain",
        "mixed-domain",
        "nonfinite-domain",
        "int64-domain",
        "context-missing",
        "context-extra",
    ],
)
def test_sobol_sealed_contract_rejects_schema_domain_name_and_context_damage_before_qmc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    import eqasim_sa.core.analyze as analyze_module

    root = _sobol_campaign(tmp_path)
    table = pq.read_table(root / "plan.parquet")
    metadata = dict(table.schema.metadata)
    arrays, fields = list(table.columns), list(table.schema)
    design = json.loads(metadata[b"sobolDesign"])
    descriptor = json.loads(metadata[b"contextDescriptor"])
    if damage == "fixed-type":
        index = table.schema.get_field_index("designId")
        arrays[index] = pa.array(range(len(table)), type=pa.int64())
        fields[index] = pa.field("designId", pa.int64(), False)
    elif damage == "fixed-nullable":
        index = table.schema.get_field_index("role")
        fields[index] = pa.field("role", pa.string(), True)
    elif damage == "factor-type":
        index = table.schema.get_field_index("a")
        arrays[index] = table["a"].cast(pa.float64())
        fields[index] = pa.field("a", pa.float64(), False)
    elif damage == "unsafe-name":
        design["factors"][0]["name"] = "bad-name"
    elif damage == "duplicate-name":
        design["factors"][1]["name"] = design["factors"][0]["name"]
    elif damage == "bool-domain":
        design["factors"][0]["values"] = [False, True]
    elif damage == "mixed-domain":
        design["factors"][0]["values"] = [0, "one"]
    elif damage == "nonfinite-domain":
        design["factors"][0]["values"] = [0.0, float("inf")]
    elif damage == "int64-domain":
        design["factors"][0]["values"] = [0, 2**63]
    elif damage == "context-missing":
        descriptor.pop("lockfileSha256")
    else:
        descriptor["extra"] = "x"
    metadata[b"sobolDesign"] = json.dumps(
        design, sort_keys=True, separators=(",", ":")
    ).encode()
    metadata[b"contextDescriptor"] = json.dumps(
        descriptor, sort_keys=True, separators=(",", ":")
    ).encode()
    damaged = pa.Table.from_arrays(arrays, schema=pa.schema(fields, metadata=metadata))
    monkeypatch.setattr(
        analyze_module.qmc,
        "Sobol",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("QMC allocated")),
    )
    with pytest.raises((TypeError, ValueError), match="Sobol"):
        _sobol_ranking(damaged, [], 3, 0.95)


def test_sobol_constant_output_is_named_and_transaction_restores_stale_views(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _sobol_campaign(tmp_path)
    stale = root / "morris-ranking.parquet"
    stale.write_bytes(b"old morris")
    manifest = root / "analysis-generation.json"
    manifest.write_bytes(b"old manifest")
    with pytest.raises(ValueError, match="constant.*variance"):
        _analyze(
            root,
            (RegisteredIndicator("constant", "age", _constant_indicator),),
            completeness_threshold=1.0,
            seed_role="factor",
            method="sobol",
            bootstrap_resamples=7,
            confidence_level=0.9,
        )
    assert (
        stale.read_bytes() == b"old morris" and manifest.read_bytes() == b"old manifest"
    )


def test_sobol_bootstrap_refuses_excessive_degeneracy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DegenerateRng:
        def integers(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return np.zeros(4, dtype=int)

    monkeypatch.setattr(
        "eqasim_sa.core.analyze.np.random.default_rng", lambda seed: DegenerateRng()
    )
    with pytest.raises(ValueError, match="degenerate.*bootstrap"):
        _sobol_bootstrap(
            np.array([0.0, 1.0, 2.0, 3.0]),
            np.array([0.0, 2.0, 3.0, 4.0]),
            np.array([[1.0, 1.5, 2.5, 3.5]]),
            np.array([[0.5, 1.5, 2.5, 3.5]]),
            3,
            0.95,
            b"design",
            "degenerate",
        )


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    paths = [root / "plan.parquet"]
    paths.extend(sorted((root / "runs").glob("*")))
    paths.extend(sorted((root / "archives").glob("*/*")))
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in paths
    }


def test_analyze_rebuilds_long_zstd_view_for_complete_runs_only(tmp_path: Path) -> None:
    root, run_ids = _campaign(tmp_path)
    before = _snapshot(root)

    result = _analyze(root, (_custom_indicator(),))

    assert result.path == root / "indicators.parquet"
    assert result.runs == 2
    assert result.indicators == 1
    parquet = pq.ParquetFile(result.path)
    assert parquet.schema_arrow == pa.schema(
        [("runId", pa.string()), ("indicator", pa.string()), ("value", pa.float64())]
    )
    assert parquet.read().to_pylist() == [
        {"runId": run_ids[0], "indicator": "custom(age)", "value": 2.0},
        {"runId": run_ids[1], "indicator": "custom(age)", "value": 4.0},
    ]
    assert {
        parquet.metadata.row_group(group).column(column).compression
        for group in range(parquet.metadata.num_row_groups)
        for column in range(parquet.metadata.row_group(group).num_columns)
    } == {"ZSTD"}
    assert _snapshot(root) == before

    first = result.path.read_bytes()
    result.path.unlink()
    rebuilt = _analyze(root, (_custom_indicator(),))
    assert rebuilt.path.read_bytes() == first
    replayed = _analyze(root, (_custom_indicator(),))
    assert replayed.path.read_bytes() == first
    assert _snapshot(root) == before


def test_analyze_accepts_partial_campaign_and_empty_result(tmp_path: Path) -> None:
    root, run_ids = _campaign(tmp_path)
    (root / "runs" / f"{run_ids[1]}.parquet").unlink()
    assert _analyze(root, (_custom_indicator(),)).runs == 1
    (root / "runs" / f"{run_ids[0]}.parquet").unlink()
    result = _analyze(root, (_custom_indicator(),))
    assert result.runs == 0
    assert pq.read_table(result.path).num_rows == 0


def test_analyze_writes_replication_dispersion_for_partial_groups(
    tmp_path: Path,
) -> None:
    root = tmp_path / "campaign"
    run_ids = tuple(str(index) * 32 for index in range(1, 5))
    write_parquet_atomic(
        pa.table(
            {
                "designId": [chr(96 + index) * 32 for index in range(1, 6)],
                "method": ["grid"] * 5,
                "role": ["point"] * 5,
                "runId": [*run_ids, run_ids[0]],
                "seed": [11, 12, 13, 11, 11],
                "contextHash": ["hash"] * 5,
                "rate": [0.1, 0.1, 0.1, 0.2, 0.1],
            }
        ),
        root / "plan.parquet",
    )
    for run_id, seed, rate, value in zip(
        run_ids,
        (11, 12, 13, 11),
        (0.1, 0.1, 0.1, 0.2),
        (-1.0, 1.0, 99.0, 7.0),
        strict=True,
    ):
        write_parquet_atomic(
            pa.table({"class": ["x"], "synthetic": [value], "reference": [1.0]}),
            root / "archives" / run_id / "age.parquet",
        )
        if seed != 13:
            write_run(
                root,
                RunRecord(run_id, "test", seed, "hash", "node", 1.0, {"rate": rate}),
            )
    before = _snapshot(root)

    result = _analyze(root, (_custom_indicator(),))

    dispersion = root / "replication-dispersion.parquet"
    assert result.dispersion_path == dispersion
    schema = pq.ParquetFile(dispersion).schema_arrow
    assert schema.names == [
        "rate",
        "indicator",
        "n",
        "mean",
        "std",
        "cv",
        "ci95_low",
        "ci95_high",
    ]
    assert [field.nullable for field in schema] == [
        True,
        False,
        False,
        False,
        True,
        True,
        True,
        True,
    ]
    parquet = pq.ParquetFile(dispersion)
    assert {
        parquet.metadata.row_group(group).column(column).compression
        for group in range(parquet.metadata.num_row_groups)
        for column in range(parquet.metadata.row_group(group).num_columns)
    } == {"ZSTD"}
    rows = pq.read_table(dispersion).to_pylist()
    assert rows[0]["rate"] == 0.1 and rows[0]["n"] == 2
    assert rows[0]["mean"] == 0.0 and rows[0]["cv"] is None
    assert rows[0]["std"] == math.sqrt(2.0)
    margin = 1.96 * rows[0]["std"] / math.sqrt(2)
    assert rows[0]["ci95_low"] == -margin and rows[0]["ci95_high"] == margin
    assert rows[1] == {
        "rate": 0.2,
        "indicator": "custom(age)",
        "n": 1,
        "mean": 7.0,
        "std": None,
        "cv": None,
        "ci95_low": None,
        "ci95_high": None,
    }
    assert _snapshot(root) == before
    first = (result.path.read_bytes(), dispersion.read_bytes())
    replayed = _analyze(root, (_custom_indicator(),))
    assert (replayed.path.read_bytes(), dispersion.read_bytes()) == first


def test_morris_ranking_has_exact_effects_schema_order_and_manifest(
    tmp_path: Path,
) -> None:
    root = _morris_campaign(tmp_path)
    stale_sobol = root / "sobol-indices.parquet"
    stale_sobol.write_bytes(b"stale sobol")

    result = _analyze(
        root, (_custom_indicator(),), completeness_threshold=1.0, method="morris"
    )

    ranking = root / "morris-ranking.parquet"
    assert result.sensitivity_method == "morris"
    assert result.sensitivity_path == ranking
    schema = pq.ParquetFile(ranking).schema_arrow
    assert schema == pa.schema(
        [
            pa.field("indicator", pa.string(), nullable=False),
            pa.field("factor", pa.string(), nullable=False),
            pa.field("rank", pa.int64(), nullable=False),
            pa.field("trajectories", pa.int64(), nullable=False),
            pa.field("replications", pa.int64(), nullable=False),
            pa.field("n", pa.int64(), nullable=False),
            pa.field("mu", pa.float64(), nullable=False),
            pa.field("mu_star", pa.float64(), nullable=False),
            pa.field("sigma", pa.float64()),
            pa.field("seed_std", pa.float64()),
        ]
    )
    rows = pq.read_table(ranking).to_pylist()
    assert [row["factor"] for row in rows] == ["a", "b"]
    assert rows[0] == pytest.approx(
        {
            "indicator": "custom(age)",
            "factor": "a",
            "rank": 1,
            "trajectories": 2,
            "replications": 2,
            "n": 4,
            "mu": 15.0,
            "mu_star": 15.0,
            "sigma": math.sqrt(100 / 3),
            "seed_std": math.sqrt(50),
        }
    )
    assert rows[1] == pytest.approx(
        {
            "indicator": "custom(age)",
            "factor": "b",
            "rank": 2,
            "trajectories": 2,
            "replications": 2,
            "n": 4,
            "mu": -6.0,
            "mu_star": 6.0,
            "sigma": 0.0,
            "seed_std": 0.0,
        }
    )
    manifest = json.loads((root / "analysis-generation.json").read_text())
    assert manifest["version"] == 2
    assert manifest["sensitivityMethod"] == "morris"
    assert (
        manifest["sensitivitySha256"]
        == __import__("hashlib").sha256(ranking.read_bytes()).hexdigest()
    )
    assert not stale_sobol.exists()


@pytest.mark.parametrize(
    "damage",
    [
        "metadata-extra",
        "metadata-version",
        "factor-order",
        "mixed-domain",
        "context",
        "descriptor",
        "design-format",
        "design-duplicate",
        "too-many",
    ],
)
def test_morris_ranking_rejects_incoherent_sealed_plan(
    tmp_path: Path, damage: str
) -> None:
    root = _morris_campaign(tmp_path)
    table = pq.read_table(root / "plan.parquet")
    rows = table.to_pylist()
    metadata = dict(table.schema.metadata)
    design = json.loads(metadata[b"morrisDesign"])
    if damage == "metadata-extra":
        metadata[b"extra"] = b"1"
    elif damage == "metadata-version":
        metadata[b"planSchemaVersion"] = b"2"
    elif damage == "factor-order":
        design["factors"].reverse()
    elif damage == "mixed-domain":
        design["factors"][0]["values"][1] = 2.0
    elif damage == "context":
        rows[0]["contextHash"] = "f" * 64
    elif damage == "descriptor":
        metadata[b"contextDescriptor"] = b"{}"
    elif damage == "design-format":
        rows[0]["designId"] = "bad"
    elif damage == "design-duplicate":
        rows[1]["designId"] = rows[0]["designId"]
    elif damage == "too-many":
        design["trajectories"] = 10_000
    if damage in {"factor-order", "mixed-domain", "too-many"}:
        metadata[b"morrisDesign"] = json.dumps(
            design, sort_keys=True, separators=(",", ":")
        ).encode()
    table = pa.Table.from_pylist(rows, schema=table.schema.with_metadata(metadata))
    values = {}
    for row in rows:
        values.setdefault(row["runId"], float(row["a"]) + float(row["b"]))
    indicator_rows = [(run_id, "value", value) for run_id, value in values.items()]

    with pytest.raises(
        ValueError, match="Morris|metadata|context|design|factor|traject"
    ):
        _morris_ranking(table, indicator_rows)


def test_morris_ranking_is_independent_of_plan_and_run_order(tmp_path: Path) -> None:
    root = _morris_campaign(tmp_path)
    first = _analyze(
        root, (_custom_indicator(),), completeness_threshold=1.0, method="morris"
    )
    expected = first.sensitivity_path.read_bytes()
    plan = pq.read_table(root / "plan.parquet")
    shuffled = plan.take(pa.array(list(reversed(range(len(plan))))))
    write_parquet_atomic(shuffled, root / "plan.parquet")

    replay = _analyze(
        root, (_custom_indicator(),), completeness_threshold=1.0, method="morris"
    )

    assert replay.sensitivity_path.read_bytes() == expected


def test_morris_single_replication_uses_null_sample_statistics(tmp_path: Path) -> None:
    root = _morris_campaign(tmp_path, (11,))
    result = _analyze(
        root, (_custom_indicator(),), completeness_threshold=1.0, method="morris"
    )
    rows = pq.read_table(result.sensitivity_path).to_pylist()
    assert all(row["n"] == 2 and row["sigma"] == 0.0 for row in rows)
    assert all(row["seed_std"] is None for row in rows)


@pytest.mark.parametrize("damage", ["role", "rank", "domain", "run"])
def test_morris_invalid_structure_preserves_all_previous_outputs(
    tmp_path: Path,
    damage: str,
) -> None:
    root = _morris_campaign(tmp_path)
    outputs = {
        root / "indicators.parquet": b"old indicators",
        root / "replication-dispersion.parquet": b"old dispersion",
        root / "morris-ranking.parquet": b"old ranking",
        root / "analysis-generation.json": b"old manifest",
    }
    for path, content in outputs.items():
        path.write_bytes(content)
    plan = pq.read_table(root / "plan.parquet")
    rows = plan.to_pylist()
    if damage == "role":
        rows[0]["role"] = "trajectory:bad"
    elif damage == "rank":
        rows[1]["role"] = rows[0]["role"]
    elif damage == "domain":
        rows[1]["a"] = 99
    else:
        rows[0]["runId"] = "f" * 32
    write_parquet_atomic(
        pa.Table.from_pylist(rows, schema=plan.schema), root / "plan.parquet"
    )

    with pytest.raises(
        ValueError, match="Morris|role|rank|domain|runId|indicator|complet|marker"
    ):
        _analyze(
            root, (_custom_indicator(),), completeness_threshold=1.0, method="morris"
        )
    assert {path: path.read_bytes() for path in outputs} == outputs


def test_analysis_refuses_grid_morris_method_mismatch_before_publication(
    tmp_path: Path,
) -> None:
    root, _ = _campaign(tmp_path)
    with pytest.raises(ValueError, match="grid.*morris"):
        _analyze(root, (_custom_indicator(),), method="morris")
    assert not (root / "indicators.parquet").exists()

    morris = _morris_campaign(tmp_path)
    with pytest.raises(ValueError, match="morris.*grid"):
        _analyze(
            morris, (_custom_indicator(),), completeness_threshold=1.0, method="grid"
        )
    assert not (morris / "indicators.parquet").exists()


@pytest.mark.parametrize(
    "indicators,seed_role,message",
    [
        ((), "replication", "indicator"),
        ((_custom_indicator(),), "factor", "replication"),
    ],
)
def test_morris_analysis_requires_indicator_and_replication_seed_role(
    tmp_path: Path,
    indicators,
    seed_role: str,
    message: str,  # type: ignore[no-untyped-def]
) -> None:
    root = _morris_campaign(tmp_path)
    with pytest.raises(ValueError, match=message):
        _analyze(
            root,
            indicators,
            completeness_threshold=1.0,
            method="morris",
            seed_role=seed_role,
        )
    assert not (root / "indicators.parquet").exists()


def test_morris_restores_all_views_if_ranking_publication_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import eqasim_sa.core.analyze as analyze_module

    root = _morris_campaign(tmp_path)
    outputs = {
        root / "indicators.parquet": b"old indicators",
        root / "replication-dispersion.parquet": b"old dispersion",
        root / "morris-ranking.parquet": b"old ranking",
        root / "analysis-generation.json": b"old manifest",
    }
    for path, content in outputs.items():
        path.write_bytes(content)
    real_write = analyze_module.write_parquet_atomic

    def fail_ranking(table: pa.Table, destination: Path) -> Path:
        if destination.name == "morris-ranking.parquet":
            raise OSError("injected ranking publication failure")
        return real_write(table, destination)

    monkeypatch.setattr(analyze_module, "write_parquet_atomic", fail_ranking)
    with pytest.raises(OSError, match="injected ranking"):
        _analyze(
            root, (_custom_indicator(),), completeness_threshold=1.0, method="morris"
        )
    assert {path: path.read_bytes() for path in outputs} == outputs


def test_grid_analysis_transactionally_removes_stale_morris_ranking(
    tmp_path: Path,
) -> None:
    root, _ = _campaign(tmp_path)
    stale = root / "morris-ranking.parquet"
    stale.write_bytes(b"stale ranking")
    stale_sobol = root / "sobol-indices.parquet"
    stale_sobol.write_bytes(b"stale sobol")

    _analyze(root, (_custom_indicator(),))

    assert not stale.exists()
    assert not stale_sobol.exists()
    manifest = json.loads((root / "analysis-generation.json").read_text())
    assert manifest["sensitivityMethod"] is None
    assert manifest["sensitivitySha256"] is None


def test_grid_restores_stale_ranking_if_manifest_publication_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import eqasim_sa.core.analyze as analyze_module

    root, _ = _campaign(tmp_path)
    stale = root / "morris-ranking.parquet"
    stale.write_bytes(b"stale ranking")
    monkeypatch.setattr(
        analyze_module,
        "_write_atomic",
        lambda *args: (_ for _ in ()).throw(OSError("injected manifest failure")),
    )

    with pytest.raises(OSError, match="manifest"):
        _analyze(root, (_custom_indicator(),))
    assert stale.read_bytes() == b"stale ranking"


def test_morris_fake_pipeline_end_to_end_deduplicates_runs_and_ranks(
    tmp_path: Path,
) -> None:
    root, workdir = tmp_path / "campaign", tmp_path / "workdir"
    workdir.mkdir()
    descriptor = {
        "eqasimCommit": "a",
        "dataManifestSha256": "b",
        "lockfileSha256": "c",
        "eqasimSaVersion": "0.1.0",
    }
    context_hash = hashlib.sha256(
        b"eqasim-sa:context:v1\0"
        + json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    design = MorrisDesign(
        {"config": {"a": 0}},
        {"a": [0, 1]},
        (7,),
        {"run": ["population"]},
        context_hash,
        descriptor,
        root,
        trajectories=2,
        design_seed=7,
    )
    planned = build_plan(design)
    specs = load_run_specs(root / "plan.parquet", design, "test")

    results = [execute_run(spec, FakeSimulator(), root, workdir) for spec in specs]
    indicator = RegisteredIndicator(
        "value",
        "population",
        lambda artifacts: float(
            pq.read_table(artifacts.path("population"))["value"][0].as_py()
        ),
    )
    analyzed = _analyze(root, (indicator,), completeness_threshold=1.0, method="morris")

    assert planned.design_positions == 4 and planned.unique_runs == len(specs) == 2
    assert all(result.status == "completed" for result in results)
    ranking = pq.read_table(analyzed.sensitivity_path).to_pylist()
    assert len(ranking) == 1 and ranking[0]["factor"] == "a" and ranking[0]["n"] == 2


def test_analyze_restores_both_derived_views_if_dispersion_publication_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import eqasim_sa.core.analyze as analyze_module

    root, _ = _campaign(tmp_path)
    indicators = root / "indicators.parquet"
    dispersion = root / "replication-dispersion.parquet"
    indicators.write_bytes(b"old indicators")
    dispersion.write_bytes(b"old dispersion")
    real_write = analyze_module.write_parquet_atomic

    def fail_second(table: pa.Table, destination: Path) -> Path:
        if destination == dispersion:
            raise OSError("injected dispersion publication failure")
        return real_write(table, destination)

    monkeypatch.setattr(analyze_module, "write_parquet_atomic", fail_second)
    with pytest.raises(OSError, match="injected dispersion"):
        _analyze(root, (_custom_indicator(),))
    assert indicators.read_bytes() == b"old indicators"
    assert dispersion.read_bytes() == b"old dispersion"


def test_seed_factor_produces_no_replication_dispersion(tmp_path: Path) -> None:
    root, _ = _campaign(tmp_path)
    result = _analyze(root, (_custom_indicator(),), seed_role="factor")
    assert result.dispersion_path is None
    assert not (root / "replication-dispersion.parquet").exists()


def test_missing_artifact_names_run_indicator_and_preserves_previous(
    tmp_path: Path,
) -> None:
    root, run_ids = _campaign(tmp_path)
    output = root / "indicators.parquet"
    output.write_bytes(b"previous")
    before = _snapshot(root)
    (root / "archives" / run_ids[0] / "age.parquet").unlink()
    after_removal = _snapshot(root)

    with pytest.raises(
        ValueError,
        match=rf"{run_ids[0]}.*custom\(age\).*artifact 'age' is not available",
    ):
        _analyze(root, (_custom_indicator(),))

    assert output.read_bytes() == b"previous"
    assert _snapshot(root) == after_removal
    assert set(before) - set(after_removal) == {f"archives/{run_ids[0]}/age.parquet"}
    assert not tuple(root.glob(".*indicators.parquet.*.tmp"))


def test_cli_analyze_uses_injected_core_path_without_simulation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    meta = tmp_path / "meta.yml"
    meta.write_text("test", encoding="utf-8")
    called = []

    def factory(path: Path) -> dict:
        called.append(path)
        return {
            "status": "completed",
            "path": "campaign/indicators.parquet",
            "runs": 2,
            "indicators": 1,
            "values": 2,
            "dispersionPath": "campaign/replication-dispersion.parquet",
            "sensitivityMethod": "morris",
            "sensitivityPath": "campaign/morris-ranking.parquet",
            "error": None,
        }

    monkeypatch.setattr(
        "sys.argv", ["eqasim-sa", "analyze", "--meta-config", os.fspath(meta)]
    )
    assert dispatch(ForbiddenSimulator(), object(), analyze_factory=factory) == 0
    assert called == [meta]
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert payload["sensitivityMethod"] == "morris"


def test_adapter_uses_current_indicators_without_revalidating_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _campaign(tmp_path)
    plan_before = (root / "plan.parquet").read_bytes()
    meta = type(
        "Meta",
        (),
        {
            "campaign_root": root,
            "campaign": "test",
            "indicators": (_custom_indicator(),),
            "seed_role": "replication",
            "method": "grid",
            "completeness_threshold": 0.0,
        },
    )()
    monkeypatch.setattr(
        "eqasim_sa.adapters.application.load_meta_config", lambda path: meta
    )
    monkeypatch.setattr(
        "eqasim_sa.adapters.application.load_run_specs",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("plan must not be regenerated")
        ),
    )
    payload = analyze_from_meta_config(tmp_path / "changed-meta.yml")
    assert payload["values"] == 2
    assert payload["dispersionPath"] == str(root / "replication-dispersion.parquet")
    assert payload["sensitivityMethod"] is None and payload["sensitivityPath"] is None
    assert payload | {} == {
        **payload,
        "completionRate": 2 / 3,
        "expected": 3,
        "completed": 2,
        "missing": 1,
        "failed": 0,
    }
    assert (root / "plan.parquet").read_bytes() == plan_before


def test_analyze_rejects_invalid_completion_marker_without_publication(
    tmp_path: Path,
) -> None:
    root, run_ids = _campaign(tmp_path)
    marker = root / "runs" / f"{run_ids[0]}.parquet"
    pq.write_table(pa.table({"runId": [run_ids[0]], "status": ["completed"]}), marker)

    with pytest.raises(ValueError, match="invalid completion marker"):
        _analyze(root, (_custom_indicator(),))

    assert not (root / "indicators.parquet").exists()


def test_analyze_rejects_foreign_and_full_contract_invalid_markers(
    tmp_path: Path,
) -> None:
    root, run_ids = _campaign(tmp_path)
    marker = root / "runs" / f"{run_ids[0]}.parquet"
    table = pq.read_table(marker).set_column(
        pq.read_table(marker).schema.get_field_index("contextHash"),
        "contextHash",
        pa.array(["wrong"]),
    )
    pq.write_table(table, marker, compression="zstd")
    with pytest.raises(ValueError, match="invalid completion marker.*context"):
        _analyze(root, (_custom_indicator(),))

    write_run(root, RunRecord(run_ids[0], "test", 1, "hash", "node", 1.0, {}))
    write_parquet_atomic(
        pa.table({"runId": ["f" * 32], "status": ["completed"]}),
        root / "runs" / ("f" * 32 + ".parquet"),
    )
    with pytest.raises(ValueError, match="invalid completion marker names.*ffffffff"):
        _analyze(root, (_custom_indicator(),))


def test_registered_indicator_requires_declared_artifact_before_plugin(
    tmp_path: Path,
) -> None:
    root, run_ids = _campaign(tmp_path)
    indicator = RegisteredIndicator("constant", "missing", _constant_indicator)
    with pytest.raises(
        ValueError,
        match=rf"run {run_ids[0]}.*constant\(missing\).*artifact 'missing' is not available",
    ):
        _analyze(root, (indicator,))


def test_custom_receives_read_only_copy_and_campaign_mutation_is_restored(
    tmp_path: Path,
) -> None:
    root, _ = _campaign(tmp_path)
    output = root / "indicators.parquet"
    output.write_bytes(b"previous")
    before = _snapshot(root) | {
        "indicators.parquet": (output.read_bytes(), output.stat().st_mtime_ns)
    }
    with pytest.raises(RuntimeError, match="mutated"):
        _analyze(root, (_mutating_indicator(root),))
    after = _snapshot(root) | {
        "indicators.parquet": (output.read_bytes(), output.stat().st_mtime_ns)
    }
    assert after == before
    assert not (root / "runs" / ("f" * 32 + ".parquet")).exists()


def test_analyze_rejects_duplicate_resolved_names_and_identities(
    tmp_path: Path,
) -> None:
    root, _ = _campaign(tmp_path)
    duplicate = (_custom_indicator(), _custom_indicator())
    with pytest.raises(ValueError, match=r"duplicate indicator name.*custom\(age\)"):
        _analyze(root, duplicate)


def test_completeness_boundary_failures_duplicates_and_rate_report(
    tmp_path: Path,
) -> None:
    root, run_ids = _campaign(tmp_path)
    plan = pq.read_table(root / "plan.parquet")
    write_parquet_atomic(
        pa.concat_tables((plan, plan.slice(0, 1))), root / "plan.parquet"
    )
    (root / "runs" / f"{run_ids[1]}.parquet").unlink()
    failed = "3" * 32
    log = root / "logs" / f"{failed}.log"
    log.parent.mkdir()
    log.write_text("pipeline failed\n")
    (root / "logs" / "foreign.log").write_text("ignored\n")
    (root / "logs" / f"{run_ids[0]}.log").write_text("stale failure\n")

    with pytest.raises(ValueError, match=r"missing=1.*failed=1"):
        _analyze(root, (_custom_indicator(),), completeness_threshold=0.34)
    result = _analyze(root, (_custom_indicator(),), completeness_threshold=1 / 3)
    assert (result.expected, result.completed, result.missing, result.failed) == (
        3,
        1,
        1,
        1,
    )
    assert result.completion_rate == pytest.approx(1 / 3)
    manifest = json.loads((root / "analysis-generation.json").read_text())
    assert manifest | {} == {
        **manifest,
        "completionRate": result.completion_rate,
        "expected": 3,
        "completed": 1,
        "missing": 1,
        "failed": 1,
    }


def test_completeness_refusal_calls_no_plugin_and_preserves_outputs(
    tmp_path: Path,
) -> None:
    root, run_ids = _campaign(tmp_path)
    (root / "runs" / f"{run_ids[1]}.parquet").unlink()
    outputs = {
        root / "indicators.parquet": b"old indicators",
        root / "replication-dispersion.parquet": b"old dispersion",
        root / "analysis-generation.json": b"old generation",
    }
    for path, content in outputs.items():
        path.write_bytes(content)

    def forbidden(artifacts: ArtifactSet) -> float:
        raise AssertionError("plugin called")

    with pytest.raises(ValueError, match=r"missing=2.*failed=0"):
        analyze(
            root,
            (RegisteredIndicator("forbidden", "age", forbidden),),
            campaign="test",
            completeness_threshold=0.5,
        )
    assert {path: path.read_bytes() for path in outputs} == outputs


@pytest.mark.parametrize("kind", ["empty", "symlink", "directory"])
def test_expected_invalid_failure_log_is_rejected(tmp_path: Path, kind: str) -> None:
    root, run_ids = _campaign(tmp_path)
    (root / "runs" / f"{run_ids[1]}.parquet").unlink()
    log = root / "logs" / f"{run_ids[1]}.log"
    log.parent.mkdir()
    if kind == "empty":
        log.write_bytes(b"")
    elif kind == "symlink":
        log.symlink_to(root / "plan.parquet")
    else:
        log.mkdir()
    with pytest.raises(ValueError, match=rf"invalid failure log.*{run_ids[1]}"):
        _analyze(root, completeness_threshold=0.0)


def test_zero_threshold_accepts_no_completed_runs_but_empty_plan_is_rejected(
    tmp_path: Path,
) -> None:
    root, run_ids = _campaign(tmp_path)
    for run_id in run_ids:
        (root / "runs" / f"{run_id}.parquet").unlink()
    result = _analyze(root, completeness_threshold=0.0)
    assert (result.expected, result.completed, result.completion_rate) == (3, 0, 0.0)
    assert pq.read_table(result.path).num_rows == 0

    empty = tmp_path / "empty"
    table = pq.read_table(root / "plan.parquet").slice(0, 0)
    write_parquet_atomic(table, empty / "plan.parquet")
    with pytest.raises(ValueError, match="no expected runs"):
        _analyze(empty, completeness_threshold=0.0)


def _hold_analysis(root: str, entered, release) -> None:  # type: ignore[no-untyped-def]
    def wait_for_release(artifacts: ArtifactSet) -> float:
        entered.set()
        release.wait(5)
        return 1.0

    analyze(
        Path(root),
        (RegisteredIndicator("wait", "age", wait_for_release),),
        campaign="test",
        completeness_threshold=0.0,
    )


def test_analyze_serializes_concurrent_publications(tmp_path: Path) -> None:
    root, _ = _campaign(tmp_path)
    context = multiprocessing.get_context("fork")
    entered, release = context.Event(), context.Event()
    process = context.Process(target=_hold_analysis, args=(str(root), entered, release))
    process.start()
    assert entered.wait(5)
    started = time.monotonic()

    second = context.Process(
        target=analyze,
        args=(root, (_custom_indicator(),)),
        kwargs={"campaign": "test", "completeness_threshold": 0.0},
    )
    second.start()
    time.sleep(0.2)
    assert second.is_alive()
    release.set()
    process.join(5)
    second.join(5)
    assert process.exitcode == second.exitcode == 0
    assert time.monotonic() - started >= 0.2


def test_cli_analyze_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["eqasim-sa", "analyze", "--meta-config", str(tmp_path / "meta.yml")],
    )
    status = dispatch(
        None,
        None,
        analyze_factory=lambda path: (_ for _ in ()).throw(ValueError("missing age")),
    )
    assert status == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "path": None,
        "runs": 0,
        "indicators": 0,
        "values": 0,
        "completionRate": None,
        "expected": 0,
        "completed": 0,
        "missing": 0,
        "failed": 0,
        "dispersionPath": None,
        "sensitivityMethod": None,
        "sensitivityPath": None,
        "error": "missing age",
    }


def test_cli_analyze_reports_complete_keyboard_interrupt_payload_and_returns_130(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "sys.argv", ["eqasim-sa", "analyze", "--meta-config", "meta.yml"]
    )
    status = dispatch(
        None,
        None,
        analyze_factory=lambda path: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    assert status == 130
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "path": None,
        "runs": 0,
        "indicators": 0,
        "values": 0,
        "completionRate": None,
        "expected": 0,
        "completed": 0,
        "missing": 0,
        "failed": 0,
        "dispersionPath": None,
        "sensitivityMethod": None,
        "sensitivityPath": None,
        "error": "KeyboardInterrupt",
    }


def test_analyze_adapter_passes_requested_method_and_exposes_sensitivity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "campaign"
    seen = {}
    meta = SimpleNamespace(
        campaign_root=root,
        indicators=(),
        campaign="demo",
        completeness_threshold=1.0,
        seed_role="replication",
        method="morris",
    )
    result = SimpleNamespace(
        path=root / "indicators.parquet",
        dispersion_path=root / "replication-dispersion.parquet",
        sensitivity_method="morris",
        sensitivity_path=root / "morris-ranking.parquet",
        runs=2,
        indicators=1,
        values=2,
        completion_rate=1.0,
        expected=2,
        completed=2,
        missing=0,
        failed=0,
    )
    monkeypatch.setattr(
        "eqasim_sa.adapters.application.load_meta_config", lambda path: meta
    )

    def capture(*args, **kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        return result

    monkeypatch.setattr("eqasim_sa.adapters.application.analyze", capture)
    payload = analyze_from_meta_config(Path("meta.yml"))

    assert seen["method"] == "morris"
    assert payload["dispersionPath"] == str(result.dispersion_path)
    assert payload["sensitivityMethod"] == "morris"
    assert payload["sensitivityPath"] == str(result.sensitivity_path)


def test_analyze_adapter_passes_sobol_bootstrap_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, seen = tmp_path / "campaign", {}
    meta = SimpleNamespace(
        campaign_root=root,
        indicators=(),
        campaign="demo",
        completeness_threshold=1.0,
        seed_role="factor",
        method="sobol",
        bootstrap_resamples=999,
        confidence_level=0.95,
    )
    result = SimpleNamespace(
        path=root / "indicators.parquet",
        dispersion_path=None,
        sensitivity_method="sobol",
        sensitivity_path=root / "sobol-indices.parquet",
        runs=1,
        indicators=1,
        values=1,
        completion_rate=1.0,
        expected=1,
        completed=1,
        missing=0,
        failed=0,
    )
    monkeypatch.setattr(
        "eqasim_sa.adapters.application.load_meta_config", lambda path: meta
    )
    monkeypatch.setattr(
        "eqasim_sa.adapters.application.analyze",
        lambda *args, **kwargs: seen.update(kwargs) or result,
    )
    payload = analyze_from_meta_config(Path("meta.yml"))
    assert seen["bootstrap_resamples"] == 999 and seen["confidence_level"] == 0.95
    assert payload["dispersionPath"] is None
    assert payload["sensitivityMethod"] == "sobol"
    assert payload["sensitivityPath"] == str(root / "sobol-indices.parquet")
