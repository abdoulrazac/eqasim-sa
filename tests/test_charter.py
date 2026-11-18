import copy
import shutil
from pathlib import Path

import pytest
import yaml

from eqasim_sa.adapters.application import analyze_from_meta_config, plan_from_meta_config
from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.core.charter import (
    AllocationPhase,
    CharterStatus,
    ConditionalMeanStochastic,
    FiniteSeedFactorStochastic,
    ReplicationAllocation,
    DecisionUse,
    InventoryStatus,
    ScientificCharter,
    SensitivityObjective,
    serialize_stochastic,
)
from tests.test_plan import meta_fixture


def _document(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _write(path: Path, document: dict) -> None:
    path.write_text(yaml.safe_dump(document, sort_keys=False))


def _scientific(path: Path) -> dict:
    document = _document(path)
    for declaration in document["parameters"].values():
        declaration["distribution"] = {
            "family": "discrete_uniform",
            "transform": "identity",
            "provenance": "Declared design domain",
        }
    document["outputs"] = {
        "age": {"category": "c", "simulated": "s", "references": ["ref"]}
    }
    document["indicators"] = [{"tv_distance": {"output": "age", "reference": "ref"}}]
    item = lambda name, status: {
        "name": name,
        "status": status,
        "provenance": "Campaign declaration",
        "rationale": "Included in the uncertainty account",
    }
    document["charter"] = {
        "status": "scientific",
        "stochastic": {
            "target": "conditional_mean",
            "random_source": "simulator_seed",
            "intrinsic_variance": "report",
            "common_random_numbers": "within_trajectory",
            "allocation": {
                "phase": "pilot",
                "variance_quantile": 0.9,
                "target_mcse": 0.01,
                "minimum_replications": 2,
                "maximum_replications": 10,
                "selected_replications": None,
                "provenance": None,
            },
        },
        "decision": {
            "claim": "Identify inputs requiring further study",
            "use": "diagnostic",
            "setting": "Nantes population synthesis validation",
            "objective": "screening",
            "estimand": "Expected total-variation distance over simulation seeds",
            "primary_indicator": "tv_distance(age, ref=ref)",
        },
        "uncertainty": {
            "dependence": {
                "kind": "independent",
                "rationale": "No dependence is assumed for this design",
            },
            "inventory": {
                "parameters": [item("a", "varied"), item("b", "varied")],
                "data": [item("survey", "fixed")],
                "structures": [item("model", "excluded")],
                "scenarios": [],
                "numerics": [],
            },
        },
        "budget": {"maximum_runs": 100, "reserve_fraction": 0.1},
        "stopping": {
            "criteria": [
                {"metric": "rank_stability", "threshold": 0.05, "consecutive_checks": 2}
            ]
        },
        "robustness": {
            "checks": [
                {
                    "name": "seed sensitivity",
                    "rationale": "Verify conclusions across simulation seeds",
                }
            ]
        },
    }
    return document


def _load_scientific(tmp_path: Path, mutate=None):  # type: ignore[no-untyped-def]
    meta, _, _, _ = meta_fixture(tmp_path)
    document = _scientific(meta)
    if mutate:
        mutate(document)
    _write(meta, document)
    return load_meta_config(meta)


def test_missing_and_explicit_demonstration_charters_give_the_same_plan_bytes(
    tmp_path: Path,
) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    loaded = load_meta_config(meta)
    assert loaded.charter.status is CharterStatus.DEMONSTRATION
    assert loaded.distributions == {}
    plan_from_meta_config(meta)
    implicit = (tmp_path / "campaign/plan.parquet").read_bytes()
    shutil.rmtree(tmp_path / "campaign")

    document = _document(meta)
    document["charter"] = {"status": "demonstration"}
    _write(meta, document)
    explicit = load_meta_config(meta)
    assert explicit.charter.status is CharterStatus.DEMONSTRATION
    plan_from_meta_config(meta)
    assert (tmp_path / "campaign/plan.parquet").read_bytes() == implicit


@pytest.mark.parametrize(
    "charter",
    [
        {},
        {"status": "demo"},
        {"status": "demonstration", "extra": True},
        "demonstration",
    ],
)
def test_explicit_demonstration_is_strict(tmp_path: Path, charter: object) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    document = _document(meta)
    document["charter"] = charter
    _write(meta, document)
    with pytest.raises((TypeError, ValueError), match="charter|demonstration"):
        load_meta_config(meta)


def test_valid_scientific_charter_is_fully_typed(tmp_path: Path) -> None:
    loaded = _load_scientific(tmp_path)
    assert isinstance(loaded.charter, ScientificCharter)
    assert loaded.charter.decision.use is DecisionUse.DIAGNOSTIC
    assert loaded.charter.decision.objective is SensitivityObjective.SCREENING
    assert loaded.charter.decision.estimand.startswith("Expected")
    assert loaded.charter.inventory.parameters[0].status is InventoryStatus.VARIED
    assert set(loaded.distributions) == {"a", "b"}
    assert loaded.distributions["a"].family == "discrete_uniform"
    assert isinstance(loaded.charter.stochastic, ConditionalMeanStochastic)
    assert loaded.charter.stochastic.allocation.phase is AllocationPhase.PILOT


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda d: d["charter"].pop("stochastic"), "scientific charter keys"),
        (lambda d: d["charter"]["stochastic"].update(extra=True), "stochastic keys"),
        (
            lambda d: d["charter"]["stochastic"].update(random_source="ambient"),
            "constants",
        ),
        (
            lambda d: d["charter"]["stochastic"]["allocation"].update(
                variance_quantile=0.0
            ),
            "variance_quantile",
        ),
        (
            lambda d: d["charter"]["stochastic"]["allocation"].update(target_mcse=0.0),
            "target_mcse",
        ),
        (
            lambda d: d["charter"]["stochastic"]["allocation"].update(
                minimum_replications=1
            ),
            "minimum_replications",
        ),
        (
            lambda d: d["charter"]["stochastic"]["allocation"].update(
                maximum_replications=1
            ),
            "maximum_replications",
        ),
        (
            lambda d: d["charter"]["stochastic"]["allocation"].update(
                selected_replications=2
            ),
            "pilot requires",
        ),
    ],
)
def test_conditional_mean_stochastic_contract_is_strict(
    tmp_path: Path, mutate, message: str
) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises((TypeError, ValueError), match=message):
        _load_scientific(tmp_path, mutate)


