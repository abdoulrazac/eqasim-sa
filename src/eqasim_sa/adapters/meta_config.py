"""Parse and validate campaign meta-configuration files and identities."""

import hashlib
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from eqasim_sa.core.ledger import archive_file_name
from eqasim_sa.adapters.eqasim.artifacts import (
    DEFAULT_OUTPUT_PREFIX,
    KNOWN_OUTPUTS,
    is_csv_file,
)
from eqasim_sa.adapters.eqasim.source_fingerprint import (
    eqasim_source_exclusions,
    model_source_sha256,
)
from eqasim_sa.utils.names import reject_case_collisions, validate_portable_name
from eqasim_sa.adapters.eqasim.config import (
    CAMPAIGN,
    load_yaml_document,
    load_yaml_source,
    run_stages,
    validate_eqasim_document,
    yaml_error_reason,
)
from eqasim_sa.adapters.eqasim.workspace import CAMPAIGN_MAX
from eqasim_sa.adapters.meta_config_defaults import (
    UNKNOWN_OUTPUT_SEPARATOR,
    output_formats,
    resolve_meta_config_document,
)
from eqasim_sa.adapters.eqasim.simulator import DEDICATED, validate_transport
from eqasim_sa.core.charter import (
    AllocationPhase,
    AnalysisCharter,
    CharterStatus,
    ConditionalMeanStochastic,
    FiniteSeedFactorStochastic,
    ParameterDistribution,
    charter_status,
    parse_charter,
    parse_distribution,
    validate_scientific_stopping,
)
from eqasim_sa.core.analyses import ANALYSES_LOG_DIRECTORY, AnalysisDeclaration
from eqasim_sa.core.context_identity import ExecutionContextIdentity
from eqasim_sa.utils.filesystem import FileState, path_state
from eqasim_sa.utils import platform
from eqasim_sa.utils.text_identity import canonical_json_sha256, lf_sha256
from eqasim_sa.core.indicators import (
    DerivedOutput,
    Indicator,
    OutputDeclaration,
    PairDeclaration,
    ProductDeclaration,
    ShareIndicator,
    TableArtifact,
    TableSource,
    TotalVariationIndicator,
    ValueIndicator,
    analysis_identity,
    resolve_indicators,
    validate_output_file,
    validate_output_name,
    validate_separator,
)
from eqasim_sa.core.plan import (
    MAX_CANONICAL_MORRIS_CANDIDATES,
    SealedPlanInputs,
    sealed_inputs,
    MAX_CANONICAL_MORRIS_DISTANCE_TERMS,
    canonical_morris_distance_terms,
    validate_bootstrap_resamples,
    validate_canonical_morris_bootstrap_work,
    validate_discrete_oat_values,
)
from eqasim_sa.core.methods.multiscramble_sobol_plan import MAX_MULTISCRAMBLE_DESIGN_POSITIONS, MAX_MULTISCRAMBLE_TABLE_CELLS
from eqasim_sa.core.methods.sobol_plan import validate_sobol_bootstrap_work
from eqasim_sa.core.run import EXCLUDED_PATHS
from eqasim_sa.core.software_identity import (
    SoftwareIdentity,
    read_software_identity,
    verify_software_source_states,
)

EXPERIMENT_METHODS = (
    "grid",
    "discrete_oat",
    "canonical_morris",
    "sobol",
    "multiscramble_sobol",
)
SCALARIZERS = {
    "tv_distance": lambda artifact, reference: TotalVariationIndicator(
        artifact, reference
    ),
}
ROOT_KEYS = (
    "campaign",
    "campaign_root",
    "charter",
    "model",
    "experiment",
    "parameters",
    "analysis",
    "outputs",
    "analyses",
    "indicators",
    "execution",
    "calibration",
)
ROOT_REQUIRED = {"campaign", "model", "experiment", "parameters"}
MODEL_KEYS = ("root", "config", "data_manifest", "lockfile")
EXECUTION_KEYS = (
    "backend",
    "workers",
    "scratch",
    "invocation",
    "python",
    "machine_options",
    "slurm",
)
EXPERIMENT_KEYS = {
    "grid": ("method", "seeds"),
    "discrete_oat": ("method", "seeds", "trajectories", "design_seed"),
    "canonical_morris": (
        "method",
        "seeds",
        "levels",
        "trajectories",
        "candidate_trajectories",
        "design_seed",
    ),
    "sobol": ("method", "seeds", "base_sample_size", "design_seed"),
    "multiscramble_sobol": (
        "method",
        "seeds",
        "base_sample_sizes",
        "scramble_seeds",
        "seeds_provenance",
    ),
}
OUTPUT_KEYS = ("file", "separator", "category", "simulated", "references")
ANALYSIS_KEYS = ("script", "inputs", "category", "simulated", "references")
ANALYSIS_INPUT_SUFFIXES = (".csv", ".gpkg", ".parquet", ".geoparquet")
FORMAT_KINDS = {
    "csv": "a CSV",
    "parquet": "a Parquet",
    "gpkg": "a GeoPackage",
    "geoparquet": "a GeoParquet",
}
# Only eqasim's synthesis/output.py writes these, whatever the output name.
SPATIAL_SUFFIX_FORMATS = {".gpkg": "gpkg", ".geoparquet": "geoparquet"}
DEFAULT_ANALYSIS_PYTHON = ("uv", "run", "--locked", "python")
SYNPP_MODULE = ("-m", "synpp")
SEED_FACTOR_METHODS = {"sobol", "multiscramble_sobol"}


@dataclass(frozen=True)
class SlurmResources:
    """Scheduler resource options accepted by the SLURM adapter."""

    timeout_min: int = 1440
    mem_gb: int | float | None = None
    partition: str | None = None
    account: str | None = None
    qos: str | None = None


@dataclass(frozen=True)
class Execution:
    """Validated runtime, backend, workspace, and machine_options override."""

    backend: str
    workers: int
    processes: int
    scratch: Path
    invocation: tuple[str, ...]
    overrides: dict[str, object]
    slurm: SlurmResources
    data_path: Path | None
    python: tuple[str, ...] | None = None


