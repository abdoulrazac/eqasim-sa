"""Shared figure plumbing: rendered-figure record, governance stamp, ordering."""

from dataclasses import dataclass

import pyarrow as pa
from matplotlib.figure import Figure

from eqasim_sa.core.figures.published import PublishedAnalysis

INCOMPLETE_STATUS = "incomplete_noninferential"
INCOMPLETE_BANNER = "campagne incomplète"


class FigureNotApplicable(ValueError):
    """The figure cannot be drawn faithfully from the published data."""


@dataclass(frozen=True)
class RenderedFigure:
    name: str
    indicator: str | None
    figure: Figure
    sources: tuple[str, ...]
    omitted: tuple[str, ...]


def inference_status(analysis: PublishedAnalysis) -> str | None:
    """Single source for the status: governed report first, manifest second."""
    status = analysis.governed_report.get("inferenceStatus")
    return status if status is not None else analysis.inference_status


STAMP_FONTSIZE = 7
# Generous upper bound of the DejaVu Sans advance width, in em, used to wrap
# the stamp before drawing so that it always fits the figure width.
_EM_PER_CHARACTER = 0.62


def _stamp_tokens(analysis: PublishedAnalysis) -> list[tuple[str, str]]:
    """Stamp pieces with the separator that follows each one."""
    report = analysis.governed_report
    claim = "true" if report.get("claimAuthorized") is True else "false"
    reasons = list(report.get("reasonCodes") or []) or ["none"]
    tokens = [
        (analysis.method, " · "),
        (str(inference_status(analysis)), " · "),
        (f"claimAuthorized={claim}", " · "),
    ]
    tokens += [(reason, ", ") for reason in reasons[:-1]]
    tokens.append((reasons[-1], " · "))
    tokens.append((f"génération {analysis.generation_id[:12]}", ""))
    return tokens


def stamp_text(analysis: PublishedAnalysis, width: int | None = None) -> str:
    """The governance line, wrapped at ``width`` characters when given."""
    lines: list[str] = []
    line = ""
    for token, separator in _stamp_tokens(analysis):
        candidate = line + token
        if width is not None and line and len(candidate) > width:
            lines.append(line.rstrip())
            candidate = token
        line = candidate + separator
    lines.append(line.rstrip())
    return "\n".join(lines)


def stamp(figure: Figure, analysis: PublishedAnalysis) -> None:
    """Put the governance line at the foot, and a red banner when incomplete."""
    usable_points = 0.94 * figure.get_figwidth() * 72
    width = max(20, int(usable_points / (STAMP_FONTSIZE * _EM_PER_CHARACTER)))
    figure.text(
        0.5,
        0.01,
        stamp_text(analysis, width),
        ha="center",
        va="bottom",
        fontsize=STAMP_FONTSIZE,
        linespacing=1.3,
    )
    if inference_status(analysis) == INCOMPLETE_STATUS:
        figure.text(
            0.5,
            0.99,
            INCOMPLETE_BANNER,
            ha="center",
            va="top",
            color="red",
            fontsize=12,
            fontweight="bold",
        )


def ordered_indicators(analysis: PublishedAnalysis, table: pa.Table) -> list[str]:
    """Primary indicator first, then alphabetical.

    The primary indicator is the charter decision's ``primary_indicator`` as
    the governed report publishes it; without a charter decision (or when it
    names no indicator of the table), the table's ``is_primary_indicator``
    column decides; without either, the order is alphabetical.
    """
    names = set(table.column("indicator").to_pylist())
    decision = analysis.governed_report.get("decision") or {}
    primary = decision.get("primary_indicator")
    if primary in names:
        flagged = {primary}
    elif "is_primary_indicator" in table.column_names:
        flags = table.column("is_primary_indicator").to_pylist()
        flagged = {
            n for n, p in zip(table.column("indicator").to_pylist(), flags) if p
        }
    else:
        flagged = set()
    return sorted(names, key=lambda n: (n not in flagged, n))
