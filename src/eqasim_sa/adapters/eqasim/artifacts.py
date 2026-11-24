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


# What eqasim's synthesis/output.py does when output_formats is not set.
DEFAULT_OUTPUT_FORMATS = ("csv", "gpkg")


@dataclass(frozen=True)
class KnownOutput:
    """Files, separator and producing stage of an output eqasim writes.

    ``files`` pairs each ``output_formats`` entry that makes eqasim write the
    output with the file it then writes, in order of preference; ``None``
    means the file does not depend on ``output_formats``.  ``separator`` is
    ``None`` for a file that is not a CSV table.  ``mode_choice`` outputs are
    written only when ``mode_choice`` is true in the eqasim config.
    """

    files: tuple[tuple[str | None, str], ...]
    separator: str | None
    stage: str
    mode_choice: bool = False

    def default_file(self, formats: object = None) -> str | None:
        """Default file; for a choice of files, the first ``formats`` enables.

        An output with a single file always defaults to it (a missing format
        is the loader's error to report).  ``None`` when ``formats`` (the
        eqasim ``output_formats`` list) is unknown or enables no file.
        """

        if len(self.files) == 1:
            return self.files[0][1]
        if not isinstance(formats, (list, tuple)):
            return None
        return next((file for fmt, file in self.files if fmt in formats), None)

    def format_of(self, file: str) -> str | None:
        """``output_formats`` entry that makes eqasim write ``file``."""

        return next((fmt for fmt, item in self.files if item == file), None)

    @property
    def formats(self) -> tuple[str, ...]:
        return tuple(fmt for fmt, _ in self.files if fmt is not None)


POPULATION = (
    "persons",
    "households",
    "activities",
    "trips",
    "vehicle_types",
    "vehicles",
)
SPATIAL = ("activities", "homes", "commutes", "trips")
MODE_CHOICE = ("legs", "pt_legs")
ANALYSIS = ("age", "age_purpose", "distance", "license", "nbr_vehicle", "travel")
SYNTHESIS_OUTPUT = "synthesis.output"
KNOWN_OUTPUTS: Mapping[str, KnownOutput] = {
    **{
        name: KnownOutput(
            (("csv", f"{{prefix}}{name}.csv"),), ";", SYNTHESIS_OUTPUT
        )
        for name in POPULATION
    },
    **{
        f"{name}_parquet": KnownOutput(
            (("parquet", f"{{prefix}}{name}.parquet"),), None, SYNTHESIS_OUTPUT
        )
        for name in POPULATION
    },
    **{
        f"{name}_spatial": KnownOutput(
            (
                ("gpkg", f"{{prefix}}{name}.gpkg"),
                ("geoparquet", f"{{prefix}}{name}.geoparquet"),
            ),
            None,
            SYNTHESIS_OUTPUT,
        )
        for name in SPATIAL
    },
    # Copied, decompressed, from the MATSim mode choice output (";" tables).
    **{
        name: KnownOutput(
            ((None, f"{{prefix}}{name}.csv"),), ";", SYNTHESIS_OUTPUT, True
        )
        for name in MODE_CHOICE
    },
    **{
        name: KnownOutput(
            ((None, f"analysis_population/{{prefix}}{name}.csv"),),
            ",",
            "analysis.synthesis.population",
        )
        for name in ANALYSIS
    },
}


def synthesis_outputs(formats: object, mode_choice: bool) -> tuple[str, ...]:
    """Known outputs eqasim's synthesis/output.py writes, in declaration order.

    ``formats`` is the eqasim ``output_formats`` list (eqasim's default
    already applied); anything else enables no format-dependent output.
    """

    enabled = formats if isinstance(formats, (list, tuple)) else ()
    return tuple(
        name
        for name, known in KNOWN_OUTPUTS.items()
        if known.stage == SYNTHESIS_OUTPUT
        and (mode_choice or not known.mode_choice)
        and (not known.formats or any(fmt in enabled for fmt in known.formats))
    )


def is_csv_file(file: str) -> bool:
    """Whether ``file`` is a CSV table (the only files with a separator)."""

    return file.lower().endswith(".csv")


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
