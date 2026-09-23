import json
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from artifact_service.auth import ArtifactPrincipal, require_client, require_tenant
from artifact_service.config import ArtifactSettings, get_artifact_settings
from artifact_service.database import get_artifact_db
from artifact_service.deletion import create_deletion_job, fail_deletion, retry_deletion_job
from artifact_service.derived import store_derived_artifact
from artifact_service.models import (
    Artifact,
    ArtifactLineage,
    ClientCollection,
    CollectionArtifact,
    CollectionDeletionJob,
    CollectionItem,
    CollectionItemArtifact,
    CollectionItemCustodian,
    CollectionItemEmail,
    CollectionItemEmailRecipient,
    CollectionSelection,
    CollectionTextProcessingProfile,
    CollectionTextProcessingRun,
    ContentBlob,
    TenantStorage,
)
from artifact_service.schemas import (
    ArtifactLineageRead,
    ArtifactRead,
    CollectionCreate,
    CollectionCustodianSummary,
    CollectionDateHistogramResponse,
    CollectionDeletionFailure,
    CollectionDeletionJobRead,
    CollectionFacetKey,
    CollectionItemRead,
    CollectionItemSearchResponse,
    CollectionItemUploadMetadata,
    CollectionItemUploadResponse,
    CollectionRead,
    CollectionSelectionBatch,
    CollectionSelectionBatchCustodian,
    CollectionSelectionBatchItem,
    CollectionSelectionCreate,
    CollectionSelectionRead,
    CollectionTextProcessingProfileRead,
    CollectionTextProcessingProfileUpdate,
    CollectionTextProcessingRunCreate,
    CollectionTextProcessingRunRead,
    CollectionTextProcessingTestItem,
    CollectionTextProcessingTestRequest,
    CollectionTextProcessingTestResponse,
    DateHistogramBucket,
    DateHistogramInterval,
    DerivedArtifactUploadMetadata,
    DerivedArtifactUploadResponse,
    EmailMetadataInput,
    EmailRecipientInput,
    FacetValue,
    ProcessingStatus,
    RecordType,
    SourceContainerUploadResponse,
    TenantStorageEnsure,
    TenantStorageRead,
    TextProcessingRule,
)
from artifact_service.search import collection_item_ids, normalize_extensions
from artifact_service.selections import (
    create_collection_selection,
    delete_collection_selection,
    get_collection_selection_batch,
    get_collection_selection_batch_custodians,
)
from artifact_service.service import (
    create_artifact,
    ensure_tenant_storage,
    get_or_create_blob,
    require_tenant_storage,
    stage_upload,
)
from artifact_service.storage import BlobStorage, get_storage, iter_file
from artifact_service.text_processing import (
    DEFAULT_RULES,
    MAX_TEST_TEXT_CHARS,
    PROCESSOR_VERSION,
    configuration_hash,
    load_source_text,
    process_text,
    validate_rules,
)


def _date_bucket_start(value: datetime, interval: DateHistogramInterval) -> datetime:
    value = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    value = value.replace(hour=0, minute=0, second=0, microsecond=0)
    if interval == "week":
        return value - timedelta(days=value.weekday())
    if interval == "month":
        return value.replace(day=1)
    return value.replace(month=1, day=1)


def _next_date_bucket(value: datetime, interval: DateHistogramInterval) -> datetime:
    if interval == "week":
        return value + timedelta(days=7)
    if interval == "month":
        return value.replace(
            year=value.year + (value.month == 12),
            month=1 if value.month == 12 else value.month + 1,
        )
    return value.replace(year=value.year + 1)


MAX_DATE_HISTOGRAM_BUCKETS = 2_000


def _fill_date_buckets(
    rows: list[tuple[datetime, int]], interval: DateHistogramInterval
) -> list[DateHistogramBucket]:
    counts = {_date_bucket_start(start, interval): count for start, count in rows}
    if not counts:
        return []
    current = min(counts)
    last = max(counts)
    buckets: list[DateHistogramBucket] = []
    while current <= last and len(buckets) < MAX_DATE_HISTOGRAM_BUCKETS:
        buckets.append(DateHistogramBucket(start=current, count=counts.get(current, 0)))
        current = _next_date_bucket(current, interval)
    if current <= last:
        return [DateHistogramBucket(start=start, count=count) for start, count in sorted(counts.items())]
    return buckets

PrincipalDependency = Callable[..., ArtifactPrincipal]


