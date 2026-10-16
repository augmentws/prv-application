"""Fact ledger command service: the only write path for facts, used by people and (later) agents.

``matter_fact_event`` is authoritative. ``matter_fact`` and its issue, participant, and evidence rows are the
current projection, updated in the same transaction as the event. Content changes also append an immutable
``matter_fact_revision``. Human mutations carry the ``lock_version`` the caller loaded so concurrent edits
(including edits made while an agent run is adding evidence) fail instead of overwriting each other.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from app.fact_document_state import FactStateTracker
from app.fact_evidence import VerifiedEvidence
from app.fact_specifications import FactSpecificationError, parse_fact_development_specification
from app.matter_entities import canonical_ids
from app.models import (
    Matter,
    MatterAnalysisTask,
    MatterAnalysisTaskVersion,
    MatterEntity,
    MatterFact,
    MatterFactEntity,
    MatterFactEvent,
    MatterFactEvidence,
    MatterFactIssue,
    MatterFactRevision,
    MatterFactSuggestion,
    utcnow,
)

DatePrecision = Literal["DAY", "MONTH", "QUARTER", "YEAR", "RANGE", "UNKNOWN"]
DateBasis = Literal["STATED_IN_TEXT", "DOCUMENT_DATE", "INFERRED"]
ParticipantRole = Literal["PARTICIPANT_ACTOR", "PARTICIPANT_RECIPIENT"]
ActorType = Literal["HUMAN", "AGENT", "SYSTEM"]
CONTENT_FIELDS = frozenset(
    {
        "summary",
        "event_date_start",
        "event_date_end",
        "date_precision",
        "date_basis",
        "owner_entity_id",
        "issues",
        "participants",
    }
)


class FactError(ValueError):
    pass


class FactNotFound(FactError):
    pass


class FactConflict(FactError):
    pass


@dataclass(frozen=True)
class FactIssueRef:
    matter_analysis_task_id: uuid.UUID
    issue_key: str


@dataclass(frozen=True)
class FactParticipantRef:
    entity_id: uuid.UUID
    role: ParticipantRole


@dataclass
class FactContent:
    summary: str
    date_precision: DatePrecision = "UNKNOWN"
    event_date_start: date | None = None
    event_date_end: date | None = None
    date_basis: DateBasis | None = None
    owner_entity_id: uuid.UUID | None = None
    issues: list[FactIssueRef] = field(default_factory=list)
    participants: list[FactParticipantRef] = field(default_factory=list)


@dataclass(frozen=True)
class Actor:
    actor_type: ActorType
    user_id: uuid.UUID | None = None
    workflow_run_id: uuid.UUID | None = None


def normalize_fact_dates(
    precision: DatePrecision,
    start: date | None,
    end: date | None,
) -> tuple[date | None, date | None]:
    """Validate a date against its precision and store month/quarter/year dates at the start of the period."""

    if precision == "UNKNOWN":
        if start is not None or end is not None:
            raise FactError("An undated fact cannot carry dates")
        return None, None
    if start is None:
        raise FactError(f"A {precision.lower()}-precision fact needs a start date")
    if precision == "RANGE":
        if end is None or end < start:
            raise FactError("A date range needs an end date on or after its start date")
        return start, end
    if end is not None:
        raise FactError("Only a date range has an end date")
    if precision == "MONTH":
        return start.replace(day=1), None
    if precision == "QUARTER":
        return date(start.year, ((start.month - 1) // 3) * 3 + 1, 1), None
    if precision == "YEAR":
        return date(start.year, 1, 1), None
    return start, None


def _record(
    db: Session,
    fact: MatterFact,
    event_type: str,
    payload: dict[str, Any],
    actor: Actor,
) -> MatterFactEvent:
    event = MatterFactEvent(
        matter_id=fact.matter_id,
        fact_id=fact.id,
        event_type=event_type,
        payload=payload,
        actor_type=actor.actor_type,
        actor_user_id=actor.user_id,
        workflow_run_id=actor.workflow_run_id,
        created_at=utcnow(),
    )
    db.add(event)
    return event


def _state_tracker(
    db: Session,
    matter_id: uuid.UUID,
    actor: Actor,
    *,
    fact_ids: Sequence[uuid.UUID] = (),
    document_ids: Iterable[uuid.UUID] = (),
) -> FactStateTracker:
    """Track the documents a command can affect so their ``fact_state`` is reprojected only if it changes."""

    return FactStateTracker(
        db,
        matter_id=matter_id,
        actor_user_id=actor.user_id,
        workflow_run_id=actor.workflow_run_id,
        fact_ids=fact_ids,
        document_ids=document_ids,
    )


def get_fact(db: Session, *, matter_id: uuid.UUID, fact_id: uuid.UUID, for_update: bool = False) -> MatterFact:
    statement = select(MatterFact).where(MatterFact.id == fact_id, MatterFact.matter_id == matter_id)
    if for_update:
        statement = statement.with_for_update()
    fact = db.scalar(statement)
    if fact is None:
        raise FactNotFound("Fact not found")
    return fact


def _check_version(fact: MatterFact, lock_version: int) -> None:
    if fact.lock_version != lock_version:
        raise FactConflict(f"Fact changed since it was loaded (version {fact.lock_version}, expected {lock_version})")


def _require_not_merged(fact: MatterFact) -> None:
    if fact.status == "MERGED":
        raise FactConflict("A merged fact cannot be changed; unmerge it first")


def _bump(fact: MatterFact) -> None:
    fact.lock_version += 1
    fact.updated_at = utcnow()


def validate_issue_refs(db: Session, *, matter_id: uuid.UUID, issues: Sequence[FactIssueRef]) -> None:
    """Every issue must exist in the published catalog of a fact-development task in this matter."""

    if not issues:
        raise FactError("A fact must support at least one issue")
    if len(set(issues)) != len(issues):
        raise FactError("A fact cannot list the same issue twice")
    task_ids = {issue.matter_analysis_task_id for issue in issues}
    tasks = {
        task.id: task
        for task in db.scalars(
            select(MatterAnalysisTask).where(
                MatterAnalysisTask.id.in_(task_ids),
                MatterAnalysisTask.matter_id == matter_id,
                MatterAnalysisTask.task_type == "FACT_DEVELOPMENT",
            )
        )
    }
    for task_id in task_ids:
        task = tasks.get(task_id)
        if task is None:
            raise FactError("Issues must come from a fact-development task in this matter")
        if task.published_version is None:
            raise FactError(f"Fact-development task {task.key} has no published issue catalog")
        version = db.scalar(
            select(MatterAnalysisTaskVersion).where(
                MatterAnalysisTaskVersion.matter_analysis_task_id == task.id,
                MatterAnalysisTaskVersion.version == task.published_version,
            )
        )
        try:
            specification = parse_fact_development_specification(
                version.fact_development_specification if version is not None else None
            )
        except FactSpecificationError as exc:
            raise FactError(str(exc)) from exc
        known = {issue.key for issue in specification.issues}
        unknown = sorted(issue.issue_key for issue in issues if issue.matter_analysis_task_id == task_id and issue.issue_key not in known)
        if unknown:
            raise FactError(f"Unknown issues for task {task.key}: " + ", ".join(unknown))


def validate_entity_refs(
    db: Session,
    *,
    matter_id: uuid.UUID,
    owner_entity_id: uuid.UUID | None,
    participants: Sequence[FactParticipantRef],
) -> None:
    referenced = {participant.entity_id for participant in participants}
    if owner_entity_id is not None:
        referenced.add(owner_entity_id)
    if len(set(participants)) != len(participants):
        raise FactError("A fact cannot list the same participant and role twice")
    if not referenced:
        return
    rows = {
        entity.id: entity
        for entity in db.scalars(
            select(MatterEntity).where(MatterEntity.id.in_(referenced), MatterEntity.matter_id == matter_id)
        )
    }
    missing = referenced - set(rows)
    if missing:
        raise FactError("Owner and participants must be entities in this matter")
    canonical_by_row = canonical_ids(db, referenced)
    statuses = dict(
        db.execute(
            select(MatterEntity.id, MatterEntity.status).where(MatterEntity.id.in_(set(canonical_by_row.values())))
        ).all()
    )
    if any(statuses.get(canonical_by_row[row_id]) == "REJECTED" for row_id in referenced):
        raise FactError("Rejected entities cannot be a fact's owner or participant")


def _content_snapshot(content: FactContent) -> dict[str, Any]:
    return {
        "summary": content.summary,
        "event_date_start": content.event_date_start.isoformat() if content.event_date_start else None,
        "event_date_end": content.event_date_end.isoformat() if content.event_date_end else None,
        "date_precision": content.date_precision,
        "date_basis": content.date_basis,
        "owner_entity_id": str(content.owner_entity_id) if content.owner_entity_id else None,
        "issues": [
            {"matter_analysis_task_id": str(issue.matter_analysis_task_id), "issue_key": issue.issue_key}
            for issue in content.issues
        ],
        "participants": [
            {"entity_id": str(participant.entity_id), "role": participant.role} for participant in content.participants
        ],
    }


def current_content(db: Session, fact: MatterFact) -> FactContent:
    return FactContent(
        summary=fact.summary,
        date_precision=fact.date_precision,  # type: ignore[arg-type]
        event_date_start=fact.event_date_start,
        event_date_end=fact.event_date_end,
        date_basis=fact.date_basis,  # type: ignore[arg-type]
        owner_entity_id=fact.owner_entity_id,
        issues=[
            FactIssueRef(row.matter_analysis_task_id, row.issue_key)
            for row in db.scalars(
                select(MatterFactIssue)
                .where(MatterFactIssue.fact_id == fact.id)
                .order_by(MatterFactIssue.matter_analysis_task_id, MatterFactIssue.issue_key)
            )
        ],
        participants=[
            FactParticipantRef(row.entity_id, row.role)  # type: ignore[arg-type]
            for row in db.scalars(
                select(MatterFactEntity)
                .where(MatterFactEntity.fact_id == fact.id)
                .order_by(MatterFactEntity.role, MatterFactEntity.entity_id)
            )
        ],
    )


def _apply_content(db: Session, fact: MatterFact, content: FactContent, *, actor: Actor, reason: str | None) -> None:
    """Write the projection rows for ``content`` and append the next immutable revision."""

    fact.summary = content.summary
    fact.event_date_start = content.event_date_start
    fact.event_date_end = content.event_date_end
    fact.date_precision = content.date_precision
    fact.date_basis = content.date_basis
    fact.owner_entity_id = content.owner_entity_id
    db.execute(delete(MatterFactIssue).where(MatterFactIssue.fact_id == fact.id))
    db.execute(delete(MatterFactEntity).where(MatterFactEntity.fact_id == fact.id))
    db.add_all(
        MatterFactIssue(fact_id=fact.id, matter_analysis_task_id=issue.matter_analysis_task_id, issue_key=issue.issue_key)
        for issue in content.issues
    )
    db.add_all(
        MatterFactEntity(fact_id=fact.id, entity_id=participant.entity_id, role=participant.role)
        for participant in content.participants
    )
    snapshot = _content_snapshot(content)
    db.add(
        MatterFactRevision(
            fact_id=fact.id,
            revision=fact.current_revision,
            summary=content.summary,
            event_date_start=content.event_date_start,
            event_date_end=content.event_date_end,
            date_precision=content.date_precision,
            date_basis=content.date_basis,
            owner_entity_id=content.owner_entity_id,
            issues=snapshot["issues"],
            participants=snapshot["participants"],
            author_type="AGENT" if actor.actor_type == "AGENT" else "HUMAN",
            author_user_id=actor.user_id,
            workflow_run_id=actor.workflow_run_id,
            reason=reason,
            created_at=utcnow(),
        )
    )


def _validated_content(db: Session, *, matter_id: uuid.UUID, content: FactContent) -> FactContent:
    summary = " ".join(content.summary.split())
    if not summary:
        raise FactError("A fact needs a summary")
    start, end = normalize_fact_dates(content.date_precision, content.event_date_start, content.event_date_end)
    if content.date_precision == "UNKNOWN" and content.date_basis is not None:
        raise FactError("An undated fact has no date basis")
    validate_issue_refs(db, matter_id=matter_id, issues=content.issues)
    validate_entity_refs(
        db,
        matter_id=matter_id,
        owner_entity_id=content.owner_entity_id,
        participants=content.participants,
    )
    return FactContent(
        summary=summary,
        date_precision=content.date_precision,
        event_date_start=start,
        event_date_end=end,
        date_basis=content.date_basis,
        owner_entity_id=content.owner_entity_id,
        issues=sorted(content.issues, key=lambda issue: (str(issue.matter_analysis_task_id), issue.issue_key)),
        participants=sorted(content.participants, key=lambda item: (item.role, str(item.entity_id))),
    )


def _refresh_evidence_counts(db: Session, fact: MatterFact) -> None:
    evidence_count, document_count, contradiction_count = db.execute(
        select(
            func.count(MatterFactEvidence.id),
            func.count(func.distinct(MatterFactEvidence.matter_document_id)),
            func.count(MatterFactEvidence.id).filter(MatterFactEvidence.stance == "CONTRADICTS"),
        ).where(MatterFactEvidence.fact_id == fact.id, MatterFactEvidence.status == "ACTIVE")
    ).one()
    fact.evidence_count = int(evidence_count or 0)
    fact.document_count = int(document_count or 0)
    fact.has_contradiction = bool(contradiction_count)


def _evidence_row(fact: MatterFact, evidence: VerifiedEvidence, actor: Actor) -> MatterFactEvidence:
    return MatterFactEvidence(
        fact_id=fact.id,
        matter_document_id=evidence.matter_document_id,
        source_artifact_id=evidence.source_artifact_id,
        source_content_hash=evidence.source_content_hash,
        paragraph_map_version=evidence.paragraph_map_version,
        paragraph_id=evidence.paragraph_id,
        char_start=evidence.char_start,
        char_end=evidence.char_end,
        quote=evidence.quote,
        quote_hash=evidence.quote_hash,
        stance=evidence.stance,
        status="ACTIVE",
        added_by_user_id=actor.user_id if actor.workflow_run_id is None else None,
        workflow_run_id=actor.workflow_run_id,
        created_at=utcnow(),
    )


def _evidence_payload(row: MatterFactEvidence) -> dict[str, Any]:
    return {
        "evidence_id": str(row.id),
        "matter_document_id": str(row.matter_document_id),
        "paragraph_id": row.paragraph_id,
        "char_start": row.char_start,
        "char_end": row.char_end,
        "quote_hash": row.quote_hash,
        "stance": row.stance,
    }


def create_fact(
    db: Session,
    *,
    matter: Matter,
    content: FactContent,
    evidence: Sequence[VerifiedEvidence],
    actor: Actor,
    key_fact: bool = False,
) -> MatterFact:
    if not evidence:
        raise FactError("A fact needs at least one supporting quote")
    validated = _validated_content(db, matter_id=matter.id, content=content)
    tracker = _state_tracker(db, matter.id, actor, document_ids=[item.matter_document_id for item in evidence])
    fact = MatterFact(
        matter_id=matter.id,
        status="WORKING",
        current_revision=1,
        summary=validated.summary,
        date_precision=validated.date_precision,
        event_date_start=validated.event_date_start,
        event_date_end=validated.event_date_end,
        date_basis=validated.date_basis,
        owner_entity_id=validated.owner_entity_id,
        key_fact=key_fact,
        created_by_user_id=actor.user_id if actor.workflow_run_id is None else None,
        origin_workflow_run_id=actor.workflow_run_id,
        lock_version=1,
    )
    db.add(fact)
    db.flush()
    _apply_content(db, fact, validated, actor=actor, reason=None)
    rows = [_evidence_row(fact, item, actor) for item in evidence]
    db.add_all(rows)
    db.flush()
    _refresh_evidence_counts(db, fact)
    _record(
        db,
        fact,
        "CREATED",
        {
            "content": _content_snapshot(validated),
            "key_fact": key_fact,
            "evidence": [_evidence_payload(row) for row in rows],
        },
        actor,
    )
    tracker.publish()
    return fact


def revise_fact(
    db: Session,
    *,
    fact: MatterFact,
    lock_version: int,
    changes: dict[str, Any],
    actor: Actor,
    reason: str | None = None,
) -> MatterFact:
    """Change a fact's content and/or key-fact flag; ``changes`` holds only the fields the caller set."""

    _require_not_merged(fact)
    _check_version(fact, lock_version)
    unknown = set(changes) - CONTENT_FIELDS - {"key_fact"}
    if unknown:
        raise FactError("Unknown fact fields: " + ", ".join(sorted(unknown)))
    if not changes:
        raise FactError("No fact changes were supplied")
    if "key_fact" in changes and changes["key_fact"] != fact.key_fact:
        fact.key_fact = bool(changes["key_fact"])
        _record(db, fact, "KEY_FACT_SET", {"key_fact": fact.key_fact}, actor)
    content_changes = {key: value for key, value in changes.items() if key in CONTENT_FIELDS}
    if content_changes:
        before = current_content(db, fact)
        merged = FactContent(**{**before.__dict__, **content_changes})
        validated = _validated_content(db, matter_id=fact.matter_id, content=merged)
        if _content_snapshot(validated) != _content_snapshot(before):
            fact.current_revision += 1
            _apply_content(db, fact, validated, actor=actor, reason=reason)
            _record(
                db,
                fact,
                "REVISED",
                {
                    "revision": fact.current_revision,
                    "before": _content_snapshot(before),
                    "after": _content_snapshot(validated),
                    "reason": reason,
                },
                actor,
            )
    _bump(fact)
    return fact


