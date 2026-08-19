from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from eqasim_sa.core import ledger
from eqasim_sa.core.ledger import RunRecord, is_complete, run_path, write_run
from eqasim_sa.core.run import RunSpec, compute_run_id, pending_run_specs


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
        write_run(tmp_path, RunRecord("a" * 32, "c", 1, "x", "n", 0, {"seed": 3}))
    with pytest.raises(TypeError):
        write_run(tmp_path, RunRecord("a" * 32, "c", 1, "x", "n", 0, {"x": [1]}))
    with pytest.raises(TypeError):
        write_run(tmp_path, RunRecord("a" * 32, "c", 1, "x", "n", 0, {"x": None}))


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

    def fail_replace(source: Path, target: Path) -> None:
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

    def fail_rollback(source: Path, target: Path) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("rollback failure")
        real_replace(source, target)

    monkeypatch.setattr(ledger.os, "fsync", fail_directory_fsync)
    monkeypatch.setattr(ledger.os, "replace", fail_rollback)
    with pytest.raises(OSError, match="rollback failure"):
        ledger.write_parquet_atomic(pa.table({"value": [2]}), destination)
    backups = list(tmp_path.glob(".*.backup"))
    assert len(backups) == 1
    assert pq.read_table(backups[0]).to_pylist() == [{"value": 1}]


def test_run_marker_temporary_is_staged_outside_runs(tmp_path: Path) -> None:
    destination = tmp_path / "campaign" / "runs" / ("a" * 32 + ".parquet")

    def fail_writer(path: Path) -> None:
        assert path.parent == tmp_path / "campaign" / ".eqasim-sa-tmp"
        path.write_bytes(b"partial")
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        ledger._write_atomic(destination, fail_writer)
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
