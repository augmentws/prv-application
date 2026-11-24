import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace

from app import review_programs
from app.artifact_gateway import AnalysisInputItemSnapshot, TextSourceReference
from app.document_analysis_schemas import is_supported_evidence_citation
from app.evidence_first_launcher import EvidenceInputUnavailableError
from app.privilege_evidence_context import (
    build_privilege_family_evidence_context,
    privilege_evidence_family_groups,
)
from app.privilege_review_programs import (
    _evidence_packets_payload,
    _family_context_payload,
    _validate_evidence_packet_text_sources,
)
from app.routers.review_programs import _live_privilege_evidence_progress
from app.workflows import evidence_assessments, privilege_review_adjudication, privilege_review_evidence
from app.workflows.privilege_review_adjudication import (
    PRIVILEGE_ADJUDICATION_QUEUE,
    _family_conflict_document_ids,
    _recommendation,
)
from app.workflows.privilege_review_evidence import PRIVILEGE_EVIDENCE_QUEUE


def test_privilege_evidence_parent_queue_is_registered() -> None:
    assert PRIVILEGE_EVIDENCE_QUEUE.name == "privilege-review-evidence"


def test_privilege_evidence_model_uses_dedicated_override(monkeypatch) -> None:
    monkeypatch.setattr(
        review_programs,
        "get_settings",
        lambda: SimpleNamespace(
            agent_privilege_evidence_extraction_model="openai:gpt-6-luna",
            agent_evidence_extraction_model="google:gemini-3.5-flash-lite",
        ),
    )

    assert review_programs._privilege_evidence_extraction_model() == "openai:gpt-6-luna"


def test_privilege_evidence_model_falls_back_to_general_extraction_model(monkeypatch) -> None:
    monkeypatch.setattr(
        review_programs,
        "get_settings",
        lambda: SimpleNamespace(
            agent_privilege_evidence_extraction_model=None,
            agent_evidence_extraction_model="google:gemini-3.5-flash-lite",
        ),
    )

    assert (
        review_programs._privilege_evidence_extraction_model()
        == "google:gemini-3.5-flash-lite"
    )


def test_evidence_orchestration_freezes_privilege_stage_model_override(monkeypatch) -> None:
    spec = SimpleNamespace(
        key="evidence-extraction",
        code_version="test-v1",
        roles=(SimpleNamespace(key="evidence_extraction"),),
    )
    version = SimpleNamespace(model_key="matter-definition-evidence-extraction")
    monkeypatch.setattr(evidence_assessments, "_STAGE_SPECS", {"EXTRACTION": spec})
    monkeypatch.setattr(
        evidence_assessments,
        "resolve_workflow_skill_bindings",
        lambda *_args, **_kwargs: {"evidence_extraction": (None, None, version)},
    )
    monkeypatch.setattr(evidence_assessments, "binding_snapshot", lambda _resolved: {"frozen": True})
    monkeypatch.setattr(
        evidence_assessments,
        "resolve_agent_model",
        lambda *_args, **_kwargs: "google:gemini-3.5-flash-lite",
    )

    stages = evidence_assessments._frozen_stage_configuration(
        object(),
        tenant_id=uuid.uuid4(),
        model_overrides={"EXTRACTION": "openai:gpt-6-luna"},
    )

    assert stages["EXTRACTION"]["resolved_model"] == "openai:gpt-6-luna"


def test_privilege_adjudication_parent_queue_is_registered() -> None:
    assert PRIVILEGE_ADJUDICATION_QUEUE.name == "privilege-review-adjudication"


def test_privilege_evidence_collapses_split_packets_to_one_family_execution() -> None:
    first, second, third = (str(uuid.uuid4()) for _ in range(3))

    groups = privilege_evidence_family_groups(
        {
            "packets": [
                {
                    "packet_key": "family-a:1",
                    "family_key": "family-a",
                    "family_id": "family-id-a",
                    "review_target_document_ids": [first, second],
                },
                {
                    "packet_key": "family-a:2",
                    "family_key": "family-a",
                    "family_id": "family-id-a",
                    "review_target_document_ids": [third],
                },
                {
                    "packet_key": "standalone:1",
                    "family_key": "standalone",
                    "family_id": None,
                    "review_target_document_ids": [str(uuid.uuid4())],
                },
            ]
        }
    )

    family = next(value for value in groups if value["family_key"] == "family-a")
    assert family["anchor_document_id"] == first
    assert family["review_target_document_ids"] == [first, second, third]
    assert family["review_target_document_count"] == 3
    assert family["packet_keys"] == ["family-a:1", "family-a:2"]
    assert len(groups) == 2


