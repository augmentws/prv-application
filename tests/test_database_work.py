from __future__ import annotations

import threading
import time

from app.database_work import WorkerDatabaseLease


def _lease(slots: threading.BoundedSemaphore, operation: str) -> WorkerDatabaseLease:
    return WorkerDatabaseLease(slots, operation=operation, warning_seconds=0.01)


def test_capacity_contention_waits_instead_of_failing() -> None:
    slots = threading.BoundedSemaphore(1)
    first = _lease(slots, "first")
    second = _lease(slots, "second")
    first.acquire()
    acquired = threading.Event()

    def wait_for_capacity() -> None:
        second.acquire()
        acquired.set()
        second.release()

    waiter = threading.Thread(target=wait_for_capacity)
    waiter.start()
    time.sleep(0.03)
    assert not acquired.is_set()

    first.release()
    waiter.join(timeout=1)

    assert acquired.is_set()
    assert not waiter.is_alive()
    assert second.wait_ms >= 20


def test_suspended_lease_releases_capacity_for_external_work() -> None:
    slots = threading.BoundedSemaphore(1)
    lease = _lease(slots, "document")
    lease.acquire()

    with lease.suspended():
        assert slots.acquire(blocking=False)
        slots.release()

    assert not slots.acquire(blocking=False)
    lease.release()
    assert slots.acquire(blocking=False)
    slots.release()


def test_suspended_lease_is_reacquired_after_external_failure() -> None:
    slots = threading.BoundedSemaphore(1)
    lease = _lease(slots, "document")
    lease.acquire()

    try:
        with lease.suspended():
            raise RuntimeError("provider failed")
    except RuntimeError:
        pass

    assert not slots.acquire(blocking=False)
    lease.release()
