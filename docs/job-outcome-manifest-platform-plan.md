# Reusable Job Outcome Manifest Platform Plan

## 1. Purpose

Priv-View jobs frequently process a large, frozen or discoverable set of items,
but their durable records generally retain only aggregate counters and a bounded
error message. That is sufficient for progress reporting, but it does not answer
operational questions such as:

- Which documents were skipped?
- Was a document skipped because its output was reused or because it had no
  usable input?
- Which documents failed, at what stage, and with which stable reason code?
- Which output artifact was produced for a successful item?
- Can an administrator filter, inspect, and export the affected documents?

The immediate need is embedding diagnostics. The existing embedding workflow
stores job and batch totals, while the realtime path does not persist one
outcome and reason per matter document. In particular, `SKIPPED` currently
combines materially different outcomes such as reusing an existing vector and
finding no usable text.

This plan adds a reusable item-level outcome facility for document-oriented
jobs without putting millions of outcome rows in the Core PostgreSQL database.

## 2. Architectural decision

Use two layers:

1. Core stores one `job_outcome_manifest` row per logical job.
2. Artifact stores the complete item-level outcomes as immutable Parquet.

```text
Domain job row
    -> Core job_outcome_manifest
        -> Artifact JOB_ITEM_OUTCOMES Parquet
```

The domain job remains authoritative for configuration, business state,
authorization, and results. The common `WorkflowRun` execution
record is authoritative for an individual attempt's execution status,
progress, cancellation, retry lineage, heartbeat, and failure. The outcome
manifest adds a consistent diagnostic projection; it replaces neither the
domain job nor its execution attempts.

Lifecycle normalization is specified separately in the
[Unified Job Execution Platform Implementation Plan](unified-job-execution-platform-plan.md).

Do not create a generic PostgreSQL row for every processed document. A matter
may contain hundreds of thousands or millions of documents, and multiple jobs
would turn a generic row table into an unbounded operational ledger. Parquet is
the correct durable format for complete, immutable, high-volume outcomes.

## 3. Applicability

The platform is for jobs with meaningful item-level work, including:

- Matter embedding generation.
- Matter-document imports.
- Bulk coding and tagging.
- Topic assignment.
- Agent document processing.
- Classification scoring.
- Future CAL queue generation and batch publication.

It is not mandatory for jobs whose result is inherently run- or stage-level.
Model training, schema promotion, and index activation should use durable stage
events and logs unless they also process a meaningful item population.

The shared contract must not force every domain to use the same domain-specific
payload. It standardizes identity, status, reason, provenance, and references,
while allowing a bounded namespaced payload for job-specific diagnostics.

## 4. Current-state findings

### 4.1 Embedding jobs

Core currently stores:

- Job-level total, processed, embedded, skipped, failed, and chunk counts.
- Batch-level equivalents.
- A bounded batch `error_message` containing up to a limited number of errors.
- A per-document provider manifest only for execution modes that require one.

The realtime execution path does not retain the identity or reason for every
skipped document. `DocumentEmbeddingResult("SKIPPED", ...)` represents both
reused vectors and non-embeddable documents. Exact skipped-document diagnostics
therefore cannot be reconstructed reliably for historical realtime runs.

### 4.2 Existing reusable infrastructure

The Artifact Service already owns immutable workflow artifacts, derivation-key
idempotency, content hashes, media-type validation, lineage, and embedded or
remote operation. Classification scoring already demonstrates the desired read
pattern: Core reauthorizes the request, verifies the Artifact reference and
hash, and reads bounded Parquet row groups rather than returning an entire
artifact to the browser.

The first implementation should extend this infrastructure rather than create a
second blob or object-storage mechanism.

## 5. Terminology and semantics

### 5.1 Generic outcome statuses

Every item row uses one controlled generic status:

| Status | Meaning |
| --- | --- |
| `SUCCEEDED` | The job produced the intended new output for the item. |
| `REUSED` | A compatible existing output satisfied the job without recomputation. |
| `SKIPPED` | Policy or missing input made the item intentionally non-processable. |
| `FAILED` | The item encountered an error and did not produce usable output. |
| `EXCLUDED` | The frozen job population retained the item but policy excluded it. |
| `CANCELED` | Item processing was prevented or interrupted by job cancellation. |

`REUSED` must not be counted as `SKIPPED`. This distinction is required for
cost analysis, rerun behavior, and data-quality diagnosis.

### 5.2 Reason codes

Reason codes are stable, machine-readable, lowercase namespaced values. Human
messages may improve over time without changing the code.

Initial embedding reason codes:

```text
embedding.generated
embedding.reused_existing_vector
embedding.no_text_source
embedding.empty_after_chunking
embedding.source_read_failed
embedding.chunk_preparation_failed
embedding.provider_failed
embedding.response_invalid
embedding.vector_persistence_failed
embedding.document_not_found
embedding.job_canceled
```

Do not store raw provider responses, credentials, SQL parameters, native text,
or document contents in outcome messages. Exceptions must be sanitized before
publication.

## 6. Core data model

Add one Core migration for `job_outcome_manifest`.

### 6.1 Proposed columns

```text
job_outcome_manifest
  id                              UUID primary key
  tenant_id                       UUID not null, indexed
  client_id                       UUID nullable, indexed
  matter_id                       UUID nullable, indexed
  job_type                        VARCHAR(80) not null
  job_id                          UUID not null
  manifest_status                 VARCHAR(20) not null
  outcome_schema                  VARCHAR(100) not null
  outcome_schema_version          INTEGER not null
  artifact_id                     UUID nullable
  artifact_content_hash           VARCHAR(64) nullable
  artifact_byte_length             BIGINT nullable
  derivation_key                  VARCHAR(64) not null
  item_count                      BIGINT not null
  status_counts                   JSON not null
  reason_counts                   JSON not null
  producer_kind                   VARCHAR(50) not null
  producer_run_id                 UUID not null
  finalized_at                    TIMESTAMPTZ nullable
  failure_summary                 TEXT nullable
  created_at                      TIMESTAMPTZ not null
  updated_at                      TIMESTAMPTZ not null
```

### 6.2 Constraints

- Unique `(job_type, job_id)` for the final logical-job manifest.
- `manifest_status` is `BUILDING`, `FINALIZED`, or `FAILED`.
- Artifact ID, content hash, and byte length are either all null or all set.
- `FINALIZED` requires the complete artifact tuple and `finalized_at`.
- Counts are non-negative and sum to `item_count` when finalized.
- Status-count keys must be from the generic status catalog.
- Reason counts must be bounded in key count and encoded size.
- `job_type` is selected from a code-owned registry, never arbitrary user data.

Artifact and Core use separate databases, so `artifact_id` cannot be a database
foreign key. Finalization must verify the Artifact record's tenant, client,
scope, type, producer, byte length, and hash before storing the reference.

### 6.3 Polymorphic job reference

The generic `(job_type, job_id)` reference cannot have a direct foreign key to
multiple domain tables. A code-owned `JobOutcomeAdapter` registry supplies:

- Domain job loader.
- Tenant/client/matter scope resolver.
- Required authorization.
- Outcome schema name and supported versions.
- Display name and item-link behavior.
- Whether the domain supports item outcomes.

Unknown job types fail closed. A periodic integrity check should report
manifest rows whose registered domain job no longer resolves.

Use the code-owned adapter registry together with the existing `WorkflowRun`
table as described in the unified execution plan. Do not introduce a second
generic `job_execution` table and do not replace domain job tables with a
generic inheritance hierarchy. `producer_run_id` identifies the `WorkflowRun`
attempt that produced a fragment; the final manifest remains uniquely attached
to the logical `(job_type, job_id)`.

## 7. Artifact contract

### 7.1 Artifact types and service ownership

Generalize the workflow-artifact contract, which is currently classification-
oriented, to support:

```text
JOB_ITEM_OUTCOME_FRAGMENT
JOB_ITEM_OUTCOMES
```