def test_privilege_adjudication_maps_only_completed_decisions() -> None:
    complete = {"evidence_complete": True, "adjudication_complete": True}
    assert _recommendation(
        {
            **complete,
            "document_decision_status": "COMPLETED",
            "determination": "RESPONSIVE",
            "disposition_counts": {"MATCH": 1, "NO_MATCH": 3},
        }
    ) == "privileged"
    assert _recommendation(
        {
            **complete,
            "document_decision_status": "COMPLETED",
            "determination": "NON_RESPONSIVE",
            "disposition_counts": {"NO_MATCH": 4},
        }
    ) == "not_privileged"
    assert _recommendation(
        {
            **complete,
            "document_decision_status": "COMPLETED",
            "determination": "NON_RESPONSIVE",
            "disposition_counts": {"NEAR_MISS": 1, "NO_MATCH": 3},
        }
    ) == "needs_review"
    assert _recommendation(
        {
            **complete,
            "document_decision_status": "COMPLETED",
            "determination": "NON_RESPONSIVE",
            "disposition_counts": {"NEAR_MISS": 1, "NO_MATCH": 2},
            "criterion_dispositions": {
                "attorney_client": "NEAR_MISS",
                "mixed_purpose": "NO_MATCH",
                "work_product": "NO_MATCH",
            },
            "criterion_signal_counts": {
                "attorney_client": {"NEAR_MISS": 2, "COUNTERVAILING": 1},
            },
        }
    ) == "not_privileged"
    assert _recommendation(
        {
            **complete,
            "document_decision_status": "COMPLETED",
            "determination": "NON_RESPONSIVE",
            "disposition_counts": {"NEAR_MISS": 1, "NO_MATCH": 2},
            "criterion_dispositions": {
                "attorney_client": "NEAR_MISS",
                "mixed_purpose": "NO_MATCH",
                "work_product": "NO_MATCH",
            },
            "criterion_signal_counts": {
                "attorney_client": {"SUPPORT": 1, "NEAR_MISS": 1},
            },
        }
    ) == "needs_review"
    assert _recommendation(
        {
            **complete,
            "document_decision_status": "COMPLETED",
            "determination": "NON_RESPONSIVE",
            "unresolved_criterion_keys": ["attorney_client_privilege"],
        }
    ) == "needs_review"
    assert _recommendation(
        {**complete, "document_decision_status": "COMPLETED", "determination": "UNCLEAR"}
    ) == "needs_review"
    assert _recommendation(
        {**complete, "document_decision_status": "FAILED", "determination": "RESPONSIVE"}
    ) == "needs_review"
    assert _recommendation(
        {
            "document_decision_status": "COMPLETED",
            "determination": "NON_RESPONSIVE",
            "disposition_counts": {"NO_MATCH": 4},
        }
    ) == "needs_review"


def test_live_privilege_evidence_progress_uses_document_outcomes() -> None:
    class Result:
        @staticmethod
        def all() -> list[tuple[str, int]]:
            return [("COMPLETED", 75), ("FAILED", 3), ("QUEUED", 1_714)]

    class Database:
        @staticmethod
        def execute(_statement):
            return Result()

    progress = _live_privilege_evidence_progress(
        Database(),
        SimpleNamespace(
            status="RUNNING",
            current_stage="PRIVILEGE_EVIDENCE",
            progress_summary={
                "privilege_evidence": {
                    "state": "RUNNING",
                    "review_batch_run_id": str(uuid.uuid4()),
                    "review_document_count": 1_792,
                    "completed_document_count": 0,
                    "failed_document_count": 0,
                }
            },
        ),
    )

    assert progress["privilege_evidence"]["completed_document_count"] == 75
    assert progress["privilege_evidence"]["failed_document_count"] == 3
    assert progress["privilege_evidence"]["queued_document_count"] == 1_714


