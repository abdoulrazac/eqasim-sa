import copy
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import eqasim_sa.core.plan as plan_module
from eqasim_sa.adapters.application import analyze_from_meta_config, run_from_meta_config
from eqasim_sa.adapters.backends.local import LocalBackend
from eqasim_sa.adapters.data_manifest import generate_data_manifest
from eqasim_sa.adapters.eqasim.config import EqasimSettings
from eqasim_sa.adapters.eqasim.simulator import EqasimSimulator, eqasim_requirements
from eqasim_sa.adapters.execution_seal import create_execution_seal
from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.adapters.worker import Indexed, WorkerBatch
from eqasim_sa.core.plan import GridDesign, build_plan, load_run_specs
from eqasim_sa.core.run import RunResult, summarize_runs
from eqasim_sa.core.software_identity import read_software_identity
from eqasim_sa.ports.simulator import ExecutionEvidence
from tests.provenance_helpers import context_hash, scientific_context_descriptor
from tests.conftest import make_symlink, pid_alive


def meta_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    model = tmp_path / "model"
    model.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=model, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.test"], cwd=model, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=model, check=True)
    config = model / "config.yml"
    data = tmp_path / "data"
    data.mkdir()
    (data / "input.txt").write_text("sealed data\n")
    config.write_text(
        "working_directory: /ignored\nrun: [old]\nconfig:\n  a: 0\n  b: x\n"
        "  data_path: ../data\n  processes: 1\n  random_seed: 9\n"
    )
    (model / "lock.yml").write_text("locked\n")
    subprocess.run(["git", "add", "."], cwd=model, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=model, check=True)
    manifest = tmp_path / "manifest.json"
    generate_data_manifest(data, manifest)
    meta = tmp_path / "meta.yml"
    meta.write_text(
        "campaign: demo\ncampaign_root: campaign\nmodel:\n  config: model/config.yml\n  root: model\n"
        "context:\n  data_manifest: manifest.json\n  lockfile: model/lock.yml\n"
        "analysis:\n  completeness_threshold: 1.0\n"
        "experiment:\n  method: grid\n  seed_role: replication\n  replications:\n    seeds: [41, 42]\n"
        "parameters:\n  a:\n    values: [1, 2]\n  b:\n    values: [x, y]\n"
    )
    return meta, model, config, manifest


def add_execution(meta: Path, scratch_path: Path, **changes: object) -> None:
    values = {
        "backend": "local",
        "workers": 2,
        "scratch": str(scratch_path),
        "invocation": ["python3", "-m", "synpp"],
    }
    values.update(changes)
    meta.write_text(
        meta.read_text()
        + "execution:\n"
        + "".join(f"  {key}: {json.dumps(value)}\n" for key, value in values.items())
    )


def test_execution_is_optional_strict_and_resolved(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    assert load_meta_config(meta).execution is None
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    add_execution(meta, scratch)
    execution = load_meta_config(meta).execution
    assert execution is not None
    assert execution.backend == "local" and execution.workers == 2
    assert execution.processes == 1
    assert execution.scratch == scratch.resolve()
    assert execution.invocation == ("python3", "-m", "synpp")
    assert execution.data_path == (tmp_path / "data").resolve()


def test_meta_config_stores_runner_source_digest_and_states(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)

    loaded = load_meta_config(meta)

    assert loaded.eqasim_sa_sha256 == loaded.software_identity.sha256
    assert loaded.eqasim_sa_source_states == loaded.software_identity.source_states
    assert "eqasimSaSha256" not in loaded.context_descriptor
    assert loaded.eqasim_sa_source_states


def test_data_path_override_is_hashed_and_passed_as_the_same_absolute_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eqasim_sa.adapters.application import plan_from_meta_config

    meta, _, _, manifest = meta_fixture(tmp_path)
    alternate = tmp_path / "alternate-data"
    alternate.mkdir()
    (alternate / "input.txt").write_text("alternate sealed data\n")
    generate_data_manifest(alternate, manifest, replace=True)
    meta.write_text(
        meta.read_text()
        .replace("seeds: [41, 42]", "seeds: [41]")
        .replace("values: [1, 2]", "values: [1]")
        .replace("values: [x, y]", "values: [x]")
    )
    plan_from_meta_config(meta)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    record = tmp_path / "record.json"
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(record))
    add_execution(
        meta,
        scratch,
        workers=1,
        invocation=[sys.executable, str(Path(__file__).with_name("eqasim_spy.py"))],
        overrides={"data_path": "alternate-data"},
    )

    loaded = load_meta_config(meta)
    payload = run_from_meta_config(meta)
    arguments = json.loads(record.read_text())["argv"]

    assert loaded.execution is not None
    assert loaded.execution.data_path == alternate.resolve()
    assert arguments[arguments.index("--data_path") + 1] == str(alternate.resolve())
    assert payload["completed"] == 1


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"backend": "automatic"}, "backend"),
        ({"workers": True}, "workers"),
        ({"workers": 0}, "workers"),
        ({"invocation": []}, "invocation"),
        ({"invocation": [""]}, "invocation"),
        ({"invocation": ["x\0"]}, "invocation"),
        ({"scratch": ""}, "scratch"),
        ({"scratch": "bad\0path"}, "scratch"),
    ],
)
def test_execution_rejects_invalid_values(
    tmp_path: Path, changes: dict, message: str
) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    add_execution(meta, scratch, **changes)
    with pytest.raises((TypeError, ValueError), match=message):
        load_meta_config(meta)


def test_execution_rejects_worker_cap_and_nonpositive_local_processes(
    tmp_path: Path,
) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    add_execution(meta, scratch, workers=1025)
    with pytest.raises(ValueError, match="workers"):
        load_meta_config(meta)

    process_root = tmp_path / "processes"
    process_root.mkdir()
    meta, _, _, _ = meta_fixture(process_root)
    scratch = process_root / "scratch"
    scratch.mkdir()
    add_execution(meta, scratch, backend="local", overrides={"processes": 0})
    with pytest.raises(ValueError, match="processes"):
        load_meta_config(meta)


@pytest.mark.parametrize("overlap", ["model", "data"])
def test_execution_rejects_scratch_overlapping_model_or_effective_data(
    tmp_path: Path, overlap: str
) -> None:
    meta, model, _, _ = meta_fixture(tmp_path)
    scratch = model if overlap == "model" else tmp_path / "data"
    add_execution(meta, scratch)

    with pytest.raises(ValueError, match="overlap"):
        load_meta_config(meta)


def test_slurm_execution_resources_are_optional_strict_and_do_not_affect_plan(
    tmp_path: Path,
) -> None:
    from eqasim_sa.adapters.application import plan_from_meta_config

    meta, _, _, _ = meta_fixture(tmp_path)
    first = plan_from_meta_config(meta)
    before = (tmp_path / "campaign" / "plan.parquet").read_bytes()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    add_execution(meta, scratch, backend="slurm", overrides={"processes": 1})
    execution = load_meta_config(meta).execution
    assert execution is not None
    assert execution.processes == 1
    assert execution.slurm.timeout_min == 1440
    assert execution.slurm.mem_gb is None
    assert execution.slurm.partition is None
    assert plan_from_meta_config(meta).status == "unchanged"
    assert first.status == "created"
    assert (tmp_path / "campaign" / "plan.parquet").read_bytes() == before


