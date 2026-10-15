import hashlib
import io
import json
import uuid
from datetime import datetime, timedelta, timezone

import pyarrow as pa
import pyarrow.parquet as pq
from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_classification_builds import FakeClassificationGateway, launch_build, ready_build_fixture
from test_classification_lifecycle import approve, promote, promotion_preview, second_candidate
from test_classification_models import auth

from app.classification_builds import execute_training_run
from app.classification_projections import execute_classifier_projection
from app.classification_scoring import execute_scoring_run
from app.models import (
    ClassificationModel,
    ClassificationModelConfiguration,
    ClassificationScoringRun,
    Matter,
    MatterAnalysisTask,
    MatterAnalysisTaskRun,
    MatterAnalysisTaskVersion,
    MatterDocument,
    MatterDocumentImportJob,
    ReviewBatch,
    ReviewBatchCodingField,
    ReviewBatchCodingGroup,
    ReviewBatchRun,
    ReviewBatchRunDocument,
    ReviewBatchRunValue,
    ReviewBatchSearchCodingRun,
    WorkflowRun,
)
from classification_service.schemas import ArtifactReference, ArtifactType, ClassificationScoringResult


class FakeScoringGateway:
    def __init__(self, training_gateway: FakeClassificationGateway) -> None:
        assert training_gateway.validation is not None
        self.validation = training_gateway.validation
        self.scored_model_version_id: uuid.UUID | None = None
        self.manifest: dict | None = None

    @staticmethod
    def reference(artifact_type: ArtifactType, digit: str) -> ArtifactReference:
        return ArtifactReference(
            artifact_id=uuid.uuid4(),
            artifact_type=artifact_type,
            schema_version=1,
            content_hash=digit * 64,
            media_type=(
                "application/vnd.apache.parquet"
                if artifact_type == ArtifactType.CLASSIFICATION_PREDICTIONS
                else "application/json"
            ),
        )

    def validate_package(self, reference, access):
        del reference, access
        return self.validation

    def score(self, request, access) -> ClassificationScoringResult:
        del access
        self.scored_model_version_id = request.model_version_id
        predictions = self.reference(ArtifactType.CLASSIFICATION_PREDICTIONS, "b")
        manifest = self.reference(ArtifactType.CLASSIFICATION_RESULT_MANIFEST, "c")
        self.manifest = {
            "status": "COMPLETED_WITH_ERRORS",
            "scoring_run_id": str(request.scoring_run_id),
            "output_derivation_key": request.output_derivation_key,
            "model_version_id": str(request.model_version_id),
            "configuration_hash": request.configuration_hash,
            "feature_specification_hash": "4" * 64,
            "input_artifact_hashes": {
                "model": request.model_artifact.content_hash,
                "features": request.feature_artifact.content_hash,
            },
            "prediction_artifact": predictions.model_dump(mode="json"),
            "population_document_count": 30,
            "scored_document_count": 28,
            "abstained_document_count": 2,
            "excluded_document_count": 1,
            "failed_document_count": 1,
        }
        return ClassificationScoringResult(
            scoring_run_id=request.scoring_run_id,
            output_derivation_key=request.output_derivation_key,
            prediction_artifact=predictions,
            result_manifest_artifact=manifest,
            population_document_count=30,
            scored_document_count=28,
            abstained_document_count=2,
            excluded_document_count=1,
            failed_document_count=1,
        )

    def verify(self, result, access) -> dict:
        del result, access
        assert self.manifest is not None
        return self.manifest


def approved_version_fixture(
    client: TestClient,
    token: str,
    root_admin,
) -> tuple[dict, dict, object, uuid.UUID, FakeClassificationGateway]:
    matter, model, snapshot = ready_build_fixture(
        client,
        token,
        str(root_admin.tenant_id),
        root_admin.id,
        suffix="Scoring",
    )
    launched = launch_build(client, token, matter, model, snapshot.id)
    training_gateway = FakeClassificationGateway()
    version_id = execute_training_run(
        uuid.UUID(launched["training_run"]["id"]),
        session_factory=TestingSessionLocal,
        gateway=training_gateway,
        published_result_verifier=training_gateway.verify_published_result,
    )
    assert version_id is not None
    approve(client, token, matter["id"], model["id"], version_id)
    return matter, model, snapshot, version_id, training_gateway


