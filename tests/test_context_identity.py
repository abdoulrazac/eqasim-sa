import hashlib

import pytest

from eqasim_sa.core.context_identity import (
    CONTEXT_DOMAIN,
    LEGACY_CONTEXT_DOMAIN_V1,
    ExecutionContextIdentity,
)


def descriptor() -> dict[str, str]:
    return {
        "eqasimCommit": "a" * 40,
        "dataManifestSha256": "b" * 64,
        "lockfileSha256": "c" * 64,
        "eqasimSaVersion": "0.3.0",
    }


def test_execution_context_identity_round_trips_canonical_bytes() -> None:
    current = ExecutionContextIdentity.from_descriptor(descriptor())

    assert ExecutionContextIdentity.from_canonical_bytes(current.canonical_bytes) == current
    current.require_sha256(current.sha256)


def test_execution_context_identity_rejects_contract_drift() -> None:
    damaged = descriptor()
    damaged["extra"] = "unexpected"

    with pytest.raises(ValueError, match="exactly four string keys"):
        ExecutionContextIdentity.from_descriptor(damaged)


def test_execution_context_identity_rejects_noncanonical_bytes_and_hash() -> None:
    current = ExecutionContextIdentity.from_descriptor(descriptor())

    with pytest.raises(ValueError, match="not canonical"):
        ExecutionContextIdentity.from_canonical_bytes(
            b'{"eqasimCommit":"' + b"a" * 40 + b'", "dataManifestSha256":"' + b"b" * 64
            + b'","lockfileSha256":"' + b"c" * 64 + b'","eqasimSaVersion":"0.3.0"}'
        )
    with pytest.raises(ValueError, match="does not match"):
        current.require_sha256("0" * 64)


def test_context_identity_uses_v2_domain_and_v1_only_for_legacy() -> None:
    current = ExecutionContextIdentity.from_descriptor(descriptor())
    legacy = ExecutionContextIdentity.from_descriptor(
        descriptor(), LEGACY_CONTEXT_DOMAIN_V1
    )

    assert CONTEXT_DOMAIN == b"eqasim-sa:context:v2\0"
    assert current.sha256 == hashlib.sha256(
        CONTEXT_DOMAIN + current.canonical_bytes
    ).hexdigest()
    assert legacy.sha256 == hashlib.sha256(
        b"eqasim-sa:context:v1\0" + current.canonical_bytes
    ).hexdigest()
    assert legacy.canonical_bytes == current.canonical_bytes
    assert legacy.sha256 != current.sha256
