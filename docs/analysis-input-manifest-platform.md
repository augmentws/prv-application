# Generic Analysis Input Manifest Platform

## 1. Purpose

Analysis jobs need a durable answer to four questions before provider- or
model-specific execution begins:

1. Which documents were selected?
2. In which stable order were they selected?
3. Which resolved inputs and immutable Artifact references were supplied?
4. Which exact input contract and resolver produced those rows?

The generic Analysis Input Manifest platform answers those questions without
creating one new Core database row per document for every analysis run. Core
stores one `analysis_input_manifest` control record. Artifact stores the full,
immutable `ANALYSIS_INPUT_MANIFEST` Parquet artifact.

```text
Analysis domain job or snapshot
    -> Core analysis_input_manifest
        -> Artifact ANALYSIS_INPUT_MANIFEST Parquet
```

The first adopter is statistical classification. New classification training
and scoring snapshots use the generic manifest for their frozen population.
Question-answering Analysis Tasks are intentionally unchanged and remain a
future adopter.

## 2. Responsibilities and boundaries

### 2.1 Core owns

- Authorization to select the matter or review-batch population.
- Resolution and precedence rules for logical document inputs.
- The frozen input contract and resolver version.
- Manifest lifecycle, owner identity, scope, counts, hashes, and audit events.
- The reference to the Artifact-owned Parquet file.
- Domain-job linkage, status, progress, cancellation, and retry behavior.

### 2.2 Artifact owns

- Immutable Parquet storage.
- Tenant/client scope enforcement.
- Derivation-key idempotency.
- Content-hash, media-type, byte-length, and artifact-type validation.
- Artifact lineage and storage lifecycle.

### 2.3 A task-specific runtime owns

- Validation of its declared input contract before execution.
- Reading only the rows and columns it needs.
- Provider- or model-specific evaluation.
- Its result and outcome artifacts.

The runtime must not rediscover the population or silently resolve a newer
version of mutable Core metadata.

## 3. Core data model

`analysis_input_manifest` contains one row per logical owner:

- Tenant, client, and matter scope.
- `owner_type` and `owner_id`.
- Population `scope_type` and `scope_id`.
- Manifest schema name and version.
- Resolver version.
- Frozen input contract and its SHA-256 hash.
- Derivation key.
- Artifact ID, content hash, and byte length.
- Item count and manifest status.
- Producer identity, initiating user, timestamps, and failure summary.

`(owner_type, owner_id)` is unique. A retry for the same logical owner must
produce the same frozen identity. Reusing an owner with different content,
scope, contract, resolver, or producer identity is a conflict rather than a new
manifest.

Statuses are:

- `BUILDING`: the Core identity exists but final Artifact publication is not
  yet committed.
- `FINALIZED`: the complete Artifact tuple and finalization timestamp exist.
- `FAILED`: publication failed and the bounded failure summary was retained.

Consumers may execute only a `FINALIZED` manifest.

## 4. Artifact contract

The Artifact type is `ANALYSIS_INPUT_MANIFEST`, media type
`application/vnd.apache.parquet`, schema `analysis-input-manifest`, version 1.
The Artifact is scoped to the Core manifest through:

```text
core_scope_kind = ANALYSIS_INPUT_MANIFEST
core_scope_id   = analysis_input_manifest.id
```

The Artifact derivation key includes the owner, scope, schema, contract hash,
and content hash. Publication is therefore retry-safe. An ambiguous failure
after the Artifact was stored can be reconciled by rerunning the same publish
operation.

### 4.1 Required Parquet columns

Every row contains:

| Column | Meaning |
| --- | --- |
| `matter_document_id` | Stable Core document identity. |
| `collection_item_id` | Artifact collection-item identity. |
| `source_collection_id` | Collection from which the matter document was published. |
| `scope_ordinal` | Positive, unique ordering within the frozen population. |
| `input_status` | `READY`, `MISSING`, `FAILED`, or `EXCLUDED`. |
| `failure_code` | Stable reason for every non-ready row; null for ready rows. |
| `source_artifact_id` | Optional immutable primary text/content artifact. |
| `source_content_hash` | Optional SHA-256 hash for the primary source artifact. |
| `resolved_inputs_json` | Canonical JSON object of resolved logical input values. |
| `artifact_references_json` | Canonical JSON list of other immutable Artifact references. |
| `input_content_hash` | SHA-256 over the complete canonical logical row. |

Keeping non-ready rows in the manifest preserves population reconciliation:
the manifest item count continues to equal the selected population even when a
document lacks a required input.

### 4.2 Contract extension columns

A task may declare typed extension columns when a high-volume consumer needs a
value as a first-class Parquet column. Version 1 supports `STRING`, `INT64`,
`FLOAT64`, and `BOOLEAN`. Extension names, types, and nullability are frozen in
the input contract. Every row must supply exactly the declared extension set.

Use extension columns for values needed for column pruning, grouping, joins, or
model execution. Ordinary resolved metadata belongs in
`resolved_inputs_json`; do not add a new physical column for every metadata
definition.

Classification currently declares:

- `family_id`
- `exact_duplicate_group_id`
- `record_type`

These preserve leakage-safe grouping behavior while allowing classification to
consume the generic manifest directly.

### 4.3 Required Parquet metadata

The file footer freezes:

- Manifest schema and version.
- Contract key, version, canonical contract, and contract hash.
- Resolver version.
- Scope type and scope ID.

Core verifies this metadata, the Artifact identity, the complete content hash,
the required columns, declared extensions, and the exact row count before
returning a table to a consumer.

## 5. Input contract

Every producer creates an `AnalysisInputContract` containing:

- A stable, lowercase contract key.
- A positive contract version.
- A resolver version.
- Required logical input paths.
- Whether a source artifact is mandatory.
- Typed extension fields.
- Bounded task-specific options.

Contract changes that can change any resolved row require a new contract or
resolver version. Never change the meaning of a published version in place.

The contract is canonicalized and hashed. The hash participates in both the
Core manifest identity and Artifact derivation key.

## 6. Producer workflow

Every future document-oriented Analysis Task must use this sequence:

1. **Authorize the owner and scope.** Verify tenant, client, matter, task
   version, selected review batch, and initiating user.
2. **Pin task configuration.** Freeze the task version, provider/model version,
   requested input paths, and task-specific policy before population discovery.
3. **Freeze the population.** Resolve matter or review-batch membership in a
   deterministic order. Do not rely on a later mutable search query.
4. **Resolve inputs.** Apply the shared Core document-input resolver in bounded
   pages. Store scalar values or immutable Artifact references; do not put large
   document bodies or embedding vectors in this manifest.
5. **Represent every selected document.** Emit `READY`, `MISSING`, `FAILED`, or
   `EXCLUDED`. Do not silently drop documents.
6. **Prepare the Parquet artifact.** Call
   `prepare_analysis_input_manifest(...)` with the frozen contract, scope, and
   rows.
7. **Publish and finalize.** Call
   `publish_analysis_input_manifest(...)`. This creates or verifies the single
   Core control row, idempotently publishes the Artifact, verifies its returned
   identity, and records the audit event.
8. **Attach the manifest to the domain job.** Store the Core manifest ID on the
   owning task run, dataset snapshot, or other domain object.
9. **Execute only from the manifest.** Provider-specific workers must not rerun
   population selection or re-resolve mutable inputs.
10. **Publish results separately.** Input manifests are not result manifests or
    job-outcome manifests.

The manifest owner must be the logical run or immutable snapshot, not an
individual DBOS attempt. A retry creates a new execution attempt but reuses the
same manifest identity when its frozen inputs are unchanged.

## 7. Consumer workflow

Before using a manifest, a consumer must verify:

1. Core status is `FINALIZED`.
2. Tenant, client, matter, owner, and scope match the domain job.
3. Artifact type, Core scope, ID, derivation key, content hash, and byte length
   match the Core row.
4. Parquet footer schema, schema version, contract hash, resolver version, and
   scope match Core.
5. Required base and extension columns exist.
6. Parquet row count equals `item_count`.
7. The task supports the pinned contract version.

`read_analysis_input_manifest_table(...)` implements these checks for current
in-process consumers. Large future runtimes should use the same verification
rules with bounded Parquet row-group iteration rather than loading the complete
table.

## 8. Classification integration

New classification training and scoring dataset snapshots now:

1. Resolve matter or review-batch membership and grouping data.
2. Build the generic analysis input manifest.
3. Store its Core ID in
   `classification_dataset_snapshot.analysis_input_manifest_id`.
4. Retain `population_artifact_id` and `population_content_hash` as compatibility
   aliases to the generic Artifact so existing model-build and API contracts do
   not require a flag-day migration.
5. Read and verify the generic manifest before feature or split materialization.

Classification label, feature, split, prediction, evaluation, and result
artifacts remain domain-specific. They are not analysis input manifests.

Snapshots created before this migration have no `analysis_input_manifest_id`.
The feature reader retains a legacy, read-only
`CLASSIFICATION_POPULATION` compatibility path so historical model versions and
completed runs remain reproducible. New snapshots always use the generic type.

## 9. Relationship to other manifests

Do not conflate these artifacts:

| Contract | Purpose |
| --- | --- |
| Analysis input manifest | Frozen population and resolved task inputs. |
| Domain result manifest | Typed answers, predictions, metrics, or other business output. |
| Job outcome manifest | Per-item operational status, reason, retryability, and output references. |

A large Analysis Task normally has all three. They may share document IDs and
hash provenance, but each has a separate lifecycle and schema.

## 10. Scaling and current limitations

- Parquet rows are compressed and written with bounded 5,000-row groups.
- Core stores one manifest control row, not one manifest row per document.
- Batch membership remains in `review_batch_document`; the manifest is a frozen
  execution input, not a replacement for interactive batch state.
- Large source text and vectors remain in their own Artifact files and are
  referenced rather than copied into the generic manifest.
- Classification population discovery still resolves its current population
  and grouping records before calling the generic writer. A later optimization
  may page that producer without changing the manifest contract.
- The current Core reader materializes a complete Arrow table. Remote and
  million-document consumers should add a verified bounded row-group reader
  using the same contract before adopting the platform.
- Q&A still uses `review_batch_run_document` and resolves document text and
  metadata at per-document execution time. It has not been migrated by this
  implementation.

## 11. Adoption checklist for a future Analysis Task

A new task type is complete only when it has:

- A stable input-contract key and version.
- A versioned resolver with documented precedence rules.
- Deterministic matter/batch population ordering.
- Explicit behavior for missing and failed inputs.
- One row for every selected document.
- Immutable source Artifact IDs and hashes where source content is required.
- A Core manifest link on its domain run or immutable snapshot.
- DBOS steps for population freeze and manifest finalization.
- Idempotent retry and reconciliation tests.
- Cross-tenant, wrong-matter, wrong-owner, hash-mismatch, schema-mismatch, and
  row-count-mismatch tests.
- Cancellation checks at bounded producer/consumer boundaries.
- A separate result contract and job-outcome contract.
- Documentation of retained artifacts and deletion/retention behavior.

The task must not introduce its own population-Parquet format unless the generic
base schema cannot express a proven requirement. In that case, extend and
version this platform rather than silently forking it.
