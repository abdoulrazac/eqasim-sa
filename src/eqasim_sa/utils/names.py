"""Portable file-name rules enforced on every operating system."""

from __future__ import annotations

from collections.abc import Iterable

_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


def validate_portable_name(name: str, what: str) -> str:
    """Return ``name`` or raise if it is not a portable file name."""
    reason = None
    if not isinstance(name, str):
        reason = "must be a string"
    elif name.split(".", 1)[0].lower() in _RESERVED:
        reason = "reserved on Windows"
    elif name.endswith("."):
        reason = "ends with a dot"
    elif name.endswith(" "):
        reason = "ends with a space"
    if reason is not None:
        raise ValueError(f"{what} {name!r} is not a portable file name: {reason}")
    return name


def reject_case_collisions(names: Iterable[str], what: str) -> None:
    """Raise if two names differ only by case."""
    seen: dict[str, str] = {}
    for name in names:
        key = name.casefold()
        other = seen.setdefault(key, name)
        if other != name:
            raise ValueError(
                f"{what} names {other!r} and {name!r} differ only by case"
            )