def test_execution_stores_process_count_from_sealed_config_without_override(
    tmp_path: Path,
) -> None:
    meta, model, config, _ = meta_fixture(tmp_path)
    config.write_text(config.read_text().replace("processes: 1", "processes: 4"))
    subprocess.run(["git", "add", "config.yml"], cwd=model, check=True)
    subprocess.run(["git", "commit", "-qm", "process count"], cwd=model, check=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    add_execution(meta, scratch, backend="slurm")

    execution = load_meta_config(meta).execution

    assert execution is not None and execution.processes == 4


@pytest.mark.parametrize(
    "resources",
    [
        {"unknown": 1},
        {"timeout_min": 0},
        {"timeout_min": True},
        {"mem_gb": 0},
        {"mem_gb": 1 / 2048},
        {"mem_gb": float("inf")},
        {"partition": ""},
        {"partition": "cpu shared"},
        {"partition": "cpu/shared"},
        {"partition": "cpu\\shared"},
        {"partition": "cpu\nshared"},
        {"account": 3},
        {"account": "équipe"},
        {"qos": "bad\0value"},
    ],
)
def test_slurm_execution_rejects_invalid_resource_blocks(
    tmp_path: Path,
    resources: dict,
) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    add_execution(meta, scratch, backend="slurm", slurm=resources)
    with pytest.raises((TypeError, ValueError), match="slurm"):
        load_meta_config(meta)


def test_slurm_accepts_minimum_memory_and_safe_resource_identifiers(
    tmp_path: Path,
) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    resources = {
        "mem_gb": 1 / 1024,
        "partition": "cpu-long",
        "account": "team_1",
        "qos": "normal.v2",
    }
    add_execution(meta, scratch, backend="slurm", slurm=resources)
    execution = load_meta_config(meta).execution
    assert execution is not None
    assert execution.slurm.mem_gb == 1 / 1024
    assert execution.slurm.partition == "cpu-long"
    assert execution.slurm.account == "team_1"
    assert execution.slurm.qos == "normal.v2"


@pytest.mark.parametrize("processes", [0, -1, True, "2"])
def test_slurm_rejects_non_positive_or_non_integer_processes(
    tmp_path: Path,
    processes: object,
) -> None:
    meta, model, config, _ = meta_fixture(tmp_path)
    if isinstance(processes, str):
        config.write_text(config.read_text().replace("processes: 1", "processes: x"))
        subprocess.run(["git", "add", "config.yml"], cwd=model, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "string processes"], cwd=model, check=True
        )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    add_execution(meta, scratch, backend="slurm", overrides={"processes": processes})
    with pytest.raises((TypeError, ValueError), match="processes"):
        load_meta_config(meta)


def test_execution_seal_freezes_child_environment_and_absolute_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    frozen_record = tmp_path / "frozen-record.json"
    changed_record = tmp_path / "changed-record.json"
    secret = "runtime-secret-value"
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(frozen_record))
    monkeypatch.setenv("EQASIM_TEST_SECRET", secret)
    add_execution(
        meta,
        scratch,
        workers=1,
        invocation=[sys.executable, str(Path(__file__).with_name("eqasim_spy.py"))],
    )
    loaded = load_meta_config(meta)
    seal = create_execution_seal(loaded)
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(changed_record))
    monkeypatch.setenv("EQASIM_TEST_SECRET", "changed")
    settings = EqasimSettings(
        loaded.model_config,
        loaded.model_root,
        seal.invocation,
        loaded.campaign,
        scratch,
        loaded.context_hash,
    )
    resolved = copy.deepcopy(loaded.model_document)
    resolved["run"] = ["synthesis.output", "analysis.synthesis.population"]
    resolved["config"]["random_seed"] = 41
    adapter = EqasimSimulator(
        settings,
        loaded.model_document,
        loaded.model_bytes,
        execution_seal=seal,
    )

    manifest = adapter.simulate(resolved, 41, settings.workdir)
    evidence = ExecutionEvidence.from_bytes(manifest.execution_evidence or b"")

    assert Path(seal.invocation[0]).is_absolute()
    assert seal.environment["EQASIM_SPY_RECORD"] == str(frozen_record)
    assert frozen_record.is_file() and not changed_record.exists()
    assert secret.encode() not in (manifest.execution_evidence or b"")
    assert "EQASIM_TEST_SECRET" in evidence.environment_variables
    assert evidence.schema_version == 2
    assert evidence.eqasim_sa_sha256 == loaded.eqasim_sa_sha256


def test_execution_seal_model_failure_is_sticky_after_state_is_restored(
    tmp_path: Path,
) -> None:
    meta, model, _, _ = meta_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    add_execution(meta, scratch, workers=1)
    seal = create_execution_seal(load_meta_config(meta))
    foreign = model / "untracked"
    foreign.write_text("drift")

    with pytest.raises(RuntimeError, match="no longer clean") as first:
        seal.guard()
    foreign.unlink()
    with pytest.raises(RuntimeError) as second:
        seal.guard()

    assert str(second.value) == str(first.value)


def test_execution_seal_rejects_control_input_drift(tmp_path: Path) -> None:
    meta, _, _, manifest = meta_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    add_execution(meta, scratch, workers=1)
    seal = create_execution_seal(load_meta_config(meta))
    manifest.write_bytes(manifest.read_bytes() + b"\n")

    with pytest.raises(RuntimeError, match="control input changed"):
        seal.guard()


def test_execution_seal_rejects_runner_source_state_drift(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    add_execution(meta, scratch, workers=1)
    loaded = load_meta_config(meta)
    package = tmp_path / "installed" / "eqasim_sa"
    package.mkdir(parents=True)
    source = package / "runner.py"
    source.write_text("VALUE = 1\n")
    identity = read_software_identity(package, version=loaded.eqasim_sa_version)
    seal = create_execution_seal(replace(loaded, software_identity=identity))
    source.write_text("VALUE = 2\n")

    with pytest.raises(RuntimeError, match="source changed after sealing"):
        seal.guard()


def test_execution_seal_rejects_model_head_and_executable_drift(
    tmp_path: Path,
) -> None:
    head_root = tmp_path / "head"
    head_root.mkdir()
    meta, model, _, _ = meta_fixture(head_root)
    scratch = head_root / "scratch"
    scratch.mkdir()
    add_execution(meta, scratch, workers=1)
    seal = create_execution_seal(load_meta_config(meta))
    subprocess.run(
        ["git", "commit", "--allow-empty", "-qm", "drift"], cwd=model, check=True
    )
    with pytest.raises(RuntimeError, match="HEAD changed"):
        seal.guard()

    executable_root = tmp_path / "executable"
    executable_root.mkdir()
    meta, _, _, _ = meta_fixture(executable_root)
    scratch = executable_root / "scratch"
    scratch.mkdir()
    executable = executable_root / "runner"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    add_execution(meta, scratch, workers=1, invocation=[str(executable)])
    seal = create_execution_seal(load_meta_config(meta))
    executable.write_text("#!/bin/sh\nexit 1\n")
    executable.chmod(0o700)
    with pytest.raises(RuntimeError, match="executable changed"):
        seal.guard()


@pytest.mark.linux_only
def test_slurm_campaign_composes_remote_job_without_local_simulator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eqasim_sa.adapters import application
    from eqasim_sa.adapters.application import plan_from_meta_config
    from eqasim_sa.core.run import compute_run_id

    meta, _, _, _ = meta_fixture(tmp_path)
    meta.write_text(
        meta.read_text()
        .replace("values: [1, 2]", "values: [1]")
        .replace("values: [x, y]", "values: [x]")
    )
    plan_from_meta_config(meta)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    add_execution(meta, scratch, backend="slurm", workers=7, overrides={"processes": 3})
    observed = {}

    class Backend:
        def __init__(self, campaign_root, workers, parameters):  # type: ignore[no-untyped-def]
            observed.update(
                campaign_root=campaign_root,
                workers=workers,
                parameters=parameters,
            )

        def map(self, function, specs):  # type: ignore[no-untyped-def]
            values = tuple(specs)
            observed.update(function=function, specs=values)
            return iter(
                tuple(
                    Indexed(
                        item.index,
                        RunResult(
                            compute_run_id(
                                item.value.resolved_config, item.value.seed
                            ),
                            "completed",
                        ),
                    )
                    for item in batch.items
                )
                for batch in values
            )

    monkeypatch.setattr("eqasim_sa.adapters.backends.slurm.SlurmBackend", Backend)
    monkeypatch.setattr(
        application,
        "EqasimSimulator",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("local simulator instantiated")
        ),
    )
    payload = run_from_meta_config(meta)
    loaded = load_meta_config(meta)
    assert payload["total"] == 2 and payload["completed"] == 2
    assert observed["campaign_root"] == loaded.campaign_root
    assert observed["workers"] == 7
    assert observed["parameters"] == {"timeout_min": 1440, "cpus_per_task": 3}
    assert type(observed["function"]).__name__ == "EqasimSlurmJob"
    assert (
        observed["function"].meta_config_sha256
        == hashlib.sha256(meta.read_bytes()).hexdigest()
    )
    assert len(observed["specs"]) == 2


