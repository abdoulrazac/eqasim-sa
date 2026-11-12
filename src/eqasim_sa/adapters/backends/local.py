"""Thread-based execution backend for local campaign workers."""

from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")

# The main thread never blocks longer than this, so Ctrl+C is delivered
# promptly on Windows, where a blocked lock wait is not interruptible.
INTERRUPT_POLL_SECONDS = 0.5


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
            futures = [executor.submit(function, spec) for spec in specs]
            pending = set(futures)
            for future in futures:
                # Results keep submission order; an exception surfaces at its
                # position, as with ThreadPoolExecutor.map.
                while not future.done():
                    _, pending = wait(
                        pending,
                        timeout=INTERRUPT_POLL_SECONDS,
                        return_when=FIRST_COMPLETED,
                    )
                yield future.result()
        except KeyboardInterrupt:
            if interrupt is not None:
                interrupt()
            executor.shutdown(wait=True, cancel_futures=True)
            raise
        finally:
            if not getattr(executor, "_shutdown", False):
                executor.shutdown(wait=True, cancel_futures=True)
