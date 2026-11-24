from __future__ import annotations

import hashlib
import io
import json
import unicodedata
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from email.utils import getaddresses
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.artifact_gateway import (
    IdentityParticipantSnapshot,
    get_identity_metadata_snapshots,
    store_workflow_artifact,
)
from app.audit import record_audit
from app.job_executions import (
    IDENTITY_ANALYSIS_JOB_TYPE,
    JobExecutionConflict,
    JobScope,
    create_job_execution,
    current_job_execution,
    transition_job_execution,
)
from app.matter_entities import extract_metadata_emails
from app.models import Matter, MatterDocument, MatterIdentityAnalysis
from artifact_service.workflow_artifact_types import ArtifactType

SCHEMA_VERSION = 2
PAGE_SIZE = 500
EMAIL_VALUE_BOUNDARY_NOISE = " <>\"'\u2018\u2019\u201c\u201d"


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass
class _IdentityAggregate:
    display_names: Counter[str] = field(default_factory=Counter)
    sender_count: int = 0
    to_count: int = 0
    cc_count: int = 0
    bcc_count: int = 0
    document_ids: set[uuid.UUID] = field(default_factory=set)


@dataclass
class _UnresolvedParticipantAggregate:
    variants: Counter[str] = field(default_factory=Counter)
    reasons: Counter[str] = field(default_factory=Counter)
    roles: Counter[str] = field(default_factory=Counter)
    document_ids: set[uuid.UUID] = field(default_factory=set)


def normalize_unresolved_participant(value: str) -> str:
    """Return a stable comparison key without changing source metadata."""

    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def unresolved_participant_reason(value: str) -> str:
    if "@" not in value:
        return "NO_EMAIL_ADDRESS"
    if value.count("@") > 1:
        return "ENCODED_OR_MULTIPLE_ADDRESS"
    domain = value.rsplit("@", 1)[1].strip(EMAIL_VALUE_BOUNDARY_NOISE)
    if "." not in domain:
        return "LEGACY_DIRECTORY_IDENTIFIER"
    return "MALFORMED_EMAIL_ADDRESS"


