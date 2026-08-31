import hashlib
import json
import math
import multiprocessing
import os
import re
import statistics
import threading
import time
import copy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from scipy.stats import qmc, sobol_indices, t as student_t

from eqasim_sa.adapters.application import analyze_from_meta_config
from eqasim_sa.adapters.fake.simulator import FakeSimulator
from eqasim_sa.cli import dispatch
from eqasim_sa.core.analyze import (
    LEGACY_CANONICAL_MORRIS_DOMAIN,
    LEGACY_MULTISCRAMBLE_SOBOL_DOMAIN,
    _finite_population_variance,
    _legacy_canonical_selection,
    _morris_ranking,
    _multiscramble_sobol_tables,
    _sobol_bootstrap,
    _sobol_estimates,
    _sobol_ranking,
    _validate_frozen_plan,
    analyze,
)
from eqasim_sa.core.indicators import ArtifactSet, PluginIdentity, RegisteredIndicator
from eqasim_sa.core.ledger import (
    ArtifactManifestEntry,
    RunRecord,
    write_parquet_atomic,
    write_run,
)
from eqasim_sa.core.plan import (
    MorrisDesign,
    SobolDesign,
    _canonical_replication_seed,
    build_plan,
    load_run_specs,
)
from eqasim_sa.core.run import execute_run
from eqasim_sa.core.sensitivity import engine_descriptor, sobol_symmetric_indices
from tests.test_canonical_morris_plan import canonical_design, discrete_design
from tests.test_multiscramble_sobol_plan import multiscramble_design
from tests.provenance_helpers import (
    context_hash,
    execution_evidence,
    scientific_context_descriptor,
    scientific_registered_indicator,
)


def _artifact_value(artifacts: ArtifactSet) -> float:
    return float(pq.read_table(artifacts.path("age"))["synthetic"][0].as_py())


def _custom_indicator() -> RegisteredIndicator:
    return RegisteredIndicator("custom", "age", _artifact_value)


def _constant_indicator(artifacts: ArtifactSet) -> float:
    return 1.0


def _write_verified_run(
    root: Path,
    record: RunRecord,
    evidence=None,  # type: ignore[no-untyped-def]
) -> Path:
    archive = root / "archives" / record.run_id
    entries = tuple(
        ArtifactManifestEntry(
            path.name,
            path.stat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(archive.iterdir(), key=lambda item: os.fsencode(item.name))
    )
    return write_run(root, record, entries, execution_evidence=evidence)


def _scientific_indicator() -> RegisteredIndicator:
    return scientific_registered_indicator("synthetic", "age")


def _analyze(root: Path, indicators=(), **changes):  # type: ignore[no-untyped-def]
    options = {"campaign": "test", "completeness_threshold": 0.0, **changes}
    return analyze(root, indicators, **options)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def _legacy_scientific_campaign(
    root: Path, method: str, version: int = 2
) -> tuple[Path, dict[str, object]]:
    if version == 1:
        fixture_name = {
            "canonical_morris": "canonical-morris-v1-706f0bc.parquet",
            "multiscramble_sobol": "multiscramble-sobol-v1-706f0bc.parquet",
        }[method]
        root.mkdir(parents=True)
        path = root / "plan.parquet"
        path.write_bytes(Path(__file__).with_name(fixture_name).read_bytes())
        table = pq.read_table(path)
        design_key = (
            b"canonicalMorrisDesign"
            if method == "canonical_morris"
            else b"multiScrambleSobolDesign"
        )
        sealed = json.loads(table.schema.metadata[design_key])
        options = {
            "campaign": "test",
            "completeness_threshold": 1.0,
            "seed_role": "replication" if method == "canonical_morris" else "factor",
            "method": method,
            "confidence_level": sealed["confidenceLevel"],
        }
        if method == "canonical_morris":
            options.update(
                bootstrap_resamples=sealed["bootstrapResamples"],
                convergence_checkpoints=tuple(sealed["convergenceCheckpoints"]),
                screening_top_k=sealed["screeningTopK"],
            )
        return root, options

    if method == "canonical_morris":
        design = canonical_design(root)
        design_key = b"canonicalMorrisDesign"
        version_key = b"canonicalMorrisDesignVersion"
        options = {
            "campaign": "test",
            "completeness_threshold": 1.0,
            "seed_role": "replication",
            "method": method,
            "bootstrap_resamples": design.bootstrap_resamples,
            "confidence_level": design.confidence_level,
            "convergence_checkpoints": design.convergence_checkpoints,
            "screening_top_k": design.screening_top_k,
        }
    else:
        design = multiscramble_design(root)
        design_key = b"multiScrambleSobolDesign"
        version_key = b"multiScrambleSobolDesignVersion"
        options = {
            "campaign": "test",
            "completeness_threshold": 1.0,
            "seed_role": "factor",
            "method": method,
            "confidence_level": design.confidence_level,
        }
    build_plan(design)
    path = root / "plan.parquet"
    table = pq.read_table(path)
    metadata = dict(table.schema.metadata or {})
    sealed = json.loads(metadata[design_key])
    descriptor = json.loads(metadata[b"contextDescriptor"])
    descriptor["eqasimSaVersion"] = f"0.{version}.0"
    sealed["scientificContract"]["software"] = {
        "eqasimSaVersion": f"0.{version}.0",
        "eqasimSaSha256": "d" * 64,
    }
    legacy_context = context_hash(descriptor)
    if method == "canonical_morris":
        sealed.pop("numericalEngine")
        pool, selected_pairs = _legacy_canonical_selection(
            sealed["candidateDefinition"], sealed["trajectories"]
        )
        selected = [
            {
                "candidateId": candidate_id,
                "matrix": matrix,
                "selectionOrder": index,
            }
            for index, (candidate_id, matrix) in enumerate(selected_pairs)
        ]
        sealed.update(
            {
                "candidatePoolIdentities": [identity for identity, _ in pool],
                "selectedTrajectories": selected,
                "selectionAlgorithm": "greedy-sum-pairwise-squared-distance",
                "selectionVersion": 1,
            }
        )
        rows = []
        names = tuple(item["name"] for item in sealed["factors"])
        supports = {item["name"]: item["support"] for item in sealed["factors"]}
        for trajectory, selected_trajectory in enumerate(selected):
            matrix = selected_trajectory["matrix"]
            changed = [
                next(
                    index
                    for index, (before, after) in enumerate(zip(left, right))
                    if before != after
                )
                for left, right in zip(matrix, matrix[1:])
            ]
            mapped = [
                tuple(
                    supports[name][
                        0
                        if grid_index == 0
                        else min(
                            (len(supports[name]) * grid_index - 1)
                            // (sealed["p"] - 1),
                            len(supports[name]) - 1,
                        )
                    ]
                    for name, grid_index in zip(names, point)
                )
                for point in matrix
            ]
            for replication, base_seed in enumerate(sealed["replicationBaseSeeds"]):
                seed = _canonical_replication_seed(
                    sealed["candidateDefinition"]["designSeed"],
                    base_seed,
                    selected_trajectory["candidateId"],
                    replication,
                )
                for rank, values in enumerate(mapped):
                    role = (
                        f"trajectory:{trajectory}:rank:0:start"
                        if rank == 0
                        else f"trajectory:{trajectory}:rank:{rank}:factor:{names[changed[rank - 1]]}"
                    )
                    design_id = hashlib.sha256(
                        LEGACY_CANONICAL_MORRIS_DOMAIN
                        + _canonical_json(
                            {
                                "design": sealed,
                                "replicationIndex": replication,
                                "role": role,
                            }
                        )
                    ).hexdigest()[:32]
                    run_id = hashlib.sha256(
                        _canonical_json({"seed": seed, "values": values})
                    ).hexdigest()[:32]
                    rows.append(
                        {
                            "designId": design_id,
                            "method": method,
                            "role": role,
                            "runId": run_id,
                            "seed": seed,
                            "contextHash": legacy_context,
                            **dict(zip(names, values)),
                        }
                    )
    else:
        sealed.pop("numericalEngine")
        sealed["scrambleSeeds"] = [5, 6, 9, 11]
        sealed["qmc"] = {
            "bits": 64,
            "engine": "scipy.stats.qmc.Sobol",
            "optimization": None,
            "scramble": True,
        }
        sealed["saltelliEstimator"] = "symmetric-ab-ba-v1"
        rows = []
        names = tuple(item["name"] for item in sealed["factors"])
        supports = {item["name"]: item["support"] for item in sealed["factors"]}
        maximum = sealed["maximumBaseSampleSize"]
        for scramble, scramble_seed in enumerate(sealed["scrambleSeeds"]):
            sample = qmc.Sobol(
                d=2 * len(names),
                scramble=True,
                bits=64,
                rng=scramble_seed,
                optimization=None,
            ).random_base2(m=maximum.bit_length() - 1)
            for row_index, point in enumerate(sample):
                indices = tuple(
                    math.floor(float(value) * len(supports[name]))
                    for name, value in zip(names * 2, point)
                )
                a, b = indices[: len(names)], indices[len(names) :]
                positions = [
                    (f"scramble:{scramble}:A:row:{row_index}", a),
                    (f"scramble:{scramble}:B:row:{row_index}", b),
                ]
                for index, name in enumerate(names):
                    ab, ba = list(a), list(b)
                    ab[index], ba[index] = b[index], a[index]
                    positions.extend(
                        (
                            (
                                f"scramble:{scramble}:AB:factor:{name}:row:{row_index}",
                                tuple(ab),
                            ),
                            (
                                f"scramble:{scramble}:BA:factor:{name}:row:{row_index}",
                                tuple(ba),
                            ),
                        )
                    )
                for role, selected_indices in positions:
                    values = tuple(
                        supports[name][index]
                        for name, index in zip(names, selected_indices)
                    )
                    design_id = hashlib.sha256(
                        LEGACY_MULTISCRAMBLE_SOBOL_DOMAIN
                        + _canonical_json({"design": sealed, "role": role})
                    ).hexdigest()[:32]
                    run_id = hashlib.sha256(
                        _canonical_json({"values": values})
                    ).hexdigest()[:32]
                    logical = dict(zip(names, values))
                    rows.append(
                        {
                            "designId": design_id,
                            "method": method,
                            "role": role,
                            "runId": run_id,
                            "seed": logical.pop("seed"),
                            "contextHash": legacy_context,
                            **logical,
                        }
                    )

    metadata[version_key] = str(version).encode()
    metadata[design_key] = _canonical_json(sealed)
    metadata[b"contextDescriptor"] = _canonical_json(descriptor)
    legacy = pa.Table.from_pylist(rows, schema=table.schema.with_metadata(metadata))
    write_parquet_atomic(legacy, path)
    return root, options


def root_relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _tree_snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
        for path in sorted((root, *root.rglob("*")))
    }


