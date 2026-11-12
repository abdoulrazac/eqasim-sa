import hashlib
import json
import sys
import subprocess
import os
import socket
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import cloudpickle
import pytest

from eqasim_sa.adapters.backends import slurm
from eqasim_sa.adapters.backends.slurm import EqasimSlurmJob, SlurmBackend
from eqasim_sa.adapters.worker import Indexed, WorkerBatch
from eqasim_sa.core.run import RunResult, RunSpec
from eqasim_sa.core.software_identity import read_software_identity
from tests.conftest import make_symlink


def slurm_state(root: Path) -> Path:
    return root / ".eqasim-sa" / "slurm"


def journal_path(root: Path) -> Path:
    return slurm_state(root) / slurm.ACTIVE_JOB_JOURNAL


def intent_path(root: Path) -> Path:
    return slurm_state(root) / slurm.SUBMISSION_INTENT


def submitit_folder(root: Path) -> Path:
    return slurm_state(root) / "submitit" / "%j"


class FakeJob:
    next_id = 100

    def __init__(
        self,
        result=None,
        error: BaseException | None = None,
        *,
        job_id: str | None = None,
        wait_error: BaseException | None = None,
        on_result=None,
    ) -> None:
        self.job_id = job_id or str(FakeJob.next_id)
        FakeJob.next_id += 1
        self.value = result
        self.error = error
        self.wait_error = wait_error
        self.on_result = on_result
        self.cancelled = False
        self.finished = False
        self.wait_calls = 0

    def result(self):  # type: ignore[no-untyped-def]
        if self.on_result is not None:
            self.on_result()
        self.finished = True
        if self.error is not None:
            raise self.error
        return self.value

    def cancel(self, check: bool = True) -> None:
        self.cancelled = True

    def done(self, force_check: bool = False) -> bool:
        return self.finished

    def wait(self) -> None:
        self.wait_calls += 1
        if self.wait_error is not None:
            raise self.wait_error
        self.finished = True


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
    FakeJob.next_id = 100


def test_slurm_backend_forces_cluster_submits_one_ordered_array(tmp_path: Path) -> None:
    journal = journal_path(tmp_path)
    observed_journals = []
    FakeExecutor.jobs = [
        FakeJob(
            "second",
            on_result=lambda: observed_journals.append(
                json.loads(journal.read_text())
            ),
        ),
        FakeJob("first"),
    ]
    backend = SlurmBackend(
        tmp_path,
        3,
        {"timeout_min": 90, "cpus_per_task": 4, "slurm_partition": "cpu"},
        executor_factory=FakeExecutor,
    )

    assert list(backend.map(str, (2, 1))) == ["second", "first"]
    executor = FakeExecutor.instances[0]
    assert executor.cluster == "slurm"
    assert executor.folder == str(submitit_folder(tmp_path))
    assert executor.parameters == {
        "timeout_min": 90,
        "cpus_per_task": 4,
        "slurm_partition": "cpu",
        "slurm_array_parallelism": 3,
    }
    assert executor.map_calls == [(str, (2, 1))]
    assert len(observed_journals) == 1
    assert observed_journals[0]["jobIds"] == [
        job.job_id for job in FakeExecutor.jobs
    ]
    assert observed_journals[0]["version"] == 2
    assert slurm.SUBMISSION_ID.fullmatch(observed_journals[0]["submissionId"])
    assert all(job.wait_calls == 1 for job in FakeExecutor.jobs)
    assert not journal.exists()


def test_slurm_backend_accepts_empty_input_without_executor(tmp_path: Path) -> None:
    backend = SlurmBackend(tmp_path, 1, {}, executor_factory=FakeExecutor)
    assert list(backend.map(str, ())) == []
    assert FakeExecutor.instances == []


def test_slurm_backend_rejects_result_cardinality_and_cancels_on_abnormal_exit(
    tmp_path: Path,
) -> None:
    FakeExecutor.jobs = [FakeJob(1)]
    backend = SlurmBackend(tmp_path, 2, {}, executor_factory=FakeExecutor)
    with pytest.raises(RuntimeError, match="cardinality"):
        list(backend.map(str, (1, 2)))
    assert FakeExecutor.jobs[0].cancelled
    assert FakeExecutor.jobs[0].wait_calls == 1
    assert not journal_path(tmp_path).exists()

    failure, pending = FakeJob(error=RuntimeError("collect failed")), FakeJob(2)
    FakeExecutor.jobs = [failure, pending]
    with pytest.raises(RuntimeError, match="collect failed"):
        list(backend.map(str, (1, 2)))
    assert not failure.cancelled and pending.cancelled
    assert failure.wait_calls == pending.wait_calls == 1

    completed, failure, pending = (
        FakeJob(1),
        FakeJob(error=RuntimeError("second failed")),
        FakeJob(3),
    )
    FakeExecutor.jobs = [completed, failure, pending]
    with pytest.raises(RuntimeError, match="second failed"):
        list(backend.map(str, (1, 2, 3)))
    assert not completed.cancelled
    assert not failure.cancelled and pending.cancelled
    assert all(job.wait_calls == 1 for job in (completed, failure, pending))


