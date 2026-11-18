"""eqasim-sa runs against a plain eqasim folder: no Git anywhere."""

import json
import sys
from pathlib import Path

import pytest

from eqasim_sa.adapters.application import init_from_inputs
from eqasim_sa.adapters.data_manifest import generate_data_manifest
from eqasim_sa.adapters.eqasim.source_fingerprint import model_source_sha256
from eqasim_sa.adapters.execution_seal import create_execution_seal
from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.adapters.meta_config_defaults import DEFAULT_INVOCATION
from eqasim_sa.core.context_identity import CONTEXT_DOMAIN, CONTEXT_KEYS
from eqasim_sa.ports.simulator import EXECUTION_EVIDENCE_DOMAIN, ExecutionEvidence


MODEL_CONFIG = (
    "working_directory: cache\nrun: [synthesis.output, analysis.synthesis.population]\nconfig:\n  a: 0\n  b: x\n"
    "  data_path: ../data\n  output_path: output\n  processes: 1\n"
    "  random_seed: 9\n"
)


def plain_model(root: Path) -> Path:
    """An eqasim copy: code, a tracked-looking config.yml, an untracked copy."""

    model = root / "eqasim"
    (model / "synthesis").mkdir(parents=True)
    (model / "synthesis" / "output.py").write_text("def execute(context):\n    pass\n")
    (model / "pom.xml").write_text("<project/>\n")
    (model / "config.yml").write_text(MODEL_CONFIG.replace("a: 0", "a: 5"))
    (model / "config_nantes.yml").write_text(MODEL_CONFIG)
    (model / "uv.lock").write_text("version = 1\n")
    data = root / "data"
    data.mkdir()
    (data / "input.txt").write_text("sealed data\n")
    return model


def project(root: Path, model: Path, config: str, *, execution: bool = False) -> Path:
    study = root / "study"
    study.mkdir()
    generate_data_manifest(root / "data", study / "manifest.json")
    text = (
        "campaign: demo\ncampaign_root: campaign\n"
        f"model:\n  root: {json.dumps(str(model))}\n  config: {json.dumps(config)}\n"
        "  data_manifest: manifest.json\n"
        "experiment:\n  method: grid\n  seeds: [41, 42]\n"
        "parameters:\n  a: [1, 2]\n  b: [x, y]\n"
        "outputs:\n  persons: {}\n"
    )
    if execution:
        scratch = root / "scratch"
        scratch.mkdir()
        text += (
            "execution:\n  backend: local\n  workers: 1\n"
            f"  scratch: {json.dumps(str(scratch))}\n"
            f"  invocation: {json.dumps([sys.executable, '-m', 'synpp'])}\n"
        )
    meta = study / "meta.yml"
    meta.write_text(text)
    return meta


def test_plain_directory_model_loads(tmp_path: Path) -> None:
    model = plain_model(tmp_path)
    assert not (model / ".git").exists()

    loaded = load_meta_config(project(tmp_path, model, "config.yml"))

    assert set(loaded.context_descriptor) == CONTEXT_KEYS == {
        "eqasimSourceSha256",
        "dataManifestSha256",
        "lockfileSha256",
        "eqasimSaVersion",
    }
    assert loaded.context_descriptor["eqasimSourceSha256"] == model_source_sha256(
        model, (model / "cache", model / "output")
    )
    assert CONTEXT_DOMAIN == b"eqasim-sa:context:v3\0"


def test_untracked_config_next_to_config_yml_loads(tmp_path: Path) -> None:
    model = plain_model(tmp_path)

    loaded = load_meta_config(project(tmp_path, model, "config_nantes.yml"))

    assert loaded.model_config == (model / "config_nantes.yml").resolve()
    assert loaded.model_document["config"]["a"] == 0


def test_absolute_model_config_loads(tmp_path: Path) -> None:
    model = plain_model(tmp_path)
    elsewhere = tmp_path / "configs" / "nantes.yml"
    elsewhere.parent.mkdir()
    elsewhere.write_text(MODEL_CONFIG.replace("a: 0", "a: 7"))

    loaded = load_meta_config(project(tmp_path, model, str(elsewhere)))

    assert loaded.model_config == elsewhere.resolve()
    assert loaded.model_document["config"]["a"] == 7


