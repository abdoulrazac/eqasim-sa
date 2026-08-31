import hashlib
import importlib.metadata
import json
import math
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from eqasim_sa.adapters.eqasim.config import (
    CAMPAIGN,
    load_yaml_document,
    load_yaml_source,
    validate_eqasim_document,
)
from eqasim_sa.adapters.eqasim.artifacts import ARTIFACT_STAGE
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
from eqasim_sa.core.indicators import (
    ArtifactDeclaration,
    CustomDeclaration,
    PairDeclaration,
    PluginIdentity,
    ProductDeclaration,
    RegisteredIndicatorFactory,
    TotalVariationIndicator,
    analysis_identity,
    plugin_identity_from_entry_point,
    resolve_indicators,
)
from eqasim_sa.core.plan import (
    MAX_CANONICAL_MORRIS_CANDIDATES,
    MAX_CANONICAL_MORRIS_DISTANCE_TERMS,
    MAX_MULTISCRAMBLE_DESIGN_POSITIONS,
    MAX_MULTISCRAMBLE_TABLE_CELLS,
    canonical_morris_distance_terms,
    validate_bootstrap_resamples,
    validate_canonical_morris_bootstrap_work,
    validate_morris_values,
    validate_sobol_bootstrap_work,
)
from eqasim_sa.core.run import EXCLUDED_PATHS
from eqasim_sa.core.software_identity import (
    SoftwareIdentity,
    read_software_identity,
    verify_software_source_states,
)
from eqasim_sa.ports.simulator import IndicatorRequirement

SCALARIZERS = {
    "tv_distance": lambda artifact, reference: TotalVariationIndicator(
        artifact, reference
    ),
}


@dataclass(frozen=True)
class SlurmResources:
    timeout_min: int = 1440
    mem_gb: int | float | None = None
    partition: str | None = None
    account: str | None = None
    qos: str | None = None


@dataclass(frozen=True)
class Execution:
    backend: str
    workers: int
    processes: int
    scratch: Path
    invocation: tuple[str, ...]
    overrides: dict[str, object]
    slurm: SlurmResources
    data_path: Path | None


@dataclass(frozen=True)
class MetaConfig:
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
    control_states: tuple[tuple[Path, tuple[int, int, int, int, int]], ...]
    artifacts: dict[str, ArtifactDeclaration]
    indicators: tuple[object, ...]
    indicator_requirements: tuple[IndicatorRequirement, ...]
    software_identity: SoftwareIdentity
    structured_factors: dict[str, dict[str, list[str]]] = field(default_factory=dict)

    @property
    def eqasim_sa_version(self) -> str:
        return self.software_identity.version

    @property
    def eqasim_sa_sha256(self) -> str:
        return self.software_identity.sha256

    @property
    def eqasim_sa_source_states(self):  # type: ignore[no-untyped-def]
        return self.software_identity.source_states


def _mapping(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be a mapping")
    return value


def _file(path: Path, name: str) -> Path:
    if path.is_symlink():
        raise ValueError(f"{name} must not be a symlink")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"{name} must be a regular file")
    return resolved


def _timeout(deadline: float | None) -> float:
    if deadline is None:
        return 5
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("static check time budget exhausted")
    return min(5, remaining)


def _git(root: Path, *arguments: str, deadline: float | None = None) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
        check=False,
        timeout=_timeout(deadline),
    )
    if completed.returncode:
        raise ValueError(
            f"Git command failed: {' '.join(arguments)}: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def _git_stdin(
    root: Path, data: bytes, *arguments: str, deadline: float | None = None
) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
        timeout=_timeout(deadline),
    )
    if completed.returncode:
        raise ValueError(f"Git command failed: {' '.join(arguments)}")
    return completed.stdout.decode().strip()


