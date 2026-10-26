from __future__ import annotations

import hashlib
import math
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.analysis_input_manifests import AnalysisInputManifestError, AnalysisInputManifestReader
from app.artifact_gateway import get_collection_item_grouping_snapshots
from app.config import Settings, get_settings
from app.embedding_gateway import get_query_embedding_gateway
from app.job_executions import (
    ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
    JobExecutionConflict,
    JobScope,
    cancel_job_execution,
    create_job_execution,
    current_job_execution,
    execution_cancel_requested,
    transition_job_execution,
)
from app.models import (
    AnalysisInputManifest,
    AnalysisTaskBatchSet,
    Matter,
    MatterAnalysisTask,
    MatterAnalysisTaskVersion,
    MatterDocument,
    MetadataDefinition,
    ReviewBatch,
    ReviewBatchDocument,
    SearchIndexGeneration,
    WorkflowRun,
    utcnow,
)
from app.schemas import MatterSearchRequest
from app.search.client import OpenSearchClient
from app.search.query import RRF_SEARCH_PIPELINE, compile_search_request

ALLOCATION_ALGORITHM_VERSION = "stable-sequence-v1"
FAMILY_ALLOCATION_ALGORITHM_VERSION = "stable-family-least-loaded-v1"


class AnalysisTaskBatchSetError(ValueError):
    pass


class AnalysisTaskBatchSetCanceled(AnalysisTaskBatchSetError):
    pass


@dataclass(frozen=True)
class BatchSetPlan:
    document_ids: list[uuid.UUID]
    partitions: list[list[uuid.UUID]]
    population_hash: str
    warnings: list[dict[str, object]]
    oversized_groups: list[dict[str, object]]


def _validate_task_version(
    *,
    matter: Matter,
    task: MatterAnalysisTask,
    task_version: MatterAnalysisTaskVersion,
) -> None:
    if task.matter_id != matter.id or task_version.matter_analysis_task_id != task.id:
        raise AnalysisTaskBatchSetError("Analysis Task version does not belong to this matter")
    if task_version.status != "PUBLISHED":
        raise AnalysisTaskBatchSetError("Partitioned batch sets require a published task version")
    if "PARTITIONED_SET" not in (task_version.scope_policy or {}).get("allowed_modes", []):
        raise AnalysisTaskBatchSetError("This task version does not allow partitioned batch sets")


def _source_batch(
    db: Session,
    *,
    matter_id: uuid.UUID,
    population_source_type: str,
    source_review_batch_id: uuid.UUID | None,
) -> ReviewBatch | None:
    if population_source_type == "SOURCE_BATCH":
        source_batch = db.scalar(
            select(ReviewBatch).where(
                ReviewBatch.id == source_review_batch_id,
                ReviewBatch.matter_id == matter_id,
            )
        )
        if source_batch is None or source_batch.status != "READY":
            raise AnalysisTaskBatchSetError("A ready source Review Batch is required")
        return source_batch
    if population_source_type not in {"ALL_MATTER", "SEARCH_QUERY", "SAVED_SEARCH", "INPUT_MANIFEST"}:
        raise AnalysisTaskBatchSetError("Unsupported batch-set population source")
    return None


def _input_manifest(
    db: Session,
    *,
    matter_id: uuid.UUID,
    population_source_type: str,
    input_manifest_id: uuid.UUID | None,
) -> AnalysisInputManifest | None:
    if population_source_type != "INPUT_MANIFEST":
        if input_manifest_id is not None:
            raise AnalysisTaskBatchSetError("Input manifest applies only to INPUT_MANIFEST populations")
        return None
    manifest = db.get(AnalysisInputManifest, input_manifest_id) if input_manifest_id is not None else None
    if manifest is None or manifest.matter_id != matter_id or manifest.manifest_status != "FINALIZED":
        raise AnalysisTaskBatchSetError("A finalized input manifest in this matter is required")
    return manifest


def _search_generation(
    db: Session,
    *,
    matter_id: uuid.UUID,
    population_source_type: str,
    search_index_generation_id: uuid.UUID | None = None,
) -> SearchIndexGeneration | None:
    if population_source_type not in {"SEARCH_QUERY", "SAVED_SEARCH"}:
        return None
    if search_index_generation_id is None:
        generation = db.scalar(
            select(SearchIndexGeneration).where(
                SearchIndexGeneration.matter_id == matter_id,
                SearchIndexGeneration.status == "ACTIVE",
            )
        )
    else:
        generation = db.scalar(
            select(SearchIndexGeneration).where(
                SearchIndexGeneration.id == search_index_generation_id,
                SearchIndexGeneration.matter_id == matter_id,
            )
        )
    if generation is None:
        raise AnalysisTaskBatchSetError(
            "The matter search index must be ready before using a search-backed population"
        )
    return generation