def test_relative_model_config_is_read_from_model_root(tmp_path: Path) -> None:
    model = plain_model(tmp_path)
    meta = project(tmp_path, model, "config_nantes.yml")
    # A same-named file next to the meta-config must not be picked up.
    (meta.parent / "config_nantes.yml").write_text(MODEL_CONFIG.replace("a: 0", "a: 9"))

    loaded = load_meta_config(meta)

    assert loaded.model_config == (model / "config_nantes.yml").resolve()
    assert loaded.model_document["config"]["a"] == 0


def test_code_edit_changes_context_hash(tmp_path: Path) -> None:
    model = plain_model(tmp_path)
    meta = project(tmp_path, model, "config_nantes.yml")
    before = load_meta_config(meta)

    (model / "synthesis" / "output.py").write_text("def execute(context):\n    return 1\n")
    after = load_meta_config(meta)

    assert after.context_hash != before.context_hash
    assert (
        after.context_descriptor["eqasimSourceSha256"]
        != before.context_descriptor["eqasimSourceSha256"]
    )


def test_cache_inside_model_root_does_not_change_context(tmp_path: Path) -> None:
    model = plain_model(tmp_path)
    meta = project(tmp_path, model, "config_nantes.yml")
    before = load_meta_config(meta)

    for directory in ("cache", "output"):
        (model / directory / "pipeline").mkdir(parents=True)
        (model / directory / "pipeline" / "Run.java").write_text("class Run {}\n")
        (model / directory / "pipeline" / "stage.py").write_text("X = 1\n")
    after = load_meta_config(meta)

    assert after.context_hash == before.context_hash


def _message(error: BaseException, tmp_path: Path) -> str:
    return str(error).replace(str(tmp_path), "<tmp>").replace(
        str(tmp_path.resolve()), "<tmp>"
    )


def test_errors_never_mention_git(tmp_path: Path) -> None:
    cases = []
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    empty_model = plain_model(empty_root)
    for path in empty_model.rglob("*"):
        if path.is_file() and path.suffix != ".yml":
            path.unlink()
    cases.append(project(empty_root, empty_model, "config_nantes.yml"))
    missing_root = tmp_path / "missing"
    missing_root.mkdir()
    cases.append(project(missing_root, plain_model(missing_root), "absent.yml"))
    noroot_root = tmp_path / "noroot"
    (noroot_root / "data").mkdir(parents=True)
    cases.append(project(noroot_root, noroot_root / "nowhere", "config.yml"))

    plain_root = tmp_path / "plain"
    plain_root.mkdir()
    plain = plain_model(plain_root)
    cases.append(project(plain_root, plain, "config_nantes.yml", execution=True))

    for meta in cases[:-1]:
        with pytest.raises(Exception) as caught:
            load_meta_config(meta)
        assert "git" not in _message(caught.value, tmp_path).lower()
    seal = create_execution_seal(load_meta_config(cases[-1]))
    (plain / "synthesis" / "output.py").unlink()
    with pytest.raises(RuntimeError) as caught:
        seal.guard()
    assert "git" not in _message(caught.value, tmp_path).lower()
    assert "git" not in str(caught.value.__cause__).replace(
        str(tmp_path), ""
    ).lower()


def test_execution_seal_refuses_code_change(tmp_path: Path) -> None:
    model = plain_model(tmp_path)
    loaded = load_meta_config(
        project(tmp_path, model, "config_nantes.yml", execution=True)
    )
    seal = create_execution_seal(loaded)
    evidence = ExecutionEvidence.from_bytes(seal.evidence)
    assert evidence.model_source_sha256 == loaded.context_descriptor["eqasimSourceSha256"]

    # Caches of the eqasim config are not code under study.
    (model / "cache").mkdir()
    (model / "cache" / "Cached.java").write_text("class Cached {}\n")
    seal.guard()

    (model / "synthesis" / "output.py").write_text("def execute(context):\n    return 2\n")
    with pytest.raises(RuntimeError, match="eqasim source changed"):
        seal.guard()


