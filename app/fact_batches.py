"""Provider Batch API execution for fact-development chunks.

When a run is launched with the Batch API, each chunk submits its documents' extraction calls as one provider batch
when the chunk's turn comes in the ordered window, waits for it durably, and then applies the results in manifest
order through the same validation, consolidation and write path as real-time extraction.

Recall happens when a request is built, so documents in one chunk see facts from earlier chunks but not from each
other; consolidation still catches duplicates when the results are applied. Documents that are not batched (no text,
reused, too large for the request limit) and batch results that are missing or invalid fall back to a real-time call.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.assessment_provider_batches import (
    PROVIDER_BATCH_MAX_ESTIMATED_BYTES,
    PROVIDER_BATCH_REQUEST_OVERHEAD_BYTES,
    SUCCESS_PROVIDER_STATES,
    TERMINAL_PROVIDER_STATES,
)
from app.config import get_settings
from app.execution_accounting import ProviderUsageContext, persist_model_invocations, refresh_skill_run_usage
from app.fact_development import (
    FactDevelopmentRunError,
    PreparedDocument,
    _cancel_requested,
    _chunk_rows,
    _manifest_reader,
    _outcome_chunk,
    _outcome_scope,
    _refresh_progress,
    _run_context,
    _RunContext,
    prepare_document,
    utcnow,
)
from app.fact_extraction import extraction_output_validator
from app.job_executions import FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE
from app.job_outcomes import JobItemOutcome, find_outcome_fragment
from app.model_batching import BatchModelRequest, BatchSubmission, get_batch_adapter, resolve_batch_target
from app.model_execution import (
    InstructionLayer,
    InvocationTelemetry,
    StructuredModelRequest,
    assemble_structured_prompt,
    validate_structured_output,
)
from app.models import MatterAnalysisTaskRun, MatterAnalysisTaskRunChunk, SkillRun
from app.skill_execution import PLATFORM_SKILL_INSTRUCTIONS, complete_skill_run, create_skill_run, fail_skill_run

logger = logging.getLogger(__name__)
ACTIVE_BATCH_STATUSES = {"SUBMITTED", "RUNNING"}
PENDING_PROVIDER_STATES = {"JOB_STATE_QUEUED", "JOB_STATE_PENDING"}
MAX_POLL_FAILURES = 10
MAX_RESULT_FAILURES = 3
# Providers expire batches after 24 hours; past this the chunk stops waiting and runs in real time.
BATCH_DEADLINE = timedelta(hours=26)


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _structured_request(context: _RunContext, prepared: PreparedDocument, skill_run: SkillRun) -> StructuredModelRequest:
    version = context.skill_version
    return StructuredModelRequest(
        instruction_layers=(
            InstructionLayer("platform_security", PLATFORM_SKILL_INSTRUCTIONS),
            InstructionLayer("managed_skill", version.instructions),
        ),
        stable_context=prepared.stable_context,
        dynamic_input=prepared.dynamic_input,
        output_schema=version.output_schema,
        model_key=version.model_key,
        model_settings=version.model_policy,
        limits=version.limits,
        cache_policy=version.cache_policy,
        cache_identity=prepared.cache_identity,
        request_type=context.step.role_key.replace("_", "-"),
        run_id=str(skill_run.id),
    )


def _locked_chunk(db: Session, chunk_id: uuid.UUID) -> MatterAnalysisTaskRunChunk:
    """The chunk row, locked after its run row.

    Run then chunk is the order the cancel endpoint takes (run, execution, chunks), so submission, polling and
    cancellation serialize on the run row and can never deadlock. Callers make no provider calls while holding it.
    """

    run_id = db.scalar(select(MatterAnalysisTaskRunChunk.run_id).where(MatterAnalysisTaskRunChunk.id == chunk_id))
    if run_id is None:
        raise FactDevelopmentRunError("Fact-development chunk was not found")
    db.scalar(select(MatterAnalysisTaskRun.id).where(MatterAnalysisTaskRun.id == run_id).with_for_update())
    chunk = db.scalar(
        select(MatterAnalysisTaskRunChunk)
        .where(MatterAnalysisTaskRunChunk.id == chunk_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if chunk is None:
        raise FactDevelopmentRunError("Fact-development chunk was not found")
    return chunk


def _update_manifest(chunk: MatterAnalysisTaskRunChunk, **values: Any) -> None:
    chunk.provider_manifest = {**(chunk.provider_manifest or {}), **values}


def _fail_batch(db: Session, chunk: MatterAnalysisTaskRunChunk, message: str) -> None:
    """Give up on the chunk's provider batch; the chunk is then extracted in real time."""

    chunk.provider_batch_status = "FAILED"
    chunk.provider_error = "; ".join(part for part in (message, chunk.provider_error) if part)[:4000]
    chunk.provider_completed_at = utcnow()
    _fail_batch_skill_runs(db, chunk, message)


