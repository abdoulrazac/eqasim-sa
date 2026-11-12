"""Declare, resolve, identify, and execute campaign indicators."""

import hashlib
import inspect
import json
import math
import os
import re
import stat
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Protocol

import pyarrow as pa
import pyarrow.parquet as pq

from eqasim_sa.utils import platform
from eqasim_sa.core.software_identity import (
    SoftwareSourceState,
    stable_file_identity,
)


INDICATOR_DEFINITION_DOMAIN = b"eqasim-sa:indicator-definition:v1\0"
PLUGIN_DISTRIBUTION_DOMAIN = b"eqasim-sa:plugin-distribution:v1\0"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ENTRY_POINT_TARGET = re.compile(
    r"^(?P<module>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*):"
    r"(?P<attr>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)"
    r"(?:\s*\[[^]]+\])?$"
)
TOTAL_VARIATION_VERSION = 1
CUSTOM_INDICATOR_API_VERSION = 1


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


@dataclass(frozen=True)
class CustomDeclaration:
    """Custom indicator entry-point name and required artifact."""

    function: str
    artifact: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value for value in (self.function, self.artifact)
        ):
            raise ValueError("custom values must be non-empty strings")


class Indicator(Protocol):
    """Scalar analysis computed from a validated set of artifacts."""

    @property
    def name(self) -> str: ...

    def compute(self, artifacts: ArtifactSet) -> float: ...


@dataclass(frozen=True)
class PluginIdentity:
    """Immutable entry-point, distribution, implementation, and source identity."""

    implementation: str
    entry_point: str | None = None
    distribution: str | None = None
    distribution_version: str | None = None
    implementation_sha256: str | None = None
    distribution_sha256: str | None = None
    implementation_path: Path | None = None
    source_states: tuple[SoftwareSourceState, ...] = ()
    entry_point_group: str | None = None
    entry_point_name: str | None = None
    entry_point_value: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.implementation, str) or not self.implementation:
            raise ValueError("plugin implementation identity must not be empty")
        optional = (
            self.entry_point,
            self.distribution,
            self.distribution_version,
            self.entry_point_group,
            self.entry_point_name,
            self.entry_point_value,
        )
        if any(
            value is not None and (not isinstance(value, str) or not value)
            for value in optional
        ):
            raise ValueError("plugin identity values must be non-empty strings when present")
        if (self.distribution is None) != (self.distribution_version is None):
            raise ValueError("plugin distribution name and version must be provided together")
        if any(
            value is not None
            and (not isinstance(value, str) or SHA256.fullmatch(value) is None)
            for value in (self.implementation_sha256, self.distribution_sha256)
        ):
            raise ValueError("plugin hashes must be lowercase SHA-256 digests")
        if self.implementation_path is not None and not isinstance(
            self.implementation_path, Path
        ):
            raise ValueError("plugin implementation path must be a Path")
        if not isinstance(self.source_states, tuple) or any(
            not isinstance(value, SoftwareSourceState) for value in self.source_states
        ):
            raise ValueError("plugin source states are invalid")

    @property
    def immutable(self) -> bool:
        return (
            self.entry_point is not None
            and self.distribution is not None
            and self.implementation_sha256 is not None
            and self.distribution_sha256 is not None
            and self.implementation_path is not None
            and bool(self.source_states)
        )


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _entry_value(entry_point: object) -> tuple[str, str, str]:
    group = getattr(entry_point, "group", "eqasim_sa.indicators")
    name = getattr(entry_point, "name", None)
    value = getattr(entry_point, "value", None)
    if not all(isinstance(item, str) and item for item in (group, name, value)):
        raise ValueError("custom indicator entry-point metadata is incomplete")
    if "\0" in group or "\0" in name or "\0" in value:
        raise ValueError("custom indicator entry-point metadata contains NUL")
    return group, name, value


