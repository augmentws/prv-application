import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from embedding_service.config import EmbeddingSettings, get_embedding_settings

bearer = HTTPBearer(auto_error=False)


def require_embedding_service_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    settings: EmbeddingSettings = Depends(get_embedding_settings),
) -> None:
    if credentials is None or not secrets.compare_digest(credentials.credentials, settings.service_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid embedding service token")