@dataclass(frozen=True)
class MetaConfig:
    """Fully resolved campaign contract consumed by application services."""

    campaign: str
    campaign_root: Path
    model_config: Path
    model_root: Path
    data_manifest: Path
    factors: dict[str, list]
    distributions: dict[str, ParameterDistribution]
    charter: AnalysisCharter
    seeds: tuple[int, ...]
    seed_role: str
    method: str
    trajectories: int | None
    levels: int | None
    candidate_trajectories: int | None
    design_seed: int | None
    base_sample_size: int | None
    base_sample_sizes: tuple[int, ...] | None
    scramble_seeds: tuple[int, ...] | None
    bootstrap_resamples: int | None
    confidence_level: float | None
    convergence_checkpoints: tuple[int, ...] | None
    screening_top_k: int | None
    completeness_threshold: float
    context_descriptor: dict[str, str]
    context_hash: str
    meta_config_sha256: str
    model_document: dict
    model_bytes: bytes
    execution: Execution | None
    calibration_meta_configs: tuple[Path, ...]
    control_states: tuple[tuple[Path, FileState], ...]
    outputs: dict[str, OutputDeclaration]
    indicators: tuple[Indicator, ...]
    software_identity: SoftwareIdentity
    model_source_exclusions: tuple[Path, ...]
    structured_factors: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    sealed: SealedPlanInputs | None = None
    analyses: dict[str, AnalysisDeclaration] = field(default_factory=dict)
    analysis_python: tuple[str, ...] = ()

    @property
    def eqasim_sa_version(self) -> str:
        return self.software_identity.version

    @property
    def eqasim_sa_sha256(self) -> str:
        return self.software_identity.sha256


def _mapping(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be a mapping")
    return value


def _require_keys(
    document: dict, name: str, allowed: tuple[str, ...], required: set[str]
) -> None:
    missing = sorted(required - set(document))
    unknown = sorted(str(key) for key in document if key not in allowed)
    if missing or unknown:
        problems = (
            [f"missing required: {', '.join(missing)}"] if missing else []
        ) + ([f"unknown: {', '.join(unknown)}"] if unknown else [])
        raise ValueError(
            f"{name} keys must be among {', '.join(allowed)}; {'; '.join(problems)}"
        )


def _relative_note(value: str, base: str) -> str:
    return "" if Path(value).is_absolute() else f" (a relative path is read from {base})"


def _file(path: Path, name: str, shown: str | None = None) -> Path:
    """Resolve a regular file; a missing one is named as ``shown`` (as written)."""

    if platform.is_link(path):
        raise ValueError(f"{name} must not be a symlink")
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"{name} not found: {path if shown is None else shown}"
        ) from None
    if not resolved.is_file():
        raise ValueError(f"{name} must be a regular file")
    return resolved


def _yaml_file(path: Path, name: str, shown: str) -> object:
    try:
        return load_yaml_document(path)
    except yaml.YAMLError as error:
        raise ValueError(
            f"cannot parse {name} {shown}: {yaml_error_reason(error)}"
        ) from None


def _timeout(deadline: float | None) -> float:
    if deadline is None:
        return 5
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("static check time budget exhausted")
    return min(5, remaining)


