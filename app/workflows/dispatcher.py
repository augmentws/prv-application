from functools import lru_cache

from dbos import DBOSClient, EnqueueOptions
from sqlalchemy.orm import Session

from app.config import get_settings


@lru_cache
def get_dbos_client() -> DBOSClient:
    settings = get_settings()
    return DBOSClient(
        system_database_url=settings.dbos_system_database_url or settings.database_url,
        dbos_system_schema=settings.dbos_system_schema,
        application_name=settings.dbos_application_name,
    )


def enqueue_matter_import(db: Session, workflow_id: str, job_id: str) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "matter_document_import",
        "queue_name": "matter-import-plans",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, job_id)


def cancel_matter_import(workflow_id: str) -> None:
    if get_settings().dbos_enabled:
        get_dbos_client().cancel_workflow(workflow_id, cancel_children=True)


def enqueue_matter_embedding(db: Session, workflow_id: str, job_id: str) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "matter_embedding_job_v2",
        "queue_name": "matter-embedding-plans",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, job_id)


def cancel_matter_embedding(workflow_id: str) -> None:
    if get_settings().dbos_enabled:
        get_dbos_client().cancel_workflow(workflow_id, cancel_children=True)


def enqueue_classification_model_build(
    db: Session,
    workflow_id: str,
    training_run_id: str,
) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "classification_model_build_v1",
        "queue_name": "classification-model-builds",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, training_run_id)


def cancel_classification_model_build(workflow_id: str) -> None:
    if get_settings().dbos_enabled:
        get_dbos_client().cancel_workflow(workflow_id, cancel_children=True)


def enqueue_classification_training_job(
    db: Session,
    workflow_id: str,
    training_job_id: str,
) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "classification_training_job_v1",
        "queue_name": "classification-training-jobs",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, training_job_id)


def cancel_classification_training_job(workflow_id: str) -> None:
    if get_settings().dbos_enabled:
        get_dbos_client().cancel_workflow(workflow_id, cancel_children=True)


def enqueue_classification_model_scoring(
    db: Session,
    workflow_id: str,
    scoring_run_id: str,
) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "classification_model_score_v1",
        "queue_name": "classification-model-scoring",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, scoring_run_id)


def enqueue_classification_analysis_task(
    db: Session,
    workflow_id: str,
    run_id: str,
) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "classification_analysis_task_v1",
        "queue_name": "classification-analysis-tasks",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, run_id)


def cancel_classification_analysis_task(workflow_id: str) -> None:
    if get_settings().dbos_enabled:
        get_dbos_client().cancel_workflow(workflow_id, cancel_children=True)


def cancel_classification_model_scoring(workflow_id: str) -> None:
    if get_settings().dbos_enabled:
        get_dbos_client().cancel_workflow(workflow_id, cancel_children=True)


def enqueue_classification_projection(db: Session, workflow_id: str, run_id: str) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "classification_prediction_project_v1",
        "queue_name": "classification-projections",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, run_id)


def enqueue_analysis_task_promotion(db: Session, workflow_id: str, promotion_id: str) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "analysis_task_run_promotion_v1",
        "queue_name": "analysis-task-promotions",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, promotion_id)


def enqueue_classification_model_promotion(
    db: Session,
    workflow_id: str,
    *,
    matter_id: str,
    classification_model_id: str,
    model_version_id: str,
    action: str,
    expected_model_row_version: int,
    preview_token: str,
    reason: str,
    actor_user_id: str,
) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "classification_model_promote_v1",
        "queue_name": "classification-model-promotions",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(
        db,
        options,
        workflow_id,
        matter_id,
        classification_model_id,
        model_version_id,
        action,
        expected_model_row_version,
        preview_token,
        reason,
        actor_user_id,
    )


def enqueue_matter_topics(db: Session, workflow_id: str, job_id: str) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "matter_topic_job",
        "queue_name": "matter-topic-plans",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, job_id)


def cancel_matter_topics(workflow_id: str) -> None:
    if get_settings().dbos_enabled:
        get_dbos_client().cancel_workflow(workflow_id, cancel_children=True)


def enqueue_matter_topic_application(db: Session, job_id: str) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "matter_topic_application",
        "queue_name": "matter-topic-plans",
        "workflow_id": f"matter-topics:{job_id}:application",
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, job_id)


def cancel_matter_topic_application(job_id: str) -> None:
    if get_settings().dbos_enabled:
        get_dbos_client().cancel_workflow(f"matter-topics:{job_id}:application", cancel_children=True)


def enqueue_definition_assessment(
    db: Session,
    workflow_id: str,
    assessment_id: str,
    *,
    attempt_id: str | None = None,
) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "matter_definition_assessment_v2",
        "queue_name": "definition-assessments",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    if attempt_id is None:
        get_dbos_client().enqueue_in_transaction(db, options, assessment_id)
    else:
        get_dbos_client().enqueue_in_transaction(db, options, assessment_id, attempt_id)


