import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from eqasim_sa.core.indicators import (
    indicator_definition_sha256,
    validate_indicator_definition,
)


class CharterStatus(StrEnum):
    DEMONSTRATION = "demonstration"
    SCIENTIFIC = "scientific"


class DecisionUse(StrEnum):
    DIAGNOSTIC = "diagnostic"
    PROGNOSTIC = "prognostic"


class SensitivityObjective(StrEnum):
    SCREENING = "screening"
    FACTOR_PRIORITIZATION = "fp"
    FACTOR_FIXING = "ff"


class InventoryStatus(StrEnum):
    VARIED = "varied"
    FIXED = "fixed"
    EXCLUDED = "excluded"


class DependenceKind(StrEnum):
    INDEPENDENT = "independent"
    DECLARED = "declared"


class StochasticTarget(StrEnum):
    CONDITIONAL_MEAN = "conditional_mean"
    FINITE_SEED_FACTOR = "finite_seed_factor"


class AllocationPhase(StrEnum):
    PILOT = "pilot"
    PRODUCTION = "production"


@dataclass(frozen=True)
class ParameterDistribution:
    family: str
    transform: str
    provenance: str


@dataclass(frozen=True)
class DemonstrationCharter:
    status: CharterStatus = CharterStatus.DEMONSTRATION
    legacy: bool = False


@dataclass(frozen=True)
class Decision:
    claim: str
    use: DecisionUse
    setting: str
    objective: SensitivityObjective
    estimand: str
    primary_indicator: str
    practical_tolerance: float | None
    primary_indicator_definition: dict[str, object] | None = None
    primary_indicator_definition_sha256: str | None = None


@dataclass(frozen=True)
class DependenceGroup:
    factors: tuple[str, ...]
    model: str
    provenance: str


@dataclass(frozen=True)
class Dependence:
    kind: DependenceKind
    rationale: str
    groups: tuple[DependenceGroup, ...] = ()


@dataclass(frozen=True)
class InventoryItem:
    name: str
    status: InventoryStatus
    provenance: str
    rationale: str


@dataclass(frozen=True)
class UncertaintyInventory:
    parameters: tuple[InventoryItem, ...]
    data: tuple[InventoryItem, ...]
    structures: tuple[InventoryItem, ...]
    scenarios: tuple[InventoryItem, ...]
    numerics: tuple[InventoryItem, ...]


@dataclass(frozen=True)
class Budget:
    maximum_runs: int
    reserve_fraction: float


@dataclass(frozen=True)
class StoppingCriterion:
    metric: str
    threshold: float
    consecutive_checks: int


@dataclass(frozen=True)
class RobustnessCheck:
    name: str
    rationale: str


@dataclass(frozen=True)
class ReplicationAllocation:
    phase: AllocationPhase
    variance_quantile: float
    target_mcse: float
    minimum_replications: int
    maximum_replications: int
    selected_replications: int | None
    provenance: str | None


@dataclass(frozen=True)
class ConditionalMeanStochastic:
    allocation: ReplicationAllocation
    target: StochasticTarget = field(default=StochasticTarget.CONDITIONAL_MEAN, init=False)
    random_source: str = field(default="simulator_seed", init=False)
    intrinsic_variance: str = field(default="report", init=False)
    common_random_numbers: str = field(default="within_trajectory", init=False)


@dataclass(frozen=True)
class FiniteSeedFactorStochastic:
    target: StochasticTarget = field(default=StochasticTarget.FINITE_SEED_FACTOR, init=False)
    random_source: str = field(default="simulator_seed", init=False)
    intrinsic_variance: str = field(default="not_identified", init=False)
    common_random_numbers: str = field(default="none", init=False)
    support_interpretation: str = field(
        default="declared_finite_nuisance", init=False
    )


ScientificStochastic = ConditionalMeanStochastic | FiniteSeedFactorStochastic


@dataclass(frozen=True)
class ScientificCharter:
    decision: Decision
    dependence: Dependence
    inventory: UncertaintyInventory
    budget: Budget
    stopping: tuple[StoppingCriterion, ...]
    robustness: tuple[RobustnessCheck, ...]
    stochastic: ScientificStochastic
    status: CharterStatus = CharterStatus.SCIENTIFIC


AnalysisCharter = DemonstrationCharter | ScientificCharter

