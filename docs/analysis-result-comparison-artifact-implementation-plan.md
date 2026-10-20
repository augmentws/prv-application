# Analysis Result Comparison Artifact Implementation Plan

Status: batch-scoped interactive comparison and disagreement child-batch creation implemented; durable stored-artifact workflow proposed.

The first delivered slice lives in **Batch statistics → Compare coding**. It
compares two to six existing `review_batch_run` sources in bounded database
chunks, provides count/agreement/document views, and exports CSV or Parquet on
demand without adding per-document comparison rows or requiring OpenSearch.
The delivered batch-scoped path can create a `COMPARISON_RESULT` child batch by
replaying pinned human reviewer revisions and immutable evaluation runs against
the ready parent batch. The persisted comparison control-plane record, dedicated
DBOS comparison workflow, immutable Artifact-owned outputs, and reference metrics
described below remain the scale-out phase for repeated whole-matter comparisons.

Related documents:

- `docs/review-batches.md`
- `docs/classification-coding-projection-implementation-plan.md`
- `docs/statistical-classification-backend-implementation-plan.md`
- `docs/system-one-decision-engine-plan.md`

## 1. Outcome

Provide a durable way to compare classifier, Jev, agent, and human coding over
one explicit frozen population without creating a PostgreSQL row for every
document or document/class pair.

The initial workflow is:

```text
Jev first-pass coding
    -> statistical-model training
    -> whole-matter classifier scoring
    -> frozen review batch sampled from classifier results
    -> blind Jev review of that batch
    -> durable comparison artifact
    -> disagreement or adjudication batch
```

PostgreSQL is the control plane. It stores comparison identity, authorization,
source provenance, status, compact aggregate counts, artifact references, and
audit metadata. Immutable Parquet artifacts store document-level comparison
results and detailed class metrics.

## 2. Locked decisions

1. A comparison always has an explicit frozen cohort. The initial supported
   cohort is a ready review batch.
2. The classifier source may be a whole-matter `classification_scoring_run`.
   Its comparison rows are restricted to the cohort; it does not need to be
   projected into `review_batch_run_value` first.
3. Jev, human, agent, and workflow coding sources are identified by immutable
   `review_batch_run` IDs.
4. The complete classifier prediction artifact remains authoritative for
   classifier probabilities and resolution status.
5. Comparison artifacts are immutable. Changing either source, cohort,
   destination field, class universe, reference policy, or comparison policy
   creates a new comparison.
6. No per-document comparison table is added to Core for the MVP.
7. PostgreSQL stores top-line and compact per-class summaries only. Large
   document details remain in Artifact-owned Parquet.
8. Missing, failed, excluded, abstained, and not-evaluated are distinct states.
   None is silently converted into a negative class decision.
9. Multi-value coding is compared as a set. The artifact retains exact match,
   overlap, left-only values, right-only values, and Jaccard similarity.
10. Agreement is the default interpretation. Precision, recall, F1, and
    confusion measures are shown only when the user designates one side as a
    reference.
11. Jev-referenced metrics are labeled as such and are not called ground-truth
    accuracy. Only an adjudicated human source may be presented as a gold
    reference.
12. Documents used to train the classifier are excluded from formal validation
    metrics by default. If included for diagnostic purposes, training and
    held-out metrics remain visibly separated.
13. A classifier-positive-only cohort can measure confirmation or positive
    predictive value, but cannot estimate recall. The platform must state this
    limitation and recommend a negative sample.
14. Creating a review batch from comparison results is explicit and auditable.
    The resulting `review_batch_document` rows are intentional operational
    materialization, not comparison storage.
15. OpenSearch is not required for comparison computation or the first UI.
    It is considered only if comparison predicates later need to compose with
    ordinary matter search at interactive scale.

## 3. Existing platform boundary

The existing endpoint:

```text
GET /v1/matters/{matter_id}/review-batches/{batch_id}/comparisons
```

loads two sets of `review_batch_run_value` rows into API memory and returns
field-level match counts. It is useful for small, same-batch comparisons but
does not provide durable provenance, classifier scoring-artifact access,
document-level disagreement output, confidence analysis, asynchronous status,
or artifact-backed scale.

The new comparison module does not immediately remove that endpoint. It adds a
durable path for classifier/Jev and large comparisons. Once the new UI no
longer depends on the synchronous endpoint, the endpoint can either remain a
lightweight diagnostic or be deprecated.

Existing components to reuse:

- `classification_scoring_run` and its immutable prediction Parquet artifact.
- `review_batch`, `review_batch_run`, `review_batch_run_document`, and
  `review_batch_run_value`.
