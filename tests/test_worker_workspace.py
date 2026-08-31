import threading
from pathlib import Path

import pytest

from eqasim_sa.adapters.eqasim.workspace import WorkerWorkspace


def workspace(
    scratch: Path,
    *,
    slot: int = 0,
    context_hash: str | None = "a" * 64,
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
