import secrets

from fastapi import Depends, Header, HTTPException, status

from classification_service.config import ClassificationSettings, get_classification_settings


def require_service_token(
    x_classification_service_token: str | None = Header(default=None),
    settings: ClassificationSettings = Depends(get_classification_settings),
) -> None:
    if x_classification_service_token is None or not secrets.compare_digest(
        x_classification_service_token,
        settings.service_token,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Classification service token required",
        )
