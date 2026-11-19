"""Static and smoke checks for campaign readiness."""

import json
import math
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

from eqasim_sa.adapters.campaign_design import _design
from eqasim_sa.adapters.eqasim.artifacts import DEFAULT_OUTPUT_PREFIX
from eqasim_sa.adapters.eqasim.execution import (
    EqasimBatchExecutor,
    _apply_execution_overrides,
)
from eqasim_sa.adapters.eqasim.simulator import probe_environment
from eqasim_sa.adapters.eqasim.workspace import workspace_prefix_length
from eqasim_sa.adapters.estimate import _estimate_state, _remaining
from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.adapters.worker import ordered_results, worker_batches
from eqasim_sa.core.campaign_snapshot import CampaignSnapshot
from eqasim_sa.core.indicators import ArtifactSet, DerivedOutput
from eqasim_sa.core.ledger import archive_file_name
from eqasim_sa.core.plan import load_run_specs, unique_config_seed_pairs
from eqasim_sa.adapters.execution_seal import current_machine_identity
from eqasim_sa.core.run import RunSpec, require_same_machine
from eqasim_sa.core.software_identity import verify_software_source_states
from eqasim_sa.utils.filesystem import path_state, resolve_root
from eqasim_sa.utils import platform

SEALED_SENSITIVITY_METHODS = frozenset(
    {"discrete_oat", "canonical_morris", "sobol", "multiscramble_sobol"}
)

WINDOWS_PATH_LIMIT = 260
# Shortest usable Windows scratch: a drive root such as "C:\\".
SHORTEST_WINDOWS_SCRATCH = len("C:\\")
DARWIN_PATH_LIMIT = 1024
POSIX_PATH_LIMIT = 4096
# Longest path eqasim created below its cache directory in a real Nantes run:
# 231 characters (data.spatial.iris__<32 hex>.cache/CONTOURS-IRIS-PE_.../
# EMPRISE/EMPRISE.shx, from the extracted IGN IRIS archive), plus 9 margin.
EQASIM_RELATIVE_PATH_BUDGET: int = 240
# check --smoke runs below execution.scratch, in
# <scratch>/.smoke-<8 hex>/{c,s}, as campaign "smoke" with its own worker slots.
SMOKE_CAMPAIGN = "smoke"
_SMOKE_DIRECTORY_PREFIX = ".smoke-"
_SMOKE_SCRATCH_PREFIX_LENGTH = len(f"/{_SMOKE_DIRECTORY_PREFIX}12345678/s")


def _relative_path_budget(campaign: str) -> int:
    worker_prefix = max(
        workspace_prefix_length(campaign),
        _SMOKE_SCRATCH_PREFIX_LENGTH + workspace_prefix_length(SMOKE_CAMPAIGN),
    )
    return worker_prefix + EQASIM_RELATIVE_PATH_BUDGET


def estimate_max_path_length(scratch: Path, campaign: str) -> int:
    """Estimate the longest path eqasim will create below ``scratch``.

    Covers both the campaign's own workers and the ``check --smoke`` runs.
    """
    return len(str(resolve_root(scratch, "execution.scratch"))) + _relative_path_budget(
        campaign
    )


def _windows_path_length_error(length: int, campaign: str) -> str:
    longest_scratch = WINDOWS_PATH_LIMIT - _relative_path_budget(campaign)
    exceeded = (
        f"estimated path length {length} exceeds the Windows limit of 260 characters"
    )
    if longest_scratch < SHORTEST_WINDOWS_SCRATCH:
        return (
            f"{exceeded}; no execution.scratch can fit, because eqasim's own cache "
            f"(e.g. the extracted IRIS archive) needs up to {EQASIM_RELATIVE_PATH_BUDGET} "
            "characters on its own. Enable Windows long paths (LongPathsEnabled, a "
            "one-time administrator action outside eqasim-sa) or use an eqasim "
            "version that creates shorter paths."
        )
    return (
        f"{exceeded}; choose an execution.scratch of at most {longest_scratch} "
        "characters."
    )


