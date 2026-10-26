import json
import uuid
from dataclasses import dataclass, field, replace

import pytest
from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from sqlalchemy import select

from app.fact_batches import poll_chunk_batch, submit_chunk_batch
from app.fact_development import (
    chunk_window,
    finalize_fact_development_run,
    prepare_fact_development_run,
    process_fact_development_chunk,
)
from app.model_batching import BatchModelResult, BatchModelTarget, BatchPoll, BatchSubmission
from app.models import MatterAnalysisTaskRun, MatterAnalysisTaskRunChunk, MatterFact, ModelInvocation, SkillRun
from tests.test_agents_and_matter_definitions import auth
from tests.test_fact_development_runs import (  # noqa: F401 - fixtures
    _extraction_output,
    _reasons,
    artifacts,
    inputs,
    run_ctx,
)
from tests.test_facts_and_entities import document_text  # noqa: F401


@dataclass
class FakeBatchAdapter:
    provider: str = "fake"
    states: list[str] = field(default_factory=lambda: ["JOB_STATE_RUNNING", "JOB_STATE_SUCCEEDED"])
    outputs: dict[str, str | None] = field(default_factory=dict)
    submitted: list[list] = field(default_factory=list)
    canceled: list[str] = field(default_factory=list)
    submit_error: Exception | None = None
    poll_error: Exception | None = None
    results_error: Exception | None = None
    on_submit: object = None
    found: dict[str, str] = field(default_factory=dict)
    looked_up: list[str] = field(default_factory=list)

    def submit(self, *, model, requests, display_name):
        if self.submit_error is not None:
            raise self.submit_error
        self.submitted.append(requests)
        self.found[display_name] = "batches/fake-1"
        if callable(self.on_submit):
            self.on_submit()
        return BatchSubmission(batch_id="batches/fake-1", status="JOB_STATE_PENDING")

    def find(self, display_name):
        self.looked_up.append(display_name)
        batch_id = self.found.get(display_name)
        return BatchSubmission(batch_id=batch_id, status="JOB_STATE_RUNNING") if batch_id else None

    def poll(self, batch_id):
        if self.poll_error is not None:
            raise self.poll_error
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return BatchPoll(status=state, error="quota exhausted" if state == "JOB_STATE_FAILED" else None)

    def results(self, batch_id):
        if self.results_error is not None:
            raise self.results_error
        values = []
        for request in self.submitted[-1]:
            output = next((value for key, value in self.outputs.items() if key in request.prompt), json.dumps(_extraction_output()))
            values.append(
                BatchModelResult(
                    custom_id=request.custom_id,
                    output_text=output,
                    provider_request_id=f"response-{request.custom_id}",
                    input_tokens=1000,
                    output_tokens=200,
                )
            )
        return self.states[-1], values

    def cancel(self, batch_id):
        self.canceled.append(batch_id)


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch) -> FakeBatchAdapter:
    fake = FakeBatchAdapter()

    def resolve(model_key):
        return (fake, BatchModelTarget(provider="fake", model="fake-model")) if isinstance(model_key, str) else None

    monkeypatch.setattr("app.fact_development.resolve_batch_target", resolve)
    monkeypatch.setattr("app.fact_batches.resolve_batch_target", resolve)
    monkeypatch.setattr("app.fact_batches.get_batch_adapter", lambda provider: fake)
    return fake


def _launch_batched(client: TestClient, ctx: dict, key: str = "batched-1") -> dict:
    response = client.post(
        f"/v1/matters/{ctx['matter_id']}/analysis-tasks/{ctx['task_id']}/fact-runs",
        headers=auth(ctx["token"]),
        json={"review_batch_id": ctx["batch_id"], "idempotency_key": key, "use_batch_api": True},
    )
    assert response.status_code == 202, response.text
    assert response.json()["configuration_snapshot"]["use_batch_api"] is True
    return response.json()


def _no_realtime_calls():
    def fail(messages, info):
        raise AssertionError("A batched document must not be extracted in real time")

    return FunctionModel(fail)


