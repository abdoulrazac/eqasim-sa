import hashlib
import importlib.metadata
import importlib.util
import json
import os
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

import pyarrow.parquet as pq

from eqasim_sa.adapters.fake.simulator import FakeSimulator
from eqasim_sa.core.analyze import analyze
from eqasim_sa.core.indicators import RegisteredIndicator
from eqasim_sa.core.plan import (
    DiscreteOATDesign,
    GridDesign,
    SobolDesign,
    _table,
    build_plan,
    load_run_specs,
)
from eqasim_sa.core.run import execute_run
from tests.provenance_helpers import context_hash


def installed_script(name: str) -> Path:
    script = Path(sys.executable).with_name(name)
    assert script.is_file()
    return script


def test_installed_metadata_and_commands(tmp_path: Path) -> None:
    requirements = importlib.metadata.requires("eqasim-sa") or []
    runtime = [
        requirement for requirement in requirements if "extra ==" not in requirement
    ]
    assert runtime == [
        "pyarrow==23.0.1",
        "PyYAML==6.0.3",
        "submitit==1.5.4",
        "numpy==2.3.5",
        "pandas==2.3.3",
        "matplotlib==3.10.8",
        "SALib==1.5.2",
        "scipy==1.17.1",
    ]
    assert "synpp" not in runtime
    help_result = subprocess.run(
        [installed_script("eqasim-sa"), "--help"],
        cwd=tmp_path,
        check=True,
        text=True,
        capture_output=True,
    )
    init_help = subprocess.run(
        [installed_script("eqasim-sa"), "init", "--help"],
        cwd=tmp_path,
        check=True,
        text=True,
        capture_output=True,
    )
    subprocess.run(
        [installed_script("eqasim-sa-fake-pipeline"), "--help"],
        cwd=tmp_path,
        check=True,
    )
    assert "init" in help_result.stdout
    assert all(
        option in init_help.stdout
        for option in (
            "--project-name",
            "--directory",
            "--model-root",
            "--regenerate-data-manifest",
            "--data-path",
            "--scratch",
            "--invocation",
        )
    )
    assert "--data-manifest" not in init_help.stdout
    normalized_init_help = " ".join(init_help.stdout.split())
    assert all(
        explanation in normalized_init_help
        for explanation in (
            "default: ./NAME",
            "default: ./eqasim-france",
            "relative to --model-root",
            "<directory>/manifest.json",
            "default: ./eqasim-data",
            "created when absent",
            "default: <directory>/scratch",
            "repeat in argv order",
            "without planning or executing any campaign",
        )
    )
    resources = {
        name: files("eqasim_sa.templates").joinpath(name)
        for name in (
            "README.md", "00-calibration.yml", "01-grid.yml", "02-discrete-oat.yml", "03-sobol.yml"
        )
    }
    assert all(resource.is_file() for resource in resources.values())
    readme = resources["README.md"].read_text()
    assert "@RUNNER@" in readme
    assert "It did not" in readme and '"${SA[@]}" plan' in readme
    assert all(
        '@DATA_MANIFEST@' in resources[name].read_text()
        for name in ("00-calibration.yml", "01-grid.yml", "02-discrete-oat.yml", "03-sobol.yml")
    )
    assert "method: discrete_oat" in resources["02-discrete-oat.yml"].read_text()
    assert "design_seed: 20260817" in resources["02-discrete-oat.yml"].read_text()
    assert "method: sobol" in resources["03-sobol.yml"].read_text()
    assert "base_sample_size: 4" in resources["03-sobol.yml"].read_text()
    assert "matching_age_scheme" in resources["01-grid.yml"].read_text()
    assert "matching_age_scheme" not in resources["02-discrete-oat.yml"].read_text()
    assert "matching_age_scheme" in resources["03-sobol.yml"].read_text()
    starter_configs = [
        resources[name].read_text()
        for name in (
            "00-calibration.yml",
            "01-grid.yml",
            "02-discrete-oat.yml",
            "03-sobol.yml",
        )
    ]
    assert all(
        "HTS adjust with INSEE population weights" not in text
        for text in starter_configs
    )
    assert all(text.count("references: [INSEE]") == 1 for text in starter_configs)
    assert all(text.count("references: [HTS]") == 2 for text in starter_configs)

    base = {
        "config": {
            "sampling_rate": 0.001,
            "matching_age_scheme": "matching_6",
            "analysis_age_scheme": "analysis_7",
            "random_seed": 1,
        }
    }
    factors = {
        "sampling_rate": [0.001, 0.002],
        "matching_age_scheme": ["matching_6", "analysis_7"],
    }
    descriptor = {
        "eqasimCommit": "a",
        "dataManifestSha256": "b",
        "lockfileSha256": "c",
        "eqasimSaVersion": "d",
    }
    sealed_context_hash = context_hash(descriptor)
    grid = GridDesign(
        base,
        factors,
        (1201, 1202),
        {},
        sealed_context_hash,
        descriptor,
        Path("grid"),
    )
    grid_table = _table(grid, tuple(sorted(factors)))
    assert grid_table.num_rows == 8
    assert len(set(grid_table["runId"].to_pylist())) == 8
    oat = DiscreteOATDesign(
        base,
        {"sampling_rate": [0.001, 0.002, 0.003]},
        (1301, 1302),
        {},
        sealed_context_hash,
        descriptor,
        Path("discrete-oat"),
        2,
        20260817,
    )
    oat_table = _table(oat, ("sampling_rate",))
    assert oat_table.num_rows == 8
    assert len(set(oat_table["runId"].to_pylist())) == 6
    assert set(oat_table["sampling_rate"].to_pylist()) == {0.001, 0.002, 0.003}
    sobol = SobolDesign(
        base,
        factors,
        (1401, 1402),
        {},
        sealed_context_hash,
        descriptor,
        Path("sobol"),
        4,
        20260814,
    )
    sobol_table = _table(sobol, tuple(sorted({**factors, "seed": [1401, 1402]})))
    assert sobol_table.num_rows == 32
    assert len(set(sobol_table["runId"].to_pylist())) == 8