def _evidence(**changes: object) -> ExecutionEvidence:
    values = dict(
        context_hash="0" * 64,
        data_tree_sha256="1" * 64,
        environment_sha256="2" * 64,
        environment_variables=("PATH",),
        executable_sha256="3" * 64,
        invocation=("/usr/bin/test",),
        model_source_sha256="4" * 64,
        model_config_sha256="5" * 64,
        eqasim_sa_sha256="6" * 64,
    )
    values.update(changes)
    return ExecutionEvidence(**values)  # type: ignore[arg-type]


def test_evidence_v3_round_trip_and_v2_rejected() -> None:
    evidence = _evidence()
    payload = evidence.canonical_bytes()
    document = json.loads(payload)

    assert evidence.schema_version == 3
    assert document["schemaVersion"] == 3
    assert document["modelSourceSha256"] == "4" * 64
    assert "modelCommit" not in document
    assert ExecutionEvidence.from_bytes(payload) == evidence
    assert EXECUTION_EVIDENCE_DOMAIN == b"eqasim-sa:execution-evidence:v3\0"

    with pytest.raises(ValueError):
        _evidence(schema_version=2)
    with pytest.raises(ValueError):
        _evidence(model_source_sha256="a" * 40)
    legacy = dict(document)
    legacy.pop("modelSourceSha256")
    legacy["modelCommit"] = "a" * 40
    legacy["schemaVersion"] = 2
    with pytest.raises(ValueError, match="invalid or noncanonical"):
        ExecutionEvidence.from_bytes(
            json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
        )


INIT_MODEL_CONFIG = (
    "working_directory: cache\nrun: [synthesis.output, analysis.synthesis.population]\nconfig:\n"
    "  processes: 4\n  sampling_rate: 0.001\n  random_seed: 1234\n"
    "  data_path: /data\n  output_path: /output\n  output_prefix: nantes_\n"
)


def init_inputs(root: Path) -> tuple[Path, Path]:
    model = root / "eqasim"
    model.mkdir()
    (model / "config.yml").write_text(INIT_MODEL_CONFIG)
    (model / "run.py").write_text("print('eqasim')\n")
    (model / "uv.lock").write_text("version = 1\n")
    data = root / "data"
    data.mkdir()
    (data / "input.txt").write_text("first\n")
    return model, data


def test_init_without_git_and_absolute_model_config(tmp_path: Path) -> None:
    model, data = init_inputs(tmp_path)
    default = tmp_path / "default"
    init_from_inputs("default", default, model, data, None, DEFAULT_INVOCATION)
    assert 'config: "config.yml"' in (default / "configs/01-grid.yml").read_text()
    assert load_meta_config(default / "configs/01-grid.yml").model_config == (
        model / "config.yml"
    ).resolve()

    outside = tmp_path / "settings" / "config_nantes.yml"
    outside.parent.mkdir()
    outside.write_text(INIT_MODEL_CONFIG)
    study = tmp_path / "study"
    result = init_from_inputs(
        "study", study, model, data, None, DEFAULT_INVOCATION, model_config=outside
    )

    assert result["status"] == "initialized"
    assert f"config: {json.dumps(str(outside))}" in (
        study / "configs/01-grid.yml"
    ).read_text()
    loaded = load_meta_config(study / "configs/01-grid.yml")
    assert loaded.model_config == outside.resolve()
    assert not (model / ".git").exists()


def test_init_rerun_names_modified_file(tmp_path: Path) -> None:
    model, data = init_inputs(tmp_path)
    study = tmp_path / "study"
    init_from_inputs("study", study, model, data, None, DEFAULT_INVOCATION)
    grid = study / "configs" / "01-grid.yml"
    grid.write_text(grid.read_text().replace("seeds: [1201, 1202]", "seeds: [1, 2]"))

    with pytest.raises(FileExistsError) as caught:
        init_from_inputs("study", study, model, data, None, DEFAULT_INVOCATION)

    assert str(caught.value) == f"init would overwrite modified file {grid.resolve()}"
