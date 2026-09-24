import logging
import uuid

from dbos import DBOS, Queue, SetWorkflowID
from sqlalchemy import select

from app.assessment_execution import (
    analyze_document,
    complete_assessment,
    fail_assessment,
    fail_document,
    plan_retrieval,
    refresh_progress,
    retrieve_and_materialize,
    synthesize_assessment,
)
from app.assessment_guidance_refinement import create_guidance_revision, fail_guidance_refinement
from app.assessment_provider_batches import (
    TERMINAL_PROVIDER_STATES,
    fail_provider_batch,
    finalize_provider_batch,
    plan_provider_batches,
    poll_provider_batch,
    submit_provider_batch,
)
from app.config import get_settings
from app.database import SessionLocal
from app.models import MatterDefinitionAssessmentRun, ReviewBatchRun, ReviewBatchRunDocument
from app.search.service import sync_review_batch_search

logger = logging.getLogger(__name__)

ASSESSMENT_QUEUE = Queue("definition-assessments", global_concurrency=2)
DOCUMENT_QUEUE = Queue(
    "definition-assessment-documents",
    global_concurrency=get_settings().definition_assessment_document_concurrency,
)
PROVIDER_BATCH_QUEUE = Queue("definition-assessment-provider-batches", global_concurrency=2)


@DBOS.step(name="plan_definition_assessment_retrieval", retries_allowed=True, max_attempts=3)
def plan(assessment_id: str) -> dict:
    with SessionLocal() as db:
        try:
            return plan_retrieval(db, uuid.UUID(assessment_id))
        except Exception:
            db.commit()
            raise


@DBOS.step(name="retrieve_definition_assessment_documents", retries_allowed=True, max_attempts=3)
def retrieve(assessment_id: str) -> list[str]:
    with SessionLocal() as db:
        return retrieve_and_materialize(db, uuid.UUID(assessment_id), get_settings())


@DBOS.step(name="sync_definition_assessment_batch_search", retries_allowed=True, max_attempts=5)
def sync_batch_search(assessment_id: str) -> None:
    with SessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, uuid.UUID(assessment_id))
        if assessment is None or assessment.review_batch_id is None:
            raise ValueError("Assessment batch has not been created")
        batch_id = assessment.review_batch_id
    sync_review_batch_search(batch_id, get_settings())


@DBOS.step(name="analyze_definition_assessment_document", retries_allowed=True, max_attempts=3)
def analyze(assessment_id: str, document_id: str) -> dict:
    with SessionLocal() as db:
        try:
            return analyze_document(
                db,
                uuid.UUID(assessment_id),
                uuid.UUID(document_id),
                settings=get_settings(),
            )
        except Exception:
            db.commit()
            raise


@DBOS.step(name="fail_definition_assessment_document")
def mark_document_failed(assessment_id: str, document_id: str, message: str) -> None:
    with SessionLocal() as db:
        fail_document(db, uuid.UUID(assessment_id), uuid.UUID(document_id), message)


@DBOS.step(name="plan_definition_assessment_provider_batches", retries_allowed=True, max_attempts=3)
def plan_batches(assessment_id: str, document_ids: list[str]) -> dict:
    with SessionLocal() as db:
        return plan_provider_batches(
            db,
            uuid.UUID(assessment_id),
            [uuid.UUID(value) for value in document_ids],
            get_settings(),
        )


@DBOS.step(name="submit_definition_assessment_provider_batch", retries_allowed=True, max_attempts=3)
def submit_batch(batch_id: str) -> str:
    with SessionLocal() as db:
        return submit_provider_batch(db, uuid.UUID(batch_id), get_settings())


@DBOS.step(name="poll_definition_assessment_provider_batch", retries_allowed=True, max_attempts=10)
def poll_batch(batch_id: str) -> str:
    with SessionLocal() as db:
        return poll_provider_batch(db, uuid.UUID(batch_id))


@DBOS.step(name="finalize_definition_assessment_provider_batch", retries_allowed=True, max_attempts=3)
def finalize_batch(batch_id: str) -> dict[str, int]:
    with SessionLocal() as db:
        return finalize_provider_batch(db, uuid.UUID(batch_id))


@DBOS.step(name="fail_definition_assessment_provider_batch")
def mark_batch_failed(batch_id: str, message: str) -> None:
    with SessionLocal() as db:
        fail_provider_batch(db, uuid.UUID(batch_id), message)


@DBOS.step(name="create_definition_assessment_guidance_revision", retries_allowed=True, max_attempts=3)
def create_refined_guidance(assessment_id: str) -> str:
    with SessionLocal() as db:
        return str(create_guidance_revision(db, uuid.UUID(assessment_id)))


@DBOS.step(name="fail_definition_assessment_guidance_refinement")
def mark_guidance_refinement_failed(assessment_id: str, message: str) -> None:
    with SessionLocal() as db:
        fail_guidance_refinement(db, uuid.UUID(assessment_id), message)


