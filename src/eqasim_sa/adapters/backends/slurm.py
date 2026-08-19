import hashlib
import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import submitit

from eqasim_sa.adapters.eqasim.config import EqasimSettings
from eqasim_sa.adapters.eqasim.simulator import EqasimSimulator
from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.adapters.worker import Indexed, WorkerBatch, execute_batch, run_lease
from eqasim_sa.core.run import RunResult, RunSpec, compute_run_id, execute_run

T = TypeVar("T")
R = TypeVar("R")


def _cancel(jobs: Iterable[object]) -> None:
    for job in jobs:
        try:
            job.cancel(check=False)  # type: ignore[attr-defined]
        except BaseException:
            pass


class SlurmBackend:
    def __init__(
        self,
        folder: Path,
        max_workers: int,
        parameters: dict[str, object],
        executor_factory=submitit.AutoExecutor,
    ) -> None:
        if (
            isinstance(max_workers, bool)
            or not isinstance(max_workers, int)
            or max_workers <= 0
        ):
            raise ValueError("max_workers must be a strictly positive integer")
        self.folder = folder
        self.max_workers = max_workers
        self.parameters = dict(parameters)
        self.executor_factory = executor_factory

    def map(self, function: Callable[[T], R], specs: Iterable[T]) -> Iterator[R]:
        values = tuple(specs)
        if not values:
            return
        executor = self.executor_factory(folder=str(self.folder), cluster="slurm")
        executor.update_parameters(
            **self.parameters, slurm_array_parallelism=self.max_workers
        )
        jobs = []
        next_job = 0
        try:
            jobs = executor.map_array(function, values)
            if len(jobs) != len(values):
                raise RuntimeError(
                    "SLURM backend job cardinality does not match submitted specs"
                )
            for index, job in enumerate(jobs):
                next_job = index
                result = job.result()
                next_job = index + 1
                yield result
        except BaseException:
            _cancel(jobs[next_job:])
            raise


@dataclass(frozen=True)
class EqasimSlurmJob:
    meta_config_path: Path
    expected_context_hash: str
    meta_config_sha256: str

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
            or any(
                item.value.expected_context_hash != self.expected_context_hash
                for item in batch.items
            )
        ):
            raise ValueError("contextHash changed before remote execution")
        if meta.execution is None or meta.execution.backend != "slurm":
            raise ValueError("remote execution requires execution.backend slurm")
        settings = EqasimSettings(
            meta.model_config,
            meta.model_root,
            meta.execution.invocation,
            meta.campaign,
            meta.execution.scratch,
        )
        simulator = EqasimSimulator(
            settings, meta.model_document, meta.model_bytes, batch.slot
        )
        try:
            with simulator.lease():
                return execute_batch(
                    batch,
                    lambda spec: _execute_exclusive(
                        spec, simulator, meta.campaign_root, simulator.paths.root
                    ),
                )
        except BaseException:
            try:
                simulator.interrupt()
            except BaseException:
                pass
            raise


def _execute_exclusive(spec, simulator, campaign_root, workdir):  # type: ignore[no-untyped-def]
    run_id = compute_run_id(spec.resolved_config, spec.seed)
    with run_lease(campaign_root, run_id):
        return execute_run(spec, simulator, campaign_root, workdir)
