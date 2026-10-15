import uuid
from datetime import datetime, timedelta, timezone

from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from app.artifact_gateway import WorkflowArtifactReference
from app.config import get_settings
from app.models import Client, Matter, MatterSearchExportJob, MetadataDefinition, SearchIndexGeneration
from app.schemas import MatterSearchRequest
from app.search.mappings import compile_document_index, schema_hash
from app.search.query import compile_search_export_request
from app.search_exports import (
    SearchExportTransientError,
    _search_export_failure,
    _serialize_cell,
    _serialize_header,
    canonical_hash,
    run_search_export,
    search_export_field_catalog,
)
from classification_service.schemas import ArtifactType


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_scope(db: Session, root_admin) -> tuple[Matter, MetadataDefinition, SearchIndexGeneration]:
    client = Client(tenant_id=root_admin.tenant_id, name="Export Client")
    db.add(client)
    db.flush()
    matter = Matter(client_id=client.id, name="Export Matter")
    db.add(matter)
    db.flush()
    definition = MetadataDefinition(
        matter_id=matter.id,
        key="responsiveness",
        display_name="Responsiveness",
        type="ENUM",
        cardinality="SINGLE",
        allowed_values=[
            {"key": "responsive", "label": "Responsive", "active": True},
            {"key": "not_responsive", "label": "Not responsive", "active": True},
        ],
        value_source="ASSERTED",
        assertion_policy="IMMEDIATE",
        resolution_policy="LATEST_VALID",
        searchable=True,
        facetable=True,
        normalize_to_lowercase=False,
        reviewable=True,
        ai_assignable=True,
        status="ACTIVE",
    )
    db.add(definition)
    db.flush()
    index = compile_document_index([definition])
    generation = SearchIndexGeneration(
        matter_id=matter.id,
        generation=1,
        index_name=f"matter-{matter.id}-v1",
        alias_name=f"matter-{matter.id}",
        schema_hash=schema_hash(index),
        status="ACTIVE",
        document_count=25,
        schema_snapshot=index,
    )
    db.add(generation)
    db.commit()
    return matter, definition, generation


def test_export_compiler_uses_source_includes_and_stable_search_after_sort() -> None:
    request = MatterSearchRequest.model_validate({"query": "price fixing", "offset": 50, "size": 50})
    body = compile_search_export_request(
        request,
        [],
        tenant_id="tenant-1",
        matter_id="matter-1",
        source_paths=["original_filename", "metadata.responsiveness"],
        page_size=1000,
        search_after=[0.9, "document-1000"],
        required_filters=[{"term": {"batch_ids": "batch-1"}}],
    )

    assert "from" not in body
    assert "highlight" not in body
    assert "aggs" not in body
    assert body["size"] == 1000
    assert body["track_total_hits"] is False
    assert body["_source"] == {
        "includes": ["document_id", "metadata.responsiveness", "original_filename"]
    }
    assert body["sort"] == [{"_score": "desc"}, {"document_id": "asc"}]
    assert body["search_after"] == [0.9, "document-1000"]
    assert {"term": {"batch_ids": "batch-1"}} in body["query"]["bool"]["filter"]


def test_csv_serializer_uses_frozen_enum_labels_arrays_and_formula_safety() -> None:
    enum_field = {
        "value_type": "ENUM",
        "enum_values": [{"key": "responsive", "label": "Responsive"}],
    }
    assert _serialize_cell("responsive", enum_field, spreadsheet_safe=True) == "Responsive"
    assert _serialize_cell(["responsive", "new"], enum_field, spreadsheet_safe=True) == '["Responsive","new"]'
    assert _serialize_cell("=SUM(A1:A2)", {"value_type": "TEXT"}, spreadsheet_safe=True) == "'=SUM(A1:A2)"
    assert _serialize_cell(-4, {"value_type": "INTEGER"}, spreadsheet_safe=True) == "-4"
    assert _serialize_header("=External", spreadsheet_safe=True) == "'=External"


