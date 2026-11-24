"""Frozen family and identity evidence supplied to Privilege Review extraction."""

from __future__ import annotations

import hashlib
import io
import json
import uuid
from email.utils import getaddresses
from functools import lru_cache
from typing import Any

from app.artifact_gateway import (
    AnalysisInputItemSnapshot,
    copy_workflow_artifact_content,
    get_preferred_text_source,
)
from app.evidence_context import (
    DocumentEvidenceContext,
    DocumentEvidenceContextItem,
    build_document_evidence_context,
)
from app.model_execution import content_hash

PRIVILEGE_FAMILY_EVIDENCE_MODE = "PRIVILEGE_FAMILY_EVIDENCE_V1"
FAMILY_CONTEXT_TOTAL_TEXT_CHARACTERS = 40_000
FAMILY_CONTEXT_MEMBER_TEXT_CHARACTERS = 9_000
FAMILY_CONTEXT_SOURCE_MAX_BYTES = 16_000


def _bounded_text(value: str, maximum: int) -> str:
    rendered = " ".join(value.split())
    if len(rendered) <= maximum:
        return rendered
    head = maximum * 2 // 3
    tail = maximum - head - len(" … [truncated] … ")
    return f"{rendered[:head]} … [truncated] … {rendered[-tail:]}"


def _email_addresses(values: list[str]) -> set[str]:
    return {
        address.casefold()
        for _name, address in getaddresses(values)
        if address and "@" in address
    }


def _member_addresses(member: dict[str, Any]) -> set[str]:
    email = member.get("email")
    if not isinstance(email, dict):
        return set()
    values: list[str] = []
    sender = email.get("sender")
    if isinstance(sender, str):
        values.append(sender)
    recipients = email.get("recipients")
    if isinstance(recipients, dict):
        for raw_values in recipients.values():
            if isinstance(raw_values, list):
                values.extend(str(value) for value in raw_values)
    return _email_addresses(values)


def _identity_items(
    *,
    members: list[dict[str, Any]],
    identity_context: dict[str, Any],
) -> list[DocumentEvidenceContextItem]:
    participant_addresses = set().union(*(_member_addresses(member) for member in members))
    participant_domains = {address.rsplit("@", 1)[1] for address in participant_addresses}
    entities = [value for value in identity_context.get("entities", []) if isinstance(value, dict)]
    selected: dict[str, dict[str, Any]] = {}
    for entity in entities:
        entity_addresses = _email_addresses(
            [
                str(value)
                for value in [
                    entity.get("email"),
                    *[variant.get("email") for variant in entity.get("variants", []) if isinstance(variant, dict)],
                ]
                if value
            ]
        )
        entity_domains = {
            str(domain.get("normalized_domain") or domain.get("domain") or "").casefold()
            for domain in entity.get("domains", [])
            if isinstance(domain, dict)
        }
        if participant_addresses.intersection(entity_addresses) or participant_domains.intersection(entity_domains):
            selected[str(entity["id"])] = entity

    relationships_by_entity: dict[str, list[dict[str, Any]]] = {key: [] for key in selected}
    for relationship in identity_context.get("relationships", []):
        if not isinstance(relationship, dict):
            continue
        source_id = str(relationship.get("source_entity_id") or "")
        target_id = str(relationship.get("target_entity_id") or "")
        if source_id in relationships_by_entity:
            relationships_by_entity[source_id].append(relationship)
        if target_id in relationships_by_entity:
            relationships_by_entity[target_id].append(relationship)

    items: list[DocumentEvidenceContextItem] = []
    entity_names = {str(entity.get("id")): str(entity.get("display_name") or entity.get("id")) for entity in entities}
    for entity_id, entity in sorted(selected.items()):
        parts = [
            f"Confirmed matter entity: {entity.get('display_name') or entity_id}",
            f"type={entity.get('entity_type') or 'unknown'}",
        ]
        if entity.get("organization_category"):
            parts.append(f"organization_category={entity['organization_category']}")
        if entity.get("title"):
            parts.append(f"title={entity['title']}")
        for relationship in relationships_by_entity[entity_id]:
            source_id = str(relationship.get("source_entity_id") or "")
            target_id = str(relationship.get("target_entity_id") or "")
            counterpart_id = target_id if source_id == entity_id else source_id
            parts.append(
                "relationship="
                f"{relationship.get('relationship_type') or 'RELATED'} with "
                f"{entity_names.get(counterpart_id, counterpart_id)}"
            )
        items.append(
            DocumentEvidenceContextItem(
                citation_id=f"identity:{entity_id}:signal",
                label="Confirmed matter identity signal (context only, not proof of privilege)",
                value=_bounded_text("; ".join(parts), FAMILY_CONTEXT_MEMBER_TEXT_CHARACTERS),
            )
        )
    return items


