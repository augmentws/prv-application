import hashlib
import io
import json
import uuid
from datetime import datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq
from conftest import (
    ArtifactTestingSessionLocal,
    TestingSessionLocal,
    memory_storage,
)
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.artifact_gateway import ChunkVectorArtifactSnapshot, CollectionItemGroupingSnapshot
from app.embeddings.chunking import TextChunk
from app.embeddings.parquet import write_vector_set
from app.models import (
    AuditRecord,
    ClassificationDatasetSnapshot,
    MatterDocument,
    MatterDocumentImportJob,
    MatterEmbeddingJob,
    ReviewBatch,
    ReviewBatchDocument,
    ReviewBatchRun,
    ReviewBatchRunValue,
)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_matter(client: TestClient, token: str, tenant_id: uuid.UUID, suffix: str) -> dict:
    client_record = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=auth(token),
        json={"name": f"Dataset Client {suffix}"},
    )
    assert client_record.status_code == 201, client_record.text
    matter = client.post(
        f"/v1/clients/{client_record.json()['id']}/matters",
        headers=auth(token),
        json={"name": f"Dataset Matter {suffix}"},
    )
    assert matter.status_code == 201, matter.text
    storage = client.post(
        f"/v1/tenants/{tenant_id}/artifact-storage/ensure",
        headers=auth(token),
        json={"tenant_slug": "root"},
    )
    assert storage.status_code in {200, 201}, storage.text
    return matter.json()


