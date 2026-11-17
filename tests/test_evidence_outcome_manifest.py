from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from app.artifact_gateway import WorkflowArtifactReference
from app.evidence_outcome_manifest import (
    ANALYSIS_OUTCOME_MANIFEST_SCHEMA_VERSION,
    build_evidence_outcome_manifest,
    load_outcome_manifest_bytes,
    outcome_manifest_bytes,
)
from artifact_service.workflow_artifact_types import ArtifactType
from artifact_service.workflow_artifacts import allowed_media_types, workflow_artifact_max_bytes


def _id(value: int) -> uuid.UUID:
    return uuid.UUID(int=value)


def _workflow(
    value: int,
    *,
    job_type: str,
    document_id: uuid.UUID | None = None,
    status: str = "COMPLETED",
    progress: dict | None = None,
    requests: int = 0,
) -> SimpleNamespace:
    now = datetime(2026, 11, 16, value % 23, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=_id(value),
        job_type=job_type,
        input_snapshot={"matter_document_id": str(document_id)} if document_id else {},
        status=status,
        progress=progress or {},
        failure_retriable=status in {"FAILED", "COMPLETED_WITH_ERRORS"},
        failure_code="OPTIONAL_NARRATIVE_FAILED" if status == "COMPLETED_WITH_ERRORS" else None,
        failure_summary="Optional narrative did not complete" if status == "COMPLETED_WITH_ERRORS" else None,
        error_message=None,
        request_count=requests,
        input_tokens=requests * 100,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=requests * 10,
        created_at=now,
        updated_at=now,
    )


def _artifact(
    value: int,
    *,
    artifact_type: ArtifactType,
    producer_run_id: uuid.UUID,
    document_id: uuid.UUID,
    metadata: dict | None = None,
) -> WorkflowArtifactReference:
    return WorkflowArtifactReference(
        artifact_id=_id(value),
        tenant_id=_id(1),
        client_id=_id(2),
        core_scope_kind="MATTER",
        core_scope_id=_id(3),
        artifact_type=artifact_type,
        schema_version=1,
        derivation_key="a" * 64,
        content_hash="b" * 64,
        media_type="application/json",
        byte_length=100,
        producer_kind="CORE",
        producer_run_id=producer_run_id,
        metadata={"matter_document_id": str(document_id), **(metadata or {})},
        created_at=datetime(2026, 11, 16, tzinfo=timezone.utc),
    )


