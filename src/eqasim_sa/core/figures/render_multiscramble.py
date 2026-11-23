"""Multi-scramble Sobol figures: indices, convergence of the stopping width, by-scramble."""

import math

from matplotlib.figure import Figure

from eqasim_sa.core.figures.common import (
    RenderedFigure,
    discrete_xticks,
    ordered_indicators,
    stamp,
)
from eqasim_sa.core.figures.published import PublishedAnalysis

WIDTH = 0.38
GREY = "0.6"
ORDERS = (
    ("first_order", "first order", "tab:blue"),
    ("total_order", "total order", "tab:orange"),
)


def _finite(value) -> bool:
    return value is not None and math.isfinite(value)


def _new_figure(width: float = 8.0):
    figure = Figure(figsize=(width, 5.2))
    ax = figure.subplots()
    figure.subplots_adjust(left=0.12, right=0.95, top=0.86, bottom=0.2)
    return figure, ax


def _last(rows: list[dict], indicator: str) -> list[dict]:
    subset = [r for r in rows if r["indicator"] == indicator]
    last = max(r["checkpoint"] for r in subset)
    return [r for r in subset if r["checkpoint"] == last]


# Ranking rules of the producer (multiscramble_sobol.py, _objective_average_ranks):
# fp ranks first_order_mean in decreasing order, ff ranks total_order_ci_high in
# increasing order; ties get their average rank.
RANK_RULES = {
    "fp": "fp rank: decreasing mean S1 (rank 1 = most influential factor)",
    "ff": "ff rank: increasing upper bound of ST (rank 1 = best candidate to fix)",
}


def _rank_key(row: dict):
    rank = row.get("rank")
    return (not _finite(rank), rank if _finite(rank) else 0.0, row["factor"])


def render_multiscramble_indices(analysis: PublishedAnalysis) -> list[RenderedFigure]:
    table = analysis.table("sensitivity")
    rows = table.to_pylist()
    figures: list[RenderedFigure] = []
    for indicator in ordered_indicators(analysis, table):
        subset = _last(rows, indicator)
        actionable = sorted((r for r in subset if r["is_actionable"]), key=_rank_key)
        apart = sorted((r for r in subset if not r["is_actionable"]), key=_rank_key)
        placed = [(float(i), r) for i, r in enumerate(actionable)]
        start = len(actionable) + (1 if apart and actionable else 0)
        placed += [(float(start + i), r) for i, r in enumerate(apart)]
        figure, ax = _new_figure(max(6.0, 1.2 * len(placed) + 3))
        for offset, (key, label, color) in zip((-WIDTH / 2, WIDTH / 2), ORDERS):
            for x, row in placed:
                live = row["is_actionable"]
                ax.bar(
                    x + offset,
                    row[f"{key}_mean"],
                    WIDTH,
                    color=color if live else GREY,
                    hatch=None if live else "//",
                    edgecolor=None if live else "black",
                    label=label if x == placed[0][0] else None,
                )
                ax.vlines(
                    x + offset,
                    row[f"{key}_ci_low"],
                    row[f"{key}_ci_high"],
                    color="black",
                    linewidth=1.2,
                )
        for x, row in placed:
            if row["is_actionable"] and _finite(row.get("rank")):
                ax.text(
                    x,
                    1.0,
                    f"rank {row['rank']:g}",
                    ha="center",
                    va="bottom",
                    fontsize=7,
                    transform=ax.get_xaxis_transform(),
                )
        ax.set_xticks([x for x, _ in placed], labels=[r["factor"] for _, r in placed])
        ax.set_ylabel(f"Sobol index ({indicator})")
        if apart:
            ax.set_xlabel("non-actionable factors on the right (greyed, hatched, unranked)")
        ax.legend(fontsize=7)
        first = subset[0]
        title = (
            f"mean over {first['scrambles']} scrambles, {first['confidence_level']:.0%} CI,"
            f" N = {first['checkpoint']}"
        )
        rule = RANK_RULES.get(first.get("objective"))
        if rule is not None and any(_finite(r.get("rank")) for r in actionable):
            title += f"\n{rule}"
        ax.set_title(title, fontsize=9, pad=14)  # room for the rank labels
        stamp(figure, analysis)
        figures.append(
            RenderedFigure(
                name="multiscramble-indices",
                indicator=indicator,
                figure=figure,
                sources=("sensitivity", "governedReport"),
                omitted=(),
            )
        )
    return figures