def _execution_for_batch_set(
    db: Session,
    *,
    batch_set: AnalysisTaskBatchSet,
    matter: Matter,
    initiated_by_user_id: uuid.UUID,
    retry_of: WorkflowRun | None = None,
    notification_parent_type: str | None = None,
    notification_parent_id: uuid.UUID | None = None,
) -> WorkflowRun:
    attempt_number = retry_of.attempt_number + 1 if retry_of is not None else 1
    workflow_id = (
        batch_set.workflow_id
        if retry_of is None
        else f"analysis-task-batch-set:{batch_set.id}:attempt:{attempt_number}"
    )
    return create_job_execution(
        db,
        job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
        job_id=batch_set.id,
        scope=JobScope(batch_set.tenant_id, matter.client_id, matter.id),
        workflow_key="analysis_task_batch_set_materialization_v1",
        code_version="1",
        dbos_workflow_id=workflow_id,
        initiated_by_user_id=initiated_by_user_id,
        idempotency_key=f"analysis-task-batch-set:{batch_set.id}:attempt:{attempt_number}",
        current_stage="QUEUED",
        progress_unit="documents",
        input_snapshot={
            "task_id": str(batch_set.matter_analysis_task_id),
            "task_version_id": str(batch_set.matter_analysis_task_version_id),
            "population_source_type": batch_set.population_source_type,
            "source_review_batch_id": (
                str(batch_set.source_review_batch_id) if batch_set.source_review_batch_id else None
            ),
            "input_manifest_id": str(batch_set.input_manifest_id) if batch_set.input_manifest_id else None,
            "search_index_generation_id": (
                str(batch_set.search_index_generation_id)
                if batch_set.search_index_generation_id
                else None
            ),
            "target_batch_size": batch_set.target_batch_size,
            "allocation_policy": batch_set.allocation_policy,
        },
        configuration_snapshot={
            "allocation_algorithm_version": batch_set.allocation_algorithm_version,
        },
        retry_of=retry_of,
        notification_policy="PARENT_MANAGED" if notification_parent_id is not None else "DEFAULT",
        notification_parent_type=notification_parent_type,
        notification_parent_id=notification_parent_id,
    )