def test_manifest_selects_narrative_then_deterministic_fallback() -> None:
    first, second, third = _id(101), _id(102), _id(103)
    extraction = _workflow(
        201,
        job_type="MATTER_DEFINITION_EVIDENCE_SHADOW",
        status="COMPLETED_WITH_ERRORS",
        progress={
            "documents": [
                {
                    "matter_document_id": str(first),
                    "status": "COMPLETED",
                    "unit_count": 2,
                    "missing_unit_count": 0,
                    "plan_artifact_id": str(_id(301)),
                    "observations_artifact_id": str(_id(302)),
                },
                {
                    "matter_document_id": str(second),
                    "status": "COMPLETED",
                    "unit_count": 1,
                    "missing_unit_count": 0,
                    "plan_artifact_id": str(_id(303)),
                    "observations_artifact_id": str(_id(304)),
                },
                {
                    "matter_document_id": str(third),
                    "status": "FAILED",
                    "unit_count": 3,
                    "missing_unit_count": 3,
                },
            ]
        },
        requests=3,
    )
    first_adjudication = _workflow(
        202,
        job_type="MATTER_DEFINITION_EVIDENCE_ADJUDICATION_SHADOW",
        document_id=first,
        progress={
            "completed_packet_count": 5,
            "failed_packet_count": 0,
            "document_decision_status": "COMPLETED",
            "document_decision_artifact_id": str(_id(305)),
            "compatibility_projection_status": "COMPLETED",
            "compatibility_result_artifact_id": str(_id(306)),
        },
        requests=2,
    )
    second_adjudication = _workflow(
        203,
        job_type="MATTER_DEFINITION_EVIDENCE_ADJUDICATION_SHADOW",
        document_id=second,
        progress={
            "completed_packet_count": 5,
            "failed_packet_count": 0,
            "document_decision_status": "COMPLETED",
            "document_decision_artifact_id": str(_id(307)),
            "compatibility_projection_status": "COMPLETED",
            "compatibility_result_artifact_id": str(_id(308)),
        },
        requests=1,
    )
    first_narrative = _workflow(
        204,
        job_type="MATTER_DEFINITION_EVIDENCE_NARRATIVE_SHADOW",
        document_id=first,
        progress={
            "narrative_status": "COMPLETED",
            "narrative_artifact_id": str(_id(309)),
        },
        requests=1,
    )
    second_narrative = _workflow(
        205,
        job_type="MATTER_DEFINITION_EVIDENCE_NARRATIVE_SHADOW",
        document_id=second,
        status="COMPLETED_WITH_ERRORS",
        progress={"narrative_status": "FAILED"},
        requests=1,
    )

    manifest = build_evidence_outcome_manifest(
        tenant_id=_id(1),
        client_id=_id(2),
        matter_id=_id(3),
        assessment_id=_id(4),
        document_ids=[first, second, third],
        extraction_workflows=[extraction],
        adjudication_workflows=[first_adjudication, second_adjudication],
        narrative_workflows=[first_narrative, second_narrative],
    )

    assert manifest.readiness_counts == {
        "ACTION_REQUIRED": 1,
        "READY": 1,
        "READY_WITH_FALLBACK": 1,
    }
    assert manifest.documents[0].selected_output_kind == "NARRATIVE_PRESENTATION"
    assert manifest.documents[0].selected_output_artifact_id == _id(309)
    assert manifest.documents[1].selected_output_kind == "DETERMINISTIC_COMPATIBILITY"
    assert manifest.documents[1].selected_output_artifact_id == _id(308)
    assert manifest.documents[1].retryable_stages == ("NARRATIVE",)
    assert manifest.documents[2].synthesis_readiness == "ACTION_REQUIRED"
    assert manifest.documents[2].retryable_stages == ("EXTRACTION",)
    assert manifest.usage.workflow_count == 5
    assert manifest.usage.request_count == 8
    assert load_outcome_manifest_bytes(outcome_manifest_bytes(manifest)) == manifest


def test_outcome_manifest_artifact_is_bounded_json() -> None:
    assert ANALYSIS_OUTCOME_MANIFEST_SCHEMA_VERSION == 1
    assert allowed_media_types(ArtifactType.ANALYSIS_OUTCOME_MANIFEST) == {
        "application/json"
    }
    assert workflow_artifact_max_bytes(ArtifactType.ANALYSIS_OUTCOME_MANIFEST) > 0


def test_manifest_explains_batch_failure_before_a_durable_workflow_exists() -> None:
    document_id = _id(351)
    failure = SimpleNamespace(
        status="FAILED",
        failure_code="EXTRACTION_DBOS_MAX_RETRIES",
        failure_message="Selected document has no preferred text source",
        failure_retriable=False,
    )

    manifest = build_evidence_outcome_manifest(
        tenant_id=_id(1),
        client_id=_id(2),
        matter_id=_id(3),
        assessment_id=_id(4),
        document_ids=[document_id],
        extraction_workflows=[],
        adjudication_workflows=[],
        narrative_workflows=[],
        document_failures={document_id: failure},
    )

    outcome = manifest.documents[0]
    assert outcome.synthesis_readiness == "ACTION_REQUIRED"
    assert outcome.extraction.status == "FAILED"
    assert outcome.extraction.error_code == "EXTRACTION_DBOS_MAX_RETRIES"
    assert outcome.extraction.error_summary == "Selected document has no preferred text source"
    assert outcome.extraction.failed_count == 1
    assert outcome.retryable_stages == ()


def test_manifest_keeps_queued_batch_document_pending_before_workflow_exists() -> None:
    document_id = _id(361)

    manifest = build_evidence_outcome_manifest(
        tenant_id=_id(1),
        client_id=_id(2),
        matter_id=_id(3),
        assessment_id=_id(4),
        document_ids=[document_id],
        extraction_workflows=[],
        adjudication_workflows=[],
        narrative_workflows=[],
        pending_document_ids={document_id},
    )

    outcome = manifest.documents[0]
    assert outcome.synthesis_readiness == "PENDING"
    assert outcome.extraction.status == "QUEUED"
    assert outcome.retryable_stages == ()


