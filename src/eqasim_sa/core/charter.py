import math
from dataclasses import dataclass
from enum import StrEnum


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
class ScientificCharter:
    decision: Decision
    dependence: Dependence
    inventory: UncertaintyInventory
    budget: Budget
    stopping: tuple[StoppingCriterion, ...]
    robustness: tuple[RobustnessCheck, ...]
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


def _decision(value: object, indicator_names: set[str]) -> Decision:
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
    return Decision(
        _text(document["claim"], "charter.decision.claim"),
        use,
        _text(document["setting"], "charter.decision.setting"),
        parsed_objective,
        _text(document["estimand"], "charter.decision.estimand"),
        primary,
        tolerance,
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
) -> AnalysisCharter:
    status = charter_status(value)
    if status is CharterStatus.DEMONSTRATION:
        return DemonstrationCharter(legacy=value is None)
    if not indicator_names:
        raise ValueError("scientific charter requires at least one indicator")
    document = _mapping(value, "charter")
    _exact(
        document,
        {"status", "decision", "uncertainty", "budget", "stopping", "robustness"},
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
        _decision(document["decision"], indicator_names),
        _dependence(uncertainty["dependence"], parameter_names),
        _inventory(uncertainty["inventory"], parameter_names),
        Budget(maximum_runs, reserve),
        tuple(criteria),
        tuple(checks),
    )
