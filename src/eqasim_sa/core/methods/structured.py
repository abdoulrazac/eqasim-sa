"""Canonical metadata and validation for structured factor payloads."""

import json
import re

import pyarrow as pa

FIXED = ("designId", "method", "role", "runId", "seed", "contextHash")
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _structured_json(value: dict[str, dict[str, list[str]]]) -> bytes:
    ordered = {name: value[name] for name in sorted(value)}
    return json.dumps(
        ordered,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def validate_structured_plan_metadata(
    metadata: dict[bytes, bytes],
) -> dict[str, dict[str, list[str]]]:
    """Decode canonical schema-v3 structured-factor metadata."""

    version = metadata.get(b"planSchemaVersion")
    pair = {b"structuredFactorsVersion", b"structuredFactors"}
    present = pair & set(metadata)
    if version is None and not present:
        return {}
    if version in {b"1", b"2"}:
        if present:
            raise ValueError("structured plan metadata requires plan schema v3")
        return {}
    if version != b"3" or present != pair:
        raise ValueError("structured plan metadata pair is missing or unsupported")
    if metadata[b"structuredFactorsVersion"] != b"1":
        raise ValueError("structured plan metadata version is unsupported")
    try:
        structured = json.loads(metadata[b"structuredFactors"])
        canonical = _structured_json(structured)
    except Exception as error:
        raise ValueError("structured plan metadata is invalid") from error
    if (
        metadata[b"structuredFactors"] != canonical
        or not isinstance(structured, dict)
        or not structured
    ):
        raise ValueError("structured plan metadata is not canonical")
    for name, configurations in structured.items():
        if (
            not isinstance(name, str)
            or not NAME.fullmatch(name)
            or not isinstance(configurations, dict)
            or not configurations
        ):
            raise ValueError("structured plan metadata has invalid factors")
        payloads = []
        for label, payload in configurations.items():
            if (
                not isinstance(label, str)
                or not label
                or "\0" in label
                or not isinstance(payload, list)
                or not payload
                or any(
                    not isinstance(item, str) or not item or "\0" in item
                    for item in payload
                )
                or len(set(payload)) != len(payload)
            ):
                raise ValueError("structured plan metadata has invalid configurations")
            payloads.append(tuple(payload))
        if len(set(payloads)) != len(payloads):
            raise ValueError("structured plan metadata contains payload aliases")
    return structured


def validate_structured_plan_rows(
    plan: pa.Table, structured: dict[str, dict[str, list[str]]]
) -> None:
    """Match structured labels and declaration order to plan rows."""

    if not structured:
        return
    factor_columns = set(plan.column_names) - set(FIXED)
    methods = set(plan["method"].to_pylist()) if "method" in plan.column_names else set()
    for name, configurations in structured.items():
        if name not in factor_columns:
            raise ValueError(
                f"structured plan factor {name!r} is not a plan factor column"
            )
        observed = plan[name].to_pylist()
        if any(type(label) is not str or label not in configurations for label in observed):
            raise ValueError(
                f"structured plan factor {name!r} contains an unknown label"
            )
        if methods == {"grid"}:
            first_occurrence = list(dict.fromkeys(observed))
            if first_occurrence != list(configurations):
                raise ValueError(
                    f"structured Grid factor {name!r} labels do not match plan rows in declaration order"
                )


def validate_structured_factor_supports(
    structured: dict[str, dict[str, list[str]]], supports: dict[str, list]
) -> None:
    """Require plan supports to equal declared structured labels exactly."""

    for name, configurations in structured.items():
        if name not in supports or list(configurations) != supports[name]:
            raise ValueError(
                f"structured factor {name!r} labels do not exactly match sealed support"
            )
