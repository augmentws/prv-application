# Collection Load Performance Implementation Plan

Status: Proposed

## 1. Outcome

Increase collection-ingestion throughput for pre-parsed datasets without bypassing Artifact Service
authorization, storage isolation, collection lifecycle rules, content deduplication, family integrity, or
idempotency.

The immediate target is to raise the observed EDRM Enron v2 load rate from approximately 13 documents
per second to at least 50 documents per second on the same local deployment and hardware. A later bulk
ingestion path should exceed 100 documents per second for representative small text documents while
preserving the same authoritative collection model.

This work is about collection loading. Matter publication, text processing, embedding, and OpenSearch
projection remain separate operations and are not included in the ingestion throughput measurement.

## 2. Current-state findings

The importer already preprocesses independent families concurrently, but the server serializes writes to
one collection:

1. `BaseImporter` assigns independent document families to a bounded thread pool.
2. Each document is sent as its own multipart request.
3. `upload_collection_item` selects the shared `client_collection` row with `FOR UPDATE`.
4. The exclusive collection-row lock remains held while the request validates metadata, hashes and stages
   content, writes a content object to S3-compatible storage, inserts collection and artifact records, and
   commits the database transaction.
5. Every worker loading the same collection therefore waits for the same exclusive row lock.

At 13 documents per second, the serialized section averages approximately 77 milliseconds per document.
Eight client workers cannot materially improve that ceiling while the exclusive lock covers the complete
write path.

Additional avoidable costs are:

- the upload response hydrates a complete collection item even though the importer needs only the item ID
  and whether it was created;
- an idempotent retry uploads and hashes the complete document before checking its `source_item_id`;
- the remote Artifact proxy creates a new HTTP client for every request instead of reusing connections;
- each document incurs an HTTP request, S3 object operation, database commit, and success log line;
- family members must be uploaded sequentially because the single-item API accepts a database parent ID
  instead of a source-item parent reference; and
- concurrent content deduplication relies on a unique database constraint but does not provide a clean
  conflict-recovery path for independently uploaded S3 objects.

The compound content-blob lookup already has the required unique `(tenant_id, sha256)` index. The
performance problem is not an absent deduplication index.

## 3. Goals

1. Permit independent documents and families in the same open collection to be ingested concurrently.
2. Preserve a strict lifecycle boundary: deletion or another exclusive collection transition must wait
   for admitted uploads, and no new upload may be admitted after the transition begins.
3. Preserve tenant/client authorization and custodian scope checks.
4. Preserve source-item idempotency and detect a source ID reused with different content.
5. Preserve content deduplication without leaking orphaned random S3 objects during races.
6. Preserve parent, child, and family consistency.
7. Provide a backward-compatible single-item API while adding an optimized importer contract.
8. Add enough timing and throughput telemetry to identify the next bottleneck with evidence.
9. Support safe restart and resume without transmitting content that the server can prove is already
   present and identical.

## 4. Non-goals

- The importer will not write directly to PostgreSQL or S3 while bypassing Artifact Service.
- This work will not parse PST, MBOX, or native office formats faster.
- This work will not combine collection ingestion with matter publication or search indexing.
- This work will not introduce packed multi-document storage objects or range-based document reads.
- This work will not expose object-store credentials or presigned evidence-upload URLs to ordinary users.
- This work will not weaken collection deletion, authorization, provenance, or evidence-integrity rules.
- This work will not require or automatically start a workflow worker.

## 5. Locked architectural decisions

### 5.1 Separate upload admission from exclusive collection transitions

Uploads use a shared PostgreSQL row lock on the collection. Deletion and any future seal/archive
transition retain an exclusive row lock.

```text
concurrent uploads
    -> SELECT collection ... FOR SHARE
    -> verify status is OPEN
    -> write content and metadata
    -> commit and release shared lock

collection deletion/transition
    -> SELECT collection ... FOR UPDATE
    -> wait for admitted uploads to commit
    -> change status
    -> commit and reject later uploads
```

PostgreSQL permits multiple `FOR SHARE` holders while an exclusive `FOR UPDATE` transition waits for
them. This retains the lifecycle gate without forcing uploads to run one at a time.

