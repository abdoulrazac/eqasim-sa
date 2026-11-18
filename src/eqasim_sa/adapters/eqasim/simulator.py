"""Execute an installed eqasim pipeline behind the simulator port."""

import copy
import json
import math
import re
import subprocess
import threading
import uuid
from collections.abc import Mapping
from pathlib import Path

import yaml

from eqasim_sa.adapters.eqasim.artifacts import (
    DEFAULT_OUTPUT_PREFIX,
    collect_outputs,
)
from eqasim_sa.adapters.eqasim.config import EqasimSettings
from eqasim_sa.adapters.execution_seal import ExecutionSeal
from eqasim_sa.core.indicators import OutputDeclaration, validate_output_name
from eqasim_sa.core.software_identity import SoftwareSourceGuard
from eqasim_sa.ports.simulator import (
    Manifest,
    ResolvedConfig,
    SimulationError,
)
from eqasim_sa.utils.process_tree import ProcessTree

PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
OPTION = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEDICATED = {"output_path", "output_prefix", "random_seed", "run", "working_directory"}
TERMINATION_GRACE_SECONDS = 2.0


class EqasimSimulator:
    """Run synpp in an isolated workspace and return the declared outputs.

    synpp reads the ``run:`` list from the configuration snapshot as written.
    """

    def __init__(
        self,
        settings: EqasimSettings,
        base_config: ResolvedConfig,
        config_bytes: bytes | None = None,
        slot: int = 0,
        structured_factors: dict[str, dict[str, list[str]]] | None = None,
        execution_seal: ExecutionSeal | None = None,
        software_guard: SoftwareSourceGuard | None = None,
        *,
        outputs: Mapping[str, OutputDeclaration],
    ) -> None:
        if not isinstance(outputs, Mapping) or not outputs:
            raise ValueError("eqasim simulator requires at least one declared output")
        for name, declaration in outputs.items():
            validate_output_name(name)
            if not isinstance(declaration, OutputDeclaration):
                raise TypeError(f"output {name!r} must be an OutputDeclaration")
        self.outputs = dict(outputs)
        self.settings = settings
        self.base_config = copy.deepcopy(base_config)
        self.config_bytes = (
            settings.config_path.read_bytes()
            if config_bytes is None
            else bytes(config_bytes)
        )
        self._trees: set[ProcessTree] = set()
        self._process_lock = threading.Lock()
        self._interrupted = threading.Event()
        self.workspace = settings.workspace(slot, self._interrupted.is_set)
        self.structured_factors = copy.deepcopy(structured_factors or {})
        self.execution_seal = execution_seal
        self.software_guard = software_guard

    def interrupt(self) -> None:
        self._interrupted.set()
        with self._process_lock:
            trees = tuple(self._trees)
        for tree in trees:
            tree.terminate(TERMINATION_GRACE_SECONDS)

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
        collected = attempt / "collected"
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
        if not _same_value(config.get("run"), self.base_config.get("run")):
            raise ValueError(
                "resolved run block must equal run: of the eqasim config"
            )
        prefix = nested.get("output_prefix", DEFAULT_OUTPUT_PREFIX)
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
                        f"structured factor {key!r} value is not a declared configuration"
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
            artifacts = collect_outputs(output, collected, prefix, self.outputs)
        except Exception as error:
            with log.open("a", encoding="utf-8") as stream:
                stream.write(f"output collection failed: {error}\n")
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
        tree = ProcessTree()
        with self._process_lock:
            if self._interrupted.is_set():
                raise KeyboardInterrupt
            process = tree.start(command, **options)
            self._trees.add(tree)
        try:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                tree.terminate(0.0)
                raise
            returncode = process.returncode
        finally:
            # The slot is released by the caller, so every descendant must be
            # gone before this returns or raises.
            try:
                tree.terminate(TERMINATION_GRACE_SECONDS)
            finally:
                with self._process_lock:
                    self._trees.discard(tree)
                tree.close()
        if self._interrupted.is_set():
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def probe_environment(
    invocation: tuple[str, ...], root: Path, timeout: float = 20.0
) -> None:
    """Check that an invocation reaches synpp without starting a simulation."""

    missing = root / f".eqasim-sa-probe-{uuid.uuid4().hex}.yml"
    completed = subprocess.run(
        [*invocation, str(missing)],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Only scanned for a marker: never fail on a console-encoded byte.
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        timeout=timeout,
    )
    output = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode == 0 or "Config file does not exist:" not in output:
        raise RuntimeError(
            f"synpp environment probe failed: returncode={completed.returncode}"
        )


def validate_transport(name: str, value: object, label: str | None = None) -> None:
    """Validate a scalar value that can cross the synpp CLI boundary.

    ``label`` names the value in errors as the user declared it (for example
    ``factor 'a'`` or ``machine_options 'processes'``).
    """

    if not OPTION.fullmatch(name) or name in DEDICATED:
        raise ValueError(f"invalid or dedicated synpp option: {name!r}")
    _scalar_argument(name, value, label)


def _scalar_argument(key: str, value: object, label: str | None = None) -> str:
    subject = label or f"synpp option {key!r}"
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(f"{subject} is not faithfully representable by synpp 1.6.2")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{subject} must be finite")
    argument = str(value)
    if "\0" in argument:
        raise ValueError(f"{subject} contains a NUL character")
    return argument


def _same_value(left: object, right: object) -> bool:
    return type(left) is type(right) and left == right