def enqueue_definition_assessment_synthesis(db: Session, workflow_id: str, assessment_id: str) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "matter_definition_assessment_resynthesis_v1",
        "queue_name": "definition-assessments",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, assessment_id)


def enqueue_definition_assessment_reanalysis(
    db: Session,
    workflow_id: str,
    assessment_id: str,
    attempt_id: str,
) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "matter_definition_assessment_reanalysis_v2",
        "queue_name": "definition-assessments",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, assessment_id, attempt_id)


def enqueue_definition_assessment_guidance_refinement(
    db: Session,
    workflow_id: str,
    assessment_id: str,
) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "matter_definition_assessment_guidance_refinement_v1",
        "queue_name": "definition-assessments",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, assessment_id)


def enqueue_analysis_task_compilation(
    db: Session,
    workflow_id: str,
    task_version_id: str,
) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "matter_analysis_task_compilation_v1",
        "queue_name": "analysis-task-compilations",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, task_version_id)


def enqueue_fact_specification_compilation(
    db: Session,
    workflow_id: str,
    task_version_id: str,
) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "fact_development_specification_compilation_v1",
        "queue_name": "fact-specification-compilations",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, task_version_id)


def enqueue_analysis_task_playground(db: Session, workflow_id: str, workflow_run_id: str) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "matter_analysis_task_playground_v1",
        "queue_name": "analysis-task-playground",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, workflow_run_id)


def enqueue_analysis_task_batch(db: Session, workflow_id: str, workflow_run_id: str) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "matter_analysis_task_batch_v1",
        "queue_name": "analysis-task-batches",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, workflow_run_id)


def cancel_analysis_task_batch(workflow_id: str) -> None:
    if get_settings().dbos_enabled:
        get_dbos_client().cancel_workflow(workflow_id, cancel_children=True)


def cancel_definition_assessment(workflow_id: str) -> None:
    if get_settings().dbos_enabled:
        get_dbos_client().cancel_workflow(workflow_id, cancel_children=True)


def enqueue_review_batch(db: Session, workflow_id: str, batch_id: str) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "review_batch_build",
        "queue_name": "review-batch-builds",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, batch_id)


def enqueue_analysis_task_batch_set(db: Session, workflow_id: str, batch_set_id: str) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "analysis_task_batch_set_materialization_v1",
        "queue_name": "analysis-task-batch-sets",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, batch_set_id)


def cancel_analysis_task_batch_set_workflow(workflow_id: str) -> None:
    if get_settings().dbos_enabled:
        get_dbos_client().cancel_workflow(workflow_id, cancel_children=True)


def cancel_review_batch(workflow_id: str) -> None:
    if get_settings().dbos_enabled:
        get_dbos_client().cancel_workflow(workflow_id, cancel_children=True)


def enqueue_search_export(db: Session, workflow_id: str, job_id: str) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "matter_search_export_v1",
        "queue_name": "matter-search-exports",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, job_id)


def cancel_search_export(workflow_id: str) -> None:
    if get_settings().dbos_enabled:
        get_dbos_client().cancel_workflow(workflow_id, cancel_children=True)


def enqueue_fact_development_run(db: Session, workflow_id: str, run_id: str, *, batch_api: bool = False) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "fact_development_v1",
        # Batch API runs wait hours on provider batches, so they never take a real-time run's slot.
        "queue_name": "fact-development-batch-runs" if batch_api else "fact-development-runs",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, run_id)


def cancel_fact_development_run_workflow(workflow_id: str) -> None:
    if get_settings().dbos_enabled:
        get_dbos_client().cancel_workflow(workflow_id, cancel_children=True)


def enqueue_bulk_tag(db: Session, workflow_id: str, job_id: str) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "matter_bulk_tag_job",
        "queue_name": "matter-bulk-tag-plans",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, job_id)


def enqueue_search_projection(
    db: Session,
    workflow_id: str,
    operation_id: str,
    *,
    priority: int | None = None,
) -> None:
    settings = get_settings()
    if not settings.dbos_enabled or not settings.search_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "search_projection",
        "queue_name": "search-projections",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    if priority is not None:
        options["priority"] = priority
    get_dbos_client().enqueue_in_transaction(db, options, operation_id)


def enqueue_agent_run(db: Session, workflow_id: str, run_id: str) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "agent_turn",
        "queue_name": "agent-turns",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, run_id)


def enqueue_collection_text_processing(run_id: str) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "collection_text_processing",
        "queue_name": "collection-text-processing",
        "workflow_id": f"collection-text-processing:{run_id}",
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue(options, run_id)


def enqueue_collection_deletion(workflow_id: str, job_id: str) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "collection_deletion",
        "queue_name": "collection-deletions",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue(options, job_id)
