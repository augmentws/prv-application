import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.audit import record_audit
from app.config import Settings, get_settings
from app.database import get_db
from app.dependencies import Principal, can_admin_matter, get_principal
from app.models import Matter, MatterSavedSearch, MatterSavedSearchUserShare, User
from app.routers.search import execute_matter_search
from app.schemas import (
    MatterSavedSearchCreate,
    MatterSavedSearchExecute,
    MatterSavedSearchRead,
    MatterSavedSearchUpdate,
    MatterSavedSearchUserRead,
    MatterSearchRequest,
    MatterSearchResponse,
)

router = APIRouter(prefix="/v1/matters/{matter_id}/saved-searches", tags=["matter saved searches"])


def _matter(db: Session, matter_id: uuid.UUID, principal: Principal) -> Matter:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if not can_admin_matter(db, principal, matter):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Matter access required")
    return matter


def _access_clause(user_id: uuid.UUID):
    shared = exists().where(
        MatterSavedSearchUserShare.saved_search_id == MatterSavedSearch.id,
        MatterSavedSearchUserShare.user_id == user_id,
    )
    return or_(
        MatterSavedSearch.owner_user_id == user_id,
        MatterSavedSearch.visibility == "PUBLIC",
        and_(MatterSavedSearch.visibility == "SHARED", shared),
    )


def _saved_search(
    db: Session,
    matter_id: uuid.UUID,
    saved_search_id: uuid.UUID,
    principal: Principal,
) -> MatterSavedSearch:
    saved = db.scalar(
        select(MatterSavedSearch)
        .options(
            selectinload(MatterSavedSearch.owner),
            selectinload(MatterSavedSearch.user_shares).selectinload(MatterSavedSearchUserShare.user),
        )
        .where(
            MatterSavedSearch.id == saved_search_id,
            MatterSavedSearch.matter_id == matter_id,
            _access_clause(principal.user.id),
        )
    )
    if saved is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saved search not found")
    return saved


def _normalize_name(value: str) -> tuple[str, str]:
    name = " ".join(value.split())
    if not name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Saved search name is required")
    return name, name.casefold()


def _normalize_description(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


def _shared_users(
    db: Session,
    matter: Matter,
    owner_user_id: uuid.UUID,
    user_ids: list[uuid.UUID],
) -> list[User]:
    unique_ids = set(user_ids)
    if owner_user_id in unique_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A saved search does not need to be shared with its owner",
        )
    users = list(
        db.scalars(
            select(User).where(
                User.id.in_(unique_ids),
                User.tenant_id == matter.client.tenant_id,
                User.status == "ACTIVE",
            )
        )
    )
    if len(users) != len(unique_ids):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Every shared user must be an active user in the matter tenant",
        )
    return users


def _read(saved: MatterSavedSearch, principal: Principal) -> MatterSavedSearchRead:
    shared_users = sorted(
        (share.user for share in saved.user_shares),
        key=lambda user: (user.display_name.casefold(), user.email.casefold()),
    )
    return MatterSavedSearchRead(
        id=saved.id,
        matter_id=saved.matter_id,
        name=saved.name,
        description=saved.description,
        visibility=saved.visibility,
        search=MatterSearchRequest.model_validate(saved.search_definition),
        owner=MatterSavedSearchUserRead(
            id=saved.owner.id,
            display_name=saved.owner.display_name,
            email=saved.owner.email,
        ),
        shared_users=[
            MatterSavedSearchUserRead(id=user.id, display_name=user.display_name, email=user.email)
            for user in shared_users
        ],
        is_owner=saved.owner_user_id == principal.user.id,
        created_at=saved.created_at,
        updated_at=saved.updated_at,
    )


def _replace_shares(saved: MatterSavedSearch, users: list[User]) -> None:
    saved.user_shares = [MatterSavedSearchUserShare(user_id=user.id) for user in users]


@router.get("", response_model=list[MatterSavedSearchRead])
def list_saved_searches(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterSavedSearchRead]:
    _matter(db, matter_id, principal)
    searches = list(
        db.scalars(
            select(MatterSavedSearch)
            .options(
                selectinload(MatterSavedSearch.owner),
                selectinload(MatterSavedSearch.user_shares).selectinload(MatterSavedSearchUserShare.user),
            )
            .where(MatterSavedSearch.matter_id == matter_id, _access_clause(principal.user.id))
            .order_by(MatterSavedSearch.updated_at.desc(), MatterSavedSearch.name)
        )
    )
    return [_read(saved, principal) for saved in searches]


