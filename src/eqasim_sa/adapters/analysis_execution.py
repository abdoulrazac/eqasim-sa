"""Run analysis scripts on archived run outputs, with a fingerprinted cache.

Each script runs in a separate process started with eqasim's Python
interpreter (``cwd`` = ``model.root``) through the self-contained
:mod:`analysis_runner`, copied into a temporary directory together with the
script and copies of the archived inputs: the archives themselves are never
exposed to the script.

Results are kept in ``campaign_root/analyses-cache/<analysis>/<fingerprint>.csv``
next to ``<fingerprint>.sha256``, the SHA-256 of the CSV, written before it.
Both are published atomically from temporaries named
``.<fingerprint>.<csv|sha256>.<token>.tmp``; script error logs go to
``analyses-cache/logs/<analysis>-<runId>.log`` from ``.<log>.<token>.tmp``.
These names are those ``project_init`` accepts in a campaign.

Callers must hold the campaign's ``analysis.lock`` lease around every call
that touches the cache (:class:`AnalysisCache`, :func:`run_analysis`).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from eqasim_sa.adapters.execution_seal import resolve_invocation
from eqasim_sa.core.analyses import (
    ANALYSES_CACHE_DIRECTORY,
    ANALYSES_LOG_DIRECTORY,
    AnalysisDeclaration,
)
from eqasim_sa.core.indicators import read_csv_table
from eqasim_sa.utils import platform
from eqasim_sa.utils.filesystem import (
    absolute,
    capture_stable_file,
    secure_directory,
    stable_file,
    stable_file_bytes,
    write_all,
)
from eqasim_sa.utils.text_identity import lf_sha256

__all__ = (
    "AnalysisCache",
    "AnalysisResult",
    "AnalysisScriptError",
    "analysis_interpreter",
    "environment_digest",
    "environment_document",
    "environment_fingerprint",
    "result_fingerprint",
    "run_analysis",
)

RESULT_DOMAIN = b"eqasim-sa:analysis-result:v1\0"
RUNNER = Path(__file__).with_name("analysis_runner.py")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_NAME = re.compile(r"[A-Za-z0-9_-]+")
_VERSIONS = ("python", "pandas", "numpy", "pyarrow", "geopandas", "shapely", "pyogrio")
_CREATE = (
    os.O_CREAT
    | os.O_EXCL
    | os.O_WRONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_BINARY", 0)
)


class AnalysisScriptError(RuntimeError):
    """An analysis script failed; the full error output is in the log."""


@dataclass(frozen=True)
class AnalysisResult:
    """One analysis result and the identities it was computed from.

    ``script_sha256`` is the LF-normalized SHA-256 of the script copy taken
    by this call (executed on a miss), never of the user's file read later.
    ``warnings`` lists the altered cache entries this call removed.
    """

    path: Path
    fingerprint: str
    script_sha256: str
    inputs_sha256: dict[str, str]
    environment: str
    warnings: tuple[str, ...]


def _canonical(document: object) -> bytes:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: object, name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def analysis_interpreter(
    python: Sequence[str],
    model_root: Path,
    environment: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Resolve the analysis interpreter command like the eqasim invocation.

    :func:`resolve_invocation` resolves symbolic links, which would turn a
    virtual environment's ``python`` link into its base interpreter, outside
    the environment.  On POSIX the lexical path of the executable it found is
    therefore kept; on Windows a virtual environment's ``python.exe`` is a
    regular file and the resolved path is used as is.
    """

    environment = os.environ if environment is None else environment
    root = absolute(model_root)
    resolved = resolve_invocation(python, root, environment)
    if platform.IS_WINDOWS:
        return resolved
    command = python[0]
    target = Path(resolved[0])
    if "/" in command:
        candidates = [Path(command) if Path(command).is_absolute() else root / command]
    else:
        candidates = [
            (Path(part) if Path(part).is_absolute() else root / part) / command
            for part in environment.get("PATH", os.defpath).split(os.pathsep)
        ]
    for candidate in candidates:
        try:
            if candidate.is_file() and candidate.resolve(strict=True) == target:
                return (str(absolute(candidate)), *resolved[1:])
        except OSError:
            continue
    return resolved


def _copy_runner(directory: Path) -> Path:
    destination = directory / "runner" / RUNNER.name
    capture_stable_file(RUNNER, destination)
    return destination


def _decode(content: bytes) -> str:
    return content.decode("utf-8", errors="replace")


def _last_line(text: str, returncode: int) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else f"exit status {returncode}"


def environment_fingerprint(
    python: Sequence[str], model_root: Path, lockfile: Path | None
) -> str:
    """SHA-256 of :func:`environment_document`."""

    return environment_digest(environment_document(python, model_root, lockfile))


def environment_digest(document: Mapping[str, object]) -> str:
    """SHA-256 of an environment document."""

    return hashlib.sha256(_canonical(document)).hexdigest()


