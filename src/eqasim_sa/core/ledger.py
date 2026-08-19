import math
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

RESERVED_COLUMNS = (
    "runId",
    "campaign",
    "seed",
    "status",
    "attempts",
    "contextHash",
    "node",
    "duration",
    "scratchBytes",
)
Scalar = bool | int | float | str
RUN_ID = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    campaign: str
    seed: int
    context_hash: str
    node: str
    duration: float
    parameters: dict[str, Scalar]
    scratch_bytes: int = 0


def run_path(campaign_root: Path, run_id: str) -> Path:
    if not RUN_ID.fullmatch(run_id):
        raise ValueError(
            "run_id must contain exactly 32 lowercase hexadecimal characters"
        )
    return campaign_root / "runs" / f"{run_id}.parquet"


def _completion_duration(campaign_root: Path, expected: RunRecord) -> float | None:
    path = run_path(campaign_root, expected.run_id)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("not a regular non-symlink file")
        parquet = pq.ParquetFile(path)
        if parquet.metadata.num_rows != 1:
            raise ValueError("row count mismatch")
        table = parquet.read()
        schema_record = RunRecord(
            expected.run_id,
            expected.campaign,
            expected.seed,
            expected.context_hash,
            "",
            0.0,
            expected.parameters,
        )
        if not _run_table(schema_record).schema.equals(table.schema) or any(
            column.null_count for column in table.columns
        ):
            raise ValueError("schema, row count, or null value mismatch")
        row = table.to_pylist()[0]
        fixed = {
            "runId": expected.run_id,
            "campaign": expected.campaign,
            "seed": expected.seed,
            "status": "completed",
            "attempts": 1,
            "contextHash": expected.context_hash,
            **expected.parameters,
        }
        if any(
            row[name] != value or type(row[name]) is not type(value)
            for name, value in fixed.items()
        ):
            raise ValueError("identity, context, or parameter mismatch")
        duration = row["duration"]
        if type(duration) is not float or not math.isfinite(duration) or duration < 0:
            raise ValueError("duration must be a finite non-negative float")
        scratch_bytes = row["scratchBytes"]
        if type(scratch_bytes) is not int or scratch_bytes < 0:
            raise ValueError("scratchBytes must be a non-negative integer")
        codecs = {
            parquet.metadata.row_group(group).column(column).compression
            for group in range(parquet.metadata.num_row_groups)
            for column in range(parquet.metadata.row_group(group).num_columns)
        }
        if codecs != {"ZSTD"}:
            raise ValueError("compression must be zstd")
    except Exception as error:
        raise ValueError(f"invalid completion marker {path}: {error}") from error
    return duration


def completion_duration(campaign_root: Path, spec) -> float | None:  # type: ignore[no-untyped-def]
    from eqasim_sa.core.run import compute_run_id

    run_id = compute_run_id(spec.resolved_config, spec.seed)
    return _completion_duration(
        campaign_root,
        RunRecord(
            run_id,
            spec.campaign,
            spec.seed,
            spec.expected_context_hash,
            "",
            0.0,
            spec.parameters,
        ),
    )


def completion_scratch_bytes(campaign_root: Path, spec) -> int | None:  # type: ignore[no-untyped-def]
    from eqasim_sa.core.run import compute_run_id

    duration = completion_duration(campaign_root, spec)
    if duration is None:
        return None
    path = run_path(campaign_root, compute_run_id(spec.resolved_config, spec.seed))
    row = pq.ParquetFile(path).read(columns=["scratchBytes"]).to_pylist()[0]
    value = row["scratchBytes"]
    if type(value) is not int or value < 0:
        raise ValueError("scratchBytes must be a non-negative integer")
    return value


def completion_ids(campaign_root: Path, specs) -> set[str]:  # type: ignore[no-untyped-def]
    from eqasim_sa.core.run import compute_run_id

    runs = campaign_root / "runs"
    if not runs.exists() and not runs.is_symlink():
        return set()
    if runs.is_symlink() or not runs.is_dir():
        raise ValueError("runs must be a regular non-symlink directory")
    expected = {compute_run_id(spec.resolved_config, spec.seed): spec for spec in specs}
    entries = tuple(runs.iterdir())
    foreign = [
        entry.name
        for entry in entries
        if entry.suffix != ".parquet" or entry.stem not in expected
    ]
    if foreign:
        raise ValueError(f"invalid completion marker names: {sorted(foreign)}")
    return {
        entry.stem
        for entry in entries
        if completion_duration(campaign_root, expected[entry.stem]) is not None
    }


