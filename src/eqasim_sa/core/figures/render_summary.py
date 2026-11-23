"""The `output-summary` figure: spread of each indicator over the design."""

from matplotlib.figure import Figure

from eqasim_sa.core.figures.common import RenderedFigure, ordered_indicators, stamp
from eqasim_sa.core.figures.published import PublishedAnalysis

CAPTION = "médiane, q05–q95 et min–max : distribution sur les positions du plan"


def render_output_summary(analysis: PublishedAnalysis) -> list[RenderedFigure]:
    table = analysis.table("outputSummary")
    rows = {row["indicator"]: row for row in table.to_pylist()}
    drawn: list[str] = []
    omitted: list[str] = []
    for name in ordered_indicators(table):
        (omitted if rows[name].get("median") is None else drawn).append(name)

    figure = Figure(figsize=(8, max(2.5, 0.6 * len(drawn) + 1.5)))
    ax = figure.add_axes((0.2, 0.18, 0.75, 0.62))
    for position, name in enumerate(reversed(drawn)):
        row = rows[name]
        low, high = row.get("minimum"), row.get("maximum")
        q05, q95 = row.get("q05"), row.get("q95")
        if low is not None and high is not None:
            ax.hlines(position, low, high, color="0.6", linewidth=1)
        if q05 is not None and q95 is not None:
            ax.hlines(position, q05, q95, color="tab:blue", linewidth=5)
        ax.plot(row["median"], position, "o", color="black")
    ax.set_yticks(range(len(drawn)), labels=list(reversed(drawn)))
    ax.set_title(CAPTION, fontsize=9)
    stamp(figure, analysis)
    return [
        RenderedFigure(
            name="output-summary",
            indicator=None,
            figure=figure,
            sources=("outputSummary", "governedReport"),
            omitted=tuple(omitted),
        )
    ]
