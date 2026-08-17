import math
import os
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Protocol

import pyarrow as pa
import pyarrow.parquet as pq


@dataclass(frozen=True)
class ArtifactSet:
    paths: Mapping[str, Path]

    def __post_init__(self) -> None:
        if not isinstance(self.paths, Mapping):
            raise ValueError("artifact paths must be a mapping")
        checked: dict[str, Path] = {}
        for name, path in self.paths.items():
            if not isinstance(name, str) or not name or not isinstance(path, Path):
                raise ValueError(
                    "artifact names must be non-empty strings and paths must be Path objects"
                )
            if path.suffix != ".parquet":
                raise ValueError(f"artifact {name!r} must be parquet")
            try:
                resolved = path.resolve(strict=True)
            except OSError as error:
                raise ValueError(f"artifact {name!r} must be a regular file") from error
            if resolved != path.absolute() or not resolved.is_file():
                raise ValueError(f"artifact {name!r} must not use a symlink")
            checked[name] = resolved
        object.__setattr__(self, "paths", MappingProxyType(checked))

    def path(self, name: str) -> Path:
        try:
            return self.paths[name]
        except KeyError as error:
            raise ValueError(f"artifact {name!r} is not available") from error


@dataclass(frozen=True)
class TableArtifact:
    name: str
    class_column: str
    synthetic_column: str
    carries_reference: bool

    def __post_init__(self) -> None:
        names = (self.name, self.class_column, self.synthetic_column)
        if not all(isinstance(name, str) and name for name in names):
            raise ValueError("artifact and column names must not be empty")
        if type(self.carries_reference) is not bool:
            raise ValueError("carries_reference must be boolean")
        if self.class_column == self.synthetic_column:
            raise ValueError("class and synthetic columns must be distinct")


@dataclass(frozen=True)
class ArtifactDeclaration:
    class_column: str
    synthetic_column: str
    references: tuple[str, ...]

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (self.class_column, self.synthetic_column, *self.references)
        ):
            raise ValueError("artifact columns must be non-empty strings")
        if self.class_column == self.synthetic_column or len(
            set(self.references)
        ) != len(self.references):
            raise ValueError("artifact columns and references must be distinct")
        if set(self.references) & {self.class_column, self.synthetic_column}:
            raise ValueError(
                "artifact references must differ from class and synthetic columns"
            )


@dataclass(frozen=True)
class PairDeclaration:
    scalarizer: str
    artifact: str
    reference: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (self.scalarizer, self.artifact, self.reference)
        ):
            raise ValueError("pair values must be non-empty strings")


@dataclass(frozen=True)
class ProductDeclaration:
    scalarizers: tuple[str, ...]
    artifacts: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.scalarizers or not self.artifacts:
            raise ValueError("product scalarizers and artifacts must not be empty")


@dataclass(frozen=True)
class CustomDeclaration:
    function: str
    artifact: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value for value in (self.function, self.artifact)
        ):
            raise ValueError("custom values must be non-empty strings")


class Indicator(Protocol):
    @property
    def name(self) -> str: ...

    def compute(self, artifacts: ArtifactSet) -> float: ...


@dataclass(frozen=True)
class RegisteredIndicator:
    function: str
    artifact: str
    implementation: object

    @property
    def name(self) -> str:
        return f"{self.function}({self.artifact})"

    @property
    def identity(self) -> tuple[str, str]:
        return self.function, self.artifact

    def compute(self, artifacts: ArtifactSet) -> float:
        artifacts.path(self.artifact)
        compute = getattr(self.implementation, "compute", self.implementation)
        if not callable(compute):
            raise TypeError(
                f"custom indicator {self.function!r} must provide compute(artifacts)"
            )
        value = compute(artifacts)
        if type(value) is not float or not math.isfinite(value):
            raise TypeError(
                f"custom indicator {self.function!r} must return a finite float"
            )
        return value


@dataclass(frozen=True)
class IndicatorIdentity:
    scalarizer: str
    artifact: str
    reference: str


@dataclass(frozen=True)
class TotalVariationIndicator:
    artifact: TableArtifact
    reference_column: str

    def __post_init__(self) -> None:
        if not self.artifact.carries_reference:
            raise ValueError(
                f"artifact {self.artifact.name!r} does not carry a reference"
            )
        if not isinstance(self.reference_column, str) or not self.reference_column:
            raise ValueError("reference column must not be empty")
        if self.reference_column in (
            self.artifact.class_column,
            self.artifact.synthetic_column,
        ):
            raise ValueError("class, synthetic and reference columns must be distinct")

    @property
    def identity(self) -> IndicatorIdentity:
        return IndicatorIdentity(
            "tv_distance", self.artifact.name, self.reference_column
        )

    @property
    def name(self) -> str:
        return f"tv_distance({self.artifact.name}, ref={self.reference_column})"

    def compute(self, artifacts: ArtifactSet) -> float:
        columns = (
            self.artifact.class_column,
            self.artifact.synthetic_column,
            self.reference_column,
        )
        table = _read_columns(
            artifacts.path(self.artifact.name), columns, self.artifact.name
        )
        if table[columns[0]].null_count:
            raise ValueError(f"class column {columns[0]!r} contains null values")
        synthetic = _normalized_masses(table[columns[1]], columns[1])
        reference = _normalized_masses(table[columns[2]], columns[2])
        value = 0.5 * math.fsum(
            abs(left - right) for left, right in zip(reference, synthetic, strict=True)
        )
        if not math.isfinite(value) or value < -1e-12 or value > 1.0 + 1e-12:
            raise ValueError("total variation is outside [0, 1]")
        return float(min(1.0, max(0.0, value)))


