"""Analysis scripts computing derived outputs from archived run outputs."""

import re
from dataclasses import dataclass
from pathlib import Path

from eqasim_sa.core.indicators import DerivedOutput, validate_output_name

ANALYSES_CACHE_DIRECTORY = "analyses-cache"
"""Directory of ``campaign_root`` holding recomputable analysis results."""

ANALYSES_LOG_DIRECTORY = "logs"
"""Subdirectory of the cache holding script error logs; not an analysis name."""

CACHE_ENTRY = re.compile(r"[0-9a-f]{64}\.(csv|sha256)")
CACHE_TEMPORARY = re.compile(r"\.[0-9a-f]{64}\.(csv|sha256)\.[A-Za-z0-9_-]+\.tmp")
LOG_ENTRY = re.compile(r"[A-Za-z0-9_-]+\.log")
LOG_TEMPORARY = re.compile(r"\.[A-Za-z0-9_-]+\.log\.[A-Za-z0-9_-]+\.tmp")


@dataclass(frozen=True)
class AnalysisDeclaration:
    """One declared analysis script and the derived output it produces."""

    name: str
    script: Path
    inputs: tuple[str, ...]
    output: DerivedOutput

    def __post_init__(self) -> None:
        validate_output_name(self.name)
        if not isinstance(self.script, Path) or not self.script.is_absolute():
            raise ValueError("analysis script must be an absolute path")
        if not isinstance(self.output, DerivedOutput):
            raise ValueError("analysis output must be a DerivedOutput")
        if (
            self.output.name != self.name
            or self.output.analysis != self.name
            or self.output.inputs != self.inputs
        ):
            raise ValueError("analysis output must describe this analysis")
