"""The plan seals the eqasim config; ``analyze`` validates against the sealed plan."""

import json
import shutil
import sys
from pathlib import Path

import pyarrow.parquet as pq
import pytest
import yaml

from eqasim_sa.adapters.application import (
    analyze_from_meta_config,
    plan_from_meta_config,
    run_from_meta_config,
)
from eqasim_sa.adapters.data_manifest import generate_data_manifest
from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.core.analyze import analyze
import eqasim_sa.core.plan as plan_module

SPY = Path(__file__).with_name("eqasim_spy.py")
REFERENCE = "HTS adjust with INSEE population weights"
BOTH_STAGES = ["synthesis.output", "analysis.synthesis.population"]
BASE_CONFIG = {
    "working_directory": "cache",
    "run": BOTH_STAGES,
    "config": {
        "a": 0,
        "b": "x",
        "sampling_rate": 0.001,
        "data_path": "../data",
        "processes": 1,
        "random_seed": 9,
        "output_prefix": "nantes_",
    },
}


def _scientific_charter() -> dict:
    item = lambda name, status: {  # noqa: E731
        "name": name,
        "status": status,
        "provenance": "Campaign declaration",
        "rationale": "Included in the uncertainty account",
    }
    return {
        "status": "scientific",
        "stochastic": {
            "target": "conditional_mean",
            "random_source": "simulator_seed",
            "intrinsic_variance": "report",
            "common_random_numbers": "within_trajectory",
            "allocation": {
                "phase": "pilot",
                "variance_quantile": 0.9,
                "target_mcse": 0.01,
                "minimum_replications": 2,
                "maximum_replications": 10,
                "selected_replications": None,
                "provenance": None,
            },
        },
        "decision": {
            "claim": "Identify inputs requiring further study",
            "use": "diagnostic",
            "setting": "Sealed analysis test",
            "objective": "screening",
            "estimand": "Expected total-variation distance over simulation seeds",
            "primary_indicator": f"tv_distance(age, ref={REFERENCE})",
        },
        "uncertainty": {
            "dependence": {
                "kind": "independent",
                "rationale": "No dependence is assumed for this design",
            },
            "inventory": {
                "parameters": [item("a", "varied"), item("b", "varied")],
                "data": [item("survey", "fixed")],
                "structures": [item("model", "excluded")],
                "scenarios": [],
                "numerics": [],
            },
        },
        "budget": {"maximum_runs": 100, "reserve_fraction": 0.1},
        "stopping": {
            "criteria": [
                {"metric": "rank_stability", "threshold": 0.05, "consecutive_checks": 1}
            ]
        },
        "robustness": {
            "checks": [
                {"name": "seed sensitivity", "rationale": "Verify across seeds"}
            ]
        },
    }


def campaign(tmp_path: Path, method: str = "grid", seeds: list[int] | None = None):
    """Write an eqasim folder, its data, and a spy-executed meta-config."""

    model = tmp_path / "model"
    model.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    (data / "input.txt").write_text("sealed data\n")
    config = model / "config.yml"
    config.write_text(yaml.safe_dump(BASE_CONFIG, sort_keys=False))
    (model / "uv.lock").write_text("locked\n")
    (model / "pipeline.py").write_text("STAGES = []\n")
    generate_data_manifest(data, tmp_path / "manifest.json")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    document: dict = {
        "campaign": "demo",
        "campaign_root": "campaign",
        "model": {
            "root": "model",
            "config": "config.yml",
            "data_manifest": "manifest.json",
        },
        "outputs": {
            "age": {
                "category": "Age class",
                "simulated": "Proportion_EQASIM",
                "references": [REFERENCE],
            }
        },
        "indicators": [{"tv_distance": {"output": "age", "reference": REFERENCE}}],
        "execution": {
            "workers": 1,
            "scratch": str(scratch),
            "invocation": [sys.executable, str(SPY)],
        },
    }
    if method == "grid":
        document["experiment"] = {"method": "grid", "seeds": seeds or [41]}
        document["parameters"] = {"sampling_rate": [0.001]}
    else:
        distribution = {
            "family": "discrete_uniform",
            "transform": "identity",
            "provenance": "Declared design domain",
        }
        document["charter"] = _scientific_charter()
        document["experiment"] = {
            "method": "canonical_morris",
            "seeds": [41, 42],
            "levels": 4,
            "trajectories": 3,
            "candidate_trajectories": 8,
            "design_seed": 20261119,
        }
        document["parameters"] = {
            "a": {"values": [1, 2], "distribution": dict(distribution)},
            "b": {"values": ["x", "y"], "distribution": dict(distribution)},
        }
        document["analysis"] = {
            "completeness_threshold": 1.0,
            "bootstrap_resamples": 101,
            "confidence_level": 0.95,
            "convergence_checkpoints": [2, 3],
            "screening_top_k": 1,
        }
    meta = tmp_path / "meta.yml"
    meta.write_text(yaml.safe_dump(document, sort_keys=False))
    return meta, config


