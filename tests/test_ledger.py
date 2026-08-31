import hashlib
import json
import math
import os
import threading
from dataclasses import replace
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from eqasim_sa.core import ledger
from eqasim_sa.core import durable_publication
from eqasim_sa.core.campaign_snapshot import CampaignSnapshot
from eqasim_sa.core.ledger import RunRecord, run_path, write_run
from eqasim_sa.core.run import RunSpec, compute_run_id, pending_run_specs
from eqasim_sa.ports.simulator import EXECUTION_EVIDENCE_DOMAIN, ExecutionEvidence


def is_complete(campaign_root: Path, current: RunSpec) -> bool:
    return CampaignSnapshot.run_completed(campaign_root, current)


def codecs(path: Path) -> set[str]:
    metadata = pq.ParquetFile(path).metadata
    return {
        metadata.row_group(row).column(column).compression
        for row in range(metadata.num_row_groups)
        for column in range(metadata.row_group(row).num_columns)
    }


def test_writes_one_row_zstd_ledger_at_exact_path(tmp_path: Path) -> None:
    spec = run_spec(42, rate=0.1)
    run_id = compute_run_id(spec.resolved_config, spec.seed)
    record = RunRecord(run_id, "demo", 42, "c" * 64, "node", 1.25, {"rate": 0.1})
    destination = write_run(tmp_path, record)
    assert destination == tmp_path / "runs" / f"{run_id}.parquet"
    assert is_complete(tmp_path, spec)
    assert run_path(tmp_path, record.run_id) == destination
    table = pq.read_table(destination)
    assert table.column_names == [
        "runId",
        "campaign",
        "seed",
        "status",
        "attempts",
        "contextHash",
        "node",
        "duration",
        "scratchBytes",
        "rate",
    ]
    assert table.to_pylist() == [
        {
            "runId": run_id,
            "campaign": "demo",
            "seed": 42,
            "status": "completed",
            "attempts": 1,
            "contextHash": "c" * 64,
            "node": "node",
            "duration": 1.25,
            "scratchBytes": 0,
            "rate": 0.1,
        }
    ]
    assert codecs(destination) == {"ZSTD"}


def test_rejects_reserved_parameter_and_invalid_scalar(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="reserved"):
        write_run(tmp_path, RunRecord("a" * 32, "c", 1, "x", "n", 0.0, {"seed": 3}))
    with pytest.raises(TypeError):
        write_run(tmp_path, RunRecord("a" * 32, "c", 1, "x", "n", 0.0, {"x": [1]}))
    with pytest.raises(TypeError):
        write_run(tmp_path, RunRecord("a" * 32, "c", 1, "x", "n", 0.0, {"x": None}))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"run_id": "../escape"}, "run_id"),
        ({"campaign": ""}, "campaign"),
        ({"seed": True}, "seed"),
        ({"context_hash": ""}, "context_hash"),
        ({"node": None}, "node"),
        ({"duration": 0}, "duration"),
        ({"duration": -1.0}, "duration"),
        ({"duration": math.nan}, "duration"),
        ({"duration": math.inf}, "duration"),
        ({"scratch_bytes": True}, "scratch_bytes"),
        ({"scratch_bytes": -1}, "scratch_bytes"),
        ({"scratch_bytes": 2**63}, "scratch_bytes"),
    ],
)
def test_rejects_invalid_duration_and_scratch_before_write(
    tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    record = RunRecord("a" * 32, "demo", 1, "ctx", "node", 1.0, {})
    with pytest.raises(ValueError, match=message):
        write_run(tmp_path, replace(record, **changes))
    assert not (tmp_path / "runs").exists()


def test_rejects_invalid_run_id_before_constructing_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="run_id"):
        run_path(tmp_path, "../../escaped")
    assert not (tmp_path.parent / "escaped.parquet").exists()


