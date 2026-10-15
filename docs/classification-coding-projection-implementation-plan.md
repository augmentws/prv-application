# Classification Coding Projection Implementation Plan

Status: implemented on the feature branch. The schema, DBOS projection workflow, shared search projection, confidence-range API and UI controls, completed-scoring-run projection dialog, authorization, audit events, generated OpenAPI client, tests, and user/operator documentation are present. Producing a completed full-population scoring artifact remains a prerequisite owned by the classification-scoring lifecycle rather than this projection layer.

Related documents:

- `docs/statistical-classification-backend-implementation-plan.md`
- `docs/statistical-classification-model-infrastructure-plan.md`
- `docs/review-batches.md`
- `docs/search-index-lifecycle.md`

## 1. Outcome

Allow a completed statistical-classification scoring run to expose only a
chosen confidence band as isolated, searchable batch coding without changing
the matter's authoritative coding fields.

The implementation reuses the existing review-run and batch-coding search
infrastructure:

```text
immutable classification prediction artifact
    -> CLASSIFIER review_batch_run
    -> review_batch_run_document
    -> qualifying review_batch_run_value rows
    -> review_batch_search_coding_run
    -> BATCH_CODING_SYNC
    -> OpenSearch batch_coding
```

There is no `classification_prediction_projection` table and no separate
`classification_predictions` OpenSearch root field in this slice.

## 2. Locked decisions

1. Add `CLASSIFIER` as a distinct `review_batch_run.run_type`.
2. A classifier projection belongs to a frozen review batch. A completed
   batch- or matter-scoped analysis run may derive one or more new review
   batches directly from its immutable prediction artifact.
3. The projection batch must be a verified subset of the classification
   scoring population. Document count alone is not sufficient proof.
4. The immutable prediction artifact remains the complete authoritative
   classifier output.
5. Each confidence window creates a new immutable `CLASSIFIER` batch run. It
   does not rerun inference or mutate an earlier projection run.
6. Only successful, non-abstained, resolved class predictions inside the
   requested confidence window become `review_batch_run_value` rows.
   Successful rows without a projected value and abstentions remain completed
   evaluations with **No value**; only feature-excluded rows are **Skipped**,
   and runtime failures are **Failed**.
7. The lower confidence bound is inclusive and the optional upper bound is
   exclusive: `[minimum_confidence, maximum_confidence)`.
8. Confidence bounds are configurable values between zero and one. The
   platform does not impose a universal `0.5` cutoff.
9. Each materialized value retains its class-specific confidence and
   confidence kind.
10. `review_batch_search_coding_run` stores the currently selected run,
    `review_batch_search_coding_projection` tracks every retained run projection,
    and `search_projection_operation`, `BATCH_CODING_SYNC`, and the existing
    nested `batch_coding` mapping remain the search-projection path.
11. Only one run is the selected searchable coding run for a batch at a time.
12. `CLASSIFIER` runs cannot be promoted directly to matter coding. Human or
    agent review remains a separate run and publication remains explicit.
13. Confidence-range filtering is shared by JEV, classifier, agent, and any
    other batch-run values that contain confidence.
14. Missing confidence never matches an active confidence range.
15. Creating downstream review batches from selected classes and confidence
    bands is supported; direct classifier-to-matter publication remains out of
    scope.

## 3. Scope and prerequisites

### In scope

- Classifier projection-run schema and provenance.
- Durable artifact-to-batch-value materialization.
- Exact population validation.
- Confidence-window and optional class selection.
- Existing batch-coding OpenSearch projection reuse.
- Minimum and maximum confidence search filters.
- Shared confidence controls in the batch filter UI.
- Status, counts, retry, audit, authorization, and documentation.

### Out of scope

- Updating `metadata_event` or `document_metadata_current`.
- Publishing classifier predictions to the target metadata field.
- Creating review batches from projected search results.
- CAL acquisition policies or queues.
- Rerunning model inference for each confidence band.
- Indexing the full probability vector.
- Comparing confidence values across different models or providers as if they
  were identically calibrated.

### Prerequisites

- A completed or completed-with-errors `classification_scoring_run`.
- A verified prediction artifact and content hash.
- A ready review batch whose membership is a verified subset of the scoring
  population, or a request to derive a new batch from the artifact.
- The batch's matter and the scoring run's matter must match.
- The scoring run's target metadata definition must be present in the batch's
  coding configuration and remain compatible.

The supported entire-matter workflow is:

1. Score the frozen matter population once.
2. Select a confidence range and optional classes from the completed job.
3. Create one or more derived review batches from the immutable prediction
   artifact.
4. Materialize the corresponding classifier values as isolated Batch Coding.

This avoids attempting to reconstruct an earlier matter population after
documents have been added or removed.