def create_batch_set(
    db: Session,
    *,
    matter: Matter,
    task: MatterAnalysisTask,
    task_version: MatterAnalysisTaskVersion,
    name: str,
    description: str | None,
    population_source_type: str,
    source_review_batch_id: uuid.UUID | None,
    input_manifest_id: uuid.UUID | None,
    search: MatterSearchRequest | None,
    saved_search_id: uuid.UUID | None,
    saved_search_name: str | None,
    saved_search_updated_at: datetime | None,
    target_batch_size: int,
    keep_together: list[str],
    random_seed: str | None,
    actor_user_id: uuid.UUID,
    notification_parent_type: str | None = None,
    notification_parent_id: uuid.UUID | None = None,
) -> AnalysisTaskBatchSet:
    if (notification_parent_type is None) != (notification_parent_id is None):
        raise AnalysisTaskBatchSetError(
            "Batch Set notification parent type and id must be supplied together"
        )
    _validate_task_version(matter=matter, task=task, task_version=task_version)
    source_batch = _source_batch(
        db,
        matter_id=matter.id,
        population_source_type=population_source_type,
        source_review_batch_id=source_review_batch_id,
    )
    input_manifest = _input_manifest(
        db,
        matter_id=matter.id,
        population_source_type=population_source_type,
        input_manifest_id=input_manifest_id,
    )
    generation = _search_generation(
        db,
        matter_id=matter.id,
        population_source_type=population_source_type,
    )
    if population_source_type in {"SEARCH_QUERY", "SAVED_SEARCH"}:
        if search is None or search.search_mode != "KEYWORD":
            raise AnalysisTaskBatchSetError("Batch Set search populations currently require a Keyword search")
        if population_source_type == "SAVED_SEARCH" and saved_search_id is None:
            raise AnalysisTaskBatchSetError("A saved search is required")

    batch_set_id = uuid.uuid4()
    algorithm_version = (
        FAMILY_ALLOCATION_ALGORITHM_VERSION if "FAMILY" in keep_together else ALLOCATION_ALGORITHM_VERSION
    )
    batch_set = AnalysisTaskBatchSet(
        id=batch_set_id,
        tenant_id=matter.client.tenant_id,
        matter_id=matter.id,
        matter_analysis_task_id=task.id,
        matter_analysis_task_version_id=task_version.id,
        name=" ".join(name.split()),
        description=description.strip() if description else None,
        population_source_type=population_source_type,
        selection_definition={
            "type": population_source_type,
            "source_review_batch_id": str(source_batch.id) if source_batch else None,
            "source_document_count": source_batch.document_count if source_batch else None,
            "input_manifest": (
                {
                    "id": str(input_manifest.id),
                    "artifact_id": str(input_manifest.artifact_id),
                    "artifact_content_hash": input_manifest.artifact_content_hash,
                    "input_contract_hash": input_manifest.input_contract_hash,
                    "item_count": input_manifest.item_count,
                    "derivation_key": input_manifest.derivation_key,
                }
                if input_manifest is not None
                else None
            ),
            "saved_search_id": str(saved_search_id) if saved_search_id else None,
            "saved_search_name": saved_search_name,
            "saved_search_updated_at": saved_search_updated_at.isoformat() if saved_search_updated_at else None,
            "search": (
                search.model_copy(update={"offset": 0, "facets": []}).model_dump(mode="json", by_alias=True)
                if search is not None
                else None
            ),
            "search_index_generation": (
                {
                    "id": str(generation.id),
                    "generation": generation.generation,
                    "index_name": generation.index_name,
                    "schema_hash": generation.schema_hash,
                    "activated_at": generation.activated_at.isoformat() if generation.activated_at else None,
                }
                if generation is not None
                else None
            ),
            "population_snapshot_at": utcnow().isoformat(),
        },
        source_review_batch_id=source_batch.id if source_batch else None,
        input_manifest_id=input_manifest.id if input_manifest else None,
        search_index_generation_id=generation.id if generation else None,
        target_batch_size=target_batch_size,
        allocation_policy={
            "ordering": "SOURCE_SEQUENCE",
            "keep_together": keep_together,
            "oversized_group_behavior": "ALLOW_WITH_WARNING",
        },
        random_seed=random_seed or str(batch_set_id),
        allocation_algorithm_version=algorithm_version,
        status="QUEUED",
        workflow_id=f"analysis-task-batch-set:{batch_set_id}",
        created_by_user_id=actor_user_id,
    )
    db.add(batch_set)
    db.flush()
    execution = _execution_for_batch_set(
        db,
        batch_set=batch_set,
        matter=matter,
        initiated_by_user_id=actor_user_id,
        notification_parent_type=notification_parent_type,
        notification_parent_id=notification_parent_id,
    )
    batch_set.current_workflow_run_id = execution.id
    return batch_set


def search_document_ids(
    db: Session,
    *,
    matter: Matter,
    generation: SearchIndexGeneration,
    search_definition: dict[str, object],
    settings: Settings,
    limit: int | None = None,
    relevance_order: bool = False,
) -> list[uuid.UUID]:
    request = MatterSearchRequest.model_validate(search_definition)
    query_vector = None
    if request.search_mode in {"SEMANTIC", "HYBRID"}:
        query_vector = get_query_embedding_gateway().embed([request.query or ""], "query").embeddings[0]
    definitions = list(
        db.scalars(select(MetadataDefinition).where(MetadataDefinition.matter_id == matter.id))
    )
    body = compile_search_request(
        request.model_copy(update={"offset": 0, "size": 500, "facets": []}),
        definitions,
        tenant_id=str(matter.client.tenant_id),
        matter_id=str(matter.id),
        query_vector=query_vector,
    )
    body.pop("highlight", None)
    body["_source"] = ["document_id"]
    hybrid_relevance_order = relevance_order and request.search_mode == "HYBRID"
    if hybrid_relevance_order:
        # The RRF search pipeline rejects combining _score with a tie-breaker sort.
        # Preserve its native relevance order and paginate with an offset instead.
        body.pop("sort", None)
        body["from"] = 0
    else:
        body["sort"] = (
            [{"_score": "desc"}, {"document_id": "asc"}]
            if relevance_order
            else [{"document_id": "asc"}]
        )
        body.pop("from", None)
    client = OpenSearchClient(settings)
    document_ids: list[uuid.UUID] = []
    try:
        if request.search_mode == "HYBRID":
            client.ensure_rrf_search_pipeline(RRF_SEARCH_PIPELINE)
        while True:
            raw = client.search(
                generation.index_name,
                body,
                search_pipeline=RRF_SEARCH_PIPELINE if request.search_mode == "HYBRID" else None,
            )
            hits = raw.get("hits", {}).get("hits", [])
            if not hits:
                break
            for hit in hits:
                value = (hit.get("_source") or {}).get("document_id") or hit.get("_id")
                document_ids.append(uuid.UUID(str(value)))
                if limit is not None and len(document_ids) >= limit:
                    return document_ids
            if len(hits) < body["size"]:
                break
            if hybrid_relevance_order:
                next_offset = int(body.get("from", 0)) + len(hits)
                candidate_limit = request.candidate_limit or 10_000
                if next_offset >= candidate_limit:
                    break
                body["from"] = next_offset
                body["query"]["hybrid"]["pagination_depth"] = min(
                    candidate_limit,
                    next_offset + body["size"],
                )
            else:
                body["search_after"] = hits[-1]["sort"]
    finally:
        client.close()
    return document_ids


