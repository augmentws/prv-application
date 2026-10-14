"""normalize review batch materialization execution

Revision ID: 0073_review_batch_execution
Revises: 0072_document_import_execution
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0073_review_batch_execution"
down_revision: str | None = "0072_document_import_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_review_batch_status", "review_batch", type_="check")
    op.create_check_constraint(
        "ck_review_batch_status",
        "review_batch",
        "status IN ('QUEUED', 'BUILDING', 'READY', 'FAILED', 'CANCELED', 'ARCHIVED')",
    )
    op.add_column(
        "review_batch",
        sa.Column("current_workflow_run_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_review_batch_current_workflow",
        "review_batch",
        "workflow_run",
        ["current_workflow_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_review_batch_current_workflow_run_id",
        "review_batch",
        ["current_workflow_run_id"],
    )

    bind = op.get_bind()
    metadata = sa.MetaData()
    workflow_run = sa.Table("workflow_run", metadata, autoload_with=bind)
    review_batch = sa.Table("review_batch", metadata, autoload_with=bind)
    matter = sa.Table("matter", metadata, autoload_with=bind)
    client = sa.Table("client", metadata, autoload_with=bind)
    rows = bind.execute(
        sa.select(
            review_batch.c.id,
            client.c.tenant_id,
            matter.c.client_id,
            review_batch.c.matter_id,
            review_batch.c.selection_type,
            review_batch.c.selection_definition,
            review_batch.c.source_batch_id,
            review_batch.c.sample_size,
            review_batch.c.random_seed,
            review_batch.c.workflow_id,
            review_batch.c.status,
            review_batch.c.search_status,
            review_batch.c.document_count,
            review_batch.c.error_message,
            review_batch.c.search_error_message,
            review_batch.c.created_by_user_id,
            review_batch.c.completed_at,
            review_batch.c.created_at,
            review_batch.c.updated_at,
        )
        .join(matter, matter.c.id == review_batch.c.matter_id)
        .join(client, client.c.id == matter.c.client_id)
        .where(review_batch.c.selection_type != "DEFINITION_ASSESSMENT")
    ).mappings().all()
    for row in rows:
        execution_id = uuid.uuid4()
        execution_status = {
            "QUEUED": "QUEUED",
            "BUILDING": "RUNNING",
            "READY": (
                "COMPLETED_WITH_ERRORS"
                if row["search_status"] == "FAILED"
                else "COMPLETED"
            ),
            "FAILED": "FAILED",
            "ARCHIVED": "COMPLETED",
        }[row["status"]]
        current_stage = {
            "QUEUED": "QUEUED",
            "BUILDING": "MATERIALIZING_POPULATION",
            "READY": (
                "COMPLETED_WITH_ERRORS"
                if row["search_status"] == "FAILED"
                else "COMPLETED"
            ),
            "FAILED": "FAILED",
            "ARCHIVED": "COMPLETED",
        }[row["status"]]
        dbos_workflow_id = row["workflow_id"]
        if bind.scalar(
            sa.select(workflow_run.c.id).where(
                workflow_run.c.dbos_workflow_id == dbos_workflow_id
            )
        ) is not None:
            dbos_workflow_id = f"review-batch-backfill:{row['id']}"
        terminal = execution_status in {
            "COMPLETED",
            "COMPLETED_WITH_ERRORS",
            "FAILED",
            "CANCELED",
        }
        bind.execute(
            workflow_run.insert().values(
                id=execution_id,
                tenant_id=row["tenant_id"],
                client_id=row["client_id"],
                matter_id=row["matter_id"],
                job_type="REVIEW_BATCH_MATERIALIZATION",
                job_id=row["id"],
                attempt_number=1,
                workflow_key="review_batch_build",
                code_version="1",
                dbos_workflow_id=dbos_workflow_id,
                idempotency_key=f"review-batch:{row['id']}:attempt:1",
                status=execution_status,
                current_stage=current_stage,
                progress_completed=row["document_count"],
                progress_total=(
                    row["document_count"]
                    if terminal or row["document_count"]
                    else row["sample_size"]
                ),
                progress_unit="documents",
                input_snapshot={
                    "matter_id": str(row["matter_id"]),
                    "selection_type": row["selection_type"],
                    "selection_definition": row["selection_definition"],
                    "source_batch_id": (
                        str(row["source_batch_id"]) if row["source_batch_id"] else None
                    ),
                    "sample_size": row["sample_size"],
                    "random_seed": row["random_seed"],
                    "backfilled": True,
                },
                binding_snapshot={},
                configuration_snapshot={"backfilled": True},
                progress={
                    "membership_status": row["status"],
                    "search_status": row["search_status"],
                },
                request_count=0,
                tool_call_count=0,
                input_tokens=0,
                cached_input_tokens=0,
                cache_write_tokens=0,
                output_tokens=0,
                initiated_by_user_id=row["created_by_user_id"],
                last_heartbeat_at=(row["updated_at"] if not terminal else None),
                failure_code=(
                    "REVIEW_BATCH_MATERIALIZATION_FAILED"
                    if execution_status == "FAILED"
                    else (
                        "REVIEW_BATCH_SEARCH_PROJECTION_FAILED"
                        if execution_status == "COMPLETED_WITH_ERRORS"
                        else None
                    )
                ),
                failure_summary=(
                    row["error_message"]
                    if execution_status == "FAILED"
                    else row["search_error_message"]
                ),
                failure_retriable=execution_status in {"FAILED", "COMPLETED_WITH_ERRORS"},
                error_message=(row["error_message"] or row["search_error_message"]),
                completed_at=row["completed_at"] if terminal else None,
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        )
        bind.execute(
            review_batch.update()
            .where(review_batch.c.id == row["id"])
            .values(current_workflow_run_id=execution_id)
        )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM workflow_run WHERE job_type = 'REVIEW_BATCH_MATERIALIZATION'")
    )
    op.drop_index(
        "ix_review_batch_current_workflow_run_id",
        table_name="review_batch",
    )
    op.drop_constraint(
        "fk_review_batch_current_workflow",
        "review_batch",
        type_="foreignkey",
    )
    op.drop_column("review_batch", "current_workflow_run_id")
    op.drop_constraint("ck_review_batch_status", "review_batch", type_="check")
    op.create_check_constraint(
        "ck_review_batch_status",
        "review_batch",
        "status IN ('QUEUED', 'BUILDING', 'READY', 'FAILED', 'ARCHIVED')",
    )