class IdentityInventoryBuilder:
    """Aggregate only structured sender and recipient metadata."""

    def __init__(self) -> None:
        self.identities: dict[str, _IdentityAggregate] = defaultdict(_IdentityAggregate)
        self.unresolved_participants: dict[str, _UnresolvedParticipantAggregate] = defaultdict(
            _UnresolvedParticipantAggregate
        )
        self.processed_document_count = 0
        self.documents_with_participants_count = 0
        self.invalid_participant_count = 0

    @staticmethod
    def _observations(participant: IdentityParticipantSnapshot) -> list[tuple[str, str | None]]:
        raw = participant.email_address or ""
        addresses = extract_metadata_emails(raw) if raw.strip() else []
        parsed_names = {
            address.casefold(): name.strip()
            for name, address in getaddresses([raw])
            if address and name and name.strip()
        }
        return [
            (
                address,
                participant.display_name.strip()
                if participant.display_name and participant.display_name.strip()
                else parsed_names.get(address),
            )
            for address in addresses
        ]

    def add_document(
        self,
        document_id: uuid.UUID,
        snapshot: Any | None,
    ) -> None:
        self.processed_document_count += 1
        if snapshot is None:
            return
        usable_addresses: set[str] = set()

        def add(participant: IdentityParticipantSnapshot | None, role: str) -> None:
            if participant is None:
                return
            observations = self._observations(participant)
            if not observations:
                unresolved_value = (participant.email_address or participant.display_name or "").strip()
                if unresolved_value:
                    self.invalid_participant_count += 1
                    normalized_value = normalize_unresolved_participant(unresolved_value)
                    aggregate = self.unresolved_participants[normalized_value]
                    aggregate.variants[unresolved_value] += 1
                    aggregate.reasons[unresolved_participant_reason(unresolved_value)] += 1
                    aggregate.roles[role] += 1
                    aggregate.document_ids.add(document_id)
                return
            for address, display_name in observations:
                aggregate = self.identities[address]
                usable_addresses.add(address)
                aggregate.document_ids.add(document_id)
                if display_name:
                    aggregate.display_names[display_name] += 1
                if role == "SENDER":
                    aggregate.sender_count += 1
                elif role == "TO":
                    aggregate.to_count += 1
                elif role == "CC":
                    aggregate.cc_count += 1
                elif role == "BCC":
                    aggregate.bcc_count += 1

        add(snapshot.sender, "SENDER")
        for role in ("TO", "CC", "BCC"):
            for participant in snapshot.recipients.get(role, []):
                add(participant, role)
        if usable_addresses:
            self.documents_with_participants_count += 1

    def result(self, *, matter_id: uuid.UUID, analysis_id: uuid.UUID, population_hash: str) -> dict[str, Any]:
        identities = []
        domain_documents: dict[str, set[uuid.UUID]] = defaultdict(set)
        domain_counts: dict[str, Counter[str]] = defaultdict(Counter)
        for address in sorted(self.identities):
            aggregate = self.identities[address]
            domain = address.rsplit("@", 1)[1]
            domain_documents[domain].update(aggregate.document_ids)
            domain_counts[domain]["identity_count"] += 1
            domain_counts[domain]["sender_count"] += aggregate.sender_count
            domain_counts[domain]["recipient_count"] += aggregate.to_count + aggregate.cc_count + aggregate.bcc_count
            identities.append(
                {
                    "email_address": address,
                    "domain": domain,
                    "display_names": [
                        {"value": value, "observation_count": count}
                        for value, count in sorted(
                            aggregate.display_names.items(),
                            key=lambda item: (-item[1], item[0].casefold()),
                        )
                    ],
                    "document_count": len(aggregate.document_ids),
                    "sender_count": aggregate.sender_count,
                    "to_count": aggregate.to_count,
                    "cc_count": aggregate.cc_count,
                    "bcc_count": aggregate.bcc_count,
                }
            )
        domains = [
            {
                "domain": domain,
                "identity_count": counts["identity_count"],
                "document_count": len(domain_documents[domain]),
                "sender_count": counts["sender_count"],
                "recipient_count": counts["recipient_count"],
            }
            for domain, counts in sorted(domain_counts.items())
        ]
        unresolved_participants = [
            {
                "normalized_value": normalized_value,
                "observation_count": sum(aggregate.variants.values()),
                "document_count": len(aggregate.document_ids),
                "variants": [
                    {"value": value, "observation_count": count}
                    for value, count in sorted(
                        aggregate.variants.items(),
                        key=lambda item: (-item[1], item[0].casefold()),
                    )
                ],
                "reason_counts": dict(sorted(aggregate.reasons.items())),
                "role_counts": dict(sorted(aggregate.roles.items())),
            }
            for normalized_value, aggregate in sorted(self.unresolved_participants.items())
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "analysis_id": str(analysis_id),
            "matter_id": str(matter_id),
            "population_hash": population_hash,
            "source_contract": "structured-email-participants-only-v1",
            "coverage": {
                "documents_scanned": self.processed_document_count,
                "documents_with_usable_participants": self.documents_with_participants_count,
                "documents_without_usable_participants": (
                    self.processed_document_count - self.documents_with_participants_count
                ),
                "unresolved_participant_observations": self.invalid_participant_count,
                "unique_normalized_unresolved_values": len(unresolved_participants),
                "unique_email_addresses": len(identities),
                "unique_domains": len(domains),
            },
            "identities": identities,
            "domains": domains,
            "unresolved_participants": unresolved_participants,
        }