def test_failed_privilege_parent_reports_draining_children() -> None:
    class Result:
        @staticmethod
        def all() -> list[tuple[str, int]]:
            return [("COMPLETED", 1_028), ("FAILED", 52), ("QUEUED", 712)]

    class Database:
        @staticmethod
        def execute(_statement):
            return Result()

    progress = _live_privilege_evidence_progress(
        Database(),
        SimpleNamespace(
            status="FAILED",
            current_stage="PRIVILEGE_EVIDENCE",
            progress_summary={
                "privilege_evidence": {
                    "state": "RUNNING",
                    "review_batch_run_id": str(uuid.uuid4()),
                    "review_document_count": 1_792,
                }
            },
        ),
    )

    assert progress["privilege_evidence"]["state"] == "DRAINING_AFTER_FAILURE"
    assert progress["privilege_evidence"]["queued_document_count"] == 712


def test_privilege_evidence_checkpoint_failure_does_not_stop_chunk(monkeypatch) -> None:
    def fail_checkpoint(_assessment_id: str):
        raise RuntimeError("checkpoint unavailable")

    monkeypatch.setattr(privilege_review_evidence, "checkpoint_chunk", fail_checkpoint)

    assert privilege_review_evidence.checkpoint_chunk_without_stopping("assessment", 4) == {
        "checkpoint_failed": True
    }


def test_deterministic_evidence_input_failure_is_not_retried(monkeypatch) -> None:
    monkeypatch.setattr(evidence_assessments, "SessionLocal", lambda: nullcontext(SimpleNamespace()))

    def unavailable(*_args, **_kwargs):
        raise EvidenceInputUnavailableError(
            "NO_PREFERRED_TEXT_SOURCE",
            "Selected document has no preferred text source: document-1",
        )

    monkeypatch.setattr(evidence_assessments, "_prepared_extraction", unavailable)

    result = evidence_assessments.prepare_extraction_fanout.__wrapped__(
        "assessment-1",
        "document-1",
        "actor-1",
    )

    assert result == {
        "preparation_failure": {
            "failure_code": "EXTRACTION_NO_PREFERRED_TEXT_SOURCE",
            "failure_message": "Selected document has no preferred text source: document-1",
            "failure_retriable": False,
        }
    }


def test_privilege_document_workflow_records_direct_input_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        privilege_review_evidence,
        "prepare_extraction_fanout",
        lambda *_args: {
            "preparation_failure": {
                "failure_code": "EXTRACTION_NO_PREFERRED_TEXT_SOURCE",
                "failure_message": "No extractable text",
                "failure_retriable": False,
            }
        },
    )
    monkeypatch.setattr(
        privilege_review_evidence,
        "record_document",
        lambda _assessment_id, _document_id, result: result,
    )

    result = privilege_review_evidence.document_workflow.__wrapped__(
        "assessment-1",
        "document-1",
        "actor-1",
    )

    assert result["status"] == "FAILED"
    assert result["failure_code"] == "EXTRACTION_NO_PREFERRED_TEXT_SOURCE"
    assert result["failure_message"] == "No extractable text"
    assert result["failure_retriable"] is False


def test_privilege_provider_batch_failure_routes_documents_to_realtime(monkeypatch) -> None:
    monkeypatch.setattr(
        privilege_review_evidence,
        "submit_provider_batch",
        lambda _batch_id, _actor_user_id: "JOB_STATE_FAILED",
    )
    monkeypatch.setattr(
        privilege_review_evidence,
        "fallback_provider_batch",
        lambda _batch_id, _message: ["document-1", "document-2"],
    )

    assert privilege_review_evidence.provider_batch_workflow.__wrapped__("batch", "actor", 15.0) == {
        "completed": 0,
        "fallback_document_ids": ["document-1", "document-2"],
    }


def test_privilege_provider_batch_success_finalizes_without_realtime_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        privilege_review_evidence,
        "submit_provider_batch",
        lambda _batch_id, _actor_user_id: "JOB_STATE_SUCCEEDED",
    )
    monkeypatch.setattr(
        privilege_review_evidence,
        "finalize_provider_batch",
        lambda _batch_id, _actor_user_id: {"completed": 4, "fallback_document_ids": []},
    )

    assert privilege_review_evidence.provider_batch_workflow.__wrapped__("batch", "actor", 15.0) == {
        "completed": 4,
        "fallback_document_ids": [],
    }


