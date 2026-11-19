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
import pyarrow.csv as csv

from eqasim_sa.core.software_identity import stable_published_identity


INDICATOR_DEFINITION_DOMAIN = b"eqasim-sa:indicator-definition:v3\0"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TOTAL_VARIATION_VERSION = 1
SHARE_VERSION = 1
VALUE_VERSION = 1
KIND_VERSIONS = {
    "tv_distance": TOTAL_VARIATION_VERSION,
    "share": SHARE_VERSION,
    "value": VALUE_VERSION,
}
OUTPUT_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
PREFIX_TEMPLATE = "{prefix}"
CSV_BLOCK_SIZE = 1 << 20


def validate_output_name(name: object) -> str:
    """Validate one declared output name (letters, digits, ``_`` and ``-``)."""

    if not isinstance(name, str) or OUTPUT_NAME.fullmatch(name) is None:
        raise ValueError(
            f"output name {name!r} must contain only letters, digits, '_' and '-'"
        )
    return name


def validate_output_file(file: object) -> str:
    """Validate a relative POSIX output file whose only template is ``{prefix}``."""

    if not isinstance(file, str) or not file:
        raise ValueError("file must be a non-empty string")
    plain = file.replace(PREFIX_TEMPLATE, "")
    if "\\" in file or ":" in file or "\0" in file:
        raise ValueError(f"file {file!r} must be a relative path using '/'")
    if file.startswith("/"):
        raise ValueError(f"file {file!r} must be relative to the eqasim output_path")
    if "{" in plain or "}" in plain:
        raise ValueError(f"file {file!r} may only use the {PREFIX_TEMPLATE} template")
    if any(part in {"", ".", ".."} for part in file.split("/")):
        raise ValueError(f"file {file!r} must not contain empty, '.' or '..' segments")
    return file


def validate_separator(separator: object) -> str:
    """Validate a one-character CSV separator."""

    if (
        not isinstance(separator, str)
        or len(separator) != 1
        or separator in {'"', "\n", "\r"}
    ):
        raise ValueError("separator must be one character other than a quote or newline")
    return separator


@dataclass(frozen=True)
class OutputDeclaration:
    """A declared eqasim output file and the columns indicators may read in it.

    Only ``file`` and ``separator`` determine archived content and are sealed;
    ``category``, ``simulated`` and ``references`` describe its analysis.
    """

    file: str
    separator: str
    category: str | None = None
    simulated: str | None = None
    references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_output_file(self.file)
        validate_separator(self.separator)
        for label, value in (("category", self.category), ("simulated", self.simulated)):
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{label} must be a non-empty string")
        if (
            not isinstance(self.references, tuple)
            or any(not isinstance(item, str) or not item for item in self.references)
            or len(set(self.references)) != len(self.references)
        ):
            raise ValueError("references must be unique non-empty strings")
        named = [item for item in (self.category, self.simulated) if item is not None]
        if len(set(named)) != len(named) or set(self.references) & set(named):
            raise ValueError("category, simulated and references must be distinct")

    def source_path(self, prefix: str) -> str:
        """Return the file path relative to eqasim's ``output_path``."""

        return self.file.replace(PREFIX_TEMPLATE, prefix)

    def sealed(self) -> dict[str, str]:
        """Return the sealed identity fields (what determines archived bytes)."""

        return {"file": self.file, "separator": self.separator}


def validate_outputs_declaration(payload: object) -> tuple[dict[str, str], ...]:
    """Validate the canonical ``outputsDeclaration`` sealed in a plan."""

    try:
        if not isinstance(payload, bytes):
            raise ValueError
        entries = json.loads(payload)
        if not isinstance(entries, list) or _canonical(entries) != payload:
            raise ValueError
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {
                "name",
                "file",
                "separator",
            }:
                raise ValueError
            validate_output_name(entry["name"])
            validate_output_file(entry["file"])
            validate_separator(entry["separator"])
        names = [entry["name"].encode("utf-8") for entry in entries]
        if names != sorted(set(names)):
            raise ValueError
    except (TypeError, UnicodeError, ValueError) as error:
        raise ValueError("plan outputsDeclaration metadata is invalid") from error
    return tuple(entries)


