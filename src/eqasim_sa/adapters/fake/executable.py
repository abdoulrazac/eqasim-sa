"""Command-line fake pipeline used by integration and packaging tests."""

import argparse
import hashlib
import json
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def parser() -> argparse.ArgumentParser:
    """Build the internal fake-pipeline argument parser."""

    result = argparse.ArgumentParser(description="Installed fake eqasim pipeline")
    result.add_argument("--request", type=Path, required=True)
    result.add_argument("--manifest", type=Path, required=True)
    return result


def main() -> int:
    """Materialize deterministic fake artifacts from one request document."""

    arguments = parser().parse_args()
    request = json.loads(arguments.request.read_text(encoding="utf-8"))
    config = request["config"]
    nested = config.get("config", {})
    if nested.get("fail") is True:
        raise RuntimeError("fake pipeline failure requested")
    seed = request["seed"]
    material = json.dumps(
        {"config": config, "seed": seed},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    value = int.from_bytes(hashlib.sha256(material).digest()[:8], "big") & (2**63 - 1)
    output = arguments.manifest.parent / "population.parquet"
    pq.write_table(
        pa.table(
            {
                "person_id": ["person-0"],
                "value": [value],
                "seed": [seed],
                "producer_pid": [os.getpid()],
            }
        ),
        output,
        compression="zstd",
    )
    arguments.manifest.write_text(
        json.dumps(
            {
                "artifacts": [{"name": "population.parquet", "path": str(output)}],
            }
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
