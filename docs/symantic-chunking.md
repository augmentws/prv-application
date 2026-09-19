# Semantic Chunking

The current "semantic chunking" implementation is sentence- and paragraph-aware heuristic chunking. It does not use an AI model to discover topic boundaries.

For the approaches tried after embedding generation, their measured results, and the current topic-discovery implementation, see [Topic Clustering Experiments and Outcomes](topic-clustering-experiments.md).

## Processing flow

### 1. Select the best text source

For each document, the system uses the best available artifact in this order:

1. `NORMALIZED_TEXT` from the collection's active text-processing run
2. `EXTRACTED_TEXT`
3. `OCR_TEXT`
4. `NATIVE`, but only for emails and text-readable formats

`NORMALIZED_TEXT` is an Artifact-owned, versioned derivative. It is created by the collection-level Processing job after the immutable default processor and the collection's ordered custom rules run. The artifact records its configuration hash and has `NORMALIZED_FROM` lineage to its faithful source. Native, extracted, and OCR artifacts are never changed.

A processing run does not become active until it finishes. Chunking must filter normalized artifacts by `client_collection.active_text_processing_run_id`, so a concurrent embedding job cannot read partially generated output. If an item was skipped or failed in the active run, source selection falls back through extracted text, OCR text, and supported native text.

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

This allows a job to be safely retried and prevents unchanged documents from being processed again. Activating a new normalized-text run changes the source artifact hash and therefore produces a new chunk derivation when the matter embedding job is run again.

### 8. Update OpenSearch

Document source reads, chunk preparation, and vector-artifact writes use bounded per-document concurrency within each durable batch. Chunks from multiple documents are submitted together up to the embedding service's input limit, while vector artifacts remain separate and idempotent per document. The worker logs preparation and completion progress every 25 documents, followed by batch timing for preparation, raw model inference, vector persistence, and the complete pipeline. Inference timing reports both request count and chunks per second so storage overhead is distinguishable from model throughput. After all durable embedding batches complete, the job coalesces the affected documents into one paged OpenSearch indexing operation with a single final refresh. All chunks for a document are attached to its single OpenSearch document. Each indexed chunk contains:

- Chunk ID and ordinal
- Character offsets
- Chunk text
- Embedding vector

Semantic search can therefore retrieve the most relevant chunks while still returning and grouping results at the document level.

## Current limitation

The current chunker is deterministic and boundary-aware, but it does not detect meaning or topic transitions with a model. A future implementation could use embeddings or a segmentation model to recognize subject changes, headings, quoted email chains, and conversation boundaries.
