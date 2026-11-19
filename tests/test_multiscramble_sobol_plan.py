import hashlib
import json
import math
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from eqasim_sa.core.charter import (
    parse_charter,
    parse_distribution,
    serialize_scientific_contract,
)
from eqasim_sa.core.plan import (
    MULTISCRAMBLE_SOBOL_DOMAIN,
    MultiScrambleSobolDesign,
    build_plan,
    estimate_plan,
    load_run_specs,
)
from eqasim_sa.core.sensitivity import engine_descriptor, sobol_unit_positions
from eqasim_sa.adapters.application import (
    _design,
    analyze_from_meta_config,
    plan_from_meta_config,
)
from eqasim_sa.adapters.meta_config import load_meta_config
from tests.test_charter import _scientific
from tests.test_plan import base, meta_fixture
from tests.provenance_helpers import (
    TEST_EQASIM_SA_SHA256,
    TEST_EQASIM_SA_VERSION,
    context_hash,
    scientific_context_descriptor,
    scientific_indicator_definition,
)


def multiscramble_design(root: Path, **changes) -> MultiScrambleSobolDesign:
    factors = {"a": [0, 1]}
    full_factors = {**factors, "seed": [10, 20]}
    distributions = {
        name: {
            "family": "discrete_uniform",
            "transform": "identity",
            "provenance": f"declared support for {name}",
        }
        for name in full_factors
    }
    item = lambda name: {
        "name": name,
        "status": "varied",
        "provenance": "campaign",
        "rationale": "included",
    }
    charter = parse_charter(
        {
            "status": "scientific",
            "stochastic": {
                "target": "finite_seed_factor",
                "random_source": "simulator_seed",
                "intrinsic_variance": "not_identified",
                "common_random_numbers": "none",
                "support_interpretation": "declared_finite_nuisance",
            },
            "decision": {
                "claim": "prioritize uncertainty reduction",
                "use": "diagnostic",
                "setting": "test",
                "objective": "fp",
                "estimand": "Sobol indices",
                "primary_indicator": "tv_distance(age, ref=reference)",
            },
            "uncertainty": {
                "dependence": {"kind": "independent", "rationale": "design assumption"},
                "inventory": {
                    "parameters": [item(name) for name in sorted(full_factors)],
                    "data": [],
                    "structures": [],
                    "scenarios": [],
                    "numerics": [],
                },
            },
            "budget": {"maximum_runs": 100, "reserve_fraction": 0.0},
            "stopping": {
                "criteria": [{"metric": "ci_width", "threshold": 0.1, "consecutive_checks": 2}]
            },
            "robustness": {"checks": [{"name": "support", "rationale": "declared"}]},
        },
        set(full_factors),
        {"tv_distance(age, ref=reference)"},
        {
            "tv_distance(age, ref=reference)": scientific_indicator_definition("age")
        },
    )
    typed = {name: parse_distribution(value, name) for name, value in distributions.items()}
    descriptor = scientific_context_descriptor()
    values = dict(
        base_config=base(),
        factors=factors,
        seeds=(10, 20),
        context_hash=context_hash(descriptor),
        context_descriptor=descriptor,
        campaign_root=root,
        base_sample_sizes=(2, 4),
        scramble_seeds=(1, 3, 10, 17),
        distributions=distributions,
        scientific_contract=serialize_scientific_contract(
            charter,
            full_factors,
            typed,
            TEST_EQASIM_SA_VERSION,
            TEST_EQASIM_SA_SHA256,
        ),
        confidence_level=0.95,
    )
    values.update(changes)
    return MultiScrambleSobolDesign(**values)


def multiscramble_meta_document(meta: Path, objective: str = "fp", tolerance=None) -> dict:  # type: ignore[no-untyped-def]
    document = _scientific(meta)
    document["charter"]["stopping"]["criteria"] = [
        {"metric": "ci_width", "threshold": 0.1, "consecutive_checks": 2}
    ]
    document["charter"]["stochastic"] = {
        "target": "finite_seed_factor",
        "random_source": "simulator_seed",
        "intrinsic_variance": "not_identified",
        "common_random_numbers": "none",
        "support_interpretation": "declared_finite_nuisance",
    }
    distribution = {
        "family": "discrete_uniform",
        "transform": "identity",
        "provenance": "finite simulator-seed support",
    }
    decision = document["charter"]["decision"]
    decision["objective"] = objective
    if tolerance is not None:
        decision["practical_tolerance"] = tolerance
    document["charter"]["uncertainty"]["inventory"]["parameters"].append(
        {
            "name": "seed",
            "status": "varied",
            "provenance": "campaign",
            "rationale": "finite simulator seed factor",
        }
    )
    document["experiment"] = {
        "method": "multiscramble_sobol",
        "seeds": [10, 20],
        "seeds_provenance": distribution["provenance"],
        "base_sample_sizes": [2, 4],
        "scramble_seeds": [16, 20, 21, 27],
    }
    document["analysis"] = {"completeness_threshold": 1.0, "confidence_level": 0.95}
    return document


