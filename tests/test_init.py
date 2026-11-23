import json
import os
import re
import shutil
import shlex
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

import eqasim_sa.adapters.project_init as project_init_module
from eqasim_sa.adapters.application import init_from_inputs
from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.adapters.meta_config_defaults import DEFAULT_INVOCATION
from eqasim_sa.cli import dispatch, parser
from tests.conftest import make_symlink


SOBOL_POSITIONS, SOBOL_UNIQUE = 48, 4
METHODS = ("calibration", "grid", "discrete-oat", "sobol")


def inputs(tmp_path: Path) -> tuple[Path, Path]:
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.yml").write_text(
        "working_directory: /cache\nrun: [synthesis.output, analysis.synthesis.population]\nconfig:\n"
        "  processes: 4\n  sampling_rate: 0.001\n  random_seed: 1234\n"
        "  data_path: /data\n  output_path: /output\n  output_prefix: nantes_\n"
    )
    (model / "uv.lock").write_text("version = 1\n")
    data = tmp_path / "data"
    data.mkdir()
    (data / "input.txt").write_text("first\n")
    return model, data


def invocation() -> tuple[str, ...]:
    return "python", "-m", "synpp"


def initialize(tmp_path: Path, name: str = "my-study"):
    model, data = inputs(tmp_path)
    root = tmp_path / name
    result = init_from_inputs(
        name, root, model, data, None, invocation()
    )
    return result, root, model, data


def test_init_is_a_pure_atomic_template_generator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    model, data = inputs(tmp_path)
    root = tmp_path / "project"

    from eqasim_sa.adapters import application

    def forbidden(*args, **kwargs):
        raise AssertionError("pipeline/subprocess call from init")

    for name in (
        "plan_from_meta_config", "run_from_meta_config", "estimate_from_meta_config",
        "check_from_meta_config", "analyze_from_meta_config",
    ):
        monkeypatch.setattr(application, name, forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)

    result = init_from_inputs(
        "project", root, model, data, None, invocation()
    )

    assert result == {
        "status": "initialized",
        "initVersion": 3,
        "projectName": "project",
        "manifest": "manifest.json",
        "readme": "README.md",
        "metaConfigs": {
            "calibration": "configs/00-calibration.yml",
            "grid": "configs/01-grid.yml",
            "discrete_oat": "configs/02-discrete-oat.yml",
            "sobol": "configs/03-sobol.yml",
        },
        "restored": [],
        "elapsedSeconds": result["elapsedSeconds"],
        "error": None,
    }
    assert {path.name for path in root.iterdir()} == {
        "manifest.json", "README.md", "configs", "campaigns", "scratch",
        ".gitattributes",
    }
    assert not any((root / "campaigns").iterdir())
    assert not tuple(root.rglob("plan.parquet"))
    assert not tuple(root.rglob("runs"))
    assert not tuple(root.rglob("archives"))
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["files"] == [
        {
            "path": "input.txt",
            "size": 6,
            "sha256": "b640e840b19d378660b32fb51ae18d67dccb4a8596a29e7bd72c1b2ae5928f41",
        }
    ]
    for index, method in enumerate(METHODS):
        text = (root / f"configs/{index:02d}-{method}.yml").read_text()
        assert "data_manifest: \"../manifest.json\"" in text
        expected_campaign = "discrete_oat" if method == "discrete-oat" else method
        assert f"campaign: project-{expected_campaign}" in text
        assert "HTS adjust with INSEE population weights" not in text
        assert text.count("references: [INSEE]") == 1
        assert text.count("references: [HTS]") == 2
        assert "  sampling_rate: [" in text
        assert "config: \"config.yml\"" in text
        assert "analysis.synthesis.population" in text
        for name in ("persons", "households", "trips", "activities"):
            assert f"\n  {name}: {{}}\n" in text
        assert '    file: "{prefix}activities.gpkg"' in text
        assert "# matching_minimum_age: [0, 5, 12]" in text
        assert "age_scheme" not in text and "projection_" + "year" not in text
    readme = (root / "README.md").read_text()
    assert "did not\nplan a campaign" in readme
    project = Path(__file__).resolve().parents[1]
    runner = shlex.join(
        ["uv", "run", "--project", str(project), "--locked", "eqasim-sa"]
    )
    assert f"SA=({runner})" in readme
    assert readme.index('"${SA[@]}" plan') < readme.index('"${SA[@]}" check')
    # The README describes the worker count the starters actually write.
    workers = {
        yaml.safe_load(path.read_text())["execution"]["workers"]
        for path in (root / "configs").glob("*.yml")
    }
    assert workers == {2}
    flat = " ".join(readme.split())
    assert "one worker when `workers` is absent" in flat
    assert "the starters write `workers: 2`" in flat


