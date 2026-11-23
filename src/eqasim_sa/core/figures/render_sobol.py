"""The `sobol-indices` figure: demonstration Sobol indices, single scramble."""

from matplotlib.figure import Figure

from eqasim_sa.core.figures.common import RenderedFigure, ordered_indicators, stamp
from eqasim_sa.core.figures.published import PublishedAnalysis

BANNER = "démonstration : un seul scramble, IC non inférentiels"
WIDTH = 0.38


def render_sobol_indices(analysis: PublishedAnalysis) -> list[RenderedFigure]:
    table = analysis.table("sensitivity")
    rows = table.to_pylist()
    figures: list[RenderedFigure] = []
    for indicator in ordered_indicators(table):
        subset = sorted(
            (r for r in rows if r["indicator"] == indicator), key=lambda r: r["rank"]
        )
        figure = Figure(figsize=(max(5.0, 1.2 * len(subset) + 3), 4.6))
        ax = figure.subplots()
        figure.subplots_adjust(left=0.12, right=0.97, top=0.8, bottom=0.22)
        xs = list(range(len(subset)))
        for offset, key, label, color in (
            (-WIDTH / 2, "first_order", "premier ordre", "tab:blue"),
            (WIDTH / 2, "total_order", "ordre total", "tab:orange"),
        ):
            bars_x = [x + offset for x in xs]
            ax.bar(bars_x, [r[key] or 0.0 for r in subset], WIDTH, color=color, label=label)
            bounded = [
                (bx, r)
                for bx, r in zip(bars_x, subset)
                if r[key] is not None
                and r[f"{key}_ci_low"] is not None
                and r[f"{key}_ci_high"] is not None
            ]
            if bounded:
                bx = [x for x, _ in bounded]
                ax.vlines(
                    bx,
                    [r[f"{key}_ci_low"] for _, r in bounded],
                    [r[f"{key}_ci_high"] for _, r in bounded],
                    color="black",
                    linewidth=1.2,
                )
        ax.set_xticks(xs, labels=[r["factor"] for r in subset])
        ax.set_ylabel(f"indice de Sobol ({indicator})")
        ax.legend(fontsize=7)
        ax.set_title("IC bootstrap sur un seul scramble", fontsize=9)
        figure.text(
            0.5,
            0.93,
            BANNER,
            ha="center",
            va="top",
            color="darkred",
            fontsize=10,
            fontweight="bold",
        )
        stamp(figure, analysis)
        figures.append(
            RenderedFigure(
                name="sobol-indices",
                indicator=indicator,
                figure=figure,
                sources=("sensitivity", "governedReport"),
                omitted=(),
            )
        )
    return figures
