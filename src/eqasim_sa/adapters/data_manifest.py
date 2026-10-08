"""Create, parse, and verify immutable manifests of input data trees."""

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from eqasim_sa.utils.filesystem import (
    descriptor_sha256,
    publish_bytes,
    reject_symlink_traversal,
)


DOMAIN = b"eqasim-sa:data-tree:v1\0"
State = tuple[int, int, int, int, int]
SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class DataManifestEntry:
    """Digest and byte size of one safe relative path in a data tree."""

    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class DataManifest:
    """Canonical ordered file inventory and its domain-separated tree digest."""

    files: tuple[DataManifestEntry, ...]
    tree_sha256: str


@dataclass(frozen=True)
class DataTreeState:
    """Filesystem metadata captured while verifying an immutable data tree."""

    root: Path
    root_state: State
    entries: tuple[tuple[str, State], ...]


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document = {}
    for name, value in pairs:
        if name in document:
            raise ValueError(f"duplicate manifest key: {name}")
        document[name] = value
    return document


def parse_data_manifest(payload: bytes) -> DataManifest:
    """Parse canonical manifest bytes and reject ambiguous or malformed input."""

    try:
        document = json.loads(payload, object_pairs_hook=_unique_object)
        if not isinstance(document, dict) or set(document) != {
            "algorithm",
            "files",
            "schemaVersion",
            "treeSha256",
        }:
            raise ValueError
        if (
            document["schemaVersion"] != 1
            or type(document["schemaVersion"]) is not int
        ):
            raise ValueError
        if document["algorithm"] != "sha256":
            raise ValueError
        raw_files = document["files"]
        if not isinstance(raw_files, list):
            raise ValueError
        files = []
        for item in raw_files:
            if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
                raise ValueError
            name, size, digest = item["path"], item["size"], item["sha256"]
            if (
                not isinstance(name, str)
                or not name
                or name.startswith("/")
                or "\\" in name
                or "\0" in name
                or any(part in {"", ".", ".."} for part in name.split("/"))
                or type(size) is not int
                or size < 0
                or not isinstance(digest, str)
                or SHA256.fullmatch(digest) is None
            ):
                raise ValueError
            name.encode("utf-8")
            files.append(DataManifestEntry(name, size, digest))
        names = [entry.path for entry in files]
        if len(names) != len(set(names)) or names != sorted(
            names, key=lambda name: name.encode("utf-8")
        ):
            raise ValueError
        records = _records(files)
        canonical = json.dumps(
            records, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        tree_sha256 = document["treeSha256"]
        if (
            not isinstance(tree_sha256, str)
            or SHA256.fullmatch(tree_sha256) is None
            or hashlib.sha256(DOMAIN + canonical).hexdigest() != tree_sha256
        ):
            raise ValueError
        return DataManifest(tuple(files), tree_sha256)
    except (TypeError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("invalid data manifest") from error


def load_data_manifest(path: Path) -> DataManifest:
    """Load a regular, non-symlink manifest file from disk."""

    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError
        return parse_data_manifest(path.read_bytes())
    except OSError as error:
        raise ValueError("invalid data manifest") from error


def verify_data_manifest(data_root: Path, manifest: DataManifest) -> DataTreeState:
    """Match a live data tree against every file and digest in a manifest."""

    root, root_state, records, before = _read_tree(data_root, hash_files=True)
    _, current_root_state, _, after = _read_tree(root, hash_files=False)
    if root_state != current_root_state or before != after:
        raise RuntimeError("data tree changed while verifying its manifest")
    expected = {entry.path: entry for entry in manifest.files}
    actual = {record["path"]: record for record in records}
    missing = sorted(
        set(expected) - set(actual), key=lambda name: name.encode("utf-8")
    )
    extra = sorted(set(actual) - set(expected), key=lambda name: name.encode("utf-8"))
    if missing:
        raise ValueError(f"data tree is missing manifest file: {missing[0]}")
    if extra:
        raise ValueError(f"data tree contains an extra file: {extra[0]}")
    for entry in manifest.files:
        record = actual[entry.path]
        if record["size"] != entry.size or record["sha256"] != entry.sha256:
            raise ValueError(f"data tree content does not match manifest: {entry.path}")
    canonical = json.dumps(
        records, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    if hashlib.sha256(DOMAIN + canonical).hexdigest() != manifest.tree_sha256:
        raise ValueError("data tree hash does not match manifest")
    return DataTreeState(root, root_state, tuple(sorted(before.items())))


def verify_data_tree_state(expected: DataTreeState) -> None:
    """Fail if metadata changed after a successful manifest verification."""

    root, root_state, _, states = _read_tree(expected.root, hash_files=False)
    if (
        root != expected.root
        or root_state != expected.root_state
        or tuple(sorted(states.items())) != expected.entries
    ):
        raise RuntimeError("data tree metadata changed after manifest verification")


def _records(
    entries: list[DataManifestEntry] | tuple[DataManifestEntry, ...],
) -> list[dict[str, int | str]]:
    return [
        {"path": entry.path, "size": entry.size, "sha256": entry.sha256}
        for entry in entries
    ]


def generate_data_manifest(
    data_root: Path, destination: Path, *, replace: bool = False
) -> Path:
    """Hash a stable data tree and atomically publish its canonical manifest."""

    reject_symlink_traversal(data_root, "data root")
    root = data_root.resolve(strict=True)
    root_descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        root_state = _state(os.fstat(root_descriptor))
        records, before = _scan(root_descriptor, hash_files=True)
        _, after = _scan(root_descriptor, hash_files=False)
    finally:
        os.close(root_descriptor)
    if before != after:
        raise RuntimeError("data tree changed while generating its manifest")
    try:
        current_root = os.stat(root, follow_symlinks=False)
    except FileNotFoundError as error:
        raise RuntimeError("data root changed while generating its manifest") from error
    if not stat.S_ISDIR(current_root.st_mode) or _state(current_root) != root_state:
        raise RuntimeError("data root changed while generating its manifest")

    lexical_target = Path(os.path.abspath(destination))
    reject_symlink_traversal(lexical_target.parent, "data manifest parent")
    parent = lexical_target.parent.resolve(strict=True)
    target = parent / lexical_target.name
    if parent == root or parent.is_relative_to(root):
        raise ValueError("data manifest must be outside the data tree")
    canonical = json.dumps(
        records, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    document = {
        "algorithm": "sha256",
        "files": records,
        "schemaVersion": 1,
        "treeSha256": hashlib.sha256(DOMAIN + canonical).hexdigest(),
    }
    payload = (
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()
    try:
        publish_bytes(payload, target, replace=replace, default_mode=0o644)
    except FileExistsError as error:
        raise FileExistsError(
            "data manifest already exists; use --regenerate-data-manifest"
        ) from error
    except ValueError as error:
        if "existing destination" in str(error):
            raise ValueError(
                "existing data manifest must be a regular non-symlink file"
            ) from error
        raise
    return target


def _read_tree(
    data_root: Path, *, hash_files: bool
) -> tuple[Path, State, list[dict[str, int | str]], dict[str, State]]:
    reject_symlink_traversal(data_root, "data root")
    root = data_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("data root must be a directory")
    root_descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        root_state = _state(os.fstat(root_descriptor))
        records, states = _scan(root_descriptor, hash_files=hash_files)
    finally:
        os.close(root_descriptor)
    try:
        current_root = os.stat(root, follow_symlinks=False)
    except FileNotFoundError as error:
        raise RuntimeError("data root changed while scanning") from error
    if not stat.S_ISDIR(current_root.st_mode) or _state(current_root) != root_state:
        raise RuntimeError("data root changed while scanning")
    return root, root_state, records, states


def _scan(
    root_descriptor: int, *, hash_files: bool
) -> tuple[list[dict[str, int | str]], dict[str, State]]:
    records: list[dict[str, int | str]] = []
    states: dict[str, State] = {}

    def visit(directory_descriptor: int, prefix: str) -> None:
        directory_before = _state(os.fstat(directory_descriptor))
        states[f"{prefix}/"] = directory_before
        with os.scandir(directory_descriptor) as entries:
            ordered = list(entries)
        try:
            ordered.sort(key=lambda entry: entry.name.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ValueError("data paths must be valid UTF-8") from error
        for entry in ordered:
            try:
                entry.name.encode("utf-8")
            except UnicodeEncodeError as error:
                raise ValueError("data paths must be valid UTF-8") from error
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"data tree contains a symlink: {relative}")
            if stat.S_ISDIR(info.st_mode):
                child = os.open(
                    entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_descriptor,
                )
                try:
                    if _state(os.fstat(child)) != _state(info):
                        raise RuntimeError(f"data entry changed while opening: {relative}")
                    visit(child, relative)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                descriptor = os.open(
                    entry.name,
                    os.O_RDONLY | os.O_NOFOLLOW,
                    dir_fd=directory_descriptor,
                )
                try:
                    opened = os.fstat(descriptor)
                    if _state(opened) != _state(info) or not stat.S_ISREG(opened.st_mode):
                        raise RuntimeError(f"data entry changed while opening: {relative}")
                    digest = descriptor_sha256(descriptor) if hash_files else None
                    final = os.fstat(descriptor)
                    if _state(final) != _state(opened):
                        raise RuntimeError(f"data file changed while hashing: {relative}")
                    states[relative] = _state(final)
                    if digest is not None:
                        records.append(
                            {
                                "path": relative,
                                "size": final.st_size,
                                "sha256": digest,
                            }
                        )
                finally:
                    os.close(descriptor)
            else:
                raise ValueError(f"data tree contains a non-regular entry: {relative}")
        if _state(os.fstat(directory_descriptor)) != directory_before:
            raise RuntimeError(f"data directory changed while scanning: {prefix or '.'}")

    visit(root_descriptor, "")
    return records, states


def _state(info: os.stat_result) -> State:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