def _mutating_indicator(campaign: Path):  # type: ignore[no-untyped-def]
    def mutate(artifacts: ArtifactSet) -> float:
        assert artifacts.path("age").stat().st_mode & 0o222 == 0
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
        _write_verified_run(
            root, RunRecord(run_id, "test", 1, "hash", "node", 1.0, {})
        )
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
        _write_verified_run(
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
        _write_verified_run(
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
    ranking = first.sensitivity_path
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
    assert first.sensitivity_method == "sobol"
    assert ranking.parent.parent.name == "generations"
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
    manifest = json.loads((root / "analysis-generation.json").read_text())
    assert manifest["version"] == 7
    assert manifest["layout"] == "immutable-generation-v1"
    assert manifest["sensitivityMethod"] == "sobol"
    assert manifest["inferenceStatus"] == "legacy_noninferential"
    assert manifest["completionRate"] == 1.0
    assert manifest["completed"] == replay.completed
    assert manifest["missing"] == manifest["failed"] == 0
    sensitivity = manifest["outputs"]["sensitivity"]
    assert sensitivity["path"] == manifest["sensitivityPath"]
    assert sensitivity["sha256"] == manifest["sensitivitySha256"]
    assert sensitivity["sha256"] == hashlib.sha256(
        replay.sensitivity_path.read_bytes()
    ).hexdigest()
    assert manifest["provenance"]["analysisOptions"]["bootstrapResamples"] == 31
    frozen_plan = pq.read_table(root / "plan.parquet")
    assert manifest["provenance"]["plan"] == {
        "sha256": hashlib.sha256((root / "plan.parquet").read_bytes()).hexdigest(),
        "contextDescriptor": json.loads(
            frozen_plan.schema.metadata[b"contextDescriptor"]
        ),
    }


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


@pytest.mark.parametrize(
    "method", ["canonical_morris", "multiscramble_sobol", "discrete_oat"]
)
def test_all_scientific_and_oat_plans_are_fully_validated_before_run_state_or_plugins(
    tmp_path: Path,
    method: str,
) -> None:
    descriptor = scientific_context_descriptor()
    sealed_context_hash = context_hash(descriptor)
    root = tmp_path / method
    if method == "canonical_morris":
        design = replace(
            canonical_design(root),
            context_descriptor=descriptor,
            context_hash=sealed_context_hash,
        )
    elif method == "multiscramble_sobol":
        design = multiscramble_design(root)
    else:
        design = replace(
            discrete_design(root),
            context_descriptor=descriptor,
            context_hash=sealed_context_hash,
        )
    build_plan(design)
    plan_path = root / "plan.parquet"
    plan = pq.read_table(plan_path)
    locations = plan.to_pylist()
    if method == "multiscramble_sobol":
        locations[0]["role"] = "scramble:0:A:row:999"
    else:
        locations[0]["a"] = 999
    write_parquet_atomic(pa.Table.from_pylist(locations, schema=plan.schema), plan_path)
    runs = root / "runs"
    runs.mkdir()
    (runs / "foreign").write_text("run state must not be consulted")
    calls = []
    indicator = RegisteredIndicator(
        "forbidden",
        "missing",
        lambda artifacts: calls.append(artifacts) or 1.0,
    )
    options = {
        "campaign": "test",
        "completeness_threshold": 1.0,
        "method": method,
        "seed_role": "factor" if method == "multiscramble_sobol" else "replication",
        "bootstrap_resamples": (
            design.bootstrap_resamples if method == "canonical_morris" else None
        ),
        "confidence_level": (
            design.confidence_level
            if method in {"canonical_morris", "multiscramble_sobol"}
            else None
        ),
        "convergence_checkpoints": (
            design.convergence_checkpoints
            if method == "canonical_morris"
            else None
        ),
        "screening_top_k": (
            design.screening_top_k if method == "canonical_morris" else None
        ),
    }

    with pytest.raises(ValueError, match="canonical Morris|multi-scramble Sobol|Morris"):
        analyze(root, (indicator,), **options)

    assert calls == []
    assert not (root / "analysis").exists()
    assert not (root / "analysis-generation.json").exists()


@pytest.mark.parametrize("limit", ["resamples", "work"])
def test_sobol_bootstrap_caps_precede_qmc_artifact_capture_and_plugins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: str,
) -> None:
    import eqasim_sa.core.plan as plan_module

    root = _sobol_campaign(tmp_path)
    before = _tree_snapshot(root)
    calls = []
    indicator = RegisteredIndicator(
        "forbidden",
        "age",
        lambda artifacts: calls.append(artifacts) or 1.0,
    )
    bootstrap_resamples = 31
    if limit == "resamples":
        bootstrap_resamples = plan_module.MAX_BOOTSTRAP_RESAMPLES + 1
    else:
        monkeypatch.setattr(plan_module, "MAX_BOOTSTRAP_WORK_CELLS", 1)
        monkeypatch.setattr(
            "eqasim_sa.core.analyze.qmc.Sobol",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("QMC allocated")
            ),
        )

    with pytest.raises(ValueError, match="operational safe cap"):
        analyze(
            root,
            (indicator,),
            campaign="test",
            completeness_threshold=1.0,
            seed_role="factor",
            method="sobol",
            bootstrap_resamples=bootstrap_resamples,
            confidence_level=0.9,
        )

    assert calls == []
    assert _tree_snapshot(root) == before
    assert not (root / "analysis").exists()


