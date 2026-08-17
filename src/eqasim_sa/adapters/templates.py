import json
import os
import re
import shlex
from pathlib import Path

from eqasim_sa.templates import nantes_template


def _relative(path: Path, directory: Path) -> str:
    return os.path.relpath(path.resolve(strict=True), directory.resolve(strict=True))


def _runner(directory: Path) -> list[str]:
    project = Path(__file__).resolve().parents[3]
    if (project / "pyproject.toml").is_file():
        return [
            "uv", "run", "--project", _relative(project, directory),
            "--locked", "eqasim-sa",
        ]
    return ["eqasim-sa"]


def render_nantes(
    directory: Path,
    model_root: Path,
    manifest: Path,
    data: Path,
    scratch: Path,
    invocation: tuple[str, ...],
    calibration: bool,
    model_config: Path = Path("config_local_nantes.yml"),
    manifest_explicit: bool = False,
) -> bytes:
    runner = _runner(directory)
    manifest_option = [] if not manifest_explicit and manifest.resolve(strict=True) == Path(
        f"{data.resolve(strict=True)}.manifest.json"
    ) else ["--data-manifest", _relative(manifest, directory)]
    init_command = shlex.join(
        [
            *runner,
            "init",
            "--directory",
            ".",
            "--model-root",
            _relative(model_root, directory),
            "--model-config",
            str(model_config),
            *manifest_option,
            "--data-path",
            _relative(data, directory),
            "--scratch",
            _relative(scratch, directory),
            *(f"--invocation={argument}" for argument in invocation),
        ]
    )
    values = {
        "@CAMPAIGN@": "nantes-starter-calibration" if calibration else "nantes-starter",
        "@CAMPAIGN_ROOT@": "calibration-campaign" if calibration else "target-campaign",
        "@MODEL_CONFIG@": json.dumps(_relative(model_root / model_config, directory)),
        "@MODEL_ROOT@": json.dumps(_relative(model_root, directory)),
        "@DATA_MANIFEST@": json.dumps(_relative(manifest, directory)),
        "@LOCKFILE@": json.dumps(_relative(model_root / "uv.lock", directory)),
        "@SCRATCH@": json.dumps(_relative(scratch, directory)),
        "@INVOCATION@": json.dumps(list(invocation)),
        "@DATA_PATH@": json.dumps(_relative(data, directory)),
        "@CALIBRATION@": ""
        if calibration
        else "calibration:\n  meta_configs: [nantes-calibration.yml]",
        "@EQASIM_SA@": shlex.join(runner),
        "@INIT_COMMAND@": init_command,
    }
    rendered = re.sub(
        "|".join(map(re.escape, values)),
        lambda match: values[match.group()],
        nantes_template(),
    )
    return rendered.encode("utf-8")
