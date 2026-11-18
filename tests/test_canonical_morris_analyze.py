import json
import math
import statistics
import copy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml
import numpy as np

from eqasim_sa.adapters.application import analyze_from_meta_config, plan_from_meta_config
from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.core.analyze import analyze
from eqasim_sa.core.methods.canonical_morris import (
    _canonical_morris_tables,
    _replication_allocation,
)
from eqasim_sa.utils.filesystem import write_parquet_atomic
from eqasim_sa.core.ledger import RunRecord
from eqasim_sa.core.plan import build_plan
from eqasim_sa.core.sensitivity import morris_statistics
from tests.test_analyze import (
    _campaign,
    _age_indicator,
    _write_verified_run,
    root_relative,
)
from tests.test_canonical_morris_plan import canonical_design, canonical_meta, discrete_design
from tests.provenance_helpers import (
    context_hash,
    execution_evidence,
    scientific_context_descriptor,
    scientific_indicator,
)

# Canonical Morris and discrete OAT tests feed arbitrary signed indicator
# values through real archives: tv_distance returns their synthetic cell.
pytestmark = pytest.mark.usefixtures("synthetic_indicator")

VALUE = "tv_distance(value, ref=reference)"


def _descriptor() -> tuple[dict[str, str], str]:
    descriptor = scientific_context_descriptor()
    return descriptor, context_hash(descriptor)


def _design(root: Path, **changes):  # type: ignore[no-untyped-def]
    descriptor, context_hash = _descriptor()
    return replace(
        canonical_design(root, **changes),
        context_descriptor=descriptor,
        context_hash=context_hash,
    )


def _rows(table: pa.Table) -> list[tuple[str, str, float]]:
    blocks = {}
    for row in table.to_pylist():
        trajectory = int(row["role"].split(":")[1])
        blocks.setdefault(trajectory, [])
        if row["seed"] not in blocks[trajectory]:
            blocks[trajectory].append(row["seed"])
    category = {"low": 0.0, "middle": 1.0, "high": 2.0}
    values = {}
    for row in table.to_pylist():
        trajectory = int(row["role"].split(":")[1])
        replication = blocks[trajectory].index(row["seed"])
        value = (2.0 + 2.0 * replication) * row["a"] + 3.0 * category[row["b"]]
        values.setdefault(row["runId"], value)
        assert values[row["runId"]] == value
    return [(run_id, VALUE, value) for run_id, value in values.items()]


def _tables(tmp_path: Path, **changes):  # type: ignore[no-untyped-def]
    design = _design(tmp_path / "campaign", **changes)
    build_plan(design)
    table = pq.read_table(design.campaign_root / "plan.parquet")
    return design, table, _rows(table)


def _analyze_tables(design, table, rows):  # type: ignore[no-untyped-def]
    return _canonical_morris_tables(
        table,
        rows,
        design.bootstrap_resamples,
        design.confidence_level,
        design.convergence_checkpoints,
        design.screening_top_k,
    )