def _distribution_inventory(distribution: object) -> tuple[SoftwareSourceState, ...]:
    raw_files = getattr(distribution, "files", None)
    if raw_files is None:
        raise ValueError("plugin distribution has no immutable file inventory")
    states = []
    seen = set()
    for raw in raw_files:
        relative = Path(str(raw))
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or "\0" in relative.as_posix()
            or "__pycache__" in relative.parts
            or relative.suffix == ".pyc"
        ):
            continue
        relative_name = relative.as_posix()
        if relative_name in seen:
            raise ValueError("plugin distribution file inventory contains duplicates")
        seen.add(relative_name)
        path = Path(distribution.locate_file(raw))
        try:
            visible = path.lstat()
        except OSError as error:
            raise ValueError(
                f"plugin distribution file is unavailable: {relative_name}"
            ) from error
        if stat.S_ISDIR(visible.st_mode):
            continue
        if not stat.S_ISREG(visible.st_mode) or platform.is_link(path):
            raise ValueError(
                f"plugin distribution file must be regular and non-symlinked: {relative_name}"
            )
        size, sha256, state = stable_file_identity(path)
        states.append(
            SoftwareSourceState(relative_name, path.resolve(strict=True), state, size, sha256)
        )
    states.sort(key=lambda item: item.relative_path.encode("utf-8"))
    if not states:
        raise ValueError("plugin distribution has no stable regular files")
    return tuple(states)


def plugin_identity_from_entry_point(entry_point: object) -> PluginIdentity:
    """Seal an installed custom-indicator entry point and its distribution."""

    group, name, value = _entry_value(entry_point)
    target = ENTRY_POINT_TARGET.fullmatch(value)
    if target is None:
        raise ValueError("custom indicator entry point must use module:attribute syntax")
    implementation = f"{target['module']}:{target['attr']}"
    canonical_entry = f"{group}:{name}={value}"
    distribution = getattr(entry_point, "dist", None)
    distribution_name = getattr(distribution, "name", None)
    distribution_version = getattr(distribution, "version", None)
    if not all(
        isinstance(item, str) and item
        for item in (distribution_name, distribution_version)
    ):
        raise ValueError(
            "custom indicator entry point is not backed by an identified distribution"
        )
    states = _distribution_inventory(distribution)
    descriptor = {
        "files": [
            {
                "path": item.relative_path,
                "sha256": item.sha256,
                "size": item.size,
            }
            for item in states
        ],
        "name": distribution_name,
        "version": distribution_version,
    }
    distribution_sha256 = hashlib.sha256(
        PLUGIN_DISTRIBUTION_DOMAIN + _canonical(descriptor)
    ).hexdigest()
    module_path = Path(*target["module"].split("."))
    candidates = {
        module_path.with_suffix(".py").as_posix(),
        (module_path / "__init__.py").as_posix(),
    }
    matches = [item for item in states if item.relative_path in candidates]
    if len(matches) != 1:
        raise ValueError("plugin target module is not uniquely present in its distribution")
    implementation_state = matches[0]
    return PluginIdentity(
        implementation,
        canonical_entry,
        distribution_name,
        distribution_version,
        implementation_state.sha256,
        distribution_sha256,
        implementation_state.path,
        states,
        group,
        name,
        value,
    )


def verify_plugin_identity(entry_point: object, identity: PluginIdentity) -> None:
    """Fail if a custom-indicator entry point changed after sealing."""

    group, name, value = _entry_value(entry_point)
    distribution = getattr(entry_point, "dist", None)
    if (
        (group, name, value)
        != (
            identity.entry_point_group,
            identity.entry_point_name,
            identity.entry_point_value,
        )
        or getattr(distribution, "name", None) != identity.distribution
        or getattr(distribution, "version", None) != identity.distribution_version
    ):
        raise RuntimeError("custom indicator entry-point or distribution identity changed")
    if not identity.immutable:
        raise RuntimeError("custom indicator plugin identity is not immutable")
    current = plugin_identity_from_entry_point(entry_point)
    if (
        current.implementation != identity.implementation
        or current.implementation_sha256 != identity.implementation_sha256
        or current.distribution_sha256 != identity.distribution_sha256
        or current.implementation_path != identity.implementation_path
        or current.source_states != identity.source_states
    ):
        raise RuntimeError("custom indicator implementation or distribution changed")


