import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import submitit

from eqasim_sa.adapters.eqasim.config import EqasimSettings
from eqasim_sa.adapters.eqasim.simulator import EqasimSimulator
from eqasim_sa.adapters.execution_seal import create_execution_seal
from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.adapters.worker import Indexed, WorkerBatch, execute_batch
from eqasim_sa.core.durable_publication import (
    directory_is_current,
    file_lease,
    fsync_directory,
    open_directory,
    secure_directory,
)
from eqasim_sa.core.run import RunResult, RunSpec, execute_run
from eqasim_sa.core.software_identity import SoftwareSourceGuard

T = TypeVar("T")
R = TypeVar("R")
ACTIVE_JOB_JOURNAL = "active-jobs.json"
JOB_ID = re.compile(r"^[0-9]+(?:_[0-9]+)?$")
JOURNAL_VERSION = 1
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def _journal_bytes(job_ids: tuple[str, ...]) -> bytes:
    return json.dumps(
        {"version": JOURNAL_VERSION, "jobIds": list(job_ids)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _validate_job_ids(values: Iterable[object]) -> tuple[str, ...]:
    job_ids = tuple(values)
    if (
        not job_ids
        or any(
            type(job_id) is not str or JOB_ID.fullmatch(job_id) is None
            for job_id in job_ids
        )
        or len(set(job_ids)) != len(job_ids)
    ):
        raise ValueError("SLURM active-job journal contains invalid or duplicate job IDs")
    return job_ids  # type: ignore[return-value]


def _validate_journal_file(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
    ):
        raise ValueError("SLURM active-job journal must be an owned regular file")


def _journal_state(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read_journal(path: Path) -> tuple[str, ...] | None:
    parent = open_directory(path.parent, create=True)
    descriptor = -1
    try:
        try:
            visible = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None
        _validate_journal_file(visible)
        descriptor = os.open(path.name, _READ_FLAGS, dir_fd=parent)
        initial = os.fstat(descriptor)
        _validate_journal_file(initial)
        if (
            _journal_state(initial) != _journal_state(visible)
            or initial.st_size > 1024 * 1024
        ):
            raise ValueError("SLURM active-job journal is unstable or too large")
        chunks = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        final = os.fstat(descriptor)
        visible = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if (
            _journal_state(final) != _journal_state(initial)
            or _journal_state(visible) != _journal_state(initial)
            or not directory_is_current(path.parent, parent)
        ):
            raise RuntimeError("SLURM active-job journal changed while reading")
        content = b"".join(chunks)
        try:
            document = json.loads(content)
            if not isinstance(document, dict) or set(document) != {"version", "jobIds"}:
                raise ValueError
            if (
                document["version"] != JOURNAL_VERSION
                or type(document["jobIds"]) is not list
            ):
                raise ValueError
            job_ids = _validate_job_ids(document["jobIds"])
            if content != _journal_bytes(job_ids):
                raise ValueError
            return job_ids
        except (TypeError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise ValueError("SLURM active-job journal has an invalid schema") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _write_journal(path: Path, job_ids: tuple[str, ...]) -> None:
    content = _journal_bytes(_validate_job_ids(job_ids))
    parent = open_directory(path.parent, create=True)
    temporary = f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write while persisting SLURM active-job journal")
            remaining = remaining[written:]
        os.fsync(descriptor)
        _validate_journal_file(os.fstat(descriptor))
        os.link(
            temporary,
            path.name,
            src_dir_fd=parent,
            dst_dir_fd=parent,
            follow_symlinks=False,
        )
        os.unlink(temporary, dir_fd=parent)
        temporary = ""
        os.fsync(parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
        os.close(parent)


def _remove_journal(path: Path, expected: tuple[str, ...]) -> None:
    if _read_journal(path) != expected:
        raise RuntimeError("SLURM active-job journal changed before removal")
    parent = open_directory(path.parent)
    try:
        visible = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        _validate_journal_file(visible)
        os.unlink(path.name, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(parent)


def _job_done(job: object) -> bool:
    return bool(job.done(force_check=True))  # type: ignore[attr-defined]


def _terminal_barrier(jobs: Iterable[object], *, cancel_unfinished: bool) -> None:
    values = tuple(jobs)
    errors: list[BaseException] = []
    if cancel_unfinished:
        for job in values:
            try:
                done = _job_done(job)
            except BaseException as error:
                errors.append(error)
                done = False
            if not done:
                try:
                    job.cancel(check=False)  # type: ignore[attr-defined]
                except BaseException as error:
                    errors.append(error)
    for job in values:
        try:
            job.wait()  # type: ignore[attr-defined]
        except BaseException as error:
            errors.append(error)
    unfinished = []
    for job in values:
        try:
            if not _job_done(job):
                unfinished.append(getattr(job, "job_id", "unknown"))
        except BaseException as error:
            errors.append(error)
            unfinished.append(getattr(job, "job_id", "unknown"))
    if unfinished:
        detail = f"; unfinished={unfinished}" if unfinished else ""
        raise RuntimeError(
            f"SLURM jobs did not reach a confirmed terminal state{detail}"
        ) from (errors[0] if errors else None)


class SlurmBackend:
    def __init__(
        self,
        campaign_root: Path,
        max_workers: int,
        parameters: dict[str, object],
        executor_factory=submitit.AutoExecutor,
        job_factory=submitit.SlurmJob,
    ) -> None:
        if (
            isinstance(max_workers, bool)
            or not isinstance(max_workers, int)
            or max_workers <= 0
        ):
            raise ValueError("max_workers must be a strictly positive integer")
        campaign = secure_directory(campaign_root)
        technical = secure_directory(campaign / ".eqasim-sa", create=True)
        locks = secure_directory(technical / "locks", create=True)
        state = secure_directory(technical / "slurm", create=True)
        submitit = secure_directory(state / "submitit", create=True)
        fsync_directory(campaign)
        fsync_directory(technical)
        fsync_directory(state)
        self.campaign_root = campaign
        self.folder = submitit / "%j"
        self.legacy_folder = campaign / ".submitit" / "%j"
        self.max_workers = max_workers
        self.parameters = dict(parameters)
        self.executor_factory = executor_factory
        self.job_factory = job_factory
        self.journal_path = state / ACTIVE_JOB_JOURNAL
        self.lease_path = locks / "slurm.lock"

    def _reconcile_journal(self, journal: Path, folder: Path) -> None:
        job_ids = _read_journal(journal)
        if job_ids is None:
            return
        try:
            jobs = tuple(self.job_factory(folder, job_id) for job_id in job_ids)
        except BaseException as error:
            raise RuntimeError(
                "cannot reconstruct jobs from the SLURM active-job journal"
            ) from error
        _terminal_barrier(jobs, cancel_unfinished=True)
        _remove_journal(journal, job_ids)

    def _reconcile(self) -> None:
        legacy = self.campaign_root / ".submitit"
        if legacy.exists() or legacy.is_symlink():
            secure_directory(legacy)
            self._reconcile_journal(
                legacy / ACTIVE_JOB_JOURNAL,
                self.legacy_folder,
            )
        self._reconcile_journal(self.journal_path, self.folder)

    def map(self, function: Callable[[T], R], specs: Iterable[T]) -> Iterator[R]:
        values = tuple(specs)
        with file_lease(self.lease_path):
            self._reconcile()
            if not values:
                return
            executor = self.executor_factory(folder=str(self.folder), cluster="slurm")
            executor.update_parameters(
                **self.parameters, slurm_array_parallelism=self.max_workers
            )
            jobs: list[object] = []
            job_ids: tuple[str, ...] | None = None
            journal_written = False
            complete = False
            try:
                jobs = list(executor.map_array(function, values))
                job_ids = _validate_job_ids(
                    getattr(job, "job_id", None) for job in jobs
                )
                _write_journal(self.journal_path, job_ids)
                journal_written = True
                if len(jobs) != len(values):
                    raise RuntimeError(
                        "SLURM backend job cardinality does not match submitted specs"
                    )
                for job in jobs:
                    yield job.result()  # type: ignore[attr-defined]
                complete = True
            finally:
                if jobs:
                    _terminal_barrier(jobs, cancel_unfinished=not complete)
                if journal_written and job_ids is not None:
                    _remove_journal(self.journal_path, job_ids)


@dataclass(frozen=True)
class EqasimSlurmJob:
    meta_config_path: Path
    expected_context_hash: str
    meta_config_sha256: str
    expected_eqasim_sa_sha256: str

    def __call__(
        self, batch: WorkerBatch[RunSpec]
    ) -> tuple[Indexed[RunResult], ...]:
        actual_sha256 = hashlib.sha256(self.meta_config_path.read_bytes()).hexdigest()
        if actual_sha256 != self.meta_config_sha256:
            raise ValueError("meta-config content changed before remote execution")
        meta = load_meta_config(self.meta_config_path)
        reloaded_sha256 = hashlib.sha256(self.meta_config_path.read_bytes()).hexdigest()
        if reloaded_sha256 != self.meta_config_sha256:
            raise ValueError("meta-config content changed during remote reload")
        descriptor = json.dumps(
            meta.context_descriptor,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        computed = hashlib.sha256(b"eqasim-sa:context:v1\0" + descriptor).hexdigest()
        if (
            set(meta.context_descriptor)
            != {
                "eqasimCommit",
                "dataManifestSha256",
                "lockfileSha256",
                "eqasimSaVersion",
            }
            or computed != meta.context_hash
        ):
            raise ValueError("context descriptor does not match contextHash")
        if (
            meta.context_hash != self.expected_context_hash
            or meta.eqasim_sa_sha256 != self.expected_eqasim_sa_sha256
            or any(
                item.value.expected_context_hash != self.expected_context_hash
                for item in batch.items
            )
        ):
            raise ValueError("contextHash changed before remote execution")
        if meta.execution is None or meta.execution.backend != "slurm":
            raise ValueError("remote execution requires execution.backend slurm")
        if (
            getattr(getattr(meta, "charter", None), "status", None) == "scientific"
            and getattr(meta.execution, "data_path", None) is None
        ):
            raise ValueError(
                "scientific execution requires an effective data_path and execution seal"
            )
        execution_seal = (
            create_execution_seal(meta)
            if getattr(meta.execution, "data_path", None) is not None
            else None
        )
        settings = EqasimSettings(
            meta.model_config,
            meta.model_root,
            execution_seal.invocation
            if execution_seal is not None
            else meta.execution.invocation,
            meta.campaign,
            meta.execution.scratch,
            meta.context_hash,
        )
        simulator = EqasimSimulator(
            settings,
            meta.model_document,
            meta.model_bytes,
            batch.slot,
            meta.structured_factors,
            execution_seal,
            SoftwareSourceGuard(meta.software_identity),
        )
        try:
            with simulator.workspace.session():
                return execute_batch(
                    batch,
                    lambda spec: execute_run(
                        spec, simulator, meta.campaign_root, simulator.workspace.root
                    ),
                )
        except BaseException:
            try:
                simulator.interrupt()
            except BaseException:
                pass
            raise