def confirm_fact(db: Session, *, fact: MatterFact, lock_version: int, actor: Actor) -> MatterFact:
    """Confirm a fact. Only people confirm; confirmation is what places a fact on the timeline."""

    if actor.actor_type != "HUMAN" or actor.user_id is None:
        raise FactConflict("Only a reviewer can confirm a fact")
    _require_not_merged(fact)
    _check_version(fact, lock_version)
    if fact.status == "CONFIRMED":
        raise FactConflict("Fact is already confirmed")
    if fact.evidence_count == 0:
        raise FactConflict("A fact without active supporting quotes cannot be confirmed")
    tracker = _state_tracker(db, fact.matter_id, actor, fact_ids=[fact.id])
    prior = fact.status
    fact.status = "CONFIRMED"
    fact.confirmed_by_user_id = actor.user_id
    fact.confirmed_at = utcnow()
    _bump(fact)
    _record(db, fact, "CONFIRMED", {"prior_status": prior}, actor)
    tracker.publish()
    return fact


def reject_fact(
    db: Session,
    *,
    fact: MatterFact,
    lock_version: int,
    actor: Actor,
    reason: str | None,
) -> MatterFact:
    if actor.actor_type != "HUMAN" or actor.user_id is None:
        raise FactConflict("Only a reviewer can reject a fact")
    _require_not_merged(fact)
    _check_version(fact, lock_version)
    if fact.status == "REJECTED":
        raise FactConflict("Fact is already rejected")
    tracker = _state_tracker(db, fact.matter_id, actor, fact_ids=[fact.id])
    prior = fact.status
    fact.status = "REJECTED"
    fact.confirmed_by_user_id = None
    fact.confirmed_at = None
    _bump(fact)
    _record(db, fact, "REJECTED", {"prior_status": prior, "reason": reason}, actor)
    tracker.publish()
    return fact