def test_init_accepts_a_custom_model_config(
    tmp_path: Path,
) -> None:
    model, data = inputs(tmp_path)
    (model / "custom.yml").write_text(
        "working_directory: /cache\nrun: [synthesis.output, analysis.synthesis.population]\nconfig:\n"
        "  sampling_rate: 0.001\n"
    )
    result = init_from_inputs(
        "project", tmp_path / "project", model, data, None, invocation(), Path("custom.yml")
    )
    assert result["status"] == "initialized"


def test_init_is_byte_and_mtime_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, root, model, data = initialize(tmp_path)
    states = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*") if path.is_file()
    }

    monkeypatch.setattr(
        "eqasim_sa.adapters.project_init.generate_data_manifest",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("data rehashed")),
    )
    repeated = init_from_inputs(
        "my-study", root, model, data, None, invocation()
    )

    assert result["status"] == "initialized" and repeated["status"] == "unchanged"
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in states} == states


GITATTRIBUTES = (
    "*.yml text eol=lf\n"
    "*.yaml text eol=lf\n"
    "*.json text eol=lf\n"
    "*.lock text eol=lf\n"
)


def test_init_writes_gitattributes(tmp_path: Path) -> None:
    _, project, _, _ = initialize(tmp_path)

    assert (project / ".gitattributes").read_text(encoding="utf-8") == GITATTRIBUTES
    assert (project / ".gitattributes").read_bytes() == GITATTRIBUTES.encode()


def test_init_resume_keeps_user_gitattributes(tmp_path: Path) -> None:
    _, project, model, data = initialize(tmp_path)
    attributes = project / ".gitattributes"
    attributes.write_bytes(b"* text=auto\r\n")

    result = init_from_inputs(
        "my-study", project, model, data, None, invocation()
    )

    assert result["status"] == "unchanged" and result["restored"] == []
    assert attributes.read_bytes() == b"* text=auto\r\n"


def test_init_resume_restores_missing_gitattributes(tmp_path: Path) -> None:
    _, project, model, data = initialize(tmp_path)
    (project / ".gitattributes").unlink()

    result = init_from_inputs(
        "my-study", project, model, data, None, invocation()
    )

    assert result["status"] == "unchanged"
    assert result["restored"] == [".gitattributes"]
    assert (project / ".gitattributes").read_text(encoding="utf-8") == GITATTRIBUTES


def test_init_rejects_symlinked_gitattributes(tmp_path: Path) -> None:
    _, project, model, data = initialize(tmp_path)
    attributes = project / ".gitattributes"
    target = tmp_path / "foreign-attributes"
    target.write_text("* -text\n", encoding="utf-8")
    attributes.unlink()
    make_symlink(attributes, target)

    with pytest.raises(FileExistsError, match="foreign content"):
        init_from_inputs("my-study", project, model, data, None, invocation())
    assert target.read_text(encoding="utf-8") == "* -text\n"


def test_init_regenerates_only_the_local_manifest(tmp_path: Path) -> None:
    _, root, model, data = initialize(tmp_path)
    static = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (root / "README.md", *sorted((root / "configs").iterdir()))
    }
    before = (root / "manifest.json").read_bytes()
    (data / "second.txt").write_text("second\n")

    unchanged = init_from_inputs(
        "my-study", root, model, data, None, invocation()
    )
    regenerated = init_from_inputs(
        "my-study", root, model, data, None, invocation(),
        regenerate_data_manifest=True,
    )

    assert unchanged["status"] == "unchanged"
    assert regenerated["status"] == "initialized"
    assert (root / "manifest.json").read_bytes() != before
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in static} == static