def test_slurm_backend_cancels_and_propagates_keyboard_interrupt(
    tmp_path: Path,
) -> None:
    interrupted, pending = FakeJob(error=KeyboardInterrupt()), FakeJob(2)
    FakeExecutor.jobs = [interrupted, pending]
    backend = SlurmBackend(tmp_path, 1, {}, executor_factory=FakeExecutor)
    with pytest.raises(KeyboardInterrupt):
        list(backend.map(str, (1, 2)))
    assert not interrupted.cancelled and pending.cancelled
    assert interrupted.wait_calls == pending.wait_calls == 1


def test_slurm_backend_cancels_on_early_iterator_close_without_masking_errors(
    tmp_path: Path,
) -> None:
    first, second = BrokenCancelJob(1), FakeJob(2)
    FakeExecutor.jobs = [first, second]
    iterator = SlurmBackend(tmp_path, 1, {}, executor_factory=FakeExecutor).map(
        str, (1, 2)
    )
    assert next(iterator) == 1
    iterator.close()
    assert not first.cancelled and second.cancelled
    assert first.wait_calls == second.wait_calls == 1
    assert not journal_path(tmp_path).exists()


def test_slurm_backend_reconciles_leftover_journal_before_new_submission(
    tmp_path: Path,
) -> None:
    journal = journal_path(tmp_path)
    journal.parent.mkdir(parents=True)
    jobs = {
        "700_0": FakeJob(job_id="700_0"),
        "700_1": FakeJob(job_id="700_1"),
    }
    slurm._write_journal(journal, tuple(jobs))
    reconstructed = []

    def factory(folder: Path, job_id: str) -> FakeJob:
        reconstructed.append((folder, job_id))
        return jobs[job_id]

    backend = SlurmBackend(
        tmp_path,
        1,
        {},
        executor_factory=FakeExecutor,
        job_factory=factory,
    )

    assert list(backend.map(str, ())) == []
    assert reconstructed == [
        (submitit_folder(tmp_path), "700_0"),
        (submitit_folder(tmp_path), "700_1"),
    ]
    assert all(job.cancelled and job.wait_calls == 1 for job in jobs.values())
    assert not journal.exists()
    assert FakeExecutor.instances == []


def test_slurm_backend_treats_legacy_journal_as_read_only_and_refuses_submit(
    tmp_path: Path,
) -> None:
    legacy_journal = tmp_path / ".submitit" / slurm.ACTIVE_JOB_JOURNAL
    legacy_journal.parent.mkdir()
    slurm._write_journal(legacy_journal, ("750",))
    before = legacy_journal.read_bytes(), legacy_journal.stat().st_mtime_ns
    reconstructed = []

    def factory(folder: Path, job_id: str) -> FakeJob:
        reconstructed.append((folder, job_id))
        return FakeJob(job_id=job_id)

    backend = SlurmBackend(
        tmp_path,
        1,
        {},
        executor_factory=FakeExecutor,
        job_factory=factory,
    )

    with pytest.raises(RuntimeError, match="read-only"):
        list(backend.map(str, (1,)))

    assert not reconstructed and not FakeExecutor.instances
    assert (legacy_journal.read_bytes(), legacy_journal.stat().st_mtime_ns) == before