def test_multiscramble_plan_exact_salib_roles_identity_nesting_and_cardinality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import eqasim_sa.core.plan as plan_module

    calls = {"resolve": 0, "run_id": 0}
    real_resolve = plan_module.resolve_config
    real_compute = plan_module.compute_run_id

    def counted_resolve(*args):  # type: ignore[no-untyped-def]
        calls["resolve"] += 1
        return real_resolve(*args)

    def counted_compute(*args):  # type: ignore[no-untyped-def]
        calls["run_id"] += 1
        return real_compute(*args)

    monkeypatch.setattr(plan_module, "resolve_config", counted_resolve)
    monkeypatch.setattr(plan_module, "compute_run_id", counted_compute)
    design = multiscramble_design(tmp_path / "campaign")
    result = build_plan(design)
    table = pq.read_table(design.campaign_root / "plan.parquet")
    sealed = json.loads(table.schema.metadata[b"multiScrambleSobolDesign"])
    by_role = {row["role"]: row for row in table.to_pylist()}

    assert result.design_positions == 4 * 4 * (2 * 2 + 2)
    assert calls == {"resolve": result.unique_runs, "run_id": result.unique_runs}
    assert table.schema.metadata[b"planSchemaVersion"] == b"2"
    assert table.schema.metadata[b"multiScrambleSobolDesignVersion"] == b"3"
    assert set(table.schema.metadata) == {
        b"planSchemaVersion", b"canonicalizationVersion", b"contextDescriptor",
        b"outputsDeclaration", b"modelDocument",
        b"multiScrambleSobolDesignVersion", b"multiScrambleSobolDesign",
    }
    samples = []
    for scramble, scramble_seed in enumerate(design.scramble_seeds):
        sample = sobol_unit_positions(("a", "seed"), 4, scramble_seed)
        samples.append(sample)
        for role, point in sample:
            indices = [math.floor(value * 2) for value in point]
            expected_role = f"scramble:{scramble}:{role}"
            assert (by_role[expected_role]["a"], by_role[expected_role]["seed"]) == (
                indices[0],
                design.seeds[indices[1]],
            )
        prefixes = []
        for checkpoint in sealed["baseSampleSizes"]:
            prefix = {
                role
                for role in by_role
                if role.startswith(f"scramble:{scramble}:")
                and int(role.rsplit(":", 1)[1]) < checkpoint
            }
            assert len(prefix) == checkpoint * (2 * 2 + 2)
            prefixes.append(prefix)
        assert prefixes[0] < prefixes[1]
    assert all(samples[0] != sample for sample in samples[1:])
    role = "scramble:0:AB:factor:a:row:0"
    expected_id = hashlib.sha256(
        MULTISCRAMBLE_SOBOL_DOMAIN
        + json.dumps({"design": sealed, "role": role}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:32]
    assert by_role[role]["designId"] == expected_id
    assert sealed["baseSampleSizes"] == [2, 4]
    assert sealed["numericalEngine"] == engine_descriptor(
        "multiscramble_sobol",
        sample_size=design.base_sample_sizes[-1],
        sample_seeds=design.scramble_seeds,
    )
    assert sealed["saltelliEstimator"] == "symmetric-forward-reverse-salib-v1"
    assert "qmc" not in sealed
    assert sealed["scientificContract"]["parameters"][-1]["name"] == "seed"
    assert (
        sealed["scientificContract"]["charter"]["stochastic"]["target"]
        == "finite_seed_factor"
    )


def test_multiscramble_load_is_row_order_insensitive_and_tamper_evident(tmp_path: Path) -> None:
    design = multiscramble_design(tmp_path / "campaign")
    build_plan(design)
    path = design.campaign_root / "plan.parquet"
    table = pq.read_table(path)
    pq.write_table(table.take(pa.array(list(reversed(range(len(table)))))), path)
    assert load_run_specs(path, design, "test")
    rows = table.to_pylist()
    rows[0]["designId"] = "f" * 32
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), path)
    with pytest.raises(ValueError, match="mismatch"):
        load_run_specs(path, design, "test")


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"base_sample_sizes": ()}, "base_sample_sizes"),
        ({"base_sample_sizes": (2, 8)}, "doubling"),
        ({"scramble_seeds": (1, 2, 3)}, "at least four"),
        ({"scramble_seeds": (1, 2, 3, 3)}, "unique"),
    ],
)
def test_multiscramble_validation_boundaries(tmp_path: Path, changes: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build_plan(multiscramble_design(tmp_path / "campaign", **changes))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("metric", "rank_stability", "ci_width"),
        ("consecutive_checks", 3, "at most 2"),
    ],
)
def test_multiscramble_stopping_is_rejected_in_meta_and_core_plan_validation(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    meta_root = tmp_path / "meta"
    meta_root.mkdir()
    meta, _, _, _ = meta_fixture(meta_root)
    document = multiscramble_meta_document(meta)
    document["charter"]["budget"]["maximum_runs"] = 200
    document["charter"]["stopping"]["criteria"][0][field] = value
    meta.write_text(yaml.safe_dump(document, sort_keys=False))
    with pytest.raises(ValueError, match=message):
        load_meta_config(meta)

    design = multiscramble_design(tmp_path / "core")
    contract = json.loads(json.dumps(design.scientific_contract))
    contract["charter"]["stopping"][0][field] = value
    with pytest.raises(ValueError, match=message):
        build_plan(
            multiscramble_design(
                tmp_path / "core", scientific_contract=contract
            )
        )


def test_multiscramble_rejects_conditional_mean_stochastic_mode(tmp_path: Path) -> None:
    design = multiscramble_design(tmp_path / "unused")
    contract = json.loads(json.dumps(design.scientific_contract))
    contract["charter"]["stochastic"] = {
        "target": "conditional_mean",
        "random_source": "simulator_seed",
        "intrinsic_variance": "report",
        "common_random_numbers": "within_trajectory",
        "allocation": {
            "phase": "pilot",
            "variance_quantile": 0.9,
            "target_mcse": 0.1,
            "minimum_replications": 2,
            "maximum_replications": 4,
            "selected_replications": None,
            "provenance": None,
        },
    }
    with pytest.raises(ValueError, match="finite_seed_factor"):
        build_plan(
            multiscramble_design(
                tmp_path / "wrong", scientific_contract=contract
            )
        )


def test_multiscramble_ff_tolerance_one_is_accepted_by_core(tmp_path: Path) -> None:
    original = multiscramble_design(tmp_path / "unused")
    contract = json.loads(json.dumps(original.scientific_contract))
    contract["charter"]["decision"].update(objective="ff", practical_tolerance=1.0)

    result = build_plan(
        multiscramble_design(
            tmp_path / "campaign", scientific_contract=contract
        )
    )

    assert result.status == "created"


def test_multiscramble_ff_tolerance_above_one_is_rejected_by_core(tmp_path: Path) -> None:
    original = multiscramble_design(tmp_path / "unused")
    contract = json.loads(json.dumps(original.scientific_contract))
    contract["charter"]["decision"].update(objective="ff", practical_tolerance=1.01)

    with pytest.raises(ValueError, match=r"variance-share tolerance in \[0,1\]"):
        build_plan(
            multiscramble_design(
                tmp_path / "campaign", scientific_contract=contract
            )
        )


def test_multiscramble_table_cell_cap_precedes_salib_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    factors = {f"f{index:05d}": [0, 1] for index in range(10_599)}
    design = multiscramble_design(
        tmp_path / "campaign", factors=factors, base_sample_sizes=(2,)
    )
    monkeypatch.setattr(
        "eqasim_sa.core.methods.multiscramble_sobol_plan.sobol_unit_positions",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("SALib sample allocated")
        ),
    )
    with pytest.raises(ValueError, match="20000000 table cells"):
        build_plan(design)


