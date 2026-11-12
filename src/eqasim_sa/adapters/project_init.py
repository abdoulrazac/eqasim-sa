"""Safe, resumable project scaffolding for the init command."""

import hashlib
import os
import re
import stat
import tempfile
import time
from pathlib import Path

import pyarrow.parquet as pq

from eqasim_sa.adapters.data_manifest import generate_data_manifest, load_data_manifest
from eqasim_sa.adapters.eqasim.artifacts import ANALYSIS, POPULATION
from eqasim_sa.adapters.eqasim.config import (
    load_yaml_document,
    validate_eqasim_document,
)
from eqasim_sa.adapters.eqasim.workspace import WorkerWorkspace
from eqasim_sa.adapters.templates import render_project
from eqasim_sa.core.analysis_publication import (
    ANALYSIS_STATE_DIRECTORY,
    LEGACY_ANALYSIS_STATE_DIRECTORY,
    OUTPUT_FILENAMES,
)
from eqasim_sa.utils import platform
from eqasim_sa.utils.filesystem import reject_links_below, resolve_root
from eqasim_sa.utils.names import validate_portable_name


def _directory(path: Path, name: str) -> Path:
    resolved = resolve_root(path, name)
    if not resolved.is_dir():
        raise ValueError(f"{name} must be an existing directory")
    return resolved


def _directory_or_create(path: Path, name: str) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return _directory(path, name)


def _resolved_location(path: Path, name: str) -> Path:
    """Resolve the existing prefix of a user-supplied root once.

    Links among existing components (system ancestors, or the root itself) are
    accepted; components that do not exist yet are appended lexically.
    """
    lexical = Path(os.path.abspath(path))
    missing: list[str] = []
    existing = lexical
    while not os.path.lexists(existing) and existing.parent != existing:
        missing.append(existing.name)
        existing = existing.parent
    return resolve_root(existing, name).joinpath(*reversed(missing))


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
        "output-summary.parquet",
        "governed-report.json",
        "analysis-generation.json",
    },
    "discrete_oat": {
        "indicators.parquet", "replication-dispersion.parquet",
        "discrete-oat-effects.parquet", "output-summary.parquet",
        "governed-report.json", "analysis-generation.json",
    },
    "sobol": {
        "indicators.parquet", "sobol-indices.parquet", "output-summary.parquet",
        "governed-report.json", "analysis-generation.json",
    },
}


def _project_name(value: str) -> str:
    from eqasim_sa.adapters.eqasim.config import CAMPAIGN

    if not isinstance(value, str) or len(value) > 64 or not CAMPAIGN.fullmatch(value):
        raise ValueError("project-name must be one safe path segment of 1 to 64 characters")
    return validate_portable_name(value, "project-name")


def _owned(info: os.stat_result) -> bool:
    uid = platform.current_uid()
    return uid is None or info.st_uid == uid


def _regular(path: Path, description: str, private: bool = False) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise FileExistsError(f"foreign content in init project: {description}") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or not _owned(info)
        or info.st_nlink != 1
        or private and stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise FileExistsError(f"foreign content in init project: {description}")


def _validate_project_manifest(path: Path) -> None:
    try:
        load_data_manifest(path)
    except ValueError as error:
        raise ValueError("invalid project manifest") from error


