"""Portable, path-based ledger and analysis-publication behaviour."""

import os
import stat
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from eqasim_sa.adapters import data_manifest, project_init
from eqasim_sa.adapters.backends.local import LocalBackend
from eqasim_sa.adapters.fake.simulator import FakeSimulator
from eqasim_sa.core import (
    analysis_publication,
    analyze,
    campaign_snapshot,
    ledger,
    plan,
    run,
)
from eqasim_sa.core.ledger import RunRecord
from eqasim_sa.core.run import RunSpec, run_one
from eqasim_sa.utils import filesystem, platform
from tests.conftest import make_symlink

RUN_ID = "a" * 32


def test_ledger_sources_have_no_posix_only_calls() -> None:
    for module in (
        ledger,
        analysis_publication,
        campaign_snapshot,
        plan,
        project_init,
        run,
        data_manifest,
        analyze,
    ):
        src = Path(module.__file__).read_text(encoding="utf-8")
        for token in (
            "dir_fd",
            "O_DIRECTORY",
            "os.O_NOFOLLOW",
            ".is_symlink()",
            "os.getuid",
            "os.uname",
            "import fcntl",
            "posix_dirfd",
        ):
            assert token not in src, (module.__name__, token)


def _record() -> RunRecord:
    return RunRecord(RUN_ID, "demo", 1, "c" * 64, "node", 1.0, {})


