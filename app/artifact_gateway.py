import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.search.text import (
    SEARCH_TEXT_ROLES,
    extract_search_text,
    native_can_supply_search_text,
    read_search_text_bytes,
)
from artifact_service.auth import ArtifactPrincipal, mint_artifact_delegation
from artifact_service.config import get_artifact_settings
from artifact_service.database import ArtifactSessionLocal
from artifact_service.deletion import create_deletion_job, fail_deletion, retry_deletion_job
from artifact_service.derived import find_derived_artifact
from artifact_service.derived import store_derived_artifact as persist_derived_artifact
from artifact_service.models import (
    Artifact,
    ClientCollection,
    CollectionDeletionJob,
    CollectionItem,
    CollectionItemArtifact,
    CollectionItemCustodian,
    CollectionItemEmail,
    CollectionItemEmailRecipient,
    CollectionSelection,
    ContentBlob,
)
from artifact_service.schemas import CollectionDeletionJobRead, CollectionSelectionCreate
from artifact_service.selections import (
    create_collection_selection,
    delete_collection_selection,
    get_collection_selection_batch,
    get_collection_selection_batch_custodians,
)
from artifact_service.storage import get_storage


@dataclass(frozen=True)
class SelectionCustodian:
    custodian_id: uuid.UUID
    relationship_type: str


@dataclass(frozen=True)
class SelectionBatchItem:
    item_id: uuid.UUID
    custodians: list[SelectionCustodian]


@dataclass(frozen=True)
class SearchItemSnapshot:
    item_id: uuid.UUID
    source_item_id: str
    record_type: str
    original_filename: str
    original_extension: str | None
    original_source_path: str | None
    source_created_at: datetime | None
    source_modified_at: datetime | None
    family_id: uuid.UUID | None
    processing_status: str
    custodian_ids: list[uuid.UUID]
    email_sender: str | None
    email_subject: str | None
    email_sent_at: datetime | None
    email_received_at: datetime | None
    email_recipients: dict[str, list[str]]
    native_sha256: str
    native_byte_length: int
    page_count: int | None
    raw_metadata: dict[str, Any]
    unmapped_metadata: dict[str, Any]
    body_text: str | None


@dataclass(frozen=True)
class SearchTextArtifact:
    id: uuid.UUID
    role: str
    original_filename: str
    media_type: str
    content_blob_id: uuid.UUID | None = None
    content_hash: str | None = None
    processing_run_id: uuid.UUID | None = None


@dataclass(frozen=True)
class EmbeddingTextSource:
    artifact_id: uuid.UUID
    content_hash: str
    text: str


@dataclass(frozen=True)
class DerivedArtifactReference:
    artifact_id: uuid.UUID
    content_hash: str
    derivation_key: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class CollectionSnapshot:
    id: uuid.UUID
    tenant_id: uuid.UUID
    client_id: uuid.UUID
    name: str
    status: str


def _search_text_candidate(
    artifacts: list[SearchTextArtifact],
    *,
    record_type: str,
) -> SearchTextArtifact | None:
    for role in SEARCH_TEXT_ROLES:
        for artifact in artifacts:
            if artifact.role != role:
                continue
            if role != "NATIVE" or native_can_supply_search_text(
                media_type=artifact.media_type,
                filename=artifact.original_filename,
                record_type=record_type,
            ):
                return artifact
    return None


def _embedded_body_text(db: Session, item: CollectionItem, max_bytes: int) -> str | None:
    collection = db.get(ClientCollection, item.collection_id)
    role_order = case(
        (CollectionItemArtifact.artifact_role == "EXTRACTED_TEXT", 0),
        (CollectionItemArtifact.artifact_role == "OCR_TEXT", 1),
        (CollectionItemArtifact.artifact_role == "NATIVE", 2),
        else_=3,
    )
    rows = db.execute(
        select(CollectionItemArtifact.artifact_role, Artifact)
        .join(Artifact, Artifact.id == CollectionItemArtifact.artifact_id)
        .where(
            CollectionItemArtifact.collection_item_id == item.id,
            CollectionItemArtifact.artifact_role.in_(SEARCH_TEXT_ROLES),
            Artifact.status == "FINALIZED",
            (
                (CollectionItemArtifact.artifact_role != "NORMALIZED_TEXT")
                | (CollectionItemArtifact.processing_run_id == collection.active_text_processing_run_id)
            ),
        )
        .order_by(role_order, Artifact.created_at.desc())
    ).all()
    artifacts = [
        SearchTextArtifact(
            id=artifact.id,
            role=role,
            original_filename=artifact.original_filename,
            media_type=artifact.media_type,
            content_blob_id=artifact.content_blob_id,
            content_hash=artifact.content_hash,
            processing_run_id=None,
        )
        for role, artifact in rows
    ]
    candidate = _search_text_candidate(artifacts, record_type=item.record_type)
    if candidate is None or candidate.content_blob_id is None:
        return None
    blob = db.get(ContentBlob, candidate.content_blob_id)
    if blob is None:
        raise ValueError("Search text artifact content is unavailable")
    content = read_search_text_bytes(get_storage().open(blob.bucket_name, blob.storage_key), max_bytes)
    return extract_search_text(
        content,
        role=candidate.role,
        media_type=candidate.media_type,
        filename=candidate.original_filename,
        record_type=item.record_type,
    )


