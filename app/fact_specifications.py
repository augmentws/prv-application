"""Fact Development Specification: the reviewed issue catalog a fact-development task executes.

The specification is compiled from a pinned Review Guidance revision plus the task's own fact-development
guidance. Each issue maps to the matter metadata field and value that holds that issue's coding, so documents can
be assigned to issues deterministically at run time. Validation here is deterministic and runs both when a
specification is attached to a draft task version and again at publication.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MatterAnalysisTaskVersion, MatterDefinitionRevision, MetadataDefinition

FACT_DEVELOPMENT_SPECIFICATION_VERSION = "fact-development-specification-v1"
MAPPABLE_METADATA_TYPES = frozenset({"ENUM", "BOOLEAN", "TEXT", "LONG_TEXT"})
MATCH_TYPE_BY_METADATA_TYPE = {
    "ENUM": "ENUM_VALUE",
    "BOOLEAN": "BOOLEAN",
    "TEXT": "TEXT_EXACT",
    "LONG_TEXT": "TEXT_EXACT",
}

FactKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,99}$")]
ContentHash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
FactSourceKind = Literal["REVIEW_GUIDANCE", "TASK_DEFINITION"]


class FactSpecificationError(ValueError):
    pass


class FactSourceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: FactSourceKind
    heading: str | None
    excerpt_hash: ContentHash


class FactIssueCodingMapping(BaseModel):
    """How one issue's coding is stored: a document matches when its field holds this value."""

    model_config = ConfigDict(extra="forbid")

    metadata_definition_key: FactKey
    match_type: Literal["ENUM_VALUE", "BOOLEAN", "TEXT_EXACT"]
    value: str | bool
    include_descendants: bool = True

    @model_validator(mode="after")
    def validate_value_shape(self) -> FactIssueCodingMapping:
        if self.match_type == "BOOLEAN":
            if not isinstance(self.value, bool):
                raise ValueError("A BOOLEAN mapping requires a true or false value")
        elif not isinstance(self.value, str) or not self.value.strip():
            raise ValueError(f"A {self.match_type} mapping requires a non-empty string value")
        return self


class FactIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: FactKey
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    fact_guidance: str = Field(min_length=1, max_length=20_000)
    source_refs: list[FactSourceReference] = Field(min_length=1, max_length=50)
    coding_mappings: list[FactIssueCodingMapping] = Field(min_length=1, max_length=50)
    renamed_from: FactKey | None = None

    @model_validator(mode="after")
    def validate_unique_mappings(self) -> FactIssue:
        identities = [
            (mapping.metadata_definition_key, mapping.match_type, mapping.value) for mapping in self.coding_mappings
        ]
        if len(identities) != len(set(identities)):
            raise ValueError(f"Issue {self.key} repeats a coding mapping")
        if self.renamed_from == self.key:
            raise ValueError(f"Issue {self.key} cannot be renamed from itself")
        return self


class FactDevelopmentLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recalled_facts_per_issue: int = Field(default=20, ge=1, le=100)
    max_operations_per_document: int = Field(default=25, ge=1, le=200)


class FactDevelopmentSpecification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["fact-development-specification-v1"] = FACT_DEVELOPMENT_SPECIFICATION_VERSION
    summary: str = Field(min_length=1, max_length=20_000)
    date_rules: str = Field(min_length=1, max_length=10_000)
    owner_rules: str = Field(min_length=1, max_length=10_000)
    issues: list[FactIssue] = Field(min_length=1, max_length=200)
    limits: FactDevelopmentLimits = Field(default_factory=FactDevelopmentLimits)

    @model_validator(mode="after")
    def validate_unique_issue_keys(self) -> FactDevelopmentSpecification:
        keys = [issue.key for issue in self.issues]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError("Issue keys must be unique: " + ", ".join(duplicates))
        renamed = [issue.renamed_from for issue in self.issues if issue.renamed_from is not None]
        if len(renamed) != len(set(renamed)):
            raise ValueError("Two issues cannot be renamed from the same prior key")
        collisions = sorted(set(renamed) & set(keys))
        if collisions:
            raise ValueError("An issue cannot be renamed from a key that is still in use: " + ", ".join(collisions))
        return self


class FactSpecificationWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=4_000)
    issue_key: FactKey | None = None


class FactSpecificationOmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=4_000)
    source_refs: list[FactSourceReference] = Field(default_factory=list, max_length=20)


class FactDevelopmentSpecificationCompilationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_development_specification: FactDevelopmentSpecification
    issue_rationales: dict[str, str] = Field(default_factory=dict)
    omissions: list[FactSpecificationOmission] = Field(default_factory=list)
    warnings: list[FactSpecificationWarning] = Field(default_factory=list)


def fact_source_catalog(task_definition_markdown: str, guidance_markdown: str) -> list[dict[str, Any]]:
    """Return every citable excerpt of the pinned guidance and the task definition, with exact hashes."""

    from app.analysis_task_compilation import build_source_reference_catalog

    catalog: list[dict[str, Any]] = []
    for source, markdown in (("REVIEW_GUIDANCE", guidance_markdown), ("TASK_DEFINITION", task_definition_markdown)):
        for reference in build_source_reference_catalog(uuid.UUID(int=0), markdown):
            catalog.append(
                {
                    "source": source,
                    "heading": reference["heading"],
                    "excerpt": reference["excerpt"],
                    "excerpt_hash": reference["excerpt_hash"],
                }
            )
    return catalog