def _source(directory: Path, value: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / f"source-{value}.parquet"
    pq.write_table(pa.table({"value": [value]}), source, compression="zstd")
    return source


def _publish(campaign: Path, value: int, **options: object) -> Path:
    source = _source(campaign.parent / "sources", value)
    with ledger.archive_transaction(
        campaign, RUN_ID, (("output.parquet", source),)
    ) as publication:
        return publication.commit(_record(), **options)


def _archive_entries(campaign: Path) -> list[str]:
    return sorted(entry.name for entry in (campaign / "archives").iterdir())


def _archived_value(campaign: Path) -> list[dict[str, int]]:
    return pq.read_table(
        campaign / "archives" / RUN_ID / "output.parquet"
    ).to_pylist()


def test_republish_existing_archive_replaces_it(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    marker = _publish(campaign, 1)
    marker.unlink()

    _publish(campaign, 2)

    assert _archive_entries(campaign) == [RUN_ID]
    assert _archived_value(campaign) == [{"value": 2}]
    state = ledger.completion_state(campaign, _record())
    assert state is not None and state.archive_integrity == ledger.VERIFIED


def test_commit_rolls_back_when_marker_write_fails(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    marker = _publish(campaign, 1)
    marker.unlink()

    def failing_writer(*arguments: object) -> Path:
        raise OSError("marker write failed")

    with pytest.raises(OSError, match="marker write failed"):
        _publish(campaign, 2, marker_writer=failing_writer)

    assert _archive_entries(campaign) == [RUN_ID]
    assert _archived_value(campaign) == [{"value": 1}]
    assert not marker.exists()


def test_commit_refuses_campaign_replaced_after_prepare(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    source = _source(tmp_path / "sources", 1)
    with ledger.archive_transaction(
        campaign, RUN_ID, (("output.parquet", source),)
    ) as publication:
        campaign.rename(tmp_path / "detached")
        campaign.mkdir()
        with pytest.raises(RuntimeError, match="directories changed"):
            publication.commit(_record())

    assert not tuple(campaign.iterdir())
    assert not (tmp_path / "detached" / "runs" / f"{RUN_ID}.parquet").exists()


@pytest.mark.parametrize("name", ["archives", "runs", ".eqasim-sa"])
def test_transaction_refuses_linked_managed_directory(
    tmp_path: Path, name: str
) -> None:
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    make_symlink(campaign / name, outside, target_is_directory=True)
    source = _source(tmp_path / "sources", 1)

    with pytest.raises(NotADirectoryError):
        with ledger.archive_transaction(campaign, RUN_ID, (("output.parquet", source),)):
            pass

    assert not tuple(outside.iterdir())


@pytest.mark.parametrize("name", ["archives", "runs"])
def test_reader_refuses_linked_managed_directory(tmp_path: Path, name: str) -> None:
    campaign = tmp_path / "campaign"
    _publish(campaign, 1)
    detached = tmp_path / f"detached-{name}"
    (campaign / name).rename(detached)
    make_symlink(campaign / name, detached, target_is_directory=True)

    with pytest.raises(ValueError, match="completion marker"):
        ledger.completion_state(campaign, _record())
    if name == "runs":
        with pytest.raises(ValueError, match="completion marker directory"):
            ledger.completion_marker_names(campaign)


def test_reader_accepts_links_among_campaign_ancestors(tmp_path: Path) -> None:
    real = tmp_path / "real"
    _publish(real / "campaign", 1)
    make_symlink(tmp_path / "alias", real, target_is_directory=True)

    state = ledger.completion_state(tmp_path / "alias" / "campaign", _record())

    assert state is not None and state.archive_integrity == ledger.VERIFIED


def test_transaction_accepts_links_among_campaign_ancestors(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    make_symlink(tmp_path / "alias", real, target_is_directory=True)
    campaign = tmp_path / "alias" / "campaign"

    marker = _publish(campaign, 1)

    assert marker == campaign / "runs" / f"{RUN_ID}.parquet"
    assert (real / "campaign" / "archives" / RUN_ID / "output.parquet").is_file()
    state = ledger.completion_state(campaign, _record())
    assert state is not None and state.archive_integrity == ledger.VERIFIED


def test_close_removes_staging_that_could_not_be_anchored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = tmp_path / "campaign"
    source = _source(tmp_path / "sources", 1)
    real_child = filesystem.DirectoryAnchor.child

    def refuse_staging(self, name, *, create=False):  # type: ignore[no-untyped-def]
        if name.startswith(".s"):
            raise RuntimeError("staging anchor failed")
        return real_child(self, name, create=create)

    monkeypatch.setattr(filesystem.DirectoryAnchor, "child", refuse_staging)
    with pytest.raises(RuntimeError, match="staging anchor failed"):
        with ledger.archive_transaction(
            campaign, RUN_ID, (("output.parquet", source),)
        ):
            pass

    assert _archive_entries(campaign) == []


def test_failure_log_uses_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = "durée dépassée"

    def fail_marker(*arguments: object, **options: object) -> Path:
        raise RuntimeError(message)

    monkeypatch.setattr(ledger, "write_parquet_exclusive", fail_marker)
    spec = RunSpec(
        campaign="demo",
        resolved_config={
            "working_directory": "/ignored",
            "run": ["fake.population"],
            "config": {"sampling_rate": 0.1, "fail": False},
        },
        seed=42,
        expected_context_hash="ctx",
        parameters={"sampling_rate": 0.1},
    )
    campaign = tmp_path / "campaign"

    result = run_one(
        spec, FakeSimulator(), LocalBackend(), campaign, tmp_path / "work", "ctx"
    )

    assert result.status == "failed"
    log = campaign / "logs" / f"{result.run_id}.log"
    assert log.read_bytes() == (message + "\n").encode("utf-8")


def _read_only_unlink(monkeypatch: pytest.MonkeyPatch) -> None:
    """Emulate Windows, where unlinking a read-only file is refused."""

    real_unlink = os.unlink

    def unlink(path, *, dir_fd=None):  # type: ignore[no-untyped-def]
        info = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        if not info.st_mode & stat.S_IWRITE:
            raise PermissionError(13, "read-only file", str(path))
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(os, "remove", unlink)


def _read_only_file(path: Path) -> Path:
    path.write_bytes(b"captured")
    os.chmod(path, 0o400)
    return path


def test_remove_file_clears_read_only_attribute_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _read_only_file(tmp_path / "captured.parquet")
    _read_only_unlink(monkeypatch)
    monkeypatch.setattr(platform, "IS_WINDOWS", True)

    platform.remove_file(target)

    assert not target.exists()


def test_remove_file_does_not_change_modes_off_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _read_only_file(tmp_path / "captured.parquet")
    _read_only_unlink(monkeypatch)
    monkeypatch.setattr(platform, "IS_WINDOWS", False)

    with pytest.raises(PermissionError):
        platform.remove_file(target)

    assert stat.S_IMODE(target.stat().st_mode) == 0o400
    os.chmod(target, 0o600)


def test_remove_tree_clears_read_only_files_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / "work"
    (tree / "capture").mkdir(parents=True)
    _read_only_file(tree / "capture" / "plan.parquet")
    _read_only_unlink(monkeypatch)
    monkeypatch.setattr(platform, "IS_WINDOWS", True)

    platform.remove_tree(tree)

    assert not tree.exists()


def test_remove_tree_does_not_retry_listing_errors_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / "work"
    tree.mkdir()
    calls = []

    def refuse(path, *arguments, **options):  # type: ignore[no-untyped-def]
        calls.append(path)
        raise PermissionError(13, "listing refused", str(path))

    monkeypatch.setattr(platform, "IS_WINDOWS", True)
    monkeypatch.setattr(platform.shutil, "_use_fd_functions", False, raising=False)
    monkeypatch.setattr(os, "scandir", refuse)
    monkeypatch.setattr(platform.time, "sleep", lambda _delay: None)

    with pytest.raises(PermissionError, match="listing refused"):
        platform.remove_tree(tree)

    assert len(calls) == 1
    assert stat.S_IMODE(tree.stat().st_mode) != stat.S_IWRITE