def test_slurm_backend_rejects_symlinked_legacy_state_before_submit(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external-submitit"
    external.mkdir()
    make_symlink(tmp_path / ".submitit", external, target_is_directory=True)
    backend = SlurmBackend(tmp_path, 1, {}, executor_factory=FakeExecutor)

    with pytest.raises(ValueError, match="directory"):
        list(backend.map(str, (1,)))

    assert FakeExecutor.instances == []


def test_slurm_backend_preserves_leftover_journal_when_recovery_is_unconfirmed(
    tmp_path: Path,
) -> None:
    journal = journal_path(tmp_path)
    journal.parent.mkdir(parents=True)
    job = FakeJob(
        job_id="800",
        wait_error=RuntimeError("scheduler unavailable"),
    )
    slurm._write_journal(journal, (job.job_id,))
    backend = SlurmBackend(
        tmp_path,
        1,
        {},
        executor_factory=FakeExecutor,
        job_factory=lambda folder, job_id: job,
    )

    with pytest.raises(RuntimeError, match="terminal state"):
        list(backend.map(str, (1,)))

    assert job.cancelled and job.wait_calls == 1
    assert journal.is_file()
    assert FakeExecutor.instances == []


@pytest.mark.parametrize("damage", ["schema", "unsafe-id", "symlink"])
def test_slurm_backend_refuses_unsafe_leftover_journal_before_executor(
    tmp_path: Path,
    damage: str,
) -> None:
    journal = journal_path(tmp_path)
    journal.parent.mkdir(parents=True)
    if damage == "symlink":
        target = tmp_path / "target.json"
        target.write_text('{"jobIds":["900"],"version":1}')
        make_symlink(journal, target)
    elif damage == "unsafe-id":
        journal.write_text('{"jobIds":["../900"],"version":1}')
    else:
        journal.write_text('{"jobs":["900"],"version":1}')
    backend = SlurmBackend(
        tmp_path,
        1,
        {},
        executor_factory=FakeExecutor,
    )

    with pytest.raises(ValueError, match="SLURM|journal"):
        list(backend.map(str, (1,)))

    assert journal.exists() or journal.is_symlink()
    assert FakeExecutor.instances == []


def test_slurm_submission_failure_leaves_durable_intent_and_blocks_retry(
    tmp_path: Path,
) -> None:
    class UncertainExecutor(FakeExecutor):
        def map_array(self, function, specs):  # type: ignore[no-untyped-def]
            raise OSError("scheduler reply lost")

    backend = SlurmBackend(tmp_path, 1, {}, executor_factory=UncertainExecutor)
    with pytest.raises(slurm.PublicationOutcomeUnknown, match="outcome is unknown"):
        list(backend.map(str, (1,)))

    intent = json.loads(intent_path(tmp_path).read_text())
    assert intent["version"] == 1 and intent["specCount"] == 1
    assert slurm.SUBMISSION_ID.fullmatch(intent["submissionId"])
    assert not journal_path(tmp_path).exists()
    UncertainExecutor.instances = []

    with pytest.raises(slurm.PublicationOutcomeUnknown, match="inspect the scheduler"):
        list(backend.map(str, (1,)))
    assert not UncertainExecutor.instances


def test_slurm_reconciles_matching_intent_and_journal_after_crash(
    tmp_path: Path,
) -> None:
    state = slurm_state(tmp_path)
    state.mkdir(parents=True)
    submission = slurm.SubmissionIntent("a" * 32, 1)
    journal = slurm.ActiveJobs(("910",), submission.submission_id)
    slurm._write_intent(intent_path(tmp_path), submission)
    slurm._write_journal(journal_path(tmp_path), journal)
    job = FakeJob(job_id="910")
    backend = SlurmBackend(
        tmp_path,
        1,
        {},
        executor_factory=FakeExecutor,
        job_factory=lambda folder, job_id: job,
    )

    assert list(backend.map(str, ())) == []
    assert job.cancelled and job.wait_calls == 1
    assert not intent_path(tmp_path).exists() and not journal_path(tmp_path).exists()


@pytest.mark.parametrize("linked", [False, True])
def test_slurm_recovers_interrupted_journal_publication(
    tmp_path: Path, linked: bool
) -> None:
    state = slurm_state(tmp_path)
    state.mkdir(parents=True)
    journal = slurm.ActiveJobs(("920",))
    temporary = state / (".t" + "b" * 8)
    temporary.write_bytes(slurm._journal_bytes(journal))
    if linked:
        os.link(temporary, journal_path(tmp_path))
    job = FakeJob(job_id="920")
    backend = SlurmBackend(
        tmp_path,
        1,
        {},
        executor_factory=FakeExecutor,
        job_factory=lambda folder, job_id: job,
    )

    assert list(backend.map(str, ())) == []
    assert job.cancelled and job.wait_calls == 1
    assert not temporary.exists() and not journal_path(tmp_path).exists()


def test_slurm_attributes_short_staged_names_to_their_document(
    tmp_path: Path,
) -> None:
    state = slurm_state(tmp_path)
    state.mkdir(parents=True)
    submission = slurm.SubmissionIntent("a" * 32, 1)
    journal = slurm.ActiveJobs(("930",), submission.submission_id)
    (state / (".t" + "1" * 8)).write_bytes(slurm._intent_bytes(submission))
    (state / (".t" + "2" * 8)).write_bytes(slurm._journal_bytes(journal))
    job = FakeJob(job_id="930")
    backend = SlurmBackend(
        tmp_path,
        1,
        {},
        executor_factory=FakeExecutor,
        job_factory=lambda folder, job_id: job,
    )

    assert list(backend.map(str, ())) == []
    assert job.cancelled and job.wait_calls == 1
    assert not tuple(state.glob(".t*"))


def test_slurm_refuses_unattributable_staged_file(tmp_path: Path) -> None:
    state = slurm_state(tmp_path)
    state.mkdir(parents=True)
    (state / (".t" + "c" * 8)).write_bytes(b"truncated")
    backend = SlurmBackend(tmp_path, 1, {}, executor_factory=FakeExecutor)

    with pytest.raises(ValueError, match="ambiguous interrupted publications"):
        list(backend.map(str, ()))


def test_remote_job_is_named_serializable_and_revalidates_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eqasim_sa.adapters.backends import slurm
    from eqasim_sa.adapters.eqasim import execution as eqasim_execution

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
    software_identity = read_software_identity()
    descriptor = {
        "eqasimCommit": "a",
        "dataManifestSha256": "b",
        "lockfileSha256": "c",
        "eqasimSaVersion": "d",
    }
    encoded = json.dumps(
        descriptor, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    context_hash = hashlib.sha256(b"eqasim-sa:context:v2\0" + encoded).hexdigest()
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
        software_identity=software_identity,
        eqasim_sa_sha256=software_identity.sha256,
    )
    monkeypatch.setattr(slurm, "load_meta_config", lambda path: meta)
    settings_calls = []

    def settings(*args):  # type: ignore[no-untyped-def]
        settings_calls.append(args)
        return SimpleNamespace()

    monkeypatch.setattr(eqasim_execution, "EqasimSettings", settings)

    class Simulator:
        def __init__(self) -> None:
            self.interruptions = 0
            self.workspace = SimpleNamespace(
                root=node_scratch / "esa" / "demo" / "w2",
                session=lambda: nullcontext(self),
            )

        def interrupt(self) -> None:
            self.interruptions += 1
            raise RuntimeError("interrupt failed")

    simulator = Simulator()
    monkeypatch.setattr(eqasim_execution, "EqasimSimulator", lambda *args: simulator)
    observed = []
    monkeypatch.setattr(
        eqasim_execution,
        "execute_run",
        lambda spec, actual, root, workdir, interrupted: (
            observed.append((spec, actual, root, workdir))
            or RunResult("id", "completed")
        ),
    )
    monkeypatch.setattr(
        "eqasim_sa.adapters.application.enforce_same_machine", lambda *args: None
    )
    spec = RunSpec("demo", {"config": {}}, 1, context_hash, {})
    job = EqasimSlurmJob(
        meta_path, context_hash, meta_sha256, software_identity.sha256
    )

    batch = WorkerBatch(2, (Indexed(0, spec),))
    restored, restored_batch = cloudpickle.loads(cloudpickle.dumps((job, batch)))
    assert type(restored).__module__ == "eqasim_sa.adapters.backends.slurm"
    assert type(restored).__name__ == "EqasimSlurmJob"
    assert vars(restored) == {
        "meta_config_path": tmp_path / "meta.yml",
        "expected_context_hash": context_hash,
        "meta_config_sha256": meta_sha256,
        "expected_eqasim_sa_sha256": software_identity.sha256,
    }
    assert restored(restored_batch) == (Indexed(0, RunResult("id", "completed")),)
    assert settings_calls == [
        (
            meta.model_config,
            meta.model_root,
            meta.execution.invocation,
            meta.campaign,
            node_scratch,
            context_hash,
        )
    ]
    assert observed == [
        (
            spec,
            simulator,
            tmp_path,
            node_scratch / "esa" / "demo" / "w2",
        )
    ]

    meta.charter = SimpleNamespace(status="scientific")
    with pytest.raises(ValueError, match="effective data_path and execution seal"):
        job(batch)
    del meta.charter

    with pytest.raises(ValueError, match="contextHash"):
        EqasimSlurmJob(
            meta_path, "different", meta_sha256, software_identity.sha256
        )(batch)

    monkeypatch.setattr(
        eqasim_execution,
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
    job = EqasimSlurmJob(
        meta_path,
        "context",
        hashlib.sha256(original).hexdigest(),
        "e" * 64,
    )
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


def test_package_imports_without_slurm_module_loaded() -> None:
    code = (
        "import eqasim_sa.adapters.application, sys; "
        "assert 'eqasim_sa.adapters.backends.slurm' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