@dataclass(frozen=True)
class ArtifactSet:
    """Validated read-only mapping from output names to archived files."""

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
    """A declared CSV output and the columns a distribution indicator reads."""

    name: str
    file: str
    separator: str
    class_column: str
    synthetic_column: str
    reference_columns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_output_name(self.name)
        validate_output_file(self.file)
        validate_separator(self.separator)
        names = (self.class_column, self.synthetic_column)
        if not all(isinstance(name, str) and name for name in names):
            raise ValueError("artifact column names must not be empty")
        if self.class_column == self.synthetic_column:
            raise ValueError("class and synthetic columns must be distinct")
        if (
            not isinstance(self.reference_columns, tuple)
            or any(not isinstance(value, str) or not value for value in self.reference_columns)
            or len(set(self.reference_columns)) != len(self.reference_columns)
        ):
            raise ValueError("artifact reference columns must be unique non-empty strings")
        if set(self.reference_columns) & {self.class_column, self.synthetic_column}:
            raise ValueError("artifact reference columns must be distinct")

    @property
    def carries_reference(self) -> bool:
        return bool(self.reference_columns)

    def read(
        self, artifacts: "ArtifactSet", columns: Sequence[str] | None
    ) -> pa.Table:
        """Read this output's archived CSV with every column as text."""

        return read_csv_table(
            artifacts.path(self.name), self.separator, self.name, columns
        )


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


def _output_block(name: str, file: str, separator: str) -> dict[str, object]:
    return {"name": name, "file": file, "separator": separator}


def analysis_identity(indicator: Indicator) -> dict[str, object]:
    """Build and validate the canonical identity of a builtin indicator."""

    if isinstance(indicator, TotalVariationIndicator):
        kind, implementation = "tv_distance", TotalVariationIndicator
        source = indicator.artifact
        artifact = _output_block(source.name, source.file, source.separator) | {
            "classColumn": source.class_column,
            "syntheticColumn": source.synthetic_column,
            "referenceColumns": [indicator.reference_column],
        }
    elif isinstance(indicator, ShareIndicator):
        kind, implementation = "share", ShareIndicator
        source = indicator.artifact
        artifact = _output_block(source.name, source.file, source.separator) | {
            "classColumn": source.class_column,
            "syntheticColumn": source.synthetic_column,
            "category": indicator.category,
        }
    elif isinstance(indicator, ValueIndicator):
        kind, implementation = "value", ValueIndicator
        artifact = _output_block(
            indicator.output, indicator.file, indicator.separator
        ) | {"column": indicator.column, "where": dict(indicator.where)}
    else:
        raise TypeError(f"unsupported indicator: {type(indicator).__qualname__}")
    version = KIND_VERSIONS[kind]
    definition = {
        "schemaVersion": 1,
        "name": indicator.name,
        "kind": "builtin",
        "scalarizer": {"name": kind, "version": version},
        "artifact": artifact,
        "implementation": {
            "identity": f"eqasim_sa.core.indicators:{implementation.__name__}",
            "sha256": _source_sha256(implementation),
            "version": version,
        },
    }
    validate_indicator_definition(definition)
    return definition


_DEFINITION_KEYS = {
    "schemaVersion",
    "name",
    "kind",
    "scalarizer",
    "artifact",
    "implementation",
}
_OUTPUT_KEYS = {"name", "file", "separator"}
_ARTIFACT_KEYS = {
    "tv_distance": _OUTPUT_KEYS | {"classColumn", "syntheticColumn", "referenceColumns"},
    "share": _OUTPUT_KEYS | {"classColumn", "syntheticColumn", "category"},
    "value": _OUTPUT_KEYS | {"column", "where"},
}


def _non_empty_strings(*values: object) -> bool:
    return all(isinstance(item, str) and item for item in values)


def _validate_definition_artifact(kind: str, artifact: object) -> None:
    if not isinstance(artifact, dict) or set(artifact) != _ARTIFACT_KEYS[kind]:
        raise ValueError("indicator definition artifact schema is invalid")
    try:
        validate_output_name(artifact["name"])
        validate_output_file(artifact["file"])
        validate_separator(artifact["separator"])
    except ValueError as error:
        raise ValueError(f"indicator definition artifact is invalid: {error}") from error
    if kind == "value":
        where = artifact["where"]
        if (
            not _non_empty_strings(artifact["column"])
            or not isinstance(where, dict)
            or any(
                not _non_empty_strings(key) or not isinstance(item, str)
                for key, item in where.items()
            )
            or artifact["column"] in where
        ):
            raise ValueError("indicator definition artifact columns are invalid")
        return
    columns = (artifact["classColumn"], artifact["syntheticColumn"])
    if not _non_empty_strings(*columns) or len(set(columns)) != 2:
        raise ValueError("indicator definition artifact columns are invalid")
    if kind == "share":
        if not _non_empty_strings(artifact["category"]):
            raise ValueError("indicator definition share category is invalid")
        return
    references = artifact["referenceColumns"]
    if (
        not isinstance(references, list)
        or len(references) != 1
        or not _non_empty_strings(*references)
        or len({*columns, *references}) != 3
    ):
        raise ValueError("indicator definition reference columns are invalid")


