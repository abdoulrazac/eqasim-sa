import copy
import json
import math
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

from eqasim_sa.adapters.eqasim.artifacts import ARTIFACT_STAGE, normalize_outputs
from eqasim_sa.adapters.eqasim.config import STAGES, EqasimSettings
from eqasim_sa.adapters.execution_seal import ExecutionSeal
from eqasim_sa.core.software_identity import SoftwareSourceGuard
from eqasim_sa.ports.simulator import (
    IndicatorRequirement,
    JsonValue,
    Manifest,
    ResolvedConfig,
    SimulationError,
)

PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
OPTION = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEDICATED = {"output_path", "output_prefix", "random_seed", "run", "working_directory"}
TERMINATION_GRACE_SECONDS = 2.0


class EqasimSimulator:
    def __init__(
        self,
        settings: EqasimSettings,
        base_config: ResolvedConfig,
        config_bytes: bytes | None = None,
        slot: int = 0,
        structured_factors: dict[str, dict[str, list[str]]] | None = None,
        execution_seal: ExecutionSeal | None = None,
        software_guard: SoftwareSourceGuard | None = None,
    ) -> None:
        self.settings = settings
        self.base_config = copy.deepcopy(base_config)
        self.config_bytes = (
            settings.config_path.read_bytes()
            if config_bytes is None
            else bytes(config_bytes)
        )
        self._processes: set[subprocess.Popen] = set()
        self._groups: set[int] = set()
        self._process_lock = threading.Lock()
        self._interrupted = threading.Event()
        self.workspace = settings.workspace(slot, self._interrupted.is_set)
        self.structured_factors = copy.deepcopy(structured_factors or {})
        self.execution_seal = execution_seal
        self.software_guard = software_guard

    def interrupt(self) -> None:
        self._interrupted.set()
        with self._process_lock:
            processes = tuple(self._processes)
            groups = tuple(self._groups)
        for group in groups:
            try:
                os.killpg(group, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + TERMINATION_GRACE_SECONDS
        for process in processes:
            try:
                process.wait(max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
        remaining = max(0.0, deadline - time.monotonic())
        if groups and remaining:
            time.sleep(remaining)
        for group in groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for process in processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=TERMINATION_GRACE_SECONDS)
                except (ChildProcessError, subprocess.TimeoutExpired):
                    pass

    def requirements(
        self, indicators: Sequence[IndicatorRequirement]
    ) -> Mapping[str, JsonValue]:
        return eqasim_requirements(indicators)

    def simulate(self, config: ResolvedConfig, seed: int, workdir: Path) -> Manifest:
        with self.workspace.session():
            if self.software_guard is not None:
                self.software_guard.guard()
            if self.execution_seal is not None:
                self.execution_seal.guard()
            try:
                manifest = self._simulate(config, seed, workdir)
            except BaseException:
                if self.execution_seal is not None:
                    self.execution_seal.guard()
                if self.software_guard is not None:
                    self.software_guard.guard()
                raise
            if self.execution_seal is not None:
                self.execution_seal.guard()
            if self.software_guard is not None:
                self.software_guard.guard()
            self.workspace.mark_clean()
            return manifest

    def _simulate(self, config: ResolvedConfig, seed: int, workdir: Path) -> Manifest:
        expected_workdir = self.workspace.root
        if workdir.resolve() != expected_workdir.resolve():
            raise ValueError(f"workdir must be {expected_workdir}")
        self.workspace.prepare_cache()
        resolved_workdir = workdir.resolve(strict=True)
        if not resolved_workdir.is_relative_to(self.settings.scratch):
            raise ValueError("workdir escapes scratch")
        attempt = self.workspace.create_attempt()
        output = attempt / "output"
        normalized = attempt / "normalized"
        output.mkdir(parents=True)
        snapshot = attempt / "config.yml"
        log = attempt / "run.log"
        log.touch(mode=0o600)
        base = copy.deepcopy(self.base_config)
        base_config = base.get("config")
        if not isinstance(base_config, dict):
            raise TypeError("base config block must be a mapping")
        base_config["random_seed"] = seed
        nested = config.get("config")
        base_nested = base.get("config")
        if not isinstance(nested, dict) or not isinstance(base_nested, dict):
            raise TypeError("resolved config block must be a mapping")
        stages = config.get("run")
        if not isinstance(stages, list) or not all(
            isinstance(stage, str) and stage for stage in stages
        ):
            raise TypeError(
                "resolved run block must be a list of non-empty stage names"
            )
        requested_stages = tuple(dict.fromkeys(stages))
        if not requested_stages:
            raise ValueError("resolved run block must not be empty")
        unknown_stages = [stage for stage in requested_stages if stage not in STAGES]
        if unknown_stages:
            raise ValueError(f"unsupported producing stage: {unknown_stages[0]!r}")
        prefix = nested.get("output_prefix", "ile_de_france_")
        if (
            not isinstance(prefix, str)
            or ".." in prefix
            or (prefix and not PREFIX.fullmatch(prefix))
        ):
            raise ValueError("output_prefix must be empty or a safe file prefix")
        removed = base_nested.keys() - nested.keys()
        if removed:
            raise ValueError(f"resolved config removes YAML keys: {sorted(removed)}")
        structured_changes = set()
        for key, configurations in self.structured_factors.items():
            if key in nested and key in base_nested and not _same_value(
                nested[key], base_nested[key]
            ):
                if not any(
                    _same_value(nested[key], payload)
                    for payload in configurations.values()
                ):
                    raise ValueError(
                        f"structured override {key!r} is not a declared configuration"
                    )
                structured_changes.add(key)
        if structured_changes:
            snapshot_document = copy.deepcopy(self.base_config)
            for key in structured_changes:
                snapshot_document["config"][key] = copy.deepcopy(nested[key])
            snapshot.write_text(
                yaml.safe_dump(snapshot_document, sort_keys=False), encoding="utf-8"
            )
        else:
            snapshot.write_bytes(self.config_bytes)
        command = [
            *self.settings.invocation,
            str(snapshot),
            "--working-directory",
            str(self.workspace.cache),
        ]
        for stage in requested_stages:
            command.extend(("--run", stage))
        command.extend(
            (
                "--random_seed",
                str(seed),
                "--output_path",
                str(output),
                "--output_prefix",
                prefix,
            )
        )
        for key in sorted(nested):
            if not OPTION.fullmatch(key) or "\0" in key:
                raise ValueError(f"invalid synpp option name: {key!r}")
            if key in DEDICATED:
                if key not in {"output_path", "output_prefix", "random_seed"}:
                    raise ValueError(
                        f"config key conflicts with dedicated option: {key!r}"
                    )
                continue
            if key in base_nested and _same_value(nested[key], base_nested[key]):
                continue
            if key in structured_changes:
                continue
            validate_transport(key, nested[key])
            command.extend((f"--{key}", _scalar_argument(key, nested[key])))
        with log.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps({"command": command}, ensure_ascii=False) + "\n")
            stream.flush()
            try:
                self.workspace.mark_active()
                self.workspace.validate_session()
                completed = self._run_owned(
                    command,
                    cwd=self.settings.eqasim_root,
                    stdin=subprocess.DEVNULL,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    shell=False,
                    check=False,
                )
            except OSError as error:
                stream.write(f"process launch failed: {error}\n")
                stream.flush()
                raise SimulationError(
                    f"eqasim process launch failed: {error}", log, attempt
                ) from error
            stream.write(f"\nreturncode={completed.returncode}\n")
        if completed.returncode:
            raise SimulationError(
                f"eqasim process exited with code {completed.returncode}", log, attempt
            )
        try:
            artifacts = normalize_outputs(output, normalized, prefix, requested_stages)
        except Exception as error:
            with log.open("a", encoding="utf-8") as stream:
                stream.write(f"normalization failed: {error}\n")
            raise SimulationError(str(error), log, attempt) from error
        if self._interrupted.is_set():
            raise KeyboardInterrupt
        scratch_bytes = self.workspace.cache_bytes()
        evidence = None if self.execution_seal is None else self.execution_seal.evidence
        return Manifest(artifacts, attempt, scratch_bytes, evidence)

    def _run_owned(self, command: list[str], **options) -> subprocess.CompletedProcess:  # type: ignore[no-untyped-def]
        options.pop("check", None)
        timeout = options.pop("timeout", None)
        if self.execution_seal is not None:
            options["env"] = self.execution_seal.environment
        with self._process_lock:
            if self._interrupted.is_set():
                raise KeyboardInterrupt
            process = subprocess.Popen(command, start_new_session=True, **options)
            self._processes.add(process)
            self._groups.add(process.pid)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            returncode = process.returncode
            self._terminate_remaining_group(process.pid)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
        finally:
            with self._process_lock:
                self._processes.discard(process)
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    self._groups.discard(process.pid)
        if self._interrupted.is_set():
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    @staticmethod
    def _terminate_remaining_group(group: int) -> None:
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return
        try:
            os.killpg(group, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + TERMINATION_GRACE_SECONDS
        while time.monotonic() < deadline:
            try:
                os.killpg(group, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass


def eqasim_requirements(
    indicators: Sequence[IndicatorRequirement],
) -> Mapping[str, JsonValue]:
    stages = ["synthesis.output"]
    for requirement in indicators:
        stage = ARTIFACT_STAGE.get(requirement.artifact)
        if stage is None:
            raise ValueError(
                f"indicator {requirement.indicator!r} requires artifact {requirement.artifact!r} "
                "with no producing stage"
            )
        if stage not in stages:
            stages.append(stage)
    return {"run": stages}


def probe_environment(
    invocation: tuple[str, ...], root: Path, timeout: float = 20.0
) -> None:
    missing = root / f".eqasim-sa-probe-{uuid.uuid4().hex}.yml"
    completed = subprocess.run(
        [*invocation, str(missing)],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
        timeout=timeout,
    )
    output = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode == 0 or "Config file does not exist:" not in output:
        raise RuntimeError(
            f"synpp environment probe failed: returncode={completed.returncode}"
        )


def validate_transport(name: str, value: object) -> None:
    if not OPTION.fullmatch(name) or name in DEDICATED:
        raise ValueError(f"invalid or dedicated synpp option: {name!r}")
    _scalar_argument(name, value)


def _scalar_argument(key: str, value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(
            f"override {key!r} is not faithfully representable by synpp 1.6.2"
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"override {key!r} must be finite")
    argument = str(value)
    if "\0" in argument:
        raise ValueError(f"override {key!r} contains a NUL character")
    return argument


def _same_value(left: object, right: object) -> bool:
    return type(left) is type(right) and left == right
