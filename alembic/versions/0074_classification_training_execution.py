"""normalize classification training job execution

Revision ID: 0074_classification_training
Revises: 0073_review_batch_execution
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0074_classification_training"
down_revision: str | None = "0073_review_batch_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "classification_training_job",
        sa.Column("current_workflow_run_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_classification_training_job_current_workflow",
        "classification_training_job",
        "workflow_run",
        ["current_workflow_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_classification_training_job_current_workflow_run_id",
        "classification_training_job",
        ["current_workflow_run_id"],
    )

    bind = op.get_bind()
    metadata = sa.MetaData()
    workflow_run = sa.Table("workflow_run", metadata, autoload_with=bind)
    training_job = sa.Table("classification_training_job", metadata, autoload_with=bind)
    matter = sa.Table("matter", metadata, autoload_with=bind)
    rows = bind.execute(
        sa.select(
            training_job.c.id,
            training_job.c.tenant_id,
            matter.c.client_id,
            training_job.c.matter_id,
            training_job.c.classification_model_id,
            training_job.c.configuration_id,
            training_job.c.embedding_job_id,
            training_job.c.scope_type,
            training_job.c.scope_id,
            training_job.c.random_seed,
            training_job.c.validation_specification,
            training_job.c.dataset_snapshot_id,
            training_job.c.training_run_id,
            training_job.c.workflow_id,
            training_job.c.status,
            training_job.c.current_stage,
            training_job.c.progress_completed,
            training_job.c.progress_total,
            training_job.c.failure_code,
            training_job.c.failure_summary,
            training_job.c.failure_retriable,
            training_job.c.created_by_user_id,
            training_job.c.started_at,
            training_job.c.completed_at,
            training_job.c.canceled_at,
            training_job.c.created_at,
            training_job.c.updated_at,
        ).join(matter, matter.c.id == training_job.c.matter_id)
    ).mappings().all()
    for row in rows:
        execution_id = uuid.uuid4()
        execution_status = (
            row["status"]
            if row["status"] in {"QUEUED", "COMPLETED", "FAILED", "CANCELED"}
            else ("WAITING" if row["status"] == "TRAINING" else "RUNNING")
        )
        dbos_workflow_id = row["workflow_id"]
        if bind.scalar(
            sa.select(workflow_run.c.id).where(
                workflow_run.c.dbos_workflow_id == dbos_workflow_id
            )
        ) is not None:
            dbos_workflow_id = f"classification-training-backfill:{row['id']}"
        bind.execute(
            workflow_run.insert().values(
                id=execution_id,
                tenant_id=row["tenant_id"],
                client_id=row["client_id"],
                matter_id=row["matter_id"],
                job_type="CLASSIFICATION_TRAINING_JOB",
                job_id=row["id"],
                attempt_number=1,
                workflow_key="classification_training_job_v1",
                code_version="1",
                dbos_workflow_id=dbos_workflow_id,
                idempotency_key=f"classification-training-job:{row['id']}:attempt:1",
                status=execution_status,
                current_stage=row["current_stage"],
                progress_completed=row["progress_completed"],
                progress_total=row["progress_total"],
                progress_unit="stages",
                input_snapshot={
                    "classification_model_id": str(row["classification_model_id"]),
                    "configuration_id": str(row["configuration_id"]),
                    "embedding_job_id": str(row["embedding_job_id"]),
                    "scope_type": row["scope_type"],
                    "scope_id": str(row["scope_id"]),
                    "random_seed": row["random_seed"],
                    "backfilled": True,
                },
                binding_snapshot={},
                configuration_snapshot={
                    "validation_specification": row["validation_specification"],
                    "backfilled": True,
                },
                progress={
                    "dataset_snapshot_id": (
                        str(row["dataset_snapshot_id"]) if row["dataset_snapshot_id"] else None
                    ),
                    "training_run_id": (
                        str(row["training_run_id"]) if row["training_run_id"] else None
                    ),
                },
                request_count=0,
                tool_call_count=0,
                input_tokens=0,
                cached_input_tokens=0,
                cache_write_tokens=0,
                output_tokens=0,
                initiated_by_user_id=row["created_by_user_id"],
                cancel_requested_at=row["canceled_at"],
                last_heartbeat_at=(
                    row["updated_at"]
                    if execution_status in {"QUEUED", "RUNNING", "WAITING"}
                    else None
                ),
                failure_code=row["failure_code"],
                failure_summary=row["failure_summary"],
                failure_retriable=row["failure_retriable"],
                error_message=row["failure_summary"],
                started_at=row["started_at"],
                completed_at=row["completed_at"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        )
        bind.execute(
            training_job.update()
            .where(training_job.c.id == row["id"])
            .values(current_workflow_run_id=execution_id)
        )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM workflow_run WHERE job_type = 'CLASSIFICATION_TRAINING_JOB'")
    )
    op.drop_index(
        "ix_classification_training_job_current_workflow_run_id",
        table_name="classification_training_job",
    )
    op.drop_constraint(
        "fk_classification_training_job_current_workflow",
        "classification_training_job",
        type_="foreignkey",
    )
    op.drop_column("classification_training_job", "current_workflow_run_id")
