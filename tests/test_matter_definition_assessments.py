import asyncio
import json
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import get_args

import pytest
from conftest import TestingSessionLocal, create_client_tenant
from fastapi.testclient import TestClient
from pydantic_ai.models.test import TestModel
from sqlalchemy import select

from app.assessment_execution import (
    _assessment_terminal_status,
    _coverage_envelope,
    _enforce_named_issue_references,
    _execute_document_analysis,
    _load_document_analyses,
    _synthesis_context,
    _validate_retrieval_plan,
    _validate_synthesis_refinement,
    analyze_document,
    archive_superseded_assessments,
    collect_retrieval_hits,
    fail_assessment,
    refresh_progress,
    retrieval_plan_feedback,
)
from app.assessment_guidance_refinement import create_guidance_revision
from app.assessment_provider_batches import (
    _structured_request,
    finalize_provider_batch,
    plan_provider_batches,
    repair_failed_provider_batch_outputs,
)
from app.assessment_workflow_reconciliation import reconcile_terminal_assessment_workflows
from app.config import get_settings
from app.document_evidence import build_document_map_plan, segment_paragraphs
from app.matter_definition_assessments import (
    AssessmentError,
    RetrievalHit,
    SelectedCandidate,
    materialize_assessment_batch,
    merge_retrieval_candidates,
)
from app.models import (
    Matter,
    MatterDefinition,
    MatterDefinitionAssessmentProviderBatch,
    MatterDefinitionAssessmentQuestion,
    MatterDefinitionAssessmentRun,
    MatterDefinitionRevision,
    MatterDocument,
    MatterDocumentImportJob,
    ModelInvocation,
    ReviewBatchRun,
    ReviewBatchRunDocument,
    SearchIndexGeneration,
    SkillDefinitionVersion,
    SkillRun,
    Tenant,
    User,
    WorkflowRun,
    WorkflowStepRun,
)
from app.routers.matter_definition_assessments import _assessment_reads, _workflow_step_reads
from app.schemas import (
    MatterDefinitionAssessmentCreate,
    MatterDefinitionSourceKind,
    MatterSearchHit,
    MatterSearchResponse,
)
from app.standard_skills import ensure_standard_assessment_skills
from app.workflows.matter_definition_assessments import (
    _sync_batch_search_best_effort,
    guidance_refinement_failure_message,
)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_matter_definition_source_kind_column_fits_every_supported_value() -> None:
    source_kind_column = MatterDefinitionRevision.__table__.c.source_kind

    assert source_kind_column.type.length >= max(map(len, get_args(MatterDefinitionSourceKind)))


def test_assessment_target_accepts_legacy_maximum_name() -> None:
    payload = MatterDefinitionAssessmentCreate.model_validate({"maximum_document_count": 42})

    assert payload.target_document_count == 42


def test_guidance_refinement_failure_message_uses_last_step_error() -> None:
    wrapped_error = RuntimeError("retry wrapper")
    wrapped_error.errors = [ValueError("first failure"), ValueError("database rejected source kind")]

    assert guidance_refinement_failure_message(wrapped_error) == "database rejected source kind"


def test_assessment_batch_search_projection_failure_is_non_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment_id = uuid.uuid4()
    batch_id = uuid.uuid4()

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, model, record_id):
            assert model is MatterDefinitionAssessmentRun
            assert record_id == assessment_id
            return SimpleNamespace(review_batch_id=batch_id)

    monkeypatch.setattr(
        "app.workflows.matter_definition_assessments.SessionLocal",
        FakeSession,
    )

    def fail_projection(received_batch_id, _settings):
        assert received_batch_id == batch_id
        raise RuntimeError("Search schema changes require a confirmed full reindex")

    monkeypatch.setattr(
        "app.workflows.matter_definition_assessments.sync_review_batch_search",
        fail_projection,
    )

    result = _sync_batch_search_best_effort(str(assessment_id))

    assert result == {
        "status": "FAILED",
        "error": "Search schema changes require a confirmed full reindex",
    }


@pytest.mark.parametrize(
    ("source", "expected_code", "expected_message"),
    [
        (None, "NO_TEXT_SOURCE", "Document has no analyzable text source"),
        (SimpleNamespace(text="\n"), "EMPTY_TEXT_SOURCE", "Document text source is empty"),
    ],
)
def test_document_without_analyzable_text_is_skipped_with_reason(
    monkeypatch: pytest.MonkeyPatch,
    source,
    expected_code: str,
    expected_message: str,
) -> None:
    assessment_id = uuid.uuid4()
    review_run_id = uuid.uuid4()
    document_id = uuid.uuid4()
    run_document = SimpleNamespace(
        status="QUEUED",
        failure_code="OLD_ERROR",
        failure_message="Old error",
        failure_retriable=True,
        completed_at=None,
    )
    document = SimpleNamespace(id=document_id, collection_item_id=uuid.uuid4())
    assessment = SimpleNamespace(
        id=assessment_id,
        status="SUMMARIZING",
        review_batch_run_id=review_run_id,
        initiated_by_user_id=uuid.uuid4(),
    )
    matter = SimpleNamespace(
        client=SimpleNamespace(tenant_id=uuid.uuid4()),
        client_id=uuid.uuid4(),
    )

    class StubSession:
        def __init__(self):
            self.commits = 0

        def get(self, model, record_id):
            if model is ReviewBatchRunDocument and record_id == (review_run_id, document_id):
                return run_document
            if model is MatterDocument and record_id == document_id:
                return document
            return None

        def commit(self):
            self.commits += 1

    monkeypatch.setattr(
        "app.assessment_execution._records",
        lambda *_args: (assessment, SimpleNamespace(), SimpleNamespace(), matter),
    )
    monkeypatch.setattr(
        "app.assessment_execution.get_preferred_text_source",
        lambda **_kwargs: source,
    )

    result = analyze_document(StubSession(), assessment_id, document_id)  # type: ignore[arg-type]

    assert result == {"status": "SKIPPED", "reason": expected_code}
    assert run_document.status == "SKIPPED"
    assert run_document.failure_code == expected_code
    assert run_document.failure_message == expected_message
    assert run_document.failure_retriable is False
    assert run_document.completed_at is not None


def test_skipped_only_assessment_completes_without_errors() -> None:
    assessment = SimpleNamespace(
        failed_count=0,
        skipped_count=1,
        partial_coverage_count=0,
        invalid_result_count=0,
        synthesis_status="COMPLETED",
        completion_disposition="READY_WITH_WARNINGS",
    )

    assert _assessment_terminal_status(assessment) == "COMPLETED"
    assessment.failed_count = 1
    assert _assessment_terminal_status(assessment) == "COMPLETED_WITH_ERRORS"


def test_provider_batch_finalization_refreshes_progress_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_id = uuid.uuid4()
    assessment_id = uuid.uuid4()
    document_id = uuid.uuid4()
    skill_run_id = uuid.uuid4()
    batch = SimpleNamespace(
        id=batch_id,
        assessment_run_id=assessment_id,
        review_batch_run_id=uuid.uuid4(),
        provider_batch_id="provider-batch-1",
        provider="google",
        provider_status="JOB_STATE_SUCCEEDED",
        manifest={
            "requests": [
                {
                    "custom_id": str(document_id),
                    "document_id": str(document_id),
                    "skill_run_id": str(skill_run_id),
                }
            ]
        },
        status="RUNNING",
        completed_at=None,
        error_message=None,
    )
    assessment = SimpleNamespace(id=assessment_id)
    skill_run = SimpleNamespace(status="COMPLETED")
    run_document = SimpleNamespace(status="COMPLETED")
    document = SimpleNamespace(id=document_id)

    class StubSession:
        def get(self, model, record_id):
            if model is MatterDefinitionAssessmentProviderBatch and record_id == batch_id:
                return batch
            if model is SkillRun and record_id == skill_run_id:
                return skill_run
            if model is ReviewBatchRunDocument and record_id == (batch.review_batch_run_id, document_id):
                return run_document
            if model is MatterDocument and record_id == document_id:
                return document
            return None

    class StubAdapter:
        def results(self, provider_batch_id):
            assert provider_batch_id == "provider-batch-1"
            return "JOB_STATE_SUCCEEDED", []

    refreshed: list[uuid.UUID] = []
    monkeypatch.setattr(
        "app.assessment_provider_batches._records",
        lambda _db, received_id: (
            assessment,
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
        ) if received_id == assessment_id else pytest.fail("Unexpected assessment"),
    )
    monkeypatch.setattr(
        "app.assessment_provider_batches._skill_version",
        lambda *_args: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "app.assessment_provider_batches.get_batch_adapter",
        lambda provider: StubAdapter() if provider == "google" else pytest.fail("Unexpected provider"),
    )
    monkeypatch.setattr(
        "app.assessment_provider_batches.refresh_progress",
        lambda _db, received_id: refreshed.append(received_id),
    )

    result = finalize_provider_batch(StubSession(), batch_id)  # type: ignore[arg-type]

    assert result == {"completed": 1, "failed": 0}
    assert batch.status == "COMPLETED"
    assert refreshed == [assessment_id]