def test_init_rejects_malformed_or_inconsistent_project_manifest(tmp_path: Path) -> None:
    _, root, model, data = initialize(tmp_path)
    manifest = root / "manifest.json"
    valid = json.loads(manifest.read_text())
    malformed = [
        [],
        {**valid, "schemaVersion": 2},
        {**valid, "algorithm": "md5"},
        {**valid, "files": "bad"},
        {**valid, "files": [{"path": "../bad", "size": 1, "sha256": "a" * 64}]},
        {**valid, "files": [{"path": "a", "size": -1, "sha256": "a" * 64}]},
        {**valid, "files": [{"path": "a", "size": 1, "sha256": "not-sha256"}]},
        {**valid, "files": [valid["files"][0], valid["files"][0]]},
        {**valid, "treeSha256": "0" * 64},
    ]
    for document in malformed:
        manifest.write_text(json.dumps(document) + "\n")
        before = manifest.read_bytes(), manifest.stat().st_mtime_ns
        with pytest.raises(ValueError, match="invalid project manifest"):
            init_from_inputs("my-study", root, model, data, None, invocation())
        assert (manifest.read_bytes(), manifest.stat().st_mtime_ns) == before
    manifest.write_text("not json\n")
    with pytest.raises(ValueError, match="invalid project manifest"):
        init_from_inputs("my-study", root, model, data, None, invocation())


@pytest.mark.parametrize("analysis_state_name", ["analysis"])
def test_init_rerun_accepts_user_created_campaign_state(
    tmp_path: Path, analysis_state_name: str,
) -> None:
    _, root, model, data = initialize(tmp_path, "project")
    campaign = root / "campaigns/project-grid"
    campaign.mkdir()
    run_id = "a" * 32
    pq.write_table(pa.table({"runId": [run_id]}), campaign / "plan.parquet")
    runs, archive = campaign / "runs", campaign / "archives" / run_id
    runs.mkdir()
    archive.mkdir(parents=True)
    pq.write_table(pa.table({"runId": [run_id]}), runs / f"{run_id}.parquet")
    marker = runs / f"{run_id}.parquet"
    backup = runs / f".{run_id}.parquet.safe.backup"
    os.link(marker, backup)
    # Archive files are the outputs the starters declare, named <name><suffix>.
    for name in ("age", "distance", "nbr_vehicle"):
        (archive / f"{name}.csv").write_bytes(b"owned")
    pq.write_table(pa.table({"value": [1.0]}), campaign / "indicators.parquet")
    (campaign / "analysis-generation.json").write_text("{}\n")
    analysis_state = campaign / analysis_state_name
    (analysis_state / "staging").mkdir(parents=True)
    generation = analysis_state / "generations" / ("b" * 64)
    generation.mkdir(parents=True)
    pq.write_table(pa.table({"value": [1.0]}), generation / "indicators.parquet")
    technical = campaign / ".eqasim-sa"
    run_locks = technical / "locks" / "runs"
    transactions = technical / "transactions"
    submitit = technical / "slurm" / "submitit"
    run_locks.mkdir(parents=True)
    transactions.mkdir(parents=True)
    submitit.mkdir(parents=True)
    run_lock = run_locks / f"{run_id}.lock"
    run_lock.write_bytes(b"")
    run_lock.chmod(0o600)
    transaction = transactions / f".{run_id}.parquet.safe.tmp"
    transaction.write_bytes(b"recoverable")
    transaction.chmod(0o600)
    staged = transactions / (".t" + "c" * 8)
    staged.write_bytes(b"staged")
    staged.chmod(0o600)
    (submitit / "scheduler.log").write_text("owned\n")

    protected = {
        path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_nlink)
        for path in (
            campaign / "plan.parquet",
            marker,
            backup,
            generation / "indicators.parquet",
            run_lock,
            transaction,
            staged,
        )
    }
    result = init_from_inputs(
        "project", root, model, data, None, invocation()
    )

    assert result["status"] == "unchanged"
    assert (campaign / "plan.parquet").is_file()
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_nlink)
        for path in protected
    } == protected


def _campaign_with_figures(tmp_path: Path):
    """An init project whose grid campaign holds what `plot` leaves behind."""
    _, root, model, data = initialize(tmp_path, "project")
    campaign = root / "campaigns/project-grid"
    campaign.mkdir()
    pq.write_table(pa.table({"runId": ["a" * 32]}), campaign / "plan.parquet")
    figures = campaign / "figures"
    generation = figures / ("b" * 64)
    generation.mkdir(parents=True)
    (generation / "output-summary.png").write_bytes(b"\x89PNG")
    (generation / "grid-response-x.png").write_bytes(b"\x89PNG")
    (generation / "figures.json").write_text("{}\n")
    (figures / ("c" * 64)).mkdir()
    (figures / ".staging-0123456789abcdef0123456789abcdef").mkdir()
    (figures / ".retired-fedcba9876543210fedcba9876543210").mkdir()
    return root, model, data, figures


