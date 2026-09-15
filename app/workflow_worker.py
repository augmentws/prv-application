import logging
import threading

from dbos import DBOS

from app.config import get_settings

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
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
        }
    )
    from app.workflows import (  # noqa: F401
        agent_turn,
        matter_embeddings,
        matter_import,
        matter_topics,
        review_batches,
        search_projection,
    )

    DBOS.launch()
    logger.info("Workflow worker ready and waiting for jobs")
    threading.Event().wait()


if __name__ == "__main__":
    main()
