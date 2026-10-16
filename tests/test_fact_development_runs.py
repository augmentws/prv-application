import hashlib
import uuid
from datetime import datetime, timezone

import pytest
from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from pydantic_ai.models.test import TestModel
from sqlalchemy import select

from app import fact_development
from app.analysis_input_manifests import AnalysisInputManifestReader
from app.artifact_gateway import AnalysisInputItemSnapshot, TextSourceReference, WorkflowArtifactReference
from app.document_metadata import apply_metadata_values
from app.fact_development import (
    FactDevelopmentRunError,
    fail_fact_development_run,
    finalize_fact_development_run,
    prepare_fact_development_run,
    process_fact_development_chunk,
)
from app.fact_development_skills import ensure_standard_fact_development_skills
from app.fact_recall import RecalledFact
from app.models import (
    AnalysisInputManifest,
    JobOutcomeManifest,
    MatterAnalysisTaskRun,
    MatterAnalysisTaskRunChunk,
    MatterDocument,
    MatterEntity,
    MatterFact,
    MatterFactEvidence,
    MetadataDefinition,
    SkillRun,
    Tenant,
)
from tests.test_agents_and_matter_definitions import auth
from tests.test_fact_development_tasks import _ready_batch
from tests.test_facts_and_entities import DOCUMENT_TEXT, _matter_with_task, document_text  # noqa: F401

PRICING_QUOTE = "Jane Smith approved the 5% price increase"


def _operation(**overrides) -> dict:
    operation = {
        "op": "CREATE",
        "refine_fields": [],
        "issue_keys": ["pricing"],
        "fact_id": None,
        "summary": "Jane Smith approved a 5% price increase for Acme customers.",
        "date_precision": "DAY",
        "event_date_start": "2021-03-03",
        "event_date_end": None,
        "date_basis": "STATED_IN_TEXT",
        "owner": {"name": "Jane Smith", "email": "jane@acme.com", "entity_type": "PERSON"},
        "participants": [
            {"entity": {"name": "Acme Corp", "email": None, "entity_type": "ORGANIZATION"}, "role": "PARTICIPANT_RECIPIENT"}
        ],
        "evidence": [{"paragraph_id": "¶1", "quote": PRICING_QUOTE, "stance": "SUPPORTS"}],
        "reason": None,
    }
    operation.update(overrides)
    return operation