def test_privilege_adjudication_batch_failure_routes_families_to_realtime(monkeypatch) -> None:
    monkeypatch.setattr(
        privilege_review_adjudication,
        "submit_provider_batch",
        lambda _batch_id, _actor_user_id: "JOB_STATE_FAILED",
    )
    monkeypatch.setattr(
        privilege_review_adjudication,
        "fallback_provider_batch",
        lambda _batch_id, _message: ["family-anchor-1", "family-anchor-2"],
    )

    assert privilege_review_adjudication.provider_batch_workflow.__wrapped__(
        "batch", "actor", 15.0
    ) == {
        "completed_families": [],
        "fallback_anchor_document_ids": ["family-anchor-1", "family-anchor-2"],
    }


def test_privilege_adjudication_batch_success_returns_family_results(monkeypatch) -> None:
    expected = {
        "completed_families": [{"document": {"matter_document_id": "anchor"}, "result": {}}],
        "fallback_anchor_document_ids": [],
    }
    monkeypatch.setattr(
        privilege_review_adjudication,
        "submit_provider_batch",
        lambda _batch_id, _actor_user_id: "JOB_STATE_SUCCEEDED",
    )
    monkeypatch.setattr(
        privilege_review_adjudication,
        "finalize_provider_batch",
        lambda _batch_id, _actor_user_id: expected,
    )

    assert (
        privilege_review_adjudication.provider_batch_workflow.__wrapped__(
            "batch", "actor", 15.0
        )
        == expected
    )


def test_privilege_adjudication_batch_publishes_results_when_it_finishes(monkeypatch) -> None:
    expected = {
        "completed_families": [
            {
                "document": {
                    "matter_document_id": "anchor",
                    "matter_document_ids": ["document-1", "document-2"],
                },
                "result": {"status": "COMPLETED"},
            }
        ],
        "fallback_anchor_document_ids": [],
    }
    recorded: list[tuple[str, str, dict]] = []
    checkpoints: list[tuple[str, int]] = []
    monkeypatch.setattr(
        privilege_review_adjudication,
        "submit_provider_batch",
        lambda _batch_id, _actor_user_id: "JOB_STATE_SUCCEEDED",
    )
    monkeypatch.setattr(
        privilege_review_adjudication,
        "finalize_provider_batch",
        lambda _batch_id, _actor_user_id: expected,
    )
    monkeypatch.setattr(
        privilege_review_adjudication,
        "record_document",
        lambda run_id, document_id, result: recorded.append((run_id, document_id, result)),
    )
    monkeypatch.setattr(
        privilege_review_adjudication,
        "checkpoint_without_stopping",
        lambda run_id, ordinal: checkpoints.append((run_id, ordinal)),
    )

    result = privilege_review_adjudication.provider_batch_workflow.__wrapped__(
        "batch", "actor", 15.0, "review-run"
    )

    assert result == {**expected, "results_recorded": True}
    assert recorded == [
        ("review-run", "document-1", {"status": "COMPLETED"}),
        ("review-run", "document-2", {"status": "COMPLETED"}),
    ]
    assert checkpoints == [("review-run", 0)]


