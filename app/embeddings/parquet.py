from io import BytesIO
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from app.embeddings.chunking import TextChunk
from app.embeddings.configuration import CHUNK_SET_SCHEMA_VERSION, CHUNK_VECTOR_SET_SCHEMA_VERSION


def _schema_metadata(values: dict[str, Any]) -> dict[bytes, bytes]:
    return {f"pvr.{key}".encode(): str(value).encode() for key, value in values.items() if value is not None}


def write_chunk_set(chunks: list[TextChunk], metadata: dict[str, Any]) -> bytes:
    table = pa.table(
        {
            "chunk_id": pa.array([chunk.chunk_id for chunk in chunks], type=pa.string()),
            "ordinal": pa.array([chunk.ordinal for chunk in chunks], type=pa.int32()),
            "char_start": pa.array([chunk.char_start for chunk in chunks], type=pa.int64()),
            "char_end": pa.array([chunk.char_end for chunk in chunks], type=pa.int64()),
            "text": pa.array([chunk.text for chunk in chunks], type=pa.large_string()),
        }
    ).replace_schema_metadata(
        _schema_metadata({"schema": "chunk-set", "schema_version": CHUNK_SET_SCHEMA_VERSION, **metadata})
    )
    output = BytesIO()
    pq.write_table(table, output, compression="zstd")
    return output.getvalue()


def read_chunk_set(content: bytes) -> list[TextChunk]:
    table = pq.read_table(BytesIO(content), columns=["chunk_id", "ordinal", "char_start", "char_end", "text"])
    return [
        TextChunk(
            chunk_id=row["chunk_id"],
            ordinal=row["ordinal"],
            char_start=row["char_start"],
            char_end=row["char_end"],
            text=row["text"],
        )
        for row in table.to_pylist()
    ]


def write_vector_set(
    chunks: list[TextChunk],
    vectors: list[list[float]],
    *,
    dimensions: int,
    metadata: dict[str, Any],
) -> bytes:
    if len(chunks) != len(vectors):
        raise ValueError("Every chunk must have exactly one embedding")
    vector_type = pa.list_(pa.float32(), dimensions)
    table = pa.table(
        {
            "chunk_id": pa.array([chunk.chunk_id for chunk in chunks], type=pa.string()),
            "ordinal": pa.array([chunk.ordinal for chunk in chunks], type=pa.int32()),
            "embedding": pa.array(vectors, type=vector_type),
        }
    ).replace_schema_metadata(
        _schema_metadata(
            {
                "schema": "chunk-vector-set",
                "schema_version": CHUNK_VECTOR_SET_SCHEMA_VERSION,
                "dimensions": dimensions,
                **metadata,
            }
        )
    )
    output = BytesIO()
    pq.write_table(table, output, compression="zstd")
    return output.getvalue()


def read_vector_set(content: bytes, *, dimensions: int) -> dict[str, list[float]]:
    table = pq.read_table(BytesIO(content), columns=["chunk_id", "embedding"])
    result = {row["chunk_id"]: row["embedding"] for row in table.to_pylist()}
    if any(len(vector) != dimensions for vector in result.values()):
        raise ValueError("Chunk vector set dimensions do not match the configured search index")
    return result