def test_manifest_reconciles_artifacts_from_pre_progress_workflows() -> None:
    document_id = _id(401)
    extraction = _workflow(
        402,
        job_type="MATTER_DEFINITION_EVIDENCE_SHADOW",
        progress={},
        requests=1,
    )
    adjudication = _workflow(
        403,
        job_type="MATTER_DEFINITION_EVIDENCE_ADJUDICATION_SHADOW",
        document_id=document_id,
        progress={},
        requests=1,
    )
    references = [
        _artifact(
            404,
            artifact_type=ArtifactType.ANALYSIS_OBSERVATIONS,
            producer_run_id=extraction.id,
            document_id=document_id,
            metadata={"evidence_complete": True, "selected_unit_count": 2},
        ),
        _artifact(
            405,
            artifact_type=ArtifactType.ANALYSIS_DOCUMENT_DECISION,
            producer_run_id=adjudication.id,
            document_id=document_id,
            metadata={"status": "COMPLETED"},
        ),
        _artifact(
            406,
            artifact_type=ArtifactType.ANALYSIS_COMPATIBILITY_RESULT,
            producer_run_id=adjudication.id,
            document_id=document_id,
            metadata={"status": "COMPLETED"},
        ),
    ]

    manifest = build_evidence_outcome_manifest(
        tenant_id=_id(1),
        client_id=_id(2),
        matter_id=_id(3),
        assessment_id=_id(4),
        document_ids=[document_id],
        extraction_workflows=[extraction],
        adjudication_workflows=[adjudication],
        narrative_workflows=[],
        artifact_references=references,
    )

    outcome = manifest.documents[0]
    assert outcome.extraction.status == "COMPLETED"
    assert outcome.compatibility_projection.status == "COMPLETED"
    assert outcome.selected_output_artifact_id == _id(406)
    assert outcome.synthesis_readiness == "READY"


def test_manifest_does_not_select_stale_outputs_while_retry_is_active() -> None:
    document_id = _id(501)
    extraction = _workflow(
        502,
        job_type="MATTER_DEFINITION_EVIDENCE_SHADOW",
        progress={
            "documents": [
                {
                    "matter_document_id": str(document_id),
                    "status": "COMPLETED",
                    "unit_count": 1,
                    "missing_unit_count": 0,
                    "observations_artifact_id": str(_id(503)),
                }
            ]
        },
    )
    adjudication = _workflow(
        504,
        job_type="MATTER_DEFINITION_EVIDENCE_ADJUDICATION_SHADOW",
        document_id=document_id,
        status="QUEUED",
        progress={},
    )
    narrative = _workflow(
        505,
        job_type="MATTER_DEFINITION_EVIDENCE_NARRATIVE_SHADOW",
        document_id=document_id,
        status="QUEUED",
        progress={},
    )
    references = [
        _artifact(
            506,
            artifact_type=ArtifactType.ANALYSIS_DOCUMENT_DECISION,
            producer_run_id=adjudication.id,
            document_id=document_id,
            metadata={"status": "COMPLETED"},
        ),
        _artifact(
            507,
            artifact_type=ArtifactType.ANALYSIS_COMPATIBILITY_RESULT,
            producer_run_id=adjudication.id,
            document_id=document_id,
            metadata={"status": "COMPLETED"},
        ),
        _artifact(
            508,
            artifact_type=ArtifactType.ANALYSIS_NARRATIVE_PROJECTION,
            producer_run_id=narrative.id,
            document_id=document_id,
        ),
    ]

    manifest = build_evidence_outcome_manifest(
        tenant_id=_id(1),
        client_id=_id(2),
        matter_id=_id(3),
        assessment_id=_id(4),
        document_ids=[document_id],
        extraction_workflows=[extraction],
        adjudication_workflows=[adjudication],
        narrative_workflows=[narrative],
        artifact_references=references,
    )

    outcome = manifest.documents[0]
    assert outcome.adjudication.status == "QUEUED"
    assert outcome.compatibility_projection.status == "QUEUED"
    assert outcome.narrative.status == "QUEUED"
    assert outcome.synthesis_readiness == "PENDING"
    assert outcome.selected_output_artifact_id is None