def _digest(path: Path, deadline: float | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            _timeout(deadline)
            digest.update(chunk)
    return digest.hexdigest()


def _campaign_root(path: Path, model_root: Path) -> Path:
    candidate = Path(os.path.abspath(path))
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        if current.exists() and platform.is_link(current):
            raise ValueError("campaign_root must not traverse a symlink")
    ancestor = candidate
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if platform.is_link(ancestor) or not ancestor.is_dir():
        raise ValueError("campaign_root ancestor must be a non-symlink directory")
    resolved = ancestor.resolve(strict=True).joinpath(
        *candidate.relative_to(ancestor).parts
    )
    if resolved == model_root or resolved.is_relative_to(model_root):
        raise ValueError("campaign_root must be outside model.root")
    return resolved


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _resolved_directory(path: Path, name: str, *, must_exist: bool = True) -> Path:
    if not must_exist:
        # Analysis may run where the execution directories were never created.
        return Path(os.path.realpath(path))
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.exists() and platform.is_link(current):
            raise ValueError(f"{name} must not traverse a symlink")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(f"{name} must be an existing directory")
    return resolved


def _execution(
    value: object,
    base: Path,
    model_root: Path,
    model_document: dict,
    verify_data: bool = True,
) -> Execution:
    document = _mapping(value, "execution")
    _require_keys(
        document,
        "execution",
        EXECUTION_KEYS,
        {"backend", "workers", "scratch", "invocation"},
    )
    backend, workers = document["backend"], document["workers"]
    scratch, invocation = document["scratch"], document["invocation"]
    if backend not in {"local", "slurm"}:
        raise ValueError("execution.backend must be local or slurm")
    if (
        isinstance(workers, bool)
        or not isinstance(workers, int)
        or not 0 < workers <= 1024
    ):
        raise ValueError("execution.workers must be an integer in [1,1024]")
    if not isinstance(scratch, str):
        raise TypeError("execution.scratch must be a string path")
    if not scratch or "\0" in scratch:
        raise ValueError("execution.scratch must be a non-empty path without NUL")
    scratch_path = _resolved_directory(
        base / scratch, "execution.scratch", must_exist=verify_data
    )
    if (
        not isinstance(invocation, list)
        or not invocation
        or not all(
            isinstance(item, str) and item and "\0" not in item for item in invocation
        )
    ):
        raise ValueError(
            "execution.invocation must be a non-empty list of non-empty strings without NUL"
        )
    python = document.get("python")
    if python is not None and (
        not isinstance(python, list)
        or not python
        or not all(isinstance(item, str) and item and "\0" not in item for item in python)
    ):
        raise ValueError(
            "execution.python must be a non-empty list of non-empty strings without NUL"
        )
    overrides = _mapping(
        document.get("machine_options", {}), "execution.machine_options"
    )
    model_config = _mapping(model_document.get("config"), "model config")
    validated = {}
    for name, value in overrides.items():
        if (
            not isinstance(name, str)
            or f"config.{name}" not in EXCLUDED_PATHS
            or name in DEDICATED
        ):
            raise ValueError(
                f"machine_options {name!r} is not an allowed environment option"
            )
        if name not in model_config:
            raise ValueError(f"machine_options {name!r} is absent from the sealed YAML")
        if type(value) is not type(model_config[name]):
            raise TypeError(
                f"machine_options {name!r} must preserve the sealed YAML type"
            )
        validate_transport(name, value, f"machine_options {name!r}")
        if name == "data_path":
            validated[name] = _resolved_directory(
                base / value, "machine_options 'data_path'", must_exist=verify_data
            )
        else:
            validated[name] = value
    processes = validated.get("processes", model_config.get("processes", 1))
    if type(processes) is not int or processes <= 0:
        raise ValueError(
            "effective execution processes must be a strictly positive integer"
        )
    effective_data_path = validated.get("data_path", model_config.get("data_path"))
    if effective_data_path is None:
        data_path = None
    elif isinstance(effective_data_path, Path):
        data_path = effective_data_path
    elif isinstance(effective_data_path, str):
        if not effective_data_path or "\0" in effective_data_path:
            raise ValueError("effective execution data_path must be a non-empty path")
        data_path = _resolved_directory(
            model_root / effective_data_path,
            "effective execution data_path",
            must_exist=verify_data,
        )
    else:
        raise TypeError("effective execution data_path must be a string path")
    if _overlap(scratch_path, model_root):
        raise ValueError("execution.scratch must not overlap model.root")
    if data_path is not None and _overlap(scratch_path, data_path):
        raise ValueError("execution.scratch must not overlap the effective data tree")
    raw_slurm = _mapping(document.get("slurm", {}), "execution.slurm")
    allowed_slurm = {"timeout_min", "mem_gb", "partition", "account", "qos"}
    if set(raw_slurm) - allowed_slurm:
        raise ValueError(
            "execution.slurm keys must be timeout_min, mem_gb, partition, account, or qos"
        )
    if backend != "slurm" and "slurm" in document:
        raise ValueError("execution.slurm requires execution.backend slurm")
    timeout_min = raw_slurm.get("timeout_min", 1440)
    if (
        isinstance(timeout_min, bool)
        or not isinstance(timeout_min, int)
        or timeout_min <= 0
    ):
        raise ValueError("execution.slurm.timeout_min must be a positive integer")
    mem_gb = raw_slurm.get("mem_gb")
    if mem_gb is not None and (
        isinstance(mem_gb, bool)
        or not isinstance(mem_gb, (int, float))
        or not math.isfinite(mem_gb)
        or mem_gb < 1 / 1024
    ):
        raise ValueError("execution.slurm.mem_gb must be at least 1/1024 GiB")
    strings = {}
    for name in ("partition", "account", "qos"):
        resource = raw_slurm.get(name)
        if resource is not None and (
            not isinstance(resource, str) or CAMPAIGN.fullmatch(resource) is None
        ):
            raise ValueError(
                f"execution.slurm.{name} must be one safe resource identifier"
            )
        strings[name] = resource
    return Execution(
        backend,
        workers,
        processes,
        scratch_path,
        tuple(invocation),
        validated,
        SlurmResources(timeout_min, mem_gb, **strings),
        data_path,
        None if python is None else tuple(python),
    )


def _calibration(value: object, base: Path, source: Path) -> tuple[Path, ...]:
    document = _mapping(value, "calibration")
    if set(document) != {"meta_configs"}:
        raise ValueError("calibration keys must be exactly meta_configs")
    values = document["meta_configs"]
    if not isinstance(values, list) or not values:
        raise ValueError("calibration.meta_configs must be a non-empty list")
    paths = []
    for value in values:
        if not isinstance(value, str):
            raise TypeError("calibration meta-config paths must be strings")
        shown = value + _relative_note(value, "the meta-config folder")
        path = _file(base / value, "calibration meta-config", shown)
        if path == source:
            raise ValueError("calibration meta-config must not reference itself")
        nested = _mapping(
            _yaml_file(path, "calibration meta-config", value),
            "calibration meta-config root",
        )
        if "calibration" in nested:
            raise ValueError("recursive calibration declarations are not supported")
        if path not in paths:
            paths.append(path)
    return tuple(paths)


def _strings(value: object, name: str, allow_empty: bool = False) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or (not value and not allow_empty)
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise ValueError(
            f"{name} must be a {'possibly empty ' if allow_empty else 'non-empty '}list of strings"
        )
    if len(set(value)) != len(value):
        raise ValueError(f"{name} must not contain duplicates")
    return tuple(value)


def _formats(model_document: dict, config: Path) -> list[str]:
    """eqasim ``output_formats`` (eqasim's default when absent), validated."""

    formats = output_formats(model_document.get("config"))
    if not isinstance(formats, list) or not all(
        isinstance(item, str) for item in formats
    ):
        raise ValueError(f"output_formats of {config} must be a list of strings")
    return formats


def _outputs(
    document: dict, model_document: dict, config: Path
) -> dict[str, OutputDeclaration]:
    raw_outputs = _mapping(document.get("outputs", {}), "outputs")
    if not raw_outputs:
        raise ValueError(
            "outputs must declare at least one output (for example age: {})"
        )
    for name in raw_outputs:
        validate_output_name(name)
        validate_portable_name(name, "output")
    reject_case_collisions(list(raw_outputs), "output")
    formats = _formats(model_document, config)
    outputs = {}
    for name, value in raw_outputs.items():
        declaration = _mapping(value, f"output {name!r}")
        unknown = sorted(str(key) for key in declaration if key not in OUTPUT_KEYS)
        if unknown:
            raise ValueError(
                f"output {name!r} keys must be among {', '.join(OUTPUT_KEYS)}; "
                f"unknown: {', '.join(unknown)}"
            )
        known = KNOWN_OUTPUTS.get(name)
        if "file" in declaration:
            file = declaration["file"]
        elif known is None:
            raise ValueError(
                f"output {name!r} is not a known eqasim output; declare its file "
                "(relative to eqasim's output_path, e.g. "
                f"analysis_population/{{prefix}}{name}.csv)"
            )
        else:
            file = known.default_file(formats)
            if file is None:
                raise ValueError(
                    f"output {name!r} needs {' or '.join(known.formats)} in "
                    f"output_formats of {config}; add one"
                )
        try:
            validate_output_file(file)
        except ValueError as error:
            raise ValueError(f"output {name!r} file: {error}") from error
        if is_csv_file(file):
            separator = declaration.get("separator", UNKNOWN_OUTPUT_SEPARATOR)
            try:
                validate_separator(separator)
            except ValueError as error:
                raise ValueError(f"output {name!r} separator: {error}") from error
        elif "separator" in declaration:
            raise ValueError(f"separator applies only to .csv files (output {name!r})")
        else:
            # Never read; sealed as before so plan identities do not change.
            separator = UNKNOWN_OUTPUT_SEPARATOR
        category, simulated = declaration.get("category"), declaration.get("simulated")
        for label, item in (("category", category), ("simulated", simulated)):
            if item is not None and (not isinstance(item, str) or not item):
                raise ValueError(f"output {name!r} {label} must be a non-empty string")
        references = _strings(
            declaration.get("references", []), f"output {name!r} references", True
        )
        named = [item for item in (category, simulated) if item is not None]
        if len(set(named)) != len(named) or set(references) & set(named):
            raise ValueError(
                f"output {name!r} category, simulated and references must be distinct"
            )
        outputs[name] = OutputDeclaration(
            file, separator, category, simulated, references
        )
    return outputs


def _analysis_script(value: object, base: Path, label: str) -> Path:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError(f"{label} script must be a non-empty path")
    lexical = Path(os.path.normpath(os.path.abspath(base / value)))
    if platform.is_link(lexical):
        raise ValueError(f"{label} script must not be a symlink")
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"{label} script {value!r} does not exist") from error
    if not resolved.is_file():
        raise ValueError(f"{label} script must be a regular file")
    if resolved.suffix != ".py":
        raise ValueError(f"{label} script must be a .py file")
    return resolved


