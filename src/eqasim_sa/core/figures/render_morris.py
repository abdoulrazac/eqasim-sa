"""Canonical Morris figures: mu*-sigma plane, convergence, rank reversals, noise floor."""

import copy
import math

import numpy as np
from matplotlib import colormaps
from matplotlib.figure import Figure

from eqasim_sa.core.figures.common import (
    FigureNotApplicable,
    RenderedFigure,
    ordered_indicators,
    stamp,
)
from eqasim_sa.core.figures.published import PublishedAnalysis

CI_COLOR = "tab:blue"


def _finite(value) -> bool:
    return value is not None and math.isfinite(value)


def _new_figure(width: float = 8, height: float = 5.5):
    figure = Figure(figsize=(width, height))
    ax = figure.subplots()
    figure.subplots_adjust(left=0.12, right=0.95, top=0.88, bottom=0.18)
    return figure, ax


def _note(figure: Figure, text: str) -> None:
    figure.text(0.5, 0.06, text, ha="center", va="bottom", fontsize=7)


def render_morris_plane(analysis: PublishedAnalysis) -> list[RenderedFigure]:
    table = analysis.table("sensitivity")
    rows = table.to_pylist()
    figures: list[RenderedFigure] = []
    for indicator in ordered_indicators(analysis, table):
        subset = [r for r in rows if r["indicator"] == indicator]
        omitted = tuple(
            r["factor"]
            for r in subset
            if not (_finite(r["sigma"]) and _finite(r["mu_star"]))
        )
        drawn = [r for r in subset if r["factor"] not in omitted]
        figure, ax = _new_figure()
        with_ci = [
            r
            for r in drawn
            if _finite(r["mu_star_ci_low"]) and _finite(r["mu_star_ci_high"])
        ]
        if with_ci:
            ax.hlines(
                [r["sigma"] for r in with_ci],
                [r["mu_star_ci_low"] for r in with_ci],
                [r["mu_star_ci_high"] for r in with_ci],
                color=CI_COLOR,
                linewidth=1.2,
                label="IC de mu* (publié)",
            )
        if drawn:
            ax.plot(
                [r["mu_star"] for r in drawn],
                [r["sigma"] for r in drawn],
                "o",
                color=CI_COLOR,
                linestyle="none",
            )
            for r in drawn:
                ax.annotate(
                    r["factor"],
                    (r["mu_star"], r["sigma"]),
                    textcoords="offset points",
                    xytext=(4, 4),
                    fontsize=7,
                )
        top = max(
            [r["mu_star"] for r in drawn]
            + [r["mu_star_ci_high"] for r in with_ci]
            + [r["sigma"] for r in drawn]
            + [0.0]
        )
        ax.plot(
            [0.0, top], [0.0, top], linestyle="--", color="grey", label="sigma = mu*"
        )
        ax.set_xlabel(f"mu* ({indicator})")
        ax.set_ylabel("sigma")
        ax.set_title("plan mu*–sigma", fontsize=10)
        ax.legend(fontsize=7, loc="upper left")
        if omitted:
            _note(figure, "non tracé (sigma ou mu* absent) : " + ", ".join(omitted))
        stamp(figure, analysis)
        figures.append(
            RenderedFigure(
                name="morris-plane",
                indicator=indicator,
                figure=figure,
                sources=("sensitivity", "governedReport"),
                omitted=omitted,
            )
        )
    return figures


def render_morris_convergence(analysis: PublishedAnalysis) -> list[RenderedFigure]:
    table = analysis.table("convergence")
    rows = table.to_pylist()
    figures: list[RenderedFigure] = []
    for indicator in ordered_indicators(analysis, table):
        subset = [r for r in rows if r["indicator"] == indicator]
        figure, ax = _new_figure()
        omitted: list[str] = []
        for factor in sorted({r["factor"] for r in subset}):
            series = sorted(
                (r for r in subset if r["factor"] == factor),
                key=lambda r: r["checkpoint_trajectories"],
            )
            usable = [r for r in series if _finite(r["mu_star"])]
            if not usable:
                omitted.append(factor)
                continue
            (line,) = ax.plot(
                [r["checkpoint_trajectories"] for r in usable],
                [r["mu_star"] for r in usable],
                "o-",
                markersize=3,
                label=factor,
            )
            bounded = [
                r
                for r in usable
                if _finite(r["mu_star_ci_low"]) and _finite(r["mu_star_ci_high"])
            ]
            if bounded:
                ax.vlines(
                    [r["checkpoint_trajectories"] for r in bounded],
                    [r["mu_star_ci_low"] for r in bounded],
                    [r["mu_star_ci_high"] for r in bounded],
                    color=line.get_color(),
                    linewidth=1.0,
                    alpha=0.7,
                )
        ax.set_xlabel("trajectoires au point de contrôle")
        ax.set_ylabel(f"mu* ({indicator})")
        ax.set_title("mu* et son IC selon le point de contrôle", fontsize=10)
        if ax.get_legend_handles_labels()[0]:
            ax.legend(fontsize=7, loc="best")
        if omitted:
            _note(figure, "non tracé : " + ", ".join(omitted))
        stamp(figure, analysis)
        figures.append(
            RenderedFigure(
                name="morris-convergence",
                indicator=indicator,
                figure=figure,
                sources=("convergence", "governedReport"),
                omitted=tuple(omitted),
            )
        )
    return figures


def _factor_order(pairs: list[dict]) -> list[str]:
    """Rank order at one checkpoint, recovered from the published pairs.

    The producer emits each pair once, as (higher-ranked, lower-ranked) in the
    order of mu* at that checkpoint, so the factor at position i is factor_a in
    exactly n-1-i pairs.
    """
    count: dict[str, int] = {}
    for pair in pairs:
        count[pair["factor_a"]] = count.get(pair["factor_a"], 0) + 1
        count.setdefault(pair["factor_b"], 0)
    return sorted(count, key=lambda name: (-count[name], name))


