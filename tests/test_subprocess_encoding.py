"""Child-process text is decoded as UTF-8, never with the Windows locale (cp1252)."""

import ast
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from eqasim_sa.adapters import execution_seal, meta_config

SOURCE = Path(__file__).resolve().parents[1] / "src" / "eqasim_sa"


_SUBPROCESS = {"run", "Popen", "check_output", "check_call", "call", "start"}


def _called_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _text_mode_calls_without_encoding() -> list[str]:
    offenders = []
    for path in sorted(SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _called_name(node) not in _SUBPROCESS:
                continue
            keywords = {keyword.arg: keyword.value for keyword in node.keywords}
            text_mode = any(
                name in keywords
                and isinstance(keywords[name], ast.Constant)
                and keywords[name].value is True
                for name in ("text", "universal_newlines")
            )
            if text_mode and "encoding" not in keywords:
                offenders.append(f"{path.relative_to(SOURCE)}:{node.lineno}")
    return offenders


def test_every_text_mode_child_process_declares_its_encoding() -> None:
    assert _text_mode_calls_without_encoding() == []


@pytest.mark.parametrize("module", [meta_config, execution_seal])
def test_git_output_is_decoded_as_strict_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module
) -> None:  # type: ignore[no-untyped-def]
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    root = tmp_path / "modèle-été"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    seen = {}
    real_run = subprocess.run

    def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", spy)
    if module is meta_config:
        top = module._git(root, "rev-parse", "--show-toplevel")
    else:
        top = module._git(root, dict(os.environ), "rev-parse", "--show-toplevel")
    assert (seen["encoding"], seen["errors"]) == ("utf-8", "strict")
    assert Path(top).resolve() == root.resolve()