ScalarizerFactory = Callable[[TableArtifact, str], Indicator]
CustomFactory = Callable[[TableArtifact], object]


def resolve_indicators(
    declarations: Sequence[PairDeclaration | ProductDeclaration | CustomDeclaration],
    artifacts: Mapping[str, ArtifactDeclaration],
    scalarizers: Mapping[str, ScalarizerFactory],
    custom: Mapping[str, CustomFactory],
) -> tuple[object, ...]:
    resolved: list[object] = []
    errors: list[str] = []

    def table(name: str) -> TableArtifact | None:
        declaration = artifacts.get(name)
        if declaration is None:
            return None
        return TableArtifact(
            name,
            declaration.class_column,
            declaration.synthetic_column,
            bool(declaration.references),
        )

    def pair(scalarizer: str, artifact: str, reference: str | None = None) -> None:
        factory, source = scalarizers.get(scalarizer), table(artifact)
        if source is None:
            errors.append(f"{scalarizer}({artifact})")
            return
        references = (
            (reference,) if reference is not None else artifacts[artifact].references
        )
        if not references:
            errors.append(f"{scalarizer}({artifact})")
            return
        for selected in references:
            if factory is None:
                errors.append(f"{scalarizer}({artifact}, ref={selected})")
                continue
            if selected not in artifacts[artifact].references:
                errors.append(f"{scalarizer}({artifact}, ref={selected})")
                continue
            try:
                resolved.append(factory(source, selected))
            except (TypeError, ValueError):
                errors.append(f"{scalarizer}({artifact}, ref={selected})")

    for declaration in declarations:
        if isinstance(declaration, PairDeclaration):
            pair(declaration.scalarizer, declaration.artifact, declaration.reference)
        elif isinstance(declaration, ProductDeclaration):
            for scalarizer in declaration.scalarizers:
                for artifact in declaration.artifacts:
                    pair(scalarizer, artifact)
        else:
            factory, source = (
                custom.get(declaration.function),
                table(declaration.artifact),
            )
            if factory is None or source is None:
                errors.append(f"{declaration.function}({declaration.artifact})")
            else:
                resolved.append(
                    RegisteredIndicator(
                        declaration.function,
                        declaration.artifact,
                        factory(source),
                    )
                )
    if errors:
        raise ValueError("invalid indicator combinations: " + ", ".join(errors))
    return tuple(resolved)


def _normalized_masses(column: pa.ChunkedArray, name: str) -> tuple[float, ...]:
    if not (
        pa.types.is_integer(column.type)
        or pa.types.is_floating(column.type)
        or pa.types.is_decimal(column.type)
    ):
        raise ValueError(f"column {name!r} must be numeric")
    if column.null_count:
        raise ValueError(f"column {name!r} contains null values")
    values = tuple(float(value) for value in column.to_pylist())
    if not all(math.isfinite(value) for value in values):
        raise ValueError(f"column {name!r} must contain finite values")
    if any(value < 0 for value in values):
        raise ValueError(f"column {name!r} must contain non-negative values")
    scale = max(values, default=0.0)
    if scale <= 0:
        raise ValueError(f"column {name!r} must have a positive sum")
    scaled = tuple(value / scale for value in values)
    total = math.fsum(scaled)
    return tuple(value / total for value in scaled)


def _read_columns(path: Path, columns: tuple[str, ...], artifact: str) -> pa.Table:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"artifact {artifact!r} must be a regular file")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            parquet = pq.ParquetFile(source)
            missing = [
                name for name in columns if name not in parquet.schema_arrow.names
            ]
            if missing:
                raise ValueError(
                    f"artifact {artifact!r} is missing declared column {missing[0]!r}"
                )
            return parquet.read(columns=list(columns))
    except (OSError, pa.ArrowInvalid, pa.ArrowIOError) as error:
        raise ValueError(
            f"artifact {artifact!r} cannot be read as parquet: {error}"
        ) from error
    except ValueError:
        raise
    finally:
        if "descriptor" in locals() and descriptor >= 0:
            os.close(descriptor)
