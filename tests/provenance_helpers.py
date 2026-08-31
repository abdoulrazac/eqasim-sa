import hashlib
import json

import pyarrow.parquet as pq

from eqasim_sa.core.indicators import (
    RegisteredIndicator,
    TableArtifact,
    analysis_identity,
)
from eqasim_sa.core.software_identity import read_software_identity
from eqasim_sa.ports.simulator import ExecutionEvidence


_SOFTWARE = read_software_identity()
TEST_EQASIM_SA_VERSION = _SOFTWARE.version
TEST_EQASIM_SA_SHA256 = _SOFTWARE.sha256


def scientific_context_descriptor() -> dict[str, str]:
    return {
        "eqasimCommit": "a" * 40,
        "dataManifestSha256": "b" * 64,
        "lockfileSha256": "c" * 64,
        "eqasimSaVersion": TEST_EQASIM_SA_VERSION,
    }


def context_hash(descriptor: dict[str, str]) -> str:
    encoded = json.dumps(
        descriptor, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(b"eqasim-sa:context:v1\0" + encoded).hexdigest()


def _test_indicator_compute(artifacts) -> float:  # type: ignore[no-untyped-def]
    path = next(iter(artifacts.paths.values()))
    return float(pq.read_table(path, columns=["synthetic"])["synthetic"][0].as_py())


def scientific_registered_indicator(
    name: str,
    artifact: str,
    implementation=_test_indicator_compute,  # type: ignore[no-untyped-def]
    *,
    class_column: str = "class",
    synthetic_column: str = "synthetic",
    reference_columns: tuple[str, ...] = (),
    producing_stage: str = "test.producer",
) -> RegisteredIndicator:
    declaration = TableArtifact(
        artifact,
        class_column,
        synthetic_column,
        bool(reference_columns),
        reference_columns,
        producing_stage,
    )
    return RegisteredIndicator(name, artifact, implementation, None, declaration)


def scientific_indicator_definition(name: str, artifact: str) -> dict[str, object]:
    return analysis_identity(scientific_registered_indicator(name, artifact))


def execution_evidence(context_hash: str) -> ExecutionEvidence:
    return ExecutionEvidence(
        context_hash=context_hash,
        data_tree_sha256="1" * 64,
        environment_sha256="2" * 64,
        environment_variables=("PATH",),
        executable_sha256="3" * 64,
        invocation=("/usr/bin/test",),
        model_commit="a" * 40,
        model_config_sha256="5" * 64,
        schema_version=2,
        eqasim_sa_sha256=TEST_EQASIM_SA_SHA256,
    )