def test_init_rerun_accepts_figures_written_by_plot(tmp_path: Path) -> None:
    root, model, data, figures = _campaign_with_figures(tmp_path)
    before = {
        path: path.read_bytes() for path in figures.rglob("*") if path.is_file()
    }

    result = init_from_inputs("project", root, model, data, None, invocation())

    assert result["status"] == "unchanged"
    assert {
        path: path.read_bytes() for path in figures.rglob("*") if path.is_file()
    } == before


@pytest.mark.parametrize(
    "foreign",
    [
        "notes.txt",
        "not-a-generation",
        "B" * 64,
        "b" * 64 + "/notes.txt",
        "b" * 64 + "/nested",
        ".staging-short",
        ".other-0123456789abcdef0123456789abcdef",
    ],
)
def test_init_rerun_rejects_foreign_content_in_figures(
    tmp_path: Path, foreign: str
) -> None:
    root, model, data, figures = _campaign_with_figures(tmp_path)
    path = figures / foreign
    if "." in Path(foreign).name and not foreign.startswith("."):
        path.write_text("foreign\n")
    else:
        path.mkdir()

    with pytest.raises(FileExistsError, match="foreign content in init campaign: figures"):
        init_from_inputs("project", root, model, data, None, invocation())


def test_init_rerun_rejects_symlinked_figures(tmp_path: Path) -> None:
    root, model, data, figures = _campaign_with_figures(tmp_path)
    target = tmp_path / "elsewhere.png"
    target.write_bytes(b"\x89PNG")
    make_symlink(figures / ("b" * 64) / "linked.png", target)

    with pytest.raises(FileExistsError, match="foreign content in init campaign: figures"):
        init_from_inputs("project", root, model, data, None, invocation())


@pytest.mark.parametrize("windows", [False, True])
def test_init_rerun_private_mode_check_is_posix_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: bool
) -> None:
    # Windows reports 0o666 (or 0o444) for every regular file, so the POSIX
    # "private file" mode test cannot apply there, like the owner check.
    from eqasim_sa.utils import platform

    _, root, model, data = initialize(tmp_path, "project")
    campaign = root / "campaigns/project-grid"
    campaign.mkdir()
    run_id = "a" * 32
    pq.write_table(pa.table({"runId": [run_id]}), campaign / "plan.parquet")
    run_locks = campaign / ".eqasim-sa" / "locks" / "runs"
    run_locks.mkdir(parents=True)
    run_lock = run_locks / f"{run_id}.lock"
    run_lock.write_bytes(b"")
    run_lock.chmod(0o666)
    monkeypatch.setattr(platform, "IS_WINDOWS", windows)

    if windows:
        result = init_from_inputs("project", root, model, data, None, invocation())
        assert result["status"] == "unchanged"
    else:
        with pytest.raises(FileExistsError, match="locks/runs/"):
            init_from_inputs("project", root, model, data, None, invocation())
    assert run_lock.is_file()


@pytest.mark.parametrize("analysis_state_name", ["analysis"])
def test_init_reports_the_inspected_analysis_state_name(
    tmp_path: Path, analysis_state_name: str,
) -> None:
    _, root, model, data = initialize(tmp_path, "project")
    campaign = root / "campaigns/project-grid"
    campaign.mkdir()
    pq.write_table(
        pa.table({"runId": ["a" * 32]}),
        campaign / "plan.parquet",
    )
    state = campaign / analysis_state_name
    state.mkdir()
    (state / "foreign.txt").write_text("not owned\n")

    with pytest.raises(
        FileExistsError,
        match=rf"init campaign: {re.escape(analysis_state_name)}",
    ):
        init_from_inputs("project", root, model, data, None, invocation())

    assert (state / "foreign.txt").read_text() == "not owned\n"


@pytest.mark.parametrize(
    "relative",
    [Path("."), Path("locks"), Path("locks/runs"), Path("transactions"), Path("slurm")],
)
def test_init_technical_inventory_rejects_symlinked_runtime_directories(
    tmp_path: Path, relative: Path
) -> None:
    technical = tmp_path / ".eqasim-sa"
    (technical / "locks/runs").mkdir(parents=True)
    (technical / "transactions").mkdir()
    (technical / "slurm").mkdir()
    target = technical if relative == Path(".") else technical / relative
    external = tmp_path / f"external-{relative.as_posix().replace('/', '-')}"
    external.mkdir()
    shutil.rmtree(target)
    make_symlink(target, external, target_is_directory=True)

    with pytest.raises(FileExistsError, match="foreign content"):
        project_init_module._technical_state_allowed(technical, set(), "grid")