def test_installed_init_generates_template_without_campaign_execution(tmp_path: Path) -> None:
    model, data = tmp_path / "model", tmp_path / "data"
    model.mkdir()
    data.mkdir()
    (model / "config_local_nantes.yml").write_text(
        "working_directory: /cache\nrun: []\nconfig:\n"
        "  sampling_rate: 0.001\n  random_seed: 1\n"
        "  matching_age_scheme: matching_6\n  analysis_age_scheme: analysis_7\n"
    )
    (model / "uv.lock").write_text("version = 1\n")
    (data / "input.txt").write_text("installed wheel\n")

    completed = subprocess.run(
        [
            installed_script("eqasim-sa"), "init", "--project-name", "wheel-project",
            "--model-root", model, "--data-path", data,
        ],
        cwd=tmp_path,
        check=True,
        text=True,
        capture_output=True,
    )

    payload = json.loads(completed.stdout)
    root = tmp_path / "wheel-project"
    assert payload["manifest"] == "manifest.json"
    assert set(payload) == {
        "status", "initVersion", "projectName", "manifest", "readme",
        "metaConfigs", "restored", "elapsedSeconds", "error",
    }
    assert (root / "manifest.json").is_file()
    assert not any((root / "campaigns").iterdir())
    assert not tuple(root.rglob("plan.parquet"))
    assert all((root / path).is_file() for path in payload["metaConfigs"].values())
    readme = (root / "README.md").read_text()
    from eqasim_sa.adapters import templates

    source_project = Path(templates.__file__).resolve().parents[3]
    if (source_project / "pyproject.toml").is_file():
        assert f"--project {source_project}" in readme
    else:
        assert "SA=(eqasim-sa)" in readme
        assert "uv run --project" not in readme
    assert all(
        line.startswith('"${SA[@]}"')
        for line in readme.splitlines()
        if " --meta-config " in line
    )


