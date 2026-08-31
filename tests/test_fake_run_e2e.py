import os
import threading
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from eqasim_sa.adapters.backends.local import LocalBackend
from eqasim_sa.adapters.fake.simulator import FakeSimulator
from eqasim_sa.core import ledger
from eqasim_sa.core.run import RunSpec, execute_run, run_one, validate_manifest
from eqasim_sa.ports.simulator import Artifact, Manifest, SimulationError


def spec(*, fail: bool = False) -> RunSpec:
    return RunSpec(
        campaign="demo",
        resolved_config={
            "working_directory": "/ignored",
            "run": ["fake.population"],
            "config": {"sampling_rate": 0.1, "fail": fail},
        },
        seed=42,
        expected_context_hash="ctx",
        parameters={"sampling_rate": 0.1},
    )


class CountingSimulator(FakeSimulator):
    def __init__(self) -> None:
        self.calls = 0

    def simulate(self, config, seed, workdir):  # type: ignore[no-untyped-def]
        self.calls += 1
        return super().simulate(config, seed, workdir)


class CountingBackend(LocalBackend):
    def __init__(self) -> None:
        self.calls = 0

    def map(self, function, specs):  # type: ignore[no-untyped-def]
        self.calls += 1
        return super().map(function, specs)


def test_real_subprocess_archives_then_skips(tmp_path: Path) -> None:
    simulator = CountingSimulator()
    backend = CountingBackend()
    campaign = tmp_path / "campaign"
    first = run_one(spec(), simulator, backend, campaign, tmp_path / "work", "ctx")
    assert first.status == "completed"
    archive = campaign / "archives" / first.run_id / "population.parquet"
    run_file = campaign / "runs" / f"{first.run_id}.parquet"
    assert archive.is_file() and run_file.is_file()
    row = pq.read_table(archive).to_pylist()[0]
    assert row["producer_pid"] != os.getpid()
    assert row["seed"] == 42
    metadata = pq.ParquetFile(archive).metadata
    assert {
        metadata.row_group(group).column(column).compression
        for group in range(metadata.num_row_groups)
        for column in range(metadata.row_group(group).num_columns)
    } == {"ZSTD"}
    before = archive.read_bytes()
    second = run_one(spec(), simulator, backend, campaign, tmp_path / "work", "ctx")
    assert second == type(second)(first.run_id, "skipped")
    assert simulator.calls == 1
    assert backend.calls == 2
    assert archive.read_bytes() == before


