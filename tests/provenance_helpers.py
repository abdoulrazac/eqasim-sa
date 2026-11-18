import hashlib
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from eqasim_sa.core.context_identity import ExecutionContextIdentity
from eqasim_sa.core.indicators import (
    RegisteredIndicator,
    TableArtifact,
    analysis_identity,
)
from eqasim_sa.core.ledger import ArtifactManifestEntry, RunRecord, write_run
from eqasim_sa.core.software_identity import read_software_identity
from eqasim_sa.ports.simulator import ExecutionEvidence


_SOFTWARE = read_software_identity()
TEST_EQASIM_SA_VERSION = _SOFTWARE.version
TEST_EQASIM_SA_SHA256 = _SOFTWARE.sha256


def scientific_context_descriptor() -> dict[str, str]:
    return {
        "eqasimSourceSha256": "a" * 64,
        "dataManifestSha256": "b" * 64,
        "lockfileSha256": "c" * 64,
        "eqasimSaVersion": TEST_EQASIM_SA_VERSION,
    }


def context_hash(descriptor: dict[str, str]) -> str:
    return ExecutionContextIdentity.from_descriptor(descriptor).sha256


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
        model_source_sha256="a" * 64,
        model_config_sha256="5" * 64,
        schema_version=3,
        eqasim_sa_sha256=TEST_EQASIM_SA_SHA256,
    )


def archive_manifest(archive: Path) -> tuple[ArtifactManifestEntry, ...]:
    """Describe every file of an archive directory as v2 manifest entries."""

    return tuple(
        ArtifactManifestEntry(
            path.name,
            path.stat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(archive.iterdir(), key=lambda item: os.fsencode(item.name))
    )


def write_archive(root: Path, run_id: str) -> tuple[ArtifactManifestEntry, ...]:
    """Return the manifest of ``archives/<run_id>``, creating a default artifact.

    An existing archive directory is described as-is; a missing one receives a
    single zstd ``output.parquet`` artifact.
    """

    archive = root / "archives" / run_id
    if not archive.exists():
        archive.mkdir(parents=True)
        pq.write_table(
            pa.table({"value": [1]}), archive / "output.parquet", compression="zstd"
        )
    return archive_manifest(archive)


def write_verified_run(
    root: Path,
    record: RunRecord,
    evidence: ExecutionEvidence | None = None,
) -> Path:
    """Publish a v2 completion marker bound to a verifiable archive."""

    return write_run(
        root, record, write_archive(root, record.run_id), execution_evidence=evidence
    )