def test_canonical_point_statistics_use_salib_with_replication_mean_outputs(
    tmp_path: Path,
) -> None:
    design, table, rows = _tables(tmp_path)
    ranking, convergence, reversals = _analyze_tables(design, table, rows)

    assert ranking.schema == pa.schema(
        [
            pa.field("indicator", pa.string(), False),
            pa.field("factor", pa.string(), False),
            pa.field("rank", pa.float64(), False),
            pa.field("rank_ci_low", pa.float64(), False),
            pa.field("rank_ci_high", pa.float64(), False),
            pa.field("top_k_probability", pa.float64(), False),
            pa.field("trajectories", pa.int64(), False),
            pa.field("replications", pa.int64(), False),
            pa.field("elementary_effects", pa.int64(), False),
            pa.field("delta", pa.float64(), False),
            pa.field("mu", pa.float64(), False),
            pa.field("mu_star", pa.float64(), False),
            pa.field("mu_star_ci_low", pa.float64(), False),
            pa.field("mu_star_ci_high", pa.float64(), False),
            pa.field("sigma", pa.float64()),
            pa.field("seed_std", pa.float64()),
            pa.field("bootstrap_resamples", pa.int64(), False),
            pa.field("confidence_level", pa.float64(), False),
        ]
    )
    result = {row["factor"]: row for row in ranking.to_pylist()}
    observations = {run_id: value for run_id, _, value in rows}
    point_values = {}
    for location in table.to_pylist():
        trajectory = int(location["role"].split(":")[1])
        rank = int(location["role"].split(":")[3])
        point_values.setdefault((trajectory, rank), []).append(
            observations[location["runId"]]
        )
    sealed = json.loads(table.schema.metadata[b"canonicalMorrisDesign"])
    matrices = [item["matrix"] for item in sealed["selectedTrajectories"]]
    unit_inputs = np.asarray(matrices, dtype=np.float64).reshape(-1, 2) / 3
    mean_outputs = np.asarray(
        [
            [statistics.fmean(point_values[trajectory, rank]) for rank in range(3)]
            for trajectory in range(3)
        ]
    ).reshape(-1)
    expected = morris_statistics(
        ("a", "b"), unit_inputs, mean_outputs, levels=4
    )
    for factor in ("a", "b"):
        assert (
            result[factor]["mu"],
            result[factor]["mu_star"],
            result[factor]["sigma"],
        ) == pytest.approx(expected[factor])
    assert result["a"]["delta"] == pytest.approx(2 / 3)
    assert result["a"]["mu"] == pytest.approx(45.0)
    assert result["a"]["mu_star"] == pytest.approx(45.0)
    assert result["a"]["seed_std"] == pytest.approx(30 / 2**0.5)
    assert result["a"]["sigma"] == pytest.approx(0.0)
    assert result["a"]["elementary_effects"] == 6
    assert set(convergence["checkpoint_trajectories"].to_pylist()) == {2, 3}
    assert reversals.schema.field("point_order").type == pa.int64()
    assert reversals.schema.field("reversal_probability").nullable
    assert not reversals.schema.field("tie_probability").nullable


def test_canonical_bootstrap_replays_and_is_independent_of_input_order(
    tmp_path: Path,
) -> None:
    design, table, rows = _tables(tmp_path)
    expected = _analyze_tables(design, table, rows)
    shuffled = table.take(pa.array(list(reversed(range(len(table))))))
    replay = _analyze_tables(design, shuffled, list(reversed(rows)))
    assert all(left.equals(right) for left, right in zip(expected, replay))
    ranking, convergence, reversals = expected
    assert all(0 <= value <= 1 for value in ranking["top_k_probability"].to_pylist())
    assert all(row["rank_ci_low"] <= row["rank"] <= row["rank_ci_high"] for row in convergence.to_pylist())
    assert all(0 <= value <= 1 for value in reversals["reversal_probability"].to_pylist())


def test_canonical_rejects_one_replication_below_scientific_minimum(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="minimum_replications"):
        _tables(
            tmp_path,
            trajectories=2,
            seeds=(41,),
            convergence_checkpoints=(2,),
        )


def test_canonical_exact_ties_use_average_ranks_fractional_top_k_and_tie_probability(
    tmp_path: Path,
) -> None:
    design = _design(
        tmp_path / "campaign",
        factors={"a": [0.0, 1.0], "b": [0.0, 1.0]},
        screening_top_k=1,
    )
    build_plan(design)
    table = pq.read_table(design.campaign_root / "plan.parquet")
    values = {
        row["runId"]: row["a"] + row["b"] for row in table.to_pylist()
    }
    rows = [(run_id, "tie", value) for run_id, value in values.items()]
    ranking, _, reversals = _analyze_tables(design, table, rows)

    assert ranking["rank"].to_pylist() == [1.5, 1.5]
    assert ranking["rank_ci_low"].to_pylist() == [1.5, 1.5]
    assert ranking["rank_ci_high"].to_pylist() == [1.5, 1.5]
    assert ranking["top_k_probability"].to_pylist() == [0.5, 0.5]
    final_pair = next(
        row
        for row in reversals.to_pylist()
        if row["checkpoint_trajectories"] == design.trajectories
    )
    assert final_pair["point_order"] == 0
    assert final_pair["reversal_probability"] is None
    assert final_pair["tie_probability"] == 1.0


