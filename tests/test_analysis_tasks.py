import uuid

from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.classification_analysis_tasks import (
    materialize_classification_analysis_features,
    materialize_classification_analysis_snapshot,
)
from app.classification_features import feature_specification_hash_for_job
from app.models import (
    AuditRecord,
    ClassificationDatasetSnapshot,
    ClassificationModelConfiguration,
    ClassificationModelVersion,
    MatterAnalysisTask,
    MatterAnalysisTaskRun,
    MatterDefinition,
    MatterEmbeddingJob,
)
from tests.test_agents_and_matter_definitions import auth, create_tenant_context


def _specification(task_version_id: str) -> dict:
    return {
        "schema_version": "review-decision-specification-v1",
        "questions": {
            "responsiveness.issue_one": {
                "type": "noul",
                "instructions": {
                    "question": "Does the document concern the first issue?",
                    "state_path": "document.paragraphs",
                },
                "criteria": {
                    "true": "The first issue is present.",
                    "false": "The first issue is absent.",
                },
                "source_refs": [
                    {
                        "task_version_id": task_version_id,
                        "heading": "Issue One",
                        "excerpt_hash": "b" * 64,
                    }
                ],
                "aggregation": {"operator": "ANY_WINDOW"},
                "evidence": {"required": True, "minimum_exists_probability": 0.7},
                "field_mapping": None,
            }
        },
        "decision_policy": {
            "version": "decision-policy-v1",
            "recommendations": {},
            "routes": {},
        },
        "state_contract": {
            "builder_version": "document-review-state-v1",
            "required_paths": ["document.id", "document.paragraphs"],
        },
    }


def _create_task(client: TestClient, token: str, matter_id: str) -> dict:
    response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks",
        headers=auth(token),
        json={
            "key": "first_pass_review",
            "name": "First-pass issue review",
            "description": "Apply the reviewed issue guidance.",
            "task_type": "QUESTION_ANSWERING",
            "definition_markdown": "# Issue One\nReview documents about coverage cancellation.",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_analysis_task_definition_and_specification_publish_atomically(
    client: TestClient,
    root_token: str,
    db: Session,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    task = _create_task(client, token, matter_id)
    assert task["workflow_key"] == "question_answering_v1"
    assert task["published_version"] is None
    assert task["version"]["compilation_status"] == "NOT_GENERATED"
    assert task["version"]["decision_provider"] == "JEV"
    assert task["version"]["decision_model"] is None
    assert db.scalar(select(MatterDefinition).where(MatterDefinition.matter_id == uuid.UUID(matter_id))) is None

    publish_before_ready = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/publish",
        headers=auth(token),
    )
    assert publish_before_ready.status_code == 409

    specification_response = client.put(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/specification",
        headers=auth(token),
        json={
            "decision_specification": _specification(task["version"]["id"]),
            "input_contract": {"type": "document-review-state-v1"},
            "output_contract": {"type": "decision-result-v1"},
            "evidence_policy": {"required": True},
            "routing_policy": {"low_probability": "HUMAN_REVIEW"},
            "validation_report": {"errors": [], "warnings": []},
        },
    )
    assert specification_response.status_code == 200, specification_response.text
    ready = specification_response.json()
    assert ready["version"]["compilation_status"] == "READY"
    assert len(ready["version"]["specification_content_hash"]) == 64

    publish_response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/publish",
        headers=auth(token),
    )
    assert publish_response.status_code == 200, publish_response.text
    published = publish_response.json()
    assert published["published_version"] == 1
    assert published["version"]["status"] == "PUBLISHED"
    actions = list(
        db.scalars(
            select(AuditRecord.action)
            .where(AuditRecord.target_id == uuid.UUID(task["id"]))
            .order_by(AuditRecord.created_at)
        )
    )
    assert actions == [
        "matter_analysis_task.created",
        "matter_analysis_task.specification.updated",
        "matter_analysis_task.version.published",
    ]