def _path_length_item(meta) -> dict:  # type: ignore[no-untyped-def]
    item = {"name": "path_length", "status": "passed", "error": None}
    try:
        length = estimate_max_path_length(meta.execution.scratch, meta.campaign)
    except Exception as error:
        return {"name": "path_length", "status": "failed", "error": _safe_error(error)}
    if platform.IS_WINDOWS:
        if length <= WINDOWS_PATH_LIMIT:
            return item
        if platform.windows_long_paths_enabled():
            item["error"] = (
                f"estimated path length {length} exceeds 260 characters; "
                "Windows long paths are enabled"
            )
            return item
        item["status"] = "failed"
        item["error"] = _windows_path_length_error(length, meta.campaign)
        return item
    limit = DARWIN_PATH_LIMIT if sys.platform == "darwin" else POSIX_PATH_LIMIT
    if length <= limit:
        item["error"] = f"estimated path length {length} is within the limit of {limit}"
        return item
    item["status"] = "warning"
    item["error"] = (
        f"estimated path length {length} exceeds the platform limit of {limit} "
        "characters; choose a shorter execution.scratch"
    )
    return item


def _backend_item(meta) -> dict:  # type: ignore[no-untyped-def]
    if getattr(meta.execution, "backend", "local") == "slurm" and sys.platform != "linux":
        return {
            "name": "backend",
            "status": "failed",
            "error": "the slurm backend requires Linux; run eqasim-sa on the cluster",
        }
    return {"name": "backend", "status": "passed", "error": None}


def _same_machine_item(meta, specs, deadline=None) -> dict:  # type: ignore[no-untyped-def]
    name = "same_machine"
    if not specs:
        return {"name": name, "status": "skipped", "error": "no runs in the plan"}
    if getattr(meta.execution, "backend", "local") == "slurm":
        return {
            "name": name,
            "status": "skipped",
            "error": "slurm runs are compared on the compute node when the job starts",
        }
    try:
        existing = CampaignSnapshot.completed_evidence(meta.campaign_root, specs)
        if not any(evidence is not None for evidence in existing.values()):
            return {
                "name": name,
                "status": "skipped",
                "error": "no completed runs with execution evidence",
            }
        # Read-only: never hashes the data tree and never runs eqasim.
        require_same_machine(
            existing, current_machine_identity(meta, deadline=deadline)
        )
    except Exception as error:
        return {"name": name, "status": "failed", "error": _safe_error(error)}
    return {"name": name, "status": "passed", "error": None}


def _check(name: str, action) -> tuple[dict, object]:  # type: ignore[no-untyped-def]
    try:
        return {"name": name, "status": "passed", "error": None}, action()
    except Exception as error:
        return {"name": name, "status": "failed", "error": _safe_error(error)}, None


def _safe_error(error: BaseException) -> str:
    message = str(error).strip() or error.__class__.__name__
    return re.sub(r"(?<![A-Za-z0-9_.-])/(?:[^\s:;,]+)", "<path>", message)


