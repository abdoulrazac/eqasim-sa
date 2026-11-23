"""The `grid-response` figure: replicated mean ± 95% CI against a parameter."""

import pyarrow as pa
import pyarrow.types as pat
from matplotlib.figure import Figure

from eqasim_sa.core.figures.common import (
    FigureNotApplicable,
    RenderedFigure,
    discrete_xticks,
    ordered_indicators,
    stamp,
)
from eqasim_sa.core.figures.published import PublishedAnalysis

NON_PARAMETERS = {"indicator", "n", "mean", "std", "cv", "ci95_low", "ci95_high"}


def _is_numeric(field: pa.Field) -> bool:
    return pat.is_integer(field.type) or pat.is_floating(field.type)


def render_grid_response(analysis: PublishedAnalysis) -> list[RenderedFigure]:
    table = analysis.table("dispersion")
    parameters = [f for f in table.schema if f.name not in NON_PARAMETERS]
    numeric = [f.name for f in parameters if _is_numeric(f)]
    if not numeric:
        raise FigureNotApplicable(
            "grid-response needs at least one numeric parameter; "
            "non-numeric parameters cannot be an x axis"
        )
    names = [f.name for f in parameters]
    rows = table.to_pylist()

    figures: list[RenderedFigure] = []
    for indicator in ordered_indicators(analysis, table):
        subset = [r for r in rows if r["indicator"] == indicator]
        single = all(r["n"] == 1 for r in subset)
        figure = Figure(figsize=(max(5.0, 4.5 * len(numeric)), 4.6))
        axes = figure.subplots(1, len(numeric), squeeze=False)[0]
        figure.subplots_adjust(left=0.1, right=0.97, top=0.82, bottom=0.2, wspace=0.3)
        for ax, x_name in zip(axes, numeric):
            others = [n for n in names if n != x_name]
            groups: dict[tuple, list[dict]] = {}
            for row in subset:
                if row["mean"] is None or row[x_name] is None:
                    continue
                groups.setdefault(tuple(row[n] for n in others), []).append(row)
            for key in sorted(groups, key=lambda k: tuple(str(v) for v in k)):
                points = sorted(groups[key], key=lambda r: r[x_name])
                label = ", ".join(f"{n}={v}" for n, v in zip(others, key)) or None
                line = ax.plot(
                    [p[x_name] for p in points],
                    [p["mean"] for p in points],
                    marker="o",
                    markersize=4,
                    label=label,
                )[0]
                if single:
                    continue
                bounded = [
                    p
                    for p in points
                    if p["ci95_low"] is not None and p["ci95_high"] is not None
                ]
                if bounded:
                    ax.vlines(
                        [p[x_name] for p in bounded],
                        [p["ci95_low"] for p in bounded],
                        [p["ci95_high"] for p in bounded],
                        color=line.get_color(),
                        linewidth=1.2,
                    )
            ax.set_xlabel(x_name)
            discrete_xticks(ax, (row[x_name] for row in subset))
            ax.set_ylabel(f"{indicator} (mean)")
            if others and groups:
                ax.legend(fontsize=7, title="other parameters", title_fontsize=7)
            ax.set_title(
                "1 replication, no CI"
                if single
                else "mean ± 95% CI across replications",
                fontsize=9,
            )
        figure.suptitle(indicator, fontsize=11)
        stamp(figure, analysis)
        figures.append(
            RenderedFigure(
                name="grid-response",
                indicator=indicator,
                figure=figure,
                sources=("dispersion", "governedReport"),
                omitted=(),
            )
        )
    return figures
