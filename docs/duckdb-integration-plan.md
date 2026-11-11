# Deferred DuckDB Integration Plan

## Status

Deferred. DuckDB is not required for the current matter size. The immediate implementation should keep the
inventory format engine-neutral so DuckDB can be introduced later without redesigning the workflow.

## 1. Adoption triggers

Do not add DuckDB until benchmarking shows one or more of these problems:

- inventory generation exceeds an acceptable duration;
- repeated population previews require rereading too much data;
- Python or Arrow processing consumes excessive worker memory;
- matter populations grow into hundreds of thousands or millions of documents;
- QC filtering and overlap analysis become difficult to maintain procedurally; or
- other workflows need the same analytical query capability.

Record baseline time, peak memory, artifact-read volume, and population size now so the decision is evidence-based.

## 2. Prepare compatible data contracts now

The QC inventory should remain immutable Parquet with a versioned schema. Avoid embedding logic that requires a
specific dataframe library.

The inventory contract should include:

- document and matter identifiers;
- source availability and content hash;
- record type;
- classifier statuses, values, probabilities, and threshold margins;
- Q&A coverage and coding state;
- human-coding state;
- QC eligibility flags and exception reasons; and
- stable provenance fields.

Summary and execution manifests should reference the inventory artifact by ID and content hash.

## 3. Initial DuckDB architecture

When justified, embed DuckDB in the durable workflow worker. Do not create a separate service initially.

```text
Artifact service
      ↓
Content-addressed local Parquet cache
      ↓
Embedded DuckDB workflow step
      ↓
Summary JSON and selected Parquet manifests
      ↓
Artifact service and PostgreSQL pointers
```

DuckDB should not become an authoritative datastore.

- PostgreSQL remains the control plane.
- Parquet remains the immutable document-level record.
- DuckDB is an ephemeral computation engine.

## 4. Artifact access

DuckDB should not receive unrestricted MinIO credentials.

The workflow should:

1. Resolve artifacts through the existing artifact gateway.
2. Download them to a bounded temporary or content-addressed cache.
3. Validate the expected content hash.
4. Query local Parquet files with DuckDB.
5. Upload generated Parquet through the artifact gateway.
6. Clean up temporary files according to a disk quota.

Missing optional source artifacts should become inventory exceptions. A missing inventory or classifier-scoring
artifact should fail the calculation with a clear provenance error.

## 5. Database-derived inputs

Do not use DuckDB's PostgreSQL extension.

The application should query authorized database records and expose the required data to DuckDB as:

- Arrow tables for smaller inputs; or
- temporary Parquet files for large inputs.

This applies to Q&A results, human decisions, document metadata, and source availability.

## 6. Query templates

Only application-owned, versioned SQL templates should execute. Users provide parameters, never SQL.

Initial templates would cover:

- population inventory summaries;
- near-threshold counts;
- record-type distributions;
- exception-reason counts;
- population overlap matrices;
- deterministic random sampling; and
- QC execution-manifest generation.

Every query execution records:

- SQL template version and hash;
- DuckDB version;
- input artifact hashes;
- parameters;
- output artifact hash; and
- execution statistics.

## 7. Deterministic sampling

Random samples must remain reproducible across reruns.

Use an explicitly ordered stable digest of:

```text
review_program_run_id + inventory_hash + document_id + sample_seed
```

Do not depend on an unspecified database sampling order or a potentially version-dependent internal hash function.

## 8. Phased rollout

### Phase A: benchmark spike

- Add DuckDB only in a development branch.
- Run the current Python or Arrow and DuckDB implementations against identical inventories.
- Compare counts, selected IDs, duration, memory, and artifact reads.
- Do not change production behavior.

### Phase B: workflow-only implementation

- Place DuckDB behind a feature flag.
- Use it only for durable inventory generation and manifest selection.
- Run both engines in shadow mode and compare logical output hashes.
- Continue using the current engine as authoritative.

### Phase C: controlled production rollout

- Enable DuckDB by matter or environment.
- Fall back to the existing implementation when DuckDB initialization fails.
- Alert on count or sample-selection differences.
- Record performance metrics.

### Phase D: interactive preview queries

Only after workflow execution is stable:

- cache inventory Parquet by content hash;
- use parameterized DuckDB queries for threshold and cohort previews;
- cache results by inventory hash and parameter hash; and
- keep artifact generation inside durable workflows.

## 9. Operational limits

Add explicit configuration for:

- DuckDB thread count;
- memory limit;
- temporary-directory size;
- maximum cached artifact size;
- maximum query duration;
- maximum output rows; and
- cache retention policy.

These limits should apply per worker process.

## 10. Testing and acceptance

Required tests include:

- DuckDB and current-engine count parity;
- identical deterministic sample membership;
- empty populations;
- missing and corrupt artifacts;
- multiple record types and overlapping eligibility;
- large inventories;
- memory and temporary-disk limits;
- tenant isolation;
- workflow retries and idempotency;
- DuckDB version upgrades; and
- feature-flag rollback.

DuckDB should become the default only after output parity is demonstrated and it provides a meaningful performance
or maintainability improvement.