def build_privilege_family_evidence_context(
    *,
    target_snapshot: AnalysisInputItemSnapshot,
    target_collection_item_id: uuid.UUID,
    packet: dict[str, Any],
    identity_context: dict[str, Any],
    family_member_text: dict[uuid.UUID, str],
) -> DocumentEvidenceContext:
    """Combine target metadata with bounded, citable family and identity evidence."""

    base = build_document_evidence_context(target_snapshot)
    items = list(base.items)
    members = [value for value in packet.get("members", []) if isinstance(value, dict)]
    remaining_text = FAMILY_CONTEXT_TOTAL_TEXT_CHARACTERS

    for member in members:
        member_id = uuid.UUID(str(member["collection_item_id"]))
        prefix = f"family:{member_id}"

        def add(suffix: str, label: str, raw_value: object, *, item_prefix: str = prefix) -> None:
            if raw_value is None:
                return
            value = str(raw_value).strip()
            if value:
                items.append(
                    DocumentEvidenceContextItem(
                        f"{item_prefix}:{suffix}",
                        label,
                        _bounded_text(value, FAMILY_CONTEXT_MEMBER_TEXT_CHARACTERS),
                    )
                )

        role = str(member.get("relationship_role") or "UNKNOWN")
        add("role", "Family member relationship to the review family", role)
        add("original_filename", f"{role} document title / filename", member.get("original_filename"))
        email = member.get("email")
        if isinstance(email, dict):
            add("email.sender", f"{role} email sender", email.get("sender"))
            add("email.subject", f"{role} email subject", email.get("subject"))
            recipients = email.get("recipients")
            if isinstance(recipients, dict):
                for recipient_type in ("TO", "CC", "BCC"):
                    raw_values = recipients.get(recipient_type)
                    if not isinstance(raw_values, list):
                        continue
                    for ordinal, value in enumerate(raw_values, start=1):
                        add(
                            f"email.{recipient_type.lower()}:{ordinal}",
                            f"{role} email {recipient_type} recipient {ordinal}",
                            value,
                        )
        if member_id == target_collection_item_id or remaining_text <= 0:
            continue
        source_text = family_member_text.get(member_id)
        if not source_text:
            continue
        excerpt = _bounded_text(
            source_text,
            min(FAMILY_CONTEXT_MEMBER_TEXT_CHARACTERS, remaining_text),
        )
        if excerpt:
            add(
                "text",
                f"Bounded text from {role} family member (context only)",
                excerpt,
            )
            remaining_text -= len(excerpt)

    items.extend(_identity_items(members=members, identity_context=identity_context))
    citation_ids = [item.citation_id for item in items]
    if len(citation_ids) != len(set(citation_ids)):
        raise ValueError("Privilege family evidence contains duplicate citation identifiers")
    payload = [item.as_model_input() for item in items]
    return DocumentEvidenceContext(items=tuple(items), context_hash=content_hash(payload))


@lru_cache(maxsize=32)
def _load_json_artifact(
    *,
    artifact_id: uuid.UUID,
    expected_hash: str,
    actor_user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
) -> dict[str, Any]:
    destination = io.BytesIO()
    copy_workflow_artifact_content(
        artifact_id=artifact_id,
        destination=destination,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
    )
    content = destination.getvalue()
    if hashlib.sha256(content).hexdigest() != expected_hash:
        raise ValueError("Frozen Privilege Review artifact content hash does not match")
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise TypeError("Frozen Privilege Review artifact must contain a JSON object")
    return payload


@lru_cache(maxsize=4_096)
def _load_family_member_text(
    *,
    collection_item_id: uuid.UUID,
    expected_artifact_id: uuid.UUID,
    expected_content_hash: str,
    actor_user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
) -> str:
    source = get_preferred_text_source(
        collection_item_id=collection_item_id,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
        max_bytes=FAMILY_CONTEXT_SOURCE_MAX_BYTES,
    )
    if source is None:
        raise ValueError(f"Family member has no available text source: {collection_item_id}")
    if source.artifact_id != expected_artifact_id or source.content_hash != expected_content_hash:
        raise ValueError(f"Family member text source changed after the evidence packet was frozen: {collection_item_id}")
    return source.text