@DBOS.workflow(name="matter_definition_assessment_guidance_refinement_v1")
def guidance_refinement_workflow(assessment_id: str) -> str | None:
    try:
        return create_refined_guidance(assessment_id)
    except Exception as exc:
        logger.exception("Matter Definition guidance refinement failed assessment_id=%s", assessment_id)
        mark_guidance_refinement_failed(assessment_id, str(exc))
        return None


@DBOS.workflow(name="matter_definition_assessment_provider_batch_v1")
def provider_batch_workflow(batch_id: str, poll_seconds: float) -> dict[str, int]:
    try:
        status = submit_batch(batch_id)
        while status not in TERMINAL_PROVIDER_STATES:
            DBOS.sleep(poll_seconds)
            status = poll_batch(batch_id)
        if status not in {"JOB_STATE_SUCCEEDED", "JOB_STATE_PARTIALLY_SUCCEEDED"}:
            raise RuntimeError(f"Assessment provider batch ended in {status}")
        return finalize_batch(batch_id)
    except Exception as exc:
        logger.exception("Matter Definition provider batch failed batch_id=%s", batch_id)
        mark_batch_failed(batch_id, str(exc))
        return {"completed": 0, "failed": 0}


@DBOS.workflow(name="matter_definition_assessment_document_v1")
def document_workflow(assessment_id: str, document_id: str) -> dict:
    try:
        return analyze(assessment_id, document_id)
    except Exception as exc:
        logger.exception(
            "Matter Definition assessment document failed assessment_id=%s document_id=%s",
            assessment_id,
            document_id,
        )
        mark_document_failed(assessment_id, document_id, str(exc))
        return {"status": "FAILED", "error": str(exc)[:4000]}


@DBOS.step(name="refresh_definition_assessment_progress")
def refresh(assessment_id: str) -> dict[str, int]:
    with SessionLocal() as db:
        return refresh_progress(db, uuid.UUID(assessment_id))


@DBOS.step(name="prepare_definition_assessment_reanalysis")
def prepare_reanalysis(assessment_id: str) -> list[str]:
    with SessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, uuid.UUID(assessment_id))
        if assessment is None or assessment.review_batch_run_id is None:
            raise ValueError("Assessment reanalysis batch run is unavailable")
        review_run = db.get(ReviewBatchRun, assessment.review_batch_run_id)
        if review_run is None:
            raise ValueError("Assessment reanalysis run is unavailable")
        document_ids = list(
            db.scalars(
                select(ReviewBatchRunDocument.matter_document_id)
                .where(ReviewBatchRunDocument.review_batch_run_id == review_run.id)
                .order_by(ReviewBatchRunDocument.matter_document_id)
            )
        )
        if not document_ids:
            raise ValueError("Assessment reanalysis run has no documents")
        assessment.status = "SUMMARIZING"
        review_run.status = "RUNNING"
        db.commit()
        return [str(document_id) for document_id in document_ids]


@DBOS.step(name="synthesize_definition_assessment", retries_allowed=True, max_attempts=3)
def synthesize(assessment_id: str) -> dict:
    with SessionLocal() as db:
        return synthesize_assessment(db, uuid.UUID(assessment_id))


@DBOS.step(name="complete_definition_assessment")
def complete(assessment_id: str) -> None:
    with SessionLocal() as db:
        complete_assessment(db, uuid.UUID(assessment_id))


@DBOS.step(name="fail_definition_assessment")
def fail(assessment_id: str, message: str) -> None:
    with SessionLocal() as db:
        fail_assessment(db, uuid.UUID(assessment_id), message)


@DBOS.workflow(name="matter_definition_assessment_v1")
def matter_definition_assessment(assessment_id: str, attempt_id: str | None = None) -> None:
    logger.info("Starting Matter Definition assessment assessment_id=%s", assessment_id)
    try:
        plan(assessment_id)
        document_ids = retrieve(assessment_id)
        sync_batch_search(assessment_id)
        handles = []
        for index, document_id in enumerate(document_ids):
            retry_segment = f":retry:{attempt_id}" if attempt_id else ""
            with SetWorkflowID(f"definition-assessment:{assessment_id}{retry_segment}:document:{index}"):
                handles.append(DOCUMENT_QUEUE.enqueue(document_workflow, assessment_id, document_id))
        for handle in handles:
            handle.get_result()
            refresh(assessment_id)
        synthesize(assessment_id)
        sync_batch_search(assessment_id)
        complete(assessment_id)
        logger.info("Completed Matter Definition assessment assessment_id=%s", assessment_id)
    except Exception as exc:
        logger.exception("Matter Definition assessment failed assessment_id=%s", assessment_id)
        fail(assessment_id, str(exc))
        raise