def _state(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


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
        if current.exists() and current.is_symlink():
            raise ValueError("campaign_root must not traverse a symlink")
    ancestor = candidate
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if ancestor.is_symlink() or not ancestor.is_dir():
        raise ValueError("campaign_root ancestor must be a non-symlink directory")
    resolved = ancestor.resolve(strict=True).joinpath(
        *candidate.relative_to(ancestor).parts
    )
    if resolved == model_root or resolved.is_relative_to(model_root):
        raise ValueError("campaign_root must be outside model.root")
    return resolved


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _resolved_directory(path: Path, name: str) -> Path:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.exists() and current.is_symlink():
            raise ValueError(f"{name} must not traverse a symlink")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(f"{name} must be an existing directory")
    return resolved


def _execution(
    value: object, base: Path, model_root: Path, model_document: dict
) -> Execution:
    document = _mapping(value, "execution")
    required = {"backend", "workers", "scratch", "invocation"}
    optional = {"overrides", "slurm"}
    if not required <= set(document) or set(document) - required - optional:
        raise ValueError(
            "execution keys must be exactly backend, workers, scratch, invocation, with optional overrides and slurm"
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
    scratch_path = _resolved_directory(base / scratch, "execution.scratch")
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
    overrides = _mapping(document.get("overrides", {}), "execution.overrides")
    model_config = _mapping(model_document.get("config"), "model config")
    validated = {}
    for name, value in overrides.items():
        if (
            not isinstance(name, str)
            or f"config.{name}" not in EXCLUDED_PATHS
            or name in DEDICATED
        ):
            raise ValueError(f"override {name!r} is not an allowed environment option")
        if name not in model_config:
            raise ValueError(f"override {name!r} is absent from the sealed YAML")
        if type(value) is not type(model_config[name]):
            raise TypeError(f"override {name!r} must preserve the sealed YAML type")
        validate_transport(name, value)
        if name == "data_path":
            validated[name] = _resolved_directory(
                base / value, "override 'data_path'"
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
            model_root / effective_data_path, "effective execution data_path"
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
        path = _file(base / value, "calibration meta-config")
        if path == source:
            raise ValueError("calibration meta-config must not reference itself")
        nested = _mapping(load_yaml_document(path), "calibration meta-config root")
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


def _indicator_blocks(
    document: dict,
) -> tuple[
    dict[str, ArtifactDeclaration], tuple[object, ...], tuple[IndicatorRequirement, ...]
]:
    raw_artifacts = _mapping(document.get("artifacts", {}), "artifacts")
    artifacts = {}
    for name, value in raw_artifacts.items():
        declaration = _mapping(value, f"artifact {name!r}")
        if (
            not isinstance(name, str)
            or not name
            or set(declaration) != {"class", "synthetic", "references"}
        ):
            raise ValueError(
                f"artifact {name!r} keys must be exactly class, synthetic, references"
            )
        references = _strings(
            declaration["references"], f"artifact {name!r} references", True
        )
        artifacts[name] = ArtifactDeclaration(
            declaration["class"],
            declaration["synthetic"],
            references,
            ARTIFACT_STAGE.get(name),
        )
    raw_indicators = document.get("indicators", [])
    if not isinstance(raw_indicators, list):
        raise TypeError("indicators must be a list")
    declarations = []
    custom_names = set()
    for index, value in enumerate(raw_indicators):
        indicator = _mapping(value, f"indicator {index}")
        if len(indicator) != 1 or next(iter(indicator), None) not in {
            "pair",
            "product",
            "custom",
        }:
            raise ValueError(
                f"indicator {index} must contain exactly pair, product, or custom"
            )
        kind, raw = next(iter(indicator.items()))
        declaration = _mapping(raw, f"indicator {index}.{kind}")
        if kind == "pair":
            if set(declaration) != {"scalarizer", "artifact", "reference"}:
                raise ValueError(
                    "pair keys must be exactly scalarizer, artifact, reference"
                )
            declarations.append(
                PairDeclaration(
                    declaration["scalarizer"],
                    declaration["artifact"],
                    declaration["reference"],
                )
            )
        elif kind == "product":
            if set(declaration) != {"scalarizers", "artifacts"}:
                raise ValueError("product keys must be exactly scalarizers, artifacts")
            declarations.append(
                ProductDeclaration(
                    _strings(declaration["scalarizers"], "product scalarizers"),
                    _strings(declaration["artifacts"], "product artifacts"),
                )
            )
        else:
            if set(declaration) != {"function", "artifact"}:
                raise ValueError("custom keys must be exactly function, artifact")
            declarations.append(
                CustomDeclaration(declaration["function"], declaration["artifact"])
            )
            custom_names.add(declaration["function"])
    custom = {}
    entries = importlib.metadata.entry_points()
    selected = entries.select(group="eqasim_sa.indicators")
    for name in custom_names:
        matches = [entry for entry in selected if entry.name == name]
        if len(matches) != 1:
            raise ValueError(
                f"custom indicator {name!r} must have exactly one eqasim_sa.indicators entry point"
            )
        identity = plugin_identity_from_entry_point(matches[0])
        custom[name] = RegisteredIndicatorFactory(
            matches[0],
            identity,
        )
    resolved = resolve_indicators(
        declarations,
        artifacts,
        SCALARIZERS,
        custom,
    )
    names = [item.name for item in resolved]
    if len(names) != len(set(names)):
        raise ValueError("resolved indicator names must be unique")
    requirements = tuple(
        IndicatorRequirement(
            item.name,
            item.identity.artifact
            if isinstance(item, TotalVariationIndicator)
            else item.artifact,
        )
        for item in resolved
    )
    return artifacts, resolved, requirements


def load_meta_config(
    path: Path, validate_factors: bool = True, deadline: float | None = None
) -> MetaConfig:
    software_identity = read_software_identity()
    source = _file(path, "meta-config")
    source_state = _state(source)
    source_sha256 = _digest(source, deadline)
    base = source.parent
    document = _mapping(load_yaml_document(source), "meta-config root")
    required = {
        "campaign",
        "campaign_root",
        "model",
        "context",
        "experiment",
        "parameters",
        "analysis",
    }
    optional = {"execution", "calibration", "artifacts", "indicators", "charter"}
    if not required <= set(document) or set(document) - required - optional:
        raise ValueError(
            f"meta-config keys must include exactly {sorted(required)} plus supported optional blocks"
        )
    campaign = document["campaign"]
    if not isinstance(campaign, str) or not CAMPAIGN.fullmatch(campaign):
        raise ValueError("campaign must be one safe path segment")
    model, context = (
        _mapping(document["model"], "model"),
        _mapping(document["context"], "context"),
    )
    experiment = _mapping(document["experiment"], "experiment")
    analysis = _mapping(document["analysis"], "analysis")
    method = experiment.get("method")
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
    if set(analysis) != analysis_keys:
        raise ValueError(
            f"analysis keys must be exactly {', '.join(sorted(analysis_keys))}"
        )
    completeness_threshold = analysis["completeness_threshold"]
    if (
        type(completeness_threshold) is not float
        or not math.isfinite(completeness_threshold)
        or not 0 <= completeness_threshold <= 1
    ):
        raise ValueError(
            "analysis.completeness_threshold must be a finite float in [0,1]"
        )
    raw_parameters = _mapping(document["parameters"], "parameters")
    if (
        isinstance(raw_parameters.get("seed"), dict)
        and "configurations" in raw_parameters["seed"]
    ):
        raise ValueError("seed cannot be a structured factor")
    status = charter_status(document.get("charter"))
    if (
        method in {"morris", "discrete_oat", "sobol"}
        and status is not CharterStatus.DEMONSTRATION
    ):
        raise ValueError(f"{method} requires charter.status demonstration")
    if method in {"canonical_morris", "multiscramble_sobol"} and status is not CharterStatus.SCIENTIFIC:
        raise ValueError(f"{method} requires charter.status scientific")
    if "replications" in experiment and "seed" in raw_parameters:
        raise ValueError(
            "le seed est soit un facteur, soit une réplication, jamais les deux"
        )
    if set(model) != {"config", "root"} or set(context) != {
        "data_manifest",
        "lockfile",
    }:
        raise ValueError("model/context keys do not match the Story 1.4 contract")
    seed_role = experiment.get("seed_role")
    if seed_role not in {"replication", "factor"}:
        raise ValueError("experiment.seed_role must be replication or factor")
    if seed_role == "factor":
        if method == "grid":
            raise ValueError("grid seed factor is not supported yet")
        if (
            method not in {"sobol", "multiscramble_sobol"}
            or set(experiment) != (
                {"method", "seed_role", "base_sample_size", "design_seed"}
                if method == "sobol"
                else {"method", "seed_role", "base_sample_sizes", "scramble_seeds"}
            )
            or "seed" not in raw_parameters
        ):
            raise ValueError(
                "Sobol factor experiments require their exact sampling keys and parameters.seed"
            )
    expected_experiment = (
        {"method", "seed_role", "replications"}
        if method == "grid"
        else {
            "method",
            "seed_role",
            "levels",
            "trajectories",
            "candidate_trajectories",
            "design_seed",
            "replications",
        }
        if method == "canonical_morris"
        else {"method", "seed_role", "trajectories", "design_seed", "replications"}
    )
    if seed_role == "replication" and (
        method not in {"grid", "morris", "discrete_oat", "canonical_morris"}
        or set(experiment) != expected_experiment
    ):
        raise ValueError(
            "seed_role replication experiment keys must match grid or Morris trajectories/design_seed contract"
        )
    trajectories, design_seed = (
        experiment.get("trajectories"),
        experiment.get("design_seed"),
    )
    levels = experiment.get("levels")
    candidate_trajectories = experiment.get("candidate_trajectories")
    base_sample_size = experiment.get("base_sample_size")
    raw_base_sample_sizes = experiment.get("base_sample_sizes")
    raw_scramble_seeds = experiment.get("scramble_seeds")
    if method in {"morris", "discrete_oat"}:
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
        if design_seed is not None:
            raise ValueError("multiscramble_sobol does not permit design_seed")
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
        if method in {"sobol", "multiscramble_sobol"}:
            seeds = _mapping(raw_parameters["seed"], "parameters.seed").get("values")
        else:
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
            replications = _mapping(experiment["replications"], "experiment.replications")
            if set(replications) != {"seeds"} or not isinstance(
                replications["seeds"], list
            ):
                raise ValueError("experiment.replications.seeds must be a list")
            seeds = replications["seeds"]
    else:
        bootstrap_resamples = confidence_level = None
        replications = _mapping(experiment["replications"], "experiment.replications")
        if set(replications) != {"seeds"} or not isinstance(
            replications["seeds"], list
        ):
            raise ValueError("experiment.replications.seeds must be a list")
        seeds = replications["seeds"]
    if method in {"sobol", "multiscramble_sobol"} and (not isinstance(seeds, list) or len(seeds) < 2):
        raise ValueError("Sobol parameters.seed.values requires at least two values")
    if not seeds or any(
        type(seed) is not int or not -(2**63) <= seed < 2**63 for seed in seeds
    ):
        raise ValueError(
            "experiment.replications.seeds must be non-empty signed int64 values"
        )
    if len(set(seeds)) != len(seeds):
        raise ValueError("experiment.replications.seeds must not contain duplicates")
    root_value, config_value = model["root"], model["config"]
    if not isinstance(root_value, str) or not isinstance(config_value, str):
        raise TypeError("model paths must be strings")
    root_path = base / root_value
    if root_path.is_symlink():
        raise ValueError("model.root must not be a symlink")
    root = root_path.resolve(strict=True)
    if (
        not root.is_dir()
        or Path(_git(root, "rev-parse", "--show-toplevel", deadline=deadline)) != root
    ):
        raise ValueError("model.root must be the exact Git root")
    config = _file(base / config_value, "model.config")
    if not config.is_relative_to(root):
        raise ValueError("model.config must be strictly under model.root")
    relative = config.relative_to(root)
    _git(root, "ls-files", "--error-unmatch", "--", str(relative), deadline=deadline)
    if _git(
        root, "status", "--porcelain=v1", "--untracked-files=all", deadline=deadline
    ):
        raise ValueError("model Git repository must be clean")
    commit = _git(root, "rev-parse", "--verify", "HEAD", deadline=deadline)
    manifest_value, lock_value = context["data_manifest"], context["lockfile"]
    if not isinstance(manifest_value, str) or not isinstance(lock_value, str):
        raise TypeError("context paths must be strings")
    config_bytes = config.read_bytes()
    if _git_stdin(
        root, config_bytes, "hash-object", "--stdin", deadline=deadline
    ) != _git(root, "rev-parse", f"{commit}:{relative.as_posix()}", deadline=deadline):
        raise ValueError("model.config does not match its sealed Git blob")
    model_document = load_yaml_source(config_bytes.decode("utf-8"))
    validate_eqasim_document(model_document)
    manifest = _file(base / manifest_value, "data manifest")
    lockfile = _file(base / lock_value, "lockfile")
    control_states = (
        (source, source_state),
        (config, _state(config)),
        (manifest, _state(manifest)),
        (lockfile, _state(lockfile)),
    )
    descriptor = {
        "eqasimCommit": commit,
        "dataManifestSha256": _digest(manifest, deadline),
        "lockfileSha256": _digest(lockfile, deadline),
        "eqasimSaVersion": software_identity.version,
    }
    if _git(root, "rev-parse", "--verify", "HEAD", deadline=deadline) != commit or _git(
        root, "status", "--porcelain=v1", "--untracked-files=all", deadline=deadline
    ):
        raise ValueError("model Git repository changed while sealing context")
    encoded = json.dumps(
        descriptor, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
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
            if name == "seed":
                raise ValueError("seed cannot be a structured factor")
            if method in {"morris", "discrete_oat", "canonical_morris"}:
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
            model_value = model_document["config"].get(name)
            if name not in model_document["config"]:
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
        if method in {"sobol", "multiscramble_sobol"} and name == "seed":
            continue
        if validate_factors and name not in model_document["config"]:
            raise ValueError(f"unknown factor: {name!r}")
        if f"config.{name}" in EXCLUDED_PATHS or name in DEDICATED:
            raise ValueError(f"reserved or infrastructure factor: {name!r}")
        if not isinstance(values, list):
            raise TypeError(f"factor {name!r} values must be a list")
        for value in values:
            validate_transport(name, value)
        if status is CharterStatus.SCIENTIFIC and (
            len(values) < 2 or len(values) != len(set(values))
        ):
            raise ValueError(
                f"scientific factor {name!r} discrete-uniform support must contain "
                "at least two distinct values"
            )
        if method in {"morris", "discrete_oat"}:
            validate_morris_values(name, values)
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
    if method in {"morris", "discrete_oat", "canonical_morris"} and not factors:
        raise ValueError("trajectory design requires at least one factor")
    if method in {"morris", "discrete_oat"}:
        candidates = math.factorial(len(factors)) * math.prod(
            2 * (len(values) - 1) for values in factors.values()
        )
        if trajectories > candidates:
            raise ValueError(
                f"experiment.trajectories exceeds Morris candidate count {candidates}"
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
    campaign_root = document["campaign_root"]
    if not isinstance(campaign_root, str):
        raise TypeError("campaign_root must be a string")
    execution = (
        _execution(document["execution"], base, root, model_document)
        if "execution" in document
        else None
    )
    calibrations = (
        _calibration(document["calibration"], base, source)
        if "calibration" in document
        else ()
    )
    artifacts, indicators, indicator_requirements = _indicator_blocks(document)
    if method == "morris" and not indicators:
        raise ValueError("Morris requires at least one indicator")
    if method in {"discrete_oat", "canonical_morris"} and not indicators:
        raise ValueError(f"{method} requires at least one indicator")
    if method in {"sobol", "multiscramble_sobol"} and not indicators:
        raise ValueError("Sobol requires at least one indicator")
    charter = parse_charter(
        document.get("charter"),
        set(raw_parameters),
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
    if any(_state(item) != state for item, state in control_states):
        raise RuntimeError("sealed control input changed while loading")
    verify_software_source_states(software_identity)
    return MetaConfig(
        campaign,
        _campaign_root(base / campaign_root, root),
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
        descriptor,
        hashlib.sha256(b"eqasim-sa:context:v1\0" + encoded).hexdigest(),
        source_sha256,
        model_document,
        config_bytes,
        execution,
        calibrations,
        control_states,
        artifacts,
        indicators,
        indicator_requirements,
        software_identity,
        structured_factors,
    )
