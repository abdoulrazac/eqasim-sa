import pytest

from eqasim_sa.adapters.meta_config_defaults import (
    DEFAULT_INVOCATION, default_design_seed, resolve_meta_config_document,
)

MINIMAL = {
    "campaign": "demo",
    "model": {"root": "../model", "config": "../model/c.yml", "data_manifest": "m.json"},
    "experiment": {"method": "grid", "seeds": [1, 2]},
    "parameters": {"a": [1, 2], "b": {"values": ["x"]}},
    "outputs": {"age": {"category": "C", "simulated": "S", "references": ["R"]}},
}


def test_minimal_grid_is_completed():
    resolved = resolve_meta_config_document(MINIMAL)
    assert resolved["campaign_root"] == "../campaigns/demo"
    assert resolved["model"]["lockfile"] == "../model/uv.lock"
    assert resolved["parameters"] == {"a": {"values": [1, 2]}, "b": {"values": ["x"]}}
    assert resolved["analysis"] == {"completeness_threshold": 1.0}
    assert resolved["indicators"] == [{"tv_distance": {"outputs": ["age"]}}]
    assert "execution" not in resolved and "design_seed" not in resolved["experiment"]
    assert MINIMAL["parameters"]["a"] == [1, 2]  # input untouched


def test_method_specific_defaults():
    sobol = {**MINIMAL, "experiment": {"method": "sobol", "seeds": [1, 2], "base_sample_size": 4}}
    resolved = resolve_meta_config_document(sobol)
    assert resolved["experiment"]["design_seed"] == default_design_seed("demo")
    assert resolved["analysis"] == {
        "completeness_threshold": 1.0, "bootstrap_resamples": 999, "confidence_level": 0.95,
    }
    multi = {**MINIMAL, "experiment": {"method": "multiscramble_sobol", "seeds": [1, 2],
             "base_sample_sizes": [2, 4], "scramble_seeds": [1, 2, 3, 4], "seeds_provenance": "p"}}
    resolved = resolve_meta_config_document(multi)
    assert "design_seed" not in resolved["experiment"]
    assert resolved["analysis"] == {"completeness_threshold": 1.0, "confidence_level": 0.95}


def test_explicit_values_win():
    document = {**MINIMAL, "campaign_root": "x", "indicators": [],
                "analysis": {"completeness_threshold": 0.5}}
    resolved = resolve_meta_config_document(document)
    assert resolved["campaign_root"] == "x" and resolved["indicators"] == []
    assert resolved["analysis"] == {"completeness_threshold": 0.5}


def test_execution_defaults_only_when_block_present():
    resolved = resolve_meta_config_document({**MINIMAL, "execution": {"scratch": "s"}})
    assert resolved["execution"] == {
        "scratch": "s", "backend": "local", "workers": 1, "invocation": list(DEFAULT_INVOCATION),
    }


def test_design_seed_fits_signed_int64_and_is_stable():
    assert 0 <= default_design_seed("demo") < 2**63
    assert default_design_seed("demo") == default_design_seed("demo") != default_design_seed("demo2")


@pytest.mark.parametrize("document", [
    "grid", None, [],
    {**MINIMAL, "experiment": "grid"}, {**MINIMAL, "experiment": {"method": []}},
    {**MINIMAL, "experiment": {"method": {}}}, {**MINIMAL, "outputs": ["age"]},
    {**MINIMAL, "execution": None}, {**MINIMAL, "model": "x"}, {**MINIMAL, "parameters": 3},
])
def test_unexpected_types_pass_through_without_crashing(document):
    resolve_meta_config_document(document)  # no exception


def test_default_indicators_skip_outputs_without_references():
    outputs = {
        "age": {"category": "C", "simulated": "S", "references": ["R"]},
        "custom_only": {"category": "C", "simulated": "S", "references": []},
    }
    resolved = resolve_meta_config_document({**MINIMAL, "outputs": outputs})
    assert resolved["indicators"] == [{"tv_distance": {"outputs": ["age"]}}]
    only_empty = {"custom_only": outputs["custom_only"]}
    resolved = resolve_meta_config_document({**MINIMAL, "outputs": only_empty})
    assert resolved["indicators"] == []


def test_default_design_seed_is_pinned():
    # sha256(b"eqasim-sa:design-seed:v1\0" + campaign)[:8], big-endian, >> 1
    assert default_design_seed("demo") == 7846582582423296551