def test_provider_batch_finalization_checkpoints_without_shared_usage_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_id = uuid.uuid4()
    assessment_id = uuid.uuid4()
    review_run_id = uuid.uuid4()
    document_id = uuid.uuid4()
    skill_run_id = uuid.uuid4()
    batch = SimpleNamespace(
        id=batch_id,
        assessment_run_id=assessment_id,
        review_batch_run_id=review_run_id,
        provider_batch_id="provider-batch-1",
        provider="google",
        model="gemini-test",
        provider_status="JOB_STATE_SUCCEEDED",
        manifest={
            "requests": [
                {
                    "custom_id": str(document_id),
                    "document_id": str(document_id),
                    "skill_run_id": str(skill_run_id),
                    "model_configuration_hash": "a" * 64,
                }
            ]
        },
        status="RUNNING",
        submitted_at=datetime.now(timezone.utc),
        completed_at=None,
        error_message=None,
    )
    assessment = SimpleNamespace(id=assessment_id, initiated_by_user_id=uuid.uuid4())
    workflow = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        client_id=uuid.uuid4(),
        matter_id=uuid.uuid4(),
        initiated_by_user_id=uuid.uuid4(),
        workflow_key="matter_definition_assessment_v2",
        created_at=datetime.now(timezone.utc),
    )
    skill_run = SimpleNamespace(id=skill_run_id, status="RUNNING")
    run_document = SimpleNamespace(status="IN_PROGRESS", completed_at=None)
    document = SimpleNamespace(id=document_id, collection_item_id=uuid.uuid4())
    source = SimpleNamespace(text="Evidence", artifact_id=uuid.uuid4(), content_hash="b" * 64)
    version = SimpleNamespace(output_schema={})

    class StubSession:
        def __init__(self):
            self.commits = 0

        def get(self, model, record_id):
            if model is MatterDefinitionAssessmentProviderBatch and record_id == batch_id:
                return batch
            if model is SkillRun and record_id == skill_run_id:
                return skill_run
            if model is ReviewBatchRunDocument and record_id == (review_run_id, document_id):
                return run_document
            if model is MatterDocument and record_id == document_id:
                return document
            return None

        def commit(self):
            self.commits += 1

        def rollback(self):
            pytest.fail("Successful finalization must not roll back")

    class StubAdapter:
        def results(self, provider_batch_id):
            assert provider_batch_id == "provider-batch-1"
            return "JOB_STATE_SUCCEEDED", [
                SimpleNamespace(
                    custom_id=str(document_id),
                    error=None,
                    output_text="{}",
                    provider_request_id="request-1",
                    input_tokens=10,
                    cached_input_tokens=0,
                    output_tokens=5,
                )
            ]

    completed_with_shared_refresh: list[bool] = []
    monkeypatch.setattr(
        "app.assessment_provider_batches._records",
        lambda *_args: (assessment, workflow, SimpleNamespace(), SimpleNamespace()),
    )
    monkeypatch.setattr("app.assessment_provider_batches._skill_version", lambda *_args: version)
    monkeypatch.setattr("app.assessment_provider_batches.get_batch_adapter", lambda _provider: StubAdapter())
    monkeypatch.setattr("app.assessment_provider_batches.validate_structured_output", lambda *_args: None)
    monkeypatch.setattr("app.assessment_provider_batches.validate_document_analysis_citations", lambda *_args: None)
    monkeypatch.setattr("app.assessment_provider_batches.get_preferred_text_source", lambda **_kwargs: source)
    monkeypatch.setattr(
        "app.assessment_provider_batches.segment_paragraphs",
        lambda _text: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "app.assessment_provider_batches.build_document_map_plan",
        lambda *_args, **_kwargs: SimpleNamespace(windows=[SimpleNamespace(window_id="w1")]),
    )
    monkeypatch.setattr("app.assessment_provider_batches.document_map_coverage", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("app.assessment_provider_batches.persist_model_invocations", lambda *_args, **_kwargs: None)

    def complete_run(_db, received_skill_run, *, refresh_workflow_usage=True):
        assert received_skill_run is skill_run
        completed_with_shared_refresh.append(refresh_workflow_usage)
        received_skill_run.status = "COMPLETED"

    def persist_analysis(_db, **_kwargs):
        run_document.status = "COMPLETED"

    monkeypatch.setattr("app.assessment_provider_batches.complete_skill_run", complete_run)
    monkeypatch.setattr("app.assessment_provider_batches.persist_document_analysis", persist_analysis)
    monkeypatch.setattr("app.assessment_provider_batches.refresh_progress", lambda *_args: None)

    session = StubSession()
    result = finalize_provider_batch(session, batch_id)  # type: ignore[arg-type]

    assert result == {"completed": 1, "failed": 0}
    assert completed_with_shared_refresh == [False]
    assert session.commits == 1
    assert batch.status == "COMPLETED"


def test_provider_batch_finalization_repairs_supported_invalid_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_id = uuid.uuid4()
    assessment_id = uuid.uuid4()
    review_run_id = uuid.uuid4()
    document_id = uuid.uuid4()
    skill_run_id = uuid.uuid4()
    batch = SimpleNamespace(
        id=batch_id,
        assessment_run_id=assessment_id,
        review_batch_run_id=review_run_id,
        provider_batch_id="provider-batch-1",
        provider="google",
        model="gemini-test",
        provider_status="JOB_STATE_SUCCEEDED",
        manifest={
            "requests": [
                {
                    "custom_id": str(document_id),
                    "document_id": str(document_id),
                    "skill_run_id": str(skill_run_id),
                    "model_configuration_hash": "a" * 64,
                }
            ]
        },
        status="RUNNING",
        submitted_at=datetime.now(timezone.utc),
        completed_at=None,
        error_message=None,
    )
    assessment = SimpleNamespace(
        id=assessment_id,
        initiated_by_user_id=uuid.uuid4(),
        configuration_snapshot={"resolved_models": {"document_analysis": "google:gemini-test"}},
    )
    workflow = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        client_id=uuid.uuid4(),
        matter_id=uuid.uuid4(),
        initiated_by_user_id=uuid.uuid4(),
        workflow_key="matter_definition_assessment_v1",
        created_at=datetime.now(timezone.utc),
    )
    revision = SimpleNamespace(content_markdown="Review guidance")
    version = SimpleNamespace(
        model_key="document-analysis",
        model_policy={},
        limits={"max_output_tokens": 20_000},
        output_schema={},
    )
    skill_run = SimpleNamespace(id=skill_run_id, status="RUNNING")
    run_document = SimpleNamespace(status="IN_PROGRESS", completed_at=None)
    document = SimpleNamespace(id=document_id, collection_item_id=uuid.uuid4())
    source = SimpleNamespace(text="Evidence", artifact_id=uuid.uuid4(), content_hash="b" * 64)
    invalid_output = {
        "summary": [{"text": "Summary", "citation_ids": ["¶1"]}],
        "determination": "NOT_PRIVILEGED",
        "confidence": 0.9,
        "responsiveness_summary": [{"text": "Outside scope", "citation_ids": ["¶1"]}],
        "criterion_matches": [],
        "scope_analysis": [],
        "countervailing_considerations": [],
        "clarification_requests": [],
        "limitations": [],
    }
    repaired_output = {**invalid_output, "determination": "NON_RESPONSIVE"}

    class StubSession:
        def get(self, model, record_id):
            if model is MatterDefinitionAssessmentProviderBatch and record_id == batch_id:
                return batch
            if model is SkillRun and record_id == skill_run_id:
                return skill_run
            if model is ReviewBatchRunDocument and record_id == (review_run_id, document_id):
                return run_document
            if model is MatterDocument and record_id == document_id:
                return document
            return None

        def commit(self):
            return None

        def rollback(self):
            pytest.fail("Successful repair must not roll back")

    class StubAdapter:
        def results(self, provider_batch_id):
            assert provider_batch_id == "provider-batch-1"
            return "JOB_STATE_SUCCEEDED", [
                SimpleNamespace(
                    custom_id=str(document_id),
                    error=None,
                    output_text=json.dumps(invalid_output),
                    provider_request_id="request-1",
                    input_tokens=10,
                    cached_input_tokens=0,
                    output_tokens=5,
                )
            ]

    repair_calls: list[dict] = []

    async def fake_repair(**kwargs):
        repair_calls.append(kwargs)
        return SimpleNamespace(
            output=repaired_output,
            normalization_changes=(),
            envelope=SimpleNamespace(invocations=()),
            diagnostics=lambda: {
                "normalization_changes": [],
                "focused_repair_applied": True,
                "validation_errors": ["determination is invalid"],
                "repair_targets": {"determination": True, "citation_paths": []},
            },
        )

    invocation_attempts: list[int] = []
    persisted_diagnostics: list[dict] = []
    monkeypatch.setattr(
        "app.assessment_provider_batches._records",
        lambda *_args: (assessment, workflow, revision, SimpleNamespace()),
    )
    monkeypatch.setattr("app.assessment_provider_batches._skill_version", lambda *_args: version)
    monkeypatch.setattr("app.assessment_provider_batches.get_batch_adapter", lambda _provider: StubAdapter())
    monkeypatch.setattr(
        "app.assessment_provider_batches.normalize_or_repair_document_analysis",
        fake_repair,
    )
    monkeypatch.setattr("app.assessment_provider_batches.get_preferred_text_source", lambda **_kwargs: source)
    monkeypatch.setattr("app.assessment_provider_batches.segment_paragraphs", lambda _text: SimpleNamespace())
    monkeypatch.setattr(
        "app.assessment_provider_batches.validate_document_analysis_citations",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "app.assessment_provider_batches.build_document_map_plan",
        lambda *_args, **_kwargs: SimpleNamespace(windows=[SimpleNamespace(window_id="w1")]),
    )
    monkeypatch.setattr("app.assessment_provider_batches.document_map_coverage", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        "app.assessment_provider_batches.persist_model_invocations",
        lambda *_args, **kwargs: invocation_attempts.append(kwargs.get("attempt", 1)),
    )
    monkeypatch.setattr(
        "app.assessment_provider_batches.complete_skill_run",
        lambda _db, run, **_kwargs: setattr(run, "status", "COMPLETED"),
    )

    def persist_analysis(_db, **kwargs):
        persisted_diagnostics.append(kwargs["pipeline_diagnostics"])
        run_document.status = "COMPLETED"

    monkeypatch.setattr("app.assessment_provider_batches.persist_document_analysis", persist_analysis)
    monkeypatch.setattr("app.assessment_provider_batches.refresh_progress", lambda *_args: None)

    result = finalize_provider_batch(StubSession(), batch_id)  # type: ignore[arg-type]

    assert result == {"completed": 1, "failed": 0}
    assert len(repair_calls) == 1
    assert repair_calls[0]["output"]["determination"] == "NOT_PRIVILEGED"
    assert invocation_attempts == [1, 2]
    assert persisted_diagnostics[0]["output_repair"]["focused_repair_applied"] is True
    assert run_document.status == "COMPLETED"


def test_retry_plan_does_not_reanalyze_recovered_provider_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment_id = uuid.uuid4()
    review_run_id = uuid.uuid4()
    document_id = uuid.uuid4()
    run_document = SimpleNamespace(status="QUEUED")
    assessment = SimpleNamespace(
        id=assessment_id,
        review_batch_run_id=review_run_id,
        use_batching=False,
    )
    workflow = SimpleNamespace(dbos_workflow_id=f"definition-assessment:{assessment_id}:retry:1")

    class StubSession:
        def get(self, model, record_id):
            if model is ReviewBatchRunDocument and record_id == (review_run_id, document_id):
                return run_document
            return None

    def recover(_db, received_assessment_id, document_ids):
        assert received_assessment_id == assessment_id
        assert document_ids == [document_id]
        run_document.status = "COMPLETED"
        return {"normalized": 1, "focused_repair": 0, "fallback": 0}

    monkeypatch.setattr(
        "app.assessment_provider_batches._records",
        lambda *_args: (assessment, workflow, SimpleNamespace(), SimpleNamespace()),
    )
    monkeypatch.setattr(
        "app.assessment_provider_batches.repair_failed_provider_batch_outputs",
        recover,
    )

    result = plan_provider_batches(
        StubSession(),  # type: ignore[arg-type]
        assessment_id,
        [document_id],
        get_settings(),
    )

    assert result["realtime_document_ids"] == []
    assert result["provider_batch_ids"] == []
    assert result["repair_summary"] == {
        "normalized": 1,
        "focused_repair": 0,
        "fallback": 0,
    }


