# Search Result CSV Export Implementation Plan

Status: MVP implemented (Artifact expiration cleanup and scale/remote integration testing remain rollout work)

Related documents:

- `docs/search-index-lifecycle.md`
- `docs/unified-job-execution-platform-plan.md`
- `docs/job-outcome-manifest-platform-plan.md`
- `web/content/docs/matter-search.mdx`

## 1. Outcome

Allow an authorized user to export all documents matched by the current matter
search, choose the ordered fields included in the output, and download a CSV
after a durable background job completes.

The intended flow is:

```text
current Search & Review query
    -> choose ordered export fields
    -> create durable export job immediately
    -> pin physical search-index generation and field snapshots
    -> DBOS opens a point-in-time search view
    -> stream deterministic pages into a temporary CSV
    -> publish immutable CSV to Artifact storage
    -> download through an authorized Core endpoint
```

PostgreSQL is the control plane. It stores the job, frozen query, selected-field
snapshot, workflow attempts, progress, output reference, retention state, and
audit events. It must not store one export row or one export membership row per
document. Artifact storage owns the generated CSV bytes.

## 2. Locked decisions

1. Search export is a first-class matter job with job type
   `MATTER_SEARCH_EXPORT`.
2. Creating the job is synchronous only through validation and persistence. It
   returns a queued job and never waits for OpenSearch enumeration or CSV
   generation.
3. DBOS owns execution, retry, cancellation, reconciliation, and progress.
4. The job freezes the complete search request with UI pagination and facets
   removed. It exports the complete matched population, not only the visible
   result page.
5. The job pins the active physical `search_index_generation`, its index name,
   generation number, schema hash, and activation timestamp. It never resolves
   the mutable alias while executing.
6. A point-in-time (PIT) view is opened when the workflow starts. The completed
   file is internally consistent with that PIT even when documents are indexed
   while the export runs. The UI must say "snapshot started" rather than imply
   that the snapshot was taken at the instant the user clicked Export.
7. Deep keyword pagination uses PIT plus `search_after`; it does not use the
   API's `offset <= 10,000` interactive-search path. OpenSearch recommends PIT
   with `search_after` for a fixed view and deep pagination.
8. Every export sort is deterministic. Preserve the requested sort and append
   `document_id ASC` as a stable tie-breaker. A query without a requested sort
   uses `_score DESC, document_id ASC`; a match-all request uses
   `document_id ASC`.
9. Semantic and hybrid exports preserve the search's bounded candidate set.
   They do not claim to export every potentially similar document. The dialog
   displays the pinned candidate limit before submission.
10. The worker requests only the selected `_source` paths plus identity fields.
    It removes highlights, facets, inner hits, chunks, and embeddings unless a
    selected export column explicitly requires a supported source field.
11. The initial export catalog contains built-in document fields and active
    metadata fields present in the pinned search projection. A field that was
    never projected into the index is shown as unavailable rather than emitted
    as a silently empty column.
12. The selected field specification is snapshotted by stable field ID/key,
    display label, type, cardinality, source path, and enum label map. Later
    field renames or enum relabeling do not rewrite an existing job.
13. CSV column order is the user's selected order. Headers use frozen display
    names. Duplicate display names receive a deterministic `(<stable key>)`
    suffix.
14. Enum values are exported with their frozen display labels. The job retains
    the stable keys in its field snapshot for provenance.
15. Multiple values occupy one CSV cell as a compact JSON array. This is
    lossless when values contain commas, semicolons, quotes, or newlines.
16. Null values produce empty cells. Boolean, numeric, date, and datetime
    values retain their types through deterministic text formatting; datetimes
    use ISO 8601 with an offset.
17. CSV generation uses a versioned dialect: UTF-8, comma delimiter, CRLF row
    terminators, RFC-compatible quoting, and a documented spreadsheet-formula
    injection policy. The policy version is frozen on the job.
18. A selected document-text column is allowed only as an explicit opt-in and
    carries a large-output warning. Nested chunks, embeddings, internal batch
    projection objects, and hierarchy implementation rows are never export
    columns.
19. The CSV is an immutable `SEARCH_RESULT_EXPORT` workflow artifact scoped to
    the matter. Core stores its artifact ID, hash, byte length, row count, and
    filename.
20. Downloads are authorized again at request time. An artifact ID is never a
    bearer credential.
21. Export creation and download require a new dangerous matter-scoped
    `document.export` permission in addition to ordinary matter/document view
    access.
