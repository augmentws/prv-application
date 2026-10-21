"""Validate and apply one document's fact-extraction output.

The ``fact_extraction`` skill proposes operations; nothing it returns is trusted. Each operation is checked
deterministically here (issue keys against the document's matched issues, quotes verbatim within one paragraph of
the already-loaded text, dates against their precision, ``ADD_EVIDENCE`` targets against the recalled working facts)
and then applied through the fact and entity command services as the agent. An invalid operation is recorded with
its error and skipped; it never blocks the document's valid operations.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.fact_evidence import DocumentText, FactEvidenceError, VerifiedEvidence, verify_quote
from app.facts import (
    Actor,
    FactContent,
    FactError,
    FactIssueRef,
    FactParticipantRef,
    add_evidence,
    create_fact,
    current_content,
    normalize_fact_dates,
    revise_fact,
    suggest_revision,
)
from app.matter_entities import (
    MatterEntityError,
    add_variant,
    canonical_entity,
    clean_metadata_email,
    create_entity,
    enrich_proposed_entity,
    lock_entity_email,
    normalize_entity_name,
)
from app.models import Matter, MatterEntity, MatterFact, MatterFactIssue

MAX_RECORDED_ERRORS = 20
DATE_BASES = frozenset({"STATED_IN_TEXT", "DOCUMENT_DATE", "INFERRED"})


class ExtractionEntityRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: uuid.UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=300)
    email: str | None = Field(default=None, max_length=320)
    entity_type: Literal["PERSON", "ORGANIZATION"]

    @model_validator(mode="after")
    def validate_identity(self) -> ExtractionEntityRef:
        if self.entity_id is None and not self.name and not self.email:
            raise ValueError("An entity reference requires an entity ID, name, or email address")
        return self


class ExtractionParticipant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity: ExtractionEntityRef
    role: Literal["PARTICIPANT_ACTOR", "PARTICIPANT_RECIPIENT"]


class ExtractionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paragraph_id: str
    quote: str = Field(min_length=1, max_length=5_000)
    stance: Literal["SUPPORTS", "CONTRADICTS"]


class ExtractionOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["CREATE", "ADD_EVIDENCE", "REFINE", "NO_FACT"]
    refine_fields: list[Literal["summary", "date", "owner", "participants", "issues"]] = Field(default_factory=list)
    issue_keys: list[str] = Field(default_factory=list)
    fact_id: str | None = None
    summary: str | None = Field(default=None, max_length=2_000)
    date_precision: Literal["DAY", "MONTH", "QUARTER", "YEAR", "RANGE", "UNKNOWN"] = "UNKNOWN"
    event_date_start: str | None = None
    event_date_end: str | None = None
    date_basis: str | None = None
    owner: ExtractionEntityRef | None = None
    participants: list[ExtractionParticipant] = Field(default_factory=list)
    evidence: list[ExtractionEvidence] = Field(default_factory=list)
    reason: str | None = Field(default=None, max_length=1_000)


class ExtractionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operations: list[ExtractionOperation]


class ExtractionOperationError(ValueError):
    pass


def parse_extraction_output(output: dict[str, Any], *, max_operations: int) -> ExtractionOutput:
    """Parse the skill output; a schema or size violation is a whole-output error the model can repair."""

    try:
        parsed = ExtractionOutput.model_validate(output)
    except ValidationError as exc:
        raise ValueError(f"Fact extraction output does not match its schema: {exc}") from exc
    if len(parsed.operations) > max_operations:
        raise ValueError(f"Fact extraction returned {len(parsed.operations)} operations; the limit is {max_operations}")
    return parsed


def extraction_output_validator(*, max_operations: int):
    def validate(output: dict[str, Any]) -> None:
        parse_extraction_output(output, max_operations=max_operations)

    return validate


@dataclass(frozen=True)
class ExtractionContext:
    matter: Matter
    task_id: uuid.UUID
    document: DocumentText
    matched_issues: tuple[str, ...]
    recalled_fact_ids: frozenset[uuid.UUID]
    known_entity_ids: frozenset[uuid.UUID]
    actor: Actor


@dataclass(frozen=True)
class CreateDecision:
    """The consolidator's ruling on one CREATE: create it, add its quotes to an existing fact, or block it."""

    action: Literal["CREATE", "ADD_TO_EXISTING", "BLOCKED_BY_REJECTED"]
    fact_id: uuid.UUID | None = None
    similarity: float | None = None
    rationale: str | None = None
    # Facts the ruling already compared against; a recheck only reconsiders facts outside this set.
    considered_fact_ids: frozenset[uuid.UUID] = frozenset()