def validate_indicator_definition(
    value: object, *, require_scientific: bool = False
) -> dict[str, object]:
    """Validate a canonical indicator definition and scientific criteria."""

    if not isinstance(value, dict) or set(value) != _DEFINITION_KEYS:
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
        or scalarizer["name"] not in KIND_VERSIONS
        or type(scalarizer["version"]) is not int
        or scalarizer["version"] != KIND_VERSIONS[scalarizer["name"]]
    ):
        raise ValueError("indicator definition scalarizer is invalid")
    _validate_definition_artifact(scalarizer["name"], value["artifact"])
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
        table = self.artifact.read(artifacts, columns)
        _class_labels(table, columns[0])
        synthetic = _normalized_masses(numeric_column(table, columns[1]), columns[1])
        reference = _normalized_masses(numeric_column(table, columns[2]), columns[2])
        value = 0.5 * math.fsum(
            abs(left - right) for left, right in zip(reference, synthetic, strict=True)
        )
        if not math.isfinite(value) or value < -1e-12 or value > 1.0 + 1e-12:
            raise ValueError("total variation is outside [0, 1]")
        return float(min(1.0, max(0.0, value)))


def _class_labels(table: pa.Table, column: str) -> list[str]:
    labels = table[column].to_pylist()
    if any(label == "" for label in labels):
        raise ValueError(f"class column {column!r} contains empty labels")
    if len(labels) != len(set(labels)):
        raise ValueError(f"class column {column!r} contains duplicate class labels")
    return labels


@dataclass(frozen=True)
class ShareIndicator:
    """Proportion of one class in a distribution output's simulated column."""

    artifact: TableArtifact
    category: str

    def __post_init__(self) -> None:
        if not isinstance(self.category, str) or not self.category:
            raise ValueError("share category must be a non-empty string")

    @property
    def name(self) -> str:
        return f"share({self.artifact.name}, {self.category})"

    def compute(self, artifacts: ArtifactSet) -> float:
        columns = (self.artifact.class_column, self.artifact.synthetic_column)
        table = self.artifact.read(artifacts, columns)
        labels = _class_labels(table, columns[0])
        if self.category not in labels:
            raise ValueError(
                f"share: category {self.category!r} not found in output "
                f"{self.artifact.name!r}"
            )
        masses = _normalized_masses(numeric_column(table, columns[1]), columns[1])
        return float(masses[labels.index(self.category)])


@dataclass(frozen=True)
class ValueIndicator:
    """One number read from a table cell, its row selected by ``where``."""

    output: str
    file: str
    separator: str
    column: str
    where: Mapping[str, str] | tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        validate_output_name(self.output)
        validate_output_file(self.file)
        validate_separator(self.separator)
        if not isinstance(self.column, str) or not self.column:
            raise ValueError("value column must be a non-empty string")
        pairs = tuple(
            self.where.items() if isinstance(self.where, Mapping) else self.where
        )
        for pair in pairs:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError("where must map column names to values")
            if not isinstance(pair[0], str) or not pair[0]:
                raise ValueError("where keys must be non-empty strings")
            if not isinstance(pair[1], str):
                raise ValueError("where values must be quoted strings")
        keys = [key for key, _ in pairs]
        if len(set(keys)) != len(keys):
            raise ValueError("where keys must be unique")
        if self.column in keys:
            raise ValueError("value column must not be a where key")
        object.__setattr__(self, "where", tuple(sorted(pairs)))

    @property
    def name(self) -> str:
        selection = "".join(f", {key}={item}" for key, item in self.where)
        return f"value({self.output}, {self.column}{selection})"

    def compute(self, artifacts: ArtifactSet) -> float:
        keys = [key for key, _ in self.where]
        table = read_csv_table(
            artifacts.path(self.output),
            self.separator,
            self.output,
            [self.column, *keys],
        )
        cells = table[self.column].to_pylist()
        selectors = [table[key].to_pylist() for key in keys]
        rows = [
            index
            for index in range(len(cells))
            if all(
                selector[index] == item
                for selector, (_, item) in zip(selectors, self.where, strict=True)
            )
        ]
        if not rows:
            raise ValueError(
                f"value: no row of output {self.output!r} matches where "
                f"{dict(self.where)}"
            )
        if len(rows) > 1:
            if not self.where:
                raise ValueError(
                    f"value: output {self.output!r} has several rows; "
                    "add where to select one"
                )
            raise ValueError(
                f"value: several rows of output {self.output!r} match; "
                "add where to select one"
            )
        try:
            number = float(cells[rows[0]])
        except (TypeError, ValueError):
            number = math.nan
        if not math.isfinite(number):
            raise ValueError(
                f"value: {self.column!r} in output {self.output!r} "
                "is not a finite number"
            )
        return number