def _analyses(
    document: dict, base: Path, outputs: dict[str, OutputDeclaration]
) -> dict[str, AnalysisDeclaration]:
    """Parse ``analyses``; they never become archived outputs."""

    raw_analyses = _mapping(document.get("analyses", {}), "analyses")
    for name in raw_analyses:
        validate_output_name(name)
        validate_portable_name(name, "analysis")
        if name in outputs:
            raise ValueError(f"analysis name {name!r} is already an output name")
        if name.casefold() == ANALYSES_LOG_DIRECTORY:
            raise ValueError(
                f"analysis name {name!r} is reserved for the analyses cache logs"
            )
    reject_case_collisions([*outputs, *raw_analyses], "output or analysis")
    analyses = {}
    for name, value in raw_analyses.items():
        label = f"analysis {name!r}"
        declaration = _mapping(value, label)
        _require_keys(declaration, label, ANALYSIS_KEYS, {"script", "inputs"})
        script = _analysis_script(declaration["script"], base, label)
        inputs = _strings(declaration["inputs"], f"{label} inputs")
        for item in inputs:
            if item in raw_analyses:
                raise ValueError(
                    f"{label} input {item!r} is an analysis; inputs must be "
                    "outputs declared in outputs"
                )
            if item not in outputs:
                raise ValueError(f"{label} input {item!r} is not declared in outputs")
            file = outputs[item].file
            if not file.lower().endswith(ANALYSIS_INPUT_SUFFIXES):
                raise ValueError(
                    f"{label} input {item!r} file {file!r} must be a .csv, .gpkg, "
                    ".parquet or .geoparquet file"
                )
        category, simulated = declaration.get("category"), declaration.get("simulated")
        references = _strings(
            declaration.get("references", []), f"{label} references", True
        )
        try:
            output = DerivedOutput(
                name, name, inputs, ",", category, simulated, references
            )
        except ValueError as error:
            raise ValueError(f"{label}: {error}") from error
        analyses[name] = AnalysisDeclaration(name, script, inputs, output)
    return analyses


def _analysis_python(
    execution: Execution | None, analyses: dict[str, AnalysisDeclaration]
) -> tuple[str, ...]:
    """Return the interpreter command running analysis scripts (unresolved)."""

    if execution is None:
        return DEFAULT_ANALYSIS_PYTHON
    if execution.python is not None:
        return execution.python
    invocation = execution.invocation
    if len(invocation) > len(SYNPP_MODULE) and invocation[-2:] == SYNPP_MODULE:
        return invocation[: -len(SYNPP_MODULE)]
    if analyses:
        raise ValueError(
            "analyses need execution.python because execution.invocation "
            "does not end with -m synpp"
        )
    return ()


def _reads_analysis_output(indicator: Indicator) -> str | None:
    source = (
        indicator.source
        if isinstance(indicator, ValueIndicator)
        else getattr(getattr(indicator, "artifact", None), "source", None)
    )
    return source.name if isinstance(source, DerivedOutput) else None


def _require_output_stages(
    outputs: dict[str, OutputDeclaration], model_document: dict, config: Path
) -> None:
    """Refuse a known output left at a default file that eqasim will not write.

    Its stage must be in ``run:``, and a mode choice output needs
    ``mode_choice`` (read as eqasim does: any true value).
    """

    stages = set(run_stages(model_document.get("run")))
    nested = model_document.get("config")
    mode_choice = bool(nested.get("mode_choice", False)) if isinstance(nested, dict) else False
    for name, declaration in outputs.items():
        known = KNOWN_OUTPUTS.get(name)
        if known is None or declaration.file not in {file for _, file in known.files}:
            continue
        if known.stage not in stages:
            raise ValueError(
                f"output {name!r} is written by eqasim stage {known.stage!r}, "
                f"which is not in run: of {config}; "
                "add it to run: in your eqasim config"
            )
        if known.mode_choice and not mode_choice:
            raise ValueError(
                f"output {name!r} is written only when mode_choice is true in {config}"
            )


def _require_writable_outputs(
    outputs: dict[str, OutputDeclaration], model_document: dict, config: Path
) -> None:
    """Refuse outputs that cannot be archived or that eqasim is told not to write."""

    nested = model_document.get("config")
    nested = nested if isinstance(nested, dict) else {}
    prefix = nested.get("output_prefix", DEFAULT_OUTPUT_PREFIX)
    if isinstance(prefix, str):
        for name, declaration in outputs.items():
            archive_file_name(name, declaration.source_path(prefix))
    formats = _formats(model_document, config)
    known_files = {
        file: fmt
        for known in KNOWN_OUTPUTS.values()
        for fmt, file in known.files
        if fmt is not None
    }
    for name, declaration in outputs.items():
        missing = known_files.get(declaration.file) or SPATIAL_SUFFIX_FORMATS.get(
            Path(declaration.file).suffix.lower()
        )
        if missing is None or missing in formats:
            continue
        raise ValueError(
            f"output {name!r} is {FORMAT_KINDS[missing]} file but output_formats "
            f"of {config} is {formats!r}; add {missing} to output_formats"
        )


def _require_csv_output(
    outputs: dict[str, TableSource], name: object, label: str
) -> None:
    # A derived output is always a CSV table written by eqasim-sa.
    if (
        not isinstance(name, str)
        or name not in outputs
        or isinstance(outputs[name], DerivedOutput)
    ):
        return
    file = outputs[name].file
    if not file.lower().endswith(".csv"):
        raise ValueError(
            f"{label} reads output {name!r}, whose file {file!r} is not a .csv "
            "file; indicators read CSV tables only"
        )


INDICATOR_KINDS = (*SCALARIZERS, "share", "value")


def _declared_output(
    outputs: dict[str, TableSource], name: object, label: str
) -> TableSource:
    if not isinstance(name, str) or not name:
        raise ValueError(f"{label} output must be a non-empty string")
    if name not in outputs:
        raise ValueError(
            f"{label} output {name!r} is not declared in outputs or analyses"
        )
    return outputs[name]


def _share_indicator(
    raw: dict, outputs: dict[str, TableSource], label: str
) -> ShareIndicator:
    if set(raw) != {"output", "category"}:
        raise ValueError(f"{label} keys must be exactly output and category")
    output = raw["output"]
    declaration = _declared_output(outputs, output, label)
    category = raw["category"]
    if not isinstance(category, str):
        raise ValueError('share category must be a quoted string (e.g. "0", "True")')
    if not category:
        raise ValueError(f"{label} category must be a non-empty string")
    if declaration.category is None or declaration.simulated is None:
        raise ValueError(
            f"share({output}, {category}) requires output {output!r} "
            "to declare category and simulated"
        )
    return ShareIndicator(
        TableArtifact(
            output,
            declaration,
            declaration.category,
            declaration.simulated,
            declaration.references,
        ),
        category,
    )