@dataclass
class ExtractionResult:
    created_fact_ids: list[uuid.UUID] = field(default_factory=list)
    evidence_fact_ids: list[uuid.UUID] = field(default_factory=list)
    merged_fact_ids: list[uuid.UUID] = field(default_factory=list)
    blocked_count: int = 0
    refined_fact_ids: list[uuid.UUID] = field(default_factory=list)
    suggestion_ids: list[uuid.UUID] = field(default_factory=list)
    no_fact_issue_keys: list[str] = field(default_factory=list)
    proposed_entity_ids: list[uuid.UUID] = field(default_factory=list)
    invalid_count: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def applied_count(self) -> int:
        return (
            len(self.created_fact_ids)
            + len(self.evidence_fact_ids)
            + len(self.merged_fact_ids)
            + len(self.refined_fact_ids)
            + len(self.suggestion_ids)
        )

    def record_error(self, index: int, operation: ExtractionOperation, error: str) -> None:
        self.invalid_count += 1
        if len(self.errors) < MAX_RECORDED_ERRORS:
            self.errors.append({"operation": index, "op": operation.op, "error": " ".join(error.split())[:500]})


def _issue_refs(context: ExtractionContext, operation: ExtractionOperation) -> list[FactIssueRef]:
    if not operation.issue_keys:
        raise ExtractionOperationError("The operation names no issue")
    keys = list(dict.fromkeys(operation.issue_keys))
    outside = [key for key in keys if key not in context.matched_issues]
    if outside:
        raise ExtractionOperationError("Issues the document is not coded to: " + ", ".join(outside))
    return [FactIssueRef(matter_analysis_task_id=context.task_id, issue_key=key) for key in keys]


def _verified_evidence(context: ExtractionContext, operation: ExtractionOperation) -> list[VerifiedEvidence]:
    if not operation.evidence:
        raise ExtractionOperationError("The operation cites no quote")
    verified: list[VerifiedEvidence] = []
    seen: set[tuple[int, int]] = set()
    for item in operation.evidence:
        try:
            evidence = verify_quote(
                context.document,
                paragraph_id=item.paragraph_id,
                quote=item.quote,
                stance=item.stance,
            )
        except FactEvidenceError as exc:
            raise ExtractionOperationError(str(exc)) from exc
        if (evidence.char_start, evidence.char_end) not in seen:
            seen.add((evidence.char_start, evidence.char_end))
            verified.append(evidence)
    return verified


def _dates(operation: ExtractionOperation) -> tuple[date | None, date | None, str | None]:
    try:
        start = date.fromisoformat(operation.event_date_start) if operation.event_date_start else None
        end = date.fromisoformat(operation.event_date_end) if operation.event_date_end else None
    except ValueError as exc:
        raise ExtractionOperationError(f"Invalid date: {exc}") from exc
    try:
        start, end = normalize_fact_dates(operation.date_precision, start, end)
    except FactError as exc:
        raise ExtractionOperationError(str(exc)) from exc
    if operation.date_precision == "UNKNOWN":
        return None, None, None
    if operation.date_basis not in DATE_BASES:
        raise ExtractionOperationError("A dated fact needs date_basis STATED_IN_TEXT, DOCUMENT_DATE or INFERRED")
    return start, end, operation.date_basis


def _lock_entity_name(db: Session, matter_id: uuid.UUID, key: str) -> None:
    """Serialize entity creation per matter and normalized name so parallel chunks cannot create duplicates."""

    if db.get_bind().dialect.name != "postgresql":
        return
    digest = hashlib.sha256(f"matter-entity:{matter_id}:{key}".encode()).digest()
    lock_key = int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)
    db.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})


