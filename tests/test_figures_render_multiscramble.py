import copy
from dataclasses import replace

import pyarrow as pa
import pytest
from matplotlib.collections import LineCollection, PathCollection

from eqasim_sa.core.analyze import analyze
from eqasim_sa.core.figures.published import load_published_analysis
from eqasim_sa.core.figures.render_multiscramble import (
    render_multiscramble_by_scramble,
    render_multiscramble_convergence,
    render_multiscramble_indices,
)
from tests.figures_helpers import StubAnalysis
from tests.provenance_helpers import scientific_indicator
from tests.test_governed_reporting import _complete_multiscramble
from tests.test_multiscramble_sobol_plan import multiscramble_design


def _row(indicator, factor, cp, fw, tw, actionable=True, objective="ff", primary=True, rank=1.0):
    r = {
        "indicator": indicator,
        "factor": factor,
        "checkpoint": cp,
        "scrambles": cp,
        "confidence_level": 0.95,
        "is_primary_indicator": primary,
        "is_actionable": actionable,
        "objective": objective,
        "within_practical_tolerance": None,
        "rank": rank if actionable else None,
        "first_order_change": None,
        "total_order_change": None,
    }
    for key, w in (("first_order", fw), ("total_order", tw)):
        r.update(
            {
                f"{key}_mean": 0.3,
                f"{key}_sd": 0.1,
                f"{key}_se": 0.05,
                f"{key}_ci_low": 0.3 - w / 2,
                f"{key}_ci_high": 0.3 + w / 2,
                f"{key}_ci_width": w,
            }
        )
    return r


def _stub(rows, criteria=None, role="convergence"):
    report = {"inferenceStatus": "conditional"}
    if criteria is not None:
        report["convergence"] = {"criteria": criteria}
    return StubAnalysis(
        "multiscramble_sobol", "conditional", report, {role: pa.Table.from_pylist(rows)}
    )


def _indices_rows():
    return [
        _row("x", "a", 2, 0.1, 0.1),
        _row("x", "a", 4, 0.2, 0.2, rank=1.0),
        _row("x", "b", 4, 0.2, 0.2, rank=2.0),
        _row("x", "seed", 4, 0.2, 0.2, actionable=False),
    ]


def test_nonactionable_seed_is_hatched_and_unranked():
    fig = render_multiscramble_indices(_stub(_indices_rows(), role="sensitivity"))[0].figure
    ax = fig.axes[0]
    labels = [t.get_text() for t in ax.get_xticklabels()]
    ticks = list(ax.get_xticks())
    seed_x = ticks[labels.index("seed")]
    seed_bars = [p for p in ax.patches if abs(p.get_x() + p.get_width() / 2 - seed_x) < 0.3]
    assert seed_bars and all(p.get_hatch() for p in seed_bars)
    other = [p for p in ax.patches if p not in seed_bars]
    assert other and not any(p.get_hatch() for p in other)
    assert [t.get_text() for t in ax.texts] == ["rang 1", "rang 2"]
    assert labels == ["a", "b", "seed"]
    assert ticks[2] - ticks[1] > 1.0


def test_indices_use_last_checkpoint():
    ax = render_multiscramble_indices(_stub(_indices_rows(), role="sensitivity"))[0].figure.axes[0]
    segs = [s for c in ax.collections if isinstance(c, LineCollection) for s in c.get_segments()]
    assert len(segs) == 6
    assert all(abs((s[1][1] - s[0][1]) - 0.2) < 1e-9 for s in segs)


def _conv_rows(objective="ff", fw=(0.4, 0.2), tw=(0.1, 0.05)):
    rows = []
    for i, cp in enumerate((2, 4)):
        rows.append(_row("x", "a", cp, fw[i], tw[i], objective=objective))
        rows.append(_row("x", "seed", cp, 9.0, 9.0, actionable=False, objective=objective))
    return rows


@pytest.mark.parametrize("objective,expected", [("fp", [0.4, 0.2]), ("ff", [0.1, 0.05])])
def test_convergence_uses_objective_order_and_excludes_seed(objective, expected):
    ax = render_multiscramble_convergence(_stub(_conv_rows(objective)))[0].figure.axes[0]
    lines = {ln.get_label(): ln for ln in ax.get_lines()}
    assert list(lines["max (critère)"].get_ydata()) == pytest.approx(expected)
    assert list(lines["a"].get_ydata()) == pytest.approx(expected)
    seed = [ln for lab, ln in lines.items() if lab.startswith("seed")][0]
    assert seed.get_linestyle() == "--"
    assert max(seed.get_ydata()) == 9.0
    assert ("premier" if objective == "fp" else "total") in ax.get_ylabel()


def _threshold_lines(ax):
    return [ln for ln in ax.get_lines() if ln.get_label().startswith("seuil")]


def test_convergence_draws_ci_width_threshold_when_declared():
    crit = [{"metric": "ci_width", "threshold": 0.1, "consecutiveChecks": 2}]
    ax = render_multiscramble_convergence(_stub(_conv_rows(), crit))[0].figure.axes[0]
    (line,) = _threshold_lines(ax)
    assert list(line.get_ydata()) == [0.1, 0.1]
    assert "0.1" in line.get_label()


def test_convergence_without_ci_width_criterion_has_no_threshold_line():
    crit = [{"metric": "rank_stability", "threshold": 0.1, "consecutiveChecks": 2}]
    ax = render_multiscramble_convergence(_stub(_conv_rows(), crit))[0].figure.axes[0]
    assert not _threshold_lines(ax)
    ax = render_multiscramble_convergence(_stub(_conv_rows()))[0].figure.axes[0]
    assert not _threshold_lines(ax)


