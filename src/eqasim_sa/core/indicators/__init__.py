"""Declare, resolve, identify, and execute builtin campaign indicators."""

import hashlib
import inspect
import json
import math
import os
import re
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Protocol

import pyarrow as pa
import pyarrow.parquet as pq

from eqasim_sa.core.software_identity import stable_published_identity


INDICATOR_DEFINITION_DOMAIN = b"eqasim-sa:indicator-definition:v2\0"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TOTAL_VARIATION_VERSION = 1


@dataclass(frozen=True)
class ArtifactSet:
    """Validated read-only mapping from artifact names to Parquet paths."""

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
    """Resolved table schema and producing stage used by an indicator."""

    name: str
    class_column: str
    synthetic_column: str
    carries_reference: bool
    reference_columns: tuple[str, ...] = ()
    producing_stage: str | None = None

    def __post_init__(self) -> None:
        names = (self.name, self.class_column, self.synthetic_column)
        if not all(isinstance(name, str) and name for name in names):
            raise ValueError("artifact and column names must not be empty")
        if type(self.carries_reference) is not bool:
            raise ValueError("carries_reference must be boolean")
        if self.class_column == self.synthetic_column:
            raise ValueError("class and synthetic columns must be distinct")
        if (
            not isinstance(self.reference_columns, tuple)
            or any(not isinstance(value, str) or not value for value in self.reference_columns)
            or len(set(self.reference_columns)) != len(self.reference_columns)
        ):
            raise ValueError("artifact reference columns must be unique non-empty strings")
        if self.reference_columns and not self.carries_reference:
            raise ValueError("artifact reference declaration is inconsistent")
        if set(self.reference_columns) & {self.class_column, self.synthetic_column}:
            raise ValueError("artifact reference columns must be distinct")
        if self.producing_stage is not None and (
            not isinstance(self.producing_stage, str) or not self.producing_stage
        ):
            raise ValueError("artifact producing stage must be a non-empty string")


@dataclass(frozen=True)
class ArtifactDeclaration:
    """User declaration of an artifact's value and reference columns."""

    class_column: str
    synthetic_column: str
    references: tuple[str, ...]
    producing_stage: str | None = None

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
        if self.producing_stage is not None and (
            not isinstance(self.producing_stage, str) or not self.producing_stage
        ):
            raise ValueError("artifact producing stage must be a non-empty string")


@dataclass(frozen=True)
class PairDeclaration:
    """One scalarizer applied to one artifact-reference pair."""

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
    """Cartesian declaration of scalarizers and artifacts."""

    scalarizers: tuple[str, ...]
    artifacts: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.scalarizers or not self.artifacts:
            raise ValueError("product scalarizers and artifacts must not be empty")


class Indicator(Protocol):
    """Scalar analysis computed from a validated set of artifacts."""

    @property
    def name(self) -> str: ...

    def compute(self, artifacts: ArtifactSet) -> float: ...


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _source_sha256(value: type) -> str | None:
    try:
        source = inspect.getsourcefile(value)
        if source is None:
            return None
        _, _, sha256 = stable_published_identity(
            Path(source).resolve(strict=True), text=True
        )
        return sha256
    except (OSError, TypeError, ValueError):
        return None


def analysis_identity(indicator: Indicator) -> dict[str, object]:
    """Build and validate the canonical identity of a builtin indicator."""

    if not isinstance(indicator, TotalVariationIndicator):
        raise TypeError(f"unsupported indicator: {type(indicator).__qualname__}")
    artifact = indicator.artifact
    definition = {
        "schemaVersion": 1,
        "name": indicator.name,
        "kind": "builtin",
        "scalarizer": {
            "name": indicator.identity.scalarizer,
            "version": TOTAL_VARIATION_VERSION,
        },
        "artifact": {
            "name": artifact.name,
            "classColumn": artifact.class_column,
            "syntheticColumn": artifact.synthetic_column,
            "referenceColumns": [indicator.reference_column],
            "producingStage": artifact.producing_stage,
        },
        "implementation": {
            "identity": "eqasim_sa.core.indicators:TotalVariationIndicator",
            "sha256": _source_sha256(TotalVariationIndicator),
            "version": TOTAL_VARIATION_VERSION,
        },
    }
    validate_indicator_definition(definition)
    return definition