@pytest.mark.parametrize("quantile", [0.000001, 1.0])
def test_conditional_mean_accepts_quantile_boundaries(
    tmp_path: Path, quantile: float
) -> None:
    loaded = _load_scientific(
        tmp_path,
        lambda d: d["charter"]["stochastic"]["allocation"].update(
            variance_quantile=quantile
        ),
    )
    assert loaded.charter.stochastic.allocation.variance_quantile == quantile


def test_finite_seed_factor_is_exact_typed_and_forbids_allocation(
    tmp_path: Path,
) -> None:
    finite = {
        "target": "finite_seed_factor",
        "random_source": "simulator_seed",
        "intrinsic_variance": "not_identified",
        "common_random_numbers": "none",
        "support_interpretation": "declared_finite_nuisance",
    }
    loaded = _load_scientific(
        tmp_path, lambda d: d["charter"].update(stochastic=finite)
    )
    assert isinstance(loaded.charter.stochastic, FiniteSeedFactorStochastic)
    (tmp_path / "invalid").mkdir()
    with pytest.raises(ValueError, match="stochastic keys"):
        _load_scientific(
            tmp_path / "invalid",
            lambda d: d["charter"].update(
                stochastic={**finite, "allocation": {}}
            ),
        )


def test_typed_stochastic_serializer_revalidates_manual_allocation() -> None:
    stochastic = ConditionalMeanStochastic(
        ReplicationAllocation(
            AllocationPhase.PILOT,
            0.9,
            0.1,
            1,
            2,
            None,
            None,
        )
    )
    with pytest.raises(ValueError, match="minimum_replications"):
        serialize_stochastic(stochastic)