def _remote_body_text(
    client: httpx.Client,
    artifacts: list[dict[str, Any]],
    *,
    headers: dict[str, str],
    record_type: str,
    max_bytes: int,
    active_run_id: str | None,
) -> str | None:
    candidates = [
        SearchTextArtifact(
            id=uuid.UUID(artifact["id"]),
            role=artifact["role"],
            original_filename=artifact["original_filename"],
            media_type=artifact["media_type"],
            processing_run_id=(uuid.UUID(artifact["processing_run_id"]) if artifact.get("processing_run_id") else None),
        )
        for artifact in artifacts
        if artifact.get("status") == "FINALIZED"
        and artifact.get("role") in SEARCH_TEXT_ROLES
        and (
            artifact.get("role") != "NORMALIZED_TEXT"
            or (active_run_id and artifact.get("processing_run_id") == active_run_id)
        )
    ]
    candidate = _search_text_candidate(candidates, record_type=record_type)
    if candidate is None:
        return None
    with client.stream("GET", f"/v1/artifacts/{candidate.id}/content", headers=headers) as response:
        response.raise_for_status()
        content = bytearray()
        for chunk in response.iter_bytes():
            remaining = max_bytes - len(content)
            if remaining <= 0:
                break
            content.extend(chunk[:remaining])
    return extract_search_text(
        bytes(content),
        role=candidate.role,
        media_type=candidate.media_type,
        filename=candidate.original_filename,
        record_type=record_type,
    )


def _principal(actor_user_id: uuid.UUID, tenant_id: uuid.UUID, client_id: uuid.UUID) -> ArtifactPrincipal:
    return ArtifactPrincipal(
        actor_user_id=actor_user_id,
        allowed_tenant_ids=frozenset({tenant_id}),
        allowed_clients=frozenset({(tenant_id, client_id)}),
        allowed_custodians=frozenset(),
    )


def _headers(actor_user_id: uuid.UUID, tenant_id: uuid.UUID, client_id: uuid.UUID) -> dict[str, str]:
    token = mint_artifact_delegation(
        _principal(actor_user_id, tenant_id, client_id),
        get_artifact_settings(),
    )
    return {"authorization": f"Bearer {token}"}


def _delegated_headers(principal: ArtifactPrincipal) -> dict[str, str]:
    token = mint_artifact_delegation(principal, get_artifact_settings())
    return {"authorization": f"Bearer {token}"}


def get_collection_snapshot(
    collection_id: uuid.UUID,
    principal: ArtifactPrincipal,
    artifact_db: Session | None = None,
) -> CollectionSnapshot | None:
    settings = get_settings()
    if settings.artifact_mode == "embedded":
        if artifact_db is None:
            raise ValueError("Embedded Artifact access requires a database session")
        collection = artifact_db.get(ClientCollection, collection_id)
        if collection is None:
            return None
        if not principal.can_access_client(collection.tenant_id, collection.client_id):
            raise PermissionError("Client artifact access denied")
        return CollectionSnapshot(
            id=collection.id,
            tenant_id=collection.tenant_id,
            client_id=collection.client_id,
            name=collection.name,
            status=collection.status,
        )
    with httpx.Client(base_url=settings.artifact_base_url, timeout=30) as client:
        response = client.get(
            f"/v1/collections/{collection_id}",
            headers=_delegated_headers(principal),
        )
    if response.status_code == 404:
        return None
    if response.status_code == 403:
        raise PermissionError("Client artifact access denied")
    response.raise_for_status()
    data = response.json()
    return CollectionSnapshot(
        id=uuid.UUID(data["id"]),
        tenant_id=uuid.UUID(data["tenant_id"]),
        client_id=uuid.UUID(data["client_id"]),
        name=data["name"],
        status=data["status"],
    )