def _static_check(path: Path, calibrations: tuple[Path, ...]):  # type: ignore[no-untyped-def]
    started = time.monotonic()
    checks, meta, design, specs = [], None, None, ()
    try:
        deadline = started + 55
        meta = load_meta_config(path, validate_factors=False, deadline=deadline)
        calibrations = tuple(
            dict.fromkeys((*calibrations, *meta.calibration_meta_configs))
        )
        unknown = sorted(set(meta.factors) - set(meta.model_document["config"]))
        if unknown:
            raise ValueError(f"unknown factors: {unknown}")
        design = _design(meta)
        item = {"name": "metaConfig", "status": "passed", "error": None}
    except Exception as error:
        item = {"name": "metaConfig", "status": "failed", "error": _safe_error(error)}
        meta = None
    checks.append(item)
    dependency = lambda: (_ for _ in ()).throw(ValueError("meta-config unavailable"))
    def sealed_plan():  # type: ignore[no-untyped-def]
        if meta is None:
            raise ValueError("meta-config unavailable")
        verify_software_source_states(meta.software_identity)
        loaded = load_run_specs(
            meta.campaign_root / "plan.parquet", design, meta.campaign
        )
        verify_software_source_states(meta.software_identity)
        return loaded

    item, specs = _check("sealedPlan", sealed_plan if meta else dependency)
    checks.append(item)
    sealed_valid = item["status"] == "passed"

    def unique_pairs() -> None:
        method = getattr(meta, "method", "grid")
        if method in SEALED_SENSITIVITY_METHODS:
            if not sealed_valid:
                raise ValueError("sealedPlan validation unavailable")
        elif not unique_config_seed_pairs(design):
            raise ValueError("duplicate canonical configuration and seed pair")

    item, _ = _check(
        "uniqueConfigSeedPairs",
        unique_pairs if meta else dependency,
    )
    checks.append(item)

    def environment() -> None:
        if meta is None:
            raise ValueError("meta-config unavailable")
        if meta.execution is None:
            raise ValueError("execution section is required for check")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("static check time budget exhausted")
        return probe_environment(
            meta.execution.invocation, meta.model_root, min(20, remaining)
        )

    item, skipped = _check("eqasimEnvironment", environment)
    if skipped:
        item["status"] = "skipped"
        item["note"] = skipped
    checks.append(item)

    def disk():  # type: ignore[no-untyped-def]
        if meta is None:
            raise ValueError("meta-config unavailable")
        snapshot = []
        estimate, _, _, durable = _estimate_state(
            path, calibrations, meta, deadline, snapshot
        )
        ancestor = meta.campaign_root
        while not ancestor.exists():
            ancestor = ancestor.parent
        before = path_state(ancestor)
        scratch_before = path_state(meta.execution.scratch)
        _remaining(deadline)
        free = shutil.disk_usage(ancestor).free
        scratch_free = shutil.disk_usage(meta.execution.scratch).free
        _remaining(deadline)
        if (
            platform.is_link(ancestor)
            or path_state(ancestor).identity != before.identity
            or path_state(meta.execution.scratch) != scratch_before
            or any(path_state(item) != state for item, state in snapshot)
        ):
            raise RuntimeError("campaign snapshot changed while measuring disk space")
        required = max(0, estimate["estimatedDiskBytes"] - durable)
        missing = max(0, required - free)
        scratch_required = estimate["estimatedScratchBytes"]
        if ancestor.stat().st_dev == meta.execution.scratch.stat().st_dev:
            scratch_missing = max(0, scratch_required - max(0, free - required))
        else:
            scratch_missing = max(0, scratch_required - scratch_free)
        return (
            estimate["estimatedDiskBytes"],
            required,
            free,
            missing,
            scratch_required,
            scratch_free,
            scratch_missing,
        )

    for build in (_backend_item, _path_length_item):
        if meta is not None and meta.execution is not None:
            checks.append(build(meta))
    if meta is not None and meta.execution is not None and sealed_valid:
        checks.append(_same_machine_item(meta, specs, deadline))

    try:
        disk_values = disk()
        missing, scratch_missing = disk_values[3], disk_values[6]
        item = {
            "name": "diskSpace",
            "status": "failed" if missing or scratch_missing else "passed",
            "error": (
                "insufficient disk space: "
                f"durable missing {missing} bytes; scratch missing {scratch_missing} bytes"
            )
            if missing or scratch_missing
            else None,
        }
    except Exception as error:
        item, disk_values = (
            {"name": "diskSpace", "status": "failed", "error": _safe_error(error)},
            None,
        )
    checks.append(item)
    values = disk_values or (None,) * 7
    payload = {
        "status": "valid"
        if all(item["status"] in {"passed", "warning", "skipped"} for item in checks)
        else "invalid",
        "checkVersion": 2,
        "elapsedSeconds": time.monotonic() - started,
        "checks": checks,
        "estimatedDiskBytes": values[0],
        "requiredDiskBytes": values[1],
        "freeDiskBytes": values[2],
        "missingDiskBytes": values[3],
        "estimatedScratchBytes": values[4],
        "freeScratchBytes": values[5],
        "missingScratchBytes": values[6],
        "error": None,
    }
    return payload, meta, specs


def _smoke_log(root: Path, run_id: str) -> str:
    log = root / "logs" / f"{run_id}.log"
    if not log.is_file() or platform.is_link(log):
        return ""
    with log.open("rb") as stream:
        stream.seek(max(0, log.stat().st_size - 65536))
        return stream.read().decode("utf-8", errors="replace").strip()