class EntityResolver:
    """Resolve the agent's people and organizations: email first, then a unique exact name, else a new PROPOSED one."""

    def __init__(self, db: Session, context: ExtractionContext, result: ExtractionResult) -> None:
        self.db = db
        self.context = context
        self.result = result

    def _usable(self, entity: MatterEntity) -> MatterEntity | None:
        canonical = canonical_entity(self.db, entity)
        return None if canonical.status == "REJECTED" else entity

    def resolve(self, reference: ExtractionEntityRef) -> uuid.UUID | None:
        matter_id = self.context.matter.id
        email = clean_metadata_email(reference.email) if reference.email else None
        if reference.email and email is None:
            return None
        if reference.entity_id is not None:
            if reference.entity_id not in self.context.known_entity_ids:
                return None
            by_id = self.db.scalar(
                select(MatterEntity).where(
                    MatterEntity.id == reference.entity_id,
                    MatterEntity.matter_id == matter_id,
                )
            )
            if by_id is not None:
                usable = self._usable(by_id)
                if usable is not None:
                    canonical = canonical_entity(self.db, usable)
                    if canonical.entity_type != reference.entity_type and not (
                        canonical.status == "PROPOSED" and canonical.display_name is None
                    ):
                        return None
                    if email is not None and usable.normalized_email != email:
                        return None
                    enrich_proposed_entity(
                        self.db,
                        entity=usable,
                        display_name=reference.name,
                        entity_type=reference.entity_type,
                        actor_type="AGENT",
                        workflow_run_id=self.context.actor.workflow_run_id,
                    )
                    return usable.id
        if email is not None:
            lock_entity_email(self.db, matter_id=matter_id, normalized_email=email)
            by_email = self.db.scalar(
                select(MatterEntity).where(MatterEntity.matter_id == matter_id, MatterEntity.normalized_email == email)
            )
            if by_email is not None:
                usable = self._usable(by_email)
                if usable is not None:
                    enrich_proposed_entity(
                        self.db,
                        entity=usable,
                        display_name=reference.name,
                        entity_type=reference.entity_type,
                        actor_type="AGENT",
                        workflow_run_id=self.context.actor.workflow_run_id,
                    )
                return usable.id if usable is not None else None
        if not reference.name:
            if email is None:
                return None
            entity = create_entity(
                self.db,
                matter=self.context.matter,
                entity_type=reference.entity_type,
                display_name=None,
                email=email,
                status="PROPOSED",
                actor_type="AGENT",
                workflow_run_id=self.context.actor.workflow_run_id,
            )
            self.result.proposed_entity_ids.append(entity.id)
            return entity.id
        try:
            name = normalize_entity_name(reference.name)
        except MatterEntityError:
            return None
        _lock_entity_name(self.db, matter_id, f"name:{name}")
        by_name = list(
            self.db.scalars(
                select(MatterEntity).where(
                    MatterEntity.matter_id == matter_id,
                    MatterEntity.normalized_name == name,
                    MatterEntity.entity_type == reference.entity_type,
                )
            )
        )
        canonicals = {canonical_entity(self.db, row).id: row for row in by_name}
        if len(canonicals) == 1:
            row = next(iter(canonicals.values()))
            canonical = canonical_entity(self.db, row)
            if canonical.status == "REJECTED":
                return None
            if email is not None:
                # A known name seen with a new email address: record it as a variant of the same entity.
                variant = add_variant(
                    self.db,
                    canonical=canonical,
                    display_name=reference.name,
                    email=email,
                    actor_type="AGENT",
                    workflow_run_id=self.context.actor.workflow_run_id,
                )
                return variant.id
            return row.id
        if len(canonicals) > 1:
            # Ambiguous: several entities share the name. Leave the reference unresolved rather than guess.
            return None
        entity = create_entity(
            self.db,
            matter=self.context.matter,
            entity_type=reference.entity_type,
            display_name=reference.name,
            email=email,
            status="PROPOSED",
            actor_type="AGENT",
            workflow_run_id=self.context.actor.workflow_run_id,
        )
        self.result.proposed_entity_ids.append(entity.id)
        return entity.id


def _participants(resolver: EntityResolver, operation: ExtractionOperation) -> list[FactParticipantRef]:
    refs: dict[tuple[uuid.UUID, str], FactParticipantRef] = {}
    for participant in operation.participants:
        entity_id = resolver.resolve(participant.entity)
        if entity_id is not None:
            refs.setdefault((entity_id, participant.role), FactParticipantRef(entity_id=entity_id, role=participant.role))
    return list(refs.values())


