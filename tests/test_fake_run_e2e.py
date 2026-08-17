import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from eqasim_sa.adapters.backends.local import LocalBackend
from eqasim_sa.adapters.fake.simulator import FakeSimulator
from eqasim_sa.core import ledger
from eqasim_sa.core.run import RunSpec, run_one, validate_manifest
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
        run_module,
        "is_complete",
        lambda campaign_root, run_id: (_ for _ in ()).throw(
            OSError("ledger unavailable")
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
    destinations: list[Path] = []
    original = ledger.write_parquet_atomic

    def observe(table, destination):  # type: ignore[no-untyped-def]
        result = original(table, destination)
        destinations.append(destination)
        return result

    monkeypatch.setattr(ledger, "write_parquet_atomic", observe)
    import eqasim_sa.core.run as run_module

    monkeypatch.setattr(run_module, "write_parquet_atomic", observe)
    result = run_one(
        spec(),
        FakeSimulator(),
        LocalBackend(),
        tmp_path / "ordered",
        tmp_path / "work",
        "ctx",
    )
    assert result.status == "completed"
    assert "archives" in destinations[0].parts
    assert "runs" in destinations[-1].parts

    def fail_archive(table, destination):  # type: ignore[no-untyped-def]
        raise OSError("archive failure")

    monkeypatch.setattr(run_module, "write_parquet_atomic", fail_archive)
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
    real_archive, real_run = run_module.write_parquet_atomic, run_module.write_run
    calls = 0

    def fail_archive(table, destination):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(
                errno.ENOSPC if failure == "enospc" else errno.EIO,
                "injected publication",
            )
        return real_archive(table, destination)

    if failure in {"archive", "enospc"}:
        monkeypatch.setattr(run_module, "write_parquet_atomic", fail_archive)
    else:
        monkeypatch.setattr(
            run_module,
            "write_run",
            lambda *args: (_ for _ in ()).throw(OSError("injected final marker")),
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
    monkeypatch.setattr(run_module, "write_parquet_atomic", real_archive)
    monkeypatch.setattr(run_module, "write_run", real_run)
    replay = run_one(spec(), FakeSimulator(), LocalBackend(), campaign, work, "ctx")
    assert replay.status == "completed" and pq.read_table(stale).to_pylist()
    assert (campaign / "runs" / f"{replay.run_id}.parquet").is_file()
    assert not log.exists() and (campaign / "plan.parquet").read_bytes() == before
    assert not any(path.name.endswith(".tmp") for path in (campaign / "runs").iterdir())
