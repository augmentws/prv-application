import uuid

import pytest
from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from pydantic_ai.models.test import TestModel
from sqlalchemy import select

from app.fact_development import (
    finalize_fact_development_run,
    prepare_fact_development_run,
    process_fact_development_chunk,
    run_ordered_window,
    sweep_duplicate_facts,
)
from app.fact_extraction import CreateDecision
from app.fact_recall import RecalledFact
from app.models import MatterAnalysisTaskRun, MatterFact, MatterFactSuggestion, SkillRun, WorkflowStepRun
from tests.test_agents_and_matter_definitions import auth
from tests.test_fact_development_runs import (  # noqa: F401
    _launch,
    _operation,
    artifacts,
    inputs,
    run_ctx,
)
from tests.test_fact_search import FakeEmbeddings
from tests.test_facts_and_entities import DOCUMENT_TEXT, document_text  # noqa: F401

SUMMARY = "Jane Smith approved a 5% price increase for Acme customers."


@pytest.fixture(autouse=True)
def embeddings(monkeypatch: pytest.MonkeyPatch) -> FakeEmbeddings:
    gateway = FakeEmbeddings()
    monkeypatch.setattr("app.fact_recall.get_embedding_gateway", lambda: gateway)
    return gateway


def _existing_fact(client: TestClient, ctx: dict, *, summary: str = SUMMARY, status: str = "WORKING") -> dict:
    created = client.post(
        f"/v1/matters/{ctx['matter_id']}/facts",
        headers=auth(ctx["token"]),
        json={
            "summary": summary,
            "date_precision": "DAY",
            "event_date_start": "2021-03-03",
            "date_basis": "STATED_IN_TEXT",
            "issues": [{"matter_analysis_task_id": ctx["task_id"], "issue_key": "pricing"}],
            "evidence": [
                {"matter_document_id": ctx["documents"][2], "paragraph_id": "¶2", "quote": "The increase is final."}
            ],
        },
    )
    assert created.status_code == 201, created.text
    fact = created.json()
    if status != "WORKING":
        action = "confirm" if status == "CONFIRMED" else "reject"
        response = client.post(
            f"/v1/matters/{ctx['matter_id']}/facts/{fact['id']}/{action}",
            headers=auth(ctx["token"]),
            json={"lock_version": fact["lock_version"]},
        )
        assert response.status_code == 200, response.text
        fact = response.json()
    return fact


def _run(client: TestClient, ctx: dict, operations: list[dict], consolidation: dict | None = None) -> dict:
    run = _launch(client, ctx)
    with TestingSessionLocal() as db:
        chunk_ids = prepare_fact_development_run(db, uuid.UUID(run["id"]))
    for chunk_id in chunk_ids:
        process_fact_development_chunk(
            uuid.UUID(chunk_id),
            session_factory=TestingSessionLocal,
            model=TestModel(custom_output_args={"operations": operations}),
            consolidation_model=TestModel(
                custom_output_args=consolidation or {"decision": "DISTINCT", "fact_id": None, "rationale": "Different."}
            ),
        )
    with TestingSessionLocal() as db:
        finalize_fact_development_run(db, uuid.UUID(run["id"]))
        return db.get(MatterAnalysisTaskRun, uuid.UUID(run["id"])).report


def _consolidation_runs() -> int:
    with TestingSessionLocal() as db:
        return int(
            db.query(SkillRun)
            .join(WorkflowStepRun, WorkflowStepRun.id == SkillRun.workflow_step_run_id)
            .filter(WorkflowStepRun.role_key == "fact_consolidation")
            .count()
        )


def test_duplicate_create_adds_its_quotes_to_the_existing_fact(
    client: TestClient,
    run_ctx: dict,  # noqa: F811
    artifacts: dict,  # noqa: F811
) -> None:
    existing = _existing_fact(client, run_ctx)
    report = _run(
        client,
        run_ctx,
        [_operation()],
        {"decision": "SAME", "fact_id": existing["id"], "rationale": "Same approval."},
    )

    assert report["counts"]["merged_into_existing"] == 1
    assert report["counts"]["facts_created"] == 0
    assert _consolidation_runs() == 1  # Only the pricing document's CREATE could apply.
    with TestingSessionLocal() as db:
        assert db.query(MatterFact).count() == 1
        assert db.get(MatterFact, uuid.UUID(existing["id"])).evidence_count == 2


