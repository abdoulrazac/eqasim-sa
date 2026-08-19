from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeAlias

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
ResolvedConfig: TypeAlias = dict[str, JsonValue]


@dataclass(frozen=True)
class Artifact:
    name: str
    path: Path


@dataclass(frozen=True)
class IndicatorRequirement:
    indicator: str
    artifact: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (self.indicator, self.artifact)
        ):
            raise ValueError("indicator requirement names must be non-empty strings")


@dataclass(frozen=True)
class Manifest:
    artifacts: tuple[Artifact, ...]
    attempt_directory: Path
    scratch_bytes: int = 0


class SimulationError(RuntimeError):
    def __init__(self, message: str, log_path: Path, attempt_directory: Path) -> None:
        attempt = attempt_directory.resolve(strict=True)
        log = log_path.resolve(strict=True)
        if not attempt.is_dir():
            raise ValueError("simulation attempt must be a directory")
        if not log.is_file() or not log.is_relative_to(attempt):
            raise ValueError("simulation log escapes attempt directory")
        super().__init__(message)
        self._log_path = log
        self._attempt_directory = attempt

    @property
    def log_path(self) -> Path:
        return self._log_path

    @property
    def attempt_directory(self) -> Path:
        return self._attempt_directory


class Simulator(Protocol):
    def simulate(
        self, config: ResolvedConfig, seed: int, workdir: Path
    ) -> Manifest: ...

    def requirements(
        self, indicators: Sequence[IndicatorRequirement]
    ) -> Mapping[str, JsonValue]: ...