def test_multiscramble_descriptive_only_publishes_partial_campaign_without_inference(
    tmp_path: Path,
) -> None:
    design = multiscramble_design(tmp_path / "multiscramble-descriptive")
    build_plan(design)
    plan = pq.read_table(design.campaign_root / "plan.parquet")
    row = next(iter({item["runId"]: item for item in plan.to_pylist()}.values()))
    write_parquet_atomic(
        pa.table({"synthetic": [float(row["a"] + row["seed"])]}),
        design.campaign_root / "archives" / row["runId"] / "age.parquet",
    )
    _write_verified_run(
        design.campaign_root,
        RunRecord(
            row["runId"],
            "test",
            row["seed"],
            design.context_hash,
            "node",
            1.0,
            {"a": row["a"]},
        ),
    )
    options = {
        "campaign": "test",
        "completeness_threshold": 1.0,
        "seed_role": "factor",
        "method": "multiscramble_sobol",
        "confidence_level": design.confidence_level,
    }

    with pytest.raises(ValueError, match="below threshold"):
        analyze(design.campaign_root, (_scientific_indicator(),), **options)
    assert not (design.campaign_root / "analysis").exists()

    result = analyze(
        design.campaign_root,
        (_scientific_indicator(),),
        descriptive_only=True,
        **options,
    )

    assert result.completed == 1
    assert result.inference_status == "incomplete_noninferential"
    assert result.dispersion_path is None
    assert result.sensitivity_method is None
    assert result.sensitivity_path is None
    assert result.by_scramble_path is None
    assert result.convergence_path is None
    assert result.diagnostics_path is None
    assert result.convergence_evidence_path is None
    manifest = json.loads(
        (design.campaign_root / "analysis-generation.json").read_text()
    )
    assert set(manifest["outputs"]) == {
        "capturedRunSet",
        "indicators",
        "outputSummary",
        "governedReport",
    }
    assert manifest["provenance"]["analysisOptions"]["descriptiveOnly"] is True
    sealed = json.loads(
        plan.schema.metadata[b"multiScrambleSobolDesign"]
    )["numericalEngine"]
    assert manifest["provenance"]["numericalEngine"] == sealed
    report = json.loads(result.governed_report_path.read_text())
    assert report["completion"]["complete"] is False
    assert report["claimAuthorized"] is False
    assert "incomplete_noninferential" in report["reasonCodes"]


@pytest.mark.parametrize(
    ("name", "version_key", "sha256"),
    [
        (
            "canonical-morris-v1-706f0bc.parquet",
            b"canonicalMorrisDesignVersion",
            "3e36a12d2efe89c924e7a6e01b357c8f85ef2e589754c180c8b3d4e57171a604",
        ),
        (
            "multiscramble-sobol-v1-706f0bc.parquet",
            b"multiScrambleSobolDesignVersion",
            "0cf49034f8482d20d5e37066a0b0bdf52fc7ad41771412f371d8eea4ab2503ab",
        ),
    ],
)
def test_legacy_v1_fixtures_are_immutable_baseline_plans(
    name: str, version_key: bytes, sha256: str
) -> None:
    path = Path(__file__).with_name(name)

    assert hashlib.sha256(path.read_bytes()).hexdigest() == sha256
    assert pq.read_table(path).schema.metadata[version_key] == b"1"


@pytest.mark.parametrize("method", ["canonical_morris", "multiscramble_sobol"])
@pytest.mark.parametrize("version", [1, 2])
def test_legacy_scientific_is_descriptive_only_without_salib_reinterpretation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    version: int,
) -> None:
    import importlib

    analyze_module = importlib.import_module("eqasim_sa.core.analyze")
    root, options = _legacy_scientific_campaign(
        tmp_path / f"{method}-v{version}", method, version
    )
    for name in ("engine_descriptor", "morris_unit_trajectories", "sobol_unit_positions"):
        monkeypatch.setattr(
            analyze_module,
            name,
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("legacy validation invoked SALib")
            ),
        )

    result = analyze(
        root,
        (_custom_indicator(),),
        descriptive_only=True,
        **options,
    )

    assert result.completed == 0
    assert result.inference_status == "incomplete_noninferential"
    assert result.sensitivity_method is None
    assert result.sensitivity_path is None
    assert result.convergence_path is None
    report = json.loads(result.governed_report_path.read_text())
    assert report["claimAuthorized"] is False
    assert "scientificContract" not in report
    manifest = json.loads((root / "analysis-generation.json").read_text())
    assert manifest["provenance"]["numericalEngine"] is None


def test_legacy_scientific_v2_descriptive_accepts_legacy_completion_marker(
    tmp_path: Path,
) -> None:
    root, options = _legacy_scientific_campaign(
        tmp_path / "legacy-completion", "canonical_morris"
    )
    plan = pq.read_table(root / "plan.parquet")
    row = plan.to_pylist()[0]
    archive = root / "archives" / row["runId"]
    archive.mkdir(parents=True)
    write_parquet_atomic(pa.table({"synthetic": [1.5]}), archive / "age.parquet")
    write_run(
        root,
        RunRecord(
            row["runId"],
            "test",
            row["seed"],
            row["contextHash"],
            "node",
            1.0,
            {name: row[name] for name in ("a", "b")},
        ),
    )

    result = analyze(
        root,
        (_custom_indicator(),),
        descriptive_only=True,
        **options,
    )

    assert result.completed == 1
    assert result.inference_status == "incomplete_noninferential"
    assert pq.read_table(result.path)["value"].to_pylist() == [1.5]


@pytest.mark.parametrize("method", ["canonical_morris", "multiscramble_sobol"])
@pytest.mark.parametrize("version", [1, 2])
def test_legacy_scientific_refuses_inference_before_runs_or_indicator_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    version: int,
) -> None:
    import importlib

    analyze_module = importlib.import_module("eqasim_sa.core.analyze")
    root, options = _legacy_scientific_campaign(
        tmp_path / f"{method}-v{version}", method, version
    )
    monkeypatch.setattr(
        analyze_module.CampaignSnapshot,
        "capture",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("runs inspected")),
    )
    monkeypatch.setattr(
        analyze_module,
        "verify_indicator_runtime_identity",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("indicator runtime inspected")
        ),
    )

    with pytest.raises(
        ValueError, match=rf"v{version} is legacy.*replan.*SALib.*v3"
    ):
        analyze(root, (_custom_indicator(),), **options)

    assert not (root / "analysis").exists()
    assert not (root / "analysis-generation.json").exists()


@pytest.mark.parametrize("method", ["canonical_morris", "multiscramble_sobol"])
@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("tamper", ["noncanonical", "distribution", "design_id", "role"])
def test_legacy_scientific_descriptive_reader_rejects_tampering(
    tmp_path: Path,
    method: str,
    version: int,
    tamper: str,
) -> None:
    root, options = _legacy_scientific_campaign(
        tmp_path / f"{method}-v{version}-{tamper}", method, version
    )
    path = root / "plan.parquet"
    table = pq.read_table(path)
    metadata = dict(table.schema.metadata or {})
    design_key = (
        b"canonicalMorrisDesign"
        if method == "canonical_morris"
        else b"multiScrambleSobolDesign"
    )
    rows = table.to_pylist()
    if tamper == "noncanonical":
        metadata[design_key] += b" "
    elif tamper == "distribution":
        sealed = json.loads(metadata[design_key])
        if method == "canonical_morris":
            sealed["distributions"][0]["provenance"] = "tampered"
        else:
            sealed["factors"][0]["distribution"]["provenance"] = "tampered"
        metadata[design_key] = _canonical_json(sealed)
    elif tamper == "design_id":
        rows[1]["designId"] = "f" * 32
    else:
        rows[0]["role"] = "invalid"
    write_parquet_atomic(
        pa.Table.from_pylist(rows, schema=table.schema.with_metadata(metadata)),
        path,
    )

    with pytest.raises(ValueError, match="legacy|scientific_contract"):
        analyze(
            root,
            (_custom_indicator(),),
            descriptive_only=True,
            **options,
        )

    assert not (root / "analysis").exists()


@pytest.mark.parametrize(
    ("fixture_name", "requested_method"),
    [
        ("canonical-morris-v1-706f0bc.parquet", "multiscramble_sobol"),
        ("multiscramble-sobol-v1-706f0bc.parquet", "canonical_morris"),
    ],
)
def test_legacy_plan_method_mismatch_is_rejected_before_method_engine(
    fixture_name: str, requested_method: str
) -> None:
    plan = pq.read_table(Path(__file__).with_name(fixture_name))

    with pytest.raises(ValueError, match="does not match requested method"):
        _validate_frozen_plan(
            plan,
            requested_method,
            None,
            None,
            None,
            None,
            {},
            True,
        )


