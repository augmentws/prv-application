"""Versioned metadata profiles materialized when a matter is created."""

import uuid
from dataclasses import dataclass
from typing import Any

from app.models import MetadataDefinition, MetadataGroup, MetadataGroupField


@dataclass(frozen=True, slots=True)
class MetadataDefinitionTemplate:
    key: str
    display_name: str
    description: str
    type: str
    value_source: str
    cardinality: str = "SINGLE"
    allowed_values: tuple[dict[str, Any], ...] | None = None
    reference_target: str | None = None
    searchable: bool = True
    facetable: bool = False
    reviewable: bool = True
    ai_assignable: bool = False
    assertion_policy: str = "IMMEDIATE"
    resolution_policy: str = "EXPLICIT_ONLY"


@dataclass(frozen=True, slots=True)
class MetadataGroupTemplate:
    key: str
    display_name: str
    description: str
    field_keys: tuple[str, ...]
    sort_order: int
    default_table_visible: bool
    default_document_visible: bool


@dataclass(frozen=True, slots=True)
class MetadataProfile:
    key: str
    version: int
    definitions: tuple[MetadataDefinitionTemplate, ...]
    groups: tuple[MetadataGroupTemplate, ...]

    @property
    def identifier(self) -> str:
        return f"{self.key}-v{self.version}"


def enum_option(key: str, label: str, description: str | None = None) -> dict[str, Any]:
    return {"key": key, "label": label, "description": description, "active": True}


DEFAULT_MATTER_METADATA_PROFILE = MetadataProfile(
    key="edrm-core",
    version=1,
    definitions=(
        MetadataDefinitionTemplate(
            "control_number", "Control number", "Unique control identifier supplied with or assigned to the document.", "TEXT", "IMPORTED"
        ),
        MetadataDefinitionTemplate(
            "original_filename", "Original filename", "Filename recorded for the source item before import.", "TEXT", "IMPORTED"
        ),
        MetadataDefinitionTemplate(
            "document_title", "Document title", "Source-system document title when one is available.", "TEXT", "IMPORTED"
        ),
        MetadataDefinitionTemplate(
            "family_id", "Family ID", "Stable identifier shared by a parent document and all members of its family.", "TEXT", "SYSTEM", facetable=True
        ),
        MetadataDefinitionTemplate(
            "custodian",
            "Custodian",
            "Client-scoped person, account, device, or source from which the document was collected.",
            "TEXT",
            "SYSTEM",
            cardinality="MULTIPLE",
            reference_target="CUSTODIAN",
            facetable=True,
        ),
        MetadataDefinitionTemplate(
            "source_path", "Source path", "Original folder path or source-system location of the collected item.", "LONG_TEXT", "IMPORTED"
        ),
        MetadataDefinitionTemplate(
            "email_from", "Email from", "Sender address or display value recorded on an email.", "TEXT", "IMPORTED", facetable=True
        ),
        MetadataDefinitionTemplate(
            "email_to", "Email to", "Primary email recipients.", "TEXT", "IMPORTED", cardinality="MULTIPLE", facetable=True
        ),
        MetadataDefinitionTemplate(
            "email_cc", "Email CC", "Carbon-copy email recipients.", "TEXT", "IMPORTED", cardinality="MULTIPLE", facetable=True
        ),
        MetadataDefinitionTemplate(
            "email_bcc", "Email BCC", "Blind-carbon-copy email recipients.", "TEXT", "IMPORTED", cardinality="MULTIPLE", facetable=True
        ),
        MetadataDefinitionTemplate(
            "email_subject", "Email subject", "Subject line recorded on an email.", "TEXT", "IMPORTED"
        ),
        MetadataDefinitionTemplate(
            "email_sent_at", "Email sent at", "Date and time the email was transmitted, normalized to an unambiguous instant.", "DATETIME", "IMPORTED", facetable=True
        ),
        MetadataDefinitionTemplate(
            "email_received_at", "Email received at", "Date and time the email was received, normalized to an unambiguous instant.", "DATETIME", "IMPORTED", facetable=True
        ),
        MetadataDefinitionTemplate(
            "file_extension", "File extension", "Original filename extension without changing the source filename.", "TEXT", "IMPORTED", facetable=True
        ),
        MetadataDefinitionTemplate(
            "file_size", "File size", "Size of the native file in bytes.", "INTEGER", "IMPORTED"
        ),
        MetadataDefinitionTemplate(
            "page_count", "Page count", "Number of pages when a reliable page count is available.", "INTEGER", "IMPORTED"
        ),
        MetadataDefinitionTemplate(
            "md5_hash", "MD5 hash", "MD5 digest supplied for source identification or legacy comparison.", "TEXT", "IMPORTED"
        ),
        MetadataDefinitionTemplate(
            "sha256_hash", "SHA-256 hash", "SHA-256 digest used to verify native content integrity.", "TEXT", "IMPORTED"
        ),
        MetadataDefinitionTemplate(
            "source_created_at", "Source created at", "Creation timestamp reported by the source system.", "DATETIME", "IMPORTED", facetable=True
        ),
        MetadataDefinitionTemplate(
            "source_modified_at", "Source modified at", "Last-modified timestamp reported by the source system.", "DATETIME", "IMPORTED", facetable=True
        ),
        MetadataDefinitionTemplate(
            "responsiveness",
            "Responsiveness",
            "Review decision describing whether the document is responsive to the matter.",
            "ENUM",
            "ASSERTED",
            allowed_values=(
                enum_option("responsive", "Responsive"),
                enum_option("not_responsive", "Not responsive"),
                enum_option("needs_review", "Needs review"),
            ),
            facetable=True,
            ai_assignable=True,
        ),
        MetadataDefinitionTemplate(
            "privilege",
            "Privilege",
            "Review decision describing whether the document may contain privileged material.",
            "ENUM",
            "ASSERTED",
            allowed_values=(
                enum_option("privileged", "Privileged"),
                enum_option("not_privileged", "Not privileged"),
                enum_option("needs_review", "Needs review"),
            ),
            facetable=True,
            ai_assignable=True,
        ),
        MetadataDefinitionTemplate(
            "key_document", "Key document", "Marks a document as important to the matter.", "BOOLEAN", "ASSERTED", facetable=True, ai_assignable=True
        ),
        MetadataDefinitionTemplate(
            "review_notes", "Review notes", "Reviewer or agent narrative notes about the document.", "LONG_TEXT", "ASSERTED", ai_assignable=True
        ),
    ),
    groups=(
        MetadataGroupTemplate(
            "identification",
            "Identification",
            "Core identifiers and source names.",
            ("control_number", "original_filename", "document_title"),
            10,
            True,
            True,
        ),
        MetadataGroupTemplate(
            "family",
            "Family",
            "Document family relationship.",
            ("family_id",),
            20,
            True,
            True,
        ),
        MetadataGroupTemplate(
            "custodian_source",
            "Custodian & Source",
            "Custodians and original source location.",
            ("custodian", "source_path"),
            30,
            True,
            True,
        ),
        MetadataGroupTemplate(
            "email",
            "Email",
            "Email sender, recipients, subject, and message dates.",
            (
                "email_from",
                "email_to",
                "email_cc",
                "email_bcc",
                "email_subject",
                "email_sent_at",
                "email_received_at",
            ),
            40,
            False,
            True,
        ),
        MetadataGroupTemplate(
            "file_details",
            "File Details",
            "File characteristics and integrity hashes.",
            ("file_extension", "file_size", "page_count", "md5_hash", "sha256_hash"),
            50,
            False,
            True,
        ),
        MetadataGroupTemplate(
            "source_dates",
            "Source Dates",
            "Creation and modification dates reported by the source system.",
            ("source_created_at", "source_modified_at"),
            60,
            False,
            True,
        ),
        MetadataGroupTemplate(
            "review_coding",
            "Review Coding",
            "Responsiveness, privilege, importance, and review notes.",
            ("responsiveness", "privilege", "key_document", "review_notes"),
            70,
            True,
            True,
        ),
    ),
)