@router.post("", response_model=MatterSavedSearchRead, status_code=status.HTTP_201_CREATED)
def create_saved_search(
    matter_id: uuid.UUID,
    payload: MatterSavedSearchCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterSavedSearchRead:
    matter = _matter(db, matter_id, principal)
    name, normalized_name = _normalize_name(payload.name)
    users = _shared_users(db, matter, principal.user.id, payload.shared_user_ids)
    search = payload.search.model_copy(update={"offset": 0})
    saved = MatterSavedSearch(
        matter_id=matter.id,
        owner_user_id=principal.user.id,
        name=name,
        normalized_name=normalized_name,
        description=_normalize_description(payload.description),
        visibility=payload.visibility,
        search_definition=search.model_dump(mode="json", by_alias=True),
    )
    saved.owner = principal.user
    _replace_shares(saved, users)
    db.add(saved)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already have a saved search with this name in the matter",
        ) from exc
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter.saved_search.created",
        target_type="matter_saved_search",
        target_id=saved.id,
        details={"matter_id": str(matter.id), "visibility": saved.visibility},
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already have a saved search with this name in the matter",
        ) from exc
    db.refresh(saved)
    return _read(saved, principal)


@router.get("/{saved_search_id}", response_model=MatterSavedSearchRead)
def get_saved_search(
    matter_id: uuid.UUID,
    saved_search_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterSavedSearchRead:
    _matter(db, matter_id, principal)
    return _read(_saved_search(db, matter_id, saved_search_id, principal), principal)


@router.put("/{saved_search_id}", response_model=MatterSavedSearchRead)
def update_saved_search(
    matter_id: uuid.UUID,
    saved_search_id: uuid.UUID,
    payload: MatterSavedSearchUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterSavedSearchRead:
    matter = _matter(db, matter_id, principal)
    saved = _saved_search(db, matter_id, saved_search_id, principal)
    if saved.owner_user_id != principal.user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner can edit this saved search")
    name, normalized_name = _normalize_name(payload.name)
    users = _shared_users(db, matter, saved.owner_user_id, payload.shared_user_ids)
    saved.name = name
    saved.normalized_name = normalized_name
    saved.description = _normalize_description(payload.description)
    saved.visibility = payload.visibility
    saved.search_definition = payload.search.model_copy(update={"offset": 0}).model_dump(mode="json", by_alias=True)
    saved.updated_at = datetime.now(timezone.utc)
    _replace_shares(saved, users)
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter.saved_search.updated",
        target_type="matter_saved_search",
        target_id=saved.id,
        details={"matter_id": str(matter.id), "visibility": saved.visibility},
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already have a saved search with this name in the matter",
        ) from exc
    db.refresh(saved)
    return _read(saved, principal)


@router.delete("/{saved_search_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_saved_search(
    matter_id: uuid.UUID,
    saved_search_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> None:
    matter = _matter(db, matter_id, principal)
    saved = _saved_search(db, matter_id, saved_search_id, principal)
    if saved.owner_user_id != principal.user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner can delete this saved search")
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter.saved_search.deleted",
        target_type="matter_saved_search",
        target_id=saved.id,
        details={"matter_id": str(matter.id)},
    )
    db.delete(saved)
    db.commit()


@router.post("/{saved_search_id}/execute", response_model=MatterSearchResponse)
def execute_saved_search(
    matter_id: uuid.UUID,
    saved_search_id: uuid.UUID,
    payload: MatterSavedSearchExecute,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MatterSearchResponse:
    matter = _matter(db, matter_id, principal)
    saved = _saved_search(db, matter_id, saved_search_id, principal)
    search = MatterSearchRequest.model_validate(saved.search_definition)
    updates: dict[str, int] = {"offset": payload.offset}
    if payload.size is not None:
        updates["size"] = payload.size
    return execute_matter_search(matter, search.model_copy(update=updates), db=db, settings=settings)
