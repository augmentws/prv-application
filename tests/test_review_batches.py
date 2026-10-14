import hashlib
import uuid
from datetime import datetime, timezone

from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.analysis_task_promotions import (
    apply_analysis_task_promotion_chunk,
    complete_analysis_task_promotion,
    prepare_analysis_task_promotion,
)
from app.models import (
    AuditRecord,
    DocumentMetadataCurrent,
    JobArchive,
    Matter,
    MatterAnalysisTask,
    MatterDefinition,
    MatterDefinitionAssessmentCandidate,
    MatterDefinitionAssessmentRun,
    MatterDefinitionRevision,
    MatterDocument,
    MatterDocumentImportJob,
    MatterSearchExportJob,
    MetadataDefinition,
    MetadataGroup,
    MetadataGroupField,
    ReviewBatch,
    ReviewBatchDocument,
    ReviewBatchRun,
    ReviewBatchRunDocument,
    ReviewBatchRunPromotion,
    ReviewBatchRunValue,
    ReviewBatchSearchCodingProjection,
    ReviewBatchSearchCodingRun,
    SearchIndexGeneration,
    SearchProjectionOperation,
    WorkflowRun,
)
from app.review_batches import create_review_batch_execution
from app.schemas import MatterFacetValuesResponse, MatterSearchResponse


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_matter(client: TestClient, root_token: str, tenant_id: str) -> str:
    created_client = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=auth(root_token),
        json={"name": "Batch Client"},
    )
    assert created_client.status_code == 201, created_client.text
    matter = client.post(
        f"/v1/clients/{created_client.json()['id']}/matters",
        headers=auth(root_token),
        json={"name": "Batch Matter"},
    )
    assert matter.status_code == 201, matter.text
    return matter.json()["id"]


