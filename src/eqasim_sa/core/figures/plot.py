"""Plot orchestration: select, render everything in memory, then publish as a whole."""

import io
import json
import re
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.transforms import Bbox

from eqasim_sa.core.figures.catalogue import select_figures
from eqasim_sa.core.figures.common import (
    FigureNotApplicable,
    RenderedFigure,
    inference_status,
)
from eqasim_sa.core.figures.published import PublishedAnalysis, load_published_analysis
from eqasim_sa.core.figures.render_grid import render_grid_response
from eqasim_sa.core.figures.render_morris import (
    render_morris_convergence,
    render_morris_noise_floor,
    render_morris_plane,
    render_morris_rank_reversals,
)
from eqasim_sa.core.figures.render_multiscramble import (
    render_multiscramble_by_scramble,
    render_multiscramble_convergence,
    render_multiscramble_indices,
)
from eqasim_sa.core.figures.render_oat import render_oat_effects
from eqasim_sa.core.figures.render_sobol import render_sobol_indices
from eqasim_sa.core.figures.render_summary import render_output_summary
from eqasim_sa.utils.filesystem import write_bytes_atomic
from eqasim_sa.utils.platform import remove_tree, rename_with_retry

RENDERERS: Mapping[str, Callable[[PublishedAnalysis], list[RenderedFigure]]] = {
    "output-summary": render_output_summary,
    "grid-response": render_grid_response,
    "oat-effects": render_oat_effects,
    "sobol-indices": render_sobol_indices,
    "morris-plane": render_morris_plane,
    "morris-convergence": render_morris_convergence,
    "morris-rank-reversals": render_morris_rank_reversals,
    "morris-noise-floor": render_morris_noise_floor,
    "multiscramble-indices": render_multiscramble_indices,
    "multiscramble-convergence": render_multiscramble_convergence,
    "multiscramble-by-scramble": render_multiscramble_by_scramble,
}

FIGURES_DIRECTORY = "figures"
INDEX_NAME = "figures.json"


def _file_name(name: str, indicator: str | None) -> str:
    if indicator is None:
        return f"{name}.png"
    return f"{name}-{re.sub(r'[^A-Za-z0-9_-]', '_', indicator)}.png"


def _saved_box(figure: Figure) -> Bbox:
    """The canvas to save, in inches: it only grows, never crops.

    It is the union of the figure itself, matplotlib's tight box (long tick
    labels, the stamp) and every axis label, whose part beyond its axes the
    tight box ignores; plus a 0.1 inch margin.
    """
    canvas = FigureCanvasAgg(figure)
    canvas.draw()
    renderer = canvas.get_renderer()
    to_inches = figure.dpi_scale_trans.inverted()
    boxes = [figure.bbox_inches, figure.get_tightbbox(renderer)]
    for ax in figure.axes:
        for label in (ax.xaxis.label, ax.yaxis.label):
            if label.get_visible() and label.get_text():
                boxes.append(label.get_window_extent(renderer).transformed(to_inches))
    return Bbox.union(boxes).padded(0.1)


def _png(figure: Figure) -> bytes:
    buffer = io.BytesIO()
    figure.savefig(
        buffer,
        format="png",
        dpi=150,
        metadata={"Software": None},
        bbox_inches=_saved_box(figure),
    )
    return buffer.getvalue()


def _remove_best_effort(path: Path) -> None:
    if not path.exists():
        return
    try:
        remove_tree(path)
    except OSError:
        pass  # a leftover hidden directory is harmless and accepted by init


def plot_campaign(campaign_root: Path, requested: tuple[str, ...]) -> dict:
    analysis = load_published_analysis(campaign_root)
    specs = select_figures(analysis.method, frozenset(analysis.outputs), requested)
    explicit = set(requested)

    # Everything is rendered and encoded in memory first: any error writes nothing.
    skipped: list[dict] = []
    payloads: dict[str, bytes] = {}
    entries: list[dict] = []
    for spec in specs:
        try:
            rendered = RENDERERS[spec.name](analysis)
        except FigureNotApplicable as error:
            if spec.name in explicit:
                raise
            skipped.append({"name": spec.name, "reason": str(error)})
            continue
        for item in rendered:
            file = _file_name(item.name, item.indicator)
            if file in payloads:
                raise ValueError(
                    f"figure {item.name!r} for indicator {item.indicator!r} "
                    f"maps to the already used file name {file!r}"
                )
            payloads[file] = _png(item.figure)
            entries.append(
                {
                    "name": item.name,
                    "indicator": item.indicator,
                    "file": file,
                    "sources": [
                        {"role": role, "sha256": analysis.source_sha256(role)}
                        for role in item.sources
                    ],
                    "omitted": list(item.omitted),
                }
            )
    index = {
        "generationId": analysis.generation_id,
        "method": analysis.method,
        "inferenceStatus": inference_status(analysis),
        "figures": entries,
        "skipped": skipped,
    }
    index_bytes = (
        json.dumps(index, sort_keys=True, allow_nan=False, indent=2) + "\n"
    ).encode("utf-8")

    parent = analysis.campaign_root / FIGURES_DIRECTORY
    target = parent / analysis.generation_id
    parent.mkdir(exist_ok=True)
    staging = parent / f".staging-{uuid.uuid4().hex}"
    staging.mkdir()
    retired = parent / f".retired-{uuid.uuid4().hex}"
    try:
        for file, data in payloads.items():
            write_bytes_atomic(data, staging / file)
        write_bytes_atomic(index_bytes, staging / INDEX_NAME)
        if target.exists():
            rename_with_retry(target, retired)
        rename_with_retry(staging, target)
    except BaseException:
        _remove_best_effort(staging)
        if retired.exists() and not target.exists():
            try:
                rename_with_retry(retired, target)
            except OSError:
                pass  # the original error matters more; .retired-* stays
        raise
    _remove_best_effort(retired)
    return {
        "status": "completed",
        "generationId": analysis.generation_id,
        "directory": str(target),
        "figures": entries,
        "skipped": skipped,
    }
