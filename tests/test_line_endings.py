import hashlib
import json
from pathlib import Path

import pytest

from eqasim_sa.adapters.data_manifest import generate_data_manifest
from eqasim_sa.adapters.execution_seal import create_execution_seal
from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.core.context_identity import CONTEXT_DOMAIN
from eqasim_sa.core.software_identity import (
    SOFTWARE_IDENTITY_DOMAIN,
    read_software_identity,
)
from eqasim_sa.utils.text_identity import (
    canonical_json_sha256,
    lf_bytes,
    lf_sha256,
)


MODEL_CONFIG = (
    b"working_directory: /ignored\nrun: [synthesis.output, analysis.synthesis.population]\n"
    b"config:\n  a: 0\n  b: x\n"
    b"  data_path: ../data\n  processes: 1\n  random_seed: 9\n"
)


def _crlf(content: bytes) -> bytes:
    return lf_bytes(content).replace(b"\n", b"\r\n")


def _model(root: Path) -> Path:
    model = root / "model"
    model.mkdir(parents=True)
    (model / "config.yml").write_bytes(MODEL_CONFIG)
    (model / "pipeline.py").write_bytes(b"STAGES = []\n")
    return model


def _project(root: Path, model: Path, data: Path, *, crlf: bool) -> Path:
    root.mkdir(parents=True)
    manifest = root / "manifest.json"
    generate_data_manifest(data, manifest)
    lockfile = root / "uv.lock"
    lockfile.write_bytes(b"version = 1\n[[package]]\nname = \"demo\"\n")
    if crlf:
        manifest.write_bytes(_crlf(manifest.read_bytes()))
        lockfile.write_bytes(_crlf(lockfile.read_bytes()))
    meta = root / "meta.yml"
    meta.write_text(
        "campaign: demo\ncampaign_root: campaign\n"
        f"model:\n  config: {json.dumps(str(model / 'config.yml'))}\n"
        f"  root: {json.dumps(str(model))}\n"
        "  data_manifest: manifest.json\n  lockfile: uv.lock\n"
        "analysis:\n  completeness_threshold: 1.0\n"
        "experiment:\n  method: grid\n  seeds: [41, 42]\n"
        "parameters:\n  a:\n    values: [1, 2]\n  b:\n    values: [x, y]\n"
        "outputs:\n  persons: {}\n",
        encoding="utf-8",
    )
    return meta


def _data(root: Path) -> Path:
    data = root / "data"
    data.mkdir(parents=True)
    (data / "input.txt").write_bytes(b"sealed data\n")
    return data


def test_lf_helpers_normalize_only_crlf(tmp_path: Path) -> None:
    path = tmp_path / "text"
    path.write_bytes(b"a\r\nb\rc\n")

    assert lf_bytes(b"a\r\nb\rc\n") == b"a\nb\rc\n"
    assert lf_sha256(path) == hashlib.sha256(b"a\nb\rc\n").hexdigest()


def test_context_identical_for_crlf_and_lf_inputs(tmp_path: Path) -> None:
    model = _model(tmp_path)
    data = _data(tmp_path)
    lf_meta = _project(tmp_path / "lf", model, data, crlf=False)
    crlf_meta = _project(tmp_path / "crlf", model, data, crlf=True)
    assert (lf_meta.parent / "uv.lock").read_bytes() != (
        crlf_meta.parent / "uv.lock"
    ).read_bytes()

    lf = load_meta_config(lf_meta)
    crlf = load_meta_config(crlf_meta)

    assert crlf.context_descriptor == lf.context_descriptor
    assert crlf.context_hash == lf.context_hash
    assert lf.context_descriptor["lockfileSha256"] == lf_sha256(
        crlf_meta.parent / "uv.lock"
    )
    assert lf.context_descriptor["dataManifestSha256"] == canonical_json_sha256(
        (lf_meta.parent / "manifest.json").read_bytes()
    )


def test_manifest_hash_ignores_whitespace_and_key_order() -> None:
    assert canonical_json_sha256(b'{"b":1,"a":2}\r\n') == canonical_json_sha256(
        b'{ "a": 2, "b": 1 }'
    )
    assert canonical_json_sha256(b'{"a":2}') != canonical_json_sha256(b'{"a":3}')


