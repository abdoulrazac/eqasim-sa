"""Thread-based execution backend for local campaign workers."""

from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


class LocalBackend:
    """Map independent worker calls through a bounded thread pool."""

    def __init__(self, max_workers: int = 1) -> None:
        if (
            isinstance(max_workers, bool)
            or not isinstance(max_workers, int)
            or max_workers <= 0
        ):
            raise ValueError("max_workers must be a strictly positive integer")
        self.max_workers = max_workers

    def map(
        self,
        function: Callable[[T], R],
        specs: Iterable[T],
        interrupt: Callable[[], None] | None = None,
    ) -> Iterator[R]:
        executor = ThreadPoolExecutor(max_workers=getattr(self, "max_workers", 1))
        try:
            yield from executor.map(function, specs)
        except KeyboardInterrupt:
            if interrupt is not None:
                interrupt()
            executor.shutdown(wait=True, cancel_futures=True)
            raise
        finally:
            if not getattr(executor, "_shutdown", False):
                executor.shutdown(wait=True, cancel_futures=True)