@pytest.mark.parametrize("tamper", ["tolerance", "budget"])
def test_legacy_v1_contract_rejects_impossible_decision_or_budget(
    tmp_path: Path, tamper: str
) -> None:
    root, options = _legacy_scientific_campaign(
        tmp_path / tamper, "multiscramble_sobol", 1
    )
    path = root / "plan.parquet"
    table = pq.read_table(path)
    metadata = dict(table.schema.metadata or {})
    sealed = json.loads(metadata[b"multiScrambleSobolDesign"])
    if tamper == "tolerance":
        sealed["scientificContract"]["charter"]["decision"].update(
            objective="ff", practical_tolerance=-1.0
        )
    else:
        sealed["scientificContract"]["charter"]["budget"].update(
            maximum_runs=1, reserve_fraction=0.0
        )
    metadata[b"multiScrambleSobolDesign"] = _canonical_json(sealed)
    write_parquet_atomic(table.replace_schema_metadata(metadata), path)

    with pytest.raises(ValueError, match="tolerance|budget"):
        analyze(
            root,
            (_custom_indicator(),),
            descriptive_only=True,
            **options,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("metric", "rank_stability", "ci_width"),
        ("consecutive_checks", 3, "at most 2"),
    ],
)
def test_multiscramble_analyzer_revalidates_sealed_stopping_contract(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    design = multiscramble_design(tmp_path / "multiscramble-stopping")
    build_plan(design)
    plan = pq.read_table(design.campaign_root / "plan.parquet")
    metadata = dict(plan.schema.metadata)
    sealed = json.loads(metadata[b"multiScrambleSobolDesign"])
    sealed["scientificContract"]["charter"]["stopping"][0][field] = value
    metadata[b"multiScrambleSobolDesign"] = json.dumps(
        sealed, sort_keys=True, separators=(",", ":")
    ).encode()

    with pytest.raises(ValueError, match=message):
        _multiscramble_sobol_tables(
            plan.replace_schema_metadata(metadata),
            [],
            design.confidence_level,
        )


@pytest.mark.parametrize("damage", ["engine", "estimator"])
def test_multiscramble_analyzer_rejects_v3_numerical_contract_tampering(
    tmp_path: Path, damage: str
) -> None:
    design = multiscramble_design(tmp_path / "multiscramble-numerical-contract")
    build_plan(design)
    plan = pq.read_table(design.campaign_root / "plan.parquet")
    metadata = dict(plan.schema.metadata)
    sealed = json.loads(metadata[b"multiScrambleSobolDesign"])
    if damage == "engine":
        sealed["numericalEngine"]["version"] = "0"
    else:
        sealed["saltelliEstimator"] = "symmetric-ab-ba-v1"
    metadata[b"multiScrambleSobolDesign"] = json.dumps(
        sealed, sort_keys=True, separators=(",", ":")
    ).encode()

    with pytest.raises(ValueError, match="sealed settings"):
        _multiscramble_sobol_tables(
            plan.replace_schema_metadata(metadata),
            [],
            design.confidence_level,
        )


def test_sobol_analysis_rejects_legacy_degenerate_hybrid_contrasts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import eqasim_sa.core.plan as plan_module

    root = tmp_path / "legacy-degenerate"
    descriptor = {
        "eqasimCommit": "a",
        "dataManifestSha256": "b",
        "lockfileSha256": "c",
        "eqasimSaVersion": "0.1.0",
    }
    descriptor_bytes = json.dumps(
        descriptor, sort_keys=True, separators=(",", ":")
    ).encode()
    context_hash = hashlib.sha256(
        b"eqasim-sa:context:v1\0" + descriptor_bytes
    ).hexdigest()
    design = SobolDesign(
        {
            "config": {
                "sampling_rate": 0.001,
                "matching_age_scheme": "matching_6",
                "analysis_age_scheme": "analysis_7",
            }
        },
        {
            "sampling_rate": [0.001, 0.002],
            "matching_age_scheme": ["matching_6", "analysis_7"],
        },
        (1401, 1402),
        {},
        context_hash,
        descriptor,
        root,
        base_sample_size=2,
        design_seed=20260814,
    )
    monkeypatch.setattr(
        plan_module, "validate_sobol_effective_hybrid_contrasts", lambda *args: None
    )
    build_plan(design)
    unique = {}
    for row in pq.read_table(root / "plan.parquet").to_pylist():
        unique.setdefault(row["runId"], row)
    for run_id, row in unique.items():
        write_parquet_atomic(
            pa.table({"synthetic": [float(row["seed"])]}),
            root / "archives" / run_id / "age.parquet",
        )
        _write_verified_run(
            root,
            RunRecord(
                run_id,
                "test",
                row["seed"],
                context_hash,
                "node",
                1.0,
                {
                    "matching_age_scheme": row["matching_age_scheme"],
                    "sampling_rate": row["sampling_rate"],
                },
            ),
        )
    with pytest.raises(ValueError, match="no effective hybrid contrast"):
        _analyze(
            root,
            (_custom_indicator(),),
            completeness_threshold=1.0,
            seed_role="factor",
            method="sobol",
            bootstrap_resamples=7,
            confidence_level=0.9,
        )


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


def test_multiscramble_uses_independent_estimates_student_t_and_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    design = multiscramble_design(tmp_path / "multiscramble")
    build_plan(design)
    plan = pq.read_table(design.campaign_root / "plan.parquet")
    unique = {row["runId"]: row for row in plan.to_pylist()}
    rows = [
        (run_id, "synthetic(age)", float(row["a"] + row["seed"] / 10 + row["a"] * row["seed"] / 20))
        for run_id, row in unique.items()
    ]
    monkeypatch.setattr(
        "eqasim_sa.core.analyze._sobol_bootstrap",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("bootstrap called")),
    )
    indices, by_scramble, convergence, diagnostics = _multiscramble_sobol_tables(
        plan, rows, design.confidence_level
    )

    assert len(by_scramble) == 2 * 4 * 2
    assert len(convergence) == 2 * 2
    assert len(indices) == 2
    assert len(diagnostics) == 2 * 4
    assert set(by_scramble["scramble"].to_pylist()) == {0, 1, 2, 3}
    assert all(row["first_order_ci_width"] >= 0 for row in convergence.to_pylist())
    assert all(row["objective"] == "fp" for row in convergence.to_pylist())
    assert all(row["within_practical_tolerance"] is None for row in convergence.to_pylist())
    assert all(
        row["is_actionable"] == (row["factor"] != "seed")
        for row in by_scramble.to_pylist()
    )
    assert all(
        row["is_actionable"] == (row["factor"] != "seed")
        for row in convergence.to_pylist()
    )
    assert all(
        (row["rank"] is None) == (row["factor"] == "seed")
        for row in convergence.to_pylist()
    )
    assert all(row["maximum_multiplicity"] >= 1 for row in diagnostics.to_pylist())
    by_role = {row["role"]: row for row in plan.to_pylist()}
    observations = {run_id: value for run_id, _, value in rows}
    by_key = {
        (row["checkpoint"], row["scramble"], row["factor"]): row
        for row in by_scramble.to_pylist()
    }
    for checkpoint in design.base_sample_sizes:
        for scramble in range(len(design.scramble_seeds)):
            def role_values(prefix: str, factor: str | None = None) -> np.ndarray:
                roles = (
                    f"scramble:{scramble}:{prefix}:row:{row}"
                    if factor is None
                    else f"scramble:{scramble}:{prefix}:factor:{factor}:row:{row}"
                    for row in range(checkpoint)
                )
                return np.asarray([observations[by_role[role]["runId"]] for role in roles])

            first, total = sobol_symmetric_indices(
                ("a", "seed"),
                role_values("A"),
                role_values("B"),
                np.asarray([role_values("AB", factor) for factor in ("a", "seed")]),
                np.asarray([role_values("BA", factor) for factor in ("a", "seed")]),
                indicator="synthetic(age)",
            )
            for index, factor in enumerate(("a", "seed")):
                assert by_key[checkpoint, scramble, factor]["first_order"] == pytest.approx(
                    first[index]
                )
                assert by_key[checkpoint, scramble, factor]["total_order"] == pytest.approx(
                    total[index]
                )
    per_scramble = [
        row["first_order"]
        for row in by_scramble.to_pylist()
        if row["checkpoint"] == 4 and row["factor"] == "a"
    ]
    aggregate = next(
        row
        for row in convergence.to_pylist()
        if row["checkpoint"] == 4 and row["factor"] == "a"
    )
    mean = statistics.fmean(per_scramble)
    sd = statistics.stdev(per_scramble)
    se = sd / math.sqrt(4)
    critical = student_t.ppf(0.975, df=3)
    assert aggregate["first_order_mean"] == pytest.approx(mean)
    assert aggregate["first_order_sd"] == pytest.approx(sd)
    assert aggregate["first_order_se"] == pytest.approx(se)
    assert aggregate["first_order_ci_low"] == pytest.approx(mean - critical * se)
    assert aggregate["first_order_ci_high"] == pytest.approx(mean + critical * se)


