"""add common job execution attempt fields

Revision ID: 0071_job_execution
Revises: 0070_classification_analysis
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0071_job_execution"
down_revision: str | None = "0070_classification_analysis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_workflow_run_status", "workflow_run", type_="check")
    op.create_check_constraint(
        "ck_workflow_run_status",
        "workflow_run",
        "status IN ('QUEUED', 'RUNNING', 'WAITING', 'COMPLETED', "
        "'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
    )
    op.add_column(
        "workflow_run",
        sa.Column("job_type", sa.String(length=80), nullable=True),
    )
    op.add_column("workflow_run", sa.Column("job_id", sa.Uuid(), nullable=True))
    op.add_column(
        "workflow_run",
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "workflow_run",
        sa.Column("retry_of_workflow_run_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "workflow_run",
        sa.Column("idempotency_key", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "workflow_run",
        sa.Column("current_stage", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "workflow_run",
        sa.Column("progress_completed", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "workflow_run",
        sa.Column("progress_total", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "workflow_run",
        sa.Column("progress_unit", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "workflow_run",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "workflow_run",
        sa.Column("cancel_requested_by_user_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "workflow_run",
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "workflow_run",
        sa.Column("failure_code", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "workflow_run",
        sa.Column("failure_summary", sa.Text(), nullable=True),
    )
    op.add_column(
        "workflow_run",
        sa.Column("failure_retriable", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_foreign_key(
        "fk_workflow_run_retry_of",
        "workflow_run",
        "workflow_run",
        ["retry_of_workflow_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_workflow_run_cancel_requested_by",
        "workflow_run",
        "app_user",
        ["cancel_requested_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_workflow_run_attempt_positive",
        "workflow_run",
        "attempt_number > 0",
    )
    op.create_check_constraint(
        "ck_workflow_run_progress_nonnegative",
        "workflow_run",
        "progress_completed >= 0 AND (progress_total IS NULL OR progress_total >= 0)",
    )
    op.create_unique_constraint(
        "uq_workflow_run_job_attempt",
        "workflow_run",
        ["job_type", "job_id", "attempt_number"],
    )
    op.create_index("ix_workflow_run_job_type", "workflow_run", ["job_type"])
    op.create_index("ix_workflow_run_job_id", "workflow_run", ["job_id"])
    op.create_index(
        "ix_workflow_run_job_attempt",
        "workflow_run",
        ["job_type", "job_id", "attempt_number"],
    )
    op.create_index(
        "ix_workflow_run_matter_status_created",
        "workflow_run",
        ["matter_id", "status", "created_at"],
    )
    op.create_index(
        "ix_workflow_run_status_heartbeat",
        "workflow_run",
        ["status", "last_heartbeat_at"],
    )
    op.create_index(
        "ix_workflow_run_retry_of",
        "workflow_run",
        ["retry_of_workflow_run_id"],
    )
    op.create_index(
        "ix_workflow_run_cancel_requested_by_user_id",
        "workflow_run",
        ["cancel_requested_by_user_id"],
    )

    op.add_column(
        "matter_analysis_task_run",
        sa.Column("current_workflow_run_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_analysis_task_run_current_workflow",
        "matter_analysis_task_run",
        "workflow_run",
        ["current_workflow_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_analysis_task_run_current_workflow",
        "matter_analysis_task_run",
        ["current_workflow_run_id"],
    )
    op.add_column(
        "matter_embedding_job",
        sa.Column("current_workflow_run_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_embedding_job_current_workflow",
        "matter_embedding_job",
        "workflow_run",
        ["current_workflow_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_matter_embedding_job_current_workflow_run_id",
        "matter_embedding_job",
        ["current_workflow_run_id"],
    )

    bind = op.get_bind()
    metadata = sa.MetaData()
    workflow_run = sa.Table("workflow_run", metadata, autoload_with=bind)
    analysis_run = sa.Table("matter_analysis_task_run", metadata, autoload_with=bind)
    matter = sa.Table("matter", metadata, autoload_with=bind)
    client = sa.Table("client", metadata, autoload_with=bind)
    rows = bind.execute(
        sa.select(
            analysis_run.c.id,
            analysis_run.c.tenant_id,
            matter.c.client_id,
            analysis_run.c.matter_id,
            analysis_run.c.matter_analysis_task_id,
            analysis_run.c.matter_analysis_task_version_id,
            analysis_run.c.scope_type,
            analysis_run.c.scope_id,
            analysis_run.c.embedding_job_id,
            analysis_run.c.classification_model_id,
            analysis_run.c.classification_model_version_id,
            analysis_run.c.idempotency_key,
            analysis_run.c.workflow_id,
            analysis_run.c.status,
            analysis_run.c.current_stage,
            analysis_run.c.progress_completed,
            analysis_run.c.progress_total,
            analysis_run.c.failure_code,
            analysis_run.c.failure_summary,
            analysis_run.c.failure_retriable,
            analysis_run.c.created_by_user_id,
            analysis_run.c.started_at,
            analysis_run.c.completed_at,
            analysis_run.c.canceled_at,
            analysis_run.c.created_at,
            analysis_run.c.updated_at,
        ).join(matter, matter.c.id == analysis_run.c.matter_id)
    ).mappings().all()
    for row in rows:
        existing_id = bind.scalar(
            sa.select(workflow_run.c.id).where(
                workflow_run.c.dbos_workflow_id == row["workflow_id"]
            )
        )
        execution_id = existing_id or uuid.uuid4()
        execution_status = row["status"] if row["status"] in {
            "QUEUED",
            "COMPLETED",
            "COMPLETED_WITH_ERRORS",
            "FAILED",
            "CANCELED",
        } else "RUNNING"
        values = {
            "tenant_id": row["tenant_id"],
            "client_id": row["client_id"],
            "matter_id": row["matter_id"],
            "job_type": "CLASSIFICATION_ANALYSIS_TASK",
            "job_id": row["id"],
            "attempt_number": 1,
            "retry_of_workflow_run_id": None,
            "workflow_key": "classification_analysis_task_v1",
            "code_version": "1",
            "dbos_workflow_id": row["workflow_id"],
            "idempotency_key": row["idempotency_key"],
            "status": execution_status,
            "current_stage": row["current_stage"],
            "progress_completed": row["progress_completed"],
            "progress_total": row["progress_total"],
            "progress_unit": "stages",
            "input_snapshot": {
                "matter_analysis_task_id": str(row["matter_analysis_task_id"]),
                "matter_analysis_task_version_id": str(row["matter_analysis_task_version_id"]),
                "scope_type": row["scope_type"],
                "scope_id": str(row["scope_id"]),
                "embedding_job_id": str(row["embedding_job_id"]),
                "classification_model_id": str(row["classification_model_id"]),
                "classification_model_version_id": str(row["classification_model_version_id"]),
                "backfilled": True,
            },
            "binding_snapshot": {},
            "configuration_snapshot": {"backfilled": True},
            "progress": {},
            "request_count": 0,
            "tool_call_count": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 0,
            "initiated_by_user_id": row["created_by_user_id"],
            "cancel_requested_at": row["canceled_at"],
            "cancel_requested_by_user_id": None,
            "last_heartbeat_at": row["updated_at"] if execution_status in {"QUEUED", "RUNNING"} else None,
            "failure_code": row["failure_code"],
            "failure_summary": row["failure_summary"],
            "failure_retriable": row["failure_retriable"],
            "error_message": row["failure_summary"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if existing_id is None:
            bind.execute(workflow_run.insert().values(id=execution_id, **values))
        else:
            bind.execute(
                workflow_run.update()
                .where(workflow_run.c.id == execution_id)
                .values(**values)
            )
        bind.execute(
            analysis_run.update()
            .where(analysis_run.c.id == row["id"])
            .values(current_workflow_run_id=execution_id)
        )

    review_batch_run = sa.Table("review_batch_run", metadata, autoload_with=bind)
    batch_rows = bind.execute(
        sa.select(
            review_batch_run.c.id.label("job_id"),
            review_batch_run.c.processed_document_count,
            workflow_run.c.id.label("execution_id"),
            workflow_run.c.status,
            workflow_run.c.progress,
            workflow_run.c.error_message,
            workflow_run.c.updated_at,
        ).join(
            workflow_run,
            workflow_run.c.id == review_batch_run.c.workflow_run_record_id,
        ).where(review_batch_run.c.run_type == "WORKFLOW")
    ).mappings().all()
    for row in batch_rows:
        progress = row["progress"] or {}
        bind.execute(
            workflow_run.update()
            .where(workflow_run.c.id == row["execution_id"])
            .values(
                job_type="ANALYSIS_TASK_BATCH_RUN",
                job_id=row["job_id"],
                attempt_number=1,
                current_stage=progress.get("stage") or row["status"],
                progress_completed=row["processed_document_count"],
                progress_total=progress.get("total"),
                progress_unit="documents",
                last_heartbeat_at=(
                    row["updated_at"] if row["status"] in {"QUEUED", "RUNNING"} else None
                ),
                failure_code=("ANALYSIS_TASK_BATCH_FAILED" if row["status"] == "FAILED" else None),
                failure_summary=row["error_message"],
                failure_retriable=row["status"] == "FAILED",
            )
        )

    embedding_job = sa.Table("matter_embedding_job", metadata, autoload_with=bind)
    embedding_rows = bind.execute(
        sa.select(
            embedding_job.c.id,
            matter.c.client_id,
            client.c.tenant_id,
            embedding_job.c.matter_id,
            embedding_job.c.workflow_id,
            embedding_job.c.status,
            embedding_job.c.configuration,
            embedding_job.c.configuration_hash,
            embedding_job.c.embedding_model,
            embedding_job.c.embedding_model_revision,
            embedding_job.c.embedding_dimensions,
            embedding_job.c.total_count,
            embedding_job.c.processed_count,
            embedding_job.c.embedded_count,
            embedding_job.c.skipped_count,
            embedding_job.c.failed_count,
            embedding_job.c.chunk_count,
            embedding_job.c.error_message,
            embedding_job.c.created_by_user_id,
            embedding_job.c.started_at,
            embedding_job.c.completed_at,
            embedding_job.c.canceled_at,
            embedding_job.c.created_at,
            embedding_job.c.updated_at,
        )
        .join(matter, matter.c.id == embedding_job.c.matter_id)
        .join(client, client.c.id == matter.c.client_id)
    ).mappings().all()
    for row in embedding_rows:
        execution_id = uuid.uuid4()
        execution_status = row["status"] if row["status"] in {
            "QUEUED",
            "COMPLETED",
            "COMPLETED_WITH_ERRORS",
            "FAILED",
            "CANCELED",
        } else "RUNNING"
        current_stage = {
            "QUEUED": "QUEUED",
            "PLANNING": "PLANNING",
            "RUNNING": "PROCESSING_BATCHES",
            "COMPLETED": "COMPLETED",
            "COMPLETED_WITH_ERRORS": "COMPLETED_WITH_ERRORS",
            "FAILED": "FAILED",
            "CANCELED": "CANCELED",
        }[row["status"]]
        bind.execute(
            workflow_run.insert().values(
                id=execution_id,
                tenant_id=row["tenant_id"],
                client_id=row["client_id"],
                matter_id=row["matter_id"],
                job_type="MATTER_EMBEDDING_JOB",
                job_id=row["id"],
                attempt_number=1,
                workflow_key="matter_embedding_job_v2",
                code_version="2",
                dbos_workflow_id=row["workflow_id"],
                idempotency_key=f"matter-embedding:{row['id']}:attempt:1",
                status=execution_status,
                current_stage=current_stage,
                progress_completed=row["processed_count"],
                progress_total=row["total_count"] or None,
                progress_unit="documents",
                input_snapshot={
                    "matter_id": str(row["matter_id"]),
                    "embedding_model": row["embedding_model"],
                    "embedding_model_revision": row["embedding_model_revision"],
                    "embedding_dimensions": row["embedding_dimensions"],
                    "backfilled": True,
                },
                binding_snapshot={},
                configuration_snapshot=row["configuration"] or {},
                progress={
                    "embedded_count": row["embedded_count"],
                    "skipped_count": row["skipped_count"],
                    "failed_count": row["failed_count"],
                    "chunk_count": row["chunk_count"],
                    "configuration_hash": row["configuration_hash"],
                },
                request_count=0,
                tool_call_count=0,
                input_tokens=0,
                cached_input_tokens=0,
                cache_write_tokens=0,
                output_tokens=0,
                initiated_by_user_id=row["created_by_user_id"],
                cancel_requested_at=row["canceled_at"],
                cancel_requested_by_user_id=None,
                last_heartbeat_at=(
                    row["updated_at"] if execution_status in {"QUEUED", "RUNNING"} else None
                ),
                failure_code=("MATTER_EMBEDDING_FAILED" if execution_status == "FAILED" else None),
                failure_summary=row["error_message"],
                failure_retriable=False,
                error_message=row["error_message"],
                started_at=row["started_at"],
                completed_at=row["completed_at"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        )
        bind.execute(
            embedding_job.update()
            .where(embedding_job.c.id == row["id"])
            .values(current_workflow_run_id=execution_id)
        )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM workflow_run WHERE job_type = 'MATTER_EMBEDDING_JOB'")
    )
    op.drop_index(
        "ix_matter_embedding_job_current_workflow_run_id",
        table_name="matter_embedding_job",
    )
    op.drop_constraint(
        "fk_embedding_job_current_workflow",
        "matter_embedding_job",
        type_="foreignkey",
    )
    op.drop_column("matter_embedding_job", "current_workflow_run_id")
    op.execute(
        sa.text(
            "UPDATE workflow_run SET job_type = NULL, job_id = NULL "
            "WHERE job_type = 'ANALYSIS_TASK_BATCH_RUN'"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM workflow_run WHERE job_type = 'CLASSIFICATION_ANALYSIS_TASK'"
        )
    )
    op.drop_index(
        "ix_analysis_task_run_current_workflow",
        table_name="matter_analysis_task_run",
    )
    op.drop_constraint(
        "fk_analysis_task_run_current_workflow",
        "matter_analysis_task_run",
        type_="foreignkey",
    )
    op.drop_column("matter_analysis_task_run", "current_workflow_run_id")

    op.drop_index("ix_workflow_run_cancel_requested_by_user_id", table_name="workflow_run")
    op.drop_index("ix_workflow_run_retry_of", table_name="workflow_run")
    op.drop_index("ix_workflow_run_status_heartbeat", table_name="workflow_run")
    op.drop_index("ix_workflow_run_matter_status_created", table_name="workflow_run")
    op.drop_index("ix_workflow_run_job_attempt", table_name="workflow_run")
    op.drop_index("ix_workflow_run_job_id", table_name="workflow_run")
    op.drop_index("ix_workflow_run_job_type", table_name="workflow_run")
    op.drop_constraint("uq_workflow_run_job_attempt", "workflow_run", type_="unique")
    op.drop_constraint("ck_workflow_run_progress_nonnegative", "workflow_run", type_="check")
    op.drop_constraint("ck_workflow_run_attempt_positive", "workflow_run", type_="check")
    op.drop_constraint("fk_workflow_run_cancel_requested_by", "workflow_run", type_="foreignkey")
    op.drop_constraint("fk_workflow_run_retry_of", "workflow_run", type_="foreignkey")
    op.drop_column("workflow_run", "failure_retriable")
    op.drop_column("workflow_run", "failure_summary")
    op.drop_column("workflow_run", "failure_code")
    op.drop_column("workflow_run", "last_heartbeat_at")
    op.drop_column("workflow_run", "cancel_requested_by_user_id")
    op.drop_column("workflow_run", "cancel_requested_at")
    op.drop_column("workflow_run", "progress_unit")
    op.drop_column("workflow_run", "progress_total")
    op.drop_column("workflow_run", "progress_completed")
    op.drop_column("workflow_run", "current_stage")
    op.drop_column("workflow_run", "idempotency_key")
    op.drop_column("workflow_run", "retry_of_workflow_run_id")
    op.drop_column("workflow_run", "attempt_number")
    op.drop_column("workflow_run", "job_id")
    op.drop_column("workflow_run", "job_type")
    op.drop_constraint("ck_workflow_run_status", "workflow_run", type_="check")
    op.create_check_constraint(
        "ck_workflow_run_status",
        "workflow_run",
        "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
    )
