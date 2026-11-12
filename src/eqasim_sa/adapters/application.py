"""Application services that connect CLI commands to campaign workflows."""

import hashlib
import json
import sys
from argparse import Namespace
from pathlib import Path

import pyarrow.parquet as pq

from eqasim_sa.adapters.backends.local import LocalBackend
from eqasim_sa.adapters.campaign_design import _design, _validate_scientific_method
from eqasim_sa.adapters.checks import check_from_meta_config
from eqasim_sa.adapters.data_manifest import load_data_manifest
from eqasim_sa.adapters.eqasim.config import (
    EqasimSettings,
    load_config,
    load_yaml_document,
)
from eqasim_sa.adapters.eqasim.execution import (
    EqasimBatchExecutor,
    _apply_execution_overrides,
)
from eqasim_sa.adapters.eqasim.simulator import (
    DEDICATED,
    EqasimSimulator,
    eqasim_requirements,
    validate_transport,
)
from eqasim_sa.adapters.fake.simulator import FakeSimulator
from eqasim_sa.adapters.estimate import estimate_from_meta_config
from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.adapters.project_init import init_from_inputs
from eqasim_sa.adapters.worker import ordered_results, worker_batches
from eqasim_sa.cli import dispatch
from eqasim_sa.core.analyze import analyze
from eqasim_sa.core.context_identity import ExecutionContextIdentity
from eqasim_sa.core.plan import (
    PlanResult,
    build_plan,
    load_run_specs,
)
from eqasim_sa.adapters.execution_seal import create_execution_seal
from eqasim_sa.core.campaign_snapshot import CampaignSnapshot
from eqasim_sa.ports.simulator import ExecutionEvidence
from eqasim_sa.core.run import (
    require_same_machine,
    EXCLUDED_PATHS,
    compute_run_id,
    pending_run_specs,
    summarize_runs,
)
from eqasim_sa.core.software_identity import verify_software_source_states


def build_eqasim(arguments: Namespace) -> tuple[EqasimSimulator, dict, object]:
    """Construct an eqasim simulator from the internal worker CLI arguments."""

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
        arguments.current_context_hash,
    )
    config = load_config(settings.config_path, arguments.seed)
    simulator = EqasimSimulator(settings, config)
    return simulator, config, simulator.workspace.root


def plan_from_meta_config(path: Path) -> PlanResult:
    """Validate a meta-config and publish its deterministic campaign plan."""

    meta = load_meta_config(path)
    try:
        context_identity = ExecutionContextIdentity.from_descriptor(
            meta.context_descriptor
        )
        context_identity.require_sha256(meta.context_hash)
    except ValueError as error:
        raise ValueError("context descriptor does not match contextHash")
    verify_software_source_states(meta.software_identity)
    requirements = dict(eqasim_requirements(meta.indicator_requirements))
    config_requirements = requirements.get("config", {})
    if not isinstance(config_requirements, dict):
        raise TypeError("requirements config must be a mapping")
    for name, value in config_requirements.items():
        if f"config.{name}" in EXCLUDED_PATHS or name in DEDICATED:
            raise ValueError(f"requirements contain infrastructure option: {name!r}")
        validate_transport(name, value)
    result = build_plan(_design(meta, requirements))
    verify_software_source_states(meta.software_identity)
    return result


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
    """Resume all pending runs using the configured local or SLURM backend."""

    meta_path = path.resolve(strict=True)
    meta_bytes = meta_path.read_bytes()
    meta = load_meta_config(meta_path)
    if meta_path.read_bytes() != meta_bytes:
        raise RuntimeError("meta-config changed while preparing execution")
    if meta.execution is None:
        raise ValueError("execution section is required for run")
    if getattr(meta.execution, "backend", None) == "slurm" and sys.platform != "linux":
        raise ValueError(
            "the slurm backend requires Linux; run eqasim-sa on the cluster"
        )
    EqasimBatchExecutor.validate(meta)
    verify_software_source_states(meta.software_identity)
    if getattr(meta.execution, "data_path", None) is not None:
        load_data_manifest(meta.data_manifest)
    requirements = dict(eqasim_requirements(meta.indicator_requirements))
    design = _design(meta, requirements)
    specs = load_run_specs(meta.campaign_root / "plan.parquet", design, meta.campaign)
    unique_runs = len(specs)
    meta.campaign_root.resolve(strict=True)
    seal = None
    existing = CampaignSnapshot.completed_evidence(meta.campaign_root, specs)
    if (
        any(evidence is not None for evidence in existing.values())
        and getattr(meta.execution, "data_path", None) is not None
    ):
        seal = create_execution_seal(meta)
        require_same_machine(existing, ExecutionEvidence.from_bytes(seal.evidence))
    specs = pending_run_specs(meta.campaign_root, specs)
    if not specs:
        return summarize_runs([], [])
    if meta.method in {"sobol", "multiscramble_sobol"} and len(specs) > meta.execution.workers:
        positions = pq.ParquetFile(meta.campaign_root / "plan.parquet").metadata.num_rows
        _warn_sobol_capacity(
            positions,
            unique_runs,
            len(specs),
            meta.execution.backend,
            meta.execution.workers,
            meta.method,
        )
    specs = _apply_execution_overrides(specs, meta.execution.overrides)
    if meta.execution.backend == "local":
        executor = EqasimBatchExecutor(meta, execution_seal=seal)
        batches = worker_batches(specs, meta.execution.workers)
        results = ordered_results(
            list(
                LocalBackend(len(batches)).map(
                    executor.execute, batches, executor.interrupt
                )
            ),
            len(specs),
        )
    else:
        from eqasim_sa.adapters.backends.slurm import EqasimSlurmJob, SlurmBackend

        slurm = meta.execution.slurm
        parameters: dict[str, object] = {
            "timeout_min": slurm.timeout_min,
            "cpus_per_task": meta.execution.processes,
        }
        if slurm.mem_gb is not None:
            parameters["mem_gb"] = slurm.mem_gb
        for name in ("partition", "account", "qos"):
            value = getattr(slurm, name)
            if value is not None:
                parameters[f"slurm_{name}"] = value
        backend = SlurmBackend(
            meta.campaign_root,
            meta.execution.workers,
            parameters,
        )
        job = EqasimSlurmJob(
            meta_path,
            meta.context_hash,
            hashlib.sha256(meta_bytes).hexdigest(),
            meta.eqasim_sa_sha256,
        )
        batches = worker_batches(specs, meta.execution.workers)
        results = ordered_results(list(backend.map(job, batches)), len(specs))
    return summarize_runs(
        [compute_run_id(spec.resolved_config, spec.seed) for spec in specs], results
    )