def _population_documents(
    db: Session,
    *,
    matter: Matter,
    population_source_type: str,
    source_review_batch_id: uuid.UUID | None,
    input_manifest_id: uuid.UUID | None,
    selection_definition: dict[str, object],
    search_index_generation_id: uuid.UUID | None,
    population_snapshot_at: datetime,
    settings: Settings,
    actor_user_id: uuid.UUID,
) -> list[MatterDocument]:
    if population_source_type == "SOURCE_BATCH":
        return list(
            db.scalars(
                select(MatterDocument)
                .join(
                    ReviewBatchDocument,
                    ReviewBatchDocument.matter_document_id == MatterDocument.id,
                )
                .where(
                    ReviewBatchDocument.review_batch_id == source_review_batch_id,
                    MatterDocument.matter_id == matter.id,
                )
                .order_by(ReviewBatchDocument.sequence_number)
            )
        )
    if population_source_type in {"SEARCH_QUERY", "SAVED_SEARCH"}:
        generation = _search_generation(
            db,
            matter_id=matter.id,
            population_source_type=population_source_type,
            search_index_generation_id=search_index_generation_id,
        )
        if generation is None or not isinstance(selection_definition.get("search"), dict):
            raise AnalysisTaskBatchSetError("Search-backed population provenance is incomplete")
        document_ids = search_document_ids(
            db,
            matter=matter,
            generation=generation,
            search_definition=selection_definition["search"],
            settings=settings,
        )
        documents_by_id = {
            document.id: document
            for document in db.scalars(
                select(MatterDocument).where(
                    MatterDocument.matter_id == matter.id,
                    MatterDocument.id.in_(document_ids),
                )
            )
        }
        if len(documents_by_id) != len(document_ids):
            raise AnalysisTaskBatchSetError(
                "The pinned search generation contains documents outside the authoritative matter population"
            )
        return [documents_by_id[document_id] for document_id in document_ids]
    if population_source_type == "INPUT_MANIFEST":
        manifest = _input_manifest(
            db,
            matter_id=matter.id,
            population_source_type=population_source_type,
            input_manifest_id=input_manifest_id,
        )
        if manifest is None:
            raise AnalysisTaskBatchSetError("Input-manifest population provenance is incomplete")
        try:
            rows = AnalysisInputManifestReader(manifest, actor_user_id=actor_user_id).rows(
                0,
                manifest.item_count,
            )
            ready_ids = [
                uuid.UUID(str(row["matter_document_id"]))
                for row in rows
                if row.get("input_status") == "READY"
            ]
        except (AnalysisInputManifestError, KeyError, TypeError, ValueError) as exc:
            raise AnalysisTaskBatchSetError(f"Input manifest population is invalid: {exc}") from exc
        if len(ready_ids) != manifest.item_count:
            raise AnalysisTaskBatchSetError(
                "Batch Set input manifests must contain only ready population rows"
            )
        documents_by_id = {
            document.id: document
            for document in db.scalars(
                select(MatterDocument).where(
                    MatterDocument.matter_id == matter.id,
                    MatterDocument.id.in_(ready_ids),
                )
            )
        }
        if len(documents_by_id) != len(ready_ids):
            raise AnalysisTaskBatchSetError(
                "The input manifest contains documents outside the authoritative matter population"
            )
        return [documents_by_id[document_id] for document_id in ready_ids]
    return list(
        db.scalars(
            select(MatterDocument)
            .where(
                MatterDocument.matter_id == matter.id,
                MatterDocument.created_at <= population_snapshot_at,
            )
            .order_by(MatterDocument.created_at, MatterDocument.id)
        )
    )


def _stable_partitions(document_ids: list[uuid.UUID], target_batch_size: int) -> list[list[uuid.UUID]]:
    return [
        document_ids[offset : offset + target_batch_size]
        for offset in range(0, len(document_ids), target_batch_size)
    ]