def add_documents(matter_id: str, user_id: uuid.UUID, count: int = 5) -> list[str]:
    with TestingSessionLocal() as db:
        job = MatterDocumentImportJob(
            matter_id=uuid.UUID(matter_id),
            source_collection_id=uuid.uuid4(),
            selection_type="EXPLICIT",
            selection={},
            selection_summary="Test documents",
            status="COMPLETED",
            workflow_id=f"test:{uuid.uuid4()}",
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
        return [str(document.id) for document in documents]


def add_assessment_candidates(
    matter_id: str,
    document_ids: list[str],
    user_id: uuid.UUID,
    *,
    selected_count: int = 1,
) -> uuid.UUID:
    with TestingSessionLocal() as db:
        matter = db.get(Matter, uuid.UUID(matter_id))
        assert matter is not None
        effective_selected_count = min(selected_count, len(document_ids))
        guidance = MatterDefinition(
            matter_id=matter.id,
            key=f"assessment-{uuid.uuid4()}",
            name="Assessment source guidance",
            created_by_user_id=user_id,
        )
        db.add(guidance)
        db.flush()
        revision = MatterDefinitionRevision(
            matter_definition_id=guidance.id,
            revision=1,
            content_markdown="# Assessment source",
            source_kind="MARKDOWN",
            created_by_user_id=user_id,
        )
        db.add(revision)
        workflow = WorkflowRun(
            tenant_id=matter.client.tenant_id,
            client_id=matter.client_id,
            matter_id=matter.id,
            workflow_key="matter-definition-assessment",
            code_version="test",
            dbos_workflow_id=f"test-assessment:{uuid.uuid4()}",
            status="COMPLETED",
            initiated_by_user_id=user_id,
        )
        db.add(workflow)
        db.flush()
        assessment = MatterDefinitionAssessmentRun(
            name="Assessment retrieval pool",
            matter_id=matter.id,
            matter_definition_revision_id=revision.id,
            definition_content_hash="a" * 64,
            workflow_run_id=workflow.id,
            requested_document_count=max(selected_count, 1),
            candidate_count=len(document_ids),
            selected_count=effective_selected_count,
            status="COMPLETED",
            initiated_by_user_id=user_id,
        )
        db.add(assessment)
        db.flush()
        db.add_all(
            MatterDefinitionAssessmentCandidate(
                assessment_run_id=assessment.id,
                matter_document_id=uuid.UUID(document_id),
                retrieval_provenance=[{"query": "test"}],
                fused_score=float(len(document_ids) - ordinal),
                selected=ordinal < effective_selected_count,
                selection_order=ordinal + 1 if ordinal < effective_selected_count else None,
                selection_reason="quota" if ordinal < effective_selected_count else None,
            )
            for ordinal, document_id in enumerate(document_ids)
        )
        db.commit()
        return assessment.id


def test_review_batch_delete_archives_without_removing_history(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_matter(client, root_token, str(root_admin.tenant_id))
    add_documents(matter_id, root_admin.id, count=2)
    created = client.post(
        f"/v1/matters/{matter_id}/review-batches",
        headers=auth(root_token),
        json={"name": "Disposable batch", "selection_type": "ALL_MATTER"},
    )
    assert created.status_code == 202, created.text
    batch_id = created.json()["id"]
    assert created.json()["document_count"] == 2

    deleted = client.delete(
        f"/v1/matters/{matter_id}/review-batches/{batch_id}",
        headers=auth(root_token),
    )
    assert deleted.status_code == 204, deleted.text
    assert client.delete(
        f"/v1/matters/{matter_id}/review-batches/{batch_id}",
        headers=auth(root_token),
    ).status_code == 204

    listed = client.get(f"/v1/matters/{matter_id}/review-batches", headers=auth(root_token))
    assert listed.status_code == 200, listed.text
    assert listed.json() == []

    with TestingSessionLocal() as db:
        batch = db.get(ReviewBatch, uuid.UUID(batch_id))
        assert batch is not None
        assert batch.status == "ARCHIVED"
        assert db.scalar(
            select(ReviewBatchDocument).where(ReviewBatchDocument.review_batch_id == batch.id).limit(1)
        ) is not None
        audit = db.scalar(
            select(AuditRecord).where(
                AuditRecord.action == "review_batch.deleted",
                AuditRecord.target_id == batch.id,
            )
        )
        assert audit is not None
        assert audit.details["soft_delete"] is True


def test_random_assessment_batch_samples_full_persisted_candidate_pool(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_matter(client, root_token, str(root_admin.tenant_id))
    document_ids = add_documents(matter_id, root_admin.id, count=6)
    assessment_id = add_assessment_candidates(matter_id, document_ids, root_admin.id)
    seed = "assessment-sample-seed"

    created = client.post(
        f"/v1/matters/{matter_id}/review-batches",
        headers=auth(root_token),
        json={
            "name": "Assessment candidate sample",
            "selection_type": "RANDOM_ASSESSMENT",
            "assessment_run_id": str(assessment_id),
            "sample_size": 3,
            "random_seed": seed,
        },
    )

    assert created.status_code == 202, created.text
    payload = created.json()
    assert payload["status"] == "READY"
    assert payload["document_count"] == 3
    assert payload["execution"]["job_type"] == "REVIEW_BATCH_MATERIALIZATION"
    assert payload["execution"]["status"] == "COMPLETED"
    assert payload["execution"]["progress_completed"] == 3
    assert payload["execution"]["progress_total"] == 3
    assert payload["selection_definition"] == {
        "type": "RANDOM_ASSESSMENT",
        "assessment_run_id": str(assessment_id),
        "assessment_name": "Assessment retrieval pool",
        "assessment_candidate_count": 6,
        "assessment_selected_count": 1,
        "matter_definition_revision_id": payload["selection_definition"]["matter_definition_revision_id"],
        "definition_content_hash": "a" * 64,
        "source_review_batch_id": None,
        "search_index_generation_id": None,
    }
    expected_ids = sorted(
        (uuid.UUID(document_id) for document_id in document_ids),
        key=lambda document_id: hashlib.sha256(f"{seed}:{document_id}".encode()).digest(),
    )[:3]
    with TestingSessionLocal() as db:
        actual_ids = list(
            db.scalars(
                select(ReviewBatchDocument.matter_document_id)
                .where(ReviewBatchDocument.review_batch_id == uuid.UUID(payload["id"]))
                .order_by(ReviewBatchDocument.sequence_number)
            )
        )
    assert actual_ids == expected_ids
    assert any(str(document_id) != document_ids[0] for document_id in actual_ids)


def test_random_assessment_batch_requires_a_persisted_candidate_pool(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_matter(client, root_token, str(root_admin.tenant_id))
    assessment_id = add_assessment_candidates(matter_id, [], root_admin.id, selected_count=1)

    created = client.post(
        f"/v1/matters/{matter_id}/review-batches",
        headers=auth(root_token),
        json={
            "name": "Empty assessment sample",
            "selection_type": "RANDOM_ASSESSMENT",
            "assessment_run_id": str(assessment_id),
            "sample_size": 10,
        },
    )

    assert created.status_code == 409
    assert "persisted retrieval candidate pool" in created.text


def test_review_batch_common_execution_can_be_canceled_and_retried(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_matter(client, root_token, str(root_admin.tenant_id))
    with TestingSessionLocal() as db:
        batch = ReviewBatch(
            matter_id=uuid.UUID(matter_id),
            name="Queued population",
            selection_type="ALL_MATTER",
            selection_definition={"type": "ALL_MATTER"},
            status="QUEUED",
            search_status="QUEUED",
            workflow_id=f"review-batch:{uuid.uuid4()}",
            created_by_user_id=root_admin.id,
        )
        db.add(batch)
        db.flush()
        create_review_batch_execution(
            db,
            batch,
            initiated_by_user_id=root_admin.id,
        )
        db.commit()
        batch_id = batch.id

    canceled = client.post(
        f"/v1/jobs/REVIEW_BATCH_MATERIALIZATION/{batch_id}/cancel",
        headers=auth(root_token),
    )
    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["status"] == "CANCELED"
    assert canceled.json()["available_actions"] == ["RETRY"]

    retried = client.post(
        f"/v1/jobs/REVIEW_BATCH_MATERIALIZATION/{batch_id}/retry",
        headers=auth(root_token),
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "QUEUED"
    assert retried.json()["attempt_number"] == 2
    assert retried.json()["retry_of_workflow_run_id"] == canceled.json()["id"]


def test_batch_membership_group_snapshot_runs_and_comparison(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_matter(client, root_token, str(root_admin.tenant_id))
    document_ids = add_documents(matter_id, root_admin.id)
    with TestingSessionLocal() as db:
        definition_id = db.scalar(
            select(MetadataDefinition.id).where(
                MetadataDefinition.matter_id == uuid.UUID(matter_id),
                MetadataDefinition.key == "key_document",
            )
        )
        group_id = db.scalar(
            select(MetadataGroup.id)
            .join(MetadataGroupField, MetadataGroupField.metadata_group_id == MetadataGroup.id)
            .where(
                MetadataGroup.matter_id == uuid.UUID(matter_id),
                MetadataGroupField.metadata_definition_id == definition_id,
            )
        )
    assert group_id is not None
    assert definition_id is not None

    base = f"/v1/matters/{matter_id}/review-batches"
    created = client.post(
        base,
        headers=auth(root_token),
        json={
            "name": "All documents",
            "description": "Human reference and agent evaluation corpus",
            "selection_type": "ALL_MATTER",
            "reviewer_value_visibility": "OWN_VALUES",
            "coding_group_ids": [str(group_id)],
            "note": "Frozen before the first agent run.",
        },
    )
    assert created.status_code == 202, created.text
    batch = created.json()
    assert batch["status"] == "READY"
    assert batch["search_status"] == "NOT_CONFIGURED"
    assert batch["document_count"] == len(document_ids)
    assert batch["execution"]["status"] == "COMPLETED"
    assert batch["execution"]["stage_label"] == "Completed"
    assert batch["execution"]["attempt_number"] == 1
    assert batch["coding_groups"]

    reviewer_coding = client.post(f"{base}/{batch['id']}/reviewer-coding", headers=auth(root_token))
    assert reviewer_coding.status_code == 200, reviewer_coding.text
    resumed = client.post(f"{base}/{batch['id']}/reviewer-coding", headers=auth(root_token))
    assert resumed.status_code == 200
    assert resumed.json()["id"] == reviewer_coding.json()["id"]
    documents = client.get(
        f"{base}/{batch['id']}/documents",
        headers=auth(root_token),
        params={"reviewer_coding_id": reviewer_coding.json()["id"]},
    )
    assert documents.status_code == 200, documents.text
    assert documents.json()[0]["collection_item_id"]
    review_value_payload = {
        "matter_document_id": document_ids[0],
        "fields": [{"metadata_definition_id": str(definition_id), "values": [True]}],
    }
    saved_review = client.put(
        f"{base}/{batch['id']}/reviewer-coding/documents/{document_ids[0]}/values",
        headers=auth(root_token),
        json=review_value_payload,
    )
    assert saved_review.status_code == 200, saved_review.text
    coding = client.get(
        f"{base}/{batch['id']}/reviewer-coding/documents/{document_ids[0]}",
        headers=auth(root_token),
    )
    assert coding.status_code == 200
    assert coding.json()["review_status"] == "COMPLETED"
    assert coding.json()["values"][0]["value"] is True
    history = client.get(
        f"{base}/{batch['id']}/documents/{document_ids[0]}/coding-history",
        headers=auth(root_token),
    )
    assert history.status_code == 200, history.text
    human_history = history.json()
    assert len(human_history) == 2
    assert human_history[0]["event_kind"] == "STATUS"
    assert human_history[0]["document_status"] == "COMPLETED"
    assert human_history[0]["reviewer_revision"] == 1
    assert human_history[1]["event_kind"] == "VALUE"
    assert human_history[1]["metadata_definition_id"] == str(definition_id)
    assert human_history[1]["value"] is True
    assert human_history[1]["value_label"] == "Yes"
    assert human_history[1]["reviewer_coding_id"] == reviewer_coding.json()["id"]
    assert human_history[1]["reviewer_origin"] == "REVIEW_UI"
    assert human_history[1]["source_label"] == root_admin.email
    document_history = client.get(
        f"/v1/matters/{matter_id}/documents/{document_ids[0]}/coding-history",
        headers=auth(root_token),
    )
    assert document_history.status_code == 200, document_history.text
    assert document_history.json()["direct"] == []
    assert document_history.json()["batches"][0]["review_batch_id"] == batch["id"]
    assert document_history.json()["batches"][0]["batch_name"] == "All documents"
    assert document_history.json()["batches"][0]["entries"] == history.json()
    progress = client.get(
        f"{base}/{batch['id']}/reviewer-coding/progress",
        headers=auth(root_token),
    )
    assert progress.status_code == 200
    assert progress.json()["completed_count"] == 1
    assert progress.json()["not_started_count"] == len(document_ids) - 1

    random_batch = client.post(
        base,
        headers=auth(root_token),
        json={
            "name": "Random three",
            "selection_type": "RANDOM_BATCH",
            "source_batch_id": batch["id"],
            "sample_size": 3,
            "random_seed": "repeatable-seed",
        },
    )
    assert random_batch.status_code == 202, random_batch.text
    assert random_batch.json()["document_count"] == 3
    coding_groups = client.post(
        f"{base}/{random_batch.json()['id']}/coding-groups",
        headers=auth(root_token),
        json={"coding_group_ids": [str(group_id)]},
    )
    assert coding_groups.status_code == 200, coding_groups.text
    assert [group["source_metadata_group_id"] for group in coding_groups.json()["coding_groups"]] == [str(group_id)]
    repeated_assignment = client.post(
        f"{base}/{random_batch.json()['id']}/coding-groups",
        headers=auth(root_token),
        json={"coding_group_ids": [str(group_id)]},
    )
    assert repeated_assignment.status_code == 200, repeated_assignment.text
    assert len(repeated_assignment.json()["coding_groups"]) == 1
    removed_assignment = client.put(
        f"{base}/{random_batch.json()['id']}/coding-groups",
        headers=auth(root_token),
        json={"coding_group_ids": []},
    )
    assert removed_assignment.status_code == 200, removed_assignment.text
    assert removed_assignment.json()["coding_groups"] == []

    with TestingSessionLocal() as db:
        comparison_task = MatterAnalysisTask(
            matter_id=uuid.UUID(matter_id),
            key=f"comparison-{uuid.uuid4().hex[:12]}",
            name="Current comparison task",
            task_type="QUESTION_ANSWERING",
            workflow_key="matter_analysis_task_batch_v1",
            current_version=1,
            published_version=1,
            status="ACTIVE",
            created_by_user_id=root_admin.id,
        )
        db.add(comparison_task)
        db.flush()
        workflow = WorkflowRun(
            tenant_id=root_admin.tenant_id,
            matter_id=uuid.UUID(matter_id),
            workflow_key="comparison_test",
            code_version="1",
            dbos_workflow_id=f"comparison:{uuid.uuid4()}",
            status="COMPLETED",
            input_snapshot={},
            binding_snapshot={},
            configuration_snapshot={},
            progress={},
            initiated_by_user_id=root_admin.id,
        )
        db.add(workflow)
        db.flush()
        analysis_source = ReviewBatchRun(
            review_batch_id=uuid.UUID(batch["id"]),
            run_type="WORKFLOW",
            purpose="REVIEW",
            status="COMPLETED",
            result_policy="ISOLATED",
            workflow_run_record_id=workflow.id,
            configuration_snapshot={
                "mode": "BATCH",
                "task_id": str(comparison_task.id),
                "task_name": "Stale comparison task name",
            },
            initiated_by_user_id=root_admin.id,
            processed_document_count=1,
        )
        db.add(analysis_source)
        db.flush()
        db.add(
            ReviewBatchRunDocument(
                review_batch_run_id=analysis_source.id,
                matter_document_id=uuid.UUID(document_ids[0]),
                status="COMPLETED",
            )
        )
        db.add(
            ReviewBatchRunValue(
                review_batch_run_id=analysis_source.id,
                matter_document_id=uuid.UUID(document_ids[0]),
                metadata_definition_id=definition_id,
                value_ordinal=0,
                value_boolean=True,
            )
        )
        db.commit()
        analysis_source_id = str(analysis_source.id)
        additional_source_ids = []
        for source_number, source_value in [(2, True), (3, False)]:
            additional_workflow = WorkflowRun(
                tenant_id=root_admin.tenant_id,
                matter_id=uuid.UUID(matter_id),
                workflow_key=f"comparison_test_{source_number}",
                code_version="1",
                dbos_workflow_id=f"comparison:{uuid.uuid4()}",
                status="COMPLETED",
                input_snapshot={},
                binding_snapshot={},
                configuration_snapshot={},
                progress={},
                initiated_by_user_id=root_admin.id,
            )
            db.add(additional_workflow)
            db.flush()
            additional_source = ReviewBatchRun(
                review_batch_id=uuid.UUID(batch["id"]),
                run_type="WORKFLOW",
                purpose="REVIEW",
                status="COMPLETED",
                result_policy="ISOLATED",
                workflow_run_record_id=additional_workflow.id,
                configuration_snapshot={"mode": "BATCH", "task_name": f"Comparison source {source_number}"},
                initiated_by_user_id=root_admin.id,
                processed_document_count=1,
            )
            db.add(additional_source)
            db.flush()
            db.add(
                ReviewBatchRunDocument(
                    review_batch_run_id=additional_source.id,
                    matter_document_id=uuid.UUID(document_ids[0]),
                    status="COMPLETED",
                )
            )
            db.add(
                ReviewBatchRunValue(
                    review_batch_run_id=additional_source.id,
                    matter_document_id=uuid.UUID(document_ids[0]),
                    metadata_definition_id=definition_id,
                    value_ordinal=0,
                    value_boolean=source_value,
                )
            )
            additional_source_ids.append(str(additional_source.id))
        db.commit()

    comparison_options = client.get(
        f"{base}/{batch['id']}/comparison-options",
        headers=auth(root_token),
    )
    assert comparison_options.status_code == 200, comparison_options.text
    assert {item["id"] for item in comparison_options.json()["sources"]} >= {
        reviewer_coding.json()["id"],
        analysis_source_id,
    }
    assert next(
        item for item in comparison_options.json()["sources"] if item["id"] == analysis_source_id
    )["label"] == "Current comparison task"
    assert any(
        item["id"] == str(definition_id) and item["display_name"] == "Key document"
        for item in comparison_options.json()["fields"]
    )

    coding_comparison = client.post(
        f"{base}/{batch['id']}/coding-comparison",
        headers=auth(root_token),
        json={
            "metadata_definition_id": str(definition_id),
            "source_ids": [reviewer_coding.json()["id"], analysis_source_id],
            "row_filter": "ALL",
            "offset": 0,
            "limit": 10,
        },
    )
    assert coding_comparison.status_code == 200, coding_comparison.text
    comparison_payload = coding_comparison.json()
    assert comparison_payload["document_count"] == len(document_ids)
    assert comparison_payload["filtered_document_count"] == len(document_ids)
    assert comparison_payload["multi_source_agreement"] == {
        "compared_document_count": 1,
        "incomplete_document_count": len(document_ids) - 1,
        "class_agreement": [
            {
                "value_key": "true",
                "display_label": "Yes",
                "source_counts": [
                    {
                        "source_id": reviewer_coding.json()["id"],
                        "document_count": 1,
                    },
                    {
                        "source_id": analysis_source_id,
                        "document_count": 1,
                    },
                ],
                "all_selected_count": 1,
                "any_selected_count": 1,
                "jaccard": 1.0,
            }
        ],
    }
    assert comparison_payload["pairwise_agreements"] == [
        {
            "left_source_id": reviewer_coding.json()["id"],
            "right_source_id": analysis_source_id,
            "compared_document_count": 1,
            "exact_match_count": 1,
            "partial_match_count": 0,
            "disagreement_count": 0,
            "incomplete_document_count": len(document_ids) - 1,
            "exact_agreement": 1.0,
            "mean_jaccard": 1.0,
            "confusion_matrix": [{"left_value": "true", "right_value": "true", "document_count": 1}],
            "class_agreement": [],
        }
    ]
    assert comparison_payload["documents"][0]["sources"][0]["display_values"] == ["Yes"]
    assert comparison_payload["documents"][1]["agreement_status"] == "INCOMPLETE"

    four_source_comparison = client.post(
        f"{base}/{batch['id']}/coding-comparison",
        headers=auth(root_token),
        json={
            "metadata_definition_id": str(definition_id),
            "source_ids": [
                reviewer_coding.json()["id"],
                analysis_source_id,
                *additional_source_ids,
            ],
            "row_filter": "ALL",
            "offset": 0,
            "limit": 10,
        },
    )
    assert four_source_comparison.status_code == 200, four_source_comparison.text
    four_source_overlap = four_source_comparison.json()["multi_source_agreement"]
    assert four_source_overlap["compared_document_count"] == 1
    assert four_source_overlap["incomplete_document_count"] == len(document_ids) - 1
    assert four_source_overlap["class_agreement"] == [
        {
            "value_key": "false",
            "display_label": "No",
            "source_counts": [
                {"source_id": reviewer_coding.json()["id"], "document_count": 0},
                {"source_id": analysis_source_id, "document_count": 0},
                {"source_id": additional_source_ids[0], "document_count": 0},
                {"source_id": additional_source_ids[1], "document_count": 1},
            ],
            "all_selected_count": 0,
            "any_selected_count": 1,
            "jaccard": 0.0,
        },
        {
            "value_key": "true",
            "display_label": "Yes",
            "source_counts": [
                {"source_id": reviewer_coding.json()["id"], "document_count": 1},
                {"source_id": analysis_source_id, "document_count": 1},
                {"source_id": additional_source_ids[0], "document_count": 1},
                {"source_id": additional_source_ids[1], "document_count": 0},
            ],
            "all_selected_count": 0,
            "any_selected_count": 1,
            "jaccard": 0.0,
        },
    ]

    comparison_export = client.get(
        f"{base}/{batch['id']}/coding-comparison/export",
        headers=auth(root_token),
        params={
            "metadata_definition_id": str(definition_id),
            "source_id": [reviewer_coding.json()["id"], analysis_source_id],
            "row_filter": "ALL",
            "format": "csv",
        },
    )
    assert comparison_export.status_code == 200, comparison_export.text
    assert comparison_export.headers["content-type"].startswith("text/csv")
    assert "matter_document_id" in comparison_export.text
    assert "[NOT EVALUATED]" in comparison_export.text
    parquet_export = client.get(
        f"{base}/{batch['id']}/coding-comparison/export",
        headers=auth(root_token),
        params={
            "metadata_definition_id": str(definition_id),
            "source_id": [reviewer_coding.json()["id"], analysis_source_id],
            "row_filter": "DISAGREEMENT",
            "format": "parquet",
        },
    )
    assert parquet_export.status_code == 200, parquet_export.text
    assert parquet_export.headers["content-type"] == "application/vnd.apache.parquet"
    assert parquet_export.content.startswith(b"PAR1")

    statistics = client.get(f"{base}/{batch['id']}/statistics", headers=auth(root_token))
    assert statistics.status_code == 200, statistics.text
    assert statistics.json()["can_promote"] is True
    left_statistics = next(
        item
        for item in statistics.json()["reviewers"]
        if item["reviewer_coding_id"] == reviewer_coding.json()["id"]
    )
    assert left_statistics["reviewer"] == {
        "id": str(root_admin.id),
        "display_name": root_admin.display_name,
        "email": root_admin.email,
    }
    assert left_statistics["completed_document_count"] == 1
    key_document_statistics = next(
        item for item in left_statistics["field_counts"] if item["metadata_definition_id"] == str(definition_id)
    )
    assert key_document_statistics == {
        "coding_group_id": batch["coding_groups"][0]["id"],
        "coding_group_name": batch["coding_groups"][0]["display_name"],
        "metadata_definition_id": str(definition_id),
        "field_key": "key_document",
        "field_display_name": "Key document",
        "document_count": 1,
    }

    queued_promotion = client.post(
        f"{base}/{batch['id']}/reviewer-coding/{reviewer_coding.json()['id']}/promotion",
        headers=auth(root_token),
    )
    assert queued_promotion.status_code == 202, queued_promotion.text
    assert queued_promotion.json()["matter_analysis_task_version_id"] is None
    assert queued_promotion.json()["reviewer_coding_revision"] == 1
    assert queued_promotion.json()["execution"]["job_type"] == "REVIEW_BATCH_CODING_PROMOTION"
    assert queued_promotion.json()["execution"]["status"] == "QUEUED"
    promotion_id = uuid.UUID(queued_promotion.json()["id"])
    with TestingSessionLocal() as db:
        eligible = prepare_analysis_task_promotion(db, promotion_id)
        assert eligible == [document_ids[0]]
        apply_analysis_task_promotion_chunk(db, promotion_id, [uuid.UUID(document_ids[0])])
        complete_analysis_task_promotion(db, promotion_id)
        promotion = db.get(ReviewBatchRunPromotion, promotion_id)
        assert promotion is not None
        assert promotion.status == "COMPLETED"
        assert promotion.current_workflow_run is not None
        assert promotion.current_workflow_run.status == "COMPLETED"
        assert promotion.skipped_document_count == len(document_ids) - 1
        current = db.scalar(
            select(DocumentMetadataCurrent).where(
                DocumentMetadataCurrent.matter_document_id == uuid.UUID(document_ids[0]),
                DocumentMetadataCurrent.metadata_definition_id == definition_id,
            )
        )
        assert current is not None
        assert current.value_boolean is True

    revised_review = client.put(
        f"{base}/{batch['id']}/reviewer-coding/documents/{document_ids[0]}/values",
        headers=auth(root_token),
        json={
            "matter_document_id": document_ids[0],
            "fields": [{"metadata_definition_id": str(definition_id), "values": [False]}],
        },
    )
    assert revised_review.status_code == 200, revised_review.text
    revised_history = client.get(
        f"{base}/{batch['id']}/documents/{document_ids[0]}/coding-history",
        headers=auth(root_token),
    )
    assert revised_history.status_code == 200, revised_history.text
    human_value_history = [
        entry
        for entry in revised_history.json()
        if entry["source_kind"] == "HUMAN" and entry["event_kind"] == "VALUE"
    ]
    assert [(entry["reviewer_revision"], entry["value"]) for entry in human_value_history] == [
        (2, False),
        (1, True),
    ]
    revised_promotion = client.post(
        f"{base}/{batch['id']}/reviewer-coding/{reviewer_coding.json()['id']}/promotion",
        headers=auth(root_token),
    )
    assert revised_promotion.status_code == 202, revised_promotion.text
    assert revised_promotion.json()["id"] != queued_promotion.json()["id"]
    assert revised_promotion.json()["reviewer_coding_revision"] == 2

    imported_clear = {
        "matter_document_id": document_ids[0],
        "fields": [{"metadata_definition_id": str(definition_id), "values": []}],
        "origin": "IMPORT",
        "request_key": "test-import-clear",
    }
    for _ in range(2):
        cleared = client.put(
            f"{base}/{batch['id']}/reviewer-coding/documents/{document_ids[0]}/values",
            headers=auth(root_token),
            json=imported_clear,
        )
        assert cleared.status_code == 200, cleared.text
    after_clear = client.post(f"{base}/{batch['id']}/reviewer-coding", headers=auth(root_token))
    assert after_clear.json()["revision"] == 3
    cleared_history = client.get(
        f"{base}/{batch['id']}/documents/{document_ids[0]}/coding-history",
        headers=auth(root_token),
    ).json()
    no_value = next(entry for entry in cleared_history if entry["event_kind"] == "NO_VALUE")
    assert no_value["reviewer_revision"] == 3
    assert no_value["reviewer_origin"] == "IMPORT"
    assert no_value["value_label"] == "No value"

    for _ in range(2):
        skipped = client.post(
            f"{base}/{batch['id']}/reviewer-coding/documents/{document_ids[0]}/skip",
            headers=auth(root_token),
            params={"origin": "IMPORT", "request_key": "test-import-skip"},
        )
        assert skipped.status_code == 200, skipped.text
    after_skip = client.post(f"{base}/{batch['id']}/reviewer-coding", headers=auth(root_token))
    assert after_skip.json()["revision"] == 4
    skipped_history = client.get(
        f"{base}/{batch['id']}/documents/{document_ids[0]}/coding-history",
        headers=auth(root_token),
    ).json()
    latest_status = next(entry for entry in skipped_history if entry["event_kind"] == "STATUS")
    assert latest_status["reviewer_revision"] == 4
    assert latest_status["document_status"] == "SKIPPED"


def test_batch_selection_validation(client: TestClient, root_token: str, root_admin) -> None:
    matter_id = create_matter(client, root_token, str(root_admin.tenant_id))
    response = client.post(
        f"/v1/matters/{matter_id}/review-batches",
        headers=auth(root_token),
        json={"name": "Bad search batch", "selection_type": "SEARCH_QUERY"},
    )
    assert response.status_code == 422
    missing_saved_search = client.post(
        f"/v1/matters/{matter_id}/review-batches",
        headers=auth(root_token),
        json={"name": "Missing saved search", "selection_type": "RANDOM_SAVED_SEARCH", "sample_size": 10},
    )
    assert missing_saved_search.status_code == 422
    missing_sample_size = client.post(
        f"/v1/matters/{matter_id}/review-batches",
        headers=auth(root_token),
        json={
            "name": "Missing sample size",
            "selection_type": "RANDOM_SAVED_SEARCH",
            "saved_search_id": str(uuid.uuid4()),
        },
    )
    assert missing_sample_size.status_code == 422


def test_random_saved_search_batch_snapshots_and_samples_matching_documents(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch,
) -> None:
    matter_id = create_matter(client, root_token, str(root_admin.tenant_id))
    document_ids = add_documents(matter_id, root_admin.id, count=5)
    eligible_ids = document_ids[:4]
    with TestingSessionLocal() as db:
        generation = SearchIndexGeneration(
            matter_id=uuid.UUID(matter_id),
            generation=1,
            index_name=f"matter-{matter_id}-000001",
            alias_name=f"matter-{matter_id}",
            schema_hash="a" * 64,
            status="ACTIVE",
            document_count=len(document_ids),
            schema_snapshot={},
            activated_at=datetime.now(timezone.utc),
        )
        db.add(generation)
        db.commit()
        generation_id = str(generation.id)

    class FakeOpenSearchClient:
        def __init__(self, _settings):
            pass

        def search(self, index_name, body):
            assert index_name == f"matter-{matter_id}-000001"
            assert body["size"] == 500
            return {
                "hits": {
                    "hits": [
                        {"_source": {"document_id": document_id}, "sort": [document_id]} for document_id in eligible_ids
                    ]
                }
            }

        def close(self):
            pass

    monkeypatch.setattr("app.review_batches.OpenSearchClient", FakeOpenSearchClient)
    saved = client.post(
        f"/v1/matters/{matter_id}/saved-searches",
        headers=auth(root_token),
        json={
            "name": "Insurance correspondence",
            "visibility": "PRIVATE",
            "search": {"query": "insurance", "search_mode": "KEYWORD"},
        },
    )
    assert saved.status_code == 201, saved.text
    seed = "saved-search-sample"
    created = client.post(
        f"/v1/matters/{matter_id}/review-batches",
        headers=auth(root_token),
        json={
            "name": "Saved search sample",
            "selection_type": "RANDOM_SAVED_SEARCH",
            "saved_search_id": saved.json()["id"],
            "sample_size": 2,
            "random_seed": seed,
        },
    )
    assert created.status_code == 202, created.text
    batch = created.json()
    assert batch["status"] == "READY"
    assert batch["document_count"] == 2
    assert batch["sample_size"] == 2
    assert batch["random_seed"] == seed
    assert batch["search_index_generation_id"] == generation_id
    assert batch["selection_definition"]["saved_search_id"] == saved.json()["id"]
    assert batch["selection_definition"]["saved_search_name"] == "Insurance correspondence"
    assert batch["selection_definition"]["search"]["query"] == "insurance"
    assert batch["selection_definition"]["search_index_generation"]["generation"] == 1

    expected = sorted(
        eligible_ids,
        key=lambda document_id: hashlib.sha256(f"{seed}:{document_id}".encode()).digest(),
    )[:2]
    with TestingSessionLocal() as db:
        actual = [
            str(document_id)
            for document_id in db.scalars(
                select(ReviewBatchDocument.matter_document_id)
                .where(ReviewBatchDocument.review_batch_id == uuid.UUID(batch["id"]))
                .order_by(ReviewBatchDocument.sequence_number)
            )
        ]
    assert actual == expected


def test_workflow_review_run_has_workflow_owner(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_matter(client, root_token, str(root_admin.tenant_id))
    add_documents(matter_id, root_admin.id, count=1)
    base = f"/v1/matters/{matter_id}/review-batches"
    created = client.post(
        base,
        headers=auth(root_token),
        json={"name": "Assessment batch", "selection_type": "ALL_MATTER"},
    )
    assert created.status_code == 202, created.text
    batch_id = uuid.UUID(created.json()["id"])

    with TestingSessionLocal() as db:
        workflow_run = WorkflowRun(
            tenant_id=root_admin.tenant_id,
            matter_id=uuid.UUID(matter_id),
            workflow_key="matter_definition_assessment_v1",
            code_version="1",
            dbos_workflow_id=f"test-assessment:{uuid.uuid4()}",
            status="QUEUED",
            input_snapshot={"matter_id": matter_id},
            binding_snapshot={},
            configuration_snapshot={},
            progress={},
            initiated_by_user_id=root_admin.id,
        )
        db.add(workflow_run)
        db.flush()
        run = ReviewBatchRun(
            review_batch_id=batch_id,
            run_type="WORKFLOW",
            purpose="ASSESSMENT",
            status="COMPLETED_WITH_ERRORS",
            result_policy="ISOLATED",
            actor_user_id=None,
            agent_definition_version_id=None,
            workflow_run_record_id=workflow_run.id,
            configuration_snapshot={},
            initiated_by_user_id=root_admin.id,
        )
        db.add(run)
        db.commit()
        run_id = run.id
        workflow_run_id = workflow_run.id

    listed = client.get(f"{base}/{batch_id}/runs", headers=auth(root_token))
    assert listed.status_code == 200, listed.text
    payload = next(item for item in listed.json() if item["id"] == str(run_id))
    assert payload["run_type"] == "WORKFLOW"
    assert payload["purpose"] == "ASSESSMENT"
    assert payload["workflow_run_record_id"] == str(workflow_run_id)
    assert payload["actor_user_id"] is None
    assert payload["agent_definition_version_id"] is None

    comparison_options = client.get(f"{base}/{batch_id}/comparison-options", headers=auth(root_token))
    assert comparison_options.status_code == 200, comparison_options.text
    assert str(run_id) not in {item["id"] for item in comparison_options.json()["sources"]}

    with TestingSessionLocal() as db:
        db.add(
            JobArchive(
                job_type="ANALYSIS_TASK_BATCH_RUN",
                job_id=run_id,
                tenant_id=root_admin.tenant_id,
                matter_id=uuid.UUID(matter_id),
                archived_by_user_id=root_admin.id,
            )
        )
        db.commit()

    listed_after_archive = client.get(f"{base}/{batch_id}/runs", headers=auth(root_token))
    assert listed_after_archive.status_code == 200, listed_after_archive.text
    archived_payload = next(item for item in listed_after_archive.json() if item["id"] == str(run_id))
    assert archived_payload["archived"] is True
    assert archived_payload["archived_at"] is not None

    interactive = client.post(
        f"{base}/{batch_id}/runs",
        headers=auth(root_token),
        json={"run_type": "WORKFLOW", "purpose": "ASSESSMENT"},
    )
    assert interactive.status_code == 422


def test_batch_search_injects_authoritative_membership_filter(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch,
) -> None:
    matter_id = create_matter(client, root_token, str(root_admin.tenant_id))
    document_id = add_documents(matter_id, root_admin.id, count=1)[0]
    base = f"/v1/matters/{matter_id}/review-batches"
    created = client.post(
        base,
        headers=auth(root_token),
        json={"name": "Searchable batch", "selection_type": "ALL_MATTER"},
    )
    assert created.status_code == 202, created.text
    batch_id = created.json()["id"]
    with TestingSessionLocal() as db:
        batch = db.get(ReviewBatch, uuid.UUID(batch_id))
        assert batch is not None
        batch.search_status = "READY"
        db.add(
            SearchIndexGeneration(
                matter_id=uuid.UUID(matter_id),
                generation=1,
                index_name=f"test-{matter_id}-v1",
                alias_name=f"test-{matter_id}",
                schema_hash="a" * 64,
                status="ACTIVE",
                document_count=1,
                schema_snapshot={
                    "mappings": {"properties": {"document_id": {"type": "keyword"}}}
                },
                activated_at=datetime.now(timezone.utc),
            )
        )
        db.commit()

    captured: dict = {}

    def fake_search(_matter, _payload, **kwargs):
        captured.update(kwargs)
        return MatterSearchResponse(total=0, took_ms=1, timed_out=False, hits=[], facets={})

    monkeypatch.setattr("app.routers.review_batches.execute_matter_search", fake_search)
    response = client.post(
        f"{base}/{batch_id}/search",
        headers=auth(root_token),
        json={"query": "agreement", "search_mode": "KEYWORD"},
    )
    assert response.status_code == 200, response.text
    assert captured["required_filters"] == [{"term": {"batch_ids": batch_id}}]

    with TestingSessionLocal() as db:
        definition = db.scalar(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == uuid.UUID(matter_id),
                MetadataDefinition.key == "key_document",
            )
        )
        assert definition is not None
        responsiveness_definition = db.scalar(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == uuid.UUID(matter_id),
                MetadataDefinition.key == "responsiveness",
            )
        )
        assert responsiveness_definition is not None
        workflow = WorkflowRun(
            tenant_id=root_admin.tenant_id,
            matter_id=uuid.UUID(matter_id),
            workflow_key="search_projection_test",
            code_version="1",
            dbos_workflow_id=f"search-projection:{uuid.uuid4()}",
            status="COMPLETED",
            input_snapshot={},
            binding_snapshot={},
            configuration_snapshot={},
            progress={},
            initiated_by_user_id=root_admin.id,
        )
        db.add(workflow)
        db.flush()
        run = ReviewBatchRun(
            review_batch_id=uuid.UUID(batch_id),
            run_type="WORKFLOW",
            purpose="REFERENCE",
            status="COMPLETED",
            result_policy="ISOLATED",
            workflow_run_record_id=workflow.id,
            configuration_snapshot={"mode": "BATCH"},
            initiated_by_user_id=root_admin.id,
            processed_document_count=1,
        )
        db.add(run)
        db.flush()
        db.add(
            ReviewBatchRunValue(
                review_batch_run_id=run.id,
                matter_document_id=uuid.UUID(document_id),
                metadata_definition_id=definition.id,
                value_ordinal=0,
                value_boolean=True,
                confidence=0.91,
                confidence_kind="SELECTED_PROBABILITY",
            )
        )
        db.add(
            ReviewBatchRunValue(
                review_batch_run_id=run.id,
                matter_document_id=uuid.UUID(document_id),
                metadata_definition_id=responsiveness_definition.id,
                value_ordinal=0,
                value_text="responsive",
                confidence=0.86,
                confidence_kind="SELECTED_PROBABILITY",
            )
        )
        db.add(
            ReviewBatchSearchCodingRun(
                review_batch_id=uuid.UUID(batch_id),
                review_batch_run_id=run.id,
                status="READY",
                selected_by_user_id=root_admin.id,
            )
        )
        db.commit()
        run_id = str(run.id)
        definition_id = str(definition.id)
        responsiveness_definition_id = str(responsiveness_definition.id)

    response = client.post(
        f"{base}/{batch_id}/search",
        headers=auth(root_token),
        json={
            "search_mode": "KEYWORD",
            "coding_filters": [{
                "field": "key_document",
                "values": [True],
                "minimum_confidence": 0.8,
                "maximum_confidence": 0.95,
            }],
        },
    )
    assert response.status_code == 200, response.text
    coding_filter = captured["required_filters"][1]["nested"]
    assert coding_filter["path"] == "batch_coding"
    assert coding_filter["query"]["bool"]["filter"] == [
        {"term": {"batch_coding.batch_id": batch_id}},
        {"term": {"batch_coding.run_id": run_id}},
        {"term": {"batch_coding.field_id": definition_id}},
        {"terms": {"batch_coding.value_boolean": [True]}},
        {"range": {"batch_coding.confidence": {"gte": 0.8, "lt": 0.95}}},
    ]

    monkeypatch.setattr("app.routers.review_batches.enqueue_search_export", lambda *_: None)
    export = client.post(
        f"{base}/{batch_id}/search-exports",
        headers=auth(root_token),
        json={
            "search": {
                "search_mode": "KEYWORD",
                "coding_filters": [{
                    "field": "key_document",
                    "values": [True],
                    "minimum_confidence": 0.8,
                    "maximum_confidence": 0.95,
                }],
            },
            "field_ids": ["document_id"],
            "requested_filename": "batch.csv",
            "idempotency_key": "batch-export-1",
        },
    )
    assert export.status_code == 202, export.text
    with TestingSessionLocal() as db:
        export_job = db.get(MatterSearchExportJob, uuid.UUID(export.json()["id"]))
        assert export_job is not None
        assert export_job.search_index_snapshot["required_filters"] == captured["required_filters"]
        assert export_job.search_index_snapshot["scope"] == {
            "kind": "REVIEW_BATCH",
            "review_batch_id": batch_id,
            "review_batch_name": "Searchable batch",
            "topic_keys": [],
            "topic_taxonomy_id": None,
            "batch_coding_run_id": run_id,
        }
    facet_capture: dict = {}

    def fake_coding_facets(_matter, _payload, **kwargs):
        facet_capture.update(kwargs)
        return MatterFacetValuesResponse(
            field="key_document",
            values=[{"value": True, "count": 1}],
        )

    monkeypatch.setattr(
        "app.routers.review_batches.execute_matter_batch_coding_facets",
        fake_coding_facets,
    )
    response = client.post(
        f"{base}/{batch_id}/coding-facets/key_document/values",
        headers=auth(root_token),
        json={
            "search_mode": "KEYWORD",
            "coding_filters": [{
                "field": "key_document",
                "values": [True],
                "minimum_confidence": 0.8,
                "maximum_confidence": 0.95,
            }, {"field": "responsiveness", "values": ["responsive"]}],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["values"] == [
        {
            "value": True,
            "count": 1,
            "label": None,
            "parent": None,
            "has_children": False,
        }
    ]
    assert facet_capture["required_filters"][1]["nested"]["query"]["bool"]["filter"][:3] == [
        {"term": {"batch_coding.batch_id": batch_id}},
        {"term": {"batch_coding.run_id": run_id}},
        {"term": {"batch_coding.field_id": responsiveness_definition_id}},
    ]
    assert facet_capture["minimum_confidence"] == 0.8
    assert facet_capture["maximum_confidence"] == 0.95


def test_selecting_a_retained_batch_coding_run_reuses_its_projection(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_matter(client, root_token, str(root_admin.tenant_id))
    document_id = add_documents(matter_id, root_admin.id, count=1)[0]
    base = f"/v1/matters/{matter_id}/review-batches"
    created = client.post(
        base,
        headers=auth(root_token),
        json={"name": "Retained projections", "selection_type": "ALL_MATTER"},
    )
    assert created.status_code == 202, created.text
    batch_id = uuid.UUID(created.json()["id"])

    with TestingSessionLocal() as db:
        batch = db.get(ReviewBatch, batch_id)
        assert batch is not None
        batch.search_status = "READY"
        generation = SearchIndexGeneration(
            matter_id=uuid.UUID(matter_id),
            generation=1,
            index_name=f"test-{matter_id}-v1",
            alias_name=f"test-{matter_id}",
            schema_hash="a" * 64,
            status="ACTIVE",
            document_count=1,
            schema_snapshot={},
            activated_at=datetime.now(timezone.utc),
        )
        db.add(generation)
        db.flush()
        batch.search_index_generation_id = generation.id
        definition = db.scalar(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == uuid.UUID(matter_id),
                MetadataDefinition.key == "responsiveness",
            )
        )
        assert definition is not None
        workflows = [
            WorkflowRun(
                tenant_id=root_admin.tenant_id,
                matter_id=uuid.UUID(matter_id),
                workflow_key="retained_projection_test",
                code_version="1",
                dbos_workflow_id=f"retained-projection:{uuid.uuid4()}",
                status="COMPLETED",
                input_snapshot={},
                binding_snapshot={},
                configuration_snapshot={},
                progress={},
                initiated_by_user_id=root_admin.id,
            )
            for _ in range(2)
        ]
        db.add_all(workflows)
        db.flush()
        runs = [
            ReviewBatchRun(
                review_batch_id=batch.id,
                run_type="WORKFLOW",
                purpose="REFERENCE",
                status="COMPLETED",
                result_policy="ISOLATED",
                workflow_run_record_id=workflow.id,
                configuration_snapshot={"mode": "BATCH"},
                initiated_by_user_id=root_admin.id,
                processed_document_count=1,
            )
            for workflow in workflows
        ]
        db.add_all(runs)
        db.flush()
        db.add_all(
            ReviewBatchRunValue(
                review_batch_run_id=run.id,
                matter_document_id=uuid.UUID(document_id),
                metadata_definition_id=definition.id,
                value_ordinal=0,
                value_text="responsive",
            )
            for run in runs
        )
        db.add(
            ReviewBatchSearchCodingProjection(
                review_batch_id=batch.id,
                review_batch_run_id=runs[0].id,
                search_index_generation_id=generation.id,
                status="READY",
                requested_by_user_id=root_admin.id,
                projected_at=datetime.now(timezone.utc),
            )
        )
        db.add(
            ReviewBatchSearchCodingRun(
                review_batch_id=batch.id,
                review_batch_run_id=runs[1].id,
                status="READY",
                selected_by_user_id=root_admin.id,
                projected_at=datetime.now(timezone.utc),
            )
        )
        operation_count = db.query(SearchProjectionOperation).count()
        retained_run_id = runs[0].id
        other_run_id = runs[1].id
        db.commit()

    response = client.post(
        f"{base}/{batch_id}/runs/{retained_run_id}/coding-search",
        headers=auth(root_token),
    )
    assert response.status_code == 202, response.text
    assert response.json()["review_batch_run_id"] == str(retained_run_id)
    assert response.json()["status"] == "READY"

    with TestingSessionLocal() as db:
        selection = db.get(ReviewBatchSearchCodingRun, batch_id)
        assert selection is not None
        assert selection.review_batch_run_id == retained_run_id
        assert selection.status == "READY"
        assert db.query(SearchProjectionOperation).count() == operation_count

        selection.review_batch_run_id = other_run_id
        selection.status = "QUEUED"
        selection.projected_at = None
        db.add(
            ReviewBatchSearchCodingProjection(
                review_batch_id=batch_id,
                review_batch_run_id=other_run_id,
                status="QUEUED",
                requested_by_user_id=root_admin.id,
            )
        )
        blocked = SearchProjectionOperation(
            matter_id=uuid.UUID(matter_id),
            kind="BATCH_CODING_SYNC",
            payload={
                "batch_id": str(batch_id),
                "run_id": str(other_run_id),
                "schema_change": {"action": "REINDEX_REQUIRED", "reasons": ["Unrelated field"]},
            },
            status="AWAITING_USER",
            workflow_id=f"search-projection:{uuid.uuid4()}",
            created_by_user_id=root_admin.id,
        )
        db.add(blocked)
        db.commit()
        blocked_id = blocked.id

    response = client.get(f"{base}/{batch_id}", headers=auth(root_token))
    assert response.status_code == 200, response.text
    assert response.json()["searchable_coding_status"] == "FAILED"
    assert "unrelated search schema change" in response.json()["searchable_coding_error_message"]

    response = client.post(
        f"{base}/{batch_id}/runs/{other_run_id}/coding-search",
        headers=auth(root_token),
    )
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "QUEUED"

    with TestingSessionLocal() as db:
        blocked = db.get(SearchProjectionOperation, blocked_id)
        assert blocked is not None and blocked.status == "FAILED"
        replacement = db.scalar(
            select(SearchProjectionOperation)
            .where(
                SearchProjectionOperation.payload["batch_id"].as_string() == str(batch_id),
                SearchProjectionOperation.payload["run_id"].as_string() == str(other_run_id),
                SearchProjectionOperation.status == "QUEUED",
            )
            .order_by(SearchProjectionOperation.created_at.desc())
        )
        assert replacement is not None