def _smoke(meta, specs) -> None:  # type: ignore[no-untyped-def]
    EqasimBatchExecutor.validate(meta)
    seeds = tuple(dict.fromkeys(meta.seeds))
    if (
        not specs
        or len(seeds) < 2
        or "sampling_rate" not in specs[0].resolved_config.get("config", {})
    ):
        raise ValueError("smoke requires sampling_rate and two distinct explicit seeds")
    specs = _apply_execution_overrides(
        tuple(specs), getattr(meta.execution, "overrides", {})
    )
    # Below execution.scratch so that the path_length estimate covers it.
    configured_scratch = resolve_root(meta.execution.scratch, "execution.scratch")
    owned = configured_scratch / f"{_SMOKE_DIRECTORY_PREFIX}{uuid.uuid4().hex[:8]}"
    owned.mkdir()
    try:
        _smoke_in(meta, seeds, specs, owned)
    finally:
        platform.remove_tree(owned)


def _smoke_in(meta, seeds, specs, owned: Path) -> None:  # type: ignore[no-untyped-def]
    campaign_root, scratch = owned / "c", owned / "s"
    scratch.mkdir()
    executor = EqasimBatchExecutor(
        meta,
        campaign=SMOKE_CAMPAIGN,
        campaign_root=campaign_root,
        scratch=scratch,
    )
    first = specs[0]
    smoke_specs = []
    for seed in seeds[:2]:
        config = json.loads(json.dumps(first.resolved_config))
        config["config"]["sampling_rate"] = 0.001
        config["config"]["random_seed"] = seed
        smoke_specs.append(
            RunSpec(
                "smoke",
                config,
                seed,
                first.expected_context_hash,
                {**first.parameters, "sampling_rate": 0.001},
            )
        )
    try:
        batches = worker_batches(smoke_specs, 1)
        results = ordered_results(
            [executor.execute(batches[0])], len(smoke_specs)
        )
        if any(result.status != "completed" for result in results):
            details = []
            for result in results:
                message = (
                    _smoke_log(campaign_root, result.run_id)
                    or result.error
                    or result.status
                )
                details.append(f"{result.run_id}: {message}")
            raise RuntimeError("; ".join(details))
        expected_ids = {result.run_id for result in results}
        if (
            CampaignSnapshot.completed_specs(campaign_root, tuple(smoke_specs))
            != expected_ids
        ):
            details = [
                f"{run_id}: {_smoke_log(campaign_root, run_id) or 'incomplete marker'}"
                for run_id in sorted(expected_ids)
            ]
            raise RuntimeError("; ".join(details))
        for result in results:
            archive = campaign_root / "archives" / result.run_id
            entries = (
                tuple(archive.iterdir())
                if archive.is_dir() and not platform.is_link(archive)
                else ()
            )
            prefix = smoke_specs[0].resolved_config["config"].get(
                "output_prefix", DEFAULT_OUTPUT_PREFIX
            )
            expected = {
                archive_file_name(name, declaration.source_path(prefix)): name
                for name, declaration in meta.outputs.items()
            }
            if {item.name for item in entries} != set(expected) or any(
                platform.is_link(item) or not item.is_file() for item in entries
            ):
                message = (
                    _smoke_log(campaign_root, result.run_id)
                    or "incomplete archive"
                )
                raise RuntimeError(
                    f"{result.run_id}: {message}"
                )
            artifacts = ArtifactSet({expected[item.name]: item for item in entries})
            for indicator in getattr(meta, "indicators", ()):
                # Analysis outputs come from scripts run by analyze, not eqasim.
                source = getattr(indicator, "source", None) or getattr(
                    getattr(indicator, "artifact", None), "source", None
                )
                if isinstance(source, DerivedOutput):
                    continue
                try:
                    value = indicator.compute(artifacts)
                except Exception as error:
                    raise ValueError(
                        f"{result.run_id}: smoke indicator {indicator.name!r}: {error}"
                    ) from error
                if type(value) is not float or not math.isfinite(value):
                    raise ValueError(
                        f"{result.run_id}: smoke indicator {indicator.name!r} "
                        "must return a finite float"
                    )
    finally:
        executor.interrupt()


def check_from_meta_config(
    path: Path, calibrations: tuple[Path, ...], smoke: bool
) -> dict:
    """Run static and optional smoke checks for a sealed campaign."""

    payload, meta, specs = _static_check(path, calibrations)
    if not smoke or payload["status"] != "valid":
        return payload
    item, _ = _check("smoke", lambda: _smoke(meta, specs))
    payload["checks"].append(item)
    payload["status"] = "valid" if item["status"] == "passed" else "invalid"
    return payload
