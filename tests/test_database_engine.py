from unittest.mock import Mock, patch

from sqlalchemy.pool import QueuePool

from app.database_engine import DatabaseEngineProfile, create_service_engine, pool_metrics_snapshot


def _profile() -> DatabaseEngineProfile:
    return DatabaseEngineProfile(
        name="test-worker",
        pool_size=12,
        max_overflow=0,
        pool_timeout_seconds=7,
        pool_recycle_seconds=600,
        application_name="test-worker",
        statement_timeout_seconds=120,
        idle_transaction_timeout_seconds=30,
    )


def test_postgres_engine_applies_bounded_profile_and_server_deadlines() -> None:
    fake_engine = Mock()
    fake_engine.pool = Mock()
    with (
        patch("app.database_engine.create_engine", return_value=fake_engine) as create,
        patch("app.database_engine.event.listens_for", side_effect=lambda *_args: lambda function: function),
    ):
        engine = create_service_engine("postgresql+psycopg://user:pass@db/core", _profile())

    assert engine is fake_engine
    kwargs = create.call_args.kwargs
    assert kwargs["pool_size"] == 12
    assert kwargs["max_overflow"] == 0
    assert kwargs["pool_timeout"] == 7
    assert kwargs["pool_recycle"] == 600
    assert kwargs["connect_args"]["application_name"] == "test-worker"
    assert "statement_timeout=120000" in kwargs["connect_args"]["options"]
    assert "idle_in_transaction_session_timeout=30000" in kwargs["connect_args"]["options"]


def test_sqlite_engine_does_not_receive_queue_pool_or_postgres_options() -> None:
    with patch("app.database_engine.create_engine", return_value=Mock()) as create:
        create_service_engine("sqlite+pysqlite:///:memory:", _profile())

    assert create.call_args.kwargs == {"pool_pre_ping": True}


def test_pool_metrics_report_checked_out_capacity() -> None:
    engine = create_service_engine("postgresql+psycopg://user:pass@db/core", _profile())
    assert isinstance(engine.pool, QueuePool)
    snapshot = pool_metrics_snapshot(engine)
    assert snapshot is not None
    assert snapshot.profile == "test-worker"
    assert snapshot.checked_out == 0
    assert snapshot.overflow == 0
    engine.dispose()
