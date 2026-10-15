"""Deterministic issue membership: which issues of a Fact Development Specification each document belongs to.

A document belongs to an issue when any of the issue's coding mappings matches the document's coding. Coding is
read from the current matter coding projection or from one reviewer's batch coding pinned at a revision, so a
prepared run is not affected by later coding changes.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.fact_specifications import FactDevelopmentSpecification, FactIssueCodingMapping
from app.metadata_hierarchy import enum_option_map, hierarchy_ancestor_keys
from app.models import DocumentMetadataCurrent, MetadataDefinition
from app.reviewer_coding import resolve_reviewer_coding_as_of

CodingSource = Literal["MATTER_METADATA", "REVIEWER_CODING"]
DOCUMENT_CHUNK_SIZE = 1_000


class FactIssueMembershipError(ValueError):
    pass


@dataclass(frozen=True)
class _CompiledMapping:
    metadata_definition_id: uuid.UUID
    match_type: str
    accepted: frozenset[Any]


def _accepted_values(definition: MetadataDefinition, mapping: FactIssueCodingMapping) -> frozenset[Any]:
    if mapping.match_type == "BOOLEAN":
        return frozenset({mapping.value})
    if mapping.match_type == "TEXT_EXACT":
        return frozenset({mapping.value})
    if not mapping.include_descendants:
        return frozenset({mapping.value})
    accepted = {mapping.value}
    for option_key in enum_option_map(definition):
        try:
            if mapping.value in hierarchy_ancestor_keys(definition, option_key):
                accepted.add(option_key)
        except ValueError as exc:
            raise FactIssueMembershipError(str(exc)) from exc
    return frozenset(accepted)


def _compile_mappings(
    db: Session,
    *,
    matter_id: uuid.UUID,
    specification: FactDevelopmentSpecification,
) -> dict[str, list[_CompiledMapping]]:
    keys = {mapping.metadata_definition_key for issue in specification.issues for mapping in issue.coding_mappings}
    definitions = {
        definition.key: definition
        for definition in db.scalars(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == matter_id,
                MetadataDefinition.key.in_(keys),
            )
        )
    }
    missing = sorted(keys - set(definitions))
    if missing:
        raise FactIssueMembershipError("Issue coding fields no longer exist: " + ", ".join(missing))
    compiled: dict[str, list[_CompiledMapping]] = {}
    for issue in specification.issues:
        compiled[issue.key] = [
            _CompiledMapping(
                metadata_definition_id=definitions[mapping.metadata_definition_key].id,
                match_type=mapping.match_type,
                accepted=_accepted_values(definitions[mapping.metadata_definition_key], mapping),
            )
            for mapping in issue.coding_mappings
        ]
    return compiled


def _stored_value(match_type: str, row: Any) -> Any:
    return row.value_boolean if match_type == "BOOLEAN" else row.value_text


def _chunks(values: Sequence[uuid.UUID]) -> Iterable[Sequence[uuid.UUID]]:
    for start in range(0, len(values), DOCUMENT_CHUNK_SIZE):
        yield values[start : start + DOCUMENT_CHUNK_SIZE]


def _coded_rows(
    db: Session,
    *,
    matter_id: uuid.UUID,
    document_ids: Sequence[uuid.UUID],
    definition_ids: Sequence[uuid.UUID],
    coding_source: CodingSource,
    reviewer_coding_id: uuid.UUID | None,
    reviewer_coding_revision: int | None,
) -> Iterable[Any]:
    for chunk in _chunks(document_ids):
        if coding_source == "MATTER_METADATA":
            yield from db.scalars(
                select(DocumentMetadataCurrent).where(
                    DocumentMetadataCurrent.matter_id == matter_id,
                    DocumentMetadataCurrent.matter_document_id.in_(chunk),
                    DocumentMetadataCurrent.metadata_definition_id.in_(definition_ids),
                    DocumentMetadataCurrent.resolution_state == "VALUE",
                )
            )
        else:
            if reviewer_coding_id is None or reviewer_coding_revision is None:
                raise FactIssueMembershipError("Reviewer coding membership requires a pinned coding revision")
            yield from resolve_reviewer_coding_as_of(
                db,
                reviewer_coding_id=reviewer_coding_id,
                revision=reviewer_coding_revision,
                document_ids=list(chunk),
                metadata_definition_ids=list(definition_ids),
            ).values


def evaluate_issue_membership(
    db: Session,
    *,
    matter_id: uuid.UUID,
    specification: FactDevelopmentSpecification,
    document_ids: Sequence[uuid.UUID],
    coding_source: CodingSource = "MATTER_METADATA",
    reviewer_coding_id: uuid.UUID | None = None,
    reviewer_coding_revision: int | None = None,
) -> dict[uuid.UUID, list[str]]:
    """Return the issue keys, in specification order, for every document that matches at least one issue."""

    compiled = _compile_mappings(db, matter_id=matter_id, specification=specification)
    mappings_by_definition: dict[uuid.UUID, list[tuple[str, _CompiledMapping]]] = defaultdict(list)
    for issue_key, mappings in compiled.items():
        for mapping in mappings:
            mappings_by_definition[mapping.metadata_definition_id].append((issue_key, mapping))

    matched: dict[uuid.UUID, set[str]] = defaultdict(set)
    for row in _coded_rows(
        db,
        matter_id=matter_id,
        document_ids=list(document_ids),
        definition_ids=list(mappings_by_definition),
        coding_source=coding_source,
        reviewer_coding_id=reviewer_coding_id,
        reviewer_coding_revision=reviewer_coding_revision,
    ):
        for issue_key, mapping in mappings_by_definition.get(row.metadata_definition_id, ()):
            if _stored_value(mapping.match_type, row) in mapping.accepted:
                matched[row.matter_document_id].add(issue_key)

    order = [issue.key for issue in specification.issues]
    return {
        document_id: [key for key in order if key in keys]
        for document_id, keys in matched.items()
    }


def issue_document_counts(
    specification: FactDevelopmentSpecification,
    membership: dict[uuid.UUID, list[str]],
) -> list[dict[str, Any]]:
    counts: dict[str, int] = defaultdict(int)
    for keys in membership.values():
        for key in keys:
            counts[key] += 1
    return [
        {"issue_key": issue.key, "name": issue.name, "document_count": counts.get(issue.key, 0)}
        for issue in specification.issues
    ]
