"""Plan generation for adjacent one-at-a-time methods."""

from __future__ import annotations

import hashlib
import math

from eqasim_sa.core.methods.common import _json, trajectory_role

MORRIS_DOMAIN = b"eqasim-sa:design:morris:v1\0"
DISCRETE_OAT_DOMAIN = b"eqasim-sa:design:discrete-oat:v1\0"


def validate_morris_values(name: str, values: list) -> None:
    if (
        len(values) < 2
        or type(values[0]) not in (int, float)
        or any(type(value) is not type(values[0]) for value in values)
    ):
        raise TypeError(
            f"Morris factor {name!r} needs two or more numeric levels of the same type"
        )
    if any(not math.isfinite(value) for value in values):
        raise ValueError(f"Morris factor {name!r} levels must be finite")
    if any(left >= right for left, right in zip(values, values[1:])):
        raise ValueError(
            f"Morris factor {name!r} levels must be distinct and strictly increasing"
        )
    extent = values[-1] - values[0]
    deltas = tuple((right - left) / extent for left, right in zip(values, values[1:]))
    if not math.isfinite(extent) or any(
        not math.isfinite(delta) or delta == 0 for delta in deltas
    ):
        raise ValueError(
            f"Morris factor {name!r} range and normalized deltas must be finite and representable"
        )


def _morris_positions(
    design: MorrisDesign, names: tuple[str, ...]
):  # type: ignore[no-untyped-def]
    definition = {
        "designSeed": design.design_seed,
        "factors": [{"name": name, "values": design.factors[name]} for name in names],
    }
    encoded = _json(definition)
    count = math.factorial(len(names)) * math.prod(
        2 * (len(design.factors[name]) - 1) for name in names
    )
    offset = (
        int.from_bytes(
            hashlib.sha256(MORRIS_DOMAIN + encoded + b"\0offset").digest(), "big"
        )
        % count
    )
    stride = (
        int.from_bytes(
            hashlib.sha256(MORRIS_DOMAIN + encoded + b"\0stride").digest(), "big"
        )
        % count
        or 1
    )
    while math.gcd(stride, count) != 1:
        stride = (stride + 1) % count or 1
    for trajectory in range(design.trajectories):
        code = (offset + trajectory * stride) % count
        choices = []
        for name in names:
            code, digit = divmod(code, 2 * (len(design.factors[name]) - 1))
            choices.append((digit // 2, 1 if digit % 2 == 0 else -1))
        available, permutation = list(range(len(names))), []
        for remaining in range(len(names), 0, -1):
            block = math.factorial(remaining - 1)
            index, code = divmod(code, block)
            permutation.append(available.pop(index))
        point = [edge if direction == 1 else edge + 1 for edge, direction in choices]
        points = [tuple(point)]
        for factor in permutation:
            point = list(point)
            point[factor] += choices[factor][1]
            points.append(tuple(point))
        for replication, seed in enumerate(design.seeds):
            for rank, indices in enumerate(points):
                role = trajectory_role(
                    trajectory,
                    rank,
                    names[permutation[rank - 1]] if rank else None,
                )
                yield indices, replication, seed, role, definition


def _discrete_oat_positions(
    design: DiscreteOATDesign, names: tuple[str, ...]
):  # type: ignore[no-untyped-def]
    definition = {
        "designSeed": design.design_seed,
        "factors": [{"name": name, "values": design.factors[name]} for name in names],
    }
    encoded = _json(definition)
    count = math.factorial(len(names)) * math.prod(
        2 * (len(design.factors[name]) - 1) for name in names
    )
    offset = int.from_bytes(
        hashlib.sha256(DISCRETE_OAT_DOMAIN + encoded + b"\0offset").digest(), "big"
    ) % count
    stride = int.from_bytes(
        hashlib.sha256(DISCRETE_OAT_DOMAIN + encoded + b"\0stride").digest(), "big"
    ) % count or 1
    while math.gcd(stride, count) != 1:
        stride = (stride + 1) % count or 1
    for trajectory in range(design.trajectories):
        code = (offset + trajectory * stride) % count
        choices = []
        for name in names:
            code, digit = divmod(code, 2 * (len(design.factors[name]) - 1))
            choices.append((digit // 2, 1 if digit % 2 == 0 else -1))
        available, permutation = list(range(len(names))), []
        for remaining in range(len(names), 0, -1):
            block = math.factorial(remaining - 1)
            index, code = divmod(code, block)
            permutation.append(available.pop(index))
        point = [edge if direction == 1 else edge + 1 for edge, direction in choices]
        points = [tuple(point)]
        for factor in permutation:
            point = list(point)
            point[factor] += choices[factor][1]
            points.append(tuple(point))
        for replication, seed in enumerate(design.seeds):
            for rank, indices in enumerate(points):
                role = trajectory_role(
                    trajectory,
                    rank,
                    names[permutation[rank - 1]] if rank else None,
                )
                yield indices, replication, seed, role, definition
