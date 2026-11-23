from pathlib import Path

import pyarrow as pa
import pytest
from matplotlib.collections import LineCollection

from eqasim_sa.core.analyze import analyze
from eqasim_sa.core.figures.common import FigureNotApplicable
from eqasim_sa.core.figures.published import load_published_analysis
from eqasim_sa.core.figures.render_morris import (
    render_morris_convergence,
    render_morris_noise_floor,
    render_morris_plane,
    render_morris_rank_reversals,
)
from tests.figures_helpers import StubAnalysis
from tests.test_canonical_morris_analyze import _completed_campaign


def _stub(role, table, status="pilot_only"):
    return StubAnalysis("canonical_morris", status, {"inferenceStatus": status}, {role: table})


def _effects(checkpoint_column="trajectories", checkpoints=(4,)):
    rows = []
    for cp in checkpoints:
        for factor, mu_star, lo, hi, sigma in (
            ("a", 2.0, 1.5, 2.6, 1.0),
            ("b", 1.0, 0.2, 1.3, None),
            ("c", 0.5, 0.1, 0.9, 0.3),
        ):
            rows.append(
                {
                    "indicator": "x",
                    "factor": factor,
                    "rank": 1,
                    "rank_ci_low": 1.0,
                    "rank_ci_high": 2.0,
                    "top_k_probability": 0.5,
                    checkpoint_column: cp,
                    "replications": 2,
                    "elementary_effects": 8,
                    "delta": 0.5,
                    "mu": mu_star,
                    "mu_star": mu_star,
                    "mu_star_ci_low": lo,
                    "mu_star_ci_high": hi,
                    "sigma": sigma,
                    "seed_std": None,
                    "bootstrap_resamples": 100,
                    "confidence_level": 0.95,
                }
            )
    return pa.Table.from_pylist(rows)


def _hsegments(ax):
    out = []
    for c in ax.collections:
        if isinstance(c, LineCollection):
            for seg in c.get_segments():
                out.append(
                    (float(seg[0][0]), float(seg[1][0]), float(seg[0][1]), float(seg[1][1]))
                )
    return out


def test_plane_omits_factor_with_null_sigma():
    (rf,) = render_morris_plane(_stub("sensitivity", _effects()))
    assert rf.omitted == ("b",)
    ax = rf.figure.axes[0]
    points = next(l for l in ax.get_lines() if l.get_marker() == "o")
    assert list(points.get_xdata()) == [2.0, 0.5]
    assert list(points.get_ydata()) == [1.0, 0.3]
    assert "morris-plane" == rf.name
    assert rf.sources == ("sensitivity", "governedReport")


def test_plane_errorbars_use_mu_star_ci():
    (rf,) = render_morris_plane(_stub("sensitivity", _effects()))
    segs = _hsegments(rf.figure.axes[0])
    assert segs == [(1.5, 2.6, 1.0, 1.0), (0.1, 0.9, 0.3, 0.3)]


def test_plane_has_sigma_equals_mu_star_line():
    (rf,) = render_morris_plane(_stub("sensitivity", _effects()))
    diag = [
        l
        for l in rf.figure.axes[0].get_lines()
        if l.get_label() == "sigma = mu*"
    ]
    assert len(diag) == 1
    assert list(diag[0].get_xdata()) == list(diag[0].get_ydata())


def test_plane_stamps_incomplete_banner():
    (rf,) = render_morris_plane(
        _stub("sensitivity", _effects(), status="incomplete_noninferential")
    )
    assert any("campagne incomplète" in t.get_text() for t in rf.figure.texts)


def test_convergence_draws_published_ci_per_checkpoint():
    table = _effects("checkpoint_trajectories", (2, 4))
    (rf,) = render_morris_convergence(_stub("convergence", table))
    ax = rf.figure.axes[0]
    assert rf.sources == ("convergence", "governedReport")
    assert len(_hsegments(ax)) == 6
    assert (2.0, 2.0, 1.5, 2.6) in _hsegments(ax)
    assert sorted(l.get_label() for l in ax.get_lines()) == ["a", "b", "c"]


def _reversals(checkpoints=(2, 4), extra=None):
    rows = []
    for cp in checkpoints:
        # at checkpoint 4 the order is c, a, b; at 2 it is a, b, c
        order = ["a", "b", "c"] if cp == 2 else ["c", "a", "b"]
        for i, fa in enumerate(order):
            for fb in order[i + 1 :]:
                rows.append(
                    {
                        "indicator": "x",
                        "checkpoint_trajectories": cp,
                        "factor_a": fa,
                        "factor_b": fb,
                        "point_order": 1,
                        "reversal_probability": 0.1 * cp + (0.01 if fa == "c" else 0),
                        "tie_probability": 0.0,
                        "bootstrap_resamples": 100,
                    }
                )
    if extra:
        rows.extend(extra)
    return pa.Table.from_pylist(rows)


def _image(ax):
    return ax.images[0].get_array()


def test_rank_reversals_uses_last_checkpoint():
    (rf,) = render_morris_rank_reversals(_stub("rankReversals", _reversals()))
    ax = rf.figure.axes[0]
    labels = [t.get_text() for t in ax.get_yticklabels()]
    assert labels == ["c", "a", "b"]
    values = _image(ax)
    assert values[0, 1] == pytest.approx(0.41)  # c vs a at 4
    assert values[0, 2] == pytest.approx(0.41)
    assert values[1, 2] == pytest.approx(0.4)  # a vs b at 4
    assert values.mask[1, 0] and values.mask[0, 0]


