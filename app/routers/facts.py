import uuid
from datetime import datetime
from typing import Never

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.authorization import ResourceRef, can
from app.database import get_db
from app.dependencies import Principal, get_principal
from app.fact_evidence import FactEvidenceError, verify_evidence
from app.fact_queries import (
    FACT_DATE_ORDER,
    FACT_SORTS,
    FactFilterError,
    FactFilterNotFound,
    FactFilters,
    fact_date_histogram,
    fact_facet,
    fact_filter_statement,
)
from app.facts import (
    Actor,
    FactConflict,
    FactContent,
    FactError,
    FactIssueRef,
    FactNotFound,
    FactParticipantRef,
    accept_suggestion,
    add_evidence,
    confirm_fact,
    create_fact,
    dismiss_suggestion,
    get_fact,
    merge_facts,
    reject_fact,
    remove_evidence,
    revise_fact,
    unmerge_fact,
)
from app.models import (
    DocumentMetadataCurrent,
    Matter,
    MatterDocument,
    MatterEntity,
    MatterFact,
    MatterFactEntity,
    MatterFactEvent,
    MatterFactEvidence,
    MatterFactIssue,
    MatterFactRevision,
    MatterFactSuggestion,
    MetadataDefinition,
)
from app.schemas import (
    FactEvidenceCreate,
    FactIssueRefModel,
    MatterDocumentFactRead,
    MatterFactAction,
    MatterFactCreate,
    MatterFactDetailRead,
    MatterFactEntityRef,
    MatterFactEventRead,
    MatterFactEvidenceAdd,
    MatterFactEvidenceRead,
    MatterFactFacetRead,
    MatterFactFacetRequest,
    MatterFactHistogramRead,
    MatterFactHistogramRequest,
    MatterFactMerge,
    MatterFactPage,
    MatterFactRead,
    MatterFactRevisionRead,
    MatterFactSearchRequest,
    MatterFactSuggestionAccept,
    MatterFactSuggestionDismiss,
    MatterFactSuggestionRead,
    MatterFactUpdate,
)

router = APIRouter(prefix="/v1/matters/{matter_id}/facts", tags=["matter facts"])
document_router = APIRouter(prefix="/v1/matters/{matter_id}/documents", tags=["matter facts"])

def _matter(db: Session, principal: Principal, matter_id: uuid.UUID, *permission_keys: str) -> Matter:
    """Facts quote documents, so every fact permission also requires ``document.view``."""

    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    for permission_key in (*permission_keys, "document.view"):
        if not can(db, principal, permission_key, ResourceRef.matter(matter.id)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission required: {permission_key}",
            )
    return matter


def _raise(exc: FactError | FactEvidenceError) -> Never:
    if isinstance(exc, FactNotFound):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, FactConflict):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


def _actor(principal: Principal) -> Actor:
    return Actor("HUMAN", principal.user.id)


def _audit(db: Session, matter: Matter, principal: Principal, action: str, fact: MatterFact, details: dict) -> None:
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action=f"matter_fact.{action}",
        target_type="matter_fact",
        target_id=fact.id,
        details={"matter_id": str(matter.id), **details},
    )


def _verified(db: Session, matter: Matter, principal: Principal, evidence: FactEvidenceCreate):
    return verify_evidence(
        db,
        matter=matter,
        matter_document_id=evidence.matter_document_id,
        paragraph_id=evidence.paragraph_id,
        quote=evidence.quote,
        occurrence=evidence.occurrence,
        stance=evidence.stance,
        actor_user_id=principal.user.id,
    )


def _entity_refs(db: Session, entity_ids: set[uuid.UUID]) -> dict[uuid.UUID, tuple[MatterEntity, MatterEntity]]:
    if not entity_ids:
        return {}
    rows = {entity.id: entity for entity in db.scalars(select(MatterEntity).where(MatterEntity.id.in_(entity_ids)))}
    parent_ids = {entity.parent_id for entity in rows.values() if entity.parent_id is not None} - set(rows)
    if parent_ids:
        rows.update(
            {entity.id: entity for entity in db.scalars(select(MatterEntity).where(MatterEntity.id.in_(parent_ids)))}
        )
    return {
        entity_id: (rows[entity_id], rows[rows[entity_id].parent_id or entity_id])
        for entity_id in entity_ids
        if entity_id in rows
    }


