import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import eqasim_sa.core.plan as plan_module
from eqasim_sa.adapters.application import run_from_meta_config
from eqasim_sa.adapters.backends.local import LocalBackend
from eqasim_sa.adapters.eqasim.simulator import eqasim_requirements
from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.adapters.worker import Indexed, WorkerBatch
from eqasim_sa.core.plan import GridDesign, build_plan, load_run_specs
from eqasim_sa.core.run import RunResult, summarize_runs


def meta_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    model = tmp_path / "model"
    model.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=model, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.test"], cwd=model, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=model, check=True)
    config = model / "config.yml"
    config.write_text(
        "working_directory: /ignored\nrun: [old]\nconfig:\n  a: 0\n  b: x\n  processes: 1\n  random_seed: 9\n"
    )
    (model / "lock.yml").write_text("locked\n")
    subprocess.run(["git", "add", "."], cwd=model, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=model, check=True)
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}\n")
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
    assert execution.scratch == scratch.resolve()
    assert execution.invocation == ("python3", "-m", "synpp")


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
    assert execution.slurm.timeout_min == 1440
    assert execution.slurm.mem_gb is None
    assert execution.slurm.partition is None
    assert plan_from_meta_config(meta).status == "unchanged"
    assert first.status == "created"
    assert (tmp_path / "campaign" / "plan.parquet").read_bytes() == before


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
    add_execution(meta, scratch, backend="slurm", workers=7, overrides={"processes": 1})
    observed = {}

    class Backend:
        def __init__(self, folder, workers, parameters):  # type: ignore[no-untyped-def]
            observed.update(folder=folder, workers=workers, parameters=parameters)

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

    monkeypatch.setattr(application, "SlurmBackend", Backend)
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
    assert observed["folder"] == loaded.campaign_root / ".submitit" / "%j"
    assert observed["folder"].parent.is_dir()
    assert not observed["folder"].parent.is_symlink()
    assert observed["workers"] == 7
    assert observed["parameters"] == {"timeout_min": 1440, "cpus_per_task": 1}
    assert type(observed["function"]).__name__ == "EqasimSlurmJob"
    assert (
        observed["function"].meta_config_sha256
        == hashlib.sha256(meta.read_bytes()).hexdigest()
    )
    assert len(observed["specs"]) == 2


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_slurm_campaign_rejects_unsafe_submitit_directory_before_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    from eqasim_sa.adapters import application
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
    add_execution(meta, scratch, backend="slurm")
    technical = tmp_path / "campaign" / ".submitit"
    if kind == "file":
        technical.write_text("not a directory")
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        technical.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(
        application,
        "SlurmBackend",
        lambda *args: (_ for _ in ()).throw(AssertionError("executor constructed")),
    )
    with pytest.raises(ValueError, match="submitit.*directory"):
        run_from_meta_config(meta)


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

    monkeypatch.setattr(application, "SlurmBackend", Backend)
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
        application,
        "SlurmBackend",
        lambda *args: (_ for _ in ()).throw(AssertionError("backend instantiated")),
    )
    monkeypatch.setattr(
        application,
        "EqasimSimulator",
        lambda *args: (_ for _ in ()).throw(AssertionError("simulator instantiated")),
    )
    assert run_from_meta_config(meta)["total"] == 0


def test_remote_jobs_survive_workdir_loss_and_resume_only_from_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eqasim_sa.adapters import application
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
    workdir = scratch / "eqasim-sa" / "demo" / "workers" / "0"
    job = EqasimSlurmJob(
        meta,
        loaded.context_hash,
        hashlib.sha256(meta.read_bytes()).hexdigest(),
    )
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(tmp_path / "record.json"))

    first_batch = WorkerBatch(0, (Indexed(0, specs[0]),))
    assert job(first_batch)[0].value.status == "completed"
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
    stale_attempt = workdir / "attempt-stale"
    stale_attempt.mkdir()
    (stale_attempt / "pipeline.json").write_text("not authoritative")
    (workdir / "cache.lock").write_text("not authoritative")
    shutil.rmtree(workdir)

    second_batch = WorkerBatch(0, (Indexed(0, specs[1]),))
    assert job(second_batch)[0].value.status == "completed"
    assert workdir.is_dir()
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in durable
    } == durable

    second_id = compute_run_id(specs[1].resolved_config, specs[1].seed)
    (loaded.campaign_root / "runs" / f"{second_id}.parquet").unlink()
    deceptive_attempt = workdir / "attempt-completed"
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

    monkeypatch.setattr(application, "SlurmBackend", Backend)
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
    design = plan_module.MorrisDesign(
        {"config": {"a": 0}},
        {"a": [0, 1]},
        (11,),
        {"run": ["stage"]},
        "c" * 64,
        {"version": "test"},
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
    design = plan_module.SobolDesign(
        {"config": {"a": 0}},
        {"a": [0, 1]},
        (11, 12),
        {"run": ["stage"]},
        "c" * 64,
        {"version": "test"},
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
        path.symlink_to(meta)
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


@pytest.mark.parametrize("backend", ["local", "slurm"])
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
        overrides={},
        invocation=("spy",),
        scratch=tmp_path,
        slurm=SimpleNamespace(
            timeout_min=1, mem_gb=None, partition=None, account=None, qos=None
        ),
    )
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
    )
    monkeypatch.setattr(application, "load_meta_config", lambda path: meta)
    monkeypatch.setattr(application, "_design", lambda *args: object())
    monkeypatch.setattr(application, "load_run_specs", lambda *args: specs)
    monkeypatch.setattr(
        application, "pending_run_specs", lambda *args: specs[:pending_count]
    )
    monkeypatch.setattr(
        application,
        "execute_run",
        lambda spec, *args: RunResult(
            compute_run_id(spec.resolved_config, spec.seed), "completed"
        ),
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
    monkeypatch.setattr(application, "SlurmBackend", Backend)
    monkeypatch.setattr(
        application,
        "EqasimSimulator",
        lambda *args: SimpleNamespace(interrupt=lambda: None),
    )
    monkeypatch.setattr(application, "EqasimSlurmJob", lambda *args: object())

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
        application,
        "EqasimSimulator",
        lambda *args: (_ for _ in ()).throw(AssertionError("simulator instantiated")),
    )
    monkeypatch.setattr(
        application,
        "LocalBackend",
        lambda *args: (_ for _ in ()).throw(AssertionError("backend instantiated")),
    )
    assert run_from_meta_config(meta) == {
        "status": "completed",
        "total": 0,
        "completed": 0,
        "skipped": 0,
        "failed": 0,
        "failures": [],
        "error": None,
    }
    assert not (scratch / "eqasim-sa" / "demo" / "workers").exists()


def test_real_sigint_returns_130_and_reaps_owned_descendants(
    tmp_path: Path,
) -> None:
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
            str(Path(sys.executable).with_name("eqasim-sa")),
            "run",
            "--meta-config",
            str(meta),
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 10
    while (not child.exists() or not record.exists()) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert child.exists() and record.exists()
    process.send_signal(signal.SIGINT)
    process.communicate(timeout=10)
    assert process.returncode == 130
    for pid in (json.loads(record.read_text())["pid"], int(child.read_text())):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert not tuple((tmp_path / "campaign" / "runs").glob("*.parquet"))