class LazyPluginImplementation:
    """Verify and load a custom indicator only when it is first evaluated."""

    def __init__(self, entry_point: object, identity: PluginIdentity) -> None:
        self.entry_point = entry_point
        self.identity = identity
        self._implementation: object | None = None
        self._lock = threading.Lock()

    def _load(self) -> object:
        with self._lock:
            if self._implementation is not None:
                return self._implementation
            verify_plugin_identity(self.entry_point, self.identity)
            loaded = self.entry_point.load()  # type: ignore[attr-defined]
            module = getattr(loaded, "__module__", None)
            implementation = loaded
            if isinstance(loaded, type):
                try:
                    implementation = loaded()
                except TypeError as error:
                    raise TypeError(
                        f"custom indicator {self.identity.entry_point_name!r} class "
                        "must be instantiable without arguments"
                    ) from error
            compute = getattr(implementation, "compute", implementation)
            if not callable(compute):
                raise TypeError("custom indicator must provide compute(artifacts)")
            module_file = getattr(sys.modules.get(str(module)), "__file__", None)
            if self.identity.implementation_path is not None and (
                module_file is None
                or Path(module_file).resolve(strict=True)
                != self.identity.implementation_path
            ):
                raise RuntimeError("loaded custom indicator module path changed")
            verify_plugin_identity(self.entry_point, self.identity)
            self._implementation = implementation
            return implementation

    def compute(self, artifacts: ArtifactSet) -> float:
        implementation = self._load()
        compute = getattr(implementation, "compute", implementation)
        return compute(artifacts)


@dataclass(frozen=True)
class RegisteredIndicatorFactory:
    """Bind an immutable plugin implementation to an artifact declaration."""

    implementation: object
    plugin_identity: PluginIdentity

    def __call__(self, artifact: "TableArtifact") -> object:
        return LazyPluginImplementation(self.implementation, self.plugin_identity)


@dataclass(frozen=True)
class RegisteredIndicator:
    """Validated custom indicator with optional immutable plugin provenance."""

    function: str
    artifact: str
    implementation: object
    plugin_identity: PluginIdentity | None = None
    artifact_declaration: TableArtifact | None = None

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


def _implementation_identity(value: object) -> str | None:
    implementation = getattr(value, "compute", value)
    module = getattr(implementation, "__module__", None)
    qualname = getattr(implementation, "__qualname__", None)
    if qualname is None:
        kind = type(value)
        module = getattr(kind, "__module__", None)
        qualname = getattr(kind, "__qualname__", None)
    if isinstance(module, str) and module and isinstance(qualname, str) and qualname:
        return f"{module}:{qualname}"
    return None


def _implementation_hash(value: object) -> tuple[str | None, str | None]:
    identity = _implementation_identity(value)
    implementation = getattr(value, "compute", value)
    try:
        source = inspect.getsourcefile(implementation) or inspect.getsourcefile(type(value))
        if source is None:
            return identity, None
        _, sha256, _ = stable_file_identity(Path(source).resolve(strict=True))
        return identity, sha256
    except (OSError, TypeError, ValueError):
        return identity, None


def _artifact_identity(artifact: TableArtifact | None, name: str) -> dict[str, object]:
    return {
        "name": name,
        "classColumn": artifact.class_column if artifact is not None else None,
        "syntheticColumn": artifact.synthetic_column if artifact is not None else None,
        "referenceColumns": list(artifact.reference_columns) if artifact is not None else [],
        "producingStage": artifact.producing_stage if artifact is not None else None,
    }


