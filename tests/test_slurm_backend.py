import hashlib
import json
import os
import socket
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import cloudpickle
import pytest

from eqasim_sa.adapters.backends.slurm import EqasimSlurmJob, SlurmBackend
from eqasim_sa.adapters.worker import Indexed, WorkerBatch
from eqasim_sa.core.run import RunResult, RunSpec


class FakeJob:
    def __init__(self, result=None, error: BaseException | None = None) -> None:
        self.value = result
        self.error = error
        self.cancelled = False

    def result(self):  # type: ignore[no-untyped-def]
        if self.error is not None:
            raise self.error
        return self.value

    def cancel(self, check: bool = True) -> None:
        self.cancelled = True


class BrokenCancelJob(FakeJob):
    def cancel(self, check: bool = True) -> None:
        self.cancelled = True
        raise RuntimeError("cancel failed")


class FakeExecutor:
    instances: list["FakeExecutor"] = []
    jobs: list[FakeJob] = []

    def __init__(self, folder: str, cluster: str) -> None:
        self.folder = folder
        self.cluster = cluster
        self.parameters = {}
        self.map_calls = []
        self.instances.append(self)

    def update_parameters(self, **parameters) -> None:  # type: ignore[no-untyped-def]
        self.parameters = parameters

    def map_array(self, function, specs):  # type: ignore[no-untyped-def]
        self.map_calls.append((function, tuple(specs)))
        return list(self.jobs)


@pytest.fixture(autouse=True)
def reset_executor() -> None:
    FakeExecutor.instances = []
    FakeExecutor.jobs = []


def test_slurm_backend_forces_cluster_submits_one_ordered_array(tmp_path: Path) -> None:
    FakeExecutor.jobs = [FakeJob("second"), FakeJob("first")]
    backend = SlurmBackend(
        tmp_path / ".submitit" / "%j",
        3,
        {"timeout_min": 90, "cpus_per_task": 4, "slurm_partition": "cpu"},
        executor_factory=FakeExecutor,
    )

    assert list(backend.map(str, (2, 1))) == ["second", "first"]
    executor = FakeExecutor.instances[0]
    assert executor.cluster == "slurm"
    assert executor.folder == str(tmp_path / ".submitit" / "%j")
    assert executor.parameters == {
        "timeout_min": 90,
        "cpus_per_task": 4,
        "slurm_partition": "cpu",
        "slurm_array_parallelism": 3,
    }
    assert executor.map_calls == [(str, (2, 1))]


def test_slurm_backend_accepts_empty_input_without_executor(tmp_path: Path) -> None:
    backend = SlurmBackend(tmp_path / "%j", 1, {}, executor_factory=FakeExecutor)
    assert list(backend.map(str, ())) == []
    assert FakeExecutor.instances == []


def test_slurm_backend_rejects_result_cardinality_and_cancels_on_abnormal_exit(
    tmp_path: Path,
) -> None:
    FakeExecutor.jobs = [FakeJob(1)]
    backend = SlurmBackend(tmp_path / "%j", 2, {}, executor_factory=FakeExecutor)
    with pytest.raises(RuntimeError, match="cardinality"):
        list(backend.map(str, (1, 2)))

    failure, pending = FakeJob(error=RuntimeError("collect failed")), FakeJob(2)
    FakeExecutor.jobs = [failure, pending]
    with pytest.raises(RuntimeError, match="collect failed"):
        list(backend.map(str, (1, 2)))
    assert failure.cancelled and pending.cancelled

    completed, failure, pending = (
        FakeJob(1),
        FakeJob(error=RuntimeError("second failed")),
        FakeJob(3),
    )
    FakeExecutor.jobs = [completed, failure, pending]
    with pytest.raises(RuntimeError, match="second failed"):
        list(backend.map(str, (1, 2, 3)))
    assert not completed.cancelled
    assert failure.cancelled and pending.cancelled


def test_slurm_backend_cancels_and_propagates_keyboard_interrupt(
    tmp_path: Path,
) -> None:
    interrupted, pending = FakeJob(error=KeyboardInterrupt()), FakeJob(2)
    FakeExecutor.jobs = [interrupted, pending]
    backend = SlurmBackend(tmp_path / "%j", 1, {}, executor_factory=FakeExecutor)
    with pytest.raises(KeyboardInterrupt):
        list(backend.map(str, (1, 2)))
    assert interrupted.cancelled and pending.cancelled


def test_slurm_backend_cancels_on_early_iterator_close_without_masking_errors(
    tmp_path: Path,
) -> None:
    first, second = BrokenCancelJob(1), FakeJob(2)
    FakeExecutor.jobs = [first, second]
    iterator = SlurmBackend(tmp_path / "%j", 1, {}, executor_factory=FakeExecutor).map(
        str, (1, 2)
    )
    assert next(iterator) == 1
    iterator.close()
    assert not first.cancelled and second.cancelled