def test_init_technical_inventory_requires_runtime_directory_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    technical = tmp_path / ".eqasim-sa"
    technical.mkdir()
    uid = technical.stat().st_uid
    monkeypatch.setattr(project_init_module.os, "getuid", lambda: uid + 1)

    with pytest.raises(FileExistsError, match="foreign content"):
        project_init_module._technical_state_allowed(technical, set(), "grid")


def test_init_rejects_unsafe_or_foreign_state_without_publication(tmp_path: Path) -> None:
    model, data = inputs(tmp_path)
    for name in ("", "../bad", "bad/name", "x" * 65, " white"):
        destination = tmp_path / "destination"
        with pytest.raises(ValueError, match="project-name"):
            init_from_inputs(name, destination, model, data, None, invocation())
        assert not destination.exists()

    old = tmp_path / "old"
    old.mkdir()
    (old / "nantes.yml").write_text("old")
    with pytest.raises(FileExistsError, match="old starter"):
        init_from_inputs("project", old, model, data, None, invocation())

    linked = data / "linked"
    make_symlink(linked, data / "input.txt")
    root = tmp_path / "project"
    with pytest.raises(ValueError, match="symlink"):
        init_from_inputs("project", root, model, data, None, invocation())
    assert not root.exists()
    external = tmp_path / "external-scratch"
    with pytest.raises(ValueError, match="symlink"):
        init_from_inputs("project", root, model, data, external, invocation())
    assert not root.exists() and not external.exists()


def test_init_rejects_modified_and_hardlinked_static_files(tmp_path: Path) -> None:
    _, root, model, data = initialize(tmp_path, "project")
    config = root / "configs/01-grid.yml"
    original = config.read_bytes()
    config.write_text(config.read_text() + "# changed\n")
    with pytest.raises(FileExistsError, match=r"init would overwrite .*01-grid\.yml, which differs from what init would write \(file edited or init inputs changed\)"):
        init_from_inputs("project", root, model, data, None, invocation())
    config.write_bytes(original)
    # A separately linked README has matching bytes but is not owned template state.
    saved = tmp_path / "README.md"
    saved.write_bytes((root / "README.md").read_bytes())
    (root / "README.md").unlink()
    os.link(saved, root / "README.md")
    with pytest.raises(FileExistsError, match="README"):
        init_from_inputs("project", root, model, data, None, invocation())


def test_init_scratch_defaults_overrides_and_overlap(tmp_path: Path) -> None:
    model, data = inputs(tmp_path)
    root = tmp_path / "project"
    nested = root / "runtime/scratch"
    init_from_inputs("project", root, model, data, nested, invocation())
    assert nested.is_dir() and not (root / "scratch").exists()
    shutil.rmtree(nested)
    assert init_from_inputs(
        "project", root, model, data, nested, invocation()
    )["status"] == "unchanged"
    assert nested.is_dir()

    other = tmp_path / "other"
    with pytest.raises(ValueError, match="contain"):
        init_from_inputs("other", other, model, data, other, invocation())
    enclosing = tmp_path / "enclosing"
    enclosing.mkdir()
    with pytest.raises(ValueError, match="contain"):
        init_from_inputs(
            "nested", enclosing / "nested", model, data, enclosing, invocation()
        )


def test_init_rejects_model_destination_and_scratch_overlap_without_side_effects(
    tmp_path: Path,
) -> None:
    model, data = inputs(tmp_path)
    nested_destination = model / "project"
    with pytest.raises(ValueError, match="model-root"):
        init_from_inputs(
            "project", nested_destination, model, data, None, invocation()
        )
    assert not nested_destination.exists()

    destination = tmp_path / "project"
    nested_scratch = model / "scratch"
    with pytest.raises(ValueError, match="model-root"):
        init_from_inputs(
            "project", destination, model, data, nested_scratch, invocation()
        )
    assert not destination.exists() and not nested_scratch.exists()

    containing = tmp_path / "containing"
    containing.mkdir()
    inside_model = containing / "model"
    model.rename(inside_model)
    with pytest.raises(ValueError, match="model-root"):
        init_from_inputs(
            "project", containing, inside_model, data, None, invocation()
        )
    assert set(containing.iterdir()) == {inside_model}


