"""Fill a meta-config document with default values (pure, validates nothing)."""

from __future__ import annotations

import copy
import hashlib

DEFAULT_INVOCATION: tuple[str, ...] = ("uv", "run", "--locked", "python", "-m", "synpp")

_KEY_ORDER = (
    "campaign", "campaign_root", "charter", "model", "experiment", "parameters",
    "analysis", "outputs", "indicators", "execution", "calibration",
)
_SEEDED_METHODS = {"discrete_oat", "canonical_morris", "sobol"}
_BOOTSTRAP_METHODS = {"sobol", "canonical_morris"}
_CONFIDENCE_METHODS = {"sobol", "canonical_morris", "multiscramble_sobol"}


def default_design_seed(campaign: str) -> int:
    digest = hashlib.sha256(b"eqasim-sa:design-seed:v1\0" + campaign.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") >> 1


def resolve_meta_config_document(document: object) -> object:
    resolved = copy.deepcopy(document)
    if not isinstance(resolved, dict):
        return resolved

    campaign = resolved.get("campaign")
    if isinstance(campaign, str):
        resolved.setdefault("campaign_root", f"../campaigns/{campaign}")

    model = resolved.get("model")
    if isinstance(model, dict) and isinstance(model.get("root"), str):
        model.setdefault("lockfile", model["root"].rstrip("/\\") + "/uv.lock")

    experiment = resolved.get("experiment")
    method = experiment.get("method") if isinstance(experiment, dict) else None
    if isinstance(experiment, dict) and method in _SEEDED_METHODS and isinstance(campaign, str):
        experiment.setdefault("design_seed", default_design_seed(campaign))

    parameters = resolved.get("parameters")
    if isinstance(parameters, dict):
        for name, value in list(parameters.items()):
            if isinstance(value, list):
                parameters[name] = {"values": value}

    analysis = resolved.get("analysis")
    if analysis is None and "analysis" not in resolved:
        analysis = resolved["analysis"] = {}
    if isinstance(analysis, dict):
        analysis.setdefault("completeness_threshold", 1.0)
        if method in _BOOTSTRAP_METHODS:
            analysis.setdefault("bootstrap_resamples", 999)
        if method in _CONFIDENCE_METHODS:
            analysis.setdefault("confidence_level", 0.95)

    if "indicators" not in resolved:
        outputs = resolved.get("outputs")
        if isinstance(outputs, dict) and outputs:
            resolved["indicators"] = [{"tv_distance": {"outputs": list(outputs)}}]
        else:
            resolved["indicators"] = []

    execution = resolved.get("execution")
    if isinstance(execution, dict):
        execution.setdefault("backend", "local")
        execution.setdefault("workers", 1)
        execution.setdefault("invocation", list(DEFAULT_INVOCATION))

    ordered = {key: resolved[key] for key in _KEY_ORDER if key in resolved}
    ordered.update({key: value for key, value in resolved.items() if key not in ordered})
    return ordered
