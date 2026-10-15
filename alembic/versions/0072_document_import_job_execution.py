"""normalize matter document import job execution

Revision ID: 0072_document_import_execution
Revises: 0071_job_execution
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0072_document_import_execution"
down_revision: str | None = "0071_job_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "matter_document_import_job",
        sa.Column("current_workflow_run_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_matter_document_import_current_workflow",
        "matter_document_import_job",
        "workflow_run",
        ["current_workflow_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_matter_document_import_job_current_workflow_run_id",
        "matter_document_import_job",
        ["current_workflow_run_id"],
    )

    bind = op.get_bind()
    metadata = sa.MetaData()
    workflow_run = sa.Table("workflow_run", metadata, autoload_with=bind)
    import_job = sa.Table("matter_document_import_job", metadata, autoload_with=bind)
    matter = sa.Table("matter", metadata, autoload_with=bind)
    client = sa.Table("client", metadata, autoload_with=bind)
    rows = bind.execute(
        sa.select(
            import_job.c.id,
            client.c.tenant_id,
            matter.c.client_id,
            import_job.c.matter_id,
            import_job.c.source_collection_id,
            import_job.c.selection_type,
            import_job.c.selection,
            import_job.c.selection_summary,
            import_job.c.workflow_id,
            import_job.c.status,
            import_job.c.matched_count,
            import_job.c.processed_count,
            import_job.c.added_count,
            import_job.c.duplicate_count,
            import_job.c.failed_count,
            import_job.c.imported_label_document_count,
            import_job.c.imported_label_event_count,
            import_job.c.imported_label_warning_count,
            import_job.c.error_message,
            import_job.c.created_by_user_id,
            import_job.c.started_at,
            import_job.c.completed_at,
            import_job.c.canceled_at,
            import_job.c.created_at,
            import_job.c.updated_at,
        )
        .join(matter, matter.c.id == import_job.c.matter_id)
        .join(client, client.c.id == matter.c.client_id)
    ).mappings().all()
    for row in rows:
        execution_id = uuid.uuid4()
        execution_status = row["status"] if row["status"] in {
            "QUEUED",
            "COMPLETED",
            "FAILED",
            "CANCELED",
        } else "RUNNING"
        current_stage = {
            "QUEUED": "QUEUED",
            "SNAPSHOTTING": "FREEZING_SELECTION",
            "RUNNING": "PROCESSING_BATCHES",
            "COMPLETED": "COMPLETED",
            "FAILED": "FAILED",
            "CANCELED": "CANCELED",
        }[row["status"]]
        bind.execute(
            workflow_run.insert().values(
                id=execution_id,
                tenant_id=row["tenant_id"],
                client_id=row["client_id"],
                matter_id=row["matter_id"],
                job_type="MATTER_DOCUMENT_IMPORT",
                job_id=row["id"],
                attempt_number=1,
                workflow_key="matter_document_import",
                code_version="1",
                dbos_workflow_id=row["workflow_id"],
                idempotency_key=f"matter-document-import:{row['id']}:attempt:1",
                status=execution_status,
                current_stage=current_stage,
                progress_completed=row["processed_count"],
                progress_total=row["matched_count"] or None,
                progress_unit="documents",
                input_snapshot={
                    "matter_id": str(row["matter_id"]),
                    "source_collection_id": str(row["source_collection_id"]),
                    "selection_type": row["selection_type"],
                    "selection": row["selection"],
                    "selection_summary": row["selection_summary"],
                    "backfilled": True,
                },
                binding_snapshot={},
                configuration_snapshot={"backfilled": True},
                progress={
                    "added_count": row["added_count"],
                    "duplicate_count": row["duplicate_count"],
                    "failed_count": row["failed_count"],
                    "imported_label_document_count": row["imported_label_document_count"],
                    "imported_label_event_count": row["imported_label_event_count"],
                    "imported_label_warning_count": row["imported_label_warning_count"],
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
                failure_code=("MATTER_DOCUMENT_IMPORT_FAILED" if execution_status == "FAILED" else None),
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
            import_job.update()
            .where(import_job.c.id == row["id"])
            .values(current_workflow_run_id=execution_id)
        )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM workflow_run WHERE job_type = 'MATTER_DOCUMENT_IMPORT'")
    )
    op.drop_index(
        "ix_matter_document_import_job_current_workflow_run_id",
        table_name="matter_document_import_job",
    )
    op.drop_constraint(
        "fk_matter_document_import_current_workflow",
        "matter_document_import_job",
        type_="foreignkey",
    )
    op.drop_column("matter_document_import_job", "current_workflow_run_id")