@pytest.mark.parametrize("use", ["diagnostic", "prognostic"])
@pytest.mark.parametrize("objective", ["screening", "fp", "ff"])
def test_decision_enums_and_ff_tolerance(
    tmp_path: Path, use: str, objective: str
) -> None:
    def mutate(document):  # type: ignore[no-untyped-def]
        decision = document["charter"]["decision"]
        decision.update(use=use, objective=objective)
        if objective == "ff":
            decision["practical_tolerance"] = 0.0

    loaded = _load_scientific(tmp_path, mutate)
    assert loaded.charter.decision.use.value == use
    assert loaded.charter.decision.objective.value == objective


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda d: d["charter"]["decision"].update(extra="x"), "decision keys"),
        (lambda d: d["charter"]["decision"].pop("claim"), "decision keys"),
        (lambda d: d["charter"]["decision"].update(use="causal"), "use"),
        (lambda d: d["charter"]["decision"].update(objective="ranking"), "objective"),
        (lambda d: d["charter"]["decision"].update(setting=""), "setting"),
        (lambda d: d["charter"]["decision"].update(estimand=""), "estimand"),
        (lambda d: d["charter"]["decision"].update(claim=""), "claim"),
        (
            lambda d: d["charter"]["decision"].update(primary_indicator="unknown"),
            "resolved indicator",
        ),
        (
            lambda d: d["charter"]["decision"].update(practical_tolerance=0.1),
            "decision keys",
        ),
        (
            lambda d: d["charter"]["decision"].update(
                objective="ff", practical_tolerance=-0.1
            ),
            "practical_tolerance",
        ),
        (lambda d: d["charter"]["decision"].update(objective="ff"), "decision keys"),
    ],
)
def test_scientific_decision_schema_is_strict(tmp_path: Path, mutate, message: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises((TypeError, ValueError), match=message):
        _load_scientific(tmp_path, mutate)


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda d: d["parameters"]["a"].pop("distribution"), "distribution"),
        (
            lambda d: d["parameters"]["a"]["distribution"].update(extra="x"),
            "distribution.*keys",
        ),
        (
            lambda d: d["parameters"]["a"]["distribution"].update(family="normal"),
            "discrete_uniform",
        ),
        (
            lambda d: d["parameters"]["a"]["distribution"].update(transform="log"),
            "identity",
        ),
        (
            lambda d: d["parameters"]["a"]["distribution"].update(provenance=""),
            "provenance",
        ),
    ],
)
def test_scientific_distribution_contract(tmp_path: Path, mutate, message: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises((TypeError, ValueError), match=message):
        _load_scientific(tmp_path, mutate)


@pytest.mark.parametrize("values", [[], [1], [1, 1]])
def test_scientific_discrete_uniform_support_must_vary(
    tmp_path: Path, values: list
) -> None:
    with pytest.raises(ValueError, match="at least two distinct values"):
        _load_scientific(
            tmp_path, lambda d: d["parameters"]["a"].update(values=values)
        )


@pytest.mark.parametrize(
    "mutate,message",
    [
        (
            lambda d: d["charter"]["uncertainty"]["inventory"].pop("numerics"),
            "inventory.*keys",
        ),
        (
            lambda d: d["charter"]["uncertainty"]["inventory"]["parameters"].pop(),
            "exactly match",
        ),
        (
            lambda d: d["charter"]["uncertainty"]["inventory"]["parameters"][0].update(status="fixed"),
            "exactly match",
        ),
        (
            lambda d: d["charter"]["uncertainty"]["inventory"]["parameters"].append(
                copy.deepcopy(d["charter"]["uncertainty"]["inventory"]["parameters"][0])
            ),
            "unique",
        ),
        (
            lambda d: d["charter"]["uncertainty"]["inventory"]["data"][0].update(status="unknown"),
            "inventory status",
        ),
        (
            lambda d: d["charter"]["uncertainty"]["inventory"]["data"][0].pop("rationale"),
            "keys",
        ),
    ],
)
def test_inventory_is_complete_and_varied_parameters_match(
    tmp_path: Path, mutate, message: str
) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises((TypeError, ValueError), match=message):
        _load_scientific(tmp_path, mutate)


def _declared(document: dict) -> dict:
    dependence = document["charter"]["uncertainty"]["dependence"]
    dependence.clear()
    dependence.update(
        kind="declared",
        rationale="Dependence represented explicitly",
        groups=[
            {
                "factors": ["a", "b"],
                "model": "joint empirical distribution",
                "provenance": "survey documentation",
            }
        ],
    )
    return dependence


def test_declared_dependence_is_typed(tmp_path: Path) -> None:
    loaded = _load_scientific(tmp_path, _declared)
    assert loaded.charter.dependence.groups[0].factors == ("a", "b")


@pytest.mark.parametrize(
    "mutate,message",
    [
        (
            lambda d: d["charter"]["uncertainty"]["dependence"].update(groups=[]),
            "keys",
        ),
        (lambda d: _declared(d).update(groups=[]), "non-empty"),
        (
            lambda d: _declared(d)["groups"][0].update(factors=["a", "a"]),
            "duplicate-free",
        ),
        (
            lambda d: _declared(d)["groups"][0].update(factors=["a", "unknown"]),
            "unknown factors",
        ),
        (
            lambda d: _declared(d)["groups"].append(
                {"factors": ["a", "b"], "model": "m", "provenance": "p"}
            ),
            "overlap",
        ),
    ],
)
def test_dependence_validation(tmp_path: Path, mutate, message: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises((TypeError, ValueError), match=message):
        _load_scientific(tmp_path, mutate)


@pytest.mark.parametrize("maximum_runs", [1, 2**63 - 1])
@pytest.mark.parametrize("reserve", [0.0, 0.999999])
def test_budget_boundaries(tmp_path: Path, maximum_runs: int, reserve: float) -> None:
    loaded = _load_scientific(
        tmp_path,
        lambda d: d["charter"]["budget"].update(
            maximum_runs=maximum_runs, reserve_fraction=reserve
        ),
    )
    assert loaded.charter.budget.maximum_runs == maximum_runs
    assert loaded.charter.budget.reserve_fraction == reserve


@pytest.mark.parametrize(
    "field,value",
    [
        ("maximum_runs", 0),
        ("maximum_runs", True),
        ("maximum_runs", 2**63),
        ("reserve_fraction", 0),
        ("reserve_fraction", -0.1),
        ("reserve_fraction", 1.0),
    ],
)
def test_budget_rejects_invalid_boundaries(tmp_path: Path, field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        _load_scientific(
            tmp_path, lambda d: d["charter"]["budget"].update({field: value})
        )


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda d: d["charter"]["stopping"].update(criteria=[]), "non-empty"),
        (
            lambda d: d["charter"]["stopping"]["criteria"][0].update(threshold=0.0),
            "threshold",
        ),
        (
            lambda d: d["charter"]["stopping"]["criteria"][0].update(consecutive_checks=0),
            "consecutive_checks",
        ),
        (
            lambda d: d["charter"]["stopping"]["criteria"].append(
                {"metric": "rank_stability", "threshold": 0.1, "consecutive_checks": 1}
            ),
            "unique",
        ),
    ],
)
def test_stopping_contract(tmp_path: Path, mutate, message: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises((TypeError, ValueError), match=message):
        _load_scientific(tmp_path, mutate)


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda d: d["charter"]["robustness"].update(checks=[]), "non-empty"),
        (
            lambda d: d["charter"]["robustness"]["checks"].append(
                copy.deepcopy(d["charter"]["robustness"]["checks"][0])
            ),
            "unique",
        ),
        (
            lambda d: d["charter"]["robustness"]["checks"][0].update(rationale=""),
            "rationale",
        ),
    ],
)
def test_robustness_contract(tmp_path: Path, mutate, message: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises((TypeError, ValueError), match=message):
        _load_scientific(tmp_path, mutate)


def test_scientific_requires_an_indicator(tmp_path: Path) -> None:
    def mutate(document):  # type: ignore[no-untyped-def]
        document.pop("indicators")
        document["outputs"] = {"persons": {}}

    with pytest.raises(ValueError, match="scientific.*indicator"):
        _load_scientific(tmp_path, mutate)


def test_scientific_demonstration_method_guard_creates_no_plan(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    _write(meta, _scientific(meta))
    load_meta_config(meta)
    with pytest.raises(
        ValueError,
        match="no current method satisfies the scientific contract.*demonstration-only",
    ):
        plan_from_meta_config(meta)
    assert not (tmp_path / "campaign/plan.parquet").exists()


def test_scientific_demonstration_method_guard_prevents_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    _write(meta, _scientific(meta))
    monkeypatch.setattr(
        "eqasim_sa.adapters.application.analyze",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("analysis called")),
    )
    with pytest.raises(ValueError, match="scientific contract.*demonstration-only"):
        analyze_from_meta_config(meta)