def test_remote_job_is_named_serializable_and_revalidates_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eqasim_sa.adapters.backends import slurm

    deceptive = tmp_path / "must-not-select-workdir"
    monkeypatch.setenv("SLURM_JOB_ID", str(deceptive / "job"))
    monkeypatch.setenv("SLURM_NODEID", str(deceptive / "node"))
    monkeypatch.setenv("TMPDIR", str(deceptive / "tmp"))
    monkeypatch.setattr(
        os,
        "uname",
        lambda: (_ for _ in ()).throw(AssertionError("hostname read by remote job")),
    )
    monkeypatch.setattr(
        socket,
        "gethostname",
        lambda: (_ for _ in ()).throw(AssertionError("hostname read by remote job")),
    )
    descriptor = {
        "eqasimCommit": "a",
        "dataManifestSha256": "b",
        "lockfileSha256": "c",
        "eqasimSaVersion": "d",
    }
    encoded = json.dumps(
        descriptor, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    context_hash = hashlib.sha256(b"eqasim-sa:context:v1\0" + encoded).hexdigest()
    meta_path = tmp_path / "meta.yml"
    meta_bytes = b"complete meta-config bytes\n"
    meta_path.write_bytes(meta_bytes)
    meta_sha256 = hashlib.sha256(meta_bytes).hexdigest()
    node_scratch = tmp_path / "node-scratch"
    node_scratch.mkdir()
    meta = SimpleNamespace(
        context_descriptor=descriptor,
        context_hash=context_hash,
        execution=SimpleNamespace(
            backend="slurm", invocation=("python",), scratch=node_scratch
        ),
        model_config=tmp_path / "config.yml",
        model_root=tmp_path,
        campaign="demo",
        model_document={"config": {}},
        model_bytes=b"config",
        campaign_root=tmp_path,
        structured_factors={},
    )
    monkeypatch.setattr(slurm, "load_meta_config", lambda path: meta)
    settings_calls = []

    def settings(*args):  # type: ignore[no-untyped-def]
        settings_calls.append(args)
        return SimpleNamespace()

    monkeypatch.setattr(slurm, "EqasimSettings", settings)

    class Simulator:
        def __init__(self) -> None:
            self.interruptions = 0
            self.paths = SimpleNamespace(
                root=node_scratch / "eqasim-sa" / "demo" / "workers" / "2"
            )

        def lease(self):  # type: ignore[no-untyped-def]
            return nullcontext(self)

        def interrupt(self) -> None:
            self.interruptions += 1
            raise RuntimeError("interrupt failed")

    simulator = Simulator()
    monkeypatch.setattr(slurm, "EqasimSimulator", lambda *args: simulator)
    observed = []
    monkeypatch.setattr(
        slurm,
        "execute_run",
        lambda spec, actual, root, workdir: (
            observed.append((spec, actual, root, workdir))
            or RunResult("id", "completed")
        ),
    )
    spec = RunSpec("demo", {"config": {}}, 1, context_hash, {})
    job = EqasimSlurmJob(meta_path, context_hash, meta_sha256)

    batch = WorkerBatch(2, (Indexed(0, spec),))
    restored, restored_batch = cloudpickle.loads(cloudpickle.dumps((job, batch)))
    assert type(restored).__module__ == "eqasim_sa.adapters.backends.slurm"
    assert type(restored).__name__ == "EqasimSlurmJob"
    assert vars(restored) == {
        "meta_config_path": tmp_path / "meta.yml",
        "expected_context_hash": context_hash,
        "meta_config_sha256": meta_sha256,
    }
    assert restored(restored_batch) == (Indexed(0, RunResult("id", "completed")),)
    assert settings_calls == [
        (
            meta.model_config,
            meta.model_root,
            meta.execution.invocation,
            meta.campaign,
            node_scratch,
        )
    ]
    assert observed == [
        (
            spec,
            simulator,
            tmp_path,
            node_scratch / "eqasim-sa" / "demo" / "workers" / "2",
        )
    ]

    with pytest.raises(ValueError, match="contextHash"):
        EqasimSlurmJob(meta_path, "different", meta_sha256)(batch)

    monkeypatch.setattr(
        slurm,
        "execute_run",
        lambda *args: (_ for _ in ()).throw(RuntimeError("remote failed")),
    )
    with pytest.raises(RuntimeError, match="remote failed"):
        job(batch)
    assert simulator.interruptions == 1


def test_remote_job_rejects_meta_config_byte_changes_before_reload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eqasim_sa.adapters.backends import slurm

    meta_path = tmp_path / "meta.yml"
    original = b"campaign: original\n"
    meta_path.write_bytes(original)
    job = EqasimSlurmJob(meta_path, "context", hashlib.sha256(original).hexdigest())
    meta_path.write_bytes(b"campaign: changed\n")
    reloaded = False

    def load(path):  # type: ignore[no-untyped-def]
        nonlocal reloaded
        reloaded = True
        raise AssertionError("changed meta-config was reloaded")

    monkeypatch.setattr(slurm, "load_meta_config", load)
    spec = RunSpec("demo", {"config": {}}, 1, "context", {})
    with pytest.raises(ValueError, match="meta-config.*changed"):
        job(WorkerBatch(0, (Indexed(0, spec),)))
    assert not reloaded

    meta_path.write_bytes(original)

    def mutate_during_reload(path):  # type: ignore[no-untyped-def]
        meta_path.write_bytes(b"campaign: changed during reload\n")
        return object()

    monkeypatch.setattr(slurm, "load_meta_config", mutate_during_reload)
    with pytest.raises(ValueError, match="changed during remote reload"):
        job(WorkerBatch(0, (Indexed(0, spec),)))