INVENTORY_CATEGORIES = (
    "parameters",
    "data",
    "structures",
    "scenarios",
    "numerics",
)
INT64_MAX = 2**63 - 1


def validate_scientific_stopping(
    method: str,
    criteria: Sequence[StoppingCriterion | dict],
    checkpoint_count: int,
) -> None:
    """Validate the stopping declaration that a scientific method can evaluate."""
    if type(checkpoint_count) is not int or checkpoint_count <= 0:
        raise ValueError("scientific stopping checkpoint count must be positive")
    supported = {
        "canonical_morris": ("rank_stability", checkpoint_count - 1),
        "multiscramble_sobol": ("ci_width", checkpoint_count),
    }
    if method not in supported:
        raise ValueError(f"scientific stopping is not supported for method {method!r}")
    metric, maximum_checks = supported[method]
    if not isinstance(criteria, (list, tuple)) or len(criteria) != 1:
        raise ValueError(
            f"{method} stopping must declare exactly one {metric} criterion"
        )
    criterion = criteria[0]
    if isinstance(criterion, StoppingCriterion):
        actual_metric = criterion.metric
        consecutive_checks = criterion.consecutive_checks
    elif isinstance(criterion, dict):
        actual_metric = criterion.get("metric")
        consecutive_checks = criterion.get("consecutive_checks")
    else:
        raise TypeError(f"{method} stopping criterion must be a mapping")
    if actual_metric != metric:
        raise ValueError(
            f"{method} stopping must declare exactly the {metric} metric"
        )
    if type(consecutive_checks) is not int or consecutive_checks <= 0:
        raise ValueError(
            f"{method} stopping consecutive_checks must be a positive integer"
        )
    if consecutive_checks > maximum_checks:
        raise ValueError(
            f"{method} stopping consecutive_checks must be at most "
            f"{maximum_checks} for {checkpoint_count} checkpoints"
        )


