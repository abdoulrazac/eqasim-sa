import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import pyarrow.parquet as pq
import yaml

from eqasim_sa.adapters.application import _design
from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.core.plan import build_plan
from tests.test_init import initialize
from tests.conftest import make_symlink


PROTOCOL = Path(__file__).resolve().parents[1] / "protocols/nantes-scientific"
RENDERER = PROTOCOL / "render_protocol.py"
TEMPLATES = (
    "01-canonical-morris-pilot.yml",
    "02-canonical-morris-production.yml",
    "03-multiscramble-fp.yml",
    "04-multiscramble-ff.yml",
)
SEED_POOL = list(range(45001, 45021))
PRIMARY_INDICATOR = "tv_distance(age, ref=INSEE)"


def _write_json(path: Path, document: dict) -> None:
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


EVIDENCE_FIELDS = {
    "replication-allocation.json": (
        "replicationAllocation",
        "replicationAllocationPath",
        "replicationAllocationSha256",
    ),
    "governed-report.json": (
        "governedReport",
        "governedReportPath",
        "governedReportSha256",
    ),
}


def _generation_id(filename: str) -> str:
    return hashlib.sha256(f"test-generation:{filename}".encode()).hexdigest()


def _evidence_path(root: Path, filename: str) -> Path:
    """Return the evidence file inside its v7 immutable analysis generation."""

    return root / "analysis" / "generations" / _generation_id(filename) / filename


def _reseal_evidence(root: Path, filename: str) -> None:
    """Rebind the v7 manifest output record and digest to the evidence bytes."""

    logical_name, path_field, digest_field = EVIDENCE_FIELDS[filename]
    manifest_path = root / "analysis-generation.json"
    manifest = json.loads(manifest_path.read_text())
    evidence = _evidence_path(root, filename)
    relative = evidence.relative_to(root).as_posix()
    digest = _digest(evidence)
    manifest.update(
        {
            "outputs": {
                logical_name: {
                    "path": relative,
                    "size": evidence.stat().st_size,
                    "sha256": digest,
                }
            },
            path_field: relative,
            digest_field: digest,
        }
    )
    _write_json(manifest_path, manifest)


def _write_generation_evidence(
    root: Path, filename: str, document: dict, manifest_fields: dict
) -> Path:
    """Write ``document`` and a v7 analysis-generation manifest selecting it."""

    evidence = _evidence_path(root, filename)
    evidence.parent.mkdir(parents=True, exist_ok=True)
    _write_json(evidence, document)
    generation_id = _generation_id(filename)
    _write_json(
        root / "analysis-generation.json",
        {
            "version": 7,
            "layout": "immutable-generation-v1",
            "generationId": generation_id,
            "generationPath": f"analysis/generations/{generation_id}",
            **manifest_fields,
        },
    )
    _reseal_evidence(root, filename)
    return evidence


def _write_pilot_evidence(
    root: Path,
    *,
    selected: int = 4,
    status: str = "recommended",
) -> Path:
    return _write_generation_evidence(
        root,
        "replication-allocation.json",
        {
            "version": 1,
            "status": status,
            "primaryIndicator": PRIMARY_INDICATOR,
            "varianceQuantile": 0.9,
            "varianceQuantileValue": 0.0004,
            "targetMcse": 0.01,
            "rawRequiredReplications": selected,
            "selectedReplications": selected,
            "currentReplications": 2,
            "maximumReplications": 20,
            "estimatedProductionPositions": 24 * 3 * selected,
            "availableRuns": 1440,
            "withinTarget": selected == 2,
            "seedPolicy": "canonical-morris-derived-crn-v1",
        },
        {
            "inferenceStatus": "pilot_only",
            "sensitivityMethod": "canonical_morris",
        },
    )


