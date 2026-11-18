import os
import sys
import time
import unicodedata
from pathlib import Path

import pytest

from eqasim_sa.adapters.eqasim.source_fingerprint import (
    MODEL_SOURCE_DOMAIN,
    model_source_sha256,
)
from tests.conftest import make_symlink


def write(root: Path, relative: str, content: bytes | str = "x = 1\n") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode()
    path.write_bytes(content)
    return path


def base_tree(root: Path) -> None:
    write(root, "a/main.py", "print(1)\n")
    write(root, "b/Foo.java", "class Foo {}\n")


def test_domain_is_versioned():
    assert MODEL_SOURCE_DOMAIN == b"eqasim-sa:model-source:v1\0"


def test_same_tree_in_two_roots_created_in_different_orders_has_same_digest(tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    write(first, "a.py", "1\n")
    write(first, "z/b.java", "2\n")
    write(second, "z/b.java", "2\n")
    write(second, "a.py", "1\n")
    digest = model_source_sha256(first)
    assert digest == model_source_sha256(second)
    assert len(digest) == 64


def test_code_edit_changes_digest(tmp_path):
    base_tree(tmp_path)
    before = model_source_sha256(tmp_path)
    write(tmp_path, "a/main.py", "print(2)\n")
    assert model_source_sha256(tmp_path) != before


def test_non_code_files_are_ignored(tmp_path):
    base_tree(tmp_path)
    before = model_source_sha256(tmp_path)
    write(tmp_path, "data.csv", "a;b\n")
    write(tmp_path, "notes.md", "hi")
    write(tmp_path, "output/result.txt", "r")
    assert model_source_sha256(tmp_path) == before


def test_hidden_pycache_and_virtualenv_dirs_are_ignored(tmp_path):
    base_tree(tmp_path)
    before = model_source_sha256(tmp_path)
    write(tmp_path, ".git/hook.py")
    write(tmp_path, ".venv/lib/x.py")
    write(tmp_path, "__pycache__/x.py")
    write(tmp_path, "venv/pyvenv.cfg", "home = /usr\n")
    write(tmp_path, "venv/lib/site.py")
    write(tmp_path, "conda/conda-meta/history", "")
    write(tmp_path, "conda/lib/site.py")
    assert model_source_sha256(tmp_path) == before


def test_excluded_dirs_are_ignored(tmp_path):
    base_tree(tmp_path)
    excluded = tmp_path / "cache"
    before = model_source_sha256(tmp_path, excluded=[excluded])
    write(tmp_path, "cache/eqasim-java/Foo.java")
    assert model_source_sha256(tmp_path, excluded=[excluded]) == before
    assert model_source_sha256(tmp_path) != before


def test_symlinked_code_is_ignored(tmp_path):
    root = tmp_path / "root"
    base_tree(root)
    outside = tmp_path / "outside"
    write(outside, "ext.py")
    before = model_source_sha256(root)
    make_symlink(root / "link.py", outside / "ext.py")
    make_symlink(root / "linkdir", outside, target_is_directory=True)
    assert model_source_sha256(root) == before


def test_crlf_and_lf_give_same_digest(tmp_path):
    lf, crlf = tmp_path / "lf", tmp_path / "crlf"
    write(lf, "a.py", b"x = 1\ny = 2\n")
    write(crlf, "a.py", b"x = 1\r\ny = 2\r\n")
    assert model_source_sha256(lf) == model_source_sha256(crlf)


def test_moving_a_file_changes_digest(tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    write(first, "a/x.py")
    write(second, "b/x.py")
    assert model_source_sha256(first) != model_source_sha256(second)


@pytest.mark.parametrize("name", ["pom.xml", "pyproject.toml", "uv.lock"])
def test_build_files_count(tmp_path, name):
    base_tree(tmp_path)
    before = model_source_sha256(tmp_path)
    write(tmp_path, name, "content")
    after = model_source_sha256(tmp_path)
    assert after != before
    write(tmp_path, name, "other")
    assert model_source_sha256(tmp_path) != after


def test_root_without_code_or_not_a_directory_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        model_source_sha256(tmp_path / "missing")
    file = write(tmp_path, "f.py")
    with pytest.raises(ValueError):
        model_source_sha256(file)
    empty = tmp_path / "empty"
    write(empty, "data.csv", "a")
    with pytest.raises(ValueError):
        model_source_sha256(empty)


def test_deadline_is_honoured(tmp_path, monkeypatch):
    base_tree(tmp_path)
    with pytest.raises(TimeoutError):
        model_source_sha256(tmp_path, deadline=time.monotonic() - 1)
    assert model_source_sha256(tmp_path, deadline=time.monotonic() + 60)


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX permissions and a non-root user",
)
def test_unreadable_directory_is_an_error(tmp_path):
    base_tree(tmp_path)
    write(tmp_path, "locked/x.py")
    locked = tmp_path / "locked"
    locked.chmod(0)
    try:
        with pytest.raises(OSError):
            model_source_sha256(tmp_path)
    finally:
        locked.chmod(0o700)


def test_nfd_and_nfc_filenames_give_same_digest(tmp_path):
    nfc = unicodedata.normalize("NFC", "café.py")
    nfd = unicodedata.normalize("NFD", "café.py")
    assert nfc != nfd
    first, second = tmp_path / "one", tmp_path / "two"
    write(first, nfc)
    write(second, nfd)
    assert model_source_sha256(first) == model_source_sha256(second)
