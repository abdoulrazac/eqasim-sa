"""Shared figure plumbing: rendered-figure record, governance stamp, ordering."""

from dataclasses import dataclass

import pyarrow as pa
from matplotlib.figure import Figure

from eqasim_sa.core.figures.published import PublishedAnalysis

INCOMPLETE_STATUS = "incomplete_noninferential"
INCOMPLETE_BANNER = "campagne incomplète"


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


def stamp_text(analysis: PublishedAnalysis) -> str:
    report = analysis.governed_report
    status = inference_status(analysis)
    reasons = ", ".join(report.get("reasonCodes") or []) or "none"
    return (
        f"{analysis.method} · {status} · claimAuthorized=false · {reasons}"
        f" · génération {analysis.generation_id[:12]}"
    )


def stamp(figure: Figure, analysis: PublishedAnalysis) -> None:
    """Put the governance line at the foot, and a red banner when incomplete."""
    figure.text(0.5, 0.01, stamp_text(analysis), ha="center", va="bottom", fontsize=7)
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


def ordered_indicators(table: pa.Table) -> list[str]:
    """Primary indicator first when flagged, then alphabetical."""
    names = table.column("indicator").to_pylist()
    if "is_primary_indicator" in table.column_names:
        primary = table.column("is_primary_indicator").to_pylist()
        flagged = {n for n, p in zip(names, primary) if p}
    else:
        flagged = set()
    return sorted(set(names), key=lambda n: (n not in flagged, n))