def create_collection_deletion(
    collection_id: uuid.UUID,
    principal: ArtifactPrincipal,
    artifact_db: Session | None = None,
) -> CollectionDeletionJobRead:
    settings = get_settings()
    if settings.artifact_mode == "embedded":
        if artifact_db is None:
            raise ValueError("Embedded Artifact access requires a database session")
        job = create_deletion_job(artifact_db, collection_id, principal.actor_user_id)
        artifact_db.commit()
        return CollectionDeletionJobRead.model_validate(job)
    with httpx.Client(base_url=settings.artifact_base_url, timeout=30) as client:
        response = client.post(
            f"/v1/internal/collections/{collection_id}/deletions",
            headers=_delegated_headers(principal),
        )
    if response.status_code == 409:
        raise ValueError(response.json().get("error", {}).get("message", "Collection cannot be deleted"))
    response.raise_for_status()
    return CollectionDeletionJobRead.model_validate(response.json())


def get_collection_deletion_job(
    job_id: uuid.UUID,
    principal: ArtifactPrincipal,
    artifact_db: Session | None = None,
) -> CollectionDeletionJobRead | None:
    settings = get_settings()
    if settings.artifact_mode == "embedded":
        if artifact_db is None:
            raise ValueError("Embedded Artifact access requires a database session")
        job = artifact_db.get(CollectionDeletionJob, job_id)
        if job is None:
            return None
        if not principal.can_access_client(job.tenant_id, job.client_id):
            raise PermissionError("Client artifact access denied")
        return CollectionDeletionJobRead.model_validate(job)
    with httpx.Client(base_url=settings.artifact_base_url, timeout=30) as client:
        response = client.get(
            f"/v1/internal/collection-deletions/{job_id}",
            headers=_delegated_headers(principal),
        )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return CollectionDeletionJobRead.model_validate(response.json())


def get_latest_collection_deletion(
    collection_id: uuid.UUID,
    principal: ArtifactPrincipal,
    artifact_db: Session | None = None,
) -> CollectionDeletionJobRead | None:
    settings = get_settings()
    if settings.artifact_mode == "embedded":
        if artifact_db is None:
            raise ValueError("Embedded Artifact access requires a database session")
        job = artifact_db.scalar(
            select(CollectionDeletionJob)
            .where(CollectionDeletionJob.collection_id == collection_id)
            .order_by(CollectionDeletionJob.created_at.desc())
        )
        if job is None:
            return None
        if not principal.can_access_client(job.tenant_id, job.client_id):
            raise PermissionError("Client artifact access denied")
        return CollectionDeletionJobRead.model_validate(job)
    with httpx.Client(base_url=settings.artifact_base_url, timeout=30) as client:
        response = client.get(
            f"/v1/internal/collections/{collection_id}/deletions/latest",
            headers=_delegated_headers(principal),
        )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return CollectionDeletionJobRead.model_validate(response.json())


def retry_collection_deletion(
    job_id: uuid.UUID,
    principal: ArtifactPrincipal,
    artifact_db: Session | None = None,
) -> CollectionDeletionJobRead:
    settings = get_settings()
    if settings.artifact_mode == "embedded":
        if artifact_db is None:
            raise ValueError("Embedded Artifact access requires a database session")
        job = retry_deletion_job(artifact_db, job_id)
        artifact_db.commit()
        return CollectionDeletionJobRead.model_validate(job)
    with httpx.Client(base_url=settings.artifact_base_url, timeout=30) as client:
        response = client.post(
            f"/v1/internal/collection-deletions/{job_id}/retry",
            headers=_delegated_headers(principal),
        )
    if response.status_code == 409:
        raise ValueError(response.json().get("error", {}).get("message", "Deletion cannot be retried"))
    response.raise_for_status()
    return CollectionDeletionJobRead.model_validate(response.json())


def fail_collection_deletion_job(
    job_id: uuid.UUID,
    message: str,
    principal: ArtifactPrincipal,
    artifact_db: Session | None = None,
) -> CollectionDeletionJobRead | None:
    settings = get_settings()
    if settings.artifact_mode == "embedded":
        if artifact_db is None:
            raise ValueError("Embedded Artifact access requires a database session")
        job = fail_deletion(artifact_db, job_id, message)
        return CollectionDeletionJobRead.model_validate(job) if job is not None else None
    with httpx.Client(base_url=settings.artifact_base_url, timeout=30) as client:
        response = client.post(
            f"/v1/internal/collection-deletions/{job_id}/failure",
            json={"message": message[:4000]},
            headers=_delegated_headers(principal),
        )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return CollectionDeletionJobRead.model_validate(response.json())


