"""Construction of human- and machine-readable analysis reports."""

import math
import statistics
from typing import Mapping

import pyarrow as pa

from eqasim_sa.utils.arrow import rows_to_table

def _output_summary_table(
    plan: pa.Table,
    rows: list[tuple[str, str, float]],
    indicator_names: tuple[str, ...] | None = None,
) -> pa.Table:
    observations = {}
    for run_id, indicator, value in rows:
        key = run_id, indicator
        if key in observations:
            raise ValueError("multiple indicator values for one output-summary runId")
        observations[key] = value
    indicators = tuple(
        sorted(indicator_names or {indicator for _, indicator, _ in rows})
    )
    positions = plan["runId"].to_pylist()
    result = []
    for indicator in indicators:
        values = [
            observations[run_id, indicator]
            for run_id in positions
            if (run_id, indicator) in observations
        ]
        if not values:
            result.append((indicator, 0, None, None, None, None, None, None, None))
            continue
        ordered = sorted(values)

        def quantile(probability: float) -> float:
            position = probability * (len(ordered) - 1)
            lower = math.floor(position)
            fraction = position - lower
            upper = min(lower + 1, len(ordered) - 1)
            return math.fsum(
                ((1.0 - fraction) * ordered[lower], fraction * ordered[upper])
            )

        try:
            summary = (
                statistics.fmean(values),
                statistics.stdev(values) if len(values) > 1 else None,
                min(values),
                quantile(0.05),
                quantile(0.5),
                quantile(0.95),
                max(values),
            )
        except OverflowError as error:
            raise ValueError(
                f"indicator {indicator!r} produced an overflowing output summary"
            ) from error
        if any(value is not None and not math.isfinite(value) for value in summary):
            raise ValueError(f"indicator {indicator!r} produced a non-finite output summary")
        result.append(
            (
                indicator,
                len(values),
                *summary,
            )
        )
    schema = pa.schema(
        [
            pa.field("indicator", pa.string(), False),
            pa.field("n", pa.int64(), False),
            pa.field("mean", pa.float64()),
            pa.field("standardDeviation", pa.float64()),
            pa.field("minimum", pa.float64()),
            pa.field("q05", pa.float64()),
            pa.field("median", pa.float64()),
            pa.field("q95", pa.float64()),
            pa.field("maximum", pa.float64()),
        ]
    )
    return rows_to_table(result, schema)


def _convergence_evidence_table(
    method: str,
    charter: dict,
    convergence: pa.Table,
    actionable_factors: set[str] | None = None,
) -> tuple[pa.Table, bool, list[dict]]:
    schema = pa.schema(
        [
            pa.field("metric", pa.string(), False),
            pa.field("checkpoint", pa.int64()),
            pa.field("value", pa.float64()),
            pa.field("threshold", pa.float64(), False),
            pa.field("passed", pa.bool_(), False),
            pa.field("consecutivePasses", pa.int64(), False),
            pa.field("criterionMet", pa.bool_(), False),
            pa.field("evaluationStatus", pa.string(), False),
        ]
    )
    decision = charter["decision"]
    primary = decision["primary_indicator"]
    table_rows = convergence.to_pylist()
    evidence_rows, criterion_reports = [], []
    for criterion in charter["stopping"]:
        metric = criterion["metric"]
        threshold = criterion["threshold"]
        required = criterion["consecutive_checks"]
        supported = (method, metric) in {
            ("canonical_morris", "rank_stability"),
            ("multiscramble_sobol", "ci_width"),
        }
        values = []
        evaluation_failed = False
        if supported and method == "canonical_morris":
            grouped = {}
            all_grouped = {}
            for row in table_rows:
                all_grouped.setdefault(row["checkpoint_trajectories"], set()).add(
                    row["factor"]
                )
                if row.get("indicator") == primary:
                    grouped.setdefault(row["checkpoint_trajectories"], {})[
                        row["factor"]
                    ] = row["rank"]
            checkpoints = sorted(grouped)
            expected_factors = actionable_factors or (
                set(next(iter(grouped.values()))) if grouped else set()
            )
            evaluation_failed = (
                set(grouped) != set(all_grouped)
                or any(
                    set(grouped[checkpoint]) != all_grouped[checkpoint]
                    or set(grouped[checkpoint]) != expected_factors
                    for checkpoint in grouped
                )
            )
            for previous, current in zip(checkpoints, checkpoints[1:]):
                if set(grouped[previous]) != set(grouped[current]) or not grouped[current]:
                    evaluation_failed = True
                    break
                dimension = len(grouped[current])
                value = max(
                    abs(grouped[current][factor] - grouped[previous][factor])
                    for factor in grouped[current]
                ) / max(1, dimension - 1)
                values.append((current, value))
        elif supported:
            width = (
                "first_order_ci_width"
                if decision["objective"] == "fp"
                else "total_order_ci_width"
            )
            grouped = {}
            all_checkpoints = {row["checkpoint"] for row in table_rows}
            factors_by_checkpoint = {}
            for row in table_rows:
                if row.get("indicator") == primary and row.get("is_actionable") is True:
                    grouped.setdefault(row["checkpoint"], []).append(row.get(width))
                    factors_by_checkpoint.setdefault(row["checkpoint"], set()).add(
                        row["factor"]
                    )
            expected_factors = actionable_factors or (
                set(next(iter(factors_by_checkpoint.values())))
                if factors_by_checkpoint
                else set()
            )
            evaluation_failed = set(grouped) != all_checkpoints or any(
                factors != expected_factors for factors in factors_by_checkpoint.values()
            )
            for checkpoint in sorted(grouped):
                widths = grouped[checkpoint]
                if widths and all(
                    type(value) is float and math.isfinite(value) and value >= 0
                    for value in widths
                ):
                    values.append((checkpoint, max(widths)))
                else:
                    evaluation_failed = True
        if not supported:
            evidence_rows.append(
                (metric, None, None, threshold, False, 0, False, "unsupported")
            )
            status, consecutive = "unsupported", 0
        elif evaluation_failed or not values or any(
            not math.isfinite(value) for _, value in values
        ):
            evidence_rows.append(
                (metric, None, None, threshold, False, 0, False, "unevaluable")
            )
            status, consecutive = "unevaluable", 0
        else:
            consecutive = 0
            for checkpoint, value in values:
                passed = value <= threshold
                consecutive = consecutive + 1 if passed else 0
                evidence_rows.append(
                    (
                        metric,
                        checkpoint,
                        value,
                        threshold,
                        passed,
                        consecutive,
                        consecutive >= required,
                        "evaluated",
                    )
                )
            status = "met" if consecutive >= required else "not_met"
        criterion_reports.append(
            {
                "metric": metric,
                "threshold": threshold,
                "consecutiveChecks": required,
                "status": status,
                "finalConsecutivePasses": consecutive,
            }
        )
    return (
        rows_to_table(evidence_rows, schema),
        bool(criterion_reports)
        and all(criterion["status"] == "met" for criterion in criterion_reports),
        criterion_reports,
    )