def test_multiscramble_position_cap_precedes_salib_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import eqasim_sa.core.plan as plan_module

    monkeypatch.setattr(plan_module, "MAX_PLAN_POSITIONS", 95)
    monkeypatch.setattr(
        "eqasim_sa.core.methods.multiscramble_sobol_plan.sobol_unit_positions",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("SALib sample allocated")
        ),
    )
    with pytest.raises(ValueError, match="safe cap of 95 design positions"):
        build_plan(multiscramble_design(tmp_path / "campaign"))


def test_multiscramble_estimate_formula() -> None:
    result = estimate_plan(
        96, ("a",), 10, ((1.0, 2, 3),), 1,
        "multiscramble_sobol", {"D": 2, "N": 4, "S": 4},
    )
    assert result["formula"] == "S*N(2D+2)"


def test_multiscramble_budget_uses_structural_positions_not_unique_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    permissive = multiscramble_design(tmp_path / "permissive")
    result = build_plan(permissive)
    assert result.design_positions == 96
    assert result.unique_runs == 4

    contract = json.loads(json.dumps(permissive.scientific_contract))
    contract["charter"]["budget"] = {
        "maximum_runs": 50,
        "reserve_fraction": 0.0,
    }
    restricted = multiscramble_design(
        tmp_path / "restricted", scientific_contract=contract
    )
    monkeypatch.setattr(
        "eqasim_sa.core.methods.multiscramble_sobol_plan.sobol_unit_positions",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("SALib sample allocated")
        ),
    )
    with pytest.raises(
        ValueError,
        match=(
            "multiscramble_sobol design positions 96 "
            "exceed scientific execution budget 50"
        ),
    ):
        build_plan(restricted)
    assert not (restricted.campaign_root / "plan.parquet").exists()