def add_evidence(
    db: Session,
    *,
    fact: MatterFact,
    lock_version: int,
    evidence: VerifiedEvidence,
    actor: Actor,
) -> MatterFactEvidence:
    _require_not_merged(fact)
    _check_version(fact, lock_version)
    duplicate = db.scalar(
        select(MatterFactEvidence.id).where(
            MatterFactEvidence.fact_id == fact.id,
            MatterFactEvidence.status == "ACTIVE",
            MatterFactEvidence.matter_document_id == evidence.matter_document_id,
            MatterFactEvidence.char_start == evidence.char_start,
            MatterFactEvidence.char_end == evidence.char_end,
        )
    )
    if duplicate is not None:
        raise FactConflict("This quote is already evidence for the fact")
    tracker = _state_tracker(db, fact.matter_id, actor, document_ids=[evidence.matter_document_id])
    row = _evidence_row(fact, evidence, actor)
    db.add(row)
    db.flush()
    _refresh_evidence_counts(db, fact)
    _bump(fact)
    _record(db, fact, "EVIDENCE_ADDED", _evidence_payload(row), actor)
    tracker.publish()
    return row


def remove_evidence(
    db: Session,
    *,
    fact: MatterFact,
    lock_version: int,
    evidence_id: uuid.UUID,
    actor: Actor,
    reason: str | None = None,
) -> MatterFactEvidence:
    _require_not_merged(fact)
    _check_version(fact, lock_version)
    row = db.scalar(
        select(MatterFactEvidence).where(
            MatterFactEvidence.id == evidence_id,
            MatterFactEvidence.fact_id == fact.id,
            MatterFactEvidence.status == "ACTIVE",
        )
    )
    if row is None:
        raise FactNotFound("Active evidence was not found on this fact")
    if fact.status == "CONFIRMED" and fact.evidence_count <= 1:
        raise FactConflict("A confirmed fact must keep at least one supporting quote")
    tracker = _state_tracker(db, fact.matter_id, actor, document_ids=[row.matter_document_id])
    row.status = "REMOVED"
    row.removed_at = utcnow()
    db.flush()
    _refresh_evidence_counts(db, fact)
    _bump(fact)
    _record(db, fact, "EVIDENCE_REMOVED", {**_evidence_payload(row), "reason": reason}, actor)
    tracker.publish()
    return row