Content staging and hashing should happen before acquiring the collection lock. After staging completes,
the request acquires the shared lock and rechecks that the collection is still open. S3 publication and
the authoritative database transaction occur only after admission. This prevents slow request-body reads
from extending the lifecycle lock while still preventing deletion from racing an admitted upload.

### 5.2 Make content addressing safe before enabling concurrent writes

New content blobs use a deterministic object key derived from the SHA-256 digest inside the tenant's
private bucket. Existing opaque object keys remain valid and require no migration.

Content-blob creation uses an upsert or equivalent conflict recovery around the unique
`(tenant_id, sha256)` constraint. Two concurrent uploads of identical bytes may put the same deterministic
object safely; exactly one database content-blob row becomes authoritative and the other request reuses
it. A database conflict must not leave a unique random object that cannot be reclaimed.

The implementation must verify that an existing content blob agrees on byte length. Media-type behavior
must be documented because identical bytes may arrive under different declared media types.

### 5.3 Add a minimal ingestion contract without breaking the existing upload API

Keep the existing single-item upload endpoint and complete response for compatibility. Extract its write
logic into a service that can also back a new importer-focused endpoint returning only:

```json
{
  "item_id": "uuid",
  "source_item_id": "stable source ID",
  "content_sha256": "sha256",
  "created": true
}
```

The importer switches to the minimal contract. The server must not re-query custodians, recipients,
artifact scope, and the complete collection item merely to construct this response.

### 5.4 Add a family-aware bulk endpoint after the concurrent single-item path is stable

The bulk contract accepts family-coherent batches and identifies parents with `parent_source_item_id`, not
a database UUID. The server resolves existing parents and preallocates IDs for all new items before
inserting children.

Initial limits are configurable and begin conservatively:

- at most 500 documents per batch;
- at most 32 MiB of uncompressed document content per batch; and
- bounded concurrent S3 operations no higher than the configured storage connection pool.

The transport uses one manifest plus one streamed archive or equivalent bounded multipart bundle. It must
not base64-encode every document into JSON. Archive handling must reject absolute paths, parent-directory
traversal, duplicate member names, undeclared members, unsupported entry types, and content exceeding the
declared or configured limits.

The batch is metadata-atomic. Validation or database failure does not leave a partially committed family.
Deterministically addressed content may remain after a failed transaction and can be safely claimed by a
retry or later storage reconciliation.

### 5.5 Keep bulk ingestion opt-in until benchmark and soak gates pass

Add an importer mode such as `--ingest-mode single|bulk`. The optimized single-item path remains the
initial default. Bulk becomes the default only after correctness, restart, failure-isolation, and sustained
load tests pass.

## 6. Target ingestion flow

```text
dataset adapter
    -> normalize documents and family/source identifiers
    -> compute content hashes while reading
    -> optional batched preflight for existing source IDs and hashes
    -> build bounded family-coherent batches
    -> upload several batches concurrently

Artifact ingestion service
    -> authenticate and authorize collection/client/custodian scope
    -> stage and validate bounded content
    -> acquire shared collection admission lock and require OPEN
    -> resolve existing source IDs and validate hashes in one query
    -> preallocate IDs and resolve parent source references
    -> publish missing deterministic content blobs with bounded concurrency
    -> bulk insert items, custodians, email rows, recipients, artifacts, and links
    -> commit once
    -> return minimal ordered results
```

## 7. Phase 0: establish a repeatable baseline

Create a benchmark command or script that uses the real importer and API rather than calling internal
database functions. It must support both embedded and remote Artifact modes and record:

- document count, family count, and total bytes;
- document-size and recipient-count distributions;
- worker and batch configuration;
- created versus existing items;
- documents and MiB per second;
- request latency p50, p95, and p99;
- time spent reading/preprocessing, waiting for an upload slot, and awaiting the API;
- server time spent staging/hashing, waiting for the collection lock, querying, writing S3, flushing,
  committing, and constructing the response;
- PostgreSQL connection-pool use and lock waits; and
- S3 connection-pool use and request failures.