@pytest.mark.parametrize(
    "payload", [b'{"a":1,"a":2}', b'{"a":NaN}', b"not json", b"\xff"]
)
def test_manifest_hash_rejects_ambiguous_json(payload: bytes) -> None:
    with pytest.raises(ValueError, match="cannot be canonicalized"):
        canonical_json_sha256(payload)


def test_software_identity_ignores_crlf(tmp_path: Path) -> None:
    payloads = {
        "__init__.py": b"VALUE = 1\n",
        "core/runner.py": b"def run():\n    return 1\n",
        "templates/campaign.yml": b"campaign: demo\nname: x\n",
    }
    for variant, convert in (("lf", False), ("crlf", True)):
        for name, payload in payloads.items():
            path = tmp_path / variant / "eqasim_sa" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_crlf(payload) if convert else payload)

    lf = read_software_identity(tmp_path / "lf" / "eqasim_sa", version="test")
    crlf = read_software_identity(tmp_path / "crlf" / "eqasim_sa", version="test")

    assert crlf.sha256 == lf.sha256
    assert [item.sha256 for item in crlf.source_states] != [
        item.sha256 for item in lf.source_states
    ]


def test_crlf_model_config_and_code_keep_identity(tmp_path: Path) -> None:
    model = _model(tmp_path)
    data = _data(tmp_path)
    meta = _project(tmp_path / "project", model, data, crlf=False)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    meta.write_text(
        meta.read_text(encoding="utf-8")
        + "execution:\n  backend: local\n  workers: 1\n"
        + f"  scratch: {json.dumps(str(scratch))}\n"
        + '  invocation: ["python3", "-m", "synpp"]\n',
        encoding="utf-8",
    )
    lf = load_meta_config(meta)
    (model / "config.yml").write_bytes(_crlf(MODEL_CONFIG))
    (model / "pipeline.py").write_bytes(_crlf(b"STAGES = []\n"))

    crlf = load_meta_config(meta)
    evidence = json.loads(create_execution_seal(crlf).evidence)

    assert crlf.model_bytes == _crlf(MODEL_CONFIG)
    assert crlf.context_hash == lf.context_hash
    assert evidence["modelConfigSha256"] == hashlib.sha256(MODEL_CONFIG).hexdigest()


def test_domains_bumped() -> None:
    assert CONTEXT_DOMAIN == b"eqasim-sa:context:v3\0"
    assert SOFTWARE_IDENTITY_DOMAIN == b"eqasim-sa:software-identity:v2\0"


def _builtin_indicator():  # type: ignore[no-untyped-def]
    from eqasim_sa.core.indicators import TableArtifact, TotalVariationIndicator

    return TotalVariationIndicator(
        TableArtifact(
            "distribution", "distribution.csv", ",", "class", "synthetic", ("reference",)
        ),
        "reference",
    )


def test_builtin_indicator_definition_ignores_crlf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import inspect

    import eqasim_sa.core.indicators as indicators

    original = Path(inspect.getsourcefile(indicators.TotalVariationIndicator))
    crlf_copy = tmp_path / original.name
    crlf_copy.write_bytes(_crlf(original.read_bytes()))
    lf_definition = indicators.analysis_identity(_builtin_indicator())

    monkeypatch.setattr(
        indicators.inspect, "getsourcefile", lambda value: str(crlf_copy)
    )
    crlf_definition = indicators.analysis_identity(_builtin_indicator())

    assert crlf_copy.read_bytes() != original.read_bytes()
    # Equal definitions hash identically under INDICATOR_DEFINITION_DOMAIN.
    assert crlf_definition == lf_definition
    assert json.dumps(lf_definition).count(
        hashlib.sha256(lf_bytes(original.read_bytes())).hexdigest()
    ) == 1
    assert indicators.INDICATOR_DEFINITION_DOMAIN == (
        b"eqasim-sa:indicator-definition:v3\0"
    )


def test_manifest_hash_rejects_deeply_nested_json() -> None:
    with pytest.raises(ValueError, match="cannot be canonicalized"):
        canonical_json_sha256(b"[" * 100_000 + b"]" * 100_000)