def _fail_batch_skill_runs(db: Session, chunk: MatterAnalysisTaskRunChunk, message: str) -> None:
    for item in (chunk.provider_manifest or {}).get("requests", []):
        skill_run = db.get(SkillRun, uuid.UUID(item["skill_run_id"]))
        if skill_run is not None and skill_run.status == "RUNNING":
            fail_skill_run(skill_run, RuntimeError(message))
            refresh_skill_run_usage(db, skill_run)


def submit_chunk_batch(db: Session, chunk_id: uuid.UUID) -> dict[str, Any]:
    """Submit the chunk's extraction calls as one provider batch, at most once.

    The chunk is claimed (SUBMITTED, phase "preparing", a unique display name) before its requests are built, and
    moves to phase "sending", with its request manifest and skill runs, before the provider is called. A retried step
    therefore never submits twice: in phase "sending" it adopts the batch the provider accepted (found by display
    name) or, when the provider has none, lets the chunk run in real time. Returns `{"submitted": False}` whenever the
    chunk is processed in real time.
    """

    chunk = db.get(MatterAnalysisTaskRunChunk, chunk_id)
    if chunk is None:
        raise FactDevelopmentRunError("Fact-development chunk was not found")
    run = db.get(MatterAnalysisTaskRun, chunk.run_id)
    if run is None:
        raise FactDevelopmentRunError("Fact-development run was not found")
    configuration = run.configuration_snapshot
    poll_seconds = float(
        configuration.get("provider_batch_poll_seconds") or get_settings().fact_development_provider_batch_poll_seconds
    )

    def result(submitted: bool) -> dict[str, Any]:
        return {"submitted": submitted, "poll_seconds": poll_seconds}

    if chunk.provider_batch_status is None and not configuration.get("use_batch_api"):
        return result(False)
    resolution = resolve_batch_target(configuration["resolved_model"])
    if resolution is None:
        logger.warning("Run %s asked for the Batch API but its model has no batch adapter", run.id)
        return result(False)
    adapter, target = resolution

    chunk = _locked_chunk(db, chunk_id)
    manifest = chunk.provider_manifest or {}
    status = chunk.provider_batch_status
    if status is not None and not (status == "SUBMITTED" and manifest.get("phase") == "preparing"):
        db.commit()
        if status == "SUBMITTED" and not chunk.provider_batch_id:
            return result(_resume_submission(db, chunk_id))
        if status in {"CANCELED", "FAILED"} and manifest.get("phase") == "sending" and not chunk.provider_batch_id:
            _cancel_orphaned_batch(db, chunk_id)
        return result(status in ACTIVE_BATCH_STATUSES)
    if _cancel_requested(db, run.id):
        db.commit()
        return result(False)
    # Claim the chunk. A claim left in phase "preparing" never reached the provider, so it is simply taken over.
    display_name = f"fact-development-chunk-{chunk.id}-{uuid.uuid4().hex[:12]}"
    chunk.provider_batch_status = "SUBMITTED"
    chunk.provider = target.provider
    chunk.provider_model = target.model
    chunk.provider_batch_id = None
    chunk.provider_manifest = {"display_name": display_name, "phase": "preparing"}
    db.commit()

    rows = _chunk_rows(_manifest_reader(db, run), chunk)
    if find_outcome_fragment(_outcome_scope(db, run), _outcome_chunk(chunk, rows)) is not None:
        _release_claim(db, chunk_id, display_name)
        return result(False)
    context = _run_context(db, run, None)
    # Prepare every document first: recall may roll back the session, so no skill run is created until all are built.
    prepared_documents: list[PreparedDocument] = []
    for row in rows:
        prepared = prepare_document(db, context, chunk, row, Counter())
        if not isinstance(prepared, JobItemOutcome):
            prepared_documents.append(prepared)
    db.rollback()  # Nothing from preparation is kept; fingerprints are recorded when results are applied.

    # Lock (run, then chunk) before creating skill runs: their foreign key locks the execution row, and the cancel
    # endpoint locks run then execution, so taking the run row first keeps one lock order. The loop below is local
    # work only; no provider call is made while the lock is held.
    chunk = _locked_chunk(db, chunk_id)
    manifest = chunk.provider_manifest or {}
    if chunk.provider_batch_status != "SUBMITTED" or manifest.get("display_name") != display_name:
        db.rollback()
        return result(False)

    requests: list[BatchModelRequest] = []
    request_manifest: list[dict[str, Any]] = []
    estimated_bytes = 0
    for prepared in prepared_documents:
        skill_run = create_skill_run(
            db,
            workflow=context.workflow,
            step=context.step,
            skill_version=context.skill_version,
            scope_type="MATTER_DOCUMENT",
            scope_id=uuid.UUID(prepared.document_id),
            request_input={"stable_context": prepared.stable_context, "dynamic_input": prepared.dynamic_input},
        )
        assembly = assemble_structured_prompt(
            _structured_request(context, prepared, skill_run), resolved_model=configuration["resolved_model"]
        )
        prompt = f"{assembly.stable_prefix}\n{assembly.dynamic_payload}"
        size = len(prompt.encode("utf-8")) + PROVIDER_BATCH_REQUEST_OVERHEAD_BYTES
        if estimated_bytes + size > PROVIDER_BATCH_MAX_ESTIMATED_BYTES:
            # Over the provider's request limit: this document is extracted in real time instead.
            db.delete(skill_run)
            continue
        estimated_bytes += size
        skill_run.cache_fingerprint = assembly.cache_fingerprint
        requests.append(
            BatchModelRequest(
                custom_id=str(skill_run.id),
                prompt=prompt,
                instructions=assembly.instructions,
                output_schema=context.skill_version.output_schema,
                model_settings=context.skill_version.model_policy,
                limits=context.skill_version.limits,
            )
        )
        request_manifest.append(
            {
                "custom_id": str(skill_run.id),
                "document_id": prepared.document_id,
                "skill_run_id": str(skill_run.id),
                "model_configuration_hash": assembly.model_configuration_hash,
                "recalled_fact_ids": [item["fact_id"] for item in prepared.recalled],
                "recall_unavailable": prepared.recall_unavailable,
            }
        )
    if not requests:
        # Nothing to batch: release the claim (still locked above) and let the chunk run in real time.
        chunk.provider_batch_status = None
        chunk.provider_manifest = None
        chunk.provider = None
        chunk.provider_model = None
        db.commit()
        return result(False)
    _update_manifest(chunk, phase="sending", requests=request_manifest)
    db.commit()

    try:
        submission = adapter.submit(model=target.model, requests=requests, display_name=display_name)
    except Exception as exc:  # noqa: BLE001 - any submission error is settled by looking the batch up
        # The provider may have accepted the batch before the error; only its absence makes a fallback safe.
        logger.warning("Provider batch submission failed run_id=%s chunk=%s: %s", run.id, chunk.chunk_number, exc)
        found = adapter.find(display_name)  # A lookup error propagates, and the retried step looks again.
        if found is not None:
            return result(_record_submission(db, chunk_id, found))
        chunk = _locked_chunk(db, chunk_id)
        if chunk.provider_batch_status == "SUBMITTED" and not chunk.provider_batch_id:
            _fail_batch(db, chunk, f"Submission failed: {exc}")
        db.commit()
        return result(False)
    return result(_record_submission(db, chunk_id, submission))


