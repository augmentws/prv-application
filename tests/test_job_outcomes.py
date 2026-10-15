import hashlib
import uuid
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.artifact_gateway import WorkflowArtifactReference
from app.job_outcomes import (
    JobItemOutcome,
    ensure_embedding_outcome_manifest,
    finalize_canceled_embedding_outcome_manifest,
    finalize_embedding_outcome_manifest,
    page_job_outcomes,
    publish_embedding_outcome_fragment,
)
from app.models import JobOutcomeManifest, MatterEmbeddingBatch, MatterEmbeddingJob


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_embedding_job(client: TestClient, root_token: str) -> tuple[dict, dict]:
    headers = auth(root_token)
    tenant_id = client.get("/v1/auth/me", headers=headers).json()["tenant_id"]
    client_record = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=headers,
        json={"name": "Outcome Client"},
    ).json()
    matter = client.post(
        f"/v1/clients/{client_record['id']}/matters",
        headers=headers,
        json={"name": "Outcome Matter"},
    ).json()
    job = client.post(f"/v1/matters/{matter['id']}/embedding-jobs", headers=headers).json()
    return matter, job


def test_historical_embedding_job_exposes_partial_summary(
    client: TestClient,
    root_token: str,
) -> None:
    matter, job = create_embedding_job(client, root_token)

    response = client.get(
        f"/v1/matters/{matter['id']}/job-outcomes/MATTER_EMBEDDING_JOB/{job['id']}",
        headers=auth(root_token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["manifest_status"] == "UNAVAILABLE"
    assert response.json()["item_details_available"] is False
    assert response.json()["legacy"] is True
    items = client.get(
        f"/v1/matters/{matter['id']}/job-outcomes/MATTER_EMBEDDING_JOB/{job['id']}/items",
        headers=auth(root_token),
    )
    assert items.status_code == 409

    unsupported = client.get(
        f"/v1/matters/{matter['id']}/job-outcomes/UNKNOWN/{job['id']}",
        headers=auth(root_token),
    )
    assert unsupported.status_code == 404


def test_embedding_fragments_finalize_and_page_without_item_rows_in_core(
    client: TestClient,
    root_token: str,
    db: Session,
    monkeypatch,
) -> None:
    matter, created = create_embedding_job(client, root_token)
    job = db.get(MatterEmbeddingJob, uuid.UUID(created["id"]))
    assert job is not None
    document_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    job.total_count = 2
    job.batch_count = 1
    batch = MatterEmbeddingBatch(
        job_id=job.id,
        batch_number=0,
        document_ids=document_ids,
        item_count=2,
    )
    db.add(batch)
    db.commit()

    content_by_id: dict[uuid.UUID, bytes] = {}
    reference_by_derivation: dict[tuple[str, str], WorkflowArtifactReference] = {}
    reference_by_id: dict[uuid.UUID, WorkflowArtifactReference] = {}

    def store(**kwargs):
        content = kwargs["content"]
        content.seek(0)
        value = content.read()
        artifact_id = uuid.uuid4()
        reference = WorkflowArtifactReference(
            artifact_id=artifact_id,
            tenant_id=kwargs["tenant_id"],
            client_id=kwargs["client_id"],
            core_scope_kind=kwargs["core_scope_kind"],
            core_scope_id=kwargs["core_scope_id"],
            artifact_type=kwargs["artifact_type"],
            schema_version=kwargs["schema_version"],
            derivation_key=kwargs["derivation_key"],
            content_hash=hashlib.sha256(value).hexdigest(),
            media_type=kwargs["media_type"],
            byte_length=len(value),
            producer_kind=kwargs["producer_kind"],
            producer_run_id=kwargs["producer_run_id"],
            metadata=kwargs["artifact_metadata"],
            created_at=datetime.now(timezone.utc),
        )
        content_by_id[artifact_id] = value
        reference_by_derivation[(reference.artifact_type.value, reference.derivation_key)] = reference
        reference_by_id[artifact_id] = reference
        return reference, True

    def find(**kwargs):
        return reference_by_derivation.get((kwargs["artifact_type"].value, kwargs["derivation_key"]))

    def copy(**kwargs):
        value = content_by_id[kwargs["artifact_id"]]
        kwargs["destination"].write(value)
        return len(value)

    monkeypatch.setattr("app.job_outcomes.store_workflow_artifact", store)
    monkeypatch.setattr("app.job_outcomes.find_workflow_artifact_reference", find)
    monkeypatch.setattr("app.job_outcomes.get_workflow_artifact_reference", lambda **kwargs: reference_by_id[kwargs["artifact_id"]])
    monkeypatch.setattr("app.job_outcomes.copy_workflow_artifact_content", copy)

    publish_embedding_outcome_fragment(
        db,
        job=job,
        batch=batch,
        outcomes=[
            JobItemOutcome(
                item_id=document_ids[0],
                outcome_status="SUCCEEDED",
                reason_code="embedding.generated",
                metrics={"chunk_count": 3},
            ),
            JobItemOutcome(
                item_id=document_ids[1],
                outcome_status="SKIPPED",
                reason_code="embedding.no_text_source",
            ),
        ],
    )
    manifest = finalize_embedding_outcome_manifest(db, job)
    db.commit()

    assert manifest.status_counts == {"SKIPPED": 1, "SUCCEEDED": 1}
    assert manifest.reason_counts == {
        "embedding.generated": 1,
        "embedding.no_text_source": 1,
    }
    assert db.query(JobOutcomeManifest).count() == 1

    total, rows = page_job_outcomes(
        manifest,
        actor_user_id=job.created_by_user_id,
        status="SKIPPED",
        reason_code=None,
        offset=0,
        limit=100,
    )
    assert total == 1
    assert rows[0]["item_id"] == document_ids[1]
    assert rows[0]["reason_code"] == "embedding.no_text_source"

    api_page = client.get(
        f"/v1/matters/{matter['id']}/job-outcomes/MATTER_EMBEDDING_JOB/{job.id}/items",
        headers=auth(root_token),
        params={"status": "SKIPPED", "limit": 100},
    )
    assert api_page.status_code == 200, api_page.text
    assert api_page.json()["items"][0]["item_id"] == document_ids[1]
    csv_download = client.get(
        f"/v1/matters/{matter['id']}/job-outcomes/MATTER_EMBEDDING_JOB/{job.id}/download",
        headers=auth(root_token),
        params={"format": "csv"},
    )
    assert csv_download.status_code == 200, csv_download.text
    assert csv_download.text.startswith("ordinal,item_kind,item_id,outcome_status")
    assert "embedding.no_text_source" in csv_download.text


def test_canceled_embedding_manifest_fills_unpublished_batches(
    client: TestClient,
    root_token: str,
    db: Session,
    monkeypatch,
) -> None:
    _, created = create_embedding_job(client, root_token)
    job = db.get(MatterEmbeddingJob, uuid.UUID(created["id"]))
    assert job is not None
    document_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    job.total_count = 2
    job.batch_count = 1
    batch = MatterEmbeddingBatch(
        job_id=job.id,
        batch_number=0,
        document_ids=document_ids,
        item_count=2,
    )
    db.add(batch)
    manifest = ensure_embedding_outcome_manifest(db, job)
    db.commit()
    published: list[list[JobItemOutcome]] = []

    monkeypatch.setattr("app.job_outcomes.find_workflow_artifact_reference", lambda **_: None)
    monkeypatch.setattr(
        "app.job_outcomes.publish_embedding_outcome_fragment",
        lambda _db, *, job, batch, outcomes: published.append(outcomes),
    )
    monkeypatch.setattr(
        "app.job_outcomes.finalize_embedding_outcome_manifest",
        lambda _db, _job: manifest,
    )

    finalized = finalize_canceled_embedding_outcome_manifest(db, job)

    assert finalized is manifest
    assert batch.status == "CANCELED"
    assert [[outcome.item_id for outcome in group] for group in published] == [document_ids]
    assert {
        (outcome.outcome_status, outcome.reason_code)
        for outcome in published[0]
    } == {("CANCELED", "embedding.job_canceled")}