def load_privilege_family_evidence_context(
    *,
    packet_reference: dict[str, Any],
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    target_document_id: uuid.UUID,
    target_collection_item_id: uuid.UUID,
    target_snapshot: AnalysisInputItemSnapshot,
    actor_user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
) -> DocumentEvidenceContext:
    """Load one target's immutable family packet and resolve its bounded text context."""

    packets = _load_json_artifact(
        artifact_id=uuid.UUID(str(packet_reference["artifact_id"])),
        expected_hash=str(packet_reference["content_hash"]),
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
    )
    if (
        packets.get("kind") != "PRIVILEGE_EVIDENCE_PACKETS"
        or packets.get("matter_id") != str(matter_id)
        or packets.get("program_id") != str(program_id)
    ):
        raise ValueError("Frozen privilege evidence packets do not belong to this program")
    matches = [
        packet
        for packet in packets.get("packets", [])
        if str(target_document_id) in packet.get("review_target_document_ids", [])
    ]
    if len(matches) != 1:
        raise ValueError("Review document must belong to exactly one frozen privilege evidence packet")
    matched_packet = matches[0]
    family_key = str(matched_packet.get("family_key") or "")
    family_packets = [
        value
        for value in packets.get("packets", [])
        if isinstance(value, dict) and str(value.get("family_key") or "") == family_key
    ]
    # Large families may be frozen as several bounded transport packets. Evidence is
    # nevertheless a family-level result, so reconstruct one deterministic family
    # manifest here and let the existing context budget bound the text supplied to
    # the model. Members and review targets are de-duplicated across packets.
    members_by_document: dict[str, dict[str, Any]] = {}
    review_target_document_ids: list[str] = []
    for family_packet in family_packets:
        for member in family_packet.get("members", []):
            if isinstance(member, dict) and member.get("matter_document_id"):
                members_by_document.setdefault(str(member["matter_document_id"]), member)
        for value in family_packet.get("review_target_document_ids", []):
            rendered = str(value)
            if rendered not in review_target_document_ids:
                review_target_document_ids.append(rendered)
    packet = {
        **matched_packet,
        "packet_key": f"family:{family_key}",
        "packet_ordinal": 1,
        "packet_count_for_family": len(family_packets),
        "review_target_document_ids": review_target_document_ids,
        "member_count": len(members_by_document),
        "members": list(members_by_document.values()),
    }
    identity_reference = packets.get("source_identity_context")
    if not isinstance(identity_reference, dict):
        raise TypeError("Frozen privilege evidence packets are missing identity context")
    identity_context = _load_json_artifact(
        artifact_id=uuid.UUID(str(identity_reference["artifact_id"])),
        expected_hash=str(identity_reference["content_hash"]),
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
    )
    if (
        identity_context.get("kind") != "PRIVILEGE_IDENTITY_CONTEXT"
        or identity_context.get("matter_id") != str(matter_id)
        or identity_context.get("program_id") != str(program_id)
    ):
        raise ValueError("Frozen privilege identity context does not belong to this program")

    family_member_text: dict[uuid.UUID, str] = {}
    fully_mapped_document_ids = set(review_target_document_ids)
    for member in packet.get("members", []):
        if not isinstance(member, dict):
            continue
        member_id = uuid.UUID(str(member["collection_item_id"]))
        if member_id == target_collection_item_id:
            continue
        # Every in-scope review target is now mapped independently and merged
        # into the family artifact. Do not repeatedly inject its bounded text
        # into every sibling's extraction request. Context-only members remain
        # available here because they are outside the review denominator.
        if str(member.get("matter_document_id") or "") in fully_mapped_document_ids:
            continue
        text_source = member.get("text_source")
        if not isinstance(text_source, dict):
            continue
        family_member_text[member_id] = _load_family_member_text(
            collection_item_id=member_id,
            expected_artifact_id=uuid.UUID(str(text_source["artifact_id"])),
            expected_content_hash=str(text_source["content_hash"]),
            actor_user_id=actor_user_id,
            tenant_id=tenant_id,
            client_id=client_id,
        )
    return build_privilege_family_evidence_context(
        target_snapshot=target_snapshot,
        target_collection_item_id=target_collection_item_id,
        packet=packet,
        identity_context=identity_context,
        family_member_text=family_member_text,
    )


def privilege_evidence_family_groups(packets: dict[str, Any]) -> list[dict[str, Any]]:
    """Collapse frozen transport packets into one evidence execution per family."""

    grouped: dict[str, dict[str, Any]] = {}
    for packet in packets.get("packets", []):
        if not isinstance(packet, dict):
            continue
        family_key = str(packet.get("family_key") or "")
        if not family_key:
            raise ValueError("Frozen privilege evidence packet is missing its family key")
        group = grouped.setdefault(
            family_key,
            {
                "family_key": family_key,
                "family_id": packet.get("family_id"),
                "packet_keys": [],
                "review_target_document_ids": [],
            },
        )
        packet_key = str(packet.get("packet_key") or "")
        if packet_key:
            group["packet_keys"].append(packet_key)
        for value in packet.get("review_target_document_ids", []):
            rendered = str(value)
            if rendered not in group["review_target_document_ids"]:
                group["review_target_document_ids"].append(rendered)
    result: list[dict[str, Any]] = []
    for family_key in sorted(grouped):
        group = grouped[family_key]
        targets = group["review_target_document_ids"]
        if not targets:
            continue
        result.append(
            {
                **group,
                "anchor_document_id": targets[0],
                "review_target_document_count": len(targets),
            }
        )
    return result


def load_privilege_evidence_packets(
    *,
    packet_reference: dict[str, Any],
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
) -> dict[str, Any]:
    """Load and validate an immutable Privilege Review packet artifact."""

    packets = _load_json_artifact(
        artifact_id=uuid.UUID(str(packet_reference["artifact_id"])),
        expected_hash=str(packet_reference["content_hash"]),
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
    )
    if (
        packets.get("kind") != "PRIVILEGE_EVIDENCE_PACKETS"
        or packets.get("matter_id") != str(matter_id)
        or packets.get("program_id") != str(program_id)
    ):
        raise ValueError("Frozen privilege evidence packets do not belong to this program")
    return packets