@pytest.mark.linux_only
@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_slurm_backend_rejects_unsafe_owned_submitit_directory(
    tmp_path: Path,
    kind: str,
) -> None:
    from eqasim_sa.adapters.application import plan_from_meta_config
    from eqasim_sa.adapters.backends.slurm import SlurmBackend

    meta, _, _, _ = meta_fixture(tmp_path)
    meta.write_text(
        meta.read_text()
        .replace("values: [1, 2]", "values: [1]")
        .replace("values: [x, y]", "values: [x]")
    )
    plan_from_meta_config(meta)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    add_execution(meta, scratch, backend="slurm")
    technical = tmp_path / "campaign" / ".eqasim-sa" / "slurm" / "submitit"
    technical.parent.mkdir(parents=True)
    if kind == "file":
        technical.write_text("not a directory")
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        make_symlink(technical, outside, target_is_directory=True)
    with pytest.raises(ValueError, match="directory"):
        SlurmBackend(tmp_path / "campaign", 1, {})


@pytest.mark.linux_only
def test_slurm_campaign_resumes_only_missing_runs_and_complete_campaign_is_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eqasim_sa.adapters import application
    from eqasim_sa.adapters.application import plan_from_meta_config
    from eqasim_sa.core.ledger import RunRecord, write_run
    from eqasim_sa.core.run import compute_run_id

    meta, _, _, _ = meta_fixture(tmp_path)
    meta.write_text(
        meta.read_text()
        .replace("values: [1, 2]", "values: [1]")
        .replace("values: [x, y]", "values: [x]")
    )
    plan_from_meta_config(meta)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    add_execution(meta, scratch, backend="slurm")
    loaded = load_meta_config(meta)
    design = GridDesign(
        loaded.model_document,
        loaded.factors,
        loaded.seeds,
        dict(application.eqasim_requirements(loaded.indicator_requirements)),
        loaded.context_hash,
        loaded.context_descriptor,
        loaded.campaign_root,
    )
    specs = load_run_specs(
        loaded.campaign_root / "plan.parquet", design, loaded.campaign
    )
    completed_id = compute_run_id(specs[0].resolved_config, specs[0].seed)
    write_run(
        loaded.campaign_root,
        RunRecord(
            completed_id,
            specs[0].campaign,
            specs[0].seed,
            specs[0].expected_context_hash,
            "node",
            1.0,
            specs[0].parameters,
        ),
    )
    submitted = []

    class Backend:
        def __init__(self, *args):  # type: ignore[no-untyped-def]
            pass

        def map(self, function, values):  # type: ignore[no-untyped-def]
            submitted.extend(item.value for batch in values for item in batch.items)
            return iter(
                tuple(
                    Indexed(
                        item.index,
                        RunResult(
                            compute_run_id(
                                item.value.resolved_config, item.value.seed
                            ),
                            "completed",
                        ),
                    )
                    for item in batch.items
                )
                for batch in values
            )

    monkeypatch.setattr("eqasim_sa.adapters.backends.slurm.SlurmBackend", Backend)
    payload = run_from_meta_config(meta)
    assert payload["total"] == 1
    assert [item.seed for item in submitted] == [42]

    missing = specs[1]
    write_run(
        loaded.campaign_root,
        RunRecord(
            compute_run_id(missing.resolved_config, missing.seed),
            missing.campaign,
            missing.seed,
            missing.expected_context_hash,
            "node",
            1.0,
            missing.parameters,
        ),
    )
    monkeypatch.setattr(
        "eqasim_sa.adapters.backends.slurm.SlurmBackend",
        lambda *args: (_ for _ in ()).throw(AssertionError("backend instantiated")),
    )
    monkeypatch.setattr(
        application,
        "EqasimSimulator",
        lambda *args: (_ for _ in ()).throw(AssertionError("simulator instantiated")),
    )
    assert run_from_meta_config(meta)["total"] == 0


@pytest.mark.linux_only
def test_remote_jobs_survive_workdir_loss_and_resume_only_from_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eqasim_sa.adapters import application
    from eqasim_sa.adapters import data_manifest
    from eqasim_sa.adapters.application import plan_from_meta_config
    from eqasim_sa.adapters.backends.slurm import EqasimSlurmJob
    from eqasim_sa.core.run import compute_run_id

    meta, _, _, _ = meta_fixture(tmp_path)
    meta.write_text(
        meta.read_text()
        .replace("values: [1, 2]", "values: [1]")
        .replace("values: [x, y]", "values: [x]")
    )
    plan_from_meta_config(meta)
    scratch = tmp_path / "node-scratch"
    scratch.mkdir()
    add_execution(
        meta,
        scratch,
        backend="slurm",
        invocation=[sys.executable, str(Path(__file__).with_name("eqasim_spy.py"))],
    )
    loaded = load_meta_config(meta)
    design = GridDesign(
        loaded.model_document,
        loaded.factors,
        loaded.seeds,
        dict(application.eqasim_requirements(loaded.indicator_requirements)),
        loaded.context_hash,
        loaded.context_descriptor,
        loaded.campaign_root,
    )
    specs = load_run_specs(
        loaded.campaign_root / "plan.parquet", design, loaded.campaign
    )
    workdir = scratch / "esa" / "demo" / "w0"
    job = EqasimSlurmJob(
        meta,
        loaded.context_hash,
        hashlib.sha256(meta.read_bytes()).hexdigest(),
        loaded.eqasim_sa_sha256,
    )
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(tmp_path / "record.json"))
    real_hash = data_manifest.descriptor_sha256
    hash_calls = 0

    def counted_hash(descriptor: int) -> str:
        nonlocal hash_calls
        hash_calls += 1
        return real_hash(descriptor)

    monkeypatch.setattr(data_manifest, "descriptor_sha256", counted_hash)

    first_batch = WorkerBatch(0, (Indexed(0, specs[0]),))
    assert job(first_batch)[0].value.status == "completed"
    assert hash_calls == 1
    first_id = compute_run_id(specs[0].resolved_config, specs[0].seed)
    first_marker = loaded.campaign_root / "runs" / f"{first_id}.parquet"
    first_archives = tuple(
        sorted((loaded.campaign_root / "archives" / first_id).iterdir())
    )
    durable_paths = (first_marker, *first_archives)
    sentinel_mtime_ns = 1_234_567_890_000_000_000
    for path in durable_paths:
        os.utime(path, ns=(sentinel_mtime_ns, sentinel_mtime_ns))
    durable = {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in durable_paths
    }
    stale_attempt = workdir / "astale"
    stale_attempt.mkdir()
    (stale_attempt / "pipeline.json").write_text("not authoritative")
    (workdir / "cache.lock").write_text("not authoritative")
    shutil.rmtree(workdir)

    second_batch = WorkerBatch(0, (Indexed(0, specs[1]),))
    assert job(second_batch)[0].value.status == "completed"
    assert hash_calls == 2
    assert workdir.is_dir()
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in durable
    } == durable

    second_id = compute_run_id(specs[1].resolved_config, specs[1].seed)
    (loaded.campaign_root / "runs" / f"{second_id}.parquet").unlink()
    deceptive_attempt = workdir / "acompleted"
    deceptive_attempt.mkdir()
    deceptive_pipeline = deceptive_attempt / "pipeline.json"
    deceptive_lock = workdir / "cache.lock"
    deceptive_pipeline.write_text("completed according to cache")
    deceptive_lock.write_text("cache state must be ignored")
    deceptive_state = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (deceptive_pipeline, deceptive_lock)
    }
    submitted = []

    class Backend:
        def __init__(self, *args):  # type: ignore[no-untyped-def]
            pass

        def map(self, function, values):  # type: ignore[no-untyped-def]
            submitted.extend(item.value for batch in values for item in batch.items)
            return iter(
                tuple(
                    Indexed(
                        item.index,
                        RunResult(
                            compute_run_id(
                                item.value.resolved_config, item.value.seed
                            ),
                            "completed",
                        ),
                    )
                    for item in batch.items
                )
                for batch in values
            )

    monkeypatch.setattr("eqasim_sa.adapters.backends.slurm.SlurmBackend", Backend)
    payload = run_from_meta_config(meta)

    assert payload["total"] == 1
    assert [compute_run_id(item.resolved_config, item.seed) for item in submitted] == [
        second_id
    ]
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in deceptive_state
    } == deceptive_state
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in durable
    } == durable


