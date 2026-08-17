import copy
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from argparse import Namespace
from pathlib import Path

import pyarrow.parquet as pq

from eqasim_sa.adapters.backends.local import LocalBackend
from eqasim_sa.adapters.backends.slurm import EqasimSlurmJob, SlurmBackend
from eqasim_sa.adapters.data_manifest import generate_data_manifest
from eqasim_sa.adapters.eqasim.artifacts import ANALYSIS, POPULATION
from eqasim_sa.adapters.eqasim.config import EqasimSettings, load_config
from eqasim_sa.adapters.eqasim.simulator import (
    DEDICATED,
    EqasimSimulator,
    eqasim_requirements,
    probe_cache_coordination,
    validate_transport,
)
from eqasim_sa.adapters.fake.simulator import FakeSimulator
from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.adapters.templates import render_nantes
from eqasim_sa.cli import dispatch
from eqasim_sa.core.analyze import analyze
from eqasim_sa.core.ledger import completion_duration, completion_ids, run_path
from eqasim_sa.core.plan import (
    GridDesign,
    MorrisDesign,
    PlanResult,
    SobolDesign,
    build_plan,
    estimate_plan,
    load_run_specs,
    resolve_config,
    unique_config_seed_pairs,
)
from eqasim_sa.core.run import (
    EXCLUDED_PATHS,
    RunSpec,
    compute_run_id,
    execute_run,
    pending_run_specs,
    summarize_runs,
)


def build_eqasim(arguments: Namespace) -> tuple[EqasimSimulator, dict, object]:
    invocation = json.loads(arguments.invocation_json)
    if not isinstance(invocation, list) or not all(
        isinstance(item, str) for item in invocation
    ):
        raise TypeError("invocation-json must be a JSON array of strings")
    settings = EqasimSettings(
        arguments.config_yaml,
        arguments.eqasim_root,
        tuple(invocation),
        arguments.campaign,
        arguments.scratch,
    )
    config = load_config(settings.config_path, arguments.seed)
    return EqasimSimulator(settings, config), config, settings.workdir