Artifact Service remains the owner of content, immutable metadata, derivation
uniqueness, lineage, and storage. Core owns the manifest row and authorization.

The existing Artifact schemas import classification-owned `ArtifactType`.
Before adding generic job outcomes, move the workflow artifact type contract to
an Artifact-owned or neutral shared module. Classification may import the
neutral contract; Artifact must not depend on the Classification Runtime for a
platform-wide artifact enumeration.

Add the required Artifact database constraint values, media-type allowlists,
size limits, and embedded/remote API tests. Both Artifact migration upgrade and
downgrade paths are required.

### 7.2 Parquet v1 envelope

Every `JOB_ITEM_OUTCOMES` v1 row contains:

```text
ordinal                 int64, required
item_kind               string, required
item_id                 string, required
outcome_status          string, required
reason_code             string, required
message                 string, nullable
batch_id                string, nullable
attempt_number          int32, required
input_fingerprint       string, nullable
output_references_json  string, required, canonical JSON
metrics_json            string, required, canonical JSON
payload_json            string, required, canonical JSON
started_at              timestamp, nullable
completed_at            timestamp, nullable
```

Rules:

- Exactly one final row exists for every item in the frozen job population.
- Rows use deterministic population order and a contiguous zero-based ordinal.
- `item_id` is stable within `item_kind` and the owning job.
- Generic columns are sufficient for common filtering and summaries.
- Job-specific fields stay in bounded canonical JSON payloads unless a later
  schema version promotes a broadly useful field.
- Output references contain identifiers and hashes, not signed URLs.
- Parquet metadata freezes schema name/version, job type/ID, producer identity,
  derivation key, scope IDs, item count, and summary hash.
- Row groups should target bounded preview reads, initially approximately
  10,000 rows, and must include statistics for status and reason columns.

### 7.3 Batch fragments and finalization

Jobs such as embedding generation execute many batches concurrently. They must
not append concurrently to one Parquet file.

Each completed batch writes one immutable `JOB_ITEM_OUTCOME_FRAGMENT` artifact.
The job finalizer:

1. Resolves every expected batch fragment.
2. Validates its scope, job identity, batch identity, schema, hashes, and row
   count.
3. Rejects duplicate or missing item ordinals.
4. Concatenates fragments in deterministic population order.
5. Reconciles status/reason counts.
6. Publishes one immutable `JOB_ITEM_OUTCOMES` artifact.
7. Verifies the published artifact through the Artifact boundary.
8. Atomically finalizes the Core manifest row.

Retries reuse a matching fragment derivation instead of creating duplicate
outcomes. The final artifact derivation includes the frozen job configuration,
population identity, schema version, and ordered fragment hashes.

Fragment retention may be shortened after finalization, but deletion must be an
explicit Artifact retention policy and must never remove the final artifact.

## 8. Shared Core service

Add a `job_outcomes` domain module with no job-specific processing logic.

Responsibilities:

- Register and resolve supported job types.
- Create or reconcile a building manifest.
- Validate fragment and final artifact references.
- Reconcile counts and identity.
- Stream an Artifact to a temporary bounded file.
- Verify SHA-256 before returning any data.
- Validate Parquet schema and embedded job identity.
- Read only required row groups and columns.
- Apply controlled status/reason filtering.
- Return bounded pages and summary data.
- Record audit events for manifest finalization and exports.

Workers use an internal producer contract; browsers never upload manifests and
never receive Artifact credentials.

## 9. API design

Expose generic read APIs under the authorized matter boundary:

```text
GET /v1/matters/{matter_id}/job-outcomes/{job_type}/{job_id}
GET /v1/matters/{matter_id}/job-outcomes/{job_type}/{job_id}/items
GET /v1/matters/{matter_id}/job-outcomes/{job_type}/{job_id}/download
```

### 9.1 Summary response

The summary returns:

- Job type and ID.
- Manifest status and schema version.
- Item count.
- Status and reason counts.
- Artifact identity and finalized timestamp.
- Whether item details are available.
- A clear legacy/partial-diagnostics state when applicable.