def plan_run_ids(
    campaign_root: Path, plan: Path, campaign: str
) -> tuple[set[str], set[str]]:
    if type(campaign) is not str or not campaign:
        raise ValueError("campaign must be a non-empty string")
    try:
        parquet = pq.ParquetFile(plan)
        table = parquet.read()
    except Exception as error:
        raise ValueError(f"cannot read plan: {error}") from error
    required = {"designId", "method", "role", "runId", "seed", "contextHash"}
    if not required <= set(table.column_names) or any(
        column.null_count for column in table.columns
    ):
        raise ValueError("plan schema or null values are invalid")
    parameters = tuple(name for name in table.column_names if name not in required)
    expected: dict[str, RunRecord] = {}
    for row in table.to_pylist():
        run_id = row["runId"]
        if type(run_id) is not str or not RUN_ID.fullmatch(run_id):
            raise ValueError("plan contains an invalid runId")
        record = RunRecord(
            run_id,
            campaign,
            row["seed"],
            row["contextHash"],
            "",
            0.0,
            {name: row[name] for name in parameters},
        )
        previous = expected.setdefault(run_id, record)
        if previous != record:
            raise ValueError(f"plan has inconsistent duplicate runId {run_id}")
    runs = campaign_root / "runs"
    if not runs.exists() and not runs.is_symlink():
        return set(expected), set()
    if runs.is_symlink() or not runs.is_dir():
        raise ValueError("runs must be a regular non-symlink directory")
    entries = tuple(runs.iterdir())
    foreign = [
        entry.name
        for entry in entries
        if entry.suffix != ".parquet" or entry.stem not in expected
    ]
    if foreign:
        raise ValueError(f"invalid completion marker names: {sorted(foreign)}")
    completed = {
        entry.stem
        for entry in entries
        if _completion_duration(campaign_root, expected[entry.stem]) is not None
    }
    return set(expected), completed


def plan_completion_ids(campaign_root: Path, plan: Path, campaign: str) -> set[str]:
    return plan_run_ids(campaign_root, plan, campaign)[1]


def is_complete(campaign_root: Path, spec) -> bool:  # type: ignore[no-untyped-def]
    return completion_duration(campaign_root, spec) is not None


def write_parquet_atomic(table: pa.Table, destination: Path) -> Path:
    return _write_atomic(
        destination, lambda path: pq.write_table(table, path, compression="zstd")
    )


def copy_file_atomic(source: Path, destination: Path) -> Path:
    def copy(temporary: Path) -> None:
        with source.open("rb") as input_stream, temporary.open("wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream)

    return _write_atomic(destination, copy)


def _write_atomic(destination: Path, writer) -> Path:  # type: ignore[no-untyped-def]
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = (
        destination.parent.parent / ".eqasim-sa-tmp"
        if destination.parent.name == "runs"
        else destination.parent
    )
    staging.mkdir(exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=staging
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    backup = destination.parent / temporary.with_suffix(".backup").name
    preserve_backup = False
    try:
        writer(temporary)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        if destination.exists():
            os.link(destination, backup)
        os.replace(temporary, destination)
        if os.name == "posix":
            try:
                fsync_directory(destination.parent)
            except OSError:
                if backup.exists():
                    try:
                        os.replace(backup, destination)
                        fsync_directory(destination.parent)
                    except OSError:
                        preserve_backup = True
                        raise
                else:
                    destination.unlink(missing_ok=True)
                    fsync_directory(destination.parent)
                raise
        backup.unlink(missing_ok=True)
        return destination
    except BaseException:
        temporary.unlink(missing_ok=True)
        if not preserve_backup:
            backup.unlink(missing_ok=True)
        raise


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def validate_parameters(parameters: dict[str, Scalar]) -> None:
    if collision := RESERVED_COLUMNS & parameters.keys():
        raise ValueError(f"reserved parameter columns: {sorted(collision)}")
    invalid_types = [
        name
        for name, value in parameters.items()
        if type(name) is not str or type(value) not in (bool, int, float, str)
    ]
    invalid_numbers = [
        name
        for name, value in parameters.items()
        if type(value) is int
        and not -(2**63) <= value < 2**63
        or type(value) is float
        and not math.isfinite(value)
    ]
    if invalid_types:
        raise TypeError(
            f"parameters must have string names and scalar values: {invalid_types}"
        )
    if invalid_numbers:
        raise ValueError(
            f"parameters must contain finite int64/float values: {invalid_numbers}"
        )


def write_run(campaign_root: Path, record: RunRecord) -> Path:
    validate_parameters(record.parameters)
    return write_parquet_atomic(
        _run_table(record), run_path(campaign_root, record.run_id)
    )


def _run_table(record: RunRecord) -> pa.Table:
    fixed: dict[str, Scalar] = {
        "runId": record.run_id,
        "campaign": record.campaign,
        "seed": record.seed,
        "status": "completed",
        "attempts": 1,
        "contextHash": record.context_hash,
        "node": record.node,
        "duration": record.duration,
        "scratchBytes": record.scratch_bytes,
    }
    return pa.Table.from_pylist([{**fixed, **record.parameters}])
