import pyarrow as pa
import pytest
from matplotlib.collections import LineCollection
from matplotlib.figure import Figure

from eqasim_sa.core.figures.common import FigureNotApplicable
from eqasim_sa.core.figures.render_grid import render_grid_response
from eqasim_sa.core.figures.render_oat import render_oat_effects
from eqasim_sa.core.figures.render_sobol import render_sobol_indices
from tests.figures_helpers import StubAnalysis


def _stub(method, role, table):
    return StubAnalysis(method, "descriptive_noninferential", {}, {role: table})


def _texts(fig: Figure) -> str:
    return " ".join(t.get_text() for t in fig.texts)


def _dispersion(n=3, params=None):
    cols = params or {"a": [1, 1, 2, 2], "b": [10, 20, 10, 20]}
    return pa.table(
        {
            **cols,
            "indicator": ["x"] * 4,
            "n": [n] * 4,
            "mean": [1.0, 2.0, 3.0, 4.0],
            "std": [0.1] * 4,
            "cv": [0.1] * 4,
            "ci95_low": [0.5, 1.5, 2.5, 3.5],
            "ci95_high": [1.5, 2.5, 3.5, 4.5],
        }
    )


def _segments(ax):
    out = []
    for c in ax.collections:
        if isinstance(c, LineCollection):
            for seg in c.get_segments():
                out.append((float(seg[0][1]), float(seg[1][1])))
    return out


def _all_titles(fig: Figure) -> str:
    sup = fig._suptitle.get_text() if fig._suptitle else ""
    return " ".join(a.get_title() for a in fig.axes) + _texts(fig) + sup


def test_grid_two_parameters_draws_one_line_per_other_value():
    (rf,) = render_grid_response(_stub("grid", "dispersion", _dispersion()))
    assert rf.indicator == "x"
    assert len(rf.figure.axes) == 2
    ax_a = next(a for a in rf.figure.axes if a.get_xlabel() == "a")
    labelled = [t.get_text() for t in ax_a.get_legend().get_texts()]
    assert sorted(labelled) == ["b=10", "b=20"]
    assert len(_segments(ax_a)) == 4
    assert "governedReport" in rf.sources


def test_grid_single_replication_has_no_errorbars():
    (rf,) = render_grid_response(_stub("grid", "dispersion", _dispersion(n=1)))
    for ax in rf.figure.axes:
        assert _segments(ax) == []
        assert len(ax.get_lines()) == 2
    assert "1 réplication" in _all_titles(rf.figure)


def test_grid_no_numeric_parameter_not_applicable():
    table = _dispersion(params={"a": ["u", "u", "v", "v"], "b": ["p", "q", "p", "q"]})
    with pytest.raises(FigureNotApplicable):
        render_grid_response(_stub("grid", "dispersion", table))


def test_grid_string_parameter_is_never_an_axis():
    table = _dispersion(params={"a": [1, 1, 2, 2], "s": ["p", "q", "p", "q"]})
    (rf,) = render_grid_response(_stub("grid", "dispersion", table))
    assert [a.get_xlabel() for a in rf.figure.axes] == ["a"]


def _oat():
    return pa.table(
        {
            "indicator": ["x"] * 3,
            "factor": ["f1", "f2", "f3"],
            "rank": [3, 1, 2],
            "trajectories": [1] * 3,
            "replications": [2] * 3,
            "n": [4] * 3,
            "mu": [0.1, -0.5, 0.2],
            "mu_star": [0.1, 0.5, 0.2],
            "sigma": [0.0, None, 0.1],
            "seed_std": [0.05, 0.05, None],
        }
    )


def test_oat_bar_heights_equal_mu_star():
    (rf,) = render_oat_effects(_stub("discrete_oat", "sensitivity", _oat()))
    ax = rf.figure.axes[0]
    assert [p.get_width() for p in ax.patches] == [0.5, 0.2, 0.1]


def test_oat_title_says_local():
    (rf,) = render_oat_effects(_stub("discrete_oat", "sensitivity", _oat()))
    assert "effets OAT locaux normalisés, non globaux" in rf.figure.axes[0].get_title()


def test_oat_unusable_mu_star_omitted():
    t = _oat()
    t = t.set_column(
        t.schema.get_field_index("mu_star"),
        "mu_star",
        pa.array([0.1, None, 0.2], pa.float64()),
    )
    (rf,) = render_oat_effects(_stub("discrete_oat", "sensitivity", t))
    assert rf.omitted == ("f2",)
    assert len(rf.figure.axes[0].patches) == 2


def _sobol():
    return pa.table(
        {
            "indicator": ["x"] * 2,
            "factor": ["p", "q"],
            "rank": [1, 2],
            "base_sample_size": [8] * 2,
            "bootstrap_resamples": [100] * 2,
            "bootstrap_attempts": [100] * 2,
            "first_order": [0.6, 0.1],
            "first_order_ci_low": [0.5, 0.0],
            "first_order_ci_high": [0.7, 0.2],
            "total_order": [0.8, 0.2],
            "total_order_ci_low": [0.7, 0.1],
            "total_order_ci_high": [0.9, 0.3],
        }
    )


def test_sobol_banner_says_demonstration():
    (rf,) = render_sobol_indices(_stub("sobol", "sensitivity", _sobol()))
    assert "démonstration : un seul scramble, IC non inférentiels" in _texts(rf.figure)


def test_sobol_bars_match_first_and_total_order():
    (rf,) = render_sobol_indices(_stub("sobol", "sensitivity", _sobol()))
    ax = rf.figure.axes[0]
    assert [p.get_height() for p in ax.patches] == [0.6, 0.1, 0.8, 0.2]
    assert len(_segments(ax)) == 4


def test_sobol_non_bracketing_ci_drawn_exactly():
    t = _sobol()
    t = t.set_column(
        t.schema.get_field_index("first_order"), "first_order",
        pa.array([0.5, 0.1], pa.float64()),
    )
    t = t.set_column(
        t.schema.get_field_index("first_order_ci_low"), "first_order_ci_low",
        pa.array([0.6, 0.0], pa.float64()),
    )
    t = t.set_column(
        t.schema.get_field_index("first_order_ci_high"), "first_order_ci_high",
        pa.array([0.8, 0.2], pa.float64()),
    )
    (rf,) = render_sobol_indices(_stub("sobol", "sensitivity", t))
    assert (0.6, 0.8) in [
        (round(a, 12), round(b, 12)) for a, b in _segments(rf.figure.axes[0])
    ]


def test_grid_non_bracketing_ci_drawn_exactly():
    t = _dispersion()
    t = t.set_column(
        t.schema.get_field_index("ci95_low"), "ci95_low",
        pa.array([1.5, 1.5, 2.5, 3.5], pa.float64()),
    )
    t = t.set_column(
        t.schema.get_field_index("ci95_high"), "ci95_high",
        pa.array([1.8, 2.5, 3.5, 4.5], pa.float64()),
    )
    (rf,) = render_grid_response(_stub("grid", "dispersion", t))
    ax = next(a for a in rf.figure.axes if a.get_xlabel() == "a")
    assert (1.5, 1.8) in [(round(a, 12), round(b, 12)) for a, b in _segments(ax)]
