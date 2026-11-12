import os
import socket
import threading
from pathlib import Path

import pytest

from eqasim_sa.adapters.eqasim.workspace import WorkerWorkspace
from tests.conftest import make_symlink


def workspace(
    scratch: Path,
    *,
    slot: int = 0,
    context_hash: str = "a" * 64,
    interrupted=lambda: False,  # type: ignore[no-untyped-def]
) -> WorkerWorkspace:
    return WorkerWorkspace(scratch, "campaign", slot, context_hash, interrupted)


@pytest.mark.parametrize("slot", [0, 2, 47])
def test_workspace_owns_one_shared_slot_grammar(tmp_path: Path, slot: int) -> None:
    current = workspace(tmp_path, slot=slot)

    assert current.root == tmp_path / "eqasim-sa/campaign/workers" / str(slot)
    assert current.cache == current.root / "cache" / ("a" * 64)

    with current.session():
        current.prepare_cache()
        current.mark_clean()

    WorkerWorkspace.validate_scratch(tmp_path, {"campaign"})


@pytest.mark.parametrize("slot", [-1, True, 1.0, "2"])
def test_workspace_rejects_noncanonical_slots(tmp_path: Path, slot: object) -> None:
    with pytest.raises(ValueError, match="slot"):
        workspace(tmp_path, slot=slot)  # type: ignore[arg-type]


@pytest.mark.parametrize("context_hash", [None, "", "../bad", "bad/name"])
def test_workspace_requires_a_safe_context_hash(
    tmp_path: Path, context_hash: object
) -> None:
    with pytest.raises((TypeError, ValueError), match="context_hash"):
        workspace(tmp_path, context_hash=context_hash)  # type: ignore[arg-type]


def test_clean_cache_is_reused_and_measured(tmp_path: Path) -> None:
    first = workspace(tmp_path)
    with first.session():
        cache = first.prepare_cache()
        (cache / "kept.bin").write_bytes(b"1234")
        first.mark_clean()

    second = workspace(tmp_path)
    with second.session():
        assert (second.prepare_cache() / "kept.bin").read_bytes() == b"1234"
        assert second.cache_bytes() == 4


def test_active_cache_is_purged_before_reuse(tmp_path: Path) -> None:
    first = workspace(tmp_path)
    with first.session():
        cache = first.prepare_cache()
        (cache / "stale").write_text("discard")
        first.mark_active()

    second = workspace(tmp_path)
    with second.session():
        cache = second.prepare_cache()
        assert not (cache / "stale").exists()
        assert second.status() == "active"


def test_structurally_invalid_clean_cache_is_purged_under_lease(
    tmp_path: Path,
) -> None:
    current = workspace(tmp_path)
    with current.session():
        cache = current.prepare_cache()
        current.mark_clean()
        target = cache / "target"
        target.write_text("owned\n")
        make_symlink(cache / "unsafe", target)

    with workspace(tmp_path).session() as reopened:
        cache = reopened.prepare_cache()
        assert not (cache / "target").exists()
        assert not (cache / "unsafe").exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "socket"])
def test_invalid_cache_entries_fail_measurement_and_leave_active(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = workspace(tmp_path)
    bound_socket = None
    with current.session():
        cache = current.prepare_cache()
        current.mark_active()
        target = cache / "target"
        target.write_text("owned\n")
        invalid = cache / "invalid"
        if kind == "symlink":
            make_symlink(invalid, target)
        elif kind == "hardlink":
            os.link(target, invalid)
        elif kind == "fifo":
            os.mkfifo(invalid)
        else:
            bound_socket = socket.socket(socket.AF_UNIX)
            monkeypatch.chdir(cache)
            bound_socket.bind("invalid")

        with pytest.raises(ValueError, match="non-symlinks|hard links|regular files"):
            current.cache_bytes()
        with pytest.raises(ValueError, match="non-symlinks|hard links|regular files"):
            current.mark_clean()
        assert current.status() == "active"
    if bound_socket is not None:
        bound_socket.close()


def test_workspace_lease_wait_is_interruptible(tmp_path: Path) -> None:
    stopped = threading.Event()
    first = workspace(tmp_path)
    second = workspace(tmp_path, interrupted=stopped.is_set)
    interrupted = threading.Event()

    with first.session():
        def wait() -> None:
            try:
                with second.session():
                    pass
            except KeyboardInterrupt:
                interrupted.set()

        thread = threading.Thread(target=wait)
        thread.start()
        stopped.set()
        thread.join(5)

    assert not thread.is_alive()
    assert interrupted.is_set()


def test_scratch_inventory_rejects_foreign_worker_content(tmp_path: Path) -> None:
    slot = tmp_path / "eqasim-sa/campaign/workers/2"
    slot.mkdir(parents=True)
    (slot / "foreign").write_text("foreign")

    with pytest.raises(FileExistsError, match="foreign"):
        WorkerWorkspace.validate_scratch(tmp_path, {"campaign"})


def test_scratch_inventory_requires_owned_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = workspace(tmp_path)
    with current.session():
        current.prepare_cache()
        current.mark_clean()

    monkeypatch.setattr(os, "getuid", lambda: current.root.stat().st_uid + 1)
    with pytest.raises(FileExistsError, match="owned"):
        WorkerWorkspace.validate_scratch(tmp_path, {"campaign"})