def test_atomic_failure_cleans_temporary_and_preserves_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "value.parquet"
    pq.write_table(pa.table({"value": [1]}), destination, compression="zstd")
    original = destination.read_bytes()

    def fail_replace(*args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        raise OSError("injected")

    monkeypatch.setattr(ledger.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        ledger.write_parquet_atomic(pa.table({"value": [2]}), destination)
    assert destination.read_bytes() == original
    assert list(tmp_path.glob(".*.tmp")) == []


def test_directory_fsync_failure_rolls_back_new_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "value.parquet"
    real_fsync = ledger.os.fsync
    calls = 0

    def fail_second_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(ledger.os, "fsync", fail_second_fsync)
    with pytest.raises(OSError, match="directory fsync failure"):
        ledger.write_parquet_atomic(pa.table({"value": [2]}), destination)
    assert not destination.exists()


def test_directory_fsync_failure_restores_existing_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "value.parquet"
    pq.write_table(pa.table({"value": [1]}), destination, compression="zstd")
    real_fsync = ledger.os.fsync
    calls = 0

    def fail_second_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(ledger.os, "fsync", fail_second_fsync)
    with pytest.raises(OSError, match="directory fsync failure"):
        ledger.write_parquet_atomic(pa.table({"value": [2]}), destination)
    assert pq.read_table(destination).to_pylist() == [{"value": 1}]


def test_failed_rollback_preserves_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "value.parquet"
    pq.write_table(pa.table({"value": [1]}), destination, compression="zstd")
    real_fsync = ledger.os.fsync
    real_replace = ledger.os.replace
    fsync_calls = 0
    replace_calls = 0

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("directory fsync failure")
        real_fsync(descriptor)

    def fail_rollback(source, target, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("rollback failure")
        real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(ledger.os, "fsync", fail_directory_fsync)
    monkeypatch.setattr(ledger.os, "replace", fail_rollback)
    with pytest.raises(
        durable_publication.PublicationOutcomeUnknown, match="rollback"
    ):
        ledger.write_parquet_atomic(pa.table({"value": [2]}), destination)
    backups = list(tmp_path.glob(".*.backup"))
    assert len(backups) == 1
    assert pq.read_table(backups[0]).to_pylist() == [{"value": 1}]


def test_run_marker_temporary_is_staged_outside_runs(tmp_path: Path) -> None:
    destination = tmp_path / "campaign" / "runs" / ("a" * 32 + ".parquet")

    def fail_writer(path: Path) -> None:
        assert path.parent == tmp_path / "campaign" / ".eqasim-sa" / "transactions"
        path.write_bytes(b"partial")
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        durable_publication.write_atomic(destination, fail_writer)
    assert not tuple((tmp_path / "campaign" / "runs").iterdir())


def run_spec(seed: int = 42, **parameters: object) -> RunSpec:
    config = {"config": {"random_seed": seed, **parameters}}
    return RunSpec("demo", config, seed, "c" * 64, parameters)


def complete(root: Path, spec: RunSpec) -> Path:
    run_id = compute_run_id(spec.resolved_config, spec.seed)
    return write_run(
        root,
        RunRecord(
            run_id,
            spec.campaign,
            spec.seed,
            spec.expected_context_hash,
            "node",
            1.0,
            spec.parameters,
        ),
    )


def execution_evidence(context_hash: str = "c" * 64) -> ExecutionEvidence:
    return ExecutionEvidence(
        context_hash,
        "1" * 64,
        "2" * 64,
        ("PATH", "TOKEN"),
        "3" * 64,
        ("/usr/bin/python3", "-m", "synpp"),
        "4" * 40,
        "5" * 64,
        schema_version=2,
        eqasim_sa_sha256="6" * 64,
    )


def complete_v2(
    root: Path, spec: RunSpec, evidence: ExecutionEvidence | None = None
) -> tuple[Path, Path, bytes]:
    root.mkdir(parents=True, exist_ok=True)
    source = root / "source.parquet"
    pq.write_table(pa.table({"value": [1, 2]}), source, compression="zstd")
    source_bytes = source.read_bytes()
    run_id = compute_run_id(spec.resolved_config, spec.seed)
    record = RunRecord(
        run_id,
        spec.campaign,
        spec.seed,
        spec.expected_context_hash,
        "node",
        1.0,
        spec.parameters,
    )
    with ledger.archive_transaction(
        root, run_id, (("output.parquet", source),)
    ) as publication:
        marker = publication.commit(record, execution_evidence=evidence)
    return marker, root / "archives" / run_id / "output.parquet", source_bytes


def test_archive_transaction_never_replaces_a_competing_marker(
    tmp_path: Path,
) -> None:
    spec = run_spec(rate=1)
    tmp_path.mkdir(exist_ok=True)
    source = tmp_path / "source.parquet"
    pq.write_table(pa.table({"value": [1]}), source, compression="zstd")
    record = replace(expected_record(spec), node="node", duration=1.0)

    with ledger.archive_transaction(
        tmp_path, record.run_id, (("output.parquet", source),)
    ) as publication:
        marker = write_run(tmp_path, record)
        before = marker.read_bytes()
        with pytest.raises(FileExistsError, match="completion marker"):
            publication.commit(record)

    assert marker.read_bytes() == before
    assert not (tmp_path / "archives" / record.run_id).exists()


def test_archive_marker_commit_uses_only_descriptors_held_during_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = run_spec(rate=1)
    source = tmp_path / "source.parquet"
    pq.write_table(pa.table({"value": [1]}), source, compression="zstd")
    record = replace(expected_record(spec), node="node", duration=1.0)

    with ledger.archive_transaction(
        tmp_path, record.run_id, (("output.parquet", source),)
    ) as publication:
        monkeypatch.setattr(
            ledger,
            "open_directory",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("campaign reopened by pathname")
            ),
        )
        marker = publication.commit(record)

    assert marker.is_file()


def test_archive_transaction_marker_race_is_no_clobber_and_self_consistent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = run_spec(rate=1)
    tmp_path.mkdir(exist_ok=True)
    sources = []
    for value in (1, 2):
        source = tmp_path / f"source-{value}.parquet"
        pq.write_table(pa.table({"value": [value]}), source, compression="zstd")
        sources.append(source)
    record = replace(expected_record(spec), node="node", duration=1.0)
    barrier = threading.Barrier(2)
    real_publish = ledger._publish_run_marker_no_clobber

    def race(staging, runs, name, table):  # type: ignore[no-untyped-def]
        barrier.wait(timeout=5)
        return real_publish(staging, runs, name, table)

    monkeypatch.setattr(ledger, "_publish_run_marker_no_clobber", race)
    committed = []
    errors = []

    def publish(value: int) -> None:
        try:
            with ledger.archive_transaction(
                tmp_path,
                record.run_id,
                (("output.parquet", sources[value - 1]),),
            ) as publication:
                publication.commit(record)
            committed.append(value)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=publish, args=(value,)) for value in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert all(not thread.is_alive() for thread in threads)
    assert len(committed) == 1, [repr(error) for error in errors]
    assert len(errors) == 1 and isinstance(errors[0], FileExistsError)
    completion = ledger.completion_state(tmp_path, expected_record(spec))
    assert completion is not None and completion.archive_integrity == ledger.VERIFIED
    archive = tmp_path / "archives" / record.run_id / "output.parquet"
    assert pq.read_table(archive).to_pylist() == [{"value": committed[0]}]
    assert not tuple((tmp_path / "archives").glob(".*.backup"))


def test_run_lease_is_private_to_run_and_rejects_foreign_lock(
    tmp_path: Path,
) -> None:
    root = tmp_path / "campaign"
    root.mkdir()
    run_id = "a" * 32
    blocked = threading.Event()
    interrupt = threading.Event()

    with ledger.run_lease(root, run_id):
        def contend() -> None:
            try:
                with ledger.run_lease(root, run_id, interrupt.is_set):
                    pass
            except KeyboardInterrupt:
                blocked.set()

        thread = threading.Thread(target=contend)
        thread.start()
        interrupt.set()
        thread.join(5)
        assert not thread.is_alive() and blocked.is_set()

    lock = root / ".eqasim-sa" / "locks" / "runs" / f"{run_id}.lock"
    lock.unlink()
    foreign = root.parent / "foreign"
    foreign.write_text("foreign")
    os.link(foreign, lock)
    with pytest.raises(ValueError, match="owned regular"):
        with ledger.run_lease(root, run_id):
            pass


def expected_record(spec: RunSpec) -> RunRecord:
    return RunRecord(
        compute_run_id(spec.resolved_config, spec.seed),
        spec.campaign,
        spec.seed,
        spec.expected_context_hash,
        "",
        0.0,
        spec.parameters,
    )


def test_v2_marker_has_canonical_manifest_and_copies_zstd_bytes(
    tmp_path: Path,
) -> None:
    spec = run_spec(rate=1)
    marker, artifact, source_bytes = complete_v2(tmp_path, spec)
    metadata = dict(pq.ParquetFile(marker).schema_arrow.metadata or {})
    manifest = json.loads(metadata[ledger.ARTIFACT_MANIFEST])
    canonical = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()

    assert metadata[ledger.COMPLETION_MARKER_VERSION] == b"2"
    assert metadata[ledger.ARTIFACT_MANIFEST] == canonical
    assert manifest == [
        {
            "name": "output.parquet",
            "sha256": hashlib.sha256(source_bytes).hexdigest(),
            "size": len(source_bytes),
        }
    ]
    assert artifact.read_bytes() == source_bytes
    state = ledger.completion_state(tmp_path, expected_record(spec))
    assert state is not None and state.archive_integrity == ledger.VERIFIED
    assert is_complete(tmp_path, spec)


def test_v2_marker_binds_canonical_execution_evidence(tmp_path: Path) -> None:
    spec = run_spec(rate=1)
    expected = execution_evidence()
    marker, _, _ = complete_v2(tmp_path, spec, expected)
    metadata = dict(pq.ParquetFile(marker).schema_arrow.metadata or {})

    assert metadata[ledger.EXECUTION_EVIDENCE] == expected.canonical_bytes()
    document = json.loads(metadata[ledger.EXECUTION_EVIDENCE])
    digest = document.pop("evidenceSha256")
    descriptor = json.dumps(
        document, sort_keys=True, separators=(",", ":")
    ).encode()
    assert digest == hashlib.sha256(EXECUTION_EVIDENCE_DOMAIN + descriptor).hexdigest()
    state = ledger.completion_state(tmp_path, expected_record(spec))
    assert state is not None and state.execution_evidence == expected


@pytest.mark.parametrize(
    "damage", ["format", "digest", "context", "invocation", "extra"]
)
def test_v2_reader_rejects_execution_evidence_metadata_tamper(
    tmp_path: Path, damage: str
) -> None:
    spec = run_spec(rate=1)
    marker, _, _ = complete_v2(tmp_path, spec, execution_evidence())
    table = pq.read_table(marker)
    metadata = dict(table.schema.metadata or {})
    document = json.loads(metadata[ledger.EXECUTION_EVIDENCE])
    if damage == "format":
        metadata[ledger.EXECUTION_EVIDENCE] = json.dumps(document, indent=2).encode()
    else:
        if damage == "digest":
            document["environmentSha256"] = "invalid"
        elif damage == "context":
            document = json.loads(execution_evidence("d" * 64).canonical_bytes())
        elif damage == "invocation":
            document["invocation"].append("--tampered")
        else:
            document["unexpected"] = True
        metadata[ledger.EXECUTION_EVIDENCE] = json.dumps(
            document, sort_keys=True, separators=(",", ":")
        ).encode()
    pq.write_table(table.replace_schema_metadata(metadata), marker, compression="zstd")

    with pytest.raises(ValueError, match="completion marker"):
        is_complete(tmp_path, spec)


@pytest.mark.parametrize(
    "damage",
    ["tamper", "hash", "missing", "extra", "symlink", "directory", "fifo"],
)
def test_v2_reader_rejects_archive_inventory_and_content_damage(
    tmp_path: Path, damage: str
) -> None:
    spec = run_spec(rate=1)
    _, artifact, _ = complete_v2(tmp_path, spec)
    if damage == "tamper":
        artifact.write_bytes(artifact.read_bytes() + b"tampered")
    elif damage == "hash":
        content = bytearray(artifact.read_bytes())
        content[len(content) // 2] ^= 1
        artifact.write_bytes(content)
    elif damage == "missing":
        artifact.unlink()
    elif damage == "extra":
        pq.write_table(
            pa.table({"extra": [1]}), artifact.parent / "extra.parquet"
        )
    elif damage == "symlink":
        source = tmp_path / "source.parquet"
        artifact.unlink()
        artifact.symlink_to(source)
    elif damage == "directory":
        artifact.unlink()
        artifact.mkdir()
    else:
        artifact.unlink()
        os.mkfifo(artifact)

    with pytest.raises(ValueError, match="completion marker"):
        is_complete(tmp_path, spec)


def test_v2_reader_rejects_archive_changed_while_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = run_spec(rate=1)
    _, artifact, _ = complete_v2(tmp_path, spec)
    original_hash = ledger._hash_descriptor

    def mutate_after_hash(descriptor: int, interrupted=None):  # type: ignore[no-untyped-def]
        digest = original_hash(descriptor, interrupted)
        artifact.write_bytes(b"x" * artifact.stat().st_size)
        return digest

    monkeypatch.setattr(ledger, "_hash_descriptor", mutate_after_hash)
    with pytest.raises(ValueError, match="changed while hashing"):
        is_complete(tmp_path, spec)


def test_v2_reader_rejects_campaign_root_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = tmp_path / "campaign"
    spec = run_spec(rate=1)
    complete_v2(campaign, spec)
    original_hash = ledger._hash_descriptor

    def replace_root_after_hash(descriptor: int, interrupted=None):  # type: ignore[no-untyped-def]
        digest = original_hash(descriptor, interrupted)
        campaign.rename(tmp_path / "detached")
        campaign.mkdir()
        return digest

    monkeypatch.setattr(ledger, "_hash_descriptor", replace_root_after_hash)
    with pytest.raises(ValueError, match="campaign root changed"):
        is_complete(campaign, spec)


def test_v2_reader_rejects_symlinked_archive_directory(tmp_path: Path) -> None:
    spec = run_spec(rate=1)
    _, artifact, _ = complete_v2(tmp_path, spec)
    archive = artifact.parent
    detached = archive.with_name("detached")
    archive.rename(detached)
    archive.symlink_to(detached, target_is_directory=True)

    with pytest.raises(ValueError, match="completion marker"):
        is_complete(tmp_path, spec)


@pytest.mark.parametrize("explicit", [False, True])
def test_v1_marker_is_read_as_legacy_unbound_without_migration(
    tmp_path: Path, explicit: bool
) -> None:
    spec = run_spec(rate=1)
    marker = complete(tmp_path, spec)
    if explicit:
        table = pq.read_table(marker).replace_schema_metadata(
            {ledger.COMPLETION_MARKER_VERSION: b"1"}
        )
        pq.write_table(table, marker, compression="zstd")
    before = marker.read_bytes()

    state = ledger.completion_state(tmp_path, expected_record(spec))

    assert state is not None
    assert state.archive_integrity == ledger.LEGACY_UNBOUND
    assert state.artifacts == ()
    assert marker.read_bytes() == before


def test_unknown_completion_marker_version_is_rejected(tmp_path: Path) -> None:
    spec = run_spec(rate=1)
    marker = complete(tmp_path, spec)
    table = pq.read_table(marker).replace_schema_metadata(
        {ledger.COMPLETION_MARKER_VERSION: b"99"}
    )
    pq.write_table(table, marker, compression="zstd")

    with pytest.raises(ValueError, match="unsupported completion marker version"):
        is_complete(tmp_path, spec)


def test_noncanonical_v2_manifest_is_rejected(tmp_path: Path) -> None:
    spec = run_spec(rate=1)
    marker, _, _ = complete_v2(tmp_path, spec)
    table = pq.read_table(marker)
    metadata = dict(table.schema.metadata or {})
    document = json.loads(metadata[ledger.ARTIFACT_MANIFEST])
    metadata[ledger.ARTIFACT_MANIFEST] = json.dumps(document, indent=2).encode()
    pq.write_table(
        table.replace_schema_metadata(metadata), marker, compression="zstd"
    )

    with pytest.raises(ValueError, match="noncanonical"):
        is_complete(tmp_path, spec)


def test_marker_writer_rejects_symlinked_parent_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    (campaign / "runs").symlink_to(outside, target_is_directory=True)
    record = RunRecord("a" * 32, "demo", 1, "ctx", "node", 1.0, {})

    with pytest.raises(OSError):
        write_run(campaign, record)
    assert not tuple(outside.iterdir())


def test_pending_runs_are_ordered_and_validated_before_filtering(
    tmp_path: Path,
) -> None:
    first, second = run_spec(41, rate=1), run_spec(42, rate=2)
    assert pending_run_specs(tmp_path, (first, second)) == (first, second)
    complete(tmp_path, first)
    assert pending_run_specs(tmp_path, (first, second)) == (second,)
    complete(tmp_path, second)
    assert pending_run_specs(tmp_path, (first, second)) == ()


@pytest.mark.parametrize("unsafe", ["foreign", "symlink", "directory", "truncated"])
def test_pending_runs_reject_every_unsafe_runs_entry(
    tmp_path: Path, unsafe: str
) -> None:
    spec = run_spec(rate=1)
    runs = tmp_path / "runs"
    runs.mkdir()
    path = runs / (
        "notes.txt"
        if unsafe == "foreign"
        else f"{compute_run_id(spec.resolved_config, spec.seed)}.parquet"
    )
    if unsafe == "symlink":
        path.symlink_to(tmp_path / "missing")
    elif unsafe == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"PAR1" if unsafe == "truncated" else b"foreign")
    with pytest.raises(ValueError, match="completion marker"):
        pending_run_specs(tmp_path, (spec,))


@pytest.mark.parametrize(
    "damage",
    [
        "campaign",
        "seed",
        "status",
        "attempts",
        "contextHash",
        "parameter",
        "null",
        "extra",
        "missing",
        "rows",
        "compression",
    ],
)
def test_pending_runs_reject_marker_contract_damage(
    tmp_path: Path, damage: str
) -> None:
    spec = run_spec(rate=1)
    path = complete(tmp_path, spec)
    table = pq.read_table(path)
    rows = table.to_pylist()
    if damage == "rows":
        table = pa.Table.from_pylist(rows * 2, schema=table.schema)
    elif damage == "extra":
        table = table.append_column("extra", pa.array([1]))
    elif damage == "missing":
        table = table.drop(["node"])
    elif damage == "null":
        table = table.set_column(
            table.column_names.index("node"), "node", pa.array([None], type=pa.string())
        )
    elif damage == "compression":
        pass
    else:
        column = "rate" if damage == "parameter" else damage
        value = {
            "campaign": "other",
            "seed": 9,
            "status": "failed",
            "attempts": 2,
            "contextHash": "d" * 64,
            "parameter": 2,
        }[damage]
        table = table.set_column(
            table.column_names.index(column),
            column,
            pa.array([value], type=table.schema.field(column).type),
        )
    pq.write_table(table, path, compression=None if damage == "compression" else "zstd")
    with pytest.raises(ValueError, match="completion marker"):
        pending_run_specs(tmp_path, (spec,))


def test_is_complete_uses_the_strict_expected_contract(tmp_path: Path) -> None:
    spec = run_spec(rate=1)
    complete(tmp_path, spec)
    assert is_complete(tmp_path, spec)
    assert not is_complete(tmp_path, run_spec(rate=2))