def _write_screening_evidence(
    root: Path,
    *,
    include_convergence: bool = True,
    claim_authorized: bool = False,
) -> Path:
    document = {
            "version": 1,
            "method": "canonical_morris",
            "decision": {
                "objective": "screening",
                "primary_indicator": PRIMARY_INDICATOR,
            },
            "completion": {"complete": True},
            "factorEvidence": [
                {
                    "factor": "matching_minimum_observations",
                    "classification": "screening",
                    "decisionRank": 1,
                    "estimate": 0.2,
                    "evidenceStatus": "available",
                },
                {
                    "factor": "matching_minimum_age",
                    "classification": "screening",
                    "decisionRank": 2,
                    "estimate": 0.1,
                    "evidenceStatus": "available",
                },
            ],
            "nonActionableFactors": [],
            "robustness": {
                "status": "pending",
                "checks": [{"name": "protocol follow-up", "status": "pending"}],
            },
            "reasonCodes": ["robustness_pending"],
            "inferenceStatus": "conditional",
            "claimAuthorized": claim_authorized,
        }
    if include_convergence:
        document["convergence"] = {"status": "met", "converged": True}
    return _write_generation_evidence(
        root,
        "governed-report.json",
        document,
        {"inferenceStatus": "conditional"},
    )


def _fixture_paths(
    tmp_path: Path,
    *,
    sampling_rate: float = 0.001,
    seal_upstream: bool = True,
) -> dict[str, Path]:
    model = tmp_path / "model's source"
    model.mkdir()
    data = tmp_path / "data with spaces"
    data.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}\n")
    config = model / "config.yml"
    config.write_text(
        yaml.safe_dump(
            {
                "working_directory": "/ignored",
                "run": ["synthesis.output", "analysis.synthesis.population"],
                "config": {
                    "matching_minimum_observations": 20,
                    "matching_minimum_age": 5,
                    "sampling_rate": sampling_rate,
                    "income_assignation_method": "uniform",
                    "hts": "edgt_44",
                    "departments": [44],
                    "random_seed": 9,
                    "processes": 1,
                    "data_path": str(data),
                },
            },
            sort_keys=False,
        )
    )
    lockfile = model / "uv.lock"
    lockfile.write_text("version = 1\n")
    pilot = tmp_path / "pilot evidence"
    screening = tmp_path / "screening evidence"
    paths = {
        "model": model,
        "config": config,
        "data": data,
        "scratch": scratch,
        "manifest": manifest,
        "lockfile": lockfile,
        "pilot": pilot,
        "screening": screening,
    }
    if seal_upstream:
        upstream = tmp_path / "sealed upstream configs"
        pilot_config = _render(TEMPLATES[0], upstream / "pilot.yml", paths, pilot)
        build_plan(_design(load_meta_config(pilot_config)))
        _write_pilot_evidence(pilot)
        production_config = _render(
            TEMPLATES[1], upstream / "production.yml", paths, screening
        )
        build_plan(_design(load_meta_config(production_config)))
        _write_screening_evidence(screening)
    return paths


def _render_command(
    template: str,
    destination: Path,
    paths: dict[str, Path],
    campaign_root: Path,
) -> list[str]:
    command = [
        "python3",
        str(RENDERER),
        str(PROTOCOL / template),
        str(destination),
        "--model-config",
        str(paths["config"]),
        "--model-root",
        str(paths["model"]),
        "--data-manifest",
        str(paths["manifest"]),
        "--lockfile",
        str(paths["lockfile"]),
        "--scratch",
        str(paths["scratch"]),
        "--data-path",
        str(paths["data"]),
        "--campaign-root",
        str(campaign_root),
    ]
    if template.startswith("02-"):
        command.extend(["--pilot-campaign-root", str(paths["pilot"])])
    if template.startswith(("03-", "04-")):
        command.extend(["--screening-campaign-root", str(paths["screening"])])
    return command


def _render(
    template: str,
    destination: Path,
    paths: dict[str, Path],
    campaign_root: Path,
) -> Path:
    subprocess.run(
        _render_command(template, destination, paths, campaign_root),
        check=True,
        capture_output=True,
        text=True,
    )
    return destination


