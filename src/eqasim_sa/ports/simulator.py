import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeAlias

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
ResolvedConfig: TypeAlias = dict[str, JsonValue]
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
CHILD_ENVIRONMENT_DOMAIN = b"eqasim-sa:child-environment:v1\0"
EXECUTION_EVIDENCE_V1_DOMAIN = b"eqasim-sa:execution-evidence:v1\0"
EXECUTION_EVIDENCE_V2_DOMAIN = b"eqasim-sa:execution-evidence:v2\0"
# Retained as the current writer domain for callers that import the public constant.
EXECUTION_EVIDENCE_DOMAIN = EXECUTION_EVIDENCE_V2_DOMAIN


@dataclass(frozen=True)
class ExecutionEvidence:
    context_hash: str
    data_tree_sha256: str
    environment_sha256: str
    environment_variables: tuple[str, ...]
    executable_sha256: str
    invocation: tuple[str, ...]
    model_commit: str
    model_config_sha256: str
    schema_version: int = 2
    eqasim_sa_sha256: str | None = None

    def __post_init__(self) -> None:
        digests = (
            self.context_hash,
            self.data_tree_sha256,
            self.environment_sha256,
            self.executable_sha256,
            self.model_config_sha256,
        )
        if any(
            type(value) is not str or SHA256.fullmatch(value) is None
            for value in digests
        ):
            raise ValueError("execution evidence contains an invalid SHA-256 digest")
        if (
            type(self.model_commit) is not str
            or GIT_COMMIT.fullmatch(self.model_commit) is None
        ):
            raise ValueError("execution evidence contains an invalid model commit")
        if type(self.schema_version) is not int or self.schema_version not in {1, 2}:
            raise ValueError("unsupported execution evidence schema version")
        if self.schema_version == 1:
            if self.eqasim_sa_sha256 is not None:
                raise ValueError("legacy execution evidence must not bind eqasim_sa")
        elif (
            not isinstance(self.eqasim_sa_sha256, str)
            or SHA256.fullmatch(self.eqasim_sa_sha256) is None
        ):
            raise ValueError("execution evidence v2 requires eqasimSaSha256")
        if (
            type(self.invocation) is not tuple
            or not self.invocation
            or not all(
                type(item) is str and item and "\0" not in item
                for item in self.invocation
            )
            or not os.path.isabs(self.invocation[0])
        ):
            raise ValueError("execution evidence invocation must use an absolute executable")
        if (
            type(self.environment_variables) is not tuple
            or not all(
                type(name) is str and name and "=" not in name and "\0" not in name
                for name in self.environment_variables
            )
            or tuple(sorted(set(self.environment_variables))) != self.environment_variables
        ):
            raise ValueError("execution evidence environment names are invalid")

    def _descriptor(self) -> dict[str, object]:
        descriptor = {
            "contextHash": self.context_hash,
            "dataTreeSha256": self.data_tree_sha256,
            "environmentSha256": self.environment_sha256,
            "environmentVariables": list(self.environment_variables),
            "executableSha256": self.executable_sha256,
            "invocation": list(self.invocation),
            "modelCommit": self.model_commit,
            "modelConfigSha256": self.model_config_sha256,
            "schemaVersion": self.schema_version,
        }
        if self.schema_version == 2:
            descriptor["eqasimSaSha256"] = self.eqasim_sa_sha256
        return descriptor

    def canonical_bytes(self) -> bytes:
        descriptor = json.dumps(
            self._descriptor(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        domain = (
            EXECUTION_EVIDENCE_V1_DOMAIN
            if self.schema_version == 1
            else EXECUTION_EVIDENCE_V2_DOMAIN
        )
        evidence_sha256 = hashlib.sha256(domain + descriptor).hexdigest()
        return json.dumps(
            {**self._descriptor(), "evidenceSha256": evidence_sha256},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")

    @classmethod
    def from_bytes(cls, payload: bytes) -> "ExecutionEvidence":
        try:
            document = json.loads(payload)
            if not isinstance(document, dict) or type(document.get("schemaVersion")) is not int:
                raise ValueError
            schema_version = document["schemaVersion"]
            expected = {
                "contextHash",
                "dataTreeSha256",
                "evidenceSha256",
                "environmentSha256",
                "environmentVariables",
                "executableSha256",
                "invocation",
                "modelCommit",
                "modelConfigSha256",
                "schemaVersion",
            }
            if schema_version == 2:
                expected.add("eqasimSaSha256")
            if schema_version not in {1, 2} or set(document) != expected:
                raise ValueError
            if not isinstance(document["environmentVariables"], list) or not isinstance(
                document["invocation"], list
            ):
                raise ValueError
            evidence = cls(
                context_hash=document["contextHash"],
                data_tree_sha256=document["dataTreeSha256"],
                environment_sha256=document["environmentSha256"],
                environment_variables=tuple(document["environmentVariables"]),
                executable_sha256=document["executableSha256"],
                invocation=tuple(document["invocation"]),
                model_commit=document["modelCommit"],
                model_config_sha256=document["modelConfigSha256"],
                schema_version=schema_version,
                eqasim_sa_sha256=document.get("eqasimSaSha256"),
            )
            if payload != evidence.canonical_bytes():
                raise ValueError
            return evidence
        except (TypeError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise ValueError("execution evidence is invalid or noncanonical") from error


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
    execution_evidence: bytes | None = None


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