def plan_from_meta_config(path: Path) -> PlanResult:
    meta = load_meta_config(path)
    descriptor = json.dumps(
        meta.context_descriptor, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    if (
        set(meta.context_descriptor)
        != {"eqasimCommit", "dataManifestSha256", "lockfileSha256", "eqasimSaVersion"}
        or hashlib.sha256(b"eqasim-sa:context:v1\0" + descriptor).hexdigest()
        != meta.context_hash
    ):
        raise ValueError("context descriptor does not match contextHash")
    requirements = dict(eqasim_requirements(meta.indicator_requirements))
    config_requirements = requirements.get("config", {})
    if not isinstance(config_requirements, dict):
        raise TypeError("requirements config must be a mapping")
    for name, value in config_requirements.items():
        if f"config.{name}" in EXCLUDED_PATHS or name in DEDICATED:
            raise ValueError(f"requirements contain infrastructure option: {name!r}")
        validate_transport(name, value)
    ancestor = meta.campaign_root.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    lock_name = hashlib.sha256(os.fsencode(meta.campaign_root)).hexdigest()[:16]
    lock_path = ancestor / f".eqasim-sa-plan-{lock_name}.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return build_plan(_design(meta, requirements))
    finally:
        os.close(descriptor)


def _warn_sobol_capacity(
    design_positions: int,
    unique_runs: int,
    pending_runs: int,
    backend: str,
    parallel_capacity: int,
) -> None:
    waves = (pending_runs + parallel_capacity - 1) // parallel_capacity
    try:
        print(
            "WARNING sobol backend capacity: "
            f"designPositions={design_positions}, uniqueRuns={unique_runs}, "
            f"pendingRuns={pending_runs}, backend={backend}, "
            f"parallelCapacity={parallel_capacity}, minimumWaves={waves}",
            file=sys.stderr,
        )
    except OSError:
        pass


def run_from_meta_config(path: Path) -> dict:
    meta_path = path.resolve(strict=True)
    meta_bytes = meta_path.read_bytes()
    meta = load_meta_config(meta_path)
    if meta_path.read_bytes() != meta_bytes:
        raise RuntimeError("meta-config changed while preparing execution")
    if meta.execution is None:
        raise ValueError("execution section is required for run")
    requirements = dict(eqasim_requirements(meta.indicator_requirements))
    design = _design(meta, requirements)
    specs = load_run_specs(meta.campaign_root / "plan.parquet", design, meta.campaign)
    unique_runs = len(specs)
    campaign_root = meta.campaign_root.resolve(strict=True)
    lock_name = hashlib.sha256(os.fsencode(campaign_root)).hexdigest()[:16]
    lock_path = campaign_root.parent / f".eqasim-sa-campaign-{lock_name}.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(
                "another invocation is already running this campaign"
            ) from error
        specs = pending_run_specs(meta.campaign_root, specs)
        if not specs:
            return summarize_runs([], [])
        if meta.method == "sobol" and len(specs) > meta.execution.workers:
            positions = pq.ParquetFile(
                meta.campaign_root / "plan.parquet"
            ).metadata.num_rows
            _warn_sobol_capacity(
                positions,
                unique_runs,
                len(specs),
                meta.execution.backend,
                meta.execution.workers,
            )
        if meta.execution.overrides:
            overridden = []
            for spec in specs:
                config = copy.deepcopy(spec.resolved_config)
                nested = config.get("config")
                if not isinstance(nested, dict):
                    raise TypeError("resolved config block must be a mapping")
                for name, value in meta.execution.overrides.items():
                    nested[name] = str(value) if isinstance(value, Path) else value
                overridden.append(
                    RunSpec(
                        spec.campaign,
                        config,
                        spec.seed,
                        spec.expected_context_hash,
                        spec.parameters,
                    )
                )
            specs = tuple(overridden)
        if meta.execution.backend == "local":
            settings = EqasimSettings(
                meta.model_config,
                meta.model_root,
                meta.execution.invocation,
                meta.campaign,
                meta.execution.scratch,
            )
            simulator = EqasimSimulator(settings, meta.model_document, meta.model_bytes)
            interrupted = threading.Event()

            def interrupt() -> None:
                interrupted.set()
                simulator.interrupt()

            results = list(
                LocalBackend(meta.execution.workers).map(
                    lambda spec: execute_run(
                        spec,
                        simulator,
                        meta.campaign_root,
                        settings.workdir,
                        interrupted.is_set,
                    ),
                    specs,
                    interrupt,
                )
            )
        else:
            slurm = meta.execution.slurm
            parameters: dict[str, object] = {
                "timeout_min": slurm.timeout_min,
                "cpus_per_task": meta.execution.overrides.get("processes", 1),
            }
            if slurm.mem_gb is not None:
                parameters["mem_gb"] = slurm.mem_gb
            for name in ("partition", "account", "qos"):
                value = getattr(slurm, name)
                if value is not None:
                    parameters[f"slurm_{name}"] = value
            submitit_root = meta.campaign_root / ".submitit"
            try:
                submitit_root.mkdir(mode=0o700)
            except FileExistsError:
                pass
            if submitit_root.is_symlink() or not submitit_root.is_dir():
                raise ValueError(
                    "submitit technical path must be a non-symlink directory"
                )
            backend = SlurmBackend(
                submitit_root / "%j",
                meta.execution.workers,
                parameters,
            )
            job = EqasimSlurmJob(
                meta_path,
                meta.context_hash,
                hashlib.sha256(meta_bytes).hexdigest(),
            )
            results = list(backend.map(job, specs))
    finally:
        os.close(descriptor)
    return summarize_runs(
        [compute_run_id(spec.resolved_config, spec.seed) for spec in specs], results
    )


def analyze_from_meta_config(path: Path) -> dict:
    meta = load_meta_config(path)
    result = analyze(
        meta.campaign_root,
        meta.indicators,
        campaign=meta.campaign,
        completeness_threshold=meta.completeness_threshold,
        seed_role=meta.seed_role,
        method=meta.method,
        bootstrap_resamples=getattr(meta, "bootstrap_resamples", None),
        confidence_level=getattr(meta, "confidence_level", None),
    )
    return {
        "status": "completed",
        "path": str(result.path),
        "runs": result.runs,
        "indicators": result.indicators,
        "values": result.values,
        "completionRate": result.completion_rate,
        "expected": result.expected,
        "completed": result.completed,
        "missing": result.missing,
        "failed": result.failed,
        "dispersionPath": str(result.dispersion_path)
        if result.dispersion_path
        else None,
        "sensitivityMethod": result.sensitivity_method,
        "sensitivityPath": str(result.sensitivity_path)
        if result.sensitivity_path
        else None,
        "error": None,
    }