@pytest.fixture
def spy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(tmp_path / "record.json"))
    monkeypatch.setenv("EQASIM_SPY_CASE", "complete-references")


def _edit_config(config: Path, edit) -> None:  # type: ignore[no-untyped-def]
    document = yaml.safe_load(config.read_text())
    edit(document)
    config.write_text(yaml.safe_dump(document, sort_keys=False))


def _edit_meta(meta: Path, edit) -> None:  # type: ignore[no-untyped-def]
    document = yaml.safe_load(meta.read_text())
    edit(document)
    meta.write_text(yaml.safe_dump(document, sort_keys=False))


def test_plan_seals_the_eqasim_base_config(tmp_path: Path) -> None:
    meta, _ = campaign(tmp_path)

    plan_from_meta_config(meta)

    metadata = pq.read_schema(tmp_path / "campaign" / "plan.parquet").metadata
    expected = {
        "run": BOTH_STAGES,
        "config": {"a": 0, "b": "x", "sampling_rate": 0.001, "output_prefix": "nantes_"},
    }
    assert metadata[b"modelDocument"] == json.dumps(
        expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert metadata[b"planSchemaVersion"] == b"1"
    sealed = plan_module.sealed_inputs(tmp_path / "campaign" / "plan.parquet")
    loaded = load_meta_config(meta)
    assert sealed.model_document == expected
    assert sealed.context_descriptor == loaded.context_descriptor
    assert sealed.context_hash == loaded.context_hash


@pytest.mark.parametrize("method", ["grid", "canonical_morris"])
def test_plan_run_analyze_with_model_document(
    tmp_path: Path, spy: None, method: str
) -> None:
    meta, _ = campaign(tmp_path, method)
    plan_from_meta_config(meta)
    assert run_from_meta_config(meta)["failed"] == 0

    payload = analyze_from_meta_config(meta)

    assert payload["status"] == "completed"
    assert payload["missing"] == payload["failed"] == 0


def test_plan_without_model_document_is_rejected(tmp_path: Path, spy: None) -> None:
    meta, _ = campaign(tmp_path)
    plan_from_meta_config(meta)
    assert run_from_meta_config(meta)["completed"] == 1
    path = tmp_path / "campaign" / "plan.parquet"
    table = pq.read_table(path)
    metadata = dict(table.schema.metadata)
    del metadata[b"modelDocument"]
    pq.write_table(table.replace_schema_metadata(metadata), path, compression="zstd")

    with pytest.raises(ValueError, match="modelDocument"):
        analyze_from_meta_config(meta)
    with pytest.raises(ValueError, match="modelDocument"):
        run_from_meta_config(meta)
    loaded = load_meta_config(meta)
    with pytest.raises(ValueError, match="Grid plan metadata"):
        analyze(
            loaded.campaign_root,
            loaded.indicators,
            campaign=loaded.campaign,
            completeness_threshold=1.0,
        )


def test_analyze_after_eqasim_code_change(tmp_path: Path, spy: None) -> None:
    meta, config = campaign(tmp_path)
    plan_from_meta_config(meta)
    assert run_from_meta_config(meta)["completed"] == 1
    (config.parent / "analysis").mkdir()
    (config.parent / "analysis" / "x.py").write_text("x = 1\n")

    with pytest.raises(ValueError, match=r"eqasim source changed since plan"):
        run_from_meta_config(meta)
    payload = analyze_from_meta_config(meta)

    assert payload["status"] == "completed"
    assert payload["completed"] == 1


def test_analyze_after_lockfile_and_config_change(tmp_path: Path, spy: None) -> None:
    meta, config = campaign(tmp_path)
    plan_from_meta_config(meta)
    assert run_from_meta_config(meta)["completed"] == 1
    (config.parent / "uv.lock").write_text("relocked\n")
    _edit_config(config, lambda document: document.update(run=["synthesis.output"]))

    # The current config no longer runs the stage writing 'age'...
    with pytest.raises(ValueError, match="analysis.synthesis.population"):
        load_meta_config(meta, verify_data=False)
    # ...but the sealed plan does, and analysis only needs the sealed plan.
    payload = analyze_from_meta_config(meta)

    assert payload["status"] == "completed"
    assert payload["completed"] == 1


def test_run_resume_after_config_change_names_it(tmp_path: Path, spy: None) -> None:
    meta, config = campaign(tmp_path)
    plan_from_meta_config(meta)
    assert run_from_meta_config(meta)["completed"] == 1
    _edit_config(config, lambda document: document["config"].update(new_option=3))

    with pytest.raises(ValueError) as error:
        run_from_meta_config(meta)
    assert str(error.value) == "eqasim config changed since plan"
    with pytest.raises(FileExistsError, match="plan differs"):
        plan_from_meta_config(meta)
    assert analyze_from_meta_config(meta)["status"] == "completed"


def test_infrastructure_key_change_keeps_plan_and_resume(
    tmp_path: Path, spy: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta, config = campaign(tmp_path, seeds=[41, 42])
    plan_from_meta_config(meta)
    plan_bytes = (tmp_path / "campaign" / "plan.parquet").read_bytes()
    # Seed 42 keeps failing, so the second run must resume it; the variable
    # stays set because the child environment is part of the machine identity.
    monkeypatch.setenv("EQASIM_SPY_FAIL_SEED", "42")
    first = run_from_meta_config(meta)
    assert (first["completed"], first["failed"]) == (1, 1)
    shutil.copytree(tmp_path / "data", tmp_path / "data-copy")

    def infrastructure(document: dict) -> None:
        document["config"].update(processes=2, data_path="../data-copy")
        document["working_directory"] = "other-cache"

    _edit_config(config, infrastructure)

    assert plan_from_meta_config(meta).status == "unchanged"
    assert (tmp_path / "campaign" / "plan.parquet").read_bytes() == plan_bytes
    resumed = run_from_meta_config(meta)
    assert (resumed["total"], resumed["failed"]) == (1, 1)
    loaded = load_meta_config(meta)
    assert loaded.execution.processes == 2
    assert loaded.execution.data_path == (tmp_path / "data-copy").resolve()


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (
            lambda document: document["parameters"].update(sampling_rate=[0.002]),
            "mismatch",
        ),
        (
            lambda document: document["outputs"]["age"].update(
                file="analysis_population/{prefix}distance.csv"
            ),
            "output 'age' changed since plan: file",
        ),
    ],
    ids=["factors", "output-file"],
)
def test_analyze_refuses_changed_factors_or_output_file(
    tmp_path: Path, spy: None, edit, message: str  # type: ignore[no-untyped-def]
) -> None:
    meta, _ = campaign(tmp_path)
    plan_from_meta_config(meta)
    assert run_from_meta_config(meta)["completed"] == 1
    _edit_meta(meta, edit)

    with pytest.raises(ValueError, match=message):
        analyze_from_meta_config(meta)