### 9.2 Item query

Supported controlled parameters:

```text
status
reason_code
offset
limit (maximum 200)
```

The first version does not accept arbitrary SQL, Arrow, or OpenSearch
expressions. Core selects only the columns required for the response and reads
only relevant Parquet row groups where statistics permit.

Offset pagination is acceptable for the initial bounded diagnostic viewer.
If deep paging becomes common, add a signed cursor over ordinal and filter
state; do not return the full artifact to browser JavaScript.

### 9.3 Download

An explicit administrator action may request CSV or Parquet export. Core must
reauthorize at request time and stream through the trusted boundary. Downloads
must preserve reason codes and stable item IDs and must not contain native text
or credentials.

### 9.4 Authorization

- Require the domain job's normal read permission; MVP embedding diagnostics
  require matter `ADMIN`.
- Resolve the job through the adapter and confirm it belongs to the route's
  matter.
- Confirm the manifest's tenant/client/matter scope matches the domain job.
- Confirm the Artifact metadata matches the Core manifest.
- Never authorize from `job_type` and `job_id` alone.

## 10. Embedding producer integration

Embedding generation is the first adopter and the MVP proof of the generic
contract.

### 10.1 Replace ambiguous skipped outcomes

Refactor document preparation to return a structured outcome rather than only
`DocumentEmbeddingResult("SKIPPED", chunk_count)`.

Required mappings:

| Current condition | Generic status | Reason code |
| --- | --- | --- |
| New vector artifact stored | `SUCCEEDED` | `embedding.generated` |
| Matching finalized vector already exists | `REUSED` | `embedding.reused_existing_vector` |
| No supported text source | `SKIPPED` | `embedding.no_text_source` |
| Text produces no chunks | `SKIPPED` | `embedding.empty_after_chunking` |
| Matter document missing | `FAILED` | `embedding.document_not_found` |
| Source/chunk preparation exception | `FAILED` | appropriate preparation code |
| Provider call or response invalid | `FAILED` | appropriate provider code |
| Artifact storage fails | `FAILED` | `embedding.vector_persistence_failed` |
| Cancellation prevents execution | `CANCELED` | `embedding.job_canceled` |

### 10.2 Embedding-specific payload

The bounded `payload_json` may contain:

```text
matter_document_id
collection_item_id
chunk_count
source_artifact_id
source_content_hash
chunk_set_artifact_id
chunk_set_content_hash
vector_artifact_id
vector_content_hash
embedding_model
embedding_model_revision
embedding_dimensions
```

Omit unavailable values rather than inventing identifiers.

### 10.3 Counters

Add or derive distinct job summary counters:

- Generated.
- Reused.
- Skipped due to missing/non-usable content.
- Failed.
- Canceled.

Preserve existing aggregate fields during migration, but stop presenting
`skipped_count` as if it includes reuse. A compatibility response may expose
the legacy total while new UI uses the manifest summary.

### 10.4 Completion semantics

Outcome finalization is part of embedding-job finalization. A job cannot become
`COMPLETED` or `COMPLETED_WITH_ERRORS` until:

- Every planned item has one final outcome.
- Counts reconcile to the frozen population.
- The final outcome artifact is published and verified.
- The Core manifest reference is committed.

If output publication succeeds but the worker response is lost, reconciliation
must resolve the artifact by derivation and finish the same manifest. It must
not publish a second logical outcome artifact.

Cancellation preserves outcomes for items already processed. Unstarted items
receive `CANCELED` rows during finalization so the artifact still covers the
entire frozen population.

## 11. Administrator UI

### 11.1 Jobs table

Make Generated, Reused, Skipped, and Failed counts independently visible.
Counts with details available are links, not inert totals.

### 11.2 Outcome detail page or drawer

Provide:

- Job identity, status, configuration, and timestamps.
- Summary cards by outcome status.
- Reason-code distribution.
- Filters for status and reason.
- Paginated document rows.
- Human-readable reason plus stable reason code.
- Links to open authorized matter documents.
- Chunk and output-reference details where appropriate.
- Explicit CSV/Parquet download.

