import logging
import math
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
from sklearn.cluster import HDBSCAN, MiniBatchKMeans
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.random_projection import GaussianRandomProjection
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.artifact_gateway import load_current_chunk_artifacts
from app.audit import record_audit
from app.config import get_settings
from app.database import SessionLocal
from app.document_metadata import apply_metadata_values
from app.embeddings.parquet import read_chunk_set, read_vector_set
from app.models import (
    MatterDocument,
    MatterTopicAssignment,
    MatterTopicBatch,
    MatterTopicCluster,
    MatterTopicJob,
    MetadataDefinition,
    MetadataGroup,
    MetadataGroupField,
    SearchProjectionOperation,
)
from app.search.service import process_search_operation

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class DiscoveredTopic:
    name: str
    description: str
    keywords: list[str]
    centroid: list[float]
    sampled_chunk_count: int


def _normalize(values: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    return values / np.maximum(norms, 1e-12)


def _topic_terms(texts: list[str], labels: np.ndarray, topic_labels: list[int]) -> dict[int, list[str]]:
    vectorizer = TfidfVectorizer(
        stop_words="english",
        ngram_range=(1, 2),
        max_features=10_000,
        max_df=0.95,
    )
    try:
        matrix = vectorizer.fit_transform(texts)
    except ValueError:
        return {label: [] for label in topic_labels}
    names = np.asarray(vectorizer.get_feature_names_out())
    result: dict[int, list[str]] = {}
    for label in topic_labels:
        rows = np.flatnonzero(labels == label)
        scores = np.asarray(matrix[rows].sum(axis=0)).ravel()
        order = scores.argsort()[::-1]
        terms = [str(names[index]) for index in order if scores[index] > 0][:8]
        result[label] = terms
    return result


def discover_topics(
    vectors: list[list[float]],
    texts: list[str],
    *,
    operating_mode: str,
    requested_topic_count: int | None,
    max_topics: int,
    random_seed: int,
) -> list[DiscoveredTopic]:
    if len(vectors) != len(texts) or not vectors:
        raise ValueError("Topic discovery requires matching, non-empty vectors and texts")
    matrix = _normalize(np.asarray(vectors, dtype=np.float32))
    if operating_mode == "FIXED":
        if requested_topic_count is None:
            raise ValueError("FIXED topic mode requires a topic count")
        if requested_topic_count > len(matrix):
            raise ValueError("The requested topic count exceeds the number of sampled chunks")
        labels = MiniBatchKMeans(
            n_clusters=requested_topic_count,
            random_state=random_seed,
            n_init="auto",
            batch_size=min(2048, max(32, len(matrix))),
        ).fit_predict(matrix)
    elif len(matrix) < 6:
        labels = np.zeros(len(matrix), dtype=np.int32)
    else:
        reduced_dimensions = min(50, matrix.shape[1], len(matrix) - 1)
        reduced = GaussianRandomProjection(
            n_components=reduced_dimensions,
            random_state=random_seed,
        ).fit_transform(matrix)
        minimum_topic_size = max(5, math.ceil(len(matrix) / max_topics))
        labels = HDBSCAN(
            min_cluster_size=minimum_topic_size,
            min_samples=max(2, minimum_topic_size // 3),
            metric="euclidean",
            allow_single_cluster=True,
            copy=True,
        ).fit_predict(reduced)
        if not np.any(labels >= 0):
            labels = np.zeros(len(matrix), dtype=np.int32)

    topic_labels = sorted(int(value) for value in np.unique(labels) if value >= 0)
    terms_by_label = _topic_terms([text[:2000] for text in texts], labels, topic_labels)
    topics: list[DiscoveredTopic] = []
    used_names: set[str] = set()
    for ordinal, label in enumerate(topic_labels, start=1):
        members = matrix[labels == label]
        centroid = _normalize(members.mean(axis=0, keepdims=True))[0]
        keywords = terms_by_label[label]
        base_name = " · ".join(term.title() for term in keywords[:3]) or f"Topic {ordinal}"
        name = base_name[:200]
        if name in used_names:
            name = f"{name[:185]} ({ordinal})"
        used_names.add(name)
        description_terms = ", ".join(keywords[:5])
        description = (
            f"Documents associated with {description_terms}."
            if description_terms
            else f"Automatically discovered topic {ordinal}."
        )
        topics.append(
            DiscoveredTopic(
                name=name,
                description=description,
                keywords=keywords,
                centroid=centroid.astype(float).tolist(),
                sampled_chunk_count=len(members),
            )
        )
    return topics


def _load_document_chunks(job: MatterTopicJob, document: MatterDocument) -> tuple[list[str], list[str], list[list[float]]]:
    artifacts = load_current_chunk_artifacts(
        collection_item_id=document.collection_item_id,
        configuration_hash=job.embedding_job.configuration_hash,
        actor_user_id=job.created_by_user_id,
        tenant_id=job.matter.client.tenant_id,
        client_id=job.matter.client_id,
    )
    if artifacts is None:
        return [], [], []
    chunk_content, vector_content = artifacts
    chunks = read_chunk_set(chunk_content)
    vectors = read_vector_set(vector_content, dimensions=job.embedding_job.embedding_dimensions)
    available = [chunk for chunk in chunks if chunk.chunk_id in vectors]
    return (
        [chunk.chunk_id for chunk in available],
        [chunk.text for chunk in available],
        [vectors[chunk.chunk_id] for chunk in available],
    )


def _sample(job: MatterTopicJob, documents: list[MatterDocument]) -> tuple[list[list[float]], list[str]]:
    rng = random.Random(int(job.configuration["random_seed"]))
    shuffled = list(documents)
    rng.shuffle(shuffled)
    per_document_limit = max(1, min(10, math.ceil(job.sample_size / max(1, len(shuffled)) * 2)))
    vectors: list[list[float]] = []
    texts: list[str] = []
    for document in shuffled:
        _, document_texts, document_vectors = _load_document_chunks(job, document)
        indexes = list(range(len(document_vectors)))
        rng.shuffle(indexes)
        for index in indexes[:per_document_limit]:
            vectors.append(document_vectors[index])
            texts.append(document_texts[index])
            if len(vectors) >= job.sample_size:
                return vectors, texts
    return vectors, texts


def _ensure_topic_definition(db: Session, job: MatterTopicJob, topics: list[DiscoveredTopic]) -> tuple[MetadataDefinition, bool]:
    definition = db.scalar(
        select(MetadataDefinition).where(
            MetadataDefinition.matter_id == job.matter_id,
            MetadataDefinition.key == "topics",
        )
    )
    created = definition is None
    if definition is None:
        definition = MetadataDefinition(
            matter_id=job.matter_id,
            key="topics",
            display_name="Topics",
            description="Named topics assigned by matter topic-clustering jobs.",
            type="ENUM",
            cardinality="MULTIPLE",
            allowed_values=[],
            value_source="ASSERTED",
            assertion_policy="IMMEDIATE",
            resolution_policy="LATEST_VALID",
            searchable=True,
            facetable=True,
            reviewable=False,
            ai_assignable=True,
            status="ACTIVE",
        )
        db.add(definition)
        db.flush()
        group = db.scalar(
            select(MetadataGroup).where(
                MetadataGroup.matter_id == job.matter_id,
                MetadataGroup.scope.in_(("SYSTEM", "MATTER")),
                MetadataGroup.key == "analysis",
            )
        )
        if group is None:
            highest_order = db.scalar(
                select(func.coalesce(func.max(MetadataGroup.sort_order), 0)).where(
                    MetadataGroup.matter_id == job.matter_id
                )
            )
            group = MetadataGroup(
                matter_id=job.matter_id,
                scope="MATTER",
                owner_user_id=None,
                created_by_user_id=job.created_by_user_id,
                key="analysis",
                display_name="Analysis",
                description="Fields produced by analytical and agent workflows.",
                sort_order=int(highest_order or 0) + 10,
                default_table_visible=False,
                default_document_visible=True,
                status="ACTIVE",
            )
            db.add(group)
            db.flush()
        highest_field_order = db.scalar(
            select(func.coalesce(func.max(MetadataGroupField.sort_order), 0)).where(
                MetadataGroupField.metadata_group_id == group.id
            )
        )
        db.add(
            MetadataGroupField(
                metadata_group_id=group.id,
                metadata_definition_id=definition.id,
                sort_order=int(highest_field_order or 0) + 10,
            )
        )
        record_audit(
            db,
            tenant_id=job.matter.client.tenant_id,
            actor_user_id=job.created_by_user_id,
            action="metadata_definition.created",
            target_type="metadata_definition",
            target_id=definition.id,
            details={"matter_id": str(job.matter_id), "key": "topics", "source": "topic_job"},
        )
    elif (
        definition.type != "ENUM"
        or definition.cardinality != "MULTIPLE"
        or definition.value_source != "ASSERTED"
        or definition.status != "ACTIVE"
    ):
        raise ValueError("The existing topics field must be an active, asserted, multi-value ENUM")

    existing = {item["key"] for item in definition.allowed_values or []}
    options = list(definition.allowed_values or [])
    for ordinal, topic in enumerate(topics):
        key = f"topic_{job.id.hex[:12]}_{ordinal + 1}"
        if key not in existing:
            options.append(
                {"key": key, "label": topic.name, "description": topic.description, "active": True}
            )
    definition.allowed_values = options
    flag_modified(definition, "allowed_values")
    return definition, created


def _sync_topic_schema(job_id: uuid.UUID) -> None:
    if not get_settings().search_enabled:
        return
    workflow_id = f"topic-schema:{job_id}"
    with SessionLocal() as db:
        job = db.get(MatterTopicJob, job_id)
        if job is None:
            return
        operation = db.scalar(
            select(SearchProjectionOperation).where(SearchProjectionOperation.workflow_id == workflow_id)
        )
        if operation is None:
            operation = SearchProjectionOperation(
                matter_id=job.matter_id,
                kind="SCHEMA_SYNC",
                payload={},
                status="QUEUED",
                workflow_id=workflow_id,
                created_by_user_id=job.created_by_user_id,
            )
            db.add(operation)
            db.commit()
        operation_id = operation.id
    process_search_operation(operation_id)


def discover_and_plan(job_id: uuid.UUID) -> list[uuid.UUID]:
    definition_created = False
    with SessionLocal() as db:
        job = db.get(MatterTopicJob, job_id)
        if job is None:
            raise ValueError("Matter topic job not found")
        if job.status == "CANCELED":
            return []
        existing = list(
            db.scalars(
                select(MatterTopicBatch.id)
                .where(MatterTopicBatch.job_id == job.id)
                .order_by(MatterTopicBatch.batch_number)
            )
        )
        if existing:
            return existing
        job.status = "SAMPLING"
        job.started_at = job.started_at or utcnow()
        documents = list(
            db.scalars(
                select(MatterDocument)
                .where(MatterDocument.matter_id == job.matter_id)
                .order_by(MatterDocument.id)
            )
        )
        job.document_count = len(documents)
        db.commit()
        if not documents:
            raise ValueError("The matter has no documents to cluster")

        vectors, texts = _sample(job, documents)
        if not vectors:
            raise ValueError("No current chunk embeddings are available for this matter")
        job.sampled_chunk_count = len(vectors)
        job.status = "CLUSTERING"
        db.commit()
        topics = discover_topics(
            vectors,
            texts,
            operating_mode=job.operating_mode,
            requested_topic_count=job.requested_topic_count,
            max_topics=int(job.configuration["max_topics"]),
            random_seed=int(job.configuration["random_seed"]),
        )
        definition, definition_created = _ensure_topic_definition(db, job, topics)
        job.metadata_definition_id = definition.id
        for ordinal, topic in enumerate(topics):
            db.add(
                MatterTopicCluster(
                    job_id=job.id,
                    ordinal=ordinal,
                    topic_key=f"topic_{job.id.hex[:12]}_{ordinal + 1}",
                    name=topic.name,
                    description=topic.description,
                    keywords=topic.keywords,
                    centroid=topic.centroid,
                    sampled_chunk_count=topic.sampled_chunk_count,
                )
            )
        batch_size = get_settings().matter_topic_batch_size
        batch_ids: list[uuid.UUID] = []
        for batch_number, offset in enumerate(range(0, len(documents), batch_size)):
            ids = [str(document.id) for document in documents[offset : offset + batch_size]]
            batch = MatterTopicBatch(
                job_id=job.id,
                batch_number=batch_number,
                document_ids=ids,
                item_count=len(ids),
            )
            db.add(batch)
            db.flush()
            batch_ids.append(batch.id)
        job.topic_count = len(topics)
        job.status = "PUBLISHING"
        db.commit()
    if definition_created:
        _sync_topic_schema(job_id)
    return batch_ids


def _assign_document(job: MatterTopicJob, document: MatterDocument, clusters: list[MatterTopicCluster]):
    chunk_ids, _, vectors = _load_document_chunks(job, document)
    if not vectors:
        return []
    matrix = _normalize(np.asarray(vectors, dtype=np.float32))
    centroids = _normalize(np.asarray([cluster.centroid for cluster in clusters], dtype=np.float32))
    similarities = matrix @ centroids.T
    nearest = similarities.argmax(axis=1)
    threshold = float(job.configuration["minimum_assignment_confidence"])
    candidates = []
    for index, cluster in enumerate(clusters):
        scores = similarities[:, index]
        best = int(scores.argmax())
        confidence = max(0.0, min(1.0, float(scores[best])))
        if confidence < threshold:
            continue
        evidence = scores.argsort()[::-1][:3]
        assigned_chunk_count = int(np.sum((nearest == index) & (scores >= threshold)))
        if assigned_chunk_count == 0:
            continue
        candidates.append(
            (cluster, confidence, [chunk_ids[int(value)] for value in evidence], assigned_chunk_count)
        )
    candidates.sort(key=lambda value: value[1], reverse=True)
    return candidates[: int(job.configuration["max_topics_per_document"])]


def _index_topic_batch(job: MatterTopicJob, batch: MatterTopicBatch) -> None:
    if not get_settings().search_enabled:
        return
    workflow_id = f"topic-index:{batch.id}"
    with SessionLocal() as db:
        operation = db.scalar(
            select(SearchProjectionOperation).where(SearchProjectionOperation.workflow_id == workflow_id)
        )
        if operation is None:
            operation = SearchProjectionOperation(
                matter_id=job.matter_id,
                kind="DOCUMENT_UPSERT",
                payload={"document_ids": batch.document_ids},
                status="QUEUED",
                workflow_id=workflow_id,
                created_by_user_id=job.created_by_user_id,
            )
            db.add(operation)
            db.commit()
        operation_id = operation.id
    process_search_operation(operation_id)


def process_batch(batch_id: uuid.UUID) -> dict[str, int]:
    with SessionLocal() as db:
        batch = db.get(MatterTopicBatch, batch_id)
        if batch is None:
            raise ValueError("Matter topic batch not found")
        job = db.get(MatterTopicJob, batch.job_id)
        if job is None or job.metadata_definition_id is None:
            raise ValueError("Matter topic job is not ready for publishing")
        if batch.status == "COMPLETED":
            return {
                "processed_count": batch.processed_count,
                "assigned_count": batch.assigned_count,
                "outlier_count": batch.outlier_count,
                "failed_count": batch.failed_count,
            }
        if job.status == "CANCELED":
            batch.status = "CANCELED"
            db.commit()
            return {key: 0 for key in ("processed_count", "assigned_count", "outlier_count", "failed_count")}
        batch.status = "RUNNING"
        db.commit()
        clusters = list(
            db.scalars(
                select(MatterTopicCluster)
                .where(MatterTopicCluster.job_id == job.id)
                .order_by(MatterTopicCluster.ordinal)
            )
        )
        processed = assigned = outliers = failed = 0
        errors: list[str] = []
        if not batch.metadata_applied:
            db.execute(delete(MatterTopicAssignment).where(MatterTopicAssignment.job_id == job.id, MatterTopicAssignment.matter_document_id.in_([uuid.UUID(value) for value in batch.document_ids])))
            for value in batch.document_ids:
                document_id = uuid.UUID(value)
                document = db.get(MatterDocument, document_id)
                if document is None or document.matter_id != job.matter_id:
                    failed += 1
                    processed += 1
                    errors.append(f"{document_id}: matter document not found")
                    continue
                try:
                    topic_values = _assign_document(job, document, clusters)
                    values = [cluster.topic_key for cluster, _, _, _ in topic_values]
                    confidence_by_key = {cluster.topic_key: confidence for cluster, confidence, _, _ in topic_values}
                    for cluster, confidence, supporting_ids, chunk_count in topic_values:
                        db.add(
                            MatterTopicAssignment(
                                job_id=job.id,
                                matter_document_id=document.id,
                                topic_cluster_id=cluster.id,
                                confidence=confidence,
                                assigned_chunk_count=chunk_count,
                                supporting_chunk_ids=supporting_ids,
                            )
                        )
                    events, _ = apply_metadata_values(
                        db,
                        matter_id=job.matter_id,
                        document_id=document.id,
                        definition_id=job.metadata_definition_id,
                        values=values,
                        replace=job.assignment_mode == "REPLACE",
                        source_type="AGENT",
                        actor_id=job.created_by_user_id,
                        source_id=f"topic-job:{job.id}",
                        confidences=confidence_by_key,
                    )
                    if events:
                        record_audit(
                            db,
                            tenant_id=job.matter.client.tenant_id,
                            actor_user_id=job.created_by_user_id,
                            action="document.topics.replaced" if job.assignment_mode == "REPLACE" else "document.topics.appended",
                            target_type="matter_document",
                            target_id=document.id,
                            details={"matter_id": str(job.matter_id), "topic_job_id": str(job.id), "topic_keys": values},
                        )
                    assigned += int(bool(values))
                    outliers += int(not values)
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    errors.append(f"{document_id}: {exc}")
                    logger.warning("Topic assignment failed job_id=%s document_id=%s error=%s", job.id, document_id, exc)
                processed += 1
            batch.processed_count = processed
            batch.assigned_count = assigned
            batch.outlier_count = outliers
            batch.failed_count = failed
            batch.error_message = "\n".join(errors[:20]) or None
            batch.metadata_applied = True
            db.commit()

        _index_topic_batch(job, batch)
        batch.status = "COMPLETED"
        db.commit()
        return {
            "processed_count": batch.processed_count,
            "assigned_count": batch.assigned_count,
            "outlier_count": batch.outlier_count,
            "failed_count": batch.failed_count,
        }


def refresh_job(job_id: uuid.UUID) -> None:
    with SessionLocal() as db:
        job = db.get(MatterTopicJob, job_id)
        if job is None:
            return
        totals = db.execute(
            select(
                func.coalesce(func.sum(MatterTopicBatch.processed_count), 0),
                func.coalesce(func.sum(MatterTopicBatch.assigned_count), 0),
                func.coalesce(func.sum(MatterTopicBatch.outlier_count), 0),
                func.coalesce(func.sum(MatterTopicBatch.failed_count), 0),
            ).where(MatterTopicBatch.job_id == job.id)
        ).one()
        (
            job.processed_document_count,
            job.assigned_document_count,
            job.outlier_document_count,
            job.failed_count,
        ) = (int(value) for value in totals)
        db.commit()


def complete_job(job_id: uuid.UUID) -> None:
    refresh_job(job_id)
    with SessionLocal() as db:
        job = db.get(MatterTopicJob, job_id)
        if job is None or job.status == "CANCELED":
            return
        clusters = list(db.scalars(select(MatterTopicCluster).where(MatterTopicCluster.job_id == job.id)))
        for cluster in clusters:
            counts = db.execute(
                select(
                    func.count(MatterTopicAssignment.id),
                    func.coalesce(func.sum(MatterTopicAssignment.assigned_chunk_count), 0),
                ).where(MatterTopicAssignment.topic_cluster_id == cluster.id)
            ).one()
            cluster.assigned_document_count = int(counts[0])
            cluster.assigned_chunk_count = int(counts[1])
        job.status = "COMPLETED_WITH_ERRORS" if job.failed_count else "COMPLETED"
        errors = list(
            db.scalars(
                select(MatterTopicBatch.error_message)
                .where(MatterTopicBatch.job_id == job.id, MatterTopicBatch.error_message.is_not(None))
                .order_by(MatterTopicBatch.batch_number)
                .limit(10)
            )
        )
        job.error_message = "\n".join(errors)[:4000] if errors else None
        job.completed_at = utcnow()
        db.commit()


def fail_job(job_id: uuid.UUID, message: str) -> None:
    with SessionLocal() as db:
        job = db.get(MatterTopicJob, job_id)
        if job is not None and job.status != "CANCELED":
            job.status = "FAILED"
            job.error_message = message[:4000]
            job.completed_at = utcnow()
            db.commit()