def _family_partitions(
    documents: list[MatterDocument],
    *,
    matter: Matter,
    target_batch_size: int,
    actor_user_id: uuid.UUID,
) -> tuple[list[list[uuid.UUID]], list[dict[str, object]]]:
    item_ids = [document.collection_item_id for document in documents]
    try:
        grouping = get_collection_item_grouping_snapshots(
            item_ids=item_ids,
            actor_user_id=actor_user_id,
            tenant_id=matter.client.tenant_id,
            client_id=matter.client_id,
        )
    except AnalysisTaskBatchSetError:
        raise
    except Exception as exc:
        raise AnalysisTaskBatchSetError(f"Document family lookup failed: {exc}") from exc
    missing_count = len(set(item_ids).difference(grouping))
    if missing_count:
        raise AnalysisTaskBatchSetError(
            f"Document family data is unavailable for {missing_count} selected documents"
        )
    units_by_key: dict[str, list[uuid.UUID]] = {}
    ordered_keys: list[str] = []
    for document in documents:
        snapshot = grouping.get(document.collection_item_id)
        key = (
            f"family:{snapshot.family_id}"
            if snapshot is not None and snapshot.family_id
            else f"document:{document.id}"
        )
        if key not in units_by_key:
            units_by_key[key] = []
            ordered_keys.append(key)
        units_by_key[key].append(document.id)

    partition_count = min(len(ordered_keys), math.ceil(len(documents) / target_batch_size))
    partitions: list[list[uuid.UUID]] = [[] for _ in range(partition_count)]
    warnings: list[dict[str, object]] = []
    for key in ordered_keys:
        unit = units_by_key[key]
        if key.startswith("family:") and len(unit) > target_batch_size:
            warnings.append(
                {
                    "code": "OVERSIZED_FAMILY",
                    "family_id": key.removeprefix("family:"),
                    "document_count": len(unit),
                    "target_batch_size": target_batch_size,
                }
            )
        partition_index = min(range(partition_count), key=lambda index: (len(partitions[index]), index))
        partitions[partition_index].extend(unit)
    return partitions, warnings


def plan_batch_set(
    db: Session,
    *,
    matter: Matter,
    population_source_type: str,
    source_review_batch_id: uuid.UUID | None,
    input_manifest_id: uuid.UUID | None,
    selection_definition: dict[str, object] | None,
    search_index_generation_id: uuid.UUID | None,
    target_batch_size: int,
    keep_together: list[str],
    actor_user_id: uuid.UUID,
    population_snapshot_at: datetime | None = None,
    settings: Settings | None = None,
) -> BatchSetPlan:
    _source_batch(
        db,
        matter_id=matter.id,
        population_source_type=population_source_type,
        source_review_batch_id=source_review_batch_id,
    )
    _input_manifest(
        db,
        matter_id=matter.id,
        population_source_type=population_source_type,
        input_manifest_id=input_manifest_id,
    )
    documents = _population_documents(
        db,
        matter=matter,
        population_source_type=population_source_type,
        source_review_batch_id=source_review_batch_id,
        input_manifest_id=input_manifest_id,
        selection_definition=selection_definition or {},
        search_index_generation_id=search_index_generation_id,
        population_snapshot_at=population_snapshot_at or utcnow(),
        settings=settings or get_settings(),
        actor_user_id=actor_user_id,
    )
    if not documents:
        raise AnalysisTaskBatchSetError("The selected population has no documents")
    document_ids = [document.id for document in documents]
    population_hash = hashlib.sha256("\n".join(str(value) for value in document_ids).encode()).hexdigest()
    if "FAMILY" in keep_together:
        partitions, warnings = _family_partitions(
            documents,
            matter=matter,
            target_batch_size=target_batch_size,
            actor_user_id=actor_user_id,
        )
    else:
        partitions = _stable_partitions(document_ids, target_batch_size)
        warnings = []
    return BatchSetPlan(
        document_ids=document_ids,
        partitions=partitions,
        population_hash=population_hash,
        warnings=warnings,
        oversized_groups=[warning for warning in warnings if warning["code"] == "OVERSIZED_FAMILY"],
    )


