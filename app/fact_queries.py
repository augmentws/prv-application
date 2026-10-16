"""The controlled fact filter, facets, date histogram, and date ordering.

One filter model is shared by the fact list, search, facets, the timeline histogram, and CSV export, so all of
them always agree. Everything runs on the Core ledger tables; evidence-document filters read only Core data
(``matter_document_custodian`` and ``document_metadata_current``). Results are ordered by event date, oldest first;
within a date, coarser precision first; then creation time and ID; undated facts last.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import ColumnElement, Select, and_, case, func, literal, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Custodian,
    DocumentMetadataCurrent,
    MatterAnalysisTask,
    MatterAnalysisTaskVersion,
    MatterDocumentCustodian,
    MatterEntity,
    MatterFact,
    MatterFactEntity,
    MatterFactEvidence,
    MatterFactIssue,
    MetadataDefinition,
    User,
)

FactStatusValue = Literal["WORKING", "CONFIRMED", "REJECTED", "MERGED"]
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
FACT_SORTS = {
    "DATE_ASC": FACT_DATE_ORDER,
    "DATE_DESC": (
        MatterFact.event_date_start.is_(None),
        MatterFact.event_date_start.desc(),
        PRECISION_ORDER,
        MatterFact.created_at,
        MatterFact.id,
    ),
    "UPDATED_DESC": (MatterFact.updated_at.desc(), MatterFact.id),
    "EVIDENCE_COUNT_DESC": (MatterFact.evidence_count.desc(), *FACT_DATE_ORDER),
}
FactSort = Literal["DATE_ASC", "DATE_DESC", "UPDATED_DESC", "EVIDENCE_COUNT_DESC"]
FacetName = Annotated[
    str,
    StringConstraints(
        pattern=(
            r"^(status|issue_key|owner|participant|entity_type|date_precision|date_basis|key_fact|has_contradiction"
            r"|author_type|confirmed_by|stance|evidence_custodian|evidence_metadata:[a-z][a-z0-9_]{0,99})$"
        )
    ),
]
FACET_FILTER_FIELDS = {
    "status": "statuses",
    "issue_key": "issue_keys",
    "owner": "owner_entity_ids",
    "participant": "participant_entity_ids",
    "entity_type": "entity_types",
    "date_precision": "date_precisions",
    "date_basis": "date_bases",
    "key_fact": "key_fact",
    "has_contradiction": "has_contradiction",
    "author_type": "author_types",
    "confirmed_by": "confirmed_by_user_ids",
    "stance": "stances",
    "evidence_custodian": "evidence_custodian_ids",
}
MAX_IDS = 500


class FactFilterError(ValueError):
    pass


class FactFilterNotFound(FactFilterError):
    pass


class EvidenceMetadataFilter(BaseModel):
    """Match facts with an active quote from a document whose Core metadata field matches."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_key: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,99}$")]
    values: list[str | bool] = Field(default_factory=list, max_length=200)
    date_from: date | None = None
    date_to: date | None = None
    number_from: float | None = None
    number_to: float | None = None

    @model_validator(mode="after")
    def require_condition(self) -> EvidenceMetadataFilter:
        if not (self.values or self.date_from or self.date_to or self.number_from is not None or self.number_to is not None):
            raise ValueError(f"Evidence field {self.field_key} needs values or a range")
        return self