def test_rank_reversals_none_probability_is_masked_not_zero():
    rows = _reversals(checkpoints=(4,)).to_pylist()
    rows[0]["reversal_probability"] = None
    rows[0]["point_order"] = 0
    (rf,) = render_morris_rank_reversals(
        _stub("rankReversals", pa.Table.from_pylist(rows, schema=_reversals().schema))
    )
    ax = rf.figure.axes[0]
    assert _image(ax).mask[0, 1]
    assert "n/a" in [t.get_text() for t in ax.texts]
    assert rf.omitted == ("c/a",)


def test_rank_reversals_duplicate_pair_not_applicable():
    base = _reversals(checkpoints=(4,))
    table = pa.concat_tables([base, base.slice(0, 1)])
    with pytest.raises(FigureNotApplicable):
        render_morris_rank_reversals(_stub("rankReversals", table))


def _noise():
    rows = []
    for trajectory in (0, 1):
        for rank in range(3):
            rows.append(
                {
                    "indicator": "x",
                    "trajectory": trajectory,
                    "rank": rank,
                    "role": "base",
                    "replications": 3,
                    "mean": 1.0,
                    "intrinsic_variance": 0.04,
                    "intrinsic_sd": 0.2,
                    "mcse": 0.05 if not (trajectory == 1 and rank == 2) else None,
                    "is_primary_indicator": True,
                }
            )
    rows[5]["intrinsic_sd"] = None
    return pa.Table.from_pylist(
        rows,
        schema=pa.schema(
            [
                ("indicator", pa.string()),
                ("trajectory", pa.int64()),
                ("rank", pa.int64()),
                ("role", pa.string()),
                ("replications", pa.int64()),
                ("mean", pa.float64()),
                ("intrinsic_variance", pa.float64()),
                ("intrinsic_sd", pa.float64()),
                ("mcse", pa.float64()),
                ("is_primary_indicator", pa.bool_()),
            ]
        ),
    )


def test_noise_floor_two_distinct_series_no_sd_pm_mcse():
    (rf,) = render_morris_noise_floor(_stub("noiseFloor", _noise()))
    ax_sd, ax_mcse = rf.figure.axes
    assert ax_sd.get_ylabel() == "écart-type intrinsèque"
    assert ax_mcse.get_ylabel() == "erreur-type Monte-Carlo de la moyenne"
    assert [l.get_label() for l in ax_sd.get_lines()] == ["trajectoire 0", "trajectoire 1"]
    assert [list(l.get_ydata()) for l in ax_sd.get_lines()] == [[0.2, 0.2, 0.2], [0.2, 0.2]]
    # mcse is None for trajectory 1 rank 2 (and rank 2 sd is None): masked in its series only
    assert [list(l.get_ydata()) for l in ax_mcse.get_lines()] == [
        [0.05, 0.05, 0.05],
        [0.05, 0.05],
    ]
    assert _hsegments(ax_sd) == [] and _hsegments(ax_mcse) == []
    assert rf.omitted == ("trajectoire 1 rang 2",)
    assert rf.sources == ("noiseFloor", "governedReport")


def _allocation_stub(primary):
    return StubAnalysis(
        "canonical_morris",
        "pilot_only",
        {"inferenceStatus": "pilot_only"},
        {"noiseFloor": _two_indicator_noise()},
        documents={"replicationAllocation": {"primaryIndicator": primary, "targetMcse": 0.03}},
    )


def _two_indicator_noise():
    t = _noise()
    other = t.set_column(0, "indicator", pa.array(["y"] * t.num_rows))
    return pa.concat_tables([t, other])


def test_noise_floor_target_mcse_on_primary_indicator_only():
    figs = {f.indicator: f for f in render_morris_noise_floor(_allocation_stub("y"))}
    assert "replicationAllocation" not in figs["x"].sources
    assert figs["y"].sources == ("noiseFloor", "replicationAllocation", "governedReport")
    assert [l.get_ydata()[0] for l in figs["x"].figure.axes[1].lines if l.get_label() == "cible MCSE"] == []
    target = [l for l in figs["y"].figure.axes[1].lines if l.get_label() == "cible MCSE"]
    assert len(target) == 1 and list(target[0].get_ydata()) == [0.03, 0.03]
    assert not [l for l in figs["y"].figure.axes[0].lines if l.get_label() == "cible MCSE"]


def test_noise_floor_no_target_when_allocation_not_published():
    (rf,) = render_morris_noise_floor(_stub("noiseFloor", _noise()))
    assert not [l for ax in rf.figure.axes for l in ax.lines if l.get_label() == "cible MCSE"]


@pytest.mark.usefixtures("synthetic_indicator")
def test_end_to_end_canonical_pilot(tmp_path: Path) -> None:
    design, indicator = _completed_campaign(tmp_path)
    analyze(
        design.campaign_root,
        (indicator,),
        campaign="test",
        completeness_threshold=1.0,
        method="canonical_morris",
        bootstrap_resamples=design.bootstrap_resamples,
        confidence_level=design.confidence_level,
        convergence_checkpoints=design.convergence_checkpoints,
        screening_top_k=design.screening_top_k,
    )
    analysis = load_published_analysis(design.campaign_root)
    for render in (
        render_morris_plane,
        render_morris_convergence,
        render_morris_rank_reversals,
        render_morris_noise_floor,
    ):
        figures = render(analysis)
        assert figures
        for rf in figures:
            texts = " ".join(t.get_text() for t in rf.figure.texts)
            assert "pilot_only" in texts
            assert "governedReport" in rf.sources
