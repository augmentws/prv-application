"""Email-domain metadata derivation and matter-profile upgrades."""

from __future__ import annotations

import uuid
from email.utils import getaddresses

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.metadata_profiles import DEFAULT_MATTER_METADATA_PROFILE, MetadataDefinitionTemplate
from app.models import Matter, MetadataDefinition, MetadataGroup, MetadataGroupField, SearchProjectionOperation
from app.search.operations import create_search_operation

EMAIL_DOMAIN_KEY = "email_domain"
EMAIL_GROUP_KEY = "email"


class EmailDomainUpdateConflict(ValueError):
    """The matter already uses ``email_domain`` with an incompatible shape."""


def _email_domain_template() -> MetadataDefinitionTemplate:
    return next(
        definition
        for definition in DEFAULT_MATTER_METADATA_PROFILE.definitions
        if definition.key == EMAIL_DOMAIN_KEY
    )


def extract_email_domains(
    email_sender: str | None,
    email_recipients: dict[str, list[str]] | None,
) -> list[str]:
    """Return sorted, unique domains found in structured email participants.

    Display-name forms (``Person <person@example.com>``), comma-separated
    addresses, mixed case, and internationalized domain names are normalized.
    Missing and malformed participant values contribute no domain.
    """

    values: list[str] = []
    if email_sender:
        values.append(email_sender)
    for recipient_values in (email_recipients or {}).values():
        values.extend(value for value in recipient_values if value)

    domains: set[str] = set()
    for _display_name, address in getaddresses([value.replace(";", ",") for value in values]):
        local_part, separator, raw_domain = address.strip().rpartition("@")
        if not separator or not local_part or not raw_domain:
            continue
        domain = raw_domain.strip().strip("[]").rstrip(".").casefold()
        if not domain or any(character.isspace() for character in domain):
            continue
        try:
            domain = domain.encode("idna").decode("ascii")
        except UnicodeError:
            continue
        domains.add(domain)
    return sorted(domains)


def _validate_existing_definition(definition: MetadataDefinition) -> None:
    expected = _email_domain_template()
    actual = {
        "type": definition.type,
        "cardinality": definition.cardinality,
        "value_source": definition.value_source,
        "searchable": definition.searchable,
        "facetable": definition.facetable,
        "normalize_to_lowercase": definition.normalize_to_lowercase,
    }
    required = {
        "type": expected.type,
        "cardinality": expected.cardinality,
        "value_source": expected.value_source,
        "searchable": expected.searchable,
        "facetable": expected.facetable,
        "normalize_to_lowercase": expected.normalize_to_lowercase,
    }
    if actual != required:
        raise EmailDomainUpdateConflict(
            "The email_domain key already exists with an incompatible definition; "
            "rename that field before installing the system Email Domain field"
        )


def ensure_email_domain_metadata(
    db: Session,
    *,
    matter: Matter,
    actor_user_id: uuid.UUID,
    queue_backfill: bool = True,
) -> tuple[MetadataDefinition, bool, SearchProjectionOperation | None]:
    """Install the system field/group membership and optionally queue a refresh."""

    template = _email_domain_template()
    definition = db.scalar(
        select(MetadataDefinition).where(
            MetadataDefinition.matter_id == matter.id,
            MetadataDefinition.key == EMAIL_DOMAIN_KEY,
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
            allowed_values=None,
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
        _validate_existing_definition(definition)
        definition.status = "ACTIVE"

    group = db.scalar(
        select(MetadataGroup).where(
            MetadataGroup.matter_id == matter.id,
            MetadataGroup.scope.in_(["SYSTEM", "MATTER"]),
            MetadataGroup.key == EMAIL_GROUP_KEY,
        )
    )
    if group is None:
        group_template = next(
            item for item in DEFAULT_MATTER_METADATA_PROFILE.groups if item.key == EMAIL_GROUP_KEY
        )
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
            select(func.max(MetadataGroupField.sort_order)).where(
                MetadataGroupField.metadata_group_id == group.id
            )
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
            payload={
                "reason": "EMAIL_DOMAIN_UPDATE",
                "metadata_backfill_fields": [EMAIL_DOMAIN_KEY],
            },
            created_by_user_id=actor_user_id,
        )
        record_audit(
            db,
            tenant_id=matter.client.tenant_id,
            actor_user_id=actor_user_id,
            action="metadata.email_domains.update_queued",
            target_type="matter",
            target_id=matter.id,
            details={
                "metadata_definition_id": str(definition.id),
                "definition_created": created,
                "search_operation_id": str(operation.id),
            },
        )
    return definition, created, operation