class FactFilters(BaseModel):
    """Every fact filter. Empty lists and ``None`` mean "no restriction"; lists are ORed, fields are ANDed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str | None = Field(default=None, max_length=500)
    statuses: list[FactStatusValue] = Field(default_factory=list, max_length=4)
    issue_keys: list[str] = Field(default_factory=list, max_length=200)
    entity_id: uuid.UUID | None = None
    owner_entity_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_IDS)
    participant_entity_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_IDS)
    participant_role: Literal["PARTICIPANT_ACTOR", "PARTICIPANT_RECIPIENT"] | None = None
    entity_types: list[Literal["PERSON", "ORGANIZATION"]] = Field(default_factory=list, max_length=2)
    event_date_from: date | None = None
    event_date_to: date | None = None
    date_precisions: list[Literal["DAY", "MONTH", "QUARTER", "YEAR", "RANGE", "UNKNOWN"]] = Field(
        default_factory=list, max_length=6
    )
    date_bases: list[Literal["STATED_IN_TEXT", "DOCUMENT_DATE", "INFERRED"]] = Field(
        default_factory=list, max_length=3
    )
    undated: bool | None = None
    key_fact: bool | None = None
    has_contradiction: bool | None = None
    author_types: list[Literal["HUMAN", "AGENT"]] = Field(default_factory=list, max_length=2)
    origin_workflow_run_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_IDS)
    confirmed_by_user_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_IDS)
    confirmed_from: datetime | None = None
    confirmed_to: datetime | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    updated_since: datetime | None = None
    evidence_count_min: int | None = Field(default=None, ge=0)
    evidence_count_max: int | None = Field(default=None, ge=0)
    document_count_min: int | None = Field(default=None, ge=0)
    document_count_max: int | None = Field(default=None, ge=0)
    stances: list[Literal["SUPPORTS", "CONTRADICTS"]] = Field(default_factory=list, max_length=2)
    matter_document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_IDS)
    evidence_custodian_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_IDS)
    evidence_metadata: list[EvidenceMetadataFilter] = Field(default_factory=list, max_length=20)

    def snapshot(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_defaults=True)

    @classmethod
    def from_snapshot(cls, value: dict[str, Any]) -> FactFilters:
        return cls.model_validate(value)

    def without(self, facet: str) -> FactFilters:
        """The same filter with one facet's own selection removed, for self-excluding facet counts."""

        if facet.startswith("evidence_metadata:"):
            key = facet.split(":", 1)[1]
            return self.model_copy(
                update={"evidence_metadata": [item for item in self.evidence_metadata if item.field_key != key]}
            )
        field_name = FACET_FILTER_FIELDS[facet]
        default = type(self).model_fields[field_name].get_default(call_default_factory=True)
        return self.model_copy(update={field_name: default})


def _like_pattern(value: str) -> str:
    escaped = value.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _canonical_group(db: Session, matter_id: uuid.UUID, entity_ids: list[uuid.UUID]) -> Select[tuple[uuid.UUID]]:
    """Every entity row (canonical and variant) belonging to the canonical entities of ``entity_ids``."""

    rows = db.execute(
        select(MatterEntity.id, MatterEntity.parent_id).where(
            MatterEntity.id.in_(entity_ids), MatterEntity.matter_id == matter_id
        )
    ).all()
    if len(rows) != len(set(entity_ids)):
        raise FactFilterNotFound("Entity not found")
    canonical = {parent_id or row_id for row_id, parent_id in rows}
    return select(MatterEntity.id).where(func.coalesce(MatterEntity.parent_id, MatterEntity.id).in_(canonical))


def _active_evidence_fact_ids(*conditions: ColumnElement[bool]) -> Select[tuple[uuid.UUID]]:
    return select(MatterFactEvidence.fact_id).where(MatterFactEvidence.status == "ACTIVE", *conditions)