def test_init_cli_v3_schema_and_removed_manifest_option(monkeypatch, capsys) -> None:
    captured = []
    expected = {
        "status": "initialized", "initVersion": 3, "projectName": "demo",
        "manifest": "manifest.json", "readme": "README.md", "metaConfigs": {},
        "restored": [], "elapsedSeconds": 1.0, "error": None,
    }

    def capture(*arguments):
        captured.append(arguments)
        return expected

    monkeypatch.setattr(sys, "argv", ["eqasim-sa", "init", "--project-name", "demo"])
    assert dispatch(None, None, init_factory=capture) == 0
    assert captured[0][:2] == ("demo", Path("demo"))
    assert captured[0][3] == Path("eqasim-data") and captured[0][4] is None
    assert json.loads(capsys.readouterr().out) == expected
    assert "--data-manifest" not in parser().format_help()


def test_init_cli_failure_uses_v3_schema(monkeypatch, capsys) -> None:
    def fail(*arguments):
        raise ValueError("invalid init")

    monkeypatch.setattr(sys, "argv", ["eqasim-sa", "init", "--project-name", "demo"])
    assert dispatch(None, None, init_factory=fail) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["initVersion"] == 3


def test_init_accepts_roots_reached_through_symlinked_ancestors(
    tmp_path: Path,
) -> None:
    # Spec 8.3: user-supplied roots are resolved once (macOS /tmp -> /private/tmp).
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    make_symlink(alias, real, target_is_directory=True)
    model, data = inputs(real)

    result = init_from_inputs(
        "project",
        alias / "project",
        alias / model.relative_to(real),
        alias / data.relative_to(real),
        alias / "scratch",
        invocation(),
    )

    assert result["status"] == "initialized"
    assert (real / "project" / "configs").is_dir()
    assert (real / "scratch").is_dir()


def test_init_rejects_nested_scratch_through_link_below_project(
    tmp_path: Path,
) -> None:
    model, data = inputs(tmp_path)
    root = tmp_path / "project"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    make_symlink(root / "work", external, target_is_directory=True)

    with pytest.raises(ValueError, match="must not traverse a symlink"):
        init_from_inputs(
            "project", root, model, data, root / "work" / "scratch", invocation()
        )


def test_rendered_grid_starter_is_short_and_loads(tmp_path: Path) -> None:
    model, data = inputs(tmp_path)
    project = tmp_path / "study"
    init_from_inputs("study", project, model, data, None, DEFAULT_INVOCATION)
    text = (project / "configs/01-grid.yml").read_text()
    for absent in (
        "campaign_root", "lockfile", "seed_role", "backend", "invocation", "analysis:",
        "@",
    ):
        assert absent not in text
    body = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert len(body) <= 40
    loaded = load_meta_config(project / "configs/01-grid.yml")
    assert loaded.campaign_root == (project / "campaigns" / "study-grid").resolve()


def test_custom_invocation_is_written(tmp_path: Path) -> None:
    _, root, model, _ = initialize(tmp_path)
    path = root / "configs/01-grid.yml"
    assert 'invocation: ["python", "-m", "synpp"]' in path.read_text()
    assert tuple(load_meta_config(path).execution.invocation) == invocation()


@pytest.mark.parametrize(
    ("name", "positions", "unique"),
    [
        ("00-calibration.yml", None, None),
        ("01-grid.yml", 4, 4),
        ("02-discrete-oat.yml", 8, 6),
        ("03-sobol.yml", SOBOL_POSITIONS, SOBOL_UNIQUE),
    ],
)
def test_every_rendered_starter_loads_and_plans(
    tmp_path: Path, name: str, positions: int | None, unique: int | None
) -> None:
    from eqasim_sa.adapters.application import plan_from_meta_config

    model, data = inputs(tmp_path)
    project = tmp_path / "study"
    init_from_inputs("study", project, model, data, None, DEFAULT_INVOCATION)
    path = project / "configs" / name
    load_meta_config(path)
    if positions is not None:
        planned = plan_from_meta_config(path)
        assert (planned.design_positions, planned.unique_runs) == (positions, unique)


def test_starters_need_analysis_stage_in_run(tmp_path: Path) -> None:
    model, data = inputs(tmp_path)
    (model / "config.yml").write_text(
        "working_directory: /cache\nrun: [synthesis.output]\nconfig:\n"
        "  processes: 4\n  sampling_rate: 0.001\n  data_path: /data\n  output_path: /output\n"
        "  output_prefix: nantes_\n"
    )
    project = tmp_path / "study"
    init_from_inputs("study", project, model, data, None, DEFAULT_INVOCATION)
    for name in ("00-calibration", "01-grid", "02-discrete-oat", "03-sobol"):
        with pytest.raises(ValueError, match="not in run:"):
            load_meta_config(project / "configs" / f"{name}.yml")