def test_local_backend_is_bounded_ordered_and_closes_on_partial_consumption() -> None:
    lock = threading.Lock()
    active = peak = 0

    def work(value: int) -> int:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return value

    iterator = LocalBackend(2).map(work, range(4))
    assert next(iterator) == 0
    iterator.close()
    assert active == 0 and peak == 2
    assert tuple(LocalBackend(1).map(work, [2, 1])) == (2, 1)
    assert tuple(LocalBackend(2).map(work, [])) == ()


def test_local_backend_propagates_worker_exception() -> None:
    def fail(value: int) -> int:
        if value == 2:
            raise RuntimeError("worker broke")
        return value

    with pytest.raises(RuntimeError, match="worker broke"):
        tuple(LocalBackend(2).map(fail, [1, 2, 3]))


def test_local_backend_keeps_submission_order_when_later_work_finishes_first() -> None:
    def work(value: float) -> float:
        time.sleep(value)
        return value

    assert tuple(LocalBackend(3).map(work, [0.2, 0.0, 0.1])) == (0.2, 0.0, 0.1)


def test_local_backend_raises_the_first_failure_in_submission_order() -> None:
    def fail(value: int) -> int:
        if value == 1:
            time.sleep(0.1)
            raise RuntimeError("first")
        if value == 2:
            raise RuntimeError("second")
        return value

    iterator = LocalBackend(3).map(fail, [0, 1, 2])
    assert next(iterator) == 0
    with pytest.raises(RuntimeError, match="first"):
        next(iterator)