def environment_document(
    python: Sequence[str], model_root: Path, lockfile: Path | None
) -> dict[str, str | None]:
    """The interpreter's reported versions and eqasim's lockfile identity.

    The versions of Python, pandas, numpy, pyarrow, geopandas, shapely and
    pyogrio (``null`` when a library is absent) are reported by a probe run
    with the interpreter; the lockfile (normally ``model.root/uv.lock``) is
    hashed with LF line endings, or ``null`` when it does not exist.
    """

    command = analysis_interpreter(python, model_root)
    with tempfile.TemporaryDirectory(
        prefix="eqasim-sa-probe-", ignore_cleanup_errors=True
    ) as temporary:
        directory = Path(temporary)
        runner = _copy_runner(directory)
        report = directory / "versions.json"
        completed = subprocess.run(
            [*command, str(runner), "--probe", str(report)],
            cwd=absolute(model_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0 or not report.is_file():
            message = _last_line(_decode(completed.stderr), completed.returncode)
            raise RuntimeError(f"analysis interpreter probe failed: {message}")
        versions = json.loads(report.read_text(encoding="utf-8"))
    if not isinstance(versions, dict) or set(versions) != set(_VERSIONS):
        raise RuntimeError("analysis interpreter probe returned an invalid report")
    if type(versions["python"]) is not str or not all(
        versions[name] is None or type(versions[name]) is str
        for name in _VERSIONS[1:]
    ):
        raise RuntimeError("analysis interpreter probe returned an invalid report")
    lockfile_sha256 = (
        lf_sha256(lockfile) if lockfile is not None and lockfile.is_file() else None
    )
    return {**versions, "lockfileSha256": lockfile_sha256}


def result_fingerprint(
    declaration: AnalysisDeclaration,
    inputs_sha256: Mapping[str, str],
    parameters: Mapping[str, object],
    environment: str,
) -> str:
    """Identity of one analysis result.

    ``declaration.script`` is hashed with LF line endings: :func:`run_analysis`
    passes the copy it actually executes.
    """

    if set(inputs_sha256) != set(declaration.inputs):
        raise ValueError(
            f"analysis {declaration.name!r} needs the SHA-256 of exactly its inputs"
        )
    if not isinstance(parameters, Mapping):
        raise ValueError("analysis parameters must be a mapping")
    document = {
        "analysis": declaration.name,
        "script": lf_sha256(declaration.script),
        "inputs": {
            name: _sha256(value, f"input {name!r}")
            for name, value in inputs_sha256.items()
        },
        "parameters": dict(parameters),
        "environment": _sha256(environment, "environment fingerprint"),
    }
    return hashlib.sha256(RESULT_DOMAIN + _canonical(document)).hexdigest()


def _publish(directory: Path, name: str, content: bytes) -> Path:
    """Atomically replace ``directory/name`` through ``.<name>.<token>.tmp``."""

    destination = directory / name
    temporary = directory / f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, _CREATE, 0o600)
    try:
        try:
            write_all(descriptor, content)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        platform.replace_with_retry(temporary, destination)
    except BaseException:
        try:
            platform.remove_file(temporary)
        except OSError:
            pass
        raise
    platform.fsync_directory(directory)
    return destination


def _remove(path: Path) -> None:
    try:
        platform.remove_file(path)
    except FileNotFoundError:
        pass


class AnalysisCache:
    """Integrity-checked analysis results of one campaign.

    Altered entries found by :meth:`get` are removed and described in the
    ``warnings`` list it is given, so that callers recompute and report them.
    """

    def __init__(self, campaign_root: Path) -> None:
        self.root = absolute(campaign_root) / ANALYSES_CACHE_DIRECTORY

    def _directory(self, name: str, *, create: bool) -> Path:
        if _NAME.fullmatch(name) is None or name == ANALYSES_LOG_DIRECTORY:
            raise ValueError(f"invalid analysis name: {name!r}")
        if create:
            secure_directory(self.root, create=True)
            secure_directory(self.root / name, create=True)
        return self.root / name

    def get(
        self, name: str, fingerprint: str, warnings: list[str] | None = None
    ) -> Path | None:
        """Return the verified cached CSV, or ``None`` when it must be computed."""

        _sha256(fingerprint, "result fingerprint")
        directory = self._directory(name, create=False)
        if not os.path.lexists(directory):
            return None
        # Same rule as put: a linked cache directory is never read.
        secure_directory(self.root)
        secure_directory(directory)
        result = directory / f"{fingerprint}.csv"
        digest = directory / f"{fingerprint}.sha256"
        if not os.path.lexists(result):
            return None
        try:
            expected = digest.read_bytes().decode("ascii").strip()
            if stable_file(result).sha256 == expected:
                return result
        except (OSError, ValueError, UnicodeError):
            pass
        _remove(result)
        _remove(digest)
        platform.fsync_directory(directory)
        if warnings is not None:
            warnings.append(
                f"analysis cache entry {name}/{result.name} was altered; "
                "it was removed and recomputed"
            )
        return None

    def put(self, name: str, fingerprint: str, csv_path: Path) -> Path:
        """Publish a computed CSV, its SHA-256 first, and return its cache path.

        Concurrent writers of one fingerprint are excluded: callers hold
        ``analysis.lock`` and every result fingerprint includes the runId.
        """

        _sha256(fingerprint, "result fingerprint")
        directory = self._directory(name, create=True)
        content, snapshot = stable_file_bytes(csv_path)
        _publish(directory, f"{fingerprint}.sha256", f"{snapshot.sha256}\n".encode())
        return _publish(directory, f"{fingerprint}.csv", content)


def _write_log(campaign_root: Path, name: str, run_id: str, text: str) -> Path:
    if _NAME.fullmatch(run_id) is None:
        raise ValueError(f"invalid runId: {run_id!r}")
    root = absolute(campaign_root) / ANALYSES_CACHE_DIRECTORY
    secure_directory(root, create=True)
    logs = root / ANALYSES_LOG_DIRECTORY
    secure_directory(logs, create=True)
    return _publish(logs, f"{name}-{run_id}.log", text.encode("utf-8"))


def _remove_tree(path: Path) -> None:
    def writable(function, target, _error) -> None:  # type: ignore[no-untyped-def]
        os.chmod(target, 0o700)
        function(target)

    shutil.rmtree(path, onexc=writable)


def _remove_tree_best_effort(path: Path) -> None:
    """Remove a temporary tree; a result must survive a locked leftover.

    On Windows an indexer or antivirus can hold files for a moment: retry
    briefly, then leave the (system-temporary) leftovers.
    """

    for attempt in range(5):
        try:
            _remove_tree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt < 4:
                time.sleep(0.1 * (attempt + 1))


def run_analysis(
    declaration: AnalysisDeclaration,
    archived: Mapping[str, Path],
    parameters: Mapping[str, object],
    python: Sequence[str],
    model_root: Path,
    campaign_root: Path,
    environment: str,
    *,
    inputs_sha256: Mapping[str, str] | None = None,
    cache: AnalysisCache | None = None,
) -> AnalysisResult:
    """Return the cached result of one analysis for one run, computing it if needed.

    ``archived`` maps each declared input to its archived file;
    ``inputs_sha256`` gives their known SHA-256 (computed when omitted), and
    every copy is verified against it.  ``parameters`` is
    ``{"runId", "seed", "factors"}``; ``environment`` comes from
    :func:`environment_fingerprint`.  A script error raises
    :class:`AnalysisScriptError` and writes the full error output to
    ``analyses-cache/logs/<analysis>-<runId>.log`` (a failure to write the log
    is attached as the error's context).  The caller holds the
    ``analysis.lock`` lease.
    """

    if set(archived) != set(declaration.inputs):
        raise ValueError(
            f"analysis {declaration.name!r} needs exactly its declared inputs"
        )
    run_id = parameters.get("runId")
    if type(run_id) is not str:
        raise ValueError("analysis parameters need a runId")
    expected = (
        {name: stable_file(Path(path)).sha256 for name, path in archived.items()}
        if inputs_sha256 is None
        else dict(inputs_sha256)
    )
    cache = AnalysisCache(campaign_root) if cache is None else cache
    command = analysis_interpreter(python, model_root)
    directory = Path(tempfile.mkdtemp(prefix="eqasim-sa-analysis-"))
    try:
        script = directory / "script" / declaration.script.name
        capture_stable_file(declaration.script, script)
        script_sha256 = lf_sha256(script)
        fingerprint = result_fingerprint(
            replace(declaration, script=script), expected, parameters, environment
        )
        warnings: list[str] = []

        def result(path: Path) -> AnalysisResult:
            return AnalysisResult(
                path,
                fingerprint,
                script_sha256,
                dict(expected),
                environment,
                tuple(warnings),
            )

        cached = cache.get(declaration.name, fingerprint, warnings)
        if cached is not None:
            return result(cached)
        files = {}
        for name in declaration.inputs:
            source = Path(archived[name])
            copy = directory / "inputs" / name / source.name
            capture_stable_file(source, copy, expected_sha256=expected[name])
            files[name] = str(copy)
        runner = _copy_runner(directory)
        output = directory / "output" / "result.csv"
        output.parent.mkdir()
        request = directory / "request.json"
        request.write_text(
            json.dumps(
                {
                    "script": str(script),
                    "files": files,
                    "parameters": dict(parameters),
                    "output": str(output),
                },
                ensure_ascii=False,
                allow_nan=False,
            ),
            encoding="utf-8",
        )
        completed = subprocess.run(
            [*command, str(runner), str(request)],
            cwd=absolute(model_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            check=False,
        )
        errors = _decode(completed.stderr)
        if completed.returncode != 0 or not output.is_file():
            failure = AnalysisScriptError(
                f"analysis '{declaration.name}' failed for run {run_id}: "
                f"{_last_line(errors, completed.returncode)}"
            )
            try:
                _write_log(campaign_root, declaration.name, run_id, errors)
            except Exception as log_error:
                raise failure from log_error
            raise failure
        read_csv_table(output, ",", declaration.name)
        return result(cache.put(declaration.name, fingerprint, output))
    finally:
        _remove_tree_best_effort(directory)