def _release_claim(db: Session, chunk_id: uuid.UUID, display_name: str) -> None:
    """Return a claimed chunk that needs no provider batch to real-time processing."""

    chunk = _locked_chunk(db, chunk_id)
    if (chunk.provider_manifest or {}).get("display_name") == display_name and chunk.provider_batch_status == "SUBMITTED":
        chunk.provider_batch_status = None
        chunk.provider_manifest = None
        chunk.provider = None
        chunk.provider_model = None
    db.commit()


def _record_submission(db: Session, chunk_id: uuid.UUID, submission: BatchSubmission) -> bool:
    """Attach the provider batch to the chunk; a cancel that arrived meanwhile cancels the batch at once."""

    chunk = _locked_chunk(db, chunk_id)
    chunk.provider_batch_id = submission.batch_id
    chunk.provider_state = submission.status
    chunk.provider_submitted_at = utcnow()
    canceled = chunk.provider_batch_status != "SUBMITTED"
    run_id = chunk.run_id
    db.commit()
    if canceled:
        _send_provider_cancel(db, chunk_id)
        return False
    run = db.get(MatterAnalysisTaskRun, run_id)
    _refresh_progress(db, run, stage="WAITING_FOR_PROVIDER_BATCH")
    db.commit()
    return True


def _resume_submission(db: Session, chunk_id: uuid.UUID) -> bool:
    """Finish a submission interrupted in phase "sending": adopt the provider's batch or fall back."""

    chunk = db.get(MatterAnalysisTaskRunChunk, chunk_id)
    display_name = (chunk.provider_manifest or {}).get("display_name")
    found = get_batch_adapter(chunk.provider).find(display_name) if display_name and chunk.provider else None
    if found is not None:
        return _record_submission(db, chunk_id, found)
    chunk = _locked_chunk(db, chunk_id)
    if chunk.provider_batch_status == "SUBMITTED" and not chunk.provider_batch_id:
        _fail_batch(db, chunk, "The submission was interrupted and the provider has no matching batch")
    db.commit()
    return False


