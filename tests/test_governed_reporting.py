import json
import copy
from dataclasses import replace
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from eqasim_sa.core.analyze import (
    _convergence_evidence_table,
    _governed_report,
    _output_summary_table,
)
from eqasim_sa.core.ledger import write_parquet_atomic, write_run, RunRecord
from eqasim_sa.core.analyze import analyze
from tests.test_analyze import _custom_indicator
from tests.test_canonical_morris_analyze import _completed_campaign, _design
from tests.test_multiscramble_sobol_plan import multiscramble_design
from eqasim_sa.core.plan import build_plan


def test_output_summary_weights_design_positions_and_uses_declared_formulas() -> None:
    plan = pa.table({"runId": ["a", "a", "b", "c"]})
    rows = [
        ("a", "value", 1.0),
        ("b", "value", 3.0),
        ("c", "value", 9.0),
    ]

    summary = _output_summary_table(plan, rows).to_pylist()

    assert summary == [
        {
            "indicator": "value",
            "n": 4,
            "mean": 3.5,
            "standardDeviation": pytest.approx(3.7859388972001824),
            "minimum": 1.0,
            "q05": pytest.approx(1.0),
            "median": 2.0,
            "q95": pytest.approx(8.1),
            "maximum": 9.0,
        }
    ]


def test_output_summary_retains_indicators_without_completed_observations() -> None:
    summary = _output_summary_table(
        pa.table({"runId": ["a"]}), [], ("missing",)
    ).to_pylist()

    assert summary == [
        {
            "indicator": "missing",
            "n": 0,
            "mean": None,
            "standardDeviation": None,
            "minimum": None,
            "q05": None,
            "median": None,
            "q95": None,
            "maximum": None,
        }
    ]


def test_output_summary_rejects_overflowing_derived_statistics() -> None:
    with pytest.raises(ValueError, match="output summary"):
        _output_summary_table(
            pa.table({"runId": ["a", "b"]}),
            [("a", "value", -1.7e308), ("b", "value", 1.7e308)],
        )


def _grid_with_duplicate_positions(tmp_path: Path) -> Path:
    root = tmp_path / "campaign"
    run_ids = ("1" * 32, "2" * 32)
    write_parquet_atomic(
        pa.table(
            {
                "designId": ["a" * 32, "b" * 32, "c" * 32],
                "method": ["grid"] * 3,
                "role": ["point"] * 3,
                "runId": [run_ids[0], run_ids[0], run_ids[1]],
                "seed": [1, 1, 1],
                "contextHash": ["hash"] * 3,
            }
        ),
        root / "plan.parquet",
    )
    for run_id, value in zip(run_ids, (1.0, 5.0), strict=True):
        write_parquet_atomic(
            pa.table({"synthetic": [value]}), root / "archives" / run_id / "age.parquet"
        )
        write_run(root, RunRecord(run_id, "test", 1, "hash", "node", 1.0, {}))
    return root


def test_demonstration_publishes_noninferential_governed_artifacts_and_hashes(
    tmp_path: Path,
) -> None:
    root = _grid_with_duplicate_positions(tmp_path)

    result = analyze(
        root,
        (_custom_indicator(),),
        campaign="test",
        completeness_threshold=1.0,
    )

    assert result.inference_status == "demonstration_noninferential"
    assert result.output_summary_path == root / "output-summary.parquet"
    assert result.convergence_evidence_path is None
    assert result.governed_report_path == root / "governed-report.json"
    assert pq.read_table(result.output_summary_path).to_pylist()[0]["n"] == 3
    report = json.loads(result.governed_report_path.read_text())
    assert report["inferenceStatus"] == "demonstration_noninferential"
    assert report["claimAuthorized"] is False
    assert report["evidence"]["outputSummary"]["sha256"] == __import__(
        "hashlib"
    ).sha256(result.output_summary_path.read_bytes()).hexdigest()
    manifest = json.loads((root / "analysis-generation.json").read_text())
    for prefix, path in (
        ("outputSummary", result.output_summary_path),
        ("governedReport", result.governed_report_path),
    ):
        assert manifest[f"{prefix}Path"] == path.name
        assert manifest[f"{prefix}Sha256"] == __import__("hashlib").sha256(
            path.read_bytes()
        ).hexdigest()


