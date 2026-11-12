import errno
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from eqasim_sa.utils import platform
from tests.conftest import make_symlink

_CHILD_LOCK = """
import os, sys
from eqasim_sa.utils import platform
fd = os.open(sys.argv[1], os.O_RDWR)
sys.exit(0 if platform.lock_exclusive_nonblocking(fd) else 3)
"""

_CHILD_READ = """
import os, sys
fd = os.open(sys.argv[1], os.O_RDONLY)
sys.stdout.write(repr(os.read(fd, 10)))
"""


def _child_try_lock(path: Path) -> bool:
    result = subprocess.run([sys.executable, "-c", _CHILD_LOCK, str(path)])
    assert result.returncode in (0, 3), result.returncode
    return result.returncode == 0


def test_second_process_cannot_take_lock(tmp_path):
    path = tmp_path / "lease.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        assert platform.lock_exclusive_nonblocking(fd) is True
        assert _child_try_lock(path) is False
        platform.unlock(fd)
        assert _child_try_lock(path) is True
    finally:
        os.close(fd)


# Only meaningful on Windows (mandatory locks); flock is advisory on POSIX.
def test_locked_lease_content_remains_readable(tmp_path):
    path = tmp_path / "lease.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        os.write(fd, b"x")
        assert platform.lock_exclusive_nonblocking(fd) is True
        result = subprocess.run(
            [sys.executable, "-c", _CHILD_READ, str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout == repr(b"x")
        platform.unlock(fd)
    finally:
        os.close(fd)


def _patch_replace(monkeypatch, failures):
    calls = []
    real = os.replace

    def flaky(source, destination):
        calls.append(1)
        if len(calls) <= failures:
            raise PermissionError("busy")
        real(source, destination)

    monkeypatch.setattr(platform, "IS_WINDOWS", True)
    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr(time, "sleep", lambda _delay: None)
    return calls


def test_replace_retries_transient_permission_error(tmp_path, monkeypatch):
    source = tmp_path / "src"
    destination = tmp_path / "dst"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    calls = _patch_replace(monkeypatch, failures=2)
    platform.replace_with_retry(source, destination)
    assert destination.read_bytes() == b"new"
    assert len(calls) == 3


def test_replace_gives_up_after_retries(tmp_path, monkeypatch):
    source = tmp_path / "src"
    source.write_bytes(b"new")
    calls = _patch_replace(monkeypatch, failures=10**6)
    with pytest.raises(PermissionError):
        platform.replace_with_retry(source, tmp_path / "dst")
    assert len(calls) == len(platform.RETRY_DELAYS) + 1


def test_replace_does_not_retry_off_windows(tmp_path, monkeypatch):
    source = tmp_path / "src"
    source.write_bytes(b"new")
    calls = _patch_replace(monkeypatch, failures=10**6)
    monkeypatch.setattr(platform, "IS_WINDOWS", False)
    with pytest.raises(PermissionError):
        platform.replace_with_retry(source, tmp_path / "dst")
    assert len(calls) == 1


def test_rename_lets_file_exists_error_through_immediately(tmp_path, monkeypatch):
    calls = []

    def exists(source, destination):
        calls.append(1)
        raise FileExistsError("taken")

    monkeypatch.setattr(platform, "IS_WINDOWS", True)
    monkeypatch.setattr(os, "rename", exists)
    monkeypatch.setattr(time, "sleep", lambda _delay: None)
    with pytest.raises(FileExistsError):
        platform.rename_with_retry(tmp_path / "a", tmp_path / "b")
    assert len(calls) == 1


def test_rename_retries_permission_error(tmp_path, monkeypatch):
    source = tmp_path / "src"
    destination = tmp_path / "dst"
    source.write_bytes(b"new")
    calls = []
    real = os.rename

    def flaky(src, dst):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError("busy")
        real(src, dst)

    monkeypatch.setattr(platform, "IS_WINDOWS", True)
    monkeypatch.setattr(os, "rename", flaky)
    monkeypatch.setattr(time, "sleep", lambda _delay: None)
    platform.rename_with_retry(source, destination)
    assert destination.read_bytes() == b"new"
    assert len(calls) == 2


def test_is_link_detects_symlink(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    make_symlink(link, target, target_is_directory=True)
    assert platform.is_link(link)
    assert not platform.is_link(target)


@pytest.mark.windows_only
def test_is_link_detects_junction(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "junction"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        check=True,
        capture_output=True,
    )
    assert platform.is_link(junction)


def test_fsync_directory_accepts_existing_directory(tmp_path):
    platform.fsync_directory(tmp_path)


def test_hostname_is_nonempty():
    assert platform.hostname()


def test_current_uid_matches_platform():
    if platform.IS_WINDOWS:
        assert platform.current_uid() is None
    else:
        assert platform.current_uid() == os.getuid()


class _FakeMsvcrt:
    LK_NBLCK = 2
    LK_UNLCK = 0

    def __init__(self, error):
        self.error = error

    def locking(self, descriptor, mode, count):
        raise self.error


def _windows_lock(tmp_path, monkeypatch, error):
    monkeypatch.setattr(platform, "IS_WINDOWS", True)
    monkeypatch.setattr(platform, "msvcrt", _FakeMsvcrt(error), raising=False)
    fd = os.open(tmp_path / "lease.lock", os.O_RDWR | os.O_CREAT)
    try:
        return platform.lock_exclusive_nonblocking(fd)
    finally:
        os.close(fd)


def test_windows_lock_held_returns_false(tmp_path, monkeypatch):
    error = PermissionError(errno.EACCES, "held")
    assert _windows_lock(tmp_path, monkeypatch, error) is False


def test_windows_lock_unexpected_error_propagates(tmp_path, monkeypatch):
    with pytest.raises(OSError) as raised:
        _windows_lock(tmp_path, monkeypatch, OSError(errno.EBADF, "bad"))
    assert raised.value.errno == errno.EBADF