def test_renderer_outputs_loadable_buildable_protocols_with_exact_contract(
    tmp_path: Path,
) -> None:
    paths = _fixture_paths(tmp_path)
    rendered = tmp_path / "rendered"
    expected = {
        TEMPLATES[0]: ("canonical_morris", "screening", 144, 1600, 0.1),
        TEMPLATES[1]: ("canonical_morris", "screening", 288, 1600, 0.1),
        TEMPLATES[2]: ("multiscramble_sobol", "fp", 1024, 1280, 0.2),
        TEMPLATES[3]: ("multiscramble_sobol", "ff", 2048, 2560, 0.2),
    }
    roots = []

    for index, (template, contract) in enumerate(expected.items()):
        root = tmp_path / "campaigns" / str(index)
        path = _render(template, rendered / template, paths, root)
        text = path.read_text()
        loaded = load_meta_config(path)
        plan = build_plan(_design(loaded))
        method, objective, positions, maximum, reserve = contract

        assert text != (PROTOCOL / template).read_text()
        assert "__MACHINE_" not in text and "__BLOCKED_" not in text
        assert loaded.method == method
        assert loaded.charter.decision.objective.value == objective
        assert plan.design_positions == positions
        assert loaded.charter.budget.maximum_runs == maximum
        assert loaded.charter.budget.reserve_fraction == reserve
        assert loaded.execution is not None
        assert loaded.execution.invocation == (
            "uv",
            "run",
            "--locked",
            "python",
            "-m",
            "synpp",
        )
        assert positions <= int(maximum * (1 - reserve))
        assert loaded.campaign_root == root.resolve()
        assert loaded.factors == {
            "matching_minimum_observations": [5, 20, 50],
            "matching_minimum_age": [0, 5, 12],
        }
        if method == "multiscramble_sobol":
            assert plan.unique_runs == 36
        roots.append(loaded.campaign_root)

    assert len(set(roots)) == len(TEMPLATES)
    pilot = load_meta_config(rendered / TEMPLATES[0])
    production = load_meta_config(rendered / TEMPLATES[1])
    allocation_digest = _digest(
        _evidence_path(paths["pilot"], "replication-allocation.json")
    )
    screening_digest = _digest(
        _evidence_path(paths["screening"], "governed-report.json")
    )
    assert pilot.trajectories == production.trajectories == 24
    assert pilot.seeds == tuple(SEED_POOL[:2])
    assert pilot.convergence_checkpoints == (8, 16, 24)
    assert production.convergence_checkpoints == (6, 12, 18, 24)
    assert production.charter.stopping[0].consecutive_checks == 3
    assert production.charter.stochastic.allocation.selected_replications == 4
    assert production.seeds == tuple(SEED_POOL[:4])
    assert production.charter.stochastic.allocation.provenance.endswith(allocation_digest)
    assert screening_digest in (rendered / TEMPLATES[2]).read_text()
    assert screening_digest in (rendered / TEMPLATES[3]).read_text()


def test_renderer_accepts_v7_immutable_generation_evidence(tmp_path: Path) -> None:
    paths = _fixture_paths(tmp_path)
    allocation = _evidence_path(paths["pilot"], "replication-allocation.json")
    report = _evidence_path(paths["screening"], "governed-report.json")

    production = _render(
        TEMPLATES[1],
        tmp_path / "production-v7.yml",
        paths,
        tmp_path / "production-v7",
    )
    prioritization = _render(
        TEMPLATES[2],
        tmp_path / "fp-v7.yml",
        paths,
        tmp_path / "fp-v7",
    )
    assert load_meta_config(production).method == "canonical_morris"
    assert _digest(allocation) in production.read_text()
    assert _digest(report) in prioritization.read_text()


