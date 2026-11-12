import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from eqasim_sa.adapters import data_manifest
from eqasim_sa.adapters.data_manifest import (
    DOMAIN,
    generate_data_manifest,
    load_data_manifest,
    verify_data_manifest,
    verify_data_tree_state,
)
from tests.conftest import make_symlink


def test_generates_deterministic_auditable_manifest(tmp_path: Path) -> None:
    data = tmp_path / "eqasim-data"
    (data / "z").mkdir(parents=True)
    (data / "a.txt").write_bytes(b"alpha")
    (data / "z" / "b.bin").write_bytes(b"beta")
    destination = tmp_path / "eqasim-data.manifest.json"

    generate_data_manifest(data, destination)
    first = destination.read_bytes()
    document = json.loads(first)
    assert "generatedDate" not in document
    assert document["files"] == [
        {"path": "a.txt", "size": 5, "sha256": hashlib.sha256(b"alpha").hexdigest()},
        {"path": "z/b.bin", "size": 4, "sha256": hashlib.sha256(b"beta").hexdigest()},
    ]
    canonical = json.dumps(
        document["files"], sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()
    assert document["treeSha256"] == hashlib.sha256(DOMAIN + canonical).hexdigest()

    previous_umask = os.umask(0o077)
    try:
        generate_data_manifest(data, destination, replace=True)
    finally:
        os.umask(previous_umask)
    assert destination.read_bytes() == first


def test_refuses_overwrite_and_changes_only_after_explicit_regeneration(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    source = data / "value"
    source.write_text("first")
    destination = tmp_path / "data.manifest.json"
    generate_data_manifest(data, destination)
    destination.chmod(0o640)
    before = destination.read_bytes()
    source.write_text("second")

    with pytest.raises(FileExistsError, match="regenerate"):
        generate_data_manifest(data, destination)
    assert destination.read_bytes() == before

    generate_data_manifest(data, destination, replace=True)
    assert destination.read_bytes() != before
    assert stat.S_IMODE(destination.stat().st_mode) == 0o640


def test_rejects_symlinks_special_entries_and_destination_inside_tree(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("value")
    link = data / "link"
    make_symlink(link, outside)
    with pytest.raises(ValueError, match="symlink"):
        generate_data_manifest(data, tmp_path / "manifest.json")
    link.unlink()

    with pytest.raises(ValueError, match="outside"):
        generate_data_manifest(data, data / "manifest.json")



def test_user_supplied_roots_are_resolved_through_links(tmp_path: Path) -> None:
    # Spec 8.3: the data root and the manifest parent are resolved once at
    # entry, so links reaching them (e.g. /tmp -> /private/tmp) are accepted.
    data = tmp_path / "data"
    data.mkdir()
    (data / "input.bin").write_bytes(b"sealed")
    direct = generate_data_manifest(data, tmp_path / "direct.json")

    linked_root = tmp_path / "linked-root"
    make_symlink(linked_root, data, target_is_directory=True)
    through_root = generate_data_manifest(linked_root, tmp_path / "root.json")

    linked_parent = tmp_path / "linked-parent"
    make_symlink(linked_parent, tmp_path, target_is_directory=True)
    through_parent = generate_data_manifest(data, linked_parent / "parent.json")

    assert through_root.read_bytes() == direct.read_bytes()
    assert through_parent == tmp_path.resolve() / "parent.json"
    assert through_parent.read_bytes() == direct.read_bytes()
    with pytest.raises(ValueError, match="outside"):
        generate_data_manifest(linked_root, data / "manifest.json")


@pytest.mark.posix_only
def test_rejects_fifo_in_data_tree(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    fifo = data / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="non-regular"):
        generate_data_manifest(data, tmp_path / "manifest.json")


def test_empty_tree_has_stable_manifest(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    destination = tmp_path / "data.manifest.json"
    generate_data_manifest(data, destination)
    document = json.loads(destination.read_bytes())
    assert document["files"] == []
    assert document["treeSha256"] == hashlib.sha256(DOMAIN + b"[]").hexdigest()


@pytest.mark.parametrize("damage", ["extra", "missing", "content"])
def test_runtime_verification_rejects_exact_tree_damage(
    tmp_path: Path, damage: str
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    source = data / "input.bin"
    source.write_bytes(b"sealed")
    destination = tmp_path / "manifest.json"
    generate_data_manifest(data, destination)
    manifest = load_data_manifest(destination)
    if damage == "extra":
        (data / "extra.bin").write_bytes(b"extra")
    elif damage == "missing":
        source.unlink()
    else:
        source.write_bytes(b"SEALed")

    with pytest.raises(ValueError, match=damage if damage != "content" else "content"):
        verify_data_manifest(data, manifest)


def test_runtime_metadata_guard_rejects_change_after_one_content_verification(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    source = data / "input.bin"
    source.write_bytes(b"sealed")
    destination = tmp_path / "manifest.json"
    generate_data_manifest(data, destination)
    state = verify_data_manifest(data, load_data_manifest(destination))

    source.touch()

    with pytest.raises(RuntimeError, match="metadata changed"):
        verify_data_tree_state(state)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda document: document.update(schemaVersion=True),
        lambda document: document.update(algorithm="sha512"),
        lambda document: document["files"].append(document["files"][0]),
        lambda document: document["files"][0].update(path="../escape"),
        lambda document: document.update(treeSha256="0" * 64),
    ],
)
def test_central_manifest_parser_rejects_schema_damage(
    tmp_path: Path, mutation
) -> None:  # type: ignore[no-untyped-def]
    data = tmp_path / "data"
    data.mkdir()
    (data / "input").write_text("value")
    destination = tmp_path / "manifest.json"
    generate_data_manifest(data, destination)
    document = json.loads(destination.read_text())
    mutation(document)
    destination.write_text(json.dumps(document))

    with pytest.raises(ValueError, match="invalid data manifest"):
        load_data_manifest(destination)


def test_rejects_tree_changed_after_an_earlier_file_was_hashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    first = data / "a"
    first.write_text("before")
    (data / "b").write_text("later")
    real_hash = data_manifest.descriptor_sha256
    calls = 0

    def mutate_after_second_hash(descriptor: int) -> str:
        nonlocal calls
        result = real_hash(descriptor)
        calls += 1
        if calls == 2:
            first.write_text("after")
        return result

    monkeypatch.setattr(data_manifest, "descriptor_sha256", mutate_after_second_hash)
    with pytest.raises(RuntimeError, match="changed"):
        generate_data_manifest(data, tmp_path / "manifest.json")
    assert not (tmp_path / "manifest.json").exists()


def test_rejects_root_path_replaced_while_scanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "value").write_text("content")
    detached = tmp_path / "detached"
    real_scan = data_manifest._scan
    calls = 0

    def replace_after_second_scan(descriptor: int, *, hash_files: bool):
        nonlocal calls
        result = real_scan(descriptor, hash_files=hash_files)
        calls += 1
        if calls == 2:
            data.rename(detached)
            data.mkdir()
        return result

    monkeypatch.setattr(data_manifest, "_scan", replace_after_second_scan)
    with pytest.raises(RuntimeError, match="data root changed"):
        generate_data_manifest(data, tmp_path / "manifest.json")
    assert not (tmp_path / "manifest.json").exists()
