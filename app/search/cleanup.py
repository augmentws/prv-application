"""Safety-checked cleanup for obsolete matter search index generations."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import Matter, MatterDefinitionAssessmentRun, ReviewBatch, SearchIndexGeneration
from app.search.client import OpenSearchClient


class SearchIndexCleanupSafetyError(RuntimeError):
    """Raised when index state is not safe enough for automated cleanup."""


@dataclass(frozen=True)
class SearchIndexCleanupPlan:
    matter_id: uuid.UUID
    matter_name: str
    alias_name: str
    active_index: str
    physical_indexes: tuple[str, ...]
    delete_indexes: tuple[str, ...]
    orphan_indexes: tuple[str, ...]
    stale_generation_ids: tuple[uuid.UUID, ...]
    stale_generation_indexes: tuple[str, ...]
    missing_physical_indexes: tuple[str, ...]
    preserved_assessment_indexes: tuple[str, ...]


def active_matter_ids(db: Session) -> list[uuid.UUID]:
    """Return matters that currently have an active search generation."""

    return list(
        db.scalars(
            select(Matter.id)
            .join(SearchIndexGeneration, SearchIndexGeneration.matter_id == Matter.id)
            .where(SearchIndexGeneration.status == "ACTIVE")
            .order_by(Matter.name, Matter.id)
        )
    )


def plan_search_index_cleanup(
    db: Session,
    client: OpenSearchClient,
    matter_id: uuid.UUID,
) -> SearchIndexCleanupPlan:
    """Build a cleanup plan, refusing ambiguous alias or generation state."""

    matter = db.get(Matter, matter_id)
    if matter is None:
        raise SearchIndexCleanupSafetyError(f"Matter {matter_id} does not exist")

    generations = list(
        db.scalars(
            select(SearchIndexGeneration)
            .where(SearchIndexGeneration.matter_id == matter_id)
            .order_by(SearchIndexGeneration.generation)
        )
    )
    active_generations = [generation for generation in generations if generation.status == "ACTIVE"]
    if len(active_generations) != 1:
        raise SearchIndexCleanupSafetyError(
            f"Matter {matter.name!r} has {len(active_generations)} ACTIVE generations; expected exactly one"
        )

    active = active_generations[0]
    alias_indices = client.alias_indices(active.alias_name)
    if alias_indices != [active.index_name]:
        raise SearchIndexCleanupSafetyError(
            f"Alias {active.alias_name!r} resolves to {alias_indices!r}; expected only {active.index_name!r}"
        )

    physical_indexes = tuple(client.resolve_indices(f"{active.alias_name}-v*"))
    expected_name = re.compile(rf"{re.escape(active.alias_name)}-v[0-9]{{6}}\Z")
    unexpected = sorted(index_name for index_name in physical_indexes if expected_name.fullmatch(index_name) is None)
    if unexpected:
        raise SearchIndexCleanupSafetyError(
            f"Matter {matter.name!r} has physical indexes outside the versioned naming contract: {unexpected!r}"
        )
    if active.index_name not in physical_indexes:
        raise SearchIndexCleanupSafetyError(
            f"Active index {active.index_name!r} was not returned by OpenSearch index resolution"
        )

    invalid_records = sorted(
        generation.index_name
        for generation in generations
        if expected_name.fullmatch(generation.index_name) is None
        or generation.alias_name != active.alias_name
    )
    if invalid_records:
        raise SearchIndexCleanupSafetyError(
            f"Matter {matter.name!r} has generation records outside the active naming contract: {invalid_records!r}"
        )

    pinned_generation_ids = set(
        db.scalars(
            select(MatterDefinitionAssessmentRun.search_index_generation_id).where(
                MatterDefinitionAssessmentRun.matter_id == matter.id,
                MatterDefinitionAssessmentRun.search_index_generation_id.is_not(None),
                MatterDefinitionAssessmentRun.status.not_in(
                    ["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"]
                ),
            )
        )
    )
    all_stale_generations = [generation for generation in generations if generation.id != active.id]
    stale_generations = [
        generation for generation in all_stale_generations if generation.id not in pinned_generation_ids
    ]
    stale_names = {generation.index_name for generation in stale_generations}
    all_stale_names = {generation.index_name for generation in all_stale_generations}
    preserved_names = {
        generation.index_name for generation in all_stale_generations if generation.id in pinned_generation_ids
    }
    physical_names = set(physical_indexes)
    delete_indexes = tuple(sorted(physical_names - {active.index_name} - preserved_names))

    return SearchIndexCleanupPlan(
        matter_id=matter.id,
        matter_name=matter.name,
        alias_name=active.alias_name,
        active_index=active.index_name,
        physical_indexes=physical_indexes,
        delete_indexes=delete_indexes,
        orphan_indexes=tuple(sorted(set(delete_indexes) - all_stale_names)),
        stale_generation_ids=tuple(generation.id for generation in stale_generations),
        stale_generation_indexes=tuple(sorted(stale_names)),
        missing_physical_indexes=tuple(sorted(stale_names - physical_names)),
        preserved_assessment_indexes=tuple(sorted(preserved_names)),
    )


def execute_search_index_cleanup(
    db: Session,
    client: OpenSearchClient,
    matter_id: uuid.UUID,
) -> SearchIndexCleanupPlan:
    """Revalidate and execute one matter cleanup under its projection lock."""

    _lock_matter_search(db, matter_id)
    plan = plan_search_index_cleanup(db, client, matter_id)

    try:
        for index_name in plan.delete_indexes:
            client.delete_index(index_name)

        if plan.stale_generation_ids:
            generations = list(
                db.scalars(
                    select(SearchIndexGeneration).where(
                        SearchIndexGeneration.id.in_(plan.stale_generation_ids)
                    )
                )
            )
            generation_by_id = {generation.id: generation for generation in generations}
            batches = list(
                db.scalars(
                    select(ReviewBatch).where(
                        ReviewBatch.search_index_generation_id.in_(generation_by_id)
                    )
                )
            )
            for batch in batches:
                generation = generation_by_id[batch.search_index_generation_id]
                if "search_index_generation" not in batch.selection_definition:
                    batch.selection_definition = {
                        **batch.selection_definition,
                        "search_index_generation": {
                            "generation": generation.generation,
                            "index_name": generation.index_name,
                            "schema_hash": generation.schema_hash,
                            "activated_at": (
                                generation.activated_at.isoformat() if generation.activated_at else None
                            ),
                        },
                    }
            for generation in generations:
                db.delete(generation)

        db.commit()
    except Exception:
        db.rollback()
        raise

    return plan


def _lock_matter_search(db: Session, matter_id: uuid.UUID) -> None:
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    lock_key = matter_id.int & ((1 << 63) - 1)
    db.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})