def ensure_collection_scope(
    collection_id: uuid.UUID,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    artifact_db: Session | None = None,
) -> None:
    settings = get_settings()
    if settings.artifact_mode == "embedded":
        if artifact_db is not None:
            collection = artifact_db.get(ClientCollection, collection_id)
            if collection is None:
                raise ValueError("Collection not found")
            if collection.tenant_id != tenant_id or collection.client_id != client_id:
                raise PermissionError("Collection is not available to this matter")
            if collection.status == "DELETING":
                raise ValueError("Collection is being deleted")
        else:
            with ArtifactSessionLocal() as db:
                collection = db.get(ClientCollection, collection_id)
                if collection is None:
                    raise ValueError("Collection not found")
                if collection.tenant_id != tenant_id or collection.client_id != client_id:
                    raise PermissionError("Collection is not available to this matter")
                if collection.status == "DELETING":
                    raise ValueError("Collection is being deleted")
        return

    with httpx.Client(base_url=settings.artifact_base_url, timeout=30) as client:
        response = client.get(
            f"/v1/collections/{collection_id}",
            headers=_headers(actor_user_id, tenant_id, client_id),
        )
    if response.status_code == 404:
        raise ValueError("Collection not found")
    if response.status_code == 403:
        raise PermissionError("Collection is not available to this matter")
    response.raise_for_status()
    data = response.json()
    if uuid.UUID(data["client_id"]) != client_id or uuid.UUID(data["tenant_id"]) != tenant_id:
        raise PermissionError("Collection is not available to this matter")
    if data.get("status") == "DELETING":
        raise ValueError("Collection is being deleted")


def create_selection(
    *,
    collection_id: uuid.UUID,
    request_id: uuid.UUID,
    selection: dict[str, object],
    actor_user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
) -> tuple[uuid.UUID, int]:
    payload = CollectionSelectionCreate(request_id=request_id, **selection)
    settings = get_settings()
    if settings.artifact_mode == "embedded":
        with ArtifactSessionLocal() as db:
            collection = db.get(ClientCollection, collection_id)
            if collection is None:
                raise ValueError("Collection not found")
            if collection.tenant_id != tenant_id or collection.client_id != client_id:
                raise PermissionError("Collection is not available to this matter")
            if collection.status == "DELETING":
                raise ValueError("Collection is being deleted")
            frozen = create_collection_selection(db, collection, payload)
            db.commit()
            return frozen.id, frozen.total_count

    with httpx.Client(base_url=settings.artifact_base_url, timeout=120) as client:
        response = client.post(
            f"/v1/collections/{collection_id}/selections",
            json=payload.model_dump(mode="json"),
            headers=_headers(actor_user_id, tenant_id, client_id),
        )
    response.raise_for_status()
    data = response.json()
    return uuid.UUID(data["id"]), data["total_count"]


def selection_batch(
    *,
    selection_id: uuid.UUID,
    offset: int,
    limit: int,
    actor_user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
) -> list[SelectionBatchItem]:
    settings = get_settings()
    if settings.artifact_mode == "embedded":
        with ArtifactSessionLocal() as db:
            selection = db.get(CollectionSelection, selection_id)
            if selection is None:
                raise ValueError("Collection selection not found")
            item_ids = get_collection_selection_batch(db, selection, offset=offset, limit=limit)
            custodians = get_collection_selection_batch_custodians(db, item_ids)
            return [
                SelectionBatchItem(
                    item_id=item_id,
                    custodians=[
                        SelectionCustodian(custodian_id=custodian_id, relationship_type=relationship_type)
                        for custodian_id, relationship_type in custodians[item_id]
                    ],
                )
                for item_id in item_ids
            ]

    with httpx.Client(base_url=settings.artifact_base_url, timeout=120) as client:
        response = client.get(
            f"/v1/collection-selections/{selection_id}/items",
            params={"offset": offset, "limit": limit},
            headers=_headers(actor_user_id, tenant_id, client_id),
        )
    response.raise_for_status()
    return [
        SelectionBatchItem(
            item_id=uuid.UUID(item["item_id"]),
            custodians=[
                SelectionCustodian(
                    custodian_id=uuid.UUID(value["custodian_id"]),
                    relationship_type=value["relationship_type"],
                )
                for value in item["custodians"]
            ],
        )
        for item in response.json()["items"]
    ]