def _mapping(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be a mapping")
    return value


def _exact(document: dict, keys: set[str], name: str) -> None:
    if set(document) != keys:
        raise ValueError(f"{name} keys must be exactly {', '.join(sorted(keys))}")


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _float(value: object, name: str, *, positive: bool = False) -> float:
    if type(value) is not float or not math.isfinite(value) or (
        value <= 0 if positive else value < 0
    ):
        qualifier = "positive" if positive else "nonnegative"
        raise ValueError(f"{name} must be a finite {qualifier} float")
    return value


def _positive_int64(value: object, name: str) -> int:
    if type(value) is not int or not 1 <= value <= INT64_MAX:
        raise ValueError(f"{name} must be a positive signed-int64 integer")
    return value


def _replication_count(value: object, name: str) -> int:
    result = _positive_int64(value, name)
    if result < 2:
        raise ValueError(f"{name} must be an integer at least 2")
    return result


def parse_stochastic(value: object, name: str = "charter.stochastic") -> ScientificStochastic:
    document = _mapping(value, name)
    target = document.get("target")
    if target == StochasticTarget.CONDITIONAL_MEAN:
        _exact(
            document,
            {
                "target",
                "random_source",
                "intrinsic_variance",
                "common_random_numbers",
                "allocation",
            },
            name,
        )
        if (
            document["random_source"] != "simulator_seed"
            or document["intrinsic_variance"] != "report"
            or document["common_random_numbers"] != "within_trajectory"
        ):
            raise ValueError(f"{name} conditional_mean constants are invalid")
        raw = _mapping(document["allocation"], f"{name}.allocation")
        _exact(
            raw,
            {
                "phase",
                "variance_quantile",
                "target_mcse",
                "minimum_replications",
                "maximum_replications",
                "selected_replications",
                "provenance",
            },
            f"{name}.allocation",
        )
        try:
            phase = AllocationPhase(raw["phase"])
        except (TypeError, ValueError) as error:
            raise ValueError(f"{name}.allocation.phase must be pilot or production") from error
        quantile = raw["variance_quantile"]
        if type(quantile) is not float or not math.isfinite(quantile) or not 0 < quantile <= 1:
            raise ValueError(
                f"{name}.allocation.variance_quantile must be a finite float in (0,1]"
            )
        target_mcse = _float(
            raw["target_mcse"], f"{name}.allocation.target_mcse", positive=True
        )
        minimum = _replication_count(
            raw["minimum_replications"], f"{name}.allocation.minimum_replications"
        )
        maximum = _replication_count(
            raw["maximum_replications"], f"{name}.allocation.maximum_replications"
        )
        if maximum < minimum:
            raise ValueError(
                f"{name}.allocation.maximum_replications must be at least minimum_replications"
            )
        selected, provenance = raw["selected_replications"], raw["provenance"]
        if phase is AllocationPhase.PILOT:
            if selected is not None or provenance is not None:
                raise ValueError(
                    f"{name}.allocation pilot requires selected_replications and provenance null"
                )
        else:
            if type(selected) is not int or not minimum <= selected <= maximum:
                raise ValueError(
                    f"{name}.allocation production selected_replications must be within minimum/maximum"
                )
            provenance = _text(provenance, f"{name}.allocation.provenance")
        return ConditionalMeanStochastic(
            ReplicationAllocation(
                phase,
                quantile,
                target_mcse,
                minimum,
                maximum,
                selected,
                provenance,
            )
        )
    if target == StochasticTarget.FINITE_SEED_FACTOR:
        _exact(
            document,
            {
                "target",
                "random_source",
                "intrinsic_variance",
                "common_random_numbers",
                "support_interpretation",
            },
            name,
        )
        if document != {
            "target": "finite_seed_factor",
            "random_source": "simulator_seed",
            "intrinsic_variance": "not_identified",
            "common_random_numbers": "none",
            "support_interpretation": "declared_finite_nuisance",
        }:
            raise ValueError(f"{name} finite_seed_factor constants are invalid")
        return FiniteSeedFactorStochastic()
    raise ValueError(f"{name}.target must be conditional_mean or finite_seed_factor")


def serialize_stochastic(stochastic: ScientificStochastic) -> dict:
    if isinstance(stochastic, ConditionalMeanStochastic):
        allocation = stochastic.allocation
        document = {
            "target": stochastic.target.value,
            "random_source": stochastic.random_source,
            "intrinsic_variance": stochastic.intrinsic_variance,
            "common_random_numbers": stochastic.common_random_numbers,
            "allocation": {
                "phase": allocation.phase.value,
                "variance_quantile": allocation.variance_quantile,
                "target_mcse": allocation.target_mcse,
                "minimum_replications": allocation.minimum_replications,
                "maximum_replications": allocation.maximum_replications,
                "selected_replications": allocation.selected_replications,
                "provenance": allocation.provenance,
            },
        }
    elif isinstance(stochastic, FiniteSeedFactorStochastic):
        document = {
            "target": stochastic.target.value,
            "random_source": stochastic.random_source,
            "intrinsic_variance": stochastic.intrinsic_variance,
            "common_random_numbers": stochastic.common_random_numbers,
            "support_interpretation": stochastic.support_interpretation,
        }
    else:
        raise TypeError("scientific stochastic contract has an unsupported type")
    parse_stochastic(document, "scientific stochastic contract")
    return document


def charter_status(value: object | None) -> CharterStatus:
    if value is None:
        return CharterStatus.DEMONSTRATION
    document = _mapping(value, "charter")
    status = document.get("status")
    if status == CharterStatus.DEMONSTRATION:
        _exact(document, {"status"}, "demonstration charter")
        return CharterStatus.DEMONSTRATION
    if status == CharterStatus.SCIENTIFIC:
        return CharterStatus.SCIENTIFIC
    raise ValueError("charter.status must be demonstration or scientific")


def parse_distribution(value: object, name: str) -> ParameterDistribution:
    document = _mapping(value, f"parameter {name!r} distribution")
    _exact(
        document,
        {"family", "transform", "provenance"},
        f"parameter {name!r} distribution",
    )
    if document["family"] != "discrete_uniform":
        raise ValueError(f"parameter {name!r} distribution.family must be discrete_uniform")
    if document["transform"] != "identity":
        raise ValueError(f"parameter {name!r} distribution.transform must be identity")
    return ParameterDistribution(
        document["family"],
        document["transform"],
        _text(document["provenance"], f"parameter {name!r} distribution.provenance"),
    )


def _decision(
    value: object,
    indicator_names: set[str],
    indicator_definitions: dict[str, dict[str, object]] | None,
) -> Decision:
    document = _mapping(value, "charter.decision")
    base = {"claim", "use", "setting", "objective", "estimand", "primary_indicator"}
    objective = document.get("objective")
    keys = base | ({"practical_tolerance"} if objective == "ff" else set())
    _exact(document, keys, "charter.decision")
    try:
        use = DecisionUse(document["use"])
    except (TypeError, ValueError) as error:
        raise ValueError("charter.decision.use must be diagnostic or prognostic") from error
    try:
        parsed_objective = SensitivityObjective(objective)
    except (TypeError, ValueError) as error:
        raise ValueError("charter.decision.objective must be screening, fp, or ff") from error
    primary = _text(
        document["primary_indicator"], "charter.decision.primary_indicator"
    )
    if primary not in indicator_names:
        raise ValueError(
            "charter.decision.primary_indicator must equal a resolved indicator name"
        )
    tolerance = (
        _float(document["practical_tolerance"], "charter.decision.practical_tolerance")
        if parsed_objective is SensitivityObjective.FACTOR_FIXING
        else None
    )
    definition = (
        None
        if indicator_definitions is None
        else indicator_definitions.get(primary)
    )
    if indicator_definitions is not None and definition is None:
        raise ValueError(
            "charter.decision.primary_indicator has no canonical definition"
        )
    definition_sha256 = None
    if definition is not None:
        validate_indicator_definition(definition, require_scientific=True)
        if definition["name"] != primary:
            raise ValueError("primary indicator definition name does not match decision")
        definition_sha256 = indicator_definition_sha256(definition)
    return Decision(
        _text(document["claim"], "charter.decision.claim"),
        use,
        _text(document["setting"], "charter.decision.setting"),
        parsed_objective,
        _text(document["estimand"], "charter.decision.estimand"),
        primary,
        tolerance,
        definition,
        definition_sha256,
    )


def _dependence(value: object, parameter_names: set[str]) -> Dependence:
    document = _mapping(value, "charter.uncertainty.dependence")
    kind = document.get("kind")
    expected = (
        {"kind", "rationale", "groups"}
        if kind == DependenceKind.DECLARED
        else {"kind", "rationale"}
    )
    _exact(document, expected, "charter.uncertainty.dependence")
    rationale = _text(
        document["rationale"], "charter.uncertainty.dependence.rationale"
    )
    if kind == DependenceKind.INDEPENDENT:
        return Dependence(DependenceKind.INDEPENDENT, rationale)
    if kind != DependenceKind.DECLARED:
        raise ValueError(
            "charter.uncertainty.dependence.kind must be independent or declared"
        )
    raw_groups = document["groups"]
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValueError("declared dependence requires a non-empty groups list")
    groups, used = [], set()
    for index, raw in enumerate(raw_groups):
        group = _mapping(raw, f"dependence group {index}")
        _exact(group, {"factors", "model", "provenance"}, f"dependence group {index}")
        factors = group["factors"]
        if (
            not isinstance(factors, list)
            or len(factors) < 2
            or not all(isinstance(item, str) and item for item in factors)
            or len(set(factors)) != len(factors)
        ):
            raise ValueError(
                "dependence group factors must be a duplicate-free list of at least two names"
            )
        unknown = set(factors) - parameter_names
        if unknown:
            raise ValueError(f"dependence group contains unknown factors: {sorted(unknown)}")
        overlap = used & set(factors)
        if overlap:
            raise ValueError(f"dependence groups overlap: {sorted(overlap)}")
        used.update(factors)
        groups.append(
            DependenceGroup(
                tuple(factors),
                _text(group["model"], f"dependence group {index}.model"),
                _text(group["provenance"], f"dependence group {index}.provenance"),
            )
        )
    return Dependence(DependenceKind.DECLARED, rationale, tuple(groups))


def _inventory(
    value: object, parameter_names: set[str]
) -> UncertaintyInventory:
    document = _mapping(value, "charter.uncertainty.inventory")
    _exact(document, set(INVENTORY_CATEGORIES), "charter.uncertainty.inventory")
    result = {}
    for category in INVENTORY_CATEGORIES:
        raw_items = document[category]
        if not isinstance(raw_items, list):
            raise TypeError(f"inventory.{category} must be a list")
        items, names = [], set()
        for index, raw in enumerate(raw_items):
            item = _mapping(raw, f"inventory.{category}[{index}]")
            _exact(
                item,
                {"name", "status", "provenance", "rationale"},
                f"inventory.{category}[{index}]",
            )
            name = _text(item["name"], f"inventory.{category}[{index}].name")
            if name in names:
                raise ValueError(f"inventory.{category} names must be unique")
            names.add(name)
            try:
                status = InventoryStatus(item["status"])
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "inventory status must be varied, fixed, or excluded"
                ) from error
            items.append(
                InventoryItem(
                    name,
                    status,
                    _text(
                        item["provenance"],
                        f"inventory.{category}[{index}].provenance",
                    ),
                    _text(item["rationale"], f"inventory.{category}[{index}].rationale"),
                )
            )
        result[category] = tuple(items)
    inventory = UncertaintyInventory(**result)
    varied = {
        item.name
        for item in inventory.parameters
        if item.status is InventoryStatus.VARIED
    }
    if varied != parameter_names:
        raise ValueError(
            "varied parameter inventory names must exactly match YAML parameter names"
        )
    return inventory