- Artifact gateway authorization, content-hash verification, and immutable
  upload contracts.
- DBOS dispatcher and durable workflow patterns.
- Existing PyArrow Parquet readers used by classification scoring and
  classifier projection.
- Audit records and matter-level authorization.

## 4. Comparison semantics

### 4.1 Cohort

The MVP comparison cohort is one ready review batch. The cohort provides:

- Exact document membership.
- Stable matter ownership.
- Coding-field snapshots and display labels.
- A reproducible target for later disagreement batching.

A classifier scoring run may cover a superset, including the entire matter.
The comparison worker verifies that every cohort document occurs exactly once
in the classifier scoring artifact and ignores non-cohort predictions.

### 4.2 Source contract

Each side normalizes to this logical shape:

```text
matter_document_id
source_status
selected_value_keys
class_scores optional
top_confidence optional
probability_margin optional
abstained
excluded
failed
source_provenance
```

Supported initial sources:

- `CLASSIFICATION_SCORING_RUN`
- `REVIEW_BATCH_RUN`

`REVIEW_BATCH_RUN` covers Jev/workflow, human, agent, and classifier projection
runs. The source snapshot records the run type so the UI can apply accurate
labels.

### 4.3 Class universe

The comparison freezes ordered class keys and display labels from the
destination metadata definition and the batch coding-field snapshot. Source
keys must map to that frozen universe. Unknown keys block the comparison
instead of being silently discarded.

For `MULTIPLE` fields, absence of one class from an otherwise successful,
complete coding result is an explicit negative for that class. An entirely
missing or incomplete result is not a negative for every class.

### 4.4 Metrics

Always calculate:

- Cohort size.
- Evaluated by both.
- Left only and right only.
- Failed, excluded, abstained, missing, and incomplete counts by side.
- Exact-set agreement.
- Partial-set agreement.
- Mean and distribution of Jaccard similarity.
- Per-class both-positive, left-only, right-only, and both-negative counts for
  documents successfully evaluated by both sides.

When a reference side is selected, calculate:

- True positive, false positive, false negative, and true negative.
- Precision, recall, specificity, F1, and support.
- Macro and micro aggregates where meaningful.
- Classifier confidence bands versus reference confirmation.

The UI must distinguish symmetric agreement from reference-based quality.

### 4.5 Validation warnings

Persist warnings rather than hiding methodological limitations:

- `TRAINING_POPULATION_OVERLAP`
- `PREDICTED_POSITIVE_COHORT_ONLY`
- `NO_NEGATIVE_SAMPLE`
- `INCOMPLETE_REFERENCE_COVERAGE`
- `CLASS_NOT_OBSERVED_IN_COHORT`
- `SOURCE_FAILURE_RATE_HIGH`
- `CONFIDENCE_NOT_COMPARABLE_ACROSS_SOURCE_TYPES`

Warnings appear in both the preview and completed comparison.

## 5. Step 1: Schema and migration

Add `analysis_result_comparison` as the control-plane record.

Recommended columns:

```text
id
tenant_id
matter_id
review_batch_id
metadata_definition_id

left_source_kind
left_review_batch_run_id nullable
left_classification_scoring_run_id nullable
right_source_kind
right_review_batch_run_id nullable
right_classification_scoring_run_id nullable

reference_side              NONE | LEFT | RIGHT
class_universe_snapshot     compact JSON
comparison_policy           compact JSON
comparison_policy_hash
source_snapshot             compact JSON
derivation_key
idempotency_key

status                      QUEUED | RUNNING | COMPLETED |
                            COMPLETED_WITH_WARNINGS | FAILED | CANCELED
current_stage
progress_completed
progress_total

cohort_document_count
both_evaluated_count
exact_match_count
partial_match_count
left_only_count
right_only_count
abstained_count
failed_count
summary                     compact JSON
warnings                    compact JSON

manifest_artifact_id nullable
manifest_content_hash nullable
document_artifact_id nullable
document_content_hash nullable
class_metrics_artifact_id nullable
class_metrics_content_hash nullable

workflow_id
failure_code nullable
failure_summary nullable
failure_retriable
created_by_user_id
started_at
completed_at
canceled_at
created_at
updated_at
```

Database constraints:

- Each source side has exactly one source foreign key matching its kind.
- Sources, field, batch, and comparison all belong to the same matter and
  tenant; service validation enforces cross-table ownership.
- Left and right sources cannot identify the same immutable run.
- `reference_side` is `NONE`, `LEFT`, or `RIGHT`.
- Counts are non-negative and progress is internally consistent.
- `(matter_id, idempotency_key)` is unique.
- `(matter_id, derivation_key)` is unique for immutable-result reuse.
- Workflow ID is unique.

