"""Seal executable, model, environment, and data identities for a run."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType

from eqasim_sa.adapters.data_manifest import (
    DataTreeState,
    load_data_manifest,
    verify_data_manifest,
    verify_data_tree_state,
)
from eqasim_sa.utils import platform
from eqasim_sa.utils.filesystem import (
    FileState,
    file_state,
    stable_file,
    stable_file_bytes,
)
from eqasim_sa.core.software_identity import (
    SoftwareIdentity,
    verify_software_source_states,
)
from eqasim_sa.ports.simulator import (
    CHILD_ENVIRONMENT_DOMAIN,
    ExecutionEvidence,
)

VOLATILE_ENVIRONMENT_NAMES = frozenset(
    {
        "HOSTNAME",
        "OLDPWD",
        "PWD",
        "SHLVL",
        "TEMP",
        "TMP",
        "TMPDIR",
        "_",
    }
)
VOLATILE_ENVIRONMENT_PREFIXES = ("SLURM_", "SUBMITIT_")


def normalized_child_environment(
    environment: Mapping[str, str],
) -> dict[str, str]:
    """Remove scheduler/session identity while retaining declared model inputs."""
    result = {
        name: value
        for name, value in environment.items()
        if name not in VOLATILE_ENVIRONMENT_NAMES
        and not name.startswith(VOLATILE_ENVIRONMENT_PREFIXES)
    }
    result.setdefault("PATH", os.defpath)
    return result


def _regular_state(path: Path) -> FileState:
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"sealed file is no longer regular: {path}")
    return file_state(info)


def _path_executable(command: str, root: Path, environment: Mapping[str, str]) -> Path:
    if platform.IS_WINDOWS:
        return _windows_path_executable(command, root, environment)
    if "/" in command:
        candidate = Path(command)
        if not candidate.is_absolute():
            candidate = root / candidate
        candidates = (candidate,)
    else:
        search = environment.get("PATH", os.defpath)
        candidates = tuple(
            (Path(part) if Path(part).is_absolute() else root / part) / command
            for part in search.split(os.pathsep)
        )
    for candidate in candidates:
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                resolved = candidate.resolve(strict=True)
                if not resolved.is_file() or not os.access(resolved, os.X_OK):
                    continue
                return resolved
        except OSError:
            continue
    raise FileNotFoundError(f"execution executable is not resolvable: {command}")


def _windows_path_executable(
    command: str, root: Path, environment: Mapping[str, str]
) -> Path:
    """Resolve like ``cmd.exe``: ``PATHEXT`` suffixes, relative entries under root."""
    if os.path.dirname(command):
        candidate = Path(command)
        if not candidate.is_absolute():
            candidate = root / candidate
        found = shutil.which(str(candidate))
    else:
        search = os.pathsep.join(
            str(Path(part) if Path(part).is_absolute() else root / part)
            for part in environment.get("PATH", os.defpath).split(os.pathsep)
        )
        found = shutil.which(command, path=search)
    if found is None:
        raise FileNotFoundError(f"execution executable is not resolvable: {command}")
    return Path(found).resolve(strict=True)


def resolve_invocation(
    invocation: Sequence[str], root: Path, environment: Mapping[str, str]
) -> tuple[str, ...]:
    """Resolve an invocation's executable to one stable absolute path."""

    values = tuple(invocation)
    if not values or not all(
        type(item) is str and item and "\0" not in item for item in values
    ):
        raise ValueError("invocation must contain non-empty strings without NUL")
    executable = _path_executable(values[0], root, environment)
    return str(executable), *values[1:]