def _cancel_orphaned_batch(db: Session, chunk_id: uuid.UUID) -> None:
    """Cancel a batch the provider accepted for a chunk that was canceled or failed before the batch was recorded."""

    chunk = db.get(MatterAnalysisTaskRunChunk, chunk_id)
    manifest = chunk.provider_manifest or {}
    if manifest.get("orphan_checked") or not chunk.provider:
        return
    found = get_batch_adapter(chunk.provider).find(manifest["display_name"])
    chunk = _locked_chunk(db, chunk_id)
    if found is not None and not chunk.provider_batch_id:
        chunk.provider_batch_id = found.batch_id
        chunk.provider_state = found.status
    _update_manifest(chunk, orphan_checked=True)
    db.commit()
    if found is not None:
        _send_provider_cancel(db, chunk_id)


def _send_provider_cancel(db: Session, chunk_id: uuid.UUID) -> None:
    """Ask the provider to stop the chunk's batch, outside any lock; a failure is recorded so the live batch shows."""

    chunk = db.get(MatterAnalysisTaskRunChunk, chunk_id)
    db.commit()
    if not (chunk.provider_batch_id and chunk.provider) or (chunk.provider_manifest or {}).get("provider_canceled"):
        return
    error: str | None = None
    try:
        get_batch_adapter(chunk.provider).cancel(chunk.provider_batch_id)
    except Exception as exc:  # noqa: BLE001 - the chunk is settled either way; the failure is recorded on it
        logger.warning("Could not cancel provider batch %s: %s", chunk.provider_batch_id, exc)
        error = f"Provider cancel failed; the batch may still be running: {exc}"
    chunk = _locked_chunk(db, chunk_id)
    if error is None:
        _update_manifest(chunk, provider_canceled=True)
    else:
        chunk.provider_error = "; ".join(part for part in (error, chunk.provider_error) if part)[:4000]
    db.commit()


def poll_chunk_batch(db: Session, chunk_id: uuid.UUID) -> bool:
    """Refresh the chunk's provider batch; True while it is still pending.

    The provider is called without any lock held. Provider errors are retried on later polls; after MAX_POLL_FAILURES
    in a row, or once a pending batch outlives BATCH_DEADLINE, the batch is canceled and the chunk is extracted in
    real time.
    """

    chunk = db.get(MatterAnalysisTaskRunChunk, chunk_id)
    if chunk is None:
        raise FactDevelopmentRunError("Fact-development chunk was not found")
    if chunk.provider_batch_status not in ACTIVE_BATCH_STATUSES or not chunk.provider_batch_id:
        return False
    if _cancel_requested(db, chunk.run_id):
        chunk = _locked_chunk(db, chunk_id)
        if chunk.provider_batch_status in ACTIVE_BATCH_STATUSES:
            _mark_canceled(db, chunk)
        db.commit()
        _send_provider_cancel(db, chunk_id)
        return False
    provider, batch_id, submitted_at = chunk.provider, chunk.provider_batch_id, chunk.provider_submitted_at
    db.commit()
    error: Exception | None = None
    try:
        outcome = get_batch_adapter(provider).poll(batch_id)
    except Exception as exc:  # noqa: BLE001 - provider errors are retried, then fall back to real time
        error = exc

    chunk = _locked_chunk(db, chunk_id)
    if chunk.provider_batch_status not in ACTIVE_BATCH_STATUSES or chunk.provider_batch_id != batch_id:
        db.commit()  # Canceled while the provider was being polled.
        return False
    give_up: str | None = None
    if error is not None:
        failures = int((chunk.provider_manifest or {}).get("poll_failures", 0)) + 1
        _update_manifest(chunk, poll_failures=failures)
        chunk.provider_error = f"Polling failed: {error}"[:4000]
        if failures >= MAX_POLL_FAILURES:
            give_up = f"Polling failed {failures} times: {error}"
    else:
        _update_manifest(chunk, poll_failures=0)
        chunk.provider_state = outcome.status
        if outcome.status in SUCCESS_PROVIDER_STATES:
            chunk.provider_batch_status = "SUCCEEDED"
            chunk.provider_error = None
            chunk.provider_completed_at = utcnow()
        elif outcome.status in TERMINAL_PROVIDER_STATES:
            _fail_batch(db, chunk, str(outcome.error or f"Provider batch ended in {outcome.status}"))
        elif submitted_at and utcnow() - as_utc(submitted_at) > BATCH_DEADLINE:
            give_up = "The provider batch did not finish in time"
        else:
            chunk.provider_batch_status = "SUBMITTED" if outcome.status in PENDING_PROVIDER_STATES else "RUNNING"
    if give_up is not None:
        _fail_batch(db, chunk, give_up)
    pending = chunk.provider_batch_status in ACTIVE_BATCH_STATUSES
    db.commit()
    if give_up is not None:
        _send_provider_cancel(db, chunk_id)
    return pending