def _run_batched(run_id: str, model, polls: int = 10) -> list[str]:
    with TestingSessionLocal() as db:
        chunk_ids = prepare_fact_development_run(db, uuid.UUID(run_id))
    for chunk_id in chunk_ids:
        with TestingSessionLocal() as db:
            if submit_chunk_batch(db, uuid.UUID(chunk_id))["submitted"]:
                for _ in range(polls):
                    with TestingSessionLocal() as poll_db:
                        if not poll_chunk_batch(poll_db, uuid.UUID(chunk_id)):
                            break
                else:
                    pytest.fail("The provider batch never finished")
        process_fact_development_chunk(uuid.UUID(chunk_id), session_factory=TestingSessionLocal, model=model)
    with TestingSessionLocal() as db:
        finalize_fact_development_run(db, uuid.UUID(run_id))
    return chunk_ids


def _counts(run_id: str) -> dict:
    with TestingSessionLocal() as db:
        return db.get(MatterAnalysisTaskRun, uuid.UUID(run_id)).report["counts"]


def test_batched_run_applies_provider_results_in_order(
    client: TestClient, run_ctx: dict, artifacts: dict, adapter: FakeBatchAdapter  # noqa: F811 - pytest fixtures
) -> None:
    run = _launch_batched(client, run_ctx)
    chunk_ids = _run_batched(run["id"], _no_realtime_calls())

    # Only the two coded documents need a call; the uncoded one is settled without the model.
    assert len(adapter.submitted) == 1
    assert len(adapter.submitted[0]) == 2
    counts = _counts(run["id"])
    assert counts["batched"] == 2
    assert counts["batch_fallbacks"] == 0
    assert counts["facts_created"] == 1
    assert _reasons(run["id"]) == {"fact.facts_proposed": 1, "fact.no_fact": 1, "fact.no_matching_issue": 1}
    with TestingSessionLocal() as db:
        chunk = db.get(MatterAnalysisTaskRunChunk, uuid.UUID(chunk_ids[0]))
        assert chunk.provider_batch_status == "SUCCEEDED"
        assert chunk.provider_batch_id == "batches/fake-1"
        assert {item["document_id"] for item in chunk.provider_manifest["requests"]} == set(run_ctx["documents"][:2])
        skill_runs = list(db.scalars(select(SkillRun).where(SkillRun.scope_type == "MATTER_DOCUMENT")))
        assert len(skill_runs) == 2
        assert {skill_run.status for skill_run in skill_runs} == {"COMPLETED"}
        invocations = list(db.scalars(select(ModelInvocation).where(ModelInvocation.skill_run_id.in_([r.id for r in skill_runs]))))
        assert sum(item.input_tokens for item in invocations) == 2000
        assert db.query(MatterFact).count() == 1
        run_row = db.get(MatterAnalysisTaskRun, uuid.UUID(run["id"]))
        assert run_row.status == "COMPLETED"


def test_unusable_batch_result_falls_back_to_a_real_time_call(
    client: TestClient, run_ctx: dict, artifacts: dict, adapter: FakeBatchAdapter  # noqa: F811 - pytest fixtures
) -> None:
    # The pricing document's batch result is not valid JSON; it is extracted again in real time.
    adapter.outputs = {run_ctx["documents"][0]: "not json"}
    run = _launch_batched(client, run_ctx)
    _run_batched(run["id"], TestModel(custom_output_args=_extraction_output()))

    counts = _counts(run["id"])
    assert counts["batched"] == 1
    assert counts["batch_fallbacks"] == 1
    assert counts["facts_created"] == 1
    with TestingSessionLocal() as db:
        statuses = sorted(row.status for row in db.scalars(select(SkillRun).where(SkillRun.scope_type == "MATTER_DOCUMENT")))
        assert statuses == ["COMPLETED", "COMPLETED", "FAILED"]