def merge_facts(
    db: Session,
    *,
    source: MatterFact,
    target: MatterFact,
    source_lock_version: int,
    target_lock_version: int,
    actor: Actor,
) -> MatterFact:
    """Absorb ``source`` into ``target``: move its active evidence and add its issues and participants."""

    if source.id == target.id:
        raise FactError("A fact cannot be merged into itself")
    if source.matter_id != target.matter_id:
        raise FactError("Facts must belong to the same matter")
    _require_not_merged(source)
    _require_not_merged(target)
    _check_version(source, source_lock_version)
    _check_version(target, target_lock_version)
    if target.status == "REJECTED":
        raise FactConflict("Facts cannot be merged into a rejected fact")
    tracker = _state_tracker(db, source.matter_id, actor, fact_ids=[source.id, target.id])
    target_before = current_content(db, target)
    source_content = current_content(db, source)
    added_issues = [issue for issue in source_content.issues if issue not in target_before.issues]
    added_participants = [item for item in source_content.participants if item not in target_before.participants]
    target_evidence = {
        (row.matter_document_id, row.char_start, row.char_end)
        for row in db.scalars(
            select(MatterFactEvidence).where(
                MatterFactEvidence.fact_id == target.id, MatterFactEvidence.status == "ACTIVE"
            )
        )
    }
    moved: list[MatterFactEvidence] = []
    for row in db.scalars(
        select(MatterFactEvidence).where(MatterFactEvidence.fact_id == source.id, MatterFactEvidence.status == "ACTIVE")
    ):
        if (row.matter_document_id, row.char_start, row.char_end) in target_evidence:
            continue
        row.fact_id = target.id
        moved.append(row)
    if added_issues or added_participants:
        target.current_revision += 1
        merged_content = FactContent(
            **{
                **target_before.__dict__,
                "issues": [*target_before.issues, *added_issues],
                "participants": [*target_before.participants, *added_participants],
            }
        )
        _apply_content(
            db,
            target,
            _validated_content(db, matter_id=target.matter_id, content=merged_content),
            actor=actor,
            reason=f"Merged fact {source.id}",
        )
    prior_status = source.status
    source.status = "MERGED"
    source.merged_into_fact_id = target.id
    source.confirmed_by_user_id = None
    source.confirmed_at = None
    db.flush()
    _refresh_evidence_counts(db, source)
    _refresh_evidence_counts(db, target)
    _bump(source)
    _bump(target)
    payload = {
        "target_fact_id": str(target.id),
        "prior_status": prior_status,
        "moved_evidence_ids": [str(row.id) for row in moved],
        "added_issues": [
            {"matter_analysis_task_id": str(issue.matter_analysis_task_id), "issue_key": issue.issue_key}
            for issue in added_issues
        ],
        "added_participants": [{"entity_id": str(item.entity_id), "role": item.role} for item in added_participants],
    }
    _record(db, source, "MERGED", payload, actor)
    _record(db, target, "ABSORBED", {"source_fact_id": str(source.id), **payload}, actor)
    tracker.publish()
    return target


