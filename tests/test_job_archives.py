import uuid

from conftest import TestingSessionLocal, create_client_tenant
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_review_batches import add_documents, auth, create_matter

from app.models import AuditRecord, JobArchive, MatterDocumentImportJob


def test_terminal_job_delete_archives_without_removing_job(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_matter(client, root_token, create_client_tenant(client, root_token))
    add_documents(matter_id, root_admin.id, count=1)
    with TestingSessionLocal() as db:
        job = db.scalar(
            select(MatterDocumentImportJob).where(
                MatterDocumentImportJob.matter_id == uuid.UUID(matter_id)
            )
        )
        assert job is not None
        job_id = job.id

    listed = client.get(
        f"/v1/matters/{matter_id}/document-imports",
        headers=auth(root_token),
    )
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [str(job_id)]

    deleted = client.delete(
        f"/v1/jobs/MATTER_DOCUMENT_IMPORT/{job_id}",
        headers=auth(root_token),
    )
    assert deleted.status_code == 204, deleted.text
    assert client.delete(
        f"/v1/jobs/MATTER_DOCUMENT_IMPORT/{job_id}",
        headers=auth(root_token),
    ).status_code == 204

    listed = client.get(
        f"/v1/matters/{matter_id}/document-imports",
        headers=auth(root_token),
    )
    assert listed.status_code == 200, listed.text
    assert listed.json() == []

    with TestingSessionLocal() as db:
        assert db.get(MatterDocumentImportJob, job_id) is not None
        archive = db.get(JobArchive, ("MATTER_DOCUMENT_IMPORT", job_id))
        assert archive is not None
        audit = db.scalar(
            select(AuditRecord).where(
                AuditRecord.action == "job.deleted",
                AuditRecord.target_id == job_id,
            )
        )
        assert audit is not None
        assert audit.details["soft_delete"] is True


def test_active_job_cannot_be_deleted(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_matter(client, root_token, create_client_tenant(client, root_token))
    add_documents(matter_id, root_admin.id, count=1)
    with TestingSessionLocal() as db:
        job = db.scalar(
            select(MatterDocumentImportJob).where(
                MatterDocumentImportJob.matter_id == uuid.UUID(matter_id)
            )
        )
        assert job is not None
        job.status = "RUNNING"
        job_id = job.id
        db.commit()

    response = client.delete(
        f"/v1/jobs/MATTER_DOCUMENT_IMPORT/{job_id}",
        headers=auth(root_token),
    )
    assert response.status_code == 409
