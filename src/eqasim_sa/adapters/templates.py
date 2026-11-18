"""Render the packaged starter project into a user-owned directory."""

import json
import os
import re
import shlex
from pathlib import Path

from eqasim_sa.adapters.meta_config_defaults import DEFAULT_INVOCATION
from eqasim_sa.templates import project_templates


def _relative(path: Path, directory: Path) -> str:
    return os.path.relpath(Path(os.path.abspath(path)), Path(os.path.abspath(directory)))


def _runner() -> str:
    project = Path(__file__).resolve().parents[3]
    command = (
        ["uv", "run", "--project", str(project), "--locked", "eqasim-sa"]
        if (project / "pyproject.toml").is_file()
        else ["eqasim-sa"]
    )
    return shlex.join(command)


def render_project(
    directory: Path,
    project_name: str,
    model_root: Path,
    data: Path,
    scratch: Path,
    invocation: tuple[str, ...],
    model_config: Path = Path("config.yml"),
) -> dict[str, bytes]:
    """Render every packaged starter with project-relative paths and commands."""

    configs = directory / "configs"
    values = {
        "@PROJECT_NAME@": project_name,
        # model.config is read from model.root when relative.
        "@MODEL_CONFIG@": json.dumps(
            str(model_config) if model_config.is_absolute() else model_config.as_posix()
        ),
        "@MODEL_ROOT@": json.dumps(_relative(model_root, configs)),
        "@DATA_MANIFEST@": json.dumps("../manifest.json"),
        "@SCRATCH@": json.dumps(_relative(scratch, configs)),
        "@INVOCATION_LINE@": (
            ""
            if tuple(invocation) == DEFAULT_INVOCATION
            else f"  invocation: {json.dumps(list(invocation))}\n"
        ),
        "@DATA_PATH@": json.dumps(_relative(data, configs)),
        "@RUNNER@": _runner(),
    }
    pattern = "|".join(map(re.escape, values))
    return {
        name: re.sub(pattern, lambda match: values[match.group()], text).encode()
        for name, text in project_templates().items()
    }