Use a deterministic representative fixture with emails, standalone files, and families containing
attachments. Keep large benchmark outputs and evidence content out of Git. Commit only the generator,
configuration, summary format, and a short checked-in baseline report.

Run the baseline on the same deployment used for the observed EDRM load and record whether Artifact mode
is embedded or remote. The currently observed 13 documents per second is evidence, but not a replacement
for a reproducible benchmark.

**Exit criteria:** the benchmark reproduces a stable throughput range across three runs and phase timing
identifies the exclusive collection lock as the dominant wait.

## 8. Phase 1: concurrent and lean single-item ingestion

Implement one safe vertical slice containing all of the following:

1. Move request-body staging and hashing before collection admission.
2. Add explicit shared and exclusive collection lock modes.
3. Use the shared lock for ingestion and keep the exclusive lock for deletion and lifecycle mutation.
4. Implement deterministic content object keys and conflict-safe content-blob upsert/recovery.
5. Extract collection-item write logic from the FastAPI endpoint into a testable service.
6. Add the minimal single-item ingest endpoint and regenerate OpenAPI/client artifacts.
7. Switch the import client to the minimal response.
8. Replace per-document success output with periodic aggregate progress while retaining immediate warnings
   and failures.
9. Add structured phase timings and aggregate throughput reporting.

The Artifact database currently uses SQLAlchemy's default connection-pool sizing. Eight upload workers fit
within its default pool plus overflow, but connection-pool settings should become explicit and observable
before increasing concurrency substantially. Storage concurrency must remain at or below
`ARTIFACT_STORAGE_MAX_POOL_CONNECTIONS`.

**Exit criteria:**

- eight concurrent uploads to one collection are observed in the server rather than queued on an
  exclusive collection lock;
- a deletion request waits for admitted uploads, marks the collection `DELETING`, and causes later uploads
  to fail cleanly;
- concurrent identical content produces one authoritative content-blob row and one deterministic object;
- source-item and family idempotency tests pass;
- legacy upload responses remain compatible; and
- the representative benchmark sustains at least 50 created documents per second on the baseline local
  environment without elevated error rates.

## 9. Phase 2: remote proxy connection reuse

When `ARTIFACT_MODE=remote`, replace the request-scoped `httpx.AsyncClient` with a lifespan-managed client
and an explicitly sized connection pool. Preserve streaming request and response bodies, delegation-token
generation, hop-by-hop header filtering, and shutdown cleanup.

Benchmark embedded and remote modes separately. Remote mode should not open a new TCP connection for each
document and should remain bounded under backpressure.

**Exit criteria:** connection reuse is visible in transport metrics or traces, remote-mode throughput is no
longer dominated by connection setup, and shutdown closes the pooled client cleanly.

## 10. Phase 3: family-aware bulk ingestion

Add the bulk endpoint and importer transport described above.

Server work for one batch is:

1. Validate the complete manifest and content bundle before mutation.
2. Authorize every custodian reference in one pass.
3. Acquire the shared collection admission lock and require `OPEN`.
4. Fetch all existing source IDs, native hashes, parents, and relevant family state in set-based queries.
5. Reject source IDs whose stored content hash differs from the manifest.
6. Allocate UUIDs for all new items and resolve parent source IDs without client round trips.
7. Publish missing content objects with bounded concurrency and conflict-safe deduplication.
8. Insert metadata with SQLAlchemy Core multi-row statements or another set-based method.
9. Commit once and return ordered minimal results.

The importer keeps each family in one batch. Oversized single families receive a documented fallback that
uploads the parent first and splits children across subsequent batches while retaining the same family ID.

With `--continue-on-error`, a rejected batch is divided on family boundaries to isolate the failing family.
Successful families must not be replayed unnecessarily. The final report lists failed source IDs and the
server error without silently omitting documents.

**Exit criteria:**

- batch retry is idempotent after a client disconnect occurring before, during, and after server commit;
- parent/child relationships are correct for new, existing, and mixed families;
- duplicate source IDs and conflicting hashes produce deterministic errors;
- bounded memory and archive-security tests pass;
- database statements and commits per document are materially lower than the single-item path; and
- the representative benchmark sustains at least 100 created documents per second on the baseline local
  environment.