def preview_batch_set(
    db: Session,
    *,
    matter: Matter,
    task: MatterAnalysisTask,
    task_version: MatterAnalysisTaskVersion,
    population_source_type: str,
    source_review_batch_id: uuid.UUID | None,
    input_manifest_id: uuid.UUID | None,
    search: MatterSearchRequest | None,
    target_batch_size: int,
    keep_together: list[str],
    actor_user_id: uuid.UUID,
    settings: Settings | None = None,
) -> BatchSetPlan:
    _validate_task_version(matter=matter, task=task, task_version=task_version)
    generation = _search_generation(
        db,
        matter_id=matter.id,
        population_source_type=population_source_type,
    )
    return plan_batch_set(
        db,
        matter=matter,
        population_source_type=population_source_type,
        source_review_batch_id=source_review_batch_id,
        input_manifest_id=input_manifest_id,
        selection_definition={
            "search": (
                search.model_copy(update={"offset": 0, "facets": []}).model_dump(mode="json", by_alias=True)
                if search is not None
                else None
            )
        },
        search_index_generation_id=generation.id if generation else None,
        target_batch_size=target_batch_size,
        keep_together=keep_together,
        actor_user_id=actor_user_id,
        settings=settings,
    )


def _assert_not_canceled(db: Session, batch_set_id: uuid.UUID) -> None:
    if execution_cancel_requested(
        db,
        job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
        job_id=batch_set_id,
    ):
        raise AnalysisTaskBatchSetCanceled("Analysis Task Batch Set materialization was canceled")


def _mark_children(db: Session, batch_set_id: uuid.UUID, *, status: str, error_message: str | None = None) -> None:
    values: dict[str, object] = {"status": status, "error_message": error_message}
    if status in {"READY", "FAILED", "CANCELED"}:
        values["completed_at"] = utcnow()
    db.execute(
        update(ReviewBatch)
        .where(ReviewBatch.analysis_task_batch_set_id == batch_set_id)
        .values(**values)
    )