## 4. Step 1: Schema and migration

### `review_batch_run`

Extend the run type constraint and API literal with `CLASSIFIER`.

Add a nullable, indexed `classification_scoring_run_id` foreign key. Enforce
the run-source shape:

- `HUMAN`: human actor only.
- `AGENT`: agent-definition version only.
- `WORKFLOW`: workflow-run record only.
- `CLASSIFIER`: classification-scoring run only.

Add a nullable `derivation_key` for immutable classifier projection identity.
Add a nullable `idempotency_key` for request identity. Create classifier-only
partial unique constraints for `(review_batch_id, idempotency_key)` and for:

```text
review_batch_id
classification_scoring_run_id
derivation_key
```

Add a `result_summary` JSON document for finalized projection counts. Do not
write results into the immutable `configuration_snapshot`.

### Configuration snapshot

A `CLASSIFIER` run freezes:

```text
contract_version
classification_model_id
model_version_id
classification_scoring_run_id
dataset_snapshot_id
prediction_artifact_id
prediction_content_hash
target_metadata_definition_id
ordered_class_keys
selected_class_keys
minimum_confidence
maximum_confidence
confidence_interval_semantics
prediction_resolution_policy_hash
batch_membership_hash
```

### Migration verification

- Upgrade existing databases without changing current runs.
- Reject invalid source-column combinations.
- Downgrade only after verifying the temporary database target.
- Update generated OpenAPI types after the API schema changes.

Exit criterion: existing HUMAN, AGENT, and WORKFLOW runs remain valid, and the
database can persist a provenance-complete CLASSIFIER run.

## 5. Step 2: Contracts, validation, and derivation

Add a classifier projection request:

```text
classification_scoring_run_id
minimum_confidence
maximum_confidence optional
selected_class_keys optional
idempotency_key
```

Validation rules:

- `0 <= minimum_confidence <= 1`.
- When supplied, `minimum_confidence < maximum_confidence <= 1`.
- Selected class keys must belong to the scoring artifact's frozen ordered
  class set.
- The scoring run must be terminal and have a verified prediction artifact.
- The model, target field, matter, dataset snapshot, and batch must agree.
- Every batch member must belong to the scoring population; a batch may be a
  selected subset.
- The target field must exist in the batch coding snapshot.

The derivation key includes all frozen inputs that change output:

```text
contract version
batch ID and membership hash
scoring-run ID
prediction artifact hash
target-field ID
selected class keys in stable order
minimum and maximum confidence
confidence-boundary semantics
```

Reusing an idempotency key with different inputs fails. Repeating the same
derivation returns the existing run.

Add a focused creation endpoint while continuing to use the existing run read,
list, progress, and coding-search endpoints:

```text
POST /v1/matters/{matter_id}/review-batches/{batch_id}/classifier-runs
```

The POST commits the queued CLASSIFIER run and its DBOS dispatch in one
transaction, then returns immediately. Artifact reading and population
comparison never run in the API request.

Exit criterion: Core can determine whether a requested projection is valid and
whether it already exists without reading mutable model state.

## 6. Step 3: Durable classifier projection workflow

Implement `classification_prediction_project_v1` as a DBOS workflow.

### Stage 1: prepare

1. Reauthorize the initiating user for classification scoring and batch
   configuration.
2. Lock and load the classifier run.
3. Verify the scoring run and Artifact reference.
4. Verify the batch is ready and belongs to the same matter.
5. Verify subset population identity.
6. Move the run from `QUEUED` to `RUNNING`.

### Stage 2: stream and validate predictions

1. Stream the Parquet artifact through Core's Artifact gateway.
2. Verify the stored SHA-256 hash.
3. Verify embedded scoring-run, model-version, and ordered-class identities.
4. Require the existing scoring prediction columns.
5. Process bounded row groups; never load the complete matter into memory.

### Stage 3: select values

For each artifact row:

- Reject documents outside the batch.
- Record row failures and feature exclusions in summary counts.
- Exclude abstained predictions.
- Consider only resolved `predicted_class_keys`.
- Resolve each predicted key's class-specific probability.
- Apply selected-class restrictions when present.
- Keep values satisfying the configured confidence interval.

Strategy semantics:

- Binary: map the resolved positive or negative class to its configured
  destination value and retain that class's probability. Projection is allowed
  only when each binary class maps to exactly one destination value; an
  aggregate binary class that represents multiple field values is ambiguous
  and is rejected.
- Native multiclass: materialize the selected class and its probability.
- One-vs-rest: materialize each resolved positive class whose probability is
  inside the requested interval.

Do not create rows for every class probability.

### Stage 4: materialize isolated batch results

In bounded database chunks:

- Upsert `review_batch_run_document` idempotently.
- Write typed `review_batch_run_value` rows only for selected predictions.
- Set `confidence` to the class-specific probability.
- Set `confidence_kind` to `DERIVED_PROBABILITY`.
- Use stable value ordinals derived from the frozen ordered class keys.
- Never write `metadata_event` or `document_metadata_current`.

Document status and summary rules:

- `COMPLETED`: one or more qualifying values were materialized.
- `SKIPPED`: successfully scored but abstained, outside the interval, or
  outside the selected class set.
- `FAILED`: the scoring artifact reports a row failure.
- Feature exclusions remain separately counted in `result_summary`; map them
  to `SKIPPED` unless the artifact reports failure.

The final summary includes population, successful, projected-document,
projected-value, below-window, above-window, class-filtered, abstained,
feature-excluded, failed, and missing-confidence counts.

### Stage 5: finalize and enqueue search projection

1. Reconcile artifact rows, run-document rows, values, and summary counts.
2. Mark the CLASSIFIER run `COMPLETED` or `COMPLETED_WITH_ERRORS`.
3. Select it through `review_batch_search_coding_run`.
4. Enqueue the existing `BATCH_CODING_SYNC` operation in the same transaction.
5. Record an audit event containing the run, scoring run, model version,
   confidence interval, class restriction, counts, and search-operation ID.

Lock the batch's `review_batch_search_coding_run` row while selecting the new
run. Create or resume that run's independent projection record. Search work is
serialized per matter, while changing the selected pointer does not invalidate
another run that is already queued or syncing.

Exit criterion: a crash at any workflow boundary can be retried without
duplicate values, a second logical run, or partial authoritative coding.

## 7. Step 4: Reuse the existing search projection

Do not add a classifier-specific OpenSearch field. The existing `batch_coding`
nested entries already contain:

```text
batch_id
run_id
field_id
field_key
typed value
confidence
confidence_kind
question_key
```

For classifier values, `question_key` remains null. The selected run ID and
batch ID provide source isolation; the CLASSIFIER run provides scoring and
model provenance.

`BATCH_CODING_SYNC` replaces only entries matching the target `(batch_id,
run_id)` and retains other run IDs for the same batch. The UI and query layer
still expose one selected run at a time. Selecting a retained READY projection
on the active index generation changes only the selected-run pointer. A full
matter-index rebuild reconstructs every retained READY run from Core database
values.

Switching from `[0.90, 1.00]` to `[0.80, 0.90)` selects the second immutable
CLASSIFIER run and projects it once. Switching between previously projected
confidence windows does not delete either run's Core values, search entries,
or prediction artifact.

Exit criterion: classifier coding appears through the same batch filters and
facets as JEV coding, with no new search-source-of-truth behavior.

## 8. Step 5: Confidence range search contracts

Extend `ReviewBatchCodingSearchFilter` with:

```text
minimum_confidence optional, inclusive
maximum_confidence optional, exclusive
```

Compile both bounds inside the same `batch_coding` nested query as the field,
value, batch, and run constraints. This prevents a value from matching one
nested entry while its confidence comes from another.

Rules:

- No bounds: confidence does not affect matching.
- Minimum only: `confidence >= minimum`.
- Maximum only: `confidence < maximum`.
- Both: `minimum <= confidence < maximum`.
- Missing confidence does not match when either bound is active.

Apply the same confidence constraints to batch-coding facet counts and
type-ahead values so displayed counts match document results.

Exit criterion: backend searches and facets return identical boundary behavior
for JEV and CLASSIFIER run values.

## 9. Step 6: Batch-filter UI

Add a `Create classifier projection` action on a completed scoring run. The
dialog selects a compatible ready batch, optional class keys, minimum
confidence, and optional maximum confidence. It must explain that creation
reuses the existing scoring artifact, creates isolated batch coding, and does
not change matter coding.

The UI submits once, closes the dialog after the queued run is returned, and
shows durable run stages and result-summary counts. Browser code must not read
or filter the prediction artifact.

Add confidence controls to each Batch Coding field rather than one global
control, because confidence belongs to a nested field/value result.

Provide:

- Any confidence.
- 90% and above.
- 80% to under 90%.
- 70% to under 80%.
- Custom minimum and optional maximum.

UI requirements:

- Display inclusive/exclusive boundary language.
- Show the current CLASSIFIER run's materialized confidence window.
- Prevent a requested search range from implying that unmaterialized values
  are available.
- State that values without confidence are excluded by an active range.
- Retain confidence filters in the batch workspace search state.
- Make the same control available for JEV data with confidence.
- Show `confidence_kind` in document history/detail where a score is shown.
- Do not imply that JEV confidence and classifier probabilities are calibrated
  equivalently.

