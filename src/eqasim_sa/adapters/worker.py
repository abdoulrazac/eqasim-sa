from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar, cast

from eqasim_sa.core.ledger import run_lease

T = TypeVar("T")
R = TypeVar("R")


@dataclass(frozen=True)
class Indexed(Generic[T]):
    index: int
    value: T


@dataclass(frozen=True)
class WorkerBatch(Generic[T]):
    slot: int
    items: tuple[Indexed[T], ...]


def worker_batches(values: Sequence[T], workers: int) -> tuple[WorkerBatch[T], ...]:
    if isinstance(workers, bool) or not isinstance(workers, int) or workers <= 0:
        raise ValueError("workers must be a strictly positive integer")
    slots = min(len(values), workers)
    return tuple(
        WorkerBatch(
            slot,
            tuple(Indexed(index, values[index]) for index in range(slot, len(values), slots)),
        )
        for slot in range(slots)
    )


def execute_batch(
    batch: WorkerBatch[T], function: Callable[[T], R]
) -> tuple[Indexed[R], ...]:
    return tuple(Indexed(item.index, function(item.value)) for item in batch.items)


def ordered_results(
    batches: Sequence[Sequence[Indexed[R]]], expected: int
) -> list[R]:
    if type(expected) is not int or expected < 0:
        raise RuntimeError("expected result count must be a non-negative integer")
    missing = object()
    ordered: list[object] = [missing] * expected
    count = 0
    for batch in batches:
        for item in batch:
            index = item.index
            if (
                type(index) is not int
                or not 0 <= index < expected
                or ordered[index] is not missing
            ):
                raise RuntimeError(
                    "worker batch results do not match submitted run indices"
                )
            ordered[index] = item.value
            count += 1
    if count != expected:
        raise RuntimeError("worker batch results do not match submitted run indices")
    return cast(list[R], ordered)