Do not add `analysis_result_comparison_document` or document/class rows.

Migration requirements:

- Existing batch runs and comparisons remain unchanged.
- Migration upgrade and downgrade are tested in an isolated database.
- The API schema and generated TypeScript client are regenerated.

Exit criterion: Core can represent a comparison and all of its provenance
without storing document-level output.

## 6. Step 2: Request, preview, and derivation contracts

Add a preview request containing:

```text
review_batch_id
metadata_definition_id
left_source
right_source
reference_side
exclude_training_documents default true
confidence_bins optional
```

The preview performs bounded database and artifact-metadata validation. It
returns:

- Resolved source labels and versions.
- Cohort size.
- Source coverage known from persisted summaries.
- Frozen class universe.
- Training-overlap policy.
- Expected artifact inputs.
- Blocking reasons and methodological warnings.

The creation request adds an idempotency key and a preview token. The token
hashes all inputs that can change the result.

The derivation key includes:

```text
comparison contract version
batch ID and membership hash
metadata-definition and field-snapshot hash
left source kind, ID, and immutable content hash
right source kind, ID, and immutable content hash
ordered class universe
reference side
training exclusion policy
confidence-bin policy
```

Changing presentation-only labels does not change the result. Changing source
data, cohort membership, class mapping, or metrics policy does.

Proposed endpoints:

```text
POST /v1/matters/{matter_id}/analysis-comparisons/preview
POST /v1/matters/{matter_id}/analysis-comparisons
GET  /v1/matters/{matter_id}/analysis-comparisons
GET  /v1/matters/{matter_id}/analysis-comparisons/{comparison_id}
POST /v1/matters/{matter_id}/analysis-comparisons/{comparison_id}/cancel
```

Creation commits the queued record and DBOS dispatch atomically, then returns
`202 Accepted`. The API request does not download or join source artifacts.

Exit criterion: invalid source/cohort combinations fail before workflow
dispatch, and identical immutable requests reuse the existing comparison.

## 7. Step 3: Parquet and manifest contracts

### 7.1 Document comparison artifact

Write one row per cohort document:

```text
schema_version
comparison_id
matter_document_id
collection_item_id nullable
cohort_ordinal

left_status
right_status
left_values                 list<string>
right_values                list<string>
matched_values              list<string>
left_only_values            list<string>
right_only_values           list<string>

exact_match                 boolean nullable
jaccard_similarity          float nullable
agreement_bucket

left_top_confidence         float nullable
right_top_confidence        float nullable
left_probability_margin     float nullable
right_probability_margin    float nullable
left_class_scores           list<struct<class_key, probability, selected>> nullable
right_class_scores          same shape nullable

classifier_training_member  boolean nullable
metric_eligible             boolean
metric_exclusion_reason     string nullable
```

`agreement_bucket` uses a bounded vocabulary:

- `EXACT_MATCH`
- `PARTIAL_MATCH`
- `LEFT_ONLY`
- `RIGHT_ONLY`
- `BOTH_EMPTY`
- `ABSTAINED`
- `FAILED`
- `INCOMPLETE`

Arrays are stored in frozen class order. This preserves a compact one-row-per-
document layout rather than expanding every document across every class.

### 7.2 Class metrics artifact

Write one row per class and optional confidence bin:

```text
class_key
display_label
support
both_positive_count
left_only_count
right_only_count
both_negative_count
precision nullable
recall nullable
specificity nullable
f1 nullable
confidence_bin_lower nullable
confidence_bin_upper nullable
bin_document_count nullable
reference_confirmation_rate nullable
```

Small matters may also retain a compact copy of per-class totals in the
database summary. The Parquet artifact remains authoritative if the summary is
truncated by a configured size limit.

### 7.3 Manifest

Publish a JSON manifest containing:

- Contract and schema versions.
- Comparison ID and derivation key.
- Source IDs, types, content hashes, and frozen provenance.
- Cohort and field identities.
- Ordered class universe.
- Comparison policy.
- Artifact references and SHA-256 hashes.
- Row counts and aggregate reconciliation totals.
- Creation timestamp and producer version.

All artifacts are immutable and Artifact-owned. Core stores references and
hashes, not object paths or signed URLs.

Exit criterion: a completed comparison can be independently reproduced and
verified from its manifest and immutable sources.

## 8. Step 4: Durable DBOS comparison workflow

Implement `analysis_result_comparison_v1`.

### Stage 1: authorize and freeze

1. Reauthorize the initiating user for the matter, batch, source runs, model
   results, and destination field.
