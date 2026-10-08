"""Canonical serialization and operational limits shared by scientific methods."""

import json

MAX_BOOTSTRAP_RESAMPLES = 100_000
MAX_BOOTSTRAP_WORK_CELLS = 50_000_000
MAX_PLAN_POSITIONS = 1_000_000
MAX_PLAN_CELLS = 20_000_000


def trajectory_role(trajectory: int, rank: int, factor: str | None = None) -> str:
    """Return the canonical role name for one OAT trajectory position."""
    if rank == 0:
        return f"trajectory:{trajectory}:rank:0:start"
    if factor is None:
        raise ValueError("a non-start trajectory role requires a factor")
    return f"trajectory:{trajectory}:rank:{rank}:factor:{factor}"


def validate_bootstrap_resamples(
    value: object, *, method: str, minimum: int
) -> int:
    if (
        type(value) is not int
        or not minimum <= value <= MAX_BOOTSTRAP_RESAMPLES
    ):
        raise ValueError(
            f"{method} bootstrap_resamples must be a signed-int64 integer "
            f"between {minimum} and the operational safe cap "
            f"{MAX_BOOTSTRAP_RESAMPLES}"
        )
    return value


def _json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
