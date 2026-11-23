"""The `oat-effects` figure: local normalised one-at-a-time effects."""

from matplotlib.figure import Figure

from eqasim_sa.core.figures.common import RenderedFigure, ordered_indicators, stamp
from eqasim_sa.core.figures.published import PublishedAnalysis

TITLE = "effets OAT locaux normalisés, non globaux"


def render_oat_effects(analysis: PublishedAnalysis) -> list[RenderedFigure]:
    table = analysis.table("sensitivity")
    rows = table.to_pylist()
    figures: list[RenderedFigure] = []
    for indicator in ordered_indicators(analysis, table):
        subset = [r for r in rows if r["indicator"] == indicator]
        omitted = tuple(r["factor"] for r in subset if r["mu_star"] is None)
        drawn = sorted(
            (r for r in subset if r["mu_star"] is not None),
            key=lambda r: r["mu_star"],
            reverse=True,
        )
        figure = Figure(figsize=(8, max(2.8, 0.5 * len(drawn) + 2.2)))
        ax = figure.subplots()
        figure.subplots_adjust(left=0.22, right=0.95, top=0.85, bottom=0.2)
        positions = list(range(len(drawn)))
        ax.barh(positions, [r["mu_star"] for r in drawn], color="tab:blue", label="mu*")
        sigma = [(i, r["sigma"]) for i, r in enumerate(drawn) if r["sigma"] is not None]
        if sigma:
            ax.plot(
                [s for _, s in sigma],
                [i for i, _ in sigma],
                "D",
                color="tab:orange",
                markersize=5,
                linestyle="none",
                label="sigma",
            )
        seed = [
            (i, r["seed_std"]) for i, r in enumerate(drawn) if r["seed_std"] is not None
        ]
        if seed:
            ax.plot(
                [s for _, s in seed],
                [i for i, _ in seed],
                "|",
                color="black",
                markersize=14,
                markeredgewidth=2,
                linestyle="none",
                label="écart-type entre graines",
            )
        ax.set_yticks(positions, labels=[r["factor"] for r in drawn])
        ax.invert_yaxis()
        ax.set_xlabel(indicator)
        ax.set_title(TITLE, fontsize=10)
        ax.legend(fontsize=7, loc="lower right")
        if omitted:
            figure.text(
                0.5,
                0.06,
                "non tracé : " + ", ".join(omitted),
                ha="center",
                va="bottom",
                fontsize=7,
            )
        stamp(figure, analysis)
        figures.append(
            RenderedFigure(
                name="oat-effects",
                indicator=indicator,
                figure=figure,
                sources=("sensitivity", "governedReport"),
                omitted=omitted,
            )
        )
    return figures
