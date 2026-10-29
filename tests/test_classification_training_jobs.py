import uuid

from conftest import TestingSessionLocal, create_client_tenant
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from test_classification_models import (
    auth,
    binary_configuration,
    create_enum_field,
    create_matter,
    create_model,
)

from app.classification_features import ClassificationFeatureSplitError
from app.classification_training_jobs import training_job_failure_details
from app.models import MatterEmbeddingJob


def create_completed_embedding_job(*, matter_id: str, user_id: uuid.UUID) -> uuid.UUID:
    with TestingSessionLocal() as db:
        job = MatterEmbeddingJob(
            matter_id=uuid.UUID(matter_id),
            status="COMPLETED",
            workflow_id=f"embedding:{uuid.uuid4()}",
            configuration_hash="a" * 64,
            configuration={"schema_version": 1},
            embedding_model="voyageai/voyage-4-nano",
            embedding_model_revision="1",
            embedding_dimensions=1024,
            embedding_normalized=True,
            total_count=10,
            batch_count=1,
            processed_count=10,
            embedded_count=10,
            skipped_count=0,
            failed_count=0,
            chunk_count=10,
            created_by_user_id=user_id,
        )
        db.add(job)
        db.commit()
        return job.id


def test_training_job_failure_details_unwraps_step_retry_errors() -> None:
    wrapper = RuntimeError("step retries exhausted")
    wrapper.errors = [
        RuntimeError("temporary failure"),
        ClassificationFeatureSplitError(
            "Class rare cannot support every fold",
            code="INSUFFICIENT_CLASS_SUPPORT_PER_FOLD",
        ),
    ]

    assert training_job_failure_details(wrapper) == (
        "INSUFFICIENT_CLASS_SUPPORT_PER_FOLD",
        False,
        "Class rare cannot support every fold",
    )


def test_training_job_failure_details_marks_database_operational_errors_retriable() -> None:
    failure = OperationalError("SELECT 1", {}, RuntimeError("shared memory exhausted"))

    code, retriable, summary = training_job_failure_details(failure)

    assert code == "e3q8"
    assert retriable is True
    assert "shared memory exhausted" in summary


def test_training_job_is_created_before_dataset_preparation(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter = create_matter(client, root_token, create_client_tenant(client, root_token), "Durable training")
    definition = create_enum_field(client, root_token, matter["id"], key="durable_training_target")
    model = create_model(client, root_token, matter["id"], definition["id"])
    configuration_response = client.post(
        f"/v1/matters/{matter['id']}/classification-models/{model['id']}/configurations",
        headers=auth(root_token),
        json=binary_configuration(),
    )
    assert configuration_response.status_code == 201, configuration_response.text
    configuration = configuration_response.json()
    embedding_job_id = create_completed_embedding_job(
        matter_id=matter["id"],
        user_id=root_admin.id,
    )
    endpoint = f"/v1/matters/{matter['id']}/classification-models/{model['id']}/training-jobs"
    payload = {
        "configuration_id": configuration["id"],
        "training_scope": {"scope_type": "MATTER", "scope_id": matter["id"]},
        "embedding_job_id": str(embedding_job_id),
        "validation_policy": {
            "fold_count": 5,
            "minimum_labeled_count": 20,
            "minimum_labeled_count_per_class": 5,
        },
        "random_seed": 42,
        "idempotency_key": "durable-launch",
    }

    created = client.post(endpoint, headers=auth(root_token), json=payload)
    assert created.status_code == 202, created.text
    body = created.json()
    assert body["created"] is True
    assert body["training_job"]["status"] == "QUEUED"
    assert body["training_job"]["dataset_snapshot"] is None
    assert body["training_job"]["training_run"] is None
    assert body["training_job"]["progress_completed"] == 0
    assert body["training_job"]["progress_total"] == 4
    assert body["training_job"]["execution"]["job_type"] == "CLASSIFICATION_TRAINING_JOB"
    assert body["training_job"]["execution"]["status"] == "QUEUED"
    assert body["training_job"]["execution"]["attempt_number"] == 1

    duplicate = client.post(endpoint, headers=auth(root_token), json=payload)
    assert duplicate.status_code == 202, duplicate.text
    assert duplicate.json()["created"] is False
    assert duplicate.json()["training_job"]["id"] == body["training_job"]["id"]

    listed = client.get(endpoint, headers=auth(root_token))
    assert listed.status_code == 200, listed.text
    assert [job["id"] for job in listed.json()] == [body["training_job"]["id"]]

    conflicting = client.post(
        endpoint,
        headers=auth(root_token),
        json={**payload, "idempotency_key": "another-launch"},
    )
    assert conflicting.status_code == 409, conflicting.text

    job_id = body["training_job"]["id"]
    canceled = client.post(
        f"/v1/jobs/CLASSIFICATION_TRAINING_JOB/{job_id}/cancel",
        headers=auth(root_token),
    )
    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["status"] == "CANCELED"
    assert canceled.json()["available_actions"] == ["RETRY"]

    retried = client.post(
        f"/v1/jobs/CLASSIFICATION_TRAINING_JOB/{job_id}/retry",
        headers=auth(root_token),
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "QUEUED"
    assert retried.json()["attempt_number"] == 2
    assert retried.json()["retry_of_workflow_run_id"] == canceled.json()["id"]
