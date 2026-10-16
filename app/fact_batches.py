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
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.assessment_provider_batches import (
    PROVIDER_BATCH_MAX_ESTIMATED_BYTES,
    PROVIDER_BATCH_REQUEST_OVERHEAD_BYTES,
    SUCCESS_PROVIDER_STATES,
    TERMINAL_PROVIDER_STATES,
)
from app.execution_accounting import ProviderUsageContext, persist_model_invocations, refresh_skill_run_usage
from app.fact_development import (
    BatchedExtractions,
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
)
from app.fact_extraction import extraction_output_validator
from app.job_executions import FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE
from app.job_outcomes import JobItemOutcome, find_outcome_fragment
from app.model_batching import BatchModelRequest, get_batch_adapter, resolve_batch_target
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


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


def submit_chunk_batch(db: Session, chunk_id: uuid.UUID) -> dict[str, Any]:
    """Submit the chunk's extraction calls as one provider batch, once.

    Returns `{"submitted": False}` when the run does not use the Batch API or nothing in the chunk needs a call; the
    chunk is then processed in real time. A submission that fails is recorded and also falls back to real time.
    """

    chunk = db.get(MatterAnalysisTaskRunChunk, chunk_id)
    if chunk is None:
        raise FactDevelopmentRunError("Fact-development chunk was not found")
    run = db.get(MatterAnalysisTaskRun, chunk.run_id)
    if run is None:
        raise FactDevelopmentRunError("Fact-development run was not found")
    configuration = run.configuration_snapshot
    poll_seconds = float(configuration.get("provider_batch_poll_seconds") or 60.0)
    if chunk.provider_batch_status is not None:
        return {"submitted": chunk.provider_batch_status in ACTIVE_BATCH_STATUSES, "poll_seconds": poll_seconds}
    if not configuration.get("use_batch_api") or _cancel_requested(db, run.id):
        return {"submitted": False, "poll_seconds": poll_seconds}
    rows = _chunk_rows(_manifest_reader(db, run), chunk)
    if find_outcome_fragment(_outcome_scope(db, run), _outcome_chunk(chunk, rows)) is not None:
        return {"submitted": False, "poll_seconds": poll_seconds}
    resolution = resolve_batch_target(configuration["resolved_model"])
    if resolution is None:
        logger.warning("Run %s asked for the Batch API but its model has no batch adapter", run.id)
        return {"submitted": False, "poll_seconds": poll_seconds}
    adapter, target = resolution
    context = _run_context(db, run, None)

    # Prepare every document first: recall may roll back the session, so no skill run is created until all are built.
    prepared_documents: list[PreparedDocument] = []
    for row in rows:
        prepared = prepare_document(db, context, chunk, row, Counter())
        if not isinstance(prepared, JobItemOutcome):
            prepared_documents.append(prepared)

    requests: list[BatchModelRequest] = []
    manifest: list[dict[str, Any]] = []
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
        manifest.append(
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
        db.rollback()
        return {"submitted": False, "poll_seconds": poll_seconds}
    db.flush()
    try:
        submission = adapter.submit(
            model=target.model,
            requests=requests,
            display_name=f"fact-development-{run.id}-chunk-{chunk.chunk_number}",
        )
    except Exception as exc:  # noqa: BLE001 - a failed submission falls back to real-time extraction
        logger.warning("Provider batch submission failed run_id=%s chunk=%s: %s", run.id, chunk.chunk_number, exc)
        db.rollback()
        chunk = db.get(MatterAnalysisTaskRunChunk, chunk_id)
        chunk.provider_batch_status = "FAILED"
        chunk.provider = target.provider
        chunk.provider_model = target.model
        chunk.provider_error = f"Submission failed: {exc}"[:4000]
        chunk.provider_completed_at = utcnow()
        db.commit()
        return {"submitted": False, "poll_seconds": poll_seconds}
    chunk.provider_batch_status = "SUBMITTED"
    chunk.provider = target.provider
    chunk.provider_model = target.model
    chunk.provider_batch_id = submission.batch_id
    chunk.provider_state = submission.status
    chunk.provider_manifest = {"requests": manifest}
    chunk.provider_submitted_at = utcnow()
    _refresh_progress(db, run, stage="WAITING_FOR_PROVIDER_BATCH")
    db.commit()
    return {"submitted": True, "poll_seconds": poll_seconds}


def _fail_batch_skill_runs(db: Session, chunk: MatterAnalysisTaskRunChunk, message: str) -> None:
    for item in (chunk.provider_manifest or {}).get("requests", []):
        skill_run = db.get(SkillRun, uuid.UUID(item["skill_run_id"]))
        if skill_run is not None and skill_run.status == "RUNNING":
            fail_skill_run(skill_run, RuntimeError(message))
            refresh_skill_run_usage(db, skill_run)


def _cancel_provider_batch(db: Session, chunk: MatterAnalysisTaskRunChunk) -> None:
    if chunk.provider_batch_id and chunk.provider:
        try:
            get_batch_adapter(chunk.provider).cancel(chunk.provider_batch_id)
        except Exception:
            logger.warning("Could not cancel provider batch %s", chunk.provider_batch_id, exc_info=True)
    chunk.provider_batch_status = "CANCELED"
    chunk.provider_completed_at = utcnow()
    _fail_batch_skill_runs(db, chunk, "The run was canceled while its provider batch was pending")


def poll_chunk_batch(db: Session, chunk_id: uuid.UUID) -> bool:
    """Refresh the chunk's provider batch; True while it is still pending."""

    chunk = db.get(MatterAnalysisTaskRunChunk, chunk_id)
    if chunk is None:
        raise FactDevelopmentRunError("Fact-development chunk was not found")
    if chunk.provider_batch_status not in ACTIVE_BATCH_STATUSES:
        return False
    if _cancel_requested(db, chunk.run_id):
        _cancel_provider_batch(db, chunk)
        db.commit()
        return False
    result = get_batch_adapter(chunk.provider).poll(chunk.provider_batch_id)
    chunk.provider_state = result.status
    if result.status not in TERMINAL_PROVIDER_STATES:
        chunk.provider_batch_status = "RUNNING" if result.status not in {"JOB_STATE_QUEUED", "JOB_STATE_PENDING"} else "SUBMITTED"
        db.commit()
        return True
    chunk.provider_completed_at = utcnow()
    if result.status in SUCCESS_PROVIDER_STATES:
        chunk.provider_batch_status = "SUCCEEDED"
    else:
        # The whole chunk is extracted in real time instead.
        chunk.provider_batch_status = "FAILED"
        chunk.provider_error = str(result.error or f"Provider batch ended in {result.status}")[:4000]
        _fail_batch_skill_runs(db, chunk, chunk.provider_error)
    db.commit()
    return False


def cancel_run_batches(db: Session, run_id: uuid.UUID) -> None:
    """Cancel every pending provider batch of a run; the caller commits."""

    chunks = db.query(MatterAnalysisTaskRunChunk).filter(
        MatterAnalysisTaskRunChunk.run_id == run_id,
        MatterAnalysisTaskRunChunk.provider_batch_status.in_(ACTIVE_BATCH_STATUSES),
    )
    for chunk in chunks:
        _cancel_provider_batch(db, chunk)


class ChunkBatchResults(BatchedExtractions):
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
    """The finished provider batch's results for the chunk, or None when the chunk runs in real time."""

    if chunk.provider_batch_status != "SUCCEEDED" or not chunk.provider_batch_id or not chunk.provider:
        return None
    state, results = get_batch_adapter(chunk.provider).results(chunk.provider_batch_id)
    if state not in SUCCESS_PROVIDER_STATES:
        # Retried by the chunk step; the batch was reported finished, so this is a transient provider read.
        raise RuntimeError(f"The provider batch results are not ready: {state}")
    return ChunkBatchResults(chunk, {item.custom_id: item for item in results if item.custom_id})