def test_starters_vary_only_sampling_rate(tmp_path: Path) -> None:
    model, data = inputs(tmp_path)
    project = tmp_path / "study"
    init_from_inputs("study", project, model, data, None, DEFAULT_INVOCATION)
    for name in ("00-calibration", "01-grid", "02-discrete-oat", "03-sobol"):
        assert set(load_meta_config(project / "configs" / f"{name}.yml").factors) == {
            "sampling_rate"
        }


def test_template_readme_reports_the_planned_counts(tmp_path: Path) -> None:
    _, root, _, _ = initialize(tmp_path)
    readme = (root / "README.md").read_text()
    assert "Grid target, 4 positions and 4 unique runs" in readme
    assert "adjacent-level discrete OAT target, 8 positions and 6 unique runs" in readme
    assert f"Sobol target, {SOBOL_POSITIONS} positions and {SOBOL_UNIQUE} unique runs" in readme
    assert "analysis.synthesis.population" in readme


def test_starter_paths_are_absolute_when_no_relative_path_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # On Windows, a model, data, or scratch folder on another drive than the
    # project has no relative path from configs/: os.path.relpath raises
    # ValueError. The starters must then carry the absolute Windows path.
    from eqasim_sa.adapters import templates

    def other_drive(path, start=None):  # type: ignore[no-untyped-def]
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    drives = {"model": "D:\\eqasim-france", "data": "D:\\eqasim-data", "scratch": "E:\\esa"}

    def windows_abspath(path):  # type: ignore[no-untyped-def]
        return drives.get(Path(path).name, "C:\\study\\" + Path(path).name)

    monkeypatch.setattr(templates.os.path, "relpath", other_drive)
    monkeypatch.setattr(templates.os.path, "abspath", windows_abspath)
    rendered = templates.render_project(
        tmp_path / "study",
        "study",
        tmp_path / "model",
        tmp_path / "data",
        tmp_path / "scratch",
        invocation(),
    )

    starters = {
        name: yaml.safe_load(content.decode())
        for name, content in rendered.items()
        if name.endswith(".yml")
    }
    assert len(starters) == 4
    for document in starters.values():
        assert document["model"]["root"] == "D:\\eqasim-france"
        assert document["execution"]["scratch"] == "E:\\esa"
        assert document["execution"]["machine_options"]["data_path"] == "D:\\eqasim-data"


def test_init_rerun_accepts_a_failed_attempt_left_in_scratch(tmp_path: Path) -> None:
    # A failed run leaves its attempt directory as the eqasim simulator wrote it.
    _, root, model, data = initialize(tmp_path, "project")
    attempt = root / "scratch" / "esa" / "project-grid" / "w0" / ("a" + "0" * 12)
    (attempt / "output").mkdir(parents=True)
    (attempt / "collected").mkdir()
    (attempt / "config.yml").write_text("config: {}\n")
    (attempt / "run.log").write_text("Traceback\n")

    payload = init_from_inputs("project", root, model, data, None, invocation())

    assert payload["status"] == "unchanged", payload
    assert (attempt / "collected").is_dir()


def test_init_rerun_after_a_plan_is_unchanged(tmp_path: Path) -> None:
    from eqasim_sa.adapters.application import plan_from_meta_config

    _, root, model, data = initialize(tmp_path, "project")
    plan_from_meta_config(root / "configs/00-calibration.yml")
    assert (root / "campaigns/project-calibration/plan.parquet").is_file()

    result = init_from_inputs("project", root, model, data, None, invocation())

    assert result["status"] == "unchanged"


def test_init_regenerates_the_manifest_of_a_planned_project(tmp_path: Path) -> None:
    from eqasim_sa.adapters.application import (
        plan_from_meta_config,
        run_from_meta_config,
    )

    _, root, model, data = initialize(tmp_path, "project")
    config = root / "configs/00-calibration.yml"
    plan_from_meta_config(config)
    plan = root / "campaigns/project-calibration/plan.parquet"
    sealed = plan.read_bytes()
    (data / "second.txt").write_text("second\n")

    result = init_from_inputs(
        "project", root, model, data, None, invocation(),
        regenerate_data_manifest=True,
    )

    assert result["status"] == "initialized"
    assert plan.read_bytes() == sealed
    # The sealed plan now names the drift instead of resuming silently.
    with pytest.raises(ValueError, match=r"data manifest changed since plan"):
        run_from_meta_config(config)


