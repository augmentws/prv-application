import logging
import uuid
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from httpx import HTTPError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.artifact_gateway import get_workflow_artifact_reference, iter_workflow_artifact_content
from app.audit import record_audit
from app.authorization import ResourceRef, can
from app.config import get_settings
from app.database import get_db
from app.dependencies import Principal, get_principal
from app.fact_exports import (
    FactExportConflict,
    FactExportTooLarge,
    as_utc,
    create_fact_export,
    record_fact_export_failure,
)
from app.fact_queries import FactFilterError
from app.models import Matter, MatterFactExport
from app.schemas import MatterFactExportCreate, MatterFactExportRead
from classification_service.schemas import ArtifactType

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/matters/{matter_id}/fact-exports", tags=["matter fact exports"])
EXPORT_PERMISSIONS = ("fact.export", "fact.view", "document.view")


def _export_matter(db: Session, matter_id: uuid.UUID, principal: Principal) -> Matter:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    for permission_key in EXPORT_PERMISSIONS:
        if not can(db, principal, permission_key, ResourceRef.matter(matter.id)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission required: {permission_key}",
            )
    return matter


def _export_or_404(db: Session, matter_id: uuid.UUID, export_id: uuid.UUID) -> MatterFactExport:
    export = db.scalar(
        select(MatterFactExport).where(
            MatterFactExport.id == export_id,
            MatterFactExport.matter_id == matter_id,
        )
    )
    if export is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fact export not found")
    return export


@router.post("", response_model=MatterFactExportRead, status_code=status.HTTP_201_CREATED)
def create_fact_export_files(
    matter_id: uuid.UUID,
    payload: MatterFactExportCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterFactExport:
    """Write the facts and evidence CSV files for the filter now and return the completed export.

    Repeating a request with the same idempotency key returns the first export instead of writing new files.
    """

    matter = _export_matter(db, matter_id, principal)
    try:
        export, _ = create_fact_export(
            db,
            matter,
            filters=payload.filters,
            requested_filename=payload.requested_filename,
            idempotency_key=payload.idempotency_key,
            spreadsheet_safe=payload.spreadsheet_safe,
            actor_user_id=principal.user.id,
            settings=get_settings(),
        )
    except FactFilterError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except FactExportConflict as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (FactExportTooLarge, HTTPError, ConnectionError, TimeoutError, PermissionError, ValueError) as exc:
        db.rollback()
        logger.warning("Fact export for matter %s failed: %s", matter.id, exc)
        record_fact_export_failure(
            db, matter, actor_user_id=principal.user.id, filters=payload.filters, error=exc
        )
        db.commit()
        if isinstance(exc, FactExportTooLarge):
            raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc)) from exc
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The export files could not be stored. Try again.",
        ) from exc
    db.commit()
    db.refresh(export)
    return export


@router.get("", response_model=list[MatterFactExportRead])
def list_fact_exports(
    matter_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterFactExport]:
    matter = _export_matter(db, matter_id, principal)
    return list(
        db.scalars(
            select(MatterFactExport)
            .where(MatterFactExport.matter_id == matter.id)
            .order_by(MatterFactExport.created_at.desc(), MatterFactExport.id.desc())
            .limit(limit)
        )
    )


@router.get("/{export_id}", response_model=MatterFactExportRead)
def get_fact_export(
    matter_id: uuid.UUID,
    export_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterFactExport:
    _export_matter(db, matter_id, principal)
    return _export_or_404(db, matter_id, export_id)


@router.get("/{export_id}/download")
def download_fact_export(
    matter_id: uuid.UUID,
    export_id: uuid.UUID,
    file: Literal["facts", "evidence"] = Query(default="facts"),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Stream one of the export's two CSV files through Core after fresh authorization."""

    matter = _export_matter(db, matter_id, principal)
    export = _export_or_404(db, matter_id, export_id)
    artifact_id = export.facts_artifact_id if file == "facts" else export.evidence_artifact_id
    content_hash = export.facts_content_hash if file == "facts" else export.evidence_content_hash
    byte_length = export.facts_byte_length if file == "facts" else export.evidence_byte_length
    filename = export.facts_filename if file == "facts" else export.evidence_filename
    if as_utc(export.expires_at) <= datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Fact export has expired")
    try:
        artifact = get_workflow_artifact_reference(
            artifact_id=artifact_id,
            actor_user_id=principal.user.id,
            tenant_id=matter.client.tenant_id,
            client_id=matter.client_id,
        )
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Fact export file is unavailable") from exc
    expected_kind = "FACTS" if file == "facts" else "EVIDENCE"
    if not (
        artifact.artifact_type == ArtifactType.FACT_EXPORT
        and artifact.core_scope_kind == "MATTER"
        and artifact.core_scope_id == matter.id
        and artifact.producer_run_id == export.id
        and artifact.content_hash == content_hash
        and artifact.byte_length == byte_length
        and str(artifact.metadata.get("export_id")) == str(export.id)
        and artifact.metadata.get("file_kind") == expected_kind
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Fact export file provenance is invalid")
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="fact_export.downloaded",
        target_type="matter_fact_export",
        target_id=export.id,
        details={"matter_id": str(matter.id), "file": file, "artifact_id": str(artifact_id), "content_hash": content_hash},
    )
    db.commit()
    return StreamingResponse(
        iter_workflow_artifact_content(
            artifact_id=artifact_id,
            actor_user_id=principal.user.id,
            tenant_id=matter.client.tenant_id,
            client_id=matter.client_id,
        ),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "Content-Length": str(byte_length),
            "ETag": content_hash,
        },
    )
