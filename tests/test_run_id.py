import copy
import math

import pytest

from eqasim_sa.core.run import canonical_config, compute_run_id


def config() -> dict[str, object]:
    return {
        "working_directory": "/scratch/a",
        "run": ["synthesis.population", "analysis.age"],
        "config": {
            "sampling_rate": 0.01,
            "matching_attributes": ["age", "sex"],
            "income_assignation_method": "uniform",
            "output_path": "/output/a",
            "data_path": "/data/a",
            "processes": 4,
            "java_memory": "8G",
            "git_binary": "/usr/bin/git",
            "java_binary": "/usr/bin/java",
            "maven_binary": "/usr/bin/mvn",
            "random_seed": 42,
        },
        "metadata": {"output_path": "scientific"},
    }


def test_run_id_golden_and_does_not_mutate() -> None:
    source = config()
    before = copy.deepcopy(source)
    assert compute_run_id(source, 42) == "110e16cfff707486ce055595324cd3d7"
    assert source == before


def test_mapping_order_and_environment_paths_do_not_change_identity() -> None:
    left = config()
    right = dict(reversed(list(left.items())))
    right["working_directory"] = "/other"
    nested = right["config"]
    assert isinstance(nested, dict)
    for key in (
        "output_path",
        "data_path",
        "processes",
        "java_memory",
        "git_binary",
        "java_binary",
        "maven_binary",
    ):
        nested[key] = f"other-{key}"
    assert compute_run_id(left, 42) == compute_run_id(right, 42)


def test_scientific_value_seed_list_order_and_nested_homonym_are_hashed() -> None:
    baseline = config()
    baseline_id = compute_run_id(baseline, 42)
    changed = copy.deepcopy(baseline)
    changed["config"]["sampling_rate"] = 0.02  # type: ignore[index]
    assert compute_run_id(changed, 42) != baseline_id
    changed = copy.deepcopy(baseline)
    changed["config"]["income_assignation_method"] = "bhepop2"  # type: ignore[index]
    assert compute_run_id(changed, 42) != baseline_id
    changed = copy.deepcopy(baseline)
    changed["config"]["matching_attributes"].reverse()  # type: ignore[index,union-attr]
    assert compute_run_id(changed, 42) != baseline_id
    changed = copy.deepcopy(baseline)
    changed["metadata"]["output_path"] = "other"  # type: ignore[index]
    assert compute_run_id(changed, 42) != baseline_id
    del baseline["config"]["random_seed"]  # type: ignore[index]
    assert compute_run_id(baseline, 43) != baseline_id


@pytest.mark.parametrize("value", [(1, 2), {1, 2}, math.nan, math.inf])
def test_rejects_non_json_values(value: object) -> None:
    source = config()
    source["bad"] = value
    with pytest.raises((TypeError, ValueError)):
        canonical_config(source, 42)


def test_rejects_non_string_keys_invalid_seed_and_conflicting_seed() -> None:
    source = config()
    source[1] = "bad"  # type: ignore[index]
    with pytest.raises(TypeError):
        canonical_config(source, 42)
    with pytest.raises((TypeError, ValueError)):
        canonical_config(config(), True)
    source = config()
    source["config"]["random_seed"] = 7  # type: ignore[index]
    with pytest.raises(ValueError, match="random_seed"):
        canonical_config(source, 42)