Exit criterion: users can select non-overlapping confidence bands in the UI
and get results consistent with the active projection run.

## 10. Step 7: Run lifecycle and safety boundaries

- List CLASSIFIER runs alongside other batch runs with an explicit source
  label.
- Show scoring-run, model-version, interval, class selection, counts, status,
  and errors.
- Do not display or enable matter-coding promotion for CLASSIFIER runs.
- Reject CLASSIFIER runs at the promotion API even if a caller bypasses the
  UI.
- Allow retry only for the same immutable derivation.
- Cancellation stops future chunks but retains completed audit and artifact
  records.
- Selecting a different searchable run remains an explicit, authorized action.
- A scoring artifact cannot be deleted while a CLASSIFIER run references it.

Exit criterion: classifier projection cannot accidentally become final matter
coding and remains explainable after later model or batch changes.

## 11. Step 8: Authorization and audit

Creating a classifier projection requires both:

- `classification.score` on the matter.
- `batch.configure` on the projection batch.

Selecting the run for search continues to require `batch.configure`.

Audit at least:

- Projection requested.
- Projection completed or failed.
- Searchable classifier run selected.
- Projection canceled or retried.

Audit details use IDs, hashes, bounds, class keys, counts, and safe error codes;
never include document text, complete probability vectors, or signed Artifact
URLs.

Exit criterion: every state-changing operation is authorized in Core and has a
tenant audit record.

## 12. Step 9: Testing

### Domain and migration tests

- All four run-source shapes and invalid combinations.
- Upgrade/downgrade and existing-row compatibility.
- Derivation and idempotency races.
- Exact batch/scoring population match and mismatch.
- Class-key and interval validation.

### Workflow tests

- Binary, multiclass, and one-vs-rest projection.
- Inclusive lower and exclusive upper boundaries.
- No values below the projection window.
- Multiple projection runs from one scoring artifact without repeated
  inference.
- Abstained, excluded, failed, and missing-confidence rows.
- Stable multi-value ordinals.
- Chunk retry after partial database completion.
- Hash, artifact-identity, and population-integrity failures.
- Search operation queued transactionally after finalization.

### Search tests

- JEV minimum/maximum confidence filtering.
- CLASSIFIER minimum/maximum confidence filtering.
- Same-nested-entry correlation among field, value, and confidence.
- Facet counts respect confidence bounds.
- Switching selected runs retains the prior run projection and reuses it when
  it is current for the active index generation.
- Full index rebuild restores every retained READY classifier run.

### UI tests

- Preset and custom confidence ranges.
- Boundary labels and missing-confidence explanation.
- JEV and CLASSIFIER availability.
- CLASSIFIER provenance and counts.
- Absence of classifier promotion controls.

Exit criterion: tests demonstrate that no below-window value reaches Core
batch coding or OpenSearch and that retries cannot duplicate results.

## 13. Step 10: Documentation and rollout

1. Update `docs/review-batches.md` for CLASSIFIER runs and confidence ranges.
2. Update `docs/search-index-lifecycle.md` to confirm rebuild behavior.
3. Update the statistical-classification backend plan and API inventory.
4. Add end-user documentation under `web/content/docs` only when the UI ships.
5. Add contextual help for confidence bands and score interpretation.
6. Regenerate OpenAPI and Orval clients.
7. Run backend lint, full tests, Alembic checks, migration upgrade/downgrade,
   frontend typecheck, lint, tests, and production build.

Roll out in this order:

1. Migration and read-compatible backend.
2. CLASSIFIER workflow and APIs behind authorization.
3. Search confidence maximum support.
4. UI controls and classifier run presentation.
5. Documentation and operational metrics.

## 14. Implementation sequence

1. Schema migration and `CLASSIFIER` contracts.
2. Projection request validation and deterministic derivation.
3. Artifact streaming, subset population validation, and bounded selection.
4. Idempotent run-document and run-value materialization.
5. Finalization plus existing search-projection dispatch.
6. Maximum-confidence query and facet support.
7. Shared JEV/CLASSIFIER confidence UI.
8. Lifecycle safety, authorization, audit, and retention guards.
9. Full backend/frontend test matrix.
10. User and operator documentation.

## 15. Completion criteria

The slice is complete when an authorized administrator can take one completed
classification scoring artifact for a frozen batch and create successive
searchable classifier coding runs such as:

```text
run A: [0.90, 1.00]
run B: [0.80, 0.90)
run C: [0.70, 0.80)
```

without rerunning inference, materializing lower-confidence values into a
higher-confidence run, changing matter coding, or introducing a second search
projection framework. JEV and classifier batch coding must share the same
confidence-range filters and boundary semantics.
