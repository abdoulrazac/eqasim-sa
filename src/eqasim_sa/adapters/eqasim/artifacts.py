from pathlib import Path

import pyarrow as pa
import pyarrow.csv as csv
import pyarrow.parquet as pq

from eqasim_sa.ports.simulator import Artifact

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


def normalize_outputs(
    output: Path, normalized: Path, prefix: str, stages: tuple[str, ...] | None = None
) -> tuple[Artifact, ...]:
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
    if source.is_symlink():
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
    table = csv.read_csv(
        resolved_source, parse_options=csv.ParseOptions(delimiter=delimiter)
    )
    if remove_index and "" in table.column_names:
        if table.column_names.count("") != 1 or table.column_names[0] != "":
            raise ValueError(f"invalid anonymous index in {source}")
        index = table.column(0)
        if (
            not pa.types.is_integer(index.type)
            or index.null_count
            or index.to_pylist() != list(range(table.num_rows))
        ):
            raise ValueError(f"anonymous index is not sequential in {source}")
        table = table.remove_column(0)
    if "" in table.column_names:
        raise ValueError(f"unexpected anonymous column in {source}")
    destination = normalized / f"{name}.parquet"
    pq.write_table(table, destination, compression="zstd")
    return Artifact(destination.name, destination)
