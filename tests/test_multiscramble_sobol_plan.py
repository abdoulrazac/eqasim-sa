import hashlib
import json
import math
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml
from scipy.stats import qmc

from eqasim_sa.core.charter import parse_charter, parse_distribution, serialize_scientific_contract
from eqasim_sa.core.plan import (
    MULTISCRAMBLE_SOBOL_DOMAIN,
    MultiScrambleSobolDesign,
    build_plan,
    estimate_plan,
    load_run_specs,
)
from eqasim_sa.adapters.application import (
    _design,
    analyze_from_meta_config,
    plan_from_meta_config,
)
from eqasim_sa.adapters.meta_config import load_meta_config
from tests.test_charter import _scientific
from tests.test_plan import base, meta_fixture


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
                "primary_indicator": "synthetic",
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
        {"synthetic"},
    )
    typed = {name: parse_distribution(value, name) for name, value in distributions.items()}
    descriptor = {
        "eqasimCommit": "a",
        "dataManifestSha256": "b",
        "lockfileSha256": "c",
        "eqasimSaVersion": "0.2.0",
    }
    descriptor_bytes = json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
    values = dict(
        base_config=base(),
        factors=factors,
        seeds=(10, 20),
        requirements={},
        context_hash=hashlib.sha256(b"eqasim-sa:context:v1\0" + descriptor_bytes).hexdigest(),
        context_descriptor=descriptor,
        campaign_root=root,
        base_sample_sizes=(2, 4),
        scramble_seeds=(5, 6, 9, 11),
        distributions=distributions,
        scientific_contract=serialize_scientific_contract(charter, full_factors, typed),
        confidence_level=0.95,
    )
    values.update(changes)
    return MultiScrambleSobolDesign(**values)


def multiscramble_meta_document(meta: Path, objective: str = "fp", tolerance=None) -> dict:  # type: ignore[no-untyped-def]
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
    document["parameters"]["seed"] = {"values": [10, 20], "distribution": distribution}
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
        "seed_role": "factor",
        "base_sample_sizes": [2, 4],
        "scramble_seeds": [13, 14, 29, 31],
    }
    document["analysis"] = {"completeness_threshold": 1.0, "confidence_level": 0.95}
    return document


def test_multiscramble_plan_exact_scipy_roles_identity_nesting_and_cardinality(tmp_path: Path) -> None:
    design = multiscramble_design(tmp_path / "campaign")
    result = build_plan(design)
    table = pq.read_table(design.campaign_root / "plan.parquet")
    sealed = json.loads(table.schema.metadata[b"multiScrambleSobolDesign"])
    by_role = {row["role"]: row for row in table.to_pylist()}

    assert result.design_positions == 4 * 4 * (2 * 2 + 2)
    assert table.schema.metadata[b"planSchemaVersion"] == b"2"
    assert set(table.schema.metadata) == {
        b"planSchemaVersion", b"canonicalizationVersion", b"contextDescriptor",
        b"multiScrambleSobolDesignVersion", b"multiScrambleSobolDesign",
    }
    samples = []
    for scramble, scramble_seed in enumerate(design.scramble_seeds):
        sample = qmc.Sobol(
            d=4, scramble=True, bits=64, rng=scramble_seed, optimization=None
        ).random_base2(m=2)
        projected = [[math.floor(float(value) * 2) for value in row] for row in sample]
        samples.append(projected)
        for row_index in (0, 1, 3):
            a, b = projected[row_index][:2], projected[row_index][2:]
            expected = {
                f"scramble:{scramble}:A:row:{row_index}": a,
                f"scramble:{scramble}:B:row:{row_index}": b,
                f"scramble:{scramble}:AB:factor:a:row:{row_index}": [b[0], a[1]],
                f"scramble:{scramble}:BA:factor:a:row:{row_index}": [a[0], b[1]],
                f"scramble:{scramble}:AB:factor:seed:row:{row_index}": [a[0], b[1]],
                f"scramble:{scramble}:BA:factor:seed:row:{row_index}": [b[0], a[1]],
            }
            for expected_role, indices in expected.items():
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


def test_multiscramble_table_cell_cap_precedes_qmc_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import eqasim_sa.core.plan as plan_module

    factors = {f"f{index:05d}": [0, 1] for index in range(10_599)}
    design = multiscramble_design(
        tmp_path / "campaign", factors=factors, base_sample_sizes=(2,)
    )
    monkeypatch.setattr(
        plan_module.qmc,
        "Sobol",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("QMC allocated")),
    )
    with pytest.raises(ValueError, match="20000000 table cells"):
        build_plan(design)


def test_multiscramble_estimate_formula() -> None:
    result = estimate_plan(
        96, ("a",), 10, ((1.0, 2, 3),), 1,
        "multiscramble_sobol", {"D": 2, "N": 4, "S": 4},
    )
    assert result["formula"] == "S*N(2D+2)"


def test_multiscramble_budget_uses_structural_positions_not_unique_runs(
    tmp_path: Path,
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
    document["parameters"]["seed"] = {"values": [10, 20], "distribution": distribution}
    document["charter"]["decision"]["objective"] = "fp"
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
        "seed_role": "factor",
        "base_sample_sizes": [2, 4],
        "scramble_seeds": [13, 14, 29, 31],
    }
    document["analysis"] = {"completeness_threshold": 1.0, "confidence_level": 0.95}
    document["charter"]["budget"]["maximum_runs"] = 200
    meta.write_text(yaml.safe_dump(document, sort_keys=False))

    loaded = load_meta_config(meta)
    design = _design(loaded)
    assert loaded.base_sample_sizes == (2, 4)
    assert loaded.scramble_seeds == (13, 14, 29, 31)
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
    document["parameters"]["seed"] = {"values": [10, 20], "distribution": distribution}
    document["charter"]["decision"]["objective"] = "fp"
    document["charter"]["uncertainty"]["inventory"]["parameters"].append(
        {"name": "seed", "status": "varied", "provenance": "campaign", "rationale": "included"}
    )
    document["experiment"] = {
        "method": "multiscramble_sobol",
        "seed_role": "factor",
        "base_sample_sizes": [2, 4],
        "scramble_seeds": [13, 14, 29, 31],
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