def _value_indicator(
    raw: dict, outputs: dict[str, TableSource], label: str
) -> ValueIndicator:
    if not {"output", "column"} <= set(raw) <= {"output", "column", "where"}:
        raise ValueError(f"{label} keys must be output, column and optionally where")
    output = raw["output"]
    declaration = _declared_output(outputs, output, label)
    column = raw["column"]
    if not isinstance(column, str) or not column:
        raise ValueError(f"{label} column must be a non-empty string")
    where = _mapping(raw.get("where", {}), f"{label} where")
    if any(not isinstance(key, str) or not key for key in where):
        raise ValueError(f"{label} where keys must be non-empty strings")
    if any(not isinstance(item, str) for item in where.values()):
        raise ValueError("where values must be quoted strings")
    if column in where:
        raise ValueError(f"{label} column must not be a where key")
    return ValueIndicator(output, declaration, column, where)


def _scalarizer_declaration(
    raw: dict, kind: str, label: str
) -> PairDeclaration | ProductDeclaration:
    keys = set(raw)
    if keys not in ({"output", "reference"}, {"output"}, {"outputs"}):
        raise ValueError(
            f"{label} keys must be exactly output and reference, output, or outputs"
        )
    if keys == {"outputs"}:
        return ProductDeclaration((kind,), _strings(raw["outputs"], f"{label} outputs"))
    selected = [raw[key] for key in sorted(keys)]
    if not all(isinstance(item, str) and item for item in selected):
        raise ValueError(
            f"{label} {' and '.join(sorted(keys))} must be non-empty strings"
        )
    if "reference" in keys:
        return PairDeclaration(kind, raw["output"], raw["reference"])
    return ProductDeclaration((kind,), (raw["output"],))


def _indicator_blocks(
    document: dict, outputs: dict[str, TableSource]
) -> tuple[Indicator, ...]:
    raw_indicators = document.get("indicators", [])
    if not isinstance(raw_indicators, list):
        raise TypeError("indicators must be a list")
    resolved: list[Indicator] = []
    combinations: list[str] = []
    for index, value in enumerate(raw_indicators):
        if (
            not isinstance(value, dict)
            or len(value) != 1
            or next(iter(value)) not in INDICATOR_KINDS
        ):
            raise ValueError(
                f"indicator {index} must contain exactly one key: "
                f"one of {', '.join(INDICATOR_KINDS)}"
            )
        kind, raw = next(iter(value.items()))
        label = f"indicator {index}.{kind}"
        declaration = _mapping(raw, label)
        read = declaration.get("outputs", [declaration.get("output")])
        for name in read if isinstance(read, list) else []:
            _require_csv_output(outputs, name, label)
        if kind == "share":
            resolved.append(_share_indicator(declaration, outputs, label))
        elif kind == "value":
            resolved.append(_value_indicator(declaration, outputs, label))
        else:
            try:
                resolved.extend(
                    resolve_indicators(
                        [_scalarizer_declaration(declaration, kind, label)],
                        outputs,
                        SCALARIZERS,
                    )
                )
            except ValueError as error:
                prefix = "invalid indicator combinations: "
                if not str(error).startswith(prefix):
                    raise
                combinations.append(str(error).removeprefix(prefix))
    if combinations:
        raise ValueError("invalid indicator combinations: " + ", ".join(combinations))
    names = [item.name for item in resolved]
    if len(names) != len(set(names)):
        raise ValueError("resolved indicator names must be unique")
    reject_case_collisions(names, "indicator")
    return tuple(resolved)