def unmerge_fact(db: Session, *, source: MatterFact, lock_version: int, actor: Actor) -> MatterFact:
    """Undo the most recent merge of ``source``: return moved evidence and remove the issues it added."""

    if source.status != "MERGED" or source.merged_into_fact_id is None:
        raise FactConflict("Only a merged fact can be unmerged")
    _check_version(source, lock_version)
    merge_event = db.scalar(
        select(MatterFactEvent)
        .where(MatterFactEvent.fact_id == source.id, MatterFactEvent.event_type == "MERGED")
        .order_by(MatterFactEvent.created_at.desc())
        .limit(1)
    )
    if merge_event is None:
        raise FactConflict("The merge being undone has no recorded event")
    target = get_fact(db, matter_id=source.matter_id, fact_id=source.merged_into_fact_id, for_update=True)
    tracker = _state_tracker(db, source.matter_id, actor, fact_ids=[target.id])
    payload = merge_event.payload
    returned: list[str] = []
    for evidence_id in payload.get("moved_evidence_ids", []):
        row = db.get(MatterFactEvidence, uuid.UUID(evidence_id))
        if row is not None and row.fact_id == target.id:
            row.fact_id = source.id
            returned.append(evidence_id)
    added_issues = {
        FactIssueRef(uuid.UUID(item["matter_analysis_task_id"]), item["issue_key"])
        for item in payload.get("added_issues", [])
    }
    added_participants = {
        FactParticipantRef(uuid.UUID(item["entity_id"]), item["role"]) for item in payload.get("added_participants", [])
    }
    if added_issues or added_participants:
        before = current_content(db, target)
        remaining = FactContent(
            **{
                **before.__dict__,
                "issues": [issue for issue in before.issues if issue not in added_issues],
                "participants": [item for item in before.participants if item not in added_participants],
            }
        )
        if remaining.issues:
            target.current_revision += 1
            _apply_content(
                db,
                target,
                _validated_content(db, matter_id=target.matter_id, content=remaining),
                actor=actor,
                reason=f"Unmerged fact {source.id}",
            )
    source.status = payload.get("prior_status") if payload.get("prior_status") != "CONFIRMED" else "WORKING"
    source.merged_into_fact_id = None
    db.flush()
    _refresh_evidence_counts(db, source)
    _refresh_evidence_counts(db, target)
    _bump(source)
    _bump(target)
    _record(db, source, "UNMERGED", {"from_fact_id": str(target.id), "returned_evidence_ids": returned}, actor)
    _record(db, target, "SPLIT", {"source_fact_id": str(source.id), "returned_evidence_ids": returned}, actor)
    tracker.publish()
    return source