def test_scoring_pins_previewed_version_across_activation_change(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter, model, snapshot, first_version_id, training_gateway = approved_version_fixture(
        client,
        root_token,
        root_admin,
    )
    promote(
        client,
        root_token,
        matter["id"],
        model["id"],
        promotion_preview(client, root_token, matter["id"], model["id"], first_version_id),
    )
    base = f"/v1/matters/{matter['id']}/classification-models/{model['id']}/scoring-runs"
    preview_response = client.post(
        f"{base}/preview",
        headers=auth(root_token),
        json={"dataset_snapshot_id": str(snapshot.id)},
    )
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["can_launch"] is True
    assert preview["model_version_id"] == str(first_version_id)

    launch_response = client.post(
        base,
        headers=auth(root_token),
        json={
            "dataset_snapshot_id": str(snapshot.id),
            "model_version_id": preview["model_version_id"],
            "idempotency_key": "score-previewed-version",
        },
    )
    assert launch_response.status_code == 202, launch_response.text
    launched = launch_response.json()
    assert launched["created"] is True
    assert launched["scoring_run"]["model_version_id"] == str(first_version_id)

    second_version_id = second_candidate(client, root_token, matter, model, first_version_id)
    approve(client, root_token, matter["id"], model["id"], second_version_id)
    promote(
        client,
        root_token,
        matter["id"],
        model["id"],
        promotion_preview(client, root_token, matter["id"], model["id"], second_version_id),
    )
    with TestingSessionLocal() as db:
        registry = db.get(ClassificationModel, uuid.UUID(model["id"]))
        assert registry is not None and registry.active_model_version_id == second_version_id

    historical_preview = client.post(
        f"{base}/preview",
        headers=auth(root_token),
        json={
            "dataset_snapshot_id": str(snapshot.id),
            "model_version_id": str(first_version_id),
        },
    )
    assert historical_preview.status_code == 200
    assert historical_preview.json()["can_launch"] is True

    scoring_gateway = FakeScoringGateway(training_gateway)
    run_id = uuid.UUID(launched["scoring_run"]["id"])
    execute_scoring_run(
        run_id,
        session_factory=TestingSessionLocal,
        gateway=scoring_gateway,
        published_result_verifier=scoring_gateway.verify,
    )
    assert scoring_gateway.scored_model_version_id == first_version_id
    with TestingSessionLocal() as db:
        run = db.get(ClassificationScoringRun, run_id)
        assert run is not None
        assert run.status == "COMPLETED_WITH_ERRORS"
        assert run.population_document_count == 30
        assert run.scored_document_count == 28
        assert run.excluded_document_count == 1
        assert run.failed_document_count == 1

    read = client.get(f"{base}/{run_id}", headers=auth(root_token))
    assert read.status_code == 200
    assert read.json()["model_version_id"] == str(first_version_id)
    assert read.json()["prediction_artifact_id"] is not None


def test_scoring_launch_is_idempotent_and_cancelable(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter, model, snapshot, version_id, _ = approved_version_fixture(client, root_token, root_admin)
    base = f"/v1/matters/{matter['id']}/classification-models/{model['id']}/scoring-runs"
    payload = {
        "dataset_snapshot_id": str(snapshot.id),
        "model_version_id": str(version_id),
        "idempotency_key": "idempotent-score",
    }
    launched = client.post(base, headers=auth(root_token), json=payload)
    assert launched.status_code == 202, launched.text
    assert launched.json()["scoring_run"]["execution"]["job_type"] == "CLASSIFICATION_SCORING_RUN"
    assert launched.json()["scoring_run"]["execution"]["available_actions"] == ["CANCEL"]
    repeated = client.post(base, headers=auth(root_token), json=payload)
    assert repeated.status_code == 202
    assert repeated.json()["created"] is False
    assert repeated.json()["scoring_run"]["id"] == launched.json()["scoring_run"]["id"]

    run_id = launched.json()["scoring_run"]["id"]
    retirement_path = (
        f"/v1/matters/{matter['id']}/classification-models/{model['id']}"
        f"/versions/{version_id}/retire"
    )
    blocked_retirement = client.post(
        retirement_path,
        headers=auth(root_token),
        json={"reason": "Attempt retirement while scoring is queued."},
    )
    assert blocked_retirement.status_code == 409

    canceled = client.post(f"{base}/{run_id}/cancel", headers=auth(root_token))
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "CANCELED"
    assert canceled.json()["execution"]["status"] == "CANCELED"
    assert canceled.json()["execution"]["available_actions"] == ["RETRY"]
    retried = client.post(
        f"/v1/jobs/CLASSIFICATION_SCORING_RUN/{run_id}/retry",
        headers=auth(root_token),
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "QUEUED"
    assert retried.json()["attempt_number"] == 2
    canceled_again = client.post(f"{base}/{run_id}/cancel", headers=auth(root_token))
    assert canceled_again.status_code == 200
    assert canceled_again.json()["execution"]["attempt_number"] == 2
    retired = client.post(
        retirement_path,
        headers=auth(root_token),
        json={"reason": "No nonterminal scoring references remain."},
    )
    assert retired.status_code == 201
    assert retired.json()["policy_snapshot"]["blocking_reference_count"] == 0


def test_scoring_retry_backfills_a_missing_legacy_execution(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter, model, snapshot, version_id, _ = approved_version_fixture(client, root_token, root_admin)
    base = f"/v1/matters/{matter['id']}/classification-models/{model['id']}/scoring-runs"
    launched = client.post(
        base,
        headers=auth(root_token),
        json={
            "dataset_snapshot_id": str(snapshot.id),
            "model_version_id": str(version_id),
            "idempotency_key": "legacy-score-without-execution",
        },
    )
    assert launched.status_code == 202, launched.text
    run_id = uuid.UUID(launched.json()["scoring_run"]["id"])
    with TestingSessionLocal() as db:
        run = db.get(ClassificationScoringRun, run_id)
        assert run is not None and run.current_workflow_run_id is not None
        execution = db.get(WorkflowRun, run.current_workflow_run_id)
        assert execution is not None
        run.current_workflow_run_id = None
        run.current_workflow_run = None
        db.flush()
        db.delete(execution)
        run.status = "FAILED"
        run.current_stage = "FAILED"
        run.failure_code = "CLASSIFICATION_SCORING_FAILED"
        run.failure_summary = "Legacy scoring failure"
        run.failure_retriable = True
        db.commit()

    retried = client.post(
        f"/v1/jobs/CLASSIFICATION_SCORING_RUN/{run_id}/retry",
        headers=auth(root_token),
    )

    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "QUEUED"
    assert retried.json()["attempt_number"] == 2
    with TestingSessionLocal() as db:
        attempts = list(
            db.scalars(
                select(WorkflowRun)
                .where(
                    WorkflowRun.job_type == "CLASSIFICATION_SCORING_RUN",
                    WorkflowRun.job_id == run_id,
                )
                .order_by(WorkflowRun.attempt_number)
            )
        )
        assert [attempt.status for attempt in attempts] == ["FAILED", "QUEUED"]


def test_scoring_retry_normalizes_a_stale_nonterminal_execution(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter, model, snapshot, version_id, _ = approved_version_fixture(client, root_token, root_admin)
    base = f"/v1/matters/{matter['id']}/classification-models/{model['id']}/scoring-runs"
    launched = client.post(
        base,
        headers=auth(root_token),
        json={
            "dataset_snapshot_id": str(snapshot.id),
            "model_version_id": str(version_id),
            "idempotency_key": "score-with-stale-execution-state",
        },
    )
    assert launched.status_code == 202, launched.text
    run_id = uuid.UUID(launched.json()["scoring_run"]["id"])
    with TestingSessionLocal() as db:
        run = db.get(ClassificationScoringRun, run_id)
        assert run is not None and run.current_workflow_run_id is not None
        execution = db.get(WorkflowRun, run.current_workflow_run_id)
        assert execution is not None and execution.status == "QUEUED"
        run.status = "FAILED"
        run.current_stage = "FAILED"
        run.progress_completed = 1
        run.failure_code = "CLASSIFICATION_SCORING_FAILED"
        run.failure_summary = "Runtime exhausted its retries"
        run.failure_retriable = True
        db.commit()

    retried = client.post(
        f"/v1/jobs/CLASSIFICATION_SCORING_RUN/{run_id}/retry",
        headers=auth(root_token),
    )

    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "QUEUED"
    assert retried.json()["attempt_number"] == 2
    with TestingSessionLocal() as db:
        attempts = list(
            db.scalars(
                select(WorkflowRun)
                .where(
                    WorkflowRun.job_type == "CLASSIFICATION_SCORING_RUN",
                    WorkflowRun.job_id == run_id,
                )
                .order_by(WorkflowRun.attempt_number)
            )
        )
        assert [attempt.status for attempt in attempts] == ["FAILED", "QUEUED"]
        assert attempts[0].failure_summary == "Runtime exhausted its retries"


def test_scoring_preview_blocks_incompatible_feature_contract(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter, model, snapshot, version_id, _ = approved_version_fixture(client, root_token, root_admin)
    with TestingSessionLocal() as db:
        persisted = db.get(type(snapshot), snapshot.id)
        assert persisted is not None
        persisted.feature_specification_hash = "f" * 64
        db.commit()

    response = client.post(
        f"/v1/matters/{matter['id']}/classification-models/{model['id']}/scoring-runs/preview",
        headers=auth(root_token),
        json={
            "dataset_snapshot_id": str(snapshot.id),
            "model_version_id": str(version_id),
        },
    )
    assert response.status_code == 200
    assert response.json()["can_launch"] is False
    assert {issue["code"] for issue in response.json()["blocking_errors"]} == {
        "FEATURE_CONTRACT_MISMATCH"
    }


def test_completed_scoring_run_exposes_bounded_verified_prediction_preview(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch,
) -> None:
    matter, model, snapshot, version_id, _ = approved_version_fixture(client, root_token, root_admin)
    base = f"/v1/matters/{matter['id']}/classification-models/{model['id']}/scoring-runs"
    launched = client.post(
        base,
        headers=auth(root_token),
        json={
            "dataset_snapshot_id": str(snapshot.id),
            "model_version_id": str(version_id),
            "idempotency_key": "prediction-preview",
        },
    )
    assert launched.status_code == 202, launched.text
    run_id = uuid.UUID(launched.json()["scoring_run"]["id"])
    first_document_id = uuid.uuid4()
    second_document_id = uuid.uuid4()
    table = pa.Table.from_pylist(
        [
            {
                "matter_document_id": str(first_document_id),
                "collection_item_id": str(uuid.uuid4()),
                "scope_ordinal": 1,
                "source_vector_content_hash": "a" * 64,
                "result_status": "SUCCESS",
                "failure_reason": None,
                "probabilities": [0.2, 0.8],
                "predicted_class_keys": ["true"],
                "abstained": False,
                "probability_margin": 0.6,
                "uncertainty": 0.2,
            },
            {
                "matter_document_id": str(second_document_id),
                "collection_item_id": None,
                "scope_ordinal": 2,
                "source_vector_content_hash": None,
                "result_status": "EXCLUDED",
                "failure_reason": "FEATURE_MISSING",
                "probabilities": [],
                "predicted_class_keys": [],
                "abstained": False,
                "probability_margin": None,
                "uncertainty": None,
            },
        ]
    ).replace_schema_metadata(
        {
            b"pvr.schema": b"classification-scoring-predictions",
            b"pvr.scoring_run_id": str(run_id).encode(),
            b"pvr.model_version_id": str(version_id).encode(),
            b"pvr.ordered_class_keys": json.dumps(["false", "true"]).encode(),
        }
    )
    prediction_file = io.BytesIO()
    pq.write_table(table, prediction_file, row_group_size=1)
    prediction_content = prediction_file.getvalue()
    with TestingSessionLocal() as db:
        run = db.get(ClassificationScoringRun, run_id)
        assert run is not None
        run.status = "COMPLETED_WITH_ERRORS"
        run.current_stage = "COMPLETED"
        run.progress_completed = run.progress_total
        run.population_document_count = 2
        run.scored_document_count = 1
        run.abstained_document_count = 0
        run.excluded_document_count = 1
        run.failed_document_count = 0
        run.prediction_artifact_id = uuid.uuid4()
        run.prediction_content_hash = hashlib.sha256(prediction_content).hexdigest()
        run.result_manifest_artifact_id = uuid.uuid4()
        run.result_manifest_content_hash = "f" * 64
        run.completed_at = datetime.now(timezone.utc)
        db.commit()

    def copy_prediction(**kwargs) -> int:
        kwargs["destination"].write(prediction_content)
        return len(prediction_content)

    monkeypatch.setattr("app.classification_scoring.copy_workflow_artifact_content", copy_prediction)
    monkeypatch.setattr("app.classification_scoring.find_workflow_artifact_reference", lambda **_: None)
    response = client.get(f"{base}/{run_id}/predictions?offset=1&limit=1", headers=auth(root_token))
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["scoring_run_id"] == str(run_id)
    assert payload["ordered_class_keys"] == ["false", "true"]
    assert payload["total"] == 2
    assert payload["offset"] == 1
    assert payload["limit"] == 1
    assert payload["items"] == [
        {
            "matter_document_id": str(second_document_id),
            "collection_item_id": None,
            "scope_ordinal": 2,
            "source_vector_content_hash": None,
            "result_status": "EXCLUDED",
            "failure_reason": "FEATURE_MISSING",
            "probabilities": [],
            "predicted_class_keys": [],
            "abstained": False,
            "probability_margin": None,
            "uncertainty": None,
        }
    ]

    summary_response = client.get(f"{base}/{run_id}/prediction-summary", headers=auth(root_token))
    assert summary_response.status_code == 200, summary_response.text
    summary = summary_response.json()
    assert summary["minimum_probability"] is None
    assert summary["total_document_count"] == 2
    assert summary["evaluated_document_count"] == 1
    assert summary["predicted_document_count"] == 1
    assert summary["no_prediction_document_count"] == 0
    assert summary["predicted_value_count"] == 1
    assert summary["abstained_document_count"] == 0
    assert summary["class_counts"] == [
        {"class_key": "false", "label": "false", "document_count": 0},
        {"class_key": "true", "label": "true", "document_count": 1},
    ]

    threshold_response = client.get(
        f"{base}/{run_id}/prediction-summary?minimum_probability=0.81",
        headers=auth(root_token),
    )
    assert threshold_response.status_code == 200, threshold_response.text
    threshold_summary = threshold_response.json()
    assert threshold_summary["minimum_probability"] == 0.81
    assert threshold_summary["evaluated_document_count"] == 1
    assert threshold_summary["predicted_document_count"] == 0
    assert threshold_summary["no_prediction_document_count"] == 1
    assert threshold_summary["predicted_value_count"] == 0
    assert threshold_summary["class_counts"] == [
        {"class_key": "false", "label": "false", "document_count": 0},
        {"class_key": "true", "label": "true", "document_count": 0},
    ]


def test_completed_scoring_artifact_projects_one_confidence_window_into_batch_coding(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch,
) -> None:
    matter, model, snapshot, version_id, _ = approved_version_fixture(client, root_token, root_admin)
    with TestingSessionLocal() as db:
        configuration = db.get(ClassificationModelConfiguration, snapshot.configuration_id)
        assert configuration is not None
        configuration.binary_mapping = {
            "negative_class_key": "negative",
            "positive_class_key": "positive",
            "negative_value_keys": ["not_responsive"],
            "positive_value_keys": ["responsive"],
        }
        import_job = MatterDocumentImportJob(
            matter_id=uuid.UUID(matter["id"]),
            source_collection_id=uuid.uuid4(),
            selection_type="EXPLICIT",
            selection={},
            selection_summary="Classifier projection test",
            status="COMPLETED",
            workflow_id=f"test:{uuid.uuid4()}",
            created_by_user_id=root_admin.id,
        )
        db.add(import_job)
        db.flush()
        documents = [
            MatterDocument(
                matter_id=uuid.UUID(matter["id"]),
                source_collection_id=import_job.source_collection_id,
                collection_item_id=uuid.uuid4(),
                added_by_import_job_id=import_job.id,
            )
            for _ in range(2)
        ]
        db.add_all(documents)
        db.commit()
        document_ids = [document.id for document in documents]

    batch_response = client.post(
        f"/v1/matters/{matter['id']}/review-batches",
        headers=auth(root_token),
        json={"name": "All matter classifier review", "selection_type": "ALL_MATTER"},
    )
    assert batch_response.status_code == 202, batch_response.text
    batch_id = uuid.UUID(batch_response.json()["id"])
    with TestingSessionLocal() as db:
        batch = db.get(ReviewBatch, batch_id)
        registry = db.get(ClassificationModel, uuid.UUID(model["id"]))
        assert batch is not None and registry is not None
        batch.search_status = "READY"
        group = ReviewBatchCodingGroup(
            review_batch_id=batch.id,
            source_metadata_group_id=None,
            display_name="Classifier review",
            description=None,
            sort_order=0,
        )
        db.add(group)
        db.flush()
        db.add(
            ReviewBatchCodingField(
                review_batch_coding_group_id=group.id,
                metadata_definition_id=registry.target_metadata_definition_id,
                sort_order=0,
                definition_snapshot={
                    "key": registry.target_metadata_definition.key,
                    "display_name": registry.target_metadata_definition.display_name,
                    "type": registry.target_metadata_definition.type,
                    "cardinality": registry.target_metadata_definition.cardinality,
                    "allowed_values": registry.target_metadata_definition.allowed_values,
                },
            )
        )
        db.commit()

    scoring_base = f"/v1/matters/{matter['id']}/classification-models/{model['id']}/scoring-runs"
    launched = client.post(
        scoring_base,
        headers=auth(root_token),
        json={
            "dataset_snapshot_id": str(snapshot.id),
            "model_version_id": str(version_id),
            "idempotency_key": "projection-score",
        },
    )
    assert launched.status_code == 202, launched.text
    scoring_run_id = uuid.UUID(launched.json()["scoring_run"]["id"])
    table = pa.Table.from_pylist(
        [
            {
                "matter_document_id": str(document_ids[0]),
                "result_status": "SUCCESS",
                "failure_reason": None,
                "probabilities": [0.08, 0.92],
                "predicted_class_keys": ["positive"],
                "abstained": False,
            },
            {
                "matter_document_id": str(document_ids[1]),
                "result_status": "SUCCESS",
                "failure_reason": None,
                "probabilities": [0.15, 0.85],
                "predicted_class_keys": ["positive"],
                "abstained": False,
            },
        ]
    ).replace_schema_metadata(
        {
            b"pvr.schema": b"classification-scoring-predictions",
            b"pvr.scoring_run_id": str(scoring_run_id).encode(),
            b"pvr.model_version_id": str(version_id).encode(),
            b"pvr.ordered_class_keys": json.dumps(["negative", "positive"]).encode(),
        }
    )
    prediction_file = io.BytesIO()
    pq.write_table(table, prediction_file, row_group_size=1)
    prediction_content = prediction_file.getvalue()
    with TestingSessionLocal() as db:
        scoring_run = db.get(ClassificationScoringRun, scoring_run_id)
        assert scoring_run is not None
        scoring_run.status = "COMPLETED"
        scoring_run.current_stage = "COMPLETED"
        scoring_run.progress_completed = scoring_run.progress_total
        scoring_run.population_document_count = 2
        scoring_run.scored_document_count = 2
        scoring_run.abstained_document_count = 0
        scoring_run.excluded_document_count = 0
        scoring_run.failed_document_count = 0
        scoring_run.prediction_artifact_id = uuid.uuid4()
        scoring_run.prediction_content_hash = hashlib.sha256(prediction_content).hexdigest()
        scoring_run.result_manifest_artifact_id = uuid.uuid4()
        scoring_run.result_manifest_content_hash = "f" * 64
        scoring_run.completed_at = datetime.now(timezone.utc)
        db.commit()

    def copy_prediction(**kwargs) -> int:
        kwargs["destination"].write(prediction_content)
        return len(prediction_content)

    monkeypatch.setattr("app.classification_projections.copy_workflow_artifact_content", copy_prediction)
    projection_response = client.post(
        f"/v1/matters/{matter['id']}/review-batches/{batch_id}/classifier-runs",
        headers=auth(root_token),
        json={
            "classification_scoring_run_id": str(scoring_run_id),
            "minimum_confidence": 0.9,
            "maximum_confidence": 1.0,
            "selected_class_keys": ["positive"],
            "idempotency_key": "positive-90-100",
        },
    )
    assert projection_response.status_code == 202, projection_response.text
    projection_run_id = uuid.UUID(projection_response.json()["id"])
    assert projection_response.json()["execution"]["job_type"] == "CLASSIFICATION_PROJECTION"
    with TestingSessionLocal() as db:
        execute_classifier_projection(db, projection_run_id)

    with TestingSessionLocal() as db:
        run = db.get(ReviewBatchRun, projection_run_id)
        assert run is not None and run.status == "COMPLETED"
        assert run.configuration_snapshot["contract_version"] == 2
        assert run.current_workflow_run is not None
        assert run.current_workflow_run.status == "COMPLETED"
        assert run.result_summary is not None
        assert run.result_summary["projected_document_count"] == 1
        assert run.result_summary["no_value_document_count"] == 1
        assert run.result_summary["below_window_count"] == 1
        values = list(
            db.query(ReviewBatchRunValue)
            .filter(ReviewBatchRunValue.review_batch_run_id == projection_run_id)
            .all()
        )
        assert len(values) == 1
        assert values[0].matter_document_id == document_ids[0]
        assert values[0].value_text == "responsive"
        assert values[0].confidence == 0.92
        statuses = {
            row.matter_document_id: row.status
            for row in db.query(ReviewBatchRunDocument)
            .filter(ReviewBatchRunDocument.review_batch_run_id == projection_run_id)
            .all()
        }
        assert statuses == {document_ids[0]: "COMPLETED", document_ids[1]: "COMPLETED"}
        selection = db.get(ReviewBatchSearchCodingRun, batch_id)
        assert selection is not None
        assert selection.review_batch_run_id == projection_run_id
        assert selection.status == "QUEUED"
        legacy_projection = ReviewBatchRun(
            review_batch_id=batch_id,
            run_type="CLASSIFIER",
            purpose="CANDIDATE",
            status="COMPLETED",
            result_policy="ISOLATED",
            classification_scoring_run_id=scoring_run_id,
            derivation_key="1" * 64,
            idempotency_key="legacy-positive-90-100",
            configuration_snapshot={**run.configuration_snapshot, "contract_version": 1},
            result_summary={"projected_document_count": 1, "projected_value_count": 1},
            initiated_by_user_id=root_admin.id,
            dbos_workflow_id=f"legacy-classification-projection:{uuid.uuid4()}",
            processed_document_count=2,
            created_at=run.created_at - timedelta(days=1),
            updated_at=run.updated_at - timedelta(days=1),
        )
        db.add(legacy_projection)
        db.flush()
        db.add(
            ReviewBatchRunValue(
                review_batch_run_id=legacy_projection.id,
                matter_document_id=document_ids[0],
                metadata_definition_id=values[0].metadata_definition_id,
                value_ordinal=0,
                value_text="responsive",
                confidence=0.92,
                confidence_kind="DERIVED_PROBABILITY",
            )
        )
        db.commit()
        legacy_projection_id = legacy_projection.id

    comparison_options = client.get(
        f"/v1/matters/{matter['id']}/review-batches/{batch_id}/comparison-options",
        headers=auth(root_token),
    )
    assert comparison_options.status_code == 200, comparison_options.text
    classifier_source_ids = {
        uuid.UUID(source["id"])
        for source in comparison_options.json()["sources"]
        if source["run_type"] == "CLASSIFIER"
    }
    assert classifier_source_ids == {projection_run_id}
    assert legacy_projection_id not in classifier_source_ids

    from test_classification_training_jobs import create_completed_embedding_job

    embedding_job_id = create_completed_embedding_job(matter_id=matter["id"], user_id=root_admin.id)
    with TestingSessionLocal() as db:
        matter_row = db.get(Matter, uuid.UUID(matter["id"]))
        assert matter_row is not None
        task = MatterAnalysisTask(
            matter_id=matter_row.id,
            key="derived_classifier_review",
            name="Derived classifier review",
            description=None,
            task_type="CLASSIFICATION",
            workflow_key="classification_scoring_v1",
            current_version=1,
            published_version=1,
            status="ACTIVE",
            created_by_user_id=root_admin.id,
        )
        db.add(task)
        db.flush()
        task_version = MatterAnalysisTaskVersion(
            matter_analysis_task_id=task.id,
            version=1,
            status="PUBLISHED",
            compilation_status="READY",
            definition_markdown="Apply the approved classifier.",
            definition_content_hash="a" * 64,
            destination_metadata_definition_ids=[model["target_metadata_definition_id"]],
            decision_provider="JEV",
            classification_model_id=uuid.UUID(model["id"]),
            classification_model_version_id=version_id,
            input_contract={},
            output_contract={},
            evidence_policy={},
            routing_policy={},
            compiler_model_configuration={},
            validation_report={},
            source_provenance={},
            created_by_user_id=root_admin.id,
            published_by_user_id=root_admin.id,
            published_at=datetime.now(timezone.utc),
        )
        db.add(task_version)
        db.flush()
        analysis_run = MatterAnalysisTaskRun(
            tenant_id=matter_row.client.tenant_id,
            matter_id=matter_row.id,
            matter_analysis_task_id=task.id,
            matter_analysis_task_version_id=task_version.id,
            scope_type="MATTER",
            scope_id=matter_row.id,
            classification_model_id=uuid.UUID(model["id"]),
            classification_model_version_id=version_id,
            embedding_job_id=embedding_job_id,
            dataset_snapshot_id=snapshot.id,
            classification_scoring_run_id=scoring_run_id,
            idempotency_key="completed-whole-matter-analysis",
            workflow_id=f"classification-analysis:{uuid.uuid4()}",
            status="COMPLETED",
            current_stage="COMPLETED",
            progress_completed=4,
            progress_total=4,
            population_document_count=2,
            created_by_user_id=root_admin.id,
            completed_at=datetime.now(timezone.utc),
        )
        db.add(analysis_run)
        db.commit()
        analysis_run_id = analysis_run.id

    derived_response = client.post(
        f"/v1/matters/{matter['id']}/review-batches/from-classification-run",
        headers=auth(root_token),
        json={
            "classification_analysis_run_id": str(analysis_run_id),
            "name": "Positive predictions at 90%",
            "minimum_confidence": 0.9,
            "maximum_confidence": 1.0,
            "selected_class_keys": ["positive"],
            "idempotency_key": "derived-positive-90-100",
        },
    )
    assert derived_response.status_code == 202, derived_response.text
    derived = derived_response.json()
    assert derived["selection_type"] == "CLASSIFICATION_PREDICTIONS"
    assert derived["document_count"] == 1
    assert derived["status"] == "READY"
    assert derived["searchable_coding_status"] == "QUEUED"
    derived_batch_id = uuid.UUID(derived["id"])
    with TestingSessionLocal() as db:
        derived_batch = db.get(ReviewBatch, derived_batch_id)
        assert derived_batch is not None
        derived_projection_id = uuid.UUID(
            derived_batch.selection_definition["classification_projection_run_id"]
        )
        derived_projection = db.get(ReviewBatchRun, derived_projection_id)
        assert derived_projection is not None
        assert derived_projection.status == "COMPLETED"
        derived_documents = list(
            db.query(ReviewBatchRunDocument)
            .filter(ReviewBatchRunDocument.review_batch_run_id == derived_projection.id)
            .all()
        )
        assert [row.matter_document_id for row in derived_documents] == [document_ids[0]]
