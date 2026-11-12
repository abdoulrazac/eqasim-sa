"""Submitit-backed SLURM execution with durable submission recovery."""

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

from eqasim_sa.adapters.eqasim.execution import EqasimBatchExecutor
from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.adapters.worker import Indexed, WorkerBatch
from eqasim_sa.core.context_identity import ExecutionContextIdentity
from eqasim_sa.utils.filesystem import (
    PublicationOutcomeUnknown,
    file_lease,
    fsync_directory,
    secure_directory,
    write_bytes_exclusive,
)
from eqasim_sa.utils.posix_dirfd import directory_is_current, open_directory
from eqasim_sa.core.run import RunResult, RunSpec

T = TypeVar("T")
R = TypeVar("R")
ACTIVE_JOB_JOURNAL = "active-jobs.json"
SUBMISSION_INTENT = "submission-intent.json"
JOB_ID = re.compile(r"^[0-9]+(?:_[0-9]+)?$")
SUBMISSION_ID = re.compile(r"^[0-9a-f]{32}$")
JOURNAL_VERSION = 2
LEGACY_JOURNAL_VERSION = 1
INTENT_VERSION = 1
# Short staging names produced by utils.filesystem.write_exclusive.
_STAGED_TEMPORARY = re.compile(r"^\.t[0-9a-f]{8}$")
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


@dataclass(frozen=True)
class ActiveJobs:
    """Durable identity of jobs submitted as one recoverable SLURM batch."""

    job_ids: tuple[str, ...]
    submission_id: str | None = None


@dataclass(frozen=True)
class SubmissionIntent:
    """Pre-submission record that prevents uncertain duplicate submission."""

    submission_id: str
    spec_count: int


def _validate_submission_id(value: object) -> str:
    if type(value) is not str or SUBMISSION_ID.fullmatch(value) is None:
        raise ValueError("SLURM submission ID must be 32 lowercase hexadecimal characters")
    return value


def _journal_bytes(journal: ActiveJobs) -> bytes:
    document: dict[str, object] = {
        "version": (
            JOURNAL_VERSION
            if journal.submission_id is not None
            else LEGACY_JOURNAL_VERSION
        ),
        "jobIds": list(journal.job_ids),
    }
    if journal.submission_id is not None:
        document["submissionId"] = _validate_submission_id(journal.submission_id)
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _intent_bytes(intent: SubmissionIntent) -> bytes:
    _validate_submission_id(intent.submission_id)
    if type(intent.spec_count) is not int or intent.spec_count <= 0:
        raise ValueError("SLURM submission intent requires a positive spec count")
    return json.dumps(
        {
            "version": INTENT_VERSION,
            "submissionId": intent.submission_id,
            "specCount": intent.spec_count,
        },
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


def _validate_document_file(
    info: os.stat_result, links: frozenset[int] = frozenset({1})
) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink not in links
    ):
        raise ValueError("SLURM state must be an owned regular file")