def _mark_canceled(db: Session, chunk: MatterAnalysisTaskRunChunk) -> None:
    chunk.provider_batch_status = "CANCELED"
    chunk.provider_completed_at = utcnow()
    _fail_batch_skill_runs(db, chunk, "The run was canceled while its provider batch was pending")


def cancel_run_batches(db: Session, run_id: uuid.UUID) -> None:
    """Mark a run's provider batches canceled within the cancel transaction (the caller holds the run lock and commits).

    No provider call is made here; `cancel_provider_batches` sends the cancels after the commit. A submission still in
    flight sees CANCELED when it returns and cancels its own batch.
    """

    chunks = db.scalars(
        select(MatterAnalysisTaskRunChunk)
        .where(
            MatterAnalysisTaskRunChunk.run_id == run_id,
            MatterAnalysisTaskRunChunk.provider_batch_status.in_(ACTIVE_BATCH_STATUSES | {"SUCCEEDED"}),
        )
        # A chunk whose results are being applied is locked by its processing step, which checks for cancellation
        # before every document and closes the skill runs it does not apply; skipping it avoids a lock-order cycle.
        .with_for_update(skip_locked=True)
    )
    for chunk in chunks:
        if chunk.provider_batch_status == "SUCCEEDED":
            # Results not applied yet will not be: cancellation is final.
            _fail_batch_skill_runs(db, chunk, "The run was canceled before the batch result was applied")
        else:
            _mark_canceled(db, chunk)


def cancel_provider_batches(db: Session, run_id: uuid.UUID) -> None:
    """After a run's cancellation is committed, ask the provider to stop its batches, including orphaned ones."""

    chunk_ids = list(
        db.scalars(
            select(MatterAnalysisTaskRunChunk.id).where(
                MatterAnalysisTaskRunChunk.run_id == run_id,
                MatterAnalysisTaskRunChunk.provider_batch_status == "CANCELED",
            )
        )
    )
    db.commit()
    for chunk_id in chunk_ids:
        chunk = db.get(MatterAnalysisTaskRunChunk, chunk_id)
        if chunk.provider_batch_id:
            _send_provider_cancel(db, chunk_id)
        elif (chunk.provider_manifest or {}).get("phase") == "sending":
            try:
                _cancel_orphaned_batch(db, chunk_id)
            except Exception as exc:  # noqa: BLE001 - a later attempt of the chunk step looks again
                logger.warning("Could not look up the provider batch of canceled chunk %s: %s", chunk_id, exc)