def test_retry_recovers_stored_provider_output_and_falls_back_only_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment_id = uuid.uuid4()
    review_run_id = uuid.uuid4()
    recovered_document_id = uuid.uuid4()
    fallback_document_id = uuid.uuid4()
    run_document = SimpleNamespace(status="QUEUED", completed_at=None)
    document = SimpleNamespace(id=recovered_document_id, collection_item_id=uuid.uuid4())
    assessment = SimpleNamespace(
        id=assessment_id,
        review_batch_run_id=review_run_id,
        selected_count=2,
        initiated_by_user_id=uuid.uuid4(),
        configuration_snapshot={"resolved_models": {"document_analysis": "google:gemini-test"}},
    )
    workflow = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        client_id=uuid.uuid4(),
        matter_id=uuid.uuid4(),
        initiated_by_user_id=uuid.uuid4(),
        workflow_key="matter_definition_assessment_v1",
        created_at=datetime.now(timezone.utc),
    )
    revision = SimpleNamespace(content_markdown="Review guidance")
    matter = SimpleNamespace()
    version = SimpleNamespace(
        model_key="document-analysis",
        model_policy={},
        limits={"max_output_tokens": 20_000},
        output_schema={},
    )
    batch = SimpleNamespace(provider_batch_id="provider-batch-1")
    response = SimpleNamespace(
        error=None,
        output_text=json.dumps({"determination": "NOT_RESPONSIVE"}),
    )
    source = SimpleNamespace(text="Evidence", artifact_id=uuid.uuid4(), content_hash="c" * 64)
    skill_run = SimpleNamespace(id=uuid.uuid4(), cache_fingerprint=None, status="RUNNING")

    class StubSession:
        def __init__(self):
            self.commits = 0

        def get(self, model, record_id):
            if model is ReviewBatchRunDocument and record_id == (
                review_run_id,
                recovered_document_id,
            ):
                return run_document
            if model is MatterDocument and record_id == recovered_document_id:
                return document
            return None

        def commit(self):
            self.commits += 1

        def rollback(self):
            pytest.fail("Successful recovery must not roll back")

    async def normalize_recovered_output(**_kwargs):
        return SimpleNamespace(
            output={"determination": "NON_RESPONSIVE"},
            envelope=None,
            assembly=None,
            focused_repair_applied=False,
            diagnostics=lambda: {
                "normalization_changes": [
                    {
                        "path": "determination",
                        "from": "NOT_RESPONSIVE",
                        "to": "NON_RESPONSIVE",
                    }
                ],
                "focused_repair_applied": False,
                "validation_errors": [],
                "repair_targets": None,
            },
        )

    persisted: list[dict] = []
    refreshed: list[uuid.UUID] = []
    monkeypatch.setattr(
        "app.assessment_provider_batches._records",
        lambda *_args: (assessment, workflow, revision, matter),
    )
    monkeypatch.setattr("app.assessment_provider_batches._skill_version", lambda *_args: version)
    monkeypatch.setattr("app.assessment_provider_batches._step", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(
        "app.assessment_provider_batches._prior_provider_results",
        lambda *_args: {recovered_document_id: (batch, response)},
    )
    monkeypatch.setattr("app.assessment_provider_batches.get_preferred_text_source", lambda **_kwargs: source)
    monkeypatch.setattr("app.assessment_provider_batches.segment_paragraphs", lambda _text: SimpleNamespace())
    monkeypatch.setattr(
        "app.assessment_provider_batches.build_document_map_plan",
        lambda *_args, **_kwargs: SimpleNamespace(windows=[SimpleNamespace(window_id="w1")]),
    )
    monkeypatch.setattr("app.assessment_provider_batches.document_map_coverage", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        "app.assessment_provider_batches.normalize_or_repair_document_analysis",
        normalize_recovered_output,
    )
    monkeypatch.setattr("app.assessment_provider_batches.validate_structured_output", lambda *_args: None)
    monkeypatch.setattr("app.assessment_provider_batches.create_skill_run", lambda *_args, **_kwargs: skill_run)
    monkeypatch.setattr(
        "app.assessment_provider_batches.complete_skill_run",
        lambda _db, run, **_kwargs: setattr(run, "status", "COMPLETED"),
    )

    def persist_analysis(_db, **kwargs):
        persisted.append(kwargs)
        run_document.status = "COMPLETED"

    monkeypatch.setattr("app.assessment_provider_batches.persist_document_analysis", persist_analysis)
    monkeypatch.setattr(
        "app.assessment_provider_batches.refresh_progress",
        lambda _db, received_id: refreshed.append(received_id),
    )

    session = StubSession()
    result = repair_failed_provider_batch_outputs(
        session,  # type: ignore[arg-type]
        assessment_id,
        [recovered_document_id, fallback_document_id],
    )

    assert result == {"normalized": 1, "focused_repair": 0, "fallback": 1}
    assert run_document.status == "COMPLETED"
    assert persisted[0]["pipeline_diagnostics"]["mode"] == "RECOVERED_PROVIDER_BATCH_OUTPUT"
    assert refreshed == [assessment_id]
    assert session.commits == 1


def test_refresh_progress_locks_assessment_before_shared_rollup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment_id = uuid.uuid4()
    workflow_id = uuid.uuid4()
    review_run_id = uuid.uuid4()
    assessment = SimpleNamespace(
        id=assessment_id,
        workflow_run_id=workflow_id,
        review_batch_run_id=review_run_id,
        selected_count=10,
        summarized_count=0,
        skipped_count=0,
        failed_count=0,
    )
    workflow = SimpleNamespace(id=workflow_id)
    review_run = SimpleNamespace(processed_document_count=0)
    step = SimpleNamespace(completed_count=0, failed_count=0)

    class StubSession:
        def __init__(self):
            self.no_autoflush = nullcontext()
            self.commits = 0

        def scalar(self, statement):
            assert statement._for_update_arg is not None
            return assessment

        def get(self, model, record_id):
            if model is WorkflowRun and record_id == workflow_id:
                return workflow
            if model is ReviewBatchRun and record_id == review_run_id:
                return review_run
            return None

        def execute(self, _statement):
            return SimpleNamespace(all=lambda: [("COMPLETED", 6), ("SKIPPED", 1), ("FAILED", 2)])

        def commit(self):
            self.commits += 1

    session = StubSession()
    refreshed_usage: list[uuid.UUID] = []
    monkeypatch.setattr("app.assessment_execution._step", lambda *_args, **_kwargs: step)
    monkeypatch.setattr(
        "app.assessment_execution.refresh_workflow_execution_usage",
        lambda _db, received_id: refreshed_usage.append(received_id),
    )

    result = refresh_progress(session, assessment_id)  # type: ignore[arg-type]

    assert result == {"completed": 6, "skipped": 1, "failed": 2}
    assert assessment.summarized_count == 6
    assert assessment.skipped_count == 1
    assert assessment.failed_count == 2
    assert review_run.processed_document_count == 7
    assert step.completed_count == 7
    assert step.failed_count == 2
    assert refreshed_usage == [workflow_id]
    assert session.commits == 1


def create_assessment_matter(client: TestClient, root_token: str, root_admin) -> str:
    tenant_id = create_client_tenant(client, root_token)
    created_client = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=auth(root_token),
        json={"name": "Assessment Client"},
    )
    assert created_client.status_code == 201, created_client.text
    matter = client.post(
        f"/v1/clients/{created_client.json()['id']}/matters",
        headers=auth(root_token),
        json={"name": "Assessment Matter"},
    )
    assert matter.status_code == 201, matter.text
    matter_id = matter.json()["id"]
    definition = client.post(
        f"/v1/matters/{matter_id}/definition/revisions",
        headers=auth(root_token),
        json={"content_markdown": "# Review instructions\n\nIssue 1: hurricane insurance disruption."},
    )
    assert definition.status_code == 201, definition.text
    with TestingSessionLocal() as db:
        root = db.scalar(select(Tenant).where(Tenant.id == root_admin.tenant_id))
        assert root is not None
        ensure_standard_assessment_skills(db, root, db.merge(root_admin))
        db.add(
            SearchIndexGeneration(
                matter_id=uuid.UUID(matter_id),
                generation=1,
                index_name=f"test-{matter_id}-v1",
                alias_name=f"test-{matter_id}",
                schema_hash="a" * 64,
                status="ACTIVE",
                schema_snapshot={},
            )
        )
        db.commit()
    return matter_id


