"""The controlled fact filter and date ordering shared by the fact list and fact exports.

An export therefore contains exactly the facts the same filter lists, in the same order: by event date, oldest
first; within a date, coarser precision first; then creation time and ID; undated facts last.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, case, or_, select
from sqlalchemy.orm import Session

from app.models import MatterEntity, MatterFact, MatterFactEntity, MatterFactEvidence, MatterFactIssue

FACT_STATUSES = frozenset({"WORKING", "CONFIRMED", "REJECTED", "MERGED"})
PRECISION_ORDER = case(
    {"YEAR": 0, "QUARTER": 1, "MONTH": 2, "RANGE": 3, "DAY": 4},
    value=MatterFact.date_precision,
    else_=5,
)
FACT_DATE_ORDER = (
    MatterFact.event_date_start.is_(None),
    MatterFact.event_date_start,
    PRECISION_ORDER,
    MatterFact.created_at,
    MatterFact.id,
)


class FactFilterError(ValueError):
    pass


class FactFilterNotFound(FactFilterError):
    pass


@dataclass(frozen=True)
class FactFilters:
    statuses: tuple[str, ...] = ()
    issue_keys: tuple[str, ...] = ()
    entity_id: uuid.UUID | None = None
    matter_document_id: uuid.UUID | None = None
    updated_since: datetime | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "statuses": sorted(self.statuses),
            "issue_keys": sorted(self.issue_keys),
            "entity_id": str(self.entity_id) if self.entity_id else None,
            "matter_document_id": str(self.matter_document_id) if self.matter_document_id else None,
            "updated_since": self.updated_since.isoformat() if self.updated_since else None,
        }

    @classmethod
    def from_snapshot(cls, value: dict[str, Any]) -> FactFilters:
        return cls(
            statuses=tuple(value.get("statuses") or ()),
            issue_keys=tuple(value.get("issue_keys") or ()),
            entity_id=uuid.UUID(value["entity_id"]) if value.get("entity_id") else None,
            matter_document_id=uuid.UUID(value["matter_document_id"]) if value.get("matter_document_id") else None,
            updated_since=datetime.fromisoformat(value["updated_since"]) if value.get("updated_since") else None,
        )


def fact_filter_statement(db: Session, *, matter_id: uuid.UUID, filters: FactFilters) -> Select[tuple[MatterFact]]:
    """Return an unordered ``SELECT matter_fact`` for the filter; merged facts are excluded unless requested."""

    unknown = set(filters.statuses) - FACT_STATUSES
    if unknown:
        raise FactFilterError("Unknown fact statuses: " + ", ".join(sorted(unknown)))
    statement = select(MatterFact).where(MatterFact.matter_id == matter_id)
    if filters.statuses:
        statement = statement.where(MatterFact.status.in_(filters.statuses))
    else:
        statement = statement.where(MatterFact.status != "MERGED")
    if filters.issue_keys:
        statement = statement.where(
            MatterFact.id.in_(select(MatterFactIssue.fact_id).where(MatterFactIssue.issue_key.in_(filters.issue_keys)))
        )
    if filters.entity_id is not None:
        anchor = db.get(MatterEntity, filters.entity_id)
        if anchor is None or anchor.matter_id != matter_id:
            raise FactFilterNotFound("Entity not found")
        canonical = anchor.parent_id or anchor.id
        group = select(MatterEntity.id).where(or_(MatterEntity.id == canonical, MatterEntity.parent_id == canonical))
        statement = statement.where(
            or_(
                MatterFact.owner_entity_id.in_(group),
                MatterFact.id.in_(select(MatterFactEntity.fact_id).where(MatterFactEntity.entity_id.in_(group))),
            )
        )
    if filters.matter_document_id is not None:
        statement = statement.where(
            MatterFact.id.in_(
                select(MatterFactEvidence.fact_id).where(
                    MatterFactEvidence.matter_document_id == filters.matter_document_id,
                    MatterFactEvidence.status == "ACTIVE",
                )
            )
        )
    if filters.updated_since is not None:
        statement = statement.where(MatterFact.updated_at > filters.updated_since)
    return statement
