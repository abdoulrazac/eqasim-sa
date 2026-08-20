import copy
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
from argparse import Namespace
from pathlib import Path

import pyarrow.parquet as pq

from eqasim_sa.adapters.backends.local import LocalBackend
from eqasim_sa.adapters.backends.slurm import EqasimSlurmJob, SlurmBackend
from eqasim_sa.adapters.data_manifest import DOMAIN, generate_data_manifest
from eqasim_sa.adapters.eqasim.artifacts import ANALYSIS, POPULATION
from eqasim_sa.adapters.eqasim.config import (
    EqasimSettings,
    load_config,
    load_yaml_document,
    validate_eqasim_document,
)
from eqasim_sa.adapters.eqasim.simulator import (
    DEDICATED,
    EqasimSimulator,
    eqasim_requirements,
    probe_environment,
    validate_transport,
)
from eqasim_sa.adapters.fake.simulator import FakeSimulator
from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.adapters.templates import render_project
from eqasim_sa.adapters.worker import execute_batch, ordered_results, worker_batches
from eqasim_sa.cli import dispatch
from eqasim_sa.core.analyze import analyze
from eqasim_sa.core.charter import serialize_scientific_contract
from eqasim_sa.core.ledger import (
    completion_duration,
    completion_ids,
    completion_scratch_bytes,
    run_path,
)
from eqasim_sa.core.plan import (
    CanonicalMorrisDesign,
    DiscreteOATDesign,
    GridDesign,
    MorrisDesign,
    MultiScrambleSobolDesign,
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
    simulator = EqasimSimulator(settings, config)
    return simulator, config, simulator.paths.root


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
    method: str = "sobol",
) -> None:
    waves = (pending_runs + parallel_capacity - 1) // parallel_capacity
    try:
        print(
            f"WARNING {method} backend capacity: "
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
        if meta.method in {"sobol", "multiscramble_sobol"} and len(specs) > meta.execution.workers:
            positions = pq.ParquetFile(
                meta.campaign_root / "plan.parquet"
            ).metadata.num_rows
            _warn_sobol_capacity(
                positions,
                unique_runs,
                len(specs),
                meta.execution.backend,
                meta.execution.workers,
                meta.method,
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
            interrupted = threading.Event()
            active: set[EqasimSimulator] = set()
            active_lock = threading.Lock()

            def interrupt() -> None:
                interrupted.set()
                with active_lock:
                    simulators = tuple(active)
                for simulator in simulators:
                    simulator.interrupt()

            def run_batch(batch):  # type: ignore[no-untyped-def]
                simulator = EqasimSimulator(
                    settings, meta.model_document, meta.model_bytes, batch.slot
                )
                with active_lock:
                    active.add(simulator)
                try:
                    with simulator.lease():
                        return execute_batch(
                            batch,
                            lambda spec: execute_run(
                                spec,
                                simulator,
                                meta.campaign_root,
                                simulator.paths.root,
                                interrupted.is_set,
                            ),
                        )
                finally:
                    with active_lock:
                        active.discard(simulator)

            batches = worker_batches(specs, meta.execution.workers)
            results = ordered_results(
                list(
                    LocalBackend(len(batches)).map(run_batch, batches, interrupt)
                ),
                len(specs),
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
            batches = worker_batches(specs, meta.execution.workers)
            results = ordered_results(list(backend.map(job, batches)), len(specs))
    finally:
        os.close(descriptor)
    return summarize_runs(
        [compute_run_id(spec.resolved_config, spec.seed) for spec in specs], results
    )


def _execute_exclusive(spec, simulator, campaign_root, workdir, interrupted=None):  # type: ignore[no-untyped-def]
    run_id = compute_run_id(spec.resolved_config, spec.seed)
    with run_lease(campaign_root, run_id, interrupted):
        return execute_run(spec, simulator, campaign_root, workdir, interrupted)


def analyze_from_meta_config(path: Path) -> dict:
    meta = load_meta_config(path)
    _validate_scientific_method(meta)
    if meta.method in {"canonical_morris", "multiscramble_sobol"}:
        # Full-context run identity is an adapter preflight: core analysis has no base config.
        design = _design(meta)
        load_run_specs(meta.campaign_root / "plan.parquet", design, meta.campaign)
    result = analyze(
        meta.campaign_root,
        meta.indicators,
        campaign=meta.campaign,
        completeness_threshold=meta.completeness_threshold,
        seed_role=meta.seed_role,
        method=meta.method,
        bootstrap_resamples=getattr(meta, "bootstrap_resamples", None),
        confidence_level=getattr(meta, "confidence_level", None),
        convergence_checkpoints=getattr(meta, "convergence_checkpoints", None),
        screening_top_k=getattr(meta, "screening_top_k", None),
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
        "convergencePath": str(result.convergence_path)
        if getattr(result, "convergence_path", None)
        else None,
        "rankReversalsPath": str(result.rank_reversals_path)
        if getattr(result, "rank_reversals_path", None)
        else None,
        "byScramblePath": str(result.by_scramble_path)
        if getattr(result, "by_scramble_path", None)
        else None,
        "diagnosticsPath": str(result.diagnostics_path)
        if getattr(result, "diagnostics_path", None)
        else None,
        "error": None,
    }


def _validate_scientific_method(meta) -> None:  # type: ignore[no-untyped-def]
    status = getattr(getattr(meta, "charter", None), "status", "demonstration")
    if status == "scientific" and meta.method not in {"canonical_morris", "multiscramble_sobol"}:
        classification = (
            "a legacy inferential method"
            if meta.method in {"morris", "sobol"}
            else "demonstration-only"
        )
        raise ValueError(
            "no current method satisfies the scientific contract; "
            f"{meta.method} is {classification}"
        )


def _design(meta, requirements=None):  # type: ignore[no-untyped-def]
    _validate_scientific_method(meta)
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
    if getattr(meta, "method", "grid") == "discrete_oat":
        return DiscreteOATDesign(*common, meta.trajectories, meta.design_seed)
    if getattr(meta, "method", "grid") == "canonical_morris":
        distributions = {
            name: {
                "family": distribution.family,
                "transform": distribution.transform,
                "provenance": distribution.provenance,
            }
            for name, distribution in meta.distributions.items()
        }
        contract = serialize_scientific_contract(
            meta.charter, meta.factors, meta.distributions
        )
        return CanonicalMorrisDesign(
            *common,
            meta.trajectories,
            meta.levels,
            meta.candidate_trajectories,
            meta.design_seed,
            distributions,
            contract,
            meta.bootstrap_resamples,
            meta.confidence_level,
            meta.convergence_checkpoints,
            meta.screening_top_k,
        )
    if getattr(meta, "method", "grid") == "sobol":
        return SobolDesign(*common, meta.base_sample_size, meta.design_seed)
    if getattr(meta, "method", "grid") == "multiscramble_sobol":
        full_factors = {**meta.factors, "seed": list(meta.seeds)}
        distributions = {
            name: {
                "family": distribution.family,
                "transform": distribution.transform,
                "provenance": distribution.provenance,
            }
            for name, distribution in meta.distributions.items()
        }
        contract = serialize_scientific_contract(
            meta.charter, full_factors, meta.distributions
        )
        return MultiScrambleSobolDesign(
            *common,
            meta.base_sample_sizes,
            meta.scramble_seeds,
            distributions,
            contract,
            meta.confidence_level,
        )
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
    observations: list[tuple[float, int, int]] = []
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
            scratch_bytes = completion_scratch_bytes(root, spec)
            if duration is None:
                raise RuntimeError(f"completion marker disappeared for {run_id}")
            if scratch_bytes is None:
                raise RuntimeError(f"completion marker disappeared for {run_id}")
            after_entries = tuple(sorted(archive.iterdir(), key=lambda item: item.name))
            after = tuple(
                (entry, _file_state(entry)) for entry in (marker, *after_entries)
            )
            if before != after:
                raise RuntimeError(f"calibration source mutated while reading {run_id}")
            if snapshot is not None:
                snapshot.extend(((archive, archive_state), *before))
            observations.append(
                (duration, sum(state[2] for _, state in before), scratch_bytes)
            )
            if root == target_root:
                target_durable += before[0][1][2] + sum(
                    state[2] for _, state in before[1:]
                )
    target_plan = target_root / "plan.parquet"
    if target_plan_state is None or _file_state(target_plan) != target_plan_state:
        raise RuntimeError(f"target plan mutated while estimating {target_plan}")
    if target.execution is None:
        raise ValueError("execution section is required for estimate")
    payload = estimate_plan(
        design_positions,
        tuple(compute_run_id(spec.resolved_config, spec.seed) for spec in target_specs),
        target_plan_state[2],
        tuple(observations),
        target.execution.workers,
        getattr(target, "method", "grid"),
        (
            {"D": len(target.factors), "T": target.trajectories, "R": len(target.seeds)}
            if getattr(target, "method", "grid")
            in {"morris", "discrete_oat", "canonical_morris"}
            else {"D": len(target.factors) + 1, "N": target.base_sample_size}
            if getattr(target, "method", "grid") == "sobol"
            else {
                "D": len(target.factors) + 1,
                "N": target.base_sample_sizes[-1],
                "S": len(target.scramble_seeds),
            }
            if getattr(target, "method", "grid") == "multiscramble_sobol"
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
                    if getattr(meta, "method", "grid")
                    in {"morris", "discrete_oat", "canonical_morris", "sobol", "multiscramble_sobol"}
                    else unique_config_seed_pairs(design)
                )
                or (_ for _ in ()).throw(
                    ValueError(
                        "sealedPlan validation unavailable"
                        if getattr(meta, "method", "grid")
                        in {"morris", "discrete_oat", "canonical_morris", "sobol", "multiscramble_sobol"}
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
        probe_environment(
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
        scratch_before = _file_state(meta.execution.scratch)
        _remaining(deadline)
        free = shutil.disk_usage(ancestor).free
        scratch_free = shutil.disk_usage(meta.execution.scratch).free
        _remaining(deadline)
        if (
            ancestor.is_symlink()
            or _file_state(ancestor)[:2] != before[:2]
            or _file_state(meta.execution.scratch) != scratch_before
            or any(_file_state(item) != state for item, state in snapshot)
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
        if all(item["status"] == "passed" for item in checks)
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
                execute_run(spec, simulator, campaign_root, simulator.paths.root)
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


INIT_METHODS = ("calibration", "grid", "discrete_oat", "sobol")
INIT_CONFIGS = {
    "calibration": "configs/00-calibration.yml",
    "grid": "configs/01-grid.yml",
    "discrete_oat": "configs/02-discrete-oat.yml",
    "sobol": "configs/03-sobol.yml",
}
INIT_DERIVED = {
    "grid": {
        "indicators.parquet",
        "replication-dispersion.parquet",
        "analysis-generation.json",
    },
    "discrete_oat": {
        "indicators.parquet", "replication-dispersion.parquet",
        "discrete-oat-effects.parquet", "analysis-generation.json",
    },
    "sobol": {"indicators.parquet", "sobol-indices.parquet", "analysis-generation.json"},
}


def _project_name(value: str) -> str:
    from eqasim_sa.adapters.eqasim.config import CAMPAIGN

    if not isinstance(value, str) or len(value) > 64 or not CAMPAIGN.fullmatch(value):
        raise ValueError("project-name must be one safe path segment of 1 to 64 characters")
    return value


def _regular(path: Path, description: str, private: bool = False) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise FileExistsError(f"foreign content in init project: {description}") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or private and stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise FileExistsError(f"foreign content in init project: {description}")


def _validate_project_manifest(path: Path) -> None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or set(document) != {
            "algorithm", "files", "schemaVersion", "treeSha256",
        }:
            raise ValueError
        if (
            type(document["schemaVersion"]) is not int
            or document["schemaVersion"] != 1
            or document["algorithm"] != "sha256"
        ):
            raise ValueError
        records = document["files"]
        if not isinstance(records, list):
            raise ValueError
        paths = []
        for record in records:
            if not isinstance(record, dict) or set(record) != {"path", "size", "sha256"}:
                raise ValueError
            name, size, digest = record["path"], record["size"], record["sha256"]
            if (
                not isinstance(name, str)
                or not name
                or name.startswith("/")
                or "\\" in name
                or "\0" in name
                or any(part in {"", ".", ".."} for part in name.split("/"))
                or type(size) is not int
                or size < 0
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                raise ValueError
            name.encode("utf-8")
            paths.append(name)
        if len(paths) != len(set(paths)) or paths != sorted(
            paths, key=lambda item: item.encode("utf-8")
        ):
            raise ValueError
        canonical = json.dumps(
            records, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        if (
            not isinstance(document["treeSha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", document["treeSha256"]) is None
            or hashlib.sha256(DOMAIN + canonical).hexdigest() != document["treeSha256"]
        ):
            raise ValueError
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("invalid project manifest") from error


def _backup(path: Path, destination: Path, description: str) -> None:
    try:
        info, target = path.lstat(), destination.lstat()
    except OSError as error:
        raise FileExistsError(f"foreign content in init project: {description}") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or not stat.S_ISREG(target.st_mode)
        or info.st_uid != os.getuid()
        or target.st_uid != os.getuid()
        or info.st_nlink != 2
        or (info.st_dev, info.st_ino) != (target.st_dev, target.st_ino)
    ):
        raise FileExistsError(f"foreign content in init project: {description}")


def _control_names(directory: Path) -> set[str]:
    names = set()
    for method in INIT_METHODS:
        root = directory / "campaigns" / method
        digest = hashlib.sha256(os.fsencode(root)).hexdigest()[:16]
        names.update(
            {f".eqasim-sa-plan-{digest}.lock", f".eqasim-sa-campaign-{digest}.lock"}
        )
    return names


def _preflight_project(directory: Path, scratch_relative: Path | None) -> bool:
    if not directory.exists():
        return True
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("directory must not be a file or symlink")
    entries = tuple(directory.iterdir())
    if not entries:
        return True
    expected = {"manifest.json", "README.md", "configs", "campaigns"}
    scratch_top = scratch_relative.parts[0] if scratch_relative else None
    if scratch_top:
        expected.add(scratch_top)
    for entry in entries:
        if entry.is_symlink() or entry.name not in expected:
            if entry.name in {"nantes.yml", "nantes-calibration.yml"}:
                raise FileExistsError("old starter detected; choose a new project directory")
            raise FileExistsError(f"foreign content in init project: {entry.name}")
    _regular(directory / "README.md", "README.md")
    _regular(directory / "manifest.json", "manifest.json")
    for name in {"configs", "campaigns"}:
        path = directory / name
        if path.is_symlink() or not path.is_dir():
            raise FileExistsError(f"foreign content in init project: {name}")
    config_names = {Path(path).name for path in INIT_CONFIGS.values()}
    for entry in (directory / "configs").iterdir():
        if entry.is_symlink() or entry.name not in config_names:
            raise FileExistsError(f"foreign content in init project: configs/{entry.name}")
        _regular(entry, f"configs/{entry.name}")
    if {entry.name for entry in (directory / "configs").iterdir()} != config_names:
        raise FileExistsError("non-empty directory is not a complete init v3 state")
    campaigns = directory / "campaigns"
    controls = _control_names(directory)
    for entry in campaigns.iterdir():
        if entry.is_symlink():
            raise FileExistsError(f"foreign content in init project: campaigns/{entry.name}")
        if entry.name in controls:
            _regular(entry, f"campaigns/{entry.name}", private=True)
        elif entry.name not in INIT_METHODS or not entry.is_dir():
            raise FileExistsError(f"foreign content in init project: campaigns/{entry.name}")
    if scratch_relative and (directory / scratch_relative.parts[0]).exists():
        current = directory
        for index, part in enumerate(scratch_relative.parts):
            parent = current
            current /= part
            if not current.exists():
                if parent != directory and any(parent.iterdir()):
                    raise FileExistsError("foreign content in init scratch")
                break
            if current.is_symlink() or not current.is_dir():
                raise FileExistsError("foreign content in init scratch")
            if index + 1 < len(scratch_relative.parts):
                names = {entry.name for entry in current.iterdir()}
                next_name = scratch_relative.parts[index + 1]
                if names not in (set(), {next_name}):
                    raise FileExistsError("foreign content in init scratch")
    return False


def _init_campaign_allowed(
    root: Path,
    expected_ids: set[str],
    method: str,
    expected_artifacts: set[str] | None = None,
) -> None:
    derived = INIT_DERIVED["grid" if method == "calibration" else method]
    allowed = {
        "plan.parquet", "runs", "archives", "logs", ".eqasim-sa-tmp",
        ".eqasim-sa-run-locks", *derived,
    }
    temporary = re.compile(r"\.([0-9a-f]{32})\.parquet\.([A-Za-z0-9_-]+)\.tmp")
    backup = re.compile(r"\.([0-9a-f]{32})\.parquet\.([A-Za-z0-9_-]+)\.backup")
    for entry in root.iterdir():
        if entry.is_symlink() or entry.name not in allowed:
            raise FileExistsError(f"foreign content in init campaign: {entry.name}")
        if entry.name == "plan.parquet" or entry.name in derived:
            _regular(entry, f"campaigns/{method}/{entry.name}")
            continue
        if not entry.is_dir():
            raise FileExistsError(f"foreign content in init campaign: {entry.name}")
        if entry.name in {".eqasim-sa-tmp", ".eqasim-sa-run-locks"}:
            for child in entry.iterdir():
                match = temporary.fullmatch(child.name)
                valid = child.stem in expected_ids and child.suffix == ".lock"
                if entry.name == ".eqasim-sa-tmp":
                    valid = match is not None and match.group(1) in expected_ids
                if not valid:
                    raise FileExistsError(
                        f"foreign content in init campaign: {entry.name}/{child.name}"
                    )
                _regular(
                    child,
                    f"campaigns/{method}/{entry.name}/{child.name}",
                    private=True,
                )
            continue
        for child in entry.iterdir():
            if entry.name == "runs" and (match := temporary.fullmatch(child.name)):
                if match.group(1) not in expected_ids:
                    raise FileExistsError(
                        f"foreign content in init campaign: {entry.name}/{child.name}"
                    )
                _regular(child, f"campaigns/{method}/runs/{child.name}", private=True)
                continue
            if entry.name == "runs" and (match := backup.fullmatch(child.name)):
                if match.group(1) not in expected_ids:
                    raise FileExistsError(
                        f"foreign content in init campaign: {entry.name}/{child.name}"
                    )
                _backup(
                    child,
                    root / "runs" / f"{match.group(1)}.parquet",
                    f"campaigns/{method}/runs/{child.name}",
                )
                continue
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
            if entry.name != "archives":
                marker_backups = (
                    tuple(
                        candidate
                        for candidate in entry.iterdir()
                        if (match := backup.fullmatch(candidate.name))
                        and match.group(1) == child.stem
                    )
                    if entry.name == "runs"
                    else ()
                )
                if len(marker_backups) == 1:
                    _backup(
                        marker_backups[0],
                        child,
                        f"campaigns/{method}/runs/{marker_backups[0].name}",
                    )
                else:
                    _regular(
                        child,
                        f"campaigns/{method}/{entry.name}/{child.name}",
                        private=entry.name == "logs",
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
                    _regular(
                        artifact,
                        f"campaigns/{method}/archives/{child.name}/{artifact.name}",
                    )


def _scratch_allowed(root: Path, campaigns: set[str]) -> None:
    if root.is_symlink() or not root.is_dir():
        raise FileExistsError("scratch must be a regular non-symlink directory")
    entries = tuple(root.iterdir())
    if not entries:
        return
    if (
        len(entries) != 1
        or entries[0].name != "eqasim-sa"
        or entries[0].is_symlink()
        or not entries[0].is_dir()
    ):
        raise FileExistsError("foreign content in init scratch")
    tool = entries[0]
    for campaign in tool.iterdir():
        if campaign.name not in campaigns or campaign.is_symlink() or not campaign.is_dir():
            raise FileExistsError("foreign content in init scratch")
        campaign_entries = {item.name for item in campaign.iterdir()}
        if campaign_entries - {"workers"}:
            raise FileExistsError("foreign content in init scratch")
        if not campaign_entries:
            continue
        workers = campaign / "workers"
        if workers.is_symlink() or not workers.is_dir():
            raise FileExistsError("foreign content in init scratch")
        for slot in workers.iterdir():
            if slot.is_symlink() or not slot.is_dir() or slot.name not in {"0", "1"}:
                raise FileExistsError("foreign content in init scratch")
            for item in slot.iterdir():
                attempt = re.fullmatch(r"attempt-[0-9a-f]{32}", item.name) is not None
                state_temp = re.fullmatch(r"\.state-[0-9a-f]{32}\.tmp", item.name) is not None
                if item.is_symlink() or (
                    item.name not in {"lease.lock", "state.json", "cache"}
                    and not attempt and not state_temp
                ):
                    raise FileExistsError("foreign content in init scratch")
                if item.name in {"lease.lock", "state.json"}:
                    _regular(item, f"scratch/{campaign.name}/{slot.name}/{item.name}", private=True)
                elif state_temp:
                    _regular(item, f"scratch/{campaign.name}/{slot.name}/{item.name}")
                elif item.name == "cache" or attempt:
                    if attempt and {path.name for path in item.iterdir()} - {
                        "config.yml", "run.log", "output", "normalized"
                    }:
                        raise FileExistsError("foreign content in init scratch")
                    if not item.is_dir() or any(
                        path.is_symlink()
                        or path.stat(follow_symlinks=False).st_uid != os.getuid()
                        or not (path.is_file() or path.is_dir())
                        or path.is_file() and path.stat(follow_symlinks=False).st_nlink != 1
                        for path in item.rglob("*")
                    ):
                        raise FileExistsError("foreign content in init scratch")


def _publish_scaffold(
    destination: Path,
    templates: dict[str, bytes],
    scratch_relative: Path | None,
    data: Path,
) -> None:
    parent = destination.parent.resolve(strict=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=parent))
    try:
        (staging / "configs").mkdir()
        (staging / "campaigns").mkdir()
        if scratch_relative:
            (staging / scratch_relative).mkdir(parents=True)
        for name, content in templates.items():
            target = staging / ("README.md" if name == "README.md" else f"configs/{name}")
            target.write_bytes(content)
        generate_data_manifest(data, staging / "manifest.json")
        if destination.exists():
            if any(destination.iterdir()):
                raise FileExistsError("directory changed while publishing init project")
            destination.rmdir()
        staging.replace(destination)
        staging = None
    finally:
        if staging is not None:
            shutil.rmtree(staging)


def _durable_preflight(directory: Path, expected_artifacts: set[str]) -> None:
    for method in INIT_METHODS:
        root = directory / "campaigns" / method
        if not root.exists():
            continue
        plan = root / "plan.parquet"
        if not plan.exists():
            if any(root.iterdir()):
                raise FileExistsError(f"init campaign {method} has state without a plan")
            continue
        _regular(plan, f"campaigns/{method}/plan.parquet")
        try:
            run_ids = set(pq.read_table(plan, columns=["runId"])["runId"].to_pylist())
        except Exception as error:
            raise FileExistsError(f"invalid init campaign plan: {method}") from error
        if not run_ids or any(
            not isinstance(run_id, str) or re.fullmatch(r"[0-9a-f]{32}", run_id) is None
            for run_id in run_ids
        ):
            raise FileExistsError(f"invalid init campaign plan: {method}")
        _init_campaign_allowed(root, run_ids, method, expected_artifacts)


def init_from_inputs(
    project_name: str,
    directory: Path,
    model_root: Path,
    data_path: Path,
    scratch: Path | None,
    invocation: tuple[str, ...],
    model_config: Path = Path("config_local_nantes.yml"),
    regenerate_data_manifest: bool = False,
) -> dict:
    started = time.monotonic()
    project_name = _project_name(project_name)
    if not invocation or not all(
        isinstance(item, str) and item and "\0" not in item for item in invocation
    ):
        raise ValueError("invocation must contain non-empty arguments")
    _reject_symlink_traversal(directory, "directory")
    absolute = Path(os.path.abspath(directory))
    if not absolute.parent.resolve(strict=True).is_dir():
        raise ValueError("directory parent must be a regular non-symlink directory")
    selected_scratch = (
        absolute / "scratch" if scratch is None else Path(os.path.abspath(scratch))
    )
    if selected_scratch == absolute or absolute.is_relative_to(selected_scratch):
        raise ValueError("directory and scratch must not contain each other in that direction")
    scratch_relative = (
        selected_scratch.relative_to(absolute)
        if selected_scratch.is_relative_to(absolute)
        else None
    )
    if scratch_relative and scratch_relative.parts[0] in {
        "README.md", "configs", "campaigns",
    }:
        raise ValueError("nested scratch must not overlap generated static paths")
    _reject_symlink_traversal(selected_scratch, "scratch")
    model = _directory(model_root, "model-root")
    if (
        absolute == model
        or absolute.is_relative_to(model)
        or model.is_relative_to(absolute)
        or selected_scratch == model
        or selected_scratch.is_relative_to(model)
    ):
        raise ValueError("directory and scratch must not overlap model-root")
    new_project = _preflight_project(absolute, scratch_relative)
    if model_config.is_absolute() or ".." in model_config.parts:
        raise ValueError("model-config must be relative to model-root")
    selected_config = model / model_config
    _reject_symlink_traversal(selected_config, "model-config")
    if selected_config.is_symlink() or not selected_config.is_file():
        raise ValueError(
            "model-config must be a regular non-symlink file under model-root"
        )
    model_document = load_yaml_document(selected_config)
    validate_eqasim_document(model_document)
    model_values = model_document["config"]
    if model_values.get("matching_age_scheme") != "matching_6":
        raise ValueError(
            "config.matching_age_scheme must be explicitly matching_6"
        )
    if model_values.get("analysis_age_scheme") != "analysis_7":
        raise ValueError(
            "config.analysis_age_scheme must be explicitly analysis_7"
        )
    data = _directory(data_path, "data-path")
    campaign_names = {f"{project_name}-{method}" for method in INIT_METHODS}
    if scratch_relative is None and selected_scratch.exists():
        selected_scratch = _directory(selected_scratch, "scratch")
        _scratch_allowed(selected_scratch, campaign_names)
    expected = render_project(
        absolute, project_name, model, data, selected_scratch,
        invocation, model_config,
    )
    expected_artifacts = {
        f"{name}.parquet" for name in (*POPULATION, *ANALYSIS)
    }
    if new_project:
        created_external_scratch = False
        if scratch_relative is None:
            created_external_scratch = not selected_scratch.exists()
            selected_scratch = _directory_or_create(selected_scratch, "scratch")
        try:
            _publish_scaffold(absolute, expected, scratch_relative, data)
        except BaseException:
            if created_external_scratch:
                try:
                    selected_scratch.rmdir()
                except OSError:
                    pass
            raise
    else:
        actual = {"README.md": (absolute / "README.md").read_bytes()}
        actual.update(
            {name: (absolute / "configs" / name).read_bytes() for name in expected if name != "README.md"}
        )
        if actual != expected:
            raise FileExistsError("init templates do not match the requested inputs")
        if not regenerate_data_manifest:
            _validate_project_manifest(absolute / "manifest.json")
        _durable_preflight(absolute, expected_artifacts)
        selected_scratch = _directory_or_create(selected_scratch, "scratch")
    _scratch_allowed(selected_scratch, campaign_names)
    manifest = absolute / "manifest.json"
    if not new_project and regenerate_data_manifest:
        generate_data_manifest(data, manifest, replace=True)
    return {
        "status": "initialized" if new_project or regenerate_data_manifest else "unchanged",
        "initVersion": 3,
        "projectName": project_name,
        "manifest": "manifest.json",
        "readme": "README.md",
        "metaConfigs": dict(INIT_CONFIGS),
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
