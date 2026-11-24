"""Fill a meta-config document with default values (pure, validates nothing)."""

from __future__ import annotations

import copy
import hashlib

from eqasim_sa.adapters.eqasim.artifacts import (
    DEFAULT_OUTPUT_FORMATS,
    KNOWN_OUTPUTS,
    is_csv_file,
)

DEFAULT_INVOCATION: tuple[str, ...] = ("uv", "run", "--locked", "python", "-m", "synpp")
_SYNPP_MODULE = ("-m", "synpp")

_KEY_ORDER = (
    "campaign", "campaign_root", "charter", "model", "experiment", "parameters",
    "analysis", "outputs", "analyses", "indicators", "execution", "calibration",
)
_SEEDED_METHODS = {"discrete_oat", "canonical_morris", "sobol"}
_BOOTSTRAP_METHODS = {"sobol", "canonical_morris"}
_CONFIDENCE_METHODS = {"sobol", "canonical_morris", "multiscramble_sobol"}
UNKNOWN_OUTPUT_SEPARATOR = ","


def default_design_seed(campaign: str) -> int:
    digest = hashlib.sha256(b"eqasim-sa:design-seed:v1\0" + campaign.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") >> 1


def output_formats(eqasim_config: object) -> object:
    """eqasim ``output_formats`` of a config block, with eqasim's default.

    ``None`` when the config block is unknown.
    """

    if not isinstance(eqasim_config, dict):
        return None
    return eqasim_config.get("output_formats", list(DEFAULT_OUTPUT_FORMATS))


def mode_choice(eqasim_config: object) -> bool:
    """eqasim ``mode_choice`` of a config block, read as eqasim does (any true value)."""

    if not isinstance(eqasim_config, dict):
        return False
    return bool(eqasim_config.get("mode_choice", False))


def resolve_meta_config_document(
    document: object, eqasim_config: object = None
) -> object:
    """Return ``document`` with every default applied.

    ``eqasim_config`` (the ``config:`` block of the eqasim config) gives
    ``output_formats``, which chooses the file of a spatial output; without
    it, a spatial output declared without ``file`` is left as written.
    """

    formats = output_formats(eqasim_config)
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
    if not isinstance(method, str):
        method = None
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

    outputs = resolved.get("outputs")
    if isinstance(outputs, dict):
        for name, output in outputs.items():
            if output is None:
                output = outputs[name] = {}
            if not isinstance(output, dict):
                continue
            known = KNOWN_OUTPUTS.get(name) if isinstance(name, str) else None
            if known is not None and "file" not in output:
                file = known.default_file(formats)
                if file is not None:
                    output["file"] = file
            file = output.get("file")
            # Only a CSV table has a separator.
            if isinstance(file, str) and is_csv_file(file):
                default = (
                    known.separator
                    if known is not None and known.separator is not None
                    else UNKNOWN_OUTPUT_SEPARATOR
                )
                output.setdefault("separator", default)

    if "indicators" not in resolved:
        referenced = (
            [
                name
                for name, output in outputs.items()
                if isinstance(output, dict)
                and isinstance(output.get("references"), list)
                and output["references"]
            ]
            if isinstance(outputs, dict)
            else []
        )
        # An output without references has no output x reference pair.
        resolved["indicators"] = (
            [{"tv_distance": {"outputs": referenced}}] if referenced else []
        )

    execution = resolved.get("execution")
    if isinstance(execution, dict):
        execution.setdefault("backend", "local")
        execution.setdefault("workers", 1)
        execution.setdefault("invocation", list(DEFAULT_INVOCATION))
        # Same rule as the loader: the invocation without its final -m synpp.
        # Nothing is written when no interpreter can be derived.  Without an
        # execution block the analysis default ([uv, run, --locked, python])
        # cannot be written: an execution block requires scratch.
        invocation = execution["invocation"]
        if (
            "python" not in execution
            and isinstance(invocation, list)
            and len(invocation) > len(_SYNPP_MODULE)
            and tuple(invocation[-len(_SYNPP_MODULE):]) == _SYNPP_MODULE
        ):
            execution["python"] = invocation[: -len(_SYNPP_MODULE)]

    ordered = {key: resolved[key] for key in _KEY_ORDER if key in resolved}
    ordered.update({key: value for key, value in resolved.items() if key not in ordered})
    return ordered