def _backup(path: Path, destination: Path, description: str) -> None:
    try:
        info, target = path.lstat(), destination.lstat()
    except OSError as error:
        raise FileExistsError(f"foreign content in init project: {description}") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or not stat.S_ISREG(target.st_mode)
        or not _owned(info)
        or not _owned(target)
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
    if platform.is_link(directory) or not directory.is_dir():
        raise ValueError("directory must not be a file or symlink")
    entries = tuple(directory.iterdir())
    if not entries:
        return True
    expected = {"manifest.json", "README.md", "configs", "campaigns"}
    scratch_top = scratch_relative.parts[0] if scratch_relative else None
    if scratch_top:
        expected.add(scratch_top)
    for entry in entries:
        if platform.is_link(entry) or entry.name not in expected:
            if entry.name in {"nantes.yml", "nantes-calibration.yml"}:
                raise FileExistsError("old starter detected; choose a new project directory")
            raise FileExistsError(f"foreign content in init project: {entry.name}")
    _regular(directory / "README.md", "README.md")
    _regular(directory / "manifest.json", "manifest.json")
    for name in {"configs", "campaigns"}:
        path = directory / name
        if platform.is_link(path) or not path.is_dir():
            raise FileExistsError(f"foreign content in init project: {name}")
    config_names = {Path(path).name for path in INIT_CONFIGS.values()}
    for entry in (directory / "configs").iterdir():
        if platform.is_link(entry) or entry.name not in config_names:
            raise FileExistsError(f"foreign content in init project: configs/{entry.name}")
        _regular(entry, f"configs/{entry.name}")
    if {entry.name for entry in (directory / "configs").iterdir()} != config_names:
        raise FileExistsError("non-empty directory is not a complete init v3 state")
    campaigns = directory / "campaigns"
    controls = _control_names(directory)
    for entry in campaigns.iterdir():
        if platform.is_link(entry):
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
            if platform.is_link(current) or not current.is_dir():
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
        ".eqasim-sa-run-locks", ".submitit", ".eqasim-sa", ANALYSIS_STATE_DIRECTORY,
        LEGACY_ANALYSIS_STATE_DIRECTORY, *derived,
    }
    temporary = re.compile(r"\.([0-9a-f]{32})\.parquet\.([A-Za-z0-9_-]+)\.tmp")
    backup = re.compile(r"\.([0-9a-f]{32})\.parquet\.([A-Za-z0-9_-]+)\.backup")
    for entry in root.iterdir():
        if platform.is_link(entry) or entry.name not in allowed:
            raise FileExistsError(f"foreign content in init campaign: {entry.name}")
        if entry.name in {
            ANALYSIS_STATE_DIRECTORY,
            LEGACY_ANALYSIS_STATE_DIRECTORY,
        }:
            _analysis_state_allowed(entry, method)
            continue
        if entry.name == ".eqasim-sa":
            _technical_state_allowed(entry, expected_ids, method)
            continue
        if entry.name == ".submitit":
            _owned_technical_tree(entry, f"campaigns/{method}/.submitit")
            continue
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
                platform.is_link(child)
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
                        platform.is_link(artifact)
                        or not artifact.is_file()
                        or expected_artifacts is None
                        or artifact.name not in expected_artifacts
                    ):
                        relative = f"archives/{child.name}/{artifact.name}"
                        raise FileExistsError(
                            f"foreign content in init campaign: {relative}"
                        )
                    _regular(
                        artifact,
                        f"campaigns/{method}/archives/{child.name}/{artifact.name}",
                    )


def _owned_technical_tree(path: Path, description: str) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise FileExistsError(f"foreign content in init project: {description}") from error
    if platform.is_link(path) or not _owned(info):
        raise FileExistsError(f"foreign content in init project: {description}")
    if stat.S_ISREG(info.st_mode):
        _regular(path, description)
        return
    if not stat.S_ISDIR(info.st_mode):
        raise FileExistsError(f"foreign content in init project: {description}")
    for child in path.iterdir():
        _owned_technical_tree(child, f"{description}/{child.name}")


def _owned_technical_directory(path: Path, description: str) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise FileExistsError(
            f"foreign content in init campaign: {description}"
        ) from error
    if (
        platform.is_link(path)
        or not stat.S_ISDIR(info.st_mode)
        or not _owned(info)
    ):
        raise FileExistsError(
            f"foreign content in init campaign: {description}"
        )


def _technical_state_allowed(
    path: Path, expected_ids: set[str], method: str
) -> None:
    _owned_technical_directory(path, ".eqasim-sa")
    children = {entry.name: entry for entry in path.iterdir()}
    if set(children) - {"locks", "transactions", "slurm"}:
        raise FileExistsError(f"foreign content in init campaign: {path.name}")
    locks = children.get("locks")
    if locks is not None:
        _owned_technical_directory(locks, ".eqasim-sa/locks")
        for entry in locks.iterdir():
            if entry.name == "runs":
                _owned_technical_directory(entry, ".eqasim-sa/locks/runs")
                for lease in entry.iterdir():
                    if lease.stem not in expected_ids or lease.suffix != ".lock":
                        raise FileExistsError(
                            f"foreign content in init campaign: .eqasim-sa/locks/runs/{lease.name}"
                        )
                    _regular(
                        lease,
                        f"campaigns/{method}/.eqasim-sa/locks/runs/{lease.name}",
                        private=True,
                    )
            elif entry.name in {"analysis.lock", "slurm.lock"}:
                _regular(
                    entry,
                    f"campaigns/{method}/.eqasim-sa/locks/{entry.name}",
                    private=True,
                )
            else:
                raise FileExistsError(
                    f"foreign content in init campaign: .eqasim-sa/locks/{entry.name}"
                )
    transactions = children.get("transactions")
    if transactions is not None:
        _owned_technical_directory(transactions, ".eqasim-sa/transactions")
        transaction = re.compile(
            r"\.(?:plan\.parquet|([0-9a-f]{32})\.parquet)\.([A-Za-z0-9_-]+)\.(?:tmp|backup)"
            r"|\.[tb][0-9a-f]{8}"
        )
        for entry in transactions.iterdir():
            match = transaction.fullmatch(entry.name)
            if (
                match is None
                or match.group(1) is not None
                and match.group(1) not in expected_ids
            ):
                raise FileExistsError(
                    f"foreign content in init campaign: .eqasim-sa/transactions/{entry.name}"
                )
            _regular(
                entry,
                f"campaigns/{method}/.eqasim-sa/transactions/{entry.name}",
                private=True,
            )
    slurm_state = children.get("slurm")
    if slurm_state is not None:
        _owned_technical_directory(slurm_state, ".eqasim-sa/slurm")
        _owned_technical_tree(
            slurm_state, f"campaigns/{method}/.eqasim-sa/slurm"
        )