The UI must label reused vectors as `Reused`, not `Skipped`.

### 11.3 Classification integration

When Classification selects an embedding set, provide a link to its outcome
summary. If the selected set has missing or failed features, the administrator
can inspect those documents before launching model training.

The diagnostics UI reads the shared API; it must not parse complete Parquet
files or duplicate reason semantics in the browser.

## 12. Legacy jobs and backfill policy

Do not fabricate exact item outcomes for historical jobs.

- If a historical execution mode stored a complete provider manifest, a
  one-time controlled backfill may produce a manifest and must record that the
  data was reconstructed.
- If only aggregate counts and bounded errors exist, expose a partial summary
  with `item_details_available = false`.
- Preserve recoverable failed document IDs and sanitized messages where they
  exist, but label the result partial.
- Historical skipped identities and reasons that were never persisted remain
  unavailable.

For the current Trec embedding run, the single failed document can be located
from its batch error. The identities and reasons for its skipped documents
cannot be reconstructed reliably, so exact diagnostics begin with new runs.

## 13. Security, privacy, and retention

- Outcome artifacts inherit the domain job's tenant/client scope.
- Core reauthorizes every summary, page, and download request.
- Artifact IDs are never sufficient authorization.
- Messages are sanitized and length-limited.
- Do not store document text, provider credentials, request headers, SQL
  parameters, or stack traces in outcome artifacts.
- Output references use stable IDs and hashes only.
- Audit downloads and access to detailed failures if policy requires it.
- Retain the final manifest with the domain job by default.
- Artifact garbage collection must verify that no Core manifest references the
  artifact before deletion.
- Deleting or expiring diagnostics never deletes the domain output artifacts
  referenced by successful outcomes.

## 14. Observability

Emit metrics for:

- Outcome rows by job type, status, and reason code.
- Fragment publication and reuse.
- Finalization duration and reconciliation failures.
- Artifact size and row count.
- Preview latency, row groups read, and bytes streamed.
- Manifest/artifact hash or identity mismatches.
- Legacy jobs without item diagnostics.

Logs include job, batch, manifest, and artifact IDs but not document contents.

## 15. Phased implementation

### Phase 0: Contract and registry

Deliver:

- Generic statuses and reason-code conventions.
- `JobOutcomeAdapter` interface and controlled job-type registry.
- Parquet v1 schema and metadata contract.
- Count-reconciliation and canonical-JSON helpers.
- Architecture tests that prevent Artifact from depending on Classification
  for the generic workflow-artifact type contract.

Exit when a fixture can validate and summarize a deterministic outcome table
without any embedding-specific code in the shared reader.

### Phase 1: Artifact platform generalization

Deliver:

- Neutral workflow artifact type contract.
- `JOB_ITEM_OUTCOME_FRAGMENT` and `JOB_ITEM_OUTCOMES` types.
- Artifact database migration and constraints.
- Media-type, size-limit, derivation, hash, scope, and lineage validation.
- Embedded and remote Artifact gateway support.
- Upgrade and downgrade tests.

Exit when Core can idempotently store, resolve, verify, and stream a generic
job-outcome Parquet artifact in both Artifact modes.

### Phase 2: Core manifest and bounded reader

Deliver:

- `job_outcome_manifest` Core model and migration.
- Manifest lifecycle and reconciliation service.
- Generic summary and item-page schemas.
- Matter-authorized read APIs.
- Bounded Parquet row-group reader with hash and metadata validation.
- Audit records and negative authorization tests.

Exit when a finalized fixture manifest can be summarized and paged without
loading or returning the complete artifact.

### Phase 3: Embedding outcome production

Deliver:

- Structured per-document embedding results.
- Separate generated, reused, skipped, failed, and canceled semantics.
- Stable embedding reason codes.
- Per-batch fragment publication.
- Deterministic final artifact construction.
- Finalization/reconciliation integration.
- Synchronous and Voyage execution-mode parity.

