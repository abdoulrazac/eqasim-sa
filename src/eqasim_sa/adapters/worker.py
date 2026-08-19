import fcntl
import os
import stat
import time
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

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


@contextmanager
def run_lease(
    campaign_root: Path, run_id: str, interrupted: Callable[[], bool] | None = None
):
    directory = campaign_root / ".eqasim-sa-run-locks"
    if directory.is_symlink():
        raise ValueError("run lock directory must not be a symlink")
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("run lock directory must be an owned directory")
    directory.chmod(0o700)
    path = directory / f"{run_id}.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
        ):
            raise ValueError("run lease must be a private owned regular file")
        os.fchmod(descriptor, 0o600)
        while True:
            if interrupted is not None and interrupted():
                raise KeyboardInterrupt
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.05)
        yield
    finally:
        os.close(descriptor)


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
    indexed = [item for batch in batches for item in batch]
    if len(indexed) != expected or sorted(item.index for item in indexed) != list(
        range(expected)
    ):
        raise RuntimeError("worker batch results do not match submitted run indices")
    return [item.value for item in sorted(indexed, key=lambda item: item.index)]