def task_version_source_catalog(db: Session, task_version: MatterAnalysisTaskVersion) -> list[dict[str, Any]]:
    revision = pinned_guidance_revision(db, task_version)
    return fact_source_catalog(task_version.definition_markdown, revision.content_markdown)


def pinned_guidance_revision(db: Session, task_version: MatterAnalysisTaskVersion) -> MatterDefinitionRevision:
    if task_version.review_guidance_revision_id is None:
        raise FactSpecificationError("A fact-development task version must pin a Review Guidance revision")
    revision = db.get(MatterDefinitionRevision, task_version.review_guidance_revision_id)
    if revision is None or revision.matter_definition_id != task_version.review_guidance_id:
        raise FactSpecificationError("The pinned Review Guidance revision is unavailable")
    return revision


def mappable_metadata_definitions(db: Session, matter_id: uuid.UUID) -> dict[str, MetadataDefinition]:
    """Active, matter-owned coding fields that may hold issue coding."""

    return {
        definition.key: definition
        for definition in db.scalars(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == matter_id,
                MetadataDefinition.status == "ACTIVE",
                MetadataDefinition.value_source == "ASSERTED",
                MetadataDefinition.type.in_(MAPPABLE_METADATA_TYPES),
            )
        )
    }


def metadata_definition_catalog(definitions: Iterable[MetadataDefinition]) -> list[dict[str, Any]]:
    return [
        {
            "key": definition.key,
            "display_name": definition.display_name,
            "description": definition.description,
            "type": definition.type,
            "cardinality": definition.cardinality,
            "match_type": MATCH_TYPE_BY_METADATA_TYPE[definition.type],
            "active_values": [
                {
                    "key": str(option["key"]),
                    "label": option.get("label"),
                    "parent_key": option.get("parent_key"),
                }
                for option in definition.allowed_values or []
                if isinstance(option, dict) and option.get("key") and option.get("active", True) is not False
            ],
        }
        for definition in sorted(definitions, key=lambda item: item.key)
    ]


def validate_fact_development_specification(
    specification: FactDevelopmentSpecification,
    *,
    source_catalog: list[dict[str, Any]],
    definitions: dict[str, MetadataDefinition],
) -> list[dict[str, Any]]:
    """Check every source reference and coding mapping; return non-blocking warnings.

    Raises FactSpecificationError listing every blocking problem.
    """

    valid_references = {(entry["source"], entry["heading"], entry["excerpt_hash"]) for entry in source_catalog}
    errors: list[str] = []
    for issue in specification.issues:
        for reference in issue.source_refs:
            if (reference.source, reference.heading, reference.excerpt_hash) not in valid_references:
                errors.append(f"Issue {issue.key} cites a source excerpt that is not in the pinned guidance or task")
        for mapping in issue.coding_mappings:
            errors.extend(_mapping_errors(issue.key, mapping, definitions))
    if errors:
        raise FactSpecificationError("; ".join(errors))

    warnings: list[dict[str, Any]] = []
    signatures: dict[frozenset[tuple[str, str, Any]], str] = {}
    for issue in specification.issues:
        signature = frozenset(
            (mapping.metadata_definition_key, mapping.match_type, mapping.value) for mapping in issue.coding_mappings
        )
        if signature in signatures:
            warnings.append(
                {
                    "code": "ISSUE_MAPPINGS_IDENTICAL",
                    "message": (
                        f"Issues {signatures[signature]} and {issue.key} select exactly the same coded documents."
                    ),
                    "issue_key": issue.key,
                }
            )
        else:
            signatures[signature] = issue.key
    return warnings


def _mapping_errors(
    issue_key: str,
    mapping: FactIssueCodingMapping,
    definitions: dict[str, MetadataDefinition],
) -> list[str]:
    definition = definitions.get(mapping.metadata_definition_key)
    if definition is None:
        return [
            (
                f"Issue {issue_key} maps to {mapping.metadata_definition_key}, which is not an active, "
                "matter-owned enum, boolean, text, or long-text coding field"
            )
        ]
    expected = MATCH_TYPE_BY_METADATA_TYPE[definition.type]
    if mapping.match_type != expected:
        return [f"Issue {issue_key} must use {expected} to match {definition.type} field {definition.key}"]
    if mapping.match_type == "ENUM_VALUE":
        option = next(
            (
                item
                for item in definition.allowed_values or []
                if isinstance(item, dict) and str(item.get("key")) == mapping.value
            ),
            None,
        )
        if option is None:
            return [f"Issue {issue_key} maps to unknown value {mapping.value} of {definition.key}"]
        if option.get("active", True) is False:
            return [f"Issue {issue_key} maps to deactivated value {mapping.value} of {definition.key}"]
    return []


def parse_fact_development_specification(value: dict[str, Any] | None) -> FactDevelopmentSpecification:
    if value is None:
        raise FactSpecificationError("The task version has no Fact Development Specification")
    try:
        return FactDevelopmentSpecification.model_validate(value)
    except ValueError as exc:
        raise FactSpecificationError(str(exc)) from exc
