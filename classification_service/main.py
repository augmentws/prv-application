from fastapi import FastAPI

from classification_service.api import router
from classification_service.config import get_classification_settings

app = FastAPI(title="Priv-View Classification Runtime", version="1.0.0")
# Load settings at import so a development secret outside development stops startup.
get_classification_settings()
app.include_router(router)