def test_multiscramble_indices_are_invariant_for_low_amplitude_outputs(
    tmp_path: Path,
) -> None:
    design = multiscramble_design(tmp_path / "multiscramble-low-amplitude")
    build_plan(design)
    plan = pq.read_table(design.campaign_root / "plan.parquet")
    rows = [
        (
            run_id,
            "synthetic(age)",
            float(row["a"] + row["seed"] / 10 + row["a"] * row["seed"] / 20),
        )
        for run_id, row in {row["runId"]: row for row in plan.to_pylist()}.items()
    ]

    baseline = _multiscramble_sobol_tables(plan, rows, design.confidence_level)
    scaled = _multiscramble_sobol_tables(
        plan,
        [(run_id, indicator, value * 1e-12) for run_id, indicator, value in rows],
        design.confidence_level,
    )

    for baseline_table, scaled_table in zip(baseline[:3], scaled[:3]):
        for column in baseline_table.column_names:
            if pa.types.is_floating(baseline_table.schema.field(column).type):
                if column == "output_variance":
                    assert scaled_table[column].to_pylist() == pytest.approx(
                        [value * 1e-24 for value in baseline_table[column].to_pylist()]
                    )
                    continue
                assert scaled_table[column].to_pylist() == pytest.approx(
                    baseline_table[column].to_pylist(), nan_ok=True
                )


def test_multiscramble_output_variance_handles_float64_limits_explicitly() -> None:
    assert _finite_population_variance(
        np.asarray([0.0, 0.0, 2.0]), "asymmetric"
    ) == pytest.approx(8 / 9)
    assert _finite_population_variance(
        np.asarray([-1e150, 1e150]), "large"
    ) == pytest.approx(1e300)

    with pytest.raises(ValueError, match="output variance exceeds float64 range"):
        _finite_population_variance(np.asarray([-1e308, 1e308]), "extreme")

    with pytest.raises(ValueError, match="output variance is below float64 range"):
        _finite_population_variance(np.asarray([-1e-162, 1e-162]), "subnormal")


def test_multiscramble_table_cell_cap_precedes_analyzer_salib_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import eqasim_sa.core.analyze as analyze_module

    design = multiscramble_design(tmp_path / "multiscramble")
    build_plan(design)
    plan = pq.read_table(design.campaign_root / "plan.parquet")
    metadata = dict(plan.schema.metadata)
    sealed = json.loads(metadata[b"multiScrambleSobolDesign"])
    sealed["baseSampleSizes"] = [2]
    sealed["maximumBaseSampleSize"] = 2
    sealed["numericalEngine"] = engine_descriptor(
        "multiscramble_sobol",
        sample_size=2,
        sample_seeds=sealed["scrambleSeeds"],
    )
    sealed["factors"] = [
        {
            "name": f"f{index:05d}",
            "support": [0, 1],
            "distribution": {
                "family": "discrete_uniform",
                "transform": "identity",
                "provenance": "test",
            },
        }
        for index in range(10_599)
    ] + [sealed["factors"][-1]]
    metadata[b"multiScrambleSobolDesign"] = json.dumps(
        sealed, sort_keys=True, separators=(",", ":")
    ).encode()
    damaged = plan.replace_schema_metadata(metadata)
    monkeypatch.setattr(
        analyze_module,
        "sobol_unit_positions",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("SALib sample allocated")
        ),
    )
    with pytest.raises(ValueError, match="table cells"):
        _multiscramble_sobol_tables(damaged, [], design.confidence_level)


def test_multiscramble_ff_classifies_only_the_primary_indicator(tmp_path: Path) -> None:
    original = multiscramble_design(tmp_path / "unused")
    contract = copy.deepcopy(original.scientific_contract)
    contract["charter"]["decision"].update(objective="ff", practical_tolerance=1.0)
    design = replace(original, campaign_root=tmp_path / "ff", scientific_contract=contract)
    build_plan(design)
    plan = pq.read_table(design.campaign_root / "plan.parquet")
    unique = {row["runId"]: row for row in plan.to_pylist()}
    primary_rows = [
        (run_id, "synthetic(age)", float(row["a"] + row["seed"] / 10 + row["a"] * row["seed"] / 20))
        for run_id, row in unique.items()
    ]
    rows = primary_rows + [
        (run_id, "diagnostic", value + 0.5) for run_id, _, value in primary_rows
    ]
    indices, _, convergence, _ = _multiscramble_sobol_tables(
        plan, rows, design.confidence_level
    )
    final = {
        (row["indicator"], row["factor"]): row for row in indices.to_pylist()
    }
    assert all(row["objective"] == "ff" for row in convergence.to_pylist())
    assert final["synthetic(age)", "a"]["within_practical_tolerance"] == (
        final["synthetic(age)", "a"]["total_order_ci_high"] <= 1.0
    )
    assert final["synthetic(age)", "seed"]["within_practical_tolerance"] is None
    assert final["synthetic(age)", "seed"]["rank"] is None
    assert final["synthetic(age)", "seed"]["is_actionable"] is False
    assert final["synthetic(age)", "a"]["is_actionable"] is True
    assert all(
        final["diagnostic", factor]["within_practical_tolerance"] is None
        for factor in ("a", "seed")
    )
    assert all(
        row["is_primary_indicator"] == (row["indicator"] == "synthetic(age)")
        for row in convergence.to_pylist()
    )
    assert not indices.schema.field("is_primary_indicator").nullable
    assert not convergence.schema.field("is_primary_indicator").nullable
    primary = [
        row
        for row in final.values()
        if row["indicator"] == "synthetic(age)" and row["is_actionable"]
    ]
    ordered = sorted(primary, key=lambda row: (row["total_order_ci_high"], row["factor"]))
    assert [row["rank"] for row in ordered] == sorted(row["rank"] for row in ordered)


def test_multiscramble_analyzer_rejects_tampered_ff_tolerance_above_one(
    tmp_path: Path,
) -> None:
    design = multiscramble_design(tmp_path / "multiscramble")
    build_plan(design)
    plan = pq.read_table(design.campaign_root / "plan.parquet")
    metadata = dict(plan.schema.metadata)
    sealed = json.loads(metadata[b"multiScrambleSobolDesign"])
    sealed["scientificContract"]["charter"]["decision"].update(
        objective="ff", practical_tolerance=1.01
    )
    metadata[b"multiScrambleSobolDesign"] = json.dumps(
        sealed, sort_keys=True, separators=(",", ":")
    ).encode()

    with pytest.raises(ValueError, match=r"variance-share tolerance in \[0,1\]"):
        _multiscramble_sobol_tables(
            plan.replace_schema_metadata(metadata), [], design.confidence_level
        )


