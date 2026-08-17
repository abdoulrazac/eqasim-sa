import copy
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

from eqasim_sa.core.ledger import (
    RunRecord,
    completion_ids,
    copy_file_atomic,
    fsync_directory,
    is_complete,
    validate_parameters,
    write_parquet_atomic,
    write_run,
)
from eqasim_sa.ports.backend import Backend
from eqasim_sa.ports.simulator import (
    JsonValue,
    Manifest,
    ResolvedConfig,
    SimulationError,
    Simulator,
)

CANONICALIZATION_VERSION = 1
EXCLUDED_PATHS = (
    "config.data_path",
    "config.git_binary",
    "config.java_binary",
    "config.java_memory",
    "config.maven_binary",
    "config.output_path",
    "config.processes",
    "working_directory",
)
DOMAIN = b"eqasim-sa:run:v1\0"
ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class RunSpec:
    campaign: str
    resolved_config: ResolvedConfig
    seed: int
    expected_context_hash: str
    parameters: dict[str, None | bool | int | float | str]


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: str
    error: str | None = None


def _validate_json(value: object) -> None:
    if (
        value is None
        or type(value) in (bool, int, str)
        or type(value) is float
        and math.isfinite(value)
    ):
        return
    if type(value) in (list, dict):
        if type(value) is dict and not all(type(key) is str for key in value):
            raise TypeError("JSON object keys must be strings")
        for item in value if type(value) is list else value.values():
            _validate_json(item)
        return
    if type(value) is float:
        raise ValueError("JSON numbers must be finite")
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def canonical_config(config: ResolvedConfig, seed: int) -> ResolvedConfig:
    if type(seed) is not int or not -(2**63) <= seed < 2**63:
        raise ValueError("seed must be a signed int64")
    _validate_json(config)
    result = copy.deepcopy(config)
    result.pop("working_directory", None)
    nested = result.get("config")
    if nested is not None and type(nested) is not dict:
        raise TypeError("config block must be an object")
    if type(nested) is dict:
        for path in EXCLUDED_PATHS:
            nested.pop(path.removeprefix("config."), None)
        declared_seed = nested.pop("random_seed", seed)
        if isinstance(declared_seed, bool) or declared_seed != seed:
            raise ValueError("config.random_seed conflicts with explicit seed")
    return result