def test_failed_provider_batch_extracts_the_chunk_in_real_time(
    client: TestClient, run_ctx: dict, artifacts: dict, adapter: FakeBatchAdapter  # noqa: F811 - pytest fixtures
) -> None:
    adapter.states = ["JOB_STATE_FAILED"]
    run = _launch_batched(client, run_ctx)
    chunk_ids = _run_batched(run["id"], TestModel(custom_output_args=_extraction_output()))

    counts = _counts(run["id"])
    assert counts["batched"] == 0
    assert counts["facts_created"] == 1
    with TestingSessionLocal() as db:
        chunk = db.get(MatterAnalysisTaskRunChunk, uuid.UUID(chunk_ids[0]))
        assert chunk.provider_batch_status == "FAILED"
        assert chunk.provider_error == "quota exhausted"
        statuses = sorted(row.status for row in db.scalars(select(SkillRun).where(SkillRun.scope_type == "MATTER_DOCUMENT")))
        assert statuses == ["COMPLETED", "COMPLETED", "FAILED", "FAILED"]


def test_failed_submission_extracts_the_chunk_in_real_time(
    client: TestClient, run_ctx: dict, artifacts: dict, adapter: FakeBatchAdapter  # noqa: F811 - pytest fixtures
) -> None:
    adapter.submit_error = RuntimeError("provider unavailable")
    run = _launch_batched(client, run_ctx)
    chunk_ids = _run_batched(run["id"], TestModel(custom_output_args=_extraction_output()))

    assert _counts(run["id"])["facts_created"] == 1
    with TestingSessionLocal() as db:
        chunk = db.get(MatterAnalysisTaskRunChunk, uuid.UUID(chunk_ids[0]))
        assert chunk.provider_batch_status == "FAILED"
        assert "provider unavailable" in chunk.provider_error
        # The batch skill runs are recorded as failed; the documents were then extracted in real time.
        statuses = sorted(row.status for row in db.scalars(select(SkillRun).where(SkillRun.scope_type == "MATTER_DOCUMENT")))
        assert statuses == ["COMPLETED", "COMPLETED", "FAILED", "FAILED"]


def test_cancel_cancels_the_pending_provider_batch(
    client: TestClient, run_ctx: dict, artifacts: dict, adapter: FakeBatchAdapter  # noqa: F811 - pytest fixtures
) -> None:
    adapter.states = ["JOB_STATE_RUNNING"]
    run = _launch_batched(client, run_ctx)
    with TestingSessionLocal() as db:
        chunk_ids = prepare_fact_development_run(db, uuid.UUID(run["id"]))
    with TestingSessionLocal() as db:
        assert submit_chunk_batch(db, uuid.UUID(chunk_ids[0]))["submitted"] is True
    with TestingSessionLocal() as db:
        assert poll_chunk_batch(db, uuid.UUID(chunk_ids[0])) is True

    canceled = client.post(f"/v1/jobs/FACT_DEVELOPMENT_ANALYSIS_TASK/{run['id']}/cancel", headers=auth(run_ctx["token"]))
    assert canceled.status_code == 200, canceled.text
    assert adapter.canceled == ["batches/fake-1"]
    with TestingSessionLocal() as db:
        chunk = db.get(MatterAnalysisTaskRunChunk, uuid.UUID(chunk_ids[0]))
        assert chunk.provider_batch_status == "CANCELED"
        assert {row.status for row in db.scalars(select(SkillRun).where(SkillRun.scope_type == "MATTER_DOCUMENT"))} == {"FAILED"}
    with TestingSessionLocal() as db:
        assert poll_chunk_batch(db, uuid.UUID(chunk_ids[0])) is False