@pytest.mark.parametrize("name", ["grid", "other", "project-grid-structured"])
def test_init_rerun_refuses_a_campaign_directory_no_starter_creates(
    tmp_path: Path, name: str
) -> None:
    _, root, model, data = initialize(tmp_path, "project")
    (root / "campaigns" / name).mkdir()

    with pytest.raises(
        FileExistsError, match=rf"foreign content in init project: campaigns/{name}$"
    ):
        init_from_inputs("project", root, model, data, None, invocation())


def test_init_rerun_accepts_user_analysis_scripts(tmp_path: Path) -> None:
    _, root, model, data = initialize(tmp_path, "project")
    analyses = root / "configs" / "analyses"
    (analyses / "helpers").mkdir(parents=True)
    (analyses / "commute.py").write_text("print('anything')\n")
    (analyses / "helpers" / "geometry.py").write_text("x = 1\n")
    (analyses / "helpers" / "data.csv").write_text("a\n1\n")

    result = init_from_inputs("project", root, model, data, None, invocation())

    assert result["status"] == "unchanged"
    assert (analyses / "commute.py").read_text() == "print('anything')\n"


def test_init_rerun_refuses_links_in_analysis_scripts(tmp_path: Path) -> None:
    _, root, model, data = initialize(tmp_path, "project")
    analyses = root / "configs" / "analyses"
    (analyses / "helpers").mkdir(parents=True)
    target = tmp_path / "elsewhere.py"
    target.write_text("x = 1\n")
    make_symlink(analyses / "helpers" / "linked.py", target)

    with pytest.raises(
        FileExistsError, match="foreign content in init project: configs/analyses"
    ):
        init_from_inputs("project", root, model, data, None, invocation())


def test_init_rerun_refuses_a_linked_analyses_directory(tmp_path: Path) -> None:
    _, root, model, data = initialize(tmp_path, "project")
    target = tmp_path / "scripts"
    target.mkdir()
    make_symlink(root / "configs" / "analyses", target, target_is_directory=True)

    with pytest.raises(
        FileExistsError, match="foreign content in init project: configs/analyses"
    ):
        init_from_inputs("project", root, model, data, None, invocation())


@pytest.mark.parametrize("directory", [False, True])
def test_init_rerun_still_refuses_other_entries_in_configs(
    tmp_path: Path, directory: bool
) -> None:
    _, root, model, data = initialize(tmp_path, "project")
    other = root / "configs" / "notes"
    if directory:
        other.mkdir()
    else:
        other.write_text("notes\n")

    with pytest.raises(
        FileExistsError, match="foreign content in init project: configs/notes"
    ):
        init_from_inputs("project", root, model, data, None, invocation())


def test_init_cli_names_the_edited_starter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _, root, model, data = initialize(tmp_path, "project")
    config = root / "configs/01-grid.yml"
    config.write_text(config.read_text() + "# changed\n")
    monkeypatch.setattr(
        sys, "argv",
        [
            "eqasim-sa", "init", "--project-name", "project",
            "--directory", str(root), "--model-root", str(model),
            "--data-path", str(data), *(f"--invocation={item}" for item in invocation()),
        ],
    )

    assert dispatch(None, None, init_factory=init_from_inputs) == 1

    error = json.loads(capsys.readouterr().out)["error"]
    assert error.startswith("init would overwrite configs/01-grid.yml, which differs")
    assert "<path>" not in error


def test_generated_readme_names_the_selected_model_config(tmp_path: Path) -> None:
    model, data = inputs(tmp_path)
    shutil.copy(model / "config.yml", model / "config_local_nantes.yml")

    init_from_inputs(
        "project", tmp_path / "project", model, data, None, invocation(),
        Path("config_local_nantes.yml"),
    )

    flat = " ".join((tmp_path / "project/README.md").read_text().split())
    assert "`model.config` is `config_local_nantes.yml`" in flat
    assert "`model.config` is `config.yml`" not in flat


def test_generated_readme_names_the_default_model_config(tmp_path: Path) -> None:
    _, root, _, _ = initialize(tmp_path)

    flat = " ".join((root / "README.md").read_text().split())
    assert "`model.config` is `config.yml`" in flat


def test_init_cli_failure_reports_the_elapsed_time(monkeypatch, capsys) -> None:
    import time

    def slow_failure(*arguments):
        time.sleep(0.05)
        raise ValueError("invalid init")

    monkeypatch.setattr(sys, "argv", ["eqasim-sa", "init", "--project-name", "demo"])
    assert dispatch(None, None, init_factory=slow_failure) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["elapsedSeconds"] >= 0.05
