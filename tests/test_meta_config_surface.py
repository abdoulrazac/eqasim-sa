"""User-facing surface of the simplified meta-config format."""

from pathlib import Path

import pytest
import yaml

from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.adapters.meta_config_defaults import DEFAULT_INVOCATION
from tests.test_campaign_run import add_execution, meta_fixture


def _edit(meta: Path, change) -> None:
    document = yaml.safe_load(meta.read_text())
    change(document)
    meta.write_text(yaml.safe_dump(document, sort_keys=False))


def _with_outputs(document: dict, **references: list[str]) -> None:
    document["outputs"] = {
        name: {"category": "c", "simulated": "s", "references": refs}
        for name, refs in references.items()
    }


def test_minimal_new_format_loads_with_defaults(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)

    loaded = load_meta_config(meta)

    assert loaded.seeds == (41, 42) and loaded.seed_role == "replication"
    assert loaded.completeness_threshold == 1.0
    assert loaded.campaign_root.name == "campaign"
    assert loaded.factors == {"a": [1, 2], "b": ["x", "y"]}
    assert loaded.indicators == () and loaded.artifacts == {}
    assert loaded.execution is None


def test_omitted_campaign_root_and_lockfile_use_defaults(tmp_path: Path) -> None:
    meta, model, _, _ = meta_fixture(tmp_path)
    (model / "uv.lock").write_text("locked\n")

    def change(document: dict) -> None:
        del document["campaign_root"]
        del document["model"]["lockfile"]

    _edit(meta, change)
    loaded = load_meta_config(meta)

    assert loaded.campaign_root == (tmp_path / ".." / "campaigns" / "demo").resolve()
    assert any(path == (model / "uv.lock").resolve() for path, _ in loaded.control_states)


