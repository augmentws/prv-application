"""Duplicate control for agent-proposed facts.

Before a CREATE is applied, its summary is compared with existing facts that share an issue and fall in a
compatible date window (rejected facts included as tombstones). When a candidate is close enough, the
``fact_consolidation`` skill decides SAME or DISTINCT. SAME becomes evidence on the existing fact, or is blocked
when that fact was rejected. Decisions are made without holding locks; the caller then takes per-issue advisory
locks and calls ``late_candidates`` to catch facts created concurrently after the decision.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.fact_extraction import CreateDecision, ExtractionOperation
from app.fact_recall import RecalledFact, recall_facts

MAX_CANDIDATES = 5
# Clock skew allowance between workers when looking for facts created after a decision.
RECHECK_MARGIN = timedelta(seconds=5)

AskConsolidator = Callable[[dict[str, Any], list[dict[str, Any]]], dict[str, Any]]


@dataclass(frozen=True)
class ConsolidationSettings:
    matter_id: uuid.UUID
    task_id: uuid.UUID
    threshold: float
    window_days: int


def _event_date(operation: ExtractionOperation) -> date | None:
    try:
        return date.fromisoformat(operation.event_date_start) if operation.event_date_start else None
    except ValueError:
        return None


def duplicate_candidates(
    db: Session,
    settings: ConsolidationSettings,
    operation: ExtractionOperation,
    *,
    created_after: datetime | None = None,
) -> list[RecalledFact]:
    if not operation.summary or not operation.issue_keys:
        return []
    recalled = recall_facts(
        db,
        matter_id=settings.matter_id,
        text=operation.summary,
        issue_keys=list(dict.fromkeys(operation.issue_keys)),
        event_date=_event_date(operation),
        window_days=settings.window_days,
        limit=MAX_CANDIDATES,
        include_rejected=True,
        created_after=created_after,
    )
    return [item for item in recalled if item.similarity >= settings.threshold]


def _proposed(operation: ExtractionOperation) -> dict[str, Any]:
    return {
        "summary": operation.summary,
        "date_precision": operation.date_precision,
        "event_date_start": operation.event_date_start,
        "event_date_end": operation.event_date_end,
        "issue_keys": operation.issue_keys,
        "owner": operation.owner.model_dump() if operation.owner else None,
        "quotes": [item.quote for item in operation.evidence][:5],
    }


def _candidate(item: RecalledFact) -> dict[str, Any]:
    fact = item.fact
    return {
        "fact_id": str(fact.id),
        "status": fact.status,
        "summary": fact.summary,
        "date_precision": fact.date_precision,
        "event_date_start": fact.event_date_start.isoformat() if fact.event_date_start else None,
        "event_date_end": fact.event_date_end.isoformat() if fact.event_date_end else None,
        "similarity": round(item.similarity, 4),
    }


def decide(
    db: Session,
    settings: ConsolidationSettings,
    operation: ExtractionOperation,
    ask: AskConsolidator,
    *,
    created_after: datetime | None = None,
) -> CreateDecision:
    candidates = duplicate_candidates(db, settings, operation, created_after=created_after)
    if not candidates:
        return CreateDecision("CREATE")
    by_id = {str(item.fact.id): item for item in candidates}
    considered = frozenset(item.fact.id for item in candidates)
    output = ask(_proposed(operation), [_candidate(item) for item in candidates])
    chosen = by_id.get(output.get("fact_id") or "") if output.get("decision") == "SAME" else None
    if chosen is None:
        # DISTINCT, or a SAME naming a fact that was not offered: never guess a target.
        return CreateDecision("CREATE", rationale=output.get("rationale"), considered_fact_ids=considered)
    return CreateDecision(
        "BLOCKED_BY_REJECTED" if chosen.rejected else "ADD_TO_EXISTING",
        fact_id=chosen.fact.id,
        similarity=chosen.similarity,
        rationale=output.get("rationale"),
        considered_fact_ids=considered,
    )


def late_candidates(
    db: Session,
    settings: ConsolidationSettings,
    operation: ExtractionOperation,
    decision: CreateDecision,
    *,
    decided_at: datetime,
) -> list[RecalledFact]:
    """Close facts created around or after a ruling that the ruling did not already compare against."""

    return [
        item
        for item in duplicate_candidates(db, settings, operation, created_after=decided_at - RECHECK_MARGIN)
        if item.fact.id not in decision.considered_fact_ids
    ]


def consolidation_validator(output: dict[str, Any]) -> None:
    if output.get("decision") == "SAME" and not output.get("fact_id"):
        raise ValueError("A SAME decision must name the matching candidate's fact_id")


def lock_issue_partitions(db: Session, settings: ConsolidationSettings, issue_keys: Iterable[str]) -> None:
    """Serialize fact creation per matter, task and issue; keys are locked in sorted order to avoid deadlocks."""

    if db.get_bind().dialect.name != "postgresql":
        return
    for issue_key in sorted(set(issue_keys)):
        digest = hashlib.sha256(f"matter-fact-issue:{settings.matter_id}:{settings.task_id}:{issue_key}".encode()).digest()
        lock_key = int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)
        db.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})

