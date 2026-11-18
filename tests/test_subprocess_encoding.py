"""Child-process text is decoded as UTF-8, never with the Windows locale (cp1252)."""

import ast
from pathlib import Path



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