def test_local_backend_waits_in_short_slices_and_interrupts_on_ctrl_c(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eqasim_sa.adapters.backends import local

    release = threading.Event()
    started = []
    timeouts = []
    real_wait = local.wait

    def interrupted_wait(pending, timeout, return_when):  # type: ignore[no-untyped-def]
        timeouts.append(timeout)
        if len(timeouts) == 2:
            # A Ctrl+C delivered between two short waits of the main thread.
            raise KeyboardInterrupt
        return real_wait(pending, timeout=0.01, return_when=return_when)

    def work(value: int) -> int:
        started.append(value)
        release.wait(5)
        return value

    interrupts = []
    monkeypatch.setattr(local, "wait", interrupted_wait)
    iterator = LocalBackend(2).map(
        work, [1, 2, 3], lambda: (interrupts.append(True), release.set())
    )
    with pytest.raises(KeyboardInterrupt):
        next(iterator)
    assert interrupts == [True]
    assert timeouts == [local.INTERRUPT_POLL_SECONDS] * 2
    # The queued third item was cancelled, never started.
    assert sorted(started) == [1, 2]


def test_plan_reconstruction_deduplicates_first_occurrence(tmp_path: Path) -> None:
    from eqasim_sa.adapters.application import plan_from_meta_config

    meta, _, _, _ = meta_fixture(tmp_path)
    text = meta.read_text().replace("values: [1, 2]", "values: [1, 1]")
    meta.write_text(text)
    plan_from_meta_config(meta)
    loaded = load_meta_config(meta)
    design = GridDesign(
        loaded.model_document,
        loaded.factors,
        loaded.seeds,
        dict(eqasim_requirements(loaded.indicator_requirements)),
        loaded.context_hash,
        loaded.context_descriptor,
        loaded.campaign_root,
    )
    specs = load_run_specs(
        loaded.campaign_root / "plan.parquet", design, loaded.campaign
    )
    table = pq.ParquetFile(loaded.campaign_root / "plan.parquet").read()
    assert len(specs) == len(set(table["runId"].to_pylist())) == 4
    assert [spec.seed for spec in specs] == [41, 42, 41, 42]
    assert specs[0].parameters == {"a": 1, "b": "x"}


def test_morris_plan_reconstruction_deduplicates_coincident_structural_positions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "campaign"
    descriptor = scientific_context_descriptor()
    design = plan_module.MorrisDesign(
        {"config": {"a": 0}},
        {"a": [0, 1]},
        (11,),
        {"run": ["stage"]},
        context_hash(descriptor),
        descriptor,
        root,
        trajectories=2,
        design_seed=7,
    )
    built = build_plan(design)
    table = pq.read_table(root / "plan.parquet")

    specs = load_run_specs(root / "plan.parquet", design, "demo")

    assert built.design_positions == 4
    assert built.unique_runs == len(specs) == 2
    rows = table.to_pylist()
    assert len({row["designId"] for row in rows}) == 4
    assert len({row["role"] for row in rows}) == 4
    assert len({row["runId"] for row in rows}) == 2
    assert {spec.parameters["a"] for spec in specs} == {0, 1}


def test_sobol_plan_reconstruction_deduplicates_without_losing_positions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "campaign"
    descriptor = scientific_context_descriptor()
    design = plan_module.SobolDesign(
        {"config": {"a": 0}},
        {"a": [0, 1]},
        (11, 12),
        {"run": ["stage"]},
        context_hash(descriptor),
        descriptor,
        root,
        base_sample_size=8,
        design_seed=7,
    )
    built = build_plan(design)
    table = pq.read_table(root / "plan.parquet")
    specs = load_run_specs(root / "plan.parquet", design, "demo")

    assert built.design_positions == 8 * (2 * 2 + 2) == len(table)
    assert built.unique_runs == len(specs) == len(set(table["runId"].to_pylist()))
    assert built.unique_runs < built.design_positions
    assert len(set(table["designId"].to_pylist())) == built.design_positions
    assert all("seed" not in spec.parameters for spec in specs)
    assert all(
        spec.resolved_config["config"]["random_seed"] == spec.seed for spec in specs
    )


def test_plan_reconstruction_rejects_tampering_before_effect(tmp_path: Path) -> None:
    from eqasim_sa.adapters.application import plan_from_meta_config

    meta, _, _, _ = meta_fixture(tmp_path)
    plan_from_meta_config(meta)
    loaded = load_meta_config(meta)
    path = loaded.campaign_root / "plan.parquet"
    table = pq.ParquetFile(path).read()
    forged = table.set_column(
        table.column_names.index("runId"),
        "runId",
        pa.array(["0" * 32] * len(table), type=pa.string()),
    )
    pq.write_table(forged, path)
    with pytest.raises(ValueError, match="runId"):
        design = GridDesign(
            loaded.model_document,
            loaded.factors,
            loaded.seeds,
            {"run": ["synthesis.output", "analysis.synthesis.population"]},
            loaded.context_hash,
            loaded.context_descriptor,
            loaded.campaign_root,
        )
        load_run_specs(path, design, loaded.campaign)
    assert not any(
        (loaded.campaign_root / name).exists() for name in ("runs", "archives", "logs")
    )


def test_grid_analysis_reconstructs_and_rejects_tampered_sealed_plan(
    tmp_path: Path,
) -> None:
    from eqasim_sa.adapters.application import plan_from_meta_config

    meta, _, _, _ = meta_fixture(tmp_path)
    plan_from_meta_config(meta)
    path = tmp_path / "campaign" / "plan.parquet"
    table = pq.read_table(path)
    roles = table["role"].to_pylist()
    roles[0] = "forged"
    table = table.set_column(
        table.column_names.index("role"), "role", pa.array(roles, type=pa.string())
    )
    pq.write_table(table, path, compression="zstd")

    with pytest.raises(ValueError, match="mismatch|role|content"):
        analyze_from_meta_config(meta)


@pytest.mark.parametrize("damage", ["missing", "symlink", "truncated", "metadata"])
def test_plan_reconstruction_rejects_unsafe_files(tmp_path: Path, damage: str) -> None:
    from eqasim_sa.adapters.application import plan_from_meta_config

    meta, _, _, _ = meta_fixture(tmp_path)
    plan_from_meta_config(meta)
    loaded = load_meta_config(meta)
    path = loaded.campaign_root / "plan.parquet"
    design = GridDesign(
        loaded.model_document,
        loaded.factors,
        loaded.seeds,
        {"run": ["synthesis.output", "analysis.synthesis.population"]},
        loaded.context_hash,
        loaded.context_descriptor,
        loaded.campaign_root,
    )
    if damage == "missing":
        path.unlink()
    elif damage == "symlink":
        path.unlink()
        make_symlink(path, meta)
    elif damage == "truncated":
        path.write_bytes(b"PAR1")
    else:
        table = pq.ParquetFile(path).read().replace_schema_metadata({})
        pq.write_table(table, path)
    with pytest.raises(Exception):
        load_run_specs(path, design, loaded.campaign)
    assert not any(
        (loaded.campaign_root / name).exists() for name in ("runs", "archives", "logs")
    )


def test_summary_is_deterministic_and_rejects_backend_contract() -> None:
    results = [
        RunResult("a", "completed"),
        RunResult("b", "failed", "boom"),
        RunResult("c", "skipped"),
    ]
    assert summarize_runs(["a", "b", "c"], results) == {
        "status": "failed",
        "total": 3,
        "completed": 1,
        "skipped": 1,
        "failed": 1,
        "failures": [{"runId": "b", "error": "boom"}],
        "error": None,
    }
    with pytest.raises(RuntimeError, match="identities"):
        summarize_runs(["a"], [RunResult("b", "completed")])


def test_run_cli_payload_handles_global_and_partial_failures(
    monkeypatch, capsys
) -> None:
    import sys

    from eqasim_sa.cli import dispatch

    monkeypatch.setattr(sys, "argv", ["eqasim-sa", "run", "--meta-config", "meta.yml"])
    status = dispatch(
        None, None, run_factory=lambda path: (_ for _ in ()).throw(Exception())
    )
    payload = json.loads(capsys.readouterr().out)
    assert status == 1 and payload == {
        "status": "failed",
        "total": 0,
        "completed": 0,
        "skipped": 0,
        "failed": 0,
        "failures": [],
        "error": "Exception",
    }
    monkeypatch.setattr(sys, "argv", ["eqasim-sa", "run", "--meta-config", "meta.yml"])
    summary = {
        "status": "failed",
        "total": 1,
        "completed": 0,
        "skipped": 0,
        "failed": 1,
        "failures": [{"runId": "a", "error": "bad"}],
        "error": None,
    }
    assert dispatch(None, None, run_factory=lambda path: summary) == 1
    assert json.loads(capsys.readouterr().out) == summary


def test_run_requires_execution_before_backend_or_simulator(
    tmp_path: Path, monkeypatch
) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    from eqasim_sa.adapters import application

    monkeypatch.setattr(
        application,
        "LocalBackend",
        lambda workers: (_ for _ in ()).throw(AssertionError("backend instantiated")),
    )
    with pytest.raises(ValueError, match="execution"):
        run_from_meta_config(meta)


@pytest.mark.parametrize(
    "backend", ["local", pytest.param("slurm", marks=pytest.mark.linux_only)]
)
@pytest.mark.parametrize("pending_count", [2, 3])
def test_sobol_capacity_warning_is_exact_and_precedes_backend_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    backend: str,
    pending_count: int,
) -> None:
    from types import SimpleNamespace

    from eqasim_sa.adapters import application
    from eqasim_sa.core.run import RunSpec, compute_run_id

    root = tmp_path / "campaign"
    root.mkdir()
    pq.write_table(pa.table({"position": list(range(24))}), root / "plan.parquet")
    meta_path = tmp_path / "meta.yml"
    meta_path.write_text("sealed")
    (tmp_path / "config.yml").write_text("config: {}\n")
    specs = tuple(
        RunSpec(
            "demo",
            {"config": {"a": value, "random_seed": value}},
            value,
            "c" * 64,
            {"a": value},
        )
        for value in (1, 2, 3, 4, 5)
    )
    execution = SimpleNamespace(
        backend=backend,
        workers=2,
        processes=1,
        overrides={},
        invocation=("spy",),
        scratch=tmp_path,
        slurm=SimpleNamespace(
            timeout_min=1, mem_gb=None, partition=None, account=None, qos=None
        ),
    )
    software_identity = read_software_identity()
    meta = SimpleNamespace(
        method="sobol",
        campaign="demo",
        campaign_root=root,
        execution=execution,
        model_config=tmp_path / "config.yml",
        model_root=tmp_path,
        model_document={"config": {}},
        model_bytes=b"x",
        context_hash="c" * 64,
        indicator_requirements=(),
        factors={"a": [1, 2]},
        seeds=(1, 2),
        context_descriptor={"version": "test"},
        base_sample_size=4,
        design_seed=7,
        software_identity=software_identity,
        eqasim_sa_sha256=software_identity.sha256,
    )
    monkeypatch.setattr(application, "load_meta_config", lambda path: meta)
    monkeypatch.setattr(application, "_design", lambda *args: object())
    monkeypatch.setattr(application, "load_run_specs", lambda *args: specs)
    monkeypatch.setattr(
        application, "pending_run_specs", lambda *args: specs[:pending_count]
    )
    seen = []

    class Backend:
        def __init__(self, *args):  # type: ignore[no-untyped-def]
            pass

        def map(self, function, values, *args):  # type: ignore[no-untyped-def]
            seen.append(capsys.readouterr().err)
            return iter(
                tuple(
                    Indexed(
                        item.index,
                        RunResult(
                            compute_run_id(
                                item.value.resolved_config, item.value.seed
                            ),
                            "completed",
                        ),
                    )
                    for item in batch.items
                )
                for batch in values
            )

    monkeypatch.setattr(application, "LocalBackend", Backend)
    monkeypatch.setattr(
        application,
        "EqasimSimulator",
        lambda *args: SimpleNamespace(interrupt=lambda: None),
    )
    if backend == "slurm":
        # Imported only here: the slurm module is Linux-only.
        monkeypatch.setattr("eqasim_sa.adapters.backends.slurm.SlurmBackend", Backend)
        monkeypatch.setattr(
            "eqasim_sa.adapters.backends.slurm.EqasimSlurmJob", lambda *args: object()
        )

    payload = run_from_meta_config(meta_path)

    warning = (
        "WARNING sobol backend capacity: designPositions=24, uniqueRuns=5, "
        f"pendingRuns=3, backend={backend}, parallelCapacity=2, minimumWaves=2\n"
    )
    assert seen == ([warning] if pending_count == 3 else [""])
    assert payload["total"] == pending_count