Exit when every document in a newly completed, partially failed, or canceled
embedding job has exactly one reconciled outcome row.

### Phase 4: Embedding diagnostics UI

Deliver:

- Linked counts in the matter Jobs table.
- Outcome summary and reason distribution.
- Status/reason filters and bounded item table.
- Authorized document links.
- Explicit download action.
- Link from the Classification embedding selector.
- User documentation and contextual help.

Exit when an administrator can explain every skipped or failed item from a new
embedding job without database, object-store, or worker-log access.

### Phase 5: Additional job adapters

Adopt one domain at a time. Recommended order:

1. Bulk coding/tagging.
2. Matter-document import.
3. Topic assignment.
4. Agent document processing.
5. CAL queue generation.

Classification scoring already has a complete prediction artifact. Integrate
it through an adapter or compatible shared read model rather than duplicating
its prediction rows into a second artifact.

Exit for each adapter only when its domain-specific counters reconcile exactly
to the generic final outcome rows.

### Phase 6: Hardening and retention

Deliver:

- Fragment retention and cleanup policy.
- Deep-page cursor if measured usage requires it.
- Integrity scanner for orphaned Core references or Artifact mismatches.
- Performance tests at million-row scale.
- Failure-injection tests around publication and finalization.
- Operational dashboards and alerts.

Exit when publication, reconciliation, preview, download, and retention remain
correct under retries, cancellation, worker loss, and remote Artifact mode.

## 16. Test strategy

### Contract tests

- Required columns, types, metadata, and schema version.
- Controlled statuses and reason codes.
- Canonical JSON and message-size limits.
- Exactly one row per population ordinal.
- Deterministic ordering and hashes.

### Producer tests

- New output maps to `SUCCEEDED`.
- Existing compatible vectors map to `REUSED` without provider inference.
- No text and empty chunks have distinct skip reasons.
- Preparation, provider, validation, and persistence failures have distinct
  failure reasons.
- Cancellation fills unstarted items with canceled outcomes.
- Fragment retries are idempotent.

### Finalization tests

- Missing, duplicate, or overlapping fragments fail closed.
- Status and reason counts reconcile exactly.
- Lost response after Artifact publication resolves the same derivation.
- Artifact scope, producer, hash, and embedded identity are verified.
- Core never finalizes a manifest with an incomplete artifact tuple.

### API and security tests

- Matter authorization for every endpoint.
- Cross-tenant, cross-client, cross-matter, and wrong-job-type access fails.
- Unsupported filters fail validation.
- Page limits are enforced.
- Corrupt, oversized, wrong-schema, and hash-mismatched artifacts fail closed.
- Browser responses never contain Artifact credentials or raw stack traces.

### UI tests

- Reused and skipped are distinct.
- Reason filters produce the correct request and rows.
- Partial legacy diagnostics are labeled accurately.
- Empty, loading, corrupt, and unavailable states are actionable.
- Document links preserve authorization boundaries.

## 17. MVP boundary

The MVP includes the generic foundation plus embedding production and viewing.

Included:

- Neutral Artifact contract.
- Core manifest table and bounded read APIs.
- Complete outcomes for new embedding jobs.
- Embedding diagnostics UI and download.
- Accurate generated/reused/skipped/failed counts.

Deferred:

- Exact backfill where historical item reasons were not persisted.
- A universal replacement for all domain job tables. The separate execution
  normalization effort adds common attempts but intentionally preserves domain
  records.
- Live streaming of individual outcomes while a job runs.
- Arbitrary analytics over outcome payload JSON.
- OpenSearch indexing of outcome rows.
- Cross-job analytics and comparisons.
- Non-embedding adapters beyond the first proven platform slice.

## 18. Recommended implementation order

Implement Phases 0 through 4 as one vertical slice before adapting other jobs.
The embedding workflow is large enough to validate scale, batching,
idempotency, cancellation, partial failure, Artifact ownership, bounded reads,
and UI diagnostics. Generalizing additional jobs before that slice works end to
end would risk freezing abstractions that have not yet been exercised.