def parse_charter(
    value: object | None,
    parameter_names: set[str],
    indicator_names: set[str],
    indicator_definitions: dict[str, dict[str, object]] | None = None,
) -> AnalysisCharter:
    status = charter_status(value)
    if status is CharterStatus.DEMONSTRATION:
        return DemonstrationCharter(legacy=value is None)
    if not indicator_names:
        raise ValueError("scientific charter requires at least one indicator")
    document = _mapping(value, "charter")
    _exact(
        document,
        {"status", "decision", "uncertainty", "budget", "stopping", "robustness", "stochastic"},
        "scientific charter",
    )
    uncertainty = _mapping(document["uncertainty"], "charter.uncertainty")
    _exact(uncertainty, {"dependence", "inventory"}, "charter.uncertainty")
    budget = _mapping(document["budget"], "charter.budget")
    _exact(budget, {"maximum_runs", "reserve_fraction"}, "charter.budget")
    maximum_runs = _positive_int64(
        budget["maximum_runs"], "charter.budget.maximum_runs"
    )
    reserve = budget["reserve_fraction"]
    if type(reserve) is not float or not math.isfinite(reserve) or not 0 <= reserve < 1:
        raise ValueError("charter.budget.reserve_fraction must be a finite float in [0,1)")
    stopping = _mapping(document["stopping"], "charter.stopping")
    _exact(stopping, {"criteria"}, "charter.stopping")
    raw_criteria = stopping["criteria"]
    if not isinstance(raw_criteria, list) or not raw_criteria:
        raise ValueError("charter.stopping.criteria must be a non-empty list")
    criteria, metrics = [], set()
    for index, raw in enumerate(raw_criteria):
        criterion = _mapping(raw, f"stopping criterion {index}")
        _exact(
            criterion,
            {"metric", "threshold", "consecutive_checks"},
            f"stopping criterion {index}",
        )
        metric = _text(criterion["metric"], f"stopping criterion {index}.metric")
        if metric in metrics:
            raise ValueError("stopping criterion metrics must be unique")
        metrics.add(metric)
        criteria.append(
            StoppingCriterion(
                metric,
                _float(
                    criterion["threshold"],
                    f"stopping criterion {index}.threshold",
                    positive=True,
                ),
                _positive_int64(
                    criterion["consecutive_checks"],
                    f"stopping criterion {index}.consecutive_checks",
                ),
            )
        )
    robustness = _mapping(document["robustness"], "charter.robustness")
    _exact(robustness, {"checks"}, "charter.robustness")
    raw_checks = robustness["checks"]
    if not isinstance(raw_checks, list) or not raw_checks:
        raise ValueError("charter.robustness.checks must be a non-empty list")
    checks, names = [], set()
    for index, raw in enumerate(raw_checks):
        check = _mapping(raw, f"robustness check {index}")
        _exact(check, {"name", "rationale"}, f"robustness check {index}")
        name = _text(check["name"], f"robustness check {index}.name")
        if name in names:
            raise ValueError("robustness check names must be unique")
        names.add(name)
        checks.append(
            RobustnessCheck(
                name,
                _text(check["rationale"], f"robustness check {index}.rationale"),
            )
        )
    return ScientificCharter(
        _decision(document["decision"], indicator_names, indicator_definitions),
        _dependence(uncertainty["dependence"], parameter_names),
        _inventory(uncertainty["inventory"], parameter_names),
        Budget(maximum_runs, reserve),
        tuple(criteria),
        tuple(checks),
        parse_stochastic(document["stochastic"]),
    )