2. Lock the comparison row.
3. Verify the batch is ready and its membership hash is unchanged.
4. Verify both sources remain terminal and immutable.
5. Move the comparison to `RUNNING`.

### Stage 2: load the cohort

1. Stream batch membership in stable sequence order.
2. Build a bounded cohort-ID set or sorted Arrow table.
3. Resolve the frozen coding-field and class-label snapshot.
4. Load the classifier training population only when training-overlap analysis
   is requested.

### Stage 3: normalize sources

For a classifier source:

1. Stream the scoring prediction Parquet through the Artifact gateway.
2. Verify its stored content hash and embedded run/model identities.
3. Read only cohort rows and required columns.
4. Preserve success, abstained, excluded, and failed states.

For a review-batch source:

1. Stream run-document statuses and typed run values in chunks.
2. Group values by cohort document and target field.
3. Distinguish completed empty coding from missing or incomplete coding.
4. Preserve confidence and confidence kind without assuming calibration
   equivalence across source types.

### Stage 4: compare and aggregate

1. Join normalized sources by matter document ID.
2. Calculate document-level set relationships and status buckets.
3. Exclude training documents from formal metrics when configured.
4. Calculate per-class agreement and optional reference metrics.
5. Calculate confidence-bin confirmation where the classifier supplies
   probabilities.
6. Reconcile every aggregate to the cohort and eligible-document counts.

### Stage 5: upload and finalize

1. Write document and class-metric Parquet with deterministic schemas and row
   group sizes.
2. Upload both artifacts.
3. Verify returned artifact IDs, hashes, media types, and row counts.
4. Publish the manifest last.
5. Store artifact references and compact summaries in Core.
6. Mark the comparison `COMPLETED` or `COMPLETED_WITH_WARNINGS`.
7. Write a security audit record.

Every stage is idempotent. A retry reuses validated artifacts when their hashes
match the derivation, and never appends duplicate database detail rows.

Exit criterion: worker interruption at any stage can be retried without
duplicating artifacts or losing a terminal diagnostic.

## 9. Step 5: Detail-query API

The browser never downloads the complete Parquet artifact.

Add:

```text
POST /v1/matters/{matter_id}/analysis-comparisons/{comparison_id}/documents/search
GET  /v1/matters/{matter_id}/analysis-comparisons/{comparison_id}/classes
```

Document search supports:

- Agreement bucket.
- Class key.
- Left or right selected value.
- Classifier confidence range.
- Exact versus partial match.
- Source status.
- Training-member and metric-eligible flags.
- Stable cohort-ordinal pagination.

Use the existing PyArrow dependency and Artifact gateway first. Read only the
necessary columns and row groups, with bounded page sizes and worker memory.
Add DuckDB only after measurement shows that PyArrow predicate handling is
insufficient; do not add a second execution engine speculatively.

For repeated interactive use, a bounded server cache may retain verified
artifact metadata or local read handles. Cache identity includes artifact ID
and content hash, and cache eviction never affects correctness.

Exit criterion: the UI can page and filter comparison details without loading
the artifact into the browser or materializing document rows in PostgreSQL.

## 10. Step 6: Comparison UI

Add **Compare results** to completed Analysis Task and review-batch runs.

### Creation dialog

The dialog selects:

- Frozen review batch.
- Destination coding field.
- Left and right sources.
- Reference interpretation: peer agreement, left reference, or right
  reference.
- Training-document exclusion policy.

Show preview blockers and warnings before creation. Source labels use human
names plus run date/version; raw UUIDs remain secondary provenance.

### Comparison page

Provide:

1. **Overview** — cohort, coverage, exact/partial agreement, missing and failure
   states, training overlap, and warnings.
2. **Classes** — per-class counts, agreement, and reference metrics.
3. **Confidence** — classifier confidence bands versus confirmation rate.
4. **Documents** — paginated exact matches, partial matches, left-only,
   right-only, abstentions, and failures.

For multi-value fields, display metadata value labels as the primary text and
retain stable keys as provenance/tooltips.

The page explicitly labels limited designs, for example:

> This cohort contains classifier-selected positives only. It can measure
> confirmation of predicted positives but cannot estimate classifier recall.

Exit criterion: a matter manager can understand coverage and disagreement
without interpreting raw Parquet or database identifiers.

## 11. Step 7: Create review batches from comparison results

Implementation status: the existing Batch Statistics comparison can create a
durable child batch from all partial and complete disagreement rows. The broader
artifact-backed predicates, class/confidence filters, and deterministic sampling
below remain proposed. Each current child batch also has a dedicated review
panel that replays every pinned source for one document and lets the reviewer
save the correct value through immutable child-batch reviewer coding; explicit
adjudication status and publication remain future work.

