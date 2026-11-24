from __future__ import annotations

import hashlib
import io
import json
import uuid
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.artifact_gateway import (
    AnalysisInputItemSnapshot,
    CollectionItemRelationshipSnapshot,
    copy_workflow_artifact_content,
    get_collection_item_analysis_input_snapshots,
    get_collection_item_relationship_snapshots,
    get_preferred_text_source,
    store_workflow_artifact,
)
from app.audit import record_audit
from app.embeddings.configuration import canonical_hash
from app.models import (
    Matter,
    MatterDocument,
    MatterEntity,
    MatterEntityDomain,
    MatterEntityRelationship,
    MatterIdentityAnalysis,
    MetadataDefinition,
    ReviewBatch,
    ReviewBatchDocument,
    ReviewProgram,
)
from artifact_service.schemas import WorkflowArtifactLineageInput
from artifact_service.workflow_artifact_types import ArtifactType


class PrivilegeReviewFoundationError(ValueError):
    pass


RELATIONSHIP_SNAPSHOT_BATCH_SIZE = 5_000
MAX_EVIDENCE_PACKET_MEMBERS = 25


def _validate_evidence_packet_text_sources(
    snapshots: dict[uuid.UUID, AnalysisInputItemSnapshot],
    *,
    actor_user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
) -> dict[uuid.UUID, AnalysisInputItemSnapshot]:
    """Make packet preflight use the same extractable-text rule as execution."""

    validated: dict[uuid.UUID, AnalysisInputItemSnapshot] = {}
    for item_id, snapshot in snapshots.items():
        frozen = snapshot.text_source
        if frozen is None:
            validated[item_id] = snapshot
            continue
        preferred = get_preferred_text_source(
            collection_item_id=item_id,
            actor_user_id=actor_user_id,
            tenant_id=tenant_id,
            client_id=client_id,
        )
        if (
            preferred is None
            or preferred.artifact_id != frozen.artifact_id
            or preferred.content_hash != frozen.content_hash
        ):
            validated[item_id] = replace(snapshot, text_source=None)
            continue
        validated[item_id] = snapshot
    return validated


def _json_bytes(payload: dict[str, Any]) -> tuple[bytes, str]:
    content = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return content, hashlib.sha256(content).hexdigest()


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _store_json_artifact(
    *,
    payload: dict[str, Any],
    artifact_type: ArtifactType,
    matter: Matter,
    tenant_id: uuid.UUID,
    program: ReviewProgram,
    actor_user_id: uuid.UUID,
    filename: str,
    metadata: dict[str, Any],
    lineage: list[WorkflowArtifactLineageInput] | None = None,
) -> dict[str, Any]:
    content, content_hash = _json_bytes(payload)
    derivation_key = canonical_hash(
        {
            "artifact_type": artifact_type.value,
            "program_id": str(program.id),
            "content_hash": content_hash,
            "schema_version": 1,
        }
    )
    reference, _created = store_workflow_artifact(
        content=io.BytesIO(content),
        artifact_type=artifact_type,
        core_scope_kind="MATTER",
        core_scope_id=matter.id,
        schema_version=1,
        derivation_key=derivation_key,
        content_hash=content_hash,
        byte_length=len(content),
        media_type="application/json",
        producer_kind="CORE",
        producer_run_id=program.id,
        artifact_metadata={"program_id": str(program.id), **metadata},
        lineage=lineage or [],
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=matter.client_id,
        filename=filename,
    )
    return {
        "artifact_id": str(reference.artifact_id),
        "content_hash": content_hash,
        "byte_length": len(content),
        "schema_version": 1,
    }


