"""The `output-summary` figure: spread of each indicator over the design."""

from matplotlib.figure import Figure

from eqasim_sa.core.figures.common import RenderedFigure, ordered_indicators, stamp
from eqasim_sa.core.figures.published import PublishedAnalysis

CAPTION = "median, q05–q95 and min–max: distribution over plan positions"


def render_output_summary(analysis: PublishedAnalysis) -> list[RenderedFigure]:
    table = analysis.table("outputSummary")
    rows = {row["indicator"]: row for row in table.to_pylist()}
    drawn: list[str] = []
    omitted: list[str] = []
    for name in ordered_indicators(analysis, table):
        (omitted if rows[name].get("median") is None else drawn).append(name)

    figure = Figure(figsize=(8, max(2.5, 0.9 * len(drawn) + 1.8)))
    axes = figure.subplots(max(len(drawn), 1), 1, sharex=False, squeeze=False)[:, 0]
    figure.subplots_adjust(left=0.2, right=0.95, top=0.82, bottom=0.18, hspace=0.9)
    for ax, name in zip(axes, drawn):
        row = rows[name]
        low, high = row.get("minimum"), row.get("maximum")
        q05, q95 = row.get("q05"), row.get("q95")
        if low is not None and high is not None:
            ax.hlines(0, low, high, color="0.6", linewidth=1)
        if q05 is not None and q95 is not None:
            ax.hlines(0, q05, q95, color="tab:blue", linewidth=5)
        ax.plot(row["median"], 0, "o", color="black")
        ax.set_yticks([0], labels=[name])
        ax.tick_params(axis="x", labelsize=7)
    axes[0].set_title(CAPTION, fontsize=9)
    if omitted:
        figure.text(
            0.5, 0.06, "not plotted: " + ", ".join(omitted),
            ha="center", va="bottom", fontsize=7,
        )
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
