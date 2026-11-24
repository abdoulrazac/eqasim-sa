"""Render the packaged starter project into a user-owned directory."""

import json
import os
import re
import shlex
from pathlib import Path

from eqasim_sa.adapters.eqasim.artifacts import synthesis_outputs
from eqasim_sa.adapters.meta_config_defaults import (
    DEFAULT_INVOCATION,
    mode_choice,
    output_formats,
)
from eqasim_sa.templates import project_templates


def _relative(path: Path, directory: Path) -> str:
    absolute = Path(os.path.abspath(path))
    try:
        return os.path.relpath(absolute, Path(os.path.abspath(directory)))
    except ValueError:
        # Windows: another drive has no relative path; write it absolute.
        return str(absolute)


def _runner() -> str:
    project = Path(__file__).resolve().parents[3]
    command = (
        ["uv", "run", "--project", str(project), "--locked", "eqasim-sa"]
        if (project / "pyproject.toml").is_file()
        else ["eqasim-sa"]
    )
    return shlex.join(command)


def _outputs_block(eqasim_config: dict) -> str:
    """Commented ``outputs:`` head declaring every file synthesis.output writes."""

    formats = output_formats(eqasim_config)
    writes_mode_choice = mode_choice(eqasim_config)
    shown = (
        f"[{', '.join(map(str, formats))}]"
        if isinstance(formats, list)
        else json.dumps(formats, default=str)
    )
    if "output_formats" not in eqasim_config:
        shown += " (eqasim's default)"
    comment = (
        "# Every file eqasim's synthesis.output stage writes with output_formats "
        f"{shown} and mode_choice {str(writes_mode_choice).lower()} in your "
        "eqasim configuration. Remove the ones you do not need to save disk "
        "space (spatial files are large at high sampling rates)."
    )
    lines, line = [], ""
    for word in comment.split(" "):
        if line and len(line) + 1 + len(word) > 72:
            lines.append(line)
            line = "#"
        line = f"{line} {word}" if line else word
    lines.append(line)
    lines.append("outputs:")
    lines.extend(
        f"  {name}: {{}}"
        for name in synthesis_outputs(formats, writes_mode_choice)
    )
    return "\n".join(lines)


def render_project(
    directory: Path,
    project_name: str,
    model_root: Path,
    data: Path,
    scratch: Path,
    invocation: tuple[str, ...],
    eqasim_config: dict,
    model_config: Path = Path("config.yml"),
) -> dict[str, bytes]:
    """Render every packaged starter with project-relative paths and commands.

    ``eqasim_config`` (the ``config:`` block of the selected eqasim
    configuration) decides which synthesis.output files the starters declare.
    """

    configs = directory / "configs"
    config_text = (
        str(model_config) if model_config.is_absolute() else model_config.as_posix()
    )
    values = {
        "@PROJECT_NAME@": project_name,
        # Plain text for the README; the YAML value below is quoted.
        "@MODEL_CONFIG_PATH@": config_text,
        # model.config is read from model.root when relative.
        "@MODEL_CONFIG@": json.dumps(config_text),
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
        "@EQASIM_OUTPUTS@": _outputs_block(eqasim_config),
    }
    pattern = "|".join(map(re.escape, values))
    return {
        name: re.sub(pattern, lambda match: values[match.group()], text).encode()
        for name, text in project_templates().items()
    }
