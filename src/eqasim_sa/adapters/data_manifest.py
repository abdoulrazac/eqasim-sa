import hashlib
import json
import os
import stat
import uuid
from pathlib import Path


DOMAIN = b"eqasim-sa:data-tree:v1\0"
State = tuple[int, int, int, int, int]


def generate_data_manifest(data_root: Path, destination: Path, *, replace: bool = False) -> Path:
    _reject_symlink_traversal(data_root, "data root")
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
    _reject_symlink_traversal(lexical_target.parent, "data manifest parent")
    parent = lexical_target.parent.resolve(strict=True)
    target = parent / lexical_target.name
    if parent == root or parent.is_relative_to(root):
        raise ValueError("data manifest must be outside the data tree")
    canonical = json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    document = {
        "algorithm": "sha256",
        "files": records,
        "schemaVersion": 1,
        "treeSha256": hashlib.sha256(DOMAIN + canonical).hexdigest(),
    }
    payload = (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    _publish(parent, target.name, payload, replace)
    return target


def _scan(root_descriptor: int, *, hash_files: bool) -> tuple[list[dict[str, int | str]], dict[str, State]]:
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
                descriptor = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_descriptor)
                try:
                    opened = os.fstat(descriptor)
                    if _state(opened) != _state(info) or not stat.S_ISREG(opened.st_mode):
                        raise RuntimeError(f"data entry changed while opening: {relative}")
                    digest = _hash_descriptor(descriptor) if hash_files else None
                    final = os.fstat(descriptor)
                    if _state(final) != _state(opened):
                        raise RuntimeError(f"data file changed while hashing: {relative}")
                    states[relative] = _state(final)
                    if digest is not None:
                        records.append({"path": relative, "size": final.st_size, "sha256": digest})
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


def _reject_symlink_traversal(path: Path, name: str) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"{name} must not traverse a symlink")


def _hash_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    with os.fdopen(os.dup(descriptor), "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _publish(parent: Path, name: str, payload: bytes, replace: bool) -> None:
    parent_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary_name = f".{name}.{uuid.uuid4().hex}.tmp"
    temporary_descriptor = -1
    try:
        mode = 0o644
        try:
            existing = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISREG(existing.st_mode):
                raise ValueError("existing data manifest must be a regular non-symlink file")
            if not replace:
                raise FileExistsError("data manifest already exists; use --regenerate-data-manifest")
            mode = stat.S_IMODE(existing.st_mode)
        temporary_descriptor = os.open(
            temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode, dir_fd=parent_descriptor,
        )
        os.fchmod(temporary_descriptor, mode)
        with os.fdopen(temporary_descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(
                temporary_name, name,
                src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor,
            )
        else:
            os.link(
                temporary_name, name,
                src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        os.close(parent_descriptor)