def remove_selection(
    *,
    selection_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
) -> None:
    settings = get_settings()
    if settings.artifact_mode == "embedded":
        with ArtifactSessionLocal() as db:
            selection = db.get(CollectionSelection, selection_id)
            if selection is not None:
                delete_collection_selection(db, selection)
                db.commit()
        return

    with httpx.Client(base_url=settings.artifact_base_url, timeout=120) as client:
        response = client.delete(
            f"/v1/collection-selections/{selection_id}",
            headers=_headers(actor_user_id, tenant_id, client_id),
        )
    response.raise_for_status()


def get_search_item_snapshot(
    *,
    collection_item_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
) -> SearchItemSnapshot:
    settings = get_settings()
    if settings.artifact_mode == "embedded":
        with ArtifactSessionLocal() as db:
            item = db.get(CollectionItem, collection_item_id)
            if item is None:
                raise ValueError("Collection item not found")
            if item.tenant_id != tenant_id or item.client_id != client_id:
                raise PermissionError("Collection item is not available to this matter")
            custodian_ids = list(
                db.scalars(
                    select(CollectionItemCustodian.custodian_id).where(
                        CollectionItemCustodian.collection_item_id == item.id
                    )
                )
            )
            email = db.get(CollectionItemEmail, item.id)
            recipients: dict[str, list[str]] = {"TO": [], "CC": [], "BCC": []}
            if email is not None:
                for recipient in db.scalars(
                    select(CollectionItemEmailRecipient)
                    .where(CollectionItemEmailRecipient.collection_item_id == item.id)
                    .order_by(CollectionItemEmailRecipient.recipient_type, CollectionItemEmailRecipient.ordinal)
                ):
                    value = recipient.email_address or recipient.display_name
                    if value:
                        recipients[recipient.recipient_type].append(value)
            native_link = db.scalar(
                select(CollectionItemArtifact).where(
                    CollectionItemArtifact.collection_item_id == item.id,
                    CollectionItemArtifact.artifact_role == "NATIVE",
                )
            )
            if native_link is None:
                raise ValueError("Collection item has no native artifact")
            native = db.get(Artifact, native_link.artifact_id)
            if native is None:
                raise ValueError("Collection item native artifact is missing")
            return SearchItemSnapshot(
                item_id=item.id,
                source_item_id=item.source_item_id,
                record_type=item.record_type,
                original_filename=item.original_filename,
                original_extension=item.original_extension,
                original_source_path=item.original_source_path,
                source_created_at=item.source_created_at,
                source_modified_at=item.source_modified_at,
                family_id=item.family_id,
                processing_status=item.processing_status,
                custodian_ids=custodian_ids,
                email_sender=email.sender if email else None,
                email_subject=email.subject if email else None,
                email_sent_at=email.sent_at if email else None,
                email_received_at=email.received_at if email else None,
                email_recipients=recipients,
                native_sha256=native.content_hash,
                native_byte_length=native.byte_length,
                page_count=native_link.page_count,
                raw_metadata=item.raw_metadata or {},
                unmapped_metadata=item.unmapped_metadata or {},
                body_text=_embedded_body_text(db, item, settings.search_body_text_max_bytes),
            )

    with httpx.Client(base_url=settings.artifact_base_url, timeout=30) as client:
        headers = _headers(actor_user_id, tenant_id, client_id)
        response = client.get(
            f"/v1/collection-items/{collection_item_id}",
            headers=headers,
        )
        if response.status_code == 404:
            raise ValueError("Collection item not found")
        if response.status_code == 403:
            raise PermissionError("Collection item is not available to this matter")
        response.raise_for_status()
        data = response.json()
        artifacts_response = client.get(
            f"/v1/collection-items/{collection_item_id}/artifacts",
            headers=headers,
        )
        artifacts_response.raise_for_status()
        collection_response = client.get(f"/v1/collections/{data['collection_id']}", headers=headers)
        collection_response.raise_for_status()
        body_text = _remote_body_text(
            client,
            artifacts_response.json(),
            headers=headers,
            record_type=data["record_type"],
            max_bytes=settings.search_body_text_max_bytes,
            active_run_id=collection_response.json().get("active_text_processing_run_id"),
        )

        recipients: dict[str, list[str]] = {"TO": [], "CC": [], "BCC": []}
        email = data.get("email") or {}
        for recipient in email.get("recipients", []):
            value = recipient.get("email_address") or recipient.get("display_name")
            if value:
                recipients[recipient["recipient_type"]].append(value)
        native = data["native_artifact"]
        return SearchItemSnapshot(
            item_id=uuid.UUID(data["id"]),
            source_item_id=data["source_item_id"],
            record_type=data["record_type"],
            original_filename=data["original_filename"],
            original_extension=data.get("original_extension"),
            original_source_path=data.get("original_source_path"),
            source_created_at=(
                datetime.fromisoformat(data["source_created_at"]) if data.get("source_created_at") else None
            ),
            source_modified_at=(
                datetime.fromisoformat(data["source_modified_at"]) if data.get("source_modified_at") else None
            ),
            family_id=uuid.UUID(data["family_id"]) if data.get("family_id") else None,
            processing_status=data["processing_status"],
            custodian_ids=[uuid.UUID(value) for value in data.get("custodian_ids", [])],
            email_sender=email.get("sender"),
            email_subject=email.get("subject"),
            email_sent_at=datetime.fromisoformat(email["sent_at"]) if email.get("sent_at") else None,
            email_received_at=(
                datetime.fromisoformat(email["received_at"]) if email.get("received_at") else None
            ),
            email_recipients=recipients,
            native_sha256=native["sha256"],
            native_byte_length=native["byte_length"],
            page_count=native.get("page_count"),
            raw_metadata=data.get("raw_metadata") or {},
            unmapped_metadata=data.get("unmapped_metadata") or {},
            body_text=body_text,
        )