def test_demonstration_removes_stale_scientific_convergence_evidence(
    tmp_path: Path,
) -> None:
    root = _grid_with_duplicate_positions(tmp_path)
    stale = root / "convergence-evidence.parquet"
    stale.write_bytes(b"stale scientific evidence")

    result = analyze(
        root,
        (_custom_indicator(),),
        campaign="test",
        completeness_threshold=1.0,
    )

    assert result.convergence_evidence_path is None
    assert not stale.exists()


def test_rank_stability_formula_and_consecutive_checks() -> None:
    convergence = pa.Table.from_pylist(
        [
            {"indicator": "primary", "factor": factor, "checkpoint_trajectories": checkpoint, "rank": rank}
            for checkpoint, ranks in ((2, (1.0, 2.0, 3.0)), (3, (1.0, 2.5, 2.5)), (4, (1.0, 2.25, 2.75)))
            for factor, rank in zip(("a", "b", "c"), ranks, strict=True)
        ]
    )
    contract = {
        "decision": {"objective": "screening", "primary_indicator": "primary"},
        "stopping": [
            {"metric": "rank_stability", "threshold": 0.25, "consecutive_checks": 2}
        ],
    }

    table, converged, criteria = _convergence_evidence_table(
        "canonical_morris", contract, convergence
    )

    assert [row["value"] for row in table.to_pylist()] == [0.25, 0.125]
    assert [row["passed"] for row in table.to_pylist()] == [True, True]
    assert [row["consecutivePasses"] for row in table.to_pylist()] == [1, 2]
    assert [row["criterionMet"] for row in table.to_pylist()] == [False, True]
    assert converged is True
    assert criteria == [
        {
            "metric": "rank_stability",
            "threshold": 0.25,
            "consecutiveChecks": 2,
            "status": "met",
            "finalConsecutivePasses": 2,
        }
    ]


def test_consecutive_checks_reset_after_a_failed_checkpoint() -> None:
    rows = []
    for checkpoint, width in ((2, 0.5), (4, 2.0), (8, 0.5)):
        rows.append(
            {
                "indicator": "primary",
                "factor": "a",
                "checkpoint": checkpoint,
                "is_actionable": True,
                "first_order_ci_width": width,
                "total_order_ci_width": width,
            }
        )
    contract = {
        "decision": {"objective": "fp", "primary_indicator": "primary"},
        "stopping": [{"metric": "ci_width", "threshold": 1.0, "consecutive_checks": 2}],
    }

    table, converged, _ = _convergence_evidence_table(
        "multiscramble_sobol", contract, pa.Table.from_pylist(rows)
    )

    assert table["consecutivePasses"].to_pylist() == [1, 0, 1]
    assert converged is False


def test_invalid_width_makes_the_whole_criterion_unevaluable() -> None:
    rows = [
        {
            "indicator": "primary",
            "factor": "a",
            "checkpoint": checkpoint,
            "is_actionable": True,
            "first_order_ci_width": width,
            "total_order_ci_width": width,
        }
        for checkpoint, width in ((2, None), (4, 0.1))
    ]
    contract = {
        "decision": {"objective": "fp", "primary_indicator": "primary"},
        "stopping": [{"metric": "ci_width", "threshold": 1.0, "consecutive_checks": 1}],
    }

    table, converged, criteria = _convergence_evidence_table(
        "multiscramble_sobol", contract, pa.Table.from_pylist(rows), {"a"}
    )

    assert table.to_pylist() == [
        {
            "metric": "ci_width",
            "checkpoint": None,
            "value": None,
            "threshold": 1.0,
            "passed": False,
            "consecutivePasses": 0,
            "criterionMet": False,
            "evaluationStatus": "unevaluable",
        }
    ]
    assert converged is False
    assert criteria[0]["status"] == "unevaluable"


@pytest.mark.parametrize("method", ["canonical_morris", "multiscramble_sobol"])
def test_missing_actionable_factor_makes_convergence_unevaluable(method: str) -> None:
    if method == "canonical_morris":
        rows = [
            {
                "indicator": "primary",
                "factor": "a",
                "checkpoint_trajectories": checkpoint,
                "rank": 1.0,
            }
            for checkpoint in (2, 3)
        ]
        decision = {"objective": "screening", "primary_indicator": "primary"}
        metric = "rank_stability"
    else:
        rows = [
            {
                "indicator": "primary",
                "factor": "a",
                "checkpoint": checkpoint,
                "is_actionable": True,
                "first_order_ci_width": 0.1,
                "total_order_ci_width": 0.1,
            }
            for checkpoint in (2, 4)
        ]
        decision = {"objective": "fp", "primary_indicator": "primary"}
        metric = "ci_width"
    contract = {
        "decision": decision,
        "stopping": [{"metric": metric, "threshold": 1.0, "consecutive_checks": 1}],
    }

    table, converged, criteria = _convergence_evidence_table(
        method, contract, pa.Table.from_pylist(rows), {"a", "missing"}
    )

    assert table.to_pylist()[0]["evaluationStatus"] == "unevaluable"
    assert converged is False
    assert criteria[0]["status"] == "unevaluable"