def test_sobol_capacity_warning_is_absent_when_work_fits_or_is_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from types import SimpleNamespace

    from eqasim_sa.adapters import application

    root = tmp_path / "campaign"
    root.mkdir()
    pq.write_table(pa.table({"position": list(range(8))}), root / "plan.parquet")
    meta_path = tmp_path / "meta.yml"
    meta_path.write_text("sealed")
    software_identity = read_software_identity()
    meta = SimpleNamespace(
        method="sobol",
        campaign="demo",
        campaign_root=root,
        execution=SimpleNamespace(backend="local", workers=2, overrides={}),
        indicator_requirements=(),
        factors={},
        seeds=(1, 2),
        context_hash="c" * 64,
        context_descriptor={},
        base_sample_size=2,
        design_seed=1,
        software_identity=software_identity,
    )
    monkeypatch.setattr(application, "load_meta_config", lambda path: meta)
    monkeypatch.setattr(application, "_design", lambda *args: object())
    monkeypatch.setattr(application, "load_run_specs", lambda *args: ())
    assert run_from_meta_config(meta_path)["total"] == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("backend", ["local", "slurm"])
@pytest.mark.parametrize("error", [BrokenPipeError("closed"), OSError("unavailable")])
def test_sobol_capacity_warning_io_failure_never_blocks_submission(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    error: OSError,
) -> None:
    from eqasim_sa.adapters import application

    monkeypatch.setattr(
        "builtins.print", lambda *args, **kwargs: (_ for _ in ()).throw(error)
    )
    application._warn_sobol_capacity(24, 5, 5, backend, 2)


def test_execution_never_changes_plan(tmp_path: Path) -> None:
    from eqasim_sa.adapters.application import plan_from_meta_config

    meta, _, _, _ = meta_fixture(tmp_path)
    first = plan_from_meta_config(meta)
    before = (tmp_path / "campaign" / "plan.parquet").read_bytes()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    add_execution(meta, scratch, workers=7, invocation=["other"], backend="local")
    second = plan_from_meta_config(meta)
    assert first.status == "created" and second.status == "unchanged"
    assert (tmp_path / "campaign" / "plan.parquet").read_bytes() == before


def test_local_campaign_hashes_data_once_and_persists_execution_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eqasim_sa.adapters import data_manifest
    from eqasim_sa.adapters.application import plan_from_meta_config
    from eqasim_sa.core import ledger

    meta, _, _, _ = meta_fixture(tmp_path)
    meta.write_text(
        meta.read_text()
        .replace("values: [1, 2]", "values: [1]")
        .replace("values: [x, y]", "values: [x]")
    )
    plan_from_meta_config(meta)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    add_execution(
        meta,
        scratch,
        invocation=[sys.executable, str(Path(__file__).with_name("eqasim_spy.py"))],
    )
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(tmp_path / "record.json"))
    monkeypatch.setenv("EQASIM_RUNTIME_SECRET", "must-not-be-persisted")
    real_hash = data_manifest.descriptor_sha256
    calls = 0

    def counted_hash(descriptor: int) -> str:
        nonlocal calls
        calls += 1
        return real_hash(descriptor)

    monkeypatch.setattr(data_manifest, "descriptor_sha256", counted_hash)

    payload = run_from_meta_config(meta)

    assert payload["completed"] == 2 and calls == 1
    markers = tuple((tmp_path / "campaign" / "runs").glob("*.parquet"))
    assert len(markers) == 2
    evidence_payloads = {
        dict(pq.ParquetFile(marker).schema_arrow.metadata or {})[
            ledger.EXECUTION_EVIDENCE
        ]
        for marker in markers
    }
    assert len(evidence_payloads) == 1
    evidence_payload = next(iter(evidence_payloads))
    assert b"must-not-be-persisted" not in evidence_payload
    evidence = ExecutionEvidence.from_bytes(evidence_payload)
    assert evidence.context_hash == load_meta_config(meta).context_hash


def test_real_subprocess_campaign_deduplicates_and_reports_partial_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eqasim_sa.adapters.application import plan_from_meta_config

    meta, _, _, _ = meta_fixture(tmp_path)
    meta.write_text(
        meta.read_text()
        .replace("values: [1, 2]", "values: [1, 1]")
        .replace("values: [x, y]", "values: [x]")
    )
    plan_from_meta_config(meta)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    add_execution(
        meta,
        scratch,
        invocation=[sys.executable, str(Path(__file__).with_name("eqasim_spy.py"))],
    )
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(tmp_path / "record.json"))
    monkeypatch.setenv("EQASIM_SPY_BARRIER", str(tmp_path / "barrier"))
    monkeypatch.setenv("EQASIM_SPY_FAIL_SEED", "42")
    payload = run_from_meta_config(meta)
    assert (
        payload["total"] == 2 and payload["completed"] == 1 and payload["failed"] == 1
    )
    assert payload["skipped"] == 0 and payload["error"] is None
    assert [failure["runId"] for failure in payload["failures"]] == [
        pq.ParquetFile(tmp_path / "campaign" / "plan.parquet")
        .read()["runId"]
        .to_pylist()[1]
    ]
    assert len(tuple((tmp_path / "campaign" / "runs").glob("*.parquet"))) == 1
    assert len(tuple((tmp_path / "campaign" / "archives").iterdir())) == 1
    assert len(tuple((tmp_path / "campaign" / "logs").glob("*.log"))) == 1
    assert (tmp_path / "overlap").is_file()
    second = run_from_meta_config(meta)
    assert second["total"] == 1 and second["skipped"] == 0 and second["failed"] == 1
    assert second["completed"] == 0 and len(second["failures"]) == 1