def get_embedding_text_source(
    *,
    collection_item_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    max_bytes: int,
) -> EmbeddingTextSource | None:
    settings = get_settings()
    if settings.artifact_mode == "embedded":
        with ArtifactSessionLocal() as db:
            item = db.get(CollectionItem, collection_item_id)
            if item is None:
                raise ValueError("Collection item not found")
            if item.tenant_id != tenant_id or item.client_id != client_id:
                raise PermissionError("Collection item is not available to this matter")
            rows = db.execute(
                select(CollectionItemArtifact.artifact_role, Artifact)
                .join(Artifact, Artifact.id == CollectionItemArtifact.artifact_id)
                .where(
                    CollectionItemArtifact.collection_item_id == item.id,
                    CollectionItemArtifact.artifact_role.in_(SEARCH_TEXT_ROLES),
                    Artifact.status == "FINALIZED",
                    (
                        (CollectionItemArtifact.artifact_role != "NORMALIZED_TEXT")
                        | (
                            CollectionItemArtifact.processing_run_id
                            == db.get(ClientCollection, item.collection_id).active_text_processing_run_id
                        )
                    ),
                )
                .order_by(Artifact.created_at.desc())
            ).all()
            candidates = [
                SearchTextArtifact(
                    id=artifact.id,
                    role=role,
                    original_filename=artifact.original_filename,
                    media_type=artifact.media_type,
                    content_blob_id=artifact.content_blob_id,
                    content_hash=artifact.content_hash,
                )
                for role, artifact in rows
            ]
            candidate = _search_text_candidate(candidates, record_type=item.record_type)
            if candidate is None or candidate.content_blob_id is None or candidate.content_hash is None:
                return None
            blob = db.get(ContentBlob, candidate.content_blob_id)
            if blob is None:
                raise ValueError("Embedding source artifact content is unavailable")
            content = read_search_text_bytes(get_storage().open(blob.bucket_name, blob.storage_key), max_bytes)
            text = extract_search_text(
                content,
                role=candidate.role,
                media_type=candidate.media_type,
                filename=candidate.original_filename,
                record_type=item.record_type,
            )
            return EmbeddingTextSource(candidate.id, candidate.content_hash, text) if text and text.strip() else None

    with httpx.Client(base_url=settings.artifact_base_url, timeout=120) as client:
        headers = _headers(actor_user_id, tenant_id, client_id)
        item_response = client.get(f"/v1/collection-items/{collection_item_id}", headers=headers)
        item_response.raise_for_status()
        item = item_response.json()
        artifacts_response = client.get(
            f"/v1/collection-items/{collection_item_id}/artifacts",
            headers=headers,
        )
        artifacts_response.raise_for_status()
        collection_response = client.get(f"/v1/collections/{item['collection_id']}", headers=headers)
        collection_response.raise_for_status()
        active_run_id = collection_response.json().get("active_text_processing_run_id")
        candidates = [
            SearchTextArtifact(
                id=uuid.UUID(artifact["id"]),
                role=artifact["role"],
                original_filename=artifact["original_filename"],
                media_type=artifact["media_type"],
                content_hash=artifact["sha256"],
                processing_run_id=(uuid.UUID(artifact["processing_run_id"]) if artifact.get("processing_run_id") else None),
            )
            for artifact in artifacts_response.json()
            if artifact.get("status") == "FINALIZED"
            and artifact.get("role") in SEARCH_TEXT_ROLES
            and (
                artifact.get("role") != "NORMALIZED_TEXT"
                or (active_run_id and artifact.get("processing_run_id") == active_run_id)
            )
        ]
        candidate = _search_text_candidate(candidates, record_type=item["record_type"])
        if candidate is None or candidate.content_hash is None:
            return None
        with client.stream("GET", f"/v1/artifacts/{candidate.id}/content", headers=headers) as response:
            response.raise_for_status()
            content = bytearray()
            for part in response.iter_bytes():
                remaining = max_bytes - len(content)
                if remaining <= 0:
                    break
                content.extend(part[:remaining])
        text = extract_search_text(
            bytes(content),
            role=candidate.role,
            media_type=candidate.media_type,
            filename=candidate.original_filename,
            record_type=item["record_type"],
        )
        return EmbeddingTextSource(candidate.id, candidate.content_hash, text) if text and text.strip() else None