def materialize_batch_set(
    db: Session,
    batch_set_id: uuid.UUID,
    settings: Settings | None = None,
) -> AnalysisTaskBatchSet:
    settings = settings or get_settings()
    batch_set = db.scalar(
        select(AnalysisTaskBatchSet).where(AnalysisTaskBatchSet.id == batch_set_id).with_for_update()
    )
    if batch_set is None:
        raise AnalysisTaskBatchSetError("Analysis Task Batch Set not found")
    if batch_set.status in {"READY", "ARCHIVED"}:
        return batch_set
    _assert_not_canceled(db, batch_set.id)
    matter = db.get(Matter, batch_set.matter_id)
    if matter is None:
        raise AnalysisTaskBatchSetError("Analysis Task Batch Set matter not found")
    batch_set.status = "BUILDING"
    batch_set.error_message = None
    transition_job_execution(
        db,
        job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
        job_id=batch_set.id,
        status="RUNNING",
        current_stage="RESOLVING_POPULATION",
        progress_completed=0,
    )
    db.commit()

    try:
        raw_snapshot_at = batch_set.selection_definition.get("population_snapshot_at")
        if not raw_snapshot_at:
            raise AnalysisTaskBatchSetError("Batch Set population snapshot time is missing")
        snapshot_at = datetime.fromisoformat(str(raw_snapshot_at))
        keep_together = [str(value) for value in batch_set.allocation_policy.get("keep_together", [])]
        if "FAMILY" in keep_together:
            transition_job_execution(
                db,
                job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
                job_id=batch_set.id,
                current_stage="RESOLVING_KEEP_TOGETHER_GROUPS",
            )
        plan = plan_batch_set(
            db,
            matter=matter,
            population_source_type=batch_set.population_source_type,
            source_review_batch_id=batch_set.source_review_batch_id,
            input_manifest_id=batch_set.input_manifest_id,
            selection_definition=batch_set.selection_definition,
            search_index_generation_id=batch_set.search_index_generation_id,
            target_batch_size=batch_set.target_batch_size,
            keep_together=keep_together,
            actor_user_id=batch_set.created_by_user_id,
            population_snapshot_at=snapshot_at,
            settings=settings,
        )
        batch_set.population_count = len(plan.document_ids)
        batch_set.batch_count = len(plan.partitions)
        batch_set.population_hash = plan.population_hash
        batch_set.warning_details = plan.warnings
        transition_job_execution(
            db,
            job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
            job_id=batch_set.id,
            current_stage="PLANNING_PARTITIONS",
            progress_total=len(plan.document_ids),
        )
        db.commit()
        batch_set = db.scalar(
            select(AnalysisTaskBatchSet)
            .where(AnalysisTaskBatchSet.id == batch_set.id)
            .with_for_update()
        )
        if batch_set is None:
            raise AnalysisTaskBatchSetError("Analysis Task Batch Set not found during planning")
        _assert_not_canceled(db, batch_set.id)

        existing_children = list(
            db.scalars(select(ReviewBatch.id).where(ReviewBatch.analysis_task_batch_set_id == batch_set.id))
        )
        if existing_children:
            db.execute(delete(ReviewBatch).where(ReviewBatch.id.in_(existing_children)))
            db.commit()

        completed = 0
        for ordinal, partition in enumerate(plan.partitions, start=1):
            _assert_not_canceled(db, batch_set.id)
            membership_hash = hashlib.sha256("\n".join(str(value) for value in partition).encode()).hexdigest()
            child = ReviewBatch(
                id=uuid.uuid5(batch_set.id, f"partition:{ordinal}"),
                matter_id=batch_set.matter_id,
                name=f"{batch_set.name} · {ordinal} of {len(plan.partitions)}",
                description=batch_set.description,
                selection_type="TASK_BATCH_PARTITION",
                selection_definition={
                    "type": "TASK_BATCH_PARTITION",
                    "analysis_task_batch_set_id": str(batch_set.id),
                    "task_id": str(batch_set.matter_analysis_task_id),
                    "task_version_id": str(batch_set.matter_analysis_task_version_id),
                    "ordinal": ordinal,
                    "population_hash": plan.population_hash,
                    "membership_hash": membership_hash,
                    "allocation_algorithm_version": batch_set.allocation_algorithm_version,
                    "keep_together": keep_together,
                },
                analysis_task_batch_set_id=batch_set.id,
                batch_set_ordinal=ordinal,
                reviewer_value_visibility="OWN_VALUES",
                status="BUILDING",
                search_status=(
                    "QUEUED"
                    if settings.search_enabled and db.get_bind().dialect.name == "postgresql"
                    else "NOT_CONFIGURED"
                ),
                workflow_id=f"analysis-task-batch-set:{batch_set.id}:batch:{ordinal}",
                document_count=len(partition),
                created_by_user_id=batch_set.created_by_user_id,
            )
            db.add(child)
            db.flush()
            db.add_all(
                ReviewBatchDocument(
                    review_batch_id=child.id,
                    matter_document_id=document_id,
                    analysis_task_batch_set_id=batch_set.id,
                    sequence_number=index,
                )
                for index, document_id in enumerate(partition, start=1)
            )
            completed += len(partition)
            transition_job_execution(
                db,
                job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
                job_id=batch_set.id,
                current_stage="MATERIALIZING_MEMBERSHIP",
                progress_completed=completed,
                progress_total=len(plan.document_ids),
            )
            db.commit()
            batch_set = db.get(AnalysisTaskBatchSet, batch_set.id) or batch_set

        batch_set = db.scalar(
            select(AnalysisTaskBatchSet)
            .where(AnalysisTaskBatchSet.id == batch_set.id)
            .with_for_update()
        )
        if batch_set is None:
            raise AnalysisTaskBatchSetError("Analysis Task Batch Set not found during finalization")
        _assert_not_canceled(db, batch_set.id)
        transition_job_execution(
            db,
            job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
            job_id=batch_set.id,
            current_stage="VERIFYING_MEMBERSHIP",
        )
        assigned = list(
            db.scalars(
                select(ReviewBatchDocument.matter_document_id)
                .where(ReviewBatchDocument.analysis_task_batch_set_id == batch_set.id)
                .order_by(ReviewBatchDocument.matter_document_id)
            )
        )
        if len(assigned) != len(plan.document_ids) or set(assigned) != set(plan.document_ids):
            raise AnalysisTaskBatchSetError("Partition verification did not exactly cover the frozen population")
        _mark_children(db, batch_set.id, status="READY")
        transition_job_execution(
            db,
            job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
            job_id=batch_set.id,
            current_stage="SYNCING_SEARCH",
            progress_completed=len(plan.document_ids),
            progress_total=len(plan.document_ids),
        )
        db.commit()
        return db.get(AnalysisTaskBatchSet, batch_set.id) or batch_set
    except AnalysisTaskBatchSetCanceled:
        db.rollback()
        canceled = db.get(AnalysisTaskBatchSet, batch_set_id)
        if canceled is not None:
            canceled.status = "CANCELED"
            canceled.completed_at = utcnow()
            _mark_children(db, batch_set_id, status="CANCELED")
            db.commit()
        raise
    except Exception as exc:
        db.rollback()
        failed = db.get(AnalysisTaskBatchSet, batch_set_id)
        if failed is not None:
            current = current_job_execution(
                db,
                job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
                job_id=failed.id,
            )
            if current is not None and current.status == "CANCELED":
                failed.status = "CANCELED"
                failed.completed_at = utcnow()
                _mark_children(db, batch_set_id, status="CANCELED")
                db.commit()
                raise AnalysisTaskBatchSetCanceled(
                    "Analysis Task Batch Set materialization was canceled"
                ) from exc
            failed.status = "FAILED"
            failed.error_message = str(exc)[:4000]
            failed.completed_at = utcnow()
            _mark_children(db, batch_set_id, status="FAILED", error_message=failed.error_message)
            workflow = transition_job_execution(
                db,
                job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
                job_id=failed.id,
                status="FAILED",
                current_stage="FAILED",
                failure_code="ANALYSIS_TASK_BATCH_SET_MATERIALIZATION_FAILED",
                failure_summary=failed.error_message,
                failure_retriable=True,
            )
            if workflow.notification_policy == "PARENT_MANAGED":
                from app.review_programs import project_review_program_discovery_batch_set_terminal

                project_review_program_discovery_batch_set_terminal(
                    db,
                    batch_set=failed,
                    workflow=workflow,
                )
            db.commit()
        raise