def _apply_create(
    db: Session,
    context: ExtractionContext,
    resolver: EntityResolver,
    operation: ExtractionOperation,
    result: ExtractionResult,
) -> None:
    if not operation.summary or not operation.summary.strip():
        raise ExtractionOperationError("A new fact needs a summary")
    issues = _issue_refs(context, operation)
    evidence = _verified_evidence(context, operation)
    if not any(item.stance == "SUPPORTS" for item in evidence):
        raise ExtractionOperationError("A new fact needs at least one supporting quote")
    start, end, basis = _dates(operation)
    proposed_mark = len(result.proposed_entity_ids)
    try:
        with db.begin_nested():
            owner_id = resolver.resolve(operation.owner) if operation.owner is not None else None
            fact = create_fact(
                db,
                matter=context.matter,
                content=FactContent(
                    summary=operation.summary,
                    date_precision=operation.date_precision,
                    event_date_start=start,
                    event_date_end=end,
                    date_basis=basis,  # type: ignore[arg-type]
                    owner_entity_id=owner_id,
                    issues=issues,
                    participants=_participants(resolver, operation),
                ),
                evidence=evidence,
                actor=context.actor,
            )
    except Exception:
        # The savepoint rolled back any entities proposed for this operation.
        del result.proposed_entity_ids[proposed_mark:]
        raise
    result.created_fact_ids.append(fact.id)


def _recalled_target(context: ExtractionContext, operation: ExtractionOperation) -> uuid.UUID:
    try:
        fact_id = uuid.UUID(operation.fact_id or "")
    except ValueError as exc:
        raise ExtractionOperationError(f"{operation.op} needs the fact_id of a recalled fact") from exc
    if fact_id not in context.recalled_fact_ids:
        raise ExtractionOperationError(f"{operation.op} can only target a fact recalled for this document")
    return fact_id


def _shared_issue_check(db: Session, context: ExtractionContext, fact_id: uuid.UUID, issue_keys: set[str]) -> None:
    fact_issue_keys = set(
        db.scalars(
            select(MatterFactIssue.issue_key).where(
                MatterFactIssue.fact_id == fact_id,
                MatterFactIssue.matter_analysis_task_id == context.task_id,
            )
        )
    )
    if not issue_keys & fact_issue_keys:
        raise ExtractionOperationError("The fact does not support any issue this document is coded to")


def _locked_fact(db: Session, context: ExtractionContext, fact_id: uuid.UUID) -> MatterFact:
    fact = db.scalar(
        select(MatterFact)
        .where(MatterFact.id == fact_id, MatterFact.matter_id == context.matter.id)
        .with_for_update()
    )
    if fact is None:
        raise ExtractionOperationError("The fact no longer exists")
    return fact


def _add_quotes(db: Session, context: ExtractionContext, fact: MatterFact, evidence: list[VerifiedEvidence]) -> int:
    if fact.status not in {"WORKING", "CONFIRMED"}:
        # Rejected facts are tombstones and merged facts are frozen; agents never extend them.
        raise ExtractionOperationError(f"Evidence cannot be added to a {fact.status.lower()} fact")
    added = 0
    for item in evidence:
        try:
            with db.begin_nested():
                add_evidence(db, fact=fact, lock_version=fact.lock_version, evidence=item, actor=context.actor)
            added += 1
        except FactError:
            continue  # Already cited for this fact.
    return added


def _apply_add_evidence(
    db: Session,
    context: ExtractionContext,
    operation: ExtractionOperation,
    result: ExtractionResult,
) -> None:
    fact_id = _recalled_target(context, operation)
    issue_keys = {ref.issue_key for ref in _issue_refs(context, operation)}
    _shared_issue_check(db, context, fact_id, issue_keys)
    evidence = _verified_evidence(context, operation)
    with db.begin_nested():
        fact = _locked_fact(db, context, fact_id)
        if _add_quotes(db, context, fact, evidence) == 0:
            raise ExtractionOperationError("Every quote is already evidence for the fact")
    result.evidence_fact_ids.append(fact.id)


def _refine_changes(
    db: Session,
    context: ExtractionContext,
    resolver: EntityResolver,
    operation: ExtractionOperation,
    fact: MatterFact,
) -> dict[str, Any]:
    if not operation.refine_fields:
        raise ExtractionOperationError("REFINE needs refine_fields naming the corrected fields")
    changes: dict[str, Any] = {}
    fields = set(operation.refine_fields)
    if "summary" in fields:
        if not operation.summary or not operation.summary.strip():
            raise ExtractionOperationError("A summary refinement needs the corrected summary")
        changes["summary"] = operation.summary
    if "date" in fields:
        start, end, basis = _dates(operation)
        changes.update(
            date_precision=operation.date_precision,
            event_date_start=start,
            event_date_end=end,
            date_basis=basis,
        )
    if "owner" in fields:
        if operation.owner is None:
            raise ExtractionOperationError("An owner refinement needs the corrected owner")
        changes["owner_entity_id"] = resolver.resolve(operation.owner)
    if "participants" in fields:
        changes["participants"] = _participants(resolver, operation)
    if "issues" in fields:
        # Refinement adds issues this document is coded to; it never removes issues other documents support.
        current = current_content(db, fact).issues
        added = [ref for ref in _issue_refs(context, operation) if ref not in current]
        changes["issues"] = [*current, *added]
    return changes