def _utc_timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_metadata(value: str) -> CollectionItemUploadMetadata:
    try:
        return CollectionItemUploadMetadata.model_validate_json(value)
    except (ValidationError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


def _parse_derived_metadata(value: str) -> DerivedArtifactUploadMetadata:
    try:
        return DerivedArtifactUploadMetadata.model_validate_json(value)
    except (ValidationError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


def _get_collection(
    db: Session,
    collection_id: uuid.UUID,
    principal: ArtifactPrincipal,
    *,
    for_update: bool = False,
) -> ClientCollection:
    statement = select(ClientCollection).where(ClientCollection.id == collection_id)
    if for_update:
        statement = statement.with_for_update()
    collection = db.scalar(statement)
    if collection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    require_client(principal, collection.tenant_id, collection.client_id)
    return collection


def _facet_values(rows, *, extension: bool = False) -> list[FacetValue]:
    values = [
        FacetValue(
            value=(
                "__none__"
                if raw_value is None
                else str(raw_value).lstrip(".")
                if extension
                else str(raw_value)
            ),
            count=count,
        )
        for raw_value, count in rows
    ]
    return sorted(values, key=lambda value: (-value.count, value.value))


def _artifact_scope(
    db: Session, artifact: Artifact
) -> tuple[str, uuid.UUID | None, uuid.UUID | None]:
    collection_link = db.get(CollectionArtifact, artifact.id)
    if collection_link is not None:
        return collection_link.artifact_role, collection_link.collection_id, None
    item_link = db.get(CollectionItemArtifact, artifact.id)
    if item_link is not None:
        item = db.get(CollectionItem, item_link.collection_item_id)
        if item is not None:
            return item_link.artifact_role, item.collection_id, item.id
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact subject not found")


def _artifact_read(db: Session, artifact: Artifact) -> ArtifactRead:
    role, collection_id, collection_item_id = _artifact_scope(db, artifact)
    item_link = db.get(CollectionItemArtifact, artifact.id) if collection_item_id is not None else None
    return ArtifactRead(
        id=artifact.id,
        tenant_id=artifact.tenant_id,
        client_id=artifact.client_id,
        artifact_class=artifact.artifact_class,
        artifact_type=artifact.artifact_type,
        role=role,
        original_filename=artifact.original_filename,
        media_type=artifact.media_type,
        byte_length=artifact.byte_length,
        sha256=artifact.content_hash,
        status=artifact.status,
        created_at=artifact.created_at,
        finalized_at=artifact.finalized_at,
        collection_id=collection_id,
        collection_item_id=collection_item_id,
        derivation_key=item_link.derivation_key if item_link else None,
        processing_run_id=item_link.processing_run_id if item_link else None,
        metadata=artifact.artifact_metadata or {},
    )


def _item_read(db: Session, item: CollectionItem) -> CollectionItemRead:
    custodians = list(
        db.scalars(
            select(CollectionItemCustodian.custodian_id)
            .where(CollectionItemCustodian.collection_item_id == item.id)
            .order_by(CollectionItemCustodian.relationship_type.desc(), CollectionItemCustodian.custodian_id)
        )
    )
    email_record = db.get(CollectionItemEmail, item.id)
    email: EmailMetadataInput | None = None
    if email_record is not None:
        recipients = list(
            db.scalars(
                select(CollectionItemEmailRecipient)
                .where(CollectionItemEmailRecipient.collection_item_id == item.id)
                .order_by(CollectionItemEmailRecipient.recipient_type, CollectionItemEmailRecipient.ordinal)
            )
        )
        email = EmailMetadataInput(
            sender=email_record.sender,
            subject=email_record.subject,
            sent_at=email_record.sent_at,
            received_at=email_record.received_at,
            message_id=email_record.message_id,
            recipients=[
                EmailRecipientInput(
                    recipient_type=recipient.recipient_type,
                    display_name=recipient.display_name,
                    email_address=recipient.email_address,
                )
                for recipient in recipients
            ],
        )
    native_link = db.scalar(
        select(CollectionItemArtifact).where(
            CollectionItemArtifact.collection_item_id == item.id,
            CollectionItemArtifact.artifact_role == "NATIVE",
        )
    )
    if native_link is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Collection item has no native artifact")
    native_artifact = db.get(Artifact, native_link.artifact_id)
    if native_artifact is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Native artifact is missing")
    return CollectionItemRead(
        id=item.id,
        tenant_id=item.tenant_id,
        client_id=item.client_id,
        collection_id=item.collection_id,
        source_item_id=item.source_item_id,
        record_type=item.record_type,
        original_filename=item.original_filename,
        original_extension=item.original_extension,
        original_source_path=item.original_source_path,
        source_created_at=item.source_created_at,
        source_modified_at=item.source_modified_at,
        file_date=_utc_timestamp(item.file_date),
        family_id=item.family_id,
        parent_collection_item_id=item.parent_collection_item_id,
        processing_status=item.processing_status,
        custodian_ids=custodians,
        email=email,
        raw_metadata=item.raw_metadata or {},
        unmapped_metadata=item.unmapped_metadata or {},
        native_artifact=_artifact_read(db, native_artifact),
        created_at=item.created_at,
    )


def build_router(
    principal_dependency: PrincipalDependency,
    *,
    expose_internal_deletion_control: bool = False,
) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["artifacts"])

    @router.post(
        "/tenants/{tenant_id}/artifact-storage/ensure",
        response_model=TenantStorageRead,
        status_code=status.HTTP_201_CREATED,
    )
    def ensure_storage(
        tenant_id: uuid.UUID,
        payload: TenantStorageEnsure,
        principal: ArtifactPrincipal = Depends(principal_dependency),
        db: Session = Depends(get_artifact_db),
        storage: BlobStorage = Depends(get_storage),
        settings: ArtifactSettings = Depends(get_artifact_settings),
    ) -> TenantStorage:
        require_tenant(principal, tenant_id)
        existing = db.get(TenantStorage, tenant_id)
        result = ensure_tenant_storage(db, storage, settings, tenant_id, payload.tenant_slug)
        db.commit()
        if existing is not None and existing.tenant_slug_snapshot != payload.tenant_slug:
            return existing
        return result

    @router.post(
        "/tenants/{tenant_id}/clients/{client_id}/collections",
        response_model=CollectionRead,
        status_code=status.HTTP_201_CREATED,
    )
    def create_collection(
        tenant_id: uuid.UUID,
        client_id: uuid.UUID,
        payload: CollectionCreate,
        principal: ArtifactPrincipal = Depends(principal_dependency),
        db: Session = Depends(get_artifact_db),
    ) -> ClientCollection:
        require_client(principal, tenant_id, client_id)
        require_tenant_storage(db, tenant_id)
        collection = ClientCollection(
            tenant_id=tenant_id,
            client_id=client_id,
            name=payload.name.strip(),
            description=payload.description,
            status="OPEN",
            created_by_user_id=principal.actor_user_id,
        )
        db.add(collection)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Collection name already exists") from exc
        return collection

    @router.get(
        "/tenants/{tenant_id}/clients/{client_id}/collections",
        response_model=list[CollectionRead],
    )
    def list_collections(
        tenant_id: uuid.UUID,
        client_id: uuid.UUID,
        principal: ArtifactPrincipal = Depends(principal_dependency),
        db: Session = Depends(get_artifact_db),
    ) -> list[ClientCollection]:
        require_client(principal, tenant_id, client_id)
        return list(
            db.scalars(
                select(ClientCollection)
                .where(ClientCollection.tenant_id == tenant_id, ClientCollection.client_id == client_id)
                .order_by(ClientCollection.name)
            )
        )

    @router.get("/collections/{collection_id}", response_model=CollectionRead)
    def get_collection(
        collection_id: uuid.UUID,
        principal: ArtifactPrincipal = Depends(principal_dependency),
        db: Session = Depends(get_artifact_db),
    ) -> ClientCollection:
        return _get_collection(db, collection_id, principal)

    @router.get(
        "/collections/{collection_id}/text-processing/profile",
        response_model=CollectionTextProcessingProfileRead,
    )
    def get_text_processing_profile(
        collection_id: uuid.UUID,
        principal: ArtifactPrincipal = Depends(principal_dependency),
        db: Session = Depends(get_artifact_db),
    ) -> CollectionTextProcessingProfileRead:
        collection = _get_collection(db, collection_id, principal)
        profile = db.get(CollectionTextProcessingProfile, collection.id)
        return CollectionTextProcessingProfileRead(
            collection_id=collection.id,
            processor_version=PROCESSOR_VERSION,
            revision=profile.revision if profile else 0,
            default_rules=DEFAULT_RULES,
            rules=profile.custom_rules if profile else [],
            active_run_id=collection.active_text_processing_run_id,
            updated_at=profile.updated_at if profile else None,
        )

    @router.put(
        "/collections/{collection_id}/text-processing/profile",
        response_model=CollectionTextProcessingProfileRead,
    )
    def update_text_processing_profile(
        collection_id: uuid.UUID,
        payload: CollectionTextProcessingProfileUpdate,
        principal: ArtifactPrincipal = Depends(principal_dependency),
        db: Session = Depends(get_artifact_db),
    ) -> CollectionTextProcessingProfileRead:
        collection = _get_collection(db, collection_id, principal, for_update=True)
        if collection.status == "DELETING":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Collection is being deleted")
        try:
            validate_rules(payload.rules)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        profile = db.get(CollectionTextProcessingProfile, collection.id)
        if profile is None:
            profile = CollectionTextProcessingProfile(
                collection_id=collection.id,
                revision=1,
                custom_rules=[rule.model_dump(mode="json") for rule in payload.rules],
                updated_by_user_id=principal.actor_user_id,
            )
            db.add(profile)
        else:
            profile.revision += 1
            profile.custom_rules = [rule.model_dump(mode="json") for rule in payload.rules]
            profile.updated_by_user_id = principal.actor_user_id
        db.commit()
        db.refresh(profile)
        return CollectionTextProcessingProfileRead(
            collection_id=collection.id,
            processor_version=PROCESSOR_VERSION,
            revision=profile.revision,
            default_rules=DEFAULT_RULES,
            rules=profile.custom_rules,
            active_run_id=collection.active_text_processing_run_id,
            updated_at=profile.updated_at,
        )

    @router.post(
        "/collections/{collection_id}/text-processing:test",
        response_model=CollectionTextProcessingTestResponse,
    )
    def test_text_processing(
        collection_id: uuid.UUID,
        payload: CollectionTextProcessingTestRequest,
        principal: ArtifactPrincipal = Depends(principal_dependency),
        db: Session = Depends(get_artifact_db),
        storage: BlobStorage = Depends(get_storage),
    ) -> CollectionTextProcessingTestResponse:
        collection = _get_collection(db, collection_id, principal)
        try:
            validate_rules(payload.rules)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        known_rule_ids = {rule["id"] for rule in DEFAULT_RULES} | {rule.id for rule in payload.rules}
        unknown_disabled_rule_ids = set(payload.disabled_rule_ids) - known_rule_ids
        if unknown_disabled_rule_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unknown disabled rule ID: {min(unknown_disabled_rule_ids)}",
            )
        items = list(
            db.scalars(
                select(CollectionItem).where(
                    CollectionItem.collection_id == collection.id,
                    CollectionItem.id.in_(payload.item_ids),
                )
            )
        )
        by_id = {item.id: item for item in items}
        if len(by_id) != len(set(payload.item_ids)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="One or more collection items were not found")
        results: list[CollectionTextProcessingTestItem] = []
        for item_id in payload.item_ids:
            item = by_id[item_id]
            source = load_source_text(db, storage, item)
            if source is None:
                results.append(
                    CollectionTextProcessingTestItem(
                        item_id=item.id,
                        filename=item.original_filename,
                        source_role=None,
                        original_text=None,
                        normalized_text=None,
                        original_char_count=0,
                        normalized_char_count=0,
                        changes=[],
                        warnings=["No supported source text is available for this item."],
                    )
                )
                continue
            original = source.text[:MAX_TEST_TEXT_CHARS]
            result = process_text(original, payload.rules, disabled_rule_ids=set(payload.disabled_rule_ids))
            warnings = list(result.warnings)
            if len(source.text) > MAX_TEST_TEXT_CHARS:
                warnings.append("The test preview is limited to the first 100,000 characters.")
            results.append(
                CollectionTextProcessingTestItem(
                    item_id=item.id,
                    filename=item.original_filename,
                    source_role=source.role,
                    original_text=original,
                    normalized_text=result.text,
                    original_char_count=len(original),
                    normalized_char_count=len(result.text),
                    changes=result.changes,
                    warnings=warnings,
                )
            )
        return CollectionTextProcessingTestResponse(processor_version=PROCESSOR_VERSION, items=results)

    @router.post(
        "/collections/{collection_id}/text-processing/runs",
        response_model=CollectionTextProcessingRunRead,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_text_processing_run(
        collection_id: uuid.UUID,
        payload: CollectionTextProcessingRunCreate | None = None,
        principal: ArtifactPrincipal = Depends(principal_dependency),
        db: Session = Depends(get_artifact_db),
    ) -> CollectionTextProcessingRun:
        collection = _get_collection(db, collection_id, principal, for_update=True)
        if collection.status != "OPEN":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Collection is not open")
        active = db.scalar(
            select(CollectionTextProcessingRun).where(
                CollectionTextProcessingRun.collection_id == collection.id,
                CollectionTextProcessingRun.status.in_(("QUEUED", "RUNNING")),
            )
        )
        if active is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A text processing run is already active")
        profile = db.get(CollectionTextProcessingProfile, collection.id)
        rules = profile.custom_rules if profile else []
        parsed_rules = [TextProcessingRule.model_validate(value) for value in rules]
        selectable_custom_rules = [rule for rule in parsed_rules if rule.enabled]
        selectable_rule_ids = {rule["id"] for rule in DEFAULT_RULES} | {rule.id for rule in selectable_custom_rules}
        enabled_rule_ids = selectable_rule_ids if payload is None or payload.enabled_rule_ids is None else set(payload.enabled_rule_ids)
        unknown_rule_ids = enabled_rule_ids - selectable_rule_ids
        if unknown_rule_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unknown or disabled enabled rule ID: {min(unknown_rule_ids)}",
            )
        selected_rules = [rule for rule in selectable_custom_rules if rule.id in enabled_rule_ids]
        disabled_rule_ids = selectable_rule_ids - enabled_rule_ids
        run = CollectionTextProcessingRun(
            collection_id=collection.id,
            status="QUEUED",
            processor_version=PROCESSOR_VERSION,
            profile_revision=profile.revision if profile else 0,
            rules_snapshot=[rule.model_dump(mode="json") for rule in selected_rules],
            disabled_rule_ids=sorted(disabled_rule_ids),
            configuration_hash=configuration_hash(selected_rules, disabled_rule_ids=disabled_rule_ids),
            requested_by_user_id=principal.actor_user_id,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        from app.workflows.dispatcher import enqueue_collection_text_processing

        try:
            enqueue_collection_text_processing(str(run.id))
        except Exception as exc:
            run.status = "FAILED"
            run.error_message = f"Unable to enqueue processing: {exc}"[:4000]
            db.commit()
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=run.error_message) from exc
        return run

    @router.get(
        "/collections/{collection_id}/text-processing/runs",
        response_model=list[CollectionTextProcessingRunRead],
    )
    def list_text_processing_runs(
        collection_id: uuid.UUID,
        principal: ArtifactPrincipal = Depends(principal_dependency),
        db: Session = Depends(get_artifact_db),
    ) -> list[CollectionTextProcessingRun]:
        collection = _get_collection(db, collection_id, principal)
        return list(
            db.scalars(
                select(CollectionTextProcessingRun)
                .where(CollectionTextProcessingRun.collection_id == collection.id)
                .order_by(CollectionTextProcessingRun.created_at.desc())
                .limit(25)
            )
        )

    @router.post(
        "/collections/{collection_id}/selections",
        response_model=CollectionSelectionRead,
        status_code=status.HTTP_201_CREATED,
    )
    def create_selection(
        collection_id: uuid.UUID,
        payload: CollectionSelectionCreate,
        principal: ArtifactPrincipal = Depends(principal_dependency),
        db: Session = Depends(get_artifact_db),
    ) -> CollectionSelection:
        collection = _get_collection(db, collection_id, principal, for_update=True)
        if collection.status == "DELETING":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Collection is being deleted")
        try:
            selection = create_collection_selection(db, collection, payload)
            db.commit()
        except ValueError as exc:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return selection

    @router.get(
        "/collection-selections/{selection_id}/items",
        response_model=CollectionSelectionBatch,
    )
    def get_selection_items(
        selection_id: uuid.UUID,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
        principal: ArtifactPrincipal = Depends(principal_dependency),
        db: Session = Depends(get_artifact_db),
    ) -> CollectionSelectionBatch:
        selection = db.get(CollectionSelection, selection_id)
        if selection is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection selection not found")
        require_client(principal, selection.tenant_id, selection.client_id)
        item_ids = get_collection_selection_batch(db, selection, offset=offset, limit=limit)
        custodians = get_collection_selection_batch_custodians(db, item_ids)
        return CollectionSelectionBatch(
            selection_id=selection.id,
            offset=offset,
            total_count=selection.total_count,
            item_ids=item_ids,
            items=[
                CollectionSelectionBatchItem(
                    item_id=item_id,
                    custodian_ids=[custodian_id for custodian_id, _ in custodians[item_id]],
                    custodians=[
                        CollectionSelectionBatchCustodian(
                            custodian_id=custodian_id,
                            relationship_type=relationship_type,
                        )
                        for custodian_id, relationship_type in custodians[item_id]
                    ],
                )
                for item_id in item_ids
            ],
        )

    @router.delete(
        "/collection-selections/{selection_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def delete_selection(
        selection_id: uuid.UUID,
        principal: ArtifactPrincipal = Depends(principal_dependency),
        db: Session = Depends(get_artifact_db),
    ) -> Response:
        selection = db.get(CollectionSelection, selection_id)
        if selection is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        require_client(principal, selection.tenant_id, selection.client_id)
        delete_collection_selection(db, selection)
        db.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get(
        "/collections/{collection_id}/custodians",
        response_model=list[CollectionCustodianSummary],
    )
    def list_collection_custodians(
        collection_id: uuid.UUID,
        principal: ArtifactPrincipal = Depends(principal_dependency),
        db: Session = Depends(get_artifact_db),
    ) -> list[CollectionCustodianSummary]:
        collection = _get_collection(db, collection_id, principal)
        rows = db.execute(
            select(
                CollectionItemCustodian.custodian_id,
                func.count(CollectionItemCustodian.collection_item_id).label("item_count"),
            )
            .join(
                CollectionItem,
                CollectionItem.id == CollectionItemCustodian.collection_item_id,
            )
            .where(CollectionItem.collection_id == collection.id)
            .group_by(CollectionItemCustodian.custodian_id)
            .order_by(CollectionItemCustodian.custodian_id)
        ).all()
        return [
            CollectionCustodianSummary(custodian_id=custodian_id, item_count=item_count)
            for custodian_id, item_count in rows
        ]

    @router.post(
        "/collections/{collection_id}/source-containers:upload",
        response_model=SourceContainerUploadResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def upload_source_container(
        collection_id: uuid.UUID,
        file: Annotated[UploadFile, File()],
        original_source_path: Annotated[str | None, Form()] = None,
        principal: ArtifactPrincipal = Depends(principal_dependency),
        db: Session = Depends(get_artifact_db),
        storage: BlobStorage = Depends(get_storage),
    ) -> SourceContainerUploadResponse:
        collection = _get_collection(db, collection_id, principal, for_update=True)
        if collection.status != "OPEN":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Collection is not open")
        tenant_storage = require_tenant_storage(db, collection.tenant_id)
        staged, sha256, byte_length = stage_upload(file)
        media_type = file.content_type or "application/octet-stream"
        try:
            existing = db.scalar(
                select(Artifact)
                .join(CollectionArtifact, CollectionArtifact.artifact_id == Artifact.id)
                .where(
                    CollectionArtifact.collection_id == collection.id,
                    Artifact.artifact_type == "SOURCE_CONTAINER",
                    Artifact.original_filename == Path(file.filename or "source-container").name,
                    Artifact.content_hash == sha256,
                )
            )
            if existing is not None:
                return SourceContainerUploadResponse(artifact=_artifact_read(db, existing))
            blob = get_or_create_blob(
                db,
                storage,
                tenant_storage,
                collection.tenant_id,
                staged,
                sha256,
                byte_length,
                media_type,
            )
            artifact = create_artifact(
                db,
                tenant_id=collection.tenant_id,
                client_id=collection.client_id,
                blob=blob,
                artifact_type="SOURCE_CONTAINER",
                original_filename=file.filename or "source-container",
                actor_user_id=principal.actor_user_id,
                source_reference=original_source_path,
            )
            db.add(CollectionArtifact(artifact_id=artifact.id, collection_id=collection.id))
            db.commit()
            return SourceContainerUploadResponse(artifact=_artifact_read(db, artifact))
        finally:
            staged.close()

    @router.post(
        "/collections/{collection_id}/items:upload",
        response_model=CollectionItemUploadResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def upload_collection_item(
        collection_id: uuid.UUID,
        metadata: Annotated[str, Form()],
        file: Annotated[UploadFile, File()],
        principal: ArtifactPrincipal = Depends(principal_dependency),
        db: Session = Depends(get_artifact_db),
        storage: BlobStorage = Depends(get_storage),
    ) -> CollectionItemUploadResponse:
        payload = _parse_metadata(metadata)
        collection = _get_collection(db, collection_id, principal, for_update=True)
        if collection.status != "OPEN":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Collection is not open")
        invalid_custodians = [
            custodian_id
            for custodian_id in payload.custodian_ids
            if not principal.can_reference_custodian(collection.client_id, custodian_id)
        ]
        if invalid_custodians:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid custodian scope")
        parent: CollectionItem | None = None
        if payload.parent_collection_item_id is not None:
            parent = db.get(CollectionItem, payload.parent_collection_item_id)
            if parent is None or parent.collection_id != collection.id:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid parent item")
        if payload.source_container_artifact_id is not None:
            source_container = db.scalar(
                select(Artifact)
                .join(CollectionArtifact, CollectionArtifact.artifact_id == Artifact.id)
                .where(
                    Artifact.id == payload.source_container_artifact_id,
                    Artifact.status == "FINALIZED",
                    CollectionArtifact.collection_id == collection.id,
                )
            )
            if source_container is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Invalid source container artifact",
                )
        tenant_storage = require_tenant_storage(db, collection.tenant_id)
        staged, sha256, byte_length = stage_upload(file)
        media_type = file.content_type or "application/octet-stream"
        try:
            existing_item = db.scalar(
                select(CollectionItem).where(
                    CollectionItem.collection_id == collection.id,
                    CollectionItem.source_item_id == payload.source_item_id,
                )
            )
            if existing_item is not None:
                existing_read = _item_read(db, existing_item)
                if existing_read.native_artifact.sha256 != sha256:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="source_item_id already exists with different content",
                    )
                return CollectionItemUploadResponse(item=existing_read, created=False)

            item = CollectionItem(
                tenant_id=collection.tenant_id,
                client_id=collection.client_id,
                collection_id=collection.id,
                source_item_id=payload.source_item_id,
                record_type=payload.record_type,
                original_filename=payload.original_filename,
                original_extension=Path(payload.original_filename).suffix.lower() or None,
                original_source_path=payload.original_source_path,
                source_created_at=payload.source_created_at,
                source_modified_at=payload.source_modified_at,
                file_date=_utc_timestamp(
                    payload.email.sent_at
                    if payload.email is not None
                    else parent.file_date
                    if parent is not None
                    else payload.source_modified_at
                ),
                family_id=payload.family_id,
                parent_collection_item_id=payload.parent_collection_item_id,
                processing_status=payload.processing_status,
                raw_metadata=payload.raw_metadata,
                unmapped_metadata=payload.unmapped_metadata,
            )
            db.add(item)
            db.flush()
            primary_custodian = payload.primary_custodian_id or payload.custodian_ids[0]
            db.add_all(
                [
                    CollectionItemCustodian(
                        collection_item_id=item.id,
                        custodian_id=custodian_id,
                        relationship_type="PRIMARY" if custodian_id == primary_custodian else "COMMON",
                    )
                    for custodian_id in payload.custodian_ids
                ]
            )
            if payload.email is not None:
                db.add(
                    CollectionItemEmail(
                        collection_item_id=item.id,
                        sender=payload.email.sender,
                        subject=payload.email.subject,
                        sent_at=payload.email.sent_at,
                        received_at=payload.email.received_at,
                        message_id=payload.email.message_id,
                    )
                )
                recipient_ordinals: dict[str, int] = {"TO": 0, "CC": 0, "BCC": 0}
                for recipient in payload.email.recipients:
                    ordinal = recipient_ordinals[recipient.recipient_type]
                    recipient_ordinals[recipient.recipient_type] += 1
                    db.add(
                        CollectionItemEmailRecipient(
                            collection_item_id=item.id,
                            recipient_type=recipient.recipient_type,
                            display_name=recipient.display_name,
                            email_address=recipient.email_address,
                            ordinal=ordinal,
                        )
                    )
            blob = get_or_create_blob(
                db,
                storage,
                tenant_storage,
                collection.tenant_id,
                staged,
                sha256,
                byte_length,
                media_type,
            )
            artifact = create_artifact(
                db,
                tenant_id=collection.tenant_id,
                client_id=collection.client_id,
                blob=blob,
                artifact_type="NATIVE_FILE",
                original_filename=payload.original_filename,
                actor_user_id=principal.actor_user_id,
                source_reference=payload.original_source_path,
            )
            db.add(CollectionItemArtifact(artifact_id=artifact.id, collection_item_id=item.id, artifact_role="NATIVE"))
            if payload.source_container_artifact_id is not None:
                db.add(
                    ArtifactLineage(
                        artifact_id=artifact.id,
                        source_artifact_id=payload.source_container_artifact_id,
                        relationship="EXTRACTED_FROM_CONTAINER",
                    )
                )
            db.commit()
            return CollectionItemUploadResponse(item=_item_read(db, item), created=True)
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Collection item already exists") from exc
        finally:
            staged.close()

    @router.get(
        "/collections/{collection_id}/search",
        response_model=CollectionItemSearchResponse,
    )
    def search_collection_items(
        collection_id: uuid.UUID,
        q: Annotated[str | None, Query(max_length=500)] = None,
        custodian_id: Annotated[list[uuid.UUID] | None, Query()] = None,
        extension: Annotated[list[str] | None, Query()] = None,
        record_type: Annotated[list[RecordType] | None, Query()] = None,
        processing_status: Annotated[list[ProcessingStatus] | None, Query()] = None,
        source_created_from: datetime | None = None,
        source_created_to: datetime | None = None,
        file_date_from: datetime | None = None,
        file_date_to: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        principal: ArtifactPrincipal = Depends(principal_dependency),
        db: Session = Depends(get_artifact_db),
    ) -> CollectionItemSearchResponse:
        collection = _get_collection(db, collection_id, principal)
        search = q.strip() if q and q.strip() else None
        custodian_ids = list(dict.fromkeys(custodian_id or []))
        extensions = normalize_extensions(extension or [])
        record_types = list(dict.fromkeys(record_type or []))
        processing_statuses = list(dict.fromkeys(processing_status or []))
        filters = {
            "collection_id": collection.id,
            "search": search,
            "custodian_ids": custodian_ids,
            "extensions": extensions,
            "record_types": record_types,
            "processing_statuses": processing_statuses,
            "source_created_from": source_created_from,
            "source_created_to": source_created_to,
            "file_date_from": file_date_from,
            "file_date_to": file_date_to,
        }

        matched_ids = collection_item_ids(**filters)
        total = db.scalar(select(func.count()).select_from(matched_ids.subquery())) or 0
        items = list(
            db.scalars(
                select(CollectionItem)
                .where(CollectionItem.id.in_(matched_ids))
                .order_by(CollectionItem.created_at, CollectionItem.id)
                .offset(offset)
                .limit(limit)
            )
        )

        return CollectionItemSearchResponse(
            items=[_item_read(db, item) for item in items],
            total=total,
            limit=limit,
            offset=offset,
        )

    @router.get(
        "/collections/{collection_id}/search/facets/{facet}",
        response_model=list[FacetValue],
    )
    def collection_search_facet(
        collection_id: uuid.UUID,
        facet: CollectionFacetKey,
        q: Annotated[str | None, Query(max_length=500)] = None,
        custodian_id: Annotated[list[uuid.UUID] | None, Query()] = None,
        extension: Annotated[list[str] | None, Query()] = None,
        record_type: Annotated[list[RecordType] | None, Query()] = None,
        processing_status: Annotated[list[ProcessingStatus] | None, Query()] = None,
        source_created_from: datetime | None = None,
        source_created_to: datetime | None = None,
        file_date_from: datetime | None = None,
        file_date_to: datetime | None = None,
        principal: ArtifactPrincipal = Depends(principal_dependency),
        db: Session = Depends(get_artifact_db),
    ) -> list[FacetValue]:
        collection = _get_collection(db, collection_id, principal)
        filters = {
            "collection_id": collection.id,
            "search": q.strip() if q and q.strip() else None,
            "custodian_ids": list(dict.fromkeys(custodian_id or [])),
            "extensions": normalize_extensions(extension or []),
            "record_types": list(dict.fromkeys(record_type or [])),
            "processing_statuses": list(dict.fromkeys(processing_status or [])),
            "source_created_from": source_created_from,
            "source_created_to": source_created_to,
            "file_date_from": file_date_from,
            "file_date_to": file_date_to,
        }
        scope = collection_item_ids(**filters, exclude_facet=facet)
        if facet == "custodians":
            rows = db.execute(
                select(
                    CollectionItemCustodian.custodian_id,
                    func.count(func.distinct(CollectionItem.id)),
                )
                .join(CollectionItem, CollectionItem.id == CollectionItemCustodian.collection_item_id)
                .where(CollectionItem.id.in_(scope))
                .group_by(CollectionItemCustodian.custodian_id)
            ).all()
            return _facet_values(rows)
        column = {
            "file_extensions": CollectionItem.original_extension,
            "record_types": CollectionItem.record_type,
            "processing_statuses": CollectionItem.processing_status,
        }[facet]
        rows = db.execute(
            select(column, func.count(CollectionItem.id))
            .where(CollectionItem.id.in_(scope))
            .group_by(column)
        ).all()
        return _facet_values(rows, extension=facet == "file_extensions")

    @router.get(
        "/collections/{collection_id}/date-histogram",
        response_model=CollectionDateHistogramResponse,
    )
    def collection_date_histogram(
        collection_id: uuid.UUID,
        interval: Annotated[DateHistogramInterval, Query()] = "month",
        principal: ArtifactPrincipal = Depends(principal_dependency),
        db: Session = Depends(get_artifact_db),
    ) -> CollectionDateHistogramResponse:
        collection = _get_collection(db, collection_id, principal)
        dated = (
            CollectionItem.collection_id == collection.id,
            CollectionItem.file_date.is_not(None),
        )
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            bucket = func.date_trunc(interval, func.timezone("UTC", CollectionItem.file_date)).label("bucket")
            rows = [
                (start, int(count))
                for start, count in db.execute(
                    select(bucket, func.count(CollectionItem.id))
                    .where(*dated)
                    .group_by(bucket)
                    .order_by(bucket)
                ).all()
            ]
        else:
            counts: dict[datetime, int] = {}
            for value in db.scalars(select(CollectionItem.file_date).where(*dated)):
                start = _date_bucket_start(value, interval)
                counts[start] = counts.get(start, 0) + 1
            rows = sorted(counts.items())
        missing_count = db.scalar(
            select(func.count(CollectionItem.id)).where(
                CollectionItem.collection_id == collection.id,
                CollectionItem.file_date.is_(None),
            )
        ) or 0
        return CollectionDateHistogramResponse(
            interval=interval,
            buckets=_fill_date_buckets(rows, interval),
            missing_count=missing_count,
        )

    @router.get("/collections/{collection_id}/items", response_model=list[CollectionItemRead])
    def list_collection_items(
        collection_id: uuid.UUID,
        custodian_id: uuid.UUID | None = None,
        record_type: str | None = None,
        filename: str | None = None,
        source_created_from: datetime | None = None,
        source_created_to: datetime | None = None,
        file_date_from: datetime | None = None,
        file_date_to: datetime | None = None,
        sha256: Annotated[str | None, Query(pattern=r"^[0-9a-fA-F]{64}$")] = None,
        min_size: Annotated[int | None, Query(ge=0)] = None,
        max_size: Annotated[int | None, Query(ge=0)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        principal: ArtifactPrincipal = Depends(principal_dependency),
        db: Session = Depends(get_artifact_db),
    ) -> list[CollectionItemRead]:
        collection = _get_collection(db, collection_id, principal)
        query = select(CollectionItem).where(CollectionItem.collection_id == collection.id)
        if custodian_id is not None:
            query = query.join(CollectionItemCustodian).where(CollectionItemCustodian.custodian_id == custodian_id)
        if record_type is not None:
            query = query.where(CollectionItem.record_type == record_type.upper())
        if filename is not None:
            query = query.where(CollectionItem.original_filename.ilike(f"%{filename}%"))
        if source_created_from is not None:
            query = query.where(CollectionItem.source_created_at >= source_created_from)
        if source_created_to is not None:
            query = query.where(CollectionItem.source_created_at <= source_created_to)
        if file_date_from is not None:
            query = query.where(CollectionItem.file_date >= file_date_from)
        if file_date_to is not None:
            query = query.where(CollectionItem.file_date <= file_date_to)
        if sha256 is not None or min_size is not None or max_size is not None:
            query = (
                query.join(CollectionItemArtifact)
                .join(Artifact, Artifact.id == CollectionItemArtifact.artifact_id)
                .where(CollectionItemArtifact.artifact_role == "NATIVE")
            )
            if sha256 is not None:
                query = query.where(Artifact.content_hash == sha256.lower())
            if min_size is not None:
                query = query.where(Artifact.byte_length >= min_size)
            if max_size is not None:
                query = query.where(Artifact.byte_length <= max_size)
        items = list(db.scalars(query.order_by(CollectionItem.created_at, CollectionItem.id).offset(offset).limit(limit)))
        return [_item_read(db, item) for item in items]

    @router.get("/collection-items/{collection_item_id}", response_model=CollectionItemRead)
    def get_collection_item(
        collection_item_id: uuid.UUID,
        principal: ArtifactPrincipal = Depends(principal_dependency),
        db: Session = Depends(get_artifact_db),
    ) -> CollectionItemRead:
        item = db.get(CollectionItem, collection_item_id)
        if item is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection item not found")
        require_client(principal, item.tenant_id, item.client_id)
        return _item_read(db, item)

    @router.get("/collection-items/{collection_item_id}/artifacts", response_model=list[ArtifactRead])
    def list_collection_item_artifacts(
        collection_item_id: uuid.UUID,
        principal: ArtifactPrincipal = Depends(principal_dependency),
        db: Session = Depends(get_artifact_db),
    ) -> list[ArtifactRead]:
        item = db.get(CollectionItem, collection_item_id)
        if item is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection item not found")
        require_client(principal, item.tenant_id, item.client_id)
        role_order = case(
            (CollectionItemArtifact.artifact_role == "EXTRACTED_TEXT", 0),
            (CollectionItemArtifact.artifact_role == "OCR_TEXT", 1),
            (CollectionItemArtifact.artifact_role == "NATIVE", 2),
            else_=3,
        )
        artifacts = list(
            db.scalars(
                select(Artifact)
                .join(CollectionItemArtifact, CollectionItemArtifact.artifact_id == Artifact.id)
                .where(CollectionItemArtifact.collection_item_id == item.id)
                .order_by(role_order, Artifact.created_at.desc())
            )
        )
        return [_artifact_read(db, artifact) for artifact in artifacts]

    @router.post(
        "/collection-items/{collection_item_id}/derived-artifacts:upload",
        response_model=DerivedArtifactUploadResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def upload_derived_artifact(
        collection_item_id: uuid.UUID,
        metadata: Annotated[str, Form()],
        file: Annotated[UploadFile, File()],
        principal: ArtifactPrincipal = Depends(principal_dependency),
        db: Session = Depends(get_artifact_db),
        storage: BlobStorage = Depends(get_storage),
    ) -> DerivedArtifactUploadResponse:
        payload = _parse_derived_metadata(metadata)
        item = db.get(CollectionItem, collection_item_id)
        if item is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection item not found")
        require_client(principal, item.tenant_id, item.client_id)
        collection = _get_collection(db, item.collection_id, principal, for_update=True)
        if collection.status == "DELETING":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Collection is being deleted")
        content = file.file.read()
        try:
            artifact, created = store_derived_artifact(
                db,
                storage,
                item=item,
                content=content,
                media_type=file.content_type or "application/octet-stream",
                original_filename=file.filename or f"{payload.artifact_type.lower()}.parquet",
                artifact_type=payload.artifact_type,
                source_artifact_id=payload.source_artifact_id,
                relationship=payload.relationship,
                processing_run_id=payload.processing_run_id,
                derivation_key=payload.derivation_key,
                artifact_metadata=payload.metadata,
                actor_user_id=principal.actor_user_id,
            )
            db.commit()
        except ValueError as exc:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        return DerivedArtifactUploadResponse(artifact=_artifact_read(db, artifact), created=created)

    @router.get("/artifacts/{artifact_id}", response_model=ArtifactRead)
    def get_artifact(
        artifact_id: uuid.UUID,
        principal: ArtifactPrincipal = Depends(principal_dependency),
        db: Session = Depends(get_artifact_db),
    ) -> ArtifactRead:
        artifact = db.get(Artifact, artifact_id)
        if artifact is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
        require_client(principal, artifact.tenant_id, artifact.client_id)
        return _artifact_read(db, artifact)

    @router.get("/artifacts/{artifact_id}/lineage", response_model=list[ArtifactLineageRead])
    def get_artifact_lineage(
        artifact_id: uuid.UUID,
        principal: ArtifactPrincipal = Depends(principal_dependency),
        db: Session = Depends(get_artifact_db),
    ) -> list[ArtifactLineage]:
        artifact = db.get(Artifact, artifact_id)
        if artifact is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
        require_client(principal, artifact.tenant_id, artifact.client_id)
        return list(
            db.scalars(
                select(ArtifactLineage)
                .where(ArtifactLineage.artifact_id == artifact_id)
                .order_by(ArtifactLineage.created_at, ArtifactLineage.source_artifact_id)
            )
        )

    @router.get("/artifacts/{artifact_id}/content")
    def get_artifact_content(
        artifact_id: uuid.UUID,
        principal: ArtifactPrincipal = Depends(principal_dependency),
        db: Session = Depends(get_artifact_db),
        storage: BlobStorage = Depends(get_storage),
    ) -> StreamingResponse:
        artifact = db.get(Artifact, artifact_id)
        if artifact is None or artifact.status != "FINALIZED":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
        require_client(principal, artifact.tenant_id, artifact.client_id)
        blob = db.get(ContentBlob, artifact.content_blob_id)
        if blob is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Artifact content is unavailable")
        try:
            content = storage.open(blob.bucket_name, blob.storage_key)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Artifact content is unavailable") from exc
        headers = {
            "Content-Length": str(blob.byte_length),
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(artifact.original_filename)}",
            "ETag": artifact.content_hash,
        }
        return StreamingResponse(iter_file(content), media_type=artifact.media_type, headers=headers)

    if expose_internal_deletion_control:

        @router.post(
            "/internal/collections/{collection_id}/deletions",
            response_model=CollectionDeletionJobRead,
            status_code=status.HTTP_202_ACCEPTED,
            include_in_schema=False,
        )
        def create_internal_collection_deletion(
            collection_id: uuid.UUID,
            principal: ArtifactPrincipal = Depends(principal_dependency),
            db: Session = Depends(get_artifact_db),
        ) -> CollectionDeletionJob:
            collection = _get_collection(db, collection_id, principal)
            try:
                job = create_deletion_job(db, collection.id, principal.actor_user_id)
                db.commit()
            except ValueError as exc:
                db.rollback()
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            return job

        @router.get(
            "/internal/collections/{collection_id}/deletions/latest",
            response_model=CollectionDeletionJobRead,
            include_in_schema=False,
        )
        def get_latest_internal_collection_deletion(
            collection_id: uuid.UUID,
            principal: ArtifactPrincipal = Depends(principal_dependency),
            db: Session = Depends(get_artifact_db),
        ) -> CollectionDeletionJob:
            collection = db.get(ClientCollection, collection_id)
            job = db.scalar(
                select(CollectionDeletionJob)
                .where(CollectionDeletionJob.collection_id == collection_id)
                .order_by(CollectionDeletionJob.created_at.desc())
            )
            if collection is not None:
                require_client(principal, collection.tenant_id, collection.client_id)
            elif job is not None:
                require_client(principal, job.tenant_id, job.client_id)
            if job is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Collection deletion job not found",
                )
            return job

        @router.get(
            "/internal/collection-deletions/{job_id}",
            response_model=CollectionDeletionJobRead,
            include_in_schema=False,
        )
        def get_internal_collection_deletion(
            job_id: uuid.UUID,
            principal: ArtifactPrincipal = Depends(principal_dependency),
            db: Session = Depends(get_artifact_db),
        ) -> CollectionDeletionJob:
            job = db.get(CollectionDeletionJob, job_id)
            if job is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Collection deletion job not found",
                )
            require_client(principal, job.tenant_id, job.client_id)
            return job

        @router.post(
            "/internal/collection-deletions/{job_id}/retry",
            response_model=CollectionDeletionJobRead,
            status_code=status.HTTP_202_ACCEPTED,
            include_in_schema=False,
        )
        def retry_internal_collection_deletion(
            job_id: uuid.UUID,
            principal: ArtifactPrincipal = Depends(principal_dependency),
            db: Session = Depends(get_artifact_db),
        ) -> CollectionDeletionJob:
            current = db.get(CollectionDeletionJob, job_id)
            if current is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Collection deletion job not found",
                )
            require_client(principal, current.tenant_id, current.client_id)
            try:
                job = retry_deletion_job(db, job_id)
                db.commit()
            except ValueError as exc:
                db.rollback()
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            return job

        @router.post(
            "/internal/collection-deletions/{job_id}/failure",
            response_model=CollectionDeletionJobRead,
            include_in_schema=False,
        )
        def fail_internal_collection_deletion(
            job_id: uuid.UUID,
            payload: CollectionDeletionFailure,
            principal: ArtifactPrincipal = Depends(principal_dependency),
            db: Session = Depends(get_artifact_db),
        ) -> CollectionDeletionJob:
            current = db.get(CollectionDeletionJob, job_id)
            if current is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Collection deletion job not found",
                )
            require_client(principal, current.tenant_id, current.client_id)
            job = fail_deletion(db, job_id, payload.message)
            if job is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Collection deletion job not found",
                )
            return job

    return router