@pytest.mark.parametrize(
    "objective,width_column,expected",
    [("fp", "first_order_ci_width", 0.4), ("ff", "total_order_ci_width", 0.8)],
)
def test_ci_width_uses_objective_order_and_excludes_nonactionable_factors(
    objective: str, width_column: str, expected: float
) -> None:
    rows = []
    for checkpoint in (2, 4):
        rows.extend(
            [
                {
                    "indicator": "primary",
                    "factor": "a",
                    "checkpoint": checkpoint,
                    "is_actionable": True,
                    "first_order_ci_width": 0.4,
                    "total_order_ci_width": 0.8,
                },
                {
                    "indicator": "primary",
                    "factor": "seed",
                    "checkpoint": checkpoint,
                    "is_actionable": False,
                    "first_order_ci_width": 99.0,
                    "total_order_ci_width": 99.0,
                },
            ]
        )
    contract = {
        "decision": {"objective": objective, "primary_indicator": "primary"},
        "stopping": [{"metric": "ci_width", "threshold": 1.0, "consecutive_checks": 2}],
    }

    table, converged, _ = _convergence_evidence_table(
        "multiscramble_sobol", contract, pa.Table.from_pylist(rows)
    )

    assert table["value"].to_pylist() == [expected, expected]
    assert width_column in rows[0]
    assert converged is True


@pytest.mark.parametrize(
    "method,metric",
    [("canonical_morris", "ci_width"), ("multiscramble_sobol", "rank_stability"), ("canonical_morris", "unknown")],
)
def test_unsupported_stopping_metrics_fail_closed(method: str, metric: str) -> None:
    contract = {
        "decision": {"objective": "screening", "primary_indicator": "primary"},
        "stopping": [{"metric": metric, "threshold": 1.0, "consecutive_checks": 1}],
    }

    table, converged, criteria = _convergence_evidence_table(
        method, contract, pa.table({})
    )

    assert converged is False
    assert table.to_pylist()[0]["evaluationStatus"] == "unsupported"
    assert table.to_pylist()[0]["passed"] is False
    assert criteria[0]["status"] == "unsupported"