def test_edit_creates_new_uncompiled_version_and_rejects_stale_writes(
    client: TestClient,
    root_token: str,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    task = _create_task(client, token, matter_id)

    edit_response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions",
        headers=auth(token),
        json={
            "based_on_version": 1,
            "definition_markdown": "# Issue One\nReview cancellations and threatened non-renewals.",
        },
    )
    assert edit_response.status_code == 201, edit_response.text
    edited = edit_response.json()
    assert edited["current_version"] == 2
    assert edited["version"]["decision_specification"] is None
    assert edited["version"]["compilation_status"] == "NOT_GENERATED"

    stale_response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions",
        headers=auth(token),
        json={"based_on_version": 1, "definition_markdown": "stale edit"},
    )
    assert stale_response.status_code == 409

    versions_response = client.get(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions",
        headers=auth(token),
    )
    assert versions_response.status_code == 200
    versions = versions_response.json()
    assert [version["version"] for version in versions] == [2, 1]
    assert versions[1]["status"] == "RETIRED"


def test_question_answering_task_pins_selected_decision_provider_per_version(
    client: TestClient,
    root_token: str,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks",
        headers=auth(token),
        json={
            "key": "openai_first_pass",
            "name": "OpenAI first pass",
            "task_type": "QUESTION_ANSWERING",
            "decision_provider": "OPENAI_DECISIONS",
            "definition_markdown": "# Responsiveness\nDetermine responsiveness.",
        },
    )
    assert response.status_code == 201, response.text
    task = response.json()
    assert task["version"]["decision_provider"] == "OPENAI_DECISIONS"
    assert task["version"]["decision_model"] == "gpt-6-luna"

    revision = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions",
        headers=auth(token),
        json={
            "based_on_version": 1,
            "decision_provider": "JEV",
            "definition_markdown": "# Responsiveness\nDetermine responsiveness using revised guidance.",
        },
    )
    assert revision.status_code == 201, revision.text
    revised = revision.json()
    assert revised["version"]["decision_provider"] == "JEV"
    assert revised["version"]["decision_model"] is None


def test_specification_sources_must_reference_the_enclosing_task_version(
    client: TestClient,
    root_token: str,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    task = _create_task(client, token, matter_id)
    response = client.put(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/specification",
        headers=auth(token),
        json={"decision_specification": _specification(str(uuid.uuid4()))},
    )
    assert response.status_code == 409
    assert "source references" in response.json()["error"]["message"]


def test_analysis_task_keys_are_unique_within_a_matter(
    client: TestClient,
    root_token: str,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    _create_task(client, token, matter_id)
    duplicate = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks",
        headers=auth(token),
        json={
            "key": "first_pass_review",
            "name": "Duplicate",
            "task_type": "QUESTION_ANSWERING",
            "definition_markdown": "# Privilege",
        },
    )
    assert duplicate.status_code == 409

    tasks = db_tasks = client.get(
        f"/v1/matters/{matter_id}/analysis-tasks",
        headers=auth(token),
    )
    assert db_tasks.status_code == 200
    assert len(tasks.json()) == 1
    assert tasks.json()[0]["key"] == "first_pass_review"


def test_analysis_tasks_do_not_cross_matter_boundaries(client: TestClient, root_token: str) -> None:
    tenant_id, token, matter_id = create_tenant_context(client, root_token)
    task = _create_task(client, token, matter_id)
    second_client = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=auth(token),
        json={"name": "Second Client"},
    )
    second_matter = client.post(
        f"/v1/clients/{second_client.json()['id']}/matters",
        headers=auth(token),
        json={"name": "Second Matter"},
    )
    response = client.get(
        f"/v1/matters/{second_matter.json()['id']}/analysis-tasks/{task['id']}",
        headers=auth(token),
    )
    assert response.status_code == 404