def test_cancel_after_the_batch_finished_closes_unapplied_skill_runs(
    client: TestClient, run_ctx: dict, artifacts: dict, adapter: FakeBatchAdapter  # noqa: F811 - pytest fixtures
) -> None:
    adapter.states = ["JOB_STATE_SUCCEEDED"]
    run = _launch_batched(client, run_ctx)
    with TestingSessionLocal() as db:
        chunk_ids = prepare_fact_development_run(db, uuid.UUID(run["id"]))
    with TestingSessionLocal() as db:
        assert submit_chunk_batch(db, uuid.UUID(chunk_ids[0]))["submitted"] is True
    with TestingSessionLocal() as db:
        assert poll_chunk_batch(db, uuid.UUID(chunk_ids[0])) is False
    canceled = client.post(f"/v1/jobs/FACT_DEVELOPMENT_ANALYSIS_TASK/{run['id']}/cancel", headers=auth(run_ctx["token"]))
    assert canceled.status_code == 200, canceled.text
    process_fact_development_chunk(uuid.UUID(chunk_ids[0]), session_factory=TestingSessionLocal, model=_no_realtime_calls())

    with TestingSessionLocal() as db:
        statuses = {row.status for row in db.scalars(select(SkillRun).where(SkillRun.scope_type == "MATTER_DOCUMENT"))}
        assert statuses == {"FAILED"}
        assert db.query(MatterFact).count() == 0