def test_convergence_threshold_only_on_primary_indicator():
    rows = _conv_rows() + [
        {**r, "indicator": "y", "is_primary_indicator": False} for r in _conv_rows()
    ]
    crit = [{"metric": "ci_width", "threshold": 0.1, "consecutiveChecks": 2}]
    figs = render_multiscramble_convergence(_stub(rows, crit))
    assert [f.indicator for f in figs] == ["x", "y"]
    assert [bool(_threshold_lines(f.figure.axes[0])) for f in figs] == [True, False]


def _by_rows():
    rows = []
    for cp in (2, 4):
        for s in range(cp):
            for factor, act in (("a", True), ("seed", False)):
                rows.append(
                    {
                        "indicator": "x",
                        "checkpoint": cp,
                        "scramble": s,
                        "factor": factor,
                        "is_actionable": act,
                        "first_order": 0.1 * s,
                        "total_order": 0.2 * s,
                        "output_variance": 1.0,
                        "effective_contrasts": 3,
                        "effective_contrast_rate": 1.0,
                        "ST_minus_S": 0.1,
                        "first_order_out_of_range": False,
                        "total_order_out_of_range": False,
                    }
                )
    return rows


def test_by_scramble_one_point_per_scramble():
    ax = render_multiscramble_by_scramble(_stub(_by_rows(), role="byScramble"))[0].figure.axes[0]
    counts = [len(c.get_offsets()) for c in ax.collections if isinstance(c, PathCollection)]
    assert counts == [8, 8]  # 2 factors x 4 scrambles, per order


@pytest.mark.usefixtures("synthetic_indicator")
def test_multiscramble_end_to_end(tmp_path):
    original = multiscramble_design(tmp_path / "unused")
    contract = copy.deepcopy(original.scientific_contract)
    contract["charter"]["stopping"] = [
        {"metric": "ci_width", "threshold": 1e300, "consecutive_checks": 2}
    ]
    design = replace(original, campaign_root=tmp_path / "campaign", scientific_contract=contract)
    _complete_multiscramble(design)
    analyze(
        design.campaign_root,
        (scientific_indicator("age"),),
        campaign="test",
        completeness_threshold=1.0,
        seed_role="factor",
        method="multiscramble_sobol",
        confidence_level=design.confidence_level,
    )
    analysis = load_published_analysis(design.campaign_root)
    for render in (
        render_multiscramble_indices,
        render_multiscramble_convergence,
        render_multiscramble_by_scramble,
    ):
        figs = render(analysis)
        assert len(figs) == 1 and figs[0].indicator.startswith("tv_distance(age")
        assert "governedReport" in figs[0].sources


def test_convergence_x_axis_is_base_sample_size():
    # The producer iterates checkpoints over baseSampleSizes with a fixed
    # number of scrambles: the x axis is N, not a count of scrambles.
    ax = render_multiscramble_convergence(_stub(_conv_rows()))[0].figure.axes[0]
    assert ax.get_xlabel() == "taille d'échantillon de base N au point de contrôle"


def test_indices_and_by_scramble_titles_name_base_sample_size():
    ax = render_multiscramble_indices(_stub(_indices_rows(), role="sensitivity"))[0].figure.axes[0]
    assert "N = 4" in ax.get_title()
    assert "point de contrôle 4" not in ax.get_title()
    ax = render_multiscramble_by_scramble(_stub(_by_rows(), role="byScramble"))[0].figure.axes[0]
    assert "N = 4" in ax.get_title()
    assert "point de contrôle 4" not in ax.get_title()


@pytest.mark.parametrize(
    "objective,rule",
    [
        ("ff", "rang ff : borne haute de ST croissante (rang 1 = meilleur candidat à fixer)"),
        ("fp", "rang fp : S1 moyen décroissant (rang 1 = facteur le plus influent)"),
    ],
)
def test_indices_title_states_ranking_rule(objective, rule):
    rows = [{**r, "objective": objective} for r in _indices_rows()]
    ax = render_multiscramble_indices(_stub(rows, role="sensitivity"))[0].figure.axes[0]
    assert rule in ax.get_title()


def test_indices_print_average_ranks_without_truncation():
    rows = _indices_rows()
    rows[1]["rank"] = 1.5
    rows[2]["rank"] = 1.5
    ax = render_multiscramble_indices(_stub(rows, role="sensitivity"))[0].figure.axes[0]
    assert [t.get_text() for t in ax.texts] == ["rang 1.5", "rang 1.5"]


def test_convergence_non_primary_indicator_has_no_criterion_wording():
    rows = _conv_rows() + [
        {**r, "indicator": "y", "is_primary_indicator": False} for r in _conv_rows()
    ]
    crit = [{"metric": "ci_width", "threshold": 0.1, "consecutiveChecks": 2}]
    primary, secondary = (
        f.figure.axes[0] for f in render_multiscramble_convergence(_stub(rows, crit))
    )
    labels = [ln.get_label() for ln in primary.get_lines()]
    assert "max (critère)" in labels
    assert "critère" in primary.get_title()
    labels = [ln.get_label() for ln in secondary.get_lines()]
    assert "max des facteurs actionnables" in labels
    assert "max (critère)" not in labels
    assert "critère" not in secondary.get_title()


def test_convergence_x_ticks_are_the_published_base_sample_sizes():
    ax = render_multiscramble_convergence(_stub(_conv_rows()))[0].figure.axes[0]
    assert [float(t) for t in ax.get_xticks()] == [2.0, 4.0]
    assert [t.get_text() for t in ax.get_xticklabels()] == ["2", "4"]