def test_execution_defaults_fill_backend_workers_invocation(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    meta.write_text(meta.read_text() + f"execution:\n  scratch: {scratch}\n")

    execution = load_meta_config(meta).execution

    assert execution is not None
    assert execution.backend == "local" and execution.workers == 1
    assert execution.invocation == DEFAULT_INVOCATION


@pytest.mark.parametrize(
    ("change", "allowed"),
    [
        (
            lambda d: d.update(context={"data_manifest": "manifest.json"}),
            "meta-config keys must be among campaign, campaign_root, charter, model",
        ),
        (
            lambda d: d.update(artifacts={}),
            "meta-config keys must be among campaign, campaign_root, charter, model",
        ),
        (
            lambda d: d["experiment"].update(replications={"seeds": [41, 42]}),
            "experiment (grid) keys must be among method, seeds; unknown: ",
        ),
        (
            lambda d: d["experiment"].update(seed_role="replication"),
            "experiment (grid) keys must be among method, seeds; unknown: ",
        ),
        (
            lambda d: d.update(
                execution={"scratch": "scratch", "overrides": {"processes": 1}}
            ),
            "execution keys must be among backend, workers, scratch, invocation, "
            "machine_options, slurm",
        ),
        (
            lambda d: d.update(indicators=[{"pair": {"scalarizer": "tv_distance"}}]),
            "indicator 0 must contain exactly one key: one of tv_distance, custom",
        ),
        (
            lambda d: (
                _with_outputs(d, age=["INSEE"]),
                d["outputs"]["age"].update({"class": "c"}),
            ),
            "output 'age' keys must be exactly category, simulated, references",
        ),
        (
            lambda d: d["model"].update(extra="x"),
            "model keys must be among root, config, data_manifest, lockfile",
        ),
    ],
    ids=[
        "context",
        "artifacts",
        "replications",
        "seed_role",
        "overrides",
        "pair",
        "class",
        "model",
    ],
)
def test_old_keys_are_rejected(tmp_path: Path, change, allowed: str) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    (tmp_path / "scratch").mkdir()
    _edit(meta, change)

    with pytest.raises(ValueError) as caught:
        load_meta_config(meta)
    assert allowed in str(caught.value)


def test_missing_required_root_key_is_named(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    _edit(meta, lambda d: d.pop("experiment"))

    with pytest.raises(ValueError, match="missing required: experiment"):
        load_meta_config(meta)


def test_unsupported_method_lists_supported_methods_only(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    _edit(meta, lambda d: d["experiment"].update(method="morris"))

    with pytest.raises(ValueError) as caught:
        load_meta_config(meta)
    assert str(caught.value) == (
        "unsupported experiment.method 'morris'; expected one of grid, "
        "discrete_oat, canonical_morris, sobol, multiscramble_sobol"
    )


def test_parameters_seed_points_to_experiment_seeds(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    _edit(meta, lambda d: d["parameters"].update(seed=[1, 2]))

    with pytest.raises(ValueError, match=r"experiment\.seeds, not parameters\.seed"):
        load_meta_config(meta)


def test_scientific_shorthand_requires_distribution(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    _edit(meta, lambda d: d.update(charter={"status": "scientific"}))

    with pytest.raises(ValueError) as caught:
        load_meta_config(meta)
    assert str(caught.value) == (
        "factor 'a' requires a distribution under a scientific charter "
        "(use the long form with values and distribution)"
    )


def test_completeness_threshold_accepts_int_one_rejects_bool(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    _edit(meta, lambda d: d.update(analysis={"completeness_threshold": 1}))
    loaded = load_meta_config(meta)
    assert loaded.completeness_threshold == 1.0
    assert type(loaded.completeness_threshold) is float

    _edit(meta, lambda d: d.update(analysis={"completeness_threshold": 0}))
    assert load_meta_config(meta).completeness_threshold == 0.0

    _edit(meta, lambda d: d.update(analysis={"completeness_threshold": True}))
    with pytest.raises(ValueError, match="completeness_threshold"):
        load_meta_config(meta)

    _edit(meta, lambda d: d.update(analysis={"completeness_threshold": 2}))
    with pytest.raises(ValueError, match="completeness_threshold"):
        load_meta_config(meta)


def _indicator_names(meta: Path, change) -> set[str]:
    _edit(meta, change)
    return {item.name for item in load_meta_config(meta).indicators}


def test_indicator_forms_resolve_to_expected_names(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)

    def pair(document: dict) -> None:
        _with_outputs(document, age=["INSEE", "HTS"], distance=["HTS"])
        document["indicators"] = [
            {"tv_distance": {"output": "age", "reference": "INSEE"}}
        ]

    assert _indicator_names(meta, pair) == {"tv_distance(age, ref=INSEE)"}

    def single(document: dict) -> None:
        document["indicators"] = [{"tv_distance": {"output": "age"}}]

    assert _indicator_names(meta, single) == {
        "tv_distance(age, ref=INSEE)",
        "tv_distance(age, ref=HTS)",
    }

    def several(document: dict) -> None:
        document["indicators"] = [{"tv_distance": {"outputs": ["age", "distance"]}}]

    assert _indicator_names(meta, several) == {
        "tv_distance(age, ref=INSEE)",
        "tv_distance(age, ref=HTS)",
        "tv_distance(distance, ref=HTS)",
    }

    def omitted(document: dict) -> None:
        del document["indicators"]

    assert _indicator_names(meta, omitted) == {
        "tv_distance(age, ref=INSEE)",
        "tv_distance(age, ref=HTS)",
        "tv_distance(distance, ref=HTS)",
    }

    def disabled(document: dict) -> None:
        document["indicators"] = []

    assert _indicator_names(meta, disabled) == set()


def test_single_reference_output_form_matches_pair_name(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)

    def short(document: dict) -> None:
        _with_outputs(document, age=["INSEE"])
        document["indicators"] = [{"tv_distance": {"output": "age"}}]

    short_names = _indicator_names(meta, short)

    def pair(document: dict) -> None:
        document["indicators"] = [
            {"tv_distance": {"output": "age", "reference": "INSEE"}}
        ]

    assert short_names == _indicator_names(meta, pair) == {"tv_distance(age, ref=INSEE)"}


def test_indicator_entry_keys_are_strict(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)

    def change(document: dict) -> None:
        _with_outputs(document, age=["INSEE"])
        document["indicators"] = [{"tv_distance": {"artifact": "age"}}]

    _edit(meta, change)
    with pytest.raises(ValueError, match="output"):
        load_meta_config(meta)


def test_sobol_without_outputs_explains_outputs(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    _edit(
        meta,
        lambda d: d.update(
            experiment={
                "method": "sobol",
                "seeds": [101, 102],
                "base_sample_size": 2,
            }
        ),
    )

    with pytest.raises(ValueError, match="declare outputs"):
        load_meta_config(meta)


def test_sobol_seeds_become_the_seed_factor(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)

    def change(document: dict) -> None:
        document["experiment"] = {
            "method": "sobol",
            "seeds": [101, 102],
            "base_sample_size": 2,
        }
        _with_outputs(document, age=["INSEE"])

    _edit(meta, change)
    loaded = load_meta_config(meta)

    assert loaded.seed_role == "factor" and loaded.seeds == (101, 102)
    assert "seed" not in loaded.factors
    assert loaded.bootstrap_resamples == 999 and loaded.confidence_level == 0.95
    assert isinstance(loaded.design_seed, int)


def test_sobol_requires_two_seeds(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)

    def change(document: dict) -> None:
        document["experiment"] = {
            "method": "sobol",
            "seeds": [101],
            "base_sample_size": 2,
        }
        _with_outputs(document, age=["INSEE"])

    _edit(meta, change)
    with pytest.raises(
        ValueError, match="experiment.seeds requires at least two values for sobol"
    ):
        load_meta_config(meta)


def test_machine_options_replace_overrides(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    add_execution(meta, scratch, machine_options={"processes": 1})
    loaded = load_meta_config(meta)
    assert loaded.execution is not None
    assert loaded.execution.overrides == {"processes": 1}

    meta.write_text(meta.read_text().replace("machine_options", "overrides"))
    with pytest.raises(ValueError, match="machine_options"):
        load_meta_config(meta)


def test_machine_options_errors_use_the_new_name(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    add_execution(meta, scratch, machine_options={"random_seed": 1})

    with pytest.raises(ValueError, match="machine_options 'random_seed'"):
        load_meta_config(meta)


def test_default_indicators_skip_outputs_without_references(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)

    def change(document: dict) -> None:
        _with_outputs(document, age=["INSEE"], custom_only=[])
        document.pop("indicators", None)

    _edit(meta, change)
    loaded = load_meta_config(meta)

    assert {item.name for item in loaded.indicators} == {"tv_distance(age, ref=INSEE)"}
    assert set(loaded.artifacts) == {"age", "custom_only"}


def test_factor_transport_errors_name_the_factor(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    _edit(meta, lambda d: d["parameters"].update(a=[1, True]))

    with pytest.raises(TypeError) as caught:
        load_meta_config(meta)
    assert "factor 'a' is not faithfully representable" in str(caught.value)
    assert "override" not in str(caught.value)


def test_machine_options_transport_errors_use_the_new_name(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    add_execution(meta, scratch, machine_options={"data_path": "bad\u0000path"})

    with pytest.raises(ValueError) as caught:
        load_meta_config(meta)
    assert "machine_options 'data_path'" in str(caught.value)
    assert "override" not in str(caught.value)


@pytest.mark.parametrize(
    ("key", "value"),
    [("experiment", "grid"), ("outputs", ["age"]), ("execution", None)],
)
def test_malformed_block_types_name_the_key(tmp_path: Path, key: str, value) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    _edit(meta, lambda d: d.__setitem__(key, value))
    with pytest.raises((TypeError, ValueError), match=key):
        load_meta_config(meta)


def test_scalar_parameter_says_it_must_be_a_list(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    _edit(meta, lambda d: d["parameters"].update(a=0.001))
    with pytest.raises(
        ValueError,
        match=r"parameter 'a' must be a list of values, e\.g\. a: \[a, b\]",
    ):
        load_meta_config(meta)


def _sub(tmp_path: Path) -> Path:
    (tmp_path / "b").mkdir()
    return tmp_path / "b"


def test_experiment_and_analysis_key_errors_name_the_keys(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    _edit(meta, lambda d: d["experiment"].update(seed_role="x"))
    with pytest.raises(ValueError, match=r"experiment \(grid\) keys.*unknown: seed_role"):
        load_meta_config(meta)
    meta, _, _, _ = meta_fixture(_sub(tmp_path))
    _edit(meta, lambda d: d.update(analysis={"bootstrap_resamples": 9}))
    with pytest.raises(ValueError, match=r"analysis keys.*unknown: bootstrap_resamples"):
        load_meta_config(meta)