class ChunkBatchResults:
    """The results of a chunk's finished provider batch, applied one document at a time."""

    def __init__(self, chunk: MatterAnalysisTaskRunChunk, results: dict[str, Any]) -> None:
        self.chunk = chunk
        self.entries = {item["document_id"]: item for item in (chunk.provider_manifest or {}).get("requests", [])}
        self.results = results

    def has(self, document_id: str) -> bool:
        return document_id in self.entries

    def recalled(self, document_id: str) -> tuple[list[dict[str, Any]], bool]:
        entry = self.entries[document_id]
        return [{"fact_id": value} for value in entry["recalled_fact_ids"]], bool(entry["recall_unavailable"])

    def abandon(self, db: Session, document_id: str, reason: str) -> None:
        """Close the skill run of a batch result that will not be applied, so it is not left RUNNING."""

        skill_run = db.get(SkillRun, uuid.UUID(self.entries[document_id]["skill_run_id"]))
        if skill_run is not None and skill_run.status == "RUNNING":
            fail_skill_run(skill_run, RuntimeError(reason))
            refresh_skill_run_usage(db, skill_run)

    def output(self, db: Session, context: _RunContext, prepared: PreparedDocument) -> tuple[dict[str, Any], SkillRun]:
        entry = self.entries[prepared.document_id]
        skill_run = db.get(SkillRun, uuid.UUID(entry["skill_run_id"]))
        if skill_run is None:
            raise ValueError("The batch request's skill run is missing")
        result = self.results.get(entry["custom_id"])
        try:
            if result is None:
                raise ValueError("The provider batch returned no result for the document")
            if result.error:
                raise ValueError(f"The provider batch reported an error: {result.error}")
            if not result.output_text:
                raise ValueError("The provider batch returned an empty result")
            if skill_run.status == "RUNNING":
                now = utcnow()
                persist_model_invocations(
                    db,
                    (
                        InvocationTelemetry(
                            request_sequence=1,
                            provider_request_id=result.provider_request_id,
                            provider=self.chunk.provider,
                            model=self.chunk.provider_model,
                            model_configuration_hash=entry["model_configuration_hash"],
                            request_count=1,
                            input_tokens=result.input_tokens,
                            cached_input_tokens=result.cached_input_tokens,
                            cache_write_tokens=0,
                            output_tokens=result.output_tokens,
                            latency_ms=0,
                            started_at=self.chunk.provider_submitted_at or now,
                            completed_at=now,
                        ),
                    ),
                    skill_run_id=skill_run.id,
                    usage_context=ProviderUsageContext(
                        tenant_id=context.workflow.tenant_id,
                        client_id=context.workflow.client_id,
                        matter_id=context.workflow.matter_id,
                        started_by_user_id=context.workflow.initiated_by_user_id,
                        job_type=FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE,
                        job_id=context.workflow.id,
                        job_created_at=context.workflow.created_at,
                        details={
                            "workflow_key": context.workflow.workflow_key,
                            "role_key": context.step.role_key,
                            "provider_batch_id": self.chunk.provider_batch_id,
                        },
                    ),
                )
            output = json.loads(result.output_text)
            if not isinstance(output, dict):
                raise TypeError("The provider batch result is not a JSON object")
            validate_structured_output(output, context.skill_version.output_schema)
            extraction_output_validator(max_operations=prepared.max_operations)(output)
        except Exception as exc:
            if skill_run.status == "RUNNING":
                fail_skill_run(skill_run, exc)
                refresh_skill_run_usage(db, skill_run)
            raise
        if skill_run.status == "RUNNING":
            complete_skill_run(db, skill_run)
        return output, skill_run


def load_batched_extractions(db: Session, chunk: MatterAnalysisTaskRunChunk) -> ChunkBatchResults | None:
    """The finished provider batch's results for the chunk, or None when the chunk runs in real time.

    A failed read raises so the chunk step retries it; after MAX_RESULT_FAILURES the chunk falls back to real time.
    """

    if chunk.provider_batch_status != "SUCCEEDED" or not chunk.provider_batch_id or not chunk.provider:
        return None
    chunk_id = chunk.id
    try:
        state, results = get_batch_adapter(chunk.provider).results(chunk.provider_batch_id)
        if state not in SUCCESS_PROVIDER_STATES:
            raise RuntimeError(f"The provider batch results are not ready: {state}")
    except Exception as exc:
        locked = _locked_chunk(db, chunk_id)
        failures = int((locked.provider_manifest or {}).get("result_failures", 0)) + 1
        _update_manifest(locked, result_failures=failures)
        if failures < MAX_RESULT_FAILURES:
            locked.provider_error = f"Reading the results failed: {exc}"[:4000]
            db.commit()
            raise
        logger.warning("Provider batch results unavailable for chunk %s: %s", chunk_id, exc)
        _fail_batch(db, locked, f"Reading the results failed {failures} times: {exc}")
        db.commit()
        return None
    return ChunkBatchResults(db.get(MatterAnalysisTaskRunChunk, chunk_id), {item.custom_id: item for item in results if item.custom_id})