def test_multiscramble_transactionally_publishes_outputs_and_manifest_v7(
    tmp_path: Path,
) -> None:
    design = multiscramble_design(tmp_path / "published")
    build_plan(design)
    plan = pq.read_table(design.campaign_root / "plan.parquet")
    unique = {row["runId"]: row for row in plan.to_pylist()}
    for run_id, row in unique.items():
        value = float(row["a"] + row["seed"] / 10 + row["a"] * row["seed"] / 20)
        write_parquet_atomic(
            pa.table({"synthetic": [value]}),
            design.campaign_root / "archives" / run_id / "age.parquet",
        )
        _write_verified_run(
            design.campaign_root,
            RunRecord(
                run_id, "test", row["seed"], design.context_hash, "node", 1.0,
                {"a": row["a"]},
            ),
            execution_evidence(design.context_hash),
        )
    result = analyze(
        design.campaign_root,
        (_scientific_indicator(),),
        campaign="test",
        completeness_threshold=1.0,
        seed_role="factor",
        method="multiscramble_sobol",
        confidence_level=design.confidence_level,
    )
    assert result.sensitivity_path.name == "multiscramble-sobol-indices.parquet"
    assert result.by_scramble_path.name == "multiscramble-sobol-by-scramble.parquet"
    assert result.convergence_path.name == "multiscramble-sobol-convergence.parquet"
    assert result.diagnostics_path.name == "multiscramble-sobol-diagnostics.parquet"
    manifest = json.loads((design.campaign_root / "analysis-generation.json").read_text())
    assert manifest["version"] == 7
    assert root_relative(design.campaign_root, result.by_scramble_path) == manifest[
        "byScramblePath"
    ]
    assert root_relative(design.campaign_root, result.diagnostics_path) == manifest[
        "diagnosticsPath"
    ]


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

    assert result.path.name == "indicators.parquet"
    assert result.path.parent.parent.name == "generations"
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
    rebuilt = _analyze(root, (_custom_indicator(),))
    assert rebuilt.path.read_bytes() == first
    assert rebuilt.path == result.path
    replayed = _analyze(root, (_custom_indicator(),))
    assert replayed.path.read_bytes() == first
    assert _snapshot(root) == before


def test_analyze_rejects_foreign_analysis_directory_without_modification(
    tmp_path: Path,
) -> None:
    root, _ = _campaign(tmp_path)
    state = root / "analysis"
    state.mkdir()
    note = state / "user-notes.txt"
    note.write_text("do not adopt\n")
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="unexpected root content"):
        _analyze(root, (_custom_indicator(),))

    assert _tree_snapshot(root) == before
    assert note.read_text() == "do not adopt\n"
    assert not (root / "analysis-generation.json").exists()


def test_analyze_rejects_nonempty_analysis_staging_before_creating_generations(
    tmp_path: Path,
) -> None:
    root, _ = _campaign(tmp_path)
    staging = root / "analysis/staging"
    staging.mkdir(parents=True)
    (staging / "user-work.txt").write_text("do not adopt\n")
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="staging must be empty"):
        _analyze(root, (_custom_indicator(),))

    assert _tree_snapshot(root) == before
    assert not (root / "analysis/generations").exists()


def test_analyze_preflights_existing_analysis_children_before_creating_missing_ones(
    tmp_path: Path,
) -> None:
    root, _ = _campaign(tmp_path)
    state = root / "analysis"
    state.mkdir()
    (state / "generations").write_text("not a directory\n")
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="regular non-symlink directory"):
        _analyze(root, (_custom_indicator(),))

    assert _tree_snapshot(root) == before
    assert not (state / "staging").exists()


def test_analyze_rejects_foreign_generation_inventory_before_creating_staging(
    tmp_path: Path,
) -> None:
    root, _ = _campaign(tmp_path)
    foreign = root / "analysis/generations/user-data"
    foreign.mkdir(parents=True)
    (foreign / "notes.txt").write_text("do not adopt\n")
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="generations contain unexpected content"):
        _analyze(root, (_custom_indicator(),))

    assert _tree_snapshot(root) == before
    assert not (root / "analysis/staging").exists()


