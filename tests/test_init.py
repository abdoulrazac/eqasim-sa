import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from eqasim_sa.adapters.application import (
    check_from_meta_config,
    estimate_from_meta_config,
    init_from_inputs,
    run_from_meta_config,
)
from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.cli import parser


def git(*arguments: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=cwd, check=True, text=True, capture_output=True
    ).stdout.strip()


def inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    model = tmp_path / "model"
    model.mkdir()
    git("init", "-q", cwd=model)
    git("config", "user.email", "test@example.test", cwd=model)
    git("config", "user.name", "Test", cwd=model)
    (model / "config_local_nantes.yml").write_text(
        "working_directory: /path/to/my/cache\nrun: [old]\nconfig:\n"
        "  processes: 4\n  sampling_rate: 0.001\n  random_seed: 1234\n"
        "  data_path: /path/to/my/data\n  output_path: /path/to/my/output\n"
        "  output_prefix: nantes_\n"
    )
    (model / "uv.lock").write_text("version = 1\n")
    git("add", ".", cwd=model)
    git("commit", "-qm", "fixture", cwd=model)
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}\n")
    data, scratch = tmp_path / "data", tmp_path / "scratch"
    data.mkdir()
    scratch.mkdir()
    return model, manifest, data, scratch


def spy_invocation() -> tuple[str, ...]:
    return sys.executable, str(Path(__file__).with_name("eqasim_spy.py"))


