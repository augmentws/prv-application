import logging
import threading

from dbos import DBOS

from app.config import configure_worker_database_profile, get_settings
from app.deployment_policy import load_core_runtime_settings

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    configure_worker_database_profile()
    load_core_runtime_settings()
    settings = get_settings()
    logger.info(
        "Starting workflow worker application=%s version=%s",
        settings.dbos_application_name,
        settings.dbos_application_version,
    )
    DBOS(
        config={
            "name": settings.dbos_application_name,
            "application_version": settings.dbos_application_version,
            "system_database_url": settings.dbos_system_database_url or settings.database_url,
            "dbos_system_schema": settings.dbos_system_schema,
            "sys_db_pool_size": settings.dbos_runtime_system_pool_size,
            "sys_db_polling_concurrency": settings.dbos_system_polling_concurrency,
        }
    )
    from app.workflows import (  # noqa: F401
        agent_turn,
        analysis_task_batch,
        analysis_task_batch_set_runs,
        analysis_task_batch_sets,
        analysis_task_compilation,
        analysis_task_playground,
        analysis_task_promotions,
        bulk_tags,
        classification_analysis_tasks,
        classification_lifecycle,
        classification_models,
        classification_projections,
        classification_scoring,
        coding_comparisons,
        collection_deletion,
        collection_text_processing,
        evidence_assessments,
        evidence_stage_retries,
        fact_development,
        fact_specification_compilation,
        identity_analysis,
        identity_domain_recommendations,
        job_execution_reconciliation,
        matter_definition_assessments,
        matter_deletion,
        matter_embeddings,
        matter_import,
        matter_topics,
        privilege_review_adjudication,
        privilege_review_evidence,
        review_batches,
        review_programs,
        search_exports,
        search_projection,
    )

    DBOS.launch()
    logger.info("Workflow worker ready and waiting for jobs")
    threading.Event().wait()


if __name__ == "__main__":
    main()