def test_manifest_v7_seals_immutable_outputs_and_complete_provenance(
    tmp_path: Path,
) -> None:
    root, _ = _campaign(tmp_path)
    indicator = RegisteredIndicator(
        "custom",
        "age",
        _artifact_value,
        PluginIdentity(
            "example_plugin.indicators:compute",
            "eqasim_sa.indicators:custom=example_plugin.indicators:compute",
            "example-plugin",
            "1.2.3",
        ),
    )

    result = _analyze(root, (indicator,), meta_config_sha256="a" * 64)
    manifest_path = root / "analysis-generation.json"
    manifest = json.loads(manifest_path.read_text())

    assert manifest["version"] == 7
    assert manifest["layout"] == "immutable-generation-v1"
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["generationId"])
    assert manifest["generationPath"] == (
        f"analysis/generations/{manifest['generationId']}"
    )
    assert ".eqasim-sa-analysis/" not in json.dumps(manifest)
    assert result.path.parent == root / manifest["generationPath"]
    assert set(manifest["outputs"]) == {
        "capturedRunSet",
        "dispersion",
        "governedReport",
        "indicators",
        "outputSummary",
    }
    for record in manifest["outputs"].values():
        relative = Path(record["path"])
        assert not relative.is_absolute() and ".." not in relative.parts
        output = root / relative
        assert output.parent == result.path.parent
        assert output.stat().st_size == record["size"]
        assert hashlib.sha256(output.read_bytes()).hexdigest() == record["sha256"]
    provenance = manifest["provenance"]
    assert provenance["metaConfigSha256"] == "a" * 64
    assert provenance["plan"]["sha256"] == hashlib.sha256(
        (root / "plan.parquet").read_bytes()
    ).hexdigest()
    assert provenance["plan"]["contextDescriptor"] is None
    assert re.fullmatch(r"[0-9a-f]{64}", provenance["capturedRunSetSha256"])
    assert set(provenance["dependencies"]) == {
        "SALib",
        "eqasim-sa",
        "eqasim-sa-source",
        "numpy",
        "pyarrow",
        "python",
        "scipy",
    }
    assert provenance["indicators"] == [
        {
            "schemaVersion": 1,
            "name": "custom(age)",
            "kind": "custom",
            "scalarizer": {"name": "custom", "version": 1},
            "artifact": {
                "name": "age",
                "classColumn": None,
                "syntheticColumn": None,
                "referenceColumns": [],
                "producingStage": None,
            },
            "implementation": {
                "identity": "example_plugin.indicators:compute",
                "sha256": None,
                "version": 1,
            },
            "plugin": {
                "entryPoint": (
                    "eqasim_sa.indicators:custom="
                    "example_plugin.indicators:compute"
                ),
                "distribution": {
                    "name": "example-plugin",
                    "version": "1.2.3",
                    "sha256": None,
                },
            },
        }
    ]
    expected_provenance_hash = hashlib.sha256(
        b"eqasim-sa:analysis-provenance:v1\0"
        + json.dumps(
            provenance,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()
    assert manifest["provenanceSha256"] == expected_provenance_hash
    assert manifest["indicatorsPath"] == root_relative(root, result.path)
    assert not (root / "indicators.parquet").exists()


def test_additional_completion_after_capture_is_ignored_until_next_generation(
    tmp_path: Path,
) -> None:
    root, _ = _campaign(tmp_path)
    entered, release = threading.Event(), threading.Event()
    results, errors = [], []

    def wait(artifacts: ArtifactSet) -> float:
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return _artifact_value(artifacts)

    def run() -> None:
        try:
            results.append(
                _analyze(root, (RegisteredIndicator("wait", "age", wait),))
            )
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(5)
    added = "3" * 32
    _write_verified_run(
        root, RunRecord(added, "test", 1, "hash", "node", 1.0, {})
    )
    release.set()
    thread.join(5)

    assert not thread.is_alive() and errors == []
    captured = results[0]
    assert captured.completed == captured.runs == 2
    assert set(pq.read_table(captured.path)["runId"].to_pylist()) == {
        "1" * 32,
        "2" * 32,
    }
    replay = _analyze(root, (RegisteredIndicator("wait", "age", wait),))
    assert replay.completed == replay.runs == 3
    assert replay.path != captured.path


def test_selected_archive_mutation_aborts_without_changing_current_generation(
    tmp_path: Path,
) -> None:
    root, run_ids = _campaign(tmp_path)
    current = _analyze(root, (_custom_indicator(),))
    pointer_before = (root / "analysis-generation.json").read_bytes()
    output_before = current.path.read_bytes()
    entered, release = threading.Event(), threading.Event()
    errors = []

    def wait(artifacts: ArtifactSet) -> float:
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return _artifact_value(artifacts)

    def run() -> None:
        try:
            _analyze(root, (RegisteredIndicator("changed", "age", wait),))
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(5)
    source = root / "archives" / run_ids[0] / "age.parquet"
    source.write_bytes(b"mutated source")
    release.set()
    thread.join(5)

    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
    assert "selected analysis source" in str(errors[0])
    assert source.read_bytes() == b"mutated source"
    assert (root / "analysis-generation.json").read_bytes() == pointer_before
    assert current.path.read_bytes() == output_before

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
            _write_verified_run(
                root,
                RunRecord(run_id, "test", seed, "hash", "node", 1.0, {"rate": rate}),
            )
    before = _snapshot(root)

    result = _analyze(root, (_custom_indicator(),))

    dispersion = result.dispersion_path
    assert dispersion.name == "replication-dispersion.parquet"
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

    ranking = result.sensitivity_path
    assert result.sensitivity_method == "morris"
    assert ranking.name == "morris-ranking.parquet"
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
    assert manifest["version"] == 7
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
    before = _tree_snapshot(root)
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
    import eqasim_sa.core.analysis_publication as publication_module

    root = _morris_campaign(tmp_path)
    outputs = {
        root / "indicators.parquet": b"old indicators",
        root / "replication-dispersion.parquet": b"old dispersion",
        root / "morris-ranking.parquet": b"old ranking",
        root / "analysis-generation.json": b"old manifest",
    }
    for path, content in outputs.items():
        path.write_bytes(content)
    real_write = publication_module.write_parquet_atomic

    def fail_ranking(
        table: pa.Table, destination: Path, *, staging_directory: Path | None = None
    ) -> Path:
        if destination.name == "morris-ranking.parquet":
            raise OSError("injected ranking publication failure")
        return real_write(table, destination, staging_directory=staging_directory)

    monkeypatch.setattr(publication_module, "write_parquet_atomic", fail_ranking)
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
    arbitrary = root / "research-notes.txt"
    arbitrary.write_text("keep\n")

    _analyze(root, (_custom_indicator(),))

    assert not stale.exists()
    assert not stale_sobol.exists()
    assert arbitrary.read_text() == "keep\n"
    manifest = json.loads((root / "analysis-generation.json").read_text())
    assert manifest["sensitivityMethod"] is None
    assert manifest["sensitivitySha256"] is None


def test_manifest_failure_keeps_previous_pointer_and_leaves_only_immutable_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import eqasim_sa.core.analysis_publication as publication_module
    root, _ = _campaign(tmp_path)
    current = _analyze(root, (_custom_indicator(),))
    pointer = (root / "analysis-generation.json").read_bytes()
    stale = root / "morris-ranking.parquet"
    stale.write_bytes(b"stale ranking")
    monkeypatch.setattr(
        publication_module,
        "replace_bytes_restorable",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("injected manifest failure")
        ),
    )

    with pytest.raises(OSError, match="manifest"):
        _analyze(
            root,
            (RegisteredIndicator("alternate", "age", _artifact_value),),
        )
    assert (root / "analysis-generation.json").read_bytes() == pointer
    assert current.path.is_file()
    assert stale.read_bytes() == b"stale ranking"
    assert not any((root / "analysis/staging").iterdir())
    assert len(tuple((root / "analysis/generations").iterdir())) == 2


def test_manifest_post_replace_fsync_failure_restores_previous_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import eqasim_sa.core.durable_publication as durable_module

    root, _ = _campaign(tmp_path)
    _analyze(root, (_custom_indicator(),))
    pointer = root / "analysis-generation.json"
    before = pointer.read_bytes()
    real_fsync = durable_module.fsync_directory
    failed = False

    def fail_new_pointer(path: Path) -> None:
        nonlocal failed
        if path == root and pointer.read_bytes() != before and not failed:
            failed = True
            raise OSError("injected post-replace fsync failure")
        real_fsync(path)

    monkeypatch.setattr(durable_module, "fsync_directory", fail_new_pointer)

    with pytest.raises(OSError, match="post-replace"):
        _analyze(
            root,
            (RegisteredIndicator("alternate", "age", _artifact_value),),
        )

    assert failed
    assert pointer.read_bytes() == before


def test_manifest_post_replace_fsync_failure_removes_new_pointer_when_none_existed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import eqasim_sa.core.durable_publication as durable_module

    root, _ = _campaign(tmp_path)
    pointer = root / "analysis-generation.json"
    real_fsync = durable_module.fsync_directory
    failed = False

    def fail_new_pointer(path: Path) -> None:
        nonlocal failed
        if path == root and pointer.exists() and not failed:
            failed = True
            raise OSError("injected post-replace fsync failure")
        real_fsync(path)

    monkeypatch.setattr(durable_module, "fsync_directory", fail_new_pointer)

    with pytest.raises(OSError, match="post-replace"):
        _analyze(root, (_custom_indicator(),))

    assert failed and not pointer.exists()


def test_manifest_failed_rollback_raises_distinct_outcome_unknown_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import eqasim_sa.core.analysis_publication as publication_module
    import eqasim_sa.core.durable_publication as durable_module

    root, _ = _campaign(tmp_path)
    _analyze(root, (_custom_indicator(),))
    pointer = root / "analysis-generation.json"
    before = pointer.read_bytes()
    real_fsync = durable_module.fsync_directory
    real_replace = durable_module.os.replace
    fsync_failed = False

    def fail_new_pointer(path: Path) -> None:
        nonlocal fsync_failed
        if path == root and pointer.read_bytes() != before and not fsync_failed:
            fsync_failed = True
            raise OSError("injected post-replace fsync failure")
        real_fsync(path)

    def fail_rollback(source, destination, **options):  # type: ignore[no-untyped-def]
        if str(source).endswith(".backup"):
            raise OSError("injected rollback failure")
        return real_replace(source, destination, **options)

    monkeypatch.setattr(durable_module, "fsync_directory", fail_new_pointer)
    monkeypatch.setattr(durable_module.os, "replace", fail_rollback)

    with pytest.raises(publication_module.AnalysisManifestOutcomeUnknown):
        _analyze(
            root,
            (RegisteredIndicator("alternate", "age", _artifact_value),),
        )

    assert fsync_failed
    assert tuple((root / "analysis/staging").glob("*.backup"))


def test_legacy_cleanup_failure_does_not_fail_committed_analysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import eqasim_sa.core.analysis_publication as publication_module

    root, _ = _campaign(tmp_path)
    monkeypatch.setattr(
        publication_module,
        "_clean_legacy_outputs",
        lambda path: (_ for _ in ()).throw(OSError("injected cleanup failure")),
    )

    result = _analyze(root, (_custom_indicator(),))

    manifest = json.loads((root / "analysis-generation.json").read_text())
    assert result.path == root / manifest["outputs"]["indicators"]["path"]


def test_generation_rename_failure_keeps_previous_pointer_and_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = _campaign(tmp_path)
    current = _analyze(root, (_custom_indicator(),))
    pointer = (root / "analysis-generation.json").read_bytes()
    real_rename = Path.rename

    def fail_generation_rename(source: Path, destination: Path) -> Path:
        if source.name == "generation":
            raise OSError("injected generation rename failure")
        return real_rename(source, destination)

    monkeypatch.setattr(Path, "rename", fail_generation_rename)
    with pytest.raises(OSError, match="generation rename"):
        _analyze(
            root,
            (RegisteredIndicator("alternate", "age", _artifact_value),),
        )

    assert (root / "analysis-generation.json").read_bytes() == pointer
    assert current.path.is_file()
    assert not any((root / "analysis/staging").iterdir())
    assert tuple((root / "analysis/generations").iterdir()) == (
        current.path.parent,
    )


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


def test_analyze_keeps_previous_commit_if_dispersion_writer_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import eqasim_sa.core.analysis_publication as publication_module

    root, _ = _campaign(tmp_path)
    indicators = root / "indicators.parquet"
    dispersion = root / "replication-dispersion.parquet"
    indicators.write_bytes(b"old indicators")
    dispersion.write_bytes(b"old dispersion")
    real_write = publication_module.write_parquet_atomic

    def fail_second(
        table: pa.Table, destination: Path, *, staging_directory: Path | None = None
    ) -> Path:
        if destination.name == dispersion.name:
            raise OSError("injected dispersion publication failure")
        return real_write(table, destination, staging_directory=staging_directory)

    monkeypatch.setattr(publication_module, "write_parquet_atomic", fail_second)
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
        match=rf"invalid completion marker.*{run_ids[0]}.*artifact manifest",
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


def test_adapter_preflights_current_plan_and_uses_committed_generation_paths(
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
    design, validated = object(), []
    monkeypatch.setattr("eqasim_sa.adapters.application._design", lambda current: design)
    monkeypatch.setattr(
        "eqasim_sa.adapters.application.load_run_specs",
        lambda *args: validated.append(args),
    )
    payload = analyze_from_meta_config(tmp_path / "changed-meta.yml")
    assert payload["values"] == 2
    assert payload["dispersionPath"].endswith("/replication-dispersion.parquet")
    assert "/analysis/generations/" in payload["dispersionPath"]
    assert payload["sensitivityMethod"] is None and payload["sensitivityPath"] is None
    assert payload | {} == {
        **payload,
        "completionRate": 2 / 3,
        "expected": 3,
        "completed": 2,
        "missing": 1,
        "failed": 0,
    }
    assert validated == [(root / "plan.parquet", design, "test")]
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


def test_analyze_rejects_legacy_unbound_completion_marker(tmp_path: Path) -> None:
    root, run_ids = _campaign(tmp_path)
    write_run(root, RunRecord(run_ids[0], "test", 1, "hash", "node", 1.0, {}))

    with pytest.raises(ValueError, match="verified v2 completion marker"):
        _analyze(root, (_custom_indicator(),))

    assert not (root / "analysis-generation.json").exists()


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

    _write_verified_run(
        root, RunRecord(run_ids[0], "test", 1, "hash", "node", 1.0, {})
    )
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


def test_custom_receives_read_only_copy_and_source_mutation_aborts_without_restore(
    tmp_path: Path,
) -> None:
    root, _ = _campaign(tmp_path)
    output = root / "indicators.parquet"
    output.write_bytes(b"previous")
    with pytest.raises(RuntimeError, match="selected analysis source changed"):
        _analyze(root, (_mutating_indicator(root),))
    assert (root / "plan.parquet").read_bytes() == b"damaged plan"
    assert output.read_bytes() == b"damaged previous"
    assert (root / "runs" / ("f" * 32 + ".parquet")).read_bytes() == b"foreign"
    assert not (root / "analysis-generation.json").exists()


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
    duplicate = plan.slice(0, 1).set_column(
        0, "designId", pa.array(["a" * 32])
    )
    write_parquet_atomic(
        pa.concat_tables((plan, duplicate)), root / "plan.parquet"
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
    assert result.inference_status == "incomplete_noninferential"
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
    before = _tree_snapshot(root)

    def forbidden(artifacts: ArtifactSet) -> float:
        raise AssertionError("plugin called")

    with pytest.raises(ValueError, match=r"missing=2.*failed=0"):
        analyze(
            root,
            (RegisteredIndicator("forbidden", "age", forbidden),),
            campaign="test",
            completeness_threshold=0.5,
        )
    assert _tree_snapshot(root) == before
    assert not (root / "analysis").exists()
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
        "convergencePath": None,
        "rankReversalsPath": None,
        "byScramblePath": None,
            "diagnosticsPath": None,
            "noiseFloorPath": None,
            "replicationAllocationPath": None,
            "outputSummaryPath": None,
            "convergenceEvidencePath": None,
            "governedReportPath": None,
            "inferenceStatus": None,
        "error": "missing age",
    }


def test_cli_analyze_passes_descriptive_only_explicitly(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "eqasim-sa",
            "analyze",
            "--meta-config",
            "meta.yml",
            "--descriptive-only",
        ],
    )
    seen = {}

    def factory(path: Path, *, descriptive_only: bool = False) -> dict:
        seen.update(path=path, descriptive_only=descriptive_only)
        return {"status": "completed"}

    assert dispatch(None, None, analyze_factory=factory) == 0
    assert seen == {"path": Path("meta.yml"), "descriptive_only": True}
    assert json.loads(capsys.readouterr().out) == {"status": "completed"}


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
        "convergencePath": None,
        "rankReversalsPath": None,
        "byScramblePath": None,
            "diagnosticsPath": None,
            "noiseFloorPath": None,
            "replicationAllocationPath": None,
            "outputSummaryPath": None,
            "convergenceEvidencePath": None,
            "governedReportPath": None,
            "inferenceStatus": None,
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
    monkeypatch.setattr("eqasim_sa.adapters.application._design", lambda current: object())
    monkeypatch.setattr(
        "eqasim_sa.adapters.application.load_run_specs", lambda *args: ()
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


@pytest.mark.parametrize(
    ("method", "fixture_name"),
    [
        ("canonical_morris", "canonical-morris-v1-706f0bc.parquet"),
        ("multiscramble_sobol", "multiscramble-sobol-v1-706f0bc.parquet"),
    ],
)
def test_analyze_adapter_routes_legacy_plan_without_current_design_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    fixture_name: str,
) -> None:
    root = tmp_path / "campaign"
    root.mkdir()
    (root / "plan.parquet").write_bytes(
        Path(__file__).with_name(fixture_name).read_bytes()
    )
    meta = SimpleNamespace(
        campaign_root=root,
        indicators=(),
        campaign="test",
        completeness_threshold=1.0,
        seed_role="replication" if method == "canonical_morris" else "factor",
        method=method,
    )
    result = SimpleNamespace(
        path=root / "indicators.parquet",
        dispersion_path=None,
        sensitivity_method=None,
        sensitivity_path=None,
        runs=0,
        indicators=0,
        values=0,
        completion_rate=0.0,
        expected=1,
        completed=0,
        missing=1,
        failed=0,
    )
    seen = {}
    monkeypatch.setattr(
        "eqasim_sa.adapters.application.load_meta_config", lambda path: meta
    )
    monkeypatch.setattr(
        "eqasim_sa.adapters.application._design",
        lambda current: (_ for _ in ()).throw(
            AssertionError("legacy adapter invoked current design")
        ),
    )
    monkeypatch.setattr(
        "eqasim_sa.adapters.application.load_run_specs",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("legacy adapter invoked current plan validation")
        ),
    )
    monkeypatch.setattr(
        "eqasim_sa.adapters.application.analyze",
        lambda *args, **kwargs: seen.update(kwargs) or result,
    )

    payload = analyze_from_meta_config(Path("meta.yml"), descriptive_only=True)

    assert seen["descriptive_only"] is True
    assert seen["method"] == method
    assert payload["sensitivityMethod"] is None