def test_canonical_pilot_publishes_governed_evidence_exclusions_and_hashes(
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

    assert result.inference_status == "pilot_only"
    assert result.convergence_evidence_path.name == "convergence-evidence.parquet"
    report = json.loads(result.governed_report_path.read_text())
    assert report["decision"]["objective"] == "screening"
    assert report["robustness"]["status"] == "pending"
    assert report["robustness"]["checks"][0]["status"] == "pending"
    assert report["exclusions"][0]["name"] == "model"
    assert {row["classification"] for row in report["factorEvidence"]} == {"screening"}
    assert report["claimAuthorized"] is False
    manifest = json.loads((design.campaign_root / "analysis-generation.json").read_text())
    assert manifest["version"] == 6
    assert manifest["convergenceEvidencePath"] == result.convergence_evidence_path.name
    assert manifest["convergenceEvidenceSha256"] == __import__("hashlib").sha256(
        result.convergence_evidence_path.read_bytes()
    ).hexdigest()


def test_canonical_production_is_conditional_only_after_stopping_passes(
    tmp_path: Path,
) -> None:
    original = _design(tmp_path / "unused")
    contract = copy.deepcopy(original.scientific_contract)
    contract["charter"]["stochastic"]["allocation"].update(
        phase="production",
        selected_replications=2,
        provenance="pilot/root replication-allocation.json sha256:abc",
    )
    contract["charter"]["stopping"] = [
        {"metric": "rank_stability", "threshold": 100.0, "consecutive_checks": 1}
    ]
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

    assert result.inference_status == "conditional"
    report = json.loads(result.governed_report_path.read_text())
    assert report["convergence"]["converged"] is True
    assert report["robustness"]["status"] == "pending"
    assert report["claimAuthorized"] is False


def _complete_multiscramble(design) -> None:  # type: ignore[no-untyped-def]
    build_plan(design)
    plan = pq.read_table(design.campaign_root / "plan.parquet")
    for run_id, row in {row["runId"]: row for row in plan.to_pylist()}.items():
        value = float(row["a"] + row["seed"] / 10 + row["a"] * row["seed"] / 20)
        write_parquet_atomic(
            pa.table({"synthetic": [value]}),
            design.campaign_root / "archives" / run_id / "age.parquet",
        )
        write_run(
            design.campaign_root,
            RunRecord(run_id, "test", row["seed"], design.context_hash, "node", 1.0, {"a": row["a"]}),
        )


def test_multiscramble_exposes_scientific_status_and_nonactionable_seed(
    tmp_path: Path,
) -> None:
    original = multiscramble_design(tmp_path / "unused")
    contract = copy.deepcopy(original.scientific_contract)
    contract["charter"]["decision"]["primary_indicator"] = "custom(age)"
    contract["charter"]["stopping"] = [
        {"metric": "ci_width", "threshold": 1e300, "consecutive_checks": 2}
    ]
    design = replace(original, campaign_root=tmp_path / "campaign", scientific_contract=contract)
    _complete_multiscramble(design)

    result = analyze(
        design.campaign_root,
        (_custom_indicator(),),
        campaign="test",
        completeness_threshold=1.0,
        seed_role="factor",
        method="multiscramble_sobol",
        confidence_level=design.confidence_level,
    )

    assert result.inference_status == "conditional"
    report = json.loads(result.governed_report_path.read_text())
    assert report["nonActionableFactors"] == ["seed"]
    assert [row["factor"] for row in report["factorEvidence"]] == ["a"]
    assert report["factorEvidence"][0]["classification"] == "fp"


def test_invalid_ff_bound_remains_reportable_as_fail_closed_evidence(
    tmp_path: Path,
) -> None:
    summary = tmp_path / "output-summary.parquet"
    sensitivity = tmp_path / "sensitivity.parquet"
    write_parquet_atomic(pa.table({"value": [1.0]}), summary)
    write_parquet_atomic(pa.table({"value": [1.0]}), sensitivity)
    contract = copy.deepcopy(multiscramble_design(tmp_path / "unused").scientific_contract)
    contract["charter"]["decision"].update(
        objective="ff", practical_tolerance=0.1, primary_indicator="primary"
    )
    table = pa.Table.from_pylist(
        [
            {
                "indicator": "primary",
                "factor": "a",
                "is_actionable": True,
                "rank": 1,
                "total_order_ci_high": float("inf"),
                "within_practical_tolerance": False,
            }
        ]
    )

    report = _governed_report(
        method="multiscramble_sobol",
        inference_status="unstable",
        completion_rate=1.0,
        expected=1,
        completed=1,
        missing=0,
        failed=0,
        output_summary_path=summary,
        convergence_evidence_path=None,
        sensitivity_path=sensitivity,
        scientific_contract=contract,
        sensitivity_table=table,
        converged=False,
        criterion_reports=[],
    )

    assert report["factorEvidence"][0]["estimate"] is None
    assert report["factorEvidence"][0]["evidenceStatus"] == "invalid"
    assert json.dumps(report, allow_nan=False)


def test_governed_publication_failure_restores_all_reporting_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import eqasim_sa.core.analyze as analyze_module

    root = _grid_with_duplicate_positions(tmp_path)
    previous = {
        root / name: name.encode()
        for name in (
            "indicators.parquet",
            "output-summary.parquet",
            "convergence-evidence.parquet",
            "governed-report.json",
            "analysis-generation.json",
        )
    }
    for path, content in previous.items():
        path.write_bytes(content)
    real_write = analyze_module._write_atomic

    def fail(destination: Path, writer) -> None:  # type: ignore[no-untyped-def]
        if destination.name == "governed-report.json":
            raise OSError("injected governed report failure")
        real_write(destination, writer)

    monkeypatch.setattr(analyze_module, "_write_atomic", fail)
    with pytest.raises(OSError, match="governed report"):
        analyze(
            root,
            (_custom_indicator(),),
            campaign="test",
            completeness_threshold=1.0,
        )
    assert {path: path.read_bytes() for path in previous} == previous
