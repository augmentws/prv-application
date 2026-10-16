"""The ``fact_state`` system metadata field: whether a document supports confirmed or working facts.

``fact_state`` is a derived ``SYSTEM`` ENUM like ``email_domain`` and ``file_date``. Core is authoritative: the value
is computed from active ``matter_fact_evidence`` and fact status, never stored per document, and projected into the
document search index. ``confirmed`` means at least one active quote on the document belongs to a confirmed fact;
``unconfirmed`` means its quotes belong only to working facts; no value means no live fact cites the document.
Rejected and merged facts do not count.

The fact command service records each command's affected documents before it writes and, after it writes, queues one
document upsert for only the documents whose state changed, so most agent activity causes no reindexing.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.metadata_profiles import DEFAULT_MATTER_METADATA_PROFILE, MetadataDefinitionTemplate
from app.models import (
    Matter,
    MatterFact,
    MatterFactEvidence,
    MetadataDefinition,
    MetadataGroup,
    MetadataGroupField,
    SearchProjectionOperation,
    WorkflowRun,
)
from app.search.operations import create_search_operation

FACT_STATE_KEY = "fact_state"
FACTS_GROUP_KEY = "facts"
CONFIRMED = "confirmed"
UNCONFIRMED = "unconfirmed"
LIVE_FACT_STATUSES = ("WORKING", "CONFIRMED")
AGENT_PROJECTION_PRIORITY = 100
_DOCUMENT_CHUNK = 1_000


class FactStateUpdateConflict(ValueError):
    """The matter already uses ``fact_state`` with an incompatible shape."""


def _template() -> MetadataDefinitionTemplate:
    return next(item for item in DEFAULT_MATTER_METADATA_PROFILE.definitions if item.key == FACT_STATE_KEY)


def document_fact_states(db: Session, document_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str | None]:
    """The derived ``fact_state`` of each document; ``None`` when no live fact cites it."""

    ids = list(dict.fromkeys(document_ids))
    states: dict[uuid.UUID, str | None] = dict.fromkeys(ids)
    for start in range(0, len(ids), _DOCUMENT_CHUNK):
        chunk = ids[start : start + _DOCUMENT_CHUNK]
        for document_id, status in db.execute(
            select(MatterFactEvidence.matter_document_id, MatterFact.status)
            .join(MatterFact, MatterFact.id == MatterFactEvidence.fact_id)
            .where(
                MatterFactEvidence.matter_document_id.in_(chunk),
                MatterFactEvidence.status == "ACTIVE",
                MatterFact.status.in_(LIVE_FACT_STATUSES),
            )
            .distinct()
        ):
            if status == "CONFIRMED":
                states[document_id] = CONFIRMED
            elif states[document_id] is None:
                states[document_id] = UNCONFIRMED
    return states


def cited_document_ids(db: Session, matter_id: uuid.UUID) -> list[uuid.UUID]:
    """Documents with at least one active quote on a live fact: the only documents that can have a state."""

    return list(
        db.scalars(
            select(MatterFactEvidence.matter_document_id)
            .join(MatterFact, MatterFact.id == MatterFactEvidence.fact_id)
            .where(
                MatterFact.matter_id == matter_id,
                MatterFactEvidence.status == "ACTIVE",
                MatterFact.status.in_(LIVE_FACT_STATUSES),
            )
            .distinct()
            .order_by(MatterFactEvidence.matter_document_id)
        )
    )


def _validate_existing(definition: MetadataDefinition) -> None:
    template = _template()
    actual = (definition.type, definition.cardinality, definition.value_source, definition.searchable, definition.facetable)
    required = (template.type, template.cardinality, template.value_source, template.searchable, template.facetable)
    option_keys = {option.get("key") for option in definition.allowed_values or []}
    if actual != required or not {CONFIRMED, UNCONFIRMED} <= option_keys:
        raise FactStateUpdateConflict(
            "The fact_state key already exists with an incompatible definition; "
            "rename that field before installing the system Facts field"
        )


def ensure_fact_state_metadata(
    db: Session,
    *,
    matter: Matter,
    actor_user_id: uuid.UUID,
    queue_backfill: bool,
) -> tuple[MetadataDefinition, bool, SearchProjectionOperation | None]:
    """Install the system ``fact_state`` definition and its Facts group on an existing matter.

    With ``queue_backfill`` the search index gains the field in place and every cited document is projected.
    """

    template = _template()
    definition = db.scalar(
        select(MetadataDefinition).where(
            MetadataDefinition.matter_id == matter.id,
            MetadataDefinition.key == FACT_STATE_KEY,
        )
    )
    created = definition is None
    if definition is None:
        definition = MetadataDefinition(
            matter_id=matter.id,
            key=template.key,
            display_name=template.display_name,
            description=template.description,
            type=template.type,
            cardinality=template.cardinality,
            allowed_values=[dict(option) for option in template.allowed_values or ()],
            value_source=template.value_source,
            reference_target=template.reference_target,
            template_key=DEFAULT_MATTER_METADATA_PROFILE.key,
            template_version=DEFAULT_MATTER_METADATA_PROFILE.version,
            assertion_policy=template.assertion_policy,
            resolution_policy=template.resolution_policy,
            searchable=template.searchable,
            facetable=template.facetable,
            normalize_to_lowercase=template.normalize_to_lowercase,
            hierarchy_separator=template.hierarchy_separator,
            reviewable=template.reviewable,
            ai_assignable=template.ai_assignable,
            status="ACTIVE",
        )
        db.add(definition)
        db.flush()
    else:
        _validate_existing(definition)
        definition.status = "ACTIVE"

    group = db.scalar(
        select(MetadataGroup).where(
            MetadataGroup.matter_id == matter.id,
            MetadataGroup.scope.in_(["SYSTEM", "MATTER"]),
            MetadataGroup.key == FACTS_GROUP_KEY,
        )
    )
    if group is None:
        group_template = next(item for item in DEFAULT_MATTER_METADATA_PROFILE.groups if item.key == FACTS_GROUP_KEY)
        group = MetadataGroup(
            matter_id=matter.id,
            scope="SYSTEM",
            owner_user_id=None,
            created_by_user_id=actor_user_id,
            key=group_template.key,
            display_name=group_template.display_name,
            description=group_template.description,
            sort_order=group_template.sort_order,
            default_table_visible=group_template.default_table_visible,
            default_document_visible=group_template.default_document_visible,
            status="ACTIVE",
            template_key=DEFAULT_MATTER_METADATA_PROFILE.key,
            template_version=DEFAULT_MATTER_METADATA_PROFILE.version,
        )
        db.add(group)
        db.flush()
    membership = db.scalar(
        select(MetadataGroupField).where(
            MetadataGroupField.metadata_group_id == group.id,
            MetadataGroupField.metadata_definition_id == definition.id,
        )
    )
    if membership is None:
        max_sort_order = db.scalar(
            select(func.max(MetadataGroupField.sort_order)).where(MetadataGroupField.metadata_group_id == group.id)
        )
        db.add(
            MetadataGroupField(
                metadata_group_id=group.id,
                metadata_definition_id=definition.id,
                sort_order=int(max_sort_order or 0) + 10,
            )
        )

    operation = None
    if queue_backfill:
        operation = create_search_operation(
            db,
            matter_id=matter.id,
            kind="SCHEMA_SYNC",
            payload={"reason": "FACT_STATE_UPDATE", "metadata_backfill_fields": [FACT_STATE_KEY]},
            created_by_user_id=actor_user_id,
        )
        record_audit(
            db,
            tenant_id=matter.client.tenant_id,
            actor_user_id=actor_user_id,
            action="metadata.fact_state.update_queued",
            target_type="matter",
            target_id=matter.id,
            details={
                "metadata_definition_id": str(definition.id),
                "definition_created": created,
                "search_operation_id": str(operation.id),
            },
        )
    return definition, created, operation


class FactStateTracker:
    """Capture documents' states before a fact command writes; afterwards, project only the ones that changed."""

    def __init__(
        self,
        db: Session,
        *,
        matter_id: uuid.UUID,
        actor_user_id: uuid.UUID | None,
        workflow_run_id: uuid.UUID | None,
        fact_ids: Sequence[uuid.UUID] = (),
        document_ids: Iterable[uuid.UUID] = (),
    ) -> None:
        self.db = db
        self.matter_id = matter_id
        self.actor_user_id = actor_user_id
        self.workflow_run_id = workflow_run_id
        documents = set(document_ids)
        if fact_ids:
            documents.update(
                db.scalars(
                    select(MatterFactEvidence.matter_document_id).where(
                        MatterFactEvidence.fact_id.in_(list(fact_ids)),
                        MatterFactEvidence.status == "ACTIVE",
                    )
                )
            )
        self.document_ids = sorted(documents, key=str)
        self.before = document_fact_states(db, self.document_ids)

    def _requesting_user(self) -> uuid.UUID | None:
        if self.actor_user_id is not None:
            return self.actor_user_id
        if self.workflow_run_id is None:
            return None
        workflow = self.db.get(WorkflowRun, self.workflow_run_id)
        return workflow.initiated_by_user_id if workflow is not None else None

    def publish(self) -> SearchProjectionOperation | None:
        """Queue one document upsert for the documents whose ``fact_state`` changed; ``None`` if none did."""

        if not self.document_ids:
            return None
        self.db.flush()
        after = document_fact_states(self.db, self.document_ids)
        changed = [document_id for document_id in self.document_ids if after[document_id] != self.before[document_id]]
        if not changed:
            return None
        installed = self.db.scalar(
            select(MetadataDefinition.id).where(
                MetadataDefinition.matter_id == self.matter_id,
                MetadataDefinition.key == FACT_STATE_KEY,
                MetadataDefinition.status == "ACTIVE",
            )
        )
        requesting_user = self._requesting_user()
        if installed is None:
            matter = self.db.get(Matter, self.matter_id)
            if matter is None or requesting_user is None:
                raise ValueError("The Facts field cannot be installed without a matter and a requesting user")
            # The first fact in an older matter: install the field; its backfill projects every cited document.
            _, _, operation = ensure_fact_state_metadata(
                self.db,
                matter=matter,
                actor_user_id=requesting_user,
                queue_backfill=True,
            )
            return operation
        return create_search_operation(
            self.db,
            matter_id=self.matter_id,
            kind="DOCUMENT_UPSERT",
            payload={"document_ids": [str(document_id) for document_id in changed], "reason": "FACT_STATE"},
            created_by_user_id=requesting_user,
            # Reviewer changes project ahead of agent activity; full rebuilds stay lowest.
            priority=None if self.workflow_run_id is None else AGENT_PROJECTION_PRIORITY,
        )
