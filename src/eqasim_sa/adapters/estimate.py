"""Estimate campaign resource needs from sealed campaign snapshots."""

import time
from pathlib import Path

from eqasim_sa.adapters.campaign_design import _design
from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.core.campaign_snapshot import CampaignSnapshot
from eqasim_sa.core.plan import estimate_plan, load_run_specs
from eqasim_sa.core.run import compute_run_id
from eqasim_sa.utils.filesystem import path_state


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
        try:
            plan_snapshot = CampaignSnapshot.inspect_plan(plan)
            plan_state = plan_snapshot.source.state
            specs = load_run_specs(plan, _design(meta), meta.campaign)
            _remaining(deadline)
            campaign_snapshot = CampaignSnapshot.capture(
                root,
                plan,
                meta.campaign,
                allow_legacy_unbound=True,
                inspected_plan=plan_snapshot,
            )
        except RuntimeError as error:
            if str(plan) in str(error):
                raise RuntimeError(f"plan mutated while estimating {plan}") from error
            raise RuntimeError(
                f"calibration source mutated while reading {root}"
            ) from error
        positions = len(campaign_snapshot.plan_table)
        if root == target_root:
            target_specs, target_plan_state, design_positions = (
                specs,
                plan_state,
                positions,
            )
        if snapshot is not None:
            snapshot.extend(campaign_snapshot.tracked_states)
        observations.extend(campaign_snapshot.measurements)
        if root == target_root:
            target_durable += campaign_snapshot.durable_bytes
    target_plan = target_root / "plan.parquet"
    if target_plan_state is None or path_state(target_plan) != target_plan_state:
        raise RuntimeError(f"target plan mutated while estimating {target_plan}")
    if target.execution is None:
        raise ValueError("execution section is required for estimate")
    payload = estimate_plan(
        design_positions,
        tuple(compute_run_id(spec.resolved_config, spec.seed) for spec in target_specs),
        target_plan_state.size,
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
    durable = target_plan_state.size + target_durable
    return payload, target, target_specs, durable


def estimate_from_meta_config(path: Path, calibration_paths: tuple[Path, ...]) -> dict:
    target = load_meta_config(path)
    calibrations = tuple(
        dict.fromkeys((*calibration_paths, *target.calibration_meta_configs))
    )
    return _estimate_state(path, calibrations, target)[0]