def test_init_renders_complete_nantes_resources_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model, manifest, data, scratch = inputs(tmp_path)
    destination = tmp_path / "starter"
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(tmp_path / "record.json"))
    before = {
        path: path.read_bytes()
        for path in (
            model / "config_local_nantes.yml",
            model / "uv.lock",
            manifest,
        )
    }

    first = init_from_inputs(
        destination, model, manifest, data, scratch, spy_invocation()
    )
    target, calibration = (
        destination / "nantes.yml",
        destination / "nantes-calibration.yml",
    )
    target_text = target.read_text()
    calibration_text = calibration.read_text()
    loaded = load_meta_config(target)
    calibration_loaded = load_meta_config(calibration)

    assert first["status"] == "initialized" and first["calibrationRuns"] == 2
    assert set(first) == {
        "status",
        "metaConfig",
        "calibrationMetaConfig",
        "plan",
        "calibrationRuns",
        "elapsedSeconds",
        "error",
    }
    assert first["metaConfig"] == "nantes.yml"
    assert loaded.campaign == "nantes-starter"
    assert loaded.seed_role == "replication"
    assert loaded.factors == {"sampling_rate": [0.001]} and loaded.seeds == (1234, 5678)
    assert loaded.execution.overrides == {"data_path": data.resolve(), "processes": 1}
    assert loaded.calibration_meta_configs == (calibration.resolve(),)
    assert len(loaded.indicators) == len(loaded.indicator_requirements) == 5
    assert calibration_loaded.campaign == "nantes-starter-calibration"
    assert (
        len(tuple((calibration_loaded.campaign_root / "runs").glob("*.parquet"))) == 2
    )
    assert (
        len(tuple((calibration_loaded.campaign_root / "archives").glob("*/*.parquet")))
        == 24
    )
    assert "config_local_nantes.yml" in target_text
    assert "eqasim-sa init --directory ." in target_text
    init_command = next(
        line[2:]
        for line in target_text.splitlines()
        if line.startswith("# eqasim-sa init ")
    )
    parsed = parser().parse_args(shlex.split(init_command)[1:])
    assert parsed.invocation == list(spy_invocation())
    assert parsed.data_manifest is not None
    assert "eqasim-sa check --meta-config nantes.yml" in target_text
    assert target_text.rstrip().endswith("eqasim-sa run --meta-config nantes.yml")
    for term in (
        "matching_minimum_observations",
        "matching_attributes",
        "income_assignation_method",
        "sampling_rate",
        "projection_year",
        "random_seed",
        "recodage",
        "non transportable",
        "Epic 2",
        "persons",
        "households",
        "activities",
        "trips",
        "vehicle_types",
        "vehicles",
        "age",
        "distance",
        "license",
        "nbr_vehicle",
        "travel",
        "age_purpose",
        "INSEE",
        "HTS",
        "HTS adjust with INSEE population weights",
        "Proportion_EQASIM",
        "matching_failure_rate",
        "gini",
        "dispersion inter-seeds",
        "execution.backend",
        "SLURM",
        "shared",
        "node-local",
        "timeout_min",
        "mem_gb",
        "partition",
        "account",
        "qos",
        "array parallelism",
    ):
        assert term in target_text
    assert "method: morris" in target_text and "seed_role: replication" in target_text
    for morris_term in (
        "trajectories: 12",
        "design_seed: 20260814",
        "seeds: [101, 102, 103]",
        "values: [0.005, 0.01, 0.05]",
        "values: [5, 10, 20, 40]",
        "numeric",
        "strictly increasing",
        "adjacent",
        "completeness_threshold: 1.0",
        "at least one indicator",
        "T <= M",
        "signed int64",
        "ordered, non-empty and unique",
        "never random_seed",
        "each factor exactly once",
        "finite and representable",
        "fresh campaign_root",
        "no interpolation",
    ):
        assert morris_term in target_text
    assert "method: sobol" in target_text and "seed_role: factor" in target_text
    for sobol_term in (
        "base_sample_size: 1024",
        "design_seed: 20260814",
        "bootstrap_resamples: 999",
        "confidence_level: 0.95",
        "completeness_threshold: 1.0",
        "values: [101, 102, 103, 104]",
        "N(2D+2)",
        "A/B/AB/BA",
        "power of two",
        "equal-width",
        "counts in D",
        "no separate inter-seed dispersion",
        "fresh campaign_root",
    ):
        assert sobol_term in target_text
    for statement in (
        "scratch exists at the same absolute path and is node-local",
        "campaign must be unique among distinct campaigns sharing this scratch",
        "workdir is disposable and may disappear between jobs",
    ):
        assert all(statement in text for text in (target_text, calibration_text))
    assert set(yaml.safe_load(target_text)["execution"]) == {
        "backend",
        "workers",
        "scratch",
        "invocation",
        "overrides",
    }
    states = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (
            target,
            calibration,
            loaded.campaign_root / "plan.parquet",
            calibration_loaded.campaign_root / "plan.parquet",
        )
    }
    second = init_from_inputs(
        destination, model, manifest, data, scratch, spy_invocation()
    )
    assert second["status"] == "unchanged" and second["calibrationRuns"] == 2
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in states
    } == states
    assert {path: path.read_bytes() for path in before} == before


def test_init_accepts_an_explicit_tracked_model_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model, manifest, data, scratch = inputs(tmp_path)
    selected = model / "config_nantes.yml"
    selected.write_bytes((model / "config_local_nantes.yml").read_bytes())
    git("add", selected.name, cwd=model)
    git("commit", "-qm", "alternative config", cwd=model)
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(tmp_path / "record.json"))

    destination = tmp_path / "starter"
    init_from_inputs(
        destination,
        model,
        manifest,
        data,
        scratch,
        spy_invocation(),
        Path(selected.name),
    )

    assert "config_nantes.yml" in (destination / "nantes.yml").read_text()
    init_command = next(
        line[2:]
        for line in (destination / "nantes.yml").read_text().splitlines()
        if line.startswith("# eqasim-sa init ")
    )
    assert parser().parse_args(shlex.split(init_command)[1:]).model_config == Path(
        selected.name
    )


def test_init_defaults_to_local_nantes_config() -> None:
    arguments = parser().parse_args(["init"])
    assert arguments.directory == Path("nantes-starter")
    assert arguments.model_root == Path("eqasim-france")
    assert arguments.model_config == Path("config_local_nantes.yml")
    assert arguments.data_manifest is None
    assert arguments.regenerate_data_manifest is False
    assert arguments.data_path == Path("eqasim-data")
    assert arguments.scratch == Path("scratch")


