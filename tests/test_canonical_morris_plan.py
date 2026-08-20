import copy
import hashlib
import json
import math
from pathlib import Path

import pyarrow.parquet as pq
import pytest
import yaml

from eqasim_sa.adapters.application import _design, plan_from_meta_config
from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.core.charter import (
    parse_charter,
    parse_distribution,
    serialize_scientific_contract,
)
from eqasim_sa.core.plan import (
    CANONICAL_MORRIS_DOMAIN,
    CanonicalMorrisDesign,
    DiscreteOATDesign,
    MorrisDesign,
    _available_scientific_runs,
    _canonical_definition,
    _discrete_uniform_generalized_inverse_index,
    _trajectory_distance,
    build_plan,
    estimate_plan,
    load_run_specs,
)
from tests.test_charter import _scientific
from tests.test_plan import base, meta_fixture


def _scientific_contract(factors: dict[str, list], distributions: dict[str, dict]) -> dict:
    item = lambda name, status: {
        "name": name,
        "status": status,
        "provenance": "Campaign declaration",
        "rationale": "Included in the uncertainty account",
    }
    charter = parse_charter(
        {
            "status": "scientific",
            "decision": {
                "claim": "Identify inputs requiring further study",
                "use": "diagnostic",
                "setting": "Test decision setting",
                "objective": "screening",
                "estimand": "Expected indicator over simulation seeds",
                "primary_indicator": "indicator",
            },
            "uncertainty": {
                "dependence": {
                    "kind": "independent",
                    "rationale": "No dependence is assumed for this design",
                },
                "inventory": {
                    "parameters": [item(name, "varied") for name in sorted(factors)],
                    "data": [item("survey", "fixed")],
                    "structures": [item("model", "excluded")],
                    "scenarios": [],
                    "numerics": [],
                },
            },
            "budget": {"maximum_runs": 100, "reserve_fraction": 0.1},
            "stopping": {
                "criteria": [
                    {
                        "metric": "rank_stability",
                        "threshold": 0.05,
                        "consecutive_checks": 2,
                    }
                ]
            },
            "robustness": {
                "checks": [
                    {
                        "name": "seed sensitivity",
                        "rationale": "Verify conclusions across seeds",
                    }
                ]
            },
        },
        set(factors),
        {"indicator"},
    )
    typed_distributions = {
        name: parse_distribution(distribution, name)
        for name, distribution in distributions.items()
    }
    return serialize_scientific_contract(charter, factors, typed_distributions)


def canonical_design(root: Path, **changes) -> CanonicalMorrisDesign:
    factors = {"b": ["low", "middle", "high"], "a": [10, 20]}
    distributions = {
        name: {
            "family": "discrete_uniform",
            "transform": "identity",
            "provenance": f"declared support for {name}",
        }
        for name in factors
    }
    values = dict(
        base_config=base(),
        factors=factors,
        seeds=(41, 42),
        requirements={"run": ["stage"]},
        context_hash="c" * 64,
        context_descriptor={"version": "test"},
        campaign_root=root,
        trajectories=3,
        levels=4,
        candidate_trajectories=12,
        design_seed=20260820,
        distributions=distributions,
        bootstrap_resamples=101,
        confidence_level=0.95,
        convergence_checkpoints=(2, 3),
        screening_top_k=2,
    )
    values.update(changes)
    if "factors" in changes and "distributions" not in changes:
        values["distributions"] = {
            name: distributions.get(
                name,
                {
                    "family": "discrete_uniform",
                    "transform": "identity",
                    "provenance": f"declared support for {name}",
                },
            )
            for name in values["factors"]
        }
    if "scientific_contract" not in changes:
        values["scientific_contract"] = _scientific_contract(
            values["factors"], values["distributions"]
        )
    return CanonicalMorrisDesign(**values)


def discrete_design(root: Path, cls=DiscreteOATDesign):  # type: ignore[no-untyped-def]
    return cls(
        base(),
        {"b": [-1.0, 1.0], "a": [0, 2, 5]},
        (41,),
        {"run": ["stage"]},
        "c" * 64,
        {"version": "test"},
        root,
        16,
        20260814,
    )