def _evidence_metadata_condition(
    db: Session, matter_id: uuid.UUID, item: EvidenceMetadataFilter
) -> ColumnElement[bool]:
    definition = db.scalar(
        select(MetadataDefinition).where(
            MetadataDefinition.matter_id == matter_id, MetadataDefinition.key == item.field_key
        )
    )
    if definition is None:
        raise FactFilterNotFound(f"Document field {item.field_key} was not found")
    conditions: list[ColumnElement[bool]] = [
        DocumentMetadataCurrent.metadata_definition_id == definition.id,
        DocumentMetadataCurrent.resolution_state == "VALUE",
    ]
    if definition.type in {"TEXT", "LONG_TEXT", "ENUM"}:
        if not item.values or any(not isinstance(value, str) for value in item.values):
            raise FactFilterError(f"{item.field_key} is filtered by text values")
        conditions.append(DocumentMetadataCurrent.value_text.in_(item.values))
    elif definition.type == "BOOLEAN":
        if not item.values or any(not isinstance(value, bool) for value in item.values):
            raise FactFilterError(f"{item.field_key} is filtered by true or false")
        conditions.append(DocumentMetadataCurrent.value_boolean.in_(item.values))
    elif definition.type in {"DATE", "DATETIME"}:
        if item.date_from is None and item.date_to is None:
            raise FactFilterError(f"{item.field_key} is filtered by a date range")
        column = DocumentMetadataCurrent.value_date if definition.type == "DATE" else DocumentMetadataCurrent.value_datetime
        if item.date_from is not None:
            conditions.append(column >= item.date_from)
        if item.date_to is not None:
            bound = item.date_to if definition.type == "DATE" else datetime.combine(item.date_to, datetime.max.time())
            conditions.append(column <= bound)
    elif definition.type in {"INTEGER", "DECIMAL"}:
        if item.number_from is None and item.number_to is None:
            raise FactFilterError(f"{item.field_key} is filtered by a number range")
        column = DocumentMetadataCurrent.value_long if definition.type == "INTEGER" else DocumentMetadataCurrent.value_float
        if item.number_from is not None:
            conditions.append(column >= item.number_from)
        if item.number_to is not None:
            conditions.append(column <= item.number_to)
    else:
        raise FactFilterError(f"{item.field_key} cannot be used as a fact filter")
    documents = select(DocumentMetadataCurrent.matter_document_id).where(*conditions)
    return MatterFact.id.in_(_active_evidence_fact_ids(MatterFactEvidence.matter_document_id.in_(documents)))