def test_batch_api_requires_a_supported_model(
    client: TestClient,
    run_ctx: dict,  # noqa: F811 - pytest fixture imported for reuse
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.fact_development.resolve_batch_target", lambda _model_key: None)
    estimate = client.post(
        f"/v1/matters/{run_ctx['matter_id']}/analysis-tasks/{run_ctx['task_id']}/fact-runs/estimate",
        headers=auth(run_ctx["token"]),
        json={"review_batch_id": run_ctx["batch_id"]},
    )
    assert estimate.status_code == 200, estimate.text
    assert estimate.json()["batch_api_available"] is False
    refused = client.post(
        f"/v1/matters/{run_ctx['matter_id']}/analysis-tasks/{run_ctx['task_id']}/fact-runs",
        headers=auth(run_ctx["token"]),
        json={"review_batch_id": run_ctx["batch_id"], "idempotency_key": "batched", "use_batch_api": True},
    )
    assert refused.status_code == 409
    assert "Batch API" in refused.json()["error"]["message"]


def test_estimate_reports_batch_api_support(client: TestClient, run_ctx: dict, adapter: FakeBatchAdapter) -> None:  # noqa: F811 - pytest fixtures
    estimate = client.post(
        f"/v1/matters/{run_ctx['matter_id']}/analysis-tasks/{run_ctx['task_id']}/fact-runs/estimate",
        headers=auth(run_ctx["token"]),
        json={"review_batch_id": run_ctx["batch_id"]},
    )
    assert estimate.json()["batch_api_available"] is True


def _prepared_chunk(client: TestClient, ctx: dict) -> tuple[dict, uuid.UUID]:
    run = _launch_batched(client, ctx)
    with TestingSessionLocal() as db:
        chunk_ids = prepare_fact_development_run(db, uuid.UUID(run["id"]))
    return run, uuid.UUID(chunk_ids[0])


def test_batch_runs_wait_one_chunk_at_a_time(client: TestClient, run_ctx: dict, adapter: FakeBatchAdapter) -> None:  # noqa: F811
    run = _launch_batched(client, run_ctx)
    with TestingSessionLocal() as db:
        # Each chunk's requests are built after the previous chunk's facts are applied.
        assert chunk_window(db, uuid.UUID(run["id"])) == 1


def test_resubmitting_a_chunk_does_not_create_a_second_batch(
    client: TestClient, run_ctx: dict, artifacts: dict, adapter: FakeBatchAdapter  # noqa: F811 - pytest fixtures
) -> None:
    _, chunk_id = _prepared_chunk(client, run_ctx)
    with TestingSessionLocal() as db:
        assert submit_chunk_batch(db, chunk_id)["submitted"] is True
    with TestingSessionLocal() as db:
        assert submit_chunk_batch(db, chunk_id)["submitted"] is True
    assert len(adapter.submitted) == 1


def test_an_interrupted_submission_adopts_the_provider_batch_or_falls_back(
    client: TestClient, run_ctx: dict, artifacts: dict, adapter: FakeBatchAdapter  # noqa: F811 - pytest fixtures
) -> None:
    _, chunk_id = _prepared_chunk(client, run_ctx)

    def crash():
        raise SystemExit("worker died after the provider accepted the batch")

    adapter.on_submit = crash
    with TestingSessionLocal() as db, pytest.raises(SystemExit):
        submit_chunk_batch(db, chunk_id)
    adapter.on_submit = None
    # The retried step finds the batch the provider already accepted instead of submitting it again.
    with TestingSessionLocal() as db:
        assert submit_chunk_batch(db, chunk_id)["submitted"] is True
    assert len(adapter.submitted) == 1
    with TestingSessionLocal() as db:
        chunk = db.get(MatterAnalysisTaskRunChunk, chunk_id)
        assert chunk.provider_batch_id == "batches/fake-1"
        assert db.query(SkillRun).filter(SkillRun.scope_type == "MATTER_DOCUMENT", SkillRun.status == "RUNNING").count() == 2

    # When the provider has no such batch, the chunk is extracted in real time rather than submitted twice.
    with TestingSessionLocal() as db:
        chunk = db.get(MatterAnalysisTaskRunChunk, chunk_id)
        chunk.provider_batch_id = None
        db.commit()
    adapter.found.clear()
    with TestingSessionLocal() as db:
        assert submit_chunk_batch(db, chunk_id)["submitted"] is False
    with TestingSessionLocal() as db:
        chunk = db.get(MatterAnalysisTaskRunChunk, chunk_id)
        assert chunk.provider_batch_status == "FAILED"
        assert db.query(SkillRun).filter(SkillRun.scope_type == "MATTER_DOCUMENT", SkillRun.status == "RUNNING").count() == 0
    assert len(adapter.submitted) == 1


def test_cancel_during_submission_cancels_the_new_batch(
    client: TestClient, run_ctx: dict, artifacts: dict, adapter: FakeBatchAdapter  # noqa: F811 - pytest fixtures
) -> None:
    run, chunk_id = _prepared_chunk(client, run_ctx)

    def cancel_now():
        response = client.post(f"/v1/jobs/FACT_DEVELOPMENT_ANALYSIS_TASK/{run['id']}/cancel", headers=auth(run_ctx["token"]))
        assert response.status_code == 200, response.text

    adapter.on_submit = cancel_now
    with TestingSessionLocal() as db:
        assert submit_chunk_batch(db, chunk_id)["submitted"] is False
    assert adapter.canceled == ["batches/fake-1"]
    with TestingSessionLocal() as db:
        chunk = db.get(MatterAnalysisTaskRunChunk, chunk_id)
        assert chunk.provider_batch_status == "CANCELED"
        assert chunk.provider_batch_id == "batches/fake-1"


def test_provider_errors_fall_back_to_real_time(
    client: TestClient, run_ctx: dict, artifacts: dict, adapter: FakeBatchAdapter  # noqa: F811 - pytest fixtures
) -> None:
    adapter.poll_error = ConnectionError("provider unreachable")
    run = _launch_batched(client, run_ctx)
    chunk_ids = _run_batched(run["id"], TestModel(custom_output_args=_extraction_output()), polls=30)
    assert _counts(run["id"])["facts_created"] == 1
    # The batch is stopped at the provider so it is not paid for alongside the real-time calls.
    assert adapter.canceled == ["batches/fake-1"]
    with TestingSessionLocal() as db:
        chunk = db.get(MatterAnalysisTaskRunChunk, uuid.UUID(chunk_ids[0]))
        assert chunk.provider_batch_status == "FAILED"
        assert "provider unreachable" in chunk.provider_error


def test_unreadable_results_are_retried_then_fall_back_to_real_time(
    client: TestClient, run_ctx: dict, artifacts: dict, adapter: FakeBatchAdapter  # noqa: F811 - pytest fixtures
) -> None:
    adapter.results_error = ConnectionError("results unavailable")
    run = _launch_batched(client, run_ctx)
    with TestingSessionLocal() as db:
        chunk_id = uuid.UUID(prepare_fact_development_run(db, uuid.UUID(run["id"]))[0])
    with TestingSessionLocal() as db:
        assert submit_chunk_batch(db, chunk_id)["submitted"] is True
    with TestingSessionLocal() as db:
        assert poll_chunk_batch(db, chunk_id) is True
    with TestingSessionLocal() as db:
        assert poll_chunk_batch(db, chunk_id) is False
    model = TestModel(custom_output_args=_extraction_output())
    # A failed read raises so the chunk step retries it; the third failure falls back to real time.
    for _ in range(2):
        with pytest.raises(ConnectionError):
            process_fact_development_chunk(chunk_id, session_factory=TestingSessionLocal, model=model)
    process_fact_development_chunk(chunk_id, session_factory=TestingSessionLocal, model=model)
    with TestingSessionLocal() as db:
        finalize_fact_development_run(db, uuid.UUID(run["id"]))
    counts = _counts(run["id"])
    assert counts["facts_created"] == 1
    assert counts["batched"] == 0


def test_documents_over_the_request_limit_are_extracted_in_real_time(
    client: TestClient, run_ctx: dict, artifacts: dict, adapter: FakeBatchAdapter, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    # Every request is over the limit, so the claim is released and nothing is sent to the provider.
    monkeypatch.setattr("app.fact_batches.PROVIDER_BATCH_REQUEST_OVERHEAD_BYTES", 0)
    monkeypatch.setattr("app.fact_batches.PROVIDER_BATCH_MAX_ESTIMATED_BYTES", 1)

    run = _launch_batched(client, run_ctx)
    with TestingSessionLocal() as db:
        chunk_ids = prepare_fact_development_run(db, uuid.UUID(run["id"]))
    with TestingSessionLocal() as db:
        # Every request is over a one-byte limit, so nothing is batched and the whole chunk runs in real time.
        assert submit_chunk_batch(db, uuid.UUID(chunk_ids[0]))["submitted"] is False
    assert adapter.submitted == []
    process_fact_development_chunk(
        uuid.UUID(chunk_ids[0]), session_factory=TestingSessionLocal, model=TestModel(custom_output_args=_extraction_output())
    )
    with TestingSessionLocal() as db:
        statuses = {row.status for row in db.scalars(select(SkillRun).where(SkillRun.scope_type == "MATTER_DOCUMENT"))}
        assert statuses == {"COMPLETED"}
        assert db.query(MatterFact).count() == 1


def test_missing_or_errored_batch_results_fall_back_per_document(
    client: TestClient, run_ctx: dict, artifacts: dict, adapter: FakeBatchAdapter, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    original = adapter.results

    def first_missing_second_errored(batch_id):
        state, values = original(batch_id)
        return state, [replace(values[1], output_text=None, error="RESOURCE_EXHAUSTED")]

    monkeypatch.setattr(adapter, "results", first_missing_second_errored)
    run = _launch_batched(client, run_ctx)
    _run_batched(run["id"], TestModel(custom_output_args=_extraction_output()))
    counts = _counts(run["id"])
    assert counts["batched"] == 0
    assert counts["batch_fallbacks"] == 2
    assert counts["facts_created"] == 1


def test_cancel_finds_and_stops_a_batch_whose_submission_was_interrupted(
    client: TestClient, run_ctx: dict, artifacts: dict, adapter: FakeBatchAdapter  # noqa: F811 - pytest fixtures
) -> None:
    run, chunk_id = _prepared_chunk(client, run_ctx)

    def crash():
        raise SystemExit("worker died after the provider accepted the batch")

    adapter.on_submit = crash
    with TestingSessionLocal() as db, pytest.raises(SystemExit):
        submit_chunk_batch(db, chunk_id)
    # The batch exists at the provider but was never recorded on the chunk; the user then cancels the run.
    canceled = client.post(f"/v1/jobs/FACT_DEVELOPMENT_ANALYSIS_TASK/{run['id']}/cancel", headers=auth(run_ctx["token"]))
    assert canceled.status_code == 200, canceled.text
    assert adapter.canceled == ["batches/fake-1"]
    with TestingSessionLocal() as db:
        chunk = db.get(MatterAnalysisTaskRunChunk, chunk_id)
        assert chunk.provider_batch_status == "CANCELED"
        assert chunk.provider_batch_id == "batches/fake-1"
        assert chunk.provider_manifest["provider_canceled"] is True