def create_identity_analysis(
    db: Session,
    matter: Matter,
    *,
    actor_user_id: uuid.UUID,
) -> MatterIdentityAnalysis:
    active = db.scalar(
        select(MatterIdentityAnalysis).where(
            MatterIdentityAnalysis.matter_id == matter.id,
            MatterIdentityAnalysis.status.in_(("QUEUED", "RUNNING")),
        )
    )
    if active is not None:
        raise JobExecutionConflict("An Identity Analysis is already active for this matter")
    total = int(
        db.scalar(select(func.count()).select_from(MatterDocument).where(MatterDocument.matter_id == matter.id)) or 0
    )
    analysis_id = uuid.uuid4()
    analysis = MatterIdentityAnalysis(
        id=analysis_id,
        tenant_id=matter.client.tenant_id,
        matter_id=matter.id,
        status="QUEUED",
        workflow_id=f"matter-identity-analysis:{analysis_id}:attempt:1",
        current_stage="QUEUED",
        total_document_count=total,
        created_by_user_id=actor_user_id,
    )
    db.add(analysis)
    db.flush()
    create_job_execution(
        db,
        job_type=IDENTITY_ANALYSIS_JOB_TYPE,
        job_id=analysis.id,
        scope=JobScope(analysis.tenant_id, matter.client_id, matter.id),
        workflow_key="matter_identity_analysis_v1",
        code_version="1",
        dbos_workflow_id=analysis.workflow_id,
        initiated_by_user_id=actor_user_id,
        idempotency_key=f"identity-analysis:{analysis.id}:attempt:1",
        current_stage="QUEUED",
        progress_total=total,
        progress_unit="documents",
        configuration_snapshot={
            "scope": "MATTER",
            "metadata_contract": "structured-email-participants-only-v1",
            "schema_version": SCHEMA_VERSION,
        },
    )
    record_audit(
        db,
        tenant_id=analysis.tenant_id,
        actor_user_id=actor_user_id,
        action="identity_analysis.requested",
        target_type="matter_identity_analysis",
        target_id=analysis.id,
        details={"matter_id": str(matter.id), "document_count": total},
    )
    return analysis


