import copy
import importlib.metadata
import json
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.core.analyze import analyze
from eqasim_sa.core.ledger import RunRecord
from eqasim_sa.core.plan import build_plan
from tests.test_analyze import (
    _age_indicator,
    _sobol_campaign,
    _write_verified_run,
)
from tests.test_canonical_morris_analyze import _completed_campaign
from tests.test_canonical_morris_plan import canonical_design, canonical_meta, discrete_design
from tests.test_charter import _scientific
from tests.test_governed_reporting import _grid_with_duplicate_positions
from tests.test_init import initialize
from tests.test_multiscramble_sobol_plan import multiscramble_design
from tests.test_nantes_scientific_protocol import (
    TEMPLATES,
    _evidence_path,
    _fixture_paths,
    _render_command,
)
from tests.test_plan import design as grid_design
from tests.test_plan import meta_fixture, sobol_design
from tests.test_structured_configurations import _grid_design as structured_grid_design
from tests.provenance_helpers import write_csv
from tests.provenance_helpers import execution_evidence, scientific_indicator


def _metadata(root: Path, design) -> dict[bytes, bytes]:  # type: ignore[no-untyped-def]
    build_plan(design)
    return dict(pq.read_table(root / "plan.parquet").schema.metadata or {})


def test_release_version_and_plan_schema_matrix(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((project / "pyproject.toml").read_text())
    lock = tomllib.loads((project / "uv.lock").read_text())
    assert importlib.metadata.version("eqasim-sa") == "0.3.0"
    assert pyproject["project"]["version"] == "0.3.0"
    locked_package = next(
        package for package in lock["package"] if package["name"] == "eqasim-sa"
    )
    assert locked_package["version"] == "0.3.0"
    assert pyproject["tool"]["setuptools"]["package-data"] == {
        "eqasim_sa": ["templates/*.yml", "templates/*.md"]
    }
    dependencies = set(pyproject["project"]["dependencies"])
    assert "pandas==2.3.3" in dependencies
    assert "matplotlib==3.10.8" in dependencies
    assert next(
        package for package in lock["package"] if package["name"] == "pandas"
    )["version"] == "2.3.3"
    assert next(
        package for package in lock["package"] if package["name"] == "matplotlib"
    )["version"] == "3.10.8"

    cases = (
        ("grid", grid_design(tmp_path / "grid"), b"1", None),
        (
            "discrete_oat",
            discrete_design(tmp_path / "discrete"),
            b"1",
            b"discreteOatDesignVersion",
        ),
        ("sobol", sobol_design(tmp_path / "sobol"), b"1", b"sobolDesignVersion"),
        (
            "canonical_morris",
            canonical_design(tmp_path / "canonical"),
            b"2",
            b"canonicalMorrisDesignVersion",
        ),
        (
            "multiscramble_sobol",
            multiscramble_design(tmp_path / "multiscramble"),
            b"2",
            b"multiScrambleSobolDesignVersion",
        ),
    )

    for method, design, schema_version, design_version_key in cases:
        metadata = _metadata(design.campaign_root, design)
        assert metadata[b"planSchemaVersion"] == schema_version, method
        assert metadata[b"canonicalizationVersion"] == b"1", method
        if design_version_key is not None:
            expected_version = (
                b"3"
                if method in {"canonical_morris", "multiscramble_sobol"}
                else b"1"
            )
            assert metadata[design_version_key] == expected_version, method
        descriptor = json.loads(metadata[b"contextDescriptor"])
        assert descriptor == design.context_descriptor
        assert set(descriptor) == {
            "eqasimSourceSha256",
            "dataManifestSha256",
            "lockfileSha256",
            "eqasimSaVersion",
        }

    structured = structured_grid_design(tmp_path / "structured-grid")
    structured_metadata = _metadata(structured.campaign_root, structured)
    assert structured_metadata[b"planSchemaVersion"] == b"3"
    assert structured_metadata[b"structuredFactorsVersion"] == b"1"
    assert json.loads(structured_metadata[b"structuredFactors"]) == (
        structured.structured_factors
    )


def test_release_wheel_verifier_without_prebuilt_distribution(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[1]
    wheel = tmp_path / "eqasim_sa-0.3.0-py3-none-any.whl"
    required = (
        "00-calibration.yml",
        "01-grid.yml",
        "02-discrete-oat.yml",
        "03-sobol.yml",
        "README.md",
        "__init__.py",
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        for name in required:
            archive.writestr(f"eqasim_sa/templates/{name}", "fixture")
        archive.writestr("eqasim_sa/__init__.py", "")
    completed = subprocess.run(
        [sys.executable, project / "verify_release_wheel.py", wheel],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "verified wheel resource boundary" in completed.stdout


def test_release_init_and_package_boundaries(tmp_path: Path) -> None:
    result, initialized, _, _ = initialize(tmp_path)
    package_templates = Path(__file__).resolve().parents[1] / "src/eqasim_sa/templates"

    assert set(result["metaConfigs"]) == {
        "calibration",
        "grid",
        "discrete_oat",
        "sobol",
    }
    for relative in result["metaConfigs"].values():
        assert "charter" not in (initialized / relative).read_text()
    assert not tuple(initialized.rglob("plan.parquet"))
    assert not tuple(initialized.rglob("render_protocol.py"))
    assert all(not (package_templates / name).exists() for name in TEMPLATES)


@pytest.mark.usefixtures("synthetic_indicator")
def test_release_analysis_status_and_governed_report_matrix(tmp_path: Path) -> None:
    demonstration = _grid_with_duplicate_positions(tmp_path / "demonstration")
    demonstration_result = analyze(
        demonstration,
        (_age_indicator(),),
        campaign="test",
        completeness_threshold=1.0,
    )
    demonstration_manifest = json.loads(
        (demonstration / "analysis-generation.json").read_text()
    )
    demonstration_report = json.loads(
        demonstration_result.governed_report_path.read_text()
    )
    assert demonstration_manifest["version"] == 8
    assert demonstration_result.inference_status == "demonstration_noninferential"
    assert demonstration_report["claimAuthorized"] is False

    sobol = _sobol_campaign(tmp_path / "sobol", n=8)
    sobol_result = analyze(
        sobol,
        (_age_indicator(),),
        campaign="test",
        completeness_threshold=1.0,
        seed_role="factor",
        method="sobol",
        bootstrap_resamples=7,
        confidence_level=0.9,
    )
    assert sobol_result.inference_status == "demonstration_noninferential"
    assert json.loads(sobol_result.governed_report_path.read_text())[
        "claimAuthorized"
    ] is False

    pilot, pilot_indicator = _completed_campaign(tmp_path / "canonical-pilot")
    pilot_result = analyze(
        pilot.campaign_root,
        (pilot_indicator,),
        campaign="test",
        completeness_threshold=1.0,
        method="canonical_morris",
        bootstrap_resamples=pilot.bootstrap_resamples,
        confidence_level=pilot.confidence_level,
        convergence_checkpoints=pilot.convergence_checkpoints,
        screening_top_k=pilot.screening_top_k,
    )
    assert pilot_result.inference_status == "pilot_only"

    production_contract = copy.deepcopy(pilot.scientific_contract)
    production_contract["charter"]["stochastic"]["allocation"].update(
        phase="production",
        selected_replications=2,
        provenance="pilot/replication-allocation.json sha256:abc",
    )
    production_contract["charter"]["stopping"][0]["threshold"] = 1e-12
    production, production_indicator = _completed_campaign(
        tmp_path / "canonical-production",
        scientific_contract=production_contract,
    )
    production_result = analyze(
        production.campaign_root,
        (production_indicator,),
        campaign="test",
        completeness_threshold=1.0,
        method="canonical_morris",
        bootstrap_resamples=production.bootstrap_resamples,
        confidence_level=production.confidence_level,
        convergence_checkpoints=production.convergence_checkpoints,
        screening_top_k=production.screening_top_k,
    )
    assert production_result.inference_status == "conditional"

    original_multiscramble = multiscramble_design(tmp_path / "unused-multiscramble")
    multiscramble_contract = copy.deepcopy(original_multiscramble.scientific_contract)
    multiscramble_contract["charter"]["decision"]["primary_indicator"] = "tv_distance(age, ref=reference)"
    multiscramble_contract["charter"]["stopping"][0]["threshold"] = 1e-12
    multiscramble = multiscramble_design(
        tmp_path / "multiscramble-analysis",
        scientific_contract=multiscramble_contract,
    )
    build_plan(multiscramble)
    plan = pq.read_table(multiscramble.campaign_root / "plan.parquet")
    for run_id, row in {
        row["runId"]: row for row in plan.to_pylist()
    }.items():
        value = float(row["a"] + row["seed"] / 10 + row["a"] * row["seed"] / 20)
        write_csv(
            pa.table({"synthetic": [value]}),
            multiscramble.campaign_root / "archives" / run_id / "age.csv",
        )
        _write_verified_run(
            multiscramble.campaign_root,
            RunRecord(
                run_id,
                "test",
                row["seed"],
                multiscramble.context_hash,
                "node",
                1.0,
                {"a": row["a"]},
            ),
            execution_evidence(multiscramble.context_hash),
        )
    multiscramble_result = analyze(
        multiscramble.campaign_root,
        (scientific_indicator("age"),),
        campaign="test",
        completeness_threshold=1.0,
        seed_role="factor",
        method="multiscramble_sobol",
        confidence_level=multiscramble.confidence_level,
    )
    assert multiscramble_result.inference_status == "unstable"

    for result, expected_status in (
        (pilot_result, "pilot_only"),
        (production_result, "conditional"),
        (multiscramble_result, "unstable"),
    ):
        manifest = json.loads(
            (result.path.parents[3] / "analysis-generation.json").read_text()
        )
        report = json.loads(result.governed_report_path.read_text())
        assert manifest["version"] == 8
        assert manifest["inferenceStatus"] == expected_status
        assert report["inferenceStatus"] == expected_status
        assert report["robustness"]["status"] == "pending"
        assert report["claimAuthorized"] is False
        assert "robustness_pending" in report["reasonCodes"]


def test_release_refuses_method_contract_substitution(tmp_path: Path) -> None:
    canonical_path = canonical_meta(tmp_path / "canonical-meta")
    canonical_document = yaml.safe_load(canonical_path.read_text())
    canonical_document["charter"] = {"status": "demonstration"}
    for parameter in canonical_document["parameters"].values():
        parameter.pop("distribution")
    canonical_path.write_text(yaml.safe_dump(canonical_document, sort_keys=False))

    with pytest.raises(ValueError, match="canonical_morris.*scientific"):
        load_meta_config(canonical_path)

    for method in ("discrete_oat", "sobol"):
        root = tmp_path / f"demonstration-{method}"
        root.mkdir()
        meta, _, _, _ = meta_fixture(root)
        document = _scientific(meta)
        if method == "discrete_oat":
            document["experiment"] = {
                "method": "discrete_oat",
                "seeds": [41, 42],
                "trajectories": 2,
                "design_seed": 1,
            }
            document["analysis"] = {"completeness_threshold": 1.0}
        else:
            document["experiment"] = {
                "method": "sobol",
                "seeds": [41, 42],
                "base_sample_size": 4,
                "design_seed": 1,
            }
            document["analysis"] = {
                "completeness_threshold": 1.0,
                "bootstrap_resamples": 7,
                "confidence_level": 0.9,
            }
        meta.write_text(yaml.safe_dump(document, sort_keys=False))
        with pytest.raises(ValueError, match=f"{method}.*demonstration"):
            load_meta_config(meta)

    canonical = canonical_design(tmp_path / "wrong-canonical")
    wrong_canonical = copy.deepcopy(canonical.scientific_contract)
    wrong_canonical["charter"]["decision"]["objective"] = "fp"
    with pytest.raises(ValueError, match="screening"):
        build_plan(
            canonical_design(
                tmp_path / "wrong-canonical-plan",
                scientific_contract=wrong_canonical,
            )
        )

    wrong_canonical_stochastic = copy.deepcopy(canonical.scientific_contract)
    wrong_canonical_stochastic["charter"]["stochastic"] = copy.deepcopy(
        multiscramble_design(tmp_path / "stochastic-source").scientific_contract[
            "charter"
        ]["stochastic"]
    )
    with pytest.raises(ValueError, match="conditional_mean"):
        build_plan(
            canonical_design(
                tmp_path / "wrong-canonical-stochastic-plan",
                scientific_contract=wrong_canonical_stochastic,
            )
        )

    multiscramble = multiscramble_design(tmp_path / "wrong-multiscramble")
    wrong_multiscramble = copy.deepcopy(multiscramble.scientific_contract)
    wrong_multiscramble["charter"]["stochastic"] = wrong_canonical["charter"][
        "stochastic"
    ]
    with pytest.raises(ValueError, match="finite_seed_factor"):
        build_plan(
            multiscramble_design(
                tmp_path / "wrong-multiscramble-plan",
                scientific_contract=wrong_multiscramble,
            )
        )

    wrong_multiscramble_objective = copy.deepcopy(multiscramble.scientific_contract)
    wrong_multiscramble_objective["charter"]["decision"]["objective"] = "screening"
    with pytest.raises(ValueError, match="ff, fp"):
        build_plan(
            multiscramble_design(
                tmp_path / "wrong-multiscramble-objective-plan",
                scientific_contract=wrong_multiscramble_objective,
            )
        )


def test_release_nantes_gates_refuse_without_output(tmp_path: Path) -> None:
    paths = _fixture_paths(tmp_path)
    pilot_plan = paths["pilot"] / "plan.parquet"
    plan_bytes = pilot_plan.read_bytes()
    allocation = _evidence_path(paths["pilot"], "replication-allocation.json")
    allocation_bytes = allocation.read_bytes()

    pilot_plan.unlink()
    missing_output = tmp_path / "missing.yml"
    missing = subprocess.run(
        _render_command(TEMPLATES[1], missing_output, paths, tmp_path / "missing-root"),
        capture_output=True,
        text=True,
    )
    assert missing.returncode != 0
    assert "plan.parquet must be a regular non-symlink file" in missing.stderr
    assert not missing_output.exists()
    pilot_plan.write_bytes(plan_bytes)

    allocation.write_bytes(allocation_bytes + b" ")
    tampered_output = tmp_path / "tampered.yml"
    tampered = subprocess.run(
        _render_command(TEMPLATES[1], tampered_output, paths, tmp_path / "tampered-root"),
        capture_output=True,
        text=True,
    )
    assert tampered.returncode != 0
    assert "does not match replication-allocation.json" in tampered.stderr
    assert not tampered_output.exists()
    allocation.write_bytes(allocation_bytes)

    table = pq.read_table(pilot_plan)
    metadata = dict(table.schema.metadata or {})
    descriptor = json.loads(metadata[b"contextDescriptor"])
    descriptor["dataManifestSha256"] = "0" * 64
    metadata[b"contextDescriptor"] = json.dumps(
        descriptor, sort_keys=True, separators=(",", ":")
    ).encode()
    pq.write_table(table.replace_schema_metadata(metadata), pilot_plan)
    incompatible_output = tmp_path / "incompatible.yml"
    incompatible = subprocess.run(
        _render_command(
            TEMPLATES[1],
            incompatible_output,
            paths,
            tmp_path / "incompatible-root",
        ),
        capture_output=True,
        text=True,
    )
    assert incompatible.returncode != 0
    assert "contextDescriptor does not match current renderer inputs" in incompatible.stderr
    assert not incompatible_output.exists()