def test_assessment_launch_pins_inputs_and_requires_large_run_acknowledgment(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    base = f"/v1/matters/{matter_id}/definition-assessments"
    created = client.post(base, headers=auth(root_token), json={"name": "Initial hurricane coverage review"})
    assert created.status_code == 202, created.text
    payload = created.json()
    assert payload["name"] == "Initial hurricane coverage review"
    assert payload["requested_document_count"] == 500
    assert payload["use_batching"] is True
    assert payload["status"] == "QUEUED"
    assert payload["review_batch_id"] is None
    assert payload["definition_content_hash"]

    renamed = client.patch(
        f"{base}/{payload['id']}",
        headers=auth(root_token),
        json={"name": "  Renamed   hurricane assessment  "},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Renamed hurricane assessment"
    blank_name = client.patch(
        f"{base}/{payload['id']}",
        headers=auth(root_token),
        json={"name": "   "},
    )
    assert blank_name.status_code == 422

    warned = client.post(
        base,
        headers=auth(root_token),
        json={"target_document_count": 1001},
    )
    assert warned.status_code == 409
    assert "acknowledgment" in warned.json()["error"]["message"]

    acknowledged = client.post(
        base,
        headers=auth(root_token),
        json={"target_document_count": 1001, "acknowledge_large_run_warning": True},
    )
    assert acknowledged.status_code == 202, acknowledged.text
    assert acknowledged.json()["large_run_warning_acknowledged"] is True
    assert acknowledged.json()["warning_acknowledged_by_user_id"] == str(root_admin.id)

    realtime = client.post(
        base,
        headers=auth(root_token),
        json={"name": "Real-time assessment", "use_batching": False},
    )
    assert realtime.status_code == 202, realtime.text
    assert realtime.json()["use_batching"] is False

    listed = client.get(base, headers=auth(root_token))
    assert listed.status_code == 200
    assert len(listed.json()) == 3
    assert any(item["name"] == "Renamed hurricane assessment" for item in listed.json())
    with TestingSessionLocal() as db:
        records = list(db.scalars(select(MatterDefinitionAssessmentRun)))
        assert all(record.workflow_run_id for record in records)
        assert all(record.search_index_generation_id for record in records)
        assert all(
            set(record.binding_snapshot)
            == {"retrieval_planner", "document_analysis", "assessment_synthesis", "guidance_refinement"}
            for record in records
        )


def test_assessment_launch_routes_evidence_first_only_when_explicitly_enabled(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    base = f"/v1/matters/{matter_id}/definition-assessments"
    settings = get_settings()
    monkeypatch.setattr(settings, "definition_assessment_evidence_shadow_enabled", False)

    disabled = client.post(
        base,
        headers=auth(root_token),
        json={"name": "Evidence disabled", "execution_mode": "EVIDENCE_FIRST_V1"},
    )
    assert disabled.status_code == 409
    assert "disabled by configuration" in disabled.json()["error"]["message"]

    evidence_calls: list[dict[str, str]] = []
    direct_calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(settings, "definition_assessment_evidence_shadow_enabled", True)
    monkeypatch.setattr(
        "app.matter_definition_assessments.enqueue_evidence_assessment",
        lambda _db, workflow_id, **kwargs: evidence_calls.append(
            {"workflow_id": workflow_id, **kwargs}
        ),
    )
    monkeypatch.setattr(
        "app.matter_definition_assessments.enqueue_definition_assessment",
        lambda *args, **kwargs: direct_calls.append((args, kwargs)),
    )

    evidence = client.post(
        base,
        headers=auth(root_token),
        json={"name": "Evidence routed", "execution_mode": "EVIDENCE_FIRST_V1"},
    )
    assert evidence.status_code == 202, evidence.text
    direct = client.post(base, headers=auth(root_token), json={"name": "Direct default"})
    assert direct.status_code == 202, direct.text

    assert len(evidence_calls) == 1
    assert evidence_calls[0]["assessment_id"] == evidence.json()["id"]
    assert evidence_calls[0]["actor_user_id"] == str(root_admin.id)
    assert evidence_calls[0]["attempt_id"] == "initial"
    assert len(direct_calls) == 1
    with TestingSessionLocal() as db:
        evidence_record = db.get(MatterDefinitionAssessmentRun, uuid.UUID(evidence.json()["id"]))
        direct_record = db.get(MatterDefinitionAssessmentRun, uuid.UUID(direct.json()["id"]))
        assert evidence_record is not None and direct_record is not None
        assert evidence_record.configuration_snapshot["assessment_execution_mode"] == "EVIDENCE_FIRST_V1"
        assert direct_record.configuration_snapshot["assessment_execution_mode"] == "DIRECT_V1"


def test_failed_assessments_can_be_archived_and_restored(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    base = f"/v1/matters/{matter_id}/definition-assessments"
    assessment_ids = [
        uuid.UUID(
            client.post(
                base,
                headers=auth(root_token),
                json={"name": name, "target_document_count": 1},
            ).json()["id"]
        )
        for name in ("Failed planning", "Canceled attempt", "Successful attempt")
    ]
    with TestingSessionLocal() as db:
        failed = db.get(MatterDefinitionAssessmentRun, assessment_ids[0])
        canceled = db.get(MatterDefinitionAssessmentRun, assessment_ids[1])
        completed = db.get(MatterDefinitionAssessmentRun, assessment_ids[2])
        assert failed is not None and canceled is not None and completed is not None
        failed.status = "FAILED"
        canceled.status = "CANCELED"
        completed.status = "COMPLETED"
        guidance_id = db.get(
            MatterDefinitionRevision,
            failed.matter_definition_revision_id,
        ).matter_definition_id
        db.commit()

    archived = client.post(
        f"{base}/archive-failed",
        headers=auth(root_token),
        json={"guidance_id": str(guidance_id)},
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived_count"] == 2
    assert set(archived.json()["assessment_ids"]) == {
        str(assessment_ids[0]),
        str(assessment_ids[1]),
    }

    visible = client.get(base, headers=auth(root_token))
    assert visible.status_code == 200
    assert [item["name"] for item in visible.json()] == ["Successful attempt"]

    history = client.get(f"{base}?include_archived=true", headers=auth(root_token))
    assert history.status_code == 200
    archived_items = [item for item in history.json() if item["archived_at"]]
    assert len(archived_items) == 2
    assert {item["archive_reason"] for item in archived_items} == {"MANUAL"}
    assert {item["archived_by_user_id"] for item in archived_items} == {str(root_admin.id)}

    restored = client.post(
        f"{base}/{assessment_ids[0]}/restore",
        headers=auth(root_token),
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["archived_at"] is None
    visible_after_restore = client.get(base, headers=auth(root_token))
    assert {item["name"] for item in visible_after_restore.json()} == {
        "Failed planning",
        "Successful attempt",
    }


def test_successful_assessment_supersedes_older_failed_attempts(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    base = f"/v1/matters/{matter_id}/definition-assessments"
    prior_id = uuid.UUID(
        client.post(base, headers=auth(root_token), json={"target_document_count": 1}).json()["id"]
    )
    current_id = uuid.UUID(
        client.post(base, headers=auth(root_token), json={"target_document_count": 1}).json()["id"]
    )
    with TestingSessionLocal() as db:
        prior = db.get(MatterDefinitionAssessmentRun, prior_id)
        current = db.get(MatterDefinitionAssessmentRun, current_id)
        assert prior is not None and current is not None
        revision = db.get(MatterDefinitionRevision, current.matter_definition_revision_id)
        assert revision is not None
        prior.status = "FAILED"
        archived_ids = archive_superseded_assessments(
            db,
            assessment=current,
            revision=revision,
            archived_at=datetime.now(timezone.utc),
        )
        db.commit()
        db.refresh(prior)

        assert archived_ids == [prior.id]
        assert prior.archive_reason == "SUPERSEDED"
        assert prior.archived_by_user_id is None
        assert prior.superseded_by_assessment_id == current.id


def test_answering_all_refinement_questions_queues_and_creates_guidance_draft(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    launched = client.post(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(root_token),
        json={"target_document_count": 1},
    )
    assert launched.status_code == 202, launched.text
    assessment_id = uuid.UUID(launched.json()["id"])
    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        assert assessment is not None
        assessment.status = "COMPLETED"
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        assert workflow is not None
        workflow.status = "COMPLETED"
        questions = [
            MatterDefinitionAssessmentQuestion(
                assessment_run_id=assessment.id,
                question="Should storm-adjacent claims be included?",
                rationale="The current boundary is ambiguous.",
                priority="HIGH",
                evidence=[],
                suggested_answers=["Include them.", "Exclude them."],
            ),
            MatterDefinitionAssessmentQuestion(
                assessment_run_id=assessment.id,
                question="Should the guidance require a direct causal link?",
                rationale="Reviewers need a consistent nexus rule.",
                priority="MEDIUM",
                evidence=[],
                suggested_answers=["Require a direct link.", "Allow an indirect link."],
            ),
        ]
        db.add_all(questions)
        db.commit()
        question_ids = [question.id for question in questions]

    first = client.put(
        f"/v1/matters/{matter_id}/definition-assessments/{assessment_id}/questions/{question_ids[0]}",
        headers=auth(root_token),
        json={"status": "ANSWERED", "answer": "Include them."},
    )
    assert first.status_code == 200, first.text
    assert first.json()["suggested_answers"] == ["Include them.", "Exclude them."]
    assert first.json()["answer"] == "Include them."
    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        assert assessment is not None
        assert assessment.guidance_refinement_status == "NOT_READY"

    second = client.put(
        f"/v1/matters/{matter_id}/definition-assessments/{assessment_id}/questions/{question_ids[1]}",
        headers=auth(root_token),
        json={"status": "ANSWERED", "answer": "Require a direct link."},
    )
    assert second.status_code == 200, second.text
    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        assert assessment is not None
        assert assessment.guidance_refinement_status == "QUEUED"
        assert assessment.guidance_refinement_workflow_run_id is not None
        refinement_workflow = db.get(WorkflowRun, assessment.guidance_refinement_workflow_run_id)
        assert refinement_workflow is not None
        assert set(refinement_workflow.binding_snapshot) == {"guidance_refinement"}

        revision_id = create_guidance_revision(
            db,
            assessment.id,
            model=TestModel(
                custom_output_args={
                    "content_markdown": (
                        "# Review instructions\n\nInclude storm-adjacent claims and require a direct causal link."
                    ),
                    "change_summary": ["Clarified the covered claims and causal-link requirement."],
                }
            ),
        )
        db.refresh(assessment)
        revision = db.get(MatterDefinitionRevision, revision_id)
        definition = db.scalar(select(MatterDefinition).where(MatterDefinition.matter_id == uuid.UUID(matter_id)))
        assert revision is not None and definition is not None
        assert revision.source_kind == "ASSESSMENT_REFINEMENT"
        assert revision.source_skill_run_id is not None
        assert revision.based_on_revision == 1
        assert definition.current_revision == 2
        assert definition.published_revision is None
        assert assessment.guidance_refinement_status == "COMPLETED"
        assert assessment.refined_matter_definition_revision_id == revision.id


def test_assessment_reads_use_live_document_counts(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    created = client.post(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(root_token),
        json={"target_document_count": 10},
    )
    assert created.status_code == 202, created.text
    assessment = SimpleNamespace(**created.json())
    assessment.review_batch_run_id = uuid.uuid4()
    assessment.status = "COMPLETED_WITH_ERRORS"
    assessment.synthesis_status = "COMPLETED"
    assessment.completion_disposition = "READY_WITH_WARNINGS"

    class StubSession:
        def execute(self, statement):
            del statement
            return [
                (assessment.review_batch_run_id, "COMPLETED", 7),
                (assessment.review_batch_run_id, "SKIPPED", 1),
                (assessment.review_batch_run_id, "FAILED", 2),
            ]

    result = _assessment_reads(StubSession(), [assessment])[0]  # type: ignore[arg-type]

    assert result.summarized_count == 7
    assert result.skipped_count == 1
    assert result.failed_count == 2
    assert result.status == "COMPLETED_WITH_ERRORS"

    assessment.partial_coverage_count = 0
    assessment.invalid_result_count = 0

    class SkippedOnlySession:
        def execute(self, statement):
            del statement
            return [
                (assessment.review_batch_run_id, "COMPLETED", 9),
                (assessment.review_batch_run_id, "SKIPPED", 1),
            ]

    skipped_only = _assessment_reads(SkippedOnlySession(), [assessment])[0]  # type: ignore[arg-type]
    assert skipped_only.status == "COMPLETED"
    assert skipped_only.skipped_count == 1
    assert skipped_only.failed_count == 0


def test_assessment_reads_redact_technical_diagnostics_for_non_tenant_admins(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    created = client.post(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(root_token),
        json={"target_document_count": 10},
    )
    assert created.status_code == 202, created.text
    assessment = SimpleNamespace(**created.json())
    assessment.synthesis_result = {
        "findings": [{"topic_key": "privilege", "finding": "Supported finding"}],
        "other_unmapped_criteria": {"items": [{"source_key": "model_only_label"}]},
        "synthesis_diagnostics": {"manifest_hash": "secret-technical-hash"},
    }
    assessment.synthesis_diagnostics = {
        "operator_message": "Retry failed documents.",
        "normalization_warnings": [{"code": "OUTPUT_NORMALIZED"}],
        "canonicalization": {"unmapped": [{"source_key": "model_only_label"}]},
    }

    class StubSession:
        pass

    operator_read = _assessment_reads(StubSession(), [assessment])[0]  # type: ignore[arg-type]
    admin_read = _assessment_reads(
        StubSession(),
        [assessment],
        include_technical_diagnostics=True,
    )[0]  # type: ignore[arg-type]

    assert operator_read.technical_diagnostics_visible is False
    assert operator_read.synthesis_result is not None
    assert operator_read.synthesis_result["findings"] == assessment.synthesis_result["findings"]
    assert "other_unmapped_criteria" not in operator_read.synthesis_result
    assert "synthesis_diagnostics" not in operator_read.synthesis_result
    assert operator_read.synthesis_diagnostics == {"operator_message": "Retry failed documents."}
    assert admin_read.technical_diagnostics_visible is True
    assert admin_read.synthesis_result == assessment.synthesis_result
    assert admin_read.synthesis_diagnostics == assessment.synthesis_diagnostics


def test_synthesis_diagnostics_require_tenant_administrator_role(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    with TestingSessionLocal() as db:
        matter = db.get(Matter, uuid.UUID(matter_id))
        assert matter is not None
        tenant_id = matter.client.tenant_id
        tenant_admin = db.scalar(
            select(User).where(User.tenant_id == tenant_id, User.tenant_role == "ADMIN")
        )
        assert tenant_admin is not None
        tenant_admin_email = tenant_admin.email

    created = client.post(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(root_token),
        json={"target_document_count": 10},
    )
    assert created.status_code == 202, created.text
    assessment_id = created.json()["id"]
    retry_document_id = uuid.uuid4()
    retry_target_id = uuid.uuid4()
    monkeypatch.setattr(
        "app.routers.matter_definition_assessments.schedule_evidence_stage_retry",
        lambda *_args, **kwargs: SimpleNamespace(
            assessment_id=uuid.UUID(assessment_id),
            matter_document_id=kwargs["matter_document_id"],
            stage=kwargs["stage"],
            target_workflow_run_id=retry_target_id,
            operation_workflow_id="evidence-stage-retry:test",
            reused_completed_work=True,
        ),
    )

    user_response = client.post(
        f"/v1/tenants/{tenant_id}/users",
        headers=auth(root_token),
        json={
            "email": "assessment-matter-admin@example.com",
            "display_name": "Assessment Matter Admin",
            "password": "assessment-matter-admin-password",
        },
    )
    assert user_response.status_code == 201, user_response.text
    user_id = user_response.json()["id"]
    roles_response = client.get(f"/v1/tenants/{tenant_id}/roles", headers=auth(root_token))
    assert roles_response.status_code == 200, roles_response.text
    matter_admin_role = next(
        role for role in roles_response.json() if role["key"] == "MATTER_ADMINISTRATOR"
    )
    grant_response = client.post(
        f"/v1/tenants/{tenant_id}/authorization-grants",
        headers=auth(root_token),
        json={
            "subject": {"type": "USER", "id": user_id},
            "role_id": matter_admin_role["id"],
            "scope": {"type": "MATTER", "id": matter_id},
        },
    )
    assert grant_response.status_code == 201, grant_response.text
    login_response = client.post(
        "/v1/auth/login",
        json={
            "email": "assessment-matter-admin@example.com",
            "password": "assessment-matter-admin-password",
        },
    )
    assert login_response.status_code == 200, login_response.text
    matter_admin_token = login_response.json()["access_token"]

    operator_list = client.get(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(matter_admin_token),
    )
    assert operator_list.status_code == 200, operator_list.text
    assert operator_list.json()[0]["technical_diagnostics_visible"] is False
    operator_outcomes = client.get(
        f"/v1/matters/{matter_id}/definition-assessments/{assessment_id}/evidence-outcomes",
        headers=auth(matter_admin_token),
    )
    assert operator_outcomes.status_code == 200, operator_outcomes.text
    assert operator_outcomes.json()["technical_diagnostics_visible"] is False
    assert operator_outcomes.json()["usage"] is None
    assert operator_outcomes.json()["documents"] == []
    synthesis_units = client.get(
        f"/v1/matters/{matter_id}/definition-assessments/{assessment_id}/synthesis-units",
        headers=auth(matter_admin_token),
    )
    assert synthesis_units.status_code == 403
    assert "Tenant administrator role required" in synthesis_units.text
    operator_retry = client.post(
        f"/v1/matters/{matter_id}/definition-assessments/{assessment_id}/evidence-outcomes/{retry_document_id}/retry",
        headers=auth(matter_admin_token),
        json={"stage": "NARRATIVE"},
    )
    assert operator_retry.status_code == 403
    monkeypatch.setattr(
        get_settings(),
        "definition_assessment_evidence_shadow_enabled",
        True,
    )
    operator_launch = client.post(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(matter_admin_token),
        json={"name": "Unauthorized evidence route", "execution_mode": "EVIDENCE_FIRST_V1"},
    )
    assert operator_launch.status_code == 403
    assert "Tenant administrator role required" in operator_launch.text

    tenant_admin_login = client.post(
        "/v1/auth/login",
        json={"email": tenant_admin_email, "password": "client-tenant-password-123"},
    )
    assert tenant_admin_login.status_code == 200, tenant_admin_login.text
    tenant_admin_token = tenant_admin_login.json()["access_token"]
    tenant_admin_launch = client.post(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(tenant_admin_token),
        json={"name": "Authorized evidence route", "execution_mode": "EVIDENCE_FIRST_V1"},
    )
    assert tenant_admin_launch.status_code == 202, tenant_admin_launch.text
    assert tenant_admin_launch.json()["execution_mode"] == "EVIDENCE_FIRST_V1"
    tenant_admin_list = client.get(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(tenant_admin_token),
    )
    assert tenant_admin_list.status_code == 200, tenant_admin_list.text
    assert tenant_admin_list.json()[0]["technical_diagnostics_visible"] is True
    tenant_admin_outcomes = client.get(
        f"/v1/matters/{matter_id}/definition-assessments/{assessment_id}/evidence-outcomes",
        headers=auth(tenant_admin_token),
    )
    assert tenant_admin_outcomes.status_code == 200, tenant_admin_outcomes.text
    assert tenant_admin_outcomes.json()["technical_diagnostics_visible"] is True
    assert tenant_admin_outcomes.json()["usage"]["workflow_count"] == 0
    tenant_admin_retry = client.post(
        f"/v1/matters/{matter_id}/definition-assessments/{assessment_id}/evidence-outcomes/{retry_document_id}/retry",
        headers=auth(tenant_admin_token),
        json={"stage": "NARRATIVE"},
    )
    assert tenant_admin_retry.status_code == 202, tenant_admin_retry.text
    assert tenant_admin_retry.json()["target_workflow_run_id"] == str(retry_target_id)
    assert tenant_admin_retry.json()["reused_completed_work"] is True
    tenant_admin_units = client.get(
        f"/v1/matters/{matter_id}/definition-assessments/{assessment_id}/synthesis-units",
        headers=auth(tenant_admin_token),
    )
    assert tenant_admin_units.status_code == 200, tenant_admin_units.text


def test_execution_step_reads_use_live_document_counts() -> None:
    run_id = uuid.uuid4()
    assessment = SimpleNamespace(review_batch_run_id=run_id)
    step = WorkflowStepRun(
        id=uuid.uuid4(),
        workflow_run_id=uuid.uuid4(),
        role_key="document_analysis",
        ordinal=3,
        fan_out_group="documents",
        status="RUNNING",
        total_count=1000,
        completed_count=0,
        failed_count=0,
        request_count=0,
        input_tokens=0,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=0,
        error_message=None,
        started_at=datetime.now(timezone.utc),
        completed_at=None,
    )

    class StubSession:
        def execute(self, statement):
            del statement
            return SimpleNamespace(
                all=lambda: [
                    ("COMPLETED", 4),
                    ("SKIPPED", 1),
                    ("FAILED", 2),
                    ("IN_PROGRESS", 100),
                ]
            )

    result = _workflow_step_reads(StubSession(), assessment, [step])  # type: ignore[arg-type]

    assert result[0].completed_count == 5
    assert result[0].failed_count == 2
    assert result[0].total_count == 1000


def test_execution_can_omit_document_skill_runs(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    launched = client.post(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(root_token),
        json={"target_document_count": 1},
    )
    assert launched.status_code == 202, launched.text
    assessment_id = uuid.UUID(launched.json()["id"])
    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        assert assessment is not None
        step = WorkflowStepRun(
            workflow_run_id=assessment.workflow_run_id,
            role_key="document_analysis",
            ordinal=3,
            status="COMPLETED",
            total_count=1,
            completed_count=1,
        )
        db.add(step)
        db.flush()
        db.add(
            SkillRun(
                workflow_run_id=assessment.workflow_run_id,
                workflow_step_run_id=step.id,
                skill_definition_version_id=uuid.UUID(
                    assessment.binding_snapshot["document_analysis"]["skill_definition_version_id"]
                ),
                scope_type="MATTER_DOCUMENT",
                scope_id=uuid.uuid4(),
                input_hash="a" * 64,
                configuration_hash="b" * 64,
                output_artifact_id=uuid.uuid4(),
                status="COMPLETED",
            )
        )
        db.commit()

    base = f"/v1/matters/{matter_id}/definition-assessments/{assessment_id}/execution"
    detailed = client.get(base, headers=auth(root_token))
    assert detailed.status_code == 200, detailed.text
    assert len(detailed.json()["skill_runs"]) == 1

    compact = client.get(f"{base}?include_skill_runs=false", headers=auth(root_token))
    assert compact.status_code == 200, compact.text
    assert compact.json()["skill_runs"] == []


def test_candidate_merge_is_deterministic_deduplicated_and_preserves_provenance() -> None:
    first, second, third, control = (uuid.uuid4() for _ in range(4))
    hits = [
        RetrievalHit(first, 1, "issue_1", 1, 9.0),
        RetrievalHit(second, 1, "issue_1", 2, 8.0),
        RetrievalHit(first, 2, "issue_2", 2, 0.8),
        RetrievalHit(third, 2, "issue_2", 1, 0.9),
    ]
    selected, candidates = merge_retrieval_candidates(
        hits,
        maximum_document_count=4,
        query_quotas={1: 1, 2: 1},
        control_document_ids=[first, second, third, control],
        control_sample_size=1,
        seed="stable-seed",
    )
    repeated, _ = merge_retrieval_candidates(
        list(reversed(hits)),
        maximum_document_count=4,
        query_quotas={1: 1, 2: 1},
        control_document_ids=[control, third, second, first],
        control_sample_size=1,
        seed="stable-seed",
    )
    assert [item.document_id for item in selected] == [item.document_id for item in repeated]
    assert len({item.document_id for item in selected}) == len(selected)
    first_candidate = next(item for item in candidates if item.document_id == first)
    assert {item["criterion_key"] for item in first_candidate.provenance} == {"issue_1", "issue_2"}
    assert selected[-1].document_id == control
    assert selected[-1].reason == "CONTROL_SAMPLE"


def test_retrieval_backfills_after_deduplication_before_freezing_batch() -> None:
    first, second, third, fourth, fifth = (uuid.uuid4() for _ in range(5))
    rows = [
        SimpleNamespace(
            ordinal=1,
            criterion_key="issue_1",
            search_request={"query": "first", "search_mode": "KEYWORD", "size": 2},
            result_count=0,
        ),
        SimpleNamespace(
            ordinal=2,
            criterion_key="issue_2",
            search_request={"query": "second", "search_mode": "KEYWORD", "size": 2},
            result_count=0,
        ),
    ]
    pages = {
        (1, 0): [first, second],
        (2, 0): [first, second],
        (1, 2): [third, fourth],
        (2, 2): [third, fifth],
    }
    calls: list[tuple[int, int]] = []

    def execute_page(row, request):
        calls.append((row.ordinal, request.offset))
        return MatterSearchResponse(
            total=4,
            took_ms=1,
            timed_out=False,
            hits=[
                MatterSearchHit(document_id=document_id, score=1.0, fields={})
                for document_id in pages[(row.ordinal, request.offset)]
            ],
            facets={},
        )

    hits = collect_retrieval_hits(
        rows,  # type: ignore[arg-type]
        maximum_document_count=4,
        execute_page=execute_page,
    )
    selected, _ = merge_retrieval_candidates(
        hits,
        maximum_document_count=4,
        query_quotas={1: 2, 2: 2},
        control_document_ids=[],
        control_sample_size=0,
        seed="stable-seed",
    )

    assert calls == [(1, 0), (2, 0), (1, 2), (2, 2)]
    assert len({item.document_id for item in selected}) == 4
    assert len(selected) == 4
    assert [row.result_count for row in rows] == [4, 4]


def test_retrieval_plan_validator_requires_controlled_nested_search() -> None:
    _validate_retrieval_plan(
        {
            "criteria": [
                {
                    "criterion_key": "issue_1",
                    "criterion_label": "Issue 1",
                    "description": "Hurricane insurance disruption.",
                }
            ],
            "queries": [
                {
                    "criterion_key": "issue_1",
                    "criterion_label": "Issue 1",
                    "rationale": "Find direct evidence.",
                    "quota": 50,
                    "search": {"query": "insurance non-renewal", "search_mode": "HYBRID"},
                }
            ],
        }
    )

    with pytest.raises(AssessmentError, match="controlled search request"):
        _validate_retrieval_plan(
            {
                "criteria": [
                    {
                        "criterion_key": "issue_1",
                        "criterion_label": "Issue 1",
                        "description": "Hurricane insurance disruption.",
                    }
                ],
                "queries": [
                    {
                        "criterion_key": "issue_1",
                        "criterion_label": "Issue 1",
                        "rationale": "Uses the wrong query contract.",
                        "quota": 50,
                        "query_string": "insurance non-renewal",
                        "search_type": "keyword",
                    }
                ],
            }
        )


def test_retrieval_feedback_requires_every_topic_and_exact_target() -> None:
    plan = {
        "criteria": [
            {"criterion_key": "marketing", "criterion_label": "Marketing"},
            {"criterion_key": "space", "criterion_label": "Space"},
        ]
    }
    rows = [
        SimpleNamespace(
            criterion_key="marketing",
            search_request={"query": '"Visit Florida" | advertising', "search_mode": "KEYWORD"},
            quota=50,
            result_count=0,
        ),
        SimpleNamespace(
            criterion_key="space",
            search_request={"query": "moon mission", "search_mode": "HYBRID"},
            quota=50,
            result_count=638,
        ),
    ]

    feedback = retrieval_plan_feedback(
        plan,
        rows,  # type: ignore[arg-type]
        unique_retrieved_count=171,
        selected_count=271,
        target_document_count=1000,
        retrieval_target_count=900,
        control_sample_size=100,
        attempt=1,
    )

    assert feedback["zero_result_topics"] == ["Marketing"]
    assert feedback["target_filled"] is False
    assert feedback["topics"][1]["result_count"] == 638


def test_candidate_merge_fills_target_with_reserved_controls() -> None:
    retrieved = [uuid.uuid4() for _ in range(4)]
    controls = [uuid.uuid4() for _ in range(2)]
    hits = [
        RetrievalHit(document_id, 1, "issue_1", rank, 1.0)
        for rank, document_id in enumerate(retrieved, start=1)
    ]

    selected, _ = merge_retrieval_candidates(
        hits,
        maximum_document_count=6,
        query_quotas={1: 4},
        control_document_ids=[*retrieved, *controls],
        control_sample_size=2,
        seed="stable-seed",
    )

    assert len(selected) == 6
    assert sum(item.reason == "CONTROL_SAMPLE" for item in selected) == 2


def test_failed_assessment_marks_running_step_failed_with_skill_error(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    launched = client.post(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(root_token),
        json={"target_document_count": 1},
    )
    assert launched.status_code == 202, launched.text

    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, uuid.UUID(launched.json()["id"]))
        assert assessment is not None
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        assert workflow is not None
        step = WorkflowStepRun(
            workflow_run_id=workflow.id,
            role_key="retrieval_planner",
            ordinal=1,
            status="RUNNING",
            total_count=1,
        )
        db.add(step)
        db.flush()
        version = db.get(
            SkillDefinitionVersion,
            uuid.UUID(assessment.binding_snapshot["retrieval_planner"]["skill_definition_version_id"]),
        )
        assert version is not None
        skill_run = SkillRun(
            workflow_run_id=workflow.id,
            workflow_step_run_id=step.id,
            skill_definition_version_id=version.id,
            scope_type="MATTER_DEFINITION_REVISION",
            scope_id=assessment.matter_definition_revision_id,
            input_hash="a" * 64,
            configuration_hash="b" * 64,
            status="FAILED",
            error_code="INVALID_OUTPUT",
            error_message="Planner returned an incompatible query.",
        )
        db.add(skill_run)
        db.commit()

        fail_assessment(db, assessment.id, "DBOS retries exhausted")
        db.refresh(step)
        assert step.status == "FAILED"
        assert step.failed_count == 1
        assert step.error_message == "Planner returned an incompatible query."
        assert step.completed_at is not None

    retried = client.post(
        f"/v1/matters/{matter_id}/definition-assessments/{launched.json()['id']}/retry",
        headers=auth(root_token),
    )
    assert retried.status_code == 202, retried.text
    assert retried.json()["status"] == "QUEUED"
    assert retried.json()["error_message"] is None
    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, uuid.UUID(launched.json()["id"]))
        assert assessment is not None
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        step = db.scalar(
            select(WorkflowStepRun).where(WorkflowStepRun.workflow_run_id == assessment.workflow_run_id)
        )
        assert workflow is not None and step is not None
        assert workflow.status == "QUEUED"
        assert ":retry:" in workflow.dbos_workflow_id
        assert step.status == "QUEUED"
        assert assessment.configuration_snapshot["retry_history"]

    repeated_retry = client.post(
        f"/v1/matters/{matter_id}/definition-assessments/{launched.json()['id']}/retry",
        headers=auth(root_token),
    )
    assert repeated_retry.status_code == 409


def test_retrieval_clarification_failure_requires_new_assessment(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    launched = client.post(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(root_token),
        json={"target_document_count": 10},
    )
    assert launched.status_code == 202, launched.text
    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, uuid.UUID(launched.json()["id"]))
        assert assessment is not None
        assessment.status = "FAILED"
        assessment.error_message = "Retrieval needs clarification."
        assessment.configuration_snapshot = {
            **assessment.configuration_snapshot,
            "failure_kind": "RETRIEVAL_NEEDS_CLARIFICATION",
        }
        db.commit()

    retried = client.post(
        f"/v1/matters/{matter_id}/definition-assessments/{launched.json()['id']}/retry",
        headers=auth(root_token),
    )

    assert retried.status_code == 409
    assert "clarified Review Guidance" in retried.json()["error"]["message"]


def test_completed_with_errors_can_retry_failed_documents(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    launched = client.post(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(root_token),
        json={"target_document_count": 5},
    )
    assert launched.status_code == 202, launched.text

    assessment_id = uuid.UUID(launched.json()["id"])
    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        assert assessment is not None
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        assert workflow is not None
        assessment.status = "COMPLETED_WITH_ERRORS"
        assessment.selected_count = 5
        assessment.summarized_count = 3
        assessment.failed_count = 2
        assessment.partial_coverage_count = 1
        assessment.invalid_result_count = 1
        assessment.coverage_snapshot = {"status": "SUFFICIENT"}
        assessment.synthesis_result = {"status": "COMPLETED", "findings": ["stale"]}
        assessment.completed_at = assessment.created_at
        workflow.status = "COMPLETED_WITH_ERRORS"
        db.add_all(
            [
                WorkflowStepRun(
                    workflow_run_id=workflow.id,
                    role_key="document_analysis",
                    ordinal=3,
                    status="COMPLETED_WITH_ERRORS",
                    total_count=5,
                    completed_count=3,
                    failed_count=2,
                ),
                WorkflowStepRun(
                    workflow_run_id=workflow.id,
                    role_key="assessment_synthesis",
                    ordinal=4,
                    status="COMPLETED",
                    total_count=1,
                    completed_count=1,
                ),
            ]
        )
        db.commit()

    retried = client.post(
        f"/v1/matters/{matter_id}/definition-assessments/{assessment_id}/retry",
        headers=auth(root_token),
    )
    assert retried.status_code == 202, retried.text
    assert retried.json()["status"] == "QUEUED"
    assert retried.json()["summarized_count"] == 3
    assert retried.json()["failed_count"] == 0
    assert retried.json()["coverage_snapshot"] is None
    assert retried.json()["synthesis_result"] is None

    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        assert assessment is not None
        steps = list(
            db.scalars(
                select(WorkflowStepRun)
                .where(WorkflowStepRun.workflow_run_id == assessment.workflow_run_id)
                .order_by(WorkflowStepRun.ordinal)
            )
        )
        assert [step.status for step in steps] == ["QUEUED", "QUEUED"]
        assert steps[0].completed_count == 3
        assert steps[0].failed_count == 0
        assert steps[1].completed_count == 0
        assert assessment.configuration_snapshot["retry_history"][-1]["previous_failed_document_count"] == 2


def test_reconciliation_resumes_pending_parent_after_every_document_is_terminal(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    launched = client.post(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(root_token),
        json={"target_document_count": 2},
    )
    assert launched.status_code == 202, launched.text
    assessment_id = uuid.UUID(launched.json()["id"])
    resume_calls: list[tuple[str, str | None]] = []

    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        assert assessment is not None
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        assert workflow is not None
        job = MatterDocumentImportJob(
            matter_id=uuid.UUID(matter_id),
            source_collection_id=uuid.uuid4(),
            selection_type="EXPLICIT",
            selection={},
            selection_summary="Durable reconciliation fixture",
            status="COMPLETED",
            workflow_id=f"fixture:{uuid.uuid4()}",
            created_by_user_id=root_admin.id,
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
            for _ in range(2)
        ]
        db.add_all(documents)
        db.flush()
        selected = [
            SelectedCandidate(document.id, 1.0, ({"query_ordinal": 1},), "CRITERION_QUOTA")
            for document in documents
        ]
        materialize_assessment_batch(
            db,
            assessment,
            selected=selected,
            all_candidates=selected,
            query_plan={"criteria": [], "queries": []},
        )
        db.flush()
        for run_document in db.scalars(
            select(ReviewBatchRunDocument).where(
                ReviewBatchRunDocument.review_batch_run_id == assessment.review_batch_run_id
            )
        ):
            run_document.status = "COMPLETED"
        db.commit()

        durable = SimpleNamespace(
            workflow_id=workflow.dbos_workflow_id,
            status="PENDING",
            error=None,
        )
        fake_client = SimpleNamespace(
            list_workflows=lambda **_: [durable],
            resume_workflow=lambda workflow_id, queue_name=None: resume_calls.append(
                (workflow_id, queue_name)
            ),
        )
        monkeypatch.setattr(
            "app.assessment_workflow_reconciliation.get_dbos_client",
            lambda: fake_client,
        )
        settings = get_settings().model_copy(update={"dbos_enabled": True})

        reconciled = reconcile_terminal_assessment_workflows(db, [assessment], settings=settings)
        reconcile_terminal_assessment_workflows(db, [assessment], settings=settings)

        assert reconciled == {assessment.id}
        assert resume_calls == [(workflow.dbos_workflow_id, "definition-assessments")]
        assert assessment.configuration_snapshot["reconciled_durable_workflow_ids"] == [
            workflow.dbos_workflow_id
        ]


@pytest.mark.parametrize("terminal_status", ["COMPLETED", "FAILED"])
def test_terminal_assessment_can_regenerate_only_synthesis(
    client: TestClient,
    root_token: str,
    root_admin,
    terminal_status: str,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    launched = client.post(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(root_token),
        json={"target_document_count": 5},
    )
    assert launched.status_code == 202, launched.text
    assessment_id = uuid.UUID(launched.json()["id"])
    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        assert assessment is not None
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        assert workflow is not None
        assessment.status = terminal_status
        assessment.selected_count = 5
        assessment.summarized_count = 5
        assessment.synthesis_result = {
            "status": "COMPLETED",
            "clarification_questions": [],
        }
        workflow.status = terminal_status
        db.commit()

    regenerated = client.post(
        f"/v1/matters/{matter_id}/definition-assessments/{assessment_id}/regenerate-synthesis",
        headers=auth(root_token),
    )
    assert regenerated.status_code == 202, regenerated.text
    assert regenerated.json()["status"] == "QUEUED"
    assert regenerated.json()["summarized_count"] == 5
    assert regenerated.json()["synthesis_result"] is None
    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        assert assessment is not None
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        assert workflow is not None
        assert ":synthesis:" in workflow.dbos_workflow_id
        assert workflow.code_version == "13"
        assert (
            assessment.binding_snapshot["assessment_synthesis"]["output_schema_key"]
            == "matter_definition_assessment_synthesis_output_v5"
        )
        step = db.scalar(
            select(WorkflowStepRun).where(
                WorkflowStepRun.workflow_run_id == workflow.id,
                WorkflowStepRun.ordinal == 4,
            )
        )
        assert step is not None
        assert step.status == "QUEUED"


def test_completed_assessment_can_reanalyze_same_frozen_batch(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    launched = client.post(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(root_token),
        json={"target_document_count": 2},
    )
    assert launched.status_code == 202, launched.text
    assessment_id = uuid.UUID(launched.json()["id"])
    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        assert assessment is not None
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        assert workflow is not None
        job = MatterDocumentImportJob(
            matter_id=uuid.UUID(matter_id),
            source_collection_id=uuid.uuid4(),
            selection_type="EXPLICIT",
            selection={},
            selection_summary="Reanalysis fixture",
            status="COMPLETED",
            workflow_id=f"fixture:{uuid.uuid4()}",
            created_by_user_id=root_admin.id,
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
            for _ in range(2)
        ]
        db.add_all(documents)
        db.flush()
        selected = [
            SelectedCandidate(document.id, 1.0, ({"query_ordinal": 1},), "CRITERION_QUOTA")
            for document in documents
        ]
        materialize_assessment_batch(
            db,
            assessment,
            selected=selected,
            all_candidates=selected,
            query_plan={"criteria": [], "queries": []},
        )
        db.flush()
        previous_review_run_id = assessment.review_batch_run_id
        assert previous_review_run_id is not None
        previous_review_run = db.get(ReviewBatchRun, previous_review_run_id)
        assert previous_review_run is not None
        previous_review_run.status = "COMPLETED"
        for run_document in db.scalars(
            select(ReviewBatchRunDocument).where(
                ReviewBatchRunDocument.review_batch_run_id == previous_review_run_id
            )
        ):
            run_document.status = "COMPLETED"
        assessment.status = "COMPLETED"
        assessment.summarized_count = 2
        assessment.synthesis_result = {"status": "COMPLETED"}
        workflow.status = "COMPLETED"
        db.commit()

    regenerated = client.post(
        f"/v1/matters/{matter_id}/definition-assessments/{assessment_id}/regenerate-document-analyses",
        headers=auth(root_token),
    )
    assert regenerated.status_code == 202, regenerated.text
    assert regenerated.json()["status"] == "QUEUED"
    assert regenerated.json()["summarized_count"] == 0
    assert regenerated.json()["synthesis_result"] is None

    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        assert assessment is not None
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        assert workflow is not None
        assert assessment.review_batch_run_id != previous_review_run_id
        current_review_run = db.get(ReviewBatchRun, assessment.review_batch_run_id)
        assert current_review_run is not None
        assert current_review_run.parent_run_id == previous_review_run_id
        assert current_review_run.status == "QUEUED"
        assert set(
            db.scalars(
                select(ReviewBatchRunDocument.status).where(
                    ReviewBatchRunDocument.review_batch_run_id == current_review_run.id
                )
            )
        ) == {"QUEUED"}
        assert set(
            db.scalars(
                select(ReviewBatchRunDocument.status).where(
                    ReviewBatchRunDocument.review_batch_run_id == previous_review_run_id
                )
            )
        ) == {"COMPLETED"}
        assert ":analysis:" in workflow.dbos_workflow_id
        assert workflow.code_version == "13"
        history = assessment.configuration_snapshot["document_analysis_regeneration_history"]
        assert history[-1]["previous_review_batch_run_id"] == str(previous_review_run_id)
        steps = list(
            db.scalars(
                select(WorkflowStepRun)
                .where(
                    WorkflowStepRun.workflow_run_id == workflow.id,
                    WorkflowStepRun.ordinal.in_((3, 4)),
                )
                .order_by(WorkflowStepRun.ordinal)
            )
        )
        assert [step.status for step in steps] == ["QUEUED", "QUEUED"]
        assert [step.total_count for step in steps] == [2, 1]


def test_synthesis_loads_only_latest_completed_analysis_per_document(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    launched = client.post(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(root_token),
        json={"target_document_count": 1},
    )
    assert launched.status_code == 202, launched.text
    assessment_id = uuid.UUID(launched.json()["id"])
    document_id = uuid.uuid4()
    old_artifact_id = uuid.uuid4()
    current_artifact_id = uuid.uuid4()

    def read_fixture(*, artifact_id, **_kwargs) -> bytes:
        return json.dumps(
            {
                "result": {
                    "summary": [{"text": str(artifact_id), "citation_ids": ["¶1"]}],
                    "coverage": {"status": "COMPLETE"},
                }
            }
        ).encode()

    monkeypatch.setattr("app.assessment_execution.read_artifact_bytes", read_fixture)
    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        matter = db.get(Matter, uuid.UUID(matter_id))
        assert assessment is not None and matter is not None
        step = WorkflowStepRun(
            workflow_run_id=assessment.workflow_run_id,
            role_key="document_analysis",
            ordinal=3,
            status="COMPLETED",
            total_count=1,
            completed_count=1,
        )
        db.add(step)
        db.flush()
        version_id = uuid.UUID(
            assessment.binding_snapshot["document_analysis"]["skill_definition_version_id"]
        )
        db.add_all(
            [
                SkillRun(
                    workflow_run_id=assessment.workflow_run_id,
                    workflow_step_run_id=step.id,
                    skill_definition_version_id=version_id,
                    scope_type="MATTER_DOCUMENT",
                    scope_id=document_id,
                    input_hash="a" * 64,
                    configuration_hash="b" * 64,
                    output_artifact_id=old_artifact_id,
                    status="COMPLETED",
                    created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                ),
                SkillRun(
                    workflow_run_id=assessment.workflow_run_id,
                    workflow_step_run_id=step.id,
                    skill_definition_version_id=version_id,
                    scope_type="MATTER_DOCUMENT",
                    scope_id=document_id,
                    input_hash="c" * 64,
                    configuration_hash="d" * 64,
                    output_artifact_id=current_artifact_id,
                    status="COMPLETED",
                    created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                ),
            ]
        )
        db.commit()
        analyses, partial, invalid = _load_document_analyses(db, assessment, matter)

    assert partial == 0
    assert invalid == 0
    assert len(analyses) == 1
    assert analyses[0]["output_artifact_id"] == str(current_artifact_id)


def test_synthesis_coverage_policy_blocks_substantive_results_for_small_or_incomplete_samples() -> None:
    assessment = SimpleNamespace(selected_count=10, skipped_count=2, failed_count=3)
    insufficient = _coverage_envelope(assessment, successful=2, partial=1, invalid=1)
    assert insufficient["status"] == "INSUFFICIENT"
    assert insufficient["successful_document_ratio"] == 0.2
    assert insufficient["policy"]["version"] == "assessment_synthesis_coverage_v1"

    sufficient = _coverage_envelope(assessment, successful=5, partial=1, invalid=0)
    assert sufficient["status"] == "SUFFICIENT"


def test_synthesis_context_exposes_computed_counts_and_refinement_signals() -> None:
    coverage = {
        "selected_document_count": 3,
        "skipped_document_count": 1,
        "failed_document_count": 0,
        "partial_coverage_document_count": 0,
        "invalid_result_count": 0,
    }
    analyses = [
        {
            "matter_document_id": str(uuid.uuid4()),
            "analysis": {
                "determination": "RESPONSIVE",
                "criterion_matches": [
                    {
                        "criterion_key": "issue_7",
                        "label": "Related insurance lines",
                        "match_type": "NEAR_MISS",
                        "reasoning": "The boundary is unclear.",
                        "citation_ids": ["¶2"],
                    }
                ],
                "clarification_requests": [],
                "limitations": ["An attachment was unavailable."],
            },
        },
        {
            "matter_document_id": str(uuid.uuid4()),
            "analysis": {
                "determination": "NON_RESPONSIVE",
                "criterion_matches": [],
                "clarification_requests": [],
                "limitations": [],
            },
        },
    ]

    statistics, signals = _synthesis_context(analyses, coverage)

    assert statistics["analyzed_document_count"] == 2
    assert statistics["determination_counts"] == {"NON_RESPONSIVE": 1, "RESPONSIVE": 1}
    assert signals["near_miss_count"] == 1
    assert signals["near_misses"][0]["paragraph_ids"] == ["¶2"]
    assert signals["recurring_near_miss_pattern_count"] == 0
    assert signals["document_limitation_count"] == 1


def test_recurring_near_miss_patterns_require_evidence_backed_questions() -> None:
    document_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    analyses = [
        {
            "matter_document_id": document_id,
            "analysis": {
                "summary": [{"text": "Recurring boundary evidence", "citation_ids": ["¶1"]}],
                "determination": "NON_RESPONSIVE",
                "criterion_matches": [
                    {
                        "criterion_key": criterion_key,
                        "label": "Related insurance lines",
                        "match_type": "NEAR_MISS",
                        "reasoning": "The document concerns another insurance line without the required nexus.",
                        "citation_ids": ["¶1"],
                    }
                ],
                "clarification_requests": [],
                "limitations": [],
            },
        }
        for document_id, criterion_key in zip(document_ids, ["Issue 7", "ISSUE_7_RELATED_LINES"], strict=True)
    ]
    coverage = {
        "status": "SUFFICIENT",
        "selected_document_count": 2,
        "skipped_document_count": 0,
        "failed_document_count": 0,
        "partial_coverage_document_count": 0,
        "invalid_result_count": 0,
    }
    _, signals = _synthesis_context(analyses, coverage)
    assert signals["recurring_near_miss_pattern_count"] == 1
    assert signals["recurring_near_miss_patterns"][0]["signal_id"] == "NEAR_MISS_ISSUE_7"
    assert signals["recurring_near_miss_patterns"][0]["document_count"] == 2

    dimensions = [
        {
            "dimension": dimension,
            "conclusion": "NO_REFINEMENT_NEEDED",
            "rationale": "No change proposed.",
            "evidence": [],
        }
        for dimension in sorted(
            {
                "INCLUSION_EXCLUSION_BOUNDARIES",
                "UNCOVERED_SUBJECTS",
                "CONFLICTING_TREATMENT",
                "TEMPORAL_SCOPE",
                "GEOGRAPHIC_SCOPE",
                "ACTOR_ENTITY_SCOPE",
            }
        )
    ]
    no_refinement = {
        "refinement_assessment": {
            "outcome": "NO_REFINEMENT_WARRANTED",
            "rationale": "The existing boundary was applied consistently.",
            "evaluated_dimensions": dimensions,
        },
        "clarification_questions": [],
    }
    with pytest.raises(ValueError, match="require clarification questions"):
        _validate_synthesis_refinement(no_refinement, analyses, coverage, signals)

    evidence = [
        {
            "matter_document_id": document_ids[0],
            "paragraph_ids": ["¶1"],
            "reason": "Representative recurring boundary evidence.",
        }
    ]
    proposed = {
        "refinement_assessment": {
            "outcome": "QUESTIONS_PROPOSED",
            "rationale": "The recurring boundary requires confirmation.",
            "evaluated_dimensions": [
                {
                    **item,
                    "conclusion": "QUESTION_NEEDED",
                    "evidence": evidence,
                }
                if item["dimension"] == "INCLUSION_EXCLUSION_BOUNDARIES"
                else item
                for item in dimensions
            ],
        },
        "clarification_questions": [
            {
                "question": "Should unrelated insurance lines remain excluded without a crisis nexus?",
                "evidence": evidence,
                "suggested_answers": ["Yes, keep them excluded.", "No, include them."],
            }
        ],
    }
    _validate_synthesis_refinement(proposed, analyses, coverage, signals)


def test_synthesis_refinement_requires_questions_or_structured_no_refinement_explanation() -> None:
    document_id = str(uuid.uuid4())
    analyses = [
        {
            "matter_document_id": document_id,
            "analysis": {
                "summary": [{"text": "Evidence", "citation_ids": ["¶1"]}],
                "criterion_matches": [],
            },
        }
    ]
    dimensions = [
        {
            "dimension": dimension,
            "conclusion": "NO_REFINEMENT_NEEDED",
            "rationale": "The current instruction is consistently applied.",
            "evidence": [],
        }
        for dimension in sorted(
            {
                "INCLUSION_EXCLUSION_BOUNDARIES",
                "UNCOVERED_SUBJECTS",
                "CONFLICTING_TREATMENT",
                "TEMPORAL_SCOPE",
                "GEOGRAPHIC_SCOPE",
                "ACTOR_ENTITY_SCOPE",
            }
        )
    ]
    valid = {
        "refinement_assessment": {
            "outcome": "NO_REFINEMENT_WARRANTED",
            "rationale": "No recurring ambiguity was found after evaluating every dimension.",
            "evaluated_dimensions": dimensions,
        },
        "clarification_questions": [],
    }
    _validate_synthesis_refinement(valid, analyses, {"status": "SUFFICIENT"})

    invalid = {
        **valid,
        "refinement_assessment": {**valid["refinement_assessment"], "outcome": "QUESTIONS_PROPOSED"},
    }
    with pytest.raises(ValueError, match="requires at least one"):
        _validate_synthesis_refinement(invalid, analyses, {"status": "SUFFICIENT"})

    proposed = {
        "refinement_assessment": {
            "outcome": "QUESTIONS_PROPOSED",
            "rationale": "A recurring boundary needs a policy choice.",
            "evaluated_dimensions": [
                {
                    **item,
                    "conclusion": "QUESTION_NEEDED",
                    "evidence": [
                        {
                            "matter_document_id": document_id,
                            "paragraph_ids": ["¶1"],
                            "reason": "Representative boundary evidence.",
                        }
                    ],
                }
                if item["dimension"] == "INCLUSION_EXCLUSION_BOUNDARIES"
                else item
                for item in dimensions
            ],
        },
        "clarification_questions": [
            {
                "question": "Should this boundary be included?",
                "suggested_answers": ["Include it.", "Exclude it."],
                "evidence": [
                    {
                        "matter_document_id": document_id,
                        "paragraph_ids": ["¶1"],
                        "reason": "Representative boundary evidence.",
                    }
                ],
            }
        ],
    }
    _validate_synthesis_refinement(proposed, analyses, {"status": "SUFFICIENT"})


def test_synthesis_expands_bare_issue_references_and_rejects_wrong_names() -> None:
    matter_definition = """# Matter Definition

## Issues

### Issue 4: Executive Action and Inaction

### Issue 8: Economic Consequences
"""
    output = {
        "clarification_questions": [
            {
                "question": (
                    "Should executive disaster recovery recommendations concerning housing programs be "
                    "classified as responsive under Issue 4 when they do not propose specific property "
                    "insurance policy reforms?"
                ),
                "rationale": "This distinguishes Issue 4 from Issue 8.",
            }
        ]
    }

    _enforce_named_issue_references(output, matter_definition)

    question = output["clarification_questions"][0]
    assert "Issue 4: Executive Action and Inaction" in question["question"]
    assert question["rationale"] == (
        "This distinguishes Issue 4: Executive Action and Inaction from "
        "Issue 8: Economic Consequences."
    )

    wrong_name = {"question": "Should Issue 4: Property Insurance Policy Reforms apply?"}
    with pytest.raises(ValueError, match="must use the exact issue reference"):
        _enforce_named_issue_references(wrong_name, matter_definition)


def test_long_document_uses_one_query_directed_skill_run(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    launched = client.post(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(root_token),
        json={"target_document_count": 1},
    )
    assert launched.status_code == 202, launched.text
    assessment_id = uuid.UUID(launched.json()["id"])
    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        assert assessment is not None
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        revision = db.get(MatterDefinitionRevision, assessment.matter_definition_revision_id)
        assert workflow is not None and revision is not None
        job = MatterDocumentImportJob(
            matter_id=uuid.UUID(matter_id),
            source_collection_id=uuid.uuid4(),
            selection_type="EXPLICIT",
            selection={},
            selection_summary="Map/reduce fixture",
            status="COMPLETED",
            workflow_id=f"fixture:{uuid.uuid4()}",
            created_by_user_id=root_admin.id,
        )
        db.add(job)
        db.flush()
        document = MatterDocument(
            matter_id=uuid.UUID(matter_id),
            source_collection_id=job.source_collection_id,
            collection_item_id=uuid.uuid4(),
            added_by_import_job_id=job.id,
        )
        db.add(document)
        step = WorkflowStepRun(
            workflow_run_id=workflow.id,
            role_key="document_analysis",
            ordinal=3,
            fan_out_group="documents",
            status="RUNNING",
            total_count=1,
        )
        db.add(step)
        db.flush()
        version = db.get(
            SkillDefinitionVersion,
            uuid.UUID(assessment.binding_snapshot["document_analysis"]["skill_definition_version_id"]),
        )
        assert version is not None
        paragraph_map = segment_paragraphs("evidence " * 120)
        plan = build_document_map_plan(paragraph_map, max_characters=120, overlap_segments=0)
        assert len(plan.windows) > 1
        output = {
            "summary": [{"text": "The document contains evidence.", "citation_ids": ["¶1"]}],
            "determination": "RESPONSIVE",
            "confidence": 0.9,
            "responsiveness_summary": [{"text": "It matches the instruction.", "citation_ids": ["¶1"]}],
            "criterion_matches": [],
            "scope_analysis": [],
            "countervailing_considerations": [],
            "clarification_requests": [],
            "limitations": [],
        }
        result, skill_run, diagnostics = asyncio.run(
            _execute_document_analysis(
                db,
                assessment=assessment,
                workflow=workflow,
                step=step,
                version=version,
                revision=revision,
                document=document,
                source=SimpleNamespace(artifact_id=uuid.uuid4(), content_hash="b" * 64),
                paragraph_map=paragraph_map,
                plan=plan,
                model=TestModel(custom_output_args=output),
            )
        )
        db.commit()
        assert result["coverage"]["status"] == "PARTIAL"
        assert result["coverage"]["analyzed_paragraph_ids"] == ["¶1"]
        assert diagnostics["mode"] == "QUERY_DIRECTED_LARGE_DOCUMENT"
        assert diagnostics["selection_kind"] == "CONTROL_SAMPLE"
        assert skill_run.status == "COMPLETED"
        assert skill_run.request_count == 1
        # Document workers update only their own usage. The shared workflow
        # aggregate is refreshed later under the assessment rollup lock.
        assert step.request_count == 0
        assert len(list(db.scalars(select(SkillRun).where(SkillRun.scope_id == document.id)))) == 1
        invocations = list(db.scalars(select(ModelInvocation).where(ModelInvocation.skill_run_id == skill_run.id)))
        assert len(invocations) == 1
        assert {item.attempt for item in invocations} == {1}


def test_provider_batch_document_request_uses_final_model_schema_without_coverage() -> None:
    paragraph_map = segment_paragraphs("One paragraph of evidence.")
    request = _structured_request(
        SimpleNamespace(matter_id=uuid.uuid4(), definition_content_hash="a" * 64),
        SimpleNamespace(tenant_id=uuid.uuid4()),
        SimpleNamespace(content_markdown="Review guidance", matter_definition_id=uuid.uuid4()),
        SimpleNamespace(
            id=uuid.uuid4(),
            instructions="Analyze the document.",
            output_schema_key="document_analysis_v3",
            output_schema={},
            model_key="configured-default",
            model_policy={"temperature": 0},
            limits={"max_requests": 20},
            cache_policy={},
        ),
        SimpleNamespace(id=uuid.uuid4(), collection_item_id=uuid.uuid4()),
        SimpleNamespace(artifact_id=uuid.uuid4(), content_hash="b" * 64),
        paragraph_map,
    )

    assert "coverage" not in request.output_schema["properties"]
    assert request.dynamic_input["analysis_stage"] == "DOCUMENT"
    assert request.instruction_layers[-1].key == "analysis_stage"