def _entity_ref(pair: tuple[MatterEntity, MatterEntity], role: str | None = None) -> MatterFactEntityRef:
    row, canonical = pair
    return MatterFactEntityRef(
        entity_id=row.id,
        canonical_entity_id=canonical.id,
        display_name=row.display_name,
        canonical_display_name=canonical.display_name,
        entity_type=canonical.entity_type,  # type: ignore[arg-type]
        role=role,  # type: ignore[arg-type]
    )


def _reads(db: Session, facts: list[MatterFact]) -> list[MatterFactRead]:
    fact_ids = [fact.id for fact in facts]
    if not fact_ids:
        return []
    issues: dict[uuid.UUID, list[FactIssueRefModel]] = {fact_id: [] for fact_id in fact_ids}
    for row in db.scalars(
        select(MatterFactIssue)
        .where(MatterFactIssue.fact_id.in_(fact_ids))
        .order_by(MatterFactIssue.matter_analysis_task_id, MatterFactIssue.issue_key)
    ):
        issues[row.fact_id].append(
            FactIssueRefModel(matter_analysis_task_id=row.matter_analysis_task_id, issue_key=row.issue_key)
        )
    participants = list(
        db.scalars(
            select(MatterFactEntity)
            .where(MatterFactEntity.fact_id.in_(fact_ids))
            .order_by(MatterFactEntity.role, MatterFactEntity.entity_id)
        )
    )
    refs = _entity_refs(
        db,
        {row.entity_id for row in participants} | {fact.owner_entity_id for fact in facts if fact.owner_entity_id},
    )
    pending: dict[uuid.UUID, int] = dict.fromkeys(fact_ids, 0)
    for source_id, target_id in db.execute(
        select(MatterFactSuggestion.fact_id, MatterFactSuggestion.target_fact_id).where(
            MatterFactSuggestion.status == "PENDING",
            or_(MatterFactSuggestion.fact_id.in_(fact_ids), MatterFactSuggestion.target_fact_id.in_(fact_ids)),
        )
    ):
        # A merge suggestion is pending review on both of its facts.
        for fact_id in {source_id, target_id}:
            if fact_id in pending:
                pending[fact_id] += 1
    by_fact: dict[uuid.UUID, list[MatterFactEntityRef]] = {fact_id: [] for fact_id in fact_ids}
    for row in participants:
        if row.entity_id in refs:
            by_fact[row.fact_id].append(_entity_ref(refs[row.entity_id], row.role))
    return [
        MatterFactRead(
            id=fact.id,
            matter_id=fact.matter_id,
            status=fact.status,  # type: ignore[arg-type]
            summary=fact.summary,
            event_date_start=fact.event_date_start,
            event_date_end=fact.event_date_end,
            date_precision=fact.date_precision,  # type: ignore[arg-type]
            date_basis=fact.date_basis,  # type: ignore[arg-type]
            owner=_entity_ref(refs[fact.owner_entity_id]) if fact.owner_entity_id in refs else None,
            participants=by_fact[fact.id],
            issues=issues[fact.id],
            key_fact=fact.key_fact,
            has_contradiction=fact.has_contradiction,
            evidence_count=fact.evidence_count,
            document_count=fact.document_count,
            pending_suggestion_count=pending[fact.id],
            merged_into_fact_id=fact.merged_into_fact_id,
            confirmed_by_user_id=fact.confirmed_by_user_id,
            confirmed_at=fact.confirmed_at,
            created_by_user_id=fact.created_by_user_id,
            origin_workflow_run_id=fact.origin_workflow_run_id,
            current_revision=fact.current_revision,
            lock_version=fact.lock_version,
            created_at=fact.created_at,
            updated_at=fact.updated_at,
        )
        for fact in facts
    ]