## 11. Phase 4: efficient restart and resume

Extend the existing source-ID resolver or add an import preflight endpoint that accepts up to 5,000
source IDs with client-computed hashes and returns:

- missing source IDs;
- exact existing matches;
- hash conflicts;
- item IDs; and
- existing parent and family state required for safe continuation.

The importer computes SHA-256 while reading documents for batch construction. It does not transmit exact
existing matches. Hash conflicts stop or enter the existing `--continue-on-error` report; they are never
silently treated as successful.

Persist an optional local checkpoint containing only non-sensitive operational state such as collection
ID, adapter identity, source fingerprint, last completed source container, counts, and batch IDs. Do not
store access tokens, passwords, or document content in the checkpoint.

**Exit criteria:** restarting a substantially completed import transfers only missing content, detects a
changed source document, and produces the same final collection as an uninterrupted import.

## 12. Database and API considerations

### 12.1 Database indexes

Retain and verify these access paths with `EXPLAIN (ANALYZE, BUFFERS)` on a realistically sized test
collection:

- unique `(collection_id, source_item_id)` on collection items;
- unique `(tenant_id, sha256)` on content blobs;
- parent collection-item lookup;
- native artifact lookup by collection item and role; and
- recipient and custodian inserts used by the bulk path.

Do not remove collection browsing/search indexes merely to improve ingestion without measuring their read
and write value. Bulk commits should amortize index maintenance before any index redesign is considered.

### 12.2 Transaction boundaries

Do not hold an exclusive collection lock during network I/O. Shared admission locks may cover S3 writes so
that an exclusive deletion transition waits for admitted uploads. A transaction must not report success
before all authoritative rows are committed.

Deterministic content objects decouple recoverability from a single database transaction. A failed
transaction may leave an unreferenced deterministic object, but a retry can safely reuse it. Periodic
storage reconciliation may remove objects that remain unreferenced beyond a conservative grace period.

### 12.3 Compatibility

- Existing `items:upload` callers continue to receive the full response.
- New single and bulk ingest operations receive new OpenAPI operation IDs.
- Generated clients are updated in the same changeset as the OpenAPI document.
- A deployment may run the old importer against the compatible endpoint during rollout.
- The bulk importer detects endpoint availability and fails clearly; it must not silently downgrade after
  partially sending a batch.

## 13. Correctness and failure testing

Add tests for:

1. Concurrent independent uploads to the same collection.
2. Uploads to different collections in the same tenant.
3. Identical content uploaded concurrently under different source IDs.
4. Identical source IDs uploaded concurrently with equal and unequal content.
5. Collection deletion beginning before staging, while uploads hold shared locks, and after commit.
6. Parent and children created together, parent already existing, child already existing, and conflicting
   family metadata.
7. Client disconnect before S3 write, after S3 write, before commit, and after commit.
8. S3 timeout, database deadlock/serialization failure, uniqueness race, and retry.
9. Malformed manifests, archive traversal, duplicate archive members, incorrect declared hashes, and size
   limits.
10. Access-token expiry and reauthentication during single and bulk imports.
11. Embedded and remote Artifact modes.
12. Legacy upload response compatibility.
13. Import resume with exact matches, missing items, and hash conflicts.

PostgreSQL concurrency semantics must be tested against PostgreSQL, not inferred solely from SQLite tests.
S3 concurrency and retry behavior should have MinIO integration coverage.

## 14. Performance test matrix

Measure at least these cases:

| Case | Content profile | Family profile | Purpose |
| --- | --- | --- | --- |
| Small text | 5-25 KiB | Standalone | Fixed per-document overhead |
| Email | 10-100 KiB | Parent only | Email metadata and recipients |
| Email families | 10-250 KiB/item | Parent plus 1-10 children | Relationship handling |
| Duplicate content | 5-100 KiB | Mixed | Deduplication contention |
| Existing import | Same as above | Mixed | Resume/idempotency cost |
| Medium files | 1-16 MiB | Mixed | Byte throughput and memory bounds |