def test_duplicate_of_a_rejected_fact_is_blocked(client: TestClient, run_ctx: dict, artifacts: dict) -> None:  # noqa: F811
    rejected = _existing_fact(client, run_ctx, status="REJECTED")
    report = _run(
        client,
        run_ctx,
        [_operation()],
        {"decision": "SAME", "fact_id": rejected["id"], "rationale": "Same approval."},
    )

    assert report["counts"]["blocked_by_rejected"] == 1
    assert report["counts"]["invalid_operations"] == 1  # Only the safety document's uncoded pricing CREATE.
    with TestingSessionLocal() as db:
        assert db.query(MatterFact).count() == 1
        fact = db.get(MatterFact, uuid.UUID(rejected["id"]))
        assert (fact.status, fact.evidence_count) == ("REJECTED", 1)


def test_dissimilar_summary_creates_without_asking_the_consolidator(
    client: TestClient,
    run_ctx: dict,  # noqa: F811
    artifacts: dict,  # noqa: F811
) -> None:
    _existing_fact(client, run_ctx, summary="Bob Lee reported a safety incident at the plant.")
    report = _run(client, run_ctx, [_operation()])

    assert report["counts"]["facts_created"] == 1
    assert _consolidation_runs() == 0  # Nothing was similar enough to ask the consolidator.


def test_distinct_decision_creates_a_new_fact(client: TestClient, run_ctx: dict, artifacts: dict) -> None:  # noqa: F811
    _existing_fact(client, run_ctx)
    report = _run(client, run_ctx, [_operation()])  # The consolidator answers DISTINCT by default.

    assert _consolidation_runs() == 1
    assert report["counts"]["facts_created"] == 1
    assert report["counts"]["merged_into_existing"] == 0
    with TestingSessionLocal() as db:
        assert db.query(MatterFact).count() == 2