def _control_numbers(db: Session, matter_id: uuid.UUID, document_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not document_ids:
        return {}
    rows = db.execute(
        select(DocumentMetadataCurrent.matter_document_id, DocumentMetadataCurrent.value_text)
        .join(MetadataDefinition, MetadataDefinition.id == DocumentMetadataCurrent.metadata_definition_id)
        .where(
            MetadataDefinition.matter_id == matter_id,
            MetadataDefinition.key == "control_number",
            DocumentMetadataCurrent.matter_document_id.in_(document_ids),
            DocumentMetadataCurrent.value_text.is_not(None),
        )
    ).all()
    return {document_id: value for document_id, value in rows}


def _detail(db: Session, fact: MatterFact) -> MatterFactDetailRead:
    base = _reads(db, [fact])[0]
    evidence = list(
        db.scalars(
            select(MatterFactEvidence)
            .where(MatterFactEvidence.fact_id == fact.id)
            .order_by(MatterFactEvidence.status, MatterFactEvidence.created_at, MatterFactEvidence.id)
        )
    )
    control_numbers = _control_numbers(db, fact.matter_id, {row.matter_document_id for row in evidence})
    return MatterFactDetailRead(
        **base.model_dump(),
        evidence=[
            MatterFactEvidenceRead.model_validate(row).model_copy(
                update={"control_number": control_numbers.get(row.matter_document_id)}
            )
            for row in evidence
        ],
        revisions=[
            MatterFactRevisionRead.model_validate(row)
            for row in db.scalars(
                select(MatterFactRevision)
                .where(MatterFactRevision.fact_id == fact.id)
                .order_by(MatterFactRevision.revision.desc())
            )
        ],
        events=[
            MatterFactEventRead.model_validate(row)
            for row in db.scalars(
                select(MatterFactEvent)
                .where(MatterFactEvent.fact_id == fact.id)
                .order_by(MatterFactEvent.created_at.desc(), MatterFactEvent.id)
            )
        ],
    )


@router.get("", response_model=MatterFactPage)
def list_facts(
    matter_id: uuid.UUID,
    fact_status: list[str] = Query(default=[], alias="status"),
    issue_key: list[str] = Query(default=[]),
    entity_id: uuid.UUID | None = None,
    matter_document_id: uuid.UUID | None = None,
    updated_since: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterFactPage:
    """Facts in date order, oldest first. Merged facts are excluded unless requested by status.

    Full faceted search arrives with the facts index; this list supports the basic filters reviewers need now.
    """

    _matter(db, principal, matter_id, "fact.view")
    try:
        filters = FactFilters(
            statuses=fact_status,
            issue_keys=issue_key,
            entity_id=entity_id,
            matter_document_ids=[matter_document_id] if matter_document_id else [],
            updated_since=updated_since,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return _page(db, matter_id, filters, sort="DATE_ASC", limit=limit, offset=offset)


def _filter_error(exc: FactFilterError) -> HTTPException:
    status_code = status.HTTP_404_NOT_FOUND if isinstance(exc, FactFilterNotFound) else status.HTTP_422_UNPROCESSABLE_CONTENT
    return HTTPException(status_code=status_code, detail=str(exc))


def _page(db: Session, matter_id: uuid.UUID, filters: FactFilters, *, sort: str, limit: int, offset: int) -> MatterFactPage:
    try:
        statement = fact_filter_statement(db, matter_id=matter_id, filters=filters)
    except FactFilterError as exc:
        raise _filter_error(exc) from exc
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    facts = list(db.scalars(statement.order_by(*FACT_SORTS[sort]).limit(limit).offset(offset)))
    return MatterFactPage(items=_reads(db, facts), total=total)


@router.post("/search", response_model=MatterFactPage)
def search_facts(
    matter_id: uuid.UUID,
    payload: MatterFactSearchRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterFactPage:
    """Filter facts on every field; the same filter drives facets, the histogram, and export."""

    _matter(db, principal, matter_id, "fact.view")
    return _page(db, matter_id, payload.filters, sort=payload.sort, limit=payload.limit, offset=payload.offset)


@router.post("/facets", response_model=MatterFactFacetRead)
def facet_facts(
    matter_id: uuid.UUID,
    payload: MatterFactFacetRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterFactFacetRead:
    """Distinct-fact counts for one facet; the facet's own selection is ignored so its counts stay useful."""

    _matter(db, principal, matter_id, "fact.view")
    try:
        buckets = fact_facet(db, matter_id=matter_id, filters=payload.filters, facet=payload.facet, size=payload.size)
    except FactFilterError as exc:
        raise _filter_error(exc) from exc
    return MatterFactFacetRead(facet=payload.facet, buckets=buckets)


@router.post("/date-histogram", response_model=MatterFactHistogramRead)
def fact_histogram(
    matter_id: uuid.UUID,
    payload: MatterFactHistogramRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterFactHistogramRead:
    _matter(db, principal, matter_id, "fact.view")
    try:
        result = fact_date_histogram(db, matter_id=matter_id, filters=payload.filters, interval=payload.interval)
    except FactFilterError as exc:
        raise _filter_error(exc) from exc
    return MatterFactHistogramRead.model_validate(result)


@router.post("", response_model=MatterFactDetailRead, status_code=status.HTTP_201_CREATED)
def create_matter_fact(
    matter_id: uuid.UUID,
    payload: MatterFactCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterFactDetailRead:
    matter = _matter(db, principal, matter_id, "fact.review")
    try:
        evidence = [_verified(db, matter, principal, item) for item in payload.evidence]
        fact = create_fact(
            db,
            matter=matter,
            content=FactContent(
                summary=payload.summary,
                date_precision=payload.date_precision,
                event_date_start=payload.event_date_start,
                event_date_end=payload.event_date_end,
                date_basis=payload.date_basis,
                owner_entity_id=payload.owner_entity_id,
                issues=[FactIssueRef(item.matter_analysis_task_id, item.issue_key) for item in payload.issues],
                participants=[FactParticipantRef(item.entity_id, item.role) for item in payload.participants],
            ),
            evidence=evidence,
            actor=_actor(principal),
            key_fact=payload.key_fact,
        )
    except (FactError, FactEvidenceError) as exc:
        db.rollback()
        _raise(exc)
    _audit(db, matter, principal, "created", fact, {"evidence_count": fact.evidence_count})
    db.commit()
    db.refresh(fact)
    return _detail(db, fact)


@router.get("/{fact_id}", response_model=MatterFactDetailRead)
def get_matter_fact(
    matter_id: uuid.UUID,
    fact_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterFactDetailRead:
    _matter(db, principal, matter_id, "fact.view")
    try:
        fact = get_fact(db, matter_id=matter_id, fact_id=fact_id)
    except FactError as exc:
        _raise(exc)
    return _detail(db, fact)


@router.patch("/{fact_id}", response_model=MatterFactDetailRead)
def update_matter_fact(
    matter_id: uuid.UUID,
    fact_id: uuid.UUID,
    payload: MatterFactUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterFactDetailRead:
    matter = _matter(db, principal, matter_id, "fact.review")
    changes = {}
    for key in payload.model_fields_set - {"lock_version", "reason"}:
        value = getattr(payload, key)
        if key == "issues":
            value = [FactIssueRef(item.matter_analysis_task_id, item.issue_key) for item in value or []]
        elif key == "participants":
            value = [FactParticipantRef(item.entity_id, item.role) for item in value or []]
        changes[key] = value
    try:
        fact = get_fact(db, matter_id=matter_id, fact_id=fact_id, for_update=True)
        revise_fact(
            db,
            fact=fact,
            lock_version=payload.lock_version,
            changes=changes,
            actor=_actor(principal),
            reason=payload.reason,
        )
    except FactError as exc:
        db.rollback()
        _raise(exc)
    _audit(db, matter, principal, "revised", fact, {"fields": sorted(changes), "revision": fact.current_revision})
    db.commit()
    db.refresh(fact)
    return _detail(db, fact)


@router.post("/{fact_id}/confirm", response_model=MatterFactDetailRead)
def confirm_matter_fact(
    matter_id: uuid.UUID,
    fact_id: uuid.UUID,
    payload: MatterFactAction,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterFactDetailRead:
    matter = _matter(db, principal, matter_id, "fact.review")
    try:
        fact = get_fact(db, matter_id=matter_id, fact_id=fact_id, for_update=True)
        confirm_fact(db, fact=fact, lock_version=payload.lock_version, actor=_actor(principal))
    except FactError as exc:
        db.rollback()
        _raise(exc)
    _audit(db, matter, principal, "confirmed", fact, {})
    db.commit()
    db.refresh(fact)
    return _detail(db, fact)


@router.post("/{fact_id}/reject", response_model=MatterFactDetailRead)
def reject_matter_fact(
    matter_id: uuid.UUID,
    fact_id: uuid.UUID,
    payload: MatterFactAction,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterFactDetailRead:
    matter = _matter(db, principal, matter_id, "fact.review")
    try:
        fact = get_fact(db, matter_id=matter_id, fact_id=fact_id, for_update=True)
        reject_fact(db, fact=fact, lock_version=payload.lock_version, actor=_actor(principal), reason=payload.reason)
    except FactError as exc:
        db.rollback()
        _raise(exc)
    _audit(db, matter, principal, "rejected", fact, {"reason": payload.reason})
    db.commit()
    db.refresh(fact)
    return _detail(db, fact)


@router.post("/{fact_id}/evidence", response_model=MatterFactDetailRead, status_code=status.HTTP_201_CREATED)
def add_matter_fact_evidence(
    matter_id: uuid.UUID,
    fact_id: uuid.UUID,
    payload: MatterFactEvidenceAdd,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterFactDetailRead:
    matter = _matter(db, principal, matter_id, "fact.review")
    try:
        fact = get_fact(db, matter_id=matter_id, fact_id=fact_id, for_update=True)
        row = add_evidence(
            db,
            fact=fact,
            lock_version=payload.lock_version,
            evidence=_verified(db, matter, principal, payload.evidence),
            actor=_actor(principal),
        )
    except (FactError, FactEvidenceError) as exc:
        db.rollback()
        _raise(exc)
    _audit(db, matter, principal, "evidence_added", fact, {"evidence_id": str(row.id)})
    db.commit()
    db.refresh(fact)
    return _detail(db, fact)


@router.delete("/{fact_id}/evidence/{evidence_id}", response_model=MatterFactDetailRead)
def remove_matter_fact_evidence(
    matter_id: uuid.UUID,
    fact_id: uuid.UUID,
    evidence_id: uuid.UUID,
    lock_version: int = Query(ge=1),
    reason: str | None = Query(default=None, max_length=4_000),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterFactDetailRead:
    matter = _matter(db, principal, matter_id, "fact.review")
    try:
        fact = get_fact(db, matter_id=matter_id, fact_id=fact_id, for_update=True)
        remove_evidence(
            db,
            fact=fact,
            lock_version=lock_version,
            evidence_id=evidence_id,
            actor=_actor(principal),
            reason=reason,
        )
    except FactError as exc:
        db.rollback()
        _raise(exc)
    _audit(db, matter, principal, "evidence_removed", fact, {"evidence_id": str(evidence_id)})
    db.commit()
    db.refresh(fact)
    return _detail(db, fact)


@router.post("/{fact_id}/merge", response_model=MatterFactDetailRead)
def merge_matter_fact(
    matter_id: uuid.UUID,
    fact_id: uuid.UUID,
    payload: MatterFactMerge,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterFactDetailRead:
    """Merge this fact into the target fact; returns the target."""

    matter = _matter(db, principal, matter_id, "fact.review")
    try:
        source = get_fact(db, matter_id=matter_id, fact_id=fact_id, for_update=True)
        target = get_fact(db, matter_id=matter_id, fact_id=payload.target_fact_id, for_update=True)
        merge_facts(
            db,
            source=source,
            target=target,
            source_lock_version=payload.lock_version,
            target_lock_version=payload.target_lock_version,
            actor=_actor(principal),
        )
    except FactError as exc:
        db.rollback()
        _raise(exc)
    _audit(db, matter, principal, "merged", source, {"target_fact_id": str(target.id)})
    db.commit()
    db.refresh(target)
    return _detail(db, target)


@router.post("/{fact_id}/unmerge", response_model=MatterFactDetailRead)
def unmerge_matter_fact(
    matter_id: uuid.UUID,
    fact_id: uuid.UUID,
    payload: MatterFactAction,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterFactDetailRead:
    matter = _matter(db, principal, matter_id, "fact.review")
    try:
        source = get_fact(db, matter_id=matter_id, fact_id=fact_id, for_update=True)
        unmerge_fact(db, source=source, lock_version=payload.lock_version, actor=_actor(principal))
    except FactError as exc:
        db.rollback()
        _raise(exc)
    _audit(db, matter, principal, "unmerged", source, {})
    db.commit()
    db.refresh(source)
    return _detail(db, source)


@router.get("/{fact_id}/suggestions", response_model=list[MatterFactSuggestionRead])
def list_matter_fact_suggestions(
    matter_id: uuid.UUID,
    fact_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterFactSuggestion]:
    _matter(db, principal, matter_id, "fact.view")
    try:
        fact = get_fact(db, matter_id=matter_id, fact_id=fact_id)
    except FactError as exc:
        _raise(exc)
    return list(
        db.scalars(
            select(MatterFactSuggestion)
            .where(MatterFactSuggestion.fact_id == fact.id)
            .order_by(MatterFactSuggestion.created_at.desc(), MatterFactSuggestion.id)
        )
    )


@router.post("/{fact_id}/suggestions/{suggestion_id}/accept", response_model=MatterFactDetailRead)
def accept_matter_fact_suggestion(
    matter_id: uuid.UUID,
    fact_id: uuid.UUID,
    suggestion_id: uuid.UUID,
    payload: MatterFactSuggestionAccept,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterFactDetailRead:
    """Apply an agent suggestion as the reviewer's own change.

    A revision returns the revised fact. A merge (which needs ``target_lock_version``) returns the fact that
    absorbed this one; it can be undone with unmerge.
    """

    matter = _matter(db, principal, matter_id, "fact.review")
    try:
        fact = get_fact(db, matter_id=matter_id, fact_id=fact_id, for_update=True)
        result = accept_suggestion(
            db,
            fact=fact,
            suggestion_id=suggestion_id,
            lock_version=payload.lock_version,
            target_lock_version=payload.target_lock_version,
            actor=_actor(principal),
        )
    except FactError as exc:
        db.rollback()
        _raise(exc)
    _audit(db, matter, principal, "suggestion_accepted", fact, {"suggestion_id": str(suggestion_id)})
    db.commit()
    db.refresh(result)
    return _detail(db, result)


@router.post("/{fact_id}/suggestions/{suggestion_id}/dismiss", response_model=MatterFactSuggestionRead)
def dismiss_matter_fact_suggestion(
    matter_id: uuid.UUID,
    fact_id: uuid.UUID,
    suggestion_id: uuid.UUID,
    payload: MatterFactSuggestionDismiss,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterFactSuggestion:
    matter = _matter(db, principal, matter_id, "fact.review")
    try:
        fact = get_fact(db, matter_id=matter_id, fact_id=fact_id)
        suggestion = dismiss_suggestion(
            db,
            fact=fact,
            suggestion_id=suggestion_id,
            note=payload.note,
            actor=_actor(principal),
        )
    except FactError as exc:
        db.rollback()
        _raise(exc)
    _audit(db, matter, principal, "suggestion_dismissed", fact, {"suggestion_id": str(suggestion_id)})
    db.commit()
    db.refresh(suggestion)
    return suggestion


@document_router.get("/{document_id}/facts", response_model=list[MatterDocumentFactRead])
def list_document_facts(
    matter_id: uuid.UUID,
    document_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterDocumentFactRead]:
    """Working and confirmed facts citing one document, oldest first, read from Core so changes show immediately."""

    _matter(db, principal, matter_id, "fact.view")
    document = db.scalar(
        select(MatterDocument).where(MatterDocument.id == document_id, MatterDocument.matter_id == matter_id)
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    quotes = list(
        db.scalars(
            select(MatterFactEvidence)
            .join(MatterFact, MatterFact.id == MatterFactEvidence.fact_id)
            .where(
                MatterFactEvidence.matter_document_id == document.id,
                MatterFactEvidence.status == "ACTIVE",
                MatterFact.matter_id == matter_id,
                MatterFact.status.in_(["WORKING", "CONFIRMED"]),
            )
            .order_by(MatterFactEvidence.char_start, MatterFactEvidence.id)
        )
    )
    quotes_by_fact: dict[uuid.UUID, list[MatterFactEvidence]] = {}
    for row in quotes:
        quotes_by_fact.setdefault(row.fact_id, []).append(row)
    facts = list(
        db.scalars(
            select(MatterFact).where(MatterFact.id.in_(list(quotes_by_fact))).order_by(*FACT_DATE_ORDER)
        )
    )
    control_number = _control_numbers(db, matter_id, {document.id}).get(document.id)
    return [
        MatterDocumentFactRead(
            fact=read,
            quotes=[
                MatterFactEvidenceRead.model_validate(row).model_copy(update={"control_number": control_number})
                for row in quotes_by_fact[fact.id]
            ],
        )
        for fact, read in zip(facts, _reads(db, facts), strict=True)
    ]