def detach_entities_from_facts(
    db: Session,
    *,
    matter_id: uuid.UUID,
    entity_ids: Iterable[uuid.UUID],
    actor_user_id: uuid.UUID,
    reason: str,
) -> list[uuid.UUID]:
    """Remove rejected entity rows from every fact's owner and participants, as new revisions."""

    ids = set(entity_ids)
    if not ids:
        return []
    fact_ids = set(
        db.scalars(select(MatterFact.id).where(MatterFact.matter_id == matter_id, MatterFact.owner_entity_id.in_(ids)))
    ) | set(
        db.scalars(
            select(MatterFactEntity.fact_id)
            .join(MatterFact, MatterFact.id == MatterFactEntity.fact_id)
            .where(MatterFact.matter_id == matter_id, MatterFactEntity.entity_id.in_(ids))
        )
    )
    actor = Actor("HUMAN", actor_user_id)
    for fact_id in sorted(fact_ids, key=str):
        fact = get_fact(db, matter_id=matter_id, fact_id=fact_id, for_update=True)
        before = current_content(db, fact)
        after = FactContent(
            **{
                **before.__dict__,
                "owner_entity_id": None if before.owner_entity_id in ids else before.owner_entity_id,
                "participants": [item for item in before.participants if item.entity_id not in ids],
            }
        )
        fact.current_revision += 1
        _apply_content(db, fact, after, actor=actor, reason=reason)
        _bump(fact)
        _record(
            db,
            fact,
            "ENTITIES_DETACHED",
            {"entity_ids": sorted(str(value) for value in ids), "before": _content_snapshot(before), "reason": reason},
            actor,
        )
    return sorted(fact_ids, key=str)