def _design(meta, requirements=None):  # type: ignore[no-untyped-def]
    common = (
        meta.model_document,
        meta.factors,
        meta.seeds,
        requirements
        if requirements is not None
        else dict(eqasim_requirements(meta.indicator_requirements)),
        meta.context_hash,
        meta.context_descriptor,
        meta.campaign_root,
    )
    if getattr(meta, "method", "grid") == "morris":
        return MorrisDesign(*common, meta.trajectories, meta.design_seed)
    if getattr(meta, "method", "grid") == "sobol":
        return SobolDesign(*common, meta.base_sample_size, meta.design_seed)
    return GridDesign(*common)


def _file_state(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _remaining(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("static check time budget exhausted")


def _estimate_state(
    path: Path,
    calibration_paths: tuple[Path, ...],
    target=None,
    deadline: float | None = None,
    snapshot: list | None = None,
):  # type: ignore[no-untyped-def]
    target = target or (
        load_meta_config(path, deadline=deadline)
        if deadline
        else load_meta_config(path)
    )
    target_root = target.campaign_root.resolve(strict=True)
    target_specs = ()
    target_plan_state = None
    design_positions = 0
    observations: list[tuple[float, int]] = []
    target_durable = 0
    seen: set[Path] = set()
    calibrations = (
        load_meta_config(item, deadline=deadline)
        if deadline
        else load_meta_config(item)
        for item in calibration_paths
    )
    for meta in (target, *calibrations):
        _remaining(deadline)
        if snapshot is not None:
            snapshot.extend(getattr(meta, "control_states", ()))
        root = meta.campaign_root.resolve(strict=True)
        if root in seen:
            continue
        seen.add(root)
        plan = root / "plan.parquet"
        plan_state = _file_state(plan)
        if snapshot is not None:
            snapshot.append((plan, plan_state))
        specs = load_run_specs(plan, _design(meta), meta.campaign)
        _remaining(deadline)
        positions = pq.ParquetFile(plan).metadata.num_rows
        if _file_state(plan) != plan_state:
            raise RuntimeError(f"calibration plan mutated while reading {plan}")
        if root == target_root:
            target_specs, target_plan_state, design_positions = (
                specs,
                plan_state,
                positions,
            )
        complete = completion_ids(root, specs)
        for spec in specs:
            run_id = compute_run_id(spec.resolved_config, spec.seed)
            if run_id not in complete:
                continue
            marker = run_path(root, run_id)
            archive = root / "archives" / run_id
            if archive.is_symlink() or not archive.is_dir():
                raise ValueError(
                    f"invalid archive for {run_id}: regular directory required"
                )
            entries = tuple(sorted(archive.iterdir(), key=lambda item: item.name))
            if not entries or any(
                entry.is_symlink() or not entry.is_file() or entry.suffix != ".parquet"
                for entry in entries
            ):
                raise ValueError(
                    f"invalid archive for {run_id}: only flat regular Parquet files allowed"
                )
            before = tuple((entry, _file_state(entry)) for entry in (marker, *entries))
            archive_state = _file_state(archive)
            try:
                for entry in entries:
                    pq.ParquetFile(entry).metadata
            except Exception as error:
                raise ValueError(f"invalid archive for {run_id}: {error}") from error
            _remaining(deadline)
            duration = completion_duration(root, spec)
            if duration is None:
                raise RuntimeError(f"completion marker disappeared for {run_id}")
            after_entries = tuple(sorted(archive.iterdir(), key=lambda item: item.name))
            after = tuple(
                (entry, _file_state(entry)) for entry in (marker, *after_entries)
            )
            if before != after:
                raise RuntimeError(f"calibration source mutated while reading {run_id}")
            if snapshot is not None:
                snapshot.extend(((archive, archive_state), *before))
            observations.append((duration, sum(state[2] for _, state in before)))
            if root == target_root:
                target_durable += before[0][1][2] + sum(
                    state[2] for _, state in before[1:]
                )
    target_plan = target_root / "plan.parquet"
    if target_plan_state is None or _file_state(target_plan) != target_plan_state:
        raise RuntimeError(f"target plan mutated while estimating {target_plan}")
    payload = estimate_plan(
        design_positions,
        tuple(compute_run_id(spec.resolved_config, spec.seed) for spec in target_specs),
        target_plan_state[2],
        tuple(observations),
        getattr(target, "method", "grid"),
        (
            {"D": len(target.factors), "T": target.trajectories, "R": len(target.seeds)}
            if getattr(target, "method", "grid") == "morris"
            else {"D": len(target.factors) + 1, "N": target.base_sample_size}
            if getattr(target, "method", "grid") == "sobol"
            else None
        ),
    )
    durable = target_plan_state[2] + target_durable
    return payload, target, target_specs, durable


def estimate_from_meta_config(path: Path, calibration_paths: tuple[Path, ...]) -> dict:
    target = load_meta_config(path)
    calibrations = tuple(
        dict.fromkeys((*calibration_paths, *target.calibration_meta_configs))
    )
    return _estimate_state(path, calibrations, target)[0]


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
    item, specs = _check(
        "sealedPlan",
        (
            lambda: load_run_specs(
                meta.campaign_root / "plan.parquet", design, meta.campaign
            )
        )
        if meta
        else dependency,
    )
    checks.append(item)
    sealed_valid = item["status"] == "passed"
    item, _ = _check(
        "uniqueConfigSeedPairs",
        (
            lambda: (
                (
                    sealed_valid
                    if getattr(meta, "method", "grid") in {"morris", "sobol"}
                    else unique_config_seed_pairs(design)
                )
                or (_ for _ in ()).throw(
                    ValueError(
                        "sealedPlan validation unavailable"
                        if getattr(meta, "method", "grid") in {"morris", "sobol"}
                        else "duplicate canonical configuration and seed pair"
                    )
                )
            )
        )
        if meta
        else dependency,
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
        probe_cache_coordination(
            meta.execution.invocation, meta.model_root, min(20, remaining)
        )

    item, _ = _check("eqasimEnvironment", environment)
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
        before = _file_state(ancestor)
        _remaining(deadline)
        free = shutil.disk_usage(ancestor).free
        _remaining(deadline)
        if (
            ancestor.is_symlink()
            or _file_state(ancestor)[:2] != before[:2]
            or any(_file_state(item) != state for item, state in snapshot)
        ):
            raise RuntimeError("campaign snapshot changed while measuring disk space")
        required = max(0, estimate["estimatedDiskBytes"] - durable)
        missing = max(0, required - free)
        return estimate["estimatedDiskBytes"], required, free, missing

    try:
        disk_values = disk()
        missing = disk_values[3]
        item = {
            "name": "diskSpace",
            "status": "failed" if missing else "passed",
            "error": f"insufficient disk space: missing {missing} bytes"
            if missing
            else None,
        }
    except Exception as error:
        item, disk_values = (
            {"name": "diskSpace", "status": "failed", "error": _safe_error(error)},
            None,
        )
    checks.append(item)
    values = disk_values or (None, None, None, None)
    payload = {
        "status": "valid"
        if all(item["status"] == "passed" for item in checks)
        else "invalid",
        "checkVersion": 1,
        "elapsedSeconds": time.monotonic() - started,
        "checks": checks,
        "estimatedDiskBytes": values[0],
        "requiredDiskBytes": values[1],
        "freeDiskBytes": values[2],
        "missingDiskBytes": values[3],
        "error": None,
    }
    return payload, meta, specs


def _smoke_log(root: Path, run_id: str) -> str:
    log = root / "logs" / f"{run_id}.log"
    if not log.is_file() or log.is_symlink():
        return ""
    with log.open("rb") as stream:
        stream.seek(max(0, log.stat().st_size - 65536))
        return stream.read().decode("utf-8", errors="replace").strip()


def _smoke(meta, specs) -> None:  # type: ignore[no-untyped-def]
    seeds = tuple(dict.fromkeys(meta.seeds))
    if (
        not specs
        or len(seeds) < 2
        or "sampling_rate" not in specs[0].resolved_config.get("config", {})
    ):
        raise ValueError("smoke requires sampling_rate and two distinct explicit seeds")
    temporary_parent = Path(tempfile.gettempdir()).resolve(strict=True)
    target = meta.campaign_root.resolve(strict=True)
    if temporary_parent == target or temporary_parent.is_relative_to(target):
        temporary_parent = target.parent
    with tempfile.TemporaryDirectory(
        prefix="eqasim-sa-smoke-", dir=temporary_parent
    ) as parent:
        owned = Path(parent)
        campaign_root, scratch = owned / "campaign", owned / "scratch"
        scratch.mkdir()
        settings = EqasimSettings(
            meta.model_config,
            meta.model_root,
            meta.execution.invocation,
            "smoke",
            scratch,
        )
        simulator = EqasimSimulator(settings, meta.model_document, meta.model_bytes)
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
            results = [
                execute_run(spec, simulator, campaign_root, settings.workdir)
                for spec in smoke_specs
            ]
            if any(result.status != "completed" for result in results):
                details = [
                    f"{result.run_id}: {_smoke_log(campaign_root, result.run_id) or result.error or result.status}"
                    for result in results
                ]
                raise RuntimeError("; ".join(details))
            expected_ids = {result.run_id for result in results}
            if completion_ids(campaign_root, tuple(smoke_specs)) != expected_ids:
                details = [
                    f"{run_id}: {_smoke_log(campaign_root, run_id) or 'incomplete marker'}"
                    for run_id in sorted(expected_ids)
                ]
                raise RuntimeError("; ".join(details))
            for result in results:
                archive = campaign_root / "archives" / result.run_id
                entries = (
                    tuple(archive.iterdir())
                    if archive.is_dir() and not archive.is_symlink()
                    else ()
                )
                requested = set(
                    result
                    for stage in smoke_specs[0].resolved_config["run"]
                    for result in (
                        {
                            "synthesis.output": POPULATION,
                            "analysis.synthesis.population": ANALYSIS,
                        }[stage]
                    )
                )
                expected = {f"{name}.parquet" for name in requested}
                if {item.name for item in entries} != expected or any(
                    item.is_symlink() or not item.is_file() for item in entries
                ):
                    raise RuntimeError(
                        f"{result.run_id}: {_smoke_log(campaign_root, result.run_id) or 'incomplete archive'}"
                    )
                states = tuple((item, _file_state(item)) for item in entries)
                for item in entries:
                    pq.ParquetFile(item).metadata
                if states != tuple((item, _file_state(item)) for item in entries):
                    raise RuntimeError(
                        f"{result.run_id}: smoke archive mutated while validating"
                    )
        finally:
            simulator.interrupt()


def check_from_meta_config(
    path: Path, calibrations: tuple[Path, ...], smoke: bool
) -> dict:
    payload, meta, specs = _static_check(path, calibrations)
    if not smoke or payload["status"] != "valid":
        return payload
    item, _ = _check("smoke", lambda: _smoke(meta, specs))
    payload["checks"].append(item)
    payload["status"] = "valid" if item["status"] == "passed" else "invalid"
    return payload


def _reject_symlink_traversal(path: Path, name: str) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"{name} must not traverse a symlink")


def _directory(path: Path, name: str) -> Path:
    _reject_symlink_traversal(path, name)
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(f"{name} must be an existing directory")
    return resolved


def _directory_or_create(path: Path, name: str) -> Path:
    _reject_symlink_traversal(path, name)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return _directory(path, name)


def _init_allowed(directory: Path) -> None:
    allowed_files = {"nantes.yml", "nantes-calibration.yml"}
    allowed_directories = {"target-campaign", "calibration-campaign"}
    for entry in directory.iterdir():
        control = (
            entry.name.startswith(".eqasim-sa-plan-")
            or entry.name.startswith(".eqasim-sa-run-")
            or entry.name.startswith(".eqasim-sa-campaign-")
        ) and entry.name.endswith(".lock")
        if entry.is_symlink() or (
            entry.name not in allowed_files
            and entry.name not in allowed_directories
            and not control
        ):
            raise FileExistsError(f"foreign content in init directory: {entry.name}")
        if entry.name in allowed_files and not entry.is_file():
            raise FileExistsError(f"foreign content in init directory: {entry.name}")
        if entry.name in allowed_directories and not entry.is_dir():
            raise FileExistsError(f"foreign content in init directory: {entry.name}")
        if control and not entry.is_file():
            raise FileExistsError(f"foreign content in init directory: {entry.name}")


def _init_campaign_allowed(
    root: Path,
    expected_ids: set[str],
    calibration: bool,
    expected_artifacts: set[str] | None = None,
) -> None:
    allowed = {"plan.parquet"} | (
        {"runs", "archives", "logs", ".eqasim-sa-tmp"} if calibration else set()
    )
    for entry in root.iterdir():
        if entry.is_symlink() or entry.name not in allowed:
            raise FileExistsError(f"foreign content in init campaign: {entry.name}")
        if entry.name in {"plan.parquet", ".eqasim-sa-tmp"}:
            if (
                entry.name == ".eqasim-sa-tmp"
                and entry.is_dir()
                and not any(entry.iterdir())
            ):
                continue
            if not entry.is_file():
                raise FileExistsError(f"foreign content in init campaign: {entry.name}")
            continue
        if not entry.is_dir():
            raise FileExistsError(f"foreign content in init campaign: {entry.name}")
        for child in entry.iterdir():
            expected_name = (
                f"{child.stem}.parquet"
                if entry.name == "runs"
                else f"{child.stem}.log"
                if entry.name == "logs"
                else child.name
            )
            run_id = child.stem if entry.name != "archives" else child.name
            valid_type = child.is_dir() if entry.name == "archives" else child.is_file()
            if (
                child.is_symlink()
                or not valid_type
                or run_id not in expected_ids
                or child.name != expected_name
            ):
                raise FileExistsError(
                    f"foreign content in init campaign: {entry.name}/{child.name}"
                )
            if entry.name == "archives":
                for artifact in child.iterdir():
                    if (
                        artifact.is_symlink()
                        or not artifact.is_file()
                        or expected_artifacts is None
                        or artifact.name not in expected_artifacts
                    ):
                        raise FileExistsError(
                            f"foreign content in init campaign: archives/{child.name}/{artifact.name}"
                        )


def init_from_inputs(
    directory: Path,
    model_root: Path,
    data_manifest: Path | None,
    data_path: Path,
    scratch: Path,
    invocation: tuple[str, ...],
    model_config: Path = Path("config_local_nantes.yml"),
    regenerate_data_manifest: bool = False,
) -> dict:
    started = time.monotonic()
    model = _directory(model_root, "model-root")
    if model_config.is_absolute() or ".." in model_config.parts:
        raise ValueError("model-config must be relative to model-root")
    selected_config = model / model_config
    _reject_symlink_traversal(selected_config, "model-config")
    if selected_config.is_symlink() or not selected_config.is_file():
        raise ValueError(
            "model-config must be a regular non-symlink file under model-root"
        )
    data = _directory(data_path, "data-path")
    scratch = _directory_or_create(scratch, "scratch")
    if not invocation or not all(
        isinstance(item, str) and item and "\0" not in item for item in invocation
    ):
        raise ValueError("invocation must contain non-empty arguments")
    default_manifest = data_manifest is None
    if default_manifest:
        data_manifest = Path(f"{data}.manifest.json")
    _reject_symlink_traversal(data_manifest, "data-manifest")
    if regenerate_data_manifest or default_manifest and not data_manifest.exists():
        generate_data_manifest(data, data_manifest, replace=regenerate_data_manifest)
    if data_manifest.is_symlink() or not data_manifest.resolve(strict=True).is_file():
        raise ValueError("data-manifest must be a regular non-symlink file")
    parent = directory.parent
    _reject_symlink_traversal(directory, "directory")
    if not parent.resolve(strict=True).is_dir():
        raise ValueError("directory parent must be a regular non-symlink directory")
    absolute = Path(os.path.abspath(directory))
    if absolute.is_symlink() or absolute.exists() and not absolute.is_dir():
        raise ValueError("directory must not be a file or symlink")
    created = not absolute.exists()
    staging = None
    if created:
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{absolute.name}-", dir=parent.resolve(strict=True)
            )
        )
        target_bytes = render_nantes(
            staging,
            model,
            data_manifest,
            data,
            scratch,
            invocation,
            False,
            model_config,
            not default_manifest,
        )
        calibration_bytes = render_nantes(
            staging, model, data_manifest, data, scratch, invocation, True, model_config,
            not default_manifest,
        )
        (staging / "nantes.yml").write_bytes(target_bytes)
        (staging / "nantes-calibration.yml").write_bytes(calibration_bytes)
        try:
            load_meta_config(staging / "nantes-calibration.yml")
            load_meta_config(staging / "nantes.yml")
            staging.replace(absolute)
            staging = None
        finally:
            if staging is not None:
                shutil.rmtree(staging)
    else:
        _init_allowed(absolute)
        expected_target = render_nantes(
            absolute,
            model,
            data_manifest,
            data,
            scratch,
            invocation,
            False,
            model_config,
            not default_manifest,
        )
        expected_calibration = render_nantes(
            absolute,
            model,
            data_manifest,
            data,
            scratch,
            invocation,
            True,
            model_config,
            not default_manifest,
        )
        files = (absolute / "nantes.yml", absolute / "nantes-calibration.yml")
        if not all(path.is_file() and not path.is_symlink() for path in files):
            if any(absolute.iterdir()):
                raise FileExistsError(
                    "non-empty directory is not a complete init state"
                )
            files[0].write_bytes(expected_target)
            files[1].write_bytes(expected_calibration)
        elif (
            files[0].read_bytes() != expected_target
            or files[1].read_bytes() != expected_calibration
        ):
            raise FileExistsError("init templates do not match the requested inputs")
    target, calibration = absolute / "nantes.yml", absolute / "nantes-calibration.yml"
    target_plan = plan_from_meta_config(target)
    plan_from_meta_config(calibration)
    calibration_meta = load_meta_config(calibration)
    specs = load_run_specs(
        calibration_meta.campaign_root / "plan.parquet",
        _design(calibration_meta),
        calibration_meta.campaign,
    )
    expected_ids = {compute_run_id(spec.resolved_config, spec.seed) for spec in specs}
    requested = eqasim_requirements(calibration_meta.indicator_requirements)["run"]
    expected_artifacts = {
        f"{name}.parquet"
        for stage in requested
        for name in (
            {"synthesis.output": POPULATION, "analysis.synthesis.population": ANALYSIS}[
                stage
            ]
        )
    }
    _init_campaign_allowed(absolute / "target-campaign", set(), False)
    _init_campaign_allowed(
        calibration_meta.campaign_root, expected_ids, True, expected_artifacts
    )
    calibration_result = run_from_meta_config(calibration)
    if calibration_result["status"] != "completed":
        raise RuntimeError(f"calibration failed: {calibration_result['failures']}")
    complete = completion_ids(calibration_meta.campaign_root, specs)
    if len(complete) != 2:
        raise RuntimeError("calibration is incomplete")
    for run_id in complete:
        archive = calibration_meta.campaign_root / "archives" / run_id
        entries = (
            tuple(archive.iterdir())
            if archive.is_dir() and not archive.is_symlink()
            else ()
        )
        if {entry.name for entry in entries} != expected_artifacts or any(
            entry.is_symlink() or not entry.is_file() for entry in entries
        ):
            raise RuntimeError(f"calibration archive is incomplete for {run_id}")
        for entry in entries:
            pq.ParquetFile(entry).metadata
    return {
        "status": "initialized" if created else "unchanged",
        "metaConfig": "nantes.yml",
        "calibrationMetaConfig": "nantes-calibration.yml",
        "plan": "target-campaign/plan.parquet",
        "calibrationRuns": len(complete),
        "elapsedSeconds": time.monotonic() - started,
        "error": None,
    }


def main() -> int:
    try:
        return dispatch(
            FakeSimulator(),
            LocalBackend(),
            build_eqasim,
            plan_from_meta_config,
            run_from_meta_config,
            estimate_from_meta_config,
            check_from_meta_config,
            init_from_inputs,
            analyze_from_meta_config,
        )
    except KeyboardInterrupt:
        return 130
