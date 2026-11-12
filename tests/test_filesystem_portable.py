import os
import re
from pathlib import Path

import pytest

from eqasim_sa.utils import filesystem
from tests.conftest import make_symlink


def test_module_does_not_import_fcntl_at_top_level() -> None:
    source = Path(filesystem.__file__).read_text(encoding="utf-8")
    assert "import fcntl" not in source and "dir_fd=" not in source


def test_resolve_root_accepts_symlinked_ancestor(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    make_symlink(tmp_path / "alias", real, target_is_directory=True)

    assert filesystem.resolve_root(tmp_path / "alias", "scratch") == real.resolve()


def test_resolve_root_requires_an_existing_path(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        filesystem.resolve_root(tmp_path / "missing", "scratch")


def test_reject_links_below_rejects_inner_link(tmp_path: Path) -> None:
    root = tmp_path / "root"
    external = tmp_path / "external"
    root.mkdir()
    external.mkdir()
    make_symlink(root / "inner", external, target_is_directory=True)

    with pytest.raises(ValueError, match="must not traverse a symlink"):
        filesystem.reject_links_below(root, root / "inner" / "file.txt", "data")


def test_reject_links_below_accepts_linked_root_and_missing_leaf(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    make_symlink(tmp_path / "alias", real, target_is_directory=True)

    filesystem.reject_links_below(
        tmp_path / "alias", tmp_path / "alias" / "sub" / "missing", "data"
    )


def test_reject_links_below_requires_path_under_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be below"):
        filesystem.reject_links_below(tmp_path / "a", tmp_path / "b", "data")


def test_anchor_detects_replaced_directory(tmp_path: Path) -> None:
    anchor = filesystem.anchor_directory(tmp_path / "d", create=True)
    assert anchor.is_current() is True
    os.rename(tmp_path / "d", tmp_path / "old")
    (tmp_path / "d").mkdir()

    assert anchor.is_current() is False


def test_anchor_rejects_symlinked_directory(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    make_symlink(tmp_path / "alias", real, target_is_directory=True)

    with pytest.raises(NotADirectoryError):
        filesystem.anchor_directory(tmp_path / "alias")


def test_anchor_child_refuses_compound_names_and_links(tmp_path: Path) -> None:
    anchor = filesystem.anchor_directory(tmp_path)
    for name in ("", ".", "..", "a/b", "a\\b"):
        with pytest.raises(ValueError, match="invalid directory name"):
            anchor.child(name)
    (tmp_path / "real").mkdir()
    make_symlink(tmp_path / "alias", tmp_path / "real", target_is_directory=True)
    with pytest.raises(NotADirectoryError):
        anchor.child("alias")

    child = anchor.child("created", create=True)
    assert child.path == tmp_path / "created" and child.is_current()


def test_open_regular_returns_readable_descriptor(tmp_path: Path) -> None:
    path = tmp_path / "file.bin"
    path.write_bytes(b"content")
    descriptor = filesystem.open_regular(path)
    try:
        assert os.read(descriptor, 100) == b"content"
    finally:
        os.close(descriptor)


def test_open_regular_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "file.bin"
    target.write_bytes(b"content")
    make_symlink(tmp_path / "link.bin", target)

    with pytest.raises(ValueError, match="non-symlink"):
        filesystem.open_regular(tmp_path / "link.bin")


def test_write_atomic_replaces_while_reader_closed(tmp_path: Path) -> None:
    destination = tmp_path / "value.bin"
    destination.write_bytes(b"old")
    with destination.open("rb") as stream:
        assert stream.read() == b"old"

    filesystem.write_bytes_atomic(b"new", destination)

    assert destination.read_bytes() == b"new"
    assert not [path for path in tmp_path.iterdir() if path != destination]


def test_write_exclusive_refuses_existing(tmp_path: Path) -> None:
    destination = tmp_path / "value.bin"
    filesystem.write_bytes_exclusive(b"first", destination)

    with pytest.raises(FileExistsError):
        filesystem.write_bytes_exclusive(b"second", destination)

    assert destination.read_bytes() == b"first"
    assert not [path for path in tmp_path.iterdir() if path != destination]


def test_temporary_names_are_short(tmp_path: Path) -> None:
    seen: list[str] = []

    def writer(path: Path) -> None:
        seen.append(path.name)
        path.write_bytes(b"content")

    filesystem.write_atomic(tmp_path / ("x" * 100 + ".bin"), writer)
    filesystem.write_exclusive(tmp_path / ("y" * 100 + ".bin"), writer)

    assert all(re.fullmatch(r"\.t[0-9a-f]{8}", name) for name in seen), seen


def test_no_handle_left_open_after_publication(tmp_path: Path) -> None:
    destination = tmp_path / "value.bin"
    destination.write_bytes(b"old")
    filesystem.write_bytes_atomic(b"new", destination)
    filesystem.write_bytes_exclusive(b"other", tmp_path / "other.bin")

    os.rename(destination, tmp_path / "moved.bin")
    os.rename(tmp_path / "other.bin", tmp_path / "moved-other.bin")
    os.rename(tmp_path, tmp_path.parent / f"{tmp_path.name}-moved")
    os.rename(tmp_path.parent / f"{tmp_path.name}-moved", tmp_path)

    assert (tmp_path / "moved.bin").read_bytes() == b"new"


def test_file_lease_releases_and_can_be_retaken(tmp_path: Path) -> None:
    lease = tmp_path / "locks" / "one.lock"
    with filesystem.file_lease(lease):
        assert lease.is_file()
    with filesystem.file_lease(lease, blocking=False):
        pass
    os.rename(lease, tmp_path / "moved.lock")


def test_secure_directory_rejects_symlink(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    make_symlink(tmp_path / "alias", tmp_path / "real", target_is_directory=True)

    with pytest.raises(ValueError, match="non-symlink directory"):
        filesystem.secure_directory(tmp_path / "alias")
