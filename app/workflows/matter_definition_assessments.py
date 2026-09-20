import logging
import uuid

from dbos import DBOS, Queue, SetWorkflowID

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
from app.config import get_settings
from app.database import SessionLocal
from app.models import MatterDefinitionAssessmentRun
from app.search.service import sync_review_batch_search

logger = logging.getLogger(__name__)

ASSESSMENT_QUEUE = Queue("definition-assessments", global_concurrency=2)
DOCUMENT_QUEUE = Queue(
    "definition-assessment-documents",
    global_concurrency=get_settings().definition_assessment_document_concurrency,
)


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
def matter_definition_assessment(assessment_id: str) -> None:
    logger.info("Starting Matter Definition assessment assessment_id=%s", assessment_id)
    try:
        plan(assessment_id)
        document_ids = retrieve(assessment_id)
        sync_batch_search(assessment_id)
        handles = []
        for index, document_id in enumerate(document_ids):
            with SetWorkflowID(f"definition-assessment:{assessment_id}:document:{index}"):
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