@DBOS.workflow(name="matter_definition_assessment_v2")
def matter_definition_assessment_v2(assessment_id: str, attempt_id: str | None = None) -> None:
    logger.info("Starting batched Matter Definition assessment assessment_id=%s", assessment_id)
    try:
        plan(assessment_id)
        document_ids = retrieve(assessment_id)
        sync_batch_search(assessment_id)
        execution = plan_batches(assessment_id, document_ids)
        handles = []
        retry_segment = f":retry:{attempt_id}" if attempt_id else ""
        for index, batch_id in enumerate(execution["provider_batch_ids"]):
            with SetWorkflowID(f"definition-assessment:{assessment_id}{retry_segment}:provider-batch:{index}"):
                handles.append(
                    PROVIDER_BATCH_QUEUE.enqueue(
                        provider_batch_workflow,
                        batch_id,
                        float(execution["poll_seconds"]),
                    )
                )
        for index, document_id in enumerate(execution["realtime_document_ids"]):
            with SetWorkflowID(f"definition-assessment:{assessment_id}{retry_segment}:document:{index}"):
                handles.append(DOCUMENT_QUEUE.enqueue(document_workflow, assessment_id, document_id))
        for handle in handles:
            handle.get_result()
            refresh(assessment_id)
        synthesize(assessment_id)
        sync_batch_search(assessment_id)
        complete(assessment_id)
        logger.info("Completed batched Matter Definition assessment assessment_id=%s", assessment_id)
    except Exception as exc:
        logger.exception("Batched Matter Definition assessment failed assessment_id=%s", assessment_id)
        fail(assessment_id, str(exc))
        raise


@DBOS.workflow(name="matter_definition_assessment_resynthesis_v1")
def matter_definition_assessment_resynthesis(assessment_id: str) -> None:
    logger.info("Regenerating Matter Definition synthesis assessment_id=%s", assessment_id)
    try:
        synthesize(assessment_id)
        sync_batch_search(assessment_id)
        complete(assessment_id)
        logger.info("Regenerated Matter Definition synthesis assessment_id=%s", assessment_id)
    except Exception as exc:
        logger.exception("Matter Definition resynthesis failed assessment_id=%s", assessment_id)
        fail(assessment_id, str(exc))
        raise


@DBOS.workflow(name="matter_definition_assessment_reanalysis_v1")
def matter_definition_assessment_reanalysis(assessment_id: str, attempt_id: str) -> None:
    logger.info("Regenerating Matter Definition document analyses assessment_id=%s", assessment_id)
    try:
        document_ids = prepare_reanalysis(assessment_id)
        handles = []
        for index, document_id in enumerate(document_ids):
            with SetWorkflowID(
                f"definition-assessment:{assessment_id}:analysis:{attempt_id}:document:{index}"
            ):
                handles.append(DOCUMENT_QUEUE.enqueue(document_workflow, assessment_id, document_id))
        for handle in handles:
            handle.get_result()
            refresh(assessment_id)
        synthesize(assessment_id)
        sync_batch_search(assessment_id)
        complete(assessment_id)
        logger.info("Regenerated Matter Definition document analyses assessment_id=%s", assessment_id)
    except Exception as exc:
        logger.exception("Matter Definition document reanalysis failed assessment_id=%s", assessment_id)
        fail(assessment_id, str(exc))
        raise


@DBOS.workflow(name="matter_definition_assessment_reanalysis_v2")
def matter_definition_assessment_reanalysis_v2(assessment_id: str, attempt_id: str) -> None:
    logger.info("Regenerating batched Matter Definition document analyses assessment_id=%s", assessment_id)
    try:
        document_ids = prepare_reanalysis(assessment_id)
        execution = plan_batches(assessment_id, document_ids)
        handles = []
        for index, batch_id in enumerate(execution["provider_batch_ids"]):
            with SetWorkflowID(
                f"definition-assessment:{assessment_id}:analysis:{attempt_id}:provider-batch:{index}"
            ):
                handles.append(
                    PROVIDER_BATCH_QUEUE.enqueue(
                        provider_batch_workflow,
                        batch_id,
                        float(execution["poll_seconds"]),
                    )
                )
        for index, document_id in enumerate(execution["realtime_document_ids"]):
            with SetWorkflowID(f"definition-assessment:{assessment_id}:analysis:{attempt_id}:document:{index}"):
                handles.append(DOCUMENT_QUEUE.enqueue(document_workflow, assessment_id, document_id))
        for handle in handles:
            handle.get_result()
            refresh(assessment_id)
        synthesize(assessment_id)
        sync_batch_search(assessment_id)
        complete(assessment_id)
        logger.info("Regenerated batched Matter Definition analyses assessment_id=%s", assessment_id)
    except Exception as exc:
        logger.exception("Batched Matter Definition reanalysis failed assessment_id=%s", assessment_id)
        fail(assessment_id, str(exc))
        raise
