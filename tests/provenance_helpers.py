import hashlib
import os
from collections.abc import Callable, Sequence
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from eqasim_sa.core.context_identity import ExecutionContextIdentity
from eqasim_sa.core.indicators import (
    ArtifactSet,
    TableArtifact,
    TotalVariationIndicator,
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


def scientific_indicator(
    artifact: str = "age",
    reference: str = "reference",
    *,
    class_column: str = "class",
    synthetic_column: str = "synthetic",
    producing_stage: str = "test.producer",
) -> TotalVariationIndicator:
    """Return the real ``tv_distance`` indicator used as the shared test double.

    It reads the table written by :func:`write_artifact` under ``artifact``.
    """

    declaration = TableArtifact(
        artifact,
        class_column,
        synthetic_column,
        True,
        (reference,),
        producing_stage,
    )
    return TotalVariationIndicator(declaration, reference)


def scientific_indicator_definition(
    artifact: str = "age", reference: str = "reference"
) -> dict[str, object]:
    """Scientific definition of :func:`scientific_indicator`."""

    return analysis_identity(scientific_indicator(artifact, reference))


def artifact_table(
    synthetic: Sequence[float], reference: Sequence[float] | None = None
) -> pa.Table:
    """Small class/synthetic/reference table read by :func:`scientific_indicator`."""

    masses = [float(value) for value in synthetic]
    return pa.table(
        {
            "class": [f"c{index}" for index in range(len(masses))],
            "synthetic": masses,
            "reference": (
                [1.0] * len(masses)
                if reference is None
                else [float(value) for value in reference]
            ),
        }
    )


def tv_table(value: float) -> pa.Table:
    """Table whose total-variation distance to its reference is ``value`` in [0, 1]."""

    return artifact_table([1.0 - value, value], [1.0, 0.0])


def write_artifact(archive: Path, name: str, table: pa.Table) -> Path:
    """Write one archived artifact table ``<archive>/<name>.parquet``."""

    archive.mkdir(parents=True, exist_ok=True)
    path = archive / f"{name}.parquet"
    pq.write_table(table, path, compression="zstd")
    return path


def synthetic_value(indicator: TotalVariationIndicator, artifacts: ArtifactSet) -> float:
    """First synthetic cell of the indicator's artifact, as a float."""

    path = artifacts.path(indicator.artifact.name)
    column = indicator.artifact.synthetic_column
    return float(pq.read_table(path, columns=[column])[column][0].as_py())


def control_indicator(
    monkeypatch,  # type: ignore[no-untyped-def]
    compute: Callable[[TotalVariationIndicator, ArtifactSet], float] = synthetic_value,
) -> None:
    """Replace ``TotalVariationIndicator.compute`` for tests needing controlled values.

    The default returns the first synthetic cell, so method tests can feed
    arbitrary signed or unbounded indicator values through real archives.
    """

    monkeypatch.setattr(TotalVariationIndicator, "compute", compute)


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