def test_init_resolves_adjacent_data_manifest_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    model, manifest, data, scratch = inputs(tmp_path)
    adjacent = Path(f"{data.resolve()}.manifest.json")
    adjacent.write_bytes(manifest.read_bytes())
    scratch.rmdir()
    assert not scratch.exists()
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(tmp_path / "record.json"))

    init_from_inputs(
        tmp_path / "starter", model, None, data, scratch, spy_invocation(),
    )

    loaded = load_meta_config(tmp_path / "starter" / "nantes.yml")
    assert loaded.context_descriptor["dataManifestSha256"]
    assert adjacent.name in (tmp_path / "starter" / "nantes.yml").read_text()
    assert scratch.is_dir()
    init_command = next(
        line[2:] for line in (tmp_path / "starter" / "nantes.yml").read_text().splitlines()
        if line.startswith("# eqasim-sa init ")
    )
    assert parser().parse_args(shlex.split(init_command)[1:]).data_manifest is None


def test_init_rejects_symlinked_input_ancestors(tmp_path: Path) -> None:
    model, manifest, data, scratch = inputs(tmp_path)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(ValueError, match="traverse a symlink"):
        init_from_inputs(
            linked_parent / "starter", model, manifest, data, scratch, spy_invocation()
        )
    with pytest.raises(ValueError, match="traverse a symlink"):
        init_from_inputs(
            tmp_path / "starter",
            linked_parent / "model",
            manifest,
            data,
            scratch,
            spy_invocation(),
        )


def test_init_resumes_only_owned_state_and_refuses_unsafe_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model, manifest, data, scratch = inputs(tmp_path)
    destination = tmp_path / "starter"
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(tmp_path / "record.json"))
    monkeypatch.setenv("EQASIM_SPY_FAIL_SEED", "5678")
    with pytest.raises(RuntimeError, match="calibration"):
        init_from_inputs(destination, model, manifest, data, scratch, spy_invocation())
    calibration = destination / "calibration-campaign"
    assert len(tuple((calibration / "runs").glob("*.parquet"))) == 1
    monkeypatch.delenv("EQASIM_SPY_FAIL_SEED")
    assert (
        init_from_inputs(destination, model, manifest, data, scratch, spy_invocation())[
            "calibrationRuns"
        ]
        == 2
    )
    foreign = destination / "notes.txt"
    foreign.write_text("do not overwrite")
    with pytest.raises(FileExistsError, match="foreign"):
        init_from_inputs(destination, model, manifest, data, scratch, spy_invocation())
    assert foreign.read_text() == "do not overwrite"
    foreign.unlink()
    nested_foreign = destination / "calibration-campaign" / "archives" / "foreign"
    nested_foreign.mkdir()
    with pytest.raises(FileExistsError, match="foreign content"):
        init_from_inputs(destination, model, manifest, data, scratch, spy_invocation())
    nested_foreign.rmdir()
    archive = next((destination / "calibration-campaign" / "archives").iterdir())
    artifact_foreign = archive / "notes.txt"
    artifact_foreign.write_text("do not overwrite")
    with pytest.raises(FileExistsError, match="foreign content"):
        init_from_inputs(destination, model, manifest, data, scratch, spy_invocation())
    assert artifact_foreign.read_text() == "do not overwrite"
    artifact_foreign.unlink()
    dirty = model / "dirty"
    dirty.write_text("x")
    with pytest.raises(ValueError, match="clean"):
        init_from_inputs(
            tmp_path / "other", model, manifest, data, scratch, spy_invocation()
        )
    dirty.unlink()
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="symlink"):
        init_from_inputs(link, model, manifest, data, scratch, spy_invocation())