def _apply_refine(
    db: Session,
    context: ExtractionContext,
    resolver: EntityResolver,
    operation: ExtractionOperation,
    result: ExtractionResult,
) -> None:
    fact_id = _recalled_target(context, operation)
    issue_keys = {ref.issue_key for ref in _issue_refs(context, operation)}
    _shared_issue_check(db, context, fact_id, issue_keys)
    evidence = _verified_evidence(context, operation)
    proposed_mark = len(result.proposed_entity_ids)
    try:
        with db.begin_nested():
            fact = _locked_fact(db, context, fact_id)
            if fact.status not in {"WORKING", "CONFIRMED"}:
                raise ExtractionOperationError(f"A {fact.status.lower()} fact cannot be refined")
            changes = _refine_changes(db, context, resolver, operation, fact)
            if fact.status == "WORKING":
                revise_fact(
                    db,
                    fact=fact,
                    lock_version=fact.lock_version,
                    changes=changes,
                    actor=context.actor,
                    reason=operation.reason or "Refined from a later document",
                )
                _add_quotes(db, context, fact, evidence)
                result.refined_fact_ids.append(fact.id)
            else:
                suggestion = suggest_revision(
                    db,
                    fact=fact,
                    changes=changes,
                    evidence=evidence,
                    rationale=operation.reason,
                    actor=context.actor,
                )
                result.suggestion_ids.append(suggestion.id)
    except Exception:
        del result.proposed_entity_ids[proposed_mark:]
        raise


def _apply_merge_into(
    db: Session,
    context: ExtractionContext,
    operation: ExtractionOperation,
    decision: CreateDecision,
    result: ExtractionResult,
) -> None:
    """A CREATE the consolidator judged to be an existing fact: attach its quotes to that fact instead."""

    evidence = _verified_evidence(context, operation)
    with db.begin_nested():
        fact = _locked_fact(db, context, decision.fact_id)  # type: ignore[arg-type]
        _add_quotes(db, context, fact, evidence)
    result.merged_fact_ids.append(fact.id)


def apply_extraction(
    db: Session,
    context: ExtractionContext,
    output: ExtractionOutput,
    decisions: dict[int, CreateDecision] | None = None,
) -> ExtractionResult:
    """Apply valid operations in order; record invalid ones. The caller owns the transaction.

    ``decisions`` holds the consolidator's ruling for each CREATE, keyed by operation index; a CREATE without one
    is created.
    """

    result = ExtractionResult()
    resolver = EntityResolver(db, context, result)
    for index, operation in enumerate(output.operations):
        try:
            if operation.op == "NO_FACT":
                result.no_fact_issue_keys.extend(
                    key for key in _issue_refs_keys(context, operation) if key not in result.no_fact_issue_keys
                )
            elif operation.op == "CREATE":
                decision = (decisions or {}).get(index, CreateDecision("CREATE"))
                if decision.action == "BLOCKED_BY_REJECTED":
                    # Not an agent error: the consolidator matched a fact a reviewer already rejected.
                    result.blocked_count += 1
                elif decision.action == "ADD_TO_EXISTING":
                    _apply_merge_into(db, context, operation, decision, result)
                else:
                    _apply_create(db, context, resolver, operation, result)
            elif operation.op == "REFINE":
                _apply_refine(db, context, resolver, operation, result)
            else:
                _apply_add_evidence(db, context, operation, result)
        except (ExtractionOperationError, FactError, MatterEntityError) as exc:
            result.record_error(index, operation, str(exc))
        except SQLAlchemyError as exc:
            result.record_error(index, operation, f"Database error: {type(exc).__name__}")
    return result


def _issue_refs_keys(context: ExtractionContext, operation: ExtractionOperation) -> list[str]:
    return [ref.issue_key for ref in _issue_refs(context, operation)]
