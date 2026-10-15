"""Recall: find existing facts that may state the same thing as a new candidate fact.

Used by the fact-development agent so evidence accumulates on existing facts instead of creating near-duplicates.
SQL selects a bounded candidate set from the Core ledger (shared issues, an optional date window, no merged facts);
summary embeddings are then compared in code. Embeddings are computed on first use and cached on the fact together
with the model and a hash of the summary, so a changed summary or model is detected and recomputed.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.embedding_gateway import EmbeddingGateway, get_embedding_gateway
from app.fact_queries import FACT_DATE_ORDER, FactFilters, fact_filter_statement
from app.matter_entities import canonical_ids
from app.models import MatterFact, MatterFactEntity

EMBEDDING_BATCH_SIZE = 64


@dataclass(frozen=True)
class RecalledFact:
    fact: MatterFact
    similarity: float
    shared_participants: int
    rejected: bool


def _summary_hash(summary: str, model: str) -> str:
    return hashlib.sha256(f"{model}\n{summary}".encode()).hexdigest()


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


def ensure_summary_embeddings(db: Session, facts: Sequence[MatterFact], gateway: EmbeddingGateway) -> None:
    """Compute and cache missing or stale summary embeddings for ``facts`` (caller commits)."""

    model = gateway.settings.model
    stale = [fact for fact in facts if fact.summary_embedding_hash != _summary_hash(fact.summary, model)]
    for start in range(0, len(stale), EMBEDDING_BATCH_SIZE):
        batch = stale[start : start + EMBEDDING_BATCH_SIZE]
        response = gateway.embed([fact.summary for fact in batch], "document")
        if len(response.embeddings) != len(batch):
            raise RuntimeError("Embedding service returned the wrong number of vectors")
        for fact, vector in zip(batch, response.embeddings, strict=True):
            fact.summary_embedding = list(vector)
            fact.summary_embedding_model = model
            fact.summary_embedding_hash = _summary_hash(fact.summary, model)
    if stale:
        db.flush()


def recall_facts(
    db: Session,
    *,
    matter_id: uuid.UUID,
    text: str,
    issue_keys: Sequence[str],
    participant_entity_ids: Sequence[uuid.UUID] = (),
    event_date: date | None = None,
    window_days: int = 365,
    limit: int = 20,
    candidate_limit: int = 500,
    include_rejected: bool = True,
    gateway: EmbeddingGateway | None = None,
) -> list[RecalledFact]:
    """Return up to ``limit`` facts most similar to ``text`` among facts sharing an issue.

    Rejected facts are included (flagged) when ``include_rejected`` so a caller can avoid re-creating them.
    Undated facts are always candidates; dated facts must fall within ``window_days`` of ``event_date``.
    """

    if not text.strip() or not issue_keys:
        return []
    gateway = gateway or get_embedding_gateway()
    statuses = ["WORKING", "CONFIRMED", *(["REJECTED"] if include_rejected else [])]
    statement = fact_filter_statement(
        db,
        matter_id=matter_id,
        filters=FactFilters(statuses=statuses, issue_keys=list(issue_keys)),
    )
    if event_date is not None:
        window = timedelta(days=window_days)
        statement = statement.where(
            or_(
                MatterFact.event_date_start.is_(None),
                MatterFact.event_date_start.between(event_date - window, event_date + window),
            )
        )
    candidates = list(db.scalars(statement.order_by(*FACT_DATE_ORDER).limit(candidate_limit)))
    if not candidates:
        return []
    ensure_summary_embeddings(db, candidates, gateway)
    query_vector = gateway.embed([" ".join(text.split())], "document").embeddings[0]

    wanted = set(canonical_ids(db, participant_entity_ids).values())
    shared: dict[uuid.UUID, int] = {}
    if wanted:
        links = db.execute(
            select(MatterFactEntity.fact_id, MatterFactEntity.entity_id).where(
                MatterFactEntity.fact_id.in_([fact.id for fact in candidates])
            )
        ).all()
        owner_links = [(fact.id, fact.owner_entity_id) for fact in candidates if fact.owner_entity_id is not None]
        canonical = canonical_ids(db, [entity_id for _, entity_id in [*links, *owner_links]])
        by_fact: dict[uuid.UUID, set[uuid.UUID]] = {}
        for fact_id, entity_id in [*links, *owner_links]:
            by_fact.setdefault(fact_id, set()).add(canonical.get(entity_id, entity_id))
        for fact in candidates:
            shared[fact.id] = len(wanted & by_fact.get(fact.id, set()))
    recalled = [
        RecalledFact(
            fact=fact,
            similarity=_cosine(query_vector, fact.summary_embedding or []),
            shared_participants=shared.get(fact.id, 0),
            rejected=fact.status == "REJECTED",
        )
        for fact in candidates
        if fact.summary_embedding
    ]
    recalled.sort(key=lambda item: (-item.similarity, -item.shared_participants, str(item.fact.id)))
    return recalled[:limit]
