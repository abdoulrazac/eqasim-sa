"""Canonical provenance identity shared by planning, execution, and analysis."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


CONTEXT_DOMAIN = b"eqasim-sa:context:v2\0"
# Plans sealed before line-ending-independent identities; legacy readers only.
LEGACY_CONTEXT_DOMAIN_V1 = b"eqasim-sa:context:v1\0"
CONTEXT_KEYS = frozenset(
    {
        "eqasimCommit",
        "dataManifestSha256",
        "lockfileSha256",
        "eqasimSaVersion",
    }
)


@dataclass(frozen=True)
class ExecutionContextIdentity:
    """Validated, canonical execution-context provenance."""

    descriptor: Mapping[str, str]
    canonical_bytes: bytes
    sha256: str

    @classmethod
    def from_descriptor(
        cls, value: object, domain: bytes = CONTEXT_DOMAIN
    ) -> "ExecutionContextIdentity":
        if (
            not isinstance(value, dict)
            or set(value) != CONTEXT_KEYS
            or any(type(item) is not str for item in value.values())
        ):
            raise ValueError(
                "execution context descriptor must contain exactly four string keys"
            )
        descriptor = dict(value)
        canonical = json.dumps(
            descriptor,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        return cls(
            MappingProxyType(descriptor),
            canonical,
            hashlib.sha256(domain + canonical).hexdigest(),
        )

    @classmethod
    def from_canonical_bytes(cls, payload: bytes) -> "ExecutionContextIdentity":
        try:
            value = json.loads(payload)
        except (TypeError, ValueError, UnicodeDecodeError) as error:
            raise ValueError("execution context descriptor is not valid JSON") from error
        identity = cls.from_descriptor(value)
        if payload != identity.canonical_bytes:
            raise ValueError("execution context descriptor is not canonical")
        return identity

    def require_sha256(self, expected: object) -> None:
        if expected != self.sha256:
            raise ValueError("execution context descriptor does not match its identity")

    def as_dict(self) -> dict[str, str]:
        return dict(self.descriptor)