def _git(
    root: Path, environment: Mapping[str, str], *arguments: str
) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
        check=False,
        timeout=5,
    )
    if completed.returncode:
        raise RuntimeError(
            f"Git command failed: {' '.join(arguments)}: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


class ExecutionSeal:
    """Guard all provenance inputs against mutation during execution."""

    def __init__(
        self,
        *,
        context_hash: str,
        model_root: Path,
        model_config: Path,
        model_config_bytes: bytes,
        model_commit: str,
        control_states: Sequence[tuple[Path, FileState]],
        data_manifest: Path,
        data_root: Path,
        invocation: Sequence[str],
        environment: Mapping[str, str],
        software_identity: SoftwareIdentity,
    ) -> None:
        child_environment = normalized_child_environment(environment)
        if not all(
            type(name) is str
            and name
            and "=" not in name
            and "\0" not in name
            and type(value) is str
            and "\0" not in value
            for name, value in child_environment.items()
        ):
            raise ValueError("child environment contains an invalid name or value")
        self.environment = MappingProxyType(child_environment)
        self.model_root = model_root.resolve(strict=True)
        self.model_config = model_config.resolve(strict=True)
        self.model_config_bytes = bytes(model_config_bytes)
        self.model_commit = model_commit
        self.control_states = tuple(control_states)
        self.data_manifest = data_manifest.resolve(strict=True)
        self.invocation = resolve_invocation(invocation, self.model_root, self.environment)
        self.software_identity = software_identity
        executable = Path(self.invocation[0])
        executable_snapshot = stable_file(executable)
        executable_sha256 = executable_snapshot.sha256
        self._executable_state = executable_snapshot.state
        self._executable = executable
        self._data_state: DataTreeState | None = None
        self._failure: str | None = None
        self._lock = threading.Lock()

        self._verify_current(include_data=False)
        manifest = load_data_manifest(self.data_manifest)
        self._data_state = verify_data_manifest(data_root, manifest)
        self._verify_current(include_data=True)

        encoded_environment = json.dumps(
            child_environment,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        environment_sha256 = hashlib.sha256(
            CHILD_ENVIRONMENT_DOMAIN + encoded_environment
        ).hexdigest()
        self.evidence = ExecutionEvidence(
            context_hash=context_hash,
            data_tree_sha256=manifest.tree_sha256,
            environment_sha256=environment_sha256,
            environment_variables=tuple(sorted(child_environment)),
            executable_sha256=executable_sha256,
            invocation=self.invocation,
            model_commit=model_commit,
            model_config_sha256=hashlib.sha256(self.model_config_bytes).hexdigest(),
            schema_version=2,
            eqasim_sa_sha256=software_identity.sha256,
        ).canonical_bytes()

    def _verify_current(self, *, include_data: bool) -> None:
        verify_software_source_states(self.software_identity)
        if _git(
            self.model_root,
            self.environment,
            "rev-parse",
            "--verify",
            "HEAD",
        ) != self.model_commit:
            raise RuntimeError("model HEAD changed after context sealing")
        if _git(
            self.model_root,
            self.environment,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ):
            raise RuntimeError("model Git repository is no longer clean")
        for path, expected in self.control_states:
            try:
                current = _regular_state(path)
            except (OSError, ValueError) as error:
                raise RuntimeError(f"sealed control input changed: {path}") from error
            if current != expected:
                raise RuntimeError(f"sealed control input changed: {path}")
        try:
            config_bytes, config_snapshot = stable_file_bytes(self.model_config)
        except (OSError, ValueError) as error:
            raise RuntimeError("model config changed after context sealing") from error
        expected_config_state = dict(self.control_states).get(self.model_config)
        if (
            config_bytes != self.model_config_bytes
            or config_snapshot.state != expected_config_state
        ):
            raise RuntimeError("model config changed after context sealing")
        try:
            executable_state = _regular_state(self._executable)
        except (OSError, ValueError) as error:
            raise RuntimeError("resolved execution executable changed") from error
        if executable_state != self._executable_state:
            raise RuntimeError("resolved execution executable changed")
        if include_data:
            if self._data_state is None:
                raise RuntimeError("data tree was not sealed")
            verify_data_tree_state(self._data_state)

    def guard(self) -> None:
        with self._lock:
            if self._failure is not None:
                raise RuntimeError(self._failure)
            try:
                self._verify_current(include_data=True)
            except Exception as error:
                self._failure = f"execution provenance guard failed: {error}"
                raise RuntimeError(self._failure) from error


def create_execution_seal(
    meta, environment: Mapping[str, str] | None = None
) -> ExecutionSeal:  # type: ignore[no-untyped-def]
    """Create and fully verify the execution seal for a loaded meta-config."""

    if meta.execution is None:
        raise ValueError("execution section is required for an execution seal")
    data_root = getattr(meta.execution, "data_path", None)
    if data_root is None:
        raise ValueError("effective execution data_path is required")
    return ExecutionSeal(
        context_hash=meta.context_hash,
        model_root=meta.model_root,
        model_config=meta.model_config,
        model_config_bytes=meta.model_bytes,
        model_commit=meta.context_descriptor["eqasimCommit"],
        control_states=meta.control_states,
        data_manifest=meta.data_manifest,
        data_root=data_root,
        invocation=meta.execution.invocation,
        environment=os.environ if environment is None else environment,
        software_identity=meta.software_identity,
    )


seal_execution = create_execution_seal