def _identity_context_payload(
    db: Session,
    *,
    matter: Matter,
    program: ReviewProgram,
    frozen_at: datetime,
) -> tuple[dict[str, Any], list[WorkflowArtifactLineageInput]]:
    entities = list(
        db.scalars(
            select(MatterEntity)
            .where(
                MatterEntity.matter_id == matter.id,
                MatterEntity.parent_id.is_(None),
                MatterEntity.status == "CONFIRMED",
            )
            .order_by(MatterEntity.id)
        )
    )
    entity_ids = {entity.id for entity in entities}
    variants_by_entity: dict[uuid.UUID, list[dict[str, Any]]] = defaultdict(list)
    if entity_ids:
        for variant in db.scalars(
            select(MatterEntity)
            .where(
                MatterEntity.matter_id == matter.id,
                MatterEntity.parent_id.in_(entity_ids),
            )
            .order_by(MatterEntity.parent_id, MatterEntity.id)
        ):
            assert variant.parent_id is not None
            variants_by_entity[variant.parent_id].append(
                {
                    "id": str(variant.id),
                    "display_name": variant.display_name,
                    "email": variant.email,
                    "created_at": _iso(variant.created_at),
                }
            )
    domains_by_entity: dict[uuid.UUID, list[dict[str, Any]]] = defaultdict(list)
    if entity_ids:
        for domain in db.scalars(
            select(MatterEntityDomain)
            .where(
                MatterEntityDomain.matter_id == matter.id,
                MatterEntityDomain.entity_id.in_(entity_ids),
            )
            .order_by(MatterEntityDomain.normalized_domain, MatterEntityDomain.id)
        ):
            domains_by_entity[domain.entity_id].append(
                {
                    "id": str(domain.id),
                    "domain": domain.domain,
                    "normalized_domain": domain.normalized_domain,
                    "source_recommendation_run_id": (
                        str(domain.source_recommendation_run_id) if domain.source_recommendation_run_id else None
                    ),
                    "created_by_user_id": str(domain.created_by_user_id),
                    "created_at": _iso(domain.created_at),
                }
            )
    relationships = list(
        db.scalars(
            select(MatterEntityRelationship)
            .where(
                MatterEntityRelationship.matter_id == matter.id,
                MatterEntityRelationship.status == "CONFIRMED",
            )
            .order_by(MatterEntityRelationship.relationship_type, MatterEntityRelationship.id)
        )
    )
    latest_analysis = db.scalar(
        select(MatterIdentityAnalysis)
        .where(
            MatterIdentityAnalysis.matter_id == matter.id,
            MatterIdentityAnalysis.status == "COMPLETED",
            MatterIdentityAnalysis.result_artifact_id.is_not(None),
        )
        .order_by(MatterIdentityAnalysis.completed_at.desc(), MatterIdentityAnalysis.id.desc())
        .limit(1)
    )
    source_analysis = (
        {
            "identity_analysis_id": str(latest_analysis.id),
            "artifact_id": str(latest_analysis.result_artifact_id),
            "content_hash": latest_analysis.result_content_hash,
            "completed_at": _iso(latest_analysis.completed_at),
        }
        if latest_analysis is not None
        else None
    )
    lineage = (
        [
            WorkflowArtifactLineageInput(
                source_kind="WORKFLOW_ARTIFACT",
                source_artifact_id=latest_analysis.result_artifact_id,
            )
        ]
        if latest_analysis is not None and latest_analysis.result_artifact_id is not None
        else []
    )
    payload = {
        "schema_version": 1,
        "kind": "PRIVILEGE_IDENTITY_CONTEXT",
        "matter_id": str(matter.id),
        "program_id": str(program.id),
        "frozen_at": frozen_at.isoformat(),
        "source_identity_analysis": source_analysis,
        "entities": [
            {
                "id": str(entity.id),
                "entity_type": entity.entity_type,
                "organization_category": entity.organization_category,
                "display_name": entity.display_name,
                "email": entity.email,
                "title": entity.title,
                "organization_id": str(entity.organization_id) if entity.organization_id else None,
                "custodian_id": str(entity.custodian_id) if entity.custodian_id else None,
                "description": entity.description,
                "variants": variants_by_entity.get(entity.id, []),
                "domains": domains_by_entity.get(entity.id, []),
                "provenance": {
                    "created_by_user_id": str(entity.created_by_user_id) if entity.created_by_user_id else None,
                    "created_by_workflow_run_id": (
                        str(entity.created_by_workflow_run_id) if entity.created_by_workflow_run_id else None
                    ),
                    "created_at": _iso(entity.created_at),
                    "updated_at": _iso(entity.updated_at),
                },
            }
            for entity in entities
        ],
        "relationships": [
            {
                "id": str(relationship.id),
                "relationship_type": relationship.relationship_type,
                "source_entity_id": str(relationship.source_entity_id),
                "target_entity_id": str(relationship.target_entity_id),
                "confidence": relationship.confidence,
                "rationale": relationship.rationale,
                "effective_from": _iso(relationship.effective_from),
                "effective_to": _iso(relationship.effective_to),
                "decided_by_user_id": str(relationship.decided_by_user_id),
                "decided_at": _iso(relationship.decided_at),
            }
            for relationship in relationships
            if relationship.source_entity_id in entity_ids and relationship.target_entity_id in entity_ids
        ],
    }
    payload["counts"] = {
        "confirmed_entity_count": len(payload["entities"]),
        "confirmed_variant_count": sum(len(entity["variants"]) for entity in payload["entities"]),
        "confirmed_domain_count": sum(len(entity["domains"]) for entity in payload["entities"]),
        "confirmed_relationship_count": len(payload["relationships"]),
    }
    return payload, lineage