def _ci_width_threshold(analysis: PublishedAnalysis) -> float | None:
    criteria = (analysis.governed_report.get("convergence") or {}).get("criteria") or []
    for criterion in criteria:
        if criterion.get("metric") == "ci_width" and _finite(criterion.get("threshold")):
            return float(criterion["threshold"])
    return None


def render_multiscramble_convergence(analysis: PublishedAnalysis) -> list[RenderedFigure]:
    table = analysis.table("convergence")
    rows = table.to_pylist()
    threshold = _ci_width_threshold(analysis)
    figures: list[RenderedFigure] = []
    for indicator in ordered_indicators(analysis, table):
        subset = [r for r in rows if r["indicator"] == indicator]
        objective = subset[0]["objective"]
        # The stopping criterion reads the primary indicator only (reporting.py).
        primary = any(r.get("is_primary_indicator") for r in subset)
        # Same rule as the ci_width stopping criterion in reporting.py.
        order = "first_order" if objective == "fp" else "total_order"
        order_label = "first order" if objective == "fp" else "total order"
        column = f"{order}_ci_width"
        figure, ax = _new_figure()
        worst: dict = {}
        for factor in sorted({r["factor"] for r in subset}):
            points = sorted(
                (r for r in subset if r["factor"] == factor), key=lambda r: r["checkpoint"]
            )
            xs = [p["checkpoint"] for p in points]
            ys = [p[column] for p in points]
            if points[0]["is_actionable"]:
                ax.plot(xs, ys, marker="o", markersize=3, linewidth=1, label=factor)
                for x, y in zip(xs, ys):
                    worst[x] = max(worst.get(x, y), y)
            else:
                ax.plot(
                    xs,
                    ys,
                    color=GREY,
                    linestyle="--",
                    linewidth=1,
                    label=(
                        f"{factor} (non-actionable, excluded from criterion)"
                        if primary
                        else f"{factor} (non-actionable, excluded from max)"
                    ),
                )
        if worst:
            mx = sorted(worst)
            ax.plot(
                mx,
                [worst[x] for x in mx],
                color="black",
                linewidth=2.5,
                label="max (criterion)" if primary else "max over actionable factors",
            )
        if threshold is not None and primary:
            ax.axhline(
                threshold,
                color="red",
                linestyle=":",
                label=f"ci_width threshold = {threshold:g}",
            )
        # Checkpoints iterate over baseSampleSizes; the number of scrambles is fixed.
        ax.set_xlabel("base sample size N at checkpoint")
        discrete_xticks(ax, (r["checkpoint"] for r in subset))
        ax.set_ylabel(f"CI width, {order_label} ({indicator})")
        ax.legend(fontsize=7)
        ax.set_title(
            f"ci_width criterion: max over actionable factors, {order_label}"
            if primary
            else f"max over actionable factors, {order_label}",
            fontsize=9,
        )
        stamp(figure, analysis)
        figures.append(
            RenderedFigure(
                name="multiscramble-convergence",
                indicator=indicator,
                figure=figure,
                sources=("convergence", "governedReport"),
                omitted=(),
            )
        )
    return figures


def render_multiscramble_by_scramble(analysis: PublishedAnalysis) -> list[RenderedFigure]:
    table = analysis.table("byScramble")
    rows = table.to_pylist()
    figures: list[RenderedFigure] = []
    for indicator in ordered_indicators(analysis, table):
        subset = _last(rows, indicator)
        factors = sorted(
            {r["factor"] for r in subset},
            key=lambda f: (not any(r["is_actionable"] for r in subset if r["factor"] == f), f),
        )
        figure, ax = _new_figure(max(6.0, 1.2 * len(factors) + 3))
        for offset, (key, label, color) in zip((-0.15, 0.15), ORDERS):
            xs, ys, colors = [], [], []
            for i, factor in enumerate(factors):
                for r in subset:
                    if r["factor"] == factor and _finite(r[key]):
                        xs.append(i + offset)
                        ys.append(r[key])
                        colors.append(color if r["is_actionable"] else GREY)
            ax.scatter(xs, ys, c=colors, s=14, label=label)
        ax.set_xticks(range(len(factors)), labels=factors)
        ax.set_ylabel(f"Sobol index per scramble ({indicator})")
        ax.legend(fontsize=7)
        ax.set_title(
            f"one point per scramble, N = {subset[0]['checkpoint']}", fontsize=9
        )
        stamp(figure, analysis)
        figures.append(
            RenderedFigure(
                name="multiscramble-by-scramble",
                indicator=indicator,
                figure=figure,
                sources=("byScramble", "governedReport"),
                omitted=(),
            )
        )
    return figures
