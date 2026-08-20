import json
import os
import shutil
import shlex
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from eqasim_sa.adapters.application import init_from_inputs
from eqasim_sa.cli import dispatch, parser


METHODS = ("calibration", "grid", "morris", "sobol")


def inputs(tmp_path: Path) -> tuple[Path, Path]:
    model = tmp_path / "model"
    model.mkdir()
    (model / "config_local_nantes.yml").write_text(
        "working_directory: /cache\nrun: [old]\nconfig:\n"
        "  processes: 4\n  sampling_rate: 0.001\n  random_seed: 1234\n"
        "  matching_age_scheme: matching_6\n  analysis_age_scheme: analysis_7\n"
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
        "initVersion": 2,
        "projectName": "project",
        "manifest": "manifest.json",
        "readme": "README.md",
        "metaConfigs": {
            "calibration": "configs/00-calibration.yml",
            "grid": "configs/01-grid.yml",
            "morris": "configs/02-morris.yml",
            "sobol": "configs/03-sobol.yml",
        },
        "elapsedSeconds": result["elapsedSeconds"],
        "error": None,
    }
    assert {path.name for path in root.iterdir()} == {
        "manifest.json", "README.md", "configs", "campaigns", "scratch",
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
        assert f"campaign: project-{method}" in text
        assert "HTS adjust with INSEE population weights" not in text
        assert text.count("references: [INSEE]") == 1
        assert text.count("references: [HTS]") == 2
    assert "matching_age_scheme:\n    values: [matching_6, analysis_7]" in (
        root / "configs/01-grid.yml"
    ).read_text()
    assert "matching_age_scheme" not in (root / "configs/02-morris.yml").read_text()
    assert "matching_age_scheme:\n    values: [matching_6, analysis_7]" in (
        root / "configs/03-sobol.yml"
    ).read_text()
    readme = (root / "README.md").read_text()
    assert "did not\nplan a campaign" in readme
    project = Path(__file__).resolve().parents[1]
    runner = shlex.join(
        ["uv", "run", "--project", str(project), "--locked", "eqasim-sa"]
    )
    assert f"SA=({runner})" in readme
    assert readme.index('"${SA[@]}" plan') < readme.index('"${SA[@]}" check')


@pytest.mark.parametrize(
    ("config_lines", "message"),
    [
        ([], "matching_age_scheme"),
        (["  matching_age_scheme: [matching_6]"], "matching_age_scheme"),
        (["  matching_age_scheme: ''", "  analysis_age_scheme: analysis_7"], "matching_age_scheme"),
        (["  matching_age_scheme: unknown", "  analysis_age_scheme: analysis_7"], "matching_age_scheme"),
        (["  matching_age_scheme: analysis_7", "  analysis_age_scheme: analysis_7"], "matching_age_scheme"),
        (["  matching_age_scheme: matching_6"], "analysis_age_scheme"),
        (["  matching_age_scheme: matching_6", "  analysis_age_scheme: ''"], "analysis_age_scheme"),
        (["  matching_age_scheme: matching_6", "  analysis_age_scheme: unknown"], "analysis_age_scheme"),
        (["  matching_age_scheme: matching_6", "  analysis_age_scheme: matching_6"], "analysis_age_scheme"),
    ],
)
def test_init_rejects_model_without_required_scalar_age_contract(
    tmp_path: Path, config_lines: list[str], message: str,
) -> None:
    model, data = inputs(tmp_path)
    config = model / "custom.yml"
    config.write_text(
        "working_directory: /cache\nrun: []\nconfig:\n  sampling_rate: 0.001\n"
        + "\n".join(config_lines)
        + "\n"
    )
    destination = tmp_path / "project"
    with pytest.raises(ValueError, match=message):
        init_from_inputs(
            "project", destination, model, data, None, invocation(), Path("custom.yml")
        )
    assert not destination.exists()


def test_init_accepts_custom_model_with_explicit_historical_age_schemes(
    tmp_path: Path,
) -> None:
    model, data = inputs(tmp_path)
    (model / "custom.yml").write_text(
        "working_directory: /cache\nrun: []\nconfig:\n"
        "  sampling_rate: 0.001\n  matching_age_scheme: matching_6\n"
        "  analysis_age_scheme: analysis_7\n"
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
        "eqasim_sa.adapters.application.generate_data_manifest",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("data rehashed")),
    )
    repeated = init_from_inputs(
        "my-study", root, model, data, None, invocation()
    )

    assert result["status"] == "initialized" and repeated["status"] == "unchanged"
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in states} == states


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


def test_init_rerun_accepts_user_created_campaign_state(tmp_path: Path) -> None:
    _, root, model, data = initialize(tmp_path, "project")
    campaign = root / "campaigns/grid"
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
    for name in (
        "persons", "households", "activities", "trips", "vehicle_types", "vehicles",
        "age", "distance", "license", "nbr_vehicle", "travel", "age_purpose",
    ):
        (archive / f"{name}.parquet").write_bytes(b"owned")
    pq.write_table(pa.table({"value": [1.0]}), campaign / "indicators.parquet")
    (campaign / "analysis-generation.json").write_text("{}\n")

    protected = {
        path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_nlink)
        for path in (campaign / "plan.parquet", marker, backup)
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
    linked.symlink_to(data / "input.txt")
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
    with pytest.raises(FileExistsError, match="templates"):
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


def test_init_cli_v2_schema_and_removed_manifest_option(monkeypatch, capsys) -> None:
    captured = []
    expected = {
        "status": "initialized", "initVersion": 2, "projectName": "demo",
        "manifest": "manifest.json", "readme": "README.md", "metaConfigs": {},
        "elapsedSeconds": 1.0, "error": None,
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