def _document_state(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read_document_at(
    parent: int, name: str, *, links: frozenset[int] = frozenset({1})
) -> bytes:
    visible = os.stat(name, dir_fd=parent, follow_symlinks=False)
    _validate_document_file(visible, links)
    descriptor = os.open(name, _READ_FLAGS, dir_fd=parent)
    try:
        initial = os.fstat(descriptor)
        _validate_document_file(initial, links)
        if (
            _document_state(initial) != _document_state(visible)
            or initial.st_size > 1024 * 1024
        ):
            raise ValueError("SLURM state is unstable or too large")
        chunks = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        final = os.fstat(descriptor)
        visible = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            _document_state(final) != _document_state(initial)
            or _document_state(visible) != _document_state(initial)
        ):
            raise RuntimeError("SLURM state changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _parse_journal(content: bytes) -> ActiveJobs:
    try:
        document = json.loads(content)
        if not isinstance(document, dict):
            raise ValueError
        version = document.get("version")
        expected = (
            {"version", "jobIds"}
            if version == LEGACY_JOURNAL_VERSION
            else {"version", "jobIds", "submissionId"}
            if version == JOURNAL_VERSION
            else set()
        )
        if set(document) != expected or type(document.get("jobIds")) is not list:
            raise ValueError
        journal = ActiveJobs(
            _validate_job_ids(document["jobIds"]),
            None
            if version == LEGACY_JOURNAL_VERSION
            else _validate_submission_id(document["submissionId"]),
        )
        if content != _journal_bytes(journal):
            raise ValueError
        return journal
    except (TypeError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("SLURM active-job journal has an invalid schema") from error


def _parse_intent(content: bytes) -> SubmissionIntent:
    try:
        document = json.loads(content)
        if (
            not isinstance(document, dict)
            or set(document) != {"version", "submissionId", "specCount"}
            or document["version"] != INTENT_VERSION
        ):
            raise ValueError
        intent = SubmissionIntent(
            _validate_submission_id(document["submissionId"]),
            document["specCount"],
        )
        if content != _intent_bytes(intent):
            raise ValueError
        return intent
    except (TypeError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("SLURM submission intent has an invalid schema") from error


def _read_document(path: Path, parser):  # type: ignore[no-untyped-def]
    parent = open_directory(path.parent, create=True)
    try:
        try:
            content = _read_document_at(parent, path.name)
        except FileNotFoundError:
            return None
        if not directory_is_current(path.parent, parent):
            raise RuntimeError("SLURM state directory changed while reading")
        return parser(content)
    finally:
        os.close(parent)


def _staged_temporaries(parent: int) -> list[str]:
    return sorted(
        name for name in os.listdir(parent) if _STAGED_TEMPORARY.fullmatch(name)
    )


def _belongs_to(parent: int, name: str, visible, parser) -> bool:  # type: ignore[no-untyped-def]
    """Attribute a short-named staged file by hard-link identity or schema."""
    held = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if visible is not None:
        return (held.st_dev, held.st_ino) == (visible.st_dev, visible.st_ino)
    try:
        parser(_read_document_at(parent, name))
    except (OSError, ValueError, RuntimeError):
        return False
    return True


def _recover_exclusive_document(path: Path, parser) -> None:  # type: ignore[no-untyped-def]
    """Finish an interrupted no-clobber publication under the SLURM lease."""
    parent = open_directory(path.parent, create=True)
    try:
        try:
            visible = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            visible = None
        temporaries = [
            name
            for name in _staged_temporaries(parent)
            if _belongs_to(parent, name, visible, parser)
        ]
        if visible is None and not temporaries:
            return
        if not directory_is_current(path.parent, parent):
            raise RuntimeError("SLURM state directory changed during recovery")
        if visible is None:
            if len(temporaries) != 1:
                raise ValueError("SLURM state has ambiguous interrupted publications")
            temporary = temporaries[0]
            held = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
            content = _read_document_at(parent, temporary)
            parser(content)
            os.link(
                temporary,
                path.name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
                follow_symlinks=False,
            )
            os.fsync(parent)
            os.unlink(temporary, dir_fd=parent)
            os.fsync(parent)
            final = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            _validate_document_file(final)
            if (final.st_dev, final.st_ino) != (held.st_dev, held.st_ino):
                raise RuntimeError("SLURM state changed while recovery was finalized")
            if not directory_is_current(path.parent, parent):
                raise RuntimeError("SLURM state directory changed during recovery")
            return
        _validate_document_file(visible, frozenset({1, 2}))
        if visible.st_nlink == 1 and not temporaries:
            return
        if visible.st_nlink != 2 or len(temporaries) != 1:
            raise ValueError("SLURM state has ambiguous hard links")
        temporary = temporaries[0]
        held = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
        _validate_document_file(held, frozenset({2}))
        if (held.st_dev, held.st_ino) != (visible.st_dev, visible.st_ino):
            raise ValueError("SLURM state temporary does not match its destination")
        parser(_read_document_at(parent, path.name, links=frozenset({2})))
        os.unlink(temporary, dir_fd=parent)
        os.fsync(parent)
        final = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        _validate_document_file(final)
        if (final.st_dev, final.st_ino) != (visible.st_dev, visible.st_ino):
            raise RuntimeError("SLURM state changed while recovery was finalized")
        if not directory_is_current(path.parent, parent):
            raise RuntimeError("SLURM state directory changed during recovery")
    finally:
        os.close(parent)


def _read_journal(path: Path) -> ActiveJobs | None:
    return _read_document(path, _parse_journal)


def _read_intent(path: Path) -> SubmissionIntent | None:
    return _read_document(path, _parse_intent)


def _write_journal(path: Path, journal: ActiveJobs | tuple[str, ...]) -> None:
    value = journal if isinstance(journal, ActiveJobs) else ActiveJobs(journal)
    content = _journal_bytes(
        ActiveJobs(_validate_job_ids(value.job_ids), value.submission_id)
    )
    write_bytes_exclusive(content, path)


def _write_intent(path: Path, intent: SubmissionIntent) -> None:
    write_bytes_exclusive(_intent_bytes(intent), path)


def _remove_document(path: Path, expected: bytes, parser) -> None:  # type: ignore[no-untyped-def]
    current = _read_document(path, parser)
    if current is None:
        raise RuntimeError("SLURM state disappeared before removal")
    actual = (
        _journal_bytes(current)
        if isinstance(current, ActiveJobs)
        else _intent_bytes(current)
    )
    if actual != expected:
        raise RuntimeError("SLURM state changed before removal")
    parent = open_directory(path.parent)
    try:
        visible = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        _validate_document_file(visible)
        os.unlink(path.name, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(parent)


def _remove_journal(path: Path, expected: ActiveJobs) -> None:
    _remove_document(path, _journal_bytes(expected), _parse_journal)


def _remove_intent(path: Path, expected: SubmissionIntent) -> None:
    _remove_document(path, _intent_bytes(expected), _parse_intent)


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
    """Submit batches to SLURM while reconciling durable scheduler state."""

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
        self.max_workers = max_workers
        self.parameters = dict(parameters)
        self.executor_factory = executor_factory
        self.job_factory = job_factory
        self.journal_path = state / ACTIVE_JOB_JOURNAL
        self.intent_path = state / SUBMISSION_INTENT
        self.lease_path = locks / "slurm.lock"

    def _reconcile_jobs(self, journal: ActiveJobs) -> None:
        try:
            jobs = tuple(
                self.job_factory(self.folder, job_id) for job_id in journal.job_ids
            )
        except BaseException as error:
            raise RuntimeError(
                "cannot reconstruct jobs from the SLURM active-job journal"
            ) from error
        _terminal_barrier(jobs, cancel_unfinished=True)

    def _reconcile(self) -> None:
        legacy = self.campaign_root / ".submitit"
        if legacy.exists() or legacy.is_symlink():
            secure_directory(legacy)
            legacy_journal = _read_journal(legacy / ACTIVE_JOB_JOURNAL)
            if legacy_journal is not None:
                raise RuntimeError(
                    "legacy SLURM active-job journal is read-only; reconcile it "
                    "with the eqasim-sa version that created it"
                )
        _recover_exclusive_document(self.intent_path, _parse_intent)
        _recover_exclusive_document(self.journal_path, _parse_journal)
        state = open_directory(self.journal_path.parent)
        try:
            if _staged_temporaries(state):
                raise ValueError("SLURM state has ambiguous interrupted publications")
        finally:
            os.close(state)
        intent = _read_intent(self.intent_path)
        journal = _read_journal(self.journal_path)
        if intent is not None and journal is None:
            raise PublicationOutcomeUnknown(
                "a SLURM submission started without a recoverable job journal; "
                "inspect the scheduler before removing the submission intent"
            )
        if journal is None:
            return
        if intent is not None and (
            journal.submission_id != intent.submission_id
            or len(journal.job_ids) != intent.spec_count
        ):
            raise ValueError("SLURM journal does not match its submission intent")
        self._reconcile_jobs(journal)
        if intent is not None:
            _remove_intent(self.intent_path, intent)
        _remove_journal(self.journal_path, journal)

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
            submission = SubmissionIntent(uuid.uuid4().hex, len(values))
            journal: ActiveJobs | None = None
            intent_written = False
            journal_written = False
            complete = False
            try:
                _write_intent(self.intent_path, submission)
                intent_written = True
                try:
                    jobs = list(executor.map_array(function, values))
                    job_ids = _validate_job_ids(
                        getattr(job, "job_id", None) for job in jobs
                    )
                except BaseException as error:
                    raise PublicationOutcomeUnknown(
                        "SLURM submission outcome is unknown; the durable intent "
                        "prevents an automatic duplicate submission"
                    ) from error
                journal = ActiveJobs(job_ids, submission.submission_id)
                _write_journal(self.journal_path, journal)
                journal_written = True
                _remove_intent(self.intent_path, submission)
                intent_written = False
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
                if journal_written and journal is not None:
                    _remove_journal(self.journal_path, journal)
                if intent_written and jobs:
                    _remove_intent(self.intent_path, submission)


@dataclass(frozen=True)
class EqasimSlurmJob:
    """Reload and validate a sealed campaign before executing a remote batch."""

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
        try:
            context_identity = ExecutionContextIdentity.from_descriptor(
                meta.context_descriptor
            )
            context_identity.require_sha256(meta.context_hash)
        except ValueError as error:
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
        from eqasim_sa.adapters.application import enforce_same_machine

        enforce_same_machine(meta)
        executor = EqasimBatchExecutor(meta)
        return executor.execute(batch)