def analysis_identity(indicator: Indicator) -> dict[str, object]:
    """Build and validate the canonical identity of an indicator implementation."""

    if isinstance(indicator, TotalVariationIndicator):
        _, implementation_sha256 = _implementation_hash(TotalVariationIndicator)
        definition = {
            "schemaVersion": 1,
            "name": indicator.name,
            "kind": "builtin",
            "scalarizer": {
                "name": indicator.identity.scalarizer,
                "version": TOTAL_VARIATION_VERSION,
            },
            "artifact": {
                **_artifact_identity(indicator.artifact, indicator.artifact.name),
                "referenceColumns": [indicator.reference_column],
            },
            "implementation": {
                "identity": "eqasim_sa.core.indicators:TotalVariationIndicator",
                "sha256": implementation_sha256,
                "version": TOTAL_VARIATION_VERSION,
            },
            "plugin": None,
        }
    elif isinstance(indicator, RegisteredIndicator):
        plugin = indicator.plugin_identity
        implementation, implementation_sha256 = _implementation_hash(
            indicator.implementation
        )
        if plugin is not None:
            implementation = plugin.implementation
            implementation_sha256 = plugin.implementation_sha256
        definition = {
            "schemaVersion": 1,
            "name": indicator.name,
            "kind": "custom",
            "scalarizer": {
                "name": indicator.function,
                "version": CUSTOM_INDICATOR_API_VERSION,
            },
            "artifact": _artifact_identity(
                indicator.artifact_declaration, indicator.artifact
            ),
            "implementation": {
                "identity": implementation,
                "sha256": implementation_sha256,
                "version": CUSTOM_INDICATOR_API_VERSION,
            },
            "plugin": (
                {
                    "entryPoint": plugin.entry_point,
                    "distribution": {
                        "name": plugin.distribution,
                        "version": plugin.distribution_version,
                        "sha256": plugin.distribution_sha256,
                    },
                }
                if plugin is not None
                else None
            ),
        }
    else:
        implementation, implementation_sha256 = _implementation_hash(indicator)
        definition = {
            "schemaVersion": 1,
            "name": indicator.name,
            "kind": "python",
            "scalarizer": {"name": indicator.name, "version": 1},
            "artifact": None,
            "implementation": {
                "identity": implementation,
                "sha256": implementation_sha256,
                "version": 1,
            },
            "plugin": None,
        }
    validate_indicator_definition(definition)
    return definition


def verify_indicator_runtime_identity(indicator: Indicator) -> None:
    """Recheck immutable plugin provenance immediately before computation."""

    if not isinstance(indicator, RegisteredIndicator):
        return
    plugin = indicator.plugin_identity
    implementation = indicator.implementation
    if plugin is None:
        return
    if isinstance(implementation, LazyPluginImplementation):
        verify_plugin_identity(implementation.entry_point, plugin)
        return
    if plugin.immutable:
        raise ValueError(
            "immutable plugin provenance requires the verified lazy entry-point wrapper"
        )


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
        "plugin",
    }:
        raise ValueError("indicator definition has an invalid schema")
    if value["schemaVersion"] != 1 or type(value["schemaVersion"]) is not int:
        raise ValueError("indicator definition schemaVersion must equal 1")
    if not isinstance(value["name"], str) or not value["name"]:
        raise ValueError("indicator definition name must be non-empty")
    if value["kind"] not in {"builtin", "custom", "python"}:
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
    plugin = value["plugin"]
    if plugin is not None:
        if (
            not isinstance(plugin, dict)
            or set(plugin) != {"entryPoint", "distribution"}
            or plugin["entryPoint"] is not None
            and (not isinstance(plugin["entryPoint"], str) or not plugin["entryPoint"])
            or not isinstance(plugin["distribution"], dict)
            or set(plugin["distribution"]) != {"name", "version", "sha256"}
        ):
            raise ValueError("indicator definition plugin schema is invalid")
        distribution = plugin["distribution"]
        for key in ("name", "version"):
            if distribution[key] is not None and (
                not isinstance(distribution[key], str) or not distribution[key]
            ):
                raise ValueError("indicator definition distribution identity is invalid")
        if distribution["sha256"] is not None and (
            not isinstance(distribution["sha256"], str)
            or SHA256.fullmatch(distribution["sha256"]) is None
        ):
            raise ValueError("indicator definition distribution hash is invalid")
        if require_scientific and (
            plugin["entryPoint"] is None
            or distribution["name"] is None
            or distribution["version"] is None
            or distribution["sha256"] is None
        ):
            raise ValueError("scientific custom indicator plugin must be immutable")
    # Direct core indicators are not entry-point plugins. Their implementation
    # file hash still provides an immutable scientific definition.
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
CustomFactory = Callable[[TableArtifact], object]


def resolve_indicators(
    declarations: Sequence[PairDeclaration | ProductDeclaration | CustomDeclaration],
    artifacts: Mapping[str, ArtifactDeclaration],
    scalarizers: Mapping[str, ScalarizerFactory],
    custom: Mapping[str, CustomFactory],
) -> tuple[object, ...]:
    """Resolve builtin and custom declarations to executable indicators."""

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
                implementation = factory(source)
                resolved.append(
                    RegisteredIndicator(
                        declaration.function,
                        declaration.artifact,
                        implementation,
                        getattr(factory, "plugin_identity", None),
                        source,
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