def instantiate_metadata_profile(
    matter_id: uuid.UUID,
    profile: MetadataProfile = DEFAULT_MATTER_METADATA_PROFILE,
) -> list[MetadataDefinition]:
    """Create independent ORM rows from an immutable profile template."""
    return [
        MetadataDefinition(
            id=uuid.uuid4(),
            matter_id=matter_id,
            key=template.key,
            display_name=template.display_name,
            description=template.description,
            type=template.type,
            cardinality=template.cardinality,
            allowed_values=[dict(option) for option in template.allowed_values] if template.allowed_values else None,
            value_source=template.value_source,
            reference_target=template.reference_target,
            template_key=profile.key,
            template_version=profile.version,
            assertion_policy=template.assertion_policy,
            resolution_policy=template.resolution_policy,
            searchable=template.searchable,
            facetable=template.facetable,
            reviewable=template.reviewable,
            ai_assignable=template.ai_assignable,
            status="ACTIVE",
        )
        for template in profile.definitions
    ]


@dataclass(slots=True)
class MatterConfigurationRows:
    definitions: list[MetadataDefinition]
    groups: list[MetadataGroup]
    group_fields: list[MetadataGroupField]


def instantiate_default_configuration(
    matter_id: uuid.UUID,
    created_by_user_id: uuid.UUID,
    profile: MetadataProfile = DEFAULT_MATTER_METADATA_PROFILE,
) -> MatterConfigurationRows:
    definitions = instantiate_metadata_profile(matter_id, profile)
    definitions_by_key = {definition.key: definition for definition in definitions}
    groups: list[MetadataGroup] = []
    group_fields: list[MetadataGroupField] = []

    for template in profile.groups:
        group = MetadataGroup(
            id=uuid.uuid4(),
            matter_id=matter_id,
            scope="SYSTEM",
            owner_user_id=None,
            created_by_user_id=created_by_user_id,
            key=template.key,
            display_name=template.display_name,
            description=template.description,
            sort_order=template.sort_order,
            default_table_visible=template.default_table_visible,
            default_document_visible=template.default_document_visible,
            status="ACTIVE",
            template_key=profile.key,
            template_version=profile.version,
        )
        groups.append(group)
        group_fields.extend(
            MetadataGroupField(
                metadata_group_id=group.id,
                metadata_definition_id=definitions_by_key[field_key].id,
                sort_order=index * 10,
            )
            for index, field_key in enumerate(template.field_keys, start=1)
        )

    return MatterConfigurationRows(definitions=definitions, groups=groups, group_fields=group_fields)
