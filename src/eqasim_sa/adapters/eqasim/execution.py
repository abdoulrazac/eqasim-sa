"""Shared execution of Eqasim worker batches."""

from __future__ import annotations

import copy
import threading
from pathlib import Path

from eqasim_sa.adapters.eqasim.config import EqasimSettings
from eqasim_sa.adapters.eqasim.simulator import EqasimSimulator
from eqasim_sa.adapters.execution_seal import create_execution_seal
from eqasim_sa.adapters.worker import Indexed, WorkerBatch, execute_batch
from eqasim_sa.core.run import RunResult, RunSpec, execute_run
from eqasim_sa.core.software_identity import SoftwareSourceGuard


def _apply_execution_overrides(
    specs: tuple[RunSpec, ...], overrides: dict[str, object]
) -> tuple[RunSpec, ...]:
    if not overrides:
        return specs
    result = []
    for spec in specs:
        config = copy.deepcopy(spec.resolved_config)
        nested = config.get("config")
        if not isinstance(nested, dict):
            raise TypeError("resolved config block must be a mapping")
        for name, value in overrides.items():
            nested[name] = str(value) if isinstance(value, Path) else value
        result.append(
            RunSpec(
                spec.campaign,
                config,
                spec.seed,
                spec.expected_context_hash,
                spec.parameters,
            )
        )
    return tuple(result)


class EqasimBatchExecutor:
    """Own simulator construction, workspace sessions, execution, and interruption."""

    @staticmethod
    def validate(meta) -> None:  # type: ignore[no-untyped-def]
        if meta.execution is None:
            raise ValueError("execution section is required")
        if (
            getattr(getattr(meta, "charter", None), "status", None) == "scientific"
            and getattr(meta.execution, "data_path", None) is None
        ):
            raise ValueError(
                "scientific execution requires an effective data_path and execution seal"
            )

    def __init__(
        self,
        meta,  # type: ignore[no-untyped-def]
        *,
        campaign: str | None = None,
        campaign_root: Path | None = None,
        scratch: Path | None = None,
    ) -> None:
        self.validate(meta)
        self.meta = meta
        self.campaign_root = campaign_root or meta.campaign_root
        self.execution_seal = (
            create_execution_seal(meta)
            if getattr(meta.execution, "data_path", None) is not None
            else None
        )
        self.settings = EqasimSettings(
            meta.model_config,
            meta.model_root,
            (
                self.execution_seal.invocation
                if self.execution_seal is not None
                else meta.execution.invocation
            ),
            campaign or meta.campaign,
            scratch or meta.execution.scratch,
            meta.context_hash,
        )
        self.software_guard = SoftwareSourceGuard(meta.software_identity)
        self._interrupted = threading.Event()
        self._simulators: set[EqasimSimulator] = set()
        self._simulators_lock = threading.Lock()

    def interrupt(self) -> None:
        self._interrupted.set()
        with self._simulators_lock:
            simulators = tuple(self._simulators)
        failure: BaseException | None = None
        for simulator in simulators:
            try:
                simulator.interrupt()
            except BaseException as error:
                failure = failure or error
        if failure is not None:
            raise failure

    def execute(
        self, batch: WorkerBatch[RunSpec]
    ) -> tuple[Indexed[RunResult], ...]:
        simulator = EqasimSimulator(
            self.settings,
            self.meta.model_document,
            self.meta.model_bytes,
            batch.slot,
            self.meta.structured_factors,
            self.execution_seal,
            self.software_guard,
        )
        with self._simulators_lock:
            self._simulators.add(simulator)
        try:
            with simulator.workspace.session():
                return execute_batch(
                    batch,
                    lambda spec: execute_run(
                        spec,
                        simulator,
                        self.campaign_root,
                        simulator.workspace.root,
                        self._interrupted.is_set,
                    ),
                )
        except BaseException:
            try:
                self.interrupt()
            except BaseException:
                pass
            raise