def test_analyze_adapter_refuses_legacy_inference_before_loading_plugins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "campaign"
    root.mkdir()
    (root / "plan.parquet").write_bytes(
        Path(__file__).with_name("canonical-morris-v1-706f0bc.parquet").read_bytes()
    )
    meta_path = tmp_path / "meta.yml"
    meta_path.write_text(
        f"campaign_root: {root}\nexperiment:\n  method: canonical_morris\n"
    )
    monkeypatch.setattr(
        "eqasim_sa.adapters.application.load_meta_config",
        lambda path: (_ for _ in ()).throw(AssertionError("plugins were loaded")),
    )

    with pytest.raises(ValueError, match="legacy.*replan.*SALib.*v3"):
        analyze_from_meta_config(meta_path)


def test_analyze_adapter_rejects_legacy_method_mismatch_before_loading_plugins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "campaign"
    root.mkdir()
    (root / "plan.parquet").write_bytes(
        Path(__file__).with_name("canonical-morris-v1-706f0bc.parquet").read_bytes()
    )
    meta_path = tmp_path / "meta.yml"
    meta_path.write_text(
        f"campaign_root: {root}\nexperiment:\n  method: multiscramble_sobol\n"
    )
    monkeypatch.setattr(
        "eqasim_sa.adapters.application.load_meta_config",
        lambda path: (_ for _ in ()).throw(AssertionError("plugins were loaded")),
    )

    with pytest.raises(ValueError, match="does not match declared method"):
        analyze_from_meta_config(meta_path, descriptive_only=True)


def test_analyze_adapter_requires_boolean_descriptive_mode() -> None:
    with pytest.raises(TypeError, match="descriptive_only must be a bool"):
        analyze_from_meta_config(Path("meta.yml"), descriptive_only=1)  # type: ignore[arg-type]


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
    monkeypatch.setattr("eqasim_sa.adapters.application._design", lambda current: object())
    monkeypatch.setattr(
        "eqasim_sa.adapters.application.load_run_specs", lambda *args: ()
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
