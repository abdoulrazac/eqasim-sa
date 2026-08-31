import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from eqasim_sa.adapters import application
from eqasim_sa.core.software_identity import read_software_identity
from eqasim_sa.ports.simulator import (
    EXECUTION_EVIDENCE_V1_DOMAIN,
    EXECUTION_EVIDENCE_V2_DOMAIN,
    ExecutionEvidence,
)


def _write_package(root: Path, names: tuple[str, ...]) -> None:
    payloads = {
        "__init__.py": b"VALUE = 1\n",
        "core/runner.py": b"def run():\n    return 1\n",
        "templates/campaign.yml": b"campaign: demo\n",
        "ignored.txt": b"not installed behavior\n",
    }
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payloads[name])


def test_software_identity_digest_is_canonical_and_deterministic(
    tmp_path: Path,
) -> None:
    names = (
        "__init__.py",
        "core/runner.py",
        "templates/campaign.yml",
        "ignored.txt",
    )
    first_root = tmp_path / "first" / "eqasim_sa"
    second_root = tmp_path / "second" / "eqasim_sa"
    _write_package(first_root, names)
    _write_package(second_root, tuple(reversed(names)))

    first = read_software_identity(first_root, version="test")
    second = read_software_identity(second_root, version="test")

    assert first.sha256 == second.sha256
    assert tuple(item.relative_path for item in first.source_states) == (
        "eqasim_sa/__init__.py",
        "eqasim_sa/core/runner.py",
        "eqasim_sa/templates/campaign.yml",
    )
    assert all(item.path.is_file() and not item.path.is_symlink() for item in first.source_states)
    assert tuple(item.state for item in first.source_states) != tuple(
        item.state for item in second.source_states
    )

    (second_root / "templates" / "campaign.yml").write_text("campaign: changed\n")
    changed = read_software_identity(second_root, version="test")
    assert changed.sha256 != first.sha256


def test_software_identity_rejects_symlinked_python_source(tmp_path: Path) -> None:
    root = tmp_path / "eqasim_sa"
    root.mkdir()
    (root / "__init__.py").write_text("")
    target = tmp_path / "foreign.py"
    target.write_text("VALUE = 1\n")
    (root / "linked.py").symlink_to(target)

    with pytest.raises(ValueError, match="must not be a symlink"):
        read_software_identity(root, version="test")


V1_PAYLOAD = (
    b'{"contextHash":"'
    + b"c" * 64
    + b'","dataTreeSha256":"'
    + b"1" * 64
    + b'","environmentSha256":"'
    + b"2" * 64
    + b'","environmentVariables":["PATH"],"evidenceSha256":"'
    + b"485ac51f23d1ccb16071525a57e81f3406cb825b257bda2a61996afe852c45a8"
    + b'","executableSha256":"'
    + b"3" * 64
    + b'","invocation":["/usr/bin/test"],"modelCommit":"'
    + b"4" * 40
    + b'","modelConfigSha256":"'
    + b"5" * 64
    + b'","schemaVersion":1}'
)


def test_execution_evidence_strictly_parses_schema_v1() -> None:
    evidence = ExecutionEvidence.from_bytes(V1_PAYLOAD)

    assert evidence.schema_version == 1
    assert evidence.eqasim_sa_sha256 is None
    assert evidence.canonical_bytes() == V1_PAYLOAD

    document = json.loads(V1_PAYLOAD)
    document["eqasimSaSha256"] = "6" * 64
    descriptor = {key: value for key, value in document.items() if key != "evidenceSha256"}
    encoded = json.dumps(
        descriptor, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    document["evidenceSha256"] = hashlib.sha256(
        EXECUTION_EVIDENCE_V1_DOMAIN + encoded
    ).hexdigest()
    tampered = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")

    with pytest.raises(ValueError, match="invalid or noncanonical"):
        ExecutionEvidence.from_bytes(tampered)


def test_execution_evidence_schema_v2_binds_runner_digest() -> None:
    evidence = ExecutionEvidence(
        context_hash="c" * 64,
        data_tree_sha256="1" * 64,
        environment_sha256="2" * 64,
        environment_variables=("PATH",),
        executable_sha256="3" * 64,
        invocation=("/usr/bin/test",),
        model_commit="4" * 40,
        model_config_sha256="5" * 64,
        schema_version=2,
        eqasim_sa_sha256="6" * 64,
    )
    payload = evidence.canonical_bytes()
    document = json.loads(payload)
    evidence_sha256 = document.pop("evidenceSha256")
    descriptor = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")

    assert document["eqasimSaSha256"] == "6" * 64
    assert evidence_sha256 == hashlib.sha256(
        EXECUTION_EVIDENCE_V2_DOMAIN + descriptor
    ).hexdigest()
    assert ExecutionEvidence.from_bytes(payload) == evidence


def test_scientific_run_refuses_no_seal_before_design_or_simulation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "meta.yml"
    path.write_text("sealed\n")
    meta = SimpleNamespace(
        charter=SimpleNamespace(status="scientific"),
        execution=SimpleNamespace(data_path=None),
    )
    monkeypatch.setattr(application, "load_meta_config", lambda _: meta)
    monkeypatch.setattr(
        application,
        "_design",
        lambda *_: (_ for _ in ()).throw(AssertionError("design reached")),
    )
    monkeypatch.setattr(
        application,
        "EqasimSimulator",
        lambda *_: (_ for _ in ()).throw(AssertionError("simulation reached")),
    )

    with pytest.raises(ValueError, match="effective data_path and execution seal"):
        application.run_from_meta_config(path)


def test_scientific_smoke_refuses_no_seal_before_simulation() -> None:
    meta = SimpleNamespace(
        charter=SimpleNamespace(status="scientific"),
        execution=SimpleNamespace(data_path=None),
    )

    with pytest.raises(ValueError, match="effective data_path and execution seal"):
        application._smoke(meta, ())