def test_recheck_catches_a_fact_created_after_the_decision(
    client: TestClient,
    run_ctx: dict,  # noqa: F811
    artifacts: dict,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import fact_development

    real_decide = fact_development.decide
    concurrent: dict = {}

    def decide(db, settings, operation, ask, **kwargs):
        if not concurrent:
            # Another worker creates the same fact between this ruling and the lock.
            concurrent.update(_existing_fact(client, run_ctx))
            return CreateDecision("CREATE")
        return real_decide(db, settings, operation, ask, **kwargs)

    monkeypatch.setattr(fact_development, "decide", decide)
    monkeypatch.setattr(
        fact_development,
        "_ask_consolidator",
        lambda db, context, document_id, proposed, candidates: {
            "decision": "SAME",
            "fact_id": candidates[0]["fact_id"],
            "rationale": "Same approval.",
        },
    )
    report = _run(client, run_ctx, [_operation()])

    assert report["counts"]["duplicate_rechecks"] == 1
    assert report["counts"]["merged_into_existing"] == 1
    with TestingSessionLocal() as db:
        assert db.query(MatterFact).count() == 1
        assert db.get(MatterFact, uuid.UUID(concurrent["id"])).evidence_count == 2


def _refine_operation(fact_id: str) -> dict:
    return _operation(
        op="REFINE",
        fact_id=fact_id,
        refine_fields=["summary"],
        summary="Jane Smith approved a 5% price increase for all Acme customers on March 3, 2021.",
        owner=None,
        participants=[],
        reason="The later document names the effective date.",
    )


@pytest.fixture
def recall_existing(monkeypatch: pytest.MonkeyPatch):
    recalled: dict = {}

    def recall(db, **kwargs):
        if not recalled:
            return []
        fact = db.get(MatterFact, uuid.UUID(recalled["id"]))
        return [RecalledFact(fact=fact, similarity=0.9, shared_participants=0, rejected=fact.status == "REJECTED")]

    monkeypatch.setattr("app.fact_development.recall_facts", recall)
    return recalled


def test_refine_revises_a_working_fact(client: TestClient, run_ctx: dict, artifacts: dict, recall_existing) -> None:  # noqa: F811
    existing = _existing_fact(client, run_ctx)
    recall_existing.update(existing)
    report = _run(client, run_ctx, [_refine_operation(existing["id"])])

    assert report["counts"]["refined"] == 1
    with TestingSessionLocal() as db:
        fact = db.get(MatterFact, uuid.UUID(existing["id"]))
        assert fact.summary.endswith("on March 3, 2021.")
        assert fact.current_revision == 2
        assert fact.evidence_count == 2
        assert db.query(MatterFactSuggestion).count() == 0


def test_refine_of_a_confirmed_fact_waits_for_a_reviewer(
    client: TestClient,
    run_ctx: dict,  # noqa: F811
    artifacts: dict,  # noqa: F811
    recall_existing,
) -> None:
    ctx = run_ctx
    confirmed = _existing_fact(client, ctx, status="CONFIRMED")
    recall_existing.update(confirmed)
    report = _run(client, ctx, [_refine_operation(confirmed["id"])])

    assert report["counts"]["suggestions_created"] == 1
    facts_url = f"/v1/matters/{ctx['matter_id']}/facts/{confirmed['id']}"
    unchanged = client.get(facts_url, headers=auth(ctx["token"])).json()
    assert unchanged["summary"] == SUMMARY
    assert unchanged["lock_version"] == confirmed["lock_version"]
    suggestions = client.get(f"{facts_url}/suggestions", headers=auth(ctx["token"])).json()
    assert [(item["kind"], item["status"]) for item in suggestions] == [("REVISION", "PENDING")]
    assert suggestions[0]["changes"]["summary"].endswith("on March 3, 2021.")
    suggestion_url = f"{facts_url}/suggestions/{suggestions[0]['id']}"

    stale = client.post(
        f"{suggestion_url}/accept",
        headers=auth(ctx["token"]),
        json={"lock_version": confirmed["lock_version"] - 1},
    )
    assert stale.status_code == 409
    accepted = client.post(
        f"{suggestion_url}/accept",
        headers=auth(ctx["token"]),
        json={"lock_version": confirmed["lock_version"]},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["summary"].endswith("on March 3, 2021.")
    assert accepted.json()["status"] == "CONFIRMED"
    assert accepted.json()["evidence_count"] == 2
    again = client.post(
        f"{suggestion_url}/accept",
        headers=auth(ctx["token"]),
        json={"lock_version": accepted.json()["lock_version"]},
    )
    assert again.status_code == 409
    with TestingSessionLocal() as db:
        suggestion = db.scalar(select(MatterFactSuggestion))
        assert suggestion.status == "ACCEPTED"
        assert suggestion.resolved_by_user_id is not None


def test_dismissed_suggestion_leaves_the_fact_and_rejected_facts_cannot_be_refined(
    client: TestClient,
    run_ctx: dict,  # noqa: F811
    artifacts: dict,  # noqa: F811
    recall_existing,
) -> None:
    ctx = run_ctx
    confirmed = _existing_fact(client, ctx, status="CONFIRMED")
    recall_existing.update(confirmed)
    _run(client, ctx, [_refine_operation(confirmed["id"])])
    facts_url = f"/v1/matters/{ctx['matter_id']}/facts/{confirmed['id']}"
    suggestion = client.get(f"{facts_url}/suggestions", headers=auth(ctx["token"])).json()[0]
    dismissed = client.post(
        f"{facts_url}/suggestions/{suggestion['id']}/dismiss",
        headers=auth(ctx["token"]),
        json={"note": "The date is already in the timeline entry."},
    )
    assert dismissed.status_code == 200, dismissed.text
    assert dismissed.json()["status"] == "DISMISSED"
    assert client.get(facts_url, headers=auth(ctx["token"])).json()["summary"] == SUMMARY

    rejected = _existing_fact(client, ctx, status="REJECTED")
    recall_existing.clear()
    recall_existing.update(rejected)
    run = client.post(
        f"/v1/matters/{ctx['matter_id']}/analysis-tasks/{ctx['task_id']}/fact-runs",
        headers=auth(ctx["token"]),
        json={"review_batch_id": ctx["batch_id"], "idempotency_key": "rejected-refine"},
    ).json()
    with TestingSessionLocal() as db:
        chunk_ids = prepare_fact_development_run(db, uuid.UUID(run["id"]))
    for chunk_id in chunk_ids:
        process_fact_development_chunk(
            uuid.UUID(chunk_id),
            session_factory=TestingSessionLocal,
            model=TestModel(custom_output_args={"operations": [_refine_operation(rejected["id"])]}),
        )
    with TestingSessionLocal() as db:
        finalize_fact_development_run(db, uuid.UUID(run["id"]))
        report = db.get(MatterAnalysisTaskRun, uuid.UUID(run["id"])).report
        assert db.get(MatterFact, uuid.UUID(rejected["id"])).current_revision == 1
    assert report["counts"]["suggestions_created"] == 0
    assert report["counts"]["refined"] == 0


def test_add_evidence_may_extend_a_confirmed_fact(
    client: TestClient,
    run_ctx: dict,  # noqa: F811
    artifacts: dict,  # noqa: F811
    recall_existing,
) -> None:
    confirmed = _existing_fact(client, run_ctx, status="CONFIRMED")
    recall_existing.update(confirmed)
    report = _run(
        client,
        run_ctx,
        [_operation(op="ADD_EVIDENCE", fact_id=confirmed["id"], summary=None, owner=None, participants=[])],
    )

    assert report["counts"]["evidence_added"] == 1
    with TestingSessionLocal() as db:
        fact = db.get(MatterFact, uuid.UUID(confirmed["id"]))
        assert (fact.status, fact.evidence_count, fact.current_revision) == ("CONFIRMED", 2, 1)


def test_ordered_window_keeps_chunks_in_order_with_bounded_concurrency() -> None:
    events: list[tuple[str, str]] = []
    in_flight: set[str] = set()
    peak = 0

    def start(chunk_id: str) -> str:
        nonlocal peak
        in_flight.add(chunk_id)
        peak = max(peak, len(in_flight))
        events.append(("start", chunk_id))
        return chunk_id

    def wait(handle: str) -> None:
        in_flight.discard(handle)
        events.append(("done", handle))

    chunk_ids = [f"c{number}" for number in range(7)]
    run_ordered_window(chunk_ids, 3, start, wait)

    assert peak == 3
    assert [chunk for action, chunk in events if action == "start"] == chunk_ids
    assert [chunk for action, chunk in events if action == "done"] == chunk_ids
    # Chunk k + 3 starts only after chunk k has finished.
    for number in range(3, 7):
        assert events.index(("done", f"c{number - 3}")) < events.index(("start", f"c{number}"))


def _agent_fact(db, existing_id: str) -> MatterFact:
    return db.scalar(
        select(MatterFact).where(MatterFact.origin_workflow_run_id.isnot(None), MatterFact.id != uuid.UUID(existing_id))
    )


def test_sweep_suggests_merges_without_merging_and_accepting_merges(
    client: TestClient,
    run_ctx: dict,  # noqa: F811
    artifacts: dict,  # noqa: F811
) -> None:
    ctx = run_ctx
    existing = _existing_fact(client, ctx)
    report = _run(client, ctx, [_operation()])  # The consolidator rules DISTINCT, so a near-duplicate is created.

    assert report["counts"]["facts_created"] == 1
    assert report["sweep"] == {"working_facts_checked": 1, "merge_suggestions_created": 1}
    with TestingSessionLocal() as db:
        agent_fact = _agent_fact(db, existing["id"])
        assert {fact.status for fact in db.scalars(select(MatterFact))} == {"WORKING"}  # Nothing merged automatically.
        suggestion = db.scalar(select(MatterFactSuggestion).where(MatterFactSuggestion.kind == "MERGE"))
        # The newer agent fact is suggested for merging into the older fact.
        assert (suggestion.fact_id, suggestion.target_fact_id) == (agent_fact.id, uuid.UUID(existing["id"]))
        run_row = db.scalar(select(MatterAnalysisTaskRun))
        assert sweep_duplicate_facts(db, run_row)["merge_suggestions_created"] == 0  # Safe to repeat.
        agent_fact_id, agent_lock = str(agent_fact.id), agent_fact.lock_version

    accept_url = f"/v1/matters/{ctx['matter_id']}/facts/{agent_fact_id}/suggestions/{suggestion.id}/accept"
    missing_target = client.post(accept_url, headers=auth(ctx["token"]), json={"lock_version": agent_lock})
    assert missing_target.status_code == 422
    accepted = client.post(
        accept_url,
        headers=auth(ctx["token"]),
        json={"lock_version": agent_lock, "target_lock_version": existing["lock_version"]},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["id"] == existing["id"]
    assert accepted.json()["evidence_count"] == 2
    with TestingSessionLocal() as db:
        merged = db.get(MatterFact, uuid.UUID(agent_fact_id))
        assert (merged.status, merged.merged_into_fact_id) == ("MERGED", uuid.UUID(existing["id"]))


def test_sweep_keeps_confirmed_facts_and_respects_dismissals(
    client: TestClient,
    run_ctx: dict,  # noqa: F811
    artifacts: dict,  # noqa: F811
) -> None:
    ctx = run_ctx
    confirmed = _existing_fact(client, ctx, status="CONFIRMED")
    _run(client, ctx, [_operation()])
    with TestingSessionLocal() as db:
        suggestion = db.scalar(select(MatterFactSuggestion).where(MatterFactSuggestion.kind == "MERGE"))
        assert suggestion.target_fact_id == uuid.UUID(confirmed["id"])  # A confirmed fact is never absorbed.
    dismissed = client.post(
        f"/v1/matters/{ctx['matter_id']}/facts/{suggestion.fact_id}/suggestions/{suggestion.id}/dismiss",
        headers=auth(ctx["token"]),
        json={"note": "Different approvals."},
    )
    assert dismissed.status_code == 200, dismissed.text
    with TestingSessionLocal() as db:
        run_row = db.scalar(select(MatterAnalysisTaskRun))
        assert sweep_duplicate_facts(db, run_row)["merge_suggestions_created"] == 0
        db.commit()
        assert db.query(MatterFactSuggestion).filter(MatterFactSuggestion.kind == "MERGE").count() == 1


def test_estimate_matches_what_a_run_would_process(
    client: TestClient,
    run_ctx: dict,  # noqa: F811
    artifacts: dict,  # noqa: F811
) -> None:
    ctx = run_ctx
    estimate_url = f"/v1/matters/{ctx['matter_id']}/analysis-tasks/{ctx['task_id']}/fact-runs/estimate"
    before = client.post(estimate_url, headers=auth(ctx["token"]), json={"review_batch_id": ctx["batch_id"]})
    assert before.status_code == 200, before.text
    estimate = before.json()
    assert (
        estimate["batch_document_count"],
        estimate["matched_document_count"],
        estimate["excluded_document_count"],
        estimate["missing_text_document_count"],
        estimate["previously_extracted_count"],
        estimate["documents_to_extract"],
    ) == (3, 2, 1, 0, 0, 2)
    assert estimate["text_bytes_to_extract"] == 2 * len(DOCUMENT_TEXT)
    assert estimate["estimated_input_tokens"] == 2 * len(DOCUMENT_TEXT) // 4 + 2 * 4_000
    assert estimate["estimated_output_tokens"] == 2 * 800
    assert {item["issue_key"]: item["document_count"] for item in estimate["issue_document_counts"]} == {
        "pricing": 1,
        "safety": 1,
    }

    _run(client, ctx, [_operation(), _operation(op="NO_FACT", issue_keys=["safety"], summary=None,
                                                date_precision="UNKNOWN", event_date_start=None, date_basis=None,
                                                owner=None, participants=[], evidence=[], reason="None.")])
    after = client.post(estimate_url, headers=auth(ctx["token"]), json={"review_batch_id": ctx["batch_id"]}).json()
    # Both coded documents finished, so a new run would reuse them and extract nothing.
    assert (after["previously_extracted_count"], after["documents_to_extract"], after["estimated_input_tokens"]) == (
        2,
        0,
        0,
    )
    missing = client.post(estimate_url, headers=auth(ctx["token"]), json={"review_batch_id": str(uuid.uuid4())})
    assert missing.status_code == 409