def run_identity_analysis(
    analysis_id: uuid.UUID,
    *,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as db:
        analysis = db.get(MatterIdentityAnalysis, analysis_id)
        if analysis is None:
            raise ValueError("Identity Analysis not found")
        matter = db.get(Matter, analysis.matter_id)
        execution = current_job_execution(db, job_type=IDENTITY_ANALYSIS_JOB_TYPE, job_id=analysis.id)
        if matter is None or execution is None:
            raise ValueError("Identity Analysis context is unavailable")
        rows = list(
            db.execute(
                select(MatterDocument.id, MatterDocument.collection_item_id)
                .where(MatterDocument.matter_id == matter.id)
                .order_by(MatterDocument.id)
            )
        )
        population_hash = canonical_hash([(str(document_id), str(item_id)) for document_id, item_id in rows])
        context = {
            "matter_id": matter.id,
            "client_id": matter.client_id,
            "tenant_id": matter.client.tenant_id,
            "actor_user_id": analysis.created_by_user_id,
            "workflow_run_id": execution.id,
        }
        transition_job_execution(
            db,
            job_type=IDENTITY_ANALYSIS_JOB_TYPE,
            job_id=analysis.id,
            status="RUNNING" if execution.status == "QUEUED" else None,
            current_stage="SCANNING_METADATA",
            progress_total=len(rows),
            progress={"population_hash": population_hash},
        )
        db.commit()

    builder = IdentityInventoryBuilder()
    for start in range(0, len(rows), PAGE_SIZE):
        page = rows[start : start + PAGE_SIZE]
        snapshots = get_identity_metadata_snapshots(
            collection_item_ids=[item_id for _document_id, item_id in page],
            actor_user_id=context["actor_user_id"],
            tenant_id=context["tenant_id"],
            client_id=context["client_id"],
        )
        for document_id, item_id in page:
            builder.add_document(document_id, snapshots.get(item_id))
        with session_factory() as db:
            execution = current_job_execution(db, job_type=IDENTITY_ANALYSIS_JOB_TYPE, job_id=analysis_id)
            completed = max(builder.processed_document_count, int(execution.progress_completed) if execution else 0)
            transition_job_execution(
                db,
                job_type=IDENTITY_ANALYSIS_JOB_TYPE,
                job_id=analysis_id,
                current_stage="SCANNING_METADATA",
                progress_completed=completed,
                progress={
                    "population_hash": population_hash,
                    "documents_with_usable_participants": builder.documents_with_participants_count,
                    "unique_email_addresses": len(builder.identities),
                    "unresolved_participant_observations": builder.invalid_participant_count,
                    "unique_normalized_unresolved_values": len(builder.unresolved_participants),
                },
            )
            db.commit()

    result = builder.result(matter_id=context["matter_id"], analysis_id=analysis_id, population_hash=population_hash)
    content_bytes = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    content_hash = hashlib.sha256(content_bytes).hexdigest()
    derivation_key = canonical_hash(
        {
            "artifact_type": ArtifactType.IDENTITY_ANALYSIS_RESULT.value,
            "analysis_id": str(analysis_id),
            "population_hash": population_hash,
            "schema_version": SCHEMA_VERSION,
        }
    )
    artifact, _created = store_workflow_artifact(
        content=io.BytesIO(content_bytes),
        artifact_type=ArtifactType.IDENTITY_ANALYSIS_RESULT,
        core_scope_kind="MATTER",
        core_scope_id=context["matter_id"],
        schema_version=SCHEMA_VERSION,
        derivation_key=derivation_key,
        content_hash=content_hash,
        byte_length=len(content_bytes),
        media_type="application/json",
        producer_kind="CORE",
        producer_run_id=context["workflow_run_id"],
        artifact_metadata={
            "analysis_id": str(analysis_id),
            "population_hash": population_hash,
            "documents_scanned": builder.processed_document_count,
            "identity_count": len(result["identities"]),
            "domain_count": len(result["domains"]),
        },
        lineage=[],
        actor_user_id=context["actor_user_id"],
        tenant_id=context["tenant_id"],
        client_id=context["client_id"],
        filename=f"identity-analysis-{analysis_id}.json",
    )

    top_domains = sorted(
        result["domains"], key=lambda row: (-row["document_count"], -row["identity_count"], row["domain"])
    )[:20]
    top_identities = sorted(
        result["identities"], key=lambda row: (-row["document_count"], row["email_address"])
    )[:20]
    summary = {
        **result["coverage"],
        "population_hash": population_hash,
        "source_contract": result["source_contract"],
        "top_domains": top_domains,
        "top_identities": top_identities,
    }
    with session_factory() as db:
        analysis = db.get(MatterIdentityAnalysis, analysis_id)
        if analysis is None:
            raise ValueError("Identity Analysis not found while finalizing")
        analysis.result_artifact_id = artifact.artifact_id
        analysis.result_content_hash = artifact.content_hash
        analysis.result_byte_length = artifact.byte_length
        analysis.documents_with_participants_count = builder.documents_with_participants_count
        analysis.identity_count = len(result["identities"])
        analysis.domain_count = len(result["domains"])
        analysis.invalid_participant_count = builder.invalid_participant_count
        analysis.summary = summary
        transition_job_execution(
            db,
            job_type=IDENTITY_ANALYSIS_JOB_TYPE,
            job_id=analysis.id,
            status="COMPLETED",
            current_stage="COMPLETED",
            progress_completed=builder.processed_document_count,
            progress_total=builder.processed_document_count,
            progress=summary,
        )
        record_audit(
            db,
            tenant_id=analysis.tenant_id,
            actor_user_id=analysis.created_by_user_id,
            action="identity_analysis.completed",
            target_type="matter_identity_analysis",
            target_id=analysis.id,
            details={
                "matter_id": str(analysis.matter_id),
                "result_artifact_id": str(artifact.artifact_id),
                "identity_count": analysis.identity_count,
                "domain_count": analysis.domain_count,
            },
        )
        db.commit()


def fail_identity_analysis(db: Session, analysis_id: uuid.UUID, error: Exception) -> None:
    analysis = db.get(MatterIdentityAnalysis, analysis_id)
    if analysis is None or analysis.status in {"COMPLETED", "FAILED", "CANCELED"}:
        return
    transition_job_execution(
        db,
        job_type=IDENTITY_ANALYSIS_JOB_TYPE,
        job_id=analysis.id,
        status="FAILED",
        current_stage="FAILED",
        failure_code="IDENTITY_ANALYSIS_FAILED",
        failure_summary=str(error)[:4000],
        failure_retriable=False,
    )