def test_renderer_rejects_v6_analysis_generation_manifest(tmp_path: Path) -> None:
    paths = _fixture_paths(tmp_path)
    manifest_path = paths["pilot"] / "analysis-generation.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = 6
    _write_json(manifest_path, manifest)

    output = tmp_path / "production.yml"
    result = subprocess.run(
        _render_command(TEMPLATES[1], output, paths, tmp_path / "campaign"),
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "version must be 7" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("parent", ["results", ".eqasim-sa-analysis"])
def test_renderer_rejects_unrecognized_v7_generation_parent(
    tmp_path: Path, parent: str
) -> None:
    paths = _fixture_paths(tmp_path)
    manifest_path = paths["pilot"] / "analysis-generation.json"
    manifest = json.loads(manifest_path.read_text())
    generation_id = manifest["generationId"]
    unsupported_parent = f"{parent}/generations/{generation_id}"
    unsupported_path = f"{unsupported_parent}/replication-allocation.json"
    manifest["generationPath"] = unsupported_parent
    manifest["replicationAllocationPath"] = unsupported_path
    manifest["outputs"]["replicationAllocation"]["path"] = unsupported_path
    _write_json(manifest_path, manifest)

    output = tmp_path / "unsupported-parent.yml"
    result = subprocess.run(
        _render_command(TEMPLATES[1], output, paths, tmp_path / "campaign"),
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "explicitly supported immutable generation" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("unsafe", ["/absolute.json", "../escape.json"])
def test_renderer_rejects_unsafe_v7_evidence_paths(
    tmp_path: Path, unsafe: str
) -> None:
    paths = _fixture_paths(tmp_path)
    manifest_path = paths["pilot"] / "analysis-generation.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["replicationAllocationPath"] = unsafe
    manifest["outputs"]["replicationAllocation"]["path"] = unsafe
    _write_json(manifest_path, manifest)

    output = tmp_path / "unsafe.yml"
    result = subprocess.run(
        _render_command(TEMPLATES[1], output, paths, tmp_path / "campaign"),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "immutable generation" in result.stderr or "safe relative" in result.stderr
    assert not output.exists()


def test_renderer_rejects_symlinked_v7_generation_component(tmp_path: Path) -> None:
    paths = _fixture_paths(tmp_path)
    evidence = _evidence_path(paths["pilot"], "replication-allocation.json")
    generation = evidence.parent
    actual = generation.with_name("actual-generation")
    generation.rename(actual)
    make_symlink(generation, actual.name, target_is_directory=True)

    output = tmp_path / "symlinked-v7.yml"
    result = subprocess.run(
        _render_command(TEMPLATES[1], output, paths, tmp_path / "campaign"),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "must not traverse symlinks" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("selected", [1, 21])
def test_renderer_rejects_allocation_outside_preregistered_pool(
    tmp_path: Path, selected: int
) -> None:
    paths = _fixture_paths(tmp_path)
    _write_pilot_evidence(paths["pilot"], selected=selected)
    result = subprocess.run(
        _render_command(TEMPLATES[1], tmp_path / "production.yml", paths, tmp_path / "campaign"),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "selectedReplications must be within [2, 20]" in result.stderr
    assert not (tmp_path / "production.yml").exists()


def test_renderer_uses_exact_full_seed_prefix_at_upper_bound(tmp_path: Path) -> None:
    paths = _fixture_paths(tmp_path)
    _write_pilot_evidence(paths["pilot"], selected=20)
    output = _render(
        TEMPLATES[1],
        tmp_path / "production.yml",
        paths,
        tmp_path / "campaign",
    )
    loaded = load_meta_config(output)
    assert loaded.seeds == tuple(SEED_POOL)
    assert loaded.charter.stochastic.allocation.selected_replications == 20
    assert build_plan(_design(loaded)).design_positions == 1440


def test_renderer_rejects_tampered_evidence(tmp_path: Path) -> None:
    paths = _fixture_paths(tmp_path)
    output = tmp_path / "production.yml"
    with _evidence_path(paths["pilot"], "replication-allocation.json").open(
        "a"
    ) as stream:
        stream.write(" ")
    result = subprocess.run(
        _render_command(TEMPLATES[1], output, paths, tmp_path / "campaign"),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "does not match replication-allocation.json" in result.stderr
    assert not output.exists()


def test_renderer_rejects_missing_upstream_plan(tmp_path: Path) -> None:
    paths = _fixture_paths(tmp_path)
    (paths["pilot"] / "plan.parquet").unlink()
    output = tmp_path / "production.yml"
    result = subprocess.run(
        _render_command(TEMPLATES[1], output, paths, tmp_path / "campaign"),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "plan.parquet must be a regular non-symlink file" in result.stderr
    assert not output.exists()


def test_renderer_rejects_tampered_upstream_plan_metadata(tmp_path: Path) -> None:
    paths = _fixture_paths(tmp_path)
    plan_path = paths["pilot"] / "plan.parquet"
    table = pq.read_table(plan_path)
    metadata = dict(table.schema.metadata or {})
    design = json.loads(metadata[b"canonicalMorrisDesign"])
    design["trajectories"] = 23
    metadata[b"canonicalMorrisDesign"] = json.dumps(
        design, sort_keys=True, separators=(",", ":")
    ).encode()
    pq.write_table(table.replace_schema_metadata(metadata), plan_path)
    output = tmp_path / "production.yml"
    result = subprocess.run(
        _render_command(TEMPLATES[1], output, paths, tmp_path / "campaign"),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "canonical Morris protocol geometry" in result.stderr
    assert not output.exists()


def test_renderer_rejects_plan_from_different_context(tmp_path: Path) -> None:
    paths = _fixture_paths(tmp_path)
    plan_path = paths["pilot"] / "plan.parquet"
    table = pq.read_table(plan_path)
    metadata = dict(table.schema.metadata or {})
    descriptor = json.loads(metadata[b"contextDescriptor"])
    descriptor["dataManifestSha256"] = "0" * 64
    metadata[b"contextDescriptor"] = json.dumps(
        descriptor, sort_keys=True, separators=(",", ":")
    ).encode()
    pq.write_table(table.replace_schema_metadata(metadata), plan_path)
    output = tmp_path / "production.yml"
    result = subprocess.run(
        _render_command(TEMPLATES[1], output, paths, tmp_path / "campaign"),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "contextDescriptor does not match current renderer inputs" in result.stderr
    assert not output.exists()


def test_renderer_rejects_incompatible_upstream_plan_phase(tmp_path: Path) -> None:
    paths = _fixture_paths(tmp_path)
    shutil.copyfile(
        paths["screening"] / "plan.parquet", paths["pilot"] / "plan.parquet"
    )
    output = tmp_path / "production.yml"
    result = subprocess.run(
        _render_command(TEMPLATES[1], output, paths, tmp_path / "campaign"),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "canonical Morris protocol geometry" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("equivalent", [False, True])
def test_renderer_rejects_same_or_equivalent_evidence_root(
    tmp_path: Path, equivalent: bool
) -> None:
    paths = _fixture_paths(tmp_path)
    root = paths["pilot"]
    campaign = root.parent / "unused" / ".." / root.name if equivalent else root
    output = tmp_path / "not-created" / "production.yml"
    result = subprocess.run(
        _render_command(TEMPLATES[1], output, paths, campaign),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "campaign root must be distinct from its evidence root" in result.stderr
    assert not output.parent.exists()


def test_renderer_rejects_nonfinite_json_number(tmp_path: Path) -> None:
    paths = _fixture_paths(tmp_path)
    allocation = _evidence_path(paths["pilot"], "replication-allocation.json")
    allocation.write_text(allocation.read_text().replace("0.0004", "1e999"))
    _reseal_evidence(paths["pilot"], "replication-allocation.json")
    output = tmp_path / "production.yml"
    result = subprocess.run(
        _render_command(TEMPLATES[1], output, paths, tmp_path / "campaign"),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "non-finite JSON number" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("status", ["capped", "infeasible"])
def test_renderer_rejects_nonrecommended_allocation_status(
    tmp_path: Path, status: str
) -> None:
    paths = _fixture_paths(tmp_path)
    _write_pilot_evidence(paths["pilot"], status=status)
    output = tmp_path / "production.yml"
    result = subprocess.run(
        _render_command(TEMPLATES[1], output, paths, tmp_path / "campaign"),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "allocation.status must equal 'recommended'" in result.stderr
    assert not output.exists()


def test_renderer_rejects_screening_without_convergence(tmp_path: Path) -> None:
    paths = _fixture_paths(tmp_path)
    _write_screening_evidence(paths["screening"], include_convergence=False)
    output = tmp_path / "fp.yml"
    result = subprocess.run(
        _render_command(TEMPLATES[2], output, paths, tmp_path / "campaign"),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "report.convergence must be a JSON object" in result.stderr
    assert not output.exists()


def test_renderer_rejects_overclaiming_screening_report(tmp_path: Path) -> None:
    paths = _fixture_paths(tmp_path)
    _write_screening_evidence(paths["screening"], claim_authorized=True)
    output = tmp_path / "fp.yml"
    result = subprocess.run(
        _render_command(TEMPLATES[2], output, paths, tmp_path / "campaign"),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "report.claimAuthorized must equal False" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("robustness_status", "report.robustness.status must equal 'pending'"),
        ("empty_checks", "report.robustness.checks must be a nonempty array"),
        ("completed_check", "report.robustness.checks[0].status must equal 'pending'"),
        ("missing_reason", "report.reasonCodes must include 'robustness_pending'"),
    ],
)
def test_renderer_requires_pending_robustness_follow_up(
    tmp_path: Path, mutation: str, message: str
) -> None:
    paths = _fixture_paths(tmp_path)
    report = _evidence_path(paths["screening"], "governed-report.json")
    document = json.loads(report.read_text())
    if mutation == "robustness_status":
        document["robustness"]["status"] = "met"
    elif mutation == "empty_checks":
        document["robustness"]["checks"] = []
    elif mutation == "completed_check":
        document["robustness"]["checks"][0]["status"] = "completed"
    else:
        document["reasonCodes"] = []
    _write_json(report, document)
    _reseal_evidence(paths["screening"], "governed-report.json")
    output = tmp_path / "fp.yml"
    result = subprocess.run(
        _render_command(TEMPLATES[2], output, paths, tmp_path / "campaign"),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert message in result.stderr
    assert not output.exists()


def test_renderer_rejects_symlinked_evidence_file(tmp_path: Path) -> None:
    paths = _fixture_paths(tmp_path)
    allocation = _evidence_path(paths["pilot"], "replication-allocation.json")
    target = allocation.with_name("actual-allocation.json")
    allocation.rename(target)
    make_symlink(allocation, target.name)
    output = tmp_path / "production.yml"
    result = subprocess.run(
        _render_command(TEMPLATES[1], output, paths, tmp_path / "campaign"),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "must not traverse symlinks" in result.stderr
    assert not output.exists()


def test_renderer_rejects_symlinked_upstream_plan(tmp_path: Path) -> None:
    paths = _fixture_paths(tmp_path)
    plan = paths["pilot"] / "plan.parquet"
    target = paths["pilot"] / "actual-plan.parquet"
    plan.rename(target)
    make_symlink(plan, target.name)
    output = tmp_path / "production.yml"
    result = subprocess.run(
        _render_command(TEMPLATES[1], output, paths, tmp_path / "campaign"),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "plan.parquet must be a regular non-symlink file" in result.stderr
    assert not output.exists()


def test_renderer_accepts_linked_ancestor_of_evidence_root(tmp_path: Path) -> None:
    paths = _fixture_paths(tmp_path)
    linked_parent = tmp_path / "linked evidence"
    make_symlink(linked_parent, paths["pilot"].parent, target_is_directory=True)
    paths["pilot"] = linked_parent / paths["pilot"].name
    output = tmp_path / "production.yml"
    result = subprocess.run(
        _render_command(TEMPLATES[1], output, paths, tmp_path / "campaign"),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert load_meta_config(output).method == "canonical_morris"


def test_render_protocol_accepts_crlf_manifest(tmp_path: Path) -> None:
    paths = _fixture_paths(tmp_path)
    paths["manifest"].write_bytes(b"{ }\r\n")
    output = tmp_path / "production.yml"
    result = subprocess.run(
        _render_command(TEMPLATES[1], output, paths, tmp_path / "campaign"),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    loaded = load_meta_config(output)
    assert build_plan(_design(loaded)).design_positions == 288


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--production-r", "2"),
        ("--pilot-digest", "a" * 64),
        ("--screening-digest", "b" * 64),
    ],
)
def test_renderer_rejects_user_supplied_gate_values(
    tmp_path: Path, option: str, value: str
) -> None:
    paths = _fixture_paths(tmp_path)
    output = tmp_path / "production.yml"
    command = _render_command(TEMPLATES[1], output, paths, tmp_path / "campaign")
    result = subprocess.run([*command, option, value], capture_output=True, text=True)
    assert result.returncode != 0
    assert f"unrecognized arguments: {option} {value}" in result.stderr
    assert not output.exists()


def test_renderer_rejects_fixed_model_contradiction(tmp_path: Path) -> None:
    paths = _fixture_paths(tmp_path, sampling_rate=0.01, seal_upstream=False)
    output = tmp_path / "pilot.yml"
    result = subprocess.run(
        _render_command(TEMPLATES[0], output, paths, tmp_path / "campaign"),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "sampling_rate=0.01" in result.stderr
    assert not output.exists()


def test_renderer_refuses_to_overwrite_existing_output(tmp_path: Path) -> None:
    paths = _fixture_paths(tmp_path)
    output = tmp_path / "pilot.yml"
    output.write_text("keep me\n")
    result = subprocess.run(
        _render_command(TEMPLATES[0], output, paths, tmp_path / "campaign"),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "refusing to overwrite" in result.stderr
    assert output.read_text() == "keep me\n"


def test_protocols_and_renderer_are_isolated_from_package_and_init(tmp_path: Path) -> None:
    result, initialized, _, _ = initialize(tmp_path)
    packaged_templates = Path(__file__).resolve().parents[1] / "src/eqasim_sa/templates"

    assert RENDERER.is_file()
    assert not (PROTOCOL / "__init__.py").exists()
    assert all((PROTOCOL / name).is_file() for name in TEMPLATES)
    assert all(not (packaged_templates / name).exists() for name in TEMPLATES)
    assert set(result["metaConfigs"]) == {
        "calibration",
        "grid",
        "discrete_oat",
        "sobol",
    }
    assert not any(initialized.rglob("render_protocol.py"))
    assert all(not any(initialized.rglob(name)) for name in TEMPLATES)


def test_protocols_rely_only_on_upstream_eqasim_names() -> None:
    forbidden = (
        "matching_" + "age_scheme",
        "analysis_" + "age_scheme",
        "projection_" + "year",
        "config_local_" + "nantes",
        "analysis_7",
        "Matching and analysis age schemes",
    )
    for path in (*(PROTOCOL / name for name in TEMPLATES), RENDERER, PROTOCOL / "README.md"):
        text = path.read_text(encoding="utf-8")
        assert not [word for word in forbidden if word in text], path


def test_protocol_readme_requires_user_copy_with_analysis_stage() -> None:
    text = (PROTOCOL / "README.md").read_text(encoding="utf-8")
    assert "analysis.synthesis.population" in text
    assert "your own copy of the Nantes configuration" in text