def test_privilege_adjudication_submits_every_batch_before_enqueuing_pollers(monkeypatch) -> None:
    events: list[tuple[str, str]] = []

    class Handle:
        @staticmethod
        def get_result():
            return {"completed_families": [], "fallback_anchor_document_ids": []}

    class ProviderQueue:
        @staticmethod
        def enqueue(_workflow, batch_id, _actor_user_id, _poll_seconds, _review_batch_run_id):
            events.append(("enqueue", batch_id))
            return Handle()

    monkeypatch.setattr(privilege_review_adjudication, "activate", lambda _run_id: None)
    monkeypatch.setattr(
        privilege_review_adjudication,
        "sync_batch_search",
        lambda run_id: events.append(("sync_search", run_id)),
    )
    monkeypatch.setattr(privilege_review_adjudication, "load_documents", lambda _run_id: [])
    monkeypatch.setattr(
        privilege_review_adjudication,
        "plan_provider_batches",
        lambda *_args: {
            "provider_batch_ids": ["batch-1", "batch-2"],
            "realtime_anchor_document_ids": [],
        },
    )
    monkeypatch.setattr(
        privilege_review_adjudication,
        "submit_provider_batch",
        lambda batch_id, _actor_user_id: events.append(("submit", batch_id)) or "JOB_STATE_RUNNING",
    )
    monkeypatch.setattr(privilege_review_adjudication, "SetWorkflowID", lambda _workflow_id: nullcontext())
    monkeypatch.setattr(privilege_review_adjudication, "PRIVILEGE_ADJUDICATION_PROVIDER_QUEUE", ProviderQueue())
    monkeypatch.setattr(
        privilege_review_adjudication,
        "get_settings",
        lambda: SimpleNamespace(definition_assessment_provider_batch_poll_seconds=15.0),
    )
    monkeypatch.setattr(privilege_review_adjudication, "checkpoint_without_stopping", lambda *_args: None)
    monkeypatch.setattr(privilege_review_adjudication, "complete", lambda _run_id: None)

    privilege_review_adjudication.privilege_review_adjudication_workflow.__wrapped__("run", "actor")

    assert events == [
        ("sync_search", "run"),
        ("submit", "batch-1"),
        ("submit", "batch-2"),
        ("enqueue", "batch-1"),
        ("enqueue", "batch-2"),
    ]


def test_privilege_evidence_submits_every_batch_before_enqueuing_pollers(monkeypatch) -> None:
    events: list[tuple[str, str]] = []

    class Handle:
        @staticmethod
        def get_result():
            return {"completed": 0, "fallback_document_ids": []}

    class ProviderQueue:
        @staticmethod
        def enqueue(_workflow, batch_id, _actor_user_id, _poll_seconds):
            events.append(("enqueue", batch_id))
            return Handle()

    monkeypatch.setattr(privilege_review_evidence, "activate_evidence_first_assessment", lambda *_args: None)
    monkeypatch.setattr(privilege_review_evidence, "activate_records", lambda _assessment_id: None)
    monkeypatch.setattr(privilege_review_evidence, "load_family_member_ids", lambda *_args: [])
    monkeypatch.setattr(
        privilege_review_evidence,
        "plan_provider_batches",
        lambda *_args: {
            "provider_batch_ids": ["batch-1", "batch-2"],
            "realtime_document_ids": [],
        },
    )
    monkeypatch.setattr(
        privilege_review_evidence,
        "submit_provider_batch",
        lambda batch_id, _actor_user_id: events.append(("submit", batch_id)) or "JOB_STATE_RUNNING",
    )
    monkeypatch.setattr(privilege_review_evidence, "SetWorkflowID", lambda _workflow_id: nullcontext())
    monkeypatch.setattr(privilege_review_evidence, "PRIVILEGE_EVIDENCE_PROVIDER_QUEUE", ProviderQueue())
    monkeypatch.setattr(
        privilege_review_evidence,
        "get_settings",
        lambda: SimpleNamespace(definition_assessment_provider_batch_poll_seconds=15.0),
    )
    monkeypatch.setattr(privilege_review_evidence, "merge_family_evidence", lambda *_args: None)
    monkeypatch.setattr(privilege_review_evidence, "checkpoint_chunk_without_stopping", lambda *_args: None)
    monkeypatch.setattr(privilege_review_evidence, "complete", lambda _assessment_id: None)

    privilege_review_evidence.privilege_review_evidence_workflow.__wrapped__("assessment", "actor", "program")

    assert events == [
        ("submit", "batch-1"),
        ("submit", "batch-2"),
        ("enqueue", "batch-1"),
        ("enqueue", "batch-2"),
    ]