def batch_set_child_ids(db: Session, batch_set_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        db.scalars(
            select(ReviewBatch.id)
            .where(ReviewBatch.analysis_task_batch_set_id == batch_set_id)
            .order_by(ReviewBatch.batch_set_ordinal)
        )
    )


def complete_batch_set_materialization(
    db: Session,
    batch_set_id: uuid.UUID,
) -> AnalysisTaskBatchSet:
    batch_set = db.scalar(
        select(AnalysisTaskBatchSet)
        .where(AnalysisTaskBatchSet.id == batch_set_id)
        .with_for_update()
    )
    if batch_set is None:
        raise AnalysisTaskBatchSetError("Analysis Task Batch Set not found")
    children = list(
        db.scalars(
            select(ReviewBatch)
            .where(ReviewBatch.analysis_task_batch_set_id == batch_set.id)
            .order_by(ReviewBatch.batch_set_ordinal)
        )
    )
    failed_children = [child for child in children if child.search_status == "FAILED"]
    warnings = [warning for warning in batch_set.warning_details if warning.get("code") != "SEARCH_PROJECTION_FAILED"]
    if failed_children:
        warnings.append(
            {
                "code": "SEARCH_PROJECTION_FAILED",
                "child_batch_ids": [str(child.id) for child in failed_children],
                "child_count": len(failed_children),
            }
        )
    batch_set.warning_details = warnings
    batch_set.completed_at = utcnow()
    workflow = transition_job_execution(
        db,
        job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
        job_id=batch_set.id,
        status="COMPLETED_WITH_ERRORS" if failed_children else "COMPLETED",
        current_stage="COMPLETED_WITH_ERRORS" if failed_children else "COMPLETED",
        progress_completed=batch_set.population_count,
        progress_total=batch_set.population_count,
    )
    if workflow.notification_policy == "PARENT_MANAGED":
        from app.review_programs import project_review_program_discovery_batch_set_terminal

        project_review_program_discovery_batch_set_terminal(
            db,
            batch_set=batch_set,
            workflow=workflow,
        )
    return batch_set


def cancel_batch_set(
    db: Session,
    batch_set: AnalysisTaskBatchSet,
    *,
    actor_user_id: uuid.UUID,
) -> tuple[WorkflowRun, bool]:
    execution, canceled = cancel_job_execution(
        db,
        job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
        job_id=batch_set.id,
        actor_user_id=actor_user_id,
    )
    if canceled:
        batch_set.completed_at = utcnow()
        _mark_children(db, batch_set.id, status="CANCELED")
    return execution, canceled


def retry_batch_set(
    db: Session,
    batch_set: AnalysisTaskBatchSet,
    *,
    actor_user_id: uuid.UUID,
) -> WorkflowRun:
    current = current_job_execution(
        db,
        job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
        job_id=batch_set.id,
        for_update=True,
    )
    if current is None:
        raise JobExecutionConflict("Analysis Task Batch Set has no execution to retry")
    if current.status not in {"FAILED", "CANCELED"}:
        raise JobExecutionConflict("Only failed or canceled Batch Set materialization can be retried")
    matter = db.get(Matter, batch_set.matter_id)
    if matter is None:
        raise JobExecutionConflict("Analysis Task Batch Set matter was not found")
    batch_set.status = "QUEUED"
    batch_set.error_message = None
    batch_set.completed_at = None
    return _execution_for_batch_set(
        db,
        batch_set=batch_set,
        matter=matter,
        initiated_by_user_id=actor_user_id,
        retry_of=current,
    )
