"""Verified, read-only access to the published analysis generation."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa

from eqasim_sa.utils.arrow import read_parquet

MANIFEST_NAME = "analysis-generation.json"
MANIFEST_VERSION = 8
MANIFEST_LAYOUT = "immutable-generation-v1"


@dataclass(frozen=True)
class PublishedAnalysis:
    campaign_root: Path
    generation_id: str
    method: str
    seed_role: str
    inference_status: str | None
    governed_report: dict
    outputs: Mapping[str, Path]
    _sha256: Mapping[str, str]

    def table(self, role: str) -> pa.Table:
        """Read a published table, parsing the very bytes whose sha256 matched."""
        if role not in self.outputs:
            raise KeyError(f"analysis generation publishes no {role!r} output")
        data = _verified_bytes(role, self.outputs[role], self._sha256[role])
        return read_parquet(pa.BufferReader(data))


def _verified_bytes(role: str, path: Path, expected: str) -> bytes:
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(
            f"{role} sha256 mismatch: manifest {expected}, file {actual} ({path})"
        )
    return data


def load_published_analysis(campaign_root: Path) -> PublishedAnalysis:
    campaign_root = Path(campaign_root).resolve()
    manifest_path = campaign_root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"no {MANIFEST_NAME} in {campaign_root}: run `eqasim-sa analyze` first"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("version") != MANIFEST_VERSION
        or manifest.get("layout") != MANIFEST_LAYOUT
    ):
        raise ValueError(
            "unsupported analysis manifest: expected version "
            f"{MANIFEST_VERSION} and layout {MANIFEST_LAYOUT!r}, got "
            f"version {manifest.get('version')!r} and layout "
            f"{manifest.get('layout')!r}"
        )
    outputs: dict[str, Path] = {}
    hashes: dict[str, str] = {}
    for role, entry in manifest["outputs"].items():
        path = (campaign_root / entry["path"]).resolve()
        if not path.is_relative_to(campaign_root):
            raise ValueError(f"{role} path escapes the campaign root: {entry['path']}")
        outputs[role] = path
        hashes[role] = entry["sha256"]
    if "governedReport" not in outputs:
        raise ValueError("analysis manifest publishes no governedReport")
    report = json.loads(
        _verified_bytes("governedReport", outputs["governedReport"], hashes["governedReport"])
    )
    options = manifest["provenance"]["analysisOptions"]
    return PublishedAnalysis(
        campaign_root=campaign_root,
        generation_id=manifest["generationId"],
        method=options["method"],
        seed_role=options["seedRole"],
        inference_status=manifest.get("inferenceStatus"),
        governed_report=report,
        outputs=outputs,
        _sha256=hashes,
    )
