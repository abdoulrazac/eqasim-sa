"""User documentation never overwrites or targets upstream's own config_nantes.yml."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
USER_DOCS = (
    *sorted((ROOT / "docs").glob("*.md")),
    *sorted((ROOT / "docs-fr").glob("*.md")),
    ROOT / "README.md",
    ROOT / "README-fr.md",
    ROOT / "protocols" / "nantes-scientific" / "README.md",
)


def test_docs_never_copy_onto_or_select_upstream_config_nantes() -> None:
    offending = []
    for path in USER_DOCS:
        text = path.read_text(encoding="utf-8")
        for pattern in (
            r"cp [^\n`]*config\.yml[\"']? [^\n`]*config_nantes\.yml",
            r"--model-config [^\n]*config_nantes\.yml",
            r"config: config_nantes\.yml",
        ):
            offending += [
                f"{path.relative_to(ROOT)}: {match.group(0)}"
                for match in re.finditer(pattern, text)
            ]
    assert offending == []


def test_docs_show_the_nantes_copy_command() -> None:
    for path in (
        ROOT / "docs" / "structured-configurations-tutorial.md",
        ROOT / "docs-fr" / "structured-configurations-tutorial.md",
        ROOT / "docs" / "getting-started.md",
        ROOT / "docs-fr" / "getting-started.md",
    ):
        assert "config_nantes.yml config_nantes_sa.yml" in path.read_text(
            encoding="utf-8"
        ), path
