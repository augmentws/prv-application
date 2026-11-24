"""Shared SQLAlchemy engine construction and lightweight pool observability.

Application code intentionally remains unaware of the database endpoint.  This
module only controls the client-side budget and connection lifetime, so the
same profiles work against direct PostgreSQL or an infrastructure pooler.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DatabaseEngineProfile:
    name: str
    pool_size: int
    max_overflow: int
    pool_timeout_seconds: float
    pool_recycle_seconds: int
    application_name: str
    statement_timeout_seconds: int
    idle_transaction_timeout_seconds: int
    checkout_warning_seconds: float = 1.0
    transaction_warning_seconds: float = 30.0


@dataclass(frozen=True)
class PoolMetricsSnapshot:
    profile: str
    checkouts: int
    checkins: int
    invalidations: int
    checkout_wait_seconds: float
    slow_checkouts: int
    checked_out: int
    overflow: int


class _PoolMetrics:
    def __init__(self, profile: DatabaseEngineProfile) -> None:
        self.profile = profile
        self.lock = threading.Lock()
        self.checkouts = 0
        self.checkins = 0
        self.invalidations = 0
        self.checkout_wait_seconds = 0.0
        self.slow_checkouts = 0

    def record_wait(self, elapsed: float) -> None:
        with self.lock:
            self.checkout_wait_seconds += elapsed
            if elapsed >= self.profile.checkout_warning_seconds:
                self.slow_checkouts += 1
        if elapsed >= self.profile.checkout_warning_seconds:
            logger.warning(
                "Database checkout waited %.3fs profile=%s",
                elapsed,
                self.profile.name,
            )


class ObservedQueuePool(QueuePool):
    """QueuePool that records time spent applying client-side backpressure."""

    _pvr_metrics: _PoolMetrics | None = None

    def _do_get(self):  # type: ignore[no-untyped-def]
        started = time.perf_counter()
        try:
            return super()._do_get()
        finally:
            metrics = self._pvr_metrics
            if metrics is not None:
                metrics.record_wait(time.perf_counter() - started)


_METRICS_BY_ENGINE: dict[int, _PoolMetrics] = {}
_METRICS_LOCK = threading.Lock()


class ObservedSession(Session):
    pass


@event.listens_for(ObservedSession, "after_begin")
def _transaction_started(session: ObservedSession, transaction: object, _connection: object) -> None:
    if getattr(transaction, "parent", None) is None:
        session.info["pvr_transaction_started"] = time.perf_counter()


@event.listens_for(ObservedSession, "after_transaction_end")
def _transaction_ended(session: ObservedSession, transaction: object) -> None:
    if getattr(transaction, "parent", None) is not None:
        return
    started = session.info.pop("pvr_transaction_started", None)
    profile = session.info.get("pvr_database_profile")
    warning_seconds = session.info.get("pvr_transaction_warning_seconds")
    if not isinstance(started, float) or not isinstance(profile, str) or not isinstance(warning_seconds, float):
        return
    elapsed = time.perf_counter() - started
    if elapsed >= warning_seconds:
        logger.warning("Long database transaction %.3fs profile=%s", elapsed, profile)


def _postgres_connect_args(profile: DatabaseEngineProfile) -> dict[str, Any]:
    options = (
        f"-c statement_timeout={profile.statement_timeout_seconds * 1000} "
        f"-c idle_in_transaction_session_timeout={profile.idle_transaction_timeout_seconds * 1000}"
    )
    return {
        "application_name": profile.application_name,
        "options": options,
    }


def create_service_engine(database_url: str, profile: DatabaseEngineProfile) -> Engine:
    if database_url.startswith("sqlite"):
        return create_engine(database_url, pool_pre_ping=True)

    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=profile.pool_size,
        max_overflow=profile.max_overflow,
        pool_timeout=profile.pool_timeout_seconds,
        pool_recycle=profile.pool_recycle_seconds,
        poolclass=ObservedQueuePool,
        connect_args=_postgres_connect_args(profile),
    )
    metrics = _PoolMetrics(profile)
    pool = engine.pool
    if isinstance(pool, ObservedQueuePool):
        pool._pvr_metrics = metrics
    with _METRICS_LOCK:
        _METRICS_BY_ENGINE[id(engine)] = metrics

    @event.listens_for(engine, "checkout")
    def _checkout(*_args: object) -> None:
        with metrics.lock:
            metrics.checkouts += 1

    @event.listens_for(engine, "checkin")
    def _checkin(*_args: object) -> None:
        with metrics.lock:
            metrics.checkins += 1

    @event.listens_for(engine, "invalidate")
    def _invalidate(*_args: object) -> None:
        with metrics.lock:
            metrics.invalidations += 1
        logger.warning("Database connection invalidated profile=%s", profile.name)

    return engine


def create_service_sessionmaker(engine: Engine, profile: DatabaseEngineProfile) -> sessionmaker[ObservedSession]:
    return sessionmaker(
        bind=engine,
        class_=ObservedSession,
        autoflush=False,
        expire_on_commit=False,
        info={
            "pvr_database_profile": profile.name,
            "pvr_transaction_warning_seconds": float(profile.transaction_warning_seconds),
        },
    )


def pool_metrics_snapshot(engine: Engine) -> PoolMetricsSnapshot | None:
    with _METRICS_LOCK:
        metrics = _METRICS_BY_ENGINE.get(id(engine))
    if metrics is None:
        return None
    pool = engine.pool
    checked_out = pool.checkedout() if isinstance(pool, QueuePool) else 0
    overflow = max(pool.overflow(), 0) if isinstance(pool, QueuePool) else 0
    with metrics.lock:
        return PoolMetricsSnapshot(
            profile=metrics.profile.name,
            checkouts=metrics.checkouts,
            checkins=metrics.checkins,
            invalidations=metrics.invalidations,
            checkout_wait_seconds=metrics.checkout_wait_seconds,
            slow_checkouts=metrics.slow_checkouts,
            checked_out=checked_out,
            overflow=overflow,
        )
