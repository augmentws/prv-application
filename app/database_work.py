"""Process-wide backpressure for high-fan-out worker database work.

Capacity is deliberately a wait condition, not an operation failure.  A lease
can be suspended around remote work after the current transaction is committed;
this keeps the control-plane reserve without counting provider latency against
the worker's database budget.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from app.config import get_settings

logger = logging.getLogger(__name__)

_settings = get_settings()
_data_slots = threading.BoundedSemaphore(_settings.worker_database_concurrency)


class WorkerDatabaseLease:
    """One process-wide data-plane slot that may be suspended around remote work."""

    def __init__(
        self,
        slots: threading.BoundedSemaphore,
        *,
        operation: str,
        warning_seconds: float,
    ) -> None:
        self._slots = slots
        self.operation = operation
        self._warning_seconds = warning_seconds
        self._acquired = False
        self._wait_seconds = 0.0

    @property
    def wait_ms(self) -> int:
        """Cumulative time spent waiting for this lease, including reacquisition."""

        return max(0, round(self._wait_seconds * 1000))

    def acquire(self) -> None:
        if self._acquired:
            return
        started = time.perf_counter()
        warning_count = 0
        while not self._slots.acquire(timeout=self._warning_seconds):
            warning_count += 1
            logger.warning(
                "Still waiting for worker database capacity operation=%s waited_seconds=%.3f warnings=%s",
                self.operation,
                time.perf_counter() - started,
                warning_count,
            )
        self._acquired = True
        waited = time.perf_counter() - started
        self._wait_seconds += waited
        if waited >= _settings.database_checkout_warning_seconds:
            logger.warning(
                "Worker database capacity acquired operation=%s waited_seconds=%.3f",
                self.operation,
                waited,
            )

    def release(self) -> None:
        if not self._acquired:
            return
        self._acquired = False
        self._slots.release()

    @contextmanager
    def suspended(self) -> Iterator[None]:
        """Release the slot for external work, then reacquire before DB work resumes."""

        was_acquired = self._acquired
        if was_acquired:
            self.release()
        try:
            yield
        finally:
            if was_acquired:
                self.acquire()


@contextmanager
def worker_database_slot(*, operation: str) -> Iterator[WorkerDatabaseLease]:
    """Bound concurrent data-plane work while leaving pool capacity for control work."""

    lease = WorkerDatabaseLease(
        _data_slots,
        operation=operation,
        warning_seconds=_settings.database_pool_timeout_seconds,
    )
    lease.acquire()
    try:
        yield lease
    finally:
        lease.release()