22. Export request, completion, download, cancellation, retry, and soft deletion
    are audited. Audit details identify the job, matter, counts, field IDs, and
    output hash without copying the potentially sensitive query text.
23. A row-conversion failure fails the export. The workflow does not silently
    skip documents or create a "completed with errors" CSV whose population is
    ambiguous.
24. Retrying a failed export reuses the frozen query, generation, and field
    snapshot but creates a new `WorkflowRun` attempt and DBOS workflow ID.
25. Completed output is immutable. Running the same search again creates a new
    export job because its PIT and source state may differ.

The PIT and deep-pagination design follows the OpenSearch documentation for
[Point in Time](https://docs.opensearch.org/latest/search-plugins/searching-data/point-in-time/),
[pagination](https://docs.opensearch.org/latest/search-plugins/searching-data/paginate/),
and [hybrid `search_after`](https://docs.opensearch.org/latest/vector-search/ai-search/hybrid-search/search-after/).

## 3. Scope

### In scope

- Keyword, semantic, and hybrid matter-search requests already supported by
  Search & Review.
- Exporting the complete matched result set represented by that request.
- Ordered field selection and frozen display labels.
- Durable progress, cancellation, retry, failure reporting, and soft deletion.
- Artifact-backed CSV generation and authorized streaming download.
- A **Search exports** job type on the matter Jobs tab.
- Output retention and cleanup policy.

### Out of scope for the first slice

- Native-file, family ZIP, load-file, DAT, image, or production exports.
- Exporting a hand-selected set of result rows.
- Scheduled or emailed exports.
- Combining multiple matters in one export.
- User-authored calculated columns.
- Parquet or XLSX as user-facing formats.
- Direct browser access to the Artifact Service or object-store credentials.
- Field-level security rules that do not yet exist elsewhere in the platform.
- Snapshotting non-indexed imported metadata through one Artifact call per
  document. A later batched Artifact metadata API can extend the catalog.

## 4. User experience

### 4.1 Search & Review

Add **Export results** to the Search & Review header. Enable it when:

- a search response has completed;
- the result count is greater than zero;
- the user has `document.export`; and
- the active search-index generation is ready.

The export dialog shows:

- exact current result count or bounded semantic/hybrid candidate count;
- search mode and a warning for approximate/bounded retrieval;
- a searchable field picker grouped into **Document fields** and the existing
  matter metadata groups;
- selected-field order with drag or move-up/move-down controls;
- a default selection such as Control number, Original filename, Record type,
  Custodian, Email subject, and Matter document ID;
- a warning when Document text or long-text fields are selected;
- the retention period; and
- **Create export**.

The dialog submits the currently executed search state, not unsubmitted text in
the query input. On success it closes immediately, shows a queued toast, and
offers **View export job**.

### 4.2 Jobs tab

Add **Search exports** to the existing Job type selector. Each row shows:

- requested filename/name;
- status and current stage;
- exported rows over matched rows;
- selected field count;
- creator;
- created/completed timestamps;
- output byte size and expiration when complete; and
- the shared job action control.

Actions are:

- **Download CSV** for a completed, unexpired artifact;
- **Cancel** for queued or running jobs;
- **Retry** for retryable failed jobs while the pinned generation is retained;
- **View error** for failures; and
- **Delete job** for terminal jobs using the existing soft-delete convention.

The browser does not poll the artifact. It polls the job record or common job
execution projection and enables download only after finalization.

## 5. Export field contract

Add a server-owned field catalog rather than letting the browser invent
OpenSearch `_source` paths.

Conceptually:

```text
SearchExportField
  id                         stable built-in key or metadata definition UUID
  stable_key
  display_name
  group_id nullable
  group_display_name nullable
  source_path
  value_type
  cardinality
  selectable
  unavailable_reason nullable
  large_value
  enum_values               ordered key/display-label snapshot
```

Built-in initial columns should include:

- Matter document ID
- Collection item ID
- Source collection ID
- Date added to matter
- Record type
- Processing status
- Original filename
- Source path
- Family ID
- Custodian IDs
- Custodian names
- Email from, to, cc, bcc, and subject
- Document text, marked as a large-value field

Active metadata definitions are selectable only when the pinned index schema
contains their projected `metadata.<stable_key>` path. JSON fields and internal
nested projection structures are not selectable.

The create endpoint resolves selected IDs through the catalog and freezes an
ordered `field_snapshot`. It must reject unknown, duplicated, unavailable, or
unauthorized fields and enforce configurable limits such as a maximum of 250
columns.

### 5.1 Value serialization

Use one code-owned serializer registry keyed by the frozen type and
cardinality:

| Value | CSV representation |
| --- | --- |
| null | empty cell |
| text/long text | exact UTF-8 text with CSV quoting |
| integer/decimal | locale-independent decimal text |
| boolean | `true` or `false` |
| date | `YYYY-MM-DD` |
| datetime | ISO 8601 including offset |
| enum | frozen display label |
| multiple values | compact JSON array in source order |

The CSV safety policy must be explicit. Recommended MVP behavior is to prefix
textual scalar cells beginning with `=`, `+`, `-`, or `@` with an apostrophe
when `spreadsheet_safe=true`. Typed negative numbers and JSON-array cells are
not formulas and must not be modified. Freeze `spreadsheet_safe` and
`csv_policy_version` on the job so the transformation is reviewable. A future
raw-data export can be a separate, more-restricted policy rather than an
undocumented toggle.

## 6. Core schema and migration

Add `matter_search_export_job`:

```text
id
tenant_id
matter_id
search_index_generation_id nullable -> search_index_generation.id SET NULL

search_definition                 canonical JSON without offset/size/facets
search_definition_hash
search_index_snapshot             generation, physical index, schema hash,
                                  activation timestamp
field_snapshot                    ordered compact JSON
field_snapshot_hash
csv_policy_snapshot               compact JSON
idempotency_key
requested_filename

status                            QUEUED | RUNNING | COMPLETED | FAILED | CANCELED
current_workflow_run_id nullable -> workflow_run.id
workflow_id                       current DBOS workflow ID
current_stage nullable

matched_count
exported_count
page_count
output_artifact_id nullable
output_content_hash nullable
output_byte_length nullable
output_filename nullable
snapshot_started_at nullable
expires_at nullable
retry_until nullable

failure_code nullable
failure_summary nullable
failure_retriable
created_by_user_id
started_at nullable
completed_at nullable
canceled_at nullable
created_at
updated_at
```

Constraints and indexes:

- counts and byte length are nonnegative;
- output fields are populated together only for `COMPLETED`;
- one current workflow run belongs to one job;
- indexes on `(matter_id, created_at)`, `(matter_id, status, created_at)`,
  `created_by_user_id`, `output_artifact_id`, and
  `search_index_generation_id`;
- the physical generation snapshot remains in JSON after the nullable foreign
  key is cleared by generation cleanup.

Do not add a document-membership or export-row table.

Add `document.export` to the code-owned permission manifest and a dedicated
incremental RBAC migration. Grant it to Root, Tenant, Client, and Matter
Administrator system roles. Custom roles must opt in.

Register `MATTER_SEARCH_EXPORT` with:

- `WorkflowRun` and the common job execution adapter;
- common cancel/retry/history endpoints;
- `JobArchive` soft deletion; and
- the matter Jobs list.

## 7. Artifact Service changes

The existing workflow-artifact type is currently coupled to the classification
enum. Refactor it into a neutral shared workflow-artifact contract, retaining
the existing classification values, then add:

```text
artifact_type: SEARCH_RESULT_EXPORT
scope_kind: MATTER
media_type: text/csv
producer_kind: CORE
```

Add a configurable maximum byte size for search exports. Validation must accept
CSV only for this artifact type and must reject empty output, mismatched length,
or mismatched SHA-256.

The artifact metadata includes:

```text
contract_version: search-result-export-v1
export_job_id
matter_id
search_index_generation
search_index_schema_hash
search_definition_hash
field_snapshot_hash
csv_policy_version
row_count
column_count
snapshot_started_at
```

Generalize the Core gateway name `store_classification_workflow_artifact` so
search export does not call a classification-named API. Preserve the existing
derivation-key conflict and reconciliation behavior.

Update Artifact download filename selection to use `.csv` for `text/csv`.
Expose a streaming Core gateway for workflow-artifact content; downloading a
large CSV must not first load it into API memory.

## 8. Search execution contract

### 8.1 Canonical search snapshot

At creation:

1. Validate the ordinary matter search request.
2. Strip `offset`, `size`, and requested facets.
3. Freeze query, mode, similarity threshold, candidate limit, query fields,
   filters, and sort.
4. Resolve and pin the active physical generation.
5. Resolve the export field snapshot against that generation's schema.
6. Calculate canonical search, field, and policy hashes.

For semantic or hybrid mode, resolve the effective candidate limit before the
job is created. Do not allow worker-side defaults to change later.

### 8.2 Export query compiler

Add a dedicated `compile_search_export_request` that reuses ordinary filter and
query compilation but removes UI-only response work:

- no facets or aggregations;
- no highlighting;
- no inner hits;
- no `from`;
- bounded page `size` from configuration;
- exact `_source.includes` for selected paths and identity;
- `track_total_hits=true` on the first page;
- deterministic sort with `document_id` tie-breaker; and
- PIT plus `search_after` cursor after the first page.

This compiler must remain behaviorally consistent with the interactive query
compiler. Shared query/filter clauses should be factored once and covered by
equivalence tests.

### 8.3 Generation retention

`SearchIndexManager._cleanup_obsolete_generations` currently protects selected
nonterminal assessment runs. Extend it to protect generations referenced by:

- queued or running search export jobs; and
- retryable failed export jobs until a short `retry_until` deadline.

Completed and canceled jobs no longer require the physical index because their
CSV is immutable. A failed job whose generation has passed its retry deadline
becomes nonretryable with `SEARCH_EXPORT_GENERATION_UNAVAILABLE`; the user must
create a new export against the current index.

## 9. DBOS workflow

Register queues such as:

```text
matter-search-export-plans       low bounded concurrency
matter-search-export-runtime     lower global concurrency than interactive work
```

The workflow stages are:

```text
QUEUED
  -> PREPARING
  -> EXPORTING
  -> UPLOADING
  -> FINALIZING
  -> COMPLETED
```

### Step 1: prepare

- Load and lock the job.
- Reauthorize the initiating user for `document.export` and `document.view`.
- Verify the matter, physical generation, schema hash, and selected fields.
- Transition the common execution to `RUNNING/PREPARING`.
- Open a PIT on the pinned physical index and record `snapshot_started_at`.
- Create a task-specific temporary file; never use a broad or user-controlled
  filesystem path.

The PIT ID is ephemeral workflow state. Do not expose it in API responses or
audit details.

### Step 2: stream CSV

- Write the frozen headers.
- Execute deterministic pages against the PIT.
- Convert and write one row at a time with bounded memory.
- Refresh the PIT keep-alive on every page.
- Update `matched_count`, `exported_count`, `page_count`, heartbeat, and common
  execution progress after each successful page.
- Check cancellation between pages.
- Enforce configured row, column, runtime, temporary-disk, and output-byte
  limits.
- Fail if OpenSearch times out or reports partial shard failure.

The first implementation may use one retriable DBOS step for the streaming
loop. A process failure restarts that step from the beginning with a new PIT;
no incomplete file is published. This trades repeated OpenSearch reads for a
simple, bounded-memory, correct MVP and avoids a Core row per page. If exports
later exceed practical single-step runtimes, add immutable CSV-part artifacts
and a compose phase rather than PostgreSQL document rows.

### Step 3: publish and reconcile

- Close the PIT in a `finally` path.
- Flush the file and compute SHA-256 and exact byte length.
- Upload as `SEARCH_RESULT_EXPORT` using a derivation key containing the job ID,
  workflow attempt, search hash, field hash, and CSV policy version.
- If upload success is ambiguous, find the artifact by derivation key and
  verify type, scope, producer, run ID, content hash, row count, and snapshots.
- A retry must adopt a verified existing artifact rather than create a second
  logical output for the attempt.

### Step 4: finalize

- Persist output artifact reference, content hash, length, row count, filename,
  and expiration.
- Transition both domain job and common execution atomically to `COMPLETED`.
- Append `search_export.completed` audit data.
- Remove the temporary file in all success, failure, and cancellation paths.

### Failure and retry policy

Retryable failures include transient OpenSearch transport errors, PIT expiry
during an otherwise valid attempt, temporary Artifact Service failures, and a
worker interruption. A step retry may rebuild the unpublished file from the
beginning.

Nonretryable failures include invalid frozen field contracts, schema-hash
mismatch, revoked authorization, configured output limit exceeded, missing
pinned generation after its retention boundary, and deterministic value
serialization failure.

Cancellation is cooperative at page and upload boundaries. If cancellation
races with an already successful artifact upload, reconciliation decides
whether the job completed before cancellation; it must not expose a partially
finalized result.

## 10. API contracts

Add:

```text
GET    /v1/matters/{matter_id}/search-export-fields
POST   /v1/matters/{matter_id}/search-exports
GET    /v1/matters/{matter_id}/search-exports
GET    /v1/matters/{matter_id}/search-exports/{export_id}
GET    /v1/matters/{matter_id}/search-exports/{export_id}/download
```

Creation request:

```json
{
  "search": { "...": "MatterSearchRequest" },
  "field_ids": ["control_number", "<metadata-definition-uuid>"],
  "spreadsheet_safe": true,
  "requested_filename": "responsive-documents.csv",
  "idempotency_key": "client-generated-uuid"
}
```

Creation returns `202 Accepted` with the queued export job. Reusing an
idempotency key for the same user, matter, canonical search, and field snapshot
returns the existing job; conflicting reuse returns `409`.

The download endpoint:

- requires the completed, unexpired job and current `document.export` access;
- verifies the Artifact scope and stored hash;
- records `search_export.downloaded` before streaming;
- streams from Artifact storage without buffering the full file;
- sets `Content-Type: text/csv; charset=utf-8`, safe `Content-Disposition`,
  `Content-Length`, and `ETag`; and
- returns `410 Gone` when output retention has expired while leaving the job's
  audit/provenance record intact.

Use the common job endpoints for cancel, retry, execution history, and soft
delete after registering `MATTER_SEARCH_EXPORT`.

## 11. Authorization, privacy, and audit

Authorize at four boundaries:

1. field-catalog retrieval;
2. job creation;
3. workflow execution/retry; and
4. every download.

Required permission: `document.export`. Ordinary `document.view` remains a
prerequisite. Revoking access after generation prevents future downloads even
though the artifact remains retained.

Audit actions:

```text
search_export.requested
search_export.completed
search_export.failed
search_export.canceled
search_export.retried
search_export.downloaded
job.deleted                       existing generic action
```

Avoid logging the full search query, CSV cells, PIT ID, object key, or delegated
Artifact credentials. Structured logs should include tenant, matter, export
job, workflow attempt, DBOS workflow ID, stage, counts, duration, byte length,
and failure code.

## 12. Retention and operational limits

Recommended defaults, all configurable:

- page size: 1,000 documents;
- PIT keep-alive refresh: 10 minutes per page;
- maximum selected columns: 250;
- completed artifact retention: 7 days;
- retryable-generation retention after failure: 24 hours;
- maximum output bytes: deployment-specific, enforced before publication;
- low global export concurrency so exports cannot starve interactive search;
- one active export per user per matter initially, with a clear `429` or `409`
  response when exceeded.

Add metrics for jobs queued/running/completed/failed/canceled, documents and
bytes exported, duration, page latency, OpenSearch retries, PIT failures,
Artifact upload latency, download count, and expired artifacts.

Artifact expiration should remove content through an Artifact-owned cleanup
operation and clear or mark the Core output as expired. Soft-deleting a job
hides it but does not bypass retention, destroy audit records, or immediately
delete an artifact that another active download is using.

## 13. Test plan

### Unit tests

- Canonical search snapshot strips only UI pagination/facets.
- Interactive and export compilers produce equivalent query/filter clauses.
- Deterministic sort always includes a unique tie-breaker.
- Field catalog resolves schema paths and reports unavailable fields.
- Field snapshots remain stable across definition relabeling.
- Every value type and cardinality serializes deterministically.
- CSV quoting covers commas, quotes, CR/LF, Unicode, nulls, and large text.
- Spreadsheet-safe policy covers formula-leading scalar and array values.
- Duplicate headers are disambiguated deterministically.

### Workflow tests

- More than 10,000 keyword hits export through PIT/`search_after` without gaps
  or duplicates.
- Concurrent index updates do not alter an open PIT's exported population.
- Requested sort order is preserved across page boundaries.
- Cancellation stops at a page boundary and publishes no partial artifact.
- Worker failure retries from the beginning and publishes exactly one artifact.
- Ambiguous upload success reconciles the existing artifact.
- Transient OpenSearch and Artifact failures are retryable.
- Schema mismatch, revoked authorization, and output limits are nonretryable.
- Generation cleanup retains active/retryable export generations and releases
  them after completion or retry expiry.

### API and authorization tests

- Users without `document.export` cannot list fields, create, inspect, or
  download exports.
- Cross-tenant and cross-matter IDs do not disclose job or artifact existence.
- Idempotency replay and conflict behavior.
- Download streams bytes and emits correct length, ETag, filename, and audit.
- Expired output returns `410`; archived jobs disappear from normal lists.
- Cancel, retry, history, and delete use the common job framework.

### Frontend tests

- The dialog uses the executed search rather than unsaved query text.
- Field search, grouping, selection order, defaults, and large-value warning.
- Semantic/hybrid candidate-limit warning.
- Job creation returns immediately and links to Search exports.
- Jobs show progress and only enable Download after completion.
- Shared action control exposes Download, error, retry, and delete correctly.

### Scale test

Seed at least 300,000 indexed documents and verify bounded worker memory,
stable API latency, exact row count, no duplicate document IDs, cancellation
latency within one page, successful multi-gigabyte streaming, and no
per-document Core rows.

## 14. Phased implementation

### Phase 1: contracts and persistence

1. Add `document.export` and its incremental RBAC migration.
2. Add `matter_search_export_job` and schemas.
3. Register `MATTER_SEARCH_EXPORT` with `WorkflowRun`, job history, soft delete,
   cancel, and retry.
4. Add the field catalog and creation/list/detail endpoints.

Exit criterion: an authorized request creates a queued, provenance-complete job
without starting synchronous export work.

### Phase 2: generic Artifact support

1. Neutralize the classification-owned artifact enum boundary.
2. Add `SEARCH_RESULT_EXPORT`, CSV validation, limits, and `.csv` download
   naming.
3. Add generic store/find/verify and streaming-content gateway functions.
4. Add artifact contract and authorization tests in embedded and remote modes.

Exit criterion: Core can idempotently store, reconcile, and stream a scoped CSV
workflow artifact without buffering the download.

### Phase 3: search iterator and CSV engine

1. Add PIT create/search/close methods to `OpenSearchClient`.
2. Factor shared query/filter compilation and add the export compiler.
3. Implement deterministic `search_after` paging and candidate-limit handling.
4. Implement field resolution, enum labels, multi-value JSON, and CSV safety.
5. Protect pinned generations from cleanup.

Exit criterion: a service test exports more than 10,000 results exactly once in
bounded memory from a fixed PIT.

### Phase 4: DBOS workflow and operations

1. Add prepare, export, publish/reconcile, finalize, fail, and cancel steps.
2. Add dispatcher registration and worker imports.
3. Project progress into `WorkflowRun` and the domain job after each page.
4. Add retry classifications, audit events, cleanup, metrics, and structured
   logs.

Exit criterion: API or worker interruption cannot produce a completed partial
file or duplicate logical artifact.

### Phase 5: Search & Review UI

1. Add Export results and the ordered field-selection dialog.
2. Add exact/bounded population language and long-text warnings.
3. Submit the executed search and navigate to the queued job.
4. Add generated OpenAPI client updates and component tests.

Exit criterion: a user can create an export without leaving Search & Review and
is never held behind a synchronous request spinner.

### Phase 6: Jobs and download UI

1. Add Search exports to the Jobs type selector.
2. Add progress, counts, size, expiration, creator, and timestamps.
3. Add authorized Download CSV and shared cancel/retry/error/delete actions.
4. Add expired-output and revoked-access states.

Exit criterion: the full export lifecycle is visible and controllable from the
Jobs tab.

### Phase 7: retention, documentation, and rollout

1. Add Artifact-owned expiration cleanup and operational metrics.
2. Document export semantics, candidate limits, CSV encoding, field
   availability, permissions, and retention in `matter-search.mdx` and
   `matter-jobs.mdx`.
3. Run scale, cancellation, remote-Artifact, and index-rebuild concurrency
   tests.
4. Roll out behind a feature flag and initially cap concurrency at one.

Exit criterion: operators can size temporary disk/object storage, diagnose
failures, and safely enable the feature per environment.

## 15. MVP acceptance criteria

- An authorized user can select ordered fields and queue a CSV export from the
  current executed matter search.
- The create request returns immediately with a durable job.
- A 300,000-document keyword result exports beyond the interactive 10,000-hit
  window without omissions or duplicates.
- The output corresponds to one PIT and pinned physical generation.
- Progress, cancel, retry, failure, download, expiration, and delete are visible
  through the common Jobs experience.
- The CSV contains one header and one row per matched document, with stable
  field labels and deterministic value serialization.
- Core has no export membership or export-value row per document.
- CSV bytes live only in Artifact storage and download streams in bounded
  memory after fresh authorization.
- Every create, completion, download, cancellation, retry, and deletion is
  auditable.