def test_generated_default_calibration_drives_check_estimate_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model, manifest, data, scratch = inputs(tmp_path)
    destination = tmp_path / "starter"
    record = tmp_path / "record.json"
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(record))
    init_from_inputs(destination, model, manifest, data, scratch, spy_invocation())
    target = destination / "nantes.yml"
    checked = check_from_meta_config(target, (), False)
    estimated = estimate_from_meta_config(target, ())
    ran = run_from_meta_config(target)
    noop = run_from_meta_config(target)
    invocation = json.loads(record.read_text())["argv"]

    assert checked["status"] == "valid" and checked["elapsedSeconds"] < 60
    assert (
        estimated["status"],
        estimated["runs"],
        estimated["designPositions"],
        estimated["calibrationRuns"],
    ) == ("estimated", 2, 2, 2)
    assert ran["completed"] == 2 and noop["total"] == 0
    assert invocation[invocation.index("--data_path") + 1] == str(data.resolve())
    assert invocation[invocation.index("--processes") + 1] == "1"


def test_calibration_and_overrides_do_not_change_plan_identity(tmp_path: Path) -> None:
    from eqasim_sa.adapters.application import plan_from_meta_config

    model, manifest, data, scratch = inputs(tmp_path)
    destination = tmp_path / "starter"
    os.environ["EQASIM_SPY_RECORD"] = str(tmp_path / "record.json")
    init_from_inputs(destination, model, manifest, data, scratch, spy_invocation())
    target = destination / "nantes.yml"
    plan = destination / "target-campaign" / "plan.parquet"
    before = plan.read_bytes(), plan.stat().st_mtime_ns
    text = target.read_text().replace(
        f"data_path: {data.resolve()}", f"data_path: {data.resolve()}/.."
    )
    text = text.replace("processes: 1", "processes: 2")
    text = text.replace("nantes-calibration.yml", "./nantes-calibration.yml")
    target.write_text(text)
    result = plan_from_meta_config(target)
    assert (
        result.status == "unchanged"
        and (plan.read_bytes(), plan.stat().st_mtime_ns) == before
    )


@pytest.mark.parametrize(
    "override",
    [
        {"random_seed": 1},
        {"output_path": "/x"},
        {"unknown": 1},
        {"data_path": 1},
        {"processes": True},
        {"matching_attributes": ["age"]},
    ],
)
def test_overrides_are_strictly_limited(tmp_path: Path, override: dict) -> None:
    import yaml

    model, manifest, data, scratch = inputs(tmp_path)
    destination = tmp_path / "starter"
    os.environ["EQASIM_SPY_RECORD"] = str(tmp_path / "record.json")
    init_from_inputs(destination, model, manifest, data, scratch, spy_invocation())
    target = destination / "nantes.yml"
    document = yaml.safe_load(target.read_text())
    document["execution"]["overrides"] = override
    target.write_text(yaml.safe_dump(document, sort_keys=False))
    with pytest.raises((TypeError, ValueError), match="override"):
        load_meta_config(target)


def test_init_cli_schema_hides_absolute_paths(monkeypatch, capsys) -> None:
    from eqasim_sa.cli import dispatch

    expected = {
        "status": "initialized",
        "metaConfig": "nantes.yml",
        "calibrationMetaConfig": "nantes-calibration.yml",
        "plan": "target-campaign/plan.parquet",
        "calibrationRuns": 2,
        "elapsedSeconds": 1.0,
        "error": None,
    }
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eqasim-sa",
            "init",
            "--directory",
            "starter",
            "--model-root",
            "model",
            "--data-manifest",
            "manifest",
            "--data-path",
            "data",
            "--scratch",
            "scratch",
        ],
    )
    captured = []
    def capture(*args):
        captured.append(args)
        return expected
    assert dispatch(None, None, init_factory=capture) == 0
    assert captured[0][5] == (
        "uv", "run", "--locked", "python", "-m", "synpp",
    )
    assert json.loads(capsys.readouterr().out) == expected

    assert (
        dispatch(
            None,
            None,
            init_factory=lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        == 130
    )
    interrupted = json.loads(capsys.readouterr().out)
    assert set(interrupted) == set(expected)
    assert (
        interrupted["status"] == "failed"
        and interrupted["error"] == "KeyboardInterrupt"
    )
