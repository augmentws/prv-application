import uuid

from dbos import DBOS, Queue

from app.search.service import process_search_operation

SEARCH_QUEUE = Queue("search-projections", global_concurrency=4, priority_enabled=True)


@DBOS.step(name="apply_search_projection", retries_allowed=True, max_attempts=5)
def apply_search_projection(operation_id: str) -> None:
    process_search_operation(uuid.UUID(operation_id))


@DBOS.workflow(name="search_projection")
def search_projection(operation_id: str) -> None:
    apply_search_projection(operation_id)