def test_search_export_failure_classification_distinguishes_retryable_dependencies() -> None:
    assert _search_export_failure(SearchExportTransientError("partial shards")) == (
        "SEARCH_EXPORT_SEARCH_UNAVAILABLE",
        True,
    )
    assert _search_export_failure(PermissionError("revoked")) == (
        "SEARCH_EXPORT_AUTHORIZATION_REVOKED",
        False,
    )
    assert _search_export_failure(ProgrammingError("INSERT", {}, Exception("schema not ready"))) == (
        "SEARCH_EXPORT_STORAGE_UNAVAILABLE",
        True,
    )


def test_field_catalog_and_create_endpoint_freeze_export_contract(
    client: TestClient,
    db: Session,
    root_admin,
    root_token: str,
) -> None:
    matter, definition, generation = create_scope(db, root_admin)

    catalog = search_export_field_catalog(db, matter.id, generation)
    metadata_field = next(field for field in catalog if field.id == str(definition.id))
    assert metadata_field.source_path == "metadata.responsiveness"
    assert metadata_field.enum_values == [
        {"key": "responsive", "label": "Responsive"},
        {"key": "not_responsive", "label": "Not responsive"},
    ]

    response = client.post(
        f"/v1/matters/{matter.id}/search-exports",
        headers=auth(root_token),
        json={
            "search": {"query": None, "search_mode": "KEYWORD", "offset": 50, "size": 50},
            "field_ids": ["document_id", str(definition.id)],
            "requested_filename": "responsive export",
            "idempotency_key": "export-1",
        },
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["status"] == "QUEUED"
    assert payload["requested_filename"] == "responsive export.csv"
    assert payload["selected_field_count"] == 2
    assert payload["execution"]["job_type"] == "MATTER_SEARCH_EXPORT"

    job = db.scalar(select(MatterSearchExportJob).where(MatterSearchExportJob.id == uuid.UUID(payload["id"])))
    assert job is not None
    assert job.search_index_generation_id == generation.id
    assert job.search_definition["offset"] == 0
    assert job.search_definition["size"] == 1
    assert job.search_definition["facets"] == []
    assert [field["source_path"] for field in job.field_snapshot] == [
        "document_id",
        "metadata.responsiveness",
    ]

    replay = client.post(
        f"/v1/matters/{matter.id}/search-exports",
        headers=auth(root_token),
        json={
            "search": {"query": None, "search_mode": "KEYWORD", "offset": 0, "size": 100},
            "field_ids": ["document_id", str(definition.id)],
            "requested_filename": "ignored.csv",
            "idempotency_key": "export-1",
        },
    )
    assert replay.status_code == 202
    assert replay.json()["id"] == payload["id"]

    conflicting_policy = client.post(
        f"/v1/matters/{matter.id}/search-exports",
        headers=auth(root_token),
        json={
            "search": {"query": None, "search_mode": "KEYWORD", "offset": 0, "size": 100},
            "field_ids": ["document_id", str(definition.id)],
            "spreadsheet_safe": False,
            "requested_filename": "responsive export.csv",
            "idempotency_key": "export-1",
        },
    )
    assert conflicting_policy.status_code == 409


def test_search_export_streams_pit_pages_and_completes_job(
    client: TestClient,
    db: Session,
    root_admin,
    root_token: str,
    monkeypatch,
) -> None:
    matter, definition, _ = create_scope(db, root_admin)
    response = client.post(
        f"/v1/matters/{matter.id}/search-exports",
        headers=auth(root_token),
        json={
            "search": {
                "query": None,
                "search_mode": "KEYWORD",
                "filters": [{"field": "responsiveness", "operator": "IN", "values": ["responsive"]}],
                "offset": 0,
                "size": 50,
            },
            "field_ids": ["document_id", str(definition.id)],
            "requested_filename": "responsive.csv",
            "idempotency_key": "stream-export-1",
        },
    )
    assert response.status_code == 202, response.text
    job_id = uuid.UUID(response.json()["id"])
    definition.status = "ARCHIVED"
    db.commit()
    stored: dict[str, object] = {}

    class FakeOpenSearchClient:
        def __init__(self, settings) -> None:
            del settings
            self.page = 0

        def open_point_in_time(self, index: str, *, keep_alive: str) -> str:
            assert index.endswith("-v1")
            assert keep_alive == "10m"
            return "pit-1"

        def search_point_in_time(self, pit_id: str, body: dict, *, keep_alive: str, search_pipeline=None) -> dict:
            del keep_alive, search_pipeline
            self.page += 1
            assert pit_id in {"pit-1", "pit-2"}
            if self.page == 1:
                assert body["track_total_hits"] is True
                assert {"terms": {"metadata.responsiveness": ["responsive"]}} in body["query"]["bool"]["filter"]
                return {
                    "pit_id": "pit-2",
                    "timed_out": False,
                    "_shards": {"failed": 0},
                    "hits": {
                        "total": {"value": 2},
                        "hits": [
                            {"_source": {"document_id": "doc-1", "metadata": {"responsiveness": "responsive"}}, "sort": ["doc-1"]},
                            {"_source": {"document_id": "doc-2", "metadata": {"responsiveness": "not_responsive"}}, "sort": ["doc-2"]},
                        ],
                    },
                }
            assert body["search_after"] == ["doc-2"]
            return {"pit_id": "pit-2", "timed_out": False, "_shards": {"failed": 0}, "hits": {"total": {"value": 2}, "hits": []}}

        def close_point_in_time(self, pit_id: str) -> None:
            stored["closed_pit"] = pit_id

        def close(self) -> None:
            stored["closed_client"] = True

    def fake_store_workflow_artifact(**kwargs):
        content = kwargs["content"].read()
        stored["content"] = content
        artifact_id = uuid.uuid4()
        return WorkflowArtifactReference(
            artifact_id=artifact_id,
            tenant_id=kwargs["tenant_id"],
            client_id=kwargs["client_id"],
            core_scope_kind=kwargs["core_scope_kind"],
            core_scope_id=kwargs["core_scope_id"],
            artifact_type=ArtifactType.SEARCH_RESULT_EXPORT,
            schema_version=1,
            derivation_key=kwargs["derivation_key"],
            content_hash=kwargs["content_hash"],
            media_type="text/csv",
            byte_length=len(content),
            producer_kind="CORE",
            producer_run_id=job_id,
            metadata=kwargs["artifact_metadata"],
            created_at=datetime.now(timezone.utc),
        ), True

    monkeypatch.setattr("app.search_exports.OpenSearchClient", FakeOpenSearchClient)
    monkeypatch.setattr("app.search_exports.find_workflow_artifact_reference", lambda **_: None)
    monkeypatch.setattr("app.search_exports.store_workflow_artifact", fake_store_workflow_artifact)

    run_search_export(job_id, get_settings(), session_factory=TestingSessionLocal)

    with TestingSessionLocal() as verify:
        job = verify.get(MatterSearchExportJob, job_id)
        assert job is not None
        assert job.status == "COMPLETED"
        assert job.matched_count == 2
        assert job.exported_count == 2
        assert job.page_count == 1
        assert job.output_artifact_id is not None
    assert stored["content"] == (
        b"Matter document ID,Responsiveness\r\n"
        b"doc-1,Responsive\r\n"
        b"doc-2,Not responsive\r\n"
    )
    assert stored["closed_pit"] == "pit-2"
    assert stored["closed_client"] is True


def test_search_export_adopts_an_artifact_left_by_an_ambiguous_upload(
    client: TestClient,
    db: Session,
    root_admin,
    root_token: str,
    monkeypatch,
) -> None:
    matter, _, _ = create_scope(db, root_admin)
    response = client.post(
        f"/v1/matters/{matter.id}/search-exports",
        headers=auth(root_token),
        json={
            "search": {"query": None, "search_mode": "KEYWORD"},
            "field_ids": ["document_id"],
            "requested_filename": "documents.csv",
            "idempotency_key": "reconcile-export-1",
        },
    )
    assert response.status_code == 202, response.text
    job_id = uuid.UUID(response.json()["id"])
    job = db.get(MatterSearchExportJob, job_id)
    assert job is not None
    assert job.current_workflow_run is not None
    policy_hash = canonical_hash(job.csv_policy_snapshot)
    content_hash = "a" * 64
    artifact = WorkflowArtifactReference(
        artifact_id=uuid.uuid4(),
        tenant_id=job.tenant_id,
        client_id=matter.client_id,
        core_scope_kind="MATTER",
        core_scope_id=matter.id,
        artifact_type=ArtifactType.SEARCH_RESULT_EXPORT,
        schema_version=1,
        derivation_key="b" * 64,
        content_hash=content_hash,
        media_type="text/csv",
        byte_length=42,
        producer_kind="CORE",
        producer_run_id=job_id,
        metadata={
            "export_job_id": str(job_id),
            "search_definition_hash": job.search_definition_hash,
            "field_snapshot_hash": job.field_snapshot_hash,
            "csv_policy_hash": policy_hash,
            "attempt_number": job.current_workflow_run.attempt_number,
            "matched_count": 10,
            "row_count": 10,
            "page_count": 1,
            "snapshot_started_at": datetime.now(timezone.utc).isoformat(),
            "filename": "documents.csv",
            "content_hash": content_hash,
            "byte_length": 42,
        },
        created_at=datetime.now(timezone.utc),
    )
    observed: dict[str, object] = {}

    class NoSearchClient:
        def __init__(self, settings) -> None:
            del settings

        def open_point_in_time(self, index: str, *, keep_alive: str) -> str:
            raise AssertionError(f"search should not be opened for {index} ({keep_alive})")

        def close(self) -> None:
            observed["closed"] = True

    def find_existing(**kwargs):
        observed["derivation_key"] = kwargs["derivation_key"]
        return artifact

    monkeypatch.setattr("app.search_exports.OpenSearchClient", NoSearchClient)
    monkeypatch.setattr("app.search_exports.find_workflow_artifact_reference", find_existing)
    monkeypatch.setattr(
        "app.search_exports.store_workflow_artifact",
        lambda **_: (_ for _ in ()).throw(AssertionError("artifact should not be uploaded twice")),
    )

    run_search_export(job_id, get_settings(), session_factory=TestingSessionLocal)

    with TestingSessionLocal() as verify:
        completed = verify.get(MatterSearchExportJob, job_id)
        assert completed is not None
        assert completed.status == "COMPLETED"
        assert completed.matched_count == 10
        assert completed.exported_count == 10
        assert completed.output_artifact_id == artifact.artifact_id
    assert observed["derivation_key"]
    assert observed["closed"] is True


def test_search_export_download_reauthorizes_and_validates_artifact_provenance(
    client: TestClient,
    db: Session,
    root_admin,
    root_token: str,
    monkeypatch,
) -> None:
    matter, _, _ = create_scope(db, root_admin)
    response = client.post(
        f"/v1/matters/{matter.id}/search-exports",
        headers=auth(root_token),
        json={
            "search": {"query": None, "search_mode": "KEYWORD"},
            "field_ids": ["document_id"],
            "requested_filename": "documents.csv",
            "idempotency_key": "download-export-1",
        },
    )
    job_id = uuid.UUID(response.json()["id"])
    artifact_id = uuid.uuid4()
    content = b"Matter document ID\r\ndoc-1\r\n"
    job = db.get(MatterSearchExportJob, job_id)
    assert job is not None
    job.status = "COMPLETED"
    job.output_artifact_id = artifact_id
    job.output_content_hash = "a" * 64
    job.output_byte_length = len(content)
    job.output_filename = "documents.csv"
    job.expires_at = datetime.now(timezone.utc) + timedelta(days=1)
    db.commit()
    reference = WorkflowArtifactReference(
        artifact_id=artifact_id,
        tenant_id=root_admin.tenant_id,
        client_id=matter.client_id,
        core_scope_kind="MATTER",
        core_scope_id=matter.id,
        artifact_type=ArtifactType.SEARCH_RESULT_EXPORT,
        schema_version=1,
        derivation_key="b" * 64,
        content_hash="a" * 64,
        media_type="text/csv",
        byte_length=len(content),
        producer_kind="CORE",
        producer_run_id=job_id,
        metadata={"export_job_id": str(job_id)},
        created_at=datetime.now(timezone.utc),
    )
    monkeypatch.setattr("app.routers.search.get_workflow_artifact_reference", lambda **_: reference)
    monkeypatch.setattr("app.routers.search.iter_workflow_artifact_content", lambda **_: iter([content]))

    download = client.get(
        f"/v1/matters/{matter.id}/search-exports/{job_id}/download",
        headers=auth(root_token),
    )

    assert download.status_code == 200, download.text
    assert download.content == content
    assert download.headers["content-disposition"] == "attachment; filename*=UTF-8''documents.csv"
    assert download.headers["etag"] == "a" * 64