def compute_run_id(config: ResolvedConfig, seed: int) -> str:
    envelope: dict[str, JsonValue] = {
        "canonicalizationVersion": CANONICALIZATION_VERSION,
        "excludedPaths": list(EXCLUDED_PATHS),
        "config": canonical_config(config, seed),
        "seed": seed,
    }
    encoded = json.dumps(
        envelope,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(DOMAIN + encoded).hexdigest()[:32]


def validate_manifest(
    manifest: Manifest, workdir: Path
) -> tuple[tuple[str, Path], ...]:
    attempt, work = (
        manifest.attempt_directory.resolve(strict=True),
        workdir.resolve(strict=True),
    )
    if attempt == work or not attempt.is_relative_to(work):
        raise ValueError("attempt directory escapes workdir")
    if not manifest.artifacts:
        raise ValueError("manifest must contain at least one artifact")
    validated: dict[str, Path] = {}
    for artifact in manifest.artifacts:
        if not ARTIFACT_NAME.fullmatch(artifact.name) or not artifact.name.endswith(
            ".parquet"
        ):
            raise ValueError(f"invalid artifact name: {artifact.name!r}")
        if artifact.name in validated:
            raise ValueError(f"duplicate artifact name: {artifact.name!r}")
        if ".." in artifact.path.parts:
            raise ValueError(f"artifact path contains parent segment: {artifact.path}")
        source = artifact.path.resolve(strict=True)
        if not source.is_file() or not source.is_relative_to(attempt):
            raise ValueError(f"artifact escapes attempt directory: {artifact.path}")
        validated[artifact.name] = source
    return tuple(sorted(validated.items()))


def execute_run(
    spec: RunSpec,
    simulator: Simulator,
    campaign_root: Path,
    workdir: Path,
    interrupted=None,
) -> RunResult:  # type: ignore[no-untyped-def]
    run_id = ""
    publishing = False
    try:
        run_id = compute_run_id(spec.resolved_config, spec.seed)
        if is_complete(campaign_root, spec):
            _remove_failure_log(campaign_root, run_id)
            return RunResult(run_id, "skipped")
        start = time.monotonic()
        manifest = simulator.simulate(spec.resolved_config, spec.seed, workdir)
        publishing = True
        for name, source in validate_manifest(manifest, workdir):
            if interrupted is not None and interrupted():
                raise KeyboardInterrupt
            table = pq.read_table(source)
            write_parquet_atomic(table, campaign_root / "archives" / run_id / name)
        if interrupted is not None and interrupted():
            raise KeyboardInterrupt
        _remove_failure_log(campaign_root, run_id)
        write_run(
            campaign_root,
            RunRecord(
                run_id,
                spec.campaign,
                spec.seed,
                spec.expected_context_hash,
                os.uname().nodename,
                time.monotonic() - start,
                spec.parameters,
            ),
        )
        return RunResult(run_id, "completed")
    except SimulationError as error:
        try:
            log = error.log_path.resolve(strict=True)
            attempt, work = (
                error.attempt_directory.resolve(strict=True),
                workdir.resolve(),
            )
            if attempt == work or not attempt.is_relative_to(work):
                raise ValueError(
                    f"simulation attempt escapes workdir: {error.attempt_directory}"
                )
            if not log.is_file() or not log.is_relative_to(attempt):
                raise ValueError(
                    f"simulation log escapes attempt directory: {error.log_path}"
                )
            copy_file_atomic(log, campaign_root / "logs" / f"{run_id}.log")
        except Exception as log_error:
            return RunResult(
                run_id, "failed", f"{error}; log publication failed: {log_error}"
            )
        return RunResult(run_id, "failed", str(error))
    except Exception as error:
        if not publishing:
            return RunResult(run_id, "failed", str(error))
        try:
            from eqasim_sa.core.ledger import _write_atomic

            destination = campaign_root / "logs" / f"{run_id}.log"
            _write_atomic(destination, lambda path: path.write_text(str(error) + "\n"))
        except Exception as log_error:
            return RunResult(
                run_id, "failed", f"{error}; log publication failed: {log_error}"
            )
        return RunResult(run_id, "failed", str(error))


def pending_run_specs(
    campaign_root: Path, specs: tuple[RunSpec, ...]
) -> tuple[RunSpec, ...]:
    completed = completion_ids(campaign_root, specs)
    return tuple(
        spec
        for spec in specs
        if compute_run_id(spec.resolved_config, spec.seed) not in completed
    )


def _remove_failure_log(campaign_root: Path, run_id: str) -> None:
    log = campaign_root / "logs" / f"{run_id}.log"
    if not log.exists():
        return
    log.unlink()
    if os.name == "posix":
        fsync_directory(log.parent)


def run_one(
    spec: RunSpec,
    simulator: Simulator,
    backend: Backend,
    campaign_root: Path,
    workdir: Path,
    current_context_hash: str,
) -> RunResult:
    try:
        run_id = compute_run_id(spec.resolved_config, spec.seed)
        validate_parameters(spec.parameters)
    except Exception as error:
        return RunResult("", "failed", str(error))
    if current_context_hash != spec.expected_context_hash:
        return RunResult(
            run_id,
            "failed",
            f"contextHash mismatch: expected "
            f"{spec.expected_context_hash!r}, current {current_context_hash!r}",
        )
    try:
        results = tuple(
            backend.map(
                lambda item: execute_run(item, simulator, campaign_root, workdir),
                (spec,),
            )
        )
    except Exception as error:
        return RunResult(run_id, "failed", str(error))
    if len(results) != 1:
        raise RuntimeError(f"backend returned {len(results)} results for one run")
    return results[0]


def summarize_runs(run_ids: list[str], results: list[RunResult]) -> dict:
    if len(results) != len(run_ids) or [result.run_id for result in results] != run_ids:
        raise RuntimeError(
            "backend result cardinality or identities do not match submitted runs"
        )
    statuses = ("completed", "skipped", "failed")
    if any(result.status not in statuses for result in results):
        raise RuntimeError("backend returned an unknown run status")
    counts = {
        status: sum(result.status == status for result in results)
        for status in statuses
    }
    failures = [
        {"runId": result.run_id, "error": result.error or "worker failed"}
        for result in results
        if result.status == "failed"
    ]
    return {
        "status": "failed" if failures else "completed",
        "total": len(results),
        **counts,
        "failures": failures,
        "error": None,
    }