def test_installed_wheel_sobol_plan_fake_runs_and_analysis_end_to_end(
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
    encoded = json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
    context_hash = hashlib.sha256(b"eqasim-sa:context:v2\0" + encoded).hexdigest()
    design = SobolDesign(
        {"config": {"a": 0}},
        {"a": [0, 1]},
        (11, 12),
        {"run": ["population"]},
        context_hash,
        descriptor,
        root,
        base_sample_size=8,
        design_seed=7,
    )
    planned = build_plan(design)
    specs = load_run_specs(root / "plan.parquet", design, "wheel-sobol")
    results = [execute_run(spec, FakeSimulator(), root, workdir) for spec in specs]
    indicator = RegisteredIndicator(
        "value",
        "population",
        lambda artifacts: float(
            pq.read_table(artifacts.path("population"))["value"][0].as_py()
        ),
    )
    result = analyze(
        root,
        (indicator,),
        campaign="wheel-sobol",
        completeness_threshold=1.0,
        seed_role="factor",
        method="sobol",
        bootstrap_resamples=7,
        confidence_level=0.9,
    )
    rows = pq.read_table(result.sensitivity_path).to_pylist()
    assert planned.design_positions == 48
    assert planned.unique_runs == len(specs) < planned.design_positions
    assert all(item.status == "completed" for item in results)
    assert result.sensitivity_method == "sobol"
    assert {row["factor"] for row in rows} == {"a", "seed"}


def test_canonical_spec_and_epics_match_architecture_contract() -> None:
    project = Path(__file__).resolve().parents[2]
    spec = (project / "_bmad-output/specs/spec-eqasim-sa/SPEC.md").read_text()
    epics = (project / "_bmad-output/planning-artifacts/epics.md").read_text()
    for document in (spec, epics):
        assert "AD-1" in document and "AD-20" in document
        assert "2 500 lignes" in document
        assert "N(2D+2) <= 2**63-1" in document
        assert "bootstrap_resamples" in document and "2**63-1" in document


def test_installed_plan_creates_replays_and_refuses_conflict(tmp_path: Path) -> None:
    model = tmp_path / "model"
    model.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=model, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.test"], cwd=model, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=model, check=True)
    (model / "config.yml").write_text(
        "working_directory: /ignored\nrun: [old]\nconfig:\n  rate: 0.1\n  sampling_rate: 0.1\n  random_seed: 9\n"
    )
    (model / "lock.yml").write_text("locked\n")
    subprocess.run(["git", "add", "."], cwd=model, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=model, check=True)
    (tmp_path / "manifest.json").write_text("{}\n")
    meta = tmp_path / "meta.yml"
    template = (
        "campaign: demo\ncampaign_root: {root}\nmodel:\n  config: model/config.yml\n  root: model\n"
        "context:\n  data_manifest: manifest.json\n  lockfile: model/lock.yml\n"
        "analysis:\n  completeness_threshold: 1.0\n"
        "experiment:\n  method: grid\n  seed_role: replication\n  replications:\n    seeds: [41, 42]\n"
        "parameters:\n  rate:\n    values: [{values}]\n"
    )
    meta.write_text(template.format(root="campaign", values="0.1, 0.2"))
    command = [installed_script("eqasim-sa"), "plan", "--meta-config", meta]
    created = subprocess.run(
        command, cwd=tmp_path, check=True, text=True, capture_output=True
    )
    before = (tmp_path / "campaign" / "plan.parquet").read_bytes()
    unchanged = subprocess.run(
        command, cwd=tmp_path, check=True, text=True, capture_output=True
    )
    meta.write_text(template.format(root="campaign", values="0.3"))
    failed = subprocess.run(command, cwd=tmp_path, text=True, capture_output=True)
    assert json.loads(created.stdout)["status"] == "created"
    assert json.loads(unchanged.stdout)["status"] == "unchanged"
    assert failed.returncode == 1 and json.loads(failed.stdout)["status"] == "failed"
    assert (tmp_path / "campaign" / "plan.parquet").read_bytes() == before
    meta.write_text(template.format(root="campaign-2", values="0.1, 0.2"))
    subprocess.run(command, cwd=tmp_path, check=True, text=True, capture_output=True)
    assert (tmp_path / "campaign-2" / "plan.parquet").read_bytes() == before
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    meta.write_text(
        meta.read_text() + "execution:\n  backend: local\n  workers: 2\n"
        f"  scratch: {scratch}\n  invocation: [{sys.executable}, "
        f"{Path(__file__).with_name('eqasim_spy.py')}]\n"
    )
    environment = os.environ.copy()
    environment["EQASIM_SPY_RECORD"] = str(tmp_path / "campaign-record.json")
    executed = subprocess.run(
        [installed_script("eqasim-sa"), "run", "--meta-config", meta],
        cwd=tmp_path,
        env=environment,
        check=True,
        text=True,
        capture_output=True,
    )
    assert json.loads(executed.stdout) == {
        "status": "completed",
        "total": 4,
        "completed": 4,
        "skipped": 0,
        "failed": 0,
        "failures": [],
        "error": None,
    }
    calibration = tmp_path / "calibration.yml"
    calibration.write_text(meta.read_text())
    meta.write_text(
        meta.read_text().replace(
            "campaign_root: campaign-2", "campaign_root: campaign-estimate"
        )
    )
    subprocess.run(
        [installed_script("eqasim-sa"), "plan", "--meta-config", meta],
        cwd=tmp_path,
        check=True,
    )
    estimate = subprocess.run(
        [
            installed_script("eqasim-sa"),
            "estimate",
            "--meta-config",
            meta,
            "--calibration-meta-config",
            calibration,
        ],
        cwd=tmp_path,
        check=True,
        text=True,
        capture_output=True,
    )
    payload = json.loads(estimate.stdout)
    marker_bytes = sum(
        path.stat().st_size for path in (tmp_path / "campaign-2" / "runs").iterdir()
    )
    archive_bytes = sum(
        path.stat().st_size
        for path in (tmp_path / "campaign-2" / "archives").rglob("*.parquet")
    )
    assert payload["status"] == "estimated" and payload["runs"] == 4
    assert payload["designPositions"] == 4 and payload["calibrationRuns"] == 4
    assert (
        payload["estimatedDiskBytes"]
        == (tmp_path / "campaign-estimate" / "plan.parquet").stat().st_size
        + marker_bytes
        + archive_bytes
    )
    static = subprocess.run(
        [
            installed_script("eqasim-sa"),
            "check",
            "--meta-config",
            meta,
            "--calibration-meta-config",
            calibration,
        ],
        cwd=tmp_path,
        env=environment,
        check=True,
        text=True,
        capture_output=True,
    )
    static_payload = json.loads(static.stdout)
    assert static_payload["status"] == "valid"
    assert [item["name"] for item in static_payload["checks"]] == [
        "metaConfig",
        "sealedPlan",
        "uniqueConfigSeedPairs",
        "eqasimEnvironment",
        "backend",
        "path_length",
        "diskSpace",
    ]
    smoke = subprocess.run(
        [
            installed_script("eqasim-sa"),
            "check",
            "--meta-config",
            meta,
            "--calibration-meta-config",
            calibration,
            "--smoke",
        ],
        cwd=tmp_path,
        env=environment,
        check=True,
        text=True,
        capture_output=True,
    )
    smoke_payload = json.loads(smoke.stdout)
    assert smoke_payload["status"] == "valid" and smoke_payload["checks"][-1] == {
        "name": "smoke",
        "status": "passed",
        "error": None,
    }
    meta.write_text(calibration.read_text())
    (tmp_path / "campaign-record.json").unlink()
    resumed = subprocess.run(
        [installed_script("eqasim-sa"), "run", "--meta-config", meta],
        cwd=tmp_path,
        env=environment,
        check=True,
        text=True,
        capture_output=True,
    )
    assert json.loads(resumed.stdout) == {
        "status": "completed",
        "total": 0,
        "completed": 0,
        "skipped": 0,
        "failed": 0,
        "failures": [],
        "error": None,
    }
    assert not (tmp_path / "campaign-record.json").exists()
    meta.write_text(template.format(root="campaign-concurrent", values="0.1, 0.2"))
    processes = [
        subprocess.Popen(command, cwd=tmp_path, text=True, stdout=subprocess.PIPE)
        for _ in range(2)
    ]
    concurrent = [process.communicate(timeout=30) for process in processes]
    assert all(process.returncode == 0 for process in processes)
    assert {json.loads(output)["status"] for output, _ in concurrent} == {
        "created",
        "unchanged",
    }


def test_installed_campaign_interrupts_resumes_then_is_noop(tmp_path: Path) -> None:
    model = tmp_path / "model"
    model.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=model, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.test"], cwd=model, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=model, check=True)
    (model / "config.yml").write_text(
        "working_directory: /x\nrun: []\nconfig:\n  rate: 0.1\n  random_seed: 9\n"
    )
    (model / "lock.yml").write_text("locked\n")
    subprocess.run(["git", "add", "."], cwd=model, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=model, check=True)
    (tmp_path / "manifest.json").write_text("{}\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    meta = tmp_path / "meta.yml"
    meta.write_text(
        "campaign: demo\ncampaign_root: campaign\nmodel:\n  config: model/config.yml\n  root: model\n"
        "context:\n  data_manifest: manifest.json\n  lockfile: model/lock.yml\n"
        "analysis:\n  completeness_threshold: 1.0\n"
        "experiment:\n  method: grid\n  seed_role: replication\n  replications:\n    seeds: [41]\nparameters:\n  rate:\n    values: [0.1]\n"
        f"execution:\n  backend: local\n  workers: 1\n  scratch: {scratch}\n"
        f"  invocation: [{sys.executable}, {Path(__file__).with_name('eqasim_spy.py')}]\n"
    )
    command = [installed_script("eqasim-sa"), "run", "--meta-config", meta]
    subprocess.run(
        [installed_script("eqasim-sa"), "plan", "--meta-config", meta],
        cwd=tmp_path,
        check=True,
    )
    environment = os.environ.copy()
    record = tmp_path / "record.json"
    environment.update(EQASIM_SPY_RECORD=str(record), EQASIM_SPY_SLEEP="60")
    process = subprocess.Popen(command, cwd=tmp_path, env=environment)
    deadline = __import__("time").monotonic() + 10
    while not record.exists() and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.02)
    assert record.exists()
    process.send_signal(__import__("signal").SIGINT)
    assert process.wait(timeout=10) == 130
    assert not (tmp_path / "campaign" / "runs").exists()
    environment.pop("EQASIM_SPY_SLEEP")
    resumed = subprocess.run(
        command,
        cwd=tmp_path,
        env=environment,
        check=True,
        text=True,
        capture_output=True,
    )
    noop = subprocess.run(
        command,
        cwd=tmp_path,
        env=environment,
        check=True,
        text=True,
        capture_output=True,
    )
    assert json.loads(resumed.stdout)["total"] == 1
    assert json.loads(noop.stdout) == {
        "status": "completed",
        "total": 0,
        "completed": 0,
        "skipped": 0,
        "failed": 0,
        "failures": [],
        "error": None,
    }


def test_installed_cli_completes_then_skips(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "working_directory": "/ignored",
                "run": ["fake.population"],
                "config": {"sampling_rate": 0.1, "processes": 3},
            }
        ),
        encoding="utf-8",
    )
    command = [
        installed_script("eqasim-sa"),
        "fake-run",
        "--campaign-root",
        tmp_path / "campaign",
        "--campaign",
        "demo",
        "--config",
        config,
        "--seed",
        "42",
        "--expected-context-hash",
        "c" * 64,
        "--current-context-hash",
        "c" * 64,
        "--workdir",
        tmp_path / "work",
    ]
    first = subprocess.run(
        command, cwd=tmp_path, check=True, text=True, capture_output=True
    )
    second = subprocess.run(
        command, cwd=tmp_path, check=True, text=True, capture_output=True
    )
    assert json.loads(first.stdout)["status"] == "completed"
    assert json.loads(second.stdout)["status"] == "skipped"
    run_id = json.loads(first.stdout)["runId"]
    assert pq.read_table(
        tmp_path / "campaign" / "runs" / f"{run_id}.parquet"
    ).column_names[-2:] == [
        "sampling_rate",
        "processes",
    ]