def validate_indicator_definition(
    value: object, *, require_scientific: bool = False
) -> dict[str, object]:
    """Validate a canonical indicator definition and scientific requirements."""

    if not isinstance(value, dict) or set(value) != {
        "schemaVersion",
        "name",
        "kind",
        "scalarizer",
        "artifact",
        "implementation",
    }:
        raise ValueError("indicator definition has an invalid schema")
    if value["schemaVersion"] != 1 or type(value["schemaVersion"]) is not int:
        raise ValueError("indicator definition schemaVersion must equal 1")
    if not isinstance(value["name"], str) or not value["name"]:
        raise ValueError("indicator definition name must be non-empty")
    if value["kind"] != "builtin":
        raise ValueError("indicator definition kind is unsupported")
    scalarizer = value["scalarizer"]
    if (
        not isinstance(scalarizer, dict)
        or set(scalarizer) != {"name", "version"}
        or not isinstance(scalarizer["name"], str)
        or not scalarizer["name"]
        or type(scalarizer["version"]) is not int
        or scalarizer["version"] <= 0
    ):
        raise ValueError("indicator definition scalarizer is invalid")
    artifact = value["artifact"]
    if artifact is not None:
        if not isinstance(artifact, dict) or set(artifact) != {
            "name",
            "classColumn",
            "syntheticColumn",
            "referenceColumns",
            "producingStage",
        }:
            raise ValueError("indicator definition artifact schema is invalid")
        if not isinstance(artifact["name"], str) or not artifact["name"]:
            raise ValueError("indicator definition artifact name is invalid")
        columns = (artifact["classColumn"], artifact["syntheticColumn"])
        if any(item is not None and (not isinstance(item, str) or not item) for item in columns):
            raise ValueError("indicator definition artifact columns are invalid")
        references = artifact["referenceColumns"]
        if (
            not isinstance(references, list)
            or any(not isinstance(item, str) or not item for item in references)
            or len(set(references)) != len(references)
        ):
            raise ValueError("indicator definition reference columns are invalid")
        stage = artifact["producingStage"]
        if stage is not None and (not isinstance(stage, str) or not stage):
            raise ValueError("indicator definition producing stage is invalid")
        if require_scientific and (
            any(item is None for item in columns) or stage is None
        ):
            raise ValueError("scientific indicator definition requires all artifact provenance")
    elif require_scientific:
        raise ValueError("scientific indicator definition requires an artifact")
    implementation = value["implementation"]
    if (
        not isinstance(implementation, dict)
        or set(implementation) != {"identity", "sha256", "version"}
        or implementation["identity"] is not None
        and (not isinstance(implementation["identity"], str) or not implementation["identity"])
        or implementation["sha256"] is not None
        and (
            not isinstance(implementation["sha256"], str)
            or SHA256.fullmatch(implementation["sha256"]) is None
        )
        or type(implementation["version"]) is not int
        or implementation["version"] <= 0
    ):
        raise ValueError("indicator definition implementation is invalid")
    if require_scientific and (
        implementation["identity"] is None or implementation["sha256"] is None
    ):
        raise ValueError("scientific indicator implementation must be immutable")
    try:
        if _canonical(value) != _canonical(json.loads(_canonical(value))):
            raise ValueError
    except (TypeError, ValueError) as error:
        raise ValueError("indicator definition is not canonical JSON") from error
    return value


def indicator_definition_sha256(value: object) -> str:
    """Hash a validated scientific indicator definition with its domain."""

    definition = validate_indicator_definition(value, require_scientific=True)
    return hashlib.sha256(
        INDICATOR_DEFINITION_DOMAIN + _canonical(definition)
    ).hexdigest()


@dataclass(frozen=True)
class IndicatorIdentity:
    """Builtin scalarizer, artifact, and reference identity."""

    scalarizer: str
    artifact: str
    reference: str


@dataclass(frozen=True)
class TotalVariationIndicator:
    """Compute total-variation distance between synthetic and reference shares."""

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
        labels = table[columns[0]].to_pylist()
        try:
            duplicate_labels = len(labels) != len(set(labels))
        except TypeError:
            duplicate_labels = any(
                label in labels[:index] for index, label in enumerate(labels)
            )
        if duplicate_labels:
            raise ValueError(
                f"class column {columns[0]!r} contains duplicate class labels"
            )
        synthetic = _normalized_masses(table[columns[1]], columns[1])
        reference = _normalized_masses(table[columns[2]], columns[2])
        value = 0.5 * math.fsum(
            abs(left - right) for left, right in zip(reference, synthetic, strict=True)
        )
        if not math.isfinite(value) or value < -1e-12 or value > 1.0 + 1e-12:
            raise ValueError("total variation is outside [0, 1]")
        return float(min(1.0, max(0.0, value)))


ScalarizerFactory = Callable[[TableArtifact, str], Indicator]


def resolve_indicators(
    declarations: Sequence[PairDeclaration | ProductDeclaration],
    artifacts: Mapping[str, ArtifactDeclaration],
    scalarizers: Mapping[str, ScalarizerFactory],
) -> tuple[Indicator, ...]:
    """Resolve builtin declarations to executable indicators."""

    resolved: list[Indicator] = []
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
            declaration.references,
            declaration.producing_stage,
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
        else:
            for scalarizer in declaration.scalarizers:
                for artifact in declaration.artifacts:
                    pair(scalarizer, artifact)
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