def create_field(
    client: TestClient,
    token: str,
    matter_id: str,
    *,
    key: str,
    cardinality: str = "SINGLE",
) -> dict:
    response = client.post(
        f"/v1/matters/{matter_id}/metadata-definitions",
        headers=auth(token),
        json={
            "key": key,
            "display_name": "Classification Target",
            "type": "ENUM",
            "cardinality": cardinality,
            "reviewable": True,
            "allowed_values": [
                {"key": "responsive", "label": "Responsive"},
                {"key": "not_responsive", "label": "Not responsive"},
                {"key": "needs_review", "label": "Needs review"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_model(client: TestClient, token: str, matter_id: str, definition_id: str) -> dict:
    response = client.post(
        f"/v1/matters/{matter_id}/classification-models",
        headers=auth(token),
        json={
            "target_metadata_definition_id": definition_id,
            "name": "Dataset classifier",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_configuration(
    client: TestClient,
    token: str,
    matter_id: str,
    model_id: str,
    *,
    strategy: str = "BINARY",
    sources: list[dict],
) -> dict:
    if strategy == "BINARY":
        classes = ["negative", "positive"]
        binary_mapping = {
            "negative_class_key": "negative",
            "positive_class_key": "positive",
            "negative_value_keys": ["not_responsive"],
            "positive_value_keys": ["responsive", "needs_review"],
        }
        prediction = {"binary_positive_threshold": 0.5}
    elif strategy == "MULTICLASS":
        classes = ["not_responsive", "responsive", "needs_review"]
        binary_mapping = None
        prediction = {"multiclass_minimum_confidence": 0.5}
    else:
        classes = ["responsive", "needs_review"]
        binary_mapping = None
        prediction = {
            "one_vs_rest_thresholds": {"responsive": 0.5, "needs_review": 0.5},
        }
    response = client.post(
        f"/v1/matters/{matter_id}/classification-models/{model_id}/configurations",
        headers=auth(token),
        json={
            "classification_strategy": strategy,
            "ordered_class_keys": classes,
            "binary_mapping": binary_mapping,
            "label_policy": {
                "allowed_sources": sources,
                "missing_label_action": "EXCLUDE",
                "source_conflict_action": "REQUIRE_ADJUDICATION",
                "target_value_conflict_action": "EXCLUDE",
            },
            "feature_contract": {
                "dimensions": 1024,
                "embedding_model": "voyageai/voyage-4-nano",
                "embedding_model_revision": "revision-1",
                "text_source_policy": "active_text",
            },
            "prediction_resolution_policy": prediction,
            "trainer_specification": {
                "kind": "sklearn_logistic_regression",
                "parameters": {
                    "regularization": "l2",
                    "c_values": [0.1, 1.0],
                    "maximum_iterations": 1000,
                },
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def add_documents(matter_id: str, user_id: uuid.UUID, count: int) -> list[MatterDocument]:
    with TestingSessionLocal() as db:
        job = MatterDocumentImportJob(
            matter_id=uuid.UUID(matter_id),
            source_collection_id=uuid.uuid4(),
            selection_type="EXPLICIT",
            selection={},
            selection_summary="Classification dataset documents",
            status="COMPLETED",
            workflow_id=f"classification-dataset:{uuid.uuid4()}",
            created_by_user_id=user_id,
        )
        db.add(job)
        db.flush()
        documents = [
            MatterDocument(
                matter_id=uuid.UUID(matter_id),
                source_collection_id=job.source_collection_id,
                collection_item_id=uuid.uuid4(),
                added_by_import_job_id=job.id,
            )
            for _ in range(count)
        ]
        db.add_all(documents)
        db.commit()
        return documents


def create_review_source(
    *,
    matter_id: str,
    definition_id: str,
    documents: list[MatterDocument],
    user_id: uuid.UUID,
    values: dict[uuid.UUID, list[str]],
    purpose: str = "REFERENCE",
    configuration_snapshot: dict | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    with TestingSessionLocal() as db:
        batch = ReviewBatch(
            matter_id=uuid.UUID(matter_id),
            name=f"Label source {uuid.uuid4()}",
            selection_type="ALL_MATTER",
            selection_definition={},
            reviewer_value_visibility="ALL_REVIEWER_VALUES",
            status="READY",
            search_status="READY",
            workflow_id=f"label-source:{uuid.uuid4()}",
            document_count=len(documents),
            created_by_user_id=user_id,
            completed_at=datetime.now(timezone.utc),
        )
        db.add(batch)
        db.flush()
        for index, document in enumerate(documents, start=1):
            db.add(
                ReviewBatchDocument(
                    review_batch_id=batch.id,
                    matter_document_id=document.id,
                    sequence_number=index,
                )
            )
        run = ReviewBatchRun(
            review_batch_id=batch.id,
            run_type="HUMAN",
            purpose=purpose,
            status="COMPLETED",
            result_policy="ISOLATED",
            actor_user_id=user_id,
            configuration_snapshot=configuration_snapshot or {},
            initiated_by_user_id=user_id,
            processed_document_count=len(documents),
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.flush()
        for document_id, raw_values in values.items():
            for ordinal, value in enumerate(raw_values):
                db.add(
                    ReviewBatchRunValue(
                        review_batch_run_id=run.id,
                        matter_document_id=document_id,
                        metadata_definition_id=uuid.UUID(definition_id),
                        value_ordinal=ordinal,
                        value_text=value,
                    )
                )
        db.commit()
        return batch.id, run.id


def set_matter_value(
    client: TestClient,
    token: str,
    *,
    matter_id: str,
    document_id: uuid.UUID,
    definition_id: str,
    value: str,
) -> None:
    response = client.post(
        f"/v1/matters/{matter_id}/documents/{document_id}/metadata-values/{definition_id}/events",
        headers=auth(token),
        json={"operation": "SET", "value": value},
    )
    assert response.status_code == 201, response.text


def snapshot_request(configuration_id: str, scope_type: str, scope_id: str) -> dict:
    return {
        "configuration_id": configuration_id,
        "purpose": "TRAINING",
        "training_scope": {"scope_type": scope_type, "scope_id": scope_id},
    }


def configure_artifact_gateway(monkeypatch, documents: list[MatterDocument]) -> None:
    monkeypatch.setattr("app.artifact_gateway.ArtifactSessionLocal", ArtifactTestingSessionLocal)
    monkeypatch.setattr("app.artifact_gateway.get_storage", lambda: memory_storage)
    shared_family = uuid.uuid4()
    grouping = {
        document.collection_item_id: CollectionItemGroupingSnapshot(
            item_id=document.collection_item_id,
            family_id=shared_family if index < 2 else None,
            record_type="EMAIL",
            native_content_hash=(
                "a" * 64 if index < 2 else hashlib.sha256(document.collection_item_id.bytes).hexdigest()
            ),
        )
        for index, document in enumerate(documents)
    }
    monkeypatch.setattr(
        "app.classification_datasets.get_collection_item_grouping_snapshots",
        lambda **_: grouping,
    )


def read_workflow_parquet(
    client: TestClient,
    token: str,
    artifact_id: str,
) -> list[dict]:
    response = client.get(
        f"/v1/workflow-artifacts/{artifact_id}/content",
        headers=auth(token),
    )
    assert response.status_code == 200, response.text
    return pq.read_table(io.BytesIO(response.content)).to_pylist()


def test_matter_snapshot_resolves_precedence_deduplicates_and_reuses_identity(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch,
) -> None:
    matter = create_matter(client, root_token, root_admin.tenant_id, "Precedence")
    definition = create_field(
        client,
        root_token,
        matter["id"],
        key="dataset_precedence_target",
    )
    documents = add_documents(matter["id"], root_admin.id, 4)
    configure_artifact_gateway(monkeypatch, documents)
    for document, value in zip(
        documents[:3],
        ["responsive", "not_responsive", "responsive"],
        strict=True,
    ):
        set_matter_value(
            client,
            root_token,
            matter_id=matter["id"],
            document_id=document.id,
            definition_id=definition["id"],
            value=value,
        )
    _, human_run_id = create_review_source(
        matter_id=matter["id"],
        definition_id=definition["id"],
        documents=documents,
        user_id=root_admin.id,
        values={
            documents[0].id: ["responsive"],
            documents[2].id: ["not_responsive"],
        },
    )
    model = create_model(client, root_token, matter["id"], definition["id"])
    configuration = create_configuration(
        client,
        root_token,
        matter["id"],
        model["id"],
        sources=[
            {
                "kind": "ADJUDICATED_REVIEW",
                "authority": "ADJUDICATED",
                "source_weight": 1.0,
                "source_reference_id": str(human_run_id),
            },
            {
                "kind": "MATTER_ASSERTED",
                "authority": "SILVER",
                "source_weight": 0.25,
            },
        ],
    )
    base = f"/v1/matters/{matter['id']}/classification-models/{model['id']}/dataset-snapshots"
    payload = snapshot_request(configuration["id"], "MATTER", matter["id"])
    preview = client.post(f"{base}/preview", headers=auth(root_token), json=payload)
    assert preview.status_code == 200, preview.text
    preview_data = preview.json()
    assert preview_data["can_materialize"] is True
    assert (preview_data["population_count"], preview_data["labeled_count"]) == (4, 3)
    assert preview_data["unlabeled_count"] == 1
    assert preview_data["class_support"] == {"negative": 2, "positive": 1}
    by_kind = {source["kind"]: source for source in preview_data["source_summary"]}
    assert by_kind["ADJUDICATED_REVIEW"]["selected_count"] == 2
    assert by_kind["MATTER_ASSERTED"]["selected_count"] == 1

    created = client.post(base, headers=auth(root_token), json=payload)
    assert created.status_code == 201, created.text
    snapshot = created.json()["snapshot"]
    assert created.json()["created"] is True
    assert snapshot["status"] == "READY_FOR_FEATURES"
    assert snapshot["aggregate_hash"] == preview_data["aggregate_hash"]
    label_rows = read_workflow_parquet(client, root_token, snapshot["label_artifact_id"])
    labeled = {
        row["matter_document_id"]: row
        for row in label_rows
        if row["resolution_status"] == "LABELED"
    }
    assert labeled[str(documents[0].id)]["source_kind"] == "ADJUDICATED_REVIEW"
    assert labeled[str(documents[0].id)]["source_weight"] == 1.0
    assert len(json.loads(labeled[str(documents[0].id)]["candidate_claims_json"])) == 2
    assert labeled[str(documents[2].id)]["binary_label"] == 0
    assert labeled[str(documents[1].id)]["source_kind"] == "MATTER_ASSERTED"
    assert labeled[str(documents[1].id)]["source_weight"] == 0.25

    retry = client.post(base, headers=auth(root_token), json=payload)
    assert retry.status_code == 201, retry.text
    assert retry.json()["created"] is False
    assert retry.json()["snapshot"]["id"] == snapshot["id"]
    assert retry.json()["snapshot"]["population_artifact_id"] == snapshot["population_artifact_id"]

    add_documents(matter["id"], root_admin.id, 1)
    stored = client.get(f"{base}/{snapshot['id']}", headers=auth(root_token))
    assert stored.status_code == 200
    assert stored.json()["population_count"] == 4
    assert len(read_workflow_parquet(client, root_token, snapshot["population_artifact_id"])) == 4
    with TestingSessionLocal() as db:
        audit = db.scalar(
            select(AuditRecord).where(
                AuditRecord.action == "classification_dataset_snapshot.created",
                AuditRecord.target_id == uuid.UUID(snapshot["id"]),
            )
        )
        assert audit is not None


def test_review_batch_scope_is_exact_and_rejects_wrong_matter(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch,
) -> None:
    matter = create_matter(client, root_token, root_admin.tenant_id, "Batch Scope")
    definition = create_field(client, root_token, matter["id"], key="dataset_batch_target")
    documents = add_documents(matter["id"], root_admin.id, 4)
    configure_artifact_gateway(monkeypatch, documents)
    for document in documents:
        set_matter_value(
            client,
            root_token,
            matter_id=matter["id"],
            document_id=document.id,
            definition_id=definition["id"],
            value="responsive",
        )
    batch_id, _ = create_review_source(
        matter_id=matter["id"],
        definition_id=definition["id"],
        documents=[documents[0], documents[2]],
        user_id=root_admin.id,
        values={},
    )
    model = create_model(client, root_token, matter["id"], definition["id"])
    configuration = create_configuration(
        client,
        root_token,
        matter["id"],
        model["id"],
        sources=[
            {
                "kind": "MATTER_ASSERTED",
                "authority": "GOLD",
                "source_weight": 1.0,
            }
        ],
    )
    base = f"/v1/matters/{matter['id']}/classification-models/{model['id']}/dataset-snapshots"
    payload = snapshot_request(configuration["id"], "REVIEW_BATCH", str(batch_id))
    first = client.post(f"{base}/preview", headers=auth(root_token), json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["population_count"] == 2
    add_documents(matter["id"], root_admin.id, 1)
    second = client.post(f"{base}/preview", headers=auth(root_token), json=payload)
    assert second.status_code == 200
    assert second.json()["population_count"] == 2
    assert second.json()["aggregate_hash"] == first.json()["aggregate_hash"]

    other = create_matter(client, root_token, root_admin.tenant_id, "Wrong Batch")
    other_documents = add_documents(other["id"], root_admin.id, 1)
    wrong_batch_id, _ = create_review_source(
        matter_id=other["id"],
        definition_id=definition["id"],
        documents=other_documents,
        user_id=root_admin.id,
        values={},
    )
    wrong = client.post(
        f"{base}/preview",
        headers=auth(root_token),
        json=snapshot_request(configuration["id"], "REVIEW_BATCH", str(wrong_batch_id)),
    )
    assert wrong.status_code == 200
    assert wrong.json()["can_materialize"] is False
    assert "REVIEW_BATCH_WRONG_MATTER" in {
        issue["code"] for issue in wrong.json()["blocking_errors"]
    }


def test_multiclass_conflicts_and_blinded_sources_are_reported(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch,
) -> None:
    matter = create_matter(client, root_token, root_admin.tenant_id, "Conflicts")
    definition = create_field(client, root_token, matter["id"], key="dataset_conflict_target")
    documents = add_documents(matter["id"], root_admin.id, 2)
    configure_artifact_gateway(monkeypatch, documents)
    _, review_run_id = create_review_source(
        matter_id=matter["id"],
        definition_id=definition["id"],
        documents=documents,
        user_id=root_admin.id,
        purpose="REVIEW",
        values={
            documents[0].id: ["responsive", "not_responsive"],
            documents[1].id: ["needs_review"],
        },
    )
    _, blinded_run_id = create_review_source(
        matter_id=matter["id"],
        definition_id=definition["id"],
        documents=documents,
        user_id=root_admin.id,
        purpose="ASSESSMENT",
        configuration_snapshot={"blinded": True},
        values={documents[0].id: ["responsive"]},
    )
    model = create_model(client, root_token, matter["id"], definition["id"])
    configuration = create_configuration(
        client,
        root_token,
        matter["id"],
        model["id"],
        strategy="MULTICLASS",
        sources=[
            {
                "kind": "HUMAN_REVIEW",
                "authority": "GOLD",
                "source_weight": 1.0,
                "source_reference_id": str(review_run_id),
            },
            {
                "kind": "HUMAN_REVIEW",
                "authority": "SILVER",
                "source_weight": 0.5,
                "source_reference_id": str(blinded_run_id),
            },
        ],
    )
    base = f"/v1/matters/{matter['id']}/classification-models/{model['id']}/dataset-snapshots"
    preview = client.post(
        f"{base}/preview",
        headers=auth(root_token),
        json=snapshot_request(configuration["id"], "MATTER", matter["id"]),
    )
    assert preview.status_code == 200, preview.text
    data = preview.json()
    assert data["labeled_count"] == 1
    assert data["conflicting_count"] == 1
    assert data["source_summary"][1]["excluded_blinded"] is True
    warning_codes = {warning["code"] for warning in data["warnings"]}
    assert "TARGET_CONFLICTS_EXCLUDED" in warning_codes
    assert "BLINDED_LABEL_SOURCE_EXCLUDED" in warning_codes


def test_one_vs_rest_partial_observed_mask_is_preserved(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch,
) -> None:
    matter = create_matter(client, root_token, root_admin.tenant_id, "Observed Mask")
    definition = create_field(
        client,
        root_token,
        matter["id"],
        key="dataset_ovr_target",
        cardinality="MULTIPLE",
    )
    documents = add_documents(matter["id"], root_admin.id, 1)
    configure_artifact_gateway(monkeypatch, documents)
    _, review_run_id = create_review_source(
        matter_id=matter["id"],
        definition_id=definition["id"],
        documents=documents,
        user_id=root_admin.id,
        purpose="REVIEW",
        configuration_snapshot={"classification_observed_class_keys": ["responsive"]},
        values={documents[0].id: ["responsive"]},
    )
    model = create_model(client, root_token, matter["id"], definition["id"])
    configuration = create_configuration(
        client,
        root_token,
        matter["id"],
        model["id"],
        strategy="ONE_VS_REST",
        sources=[
            {
                "kind": "HUMAN_REVIEW",
                "authority": "GOLD",
                "source_weight": 1.0,
                "source_reference_id": str(review_run_id),
            }
        ],
    )
    base = f"/v1/matters/{matter['id']}/classification-models/{model['id']}/dataset-snapshots"
    created = client.post(
        base,
        headers=auth(root_token),
        json=snapshot_request(configuration["id"], "MATTER", matter["id"]),
    )
    assert created.status_code == 201, created.text
    label_rows = read_workflow_parquet(
        client,
        root_token,
        created.json()["snapshot"]["label_artifact_id"],
    )
    assert label_rows[0]["one_vs_rest_labels"] == [1, 0]
    assert label_rows[0]["one_vs_rest_observed_mask"] == [True, False]
    assert label_rows[0]["class_keys"] == ["responsive", "needs_review"]


def test_snapshot_rows_are_listed_and_scoped_to_model(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch,
) -> None:
    matter = create_matter(client, root_token, root_admin.tenant_id, "Listing")
    definition = create_field(client, root_token, matter["id"], key="dataset_list_target")
    documents = add_documents(matter["id"], root_admin.id, 1)
    configure_artifact_gateway(monkeypatch, documents)
    set_matter_value(
        client,
        root_token,
        matter_id=matter["id"],
        document_id=documents[0].id,
        definition_id=definition["id"],
        value="responsive",
    )
    model = create_model(client, root_token, matter["id"], definition["id"])
    configuration = create_configuration(
        client,
        root_token,
        matter["id"],
        model["id"],
        sources=[
            {
                "kind": "MATTER_ASSERTED",
                "authority": "GOLD",
                "source_weight": 1.0,
            }
        ],
    )
    base = f"/v1/matters/{matter['id']}/classification-models/{model['id']}/dataset-snapshots"
    created = client.post(
        base,
        headers=auth(root_token),
        json=snapshot_request(configuration["id"], "MATTER", matter["id"]),
    )
    assert created.status_code == 201, created.text
    listed = client.get(base, headers=auth(root_token))
    assert listed.status_code == 200
    assert [value["id"] for value in listed.json()] == [created.json()["snapshot"]["id"]]
    with TestingSessionLocal() as db:
        stored = db.scalar(select(ClassificationDatasetSnapshot))
        assert stored is not None
        assert stored.population_content_hash == created.json()["snapshot"]["population_content_hash"]


def create_completed_embedding_job(
    *,
    matter_id: str,
    user_id: uuid.UUID,
) -> MatterEmbeddingJob:
    configuration_hash = hashlib.sha256(b"phase-5-embedding-configuration").hexdigest()
    with TestingSessionLocal() as db:
        job = MatterEmbeddingJob(
            matter_id=uuid.UUID(matter_id),
            status="COMPLETED",
            workflow_id=f"classification-features:{uuid.uuid4()}",
            configuration_hash=configuration_hash,
            configuration={
                "input": {"name": "subject-filename-body", "version": 1},
                "chunking": {"name": "sentence-aware", "version": 1},
                "embedding": {
                    "provider": "test",
                    "model": "voyageai/voyage-4-nano",
                    "model_revision": "revision-1",
                    "dimensions": 1024,
                    "normalized": True,
                },
            },
            embedding_model="voyageai/voyage-4-nano",
            embedding_model_revision="revision-1",
            embedding_dimensions=1024,
            embedding_normalized=True,
            total_count=0,
            processed_count=0,
            embedded_count=0,
            skipped_count=0,
            failed_count=0,
            chunk_count=0,
            created_by_user_id=user_id,
            completed_at=datetime.now(timezone.utc),
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job


def chunk_vector_snapshot(
    *,
    document_index: int,
    configuration_hash: str,
    dimensions: int = 1024,
) -> ChunkVectorArtifactSnapshot:
    chunks = [
        TextChunk(
            chunk_id=f"chunk-{document_index}-{ordinal}",
            ordinal=ordinal,
            char_start=ordinal * 10,
            char_end=(ordinal + 1) * 10,
            text=f"test chunk {ordinal}",
        )
        for ordinal in range(2)
    ]
    vectors = [[0.0] * dimensions for _ in chunks]
    vectors[0][document_index % dimensions] = 1.0
    vectors[1][(document_index + 1) % dimensions] = 1.0
    content = write_vector_set(
        chunks,
        vectors,
        dimensions=dimensions,
        metadata={
            "model": "voyageai/voyage-4-nano",
            "configuration_hash": configuration_hash,
        },
    )
    return ChunkVectorArtifactSnapshot(
        artifact_id=uuid.uuid4(),
        content_hash=hashlib.sha256(content).hexdigest(),
        content=content,
        metadata={"configuration_hash": configuration_hash},
    )


def test_feature_and_split_materialization_is_deterministic_and_group_aware(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch,
) -> None:
    matter = create_matter(client, root_token, root_admin.tenant_id, "Features")
    definition = create_field(client, root_token, matter["id"], key="dataset_feature_target")
    documents = add_documents(matter["id"], root_admin.id, 10)
    configure_artifact_gateway(monkeypatch, documents)
    for index, document in enumerate(documents):
        set_matter_value(
            client,
            root_token,
            matter_id=matter["id"],
            document_id=document.id,
            definition_id=definition["id"],
            value="responsive" if index % 2 == 0 else "not_responsive",
        )
    _, human_run_id = create_review_source(
        matter_id=matter["id"],
        definition_id=definition["id"],
        documents=documents,
        user_id=root_admin.id,
        values={
            document.id: ["responsive" if index % 2 == 0 else "not_responsive"]
            for index, document in enumerate(documents[:4])
        },
    )
    model = create_model(client, root_token, matter["id"], definition["id"])
    configuration = create_configuration(
        client,
        root_token,
        matter["id"],
        model["id"],
        sources=[
            {
                "kind": "HUMAN_REVIEW",
                "authority": "GOLD",
                "source_weight": 1.0,
                "source_reference_id": str(human_run_id),
            },
            {
                "kind": "MATTER_ASSERTED",
                "authority": "SILVER",
                "source_weight": 0.5,
            }
        ],
    )
    base = f"/v1/matters/{matter['id']}/classification-models/{model['id']}/dataset-snapshots"
    snapshot_response = client.post(
        base,
        headers=auth(root_token),
        json=snapshot_request(configuration["id"], "MATTER", matter["id"]),
    )
    assert snapshot_response.status_code == 201, snapshot_response.text
    snapshot = snapshot_response.json()["snapshot"]
    job = create_completed_embedding_job(matter_id=matter["id"], user_id=root_admin.id)
    vector_artifacts = {
        document.collection_item_id: chunk_vector_snapshot(
            document_index=index,
            configuration_hash=job.configuration_hash,
            dimensions=32 if index == 8 else 1024,
        )
        for index, document in enumerate(documents[:-1])
    }
    monkeypatch.setattr(
        "app.classification_features.load_current_chunk_vector_artifact",
        lambda collection_item_id, **_: vector_artifacts.get(collection_item_id),
    )
    endpoint = f"{base}/{snapshot['id']}/features-and-splits"
    payload = {
        "embedding_job_id": str(job.id),
        "validation_policy": {
            "fold_count": 2,
            "minimum_labeled_count": 6,
            "minimum_labeled_count_per_class": 1,
        },
        "random_seed": 1729,
    }
    materialized = client.post(endpoint, headers=auth(root_token), json=payload)
    assert materialized.status_code == 201, materialized.text
    result = materialized.json()
    assert result["materialized"] is True
    ready = result["snapshot"]
    assert ready["status"] == "READY_FOR_TRAINING"
    assert ready["feature_ready_count"] == 8
    assert ready["feature_missing_count"] == 1
    assert ready["feature_failed_count"] == 1
    assert ready["training_eligible_count"] == 8
    assert ready["feature_specification"]["embedding_job_id"] == str(job.id)
    assert ready["split_specification"]["algorithm"]["version"] == 1
    assert ready["training_aggregate_hash"]
    assert {
        warning["code"] for warning in ready["warnings"]
    } >= {
        "MISSING_FEATURES_EXCLUDED",
        "FAILED_FEATURES_EXCLUDED",
        "NEAR_DUPLICATE_GROUPING_UNAVAILABLE",
    }

    feature_rows = read_workflow_parquet(
        client,
        root_token,
        ready["feature_artifact_id"],
    )
    assert [row["matter_document_id"] for row in feature_rows] == sorted(
        row["matter_document_id"] for row in feature_rows
    )
    first_ready_feature = next(row for row in feature_rows if row["feature_status"] == "READY")
    assert len(first_ready_feature["embedding"]) == 1024
    assert abs(sum(value * value for value in first_ready_feature["embedding"]) - 1.0) < 1e-6
    feature_content = client.get(
        f"/v1/workflow-artifacts/{ready['feature_artifact_id']}/content",
        headers=auth(root_token),
    )
    feature_table = pq.read_table(io.BytesIO(feature_content.content))
    assert pa.types.is_float32(feature_table.schema.field("embedding").type.value_type)
    assert {row["chunk_count"] for row in feature_rows if row["feature_status"] == "READY"} == {2}
    features_by_document = {row["matter_document_id"]: row for row in feature_rows}
    assert features_by_document[str(documents[8].id)]["feature_status"] == "FAILED"
    assert features_by_document[str(documents[9].id)]["feature_status"] == "MISSING"

    split_rows = read_workflow_parquet(
        client,
        root_token,
        ready["split_artifact_id"],
    )
    splits_by_document = {row["matter_document_id"]: row for row in split_rows}
    assert (
        splits_by_document[str(documents[0].id)]["group_id"]
        == splits_by_document[str(documents[1].id)]["group_id"]
    )
    assert (
        splits_by_document[str(documents[0].id)]["fold_index"]
        == splits_by_document[str(documents[1].id)]["fold_index"]
    )
    for fold in range(2):
        training_rows = [
            row
            for row in split_rows
            if row["normalized_training_weights"][fold] is not None
        ]
        training_weights = [row["normalized_training_weights"][fold] for row in training_rows]
        assert abs(sum(training_weights) / len(training_weights) - 1.0) < 1e-12
        normalization_ratios = {
            round(row["normalized_training_weights"][fold] / row["source_weight"], 12)
            for row in training_rows
        }
        assert len(normalization_ratios) == 1
    assert {row["source_weight"] for row in split_rows if row["training_eligible"]} == {0.5, 1.0}
    assert splits_by_document[str(documents[8].id)]["training_eligible"] is False
    assert splits_by_document[str(documents[9].id)]["training_eligible"] is False

    repeated = client.post(endpoint, headers=auth(root_token), json=payload)
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["materialized"] is False
    assert repeated.json()["snapshot"]["feature_content_hash"] == ready["feature_content_hash"]
    assert repeated.json()["snapshot"]["split_content_hash"] == ready["split_content_hash"]

    with TestingSessionLocal() as db:
        audit = db.scalar(
            select(AuditRecord).where(
                AuditRecord.action == "classification_dataset_snapshot.ready_for_training"
            )
        )
        assert audit is not None
        assert audit.details["training_aggregate_hash"] == ready["training_aggregate_hash"]


def test_feature_split_rejects_class_confined_to_one_connected_group(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch,
) -> None:
    matter = create_matter(client, root_token, root_admin.tenant_id, "Split support")
    definition = create_field(client, root_token, matter["id"], key="dataset_split_support")
    documents = add_documents(matter["id"], root_admin.id, 6)
    positive_family = uuid.uuid4()
    grouping = {
        document.collection_item_id: CollectionItemGroupingSnapshot(
            item_id=document.collection_item_id,
            family_id=positive_family if index < 3 else None,
            record_type="EMAIL",
            native_content_hash=hashlib.sha256(document.collection_item_id.bytes).hexdigest(),
        )
        for index, document in enumerate(documents)
    }
    monkeypatch.setattr("app.artifact_gateway.ArtifactSessionLocal", ArtifactTestingSessionLocal)
    monkeypatch.setattr("app.artifact_gateway.get_storage", lambda: memory_storage)
    monkeypatch.setattr(
        "app.classification_datasets.get_collection_item_grouping_snapshots",
        lambda **_: grouping,
    )
    for index, document in enumerate(documents):
        set_matter_value(
            client,
            root_token,
            matter_id=matter["id"],
            document_id=document.id,
            definition_id=definition["id"],
            value="responsive" if index < 3 else "not_responsive",
        )
    model = create_model(client, root_token, matter["id"], definition["id"])
    configuration = create_configuration(
        client,
        root_token,
        matter["id"],
        model["id"],
        sources=[
            {
                "kind": "MATTER_ASSERTED",
                "authority": "GOLD",
                "source_weight": 1.0,
            }
        ],
    )
    base = f"/v1/matters/{matter['id']}/classification-models/{model['id']}/dataset-snapshots"
    snapshot_response = client.post(
        base,
        headers=auth(root_token),
        json=snapshot_request(configuration["id"], "MATTER", matter["id"]),
    )
    assert snapshot_response.status_code == 201, snapshot_response.text
    snapshot = snapshot_response.json()["snapshot"]
    job = create_completed_embedding_job(matter_id=matter["id"], user_id=root_admin.id)
    vectors = {
        document.collection_item_id: chunk_vector_snapshot(
            document_index=index,
            configuration_hash=job.configuration_hash,
        )
        for index, document in enumerate(documents)
    }
    monkeypatch.setattr(
        "app.classification_features.load_current_chunk_vector_artifact",
        lambda collection_item_id, **_: vectors[collection_item_id],
    )
    response = client.post(
        f"{base}/{snapshot['id']}/features-and-splits",
        headers=auth(root_token),
        json={
            "embedding_job_id": str(job.id),
            "validation_policy": {
                "fold_count": 2,
                "minimum_labeled_count": 6,
                "minimum_labeled_count_per_class": 1,
            },
            "random_seed": 42,
        },
    )
    assert response.status_code == 409, response.text
    read = client.get(f"{base}/{snapshot['id']}", headers=auth(root_token))
    assert read.status_code == 200
    assert read.json()["status"] == "FAILED"
    assert read.json()["failure_code"] == "INSUFFICIENT_CLASS_SUPPORT_PER_FOLD"