def test_fake_simulator_prefers_script_next_to_python(tmp_path: Path) -> None:
    injected = tmp_path / "eqasim-sa-fake-pipeline"
    injected.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    injected.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{tmp_path}:{environment['PATH']}"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"run": [], "config": {}}), encoding="utf-8")
    command = [
        installed_script("eqasim-sa"),
        "fake-run",
        "--campaign-root",
        tmp_path / "campaign",
        "--campaign",
        "demo",
        "--config",
        config,
        "--seed",
        "42",
        "--expected-context-hash",
        "c" * 64,
        "--current-context-hash",
        "c" * 64,
        "--workdir",
        tmp_path / "work",
    ]
    result = subprocess.run(
        command, cwd=tmp_path, env=environment, text=True, capture_output=True
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "completed"


def test_cli_reports_non_object_config_as_structured_failure(tmp_path: Path) -> None:
    for document in ([], {"config": None}):
        config = tmp_path / f"config-{len(list(tmp_path.glob('config-*')))}.json"
        config.write_text(json.dumps(document), encoding="utf-8")
        result = subprocess.run(
            [
                installed_script("eqasim-sa"),
                "fake-run",
                "--campaign-root",
                tmp_path / "campaign",
                "--campaign",
                "demo",
                "--config",
                config,
                "--seed",
                "42",
                "--expected-context-hash",
                "c" * 64,
                "--current-context-hash",
                "c" * 64,
                "--workdir",
                tmp_path / "work",
            ],
            cwd=tmp_path,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 1
        assert json.loads(result.stdout)["status"] == "failed"


def test_plan_cli_never_reports_an_empty_error(monkeypatch, capsys) -> None:
    from eqasim_sa.cli import dispatch

    monkeypatch.setattr(sys, "argv", ["eqasim-sa", "plan", "--meta-config", "meta.yml"])
    status = dispatch(
        None, None, plan_factory=lambda path: (_ for _ in ()).throw(Exception())
    )
    assert status == 1 and json.loads(capsys.readouterr().out)["error"] == "Exception"


def test_fake_executable_requires_request_and_manifest(tmp_path: Path) -> None:
    result = subprocess.run(
        [installed_script("eqasim-sa-fake-pipeline")],
        cwd=tmp_path,
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0


def test_installed_cli_runs_eqasim_subprocess_then_skips(tmp_path: Path) -> None:
    config = tmp_path / "config.yml"
    config.write_text(
        "working_directory: /ignored\nrun: [old.stage]\nconfig:\n"
        "  sampling_rate: 0.001\n  random_seed: 9\n"
        "  hts: edgt_44\n  income_assignation_method: bhepop2\n"
        "  gtfs_path: secret/path\n  output_prefix: nantes_\n  output_path: /ignored\n",
        encoding="utf-8",
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    record = tmp_path / "record.json"
    environment = os.environ.copy()
    environment["EQASIM_SPY_RECORD"] = str(record)
    invocation = [sys.executable, str(Path(__file__).with_name("eqasim_spy.py"))]
    command = [
        installed_script("eqasim-sa"),
        "eqasim-run",
        "--campaign-root",
        tmp_path / "campaign",
        "--campaign",
        "nantes.v1",
        "--config-yaml",
        config,
        "--eqasim-root",
        tmp_path,
        "--invocation-json",
        json.dumps(invocation),
        "--scratch",
        scratch,
        "--seed",
        "42",
        "--expected-context-hash",
        "c" * 64,
        "--current-context-hash",
        "c" * 64,
    ]
    first = subprocess.run(
        command,
        cwd=tmp_path,
        env=environment,
        check=True,
        text=True,
        capture_output=True,
    )
    first_record = json.loads(record.read_text())
    record.unlink()
    second = subprocess.run(
        command,
        cwd=tmp_path,
        env=environment,
        check=True,
        text=True,
        capture_output=True,
    )
    first_result = json.loads(first.stdout)
    assert first_result["status"] == "completed"
    assert json.loads(second.stdout)["status"] == "skipped"
    assert first_record["cwd"] == str(tmp_path)
    assert not record.exists()
    archives = tmp_path / "campaign" / "archives" / first_result["runId"]
    assert len(tuple(archives.glob("*.parquet"))) == 12
    assert all(
        pq.ParquetFile(path).metadata.row_group(row).column(column).compression
        == "ZSTD"
        for path in archives.glob("*.parquet")
        for row in range(pq.ParquetFile(path).metadata.num_row_groups)
        for column in range(pq.ParquetFile(path).metadata.row_group(row).num_columns)
    )
    ledger = pq.read_table(
        tmp_path / "campaign" / "runs" / f"{first_result['runId']}.parquet"
    )
    assert ledger.column_names[-3:] == [
        "sampling_rate",
        "hts",
        "income_assignation_method",
    ]
    assert "gtfs_path" not in ledger.column_names


def test_installed_cli_publishes_eqasim_failure_log(tmp_path: Path) -> None:
    config = tmp_path / "config.yml"
    config.write_text(
        "working_directory: /ignored\nrun: []\nconfig:\n  output_prefix: nantes_\n",
        encoding="utf-8",
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    environment = os.environ.copy()
    environment["EQASIM_SPY_RECORD"] = str(tmp_path / "record.json")
    environment["EQASIM_SPY_FAIL"] = "7"
    result = subprocess.run(
        [
            installed_script("eqasim-sa"),
            "eqasim-run",
            "--campaign-root",
            tmp_path / "campaign",
            "--campaign",
            "nantes.v1",
            "--config-yaml",
            config,
            "--eqasim-root",
            tmp_path,
            "--invocation-json",
            json.dumps(
                [
                    sys.executable,
                    str(Path(__file__).with_name("eqasim_spy.py")),
                ]
            ),
            "--scratch",
            scratch,
            "--seed",
            "42",
            "--expected-context-hash",
            "c" * 64,
            "--current-context-hash",
            "c" * 64,
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 1
    assert payload["status"] == "failed"
    assert "code 7" in payload["error"]
    assert (tmp_path / "campaign" / "logs" / f"{payload['runId']}.log").is_file()
    assert not (tmp_path / "campaign" / "runs" / f"{payload['runId']}.parquet").exists()