def test_analysis_task_row_uses_code_owned_workflow_key(
    client: TestClient,
    root_token: str,
    db: Session,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    task = _create_task(client, token, matter_id)
    row = db.get(MatterAnalysisTask, uuid.UUID(task["id"]))
    assert row is not None
    assert row.workflow_key == "question_answering_v1"


def test_classification_analysis_task_pins_model_and_queues_artifact_backed_run(
    client: TestClient,
    root_token: str,
    root_admin,
    db: Session,
    monkeypatch,
) -> None:
    from test_classification_datasets import (
        add_documents,
        configure_artifact_gateway,
        read_workflow_parquet,
    )
    from test_classification_scoring import approved_version_fixture
    from test_classification_training_jobs import create_completed_embedding_job

    matter, model, _snapshot, version_id, _ = approved_version_fixture(
        client,
        root_token,
        root_admin,
    )
    documents = add_documents(matter["id"], root_admin.id, 30)
    configure_artifact_gateway(monkeypatch, documents)
    ensured_storage = client.post(
        f"/v1/tenants/{root_admin.tenant_id}/artifact-storage/ensure",
        headers=auth(root_token),
        json={"tenant_slug": "root"},
    )
    assert ensured_storage.status_code in {200, 201}, ensured_storage.text
    monkeypatch.setattr(
        "app.classification_features.load_current_chunk_vector_artifacts",
        lambda collection_item_ids, **_: {},
    )
    enabled = client.patch(
        f"/v1/matters/{matter['id']}/metadata-definitions/{model['target_metadata_definition_id']}",
        headers=auth(root_token),
        json={"ai_assignable": True},
    )
    assert enabled.status_code == 200, enabled.text
    created = client.post(
        f"/v1/matters/{matter['id']}/analysis-tasks",
        headers=auth(root_token),
        json={
            "key": "approved_classifier",
            "name": "Approved classifier",
            "task_type": "CLASSIFICATION",
            "definition_markdown": "Apply the approved classifier.",
            "destination_metadata_definition_ids": [model["target_metadata_definition_id"]],
            "classification_model_version_id": str(version_id),
        },
    )
    assert created.status_code == 201, created.text
    task = created.json()
    assert task["workflow_key"] == "classification_scoring_v1"
    assert task["version"]["compilation_status"] == "READY"
    assert task["version"]["decision_specification"] is None
    assert task["version"]["classification_model_id"] == model["id"]
    assert task["version"]["classification_model_version_id"] == str(version_id)

    published = client.post(
        f"/v1/matters/{matter['id']}/analysis-tasks/{task['id']}/versions/1/publish",
        headers=auth(root_token),
    )
    assert published.status_code == 200, published.text
    embedding_job_id = create_completed_embedding_job(
        matter_id=matter["id"],
        user_id=root_admin.id,
    )
    pinned_version = db.get(ClassificationModelVersion, version_id)
    embedding_job = db.get(MatterEmbeddingJob, embedding_job_id)
    assert pinned_version is not None and embedding_job is not None
    configuration = db.get(ClassificationModelConfiguration, pinned_version.configuration_id)
    assert configuration is not None
    embedding_job.embedding_model_revision = "revision-1"
    embedding_job.configuration = {
        "schema_version": 1,
        "embedding": {
            "model": embedding_job.embedding_model,
            "model_revision": embedding_job.embedding_model_revision,
            "dimensions": embedding_job.embedding_dimensions,
        },
    }
    pinned_version.feature_specification_hash = feature_specification_hash_for_job(
        configuration,
        embedding_job,
    )
    db.commit()
    launched = client.post(
        f"/v1/matters/{matter['id']}/analysis-tasks/{task['id']}/runs",
        headers=auth(root_token),
        json={
            "scope_type": "MATTER",
            "scope_id": matter["id"],
            "embedding_job_id": str(embedding_job_id),
            "idempotency_key": "whole-matter-run",
        },
    )
    assert launched.status_code == 202, launched.text
    run = launched.json()
    assert run["status"] == "QUEUED"
    assert run["scope_type"] == "MATTER"
    assert run["dataset_snapshot_id"] is None
    assert run["execution"]["job_type"] == "CLASSIFICATION_ANALYSIS_TASK"
    assert run["execution"]["job_id"] == run["id"]
    assert run["execution"]["attempt_number"] == 1
    assert run["execution"]["status"] == "QUEUED"
    assert run["execution"]["progress_total"] == 4
    assert run["execution"]["progress_unit"] == "stages"
    execution_response = client.get(
        f"/v1/jobs/CLASSIFICATION_ANALYSIS_TASK/{run['id']}/execution",
        headers=auth(root_token),
    )
    assert execution_response.status_code == 200, execution_response.text
    execution_history = execution_response.json()
    assert execution_history["current_execution"]["id"] == run["execution"]["id"]
    assert len(execution_history["attempts"]) == 1
    assert db.get(MatterAnalysisTaskRun, uuid.UUID(run["id"])) is not None

    repeated = client.post(
        f"/v1/matters/{matter['id']}/analysis-tasks/{task['id']}/runs",
        headers=auth(root_token),
        json={
            "scope_type": "MATTER",
            "scope_id": matter["id"],
            "embedding_job_id": str(embedding_job_id),
            "idempotency_key": "whole-matter-run",
        },
    )
    assert repeated.status_code == 202
    assert repeated.json()["id"] == run["id"]

    snapshot_id = materialize_classification_analysis_snapshot(
        uuid.UUID(run["id"]),
        session_factory=TestingSessionLocal,
    )
    assert snapshot_id is not None
    materialize_classification_analysis_features(
        uuid.UUID(run["id"]),
        snapshot_id,
        session_factory=TestingSessionLocal,
    )
    db.expire_all()
    scoring_snapshot = db.get(ClassificationDatasetSnapshot, snapshot_id)
    persisted_run = db.get(MatterAnalysisTaskRun, uuid.UUID(run["id"]))
    assert scoring_snapshot is not None and persisted_run is not None
    assert scoring_snapshot.purpose == "SCORING"
    assert scoring_snapshot.status == "READY_FOR_SCORING"
    assert scoring_snapshot.label_artifact_id is None
    assert scoring_snapshot.population_count == 30
    assert (
        scoring_snapshot.feature_ready_count
        + scoring_snapshot.feature_missing_count
        + scoring_snapshot.feature_failed_count
    ) == scoring_snapshot.population_count
    feature_rows = read_workflow_parquet(
        client,
        root_token,
        str(scoring_snapshot.feature_artifact_id),
    )
    assert len(feature_rows) == scoring_snapshot.population_count
    assert {row["feature_status"] for row in feature_rows} == {"MISSING"}
    assert persisted_run.population_document_count == 30
    assert persisted_run.status == "SCORING"
    assert persisted_run.current_workflow_run is not None
    assert persisted_run.current_workflow_run.status == "RUNNING"
    assert persisted_run.current_workflow_run.current_stage == "LAUNCHING_SCORING"
    assert persisted_run.current_workflow_run.progress_completed == 2
    assert persisted_run.current_workflow_run.last_heartbeat_at is not None
    expected_workflow_id = persisted_run.workflow_id

    canceled_workflows: list[str] = []
    monkeypatch.setattr(
        "app.routers.analysis_tasks.cancel_classification_analysis_task",
        lambda workflow_id: canceled_workflows.append(workflow_id),
    )
    canceled = client.post(
        f"/v1/matters/{matter['id']}/analysis-tasks/{task['id']}/runs/{run['id']}/cancel",
        headers=auth(root_token),
    )
    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["status"] == "CANCELED"
    assert canceled.json()["current_stage"] == "CANCELED"
    assert canceled.json()["canceled_at"] is not None
    assert canceled.json()["execution"]["status"] == "CANCELED"
    assert canceled.json()["execution"]["cancel_requested_at"] is not None
    assert canceled.json()["feature_processed_count"] == 30
    assert canceled.json()["feature_missing_count"] == 30
    assert canceled_workflows == [expected_workflow_id]
    audit = db.scalar(
        select(AuditRecord).where(
            AuditRecord.target_id == uuid.UUID(run["id"]),
            AuditRecord.action == "matter_analysis_task.classification_run.canceled",
        )
    )
    assert audit is not None

    repeated_cancel = client.post(
        f"/v1/matters/{matter['id']}/analysis-tasks/{task['id']}/runs/{run['id']}/cancel",
        headers=auth(root_token),
    )
    assert repeated_cancel.status_code == 200
    assert repeated_cancel.json()["status"] == "CANCELED"
    assert canceled_workflows == [expected_workflow_id]

    common_cancel = client.post(
        f"/v1/jobs/CLASSIFICATION_ANALYSIS_TASK/{run['id']}/cancel",
        headers=auth(root_token),
    )
    assert common_cancel.status_code == 200, common_cancel.text
    assert common_cancel.json()["status"] == "CANCELED"
    assert canceled_workflows == [expected_workflow_id]

    retried = client.post(
        f"/v1/jobs/CLASSIFICATION_ANALYSIS_TASK/{run['id']}/retry",
        headers=auth(root_token),
    )
    assert retried.status_code == 200, retried.text
    retry_execution = retried.json()
    assert retry_execution["status"] == "QUEUED"
    assert retry_execution["attempt_number"] == 2
    assert retry_execution["retry_of_workflow_run_id"] == run["execution"]["id"]
    retry_history = client.get(
        f"/v1/jobs/CLASSIFICATION_ANALYSIS_TASK/{run['id']}/execution",
        headers=auth(root_token),
    )
    assert retry_history.status_code == 200, retry_history.text
    assert [item["attempt_number"] for item in retry_history.json()["attempts"]] == [2, 1]
    db.expire_all()
    retried_run = db.get(MatterAnalysisTaskRun, uuid.UUID(run["id"]))
    assert retried_run is not None
    assert retried_run.status == "QUEUED"
    assert retried_run.current_workflow_run_id == uuid.UUID(retry_execution["id"])
    assert retried_run.canceled_at is None

    batch_response = client.post(
        f"/v1/matters/{matter['id']}/review-batches",
        headers=auth(root_token),
        json={"name": "Classifier analysis batch", "selection_type": "ALL_MATTER"},
    )
    assert batch_response.status_code == 202, batch_response.text
    batch = batch_response.json()
    assert batch["status"] == "READY"
    batch_run_response = client.post(
        f"/v1/matters/{matter['id']}/analysis-tasks/{task['id']}/runs",
        headers=auth(root_token),
        json={
            "scope_type": "REVIEW_BATCH",
            "scope_id": batch["id"],
            "embedding_job_id": str(embedding_job_id),
            "idempotency_key": "batch-classification-run",
        },
    )
    assert batch_run_response.status_code == 202, batch_run_response.text
    batch_run = batch_run_response.json()

    listed_batch_runs = client.get(
        f"/v1/matters/{matter['id']}/review-batches/{batch['id']}/analysis-runs",
        headers=auth(root_token),
    )
    assert listed_batch_runs.status_code == 200, listed_batch_runs.text
    assert listed_batch_runs.json() == [
        {
            "id": batch_run["id"],
            "job_type": "CLASSIFICATION_ANALYSIS_TASK",
            "task_type": "CLASSIFICATION",
            "matter_analysis_task_id": task["id"],
            "task_name": "Approved classifier",
            "task_version": 1,
            "status": "QUEUED",
            "created_at": batch_run["created_at"],
            "updated_at": batch_run["updated_at"],
            "execution": batch_run["execution"],
            "review_batch_run": None,
            "classification_analysis_run": batch_run,
        }
    ]


def test_analysis_task_versions_pin_validated_ai_assignable_destinations(
    client: TestClient,
    root_token: str,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    field_response = client.post(
        f"/v1/matters/{matter_id}/metadata-definitions",
        headers=auth(token),
        json={
            "key": "issue_coding",
            "display_name": "Issue coding",
            "type": "ENUM",
            "cardinality": "MULTIPLE",
            "allowed_values": [{"key": "topic_401", "label": "Olympics"}],
            "ai_assignable": False,
        },
    )
    assert field_response.status_code == 201, field_response.text
    field = field_response.json()

    unavailable = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks",
        headers=auth(token),
        json={
            "key": "issue_review",
            "name": "Issue review",
            "task_type": "QUESTION_ANSWERING",
            "definition_markdown": "# Issues\nApply every supported issue.",
            "destination_metadata_definition_ids": [field["id"]],
        },
    )
    assert unavailable.status_code == 409
    assert "AI-assignable" in unavailable.json()["error"]["message"]

    enabled_response = client.patch(
        f"/v1/matters/{matter_id}/metadata-definitions/{field['id']}",
        headers=auth(token),
        json={"ai_assignable": True},
    )
    assert enabled_response.status_code == 200, enabled_response.text
    assert enabled_response.json()["ai_assignable"] is True

    created_response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks",
        headers=auth(token),
        json={
            "key": "issue_review",
            "name": "Issue review",
            "task_type": "QUESTION_ANSWERING",
            "definition_markdown": "# Issues\nApply every supported issue.",
            "destination_metadata_definition_ids": [field["id"]],
        },
    )
    assert created_response.status_code == 201, created_response.text
    created = created_response.json()
    assert created["version"]["destination_metadata_definition_ids"] == [field["id"]]

    specification = _specification(created["version"]["id"])
    specification["questions"]["responsiveness.issue_one"]["field_mapping"] = {
        "metadata_definition_key": "responsiveness",
        "value": "responsive",
        "uncertainty": {
            "kind": "DERIVED_PROBABILITY",
            "source": "MAPPED_BOOLEAN_PROBABILITY",
        },
    }
    unselected_mapping = client.put(
        f"/v1/matters/{matter_id}/analysis-tasks/{created['id']}/versions/1/specification",
        headers=auth(token),
        json={"decision_specification": specification},
    )
    assert unselected_mapping.status_code == 409
    assert "outside this task version's destinations" in unselected_mapping.json()["error"]["message"]

    revision_response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{created['id']}/versions",
        headers=auth(token),
        json={
            "based_on_version": 1,
            "definition_markdown": "# Issues\nApply every supported issue using the clarified definitions.",
        },
    )
    assert revision_response.status_code == 201, revision_response.text
    assert revision_response.json()["version"]["destination_metadata_definition_ids"] == [field["id"]]


def test_matter_definition_remains_separate_from_question_answering_tasks(
    client: TestClient,
    root_token: str,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    first = client.post(
        f"/v1/matters/{matter_id}/definition/revisions",
        headers=auth(token),
        json={
            "content_markdown": "# Responsiveness\n\nReview documents against the matter-wide issues.",
            "source_kind": "PASTE",
        },
    )
    assert first.status_code == 201, first.text

    tasks = client.get(f"/v1/matters/{matter_id}/analysis-tasks", headers=auth(token))
    assert tasks.status_code == 200, tasks.text
    assert tasks.json() == []

    unsupported = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks",
        headers=auth(token),
        json={
            "key": "matter_definition",
            "name": "Matter Definition",
            "task_type": "MATTER_DEFINITION",
            "definition_markdown": "# Responsiveness\n\nReview the matter-wide issues.",
        },
    )
    assert unsupported.status_code == 422