def fact_filter_statement(db: Session, *, matter_id: uuid.UUID, filters: FactFilters) -> Select[tuple[MatterFact]]:
    """Return an unordered ``SELECT matter_fact`` for the filter; merged facts are excluded unless requested."""

    statement = select(MatterFact).where(MatterFact.matter_id == matter_id)
    if filters.statuses:
        statement = statement.where(MatterFact.status.in_(filters.statuses))
    else:
        statement = statement.where(MatterFact.status != "MERGED")
    if filters.query and filters.query.strip():
        pattern = _like_pattern(" ".join(filters.query.split()))
        statement = statement.where(
            or_(
                func.lower(MatterFact.summary).like(pattern, escape="\\"),
                MatterFact.id.in_(
                    _active_evidence_fact_ids(func.lower(MatterFactEvidence.quote).like(pattern, escape="\\"))
                ),
            )
        )
    if filters.issue_keys:
        statement = statement.where(
            MatterFact.id.in_(select(MatterFactIssue.fact_id).where(MatterFactIssue.issue_key.in_(filters.issue_keys)))
        )
    if filters.entity_id is not None:
        group = _canonical_group(db, matter_id, [filters.entity_id])
        statement = statement.where(
            or_(
                MatterFact.owner_entity_id.in_(group),
                MatterFact.id.in_(select(MatterFactEntity.fact_id).where(MatterFactEntity.entity_id.in_(group))),
            )
        )
    if filters.owner_entity_ids:
        statement = statement.where(
            MatterFact.owner_entity_id.in_(_canonical_group(db, matter_id, filters.owner_entity_ids))
        )
    if filters.participant_entity_ids or filters.participant_role:
        participant = select(MatterFactEntity.fact_id)
        if filters.participant_entity_ids:
            participant = participant.where(
                MatterFactEntity.entity_id.in_(_canonical_group(db, matter_id, filters.participant_entity_ids))
            )
        if filters.participant_role:
            participant = participant.where(MatterFactEntity.role == filters.participant_role)
        statement = statement.where(MatterFact.id.in_(participant))
    if filters.entity_types:
        typed = select(MatterEntity.id).where(
            MatterEntity.matter_id == matter_id, MatterEntity.entity_type.in_(filters.entity_types)
        )
        statement = statement.where(
            or_(
                MatterFact.owner_entity_id.in_(typed),
                MatterFact.id.in_(select(MatterFactEntity.fact_id).where(MatterFactEntity.entity_id.in_(typed))),
            )
        )
    if filters.event_date_from is not None:
        statement = statement.where(
            func.coalesce(MatterFact.event_date_end, MatterFact.event_date_start) >= filters.event_date_from
        )
    if filters.event_date_to is not None:
        statement = statement.where(MatterFact.event_date_start <= filters.event_date_to)
    if filters.date_precisions:
        statement = statement.where(MatterFact.date_precision.in_(filters.date_precisions))
    if filters.date_bases:
        statement = statement.where(MatterFact.date_basis.in_(filters.date_bases))
    if filters.undated is not None:
        statement = statement.where(
            MatterFact.event_date_start.is_(None) if filters.undated else MatterFact.event_date_start.is_not(None)
        )
    if filters.key_fact is not None:
        statement = statement.where(MatterFact.key_fact.is_(filters.key_fact))
    if filters.has_contradiction is not None:
        statement = statement.where(MatterFact.has_contradiction.is_(filters.has_contradiction))
    if filters.author_types:
        conditions = []
        if "HUMAN" in filters.author_types:
            conditions.append(MatterFact.origin_workflow_run_id.is_(None))
        if "AGENT" in filters.author_types:
            conditions.append(MatterFact.origin_workflow_run_id.is_not(None))
        statement = statement.where(or_(*conditions))
    if filters.origin_workflow_run_ids:
        statement = statement.where(MatterFact.origin_workflow_run_id.in_(filters.origin_workflow_run_ids))
    if filters.confirmed_by_user_ids:
        statement = statement.where(MatterFact.confirmed_by_user_id.in_(filters.confirmed_by_user_ids))
    if filters.confirmed_from is not None:
        statement = statement.where(MatterFact.confirmed_at >= filters.confirmed_from)
    if filters.confirmed_to is not None:
        statement = statement.where(MatterFact.confirmed_at <= filters.confirmed_to)
    if filters.created_from is not None:
        statement = statement.where(MatterFact.created_at >= filters.created_from)
    if filters.created_to is not None:
        statement = statement.where(MatterFact.created_at <= filters.created_to)
    if filters.updated_since is not None:
        statement = statement.where(MatterFact.updated_at > filters.updated_since)
    for column, minimum, maximum in (
        (MatterFact.evidence_count, filters.evidence_count_min, filters.evidence_count_max),
        (MatterFact.document_count, filters.document_count_min, filters.document_count_max),
    ):
        if minimum is not None:
            statement = statement.where(column >= minimum)
        if maximum is not None:
            statement = statement.where(column <= maximum)
    if filters.stances:
        statement = statement.where(
            MatterFact.id.in_(_active_evidence_fact_ids(MatterFactEvidence.stance.in_(filters.stances)))
        )
    if filters.matter_document_ids:
        statement = statement.where(
            MatterFact.id.in_(
                _active_evidence_fact_ids(MatterFactEvidence.matter_document_id.in_(filters.matter_document_ids))
            )
        )
    if filters.evidence_custodian_ids:
        documents = select(MatterDocumentCustodian.matter_document_id).where(
            MatterDocumentCustodian.custodian_id.in_(filters.evidence_custodian_ids)
        )
        statement = statement.where(
            MatterFact.id.in_(_active_evidence_fact_ids(MatterFactEvidence.matter_document_id.in_(documents)))
        )
    for item in filters.evidence_metadata:
        statement = statement.where(_evidence_metadata_condition(db, matter_id, item))
    return statement


def _filtered_ids(db: Session, matter_id: uuid.UUID, filters: FactFilters):
    return fact_filter_statement(db, matter_id=matter_id, filters=filters).with_only_columns(MatterFact.id)


def _counted(rows: list[tuple[Any, int]]) -> list[tuple[Any, int]]:
    return [(value, int(count)) for value, count in rows if value is not None]