def render_morris_rank_reversals(analysis: PublishedAnalysis) -> list[RenderedFigure]:
    table = analysis.table("rankReversals")
    rows = table.to_pylist()
    figures: list[RenderedFigure] = []
    for indicator in ordered_indicators(analysis, table):
        subset = [r for r in rows if r["indicator"] == indicator]
        last = max(r["checkpoint_trajectories"] for r in subset)
        pairs = [r for r in subset if r["checkpoint_trajectories"] == last]
        keys = [(r["factor_a"], r["factor_b"]) for r in pairs]
        if len(set(keys)) != len(keys):
            raise FigureNotApplicable(
                f"rankReversals has several rows per factor pair for {indicator!r}"
                f" at checkpoint {last}"
            )
        order = _factor_order(pairs)
        index = {name: i for i, name in enumerate(order)}
        size = len(order)
        values = np.ma.masked_all((size, size), dtype=float)
        unavailable: list[str] = []
        for pair in pairs:
            probability = pair["reversal_probability"]
            if _finite(probability):
                values[index[pair["factor_a"]], index[pair["factor_b"]]] = probability
            else:
                unavailable.append(f"{pair['factor_a']}/{pair['factor_b']}")
        figure, ax = _new_figure(7, 6)
        figure.subplots_adjust(left=0.2, right=0.85, top=0.88, bottom=0.22)
        cmap = copy.copy(colormaps["viridis"])
        cmap.set_bad("lightgrey")
        image = ax.imshow(values, cmap=cmap, vmin=0.0, vmax=1.0)
        ax.set_xticks(range(size), labels=order, rotation=60, ha="right", fontsize=7)
        ax.set_yticks(range(size), labels=order, fontsize=7)
        for pair in pairs:
            probability = pair["reversal_probability"]
            ax.text(
                index[pair["factor_b"]],
                index[pair["factor_a"]],
                f"{probability:.2f}" if _finite(probability) else "n/a",
                ha="center",
                va="center",
                fontsize=6,
                color="white" if _finite(probability) and probability < 0.5 else "black",
            )
        figure.colorbar(image, ax=ax, shrink=0.8, label="P(ordre de mu* inversé)")
        ax.set_xlabel("facteur b (classé plus bas)")
        ax.set_ylabel("facteur a (classé plus haut)")
        ax.set_title(
            f"inversion de l'ordre de mu* par paire — {last} trajectoires", fontsize=9
        )
        note = indicator
        if unavailable:
            note += " · n/a : " + ", ".join(unavailable)
        _note(figure, note)
        stamp(figure, analysis)
        figures.append(
            RenderedFigure(
                name="morris-rank-reversals",
                indicator=indicator,
                figure=figure,
                sources=("rankReversals", "governedReport"),
                omitted=tuple(unavailable),
            )
        )
    return figures


def render_morris_noise_floor(analysis: PublishedAnalysis) -> list[RenderedFigure]:
    table = analysis.table("noiseFloor")
    rows = table.to_pylist()
    allocation = (
        analysis.document("replicationAllocation")
        if "replicationAllocation" in analysis.outputs
        else None
    )
    figures: list[RenderedFigure] = []
    for indicator in ordered_indicators(analysis, table):
        subset = [r for r in rows if r["indicator"] == indicator]
        figure = Figure(figsize=(8, 6.5))
        ax_sd, ax_mcse = figure.subplots(2, 1, sharex=True)
        figure.subplots_adjust(left=0.12, right=0.95, top=0.9, bottom=0.17, hspace=0.18)
        omitted: list[str] = []
        for trajectory in sorted({r["trajectory"] for r in subset}):
            series = sorted(
                (r for r in subset if r["trajectory"] == trajectory),
                key=lambda r: r["rank"],
            )
            for ax, column in ((ax_sd, "intrinsic_sd"), (ax_mcse, "mcse")):
                usable = [r for r in series if _finite(r[column])]
                if column == "intrinsic_sd":
                    omitted.extend(
                        f"trajectoire {trajectory} rang {r['rank']}"
                        for r in series
                        if not _finite(r[column])
                    )
                if usable:
                    ax.plot(
                        [r["rank"] for r in usable],
                        [r[column] for r in usable],
                        "o-",
                        markersize=3,
                        label=f"trajectoire {trajectory}",
                    )
        ax_sd.set_ylabel("écart-type intrinsèque")
        ax_mcse.set_ylabel("erreur-type Monte-Carlo de la moyenne")
        ax_mcse.set_xlabel(f"rang du point dans la trajectoire ({indicator})")
        ax_sd.set_title("bruit intrinsèque par point", fontsize=10)
        sources = ["noiseFloor", "governedReport"]
        if allocation is not None and allocation.get("primaryIndicator") == indicator:
            target = allocation.get("targetMcse")
            if _finite(target):
                ax_mcse.axhline(
                    target, linestyle=":", color="black", label="cible MCSE"
                )
                sources.insert(1, "replicationAllocation")
        for ax in (ax_sd, ax_mcse):
            if ax.get_legend_handles_labels()[0]:
                ax.legend(fontsize=7, loc="best")
        if omitted:
            _note(figure, "non tracé (écart-type indisponible) : " + ", ".join(omitted))
        stamp(figure, analysis)
        figures.append(
            RenderedFigure(
                name="morris-noise-floor",
                indicator=indicator,
                figure=figure,
                sources=tuple(sources),
                omitted=tuple(omitted),
            )
        )
    return figures