def test_concurrent_execute_run_simulates_and_publishes_only_once(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    calls = 0

    class BlockingSimulator(FakeSimulator):
        def simulate(self, config, seed, workdir):  # type: ignore[no-untyped-def]
            nonlocal calls
            with lock:
                calls += 1
            entered.set()
            assert release.wait(5)
            return super().simulate(config, seed, workdir)

    campaign = tmp_path / "campaign"
    workdir = tmp_path / "work"
    results = []

    def run() -> None:
        results.append(
            execute_run(spec(), BlockingSimulator(), campaign, workdir)
        )

    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    assert entered.wait(5)
    second.start()
    time.sleep(0.1)
    assert calls == 1
    release.set()
    first.join(10)
    second.join(10)

    assert not first.is_alive() and not second.is_alive()
    assert calls == 1
    assert sorted(result.status for result in results) == ["completed", "skipped"]
    assert len(tuple((campaign / "runs").glob("*.parquet"))) == 1
    assert len(tuple((campaign / "archives").iterdir())) == 1


def test_zstd_artifact_uses_bounded_validation_and_exact_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class CapturingSimulator(FakeSimulator):
        source_bytes = b""

        def simulate(self, config, seed, workdir):  # type: ignore[no-untyped-def]
            manifest = super().simulate(config, seed, workdir)
            self.source_bytes = manifest.artifacts[0].path.read_bytes()
            return manifest

    def forbid_full_table_load(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("publication must not call pq.read_table")

    monkeypatch.setattr(ledger.pq, "read_table", forbid_full_table_load)
    simulator = CapturingSimulator()
    result = run_one(
        spec(),
        simulator,
        LocalBackend(),
        tmp_path / "campaign",
        tmp_path / "work",
        "ctx",
    )

    archive = (
        tmp_path
        / "campaign"
        / "archives"
        / result.run_id
        / "population.parquet"
    )
    assert result.status == "completed"
    assert archive.read_bytes() == simulator.source_bytes


def test_invalid_manifest_scratch_is_rejected_before_publication(
    tmp_path: Path,
) -> None:
    class InvalidScratch(FakeSimulator):
        def simulate(self, config, seed, workdir):  # type: ignore[no-untyped-def]
            manifest = super().simulate(config, seed, workdir)
            return Manifest(
                manifest.artifacts, manifest.attempt_directory, scratch_bytes=-1
            )

    campaign = tmp_path / "campaign"
    result = run_one(
        spec(), InvalidScratch(), LocalBackend(), campaign, tmp_path / "work", "ctx"
    )

    assert result.status == "failed"
    assert "scratch_bytes" in result.error
    assert not (campaign / "runs" / f"{result.run_id}.parquet").exists()
    assert not (campaign / "archives" / result.run_id).exists()


def test_failure_after_first_staged_artifact_leaves_no_partial_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class TwoArtifacts(FakeSimulator):
        def simulate(self, config, seed, workdir):  # type: ignore[no-untyped-def]
            manifest = super().simulate(config, seed, workdir)
            first = manifest.artifacts[0]
            return Manifest(
                (first, Artifact("second.parquet", first.path)),
                manifest.attempt_directory,
            )

    original = ledger._copy_parquet_artifact
    calls = 0

    def fail_second(source, destination, name, interrupted):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("second artifact failed")
        return original(source, destination, name, interrupted)

    monkeypatch.setattr(ledger, "_copy_parquet_artifact", fail_second)
    campaign = tmp_path / "campaign"
    result = run_one(
        spec(), TwoArtifacts(), LocalBackend(), campaign, tmp_path / "work", "ctx"
    )

    assert result.status == "failed"
    assert not (campaign / "archives" / result.run_id).exists()
    assert not tuple((campaign / "archives").glob(".*.tmp"))
    assert not (campaign / "runs" / f"{result.run_id}.parquet").exists()


def test_orphan_archive_symlink_is_replaced_without_following_it(
    tmp_path: Path,
) -> None:
    from eqasim_sa.core.run import compute_run_id

    campaign = tmp_path / "campaign"
    run_id = compute_run_id(spec().resolved_config, spec().seed)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep"
    sentinel.write_text("untouched")
    archive = campaign / "archives" / run_id
    archive.parent.mkdir(parents=True)
    archive.symlink_to(outside, target_is_directory=True)

    result = run_one(
        spec(), FakeSimulator(), LocalBackend(), campaign, tmp_path / "work", "ctx"
    )

    assert result.status == "completed"
    assert archive.is_dir() and not archive.is_symlink()
    assert sentinel.read_text() == "untouched"


def test_context_mismatch_and_process_failure_publish_no_run(tmp_path: Path) -> None:
    simulator = CountingSimulator()
    mismatch = run_one(
        spec(),
        simulator,
        LocalBackend(),
        tmp_path / "mismatch",
        tmp_path / "work",
        "other",
    )
    assert mismatch.status == "failed"
    assert "expected 'ctx', current 'other'" in mismatch.error
    assert simulator.calls == 0
    failed = run_one(
        spec(fail=True),
        simulator,
        LocalBackend(),
        tmp_path / "failed",
        tmp_path / "work",
        "ctx",
    )
    assert failed.status == "failed"
    assert not (tmp_path / "failed" / "runs" / f"{failed.run_id}.parquet").exists()


def test_invalid_parameters_fail_before_backend_or_simulator(tmp_path: Path) -> None:
    simulator = CountingSimulator()
    backend = CountingBackend()
    invalid = RunSpec("demo", spec().resolved_config, 42, "ctx", {"seed": 9})
    result = run_one(
        invalid, simulator, backend, tmp_path / "campaign", tmp_path / "work", "ctx"
    )
    assert result.status == "failed"
    assert "reserved" in result.error
    assert simulator.calls == 0
    assert backend.calls == 0
    assert not (tmp_path / "campaign" / "archives").exists()
    oversized = RunSpec("demo", spec().resolved_config, 42, "ctx", {"huge": 2**80})
    result = run_one(
        oversized, simulator, backend, tmp_path / "campaign", tmp_path / "work", "ctx"
    )
    assert result.status == "failed"
    assert "int64" in result.error
    assert simulator.calls == 0
    null = RunSpec("demo", spec().resolved_config, 42, "ctx", {"null": None})
    result = run_one(
        null, simulator, backend, tmp_path / "campaign", tmp_path / "work", "ctx"
    )
    assert result.status == "failed"
    assert "scalar" in result.error


@pytest.mark.parametrize(
    "artifact",
    [
        Artifact("../escape.parquet", Path("missing")),
        Artifact("bad.txt", Path("missing")),
    ],
)
def test_rejects_malicious_manifests(tmp_path: Path, artifact: Artifact) -> None:
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    with pytest.raises((ValueError, FileNotFoundError)):
        validate_manifest(Manifest((artifact,), attempt), tmp_path)


def test_rejects_duplicate_and_symlink_escape(tmp_path: Path) -> None:
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    outside = tmp_path / "outside.parquet"
    outside.write_bytes(b"outside")
    link = attempt / "population.parquet"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        validate_manifest(
            Manifest((Artifact("population.parquet", link),), attempt), tmp_path
        )
    inside = attempt / "inside.parquet"
    inside.write_bytes(b"inside")
    duplicate = Artifact("inside.parquet", inside)
    with pytest.raises(ValueError, match="duplicate"):
        validate_manifest(Manifest((duplicate, duplicate), attempt), tmp_path)


def test_rejects_artifact_with_symlinked_parent(tmp_path: Path) -> None:
    attempt = tmp_path / "attempt"
    real = attempt / "real"
    real.mkdir(parents=True)
    source = real / "population.parquet"
    pq.write_table(pa.table({"value": [1]}), source, compression="zstd")
    linked = attempt / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes"):
        validate_manifest(
            Manifest((Artifact("population.parquet", linked / source.name),), attempt),
            tmp_path,
        )


def test_rejects_attempt_directory_outside_workdir(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    source = outside / "population.parquet"
    source.write_bytes(b"not inspected")
    with pytest.raises(ValueError, match="workdir"):
        validate_manifest(
            Manifest((Artifact("population.parquet", source),), outside), workdir
        )
    source = workdir / "population.parquet"
    source.write_bytes(b"not inspected")
    with pytest.raises(ValueError, match="workdir"):
        validate_manifest(
            Manifest((Artifact("population.parquet", source),), workdir), workdir
        )


def test_rejects_lexical_parent_segment_inside_attempt(tmp_path: Path) -> None:
    attempt = tmp_path / "attempt"
    child = attempt / "child"
    child.mkdir(parents=True)
    source = attempt / "population.parquet"
    pq.write_table(pa.table({"x": [1]}), source)
    with pytest.raises(ValueError, match="parent segment"):
        validate_manifest(
            Manifest(
                (Artifact("population.parquet", child / ".." / "population.parquet"),),
                attempt,
            ),
            tmp_path,
        )


def test_backend_failure_is_structured(tmp_path: Path) -> None:
    class FailingBackend:
        def map(self, function, specs):  # type: ignore[no-untyped-def]
            raise RuntimeError("backend unavailable")

    result = run_one(
        spec(),
        FakeSimulator(),
        FailingBackend(),
        tmp_path / "campaign",
        tmp_path / "work",
        "ctx",
    )
    assert result.status == "failed"
    assert result.run_id
    assert result.error == "backend unavailable"


def test_simulation_error_publishes_log_and_success_removes_it(tmp_path: Path) -> None:
    class LoggedFailure:
        def simulate(self, config, seed, workdir):  # type: ignore[no-untyped-def]
            attempt = workdir / "attempt-logged"
            attempt.mkdir(parents=True)
            log = attempt / "run.log"
            log.write_text("process output\n", encoding="utf-8")
            raise SimulationError("process failed", log, attempt)

    campaign = tmp_path / "campaign"
    failed = run_one(
        spec(), LoggedFailure(), LocalBackend(), campaign, tmp_path / "work", "ctx"
    )
    durable = campaign / "logs" / f"{failed.run_id}.log"
    assert failed.status == "failed"
    assert durable.read_text(encoding="utf-8") == "process output\n"
    completed = run_one(
        spec(), FakeSimulator(), LocalBackend(), campaign, tmp_path / "work", "ctx"
    )
    assert completed.status == "completed"
    assert not durable.exists()


def test_skip_removes_stale_log_and_plain_failure_creates_none(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    completed = run_one(
        spec(), FakeSimulator(), LocalBackend(), campaign, tmp_path / "work", "ctx"
    )
    stale = campaign / "logs" / f"{completed.run_id}.log"
    stale.parent.mkdir()
    stale.write_text("stale", encoding="utf-8")
    skipped = run_one(
        spec(), FakeSimulator(), LocalBackend(), campaign, tmp_path / "work", "ctx"
    )
    assert skipped.status == "skipped"
    assert not stale.exists()

    plain = run_one(
        spec(fail=True),
        FakeSimulator(),
        LocalBackend(),
        tmp_path / "plain",
        tmp_path / "work",
        "ctx",
    )
    assert plain.status == "failed"
    assert not (tmp_path / "plain" / "logs").exists()


def test_rejects_simulation_log_outside_attempt(tmp_path: Path) -> None:
    class EscapingFailure:
        def simulate(self, config, seed, workdir):  # type: ignore[no-untyped-def]
            attempt = workdir / "attempt-escape"
            attempt.mkdir(parents=True)
            outside = workdir / "outside.log"
            outside.write_text("outside", encoding="utf-8")
            raise SimulationError("bad log", outside, attempt)

    result = run_one(
        spec(),
        EscapingFailure(),
        LocalBackend(),
        tmp_path / "campaign",
        tmp_path / "work",
        "ctx",
    )
    assert result.status == "failed"
    assert "escapes attempt" in result.error
    assert not (tmp_path / "campaign" / "logs").exists()


def test_rejects_simulation_attempt_outside_workdir(tmp_path: Path) -> None:
    class EscapingAttempt:
        def simulate(self, config, seed, workdir):  # type: ignore[no-untyped-def]
            attempt = tmp_path / "outside-attempt"
            attempt.mkdir()
            log = attempt / "run.log"
            log.write_text("outside", encoding="utf-8")
            raise SimulationError("bad attempt", log, attempt)

    campaign = tmp_path / "campaign"
    result = run_one(
        spec(),
        EscapingAttempt(),
        LocalBackend(),
        campaign,
        tmp_path / "work",
        "ctx",
    )
    assert result.status == "failed"
    assert "attempt escapes workdir" in result.error
    assert not (campaign / "logs").exists()


def test_log_publication_failure_preserves_original_error_and_no_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LoggedFailure:
        def simulate(self, config, seed, workdir):  # type: ignore[no-untyped-def]
            attempt = workdir / "attempt-failed-copy"
            attempt.mkdir(parents=True)
            log = attempt / "run.log"
            log.write_text("diagnostic", encoding="utf-8")
            raise SimulationError("simulation failed", log, attempt)

    import eqasim_sa.core.run as run_module

    monkeypatch.setattr(
        run_module,
        "copy_file_atomic",
        lambda source, destination: (_ for _ in ()).throw(OSError("copy failed")),
    )
    campaign = tmp_path / "campaign"
    result = run_one(
        spec(), LoggedFailure(), LocalBackend(), campaign, tmp_path / "work", "ctx"
    )
    assert result.status == "failed"
    assert result.error == "simulation failed; log publication failed: copy failed"
    assert not (campaign / "runs" / f"{result.run_id}.parquet").exists()
    assert (
        tmp_path / "work" / "attempt-failed-copy" / "run.log"
    ).read_text() == "diagnostic"


def test_stale_log_fsync_failure_blocks_success_and_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import eqasim_sa.core.run as run_module

    for name, prepare_complete in (("success", False), ("skip", True)):
        campaign = tmp_path / name
        if prepare_complete:
            completed = run_one(
                spec(),
                FakeSimulator(),
                LocalBackend(),
                campaign,
                tmp_path / "work",
                "ctx",
            )
            run_id = completed.run_id
        else:
            from eqasim_sa.core.run import compute_run_id

            run_id = compute_run_id(spec().resolved_config, spec().seed)
        stale = campaign / "logs" / f"{run_id}.log"
        stale.parent.mkdir(parents=True)
        stale.write_text("stale", encoding="utf-8")
        real_fsync = run_module.os.fsync

        def fail_fsync(descriptor: int) -> None:
            raise OSError("log directory fsync failed")

        monkeypatch.setattr(run_module.os, "fsync", fail_fsync)
        result = run_one(
            spec(), FakeSimulator(), LocalBackend(), campaign, tmp_path / "work", "ctx"
        )
        assert result.status == "failed"
        assert "fsync failed" in result.error
        monkeypatch.setattr(run_module.os, "fsync", real_fsync)


def test_execute_run_converts_completion_check_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import eqasim_sa.core.run as run_module

    monkeypatch.setattr(
        run_module.CampaignSnapshot,
        "run_completed",
        classmethod(
            lambda cls, campaign_root, run_id: (_ for _ in ()).throw(
                OSError("ledger unavailable")
            )
        ),
    )
    result = run_module.execute_run(
        spec(), FakeSimulator(), tmp_path / "campaign", tmp_path / "work"
    )
    assert result.status == "failed" and result.run_id
    assert result.error == "ledger unavailable"


def test_run_file_is_published_last_and_archive_failure_is_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import eqasim_sa.core.run as run_module

    events: list[str] = []
    original_rename = ledger.os.rename
    original_publish = ledger.write_parquet_exclusive
    campaign = tmp_path / "ordered"

    def observe_rename(source, target, *args, **kwargs):  # type: ignore[no-untyped-def]
        result = original_rename(source, target, *args, **kwargs)
        if kwargs.get("src_dir_fd") is not None and str(source).endswith(".tmp"):
            events.append("archive")
        return result

    def observe_marker(table, destination, **options):  # type: ignore[no-untyped-def]
        assert (campaign / "archives" / destination.stem).is_dir()
        events.append("marker")
        return original_publish(table, destination, **options)

    monkeypatch.setattr(ledger.os, "rename", observe_rename)
    monkeypatch.setattr(ledger, "write_parquet_exclusive", observe_marker)
    result = run_one(
        spec(),
        FakeSimulator(),
        LocalBackend(),
        campaign,
        tmp_path / "work",
        "ctx",
    )
    assert result.status == "completed"
    assert events == ["archive", "marker"]

    def fail_archive(source, destination, name, interrupted):  # type: ignore[no-untyped-def]
        raise OSError("archive failure")

    monkeypatch.setattr(ledger, "_copy_parquet_artifact", fail_archive)
    failed = run_one(
        RunSpec("other", spec().resolved_config, 43, "ctx", {}),
        FakeSimulator(),
        LocalBackend(),
        tmp_path / "broken",
        tmp_path / "work",
        "ctx",
    )
    assert failed.status == "failed"
    assert not (tmp_path / "broken" / "runs" / f"{failed.run_id}.parquet").exists()
    assert not (tmp_path / "broken" / "archives" / failed.run_id).exists()
    assert not tuple((tmp_path / "broken" / "archives").glob(".*.tmp"))


@pytest.mark.parametrize("failure", ["archive", "run", "enospc"])
def test_partial_publication_replays_without_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    import errno

    import eqasim_sa.core.run as run_module

    campaign, work = tmp_path / "campaign", tmp_path / "work"
    (campaign / "plan.parquet").parent.mkdir(parents=True)
    (campaign / "plan.parquet").write_bytes(b"sealed-plan")
    before = (campaign / "plan.parquet").read_bytes()
    real_archive = ledger._copy_parquet_artifact
    real_marker = ledger.write_parquet_exclusive
    calls = 0

    def fail_archive(source, destination, name, interrupted):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(
                errno.ENOSPC if failure == "enospc" else errno.EIO,
                "injected publication",
            )
        return real_archive(source, destination, name, interrupted)

    if failure in {"archive", "enospc"}:
        monkeypatch.setattr(ledger, "_copy_parquet_artifact", fail_archive)
    else:
        monkeypatch.setattr(
            ledger,
            "write_parquet_exclusive",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                OSError("injected final marker")
            ),
        )
    first = run_one(spec(), FakeSimulator(), LocalBackend(), campaign, work, "ctx")
    assert (
        first.status == "failed"
        and not (campaign / "runs" / f"{first.run_id}.parquet").exists()
    )
    if failure == "run":
        assert (
            "injected final marker"
            in (campaign / "logs" / f"{first.run_id}.log").read_text()
        )
    archive = campaign / "archives" / first.run_id
    stale = archive / "population.parquet"
    if stale.exists():
        stale.write_bytes(b"stale")
    log = campaign / "logs" / f"{first.run_id}.log"
    log.parent.mkdir(exist_ok=True)
    log.write_text("old failure")
    monkeypatch.setattr(ledger, "_copy_parquet_artifact", real_archive)
    monkeypatch.setattr(ledger, "write_parquet_exclusive", real_marker)
    replay = run_one(spec(), FakeSimulator(), LocalBackend(), campaign, work, "ctx")
    assert replay.status == "completed" and pq.read_table(stale).to_pylist()
    assert (campaign / "runs" / f"{replay.run_id}.parquet").is_file()
    assert not log.exists() and (campaign / "plan.parquet").read_bytes() == before
    assert not any(path.name.endswith(".tmp") for path in (campaign / "runs").iterdir())
