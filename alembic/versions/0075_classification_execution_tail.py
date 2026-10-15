"""normalize scoring, classifier projection, and coding promotion execution

Revision ID: 0075_classification_tail
Revises: 0074_classification_training
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0075_classification_tail"
down_revision: str | None = "0074_classification_training"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_execution_pointer(table_name: str, constraint_name: str) -> None:
    op.add_column(table_name, sa.Column("current_workflow_run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        constraint_name,
        table_name,
        "workflow_run",
        ["current_workflow_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        f"ix_{table_name}_current_workflow_run_id",
        table_name,
        ["current_workflow_run_id"],
    )


def upgrade() -> None:
    _add_execution_pointer(
        "classification_scoring_run",
        "fk_classification_scoring_run_current_workflow",
    )
    _add_execution_pointer("review_batch_run", "fk_review_batch_run_current_workflow")
    _add_execution_pointer(
        "review_batch_run_promotion",
        "fk_review_batch_run_promotion_current_workflow",
    )

    bind = op.get_bind()
    metadata = sa.MetaData()
    workflow_run = sa.Table("workflow_run", metadata, autoload_with=bind)
    scoring = sa.Table("classification_scoring_run", metadata, autoload_with=bind)
    batch_run = sa.Table("review_batch_run", metadata, autoload_with=bind)
    promotion = sa.Table("review_batch_run_promotion", metadata, autoload_with=bind)
    batch = sa.Table("review_batch", metadata, autoload_with=bind)
    matter = sa.Table("matter", metadata, autoload_with=bind)
    client = sa.Table("client", metadata, autoload_with=bind)

    def unique_workflow_id(candidate: str, fallback: str) -> str:
        exists = bind.scalar(sa.select(workflow_run.c.id).where(workflow_run.c.dbos_workflow_id == candidate))
        return fallback if exists is not None else candidate

    scoring_rows = bind.execute(
        sa.select(scoring, matter.c.client_id).join(matter, matter.c.id == scoring.c.matter_id)
    ).mappings()
    for row in scoring_rows:
        execution_id = uuid.uuid4()
        execution_status = (
            row["status"]
            if row["status"]
            in {
                "QUEUED",
                "COMPLETED",
                "COMPLETED_WITH_ERRORS",
                "FAILED",
                "CANCELED",
            }
            else "RUNNING"
        )
        bind.execute(
            workflow_run.insert().values(
                id=execution_id,
                tenant_id=row["tenant_id"],
                client_id=row["client_id"],
                matter_id=row["matter_id"],
                job_type="CLASSIFICATION_SCORING_RUN",
                job_id=row["id"],
                attempt_number=1,
                workflow_key="classification_model_scoring_v1",
                code_version="1",
                dbos_workflow_id=unique_workflow_id(row["workflow_id"], f"classification-scoring-backfill:{row['id']}"),
                idempotency_key=row["idempotency_key"],
                status=execution_status,
                current_stage=row["current_stage"],
                progress_completed=row["progress_completed"],
                progress_total=row["progress_total"],
                progress_unit="stages",
                input_snapshot={"derivation_key": row["derivation_key"], "backfilled": True},
                binding_snapshot={},
                configuration_snapshot={"scoring_policy": row["scoring_policy"]},
                progress={
                    "population_document_count": row["population_document_count"],
                    "scored_document_count": row["scored_document_count"],
                    "failed_document_count": row["failed_document_count"],
                },
                request_count=0,
                tool_call_count=0,
                input_tokens=0,
                cached_input_tokens=0,
                cache_write_tokens=0,
                output_tokens=0,
                initiated_by_user_id=row["created_by_user_id"],
                cancel_requested_at=row["canceled_at"],
                last_heartbeat_at=(row["updated_at"] if execution_status in {"QUEUED", "RUNNING"} else None),
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
        bind.execute(scoring.update().where(scoring.c.id == row["id"]).values(current_workflow_run_id=execution_id))

    projection_rows = bind.execute(
        sa.select(
            batch_run,
            batch.c.matter_id,
            batch.c.document_count,
            matter.c.client_id,
            client.c.tenant_id,
        )
        .join(batch, batch.c.id == batch_run.c.review_batch_id)
        .join(matter, matter.c.id == batch.c.matter_id)
        .join(client, client.c.id == matter.c.client_id)
        .where(batch_run.c.run_type == "CLASSIFIER")
    ).mappings()
    for row in projection_rows:
        execution_id = uuid.uuid4()
        bind.execute(
            workflow_run.insert().values(
                id=execution_id,
                tenant_id=row["tenant_id"],
                client_id=row["client_id"],
                matter_id=row["matter_id"],
                job_type="CLASSIFICATION_PROJECTION",
                job_id=row["id"],
                attempt_number=1,
                workflow_key="classification_prediction_projection_v1",
                code_version="1",
                dbos_workflow_id=unique_workflow_id(
                    row["dbos_workflow_id"], f"classification-projection-backfill:{row['id']}"
                ),
                idempotency_key=row["idempotency_key"],
                status=row["status"],
                current_stage=row["status"],
                progress_completed=row["processed_document_count"],
                progress_total=row["document_count"],
                progress_unit="documents",
                input_snapshot={"review_batch_id": str(row["review_batch_id"]), "backfilled": True},
                binding_snapshot={},
                configuration_snapshot=row["configuration_snapshot"],
                progress=row["result_summary"] or {},
                request_count=0,
                tool_call_count=0,
                input_tokens=0,
                cached_input_tokens=0,
                cache_write_tokens=0,
                output_tokens=0,
                initiated_by_user_id=row["initiated_by_user_id"],
                last_heartbeat_at=(row["updated_at"] if row["status"] in {"QUEUED", "RUNNING"} else None),
                failure_summary=row["error_message"],
                failure_retriable=row["status"] == "FAILED",
                error_message=row["error_message"],
                started_at=row["started_at"],
                completed_at=row["completed_at"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        )
        bind.execute(batch_run.update().where(batch_run.c.id == row["id"]).values(current_workflow_run_id=execution_id))

    promotion_rows = bind.execute(
        sa.select(promotion, matter.c.client_id, client.c.tenant_id)
        .join(matter, matter.c.id == promotion.c.matter_id)
        .join(client, client.c.id == matter.c.client_id)
    ).mappings()
    for row in promotion_rows:
        execution_id = uuid.uuid4()
        bind.execute(
            workflow_run.insert().values(
                id=execution_id,
                tenant_id=row["tenant_id"],
                client_id=row["client_id"],
                matter_id=row["matter_id"],
                job_type="REVIEW_BATCH_CODING_PROMOTION",
                job_id=row["id"],
                attempt_number=1,
                workflow_key="review_batch_coding_promotion_v1",
                code_version="1",
                dbos_workflow_id=unique_workflow_id(row["dbos_workflow_id"], f"coding-promotion-backfill:{row['id']}"),
                status=row["status"],
                current_stage=row["status"],
                progress_completed=row["processed_document_count"],
                progress_total=(row["eligible_document_count"] or row["total_document_count"]),
                progress_unit="documents",
                input_snapshot={"review_batch_run_id": str(row["review_batch_run_id"]), "backfilled": True},
                binding_snapshot={},
                configuration_snapshot={"destinations": row["destination_snapshot"]},
                progress={
                    "eligible_document_count": row["eligible_document_count"],
                    "skipped_document_count": row["skipped_document_count"],
                    "event_count": row["event_count"],
                },
                request_count=0,
                tool_call_count=0,
                input_tokens=0,
                cached_input_tokens=0,
                cache_write_tokens=0,
                output_tokens=0,
                initiated_by_user_id=row["created_by_user_id"],
                last_heartbeat_at=(row["updated_at"] if row["status"] in {"QUEUED", "RUNNING"} else None),
                failure_code=("CODING_PROMOTION_FAILED" if row["status"] == "FAILED" else None),
                failure_summary=row["error_message"],
                failure_retriable=row["status"] == "FAILED",
                error_message=row["error_message"],
                started_at=row["started_at"],
                completed_at=row["completed_at"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        )
        bind.execute(promotion.update().where(promotion.c.id == row["id"]).values(current_workflow_run_id=execution_id))


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM workflow_run WHERE job_type IN "
            "('CLASSIFICATION_SCORING_RUN', 'CLASSIFICATION_PROJECTION', "
            "'REVIEW_BATCH_CODING_PROMOTION')"
        )
    )
    for table_name, constraint_name in (
        ("review_batch_run_promotion", "fk_review_batch_run_promotion_current_workflow"),
        ("review_batch_run", "fk_review_batch_run_current_workflow"),
        ("classification_scoring_run", "fk_classification_scoring_run_current_workflow"),
    ):
        op.drop_index(f"ix_{table_name}_current_workflow_run_id", table_name=table_name)
        op.drop_constraint(constraint_name, table_name, type_="foreignkey")
        op.drop_column(table_name, "current_workflow_run_id")