def load_meta_config(
    path: Path,
    validate_factors: bool = True,
    deadline: float | None = None,
    verify_data: bool = True,
    use_sealed_plan: bool = False,
) -> MetaConfig:
    """Load, seal, and cross-validate a campaign meta-configuration.

    ``verify_data=False`` (analysis) does not require the execution scratch
    and data directories to exist; the hashed data manifest suffices.

    ``use_sealed_plan=True`` (analysis) reads the eqasim model document and
    context sealed by ``campaign_root/plan.parquet`` into ``sealed`` and
    validates factors, ``run:`` stages, ``output_formats`` and
    ``output_prefix`` against that sealed document instead of the current
    eqasim config; machine options are still validated against the current
    config. The current values stay in ``model_document`` and the context.
    """

    software_identity = read_software_identity()
    source = _file(path, "meta-config", str(path))
    source_state = path_state(source)
    source_sha256 = _digest(source, deadline)
    base = source.parent
    document = _mapping(
        resolve_meta_config_document(_yaml_file(source, "meta-config", str(path))),
        "meta-config root",
    )
    _require_keys(document, "meta-config", ROOT_KEYS, ROOT_REQUIRED)
    campaign = document["campaign"]
    if not isinstance(campaign, str) or not CAMPAIGN.fullmatch(campaign):
        raise ValueError("campaign must be one safe path segment")
    if len(campaign) > CAMPAIGN_MAX:
        raise ValueError(f"campaign must have at most {CAMPAIGN_MAX} characters")
    validate_portable_name(campaign, "campaign")
    model = _mapping(document["model"], "model")
    experiment = _mapping(document["experiment"], "experiment")
    analysis = _mapping(document["analysis"], "analysis")
    method = experiment.get("method")
    if not isinstance(method, str) or method not in EXPERIMENT_METHODS:
        raise ValueError(
            f"unsupported experiment.method {method!r}; expected one of "
            f"{', '.join(EXPERIMENT_METHODS)}"
        )
    _require_keys(
        experiment,
        f"experiment ({method})",
        EXPERIMENT_KEYS[method],
        set(EXPERIMENT_KEYS[method]),
    )
    seed_role = "factor" if method in SEED_FACTOR_METHODS else "replication"
    analysis_keys = (
        {
            "completeness_threshold",
            "bootstrap_resamples",
            "confidence_level",
            "convergence_checkpoints",
            "screening_top_k",
        }
        if method == "canonical_morris"
        else {"completeness_threshold", "confidence_level"}
        if method == "multiscramble_sobol"
        else {"completeness_threshold", "bootstrap_resamples", "confidence_level"}
        if method == "sobol"
        else {"completeness_threshold"}
    )
    _require_keys(analysis, "analysis", tuple(sorted(analysis_keys)), analysis_keys)
    completeness_threshold = analysis["completeness_threshold"]
    if type(completeness_threshold) is int and completeness_threshold in (0, 1):
        completeness_threshold = float(completeness_threshold)
    if (
        type(completeness_threshold) is not float
        or not math.isfinite(completeness_threshold)
        or not 0 <= completeness_threshold <= 1
    ):
        raise ValueError(
            "analysis.completeness_threshold must be a finite float in [0,1]"
        )
    raw_parameters = _mapping(document["parameters"], "parameters")
    if "seed" in raw_parameters:
        raise ValueError("declare simulation seeds in experiment.seeds, not parameters.seed")
    status = charter_status(document.get("charter"))
    if (
        method in {"discrete_oat", "sobol"}
        and status is not CharterStatus.DEMONSTRATION
    ):
        raise ValueError(f"{method} requires charter.status demonstration")
    if method in {"canonical_morris", "multiscramble_sobol"} and status is not CharterStatus.SCIENTIFIC:
        raise ValueError(f"{method} requires charter.status scientific")
    _require_keys(model, "model", MODEL_KEYS, {"root", "config", "data_manifest"})
    trajectories, design_seed = (
        experiment.get("trajectories"),
        experiment.get("design_seed"),
    )
    levels = experiment.get("levels")
    candidate_trajectories = experiment.get("candidate_trajectories")
    base_sample_size = experiment.get("base_sample_size")
    raw_base_sample_sizes = experiment.get("base_sample_sizes")
    raw_scramble_seeds = experiment.get("scramble_seeds")
    if method == "discrete_oat":
        if type(trajectories) is not int or trajectories <= 0:
            raise ValueError(
                "experiment.trajectories must be a strictly positive integer"
            )
        if type(design_seed) is not int or not -(2**63) <= design_seed < 2**63:
            raise ValueError("experiment.design_seed must be a signed int64")
        if completeness_threshold != 1.0:
            raise ValueError(
                "adjacent OAT analysis.completeness_threshold must equal 1.0"
            )
    if method == "canonical_morris":
        if type(levels) is not int or levels < 4 or levels % 2:
            raise ValueError(
                "experiment.levels must be an even integer greater than or equal to 4"
            )
        if type(trajectories) is not int or trajectories < 2:
            raise ValueError("experiment.trajectories must be an integer at least 2")
        if (
            type(candidate_trajectories) is not int
            or candidate_trajectories <= trajectories
            or candidate_trajectories > MAX_CANONICAL_MORRIS_CANDIDATES
        ):
            raise ValueError(
                "experiment.candidate_trajectories must be an integer strictly greater "
                "than trajectories "
                f"and at most {MAX_CANONICAL_MORRIS_CANDIDATES}"
            )
        if type(design_seed) is not int or not -(2**63) <= design_seed < 2**63:
            raise ValueError("experiment.design_seed must be a signed int64")
        if completeness_threshold != 1.0:
            raise ValueError(
                "canonical Morris analysis.completeness_threshold must equal 1.0"
            )
    if method == "sobol":
        if (
            type(base_sample_size) is not int
            or base_sample_size < 2
            or base_sample_size & (base_sample_size - 1)
        ):
            raise ValueError(
                "experiment.base_sample_size must be a power of two greater than or equal to 2"
            )
        if type(design_seed) is not int or not 0 <= design_seed < 2**64:
            raise ValueError(
                "experiment.design_seed must be an unsigned 64-bit integer"
            )
        if completeness_threshold != 1.0:
            raise ValueError("Sobol analysis.completeness_threshold must equal 1.0")
    base_sample_sizes = scramble_seeds = None
    if method == "multiscramble_sobol":
        if (
            not isinstance(raw_base_sample_sizes, list)
            or not raw_base_sample_sizes
            or any(type(value) is not int or value < 2 or value & (value - 1) for value in raw_base_sample_sizes)
            or sorted(set(raw_base_sample_sizes)) != raw_base_sample_sizes
            or any(right != 2 * left for left, right in zip(raw_base_sample_sizes, raw_base_sample_sizes[1:]))
        ):
            raise ValueError("experiment.base_sample_sizes must be sorted powers of two at least 2, each doubling")
        if (
            not isinstance(raw_scramble_seeds, list)
            or len(raw_scramble_seeds) < 4
            or any(type(seed) is not int or not 0 <= seed < 2**64 for seed in raw_scramble_seeds)
            or len(set(raw_scramble_seeds)) != len(raw_scramble_seeds)
        ):
            raise ValueError("experiment.scramble_seeds must contain at least four unique uint64 integers")
        if completeness_threshold != 1.0:
            raise ValueError("multi-scramble Sobol analysis.completeness_threshold must equal 1.0")
        base_sample_sizes = tuple(raw_base_sample_sizes)
        scramble_seeds = tuple(raw_scramble_seeds)
    convergence_checkpoints = screening_top_k = None
    if method in {"sobol", "canonical_morris", "multiscramble_sobol"}:
        bootstrap_resamples = analysis.get("bootstrap_resamples")
        confidence_level = analysis["confidence_level"]
        minimum_resamples = 100 if method == "canonical_morris" else 1
        if method != "multiscramble_sobol":
            validate_bootstrap_resamples(
                bootstrap_resamples,
                method=f"analysis {method}",
                minimum=minimum_resamples,
            )
        if (
            type(confidence_level) is not float
            or not math.isfinite(confidence_level)
            or not 0 < confidence_level < 1
        ):
            raise ValueError(
                "analysis.confidence_level must be a finite float in (0,1)"
            )
        if method == "canonical_morris":
            raw_checkpoints = analysis["convergence_checkpoints"]
            if (
                not isinstance(raw_checkpoints, list)
                or not raw_checkpoints
                or any(type(value) is not int or value < 2 for value in raw_checkpoints)
                or sorted(set(raw_checkpoints)) != raw_checkpoints
                or raw_checkpoints[-1] != trajectories
            ):
                raise ValueError(
                    "analysis.convergence_checkpoints must be sorted unique integers at least 2 ending at trajectories"
                )
            convergence_checkpoints = tuple(raw_checkpoints)
            screening_top_k = analysis["screening_top_k"]
    else:
        bootstrap_resamples = confidence_level = None
    seeds = experiment["seeds"]
    if not isinstance(seeds, list):
        raise ValueError("experiment.seeds must be a list")
    if method in SEED_FACTOR_METHODS and len(seeds) < 2:
        raise ValueError(f"experiment.seeds requires at least two values for {method}")
    if not seeds or any(
        type(seed) is not int or not -(2**63) <= seed < 2**63 for seed in seeds
    ):
        raise ValueError("experiment.seeds must be non-empty signed int64 values")
    if len(set(seeds)) != len(seeds):
        raise ValueError("experiment.seeds must not contain duplicates")
    seeds_provenance = experiment.get("seeds_provenance")
    if method == "multiscramble_sobol" and (
        not isinstance(seeds_provenance, str) or not seeds_provenance.strip()
    ):
        raise ValueError("experiment.seeds_provenance must be a non-empty string")
    root_value, config_value = model["root"], model["config"]
    if not isinstance(root_value, str) or not isinstance(config_value, str):
        raise TypeError("model paths must be strings")
    root_path = base / root_value
    if platform.is_link(root_path):
        raise ValueError("model.root must not be a symlink")
    try:
        root = root_path.resolve(strict=True)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"model.root not found: {root_value}"
            + _relative_note(root_value, "the meta-config folder")
        ) from None
    if not root.is_dir():
        raise ValueError("model.root must be a directory")
    # A relative model.config is read from model.root, as when eqasim is run
    # from its own folder; an absolute one may live anywhere.
    config = _file(
        root / config_value,
        "model.config",
        config_value + _relative_note(config_value, "model.root"),
    )
    manifest_value, lock_value = model["data_manifest"], model.get("lockfile")
    if not isinstance(manifest_value, str) or not isinstance(lock_value, str):
        raise TypeError("model.data_manifest and model.lockfile must be strings")
    config_bytes = config.read_bytes()
    try:
        model_document = load_yaml_source(config_bytes.decode("utf-8"))
    except yaml.YAMLError as error:
        raise ValueError(
            f"cannot parse model.config {config_value}: {yaml_error_reason(error)}"
        ) from None
    validate_eqasim_document(model_document)
    campaign_root_value = document["campaign_root"]
    if not isinstance(campaign_root_value, str):
        raise TypeError("campaign_root must be a string")
    campaign_root = _campaign_root(base / campaign_root_value, root)
    sealed = (
        sealed_inputs(campaign_root / "plan.parquet") if use_sealed_plan else None
    )
    # The document that defines the experiment: the sealed one for analysis.
    experiment_document = model_document if sealed is None else sealed.model_document
    model_source_exclusions = eqasim_source_exclusions(root, model_document)
    eqasim_source_sha256 = model_source_sha256(
        root, model_source_exclusions, deadline
    )
    manifest = _file(
        base / manifest_value,
        "model.data_manifest",
        manifest_value + _relative_note(manifest_value, "the meta-config folder"),
    )
    lockfile = _file(
        base / lock_value,
        "model.lockfile",
        lock_value + _relative_note(lock_value, "the meta-config folder"),
    )
    control_states = (
        (source, source_state),
        (config, path_state(config)),
        (manifest, path_state(manifest)),
        (lockfile, path_state(lockfile)),
    )
    _timeout(deadline)
    try:
        manifest_sha256 = canonical_json_sha256(manifest.read_bytes())
    except ValueError as error:
        raise ValueError("data manifest must be a valid JSON document") from error
    descriptor = {
        "eqasimSourceSha256": eqasim_source_sha256,
        "dataManifestSha256": manifest_sha256,
        "lockfileSha256": lf_sha256(lockfile),
        "eqasimSaVersion": software_identity.version,
    }
    _timeout(deadline)
    context_identity = ExecutionContextIdentity.from_descriptor(descriptor)
    factors = {}
    distributions = {}
    structured_factors = {}
    for name, declaration in raw_parameters.items():
        structured = (
            isinstance(declaration, dict) and "configurations" in declaration
        )
        expected_parameter_keys = (
            {"configurations", "distribution"}
            if structured and status is CharterStatus.SCIENTIFIC
            else {"configurations"}
            if structured
            else {"values", "distribution"}
            if status is CharterStatus.SCIENTIFIC
            else {"values"}
        )
        if (
            status is CharterStatus.SCIENTIFIC
            and isinstance(declaration, dict)
            and set(declaration) == {"values"}
        ):
            raise ValueError(
                f"factor {name!r} requires a distribution under a scientific charter "
                "(use the long form with values and distribution)"
            )
        if not isinstance(declaration, (list, dict)):
            raise ValueError(
                f"parameter {name!r} must be a list of values, "
                f"e.g. {name}: [a, b]"
            )
        if (
            not isinstance(name, str)
            or not isinstance(declaration, dict)
            or set(declaration) != expected_parameter_keys
        ):
            required = (
                "configurations and distribution"
                if structured and status is CharterStatus.SCIENTIFIC
                else "only configurations"
                if structured
                else "values and distribution"
                if status is CharterStatus.SCIENTIFIC
                else "only values"
            )
            raise ValueError(f"factor {name!r} must contain exactly {required}")
        if status is CharterStatus.SCIENTIFIC:
            distributions[name] = parse_distribution(declaration["distribution"], name)
        if structured:
            if method in {"discrete_oat", "canonical_morris"}:
                raise ValueError(f"{method} does not support structured factors")
            configurations = _mapping(
                declaration["configurations"], f"factor {name!r} configurations"
            )
            validated = {}
            payloads = set()
            for label, payload in configurations.items():
                if not isinstance(label, str) or not label or "\0" in label:
                    raise ValueError(
                        f"factor {name!r} configuration labels must be non-empty strings without NUL"
                    )
                if (
                    not isinstance(payload, list)
                    or not payload
                    or any(
                        not isinstance(item, str) or not item or "\0" in item
                        for item in payload
                    )
                    or len(set(payload)) != len(payload)
                ):
                    raise ValueError(
                        f"factor {name!r} configuration {label!r} must be a non-empty ordered list of unique non-empty strings without NUL"
                    )
                identity = tuple(payload)
                if identity in payloads:
                    raise ValueError(
                        f"factor {name!r} configurations must have distinct payloads"
                    )
                payloads.add(identity)
                validated[label] = list(payload)
            if not validated:
                raise ValueError(f"factor {name!r} configurations must not be empty")
            model_value = experiment_document["config"].get(name)
            if name not in experiment_document["config"]:
                raise ValueError(f"unknown factor: {name!r}")
            if not isinstance(model_value, list) or any(
                not isinstance(item, str) for item in model_value
            ):
                raise TypeError(
                    f"structured factor {name!r} model config value must be a list of strings"
                )
            structured_factors[name] = validated
            values = list(validated)
        else:
            values = declaration["values"]
        if validate_factors and name not in experiment_document["config"]:
            raise ValueError(f"unknown factor: {name!r}")
        if f"config.{name}" in EXCLUDED_PATHS or name in DEDICATED:
            raise ValueError(f"reserved or infrastructure factor: {name!r}")
        if not isinstance(values, list):
            raise TypeError(f"factor {name!r} values must be a list")
        for value in values:
            validate_transport(name, value, f"factor {name!r}")
        if status is CharterStatus.SCIENTIFIC and (
            len(values) < 2 or len(values) != len(set(values))
        ):
            raise ValueError(
                f"scientific factor {name!r} discrete-uniform support must contain "
                "at least two distinct values"
            )
        if method == "discrete_oat":
            validate_discrete_oat_values(name, values)
        if method in {"sobol", "canonical_morris", "multiscramble_sobol"}:
            if len(values) < 2 or len(set(values)) != len(values):
                raise ValueError(
                    f"factor {name!r} values must contain at least two distinct values"
                )
            if len({type(value) for value in values}) != 1 or type(values[0]) not in (
                int,
                float,
                str,
            ):
                raise TypeError(
                    f"factor {name!r} values must be homogeneous scalars"
                )
        factors[name] = values
    if method == "multiscramble_sobol":
        distributions["seed"] = parse_distribution(
            {
                "family": "discrete_uniform",
                "transform": "identity",
                "provenance": seeds_provenance,
            },
            "seed",
        )
    if method in {"discrete_oat", "canonical_morris"} and not factors:
        raise ValueError("trajectory design requires at least one factor")
    if method == "discrete_oat":
        candidates = math.factorial(len(factors)) * math.prod(
            2 * (len(values) - 1) for values in factors.values()
        )
        if trajectories > candidates:
            raise ValueError(
                f"experiment.trajectories exceeds discrete OAT candidate count {candidates}"
            )
    if method == "canonical_morris":
        universe = math.factorial(len(factors)) * levels ** len(factors)
        if candidate_trajectories > universe:
            raise ValueError(
                f"experiment.candidate_trajectories exceeds canonical candidate count {universe}"
            )
        if canonical_morris_distance_terms(
            len(factors), candidate_trajectories, trajectories
        ) > MAX_CANONICAL_MORRIS_DISTANCE_TERMS:
            raise ValueError(
                "canonical Morris candidate selection exceeds the deterministic "
                "distance-computation budget"
            )
        if (
            type(screening_top_k) is not int
            or not 1 <= screening_top_k <= len(factors)
        ):
            raise ValueError(
                "analysis.screening_top_k must be a positive integer no greater than factor count"
            )
        validate_canonical_morris_bootstrap_work(
            bootstrap_resamples,
            len(factors),
            trajectories,
            len(seeds),
            convergence_checkpoints,
        )
    if method == "sobol":
        if len(factors) + 1 < 2:
            raise ValueError("Sobol requires at least two factors including seed")
        if len(factors) + 1 > 10_600:
            raise ValueError("Sobol supports at most 10600 factors")
        if base_sample_size * (2 * (len(factors) + 1) + 2) > 2**63 - 1:
            raise ValueError("Sobol N(2D+2) must fit in signed int64")
        validate_sobol_bootstrap_work(
            bootstrap_resamples, base_sample_size, len(factors) + 1
        )
    if method == "multiscramble_sobol":
        dimensions = len(factors) + 1
        positions = len(scramble_seeds) * base_sample_sizes[-1] * (2 * dimensions + 2)
        if not 2 <= dimensions <= 10_600:
            raise ValueError("multi-scramble Sobol requires 2 to 10600 factors including seed")
        if positions > 2**63 - 1:
            raise ValueError("multi-scramble Sobol S*N*(2D+2) must fit in signed int64")
        if positions > MAX_MULTISCRAMBLE_DESIGN_POSITIONS:
            raise ValueError(
                "multi-scramble Sobol allocation exceeds the safe cap of "
                f"{MAX_MULTISCRAMBLE_DESIGN_POSITIONS} design positions"
            )
        if positions * (dimensions + 5) > MAX_MULTISCRAMBLE_TABLE_CELLS:
            raise ValueError(
                "multi-scramble Sobol allocation exceeds the safe cap of "
                f"{MAX_MULTISCRAMBLE_TABLE_CELLS} table cells"
            )
    execution = (
        _execution(
            document["execution"], base, root, model_document, verify_data
        )
        if "execution" in document
        else None
    )
    calibrations = (
        _calibration(document["calibration"], base, source)
        if "calibration" in document
        else ()
    )
    outputs = _outputs(document, experiment_document, config)
    _require_output_stages(outputs, experiment_document, config)
    _require_writable_outputs(outputs, experiment_document, config)
    analyses = _analyses(document, base, outputs)
    analysis_python = _analysis_python(execution, analyses)
    indicators = _indicator_blocks(
        document,
        {**outputs, **{name: item.output for name, item in analyses.items()}},
    )
    if method != "grid" and not indicators:
        raise ValueError(
            f"{method} requires at least one indicator; "
            "declare outputs (and optionally indicators)"
        )
    if isinstance(charter_document := document.get("charter"), dict):
        decision = charter_document.get("decision")
        primary = decision.get("primary_indicator") if isinstance(decision, dict) else None
        for item in indicators:
            if item.name == primary and (derived := _reads_analysis_output(item)):
                raise ValueError(
                    f"primary_indicator must not use analysis output '{derived}'"
                )
    charter = parse_charter(
        document.get("charter"),
        set(raw_parameters) | ({"seed"} if method in SEED_FACTOR_METHODS else set()),
        {item.name for item in indicators},
        {item.name: analysis_identity(item) for item in indicators},
    )
    if method == "canonical_morris":
        if not isinstance(charter.stochastic, ConditionalMeanStochastic):
            raise ValueError("canonical_morris requires stochastic.target conditional_mean")
        allocation = charter.stochastic.allocation
        if allocation.phase is AllocationPhase.PILOT:
            if len(seeds) < allocation.minimum_replications:
                raise ValueError(
                    "canonical_morris pilot replication seed count must be at least minimum_replications"
                )
        elif len(seeds) != allocation.selected_replications:
            raise ValueError(
                "canonical_morris production replication seed count must equal selected_replications"
            )
        if charter.decision.objective.value != "screening":
            raise ValueError("canonical_morris requires sensitivity objective screening")
        if charter.dependence.kind.value != "independent":
            raise ValueError("canonical_morris requires independent parameter dependence")
        validate_scientific_stopping(
            method,
            charter.stopping,
            len(convergence_checkpoints),
        )
    if method == "multiscramble_sobol":
        if not isinstance(charter.stochastic, FiniteSeedFactorStochastic):
            raise ValueError(
                "multiscramble_sobol requires stochastic.target finite_seed_factor"
            )
        if charter.decision.objective.value not in {"fp", "ff"}:
            raise ValueError("multiscramble_sobol requires sensitivity objective fp or ff")
        if charter.dependence.kind.value != "independent":
            raise ValueError("multiscramble_sobol requires independent parameter dependence")
        if (
            charter.decision.objective.value == "ff"
            and charter.decision.practical_tolerance is not None
            and charter.decision.practical_tolerance > 1
        ):
            raise ValueError(
                "multiscramble_sobol practical_tolerance must be a dimensionless "
                "total-order variance-share tolerance in [0,1]"
            )
        validate_scientific_stopping(
            method,
            charter.stopping,
            len(base_sample_sizes),
        )
    if any(path_state(item) != state for item, state in control_states):
        raise RuntimeError("sealed control input changed while loading")
    verify_software_source_states(software_identity)
    return MetaConfig(
        campaign,
        campaign_root,
        config,
        root,
        manifest,
        factors,
        distributions,
        charter,
        tuple(seeds),
        seed_role,
        method,
        trajectories,
        levels,
        candidate_trajectories,
        design_seed,
        base_sample_size,
        base_sample_sizes,
        scramble_seeds,
        bootstrap_resamples,
        confidence_level,
        convergence_checkpoints,
        screening_top_k,
        completeness_threshold,
        context_identity.as_dict(),
        context_identity.sha256,
        source_sha256,
        model_document,
        config_bytes,
        execution,
        calibrations,
        control_states,
        outputs,
        indicators,
        software_identity,
        model_source_exclusions,
        structured_factors,
        sealed,
        analyses,
        analysis_python,
    )
