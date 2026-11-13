import threading
import time

import pytest

from eqasim_sa.adapters.worker import (
    Indexed,
    execute_batch,
    ordered_results,
    worker_batches,
)
from eqasim_sa.core.ledger import run_lease


def test_round_robin_batches_are_deterministic_and_bounded() -> None:
    batches = worker_batches(tuple("abcdefg"), 3)
    assert [(batch.slot, [(item.index, item.value) for item in batch.items]) for batch in batches] == [
        (0, [(0, "a"), (3, "d"), (6, "g")]),
        (1, [(1, "b"), (4, "e")]),
        (2, [(2, "c"), (5, "f")]),
    ]
    assert len(worker_batches((1, 2), 20)) == 2
    assert worker_batches((), 2) == ()


def test_batch_execution_is_sequential_and_order_is_restored() -> None:
    active = 0
    peak = 0
    lock = threading.Lock()

    def run(value: int) -> int:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return value * 10

    batches = worker_batches((0, 1, 2, 3, 4), 2)
    completed = [execute_batch(batch, run) for batch in reversed(batches)]
    assert peak == 1
    assert ordered_results(completed, 5) == [0, 10, 20, 30, 40]


def test_ordered_results_rejects_missing_or_duplicate_indices() -> None:
    with pytest.raises(RuntimeError, match="indices"):
        ordered_results(((Indexed(0, "a"), Indexed(0, "b")),), 2)
    with pytest.raises(RuntimeError, match="indices"):
        ordered_results(((Indexed(0, "a"), Indexed(2, "b")),), 2)
    with pytest.raises(RuntimeError, match="indices"):
        ordered_results(((Indexed(-1, "a"),),), 1)


def test_run_lease_is_exclusive_and_interruptible(tmp_path) -> None:  # type: ignore[no-untyped-def]
    interrupted = threading.Event()
    stopped = threading.Event()
    acquired = threading.Event()

    with run_lease(tmp_path, "a" * 32):
        def wait() -> None:
            try:
                with run_lease(tmp_path, "a" * 32, interrupted.is_set):
                    acquired.set()
            except KeyboardInterrupt:
                stopped.set()

        thread = threading.Thread(target=wait)
        thread.start()
        time.sleep(0.1)
        interrupted.set()
        thread.join(timeout=2)

    assert stopped.is_set() and not acquired.is_set()
