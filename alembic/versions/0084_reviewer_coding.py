"""Replace human review runs with canonical reviewer coding.

Revision ID: 0084_reviewer_coding
Revises: 0083_search_result_exports
"""

import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa

from alembic import op

revision: str = "0084_reviewer_coding"
down_revision: str | None = "0083_search_result_exports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _typed_value_columns() -> list[sa.Column]:
    return [
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_long", sa.BigInteger(), nullable=True),
        sa.Column("value_float", sa.Float(), nullable=True),
        sa.Column("value_boolean", sa.Boolean(), nullable=True),
        sa.Column("value_date", sa.Date(), nullable=True),
        sa.Column("value_datetime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("value_json", sa.JSON(), nullable=True),
    ]


VALUE_SHAPE = (
    "(CASE WHEN value_text IS NOT NULL THEN 1 ELSE 0 END + "
    "CASE WHEN value_long IS NOT NULL THEN 1 ELSE 0 END + "
    "CASE WHEN value_float IS NOT NULL THEN 1 ELSE 0 END + "
    "CASE WHEN value_boolean IS NOT NULL THEN 1 ELSE 0 END + "
    "CASE WHEN value_date IS NOT NULL THEN 1 ELSE 0 END + "
    "CASE WHEN value_datetime IS NOT NULL THEN 1 ELSE 0 END + "
    "CASE WHEN value_json IS NOT NULL THEN 1 ELSE 0 END) = 1"
)


def _rewrite_label_policy(policy: dict, source_id_map: dict[str, str]) -> dict:
    rewritten = json.loads(json.dumps(policy))
    sources = rewritten.get("allowed_sources")
    if not isinstance(sources, list):
        return rewritten
    unique: list[dict] = []
    seen: set[tuple[object, object]] = set()
    for source in sources:
        if not isinstance(source, dict):
            continue
        reference = source.get("source_reference_id")
        if reference is not None and str(reference) in source_id_map:
            source["source_reference_id"] = source_id_map[str(reference)]
        identity = (source.get("kind"), source.get("source_reference_id"))
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(source)
    rewritten["allowed_sources"] = unique
    return rewritten


def _configuration_hash(row: dict, label_policy: dict) -> str:
    specification = {
        "configuration_schema_version": 1,
        "target_field_snapshot": row["target_field_snapshot"],
        "classification_strategy": row["classification_strategy"],
        "ordered_class_keys": row["ordered_class_keys"],
        "label_policy": label_policy,
        "feature_contract": row["feature_contract"],
        "prediction_resolution_policy": row["prediction_resolution_policy"],
        "trainer_specification": row["trainer_specification"],
    }
    if row["binary_mapping"] is not None:
        specification["binary_mapping"] = row["binary_mapping"]
    def without_none(value):
        if isinstance(value, dict):
            return {key: without_none(item) for key, item in value.items() if item is not None}
        if isinstance(value, list):
            return [without_none(item) for item in value]
        return value

    encoded = json.dumps(
        without_none(specification),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _rewrite_classification_sources(connection, source_id_map: dict[str, str]) -> None:
    if not source_id_map:
        return
    rows = list(
        connection.execute(
            sa.text(
                "SELECT id, classification_model_id, target_field_snapshot, classification_strategy, "
                "ordered_class_keys, binary_mapping, label_policy, feature_contract, "
                "prediction_resolution_policy, trainer_specification, configuration_hash "
                "FROM classification_model_configuration ORDER BY created_at, id"
            )
        ).mappings()
    )
    claimed_hashes = {
        (str(row["classification_model_id"]), row["configuration_hash"]): str(row["id"])
        for row in rows
    }
    update_configuration = sa.text(
        "UPDATE classification_model_configuration "
        "SET label_policy = :label_policy, configuration_hash = :configuration_hash WHERE id = :id"
    ).bindparams(sa.bindparam("label_policy", type_=sa.JSON()))
    for row in rows:
        rewritten = _rewrite_label_policy(row["label_policy"], source_id_map)
        if rewritten == row["label_policy"]:
            continue
        candidate_hash = _configuration_hash(row, rewritten)
        identity = (str(row["classification_model_id"]), candidate_hash)
        owner = claimed_hashes.get(identity)
        effective_hash = candidate_hash if owner in {None, str(row["id"])} else row["configuration_hash"]
        if effective_hash == candidate_hash:
            claimed_hashes.pop((str(row["classification_model_id"]), row["configuration_hash"]), None)
            claimed_hashes[identity] = str(row["id"])
        connection.execute(
            update_configuration,
            {"id": row["id"], "label_policy": rewritten, "configuration_hash": effective_hash},
        )

    snapshot_rows = connection.execute(
        sa.text("SELECT id, label_policy_snapshot FROM classification_dataset_snapshot")
    ).mappings()
    update_snapshot = sa.text(
        "UPDATE classification_dataset_snapshot SET label_policy_snapshot = :label_policy WHERE id = :id"
    ).bindparams(sa.bindparam("label_policy", type_=sa.JSON()))
    for row in snapshot_rows:
        rewritten = _rewrite_label_policy(row["label_policy_snapshot"], source_id_map)
        if rewritten != row["label_policy_snapshot"]:
            connection.execute(update_snapshot, {"id": row["id"], "label_policy": rewritten})


def upgrade() -> None:
    op.create_table(
        "review_batch_reviewer_coding",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("review_batch_id", sa.Uuid(), sa.ForeignKey("review_batch.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reviewer_user_id", sa.Uuid(), sa.ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 0", name="ck_review_batch_reviewer_coding_revision"),
        sa.UniqueConstraint("review_batch_id", "reviewer_user_id", name="uq_review_batch_reviewer_coding_user"),
    )
    op.create_index("ix_review_batch_reviewer_coding_review_batch_id", "review_batch_reviewer_coding", ["review_batch_id"])
    op.create_index("ix_review_batch_reviewer_coding_reviewer_user_id", "review_batch_reviewer_coding", ["reviewer_user_id"])

    op.create_table(
        "review_batch_reviewer_document",
        sa.Column("reviewer_coding_id", sa.Uuid(), sa.ForeignKey("review_batch_reviewer_coding.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("matter_document_id", sa.Uuid(), sa.ForeignKey("matter_document.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('IN_PROGRESS', 'COMPLETED', 'SKIPPED')", name="ck_review_batch_reviewer_document_status"),
        sa.CheckConstraint("revision >= 1", name="ck_review_batch_reviewer_document_revision"),
    )
    op.create_index("ix_review_batch_reviewer_document_matter_document_id", "review_batch_reviewer_document", ["matter_document_id"])
    op.create_index("ix_review_batch_reviewer_document_coding_status", "review_batch_reviewer_document", ["reviewer_coding_id", "status"])

    op.create_table(
        "review_batch_reviewer_value",
        sa.Column("reviewer_coding_id", sa.Uuid(), sa.ForeignKey("review_batch_reviewer_coding.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("matter_document_id", sa.Uuid(), sa.ForeignKey("matter_document.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("metadata_definition_id", sa.Uuid(), sa.ForeignKey("metadata_definition.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("value_ordinal", sa.Integer(), primary_key=True),
        *_typed_value_columns(),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("value_ordinal >= 0", name="ck_review_batch_reviewer_value_ordinal"),
        sa.CheckConstraint("revision >= 1", name="ck_review_batch_reviewer_value_revision"),
        sa.CheckConstraint(VALUE_SHAPE, name="ck_review_batch_reviewer_value_shape"),
    )
    op.create_index("ix_review_batch_reviewer_value_document", "review_batch_reviewer_value", ["matter_document_id", "metadata_definition_id"])

    op.add_column("review_batch_run_promotion", sa.Column("reviewer_coding_id", sa.Uuid(), nullable=True))
    op.add_column("review_batch_run_promotion", sa.Column("reviewer_coding_revision", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_review_batch_run_promotion_reviewer_coding",
        "review_batch_run_promotion",
        "review_batch_reviewer_coding",
        ["reviewer_coding_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_review_batch_run_promotion_reviewer_coding_id", "review_batch_run_promotion", ["reviewer_coding_id"])
    op.alter_column("review_batch_run_promotion", "review_batch_run_id", existing_type=sa.Uuid(), nullable=True)
    op.create_check_constraint(
        "ck_review_batch_run_promotion_reviewer_revision",
        "review_batch_run_promotion",
        "reviewer_coding_revision IS NULL OR reviewer_coding_revision >= 0",
    )

    op.create_table(
        "review_batch_promotion_document",
        sa.Column("promotion_id", sa.Uuid(), sa.ForeignKey("review_batch_run_promotion.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("matter_document_id", sa.Uuid(), sa.ForeignKey("matter_document.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_index("ix_review_batch_promotion_document_matter_document_id", "review_batch_promotion_document", ["matter_document_id"])
    op.create_table(
        "review_batch_promotion_value",
        sa.Column("promotion_id", sa.Uuid(), sa.ForeignKey("review_batch_run_promotion.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("matter_document_id", sa.Uuid(), sa.ForeignKey("matter_document.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("metadata_definition_id", sa.Uuid(), sa.ForeignKey("metadata_definition.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("value_ordinal", sa.Integer(), primary_key=True),
        *_typed_value_columns(),
        sa.CheckConstraint("value_ordinal >= 0", name="ck_review_batch_promotion_value_ordinal"),
        sa.CheckConstraint(VALUE_SHAPE, name="ck_review_batch_promotion_value_shape"),
    )

    connection = op.get_bind()
    now = datetime.now(timezone.utc)
    legacy_run_to_coding: dict[str, str] = {}
    reviewer_pairs = connection.execute(
        sa.text(
            "SELECT DISTINCT review_batch_id, actor_user_id "
            "FROM review_batch_run WHERE run_type = 'HUMAN' AND actor_user_id IS NOT NULL"
        )
    ).mappings()
    for pair in reviewer_pairs:
        coding_id = uuid.uuid4()
        legacy_run_ids = list(
            connection.execute(
                sa.text(
                    "SELECT id FROM review_batch_run WHERE review_batch_id = :batch_id "
                    "AND actor_user_id = :user_id AND run_type = 'HUMAN'"
                ),
                {"batch_id": pair["review_batch_id"], "user_id": pair["actor_user_id"]},
            ).scalars()
        )
        legacy_run_to_coding.update({str(run_id): str(coding_id) for run_id in legacy_run_ids})
        connection.execute(
            sa.text(
                "INSERT INTO review_batch_reviewer_coding "
                "(id, review_batch_id, reviewer_user_id, revision, created_at, updated_at) "
                "VALUES (:id, :batch_id, :user_id, 1, :now, :now)"
            ),
            {"id": coding_id, "batch_id": pair["review_batch_id"], "user_id": pair["actor_user_id"], "now": now},
        )
        connection.execute(
            sa.text(
                "INSERT INTO review_batch_reviewer_document "
                "(reviewer_coding_id, matter_document_id, status, revision, started_at, completed_at, updated_at) "
                "SELECT :coding_id, chosen.matter_document_id, "
                "CASE WHEN chosen.status = 'SKIPPED' THEN 'SKIPPED' ELSE 'COMPLETED' END, 1, "
                "chosen.started_at, chosen.completed_at, chosen.updated_at "
                "FROM (SELECT DISTINCT ON (rd.matter_document_id) rd.* "
                "FROM review_batch_run_document rd JOIN review_batch_run r ON r.id = rd.review_batch_run_id "
                "WHERE r.review_batch_id = :batch_id AND r.actor_user_id = :user_id AND r.run_type = 'HUMAN' "
                "AND rd.status IN ('COMPLETED', 'SKIPPED') "
                "ORDER BY rd.matter_document_id, rd.updated_at DESC, r.created_at DESC, r.id DESC) chosen"
            ),
            {"coding_id": coding_id, "batch_id": pair["review_batch_id"], "user_id": pair["actor_user_id"]},
        )
        connection.execute(
            sa.text(
                "WITH latest AS ("
                " SELECT DISTINCT ON (v.matter_document_id, v.metadata_definition_id) "
                " v.matter_document_id, v.metadata_definition_id, r.id AS run_id "
                " FROM review_batch_run_value v JOIN review_batch_run r ON r.id = v.review_batch_run_id "
                " WHERE r.review_batch_id = :batch_id AND r.actor_user_id = :user_id AND r.run_type = 'HUMAN' "
                " ORDER BY v.matter_document_id, v.metadata_definition_id, v.updated_at DESC, r.created_at DESC, r.id DESC"
                ") INSERT INTO review_batch_reviewer_value "
                "(reviewer_coding_id, matter_document_id, metadata_definition_id, value_ordinal, "
                "value_text, value_long, value_float, value_boolean, value_date, value_datetime, value_json, "
                "revision, created_at, updated_at) "
                "SELECT :coding_id, v.matter_document_id, v.metadata_definition_id, v.value_ordinal, "
                "v.value_text, v.value_long, v.value_float, v.value_boolean, v.value_date, v.value_datetime, v.value_json, "
                "1, v.created_at, v.updated_at FROM review_batch_run_value v JOIN latest l "
                "ON l.run_id = v.review_batch_run_id AND l.matter_document_id = v.matter_document_id "
                "AND l.metadata_definition_id = v.metadata_definition_id"
            ),
            {"coding_id": coding_id, "batch_id": pair["review_batch_id"], "user_id": pair["actor_user_id"]},
        )
        promotions = connection.execute(
            sa.text(
                "SELECT p.id, p.review_batch_run_id FROM review_batch_run_promotion p "
                "JOIN review_batch_run r ON r.id = p.review_batch_run_id "
                "WHERE r.review_batch_id = :batch_id AND r.actor_user_id = :user_id AND r.run_type = 'HUMAN'"
            ),
            {"batch_id": pair["review_batch_id"], "user_id": pair["actor_user_id"]},
        ).mappings()
        for promotion in promotions:
            connection.execute(
                sa.text(
                    "INSERT INTO review_batch_promotion_document (promotion_id, matter_document_id) "
                    "SELECT :promotion_id, matter_document_id FROM review_batch_run_document "
                    "WHERE review_batch_run_id = :run_id AND status = 'COMPLETED'"
                ),
                {"promotion_id": promotion["id"], "run_id": promotion["review_batch_run_id"]},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO review_batch_promotion_value "
                    "(promotion_id, matter_document_id, metadata_definition_id, value_ordinal, value_text, value_long, "
                    "value_float, value_boolean, value_date, value_datetime, value_json) "
                    "SELECT :promotion_id, matter_document_id, metadata_definition_id, value_ordinal, value_text, value_long, "
                    "value_float, value_boolean, value_date, value_datetime, value_json FROM review_batch_run_value "
                    "WHERE review_batch_run_id = :run_id"
                ),
                {"promotion_id": promotion["id"], "run_id": promotion["review_batch_run_id"]},
            )
            connection.execute(
                sa.text(
                    "UPDATE review_batch_run_promotion SET reviewer_coding_id = :coding_id, "
                    "reviewer_coding_revision = 1, review_batch_run_id = NULL "
                    "WHERE id = :promotion_id"
                ),
                {"coding_id": coding_id, "promotion_id": promotion["id"]},
            )

    _rewrite_classification_sources(connection, legacy_run_to_coding)
    connection.execute(sa.text("DELETE FROM review_batch_run WHERE run_type = 'HUMAN'"))
    op.drop_constraint("ck_review_batch_run_actor", "review_batch_run", type_="check")
    op.drop_constraint("ck_review_batch_run_type", "review_batch_run", type_="check")
    op.create_check_constraint("ck_review_batch_run_type", "review_batch_run", "run_type IN ('AGENT', 'WORKFLOW', 'CLASSIFIER')")
    op.create_check_constraint(
        "ck_review_batch_run_actor",
        "review_batch_run",
        "(run_type = 'AGENT' AND actor_user_id IS NULL AND agent_definition_version_id IS NOT NULL "
        "AND workflow_run_record_id IS NULL AND classification_scoring_run_id IS NULL) OR "
        "(run_type = 'WORKFLOW' AND actor_user_id IS NULL AND agent_definition_version_id IS NULL "
        "AND workflow_run_record_id IS NOT NULL AND classification_scoring_run_id IS NULL) OR "
        "(run_type = 'CLASSIFIER' AND actor_user_id IS NULL AND agent_definition_version_id IS NULL "
        "AND workflow_run_record_id IS NULL AND classification_scoring_run_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_review_batch_run_promotion_source",
        "review_batch_run_promotion",
        "(review_batch_run_id IS NOT NULL AND reviewer_coding_id IS NULL "
        "AND reviewer_coding_revision IS NULL) OR "
        "(review_batch_run_id IS NULL AND reviewer_coding_id IS NOT NULL "
        "AND reviewer_coding_revision IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_review_batch_run_promotion_source", "review_batch_run_promotion", type_="check")
    op.drop_constraint("ck_review_batch_run_actor", "review_batch_run", type_="check")
    op.drop_constraint("ck_review_batch_run_type", "review_batch_run", type_="check")
    op.create_check_constraint(
        "ck_review_batch_run_type",
        "review_batch_run",
        "run_type IN ('HUMAN', 'AGENT', 'WORKFLOW', 'CLASSIFIER')",
    )
    op.create_check_constraint(
        "ck_review_batch_run_actor",
        "review_batch_run",
        "(run_type = 'HUMAN' AND actor_user_id IS NOT NULL AND agent_definition_version_id IS NULL "
        "AND workflow_run_record_id IS NULL AND classification_scoring_run_id IS NULL) OR "
        "(run_type = 'AGENT' AND actor_user_id IS NULL AND agent_definition_version_id IS NOT NULL "
        "AND workflow_run_record_id IS NULL AND classification_scoring_run_id IS NULL) OR "
        "(run_type = 'WORKFLOW' AND actor_user_id IS NULL AND agent_definition_version_id IS NULL "
        "AND workflow_run_record_id IS NOT NULL AND classification_scoring_run_id IS NULL) OR "
        "(run_type = 'CLASSIFIER' AND actor_user_id IS NULL AND agent_definition_version_id IS NULL "
        "AND workflow_run_record_id IS NULL AND classification_scoring_run_id IS NOT NULL)",
    )
    connection = op.get_bind()
    coding_to_legacy_run: dict[str, str] = {}
    coding_ids = connection.execute(
        sa.text("SELECT id FROM review_batch_reviewer_coding ORDER BY created_at, id")
    ).scalars()
    for coding_id in coding_ids:
        run_id = uuid.uuid4()
        coding_to_legacy_run[str(coding_id)] = str(run_id)
        connection.execute(
            sa.text(
                "INSERT INTO review_batch_run "
                "(id, review_batch_id, run_type, purpose, status, result_policy, actor_user_id, "
                "configuration_snapshot, initiated_by_user_id, processed_document_count, "
                "started_at, completed_at, created_at, updated_at) "
                "SELECT :run_id, c.review_batch_id, 'HUMAN', 'REFERENCE', 'COMPLETED', 'ISOLATED', "
                "c.reviewer_user_id, '{}'::json, c.reviewer_user_id, COUNT(d.matter_document_id), "
                "MIN(d.started_at), MAX(d.completed_at), c.created_at, c.updated_at "
                "FROM review_batch_reviewer_coding c "
                "LEFT JOIN review_batch_reviewer_document d ON d.reviewer_coding_id = c.id "
                "WHERE c.id = :coding_id GROUP BY c.id"
            ),
            {"run_id": run_id, "coding_id": coding_id},
        )
        connection.execute(
            sa.text(
                "INSERT INTO review_batch_run_document "
                "(review_batch_run_id, matter_document_id, status, started_at, completed_at, updated_at, "
                "failure_retriable) "
                "SELECT :run_id, matter_document_id, status, started_at, completed_at, updated_at, false "
                "FROM review_batch_reviewer_document WHERE reviewer_coding_id = :coding_id"
            ),
            {"run_id": run_id, "coding_id": coding_id},
        )
        connection.execute(
            sa.text(
                "INSERT INTO review_batch_run_value "
                "(review_batch_run_id, matter_document_id, metadata_definition_id, value_ordinal, "
                "value_text, value_long, value_float, value_boolean, value_date, value_datetime, value_json, "
                "created_at, updated_at) "
                "SELECT :run_id, matter_document_id, metadata_definition_id, value_ordinal, "
                "value_text, value_long, value_float, value_boolean, value_date, value_datetime, value_json, "
                "created_at, updated_at FROM review_batch_reviewer_value WHERE reviewer_coding_id = :coding_id"
            ),
            {"run_id": run_id, "coding_id": coding_id},
        )
        connection.execute(
            sa.text(
                "UPDATE review_batch_run_promotion SET review_batch_run_id = :run_id, "
                "reviewer_coding_id = NULL, reviewer_coding_revision = NULL "
                "WHERE reviewer_coding_id = :coding_id"
            ),
            {"run_id": run_id, "coding_id": coding_id},
        )
    _rewrite_classification_sources(connection, coding_to_legacy_run)
    op.drop_table("review_batch_promotion_value")
    op.drop_table("review_batch_promotion_document")
    op.drop_index("ix_review_batch_run_promotion_reviewer_coding_id", table_name="review_batch_run_promotion")
    op.drop_constraint("fk_review_batch_run_promotion_reviewer_coding", "review_batch_run_promotion", type_="foreignkey")
    op.drop_constraint(
        "ck_review_batch_run_promotion_reviewer_revision",
        "review_batch_run_promotion",
        type_="check",
    )
    op.drop_column("review_batch_run_promotion", "reviewer_coding_revision")
    op.drop_column("review_batch_run_promotion", "reviewer_coding_id")
    op.alter_column("review_batch_run_promotion", "review_batch_run_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_table("review_batch_reviewer_value")
    op.drop_table("review_batch_reviewer_document")
    op.drop_table("review_batch_reviewer_coding")