ScalarizerFactory = Callable[[TableArtifact, str], Indicator]


def resolve_indicators(
    declarations: Sequence[PairDeclaration | ProductDeclaration],
    outputs: Mapping[str, OutputDeclaration],
    scalarizers: Mapping[str, ScalarizerFactory],
) -> tuple[Indicator, ...]:
    """Resolve builtin declarations to executable indicators."""

    resolved: list[Indicator] = []
    errors: list[str] = []

    def table(name: str) -> TableArtifact | None:
        declaration = outputs.get(name)
        if (
            declaration is None
            or declaration.category is None
            or declaration.simulated is None
        ):
            return None
        return TableArtifact(
            name,
            declaration.file,
            declaration.separator,
            declaration.category,
            declaration.simulated,
            declaration.references,
        )

    def pair(scalarizer: str, artifact: str, reference: str | None = None) -> None:
        factory, source = scalarizers.get(scalarizer), table(artifact)
        if source is None:
            errors.append(f"{scalarizer}({artifact})")
            return
        references = (
            (reference,) if reference is not None else outputs[artifact].references
        )
        if not references:
            errors.append(f"{scalarizer}({artifact})")
            return
        for selected in references:
            if factory is None:
                errors.append(f"{scalarizer}({artifact}, ref={selected})")
                continue
            if selected not in outputs[artifact].references:
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


def numeric_column(table: pa.Table, name: str) -> tuple[float, ...]:
    """Convert one text column to finite floats, or fail naming the column."""

    values = []
    for text in table[name].to_pylist():
        try:
            value = float(text)
        except (TypeError, ValueError) as error:
            raise ValueError(f"column {name!r} must be numeric: {text!r}") from error
        if not math.isfinite(value):
            raise ValueError(f"column {name!r} must contain finite values")
        values.append(value)
    return tuple(values)


def _normalized_masses(values: tuple[float, ...], name: str) -> tuple[float, ...]:
    if any(value < 0 for value in values):
        raise ValueError(f"column {name!r} must contain non-negative values")
    scale = max(values, default=0.0)
    if scale <= 0:
        raise ValueError(f"column {name!r} must have a positive sum")
    scaled = tuple(value / scale for value in values)
    total = math.fsum(scaled)
    return tuple(value / total for value in scaled)


def read_csv_table(
    path: Path,
    separator: str,
    output: str,
    columns: Sequence[str] | None = None,
) -> pa.Table:
    """Read an archived CSV output with every column as text.

    The header is read first; a first column with an empty header (a pandas
    index) is dropped; any other empty or duplicate header is an error.
    ``columns`` selects and orders the returned columns.
    """

    validate_separator(separator)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"artifact {output!r} must be a regular file")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            parse = csv.ParseOptions(delimiter=separator)
            read = csv.ReadOptions(block_size=CSV_BLOCK_SIZE)
            # The header is read on its own, so every column can then be
            # declared as text before any value is converted.
            with csv.open_csv(
                source, read_options=read, parse_options=parse
            ) as reader:
                header = list(reader.schema.names)
            if len(header) != len(set(header)):
                raise ValueError(f"artifact {output!r} has duplicate column names")
            kept = header[1:] if header and header[0] == "" else header
            if "" in kept:
                raise ValueError(f"artifact {output!r} has an unnamed column")
            selected = list(kept if columns is None else columns)
            missing = [name for name in selected if name not in kept]
            if missing:
                raise ValueError(
                    f"artifact {output!r} is missing declared column {missing[0]!r}"
                )
            source.seek(0)
            return csv.read_csv(
                source,
                read_options=read,
                parse_options=parse,
                convert_options=csv.ConvertOptions(
                    column_types={name: pa.string() for name in header},
                    include_columns=selected,
                    strings_can_be_null=False,
                    quoted_strings_can_be_null=False,
                ),
            )
    except (OSError, pa.ArrowInvalid, pa.ArrowIOError) as error:
        raise ValueError(
            f"artifact {output!r} cannot be read as CSV: {error}"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
