"""Execution backend protocol used by local and SLURM adapters."""

from collections.abc import Callable, Iterable, Iterator
from typing import Protocol, TypeVar

T = TypeVar("T")
R = TypeVar("R")


class Backend(Protocol):
    """Map independent specifications to results through an execution backend."""

    def map(self, function: Callable[[T], R], specs: Iterable[T]) -> Iterator[R]: ...