def fact_facet(
    db: Session,
    *,
    matter_id: uuid.UUID,
    filters: FactFilters,
    facet: str,
    size: int = 50,
) -> list[dict[str, Any]]:
    """Distinct-fact counts per value of one facet, excluding that facet's own selection."""

    ids = _filtered_ids(db, matter_id, filters.without(facet))
    distinct_facts = func.count(func.distinct(MatterFact.id))
    canonical = func.coalesce(MatterEntity.parent_id, MatterEntity.id)
    labels: dict[Any, str] = {}
    if facet in {"status", "date_precision", "date_basis", "key_fact", "has_contradiction"}:
        column = {
            "status": MatterFact.status,
            "date_precision": MatterFact.date_precision,
            "date_basis": MatterFact.date_basis,
            "key_fact": MatterFact.key_fact,
            "has_contradiction": MatterFact.has_contradiction,
        }[facet]
        rows = db.execute(select(column, distinct_facts).where(MatterFact.id.in_(ids)).group_by(column)).all()
    elif facet == "author_type":
        author = case((MatterFact.origin_workflow_run_id.is_(None), literal("HUMAN")), else_=literal("AGENT"))
        rows = db.execute(select(author, distinct_facts).where(MatterFact.id.in_(ids)).group_by(author)).all()
    elif facet == "confirmed_by":
        rows = db.execute(
            select(MatterFact.confirmed_by_user_id, distinct_facts)
            .where(MatterFact.id.in_(ids))
            .group_by(MatterFact.confirmed_by_user_id)
        ).all()
        user_ids = [value for value, _ in rows if value is not None]
        labels = dict(db.execute(select(User.id, User.display_name).where(User.id.in_(user_ids))).all()) if user_ids else {}
    elif facet == "issue_key":
        rows = db.execute(
            select(MatterFactIssue.issue_key, func.count(func.distinct(MatterFactIssue.fact_id)))
            .where(MatterFactIssue.fact_id.in_(ids))
            .group_by(MatterFactIssue.issue_key)
        ).all()
        labels = issue_names(db, matter_id)
    elif facet == "owner":
        rows = db.execute(
            select(canonical, distinct_facts)
            .join(MatterEntity, MatterEntity.id == MatterFact.owner_entity_id)
            .where(MatterFact.id.in_(ids))
            .group_by(canonical)
        ).all()
        labels = _entity_names(db, [value for value, _ in rows])
    elif facet == "participant":
        statement = (
            select(canonical, func.count(func.distinct(MatterFactEntity.fact_id)))
            .join(MatterEntity, MatterEntity.id == MatterFactEntity.entity_id)
            .where(MatterFactEntity.fact_id.in_(ids))
        )
        if filters.participant_role:
            statement = statement.where(MatterFactEntity.role == filters.participant_role)
        rows = db.execute(statement.group_by(canonical)).all()
        labels = _entity_names(db, [value for value, _ in rows])
    elif facet == "entity_type":
        owners = select(MatterFact.id.label("fact_id"), MatterEntity.entity_type.label("entity_type")).join(
            MatterEntity, MatterEntity.id == MatterFact.owner_entity_id
        )
        participants = select(
            MatterFactEntity.fact_id.label("fact_id"), MatterEntity.entity_type.label("entity_type")
        ).join(MatterEntity, MatterEntity.id == MatterFactEntity.entity_id)
        links = owners.union(participants).subquery()
        rows = db.execute(
            select(links.c.entity_type, func.count(func.distinct(links.c.fact_id)))
            .where(links.c.fact_id.in_(ids))
            .group_by(links.c.entity_type)
        ).all()
    elif facet == "stance":
        rows = db.execute(
            select(MatterFactEvidence.stance, func.count(func.distinct(MatterFactEvidence.fact_id)))
            .where(MatterFactEvidence.fact_id.in_(ids), MatterFactEvidence.status == "ACTIVE")
            .group_by(MatterFactEvidence.stance)
        ).all()
    elif facet == "evidence_custodian":
        rows = db.execute(
            select(MatterDocumentCustodian.custodian_id, func.count(func.distinct(MatterFactEvidence.fact_id)))
            .join(
                MatterDocumentCustodian,
                MatterDocumentCustodian.matter_document_id == MatterFactEvidence.matter_document_id,
            )
            .where(MatterFactEvidence.fact_id.in_(ids), MatterFactEvidence.status == "ACTIVE")
            .group_by(MatterDocumentCustodian.custodian_id)
        ).all()
        custodian_ids = [value for value, _ in rows]
        labels = (
            dict(db.execute(select(Custodian.id, Custodian.display_name).where(Custodian.id.in_(custodian_ids))).all())
            if custodian_ids
            else {}
        )
    elif facet.startswith("evidence_metadata:"):
        key = facet.split(":", 1)[1]
        definition = db.scalar(
            select(MetadataDefinition).where(MetadataDefinition.matter_id == matter_id, MetadataDefinition.key == key)
        )
        if definition is None:
            raise FactFilterNotFound(f"Document field {key} was not found")
        if definition.type not in {"TEXT", "ENUM", "BOOLEAN"}:
            raise FactFilterError(f"{key} cannot be faceted; filter it by range instead")
        value_column = (
            DocumentMetadataCurrent.value_boolean if definition.type == "BOOLEAN" else DocumentMetadataCurrent.value_text
        )
        rows = db.execute(
            select(value_column, func.count(func.distinct(MatterFactEvidence.fact_id)))
            .join(
                DocumentMetadataCurrent,
                and_(
                    DocumentMetadataCurrent.matter_document_id == MatterFactEvidence.matter_document_id,
                    DocumentMetadataCurrent.metadata_definition_id == definition.id,
                    DocumentMetadataCurrent.resolution_state == "VALUE",
                ),
            )
            .where(MatterFactEvidence.fact_id.in_(ids), MatterFactEvidence.status == "ACTIVE")
            .group_by(value_column)
        ).all()
        if definition.type == "ENUM":
            labels = {
                str(option["key"]): str(option.get("label") or option["key"])
                for option in definition.allowed_values or []
                if isinstance(option, dict) and option.get("key")
            }
    else:
        raise FactFilterError(f"Unknown facet: {facet}")
    buckets = [
        {"value": value, "label": labels.get(value, str(value)), "count": count}
        for value, count in _counted(rows)
    ]
    buckets.sort(key=lambda bucket: (-bucket["count"], str(bucket["label"])))
    return buckets[:size]


