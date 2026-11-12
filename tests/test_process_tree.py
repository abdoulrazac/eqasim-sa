"""Supervision of a launched process and every descendant it starts."""

import ctypes
import importlib
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from eqasim_sa.utils import platform, process_tree
from eqasim_sa.utils.process_tree import ProcessTree

# Start a sleeping grandchild, publish its PID atomically, then either keep
# sleeping ("stay") or exit at once so the grandchild is orphaned ("leave").
SPAWNER = """
import os, subprocess, sys, time
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
partial = sys.argv[1] + ".partial"
with open(partial, "w", encoding="utf-8") as stream:
    stream.write(str(grandchild.pid))
os.replace(partial, sys.argv[1])
if sys.argv[2] == "stay":
    time.sleep(60)
"""


def wait_for(condition: Callable[[], bool], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.02)


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        synchronize = 0x00100000
        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _spawn(tmp_path: Path, mode: str) -> tuple[ProcessTree, int]:
    pid_file = tmp_path / "grandchild.pid"
    tree = ProcessTree()
    process = tree.start(
        [sys.executable, "-c", SPAWNER, str(pid_file), mode],
        stdin=subprocess.DEVNULL,
    )
    wait_for(pid_file.exists)
    if mode == "leave":
        process.wait(timeout=15)
    return tree, int(pid_file.read_text(encoding="utf-8"))


def test_terminate_kills_grandchildren(tmp_path: Path) -> None:
    tree, grandchild = _spawn(tmp_path, "stay")
    try:
        assert not tree.is_empty()
        tree.terminate(grace_seconds=1.0)
        assert tree.is_empty()
        assert not pid_alive(grandchild)
    finally:
        tree.close()


def test_orphaned_grandchild_is_still_killed(tmp_path: Path) -> None:
    tree, grandchild = _spawn(tmp_path, "leave")
    try:
        assert pid_alive(grandchild)
        assert not tree.is_empty()
        tree.terminate(grace_seconds=1.0)
        assert tree.is_empty()
        assert not pid_alive(grandchild)
    finally:
        tree.close()


def test_finished_tree_is_empty_and_terminate_is_a_no_op() -> None:
    tree = ProcessTree()
    assert tree.is_empty()
    process = tree.start([sys.executable, "-c", "pass"])
    assert process.wait(timeout=15) == 0
    wait_for(tree.is_empty)
    tree.terminate(grace_seconds=1.0)
    tree.close()
    assert tree.is_empty()


def test_close_stops_a_running_tree(tmp_path: Path) -> None:
    tree, grandchild = _spawn(tmp_path, "stay")
    tree.close()
    assert tree.is_empty()
    assert not pid_alive(grandchild)


def test_tree_cannot_be_started_twice() -> None:
    tree = ProcessTree()
    tree.start([sys.executable, "-c", "pass"])
    try:
        with pytest.raises(RuntimeError, match="already started"):
            tree.start([sys.executable, "-c", "pass"])
    finally:
        tree.close()


@pytest.mark.posix_only
def test_import_does_not_touch_windows_apis(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Windows API loaded at import time")

    monkeypatch.setattr(ctypes, "WinDLL", forbidden, raising=False)
    reloaded = importlib.reload(process_tree)
    try:
        assert reloaded._WINDOWS_API is None
        assert hasattr(reloaded, "ProcessTree")
    finally:
        monkeypatch.undo()
        importlib.reload(process_tree)


class _FakeWindowsApi:
    """Records Job Object calls so the Windows ordering is checked anywhere."""

    def __init__(self, *, assign_error: OSError | None = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self.active = 0
        self.assign_error = assign_error

    def create_job(self) -> int:
        self.calls.append(("create_job", None))
        return 77

    def assign(self, job: int, process: int) -> None:
        self.calls.append(("assign", (job, process)))
        if self.assign_error is not None:
            raise self.assign_error
        self.active = 1

    def resume(self, process: int) -> None:
        self.calls.append(("resume", process))

    def terminate(self, job: int) -> None:
        self.calls.append(("terminate", job))
        self.active = 0

    def active_processes(self, job: int) -> int:
        return self.active

    def close(self, job: int) -> None:
        self.calls.append(("close", job))


class _FakePopen:
    def __init__(self, command: list[str], **options: object) -> None:
        self.command = command
        self.options = options
        self._handle = 1234
        self.pid = 4321
        self.killed = False

    def poll(self) -> int | None:
        return None

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        return 1


def _windows_tree(
    monkeypatch: pytest.MonkeyPatch, api: _FakeWindowsApi
) -> list[_FakePopen]:
    created: list[_FakePopen] = []

    def popen(command: list[str], **options: object) -> _FakePopen:
        created.append(_FakePopen(command, **options))
        return created[-1]

    monkeypatch.setattr(platform, "IS_WINDOWS", True)
    monkeypatch.setattr(process_tree, "_windows_api", lambda: api)
    monkeypatch.setattr(process_tree, "subprocess", SimpleNamespace(
        Popen=popen, TimeoutExpired=subprocess.TimeoutExpired
    ))
    return created


def test_windows_child_starts_suspended_inside_the_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    created = _windows_tree(monkeypatch, api)
    tree = ProcessTree()
    tree.start(["eqasim"], creationflags=0x10)
    flags = created[0].options["creationflags"]
    assert flags == 0x10 | process_tree.CREATE_SUSPENDED | process_tree.CREATE_NEW_PROCESS_GROUP
    assert "start_new_session" not in created[0].options
    assert [name for name, _ in api.calls] == ["create_job", "assign", "resume"]
    assert api.calls[1] == ("assign", (77, 1234))
    assert not tree.is_empty()
    tree.terminate(grace_seconds=5.0)
    assert tree.is_empty()
    tree.close()
    assert [name for name, _ in api.calls][-2:] == ["terminate", "close"]


def test_windows_assignment_failure_kills_the_suspended_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi(assign_error=OSError("access denied"))
    created = _windows_tree(monkeypatch, api)
    tree = ProcessTree()
    with pytest.raises(OSError, match="access denied"):
        tree.start(["eqasim"])
    assert created[0].killed
    assert [name for name, _ in api.calls] == ["create_job", "assign", "close"]
    assert tree.is_empty()
