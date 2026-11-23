from pathlib import Path

import pyarrow as pa
import pytest
from matplotlib.figure import Figure

from eqasim_sa.core.analyze import analyze
from eqasim_sa.core.figures.common import ordered_indicators, stamp
from eqasim_sa.core.figures.published import load_published_analysis
from eqasim_sa.core.figures.render_summary import render_output_summary
from tests.figures_helpers import StubAnalysis
from tests.test_analyze import _age_indicator
from tests.test_governed_reporting import _grid_with_duplicate_positions


@pytest.fixture
def grid_analysis(tmp_path: Path, synthetic_indicator):
    root = _grid_with_duplicate_positions(tmp_path)
    analyze(root, (_age_indicator(),), campaign="test", completeness_threshold=1.0)
    return load_published_analysis(root)


def _texts(fig: Figure) -> str:
    return " ".join(t.get_text() for t in fig.texts)


def _summary_table() -> pa.Table:
    return pa.table(
        {
            "indicator": ["b", "a", "empty"],
            "n": [3, 3, 0],
            "mean": [2.0, 1.0, None],
            "standardDeviation": [0.5, 0.5, None],
            "minimum": [1.0, 0.0, None],
            "q05": [1.1, 0.1, None],
            "median": [2.0, 1.0, None],
            "q95": [2.9, 1.9, None],
            "maximum": [3.0, 2.0, None],
        }
    )


def _stub(status="demonstration_noninferential", reasons=None) -> StubAnalysis:
    return StubAnalysis(
        method="grid",
        inference_status=status,
        governed_report={
            "method": "grid",
            "inferenceStatus": status,
            "claimAuthorized": False,
            "reasonCodes": reasons if reasons is not None else [],
        },
        tables={"outputSummary": _summary_table()},
        generation_id="abcdef0123456789" + "0" * 48,
    )


def test_stamp_contains_governance_line(grid_analysis) -> None:
    fig = Figure()
    stamp(fig, grid_analysis)
    texts = _texts(fig)
    assert "grid" in texts and "demonstration_noninferential" in texts
    assert "claimAuthorized=false" in texts
    assert grid_analysis.generation_id[:12] in texts
    assert "campagne incomplète" not in texts


def test_stamp_exact_format() -> None:
    fig = Figure()
    stamp(fig, _stub(reasons=["x", "y"]))
    assert (
        "grid · demonstration_noninferential · claimAuthorized=false · x, y"
        " · génération abcdef012345" in _texts(fig)
    )
    fig = Figure()
    stamp(fig, _stub())
    assert "claimAuthorized=false · none · génération" in _texts(fig)


def test_incomplete_campaign_gets_banner() -> None:
    fig = Figure()
    stamp(fig, _stub(status="incomplete_noninferential"))
    banner = [t for t in fig.texts if "campagne incomplète" in t.get_text()]
    assert banner and banner[0].get_color() == "red"


def test_ordered_indicators() -> None:
    assert ordered_indicators(_summary_table()) == ["a", "b", "empty"]
    table = pa.table(
        {"indicator": ["a", "b", "c"], "is_primary_indicator": [False, False, True]}
    )
    assert ordered_indicators(table) == ["c", "a", "b"]


def test_output_summary_caption_is_not_uncertainty(grid_analysis) -> None:
    (rendered,) = render_output_summary(grid_analysis)
    title = rendered.figure.axes[0].get_title()
    assert "distribution sur les positions du plan" in title
    assert "incertitude" not in title
    assert rendered.name == "output-summary"
    assert rendered.indicator is None
    assert "grid" in _texts(rendered.figure)


def test_null_median_is_omitted() -> None:
    (rendered,) = render_output_summary(_stub())
    assert rendered.omitted == ("empty",)
    assert rendered.sources == ("outputSummary", "governedReport")
    assert "non tracé : empty" in _texts(rendered.figure)
    labels = [ax.get_yticklabels()[0].get_text() for ax in rendered.figure.axes]
    assert labels == ["a", "b"]


def test_one_axis_per_indicator_with_own_scale() -> None:
    (rendered,) = render_output_summary(_stub())
    first, second = rendered.figure.axes
    assert first.get_xlim() != second.get_xlim()


def test_status_only_in_governed_report_triggers_banner() -> None:
    analysis = _stub(status="incomplete_noninferential")
    analysis.inference_status = None
    fig = Figure()
    stamp(fig, analysis)
    assert "campagne incomplète" in _texts(fig)
    assert "incomplete_noninferential" in _texts(fig)
