"""Deterministic simulator double for end-to-end campaign tests."""

import json
import shutil
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

from eqasim_sa.ports.simulator import (
    Artifact,
    IndicatorRequirement,
    JsonValue,
    Manifest,
    ResolvedConfig,
)


class FakeSimulator:
    """Invoke the installed fake pipeline through the simulator interface."""

    def requirements(
        self, indicators: Sequence[IndicatorRequirement]
    ) -> Mapping[str, JsonValue]:
        return {"run": [item.artifact for item in indicators]}

    def simulate(self, config: ResolvedConfig, seed: int, workdir: Path) -> Manifest:
        attempt = workdir / f"a{uuid.uuid4().hex[:12]}"
        attempt.mkdir(parents=True)
        request = attempt / "request.json"
        manifest_path = attempt / "manifest.json"
        request.write_text(
            json.dumps(
                {"config": config, "seed": seed},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ),
            encoding="utf-8",
        )
        sibling = Path(sys.executable).with_name("eqasim-sa-fake-pipeline")
        executable = (
            str(sibling)
            if sibling.is_file()
            else shutil.which("eqasim-sa-fake-pipeline")
        )
        if executable is None:
            raise FileNotFoundError("installed eqasim-sa-fake-pipeline not found")
        subprocess.run(
            [executable, "--request", str(request), "--manifest", str(manifest_path)],
            shell=False,
            check=True,
            capture_output=True,
            text=True,
        )
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        artifacts = tuple(
            Artifact(item["name"], Path(item["path"])) for item in document["artifacts"]
        )
        return Manifest(artifacts, attempt)