@pytest.fixture(autouse=True)
def inputs(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Fake Artifact inputs, keyed by matter document ID: File Dates, the text hash, and documents without text."""

    state: dict = {"item_documents": {}, "file_dates": {}, "content_hash": "a" * 64, "no_text": set(), "text_reads": 0}

    def snapshots(*, item_ids, **_):
        result = {}
        for item_id in item_ids:
            document_id = state["item_documents"].get(item_id)
            result[item_id] = AnalysisInputItemSnapshot(
                item_id=item_id,
                family_id=None,
                record_type="EMAIL",
                original_filename="pricing.msg",
                file_date=state["file_dates"].get(document_id),
                email_sender="jane@acme.com",
                email_subject="Price increase",
                email_sent_at=state["file_dates"].get(document_id),
                email_recipients={"TO": ["bob@acme.com"], "CC": [], "BCC": []},
                text_source=(
                    None
                    if document_id in state["no_text"]
                    else TextSourceReference(
                        artifact_id=uuid.uuid5(item_id, "text"),
                        role="EXTRACTED_TEXT",
                        media_type="text/plain",
                        original_filename="pricing.txt",
                        content_hash=state["content_hash"],
                        byte_length=len(DOCUMENT_TEXT),
                    )
                ),
            )
        return result

    def read_text(reference, **_):
        state["text_reads"] += 1
        assert reference.content_hash == state["content_hash"]
        return DOCUMENT_TEXT

    monkeypatch.setattr("app.fact_development.get_collection_item_analysis_input_snapshots", snapshots)
    monkeypatch.setattr("app.fact_development.read_text_source", read_text)
    return state


@pytest.fixture
def artifacts(monkeypatch: pytest.MonkeyPatch) -> dict:
    content_by_id: dict[uuid.UUID, bytes] = {}
    by_derivation: dict[tuple[str, str], WorkflowArtifactReference] = {}
    by_id: dict[uuid.UUID, WorkflowArtifactReference] = {}

    def store(**kwargs):
        content = kwargs["content"]
        content.seek(0)
        value = content.read()
        key = (kwargs["artifact_type"].value, kwargs["derivation_key"])
        if key in by_derivation:
            return by_derivation[key], False
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
        by_derivation[key] = reference
        by_id[artifact_id] = reference
        return reference, True

    def copy(**kwargs):
        value = content_by_id[kwargs["artifact_id"]]
        kwargs["destination"].write(value)
        return len(value)

    monkeypatch.setattr("app.job_outcomes.store_workflow_artifact", store)
    monkeypatch.setattr(
        "app.job_outcomes.find_workflow_artifact_reference",
        lambda **kwargs: by_derivation.get((kwargs["artifact_type"].value, kwargs["derivation_key"])),
    )
    monkeypatch.setattr("app.job_outcomes.get_workflow_artifact_reference", lambda **kwargs: by_id[kwargs["artifact_id"]])
    monkeypatch.setattr("app.job_outcomes.copy_workflow_artifact_content", copy)
    monkeypatch.setattr("app.analysis_input_manifests.store_workflow_artifact", store)
    monkeypatch.setattr(
        "app.analysis_input_manifests.get_workflow_artifact_reference",
        lambda **kwargs: by_id[kwargs["artifact_id"]],
    )
    monkeypatch.setattr("app.analysis_input_manifests.copy_workflow_artifact_content", copy)
    return by_derivation


@pytest.fixture
def run_ctx(client: TestClient, root_token: str, root_admin, inputs: dict) -> dict:
    with TestingSessionLocal() as db:
        root = db.get(Tenant, root_admin.tenant_id)
        ensure_standard_fact_development_skills(db, root, root_admin)
        db.commit()
    ctx = _matter_with_task(client, root_token)
    user_id = client.get("/v1/auth/me", headers=auth(ctx["token"])).json()["id"]
    with TestingSessionLocal() as db:
        definition = db.scalar(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == uuid.UUID(ctx["matter_id"]),
                MetadataDefinition.key == "issue_coding",
            )
        )
        for document_id, values in ((ctx["documents"][0], ["pricing"]), (ctx["documents"][1], ["safety"])):
            apply_metadata_values(
                db,
                matter_id=uuid.UUID(ctx["matter_id"]),
                document_id=uuid.UUID(document_id),
                definition_id=definition.id,
                values=values,
                replace=True,
                source_type="HUMAN",
                actor_id=uuid.UUID(user_id),
                source_id=f"test:{document_id}",
            )
        batch = _ready_batch(db, ctx["matter_id"], user_id, ctx["documents"])
        db.commit()
        ctx["batch_id"] = str(batch.id)
        for document_id in ctx["documents"]:
            inputs["item_documents"][db.get(MatterDocument, uuid.UUID(document_id)).collection_item_id] = document_id
    # Batch order is pricing, safety, uncoded; chronological order puts the 2020 safety document first.
    inputs["file_dates"] = {
        ctx["documents"][0]: datetime(2021, 3, 4, tzinfo=timezone.utc),
        ctx["documents"][1]: datetime(2020, 1, 1, tzinfo=timezone.utc),
    }
    ctx["user_id"] = user_id
    return ctx


def _launch(client: TestClient, ctx: dict, key: str = "run-1") -> dict:
    response = client.post(
        f"/v1/matters/{ctx['matter_id']}/analysis-tasks/{ctx['task_id']}/fact-runs",
        headers=auth(ctx["token"]),
        json={"review_batch_id": ctx["batch_id"], "idempotency_key": key},
    )
    assert response.status_code == 202, response.text
    return response.json()


def _run_all(run_id: str, model) -> None:
    with TestingSessionLocal() as db:
        chunk_ids = prepare_fact_development_run(db, uuid.UUID(run_id))
    for chunk_id in chunk_ids:
        process_fact_development_chunk(uuid.UUID(chunk_id), session_factory=TestingSessionLocal, model=model)
    with TestingSessionLocal() as db:
        finalize_fact_development_run(db, uuid.UUID(run_id))


def test_run_extracts_verified_facts_and_publishes_outcomes(
    client: TestClient,
    run_ctx: dict,
    artifacts: dict,
) -> None:
    ctx = run_ctx
    run = _launch(client, ctx)
    assert run["status"] == "QUEUED"
    assert run["run_kind"] == "FACT_DEVELOPMENT"
    assert run["population_document_count"] == 3
    assert _launch(client, ctx)["id"] == run["id"]  # idempotent launch
    # Classification run listings never show fact runs.
    classification_runs = client.get(
        f"/v1/matters/{ctx['matter_id']}/analysis-tasks/{ctx['task_id']}/runs",
        headers=auth(ctx["token"]),
    )
    assert classification_runs.json() == []

    output = {
        "operations": [
            _operation(),
            _operation(evidence=[{"paragraph_id": "¶1", "quote": "Jane approved nothing", "stance": "SUPPORTS"}]),
            _operation(op="NO_FACT", issue_keys=["safety"], summary=None, date_precision="UNKNOWN",
                       event_date_start=None, date_basis=None, owner=None, participants=[], evidence=[],
                       reason="No safety content."),
        ]
    }
    _run_all(run["id"], TestModel(custom_output_args=output))

    detail = client.get(
        f"/v1/matters/{ctx['matter_id']}/analysis-tasks/{ctx['task_id']}/fact-runs/{run['id']}",
        headers=auth(ctx["token"]),
    ).json()
    assert detail["status"] == "COMPLETED", detail
    assert detail["progress_completed"] == 3
    report = detail["report"]
    # Document 1 (pricing): one fact created, the fabricated quote and the uncoded safety issue are invalid.
    # Document 2 (safety): the pricing creates are invalid, NO_FACT for safety is valid. Document 3: no issue.
    assert report["status_counts"] == {"EXCLUDED": 1, "SUCCEEDED": 2}
    assert report["reason_counts"] == {"fact.facts_proposed": 1, "fact.no_fact": 1, "fact.no_matching_issue": 1}
    assert report["counts"]["facts_created"] == 1
    assert report["counts"]["invalid_operations"] == 4
    assert report["facts_by_issue"] == [{"issue_key": "pricing", "fact_count": 1}]
    assert report["qc"]["entities_awaiting_confirmation"] == 2
    assert report["matched_document_count"] == 2

    with TestingSessionLocal() as db:
        facts = list(db.scalars(select(MatterFact).where(MatterFact.matter_id == uuid.UUID(ctx["matter_id"]))))
        assert len(facts) == 1
        fact = facts[0]
        assert fact.status == "WORKING"
        assert fact.created_by_user_id is None
        assert fact.origin_workflow_run_id is not None
        assert fact.event_date_start.isoformat() == "2021-03-03"
        evidence = db.scalar(select(MatterFactEvidence).where(MatterFactEvidence.fact_id == fact.id))
        assert evidence.quote == PRICING_QUOTE
        assert DOCUMENT_TEXT[evidence.char_start : evidence.char_end] == PRICING_QUOTE
        entities = {row.display_name: row for row in db.scalars(select(MatterEntity))}
        assert entities["Jane Smith"].status == "PROPOSED"
        assert entities["Jane Smith"].normalized_email == "jane@acme.com"
        assert entities["Acme Corp"].entity_type == "ORGANIZATION"
        assert fact.owner_entity_id == entities["Jane Smith"].id
        assert db.query(SkillRun).filter(SkillRun.scope_type == "MATTER_DOCUMENT").count() == 2
        manifest = db.scalar(select(JobOutcomeManifest).where(JobOutcomeManifest.job_id == uuid.UUID(run["id"])))
        assert manifest.manifest_status == "FINALIZED"

    items = client.get(
        f"/v1/matters/{ctx['matter_id']}/job-outcomes/FACT_DEVELOPMENT_ANALYSIS_TASK/{run['id']}/items",
        headers=auth(ctx["token"]),
        params={"limit": 100},
    )
    assert items.status_code == 200, items.text
    rows = items.json()["items"]
    # Outcomes follow the frozen chronological order: 2020 safety, 2021 pricing, then the undated document.
    assert [row["item_id"] for row in rows] == [ctx["documents"][1], ctx["documents"][0], ctx["documents"][2]]
    first_payload = rows[1]["payload"]
    assert len(first_payload["errors"]) == 2
    assert "does not appear verbatim" in first_payload["errors"][0]["error"]
    assert "not coded to" in first_payload["errors"][1]["error"]

    jobs = client.get(
        f"/v1/jobs/FACT_DEVELOPMENT_ANALYSIS_TASK/{run['id']}/execution",
        headers=auth(ctx["token"]),
    )
    assert jobs.status_code == 200, jobs.text


def test_add_evidence_targets_only_recalled_working_facts(
    client: TestClient,
    run_ctx: dict,
    artifacts: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = run_ctx
    existing = client.post(
        f"/v1/matters/{ctx['matter_id']}/facts",
        headers=auth(ctx["token"]),
        json={
            "summary": "Prices rose 5%.",
            "date_precision": "UNKNOWN",
            "issues": [{"matter_analysis_task_id": ctx["task_id"], "issue_key": "pricing"}],
            "evidence": [
                {"matter_document_id": ctx["documents"][2], "paragraph_id": "¶2", "quote": "The increase is final."}
            ],
        },
    )
    assert existing.status_code == 201, existing.text
    fact_id = existing.json()["id"]

    def recalled(db, **kwargs):
        return [RecalledFact(fact=db.get(MatterFact, uuid.UUID(fact_id)), similarity=0.9, shared_participants=0, rejected=False)]

    monkeypatch.setattr("app.fact_development.recall_facts", recalled)
    output = {
        "operations": [
            _operation(op="ADD_EVIDENCE", fact_id=fact_id, summary=None, owner=None, participants=[]),
            _operation(op="ADD_EVIDENCE", fact_id=str(uuid.uuid4()), summary=None, owner=None, participants=[]),
        ]
    }
    run = _launch(client, ctx)
    _run_all(run["id"], TestModel(custom_output_args=output))

    with TestingSessionLocal() as db:
        fact = db.get(MatterFact, uuid.UUID(fact_id))
        assert fact.evidence_count == 2
        assert fact.document_count == 2
        run_row = db.get(MatterAnalysisTaskRun, uuid.UUID(run["id"]))
        assert run_row.report["counts"]["evidence_added"] == 1
        # The safety document's pricing evidence is outside its coding; the unknown fact is not recalled.
        assert run_row.report["counts"]["invalid_operations"] == 3


def test_cancel_is_final_and_retry_resumes_only_failed_runs(
    client: TestClient,
    run_ctx: dict,
    artifacts: dict,
) -> None:
    ctx = run_ctx
    run = _launch(client, ctx)
    with TestingSessionLocal() as db:
        prepare_fact_development_run(db, uuid.UUID(run["id"]))

    canceled = client.post(
        f"/v1/jobs/FACT_DEVELOPMENT_ANALYSIS_TASK/{run['id']}/cancel",
        headers=auth(ctx["token"]),
    )
    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["status"] == "CANCELED"
    assert "RETRY" not in canceled.json()["available_actions"]
    with TestingSessionLocal() as db:
        manifest = db.scalar(select(JobOutcomeManifest).where(JobOutcomeManifest.job_id == uuid.UUID(run["id"])))
        assert manifest.manifest_status == "FINALIZED"
        assert manifest.status_counts == {"CANCELED": 3}
        assert {chunk.status for chunk in db.scalars(select(MatterAnalysisTaskRunChunk))} == {"CANCELED"}
    retry = client.post(f"/v1/jobs/FACT_DEVELOPMENT_ANALYSIS_TASK/{run['id']}/retry", headers=auth(ctx["token"]))
    assert retry.status_code == 409


def test_failed_run_retry_skips_documents_already_applied(
    client: TestClient,
    run_ctx: dict,
    artifacts: dict,
) -> None:
    ctx = run_ctx
    run = _launch(client, ctx)
    model = TestModel(custom_output_args={"operations": [_operation()]})
    with TestingSessionLocal() as db:
        (chunk_id,) = prepare_fact_development_run(db, uuid.UUID(run["id"]))
    # Simulate a crash mid-chunk: the first document's facts were committed but no fragment was published.
    process_fact_development_chunk(uuid.UUID(chunk_id), session_factory=TestingSessionLocal, model=model)
    artifacts.clear()
    with TestingSessionLocal() as db:
        db.query(JobOutcomeManifest).delete()
        db.get(MatterAnalysisTaskRunChunk, uuid.UUID(chunk_id)).status = "RUNNING"
        db.commit()
    failed = client.post(f"/v1/jobs/FACT_DEVELOPMENT_ANALYSIS_TASK/{run['id']}/retry", headers=auth(ctx["token"]))
    assert failed.status_code == 409  # Still running; only failed runs resume.
    with TestingSessionLocal() as db:
        fail_fact_development_run(db, uuid.UUID(run["id"]), "worker lost", code="WORKER_LOST", retriable=True)
    retry = client.post(f"/v1/jobs/FACT_DEVELOPMENT_ANALYSIS_TASK/{run['id']}/retry", headers=auth(ctx["token"]))
    assert retry.status_code == 200, retry.text
    assert retry.json()["attempt_number"] == 2

    _run_all(run["id"], model)
    with TestingSessionLocal() as db:
        assert db.query(MatterFact).count() == 1  # Not re-created by the second attempt.
        run_row = db.get(MatterAnalysisTaskRun, uuid.UUID(run["id"]))
        # The safety document's only operation is a pricing fact it is not coded to, so that document fails.
        assert run_row.status == "COMPLETED_WITH_ERRORS"
        assert run_row.failure_code == "FACT_DEVELOPMENT_DOCUMENT_FAILURES"
        assert run_row.report["reason_counts"] == {
            "fact.reused_prior_extraction": 1,
            "fact.operations_invalid": 1,
            "fact.no_matching_issue": 1,
        }


def _extraction_output() -> dict:
    return {
        "operations": [
            _operation(),
            _operation(op="NO_FACT", issue_keys=["safety"], summary=None, date_precision="UNKNOWN",
                       event_date_start=None, date_basis=None, owner=None, participants=[], evidence=[],
                       reason="No safety content."),
        ]
    }


def _reasons(run_id: str) -> dict:
    with TestingSessionLocal() as db:
        return db.get(MatterAnalysisTaskRun, uuid.UUID(run_id)).report["reason_counts"]


def test_relaunch_after_cancel_reuses_finished_documents(
    client: TestClient,
    run_ctx: dict,
    artifacts: dict,
) -> None:
    ctx = run_ctx
    model = TestModel(custom_output_args=_extraction_output())
    first = _launch(client, ctx, key="first")
    with TestingSessionLocal() as db:
        (chunk_id,) = prepare_fact_development_run(db, uuid.UUID(first["id"]))
    process_fact_development_chunk(uuid.UUID(chunk_id), session_factory=TestingSessionLocal, model=model)
    canceled = client.post(f"/v1/jobs/FACT_DEVELOPMENT_ANALYSIS_TASK/{first['id']}/cancel", headers=auth(ctx["token"]))
    assert canceled.json()["status"] == "CANCELED"

    second = _launch(client, ctx, key="second")
    _run_all(second["id"], model)
    assert _reasons(second["id"]) == {"fact.reused_prior_extraction": 2, "fact.no_matching_issue": 1}
    with TestingSessionLocal() as db:
        assert db.query(MatterFact).count() == 1
        assert db.query(SkillRun).filter(SkillRun.scope_type == "MATTER_DOCUMENT").count() == 2  # No new model calls.
        assert db.get(MatterAnalysisTaskRun, uuid.UUID(second["id"])).report["counts"]["reused"] == 2
    items = client.get(
        f"/v1/matters/{ctx['matter_id']}/job-outcomes/FACT_DEVELOPMENT_ANALYSIS_TASK/{second['id']}/items",
        headers=auth(ctx["token"]),
        params={"status": "REUSED"},
    ).json()["items"]
    assert all(row["input_fingerprint"] for row in items)


def test_changed_coding_or_text_is_extracted_again(
    client: TestClient,
    run_ctx: dict,
    artifacts: dict,
    inputs: dict,
) -> None:
    ctx = run_ctx
    model = TestModel(custom_output_args=_extraction_output())
    _run_all(_launch(client, ctx, key="first")["id"], model)

    # The safety document is now also coded to pricing; the pricing document is unchanged.
    with TestingSessionLocal() as db:
        definition = db.scalar(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == uuid.UUID(ctx["matter_id"]),
                MetadataDefinition.key == "issue_coding",
            )
        )
        apply_metadata_values(
            db,
            matter_id=uuid.UUID(ctx["matter_id"]),
            document_id=uuid.UUID(ctx["documents"][1]),
            definition_id=definition.id,
            values=["safety", "pricing"],
            replace=True,
            source_type="HUMAN",
            actor_id=uuid.UUID(ctx["user_id"]),
            source_id="test:recode",
        )
        db.commit()
    recoded = _launch(client, ctx, key="recoded")
    _run_all(recoded["id"], model)
    reasons = _reasons(recoded["id"])
    assert reasons["fact.reused_prior_extraction"] == 1
    assert reasons["fact.facts_proposed"] == 1

    # New text for every document (a reprocessed text source) invalidates every earlier extraction.
    inputs["content_hash"] = "b" * 64
    retexted = _launch(client, ctx, key="retexted")
    _run_all(retexted["id"], model)
    assert "fact.reused_prior_extraction" not in _reasons(retexted["id"])


def test_population_is_frozen_in_a_chronological_input_manifest(
    client: TestClient,
    run_ctx: dict,
    artifacts: dict,
    inputs: dict,
) -> None:
    ctx = run_ctx
    inputs["no_text"].add(ctx["documents"][1])
    run = _launch(client, ctx)
    _run_all(run["id"], TestModel(custom_output_args=_extraction_output()))

    with TestingSessionLocal() as db:
        run_row = db.get(MatterAnalysisTaskRun, uuid.UUID(run["id"]))
        manifest = db.get(AnalysisInputManifest, run_row.analysis_input_manifest_id)
        assert manifest.manifest_status == "FINALIZED"
        assert (manifest.owner_type, manifest.owner_id) == ("MATTER_ANALYSIS_TASK_RUN", run_row.id)
        assert (manifest.scope_type, manifest.scope_id) == ("REVIEW_BATCH", uuid.UUID(ctx["batch_id"]))
        assert manifest.input_contract["key"] == "fact-development.population"
        assert manifest.item_count == 3
        rows = AnalysisInputManifestReader(manifest, actor_user_id=run_row.created_by_user_id).rows(0, 3)
        assert [(row["matter_document_id"], row["input_status"], row["failure_code"]) for row in rows] == [
            (ctx["documents"][1], "MISSING", "fact.no_text_source"),
            (ctx["documents"][0], "READY", None),
            (ctx["documents"][2], "EXCLUDED", "fact.no_matching_issue"),
        ]
        ready = rows[1]
        assert ready["source_content_hash"] == "a" * 64
        assert ready["file_date"] == "2021-03-04T00:00:00+00:00"
        assert ready["resolved_inputs"]["issue_keys"] == ["pricing"]
        assert ready["resolved_inputs"]["metadata"]["email_from"] == "jane@acme.com"
        assert run_row.report["input_status_counts"] == {"MISSING": 1, "READY": 1, "EXCLUDED": 1}
        assert run_row.report["reason_counts"] == {
            "fact.facts_proposed": 1,
            "fact.no_matching_issue": 1,
            "fact.no_text_source": 1,
        }
    # Only the one ready document's pinned text was read; nothing was re-resolved at execution time.
    assert inputs["text_reads"] == 1


def test_chunks_refuse_a_mismatched_or_unsupported_input_manifest(
    client: TestClient,
    run_ctx: dict,
    artifacts: dict,
) -> None:
    run = _launch(client, run_ctx)
    with TestingSessionLocal() as db:
        (chunk_id,) = prepare_fact_development_run(db, uuid.UUID(run["id"]))
        manifest_id = db.get(MatterAnalysisTaskRun, uuid.UUID(run["id"])).analysis_input_manifest_id

    def expect(code: str, **changes) -> None:
        with TestingSessionLocal() as db:
            manifest = db.get(AnalysisInputManifest, manifest_id)
            original = {key: getattr(manifest, key) for key in changes}
            for key, value in changes.items():
                setattr(manifest, key, value)
            db.commit()
        with pytest.raises(FactDevelopmentRunError) as raised:
            process_fact_development_chunk(uuid.UUID(chunk_id), session_factory=TestingSessionLocal, model=TestModel())
        assert raised.value.code == code
        with TestingSessionLocal() as db:
            manifest = db.get(AnalysisInputManifest, manifest_id)
            for key, value in original.items():
                setattr(manifest, key, value)
            db.commit()

    expect("INPUT_MANIFEST_IDENTITY_MISMATCH", owner_id=uuid.uuid4())
    expect("INPUT_MANIFEST_IDENTITY_MISMATCH", scope_id=uuid.uuid4())
    with TestingSessionLocal() as db:
        contract = dict(db.get(AnalysisInputManifest, manifest_id).input_contract)
    expect("INPUT_CONTRACT_UNSUPPORTED", input_contract={**contract, "version": 2})
    expect("INPUT_MANIFEST_INVALID", artifact_content_hash="0" * 64)
    expect("INPUT_MANIFEST_IDENTITY_MISMATCH", item_count=99)


def test_cancel_while_freezing_publishes_no_manifest(
    client: TestClient,
    run_ctx: dict,
    artifacts: dict,
    inputs: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _launch(client, run_ctx)
    resolve = fact_development.get_collection_item_analysis_input_snapshots

    def resolve_then_cancel(**kwargs):
        canceled = client.post(f"/v1/jobs/FACT_DEVELOPMENT_ANALYSIS_TASK/{run['id']}/cancel", headers=auth(run_ctx["token"]))
        assert canceled.status_code == 200, canceled.text
        return resolve(**kwargs)

    monkeypatch.setattr(fact_development, "get_collection_item_analysis_input_snapshots", resolve_then_cancel)
    with TestingSessionLocal() as db, pytest.raises(FactDevelopmentRunError) as raised:
        prepare_fact_development_run(db, uuid.UUID(run["id"]))
    assert raised.value.code == "CANCELED"
    with TestingSessionLocal() as db:
        assert db.query(AnalysisInputManifest).count() == 0
        run_row = db.get(MatterAnalysisTaskRun, uuid.UUID(run["id"]))
        assert (run_row.status, run_row.analysis_input_manifest_id) == ("CANCELED", None)


def test_fact_runs_require_the_run_permission(client: TestClient, run_ctx: dict) -> None:
    ctx = run_ctx
    roles = {
        role["key"]: role
        for role in client.get(f"/v1/tenants/{ctx['tenant_id']}/roles", headers=auth(ctx["token"])).json()
    }

    def user_with_role(email: str, role_key: str) -> str:
        user = client.post(
            f"/v1/tenants/{ctx['tenant_id']}/users",
            headers=auth(ctx["token"]),
            json={"email": email, "display_name": email.split("@")[0], "password": "long-enough-test-password"},
        )
        assert user.status_code == 201, user.text
        grant = client.post(
            f"/v1/tenants/{ctx['tenant_id']}/authorization-grants",
            headers=auth(ctx["token"]),
            json={
                "subject": {"type": "USER", "id": user.json()["id"]},
                "role_id": roles[role_key]["id"],
                "scope": {"type": "MATTER", "id": ctx["matter_id"]},
            },
        )
        assert grant.status_code == 201, grant.text
        return client.post("/v1/auth/login", json={"email": email, "password": "long-enough-test-password"}).json()[
            "access_token"
        ]

    runs_url = f"/v1/matters/{ctx['matter_id']}/analysis-tasks/{ctx['task_id']}/fact-runs"
    payload = {"review_batch_id": ctx["batch_id"], "idempotency_key": "permission-run"}
    reviewer = user_with_role("fact-run-reviewer@example.com", "REVIEWER")
    assert client.post(runs_url, headers=auth(reviewer), json=payload).status_code == 403
    assert client.get(runs_url, headers=auth(reviewer)).status_code == 403
    operator = user_with_role("fact-run-operator@example.com", "ANALYSIS_OPERATOR")
    launched = client.post(runs_url, headers=auth(operator), json=payload)
    assert launched.status_code == 202, launched.text
    assert client.post(
        f"/v1/jobs/FACT_DEVELOPMENT_ANALYSIS_TASK/{launched.json()['id']}/cancel",
        headers=auth(reviewer),
    ).status_code == 403