def _legacy_scientific_plan(path: Path) -> tuple[str, int] | None:
    try:
        metadata = pq.ParquetFile(path).schema_arrow.metadata or {}
    except FileNotFoundError:
        return None
    found = []
    for method, version_key, design_key in (
        (
            "canonical_morris",
            b"canonicalMorrisDesignVersion",
            b"canonicalMorrisDesign",
        ),
        (
            "multiscramble_sobol",
            b"multiScrambleSobolDesignVersion",
            b"multiScrambleSobolDesign",
        ),
    ):
        version_present = version_key in metadata
        design_present = design_key in metadata
        if version_present != design_present:
            raise ValueError(f"plan has incomplete {method} version metadata")
        if not version_present:
            continue
        version = metadata[version_key]
        if version not in {b"1", b"2", b"3"}:
            raise ValueError(f"plan has unsupported {method} design version")
        found.append((method, int(version)))
    if len(found) > 1:
        raise ValueError("plan declares multiple scientific methods")
    return found[0] if found and found[0][1] in {1, 2} else None


def _legacy_plan_from_meta_path(path: Path) -> tuple[str, tuple[str, int]] | None:
    try:
        source = path.resolve(strict=True)
        document = load_yaml_document(source)
    except FileNotFoundError:
        return None
    if not isinstance(document, dict):
        return None
    campaign_root = document.get("campaign_root")
    experiment = document.get("experiment")
    if not isinstance(campaign_root, str) or not isinstance(experiment, dict):
        return None
    method = experiment.get("method")
    if not isinstance(method, str):
        return None
    legacy = _legacy_scientific_plan(
        source.parent / campaign_root / "plan.parquet"
    )
    return (method, legacy) if legacy is not None else None


def analyze_from_meta_config(path: Path, descriptive_only: bool = False) -> dict:
    """Analyze a campaign after enforcing current or legacy plan constraints."""

    if type(descriptive_only) is not bool:
        raise TypeError("descriptive_only must be a bool")
    early = _legacy_plan_from_meta_path(path)
    if early is not None:
        declared_method, (legacy_method, legacy_version) = early
        if declared_method != legacy_method:
            raise ValueError(
                f"legacy {legacy_method} plan does not match declared method {declared_method}"
            )
        if not descriptive_only:
            raise ValueError(
                f"{legacy_method} design v{legacy_version} is legacy and cannot support "
                "inference; replan the campaign with the current SALib design v3"
            )
    meta = load_meta_config(path, verify_data=False)
    _validate_scientific_method(meta)
    plan_path = meta.campaign_root / "plan.parquet"
    legacy = _legacy_scientific_plan(plan_path)
    if legacy is not None and legacy[0] != meta.method:
        raise ValueError(
            f"legacy {legacy[0]} plan does not match declared method {meta.method}"
        )
    if legacy is not None and not descriptive_only:
        raise ValueError(
            f"{legacy[0]} design v{legacy[1]} is legacy and cannot support inference; "
            "replan the campaign with the current SALib design v3"
        )
    if legacy is None:
        # Full-context run identity is an adapter preflight: core analysis has no base config.
        design = _design(meta)
        load_run_specs(plan_path, design, meta.campaign)
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
        meta_config_sha256=getattr(meta, "meta_config_sha256", None),
        descriptive_only=descriptive_only,
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
        "noiseFloorPath": str(result.noise_floor_path)
        if getattr(result, "noise_floor_path", None)
        else None,
        "replicationAllocationPath": str(result.replication_allocation_path)
        if getattr(result, "replication_allocation_path", None)
        else None,
        "outputSummaryPath": str(result.output_summary_path)
        if getattr(result, "output_summary_path", None)
        else None,
        "convergenceEvidencePath": str(result.convergence_evidence_path)
        if getattr(result, "convergence_evidence_path", None)
        else None,
        "governedReportPath": str(result.governed_report_path)
        if getattr(result, "governed_report_path", None)
        else None,
        "inferenceStatus": getattr(result, "inference_status", None),
        "error": None,
    }


def main() -> int:
    """Run the production CLI and translate user interruption to exit code 130."""

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