def _analysis_state_allowed(path: Path, method: str) -> None:
    state_name = path.name
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or not _owned(info)
        or platform.is_link(path)
    ):
        raise FileExistsError(
            f"foreign content in init campaign: {state_name}"
        )
    children = {entry.name: entry for entry in path.iterdir()}
    if set(children) - {"staging", "generations"}:
        raise FileExistsError(
            f"foreign content in init campaign: {state_name}"
        )
    staging = children.get("staging")
    if staging is not None:
        if platform.is_link(staging) or not staging.is_dir() or any(staging.iterdir()):
            raise FileExistsError(
                f"foreign content in init campaign: {state_name}/staging"
            )
    generations = children.get("generations")
    if generations is None:
        return
    if platform.is_link(generations) or not generations.is_dir():
        raise FileExistsError(
            f"foreign content in init campaign: {state_name}/generations"
        )
    for generation in generations.iterdir():
        if (
            platform.is_link(generation)
            or not generation.is_dir()
            or re.fullmatch(r"[0-9a-f]{64}", generation.name) is None
        ):
            raise FileExistsError(
                f"foreign content in init campaign: {state_name}/generations"
            )
        outputs = tuple(generation.iterdir())
        if not outputs or any(output.name not in OUTPUT_FILENAMES for output in outputs):
            raise FileExistsError(
                f"foreign content in init campaign: {state_name}/generations/"
                f"{generation.name}"
            )
        for output in outputs:
            _regular(
                output,
                f"campaigns/{method}/{state_name}/generations/"
                f"{generation.name}/{output.name}",
            )


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
        platform.replace_with_retry(staging, destination)
        staging = None
    finally:
        if staging is not None:
            platform.remove_tree(staging)


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
    """Create or safely resume a generated demonstration project."""

    started = time.monotonic()
    project_name = _project_name(project_name)
    if not invocation or not all(
        isinstance(item, str) and item and "\0" not in item for item in invocation
    ):
        raise ValueError("invocation must contain non-empty arguments")
    lexical_directory = Path(os.path.abspath(directory))
    if not lexical_directory.parent.resolve(strict=True).is_dir():
        raise ValueError("directory parent must be an existing directory")
    absolute = _resolved_location(lexical_directory, "directory")
    lexical_scratch = (
        lexical_directory / "scratch"
        if scratch is None
        else Path(os.path.abspath(scratch))
    )
    selected_scratch = (
        absolute / lexical_scratch.relative_to(lexical_directory)
        if lexical_scratch.is_relative_to(lexical_directory)
        else _resolved_location(lexical_scratch, "scratch")
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
    if scratch_relative is not None:
        reject_links_below(absolute, selected_scratch, "scratch")
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
    reject_links_below(model, selected_config, "model-config")
    if platform.is_link(selected_config) or not selected_config.is_file():
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
        WorkerWorkspace.validate_scratch(selected_scratch, campaign_names)
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
            {
                name: (absolute / "configs" / name).read_bytes()
                for name in expected
                if name != "README.md"
            }
        )
        if actual != expected:
            raise FileExistsError("init templates do not match the requested inputs")
        if not regenerate_data_manifest:
            _validate_project_manifest(absolute / "manifest.json")
        _durable_preflight(absolute, expected_artifacts)
        selected_scratch = _directory_or_create(selected_scratch, "scratch")
    WorkerWorkspace.validate_scratch(selected_scratch, campaign_names)
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