def test_family_context_keeps_document_with_missing_relationship_snapshot() -> None:
    matter_id = uuid.uuid4()
    program_id = uuid.uuid4()
    document_id = uuid.uuid4()
    collection_id = uuid.uuid4()
    item_id = uuid.uuid4()
    payload = _family_context_payload(
        population_payload={
            "documents": [
                {
                    "matter_document_id": str(document_id),
                    "source_collection_id": str(collection_id),
                    "collection_item_id": str(item_id),
                    "source_review_batch_ids": [str(uuid.uuid4())],
                }
            ]
        },
        snapshots={},
        matter=SimpleNamespace(id=matter_id),
        program=SimpleNamespace(id=program_id),
        frozen_at=datetime.now(timezone.utc),
        population_reference={"artifact_id": str(uuid.uuid4()), "content_hash": "a" * 64},
    )

    assert payload["coverage_status"] == "COMPLETE_WITH_EXCEPTIONS"
    assert payload["counts"]["review_document_count"] == 1
    assert payload["counts"]["review_unit_count"] == 1
    assert payload["counts"]["relationship_exception_count"] == 1
    assert payload["documents"] == [
        {
            "matter_document_id": str(document_id),
            "collection_item_id": str(item_id),
            "family_key": f"unresolved:{item_id}",
            "relationship_role": "UNKNOWN",
            "exception_codes": ["RELATIONSHIP_SNAPSHOT_MISSING"],
            "family_member_count": 1,
            "context_only_member_count": 0,
        }
    ]
    assert payload["exceptions"][0]["code"] == "RELATIONSHIP_SNAPSHOT_MISSING"


def test_evidence_packets_pin_sources_and_surface_missing_text() -> None:
    matter_id = uuid.uuid4()
    program_id = uuid.uuid4()
    root_item_id = uuid.uuid4()
    attachment_item_id = uuid.uuid4()
    root_document_id = uuid.uuid4()
    attachment_document_id = uuid.uuid4()
    text_artifact_id = uuid.uuid4()
    family_context = {
        "kind": "PRIVILEGE_FAMILY_CONTEXT",
        "matter_id": str(matter_id),
        "program_id": str(program_id),
        "families": [
            {
                "family_key": "family-1",
                "family_id": str(uuid.uuid4()),
                "root_collection_item_id": str(root_item_id),
                "exception_codes": [],
                "members": [
                    {
                        "collection_item_id": str(root_item_id),
                        "matter_document_id": str(root_document_id),
                        "relationship_role": "PARENT",
                        "in_review_population": True,
                    },
                    {
                        "collection_item_id": str(attachment_item_id),
                        "matter_document_id": str(attachment_document_id),
                        "relationship_role": "ATTACHMENT",
                        "in_review_population": True,
                    },
                ],
            }
        ],
        "documents": [
            {"matter_document_id": str(root_document_id)},
            {"matter_document_id": str(attachment_document_id)},
        ],
        "exceptions": [],
    }
    snapshots = {
        root_item_id: AnalysisInputItemSnapshot(
            item_id=root_item_id,
            family_id=None,
            record_type="EMAIL",
            original_filename="parent.eml",
            file_date=None,
            email_sender="lawyer@example.com",
            email_subject="Legal advice",
            email_sent_at=None,
            email_recipients={"TO": ["client@example.com"], "CC": [], "BCC": []},
            text_source=TextSourceReference(
                artifact_id=text_artifact_id,
                role="TEXT",
                media_type="text/plain",
                original_filename="parent.txt",
                content_hash="a" * 64,
                byte_length=120,
            ),
        ),
        attachment_item_id: AnalysisInputItemSnapshot(
            item_id=attachment_item_id,
            family_id=None,
            record_type="DOCUMENT",
            original_filename="attachment.docx",
            file_date=None,
            email_sender=None,
            email_subject=None,
            email_sent_at=None,
            email_recipients={"TO": [], "CC": [], "BCC": []},
            text_source=None,
        ),
    }
    payload = _evidence_packets_payload(
        family_context=family_context,
        input_snapshots=snapshots,
        matter=SimpleNamespace(id=matter_id),
        program=SimpleNamespace(id=program_id),
        frozen_at=datetime.now(timezone.utc),
        family_context_reference={"artifact_id": str(uuid.uuid4()), "content_hash": "b" * 64},
        identity_context_reference={"artifact_id": str(uuid.uuid4()), "content_hash": "c" * 64},
        accepted_matter_definition={"revision_id": str(uuid.uuid4()), "content_hash": "d" * 64},
    )

    assert payload["coverage_status"] == "COMPLETE_WITH_EXCEPTIONS"
    assert payload["counts"]["review_document_count"] == 2
    assert payload["counts"]["packet_count"] == 1
    assert payload["counts"]["missing_text_document_count"] == 1
    assert payload["packets"][0]["review_target_document_ids"] == [
        str(root_document_id),
        str(attachment_document_id),
    ]
    assert payload["packets"][0]["members"][0]["text_source"]["artifact_id"] == str(text_artifact_id)
    assert payload["packets"][0]["members"][1]["exception_codes"] == ["TEXT_SOURCE_MISSING"]


