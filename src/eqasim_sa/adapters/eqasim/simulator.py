"""Execute an installed eqasim pipeline behind the simulator port."""

import copy
import json
import math
import re
import subprocess
import threading
from collections.abc import Callable, Mapping
from pathlib import Path

import yaml

from eqasim_sa.adapters.eqasim.artifacts import (
    DEFAULT_OUTPUT_PREFIX,
    collect_outputs,
)
from eqasim_sa.adapters.eqasim.config import EqasimSettings
from eqasim_sa.adapters.execution_seal import ExecutionSeal, child_process_environment
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
# How synpp's command line receives a configuration override. synpp 1.7.0
# replaced ``--KEY VALUE`` with ``--config KEY VALUE``; neither version accepts
# the other's form. Both convert the string to the type of the value already in
# the YAML ``config:`` block, so the two forms set the same configuration.
SYNPP_CONFIG_FLAG = "config"
SYNPP_LEGACY_FLAG = "legacy"
SYNPP_DIALECTS = frozenset({SYNPP_CONFIG_FLAG, SYNPP_LEGACY_FLAG})
SYNPP_CONFIG_FLAG_SINCE = (1, 7)
_VERSION = re.compile(r"^(\d+)\.(\d+)")


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
        synpp_dialect: str | None = None,
    ) -> None:
        if synpp_dialect is not None and synpp_dialect not in SYNPP_DIALECTS:
            raise ValueError(f"unknown synpp dialect: {synpp_dialect!r}")
        self._synpp_dialect = synpp_dialect
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

    def _detect_synpp_dialect(self) -> str:
        """Override form of the model's synpp, asked once per simulator.

        The question runs like a simulation: owned by this simulator so that
        an interruption stops it, without a time limit since ``uv run`` may
        first have to synchronize the model environment.
        """

        if self._synpp_dialect is None:
            self._synpp_dialect = detect_synpp_dialect(
                self.settings.invocation,
                self.settings.eqasim_root,
                runner=self._run_owned,
            )
        return self._synpp_dialect

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
        try:
            dialect = self._detect_synpp_dialect()
        except RuntimeError as error:
            log.write_text(f"{error}\n", encoding="utf-8")
            raise SimulationError(str(error), log, attempt) from error
        command = [
            *self.settings.invocation,
            str(snapshot),
            "--working-directory",
            str(self.workspace.cache),
        ]
        command.extend(synpp_override("random_seed", str(seed), dialect))
        command.extend(synpp_override("output_path", str(output), dialect))
        command.extend(synpp_override("output_prefix", prefix, dialect))
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
            command.extend(
                synpp_override(key, _scalar_argument(key, nested[key]), dialect)
            )
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
        options["env"] = (
            self.execution_seal.environment
            if self.execution_seal is not None
            else child_process_environment()
        )
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


PROBE_SKIPPED = "synpp probe skipped: invocation does not end with -m synpp"


def probe_environment(
    invocation: tuple[str, ...], root: Path, timeout: float = 20.0
) -> str | None:
    """Check that the invocation's interpreter can import synpp.

    Only an invocation ending with ``-m synpp`` is probed: those two elements
    become ``-c "import synpp"`` and the exit code must be 0. Nothing printed
    by synpp is read. Any other invocation is not probed and the returned text
    says so; ``None`` means the probe passed.
    """

    if tuple(invocation[-2:]) != ("-m", "synpp"):
        return PROBE_SKIPPED
    command = [*invocation[:-2], "-c", "import synpp"]
    label = " ".join(invocation)
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            env=child_process_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            timeout=timeout,
        )
    except OSError as error:
        raise RuntimeError(
            f"synpp environment probe failed: cannot start invocation {label!r}: "
            f"{error.__class__.__name__}"
        ) from error
    if completed.returncode != 0:
        raise RuntimeError(
            f"synpp environment probe failed: invocation {label!r} cannot import "
            f"synpp (returncode={completed.returncode})"
        )
    return None


def synpp_override(key: str, value: str, dialect: str) -> tuple[str, ...]:
    """Command-line arguments that set ``config.<key>`` in the given dialect."""

    if dialect == SYNPP_CONFIG_FLAG:
        return ("--config", key, value)
    if dialect == SYNPP_LEGACY_FLAG:
        return (f"--{key}", value)
    raise ValueError(f"unknown synpp dialect: {dialect!r}")


def synpp_dialect_for_version(version: str) -> str:
    """Map an installed synpp version to the override form it parses."""

    match = _VERSION.match(version.strip())
    if match is None:
        raise RuntimeError(f"cannot read the synpp version {version.strip()!r}")
    release = (int(match.group(1)), int(match.group(2)))
    return (
        SYNPP_CONFIG_FLAG if release >= SYNPP_CONFIG_FLAG_SINCE else SYNPP_LEGACY_FLAG
    )


_VERSION_SCRIPT = "import importlib.metadata as m; print(m.version('synpp'))"
_STDERR_LINES = 10


def detect_synpp_dialect(
    invocation: tuple[str, ...],
    root: Path,
    environment: Mapping[str, str] | None = None,
    timeout: float | None = None,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> str:
    """Ask the model's interpreter which synpp it runs.

    Only an invocation ending with ``-m synpp`` can be asked: those two
    elements become ``-c`` and a script printing the installed version. Any
    other invocation is assumed to run synpp 1.7.0 or later. ``runner``
    replaces :func:`subprocess.run` and then chooses the environment itself.
    """

    if tuple(invocation[-2:]) != ("-m", "synpp"):
        return SYNPP_CONFIG_FLAG
    command = [*invocation[:-2], "-c", _VERSION_SCRIPT]
    label = " ".join(invocation)
    options = {
        "cwd": root,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    try:
        if runner is None:
            completed = subprocess.run(
                command,
                env=(
                    dict(environment)
                    if environment is not None
                    else child_process_environment()
                ),
                timeout=timeout,
                check=False,
                **options,
            )
        else:
            completed = runner(command, **options)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(
            f"cannot read the synpp version of invocation {label!r}: "
            f"{error.__class__.__name__}: {error}"
        ) from error
    stderr = (completed.stderr or "").strip().splitlines()[-_STDERR_LINES:]
    detail = ("\n" + "\n".join(stderr)) if stderr else ""
    if completed.returncode != 0:
        raise RuntimeError(
            f"cannot read the synpp version of invocation {label!r} "
            f"(returncode={completed.returncode}){detail}"
        )
    lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"invocation {label!r} printed no synpp version{detail}")
    return synpp_dialect_for_version(lines[-1])


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
        raise TypeError(
            f"{subject} is not faithfully representable on the synpp command line"
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{subject} must be finite")
    argument = str(value)
    if "\0" in argument:
        raise ValueError(f"{subject} contains a NUL character")
    return argument


def _same_value(left: object, right: object) -> bool:
    return type(left) is type(right) and left == right