def validate_scientific_contract(
    contract: object,
    factors: dict[str, list],
    distributions: dict[str, ParameterDistribution | dict],
    *,
    allowed_objectives: frozenset[str] = frozenset({"screening"}),
) -> None:
    """Validate the normalized scientific contract for a method's objectives."""
    try:
        json.dumps(contract, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("scientific_contract must be JSON-safe") from error
    document = _mapping(contract, "scientific_contract")
    _exact(
        document,
        {"schemaVersion", "software", "charter", "parameters"},
        "scientific_contract",
    )
    if document["schemaVersion"] != 2 or type(document["schemaVersion"]) is not int:
        raise ValueError("scientific_contract schemaVersion must equal 2")
    software = _mapping(document["software"], "scientific_contract.software")
    _exact(
        software,
        {"eqasimSaVersion", "eqasimSaSha256"},
        "scientific_contract.software",
    )
    _text(software["eqasimSaVersion"], "scientific_contract software version")
    software_sha256 = software["eqasimSaSha256"]
    if (
        not isinstance(software_sha256, str)
        or len(software_sha256) != 64
        or any(character not in "0123456789abcdef" for character in software_sha256)
    ):
        raise ValueError("scientific_contract software digest must be lowercase SHA-256")
    charter = _mapping(document["charter"], "scientific_contract.charter")
    _exact(
        charter,
        {"status", "decision", "uncertainty", "budget", "stopping", "robustness", "stochastic"},
        "scientific_contract.charter",
    )
    if charter["status"] != CharterStatus.SCIENTIFIC.value:
        raise ValueError("scientific_contract charter status must be scientific")
    stochastic = parse_stochastic(
        charter["stochastic"], "scientific_contract.charter.stochastic"
    )

    decision = _mapping(charter["decision"], "scientific_contract.charter.decision")
    _exact(
        decision,
        {
            "claim",
            "use",
            "setting",
            "objective",
            "estimand",
            "primary_indicator",
            "primary_indicator_definition",
            "primary_indicator_definition_sha256",
            "practical_tolerance",
        },
        "scientific_contract.charter.decision",
    )
    for key in ("claim", "setting", "estimand", "primary_indicator"):
        if "\0" in _text(decision[key], f"scientific_contract decision {key}"):
            raise ValueError(f"scientific_contract decision {key} must not contain NUL")
    primary_definition = validate_indicator_definition(
        decision["primary_indicator_definition"], require_scientific=True
    )
    if primary_definition["name"] != decision["primary_indicator"]:
        raise ValueError(
            "scientific_contract primary indicator definition name does not match"
        )
    expected_definition_sha256 = indicator_definition_sha256(primary_definition)
    if decision["primary_indicator_definition_sha256"] != expected_definition_sha256:
        raise ValueError(
            "scientific_contract primary indicator definition digest does not match"
        )
    if decision["use"] not in {item.value for item in DecisionUse}:
        raise ValueError("scientific_contract decision use must be diagnostic or prognostic")
    if decision["objective"] not in allowed_objectives:
        if allowed_objectives == frozenset({SensitivityObjective.SCREENING.value}):
            raise ValueError("scientific_contract decision objective must be screening")
        choices = ", ".join(sorted(allowed_objectives))
        raise ValueError(f"scientific_contract decision objective must be one of {choices}")
    if decision["objective"] == SensitivityObjective.FACTOR_FIXING.value:
        _float(
            decision["practical_tolerance"],
            "scientific_contract factor-fixing practical_tolerance",
        )
    elif decision["practical_tolerance"] is not None:
        raise ValueError("scientific_contract practical_tolerance must be null unless objective is ff")

    uncertainty = _mapping(
        charter["uncertainty"], "scientific_contract.charter.uncertainty"
    )
    _exact(
        uncertainty,
        {"dependence", "inventory"},
        "scientific_contract.charter.uncertainty",
    )
    dependence = _mapping(
        uncertainty["dependence"], "scientific_contract dependence"
    )
    _exact(dependence, {"kind", "rationale", "groups"}, "scientific_contract dependence")
    if dependence["kind"] != DependenceKind.INDEPENDENT.value:
        raise ValueError("scientific_contract dependence must be independent")
    _text(dependence["rationale"], "scientific_contract dependence rationale")
    if dependence["groups"] != []:
        raise ValueError("scientific_contract independent dependence groups must be empty")

    inventory = _mapping(uncertainty["inventory"], "scientific_contract inventory")
    _exact(inventory, set(INVENTORY_CATEGORIES), "scientific_contract inventory")
    varied_parameters = set()
    for category in INVENTORY_CATEGORIES:
        items = inventory[category]
        if not isinstance(items, list):
            raise TypeError(f"scientific_contract inventory {category} must be a list")
        seen = set()
        for index, raw_item in enumerate(items):
            item = _mapping(raw_item, f"scientific_contract inventory {category}[{index}]")
            _exact(
                item,
                {"name", "status", "provenance", "rationale"},
                f"scientific_contract inventory {category}[{index}]",
            )
            name = _text(item["name"], f"scientific_contract inventory {category} name")
            if name in seen:
                raise ValueError(f"scientific_contract inventory {category} names must be unique")
            seen.add(name)
            if item["status"] not in {status.value for status in InventoryStatus}:
                raise ValueError("scientific_contract inventory status is invalid")
            _text(item["provenance"], "scientific_contract inventory provenance")
            _text(item["rationale"], "scientific_contract inventory rationale")
            if category == "parameters" and item["status"] == InventoryStatus.VARIED.value:
                varied_parameters.add(name)
    if varied_parameters != set(factors):
        raise ValueError("scientific_contract varied parameters must exactly match factors")

    budget = _mapping(charter["budget"], "scientific_contract budget")
    _exact(budget, {"maximum_runs", "reserve_fraction"}, "scientific_contract budget")
    _positive_int64(budget["maximum_runs"], "scientific_contract budget maximum_runs")
    reserve = budget["reserve_fraction"]
    if type(reserve) is not float or not math.isfinite(reserve) or not 0 <= reserve < 1:
        raise ValueError("scientific_contract reserve_fraction must be a finite float in [0,1)")

    stopping = charter["stopping"]
    if not isinstance(stopping, list) or not stopping:
        raise ValueError("scientific_contract stopping must be a non-empty list")
    metrics = set()
    for index, raw_criterion in enumerate(stopping):
        criterion = _mapping(raw_criterion, f"scientific_contract stopping[{index}]")
        _exact(
            criterion,
            {"metric", "threshold", "consecutive_checks"},
            f"scientific_contract stopping[{index}]",
        )
        metric = _text(criterion["metric"], f"scientific_contract stopping[{index}].metric")
        if metric in metrics:
            raise ValueError("scientific_contract stopping metrics must be unique")
        metrics.add(metric)
        _float(criterion["threshold"], f"scientific_contract stopping[{index}].threshold", positive=True)
        _positive_int64(
            criterion["consecutive_checks"],
            f"scientific_contract stopping[{index}].consecutive_checks",
        )

    robustness = charter["robustness"]
    if not isinstance(robustness, list) or not robustness:
        raise ValueError("scientific_contract robustness must be a non-empty list")
    check_names = set()
    for index, raw_check in enumerate(robustness):
        check = _mapping(raw_check, f"scientific_contract robustness[{index}]")
        _exact(check, {"name", "rationale"}, f"scientific_contract robustness[{index}]")
        name = _text(check["name"], f"scientific_contract robustness[{index}].name")
        if name in check_names:
            raise ValueError("scientific_contract robustness names must be unique")
        check_names.add(name)
        _text(check["rationale"], f"scientific_contract robustness[{index}].rationale")

    if set(factors) != set(distributions):
        raise ValueError("scientific_contract distributions must exactly match factors")
    parameters = document["parameters"]
    if not isinstance(parameters, list) or len(parameters) != len(factors):
        raise ValueError("scientific_contract parameters must exactly match factors")
    expected_parameters = []
    for name in sorted(factors):
        support = factors[name]
        if not isinstance(support, list) or len(support) < 2 or len(set(support)) != len(support):
            raise ValueError(f"scientific_contract factor {name!r} support is invalid")
        for value in support:
            if type(value) not in (int, float, str):
                raise TypeError(f"scientific_contract factor {name!r} support must contain scalars")
            if type(value) is int and not -(2**63) <= value < 2**63:
                raise ValueError(f"scientific_contract factor {name!r} integer is outside int64")
            if type(value) is float and not math.isfinite(value):
                raise ValueError(f"scientific_contract factor {name!r} float must be finite")
            if type(value) is str and (not value or "\0" in value):
                raise ValueError(f"scientific_contract factor {name!r} text is invalid")
        distribution = distributions[name]
        normalized_distribution = (
            {
                "family": distribution.family,
                "transform": distribution.transform,
                "provenance": distribution.provenance,
            }
            if isinstance(distribution, ParameterDistribution)
            else distribution
        )
        if not isinstance(normalized_distribution, dict):
            raise TypeError(f"scientific_contract distribution for {name!r} must be a mapping")
        _exact(
            normalized_distribution,
            {"family", "transform", "provenance"},
            f"scientific_contract distribution for {name!r}",
        )
        if (
            normalized_distribution["family"] != "discrete_uniform"
            or normalized_distribution["transform"] != "identity"
        ):
            raise ValueError(f"scientific_contract distribution for {name!r} is invalid")
        _text(
            normalized_distribution["provenance"],
            f"scientific_contract distribution for {name!r} provenance",
        )
        expected_parameters.append(
            {
                "name": name,
                "support": support,
                "distribution": normalized_distribution,
            }
        )
    if parameters != expected_parameters:
        raise ValueError(
            "scientific_contract parameters must exactly match sorted factor supports and distributions"
        )


def serialize_scientific_contract(
    charter: ScientificCharter,
    factors: dict[str, list],
    distributions: dict[str, ParameterDistribution],
    eqasim_sa_version: str,
    eqasim_sa_sha256: str,
) -> dict:
    if not isinstance(charter, ScientificCharter):
        raise TypeError("canonical scientific contract requires a ScientificCharter")
    if set(factors) != set(distributions):
        raise ValueError("scientific contract distributions must exactly match factors")
    decision = charter.decision
    if (
        decision.primary_indicator_definition is None
        or decision.primary_indicator_definition_sha256 is None
    ):
        raise ValueError("scientific contract requires a sealed primary indicator definition")
    contract = {
        "schemaVersion": 2,
        "software": {
            "eqasimSaVersion": eqasim_sa_version,
            "eqasimSaSha256": eqasim_sa_sha256,
        },
        "charter": {
            "status": charter.status.value,
            "decision": {
                "claim": decision.claim,
                "use": decision.use.value,
                "setting": decision.setting,
                "objective": decision.objective.value,
                "estimand": decision.estimand,
                "primary_indicator": decision.primary_indicator,
                "primary_indicator_definition": decision.primary_indicator_definition,
                "primary_indicator_definition_sha256": (
                    decision.primary_indicator_definition_sha256
                ),
                "practical_tolerance": decision.practical_tolerance,
            },
            "uncertainty": {
                "dependence": {
                    "kind": charter.dependence.kind.value,
                    "rationale": charter.dependence.rationale,
                    "groups": [
                        {
                            "factors": list(group.factors),
                            "model": group.model,
                            "provenance": group.provenance,
                        }
                        for group in charter.dependence.groups
                    ],
                },
                "inventory": {
                    category: [
                        {
                            "name": item.name,
                            "status": item.status.value,
                            "provenance": item.provenance,
                            "rationale": item.rationale,
                        }
                        for item in getattr(charter.inventory, category)
                    ]
                    for category in INVENTORY_CATEGORIES
                },
            },
            "budget": {
                "maximum_runs": charter.budget.maximum_runs,
                "reserve_fraction": charter.budget.reserve_fraction,
            },
            "stopping": [
                {
                    "metric": criterion.metric,
                    "threshold": criterion.threshold,
                    "consecutive_checks": criterion.consecutive_checks,
                }
                for criterion in charter.stopping
            ],
            "robustness": [
                {"name": check.name, "rationale": check.rationale}
                for check in charter.robustness
            ],
            "stochastic": serialize_stochastic(charter.stochastic),
        },
        "parameters": [
            {
                "name": name,
                "support": factors[name],
                "distribution": {
                    "family": distributions[name].family,
                    "transform": distributions[name].transform,
                    "provenance": distributions[name].provenance,
                },
            }
            for name in sorted(factors)
        ],
    }
    validate_scientific_contract(
        contract,
        factors,
        distributions,
        allowed_objectives=frozenset(
            {SensitivityObjective.SCREENING.value, SensitivityObjective.FACTOR_PRIORITIZATION.value, SensitivityObjective.FACTOR_FIXING.value}
        ),
    )
    return contract