def _entity_names(db: Session, entity_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not entity_ids:
        return {}
    return dict(db.execute(select(MatterEntity.id, MatterEntity.display_name).where(MatterEntity.id.in_(entity_ids))).all())


def issue_names(db: Session, matter_id: uuid.UUID) -> dict[str, str]:
    """Issue key to display name across the published catalogs of the matter's fact-development tasks."""

    from app.fact_specifications import FactSpecificationError, parse_fact_development_specification

    names: dict[str, str] = {}
    rows = db.execute(
        select(MatterAnalysisTaskVersion.fact_development_specification)
        .join(MatterAnalysisTask, MatterAnalysisTask.id == MatterAnalysisTaskVersion.matter_analysis_task_id)
        .where(
            MatterAnalysisTask.matter_id == matter_id,
            MatterAnalysisTask.task_type == "FACT_DEVELOPMENT",
            MatterAnalysisTaskVersion.version == MatterAnalysisTask.published_version,
        )
    ).scalars()
    for value in rows:
        try:
            specification = parse_fact_development_specification(value)
        except FactSpecificationError:
            continue
        for issue in specification.issues:
            names.setdefault(issue.key, issue.name)
    return names


def _bucket_start(value: date, interval: str) -> date:
    if interval == "YEAR":
        return date(value.year, 1, 1)
    if interval == "QUARTER":
        return date(value.year, ((value.month - 1) // 3) * 3 + 1, 1)
    return date(value.year, value.month, 1)


def fact_date_histogram(
    db: Session,
    *,
    matter_id: uuid.UUID,
    filters: FactFilters,
    interval: Literal["YEAR", "QUARTER", "MONTH"],
) -> dict[str, Any]:
    """Fact counts per period of the event start date, plus the number of undated facts."""

    ids = _filtered_ids(db, matter_id, filters)
    counts: dict[date, int] = defaultdict(int)
    undated = 0
    for start, count in db.execute(
        select(MatterFact.event_date_start, func.count(MatterFact.id))
        .where(MatterFact.id.in_(ids))
        .group_by(MatterFact.event_date_start)
    ).all():
        if start is None:
            undated += int(count)
        else:
            counts[_bucket_start(start, interval)] += int(count)
    return {
        "interval": interval,
        "buckets": [{"start": start, "count": counts[start]} for start in sorted(counts)],
        "undated_count": undated,
    }
