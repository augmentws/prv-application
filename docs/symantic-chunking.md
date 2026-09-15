# Semantic Chunking

The current "semantic chunking" implementation is sentence- and paragraph-aware heuristic chunking. It does not use an AI model to discover topic boundaries.

## Processing flow

### 1. Select the best text source

For each document, the system uses the newest available artifact in this order:

1. `EXTRACTED_TEXT`
2. `OCR_TEXT`
3. `NATIVE`, but only for emails and text-readable formats

For native email files, the system extracts the message body, preferring plain text and falling back to HTML. Attachments are not included in the parent email's text. HTML is converted to plain text.

### 2. Identify semantic units

Text is divided at:

- Sentence endings: `.`, `!`, or `?`
- Blank lines, which generally represent paragraph boundaries

A single unit longer than the maximum chunk size is split at the nearest preceding space, or hard-split if no space is available.

### 3. Assemble chunks

The current defaults are:

- Target: 1,800 characters
- Maximum: 2,600 characters
- Overlap: up to 200 characters
- Maximum source text read: 10 MB

Consecutive sentences or paragraphs are accumulated until the chunk reaches approximately 1,800 characters. A chunk will not intentionally exceed 2,600 characters.

For overlap, complete trailing semantic units within the last 200 characters are retained for the next chunk. The system does not cut a sentence merely to guarantee exactly 200 characters of overlap.

The values are configured through:

- `CHUNK_TARGET_CHARACTERS`
- `CHUNK_MAX_CHARACTERS`
- `CHUNK_OVERLAP_CHARACTERS`
- `EMBEDDING_TEXT_MAX_BYTES`

### 4. Assign stable identities

Each chunk records:

- `chunk_id`
- Ordinal position
- Starting and ending character offsets
- Text

The chunk ID is a SHA-256 hash based on the source artifact hash, ordinal, and offsets. Unchanged source text and configuration therefore produce repeatable chunk identities.

### 5. Store the chunk set

Each document gets its own `CHUNK_SET` Parquet artifact containing all of its chunks. It is Zstandard-compressed and includes the chunking configuration and source-artifact information.

The Parquet columns are:

- `chunk_id`
- `ordinal`
- `char_start`
- `char_end`
- `text`

### 6. Generate embeddings

Each chunk is passed to the configured embedding model. The current default configuration is:

- Model: `voyageai/voyage-4-nano`
- Dimensions: 1,024
- Provider: `sentence_transformers`
- Execution mode: embedded/local
- Vector normalization: enabled

A second per-document Parquet artifact, `CHUNK_VECTOR_SET`, stores the chunk IDs and their corresponding fixed-size `float32` vectors.

### 7. Avoid duplicate work

The source content hash and complete chunking configuration form a derivation key. If the matching `CHUNK_SET` already exists, it is reused.

The chunk-set content hash and embedding configuration similarly identify the vector set. If the matching `CHUNK_VECTOR_SET` already exists, embedding generation is skipped.

This allows a job to be safely retried and prevents unchanged documents from being processed again.

### 8. Update OpenSearch

After an embedding batch completes, each affected matter document is reindexed. All of its chunks are attached to its single OpenSearch document. Each indexed chunk contains:

- Chunk ID and ordinal
- Character offsets
- Chunk text
- Embedding vector

Semantic search can therefore retrieve the most relevant chunks while still returning and grouping results at the document level.

## Current limitation

The current chunker is deterministic and boundary-aware, but it does not detect meaning or topic transitions with a model. A future implementation could use embeddings or a segmentation model to recognize subject changes, headings, quoted email chains, and conversation boundaries.