# Suggested revisions: agent changes to reviewer-owned facts wait for a reviewer.


def _serialize_changes(changes: dict[str, Any]) -> dict[str, Any]:
    snapshot = _content_snapshot(FactContent(**{**FactContent(summary="").__dict__, **changes}))
    return {key: snapshot[key] for key in changes}


def _deserialize_changes(changes: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, value in changes.items():
        if key in {"event_date_start", "event_date_end"}:
            values[key] = date.fromisoformat(value) if value else None
        elif key == "owner_entity_id":
            values[key] = uuid.UUID(value) if value else None
        elif key == "issues":
            values[key] = [FactIssueRef(uuid.UUID(item["matter_analysis_task_id"]), item["issue_key"]) for item in value]
        elif key == "participants":
            values[key] = [FactParticipantRef(uuid.UUID(item["entity_id"]), item["role"]) for item in value]
        else:
            values[key] = value
    return values


def _verified_evidence_payload(item: VerifiedEvidence) -> dict[str, Any]:
    return {
        "matter_document_id": str(item.matter_document_id),
        "source_artifact_id": str(item.source_artifact_id),
        "source_content_hash": item.source_content_hash,
        "paragraph_map_version": item.paragraph_map_version,
        "paragraph_id": item.paragraph_id,
        "char_start": item.char_start,
        "char_end": item.char_end,
        "quote": item.quote,
        "quote_hash": item.quote_hash,
        "stance": item.stance,
    }


def _verified_evidence_from_payload(value: dict[str, Any]) -> VerifiedEvidence:
    return VerifiedEvidence(
        matter_document_id=uuid.UUID(value["matter_document_id"]),
        source_artifact_id=uuid.UUID(value["source_artifact_id"]),
        source_content_hash=value["source_content_hash"],
        paragraph_map_version=value["paragraph_map_version"],
        paragraph_id=value["paragraph_id"],
        char_start=int(value["char_start"]),
        char_end=int(value["char_end"]),
        quote=value["quote"],
        quote_hash=value["quote_hash"],
        stance=value["stance"],
    )


def suggest_revision(
    db: Session,
    *,
    fact: MatterFact,
    changes: dict[str, Any],
    evidence: Sequence[VerifiedEvidence],
    rationale: str | None,
    actor: Actor,
) -> MatterFactSuggestion:
    """Record a proposed content change to a confirmed fact without changing the fact."""

    _require_not_merged(fact)
    if fact.status != "CONFIRMED":
        raise FactConflict("Only confirmed facts receive suggestions; working facts are revised directly")
    unknown = set(changes) - CONTENT_FIELDS
    if unknown or not changes:
        raise FactError("A suggestion needs one or more known fact fields")
    # Validate now so reviewers only ever see suggestions that could be applied to the current fact.
    before = current_content(db, fact)
    _validated_content(db, matter_id=fact.matter_id, content=FactContent(**{**before.__dict__, **changes}))
    suggestion = MatterFactSuggestion(
        matter_id=fact.matter_id,
        fact_id=fact.id,
        kind="REVISION",
        status="PENDING",
        base_revision=fact.current_revision,
        changes=_serialize_changes(changes),
        evidence=[_verified_evidence_payload(item) for item in evidence],
        rationale=rationale,
        created_by_workflow_run_id=actor.workflow_run_id,
    )
    db.add(suggestion)
    db.flush()
    _record(
        db,
        fact,
        "SUGGESTION_CREATED",
        {"suggestion_id": str(suggestion.id), "kind": suggestion.kind, "changes": suggestion.changes},
        actor,
    )
    return suggestion


def suggest_merge(
    db: Session,
    *,
    source: MatterFact,
    target: MatterFact,
    rationale: str | None,
    actor: Actor,
) -> MatterFactSuggestion | None:
    """Propose absorbing ``source`` into ``target``; ``None`` when the pair was already suggested or dismissed."""

    if source.id == target.id or source.matter_id != target.matter_id:
        raise FactError("A merge suggestion needs two different facts in the same matter")
    for fact in (source, target):
        if fact.status not in {"WORKING", "CONFIRMED"}:
            raise FactConflict(f"A {fact.status.lower()} fact cannot be part of a merge suggestion")
    existing = db.scalar(
        select(MatterFactSuggestion.id).where(
            MatterFactSuggestion.kind == "MERGE",
            MatterFactSuggestion.status.in_(["PENDING", "DISMISSED"]),
            or_(
                and_(MatterFactSuggestion.fact_id == source.id, MatterFactSuggestion.target_fact_id == target.id),
                and_(MatterFactSuggestion.fact_id == target.id, MatterFactSuggestion.target_fact_id == source.id),
            ),
        )
    )
    if existing is not None:
        # A pending pair needs no second suggestion, and a reviewer who dismissed it is not asked again.
        return None
    suggestion = MatterFactSuggestion(
        matter_id=source.matter_id,
        fact_id=source.id,
        kind="MERGE",
        status="PENDING",
        base_revision=source.current_revision,
        changes={},
        evidence=[],
        target_fact_id=target.id,
        rationale=rationale,
        created_by_workflow_run_id=actor.workflow_run_id,
    )
    db.add(suggestion)
    db.flush()
    _record(
        db,
        source,
        "SUGGESTION_CREATED",
        {"suggestion_id": str(suggestion.id), "kind": "MERGE", "target_fact_id": str(target.id)},
        actor,
    )
    return suggestion


def _pending_suggestion(db: Session, fact: MatterFact, suggestion_id: uuid.UUID) -> MatterFactSuggestion:
    suggestion = db.scalar(
        select(MatterFactSuggestion)
        .where(MatterFactSuggestion.id == suggestion_id, MatterFactSuggestion.fact_id == fact.id)
        .with_for_update()
    )
    if suggestion is None:
        raise FactNotFound("Suggestion not found")
    if suggestion.status != "PENDING":
        raise FactConflict(f"The suggestion is already {suggestion.status.lower()}")
    return suggestion


def accept_suggestion(
    db: Session,
    *,
    fact: MatterFact,
    suggestion_id: uuid.UUID,
    lock_version: int,
    actor: Actor,
    target_lock_version: int | None = None,
) -> MatterFact:
    """Apply a pending suggestion as the reviewer's own change.

    A revision is applied with its supporting quotes and returns ``fact``; a merge absorbs ``fact`` into the
    suggested target (undoable with unmerge) and returns the target.
    """

    if actor.actor_type != "HUMAN":
        raise FactConflict("Only a person can accept a suggestion")
    suggestion = _pending_suggestion(db, fact, suggestion_id)
    if suggestion.kind == "MERGE":
        if target_lock_version is None:
            raise FactError("Accepting a merge suggestion needs the target fact's lock_version")
        target = get_fact(db, matter_id=fact.matter_id, fact_id=suggestion.target_fact_id, for_update=True)  # type: ignore[arg-type]
        merge_facts(
            db,
            source=fact,
            target=target,
            source_lock_version=lock_version,
            target_lock_version=target_lock_version,
            actor=actor,
        )
        suggestion.status = "ACCEPTED"
        suggestion.resolved_by_user_id = actor.user_id
        suggestion.resolved_at = utcnow()
        _record(db, fact, "SUGGESTION_ACCEPTED", {"suggestion_id": str(suggestion.id)}, actor)
        return target
    _require_not_merged(fact)
    _check_version(fact, lock_version)
    tracker = _state_tracker(
        db,
        fact.matter_id,
        actor,
        document_ids=[uuid.UUID(item["matter_document_id"]) for item in suggestion.evidence],
    )
    revise_fact(
        db,
        fact=fact,
        lock_version=lock_version,
        changes=_deserialize_changes(suggestion.changes),
        actor=actor,
        reason=f"Accepted suggestion {suggestion.id}",
    )
    for payload in suggestion.evidence:
        item = _verified_evidence_from_payload(payload)
        duplicate = db.scalar(
            select(MatterFactEvidence.id).where(
                MatterFactEvidence.fact_id == fact.id,
                MatterFactEvidence.status == "ACTIVE",
                MatterFactEvidence.matter_document_id == item.matter_document_id,
                MatterFactEvidence.char_start == item.char_start,
                MatterFactEvidence.char_end == item.char_end,
            )
        )
        if duplicate is None:
            row = _evidence_row(fact, item, actor)
            db.add(row)
            db.flush()
            _record(db, fact, "EVIDENCE_ADDED", _evidence_payload(row), actor)
    _refresh_evidence_counts(db, fact)
    suggestion.status = "ACCEPTED"
    suggestion.resolved_by_user_id = actor.user_id
    suggestion.resolved_at = utcnow()
    _record(db, fact, "SUGGESTION_ACCEPTED", {"suggestion_id": str(suggestion.id)}, actor)
    tracker.publish()
    return fact


def dismiss_suggestion(
    db: Session,
    *,
    fact: MatterFact,
    suggestion_id: uuid.UUID,
    note: str | None,
    actor: Actor,
) -> MatterFactSuggestion:
    if actor.actor_type != "HUMAN":
        raise FactConflict("Only a person can dismiss a suggestion")
    suggestion = _pending_suggestion(db, fact, suggestion_id)
    suggestion.status = "DISMISSED"
    suggestion.resolved_by_user_id = actor.user_id
    suggestion.resolved_at = utcnow()
    suggestion.resolution_note = note
    _record(db, fact, "SUGGESTION_DISMISSED", {"suggestion_id": str(suggestion.id), "note": note}, actor)
    return suggestion
