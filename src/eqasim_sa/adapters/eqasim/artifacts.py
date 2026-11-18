"""Known eqasim outputs and byte-for-byte collection of declared output files."""

import os
import shutil
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from eqasim_sa.core.indicators import OutputDeclaration
from eqasim_sa.core.ledger import archive_file_name
from eqasim_sa.ports.simulator import Artifact
from eqasim_sa.utils import platform

DEFAULT_OUTPUT_PREFIX = "ile_de_france_"


@dataclass(frozen=True)
class KnownOutput:
    """Default file, separator and producing stage of an output eqasim writes."""

    file: str
    separator: str
    stage: str


POPULATION = (
    "persons",
    "households",
    "activities",
    "trips",
    "vehicle_types",
    "vehicles",
)
ANALYSIS = ("age", "age_purpose", "distance", "license", "nbr_vehicle", "travel")
KNOWN_OUTPUTS: Mapping[str, KnownOutput] = {
    **{
        name: KnownOutput(f"{{prefix}}{name}.csv", ";", "synthesis.output")
        for name in POPULATION
    },
    **{
        name: KnownOutput(
            f"analysis_population/{{prefix}}{name}.csv",
            ",",
            "analysis.synthesis.population",
        )
        for name in ANALYSIS
    },
}


def collect_outputs(
    output: Path,
    destination: Path,
    prefix: str,
    outputs: Mapping[str, OutputDeclaration],
) -> tuple[Artifact, ...]:
    """Copy every declared output byte for byte into ``destination``.

    Each copy is named ``<name><suffix>`` and remembers its path relative to
    ``output`` (eqasim's ``output_path``).  Undeclared files are ignored.
    """

    destination.mkdir()
    try:
        resolved_output = output.resolve(strict=True)
        resolved_attempt = destination.parent.resolve(strict=True)
    except FileNotFoundError:
        raise FileNotFoundError("eqasim output directory is missing")
    if resolved_output == resolved_attempt or not resolved_output.is_relative_to(
        resolved_attempt
    ):
        raise ValueError("eqasim output directory escapes attempt directory")
    artifacts = []
    for name, declaration in outputs.items():
        relative = declaration.source_path(prefix)
        artifacts.append(
            _collect(
                output,
                resolved_output,
                destination,
                name,
                relative,
            )
        )
    return tuple(artifacts)


def _collect(
    output: Path,
    resolved_output: Path,
    destination: Path,
    name: str,
    relative: str,
) -> Artifact:
    source = output.joinpath(*PurePosixPath(relative).parts)
    current = output
    for part in PurePosixPath(relative).parts:
        current = current / part
        if platform.is_link(current):
            raise ValueError(
                f"eqasim output symlink escapes the accepted source contract: {name}"
            )
    try:
        resolved_source = source.resolve(strict=True)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"eqasim did not write declared output {name!r} (expected {relative}); "
            "check that the stage producing it is in run: of your eqasim config"
        ) from None
    if not resolved_source.is_file() or not resolved_source.is_relative_to(
        resolved_output
    ):
        raise ValueError(f"eqasim output escapes output directory: {name}")
    target = destination / archive_file_name(name, relative)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(resolved_source, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"eqasim output is not a regular file: {name}")
        with os.fdopen(descriptor, "rb") as reader, target.open("xb") as writer:
            descriptor = -1
            shutil.copyfileobj(reader, writer, 1024 * 1024)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return Artifact(name, target, relative)