def test_campaign_resume_filters_before_simulator_backend_and_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eqasim_sa.adapters.eqasim import execution as eqasim_execution

    from eqasim_sa.adapters import application
    from eqasim_sa.adapters.application import plan_from_meta_config
    from eqasim_sa.core.ledger import RunRecord, write_run
    from eqasim_sa.core.run import compute_run_id

    meta, _, _, _ = meta_fixture(tmp_path)
    meta.write_text(
        meta.read_text()
        .replace("values: [1, 2]", "values: [1]")
        .replace("values: [x, y]", "values: [x]")
    )
    plan_from_meta_config(meta)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    add_execution(meta, scratch)
    loaded = load_meta_config(meta)
    design = GridDesign(
        loaded.model_document,
        loaded.factors,
        loaded.seeds,
        dict(application.eqasim_requirements(loaded.indicator_requirements)),
        loaded.context_hash,
        loaded.context_descriptor,
        loaded.campaign_root,
    )
    specs = load_run_specs(
        loaded.campaign_root / "plan.parquet", design, loaded.campaign
    )
    for spec in specs:
        run_id = compute_run_id(spec.resolved_config, spec.seed)
        write_run(
            loaded.campaign_root,
            RunRecord(
                run_id,
                spec.campaign,
                spec.seed,
                spec.expected_context_hash,
                "node",
                1.0,
                spec.parameters,
            ),
        )
    monkeypatch.setattr(
        eqasim_execution,
        "EqasimSimulator",
        lambda *args: (_ for _ in ()).throw(AssertionError("simulator instantiated")),
    )
    monkeypatch.setattr(
        application,
        "LocalBackend",
        lambda *args: (_ for _ in ()).throw(AssertionError("backend instantiated")),
    )
    monkeypatch.setattr(
        eqasim_execution,
        "create_execution_seal",
        lambda *args: (_ for _ in ()).throw(AssertionError("data tree hashed")),
    )
    real_load_manifest = application.load_data_manifest
    parsed = 0

    def counted_manifest(path: Path):  # type: ignore[no-untyped-def]
        nonlocal parsed
        parsed += 1
        return real_load_manifest(path)

    monkeypatch.setattr(application, "load_data_manifest", counted_manifest)
    assert run_from_meta_config(meta) == {
        "status": "completed",
        "total": 0,
        "completed": 0,
        "skipped": 0,
        "failed": 0,
        "failures": [],
        "error": None,
    }
    assert parsed == 1
    assert not (scratch / "esa" / "demo").exists()


def _start_interruptible_spy_run(
    tmp_path: Path, **popen_options: object
) -> tuple[subprocess.Popen, Path, Path]:
    from eqasim_sa.adapters.application import plan_from_meta_config

    meta, _, _, _ = meta_fixture(tmp_path)
    meta.write_text(
        meta.read_text()
        .replace("values: [1, 2]", "values: [1]")
        .replace("values: [x, y]", "values: [x]")
    )
    plan_from_meta_config(meta)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    add_execution(
        meta,
        scratch,
        invocation=[sys.executable, str(Path(__file__).with_name("eqasim_spy.py"))],
    )
    environment = os.environ.copy()
    record, child = tmp_path / "record.json", tmp_path / "child.pid"
    environment.update(
        EQASIM_SPY_RECORD=str(record),
        EQASIM_SPY_CHILD_RECORD=str(child),
        EQASIM_SPY_SLEEP="60",
    )
    process = subprocess.Popen(
        [
            str(
                Path(sys.executable).with_name(
                    "eqasim-sa.exe" if sys.platform == "win32" else "eqasim-sa"
                )
            ),
            "run",
            "--meta-config",
            str(meta),
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **popen_options,
    )
    deadline = time.monotonic() + 10
    while (not child.exists() or not record.exists()) and time.monotonic() < deadline:
        time.sleep(0.02)
    if not (child.exists() and record.exists()):
        process.kill()
        process.communicate(timeout=10)
        pytest.fail("the spy pipeline did not start")
    return process, record, child


@pytest.mark.posix_only
def test_real_sigint_returns_130_and_reaps_owned_descendants(
    tmp_path: Path,
) -> None:
    process, record, child = _start_interruptible_spy_run(tmp_path)
    process.send_signal(signal.SIGINT)
    process.communicate(timeout=10)
    assert process.returncode == 130
    for pid in (json.loads(record.read_text())["pid"], int(child.read_text())):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert not tuple((tmp_path / "campaign" / "runs").glob("*.parquet"))


@pytest.mark.windows_only
def test_real_ctrl_break_interrupts_and_leaves_no_descendants(
    tmp_path: Path,
) -> None:
    # Windows cannot deliver SIGINT to one process: CTRL_BREAK_EVENT reaches
    # the new process group of eqasim-sa (and its descendants). Python either
    # turns it into KeyboardInterrupt (130) or ends with STATUS_CONTROL_C_EXIT;
    # in both cases the Job Object must leave no eqasim descendant alive.
    process, record, child = _start_interruptible_spy_run(
        tmp_path, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
    )
    pids = (json.loads(record.read_text(encoding="utf-8"))["pid"], int(child.read_text()))
    try:
        process.send_signal(signal.CTRL_BREAK_EVENT)
        process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    assert process.returncode in (130, 0xC000013A)
    deadline = time.monotonic() + 10
    while any(pid_alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not any(pid_alive(pid) for pid in pids)
    assert not tuple((tmp_path / "campaign" / "runs").glob("*.parquet"))


def _completed_spy_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, ExecutionEvidence]:
    from eqasim_sa.adapters.application import plan_from_meta_config
    from eqasim_sa.core import ledger

    meta, _, _, _ = meta_fixture(tmp_path)
    meta.write_text(
        meta.read_text()
        .replace("values: [1, 2]", "values: [1]")
        .replace("values: [x, y]", "values: [x]")
    )
    plan_from_meta_config(meta)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    add_execution(
        meta,
        scratch,
        invocation=[sys.executable, str(Path(__file__).with_name("eqasim_spy.py"))],
    )
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(tmp_path / "record.json"))
    assert run_from_meta_config(meta)["completed"] == 2
    marker = next((tmp_path / "campaign" / "runs").glob("*.parquet"))
    payload = dict(pq.ParquetFile(marker).schema_arrow.metadata)[
        ledger.EXECUTION_EVIDENCE
    ]
    return meta, ExecutionEvidence.from_bytes(payload)


def _evidence() -> ExecutionEvidence:
    return ExecutionEvidence(
        context_hash="1" * 64,
        data_tree_sha256="2" * 64,
        environment_sha256="3" * 64,
        environment_variables=("PATH",),
        executable_sha256="4" * 64,
        invocation=(os.path.abspath("python"),),
        model_commit="5" * 40,
        model_config_sha256="6" * 64,
        eqasim_sa_sha256="7" * 64,
    )


def test_resume_with_same_evidence_is_allowed() -> None:
    from eqasim_sa.core.run import require_same_machine

    require_same_machine({"r": _evidence()}, _evidence())


def test_resume_with_other_executable_is_refused() -> None:
    from eqasim_sa.core.run import require_same_machine

    with pytest.raises(
        ValueError, match="prepare a new campaign on this machine"
    ) as error:
        require_same_machine(
            {"r": _evidence()}, replace(_evidence(), executable_sha256="0" * 64)
        )
    assert "differing: executable_sha256)" in str(error.value)


@pytest.mark.parametrize(
    ("field", "value", "cause"),
    [
        ("executable_sha256", "0" * 64, "executable differs"),
        ("invocation", (os.path.abspath("other"),), "executable differs"),
        ("environment_sha256", "0" * 64, "environment differs"),
        ("environment_variables", ("HOME", "PATH"), "environment differs"),
        ("model_config_sha256", "0" * 64, "model configuration changed"),
        ("eqasim_sa_sha256", "0" * 64, "eqasim-sa installation changed"),
    ],
)
def test_resume_refusal_names_the_cause(field: str, value: object, cause: str) -> None:
    from eqasim_sa.core.run import require_same_machine

    with pytest.raises(ValueError) as error:
        require_same_machine({"r": _evidence()}, replace(_evidence(), **{field: value}))
    assert str(error.value) == (
        "campaign runs were executed on another machine or with other inputs: "
        f"{cause} (differing: {field}); prepare a new campaign on this machine"
    )


