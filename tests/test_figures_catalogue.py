import pytest

from eqasim_sa.core.figures.catalogue import FIGURES, applicable_figures, select_figures


def test_descriptive_grid_only_gets_summary_and_response():
    names = [f.name for f in applicable_figures("grid", frozenset({"outputSummary", "dispersion", "governedReport"}))]
    assert names == ["output-summary", "grid-response"]


def test_descriptive_morris_has_no_sensitivity_figure():
    names = [f.name for f in applicable_figures("canonical_morris", frozenset({"outputSummary", "governedReport"}))]
    assert names == ["output-summary"]


def test_full_morris():
    roles = frozenset({"outputSummary", "governedReport", "sensitivity", "convergence", "rankReversals", "noiseFloor"})
    assert [f.name for f in applicable_figures("canonical_morris", roles)] == [
        "output-summary", "morris-plane", "morris-convergence", "morris-rank-reversals", "morris-noise-floor"]


def test_requesting_invalid_figure_lists_valid_ones():
    with pytest.raises(ValueError, match=r"'morris-plane' is not valid for method 'sobol'; valid: output-summary, sobol-indices"):
        select_figures("sobol", frozenset({"outputSummary", "sensitivity", "governedReport"}), ("morris-plane",))


def test_unknown_method_is_rejected():
    with pytest.raises(ValueError, match="unsupported method"):
        applicable_figures("tornado", frozenset())


def test_select_empty_means_all_applicable_and_requested_subset():
    roles = frozenset({"outputSummary", "sensitivity"})
    assert [f.name for f in select_figures("sobol", roles, ())] == ["output-summary", "sobol-indices"]
    assert [f.name for f in select_figures("sobol", roles, ("sobol-indices",))] == ["sobol-indices"]


def test_catalogue_has_eleven_figures_in_matrix_order():
    assert [f.name for f in FIGURES] == [
        "output-summary", "grid-response", "oat-effects", "sobol-indices", "morris-plane",
        "morris-convergence", "morris-rank-reversals", "morris-noise-floor",
        "multiscramble-indices", "multiscramble-convergence", "multiscramble-by-scramble"]


def test_multiscramble_convergence_needs_governed_report():
    base = frozenset({"outputSummary", "convergence"})
    assert "multiscramble-convergence" not in [f.name for f in applicable_figures("multiscramble_sobol", base)]
    assert "multiscramble-convergence" in [f.name for f in applicable_figures("multiscramble_sobol", base | {"governedReport"})]