def find_derived_artifact_reference(
    *,
    collection_item_id: uuid.UUID,
    artifact_role: str,
    derivation_key: str,
    actor_user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
) -> DerivedArtifactReference | None:
    settings = get_settings()
    if settings.artifact_mode == "embedded":
        with ArtifactSessionLocal() as db:
            artifact = find_derived_artifact(
                db,
                collection_item_id=collection_item_id,
                artifact_role=artifact_role,
                derivation_key=derivation_key,
            )
            if artifact is None:
                return None
            if artifact.tenant_id != tenant_id or artifact.client_id != client_id:
                raise PermissionError("Artifact is not available to this matter")
            return DerivedArtifactReference(
                artifact.id,
                artifact.content_hash,
                derivation_key,
                artifact.artifact_metadata or {},
            )

    with httpx.Client(base_url=settings.artifact_base_url, timeout=120) as client:
        response = client.get(
            f"/v1/collection-items/{collection_item_id}/artifacts",
            headers=_headers(actor_user_id, tenant_id, client_id),
        )
    response.raise_for_status()
    for artifact in response.json():
        if artifact.get("role") == artifact_role and artifact.get("derivation_key") == derivation_key:
            return DerivedArtifactReference(
                uuid.UUID(artifact["id"]),
                artifact["sha256"],
                derivation_key,
                artifact.get("metadata") or {},
            )
    return None


def read_artifact_bytes(
    *,
    artifact_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
) -> bytes:
    settings = get_settings()
    if settings.artifact_mode == "embedded":
        with ArtifactSessionLocal() as db:
            artifact = db.get(Artifact, artifact_id)
            if artifact is None or artifact.status != "FINALIZED":
                raise ValueError("Artifact not found")
            if artifact.tenant_id != tenant_id or artifact.client_id != client_id:
                raise PermissionError("Artifact is not available to this matter")
            blob = db.get(ContentBlob, artifact.content_blob_id)
            if blob is None:
                raise ValueError("Artifact content is unavailable")
            with get_storage().open(blob.bucket_name, blob.storage_key) as content:
                return content.read()

    with httpx.Client(base_url=settings.artifact_base_url, timeout=120) as client:
        response = client.get(
            f"/v1/artifacts/{artifact_id}/content",
            headers=_headers(actor_user_id, tenant_id, client_id),
        )
    response.raise_for_status()
    return response.content