def _governed_report(
    *,
    method: str,
    inference_status: str | None,
    completion_rate: float,
    expected: int,
    completed: int,
    missing: int,
    failed: int,
    output_records: Mapping[str, Mapping[str, object]],
    scientific_contract: dict | None,
    sensitivity_table: pa.Table | None,
    converged: bool,
    criterion_reports: list[dict],
) -> dict:
    charter = scientific_contract["charter"] if scientific_contract else None
    decision = charter["decision"] if charter else None
    exclusions = []
    non_actionable = []
    factor_evidence = []
    robustness_checks = []
    if charter:
        for category, items in charter["uncertainty"]["inventory"].items():
            for item in items:
                if item["status"] == "excluded":
                    exclusions.append({"category": category, **item})
        primary = decision["primary_indicator"]
        objective = decision["objective"]
        if sensitivity_table is not None:
            for row in sensitivity_table.to_pylist():
                if row["indicator"] != primary:
                    continue
                actionable = row.get("is_actionable", True)
                if not actionable:
                    non_actionable.append(row["factor"])
                    continue
                evidence = {
                    "factor": row["factor"],
                    "classification": objective,
                    "decisionRank": (
                        row["rank"]
                        if type(row["rank"]) in (int, float)
                        and math.isfinite(row["rank"])
                        else None
                    ),
                }
                if objective == "fp":
                    evidence["estimate"] = row["first_order_mean"]
                elif objective == "ff":
                    evidence["estimate"] = row["total_order_ci_high"]
                    evidence["withinPracticalTolerance"] = row[
                        "within_practical_tolerance"
                    ]
                else:
                    evidence["estimate"] = row["mu_star"]
                if type(evidence["estimate"]) is not float or not math.isfinite(
                    evidence["estimate"]
                ):
                    evidence["estimate"] = None
                    evidence["evidenceStatus"] = "invalid"
                else:
                    evidence["evidenceStatus"] = "available"
                factor_evidence.append(evidence)
        robustness_checks = [
            {**check, "status": "pending"} for check in charter["robustness"]
        ]
    robustness_status = "pending" if robustness_checks else "not_applicable"
    reasons = []
    if inference_status in {
        "demonstration_noninferential",
        "pilot_only",
        "incomplete_noninferential",
        "descriptive_noninferential",
    }:
        reasons.append(inference_status)
    if charter and not converged:
        reasons.append("stopping_not_met")
    if robustness_checks:
        reasons.append("robustness_pending")
    output_summary = output_records["outputSummary"]
    convergence_evidence = output_records.get("convergenceEvidence")
    sensitivity = output_records.get("sensitivity")
    return {
        "version": 1,
        "method": method,
        "decision": decision,
        "completion": {
            "complete": completed == expected and missing == 0 and failed == 0,
            "completionRate": completion_rate,
            "expected": expected,
            "completed": completed,
            "missing": missing,
            "failed": failed,
        },
        "evidence": {
            "outputSummary": {
                "path": output_summary["name"],
                "sha256": output_summary["sha256"],
            },
            "convergence": (
                {
                    "path": convergence_evidence["name"],
                    "sha256": convergence_evidence["sha256"],
                }
                if convergence_evidence
                else None
            ),
            "sensitivity": (
                {"path": sensitivity["name"], "sha256": sensitivity["sha256"]}
                if sensitivity
                else None
            ),
        },
        "exclusions": exclusions,
        "nonActionableFactors": sorted(set(non_actionable)),
        "factorEvidence": factor_evidence,
        "convergence": {
            "status": "met" if converged else "not_met" if charter else "not_applicable",
            "converged": converged,
            "criteria": criterion_reports,
        },
        "robustness": {"status": robustness_status, "checks": robustness_checks},
        "inferenceStatus": inference_status,
        "claimAuthorized": False,
        "reasonCodes": reasons,
    }
