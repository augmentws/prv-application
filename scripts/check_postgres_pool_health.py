"""Observe direct-PostgreSQL connection acceptance criteria during a load run."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone

from sqlalchemy import text

from app.config import get_settings
from app.database_engine import DatabaseEngineProfile, create_service_engine


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch-seconds", type=int, default=0)
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    parser.add_argument("--max-utilization", type=float, default=0.8)
    parser.add_argument("--max-idle-transaction-seconds", type=float, default=30.0)
    return parser


def _snapshot(engine) -> dict:
    with engine.connect() as connection:
        max_connections = int(
            connection.execute(text("SELECT setting FROM pg_settings WHERE name = 'max_connections'")).scalar_one()
        )
        reserved = int(
            connection.execute(
                text("SELECT setting FROM pg_settings WHERE name = 'superuser_reserved_connections'")
            ).scalar_one()
        )
        rows = connection.execute(
            text(
                """
                SELECT
                    COALESCE(application_name, '') AS application_name,
                    state,
                    count(*) AS connection_count,
                    COALESCE(max(EXTRACT(EPOCH FROM (clock_timestamp() - xact_start)))
                        FILTER (WHERE xact_start IS NOT NULL), 0) AS max_transaction_seconds
                FROM pg_stat_activity
                WHERE backend_type = 'client backend'
                GROUP BY COALESCE(application_name, ''), state
                ORDER BY application_name, state
                """
            )
        ).mappings()
        activity = [dict(row) for row in rows]
    used = sum(int(row["connection_count"]) for row in activity)
    usable = max_connections - reserved
    idle_transaction_seconds = max(
        (float(row["max_transaction_seconds"] or 0) for row in activity if row["state"] == "idle in transaction"),
        default=0.0,
    )
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "max_connections": max_connections,
        "reserved_connections": reserved,
        "usable_connections": usable,
        "used_connections": used,
        "utilization": used / usable if usable else 1.0,
        "max_idle_transaction_seconds": idle_transaction_seconds,
        "activity": activity,
    }


def main() -> None:
    args = _parser().parse_args()
    settings = get_settings()
    profile = DatabaseEngineProfile(
        name="maintenance-pool-health",
        pool_size=1,
        max_overflow=0,
        pool_timeout_seconds=settings.database_pool_timeout_seconds,
        pool_recycle_seconds=settings.database_pool_recycle_seconds,
        application_name="priv-view-pool-health",
        statement_timeout_seconds=settings.database_statement_timeout_seconds,
        idle_transaction_timeout_seconds=settings.database_idle_transaction_timeout_seconds,
    )
    engine = create_service_engine(settings.database_url, profile)
    deadline = time.monotonic() + args.watch_seconds
    violations: list[dict] = []
    try:
        while True:
            snapshot = _snapshot(engine)
            current: list[str] = []
            if snapshot["utilization"] >= args.max_utilization:
                current.append("CONNECTION_UTILIZATION")
            if snapshot["max_idle_transaction_seconds"] > args.max_idle_transaction_seconds:
                current.append("IDLE_IN_TRANSACTION")
            snapshot["violations"] = current
            print(json.dumps(snapshot, default=str, sort_keys=True))
            if current:
                violations.append(snapshot)
            if time.monotonic() >= deadline:
                break
            time.sleep(args.interval_seconds)
    finally:
        engine.dispose()
    if violations:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