def test_resume_refusal_lists_each_cause_once() -> None:
    from eqasim_sa.core.run import require_same_machine

    current = replace(
        _evidence(),
        executable_sha256="0" * 64,
        invocation=(os.path.abspath("other"),),
        model_config_sha256="0" * 64,
    )
    with pytest.raises(ValueError) as error:
        require_same_machine({"r": _evidence()}, current)
    assert str(error.value).startswith(
        "campaign runs were executed on another machine or with other inputs: "
        "executable differs, model configuration changed (differing: "
        "executable_sha256, invocation, model_config_sha256);"
    )


def test_session_variables_do_not_change_the_child_environment() -> None:
    from eqasim_sa.adapters.execution_seal import normalized_child_environment

    base = {"PATH": "/bin", "JAVA_HOME": "/opt/java", "EQASIM_SEED": "1"}
    session = {
        name: "session-value"
        for name in (
            "WT_SESSION", "WT_PROFILE_ID", "TERM_SESSION_ID", "SECURITYSESSIONID",
            "ITERM_SESSION_ID", "TERM_PROGRAM_VERSION", "SSH_CLIENT",
            "SSH_CONNECTION", "SSH_TTY", "SSH_AUTH_SOCK", "WINDOWID", "DISPLAY",
            "XDG_SESSION_ID", "XDG_VTNR", "SHLVL", "OLDPWD", "PWD", "_",
            "HOSTNAME", "TEMP", "TMP", "TMPDIR", "SLURM_JOB_ID", "SUBMITIT_FOLDER",
        )
    }
    assert normalized_child_environment({**base, **session}) == base


def test_empty_campaign_has_no_constraint() -> None:
    from eqasim_sa.core.run import require_same_machine

    require_same_machine({}, _evidence())
    require_same_machine({"r": None}, _evidence())


def test_legacy_evidence_compares_only_its_own_fields() -> None:
    from eqasim_sa.core.run import require_same_machine

    legacy = replace(_evidence(), schema_version=1, eqasim_sa_sha256=None)
    require_same_machine({"r": legacy}, _evidence())
    with pytest.raises(ValueError, match="differing: environment_sha256"):
        require_same_machine({"r": legacy}, replace(_evidence(), environment_sha256="0" * 64))


def test_run_refuses_resume_on_another_machine_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta, _ = _completed_spy_campaign(tmp_path, monkeypatch)
    runs = sorted((tmp_path / "campaign" / "runs").glob("*.parquet"))
    runs[0].unlink()
    monkeypatch.setenv("EQASIM_RUNTIME_OTHER", "machine")
    with pytest.raises(ValueError, match="prepare a new campaign on this machine"):
        run_from_meta_config(meta)
    assert not runs[0].exists()


def test_analysis_does_not_compare_to_local_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta, _ = _completed_spy_campaign(tmp_path, monkeypatch)
    monkeypatch.setenv("EQASIM_RUNTIME_OTHER", "machine")
    monkeypatch.setattr(
        "eqasim_sa.adapters.application.current_machine_identity",
        lambda *args: (_ for _ in ()).throw(AssertionError("identity compared")),
    )
    analyze_from_meta_config(meta)


def test_analysis_without_data_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta, _ = _completed_spy_campaign(tmp_path, monkeypatch)
    shutil.rmtree(tmp_path / "data")
    shutil.rmtree(tmp_path / "scratch")
    analyze_from_meta_config(meta)


def test_check_reports_same_machine_for_completed_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eqasim_sa.adapters.checks import _same_machine_item
    from eqasim_sa.adapters.application import _design
    from eqasim_sa.core.plan import load_run_specs

    meta, _ = _completed_spy_campaign(tmp_path, monkeypatch)
    loaded = load_meta_config(meta)
    specs = load_run_specs(
        loaded.campaign_root / "plan.parquet", _design(loaded), loaded.campaign
    )
    assert _same_machine_item(loaded, specs)["status"] == "passed"
    assert _same_machine_item(loaded, ())["status"] == "skipped"
    monkeypatch.setenv("EQASIM_RUNTIME_OTHER", "machine")
    item = _same_machine_item(loaded, specs)
    assert item["status"] == "failed"
    assert "prepare a new campaign on this machine" in item["error"]


def test_resume_without_current_evidence_is_refused() -> None:
    from eqasim_sa.core.run import require_same_machine

    with pytest.raises(ValueError, match="execution evidence unavailable"):
        require_same_machine({"r": _evidence()}, None)


def test_check_does_not_hash_the_data_tree_and_needs_no_data_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eqasim_sa.adapters import data_manifest
    from eqasim_sa.adapters.application import _design
    from eqasim_sa.adapters.checks import _same_machine_item
    from eqasim_sa.core.plan import load_run_specs

    meta, _ = _completed_spy_campaign(tmp_path, monkeypatch)
    loaded = load_meta_config(meta)
    specs = load_run_specs(
        loaded.campaign_root / "plan.parquet", _design(loaded), loaded.campaign
    )
    monkeypatch.setattr(
        data_manifest,
        "descriptor_sha256",
        lambda descriptor: (_ for _ in ()).throw(AssertionError("data hashed")),
    )
    loaded = replace(loaded, execution=replace(loaded.execution, data_path=None))
    assert _same_machine_item(loaded, specs)["status"] == "passed"
    expired = _same_machine_item(loaded, specs, deadline=time.monotonic() - 1)
    assert expired["status"] == "failed" and "time budget" in expired["error"]


@pytest.mark.linux_only
def test_slurm_job_refuses_other_machine_on_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eqasim_sa.adapters.backends import slurm
    from eqasim_sa.adapters.checks import _same_machine_item

    meta, _ = _completed_spy_campaign(tmp_path, monkeypatch)
    meta.write_text(meta.read_text().replace('"local"', '"slurm"'))
    runs = sorted((tmp_path / "campaign" / "runs").glob("*.parquet"))
    runs[0].unlink()
    loaded = load_meta_config(meta)
    assert _same_machine_item(loaded, (object(),))["status"] == "skipped"
    # The submitter never refuses: only the worker compares.
    monkeypatch.setenv("EQASIM_RUNTIME_OTHER", "login-node")
    monkeypatch.setattr(
        "eqasim_sa.adapters.backends.slurm.SlurmBackend",
        lambda *args: (_ for _ in ()).throw(RuntimeError("submitted")),
    )
    with pytest.raises(RuntimeError, match="submitted"):
        run_from_meta_config(meta)
    job = slurm.EqasimSlurmJob(
        meta.resolve(),
        loaded.context_hash,
        hashlib.sha256(meta.read_bytes()).hexdigest(),
        loaded.eqasim_sa_sha256,
    )
    monkeypatch.setattr(
        slurm,
        "EqasimBatchExecutor",
        lambda *args: (_ for _ in ()).throw(AssertionError("executed")),
    )
    with pytest.raises(ValueError, match="prepare a new campaign on this machine"):
        job(WorkerBatch(0, ()))


def test_analysis_ignores_invalid_execution_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta, _ = _completed_spy_campaign(tmp_path, monkeypatch)
    shutil.rmtree(tmp_path / "data")
    (tmp_path / "data").write_text("not a directory")
    shutil.rmtree(tmp_path / "scratch")
    (tmp_path / "scratch").write_text("not a directory")
    analyze_from_meta_config(meta)