def test_multiscramble_meta_serializes_seed_as_scientific_factor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    document = _scientific(meta)
    document["charter"]["stochastic"] = {
        "target": "finite_seed_factor",
        "random_source": "simulator_seed",
        "intrinsic_variance": "not_identified",
        "common_random_numbers": "none",
        "support_interpretation": "declared_finite_nuisance",
    }
    distribution = {
        "family": "discrete_uniform",
        "transform": "identity",
        "provenance": "finite simulator-seed support",
    }
    document["charter"]["decision"]["objective"] = "fp"
    document["charter"]["stopping"]["criteria"] = [
        {"metric": "ci_width", "threshold": 0.1, "consecutive_checks": 2}
    ]
    document["charter"]["uncertainty"]["inventory"]["parameters"].append(
        {
            "name": "seed",
            "status": "varied",
            "provenance": "campaign",
            "rationale": "finite simulator seed factor",
        }
    )
    document["experiment"] = {
        "method": "multiscramble_sobol",
        "seeds": [10, 20],
        "seeds_provenance": distribution["provenance"],
        "base_sample_sizes": [2, 4],
        "scramble_seeds": [16, 20, 21, 27],
    }
    document["analysis"] = {"completeness_threshold": 1.0, "confidence_level": 0.95}
    document["charter"]["budget"]["maximum_runs"] = 200
    meta.write_text(yaml.safe_dump(document, sort_keys=False))

    loaded = load_meta_config(meta)
    design = _design(loaded)
    assert loaded.base_sample_sizes == (2, 4)
    assert loaded.scramble_seeds == (16, 20, 21, 27)
    assert design.scientific_contract["parameters"][-1]["name"] == "seed"
    result = plan_from_meta_config(meta)
    assert result.design_positions == 4 * 4 * (2 * 3 + 2)
    cells = result.design_positions * (3 + 5)
    monkeypatch.setattr("eqasim_sa.adapters.meta_config.MAX_MULTISCRAMBLE_TABLE_CELLS", cells - 1)
    with pytest.raises(ValueError, match="table cells"):
        load_meta_config(meta)


@pytest.mark.parametrize("tolerance", [1.0, 1.01])
def test_multiscramble_ff_meta_tolerance_is_a_variance_share(
    tmp_path: Path, tolerance: float
) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    document = multiscramble_meta_document(meta, objective="ff", tolerance=tolerance)
    meta.write_text(yaml.safe_dump(document, sort_keys=False))

    if tolerance == 1.0:
        assert load_meta_config(meta).charter.decision.practical_tolerance == 1.0
    else:
        with pytest.raises(ValueError, match=r"variance-share tolerance in \[0,1\]"):
            load_meta_config(meta)


def test_application_refuses_multiscramble_contract_substitution_before_analyze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    document = _scientific(meta)
    document["charter"]["stochastic"] = {
        "target": "finite_seed_factor",
        "random_source": "simulator_seed",
        "intrinsic_variance": "not_identified",
        "common_random_numbers": "none",
        "support_interpretation": "declared_finite_nuisance",
    }
    distribution = {
        "family": "discrete_uniform",
        "transform": "identity",
        "provenance": "finite simulator-seed support",
    }
    document["charter"]["decision"]["objective"] = "fp"
    document["charter"]["stopping"]["criteria"] = [
        {"metric": "ci_width", "threshold": 0.1, "consecutive_checks": 2}
    ]
    document["charter"]["uncertainty"]["inventory"]["parameters"].append(
        {"name": "seed", "status": "varied", "provenance": "campaign", "rationale": "included"}
    )
    document["experiment"] = {
        "method": "multiscramble_sobol",
        "seeds": [10, 20],
        "seeds_provenance": distribution["provenance"],
        "base_sample_sizes": [2, 4],
        "scramble_seeds": [16, 20, 21, 27],
    }
    document["analysis"] = {"completeness_threshold": 1.0, "confidence_level": 0.95}
    document["charter"]["budget"]["maximum_runs"] = 200
    meta.write_text(yaml.safe_dump(document, sort_keys=False))
    plan_from_meta_config(meta)
    document["charter"]["budget"]["maximum_runs"] += 1
    meta.write_text(yaml.safe_dump(document, sort_keys=False))
    called = []
    monkeypatch.setattr(
        "eqasim_sa.adapters.application.analyze", lambda *args, **kwargs: called.append(1)
    )
    with pytest.raises(ValueError, match="mismatch"):
        analyze_from_meta_config(meta)
    assert called == []