@pytest.mark.parametrize(
    "damage",
    [
        "noncanonical-metadata",
        "trajectory-id",
        "engine",
        "candidate-type",
        "method",
        "role",
        "seed",
        "design-id",
        "context",
        "mapped-value",
    ],
)
def test_canonical_refuses_metadata_and_row_tampering(
    tmp_path: Path, damage: str
) -> None:
    design, table, rows = _tables(tmp_path)
    metadata = dict(table.schema.metadata)
    locations = table.to_pylist()
    if damage == "noncanonical-metadata":
        metadata[b"canonicalMorrisDesign"] += b" "
    elif damage == "trajectory-id":
        sealed = json.loads(metadata[b"canonicalMorrisDesign"])
        sealed["selectedTrajectories"][0]["trajectoryId"] = "0" * 64
        metadata[b"canonicalMorrisDesign"] = json.dumps(
            sealed, sort_keys=True, separators=(",", ":")
        ).encode()
    elif damage == "engine":
        sealed = json.loads(metadata[b"canonicalMorrisDesign"])
        sealed["numericalEngine"]["version"] = "0"
        metadata[b"canonicalMorrisDesign"] = json.dumps(
            sealed, sort_keys=True, separators=(",", ":")
        ).encode()
    elif damage == "candidate-type":
        sealed = json.loads(metadata[b"canonicalMorrisDesign"])
        sealed["candidateDefinition"] = []
        metadata[b"canonicalMorrisDesign"] = json.dumps(
            sealed, sort_keys=True, separators=(",", ":")
        ).encode()
    elif damage == "method":
        locations[0]["method"] = "discrete_oat"
    elif damage == "role":
        locations[0]["role"] = locations[1]["role"]
    elif damage == "seed":
        locations[0]["seed"] += 1
    elif damage == "design-id":
        locations[0]["designId"] = "f" * 32
    elif damage == "context":
        locations[0]["contextHash"] = "f" * 64
    else:
        locations[0]["a"] = 999
    damaged = pa.Table.from_pylist(
        locations, schema=table.schema.with_metadata(metadata)
    )
    with pytest.raises(ValueError, match="canonical Morris"):
        _analyze_tables(design, damaged, rows)


def test_canonical_refuses_discrete_oat_plans(tmp_path: Path) -> None:
    descriptor, context_hash = _descriptor()
    discrete = replace(
        discrete_design(tmp_path / "discrete"),
        context_descriptor=descriptor,
        context_hash=context_hash,
    )
    build_plan(discrete)
    table = pq.read_table(discrete.campaign_root / "plan.parquet")
    with pytest.raises(ValueError, match="canonical Morris.*metadata"):
        _canonical_morris_tables(table, [], 101, 0.95, (2,), 1)