def _population_payload(
    db: Session,
    *,
    matter: Matter,
    program: ReviewProgram,
    review_batch_ids: list[uuid.UUID],
    selection_type: str,
    frozen_at: datetime,
) -> dict[str, Any]:
    unique_batch_ids = list(dict.fromkeys(review_batch_ids))
    batches = list(
        db.scalars(
            select(ReviewBatch).where(
                ReviewBatch.matter_id == matter.id,
                ReviewBatch.id.in_(unique_batch_ids),
                ReviewBatch.status != "ARCHIVED",
            )
        )
    )
    by_id = {batch.id: batch for batch in batches}
    missing = [batch_id for batch_id in unique_batch_ids if batch_id not in by_id]
    if missing:
        raise PrivilegeReviewFoundationError("One or more selected review batches are unavailable")
    rows = list(
        db.execute(
            select(
                ReviewBatchDocument.review_batch_id,
                MatterDocument.id,
                MatterDocument.source_collection_id,
                MatterDocument.collection_item_id,
            )
            .join(MatterDocument, MatterDocument.id == ReviewBatchDocument.matter_document_id)
            .where(
                ReviewBatchDocument.review_batch_id.in_(unique_batch_ids),
                MatterDocument.matter_id == matter.id,
            )
            .order_by(MatterDocument.id, ReviewBatchDocument.review_batch_id)
        )
    )
    documents: dict[uuid.UUID, dict[str, Any]] = {}
    for batch_id, document_id, source_collection_id, collection_item_id in rows:
        item = documents.setdefault(
            document_id,
            {
                "matter_document_id": str(document_id),
                "source_collection_id": str(source_collection_id),
                "collection_item_id": str(collection_item_id),
                "source_review_batch_ids": [],
            },
        )
        item["source_review_batch_ids"].append(str(batch_id))
    if not documents:
        raise PrivilegeReviewFoundationError("The selected review population contains no documents")
    return {
        "schema_version": 1,
        "kind": "PRIVILEGE_REVIEW_POPULATION",
        "matter_id": str(matter.id),
        "program_id": str(program.id),
        "frozen_at": frozen_at.isoformat(),
        "selection": {
            "type": selection_type,
            "review_batches": [
                {"id": str(batch_id), "name": by_id[batch_id].name, "document_count": by_id[batch_id].document_count}
                for batch_id in unique_batch_ids
            ],
        },
        "document_count": len(documents),
        "documents": list(documents.values()),
    }


def _create_all_matter_review_batch(
    db: Session,
    *,
    matter: Matter,
    program: ReviewProgram,
    actor_user_id: uuid.UUID,
) -> ReviewBatch:
    document_ids = list(
        db.scalars(
            select(MatterDocument.id)
            .where(MatterDocument.matter_id == matter.id)
            .order_by(MatterDocument.id)
        )
    )
    if not document_ids:
        raise PrivilegeReviewFoundationError("The matter contains no documents")
    batch = ReviewBatch(
        matter_id=matter.id,
        name=program.name,
        description=f"All-matter population for Privilege Review Program {program.name}.",
        selection_type="ALL_MATTER",
        selection_definition={
            "type": "ALL_MATTER",
            "created_by": "PRIVILEGE_REVIEW_PROGRAM",
            "review_program_id": str(program.id),
        },
        reviewer_value_visibility="OWN_VALUES",
        status="READY",
        search_status="NOT_CONFIGURED",
        workflow_id=f"privilege-review-program:{program.id}:all-matter",
        document_count=len(document_ids),
        created_by_user_id=actor_user_id,
    )
    db.add(batch)
    db.flush()
    for offset in range(0, len(document_ids), 1_000):
        page = document_ids[offset : offset + 1_000]
        db.execute(
            insert(ReviewBatchDocument),
            [
                {
                    "review_batch_id": batch.id,
                    "matter_document_id": document_id,
                    "sequence_number": offset + index,
                }
                for index, document_id in enumerate(page, start=1)
            ],
        )
    return batch