Run worker counts of 1, 4, 8, 16, and 32 only where the database and storage pools are configured to
support them. Report throughput together with error rate, latency percentiles, database connections,
collection-lock wait, S3 latency, CPU, memory, and disk/network utilization. Do not present a worker count
as faster unless sustained throughput improves without destabilizing the APIs.

CI correctness tests must not use fragile wall-clock performance assertions. Keep the full throughput gate
as a repeatable local or dedicated-environment benchmark and record its environment with each result.

## 15. Observability and operator experience

The importer prints a progress summary at a fixed interval, for example every five seconds:

```text
created=42,100 existing=0 failed=0 rate=68.4 docs/s transferred=1.8 GiB in_flight=8
```

The final report includes:

- elapsed time and average document/byte throughput;
- created, existing, skipped, and failed counts;
- request and batch latency percentiles;
- configured worker, batch, and byte limits;
- retries and reauthentication events; and
- collection ID and adapter identity.

Server telemetry distinguishes staging, admission-lock wait, S3, database flush/commit, and response time.
Logs use aggregate summaries and sampled slow operations rather than one success line per document. Failures
retain source IDs but must not log document content, credentials, or sensitive metadata.

## 16. Rollout and rollback

1. Deploy server instrumentation and the backward-compatible minimal endpoint.
2. Deploy concurrency-safe deterministic blob creation.
3. Enable shared-lock single-item ingestion and run the benchmark plus a representative soak import into a
   disposable collection.
4. Compare item, artifact, blob, custodian, email, recipient, parent, and family counts with the source
   manifest.
5. Roll out the optimized single-item importer.
6. Deploy bulk ingestion as opt-in and repeat correctness, failure, restart, and soak tests.
7. Make bulk the default only after the acceptance gates pass.

Rollback from the optimized single-item path means returning the importer to the legacy endpoint. Existing
deterministically addressed blobs remain valid. Rollback from bulk mode means selecting single mode; no
database downgrade or evidence rewrite should be required.

Do not test a rollback by deleting a collection containing the only copy of evidence. Use disposable test
collections and retain source manifests and checksum reports.

## 17. Acceptance criteria

The project is complete when:

1. The exclusive collection-row lock no longer serializes admitted uploads.
2. Deletion and lifecycle transitions remain race-safe under concurrent load.
3. Concurrent deduplication cannot create conflicting content-blob rows or unrecoverable random objects.
4. Existing single-item API clients remain compatible.
5. The importer can use a minimal single-item response and a family-aware bulk path.
6. Restart skips server-confirmed exact matches and detects hash conflicts.
7. The final collection reconciles exactly to the source manifest for items, families, custodians, hashes,
   and relationships.
8. The optimized single-item path sustains at least 50 created documents per second on the recorded
   baseline environment.
9. The bulk path sustains at least 100 created documents per second on the same environment.
10. No benchmark run shows unresolved upload failures, database pool exhaustion, S3 pool exhaustion,
    deadlocks, or collection lifecycle violations.
11. Documentation describes operational settings, progress output, resume behavior, limits, and failure
    recovery.

## 18. Delivery slices

### Pull request 1: measurement and safe concurrent single ingestion

- Benchmark harness and baseline report format.
- Phase timing and aggregate progress.
- Deterministic/concurrency-safe content blobs.
- Shared upload admission and exclusive lifecycle locking.
- Minimal ingest response and importer support.
- PostgreSQL and MinIO concurrency tests.

### Pull request 2: remote transport and resume preflight

- Lifespan-managed remote Artifact HTTP client.
- Explicit HTTP, database, and storage pool metrics/configuration.
- Source-ID/hash preflight.
- Resume behavior and reporting.

### Pull request 3: family-aware bulk ingestion

- Bounded manifest/archive protocol.
- Set-based resolution and inserts.
- Family-aware batching and failure isolation.
- Bulk importer mode and integration tests.
- Throughput and soak acceptance report.

### Pull request 4: default enablement and documentation

- Reconciliation tooling and final operational documentation.
- Default-mode decision based on benchmark evidence.
- Removal of temporary rollout switches that are no longer needed.