@pytest.mark.parametrize(
    "setting,value",
    [("bootstrapResamples", 99), ("convergenceCheckpoints", [1, 3])],
)
def test_canonical_analyzer_rejects_sealed_settings_below_scientific_minima(
    tmp_path: Path, setting: str, value: object
) -> None:
    design, table, rows = _tables(tmp_path)
    metadata = dict(table.schema.metadata)
    sealed = json.loads(metadata[b"canonicalMorrisDesign"])
    sealed[setting] = value
    metadata[b"canonicalMorrisDesign"] = json.dumps(
        sealed, sort_keys=True, separators=(",", ":")
    ).encode()
    damaged = table.replace_schema_metadata(metadata)
    with pytest.raises(ValueError, match="sealed analysis settings|bootstrap_resamples"):
        _canonical_morris_tables(
            damaged,
            rows,
            sealed["bootstrapResamples"],
            design.confidence_level,
            tuple(sealed["convergenceCheckpoints"]),
            design.screening_top_k,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("metric", "ci_width", "rank_stability"),
        ("consecutive_checks", 2, "at most 1"),
    ],
)
def test_canonical_analyzer_revalidates_sealed_stopping_contract(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    design, table, _ = _tables(tmp_path)
    metadata = dict(table.schema.metadata)
    sealed = json.loads(metadata[b"canonicalMorrisDesign"])
    sealed["scientificContract"]["charter"]["stopping"][0][field] = value
    metadata[b"canonicalMorrisDesign"] = json.dumps(
        sealed, sort_keys=True, separators=(",", ":")
    ).encode()

    with pytest.raises(ValueError, match=message):
        _canonical_morris_tables(
            table.replace_schema_metadata(metadata),
            [],
            design.bootstrap_resamples,
            design.confidence_level,
            design.convergence_checkpoints,
            design.screening_top_k,
        )


def test_discrete_oat_analyzer_preserves_adjacent_effects_under_new_identity(
    tmp_path: Path,
) -> None:
    descriptor, context_hash = _descriptor()
    design = replace(
        discrete_design(tmp_path / "discrete"),
        context_descriptor=descriptor,
        context_hash=context_hash,
    )
    build_plan(design)
    table = pq.read_table(design.campaign_root / "plan.parquet")
    unique = {row["runId"]: row for row in table.to_pylist()}
    for run_id, row in unique.items():
        value = 2.0 * row["a"] - 3.0 * row["b"]
        write_parquet_atomic(
            pa.table({"synthetic": [value]}),
            design.campaign_root / "archives" / run_id / "value.parquet",
        )
        _write_verified_run(
            design.campaign_root,
            RunRecord(
                run_id,
                "test",
                row["seed"],
                context_hash,
                "node",
                1.0,
                {"a": row["a"], "b": row["b"]},
            ),
            execution_evidence(design.context_hash),
        )
    indicator = scientific_indicator("value")
    result = analyze(
        design.campaign_root,
        (indicator,),
        campaign="test",
        completeness_threshold=1.0,
        method="discrete_oat",
    )
    assert result.sensitivity_method == "discrete_oat"
    assert result.sensitivity_path.name == "discrete-oat-effects.parquet"
    effects = pq.read_table(result.sensitivity_path).to_pylist()
    assert {row["factor"]: row["mu"] for row in effects} == pytest.approx(
        {"a": 10.0, "b": -6.0}
    )
    manifest = json.loads(
        (design.campaign_root / "analysis-generation.json").read_text()
    )
    assert manifest["version"] == 7
    assert manifest["sensitivityMethod"] == "discrete_oat"


def _completed_campaign(tmp_path: Path, **changes):  # type: ignore[no-untyped-def]
    design, table, rows = _tables(tmp_path, **changes)
    values = {run_id: value for run_id, _, value in rows}
    context_hash = _descriptor()[1]
    plan_rows = {row["runId"]: row for row in table.to_pylist()}
    for run_id, value in values.items():
        row = plan_rows[run_id]
        write_parquet_atomic(
            pa.table({"synthetic": [value]}),
            design.campaign_root / "archives" / run_id / "value.parquet",
        )
        _write_verified_run(
            design.campaign_root,
            RunRecord(
                run_id,
                "test",
                row["seed"],
                context_hash,
                "node",
                1.0,
                {"a": row["a"], "b": row["b"]},
            ),
            execution_evidence(design.context_hash),
        )
    indicator = scientific_indicator("value")
    return design, indicator


@pytest.mark.parametrize("completed_count", [0, 1])
def test_canonical_descriptive_only_publishes_partial_or_empty_outputs_without_inference(
    tmp_path: Path,
    completed_count: int,
) -> None:
    design, table, rows = _tables(tmp_path)
    values = {run_id: value for run_id, _, value in rows}
    plan_rows = {row["runId"]: row for row in table.to_pylist()}
    for run_id in sorted(values)[:completed_count]:
        row = plan_rows[run_id]
        write_parquet_atomic(
            pa.table({"synthetic": [values[run_id]]}),
            design.campaign_root / "archives" / run_id / "value.parquet",
        )
        _write_verified_run(
            design.campaign_root,
            RunRecord(
                run_id,
                "test",
                row["seed"],
                design.context_hash,
                "node",
                1.0,
                {"a": row["a"], "b": row["b"]},
            ),
        )
    indicator = scientific_indicator("value")
    options = {
        "campaign": "test",
        "completeness_threshold": 1.0,
        "method": "canonical_morris",
        "bootstrap_resamples": design.bootstrap_resamples,
        "confidence_level": design.confidence_level,
        "convergence_checkpoints": design.convergence_checkpoints,
        "screening_top_k": design.screening_top_k,
    }

    with pytest.raises(ValueError, match="below threshold"):
        analyze(design.campaign_root, (indicator,), **options)
    assert not (design.campaign_root / "analysis").exists()

    result = analyze(
        design.campaign_root,
        (indicator,),
        descriptive_only=True,
        **options,
    )

    assert result.completed == completed_count
    assert result.inference_status == "incomplete_noninferential"
    assert result.dispersion_path is not None
    assert result.sensitivity_method is None
    assert result.sensitivity_path is None
    assert result.convergence_path is None
    assert result.rank_reversals_path is None
    assert result.noise_floor_path is None
    assert result.replication_allocation_path is None
    assert result.convergence_evidence_path is None
    assert pq.read_table(result.path).num_rows == completed_count
    assert pq.read_table(result.output_summary_path)["n"].to_pylist() == [
        completed_count
    ]
    manifest = json.loads(
        (design.campaign_root / "analysis-generation.json").read_text()
    )
    assert set(manifest["outputs"]) == {
        "capturedRunSet",
        "indicators",
        "dispersion",
        "outputSummary",
        "governedReport",
    }
    assert manifest["provenance"]["analysisOptions"]["descriptiveOnly"] is True
    report = json.loads(result.governed_report_path.read_text())
    assert report["inferenceStatus"] == "incomplete_noninferential"
    assert "incomplete_noninferential" in report["reasonCodes"]
    assert report["claimAuthorized"] is False


def test_complete_canonical_descriptive_only_is_explicitly_noninferential(
    tmp_path: Path,
) -> None:
    design, indicator = _completed_campaign(tmp_path)

    result = analyze(
        design.campaign_root,
        (indicator,),
        campaign="test",
        completeness_threshold=1.0,
        method="canonical_morris",
        bootstrap_resamples=design.bootstrap_resamples,
        confidence_level=design.confidence_level,
        convergence_checkpoints=design.convergence_checkpoints,
        screening_top_k=design.screening_top_k,
        descriptive_only=True,
    )

    assert result.inference_status == "descriptive_noninferential"
    assert result.sensitivity_path is None
    assert result.convergence_path is None
    assert result.noise_floor_path is None
    report = json.loads(result.governed_report_path.read_text())
    assert "descriptive_noninferential" in report["reasonCodes"]


@pytest.mark.parametrize(
    "bootstrap_resamples,checkpoints,message",
    [
        (99, (2, 3), "bootstrap_resamples"),
        (101, (1, 3), "convergence_checkpoints"),
    ],
)
def test_canonical_analyze_rejects_scientific_minimum_boundaries(
    tmp_path: Path,
    bootstrap_resamples: int,
    checkpoints: tuple[int, ...],
    message: str,
) -> None:
    design, indicator = _completed_campaign(tmp_path)
    with pytest.raises(ValueError, match=message):
        analyze(
            design.campaign_root,
            (indicator,),
            campaign="test",
            completeness_threshold=1.0,
            method="canonical_morris",
            bootstrap_resamples=bootstrap_resamples,
            confidence_level=design.confidence_level,
            convergence_checkpoints=checkpoints,
            screening_top_k=design.screening_top_k,
        )


def test_canonical_transactionally_publishes_noise_allocation_and_manifest_v7(
    tmp_path: Path,
) -> None:
    design, indicator = _completed_campaign(tmp_path)
    result = analyze(
        design.campaign_root,
        (indicator,),
        campaign="test",
        completeness_threshold=1.0,
        method="canonical_morris",
        bootstrap_resamples=design.bootstrap_resamples,
        confidence_level=design.confidence_level,
        convergence_checkpoints=design.convergence_checkpoints,
        screening_top_k=design.screening_top_k,
    )
    assert result.sensitivity_path.name == "canonical-morris-ranking.parquet"
    assert result.convergence_path.name == "canonical-morris-convergence.parquet"
    assert result.rank_reversals_path.name == "canonical-morris-rank-reversals.parquet"
    assert result.noise_floor_path.name == "noise-floor.parquet"
    assert result.replication_allocation_path.name == "replication-allocation.json"
    assert result.inference_status == "pilot_only"
    manifest = json.loads(
        (design.campaign_root / "analysis-generation.json").read_text()
    )
    assert manifest["version"] == 7
    assert manifest["layout"] == "immutable-generation-v1"
    assert manifest["inferenceStatus"] == "pilot_only"
    assert manifest["noiseFloorPath"] == root_relative(
        design.campaign_root, result.noise_floor_path
    )
    assert manifest["replicationAllocationPath"] == root_relative(
        design.campaign_root, result.replication_allocation_path
    )
    assert manifest["sensitivityMethod"] == "canonical_morris"
    assert manifest["convergencePath"] == root_relative(
        design.campaign_root, result.convergence_path
    )
    assert manifest["rankReversalsPath"] == root_relative(
        design.campaign_root, result.rank_reversals_path
    )

    noise = pq.read_table(result.noise_floor_path)
    assert noise.schema == pa.schema(
        [
            pa.field("indicator", pa.string(), False),
            pa.field("trajectory", pa.int64(), False),
            pa.field("rank", pa.int64(), False),
            pa.field("role", pa.string(), False),
            pa.field("replications", pa.int64(), False),
            pa.field("mean", pa.float64(), False),
            pa.field("intrinsic_variance", pa.float64()),
            pa.field("intrinsic_sd", pa.float64()),
            pa.field("mcse", pa.float64()),
            pa.field("is_primary_indicator", pa.bool_(), False),
        ]
    )
    assert len(noise) == design.trajectories * (len(design.factors) + 1)
    assert set(noise["replications"].to_pylist()) == {2}
    assert all(noise["is_primary_indicator"].to_pylist())
    for row in noise.to_pylist():
        assert row["intrinsic_sd"] == pytest.approx(
            math.sqrt(row["intrinsic_variance"])
        )
        assert row["mcse"] == pytest.approx(row["intrinsic_sd"] / math.sqrt(2))

    recommendation = json.loads(result.replication_allocation_path.read_text())
    variances = noise["intrinsic_variance"].to_pylist()
    quantile = float(np.quantile(variances, 0.9))
    raw = max(2, math.ceil(quantile / 0.5**2))
    assert recommendation == {
        "version": 1,
        "status": "capped",
        "primaryIndicator": VALUE,
        "varianceQuantile": 0.9,
        "varianceQuantileValue": quantile,
        "targetMcse": 0.5,
        "rawRequiredReplications": raw,
        "selectedReplications": 10,
        "currentReplications": 2,
        "maximumReplications": 10,
        "estimatedProductionPositions": 3 * 3 * 10,
        "availableRuns": 90,
        "withinTarget": False,
        "seedPolicy": "canonical-morris-derived-crn-v1",
    }


@pytest.mark.parametrize(
    "target,maximum,budget,status,selected",
    [
        (0.2, 4, 100, "recommended", 2),
        (0.1, 4, 100, "capped", 4),
        (0.2, 4, 10, "infeasible", 2),
    ],
)
def test_pilot_allocation_status_formula_and_budget(
    target: float,
    maximum: int,
    budget: int,
    status: str,
    selected: int,
) -> None:
    design = {
        "trajectories": 3,
        "factors": [{"name": "a"}, {"name": "b"}],
        "replicationBaseSeeds": [1, 2],
        "scientificContract": {
            "charter": {
                "decision": {"primary_indicator": "primary"},
                "budget": {"maximum_runs": budget, "reserve_fraction": 0.1},
                "stochastic": {
                    "target": "conditional_mean",
                    "allocation": {
                        "phase": "pilot",
                        "variance_quantile": 0.5,
                        "target_mcse": target,
                        "minimum_replications": 2,
                        "maximum_replications": maximum,
                    },
                },
            }
        },
    }
    noise = pa.table(
        {
            "indicator": ["primary", "primary", "secondary"],
            "intrinsic_variance": [0.04, 0.09, 999.0],
        }
    )
    result = _replication_allocation(design, noise)
    assert result["varianceQuantileValue"] == pytest.approx(0.065)
    assert result["rawRequiredReplications"] == max(
        2, math.ceil(0.065 / target**2)
    )
    assert result["selectedReplications"] == selected
    assert result["status"] == status
    assert result["availableRuns"] == math.floor(budget * 0.9)
    assert result["estimatedProductionPositions"] == 3 * 3 * selected


def test_pilot_allocation_handles_subnormal_positive_target() -> None:
    design = {
        "trajectories": 2,
        "factors": [{"name": "a"}],
        "replicationBaseSeeds": [1, 2],
        "scientificContract": {
            "charter": {
                "decision": {"primary_indicator": "primary"},
                "budget": {"maximum_runs": 100, "reserve_fraction": 0.0},
                "stochastic": {
                    "target": "conditional_mean",
                    "allocation": {
                        "phase": "pilot",
                        "variance_quantile": 1.0,
                        "target_mcse": 5e-324,
                        "minimum_replications": 2,
                        "maximum_replications": 4,
                    },
                },
            }
        },
    }
    result = _replication_allocation(
        design,
        pa.table({"indicator": ["primary"], "intrinsic_variance": [1.0]}),
    )
    assert result["status"] == "capped"
    assert result["rawRequiredReplications"] > 4


def test_canonical_noise_floor_requires_sealed_primary_indicator(tmp_path: Path) -> None:
    design, table, rows = _tables(tmp_path)
    wrong_rows = [(run_id, "other", value) for run_id, _, value in rows]
    with pytest.raises(ValueError, match="primary indicator"):
        _canonical_morris_tables(
            table,
            wrong_rows,
            design.bootstrap_resamples,
            design.confidence_level,
            design.convergence_checkpoints,
            design.screening_top_k,
            include_noise_floor=True,
        )


def test_canonical_production_publishes_noise_without_allocation(tmp_path: Path) -> None:
    original = _design(tmp_path / "unused")
    contract = copy.deepcopy(original.scientific_contract)
    contract["charter"]["stochastic"]["allocation"].update(
        phase="production",
        selected_replications=2,
        provenance="pilot/root replication-allocation.json sha256:abc",
    )
    design, indicator = _completed_campaign(tmp_path, scientific_contract=contract)
    result = analyze(
        design.campaign_root,
        (indicator,),
        campaign="test",
        completeness_threshold=1.0,
        method="canonical_morris",
        bootstrap_resamples=design.bootstrap_resamples,
        confidence_level=design.confidence_level,
        convergence_checkpoints=design.convergence_checkpoints,
        screening_top_k=design.screening_top_k,
    )
    assert result.noise_floor_path.is_file()
    assert result.replication_allocation_path is None
    assert result.inference_status == "conditional"
    manifest = json.loads((design.campaign_root / "analysis-generation.json").read_text())
    assert manifest["inferenceStatus"] == "conditional"
    assert manifest["replicationAllocationPath"] is None


def test_canonical_publication_failure_restores_all_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import eqasim_sa.core.analysis_publication as publication_module

    design, indicator = _completed_campaign(tmp_path)
    names = (
        "indicators.parquet",
        "replication-dispersion.parquet",
        "canonical-morris-ranking.parquet",
        "canonical-morris-convergence.parquet",
        "canonical-morris-rank-reversals.parquet",
        "noise-floor.parquet",
        "replication-allocation.json",
        "analysis-generation.json",
    )
    previous = {design.campaign_root / name: name.encode() for name in names}
    for path, content in previous.items():
        path.write_bytes(content)
    real_write = publication_module.write_parquet_exclusive

    def fail(
        table: pa.Table, destination: Path, *, staging_directory: Path | None = None
    ) -> Path:
        if destination.name == "canonical-morris-convergence.parquet":
            raise OSError("injected canonical convergence failure")
        return real_write(table, destination, staging_directory=staging_directory)

    monkeypatch.setattr(publication_module, "write_parquet_exclusive", fail)
    with pytest.raises(OSError, match="canonical convergence"):
        analyze(
            design.campaign_root,
            (indicator,),
            campaign="test",
            completeness_threshold=1.0,
            method="canonical_morris",
            bootstrap_resamples=design.bootstrap_resamples,
            confidence_level=design.confidence_level,
            convergence_checkpoints=design.convergence_checkpoints,
            screening_top_k=design.screening_top_k,
        )
    assert {path: path.read_bytes() for path in previous} == previous


def test_canonical_allocation_failure_rolls_back_new_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import eqasim_sa.core.analysis_publication as publication_module

    design, indicator = _completed_campaign(tmp_path)
    noise = design.campaign_root / "noise-floor.parquet"
    allocation = design.campaign_root / "replication-allocation.json"
    noise.write_bytes(b"old-noise")
    allocation.write_bytes(b"old-allocation")
    real_write = publication_module.write_bytes_exclusive

    def fail(
        content: bytes, destination: Path, *, staging_directory: Path | None = None
    ) -> Path:
        if destination.name == "replication-allocation.json":
            raise OSError("injected allocation failure")
        return real_write(content, destination, staging_directory=staging_directory)

    monkeypatch.setattr(publication_module, "write_bytes_exclusive", fail)
    with pytest.raises(OSError, match="allocation failure"):
        analyze(
            design.campaign_root,
            (indicator,),
            campaign="test",
            completeness_threshold=1.0,
            method="canonical_morris",
            bootstrap_resamples=design.bootstrap_resamples,
            confidence_level=design.confidence_level,
            convergence_checkpoints=design.convergence_checkpoints,
            screening_top_k=design.screening_top_k,
        )
    assert noise.read_bytes() == b"old-noise"
    assert allocation.read_bytes() == b"old-allocation"


def test_other_method_publishes_no_canonical_outputs(tmp_path: Path) -> None:
    root, _ = _campaign(tmp_path)
    result = analyze(
        root,
        (_age_indicator(),),
        campaign="test",
        completeness_threshold=0.0,
    )
    manifest = json.loads((root / "analysis-generation.json").read_text())
    assert result.noise_floor_path is None
    assert result.replication_allocation_path is None
    for field in (
        "sensitivityPath",
        "convergencePath",
        "rankReversalsPath",
        "noiseFloorPath",
        "replicationAllocationPath",
    ):
        assert manifest[field] is None
    assert sorted(manifest["outputs"]) == [
        "capturedRunSet",
        "dispersion",
        "governedReport",
        "indicators",
        "outputSummary",
    ]


def test_application_passes_sealed_canonical_analysis_and_exposes_extra_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, seen = tmp_path / "campaign", {}
    meta = SimpleNamespace(
        campaign_root=root,
        indicators=(),
        campaign="scientific",
        completeness_threshold=1.0,
        seed_role="replication",
        method="canonical_morris",
        bootstrap_resamples=101,
        confidence_level=0.95,
        convergence_checkpoints=(2, 3),
        screening_top_k=2,
        meta_config_sha256="a" * 64,
        charter=SimpleNamespace(status="scientific"),
    )
    result = SimpleNamespace(
        path=root / "indicators.parquet",
        dispersion_path=root / "replication-dispersion.parquet",
        sensitivity_method="canonical_morris",
        sensitivity_path=root / "canonical-morris-ranking.parquet",
        convergence_path=root / "canonical-morris-convergence.parquet",
        rank_reversals_path=root / "canonical-morris-rank-reversals.parquet",
        noise_floor_path=root / "noise-floor.parquet",
        replication_allocation_path=root / "replication-allocation.json",
        output_summary_path=root / "output-summary.parquet",
        convergence_evidence_path=root / "convergence-evidence.parquet",
        governed_report_path=root / "governed-report.json",
        inference_status="pilot_only",
        runs=9,
        indicators=1,
        values=9,
        completion_rate=1.0,
        expected=9,
        completed=9,
        missing=0,
        failed=0,
    )
    monkeypatch.setattr(
        "eqasim_sa.adapters.application.load_meta_config", lambda path, **kwargs: meta
    )
    sealed_design = object()
    validation = {}
    monkeypatch.setattr(
        "eqasim_sa.adapters.application._design", lambda current: sealed_design
    )
    monkeypatch.setattr(
        "eqasim_sa.adapters.application.load_run_specs",
        lambda path, design, campaign: validation.update(
            path=path, design=design, campaign=campaign
        ),
    )
    monkeypatch.setattr(
        "eqasim_sa.adapters.application.analyze",
        lambda *args, **kwargs: seen.update(kwargs) or result,
    )
    payload = analyze_from_meta_config(Path("meta.yml"), descriptive_only=True)
    assert seen["bootstrap_resamples"] == 101
    assert seen["confidence_level"] == 0.95
    assert seen["convergence_checkpoints"] == (2, 3)
    assert seen["screening_top_k"] == 2
    assert seen["meta_config_sha256"] == "a" * 64
    assert seen["descriptive_only"] is True
    assert validation == {
        "path": root / "plan.parquet",
        "design": sealed_design,
        "campaign": "scientific",
    }
    assert payload["convergencePath"].endswith("canonical-morris-convergence.parquet")
    assert payload["rankReversalsPath"].endswith(
        "canonical-morris-rank-reversals.parquet"
    )
    assert payload["noiseFloorPath"].endswith("noise-floor.parquet")
    assert payload["replicationAllocationPath"].endswith("replication-allocation.json")
    assert payload["outputSummaryPath"].endswith("output-summary.parquet")
    assert payload["convergenceEvidencePath"].endswith("convergence-evidence.parquet")
    assert payload["governedReportPath"].endswith("governed-report.json")
    assert payload["inferenceStatus"] == "pilot_only"


@pytest.mark.parametrize("damage", ["contract", "run-id"])
def test_application_refuses_canonical_meta_or_run_id_substitution_before_analyze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    meta_path = canonical_meta(tmp_path)
    campaign_root = load_meta_config(meta_path).campaign_root
    plan_from_meta_config(meta_path)
    if damage == "contract":
        document = yaml.safe_load(meta_path.read_text())
        document["charter"]["budget"]["maximum_runs"] += 1
        meta_path.write_text(yaml.safe_dump(document, sort_keys=False))
    else:
        plan_path = campaign_root / "plan.parquet"
        table = pq.read_table(plan_path)
        plan_rows = table.to_pylist()
        plan_rows[0]["runId"] = "f" * 32
        pq.write_table(pa.Table.from_pylist(plan_rows, schema=table.schema), plan_path)
    called = []
    monkeypatch.setattr(
        "eqasim_sa.adapters.application.analyze", lambda *args, **kwargs: called.append(1)
    )
    with pytest.raises(ValueError, match="mismatch"):
        analyze_from_meta_config(meta_path)
    assert called == []
