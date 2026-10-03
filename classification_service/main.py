from fastapi import FastAPI

from classification_service.api import router

app = FastAPI(title="Priv-View Classification Runtime", version="1.0.0")
app.include_router(router)