def test_evidence_packet_preflight_rejects_snapshot_source_without_extractable_text(
    monkeypatch,
) -> None:
    item_id = uuid.uuid4()
    snapshot = AnalysisInputItemSnapshot(
        item_id=item_id,
        family_id=None,
        record_type="DOCUMENT",
        original_filename="placeholder.txt",
        file_date=None,
        email_sender=None,
        email_subject=None,
        email_sent_at=None,
        email_recipients={"TO": [], "CC": [], "BCC": []},
        text_source=TextSourceReference(
            artifact_id=uuid.uuid4(),
            role="NATIVE",
            media_type="text/plain",
            original_filename="placeholder.txt",
            content_hash="a" * 64,
            byte_length=46,
        ),
    )
    monkeypatch.setattr(
        "app.privilege_review_programs.get_preferred_text_source",
        lambda **_kwargs: None,
    )

    validated = _validate_evidence_packet_text_sources(
        {item_id: snapshot},
        actor_user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        client_id=uuid.uuid4(),
    )

    assert validated[item_id].text_source is None
    assert snapshot.text_source is not None


def test_privilege_family_context_supplies_citable_parent_and_identity_signals() -> None:
    target_item_id = uuid.uuid4()
    parent_item_id = uuid.uuid4()
    law_firm_id = uuid.uuid4()
    snapshot = AnalysisInputItemSnapshot(
        item_id=target_item_id,
        family_id=None,
        record_type="DOCUMENT",
        original_filename="questions.docx",
        file_date=None,
        email_sender=None,
        email_subject=None,
        email_sent_at=None,
        email_recipients={"TO": [], "CC": [], "BCC": []},
        text_source=TextSourceReference(
            artifact_id=uuid.uuid4(),
            role="TEXT",
            media_type="text/plain",
            original_filename="questions.txt",
            content_hash="a" * 64,
            byte_length=100,
        ),
    )
    context = build_privilege_family_evidence_context(
        target_snapshot=snapshot,
        target_collection_item_id=target_item_id,
        packet={
            "members": [
                {
                    "collection_item_id": str(parent_item_id),
                    "relationship_role": "PARENT",
                    "original_filename": "parent.eml",
                    "email": {
                        "sender": "lawyer@firm.example",
                        "subject": "Legal advice request",
                        "recipients": {"TO": ["client@example.com"], "CC": [], "BCC": []},
                    },
                },
                {
                    "collection_item_id": str(target_item_id),
                    "relationship_role": "ATTACHMENT",
                    "original_filename": "questions.docx",
                    "email": None,
                },
            ]
        },
        identity_context={
            "entities": [
                {
                    "id": str(law_firm_id),
                    "display_name": "Example Law Firm",
                    "entity_type": "ORGANIZATION",
                    "organization_category": "LAW_FIRM",
                    "email": None,
                    "title": None,
                    "variants": [],
                    "domains": [{"normalized_domain": "firm.example"}],
                }
            ],
            "relationships": [],
        },
        family_member_text={
            parent_item_id: "Please review the attached questions and provide legal advice."
        },
    )

    values = {item.citation_id: item.value for item in context.items}
    assert all(is_supported_evidence_citation(item.citation_id) for item in context.items)
    assert values[f"family:{parent_item_id}:role"] == "PARENT"
    assert "provide legal advice" in values[f"family:{parent_item_id}:text"]
    assert "LAW_FIRM" in values[f"identity:{law_firm_id}:signal"]


def test_family_conflicts_route_only_negative_family_members() -> None:
    privileged_id = str(uuid.uuid4())
    negative_id = str(uuid.uuid4())
    standalone_id = str(uuid.uuid4())
    packets = {
        "packets": [
            {
                "family_key": "family-1",
                "review_target_document_ids": [privileged_id, negative_id],
            },
            {
                "family_key": "family-2",
                "review_target_document_ids": [standalone_id],
            },
        ]
    }
    assert _family_conflict_document_ids(
        packets,
        {
            privileged_id: "privileged",
            negative_id: "not_privileged",
            standalone_id: "not_privileged",
        },
    ) == {negative_id}