def store_derived_artifact(
    *,
    collection_item_id: uuid.UUID,
    content: bytes,
    artifact_type: str,
    source_artifact_id: uuid.UUID,
    relationship: str,
    processing_run_id: uuid.UUID,
    derivation_key: str,
    artifact_metadata: dict[str, Any],
    actor_user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
) -> tuple[DerivedArtifactReference, bool]:
    filename = f"{collection_item_id}-{artifact_type.lower().replace('_', '-')}.parquet"
    settings = get_settings()
    if settings.artifact_mode == "embedded":
        with ArtifactSessionLocal() as db:
            item = db.get(CollectionItem, collection_item_id)
            if item is None:
                raise ValueError("Collection item not found")
            if item.tenant_id != tenant_id or item.client_id != client_id:
                raise PermissionError("Collection item is not available to this matter")
            artifact, created = persist_derived_artifact(
                db,
                get_storage(),
                item=item,
                content=content,
                media_type="application/vnd.apache.parquet",
                original_filename=filename,
                artifact_type=artifact_type,
                source_artifact_id=source_artifact_id,
                relationship=relationship,
                processing_run_id=processing_run_id,
                derivation_key=derivation_key,
                artifact_metadata=artifact_metadata,
                actor_user_id=actor_user_id,
            )
            db.commit()
            return (
                DerivedArtifactReference(
                    artifact.id,
                    artifact.content_hash,
                    derivation_key,
                    artifact.artifact_metadata or {},
                ),
                created,
            )

    payload = {
        "artifact_type": artifact_type,
        "source_artifact_id": str(source_artifact_id),
        "relationship": relationship,
        "processing_run_id": str(processing_run_id),
        "derivation_key": derivation_key,
        "metadata": artifact_metadata,
    }
    with httpx.Client(base_url=settings.artifact_base_url, timeout=120) as client:
        response = client.post(
            f"/v1/collection-items/{collection_item_id}/derived-artifacts:upload",
            headers=_headers(actor_user_id, tenant_id, client_id),
            data={"metadata": json.dumps(payload)},
            files={"file": (filename, content, "application/vnd.apache.parquet")},
        )
    response.raise_for_status()
    data = response.json()
    artifact = data["artifact"]
    return (
        DerivedArtifactReference(
            uuid.UUID(artifact["id"]),
            artifact["sha256"],
            artifact["derivation_key"],
            artifact.get("metadata") or {},
        ),
        bool(data["created"]),
    )


def load_current_chunk_artifacts(
    *,
    collection_item_id: uuid.UUID,
    configuration_hash: str,
    actor_user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
) -> tuple[bytes, bytes] | None:
    settings = get_settings()
    if settings.artifact_mode == "embedded":
        with ArtifactSessionLocal() as db:
            rows = db.execute(
                select(Artifact, CollectionItemArtifact)
                .join(CollectionItemArtifact, CollectionItemArtifact.artifact_id == Artifact.id)
                .where(
                    CollectionItemArtifact.collection_item_id == collection_item_id,
                    CollectionItemArtifact.artifact_role == "CHUNK_VECTOR_SET",
                    Artifact.status == "FINALIZED",
                )
                .order_by(Artifact.created_at.desc())
            ).all()
            vector = next(
                (artifact for artifact, _ in rows if (artifact.artifact_metadata or {}).get("configuration_hash") == configuration_hash),
                None,
            )
            if vector is None:
                return None
            chunk_id = uuid.UUID(vector.artifact_metadata["chunk_set_artifact_id"])
            chunk = db.get(Artifact, chunk_id)
            if chunk is None:
                raise ValueError("Chunk vector set references a missing chunk set")
            if vector.tenant_id != tenant_id or vector.client_id != client_id:
                raise PermissionError("Artifact is not available to this matter")
            contents = []
            for artifact in (chunk, vector):
                blob = db.get(ContentBlob, artifact.content_blob_id)
                if blob is None:
                    raise ValueError("Derived artifact content is unavailable")
                with get_storage().open(blob.bucket_name, blob.storage_key) as stream:
                    contents.append(stream.read())
            return contents[0], contents[1]

    headers = _headers(actor_user_id, tenant_id, client_id)
    with httpx.Client(base_url=settings.artifact_base_url, timeout=120) as client:
        response = client.get(f"/v1/collection-items/{collection_item_id}/artifacts", headers=headers)
        response.raise_for_status()
        vectors = [
            artifact
            for artifact in response.json()
            if artifact.get("role") == "CHUNK_VECTOR_SET"
            and (artifact.get("metadata") or {}).get("configuration_hash") == configuration_hash
        ]
        if not vectors:
            return None
        vector = vectors[0]
        chunk_id = vector["metadata"].get("chunk_set_artifact_id")
        if not chunk_id:
            raise ValueError("Chunk vector set does not identify its chunk set")
        chunk_response = client.get(f"/v1/artifacts/{chunk_id}/content", headers=headers)
        chunk_response.raise_for_status()
        vector_response = client.get(f"/v1/artifacts/{vector['id']}/content", headers=headers)
        vector_response.raise_for_status()
        return chunk_response.content, vector_response.content