Add:

```text
POST /v1/matters/{matter_id}/analysis-comparisons/{comparison_id}/review-batches/preview
POST /v1/matters/{matter_id}/analysis-comparisons/{comparison_id}/review-batches
```

Selection supports:

- Exact or partial disagreements.
- Classifier-only positives.
- Jev/reference-only positives.
- Abstained, failed, or incomplete rows.
- Class and confidence filters.
- Optional deterministic random sample and seed.

Introduce a review-batch selection provenance type such as
`COMPARISON_RESULT`. Its snapshot stores:

- Comparison ID and manifest hash.
- Artifact ID and content hash.
- Selection predicate.
- Requested size and random seed.
- Result count.

The normal durable batch builder reads matching document IDs from the verified
comparison artifact and creates ordinary `review_batch_document` membership.
Those rows are required for operational review and are not duplicated in the
comparison record.

Exit criterion: a manager can turn a reproducible disagreement stratum into an
adjudication batch without exporting and reimporting document lists.

## 12. Step 8: Tests and verification

### Contract and unit tests

- Canonical source normalization for classifier and review-batch runs.
- Single-, multi-, and no-value comparisons.
- Exact and partial set agreement.
- Missing versus explicit empty coding.
- Abstained, excluded, failed, and incomplete status handling.
- Training-overlap exclusion.
- Per-class and aggregate metric reconciliation.
- Confidence-bin boundary behavior.
- Deterministic derivation and idempotency keys.

### API and authorization tests

- Cross-matter, cross-tenant, and inaccessible-source rejection.
- Invalid cohort/source coverage rejection.
- Preview-token mismatch.
- List/read/detail authorization.
- Cancellation and terminal retry behavior.
- Audit events for creation, completion, failure, cancellation, artifact
  access, and derived-batch creation.

### Artifact tests

- Schema and metadata validation.
- Hash mismatch and corrupted Parquet rejection.
- Duplicate or missing cohort document detection.
- Artifact row-count and database-summary reconciliation.
- Large synthetic cohort processing with bounded memory.
- Retry after each upload/finalization boundary.

### UI tests

- Source selection and preview warnings.
- Peer versus reference metric labels.
- Positive-only and training-overlap warnings.
- Per-class display labels.
- Document filtering and pagination.
- Derived review-batch preview and creation.

### Required commands

Use Pipenv for Python commands:

```text
pipenv run alembic upgrade head
pipenv run pytest tests/test_analysis_result_comparisons.py
pipenv run python scripts/export_openapi.py
```

Then regenerate and verify the web client:

```text
cd web
pnpm run generate:api
pnpm run typecheck
pnpm run lint
pnpm test
```

Exit criterion: the complete comparison lifecycle is reproducible, bounded in
memory, authorized, audited, and leaves no document-level comparison rows in
Core.

## 13. Delivery phases

### Phase A: durable comparison foundation

- Migration and ORM models.
- Preview/create/list/read/cancel contracts.
- Source validation and derivation keys.
- DBOS workflow skeleton and audit events.

### Phase B: artifact computation

- Classifier scoring-artifact reader.
- Review-batch run normalizer.
- Document and class metric calculation.
- Parquet and manifest upload/finalization.
- Failure and reconciliation tests.

### Phase C: comparison workspace

- Comparison creation dialog.
- Overview and class metrics.
- Artifact-backed document detail search.
- Confidence analysis and methodological warnings.

### Phase D: adjudication workflow

- Comparison-result review-batch selection type.
- Preview and durable materialization.
- Links from comparison strata to the resulting batch.

### Phase E: hardening

- Performance tests at whole-matter scale.
- Retention and legal-hold integration.
- Orphan-artifact reconciliation.
- Operational metrics and support diagnostics.
- Deprecation decision for the synchronous batch comparison endpoint.

## 14. MVP completion criteria

The MVP is complete when a matter manager can:

1. Select a completed whole-matter classifier scoring run.
2. Select a completed Jev run on a frozen review batch drawn from that scoring
   population.
3. Create a durable comparison without copying all classifier predictions into
   PostgreSQL.
4. See coverage, exact/partial agreement, per-class results, confidence bands,
   training overlap, and methodological warnings.
5. Filter and inspect disagreement documents through an artifact-backed API.
6. Create a reproducible adjudication batch from a selected disagreement
   stratum.

The database contains one comparison control row plus normal audit records.
Document-level and detailed class results remain in immutable artifacts.