def canonical_meta(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    meta, _, _, _ = meta_fixture(tmp_path)
    document = _scientific(meta)
    document["experiment"] = {
        "method": "canonical_morris",
        "seed_role": "replication",
        "levels": 4,
        "trajectories": 2,
        "candidate_trajectories": 8,
        "design_seed": 20260820,
        "replications": {"seeds": [41, 42]},
    }
    document["analysis"] = {
        "completeness_threshold": 1.0,
        "bootstrap_resamples": 101,
        "confidence_level": 0.95,
        "convergence_checkpoints": [2],
        "screening_top_k": 2,
    }
    meta.write_text(yaml.safe_dump(document, sort_keys=False))
    return meta


def test_discrete_oat_preserves_adjacent_universe_but_has_independent_identity(
    tmp_path: Path,
) -> None:
    discrete = discrete_design(tmp_path / "discrete")
    historical = discrete_design(tmp_path / "historical", MorrisDesign)
    build_plan(discrete)
    build_plan(historical)
    oat = pq.read_table(discrete.campaign_root / "plan.parquet")
    morris = pq.read_table(historical.campaign_root / "plan.parquet")

    assert sorted((row["a"], row["b"]) for row in oat.to_pylist()) == sorted(
        (row["a"], row["b"]) for row in morris.to_pylist()
    )
    assert set(oat["method"].to_pylist()) == {"discrete_oat"}
    assert set(oat["designId"].to_pylist()).isdisjoint(morris["designId"].to_pylist())
    assert oat.schema.metadata[b"discreteOatDesignVersion"] == b"1"
    assert b"morrisDesign" not in oat.schema.metadata
    assert morris.schema.metadata[b"morrisDesignVersion"] == b"1"


def test_canonical_exact_delta_grid_mapping_cardinality_crn_and_metadata(
    tmp_path: Path,
) -> None:
    design = canonical_design(tmp_path / "campaign")
    result = build_plan(design)
    table = pq.read_table(design.campaign_root / "plan.parquet")
    rows = table.to_pylist()
    metadata = json.loads(table.schema.metadata[b"canonicalMorrisDesign"])

    assert result.design_positions == 3 * (2 + 1) * 2
    assert table.schema.metadata[b"planSchemaVersion"] == b"2"
    assert metadata["delta"] == {"numerator": 2, "denominator": 3}
    assert metadata["p"] == 4
    assert metadata["inverseCdfMap"] == "discrete-uniform-generalized-inverse-v1"
    assert metadata["selectionAlgorithm"] == "greedy-sum-pairwise-squared-distance"
    assert metadata["selectionVersion"] == 1
    assert metadata["replicationBaseSeeds"] == [41, 42]
    assert metadata["seedDerivation"]["version"] == 1
    assert len(metadata["candidatePoolIdentities"]) == 12
    assert len(set(metadata["candidatePoolIdentities"])) == 12
    assert [item["selectionOrder"] for item in metadata["selectedTrajectories"]] == [0, 1, 2]
    assert metadata["scientificContract"] == design.scientific_contract
    assert not ({"unitA", "unitB"} & set(table.column_names))
    blocks = [rows[index : index + 3] for index in range(0, len(rows), 3)]
    block_seeds = []
    for block in blocks:
        assert len({row["seed"] for row in block}) == 1
        block_seeds.append(block[0]["seed"])
        for left, right in zip(block, block[1:]):
            changed = [name for name in ("a", "b") if left[name] != right[name]]
            assert changed == [right["role"].split(":")[-1]]
    assert len(block_seeds) == len(set(block_seeds))
    for selected in metadata["selectedTrajectories"]:
        matrix = selected["matrix"]
        for left, right in zip(matrix, matrix[1:]):
            differences = [abs(a - b) for a, b in zip(left, right) if a != b]
            assert differences == [2]


def test_canonical_is_order_independent_and_sensitive_to_seed_and_support_order(
    tmp_path: Path,
) -> None:
    baseline = canonical_design(tmp_path / "baseline")
    reordered = canonical_design(
        tmp_path / "reordered",
        factors={"a": [10, 20], "b": ["low", "middle", "high"]},
        distributions={
            name: baseline.distributions[name] for name in ("a", "b")
        },
    )
    changed_seed = canonical_design(tmp_path / "seed", design_seed=20260821)
    changed_support = canonical_design(
        tmp_path / "support",
        factors={"a": [20, 10], "b": ["low", "middle", "high"]},
    )
    for current in (baseline, reordered, changed_seed, changed_support):
        build_plan(current)
    original = (baseline.campaign_root / "plan.parquet").read_bytes()
    assert (reordered.campaign_root / "plan.parquet").read_bytes() == original
    assert (changed_seed.campaign_root / "plan.parquet").read_bytes() != original
    assert (changed_support.campaign_root / "plan.parquet").read_bytes() != original


def test_canonical_greedy_selection_and_tie_break_are_deterministic(tmp_path: Path) -> None:
    design = canonical_design(tmp_path / "campaign")
    definition, selected = _canonical_definition(design, tuple(sorted(design.factors)))
    selected_ids = [identity for identity, _ in selected]
    again = _canonical_definition(copy.deepcopy(design), tuple(sorted(design.factors)))
    assert again == (definition, selected)

    candidate_definition = definition["candidateDefinition"]
    encoded = json.dumps(candidate_definition, sort_keys=True, separators=(",", ":")).encode()
    universe = math.factorial(2) * 4**2
    offset = int.from_bytes(
        hashlib.sha256(CANONICAL_MORRIS_DOMAIN + encoded + b"\0offset").digest(), "big"
    ) % universe
    stride = int.from_bytes(
        hashlib.sha256(CANONICAL_MORRIS_DOMAIN + encoded + b"\0stride").digest(), "big"
    ) % universe or 1
    while math.gcd(stride, universe) != 1:
        stride = (stride + 1) % universe or 1
    from eqasim_sa.core.plan import _canonical_candidate

    pool = {}
    for index in range(design.candidate_trajectories):
        matrix = _canonical_candidate((offset + index * stride) % universe, 2, 4)
        identity = hashlib.sha256(
            json.dumps(matrix, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        pool[identity] = matrix
    identities = sorted(pool)
    pairs = [
        (left, right)
        for index, left in enumerate(identities)
        for right in identities[index + 1 :]
    ]
    maximum_pair_distance = max(
        _trajectory_distance(pool[left], pool[right]) for left, right in pairs
    )
    expected_pair = min(
        pair
        for pair in pairs
        if _trajectory_distance(pool[pair[0]], pool[pair[1]])
        == maximum_pair_distance
    )
    assert selected_ids[:2] == list(expected_pair)

    remaining = sorted(set(pool) - set(expected_pair))
    scores = {
        candidate: sum(
            sum(
                (left - right) ** 2
                for left_point in pool[candidate]
                for right_point in pool[chosen]
                for left, right in zip(left_point, right_point)
            )
            for chosen in expected_pair
        )
        for candidate in remaining
    }
    expected_third = min(
        candidate for candidate in remaining if scores[candidate] == max(scores.values())
    )
    assert selected_ids[2] == expected_third


def test_discrete_uniform_generalized_inverse_exact_jump_boundaries() -> None:
    assert [
        _discrete_uniform_generalized_inverse_index(3, 4, grid_index)
        for grid_index in range(4)
    ] == [0, 0, 1, 2]


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"levels": 3}, "levels"),
        ({"levels": 2}, "levels"),
        ({"levels": True}, "levels"),
        ({"trajectories": 0}, "trajectories"),
        ({"trajectories": 1, "convergence_checkpoints": (1,)}, "trajectories"),
        ({"candidate_trajectories": 2}, "candidate_trajectories"),
        ({"candidate_trajectories": 33}, "candidate_trajectories"),
        ({"candidate_trajectories": 501}, "candidate_trajectories"),
        ({"design_seed": 2**63}, "design_seed"),
        ({"design_seed": True}, "design_seed"),
        ({"seeds": ()}, "seeds"),
        ({"seeds": (1, 1)}, "unique"),
        ({"factors": {"a": [1]}}, "two or more"),
        ({"factors": {"a": [1, 1]}}, "distinct"),
        ({"factors": {"a": [1, "x"]}}, "same type"),
        ({"bootstrap_resamples": 0}, "bootstrap_resamples"),
        ({"bootstrap_resamples": 99}, "bootstrap_resamples"),
        ({"confidence_level": 1.0}, "confidence_level"),
        ({"convergence_checkpoints": (1, 3)}, "convergence_checkpoints"),
        ({"convergence_checkpoints": (2, 4)}, "convergence_checkpoints"),
        ({"convergence_checkpoints": (3, 1, 3)}, "convergence_checkpoints"),
        ({"screening_top_k": 3}, "screening_top_k"),
        ({"scientific_contract": []}, "scientific_contract"),
        ({"scientific_contract": {"charter": {}}}, "scientific_contract"),
    ],
)
def test_canonical_core_validation_boundaries(
    tmp_path: Path, changes: dict, message: str
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        build_plan(canonical_design(tmp_path / "campaign", **changes))


def test_canonical_rejects_excessive_selection_work(tmp_path: Path) -> None:
    factors = {f"factor_{index}": [0, 1] for index in range(10)}
    with pytest.raises(ValueError, match="distance-computation budget"):
        build_plan(
            canonical_design(
                tmp_path / "campaign",
                factors=factors,
                candidate_trajectories=500,
                trajectories=2,
                convergence_checkpoints=(2,),
                screening_top_k=2,
            )
        )


def test_canonical_meta_contract_is_typed_sealed_and_reconstructed(tmp_path: Path) -> None:
    meta = canonical_meta(tmp_path)
    loaded = load_meta_config(meta)
    assert loaded.method == "canonical_morris"
    assert loaded.levels == 4 and loaded.candidate_trajectories == 8
    assert loaded.convergence_checkpoints == (2,)
    contract = serialize_scientific_contract(
        loaded.charter, loaded.factors, loaded.distributions
    )
    assert [item["name"] for item in contract["parameters"]] == ["a", "b"]
    result = plan_from_meta_config(meta)
    assert result.design_positions == 12
    design = _design(loaded)
    path = loaded.campaign_root / "plan.parquet"
    assert len(load_run_specs(path, design, loaded.campaign)) == result.unique_runs

    table = pq.read_table(path)
    metadata = dict(table.schema.metadata)
    sealed = json.loads(metadata[b"canonicalMorrisDesign"])
    sealed["scientificContract"]["charter"]["budget"]["maximum_runs"] += 1
    metadata[b"canonicalMorrisDesign"] = json.dumps(
        sealed, sort_keys=True, separators=(",", ":")
    ).encode()
    pq.write_table(table.replace_schema_metadata(metadata), path)
    with pytest.raises(ValueError, match="mismatch"):
        load_run_specs(path, design, loaded.campaign)


def test_canonical_meta_rejects_candidate_pool_above_safe_cap(tmp_path: Path) -> None:
    meta = canonical_meta(tmp_path)
    document = yaml.safe_load(meta.read_text())
    document["experiment"]["candidate_trajectories"] = 501
    meta.write_text(yaml.safe_dump(document, sort_keys=False))
    with pytest.raises(ValueError, match="at most 500"):
        load_meta_config(meta)


@pytest.mark.parametrize(
    "section,key,value,message",
    [
        ("experiment", "trajectories", 1, "trajectories"),
        ("analysis", "convergence_checkpoints", [1, 2], "convergence_checkpoints"),
        ("analysis", "bootstrap_resamples", 99, "bootstrap_resamples"),
    ],
)
def test_canonical_meta_rejects_scientific_minimum_boundaries(
    tmp_path: Path, section: str, key: str, value: object, message: str
) -> None:
    meta = canonical_meta(tmp_path)
    document = yaml.safe_load(meta.read_text())
    document[section][key] = value
    if key == "trajectories":
        document["analysis"]["convergence_checkpoints"] = [1]
    meta.write_text(yaml.safe_dump(document, sort_keys=False))
    with pytest.raises(ValueError, match=message):
        load_meta_config(meta)


@pytest.mark.parametrize(
    "mutation,message",
    [
        (lambda d: d["charter"]["decision"].update(objective="fp"), "screening"),
        (lambda d: d["charter"]["decision"].update(objective="ff", practical_tolerance=0.1), "screening"),
        (
            lambda d: d["charter"]["uncertainty"]["dependence"].update(
                kind="declared",
                groups=[{"factors": ["a", "b"], "model": "joint", "provenance": "p"}],
            ),
            "independent",
        ),
    ],
)
def test_canonical_meta_rejects_incompatible_scientific_contract(
    tmp_path: Path, mutation, message: str
) -> None:  # type: ignore[no-untyped-def]
    meta = canonical_meta(tmp_path)
    document = yaml.safe_load(meta.read_text())
    mutation(document)
    meta.write_text(yaml.safe_dump(document, sort_keys=False))
    with pytest.raises(ValueError, match=message):
        load_meta_config(meta)


def test_canonical_requires_scientific_and_discrete_requires_demonstration(
    tmp_path: Path,
) -> None:
    meta = canonical_meta(tmp_path / "canonical")
    document = yaml.safe_load(meta.read_text())
    document["charter"] = {"status": "demonstration"}
    for parameter in document["parameters"].values():
        parameter.pop("distribution")
    meta.write_text(yaml.safe_dump(document, sort_keys=False))
    with pytest.raises(ValueError, match="canonical_morris.*scientific"):
        load_meta_config(meta)

    discrete_root = tmp_path / "discrete"
    discrete_root.mkdir()
    meta, _, _, _ = meta_fixture(discrete_root)
    document = _scientific(meta)
    document["experiment"].update(
        method="discrete_oat", trajectories=1, design_seed=1
    )
    meta.write_text(yaml.safe_dump(document, sort_keys=False))
    with pytest.raises(ValueError, match="discrete_oat.*demonstration"):
        load_meta_config(meta)


def test_canonical_budget_rejects_before_plan_publication(tmp_path: Path) -> None:
    contract = copy.deepcopy(canonical_design(tmp_path / "unused").scientific_contract)
    contract["charter"]["budget"] = {"maximum_runs": 1, "reserve_fraction": 0.5}
    design = canonical_design(tmp_path / "campaign", scientific_contract=contract)
    with pytest.raises(ValueError, match="execution budget 0"):
        build_plan(design)
    assert not (design.campaign_root / "plan.parquet").exists()


def test_available_scientific_runs_uses_decimal_user_semantics() -> None:
    assert _available_scientific_runs(5, 0.8) == 1


@pytest.mark.parametrize(
    "mutation,message",
    [
        (lambda contract: contract["charter"].update(status="demonstration"), "status"),
        (
            lambda contract: contract["charter"]["decision"].update(objective="fp"),
            "screening",
        ),
        (
            lambda contract: contract["parameters"][0].update(support=[999, 1000]),
            "supports",
        ),
        (
            lambda contract: contract["parameters"][0]["distribution"].update(
                family="normal"
            ),
            "distributions",
        ),
    ],
)
def test_canonical_core_rejects_contract_contradictions(
    tmp_path: Path, mutation, message: str
) -> None:  # type: ignore[no-untyped-def]
    valid = canonical_design(tmp_path / "valid")
    contract = copy.deepcopy(valid.scientific_contract)
    mutation(contract)
    with pytest.raises(ValueError, match=message):
        build_plan(canonical_design(tmp_path / "campaign", scientific_contract=contract))


def test_canonical_seed_derivation_replays_deterministically(tmp_path: Path) -> None:
    first = canonical_design(tmp_path / "first")
    second = canonical_design(tmp_path / "second")
    build_plan(first)
    build_plan(second)
    first_seeds = pq.read_table(first.campaign_root / "plan.parquet")["seed"].to_pylist()
    second_seeds = pq.read_table(second.campaign_root / "plan.parquet")["seed"].to_pylist()
    assert first_seeds == second_seeds


@pytest.mark.parametrize("method", ["discrete_oat", "canonical_morris"])
def test_trajectory_estimate_formula_supports_new_method_names(method: str) -> None:
    estimate = estimate_plan(
        18,
        ("a", "b"),
        10,
        ((1.0, 2, 3),),
        1,
        method,
        {"D": 2, "T": 3, "R": 2},
    )
    assert estimate["formula"] == "T(D+1)R"
    assert estimate["formulaInputs"] == {"D": 2, "T": 3, "R": 2}
