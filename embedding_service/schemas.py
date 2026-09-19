from typing import Annotated, Literal

from pydantic import BaseModel, Field

EmbeddingInputType = Literal["document", "query"]


class EmbeddingRequest(BaseModel):
    inputs: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1, max_length=1000)
    input_type: EmbeddingInputType = "document"


class EmbeddingResponse(BaseModel):
    model: str
    model_revision: str | None
    dimensions: int
    normalized: bool
    embeddings: list[list[float]]
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    request_count: int = Field(default=0, ge=0)


class EmbeddingModelRead(BaseModel):
    provider: str
    model: str
    model_revision: str | None
    dimensions: int
    normalized: bool
    device: str
    loaded: bool