def freeze_privilege_review_foundation(
    db: Session,
    *,
    matter: Matter,
    tenant_id: uuid.UUID,
    program: ReviewProgram,
    actor_user_id: uuid.UUID,
    review_batch_ids: list[uuid.UUID],
    population_type: str,
    target_metadata_definition_id: uuid.UUID,
    review_policy: str,
    keep_families_together: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    target = db.get(MetadataDefinition, target_metadata_definition_id)
    if (
        target is None
        or target.matter_id != matter.id
        or target.status != "ACTIVE"
        or not target.reviewable
        or target.cardinality != "SINGLE"
        or target.type != "ENUM"
    ):
        raise PrivilegeReviewFoundationError("Select an active, single-value privilege review field from this matter")
    active_value_keys = {
        str(item.get("key"))
        for item in target.allowed_values or []
        if item.get("active", True) and item.get("key") is not None
    }
    required_value_keys = {"privileged", "not_privileged", "needs_review"}
    if not required_value_keys.issubset(active_value_keys):
        raise PrivilegeReviewFoundationError(
            "The operational privilege field must provide privileged, not_privileged, and needs_review values"
        )
    resolved_batch_ids = list(review_batch_ids)
    created_all_matter_batch_id: uuid.UUID | None = None
    if population_type == "ALL_MATTER":
        all_matter_batch = _create_all_matter_review_batch(
            db,
            matter=matter,
            program=program,
            actor_user_id=actor_user_id,
        )
        resolved_batch_ids = [all_matter_batch.id]
        created_all_matter_batch_id = all_matter_batch.id
        record_audit(
            db,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="review_batch.created",
            target_type="review_batch",
            target_id=all_matter_batch.id,
            details={
                "matter_id": str(matter.id),
                "selection_type": "ALL_MATTER",
                "created_by": "PRIVILEGE_REVIEW_PROGRAM",
                "review_program_id": str(program.id),
            },
        )
    elif population_type != "REVIEW_BATCHES":
        raise PrivilegeReviewFoundationError("Unsupported Privilege Review population type")
    frozen_at = datetime.now(timezone.utc)
    population_payload = _population_payload(
        db,
        matter=matter,
        program=program,
        review_batch_ids=resolved_batch_ids,
        selection_type=population_type,
        frozen_at=frozen_at,
    )
    identity_payload, identity_lineage = _identity_context_payload(
        db,
        matter=matter,
        program=program,
        frozen_at=frozen_at,
    )
    population_reference = _store_json_artifact(
        payload=population_payload,
        artifact_type=ArtifactType.PRIVILEGE_REVIEW_POPULATION,
        matter=matter,
        tenant_id=tenant_id,
        program=program,
        actor_user_id=actor_user_id,
        filename=f"privilege-review-population-{program.id}.json",
        metadata={
            "document_count": population_payload["document_count"],
            "source_review_batch_count": len(resolved_batch_ids),
        },
    )
    identity_reference = _store_json_artifact(
        payload=identity_payload,
        artifact_type=ArtifactType.PRIVILEGE_IDENTITY_CONTEXT,
        matter=matter,
        tenant_id=tenant_id,
        program=program,
        actor_user_id=actor_user_id,
        filename=f"privilege-identity-context-{program.id}.json",
        metadata=identity_payload["counts"],
        lineage=identity_lineage,
    )
    configuration = {
        "schema_version": 1,
        "review_population": {
            **population_reference,
            "selection_type": population_type,
            "review_batch_ids": [str(value) for value in dict.fromkeys(resolved_batch_ids)],
            "created_all_matter_batch_id": (
                str(created_all_matter_batch_id) if created_all_matter_batch_id is not None else None
            ),
            "document_count": population_payload["document_count"],
        },
        "identity_context": {
            **identity_reference,
            **identity_payload["counts"],
            "source_identity_analysis": identity_payload["source_identity_analysis"],
            "required": False,
        },
        "target_metadata_definition": {
            "id": str(target.id),
            "key": target.key,
            "display_name": target.display_name,
            "type": target.type,
            "allowed_values": target.allowed_values,
        },
        "review_policy": review_policy,
        "family_policy": {"keep_families_together": keep_families_together},
        "execution_status": "FOUNDATION_READY",
    }
    source_hashes = {
        "review_population_content_hash": population_reference["content_hash"],
        "identity_context_content_hash": identity_reference["content_hash"],
    }
    program.target_metadata_definition_id = target.id
    return configuration, source_hashes


def _load_review_population(
    *,
    population_reference: dict[str, Any],
    matter: Matter,
    tenant_id: uuid.UUID,
    program: ReviewProgram,
    actor_user_id: uuid.UUID,
) -> dict[str, Any]:
    try:
        artifact_id = uuid.UUID(str(population_reference["artifact_id"]))
        expected_hash = str(population_reference["content_hash"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PrivilegeReviewFoundationError("The frozen review population reference is invalid") from exc
    destination = io.BytesIO()
    copy_workflow_artifact_content(
        artifact_id=artifact_id,
        destination=destination,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=matter.client_id,
    )
    content = destination.getvalue()
    if hashlib.sha256(content).hexdigest() != expected_hash:
        raise PrivilegeReviewFoundationError("The frozen review population content hash does not match")
    try:
        payload = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise PrivilegeReviewFoundationError("The frozen review population is not valid JSON") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("kind") != "PRIVILEGE_REVIEW_POPULATION"
        or payload.get("matter_id") != str(matter.id)
        or payload.get("program_id") != str(program.id)
        or not isinstance(payload.get("documents"), list)
    ):
        raise PrivilegeReviewFoundationError("The frozen review population does not belong to this program")
    return payload


def _relationship_snapshots(
    *,
    item_ids: list[uuid.UUID],
    actor_user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
) -> dict[uuid.UUID, CollectionItemRelationshipSnapshot]:
    snapshots: dict[uuid.UUID, CollectionItemRelationshipSnapshot] = {}
    unique_ids = list(dict.fromkeys(item_ids))
    for offset in range(0, len(unique_ids), RELATIONSHIP_SNAPSHOT_BATCH_SIZE):
        snapshots.update(
            get_collection_item_relationship_snapshots(
                item_ids=unique_ids[offset : offset + RELATIONSHIP_SNAPSHOT_BATCH_SIZE],
                actor_user_id=actor_user_id,
                tenant_id=tenant_id,
                client_id=client_id,
            )
        )
    return snapshots


def _member(
    *,
    collection_item_id: uuid.UUID,
    population_by_item: dict[uuid.UUID, dict[str, Any]],
    role: str,
    source_item_id: str | None = None,
    identifier: str | None = None,
) -> dict[str, Any]:
    population_document = population_by_item.get(collection_item_id)
    return {
        "collection_item_id": str(collection_item_id),
        "matter_document_id": (
            str(population_document["matter_document_id"]) if population_document is not None else None
        ),
        "source_collection_id": (
            str(population_document["source_collection_id"]) if population_document is not None else None
        ),
        "relationship_role": role,
        "in_review_population": population_document is not None,
        "source_item_id": source_item_id,
        "identifier": identifier,
    }


def _family_context_payload(
    *,
    population_payload: dict[str, Any],
    snapshots: dict[uuid.UUID, CollectionItemRelationshipSnapshot],
    matter: Matter,
    program: ReviewProgram,
    frozen_at: datetime,
    population_reference: dict[str, Any],
) -> dict[str, Any]:
    population_by_item: dict[uuid.UUID, dict[str, Any]] = {}
    for raw_document in population_payload["documents"]:
        try:
            item_id = uuid.UUID(str(raw_document["collection_item_id"]))
            document_id = uuid.UUID(str(raw_document["matter_document_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise PrivilegeReviewFoundationError("The frozen review population contains an invalid document") from exc
        if item_id in population_by_item:
            raise PrivilegeReviewFoundationError("The frozen review population contains a duplicate collection item")
        population_by_item[item_id] = {**raw_document, "matter_document_id": document_id}

    families: dict[str, dict[str, Any]] = {}
    document_index: list[dict[str, Any]] = []
    exceptions: list[dict[str, Any]] = []

    def add_exception(
        family: dict[str, Any],
        *,
        code: str,
        item_id: uuid.UUID,
        related_item_id: uuid.UUID | None = None,
    ) -> None:
        entry = {
            "family_key": family["family_key"],
            "code": code,
            "collection_item_id": str(item_id),
            "related_collection_item_id": str(related_item_id) if related_item_id else None,
        }
        if entry not in exceptions:
            exceptions.append(entry)
        if code not in family["exception_codes"]:
            family["exception_codes"].append(code)

    for item_id, population_document in sorted(population_by_item.items(), key=lambda item: str(item[0])):
        snapshot = snapshots.get(item_id)
        if snapshot is None:
            family_key = f"unresolved:{item_id}"
            family = families.setdefault(
                family_key,
                {
                    "family_key": family_key,
                    "family_id": None,
                    "root_collection_item_id": str(item_id),
                    "members": {},
                    "exception_codes": [],
                },
            )
            family["members"][item_id] = _member(
                collection_item_id=item_id,
                population_by_item=population_by_item,
                role="UNKNOWN",
            )
            add_exception(family, code="RELATIONSHIP_SNAPSHOT_MISSING", item_id=item_id)
            role = "UNKNOWN"
        else:
            root_id = snapshot.parent_item_id or item_id
            root_snapshot = snapshots.get(root_id)
            family_id = snapshot.family_id or (root_snapshot.family_id if root_snapshot is not None else None)
            family_key = str(family_id) if family_id is not None else f"root:{root_id}"
            family = families.setdefault(
                family_key,
                {
                    "family_key": family_key,
                    "family_id": str(family_id) if family_id is not None else None,
                    "root_collection_item_id": str(root_id),
                    "members": {},
                    "exception_codes": [],
                },
            )
            family["members"].setdefault(
                root_id,
                _member(
                    collection_item_id=root_id,
                    population_by_item=population_by_item,
                    role=(
                        "PARENT"
                        if snapshot.parent_item_id is not None
                        or (root_snapshot is not None and root_snapshot.child_item_ids)
                        else "STANDALONE"
                    ),
                    source_item_id=snapshot.parent_source_item_id if snapshot.parent_item_id else None,
                    identifier=snapshot.parent_identifier if snapshot.parent_item_id else None,
                ),
            )
            if root_snapshot is None:
                add_exception(
                    family,
                    code="PARENT_SNAPSHOT_MISSING",
                    item_id=item_id,
                    related_item_id=root_id,
                )
            else:
                if len(root_snapshot.child_source_item_ids) != len(root_snapshot.child_item_ids) or len(
                    root_snapshot.child_identifiers
                ) != len(root_snapshot.child_item_ids):
                    add_exception(
                        family,
                        code="CHILD_METADATA_MISMATCH",
                        item_id=root_id,
                    )
                if snapshot.parent_item_id is not None and item_id not in root_snapshot.child_item_ids:
                    add_exception(
                        family,
                        code="PARENT_CHILD_MISMATCH",
                        item_id=item_id,
                        related_item_id=root_id,
                    )
                for index, child_id in enumerate(root_snapshot.child_item_ids):
                    source_item_id = (
                        root_snapshot.child_source_item_ids[index]
                        if index < len(root_snapshot.child_source_item_ids)
                        else None
                    )
                    identifier = (
                        root_snapshot.child_identifiers[index] if index < len(root_snapshot.child_identifiers) else None
                    )
                    family["members"].setdefault(
                        child_id,
                        _member(
                            collection_item_id=child_id,
                            population_by_item=population_by_item,
                            role="ATTACHMENT",
                            source_item_id=source_item_id,
                            identifier=identifier,
                        ),
                    )
            if snapshot.parent_item_id is not None:
                family["members"].setdefault(
                    item_id,
                    _member(
                        collection_item_id=item_id,
                        population_by_item=population_by_item,
                        role="ATTACHMENT",
                    ),
                )
                role = "ATTACHMENT"
            elif snapshot.child_item_ids:
                role = "PARENT"
            else:
                role = "STANDALONE"

        document_index.append(
            {
                "matter_document_id": str(population_document["matter_document_id"]),
                "collection_item_id": str(item_id),
                "family_key": family_key,
                "relationship_role": role,
                "exception_codes": list(family["exception_codes"]),
            }
        )

    family_rows: list[dict[str, Any]] = []
    for family in sorted(families.values(), key=lambda item: item["family_key"]):
        members = sorted(family.pop("members").values(), key=lambda item: item["collection_item_id"])
        in_scope_count = sum(1 for member in members if member["in_review_population"])
        family_rows.append(
            {
                **family,
                "member_count": len(members),
                "in_scope_document_count": in_scope_count,
                "context_only_member_count": len(members) - in_scope_count,
                "members": members,
            }
        )
    family_summaries = {family["family_key"]: family for family in family_rows}
    for document in document_index:
        family_summary = family_summaries[document["family_key"]]
        document["family_member_count"] = family_summary["member_count"]
        document["context_only_member_count"] = family_summary["context_only_member_count"]
        document["exception_codes"] = list(family_summary["exception_codes"])

    counts = {
        "review_document_count": len(document_index),
        "review_unit_count": len(family_rows),
        "multi_document_family_count": sum(1 for family in family_rows if family["member_count"] > 1),
        "documents_with_family_context_count": sum(
            1 for document in document_index if document["family_member_count"] > 1
        ),
        "parent_document_count": sum(1 for document in document_index if document["relationship_role"] == "PARENT"),
        "attachment_document_count": sum(
            1 for document in document_index if document["relationship_role"] == "ATTACHMENT"
        ),
        "standalone_document_count": sum(
            1 for document in document_index if document["relationship_role"] == "STANDALONE"
        ),
        "context_only_member_count": sum(family["context_only_member_count"] for family in family_rows),
        "relationship_exception_count": len(exceptions),
        "families_with_exceptions_count": sum(1 for family in family_rows if family["exception_codes"]),
    }
    return {
        "schema_version": 1,
        "kind": "PRIVILEGE_FAMILY_CONTEXT",
        "matter_id": str(matter.id),
        "program_id": str(program.id),
        "frozen_at": frozen_at.isoformat(),
        "source_review_population": {
            "artifact_id": str(population_reference["artifact_id"]),
            "content_hash": str(population_reference["content_hash"]),
        },
        "context_policy": {
            "context_only_members_are_in_review_denominator": False,
            "missing_relationships_remove_documents_from_review": False,
        },
        "coverage_status": "COMPLETE_WITH_EXCEPTIONS" if exceptions else "COMPLETE",
        "counts": counts,
        "families": family_rows,
        "documents": sorted(document_index, key=lambda item: item["matter_document_id"]),
        "exceptions": sorted(
            exceptions,
            key=lambda item: (item["family_key"], item["code"], item["collection_item_id"]),
        ),
    }


def prepare_privilege_family_context(
    *,
    matter: Matter,
    tenant_id: uuid.UUID,
    program: ReviewProgram,
    actor_user_id: uuid.UUID,
    population_reference: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    population_payload = _load_review_population(
        population_reference=population_reference,
        matter=matter,
        tenant_id=tenant_id,
        program=program,
        actor_user_id=actor_user_id,
    )
    item_ids = [uuid.UUID(str(document["collection_item_id"])) for document in population_payload["documents"]]
    snapshots = _relationship_snapshots(
        item_ids=item_ids,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=matter.client_id,
    )
    missing_parent_ids = sorted(
        {
            snapshot.parent_item_id
            for snapshot in snapshots.values()
            if snapshot.parent_item_id is not None and snapshot.parent_item_id not in snapshots
        },
        key=str,
    )
    if missing_parent_ids:
        snapshots.update(
            _relationship_snapshots(
                item_ids=missing_parent_ids,
                actor_user_id=actor_user_id,
                tenant_id=tenant_id,
                client_id=matter.client_id,
            )
        )
    frozen_at = datetime.now(timezone.utc)
    payload = _family_context_payload(
        population_payload=population_payload,
        snapshots=snapshots,
        matter=matter,
        program=program,
        frozen_at=frozen_at,
        population_reference=population_reference,
    )
    reference = _store_json_artifact(
        payload=payload,
        artifact_type=ArtifactType.PRIVILEGE_FAMILY_CONTEXT,
        matter=matter,
        tenant_id=tenant_id,
        program=program,
        actor_user_id=actor_user_id,
        filename=f"privilege-family-context-{program.id}.json",
        metadata={"coverage_status": payload["coverage_status"], **payload["counts"]},
        lineage=[
            WorkflowArtifactLineageInput(
                source_kind="WORKFLOW_ARTIFACT",
                source_artifact_id=uuid.UUID(str(population_reference["artifact_id"])),
            )
        ],
    )
    configuration = {
        **reference,
        "coverage_status": payload["coverage_status"],
        "prepared_at": frozen_at.isoformat(),
        "source_review_population_content_hash": str(population_reference["content_hash"]),
        **payload["counts"],
    }
    return configuration, reference["content_hash"]


def _load_program_json_artifact(
    *,
    reference: dict[str, Any],
    expected_kind: str,
    matter: Matter,
    tenant_id: uuid.UUID,
    program: ReviewProgram,
    actor_user_id: uuid.UUID,
) -> dict[str, Any]:
    try:
        artifact_id = uuid.UUID(str(reference["artifact_id"]))
        expected_hash = str(reference["content_hash"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PrivilegeReviewFoundationError(f"The frozen {expected_kind.lower()} reference is invalid") from exc
    destination = io.BytesIO()
    copy_workflow_artifact_content(
        artifact_id=artifact_id,
        destination=destination,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=matter.client_id,
    )
    content = destination.getvalue()
    if hashlib.sha256(content).hexdigest() != expected_hash:
        raise PrivilegeReviewFoundationError(f"The frozen {expected_kind.lower()} content hash does not match")
    try:
        payload = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise PrivilegeReviewFoundationError(f"The frozen {expected_kind.lower()} is not valid JSON") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("kind") != expected_kind
        or payload.get("matter_id") != str(matter.id)
        or payload.get("program_id") != str(program.id)
    ):
        raise PrivilegeReviewFoundationError(f"The frozen {expected_kind.lower()} does not belong to this program")
    return payload


def _packet_member(
    member: dict[str, Any],
    snapshot: AnalysisInputItemSnapshot | None,
) -> tuple[dict[str, Any], list[str]]:
    exceptions: list[str] = []
    if snapshot is None:
        exceptions.append("ANALYSIS_INPUT_MISSING")
    elif snapshot.text_source is None:
        exceptions.append("TEXT_SOURCE_MISSING")
    text_source = snapshot.text_source if snapshot is not None else None
    return (
        {
            **member,
            "record_type": snapshot.record_type if snapshot is not None else None,
            "original_filename": snapshot.original_filename if snapshot is not None else None,
            "file_date": _iso(snapshot.file_date) if snapshot is not None else None,
            "email": (
                {
                    "sender": snapshot.email_sender,
                    "subject": snapshot.email_subject,
                    "sent_at": _iso(snapshot.email_sent_at),
                    "recipients": snapshot.email_recipients,
                }
                if snapshot is not None
                else None
            ),
            "text_source": (
                {
                    "artifact_id": str(text_source.artifact_id),
                    "role": text_source.role,
                    "media_type": text_source.media_type,
                    "original_filename": text_source.original_filename,
                    "content_hash": text_source.content_hash,
                    "byte_length": text_source.byte_length,
                }
                if text_source is not None
                else None
            ),
            "exception_codes": exceptions,
        },
        exceptions,
    )


def _evidence_packets_payload(
    *,
    family_context: dict[str, Any],
    input_snapshots: dict[uuid.UUID, AnalysisInputItemSnapshot],
    matter: Matter,
    program: ReviewProgram,
    frozen_at: datetime,
    family_context_reference: dict[str, Any],
    identity_context_reference: dict[str, Any],
    accepted_matter_definition: dict[str, Any],
) -> dict[str, Any]:
    packets: list[dict[str, Any]] = []
    packet_exceptions: list[dict[str, Any]] = []
    targeted_document_ids: set[str] = set()
    context_only_item_ids: set[str] = set()

    for family in family_context.get("families", []):
        members = list(family.get("members") or [])
        root_id = str(family.get("root_collection_item_id") or "")
        members.sort(
            key=lambda member: (
                str(member.get("collection_item_id")) != root_id,
                not bool(member.get("in_review_population")),
                str(member.get("collection_item_id")),
            )
        )
        root_member = next(
            (member for member in members if str(member.get("collection_item_id")) == root_id),
            None,
        )
        non_root_members = [
            member for member in members if str(member.get("collection_item_id")) != root_id
        ]
        if len(members) <= MAX_EVIDENCE_PACKET_MEMBERS or root_member is None:
            packet_members = [members]
        else:
            packet_members = [
                [root_member, *non_root_members[offset : offset + MAX_EVIDENCE_PACKET_MEMBERS - 1]]
                for offset in range(0, len(non_root_members), MAX_EVIDENCE_PACKET_MEMBERS - 1)
            ]
        family_targeted_document_ids: set[str] = set()
        for packet_index, raw_members in enumerate(packet_members, start=1):
            rendered_members: list[dict[str, Any]] = []
            exception_codes = set(family.get("exception_codes") or [])
            target_ids: list[str] = []
            for member in raw_members:
                try:
                    item_id = uuid.UUID(str(member["collection_item_id"]))
                except (KeyError, TypeError, ValueError) as exc:
                    raise PrivilegeReviewFoundationError("Family context contains an invalid member") from exc
                rendered, member_exceptions = _packet_member(member, input_snapshots.get(item_id))
                rendered_members.append(rendered)
                exception_codes.update(member_exceptions)
                matter_document_id = member.get("matter_document_id")
                if (
                    member.get("in_review_population")
                    and matter_document_id
                    and str(matter_document_id) not in family_targeted_document_ids
                ):
                    target_ids.append(str(matter_document_id))
                    family_targeted_document_ids.add(str(matter_document_id))
                    targeted_document_ids.add(str(matter_document_id))
                elif not member.get("in_review_population"):
                    context_only_item_ids.add(str(item_id))
                for code in member_exceptions:
                    packet_exceptions.append(
                        {
                            "family_key": family["family_key"],
                            "collection_item_id": str(item_id),
                            "code": code,
                        }
                    )
            packets.append(
                {
                    "packet_key": f"{family['family_key']}:{packet_index}",
                    "family_key": family["family_key"],
                    "family_id": family.get("family_id"),
                    "packet_ordinal": packet_index,
                    "packet_count_for_family": len(packet_members),
                    "review_target_document_ids": target_ids,
                    "member_count": len(rendered_members),
                    "members": rendered_members,
                    "exception_codes": sorted(exception_codes),
                }
            )

    expected_targets = {
        str(document["matter_document_id"])
        for document in family_context.get("documents", [])
        if document.get("matter_document_id")
    }
    missing_targets = sorted(expected_targets - targeted_document_ids)
    if missing_targets:
        raise PrivilegeReviewFoundationError(
            "Family review packets do not cover every document in the frozen review population"
        )
    packet_exceptions = list(
        {
            (item["family_key"], item["collection_item_id"], item["code"]): item
            for item in packet_exceptions
        }.values()
    )
    all_exceptions = [*(family_context.get("exceptions") or []), *packet_exceptions]
    counts = {
        "review_document_count": len(expected_targets),
        "family_count": len(family_context.get("families", [])),
        "packet_count": len(packets),
        "split_family_count": sum(1 for family in family_context.get("families", []) if len(family.get("members") or []) > MAX_EVIDENCE_PACKET_MEMBERS),
        "context_only_member_count": len(context_only_item_ids),
        "missing_text_document_count": len(
            {
                item["collection_item_id"]
                for item in packet_exceptions
                if item["code"] == "TEXT_SOURCE_MISSING"
            }
        ),
        "missing_input_document_count": len(
            {
                item["collection_item_id"]
                for item in packet_exceptions
                if item["code"] == "ANALYSIS_INPUT_MISSING"
            }
        ),
        "exception_count": len(all_exceptions),
    }
    return {
        "schema_version": 1,
        "kind": "PRIVILEGE_EVIDENCE_PACKETS",
        "matter_id": str(matter.id),
        "program_id": str(program.id),
        "frozen_at": frozen_at.isoformat(),
        "source_family_context": {
            "artifact_id": str(family_context_reference["artifact_id"]),
            "content_hash": str(family_context_reference["content_hash"]),
        },
        "source_identity_context": {
            "artifact_id": str(identity_context_reference["artifact_id"]),
            "content_hash": str(identity_context_reference["content_hash"]),
        },
        "accepted_matter_definition": accepted_matter_definition,
        "packet_policy": {
            "maximum_members_per_packet": MAX_EVIDENCE_PACKET_MEMBERS,
            "context_only_members_are_review_targets": False,
            "missing_text_is_blocking_for_that_member": True,
            "identity_context_is_a_signal_not_a_determination": True,
        },
        "coverage_status": "COMPLETE_WITH_EXCEPTIONS" if counts["exception_count"] else "COMPLETE",
        "review_document_ids_hash": canonical_hash(sorted(expected_targets)),
        "counts": counts,
        "packets": packets,
        "exceptions": all_exceptions,
    }


def prepare_privilege_evidence_packets(
    *,
    matter: Matter,
    tenant_id: uuid.UUID,
    program: ReviewProgram,
    actor_user_id: uuid.UUID,
    family_context_reference: dict[str, Any],
    identity_context_reference: dict[str, Any],
    accepted_matter_definition: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    family_context = _load_program_json_artifact(
        reference=family_context_reference,
        expected_kind="PRIVILEGE_FAMILY_CONTEXT",
        matter=matter,
        tenant_id=tenant_id,
        program=program,
        actor_user_id=actor_user_id,
    )
    item_ids = [
        uuid.UUID(str(member["collection_item_id"]))
        for family in family_context.get("families", [])
        for member in family.get("members", [])
    ]
    snapshots = get_collection_item_analysis_input_snapshots(
        item_ids=item_ids,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=matter.client_id,
    )
    snapshots = _validate_evidence_packet_text_sources(
        snapshots,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=matter.client_id,
    )
    frozen_at = datetime.now(timezone.utc)
    payload = _evidence_packets_payload(
        family_context=family_context,
        input_snapshots=snapshots,
        matter=matter,
        program=program,
        frozen_at=frozen_at,
        family_context_reference=family_context_reference,
        identity_context_reference=identity_context_reference,
        accepted_matter_definition=accepted_matter_definition,
    )
    reference = _store_json_artifact(
        payload=payload,
        artifact_type=ArtifactType.PRIVILEGE_EVIDENCE_PACKETS,
        matter=matter,
        tenant_id=tenant_id,
        program=program,
        actor_user_id=actor_user_id,
        filename=f"privilege-evidence-packets-{program.id}.json",
        metadata={"coverage_status": payload["coverage_status"], **payload["counts"]},
        lineage=[
            WorkflowArtifactLineageInput(
                source_kind="WORKFLOW_ARTIFACT",
                source_artifact_id=uuid.UUID(str(source_id)),
            )
            for source_id in (
                family_context_reference["artifact_id"],
                identity_context_reference["artifact_id"],
            )
        ],
    )
    configuration = {
        **reference,
        "state": "PACKETS_READY",
        "coverage_status": payload["coverage_status"],
        "prepared_at": frozen_at.isoformat(),
        "review_document_ids_hash": payload["review_document_ids_hash"],
        **payload["counts"],
    }
    return configuration, reference["content_hash"]
