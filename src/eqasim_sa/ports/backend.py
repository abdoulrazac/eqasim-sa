from collections.abc import Callable, Iterable, Iterator
from typing import Protocol, TypeVar

T = TypeVar("T")
R = TypeVar("R")


class Backend(Protocol):
    def map(self, function: Callable[[T], R], specs: Iterable[T]) -> Iterator[R]: ...
