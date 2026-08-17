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

from eqasim_sa.adapters.eqasim.artifacts import ARTIFACT_STAGE, normalize_outputs
from eqasim_sa.adapters.eqasim.config import STAGES, EqasimSettings
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
CACHE_COORDINATION_CAPABILITY = "synpp-cache-coordination-v1"
TERMINATION_GRACE_SECONDS = 2.0


def _has_cache_coordination(stdout: str) -> bool:
    return stdout == f"{CACHE_COORDINATION_CAPABILITY}\n"


def probe_cache_coordination(
    invocation: tuple[str, ...], root: Path, timeout: float = 20.0
) -> None:
    completed = subprocess.run(
        [*invocation, "--cache-coordination-capability"],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
        timeout=timeout,
    )
    if completed.returncode or not _has_cache_coordination(completed.stdout):
        raise RuntimeError(
            f"cache coordination capability mismatch: returncode={completed.returncode}"
        )


class EqasimSimulator:
    def __init__(
        self,
        settings: EqasimSettings,
        base_config: ResolvedConfig,
        config_bytes: bytes | None = None,
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
        expected_workdir = self.settings.workdir
        if workdir.resolve() != expected_workdir.resolve():
            raise ValueError(f"workdir must be {expected_workdir}")
        workdir.mkdir(parents=True, exist_ok=True)
        resolved_workdir = workdir.resolve(strict=True)
        if not resolved_workdir.is_relative_to(self.settings.scratch):
            raise ValueError("workdir escapes scratch")
        attempt = workdir / f"attempt-{uuid.uuid4().hex}"
        output = attempt / "output"
        normalized = attempt / "normalized"
        output.mkdir(parents=True)
        snapshot = attempt / "config.yml"
        snapshot.write_bytes(self.config_bytes)
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
        try:
            capability = self._run_owned(
                [*self.settings.invocation, "--cache-coordination-capability"],
                cwd=self.settings.eqasim_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            message = f"eqasim process launch failed during cache coordination preflight: {error}"
            log.write_text(message + "\n", encoding="utf-8")
            raise SimulationError(
                message,
                log,
                attempt,
            ) from error
        if capability.returncode or not _has_cache_coordination(capability.stdout):
            log.write_text(
                f"cache coordination preflight failed: returncode={capability.returncode}\n"
                f"stdout={capability.stdout}\nstderr={capability.stderr}\n",
                encoding="utf-8",
            )
            raise SimulationError(
                "synpp does not provide required cache coordination capability "
                f"{CACHE_COORDINATION_CAPABILITY!r}; install the pinned corrected synpp commit",
                log,
                attempt,
            )
        command = [
            *self.settings.invocation,
            str(snapshot),
            "--working-directory",
            str(workdir),
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
            validate_transport(key, nested[key])
            command.extend((f"--{key}", _scalar_argument(key, nested[key])))
        with log.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps({"command": command}, ensure_ascii=False) + "\n")
            stream.flush()
            try:
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
        return Manifest(artifacts, attempt)

    def _run_owned(self, command: list[str], **options) -> subprocess.CompletedProcess:  # type: ignore[no-untyped-def]
        options.pop("check", None)
        timeout = options.pop("timeout", None)
        with self._process_lock:
            if self._interrupted.is_set():
                raise KeyboardInterrupt
            process = subprocess.Popen(command, start_new_session=True, **options)
            self._processes.add(process)
            self._groups.add(process.pid)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            returncode = process.returncode
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


def validate_transport(name: str, value: object) -> None:
    if not OPTION.fullmatch(name) or name in DEDICATED:
        raise ValueError(f"invalid or dedicated synpp option: {name!r}")
    _scalar_argument(name, value)


def _scalar_argument(key: str, value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(
            f"override {key!r} is not faithfully representable by synpp 1.6.1"
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"override {key!r} must be finite")
    argument = str(value)
    if "\0" in argument:
        raise ValueError(f"override {key!r} contains a NUL character")
    return argument


def _same_value(left: object, right: object) -> bool:
    return type(left) is type(right) and left == right
