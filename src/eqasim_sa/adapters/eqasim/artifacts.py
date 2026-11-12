"""Normalize eqasim CSV outputs into the canonical Parquet artifacts."""

from pathlib import Path

import pyarrow as pa
import pyarrow.csv as csv
import pyarrow.parquet as pq

from eqasim_sa.ports.simulator import Artifact
from eqasim_sa.utils import platform

POPULATION = (
    "persons",
    "households",
    "activities",
    "trips",
    "vehicle_types",
    "vehicles",
)
ANALYSIS = ("age", "age_purpose", "distance", "license", "nbr_vehicle", "travel")
STAGE_ARTIFACTS = {
    "synthesis.output": POPULATION,
    "analysis.synthesis.population": ANALYSIS,
}
ARTIFACT_STAGE = {
    artifact: stage
    for stage, artifacts in STAGE_ARTIFACTS.items()
    for artifact in artifacts
}
CSV_BLOCK_SIZE = 1 << 20


def normalize_outputs(
    output: Path, normalized: Path, prefix: str, stages: tuple[str, ...] | None = None
) -> tuple[Artifact, ...]:
    """Convert requested stage outputs to validated immutable Parquet files."""

    normalized.mkdir()
    artifacts: list[Artifact] = []
    requested = tuple(STAGE_ARTIFACTS) if stages is None else stages
    for stage in requested:
        if stage not in STAGE_ARTIFACTS:
            raise ValueError(f"unsupported producing stage: {stage!r}")
        for name in STAGE_ARTIFACTS[stage]:
            analysis = stage == "analysis.synthesis.population"
            source = (
                output / "analysis_population" / f"{prefix}{name}.csv"
                if analysis
                else output / f"{prefix}{name}.csv"
            )
            artifacts.append(
                _normalize(
                    source, output, normalized, name, "," if analysis else ";", analysis
                )
            )
    return tuple(artifacts)


def _normalize(
    source: Path,
    output: Path,
    normalized: Path,
    name: str,
    delimiter: str,
    remove_index: bool,
) -> Artifact:
    if platform.is_link(source):
        raise ValueError(
            f"eqasim artifact symlink escapes the accepted source contract: {name}"
        )
    try:
        resolved_source = source.resolve(strict=True)
        resolved_output = output.resolve(strict=True)
        resolved_attempt = normalized.parent.resolve(strict=True)
    except FileNotFoundError as error:
        raise FileNotFoundError(f"missing eqasim artifact: {name}")
    if resolved_output == resolved_attempt or not resolved_output.is_relative_to(
        resolved_attempt
    ):
        raise ValueError("eqasim output directory escapes attempt directory")
    if not resolved_source.is_file() or not resolved_source.is_relative_to(
        resolved_output
    ):
        raise ValueError(f"eqasim artifact escapes output directory: {name}")
    destination = normalized / f"{name}.parquet"
    try:
        with csv.open_csv(
            resolved_source,
            read_options=csv.ReadOptions(block_size=CSV_BLOCK_SIZE),
            parse_options=csv.ParseOptions(delimiter=delimiter),
        ) as reader:
            column_names = reader.schema.names
            has_index = remove_index and "" in column_names
            if has_index and (column_names.count("") != 1 or column_names[0] != ""):
                raise ValueError(f"invalid anonymous index in {source}")
            if "" in column_names and not has_index:
                raise ValueError(f"unexpected anonymous column in {source}")
            if has_index and not pa.types.is_integer(reader.schema.field(0).type):
                raise ValueError(f"anonymous index is not sequential in {source}")

            schema = reader.schema.remove(0) if has_index else reader.schema
            offset = 0
            with pq.ParquetWriter(destination, schema, compression="zstd") as writer:
                for batch in reader:
                    if has_index:
                        index = batch.column(0)
                        expected = range(offset, offset + batch.num_rows)
                        if index.null_count or any(
                            value != position
                            for value, position in zip(index.to_pylist(), expected)
                        ):
                            raise ValueError(
                                f"anonymous index is not sequential in {source}"
                            )
                        batch = batch.remove_column(0)
                    writer.write_batch(batch)
                    offset += batch.num_rows
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return Artifact(destination.name, destination)
